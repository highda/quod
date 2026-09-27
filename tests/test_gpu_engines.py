"""The Metal-backed engines: array search, SOS certification, closed-form discovery, premise retrieval,
the local-prover guards, and the two formal-layer costs (import narrowing, verdict cache)."""
import json

import pytest
import sympy as sp
from typer.testing import CliRunner

from quod import conjecture as cj, formal, gpu, neural, premise, search as search_mod, sos
from quod.cli import app
from quod.models import Branch, Claim, FormalClaim
from quod.state import StateStore

runner = CliRunner()


def run(*args):
    r = runner.invoke(app, list(args))
    return r.exit_code, r.stdout


def fc(concl, **vars_):
    return FormalClaim(variables=vars_ or {"x": "R", "y": "R"}, conclusion=concl)


# -- array backend ------------------------------------------------------------------------------
def test_backend_reports_which_one_ran():
    b = gpu.backend(prefer_gpu=False)
    assert b.name == "numpy-cpu"
    assert gpu.backend(prefer_gpu=True).name in ("mlx-gpu", "numpy-cpu")


def test_unsupported_expressions_are_refused_not_approximated():
    b = gpu.backend(prefer_gpu=False)
    x = sp.Symbol("x")
    with pytest.raises(gpu.Unsupported):
        gpu.compile_expr(sp.zeta(x), [x], b)
    with pytest.raises(gpu.Unsupported):
        gpu.relation_parts(sp.Symbol("p"))


def test_compiled_expression_matches_sympy():
    b = gpu.backend(prefer_gpu=False)
    x, y = sp.symbols("x y")
    f = gpu.compile_expr(x ** 2 - 2 * x * y + y ** 2, [x, y], b)
    cols = [b.asarray([1.0, 3.0, -2.0]), b.asarray([1.0, 1.0, 5.0])]
    assert b.to_list(f(cols)) == [0.0, 4.0, 49.0]


# -- search -------------------------------------------------------------------------------------
def test_search_finds_and_exactly_rechecks_a_counterexample():
    rep = search_mod.run(fc("Ge(n**2, n**3)", n="N+"), mode="counterexample", box={"n": (1, 50)})
    assert rep.counterexamples and rep.counterexamples[0]["n"] == 2
    assert rep.exhausted is True


def test_search_witness_mode_respects_hypotheses():
    f = FormalClaim(variables={"n": "N+"}, hypotheses=["Ge(n, 10)"], conclusion="Eq(Mod(n, 7), 0)")
    rep = search_mod.run(f, mode="witness", box={"n": (1, 40)}, want=3)
    assert [w["n"] for w in rep.witnesses] == [14, 21, 28]


def test_search_declines_what_it_cannot_evaluate():
    rep = search_mod.run(fc("Ge(zeta(x), 0)", x="R"), mode="counterexample", box={"x": (1, 10)})
    assert rep.unsupported and "zeta" in rep.unsupported


def test_search_records_refutation_and_gpu_evidence(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="bad", branch_id="B", statement="n^2 >= n^3", formal=fc("Ge(n**2, n**3)", n="N+")))
        s.add_claim(Claim(id="ok", branch_id="B", statement="n^2 >= 2n-1", formal=fc("Ge(n**2, 2*n - 1)", n="Z")))
        bad = search_mod.attack(s, "bad", box={"n": (1, 20)})
        assert bad["refuted"] is True and s.require_claim("bad").rigor == "refuted"
        good = search_mod.attack(s, "ok", box={"n": (-50, 50)})
        assert "gpu_search" in good["evidence"]
        c = s.require_claim("ok")
        assert c.evidence_kinds() == {"gpu_search"} and c.rigor == "speculative"  # evidence alone never promotes


def test_gpu_search_evidence_cannot_climb_past_empirical(root):
    from quod.models import Evidence, Rigor
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="C", branch_id="B", statement="s", formal=fc("Ge(x**2, 0)")))
        s.add_evidence("C", Evidence(kind="gpu_search", summary="10^6 candidates, no counterexample"))
        assert s.promote("C", Rigor.EMPIRICAL).rigor == Rigor.EMPIRICAL
        for target in (Rigor.NUMERICALLY_CERTIFIED, Rigor.SYMBOLICALLY_VERIFIED, Rigor.FORMALLY_PROVED):
            with pytest.raises(Exception):
                s.promote("C", target)


