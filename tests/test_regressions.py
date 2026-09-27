"""Regressions found by running the harness: engine deadlines, ground sums, library indexing, ledger
de-duplication, strategy card lookup by name, and imports surviving attach."""
import pytest
import sympy as sp
from typer.testing import CliRunner

from quod import expand, formal
from quod.cli import app
from quod.library import Library
from quod.models import Branch, Claim, Evidence, FormalClaim, Rigor, ToolboxProposal
from quod.state import StateStore

runner = CliRunner()


def run(*args):
    r = runner.invoke(app, list(args))
    return r.exit_code, r.stdout


# -- #30 ground sums with floor ----------------------------------------------------------------
def test_floor_inside_a_ground_sum_is_decided():
    k = sp.Symbol("k", integer=True)
    total = sum((i - 1) // 4 for i in range(1, 2020))
    fc = FormalClaim(conclusion=f"Eq(Sum(floor((k-1)/4),(k,1,2019)),{total})", variables={"k": "Z"})
    rep = expand.symbolic(fc)
    assert rep.verdict == "verified", rep.details


def test_expand_finite_leaves_symbolic_bounds_alone():
    n, i = sp.symbols("n i", integer=True)
    e = sp.Sum(i, (i, 1, n))
    assert expand.expand_finite(e) == e


# -- #20 per-engine deadline -------------------------------------------------------------------
def test_engine_deadline_raises_and_is_catchable():
    from quod.attack import EngineTimeout, deadline
    with pytest.raises(EngineTimeout):
        with deadline(1, "test"):
            x = 0
            for i in range(10**9):  # long enough that the timer fires first
                x += i * i


# -- #21 library indexes the problem text for imported targets ---------------------------------
def test_library_indexes_problem_text_for_target_claims(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="Lattice points, pigeonhole and parity, Putnam 1971 A1.",
                               objective="prove it"))
        s.add_claim(Claim(id="B-target", branch_id="B", statement="Target statement from putnam putnam_1971_a1.",
                          tags=["target"], rigor=Rigor.FORMALLY_PROVED,
                          evidence=[Evidence(kind="lean", summary="kernel ok")]))
        c, b = s.require_claim("B-target"), s.require_branch("B")
    lib = Library()
    try:
        entry = lib.from_claim(c, problem=b.problem)
        assert "pigeonhole" in entry.statement
        assert [e.id for e in lib.search("pigeonhole parity lattice")] == [entry.id]
    finally:
        lib.close()


# -- #44 ledger de-duplication -----------------------------------------------------------------
def test_ledger_refuses_a_near_duplicate(root):
    assert run("init")[0] == 0
    t = "quod strategy match crashes on branch status done"
    assert run("ledger", "add", "bug", "--title", t, "--rationale", "r" * 30)[0] == 0
    code, _ = run("ledger", "add", "bug", "--title", "strategy match crashes on branch status 'done'",
                  "--rationale", "r" * 30)
    assert code == 3
    assert run("ledger", "append", "1", "--evidence", "seen again on another branch")[0] == 0
    assert run("ledger", "add", "bug", "--title", "strategy match crashes on branch status 'done'",
               "--rationale", "r" * 30, "--force")[0] == 0
    with StateStore() as s:
        assert len(s.proposals()) == 2
        assert "seen again" in s.proposals()[0].evidence


# -- #19/#23 a card is reachable by name as well as by id --------------------------------------
def test_strategy_card_lookup_accepts_the_printed_name(root):
    from quod.strategy import StrategyCard, StrategyStore
    ss = StrategyStore(root / "state" / "library.sqlite3")
    try:
        ss.add(StrategyCard(id="S-foo-bar", name="foo-bar"))
        assert ss.get("S-foo-bar") is not None
        assert ss.get("foo-bar").id == "S-foo-bar"
    finally:
        ss.close()


# -- #39 non-Mathlib imports survive the unwrap/rewrap round trip -------------------------------
def test_unwrap_keeps_non_mathlib_imports():
    src = ("import Mathlib\nimport FormalConjectures.ErdosProblems.«1084»\n\nnamespace Quod.Scratch\n\n"
           "theorem t : True := trivial\n\nend Quod.Scratch\n")
    stored = formal.unwrap(src)
    assert "import FormalConjectures.ErdosProblems.«1084»" in stored
    assert "import Mathlib" not in stored
    rewrapped = formal._wrap(stored, "t")
    assert rewrapped.count("import Mathlib") == 1
    assert "import FormalConjectures.ErdosProblems.«1084»" in rewrapped
    assert "namespace Quod.Scratch" in rewrapped


# -- #36 the kernel budget is configurable ------------------------------------------------------
def test_lean_timeout_comes_from_configuration(root, monkeypatch):
    monkeypatch.setenv("QUOD_LEAN_TIMEOUT", "2400")
    assert formal.default_timeout() == 2400