# -- sum of squares -----------------------------------------------------------------------------
@pytest.mark.parametrize("concl,expect_terms", [
    ("Ge(x**2 + y**2, 2*x*y)", 1),
    ("Le(2*x*y, x**2 + y**2)", 1),
    ("Ge(x**4 + y**4, 2*x**2*y**2)", 1),
])
def test_sos_certifies_true_inequalities_exactly(concl, expect_terms):
    rep = sos.certify(fc(concl))
    assert rep.verdict == "certified"
    assert len(rep.squares) == expect_terms
    assert rep.residual == 0
    assert rep.lean_hint.startswith("nlinarith [sq_nonneg")


def test_sos_certificate_is_an_exact_rational_identity():
    rep = sos.certify(fc("Ge(x**2 + y**2 + 1, x*y + x + y)"))
    assert rep.verdict == "certified"
    # the engine works over domain-assumed symbols; a plain Symbol("x") is a *different* symbol and nothing
    # would cancel against it
    x, y = sp.Symbol("x", real=True), sp.Symbol("y", real=True)
    target = sp.expand(x ** 2 + y ** 2 + 1 - (x * y + x + y))
    assert sp.expand(target - sum(c * s ** 2 for c, s in rep.squares)) == 0
    assert all(c > 0 for c, _ in rep.squares)
    assert all(q.is_Rational for q, _ in rep.squares)


def test_sos_refuses_a_false_inequality_and_odd_degree():
    assert sos.certify(fc("Ge(x**2 - 1, 0)")).verdict == "not_found"
    assert sos.certify(fc("Ge(x**3, 0)")).verdict == "unsupported"
    assert sos.certify(FormalClaim(variables={"x": "R"}, hypotheses=["Gt(x, 0)"],
                                   conclusion="Ge(x, 0)")).verdict == "unsupported"


def test_sos_records_cas_evidence(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="C", branch_id="B", statement="amgm", formal=fc("Ge(x**2 + y**2, 2*x*y)")))
        res = sos.attack(s, "C")
        assert res["verdict"] == "certified"
        assert s.require_claim("C").evidence_kinds() == {"cas"}


# -- closed forms -------------------------------------------------------------------------------
def test_conjecture_finds_known_shapes():
    assert cj.render(cj.analyze([0, 1, 4, 9, 16, 25, 36, 49]))["best"] == "n**2"
    assert cj.render(cj.analyze([1, 2, 4, 8, 16, 32, 64, 128]))["best"] == "2**n"
    assert "factorial" in cj.render(cj.analyze([1, 1, 2, 6, 24, 120, 720]))["best"]
    fib = cj.analyze([0, 1, 1, 2, 3, 5, 8, 13, 21, 34])
    assert any(c.kind == "linear-recurrence" for c in fib.candidates)


def test_conjecture_invents_nothing_from_noise():
    rep = cj.analyze([3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5])
    assert rep.candidates == []
    assert cj.render(rep)["best"] is None


def test_conjecture_candidates_are_validated_beyond_the_fit():
    rep = cj.analyze([0, 1, 4, 9, 16, 25, 36, 49])
    poly = next(c for c in rep.candidates if c.kind == "polynomial")
    assert poly.validated_on > 0 and poly.formal() is not None


# -- premise index ------------------------------------------------------------------------------
def _fake_mathlib(root):
    d = root / "formal" / ".lake" / "packages" / "mathlib" / "Mathlib" / "Algebra"
    d.mkdir(parents=True)
    (d / "Odd.lean").write_text(
        "theorem sum_range_odd (n : ℕ) : ∑ k ∈ Finset.range n, (2 * k + 1) = n ^ 2 := by\n  simp\n\n"
        "lemma odd_mul_odd {a b : ℕ} (ha : Odd a) (hb : Odd b) : Odd (a * b) := by\n  exact ha.mul hb\n")
    (d / "Prime.lean").write_text("theorem prime_two : Nat.Prime 2 := by norm_num\n")
    (root / "formal" / "lake-manifest.json").write_text(json.dumps(
        {"packages": [{"name": "mathlib", "rev": "abcdef1234567890"}]}))
    return d


def test_declaration_extraction(root):
    d = _fake_mathlib(root)
    decls = premise.declarations(d / "Odd.lean")
    assert [n for n, _ in decls] == ["sum_range_odd", "odd_mul_odd"]
    assert "Finset.range" in decls[0][1]


def test_index_is_keyed_to_the_revision_and_is_incremental(root):
    _fake_mathlib(root)
    first = premise.build()
    assert first["declarations"] == 3 and first["files_reindexed"] == 2
    assert first["mathlib_rev"] == "abcdef123456"
    again = premise.build()
    assert again["files_reindexed"] == 0          # nothing changed, nothing re-read
    assert premise.status()["built"] is True


def test_lexical_search_returns_relevant_declarations(root):
    _fake_mathlib(root)
    premise.build()
    hits = premise.search("Finset range sum", k=3)["hits"]
    assert hits and hits[0]["name"] == "sum_range_odd"
    assert premise.search("Odd mul", k=3)["hits"][0]["name"] in ("odd_mul_odd", "sum_range_odd")


def test_search_without_an_index_says_so(root):
    _fake_mathlib(root)
    assert "error" in premise.search("anything")


# -- local prover guards ------------------------------------------------------------------------
def test_local_prover_is_off_until_configured(root, monkeypatch):
    monkeypatch.delenv("QUOD_PROVER_MODEL", raising=False)
    rep = neural.availability()
    assert rep.available is False and "prover_model" in rep.reason


def test_local_prover_refuses_while_lean_is_building(root, monkeypatch):
    monkeypatch.setenv("QUOD_PROVER_MODEL", "some/model")
    monkeypatch.setattr(neural.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(neural, "lake_running", lambda: True)
    rep = neural.availability()
    assert rep.available is False and "lake" in rep.reason


def test_local_prover_refuses_under_memory_pressure(root, monkeypatch):
    monkeypatch.setenv("QUOD_PROVER_MODEL", "some/model")
    monkeypatch.setattr(neural.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(neural, "lake_running", lambda: False)
    monkeypatch.setattr(neural, "free_memory_gb", lambda: 1.0)
    rep = neural.availability(min_free_gb=5.0)
    assert rep.available is False and "free" in rep.reason


def test_candidate_extraction_from_model_output():
    assert neural.extract_lean("blah\n```lean\ntheorem t : True := trivial\n```\n") == "theorem t : True := trivial"
    assert neural.extract_lean("theorem t : True := trivial") == "theorem t : True := trivial"
    assert neural.extract_lean("I could not do it") is None


# -- formal layer costs -------------------------------------------------------------------------
def test_narrow_imports_are_derived_from_the_snippet():
    mods = formal.narrow_imports("theorem t (n : ℕ) : ∑ k ∈ Finset.range n, k = n * (n-1) / 2 := by simp")
    assert "Mathlib.Tactic" in mods and any("BigOperators" in m for m in mods)
    assert formal.narrow_imports("theorem t : True := trivial") == ["Mathlib.Tactic"]


def test_verdict_cache_round_trip(root):
    (root / "formal").mkdir(exist_ok=True)
    (root / "formal" / "lean-toolchain").write_text("leanprover/lean4:v4.33.1")
    src = "import Mathlib\ntheorem t : True := trivial\n"
    assert formal.cached_verdict(src) is None
    formal.store_verdict(src, formal.LeanReport("proved", "kernel accepted t"))
    hit = formal.cached_verdict(src)
    assert hit and hit.verdict == "proved"
    formal.store_verdict(src + " ", formal.LeanReport("failed", "lean timed out after 900s"))
    assert formal.cached_verdict(src + " ") is None          # a timeout says nothing; it is not cached


def test_same_source_detects_a_pointless_second_compile():
    stored = "open Polynomial\n\ntheorem t : True := trivial"
    compiled = formal._wrap(stored, "t") + "\n#print axioms Quod.Scratch.t\n"
    assert formal.same_source(compiled, stored, "t") is True
    assert formal.same_source("import Mathlib\ntheorem other : True := trivial", stored, "t") is False


# -- CLI ----------------------------------------------------------------------------------------
def test_cli_exposes_the_new_engines(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    assert run("claim", "add", "C", "-b", "B", "--statement", "amgm",
               "--formal", json.dumps({"variables": {"x": "R", "y": "R"},
                                       "conclusion": "Ge(x**2 + y**2, 2*x*y)"}))[0] == 0
    code, out = run("certify", "C")
    assert code == 0 and json.loads(out)["verdict"] == "certified"
    code, out = run("conjecture", "0,1,4,9,16,25")
    assert code == 0 and json.loads(out)["best"] == "n**2"
    code, out = run("search", "C", "--lo", "-20", "--hi", "20")
    assert code == 0 and json.loads(out)["counterexamples"] == []
