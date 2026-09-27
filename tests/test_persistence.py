"""The anti-surrender layer: obstructions, difficulty gating, and the refusals that stop a run from
quietly ending with nothing recorded."""
import json

import pytest
from typer.testing import CliRunner

from quod.cli import app
from quod.models import Branch, Claim, Epoch, Evidence, FailedStrategy, Obstruction, Rigor
from quod.state import StateError, StateStore

runner = CliRunner()


def run(*args):
    r = runner.invoke(app, list(args))
    return r.exit_code, r.stdout


@pytest.fixture
def store(root):
    with StateStore(root / "state" / "t.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        yield s


def ob(**kw):
    base = dict(id="O1", branch_id="B", kind="missing_mathlib",
                statement="Mathlib has no surface measure on the 2-sphere as a Hausdorff measure restriction",
                unblock="a lemma giving μH[2] restricted to sphere 0 1 as a Measure, with its total mass")
    base.update(kw)
    return Obstruction(**base)


# -- obstruction records ----------------------------------------------------------------------
def test_obstruction_demands_specificity():
    with pytest.raises(ValueError):
        ob(statement="too hard")
    with pytest.raises(ValueError):
        ob(unblock="this is impossible with current methods and tools")
    # a conditional use of the word is fine: it names something concrete
    ob(unblock="nothing short of a new zero-density estimate; feasible if Mathlib gains Vinogradov bounds")


def test_obstruction_lifecycle(store):
    store.add_obstruction(ob())
    with pytest.raises(StateError):
        store.add_obstruction(ob())
    assert len(store.obstructions("B", status="open")) == 1
    with pytest.raises(StateError):
        store.clear_obstruction("O1", "done")
    cleared = store.clear_obstruction("O1", "proved the restriction lemma in Quod/Sphere.lean")
    assert cleared.status == "cleared"
    assert store.obstructions("B", status="open") == []


# -- closing a branch requires recorded work --------------------------------------------------
def test_close_refused_without_recorded_work(store):
    with pytest.raises(StateError) as e:
        store.set_branch_status("B", "closed", "no progress")
    assert "failed strategies" in str(e.value) and "obstruction" in str(e.value)

    store.record_failure("B", FailedStrategy(strategy="contour shift", reason="Mathlib lacks the functional equation"))
    store.record_failure("B", FailedStrategy(strategy="explicit formula", reason="needs unavailable zero-counting bound"))
    with pytest.raises(StateError):
        store.set_branch_status("B", "closed", "still nothing")
    store.add_obstruction(ob())
    assert store.set_branch_status("B", "closed", "blocked on O1").status == "closed"


def test_proved_and_refuted_are_never_gated(store):
    assert store.set_branch_status("B", "proved", "kernel accepted the target").status == "proved"
    assert store.set_branch_status("B", "refuted", "counterexample at n=4").status == "refuted"


def test_close_force_is_available_to_a_human(store):
    assert store.set_branch_status("B", "abandoned", "human override", force=True).status == "abandoned"


# -- epoch activity ---------------------------------------------------------------------------
def test_epoch_activity_counts_recorded_work(store):
    store.start_epoch(Epoch(branch_id="B", model="test"))
    assert store.epoch_activity("B")["total"] == 0
    store.add_claim(Claim(id="C", branch_id="B", statement="s"))
    act = store.epoch_activity("B")
    assert act["total"] == 1 and "claim.created" in act["recorded"]


def test_epoch_end_refuses_an_empty_epoch(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    assert run("epoch", "start", "-b", "B", "--model", "test")[0] == 0
    code, _ = run("epoch", "end", "-b", "B", "--reason", "nothing worked")
    assert code == 3
    # recording what blocked the target is enough to end honestly
    assert run("obstruction", "add", "O1", "-b", "B", "--kind", "missing_theory",
               "--statement", "no known route bounds the error term without a zero-density estimate",
               "--unblock", "a Lean formalization of the Vinogradov-Korobov bound")[0] == 0
    assert run("epoch", "end", "-b", "B", "--reason", "obstruction recorded")[0] == 0


def test_epoch_end_force_logs_the_defect(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    assert run("epoch", "start", "-b", "B", "--model", "test")[0] == 0
    assert run("epoch", "end", "-b", "B", "--reason", "broken run", "--force")[0] == 0
    with StateStore() as s:
        assert any(e["type"] == "epoch.empty" for e in s.events("B"))


# -- difficulty gating ------------------------------------------------------------------------
def test_difficulty_starts_easy_and_turns_hard(store):
    d = store.difficulty("B")
    assert d["hard"] is False and d["why"] == []
    store.record_failure("B", FailedStrategy(strategy="direct induction", reason="base case fails for n=0 as stated"))
    d = store.difficulty("B")
    assert d["hard"] is True and "failed_strategies" in d["why"]


def test_declared_hard_branch_is_past_the_gate(store):
    store.create_branch(Branch(id="H", problem="p", objective="o", tags=["open-problem"]))
    assert store.difficulty("H")["hard"] is True


def test_strategy_match_declines_on_an_easy_branch(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    code, out = run("strategy", "match", "-b", "B")
    assert code == 0 and "features:" not in out
    code, out = run("strategy", "match", "-b", "B", "--force")
    assert code == 0 and "features:" in out


# -- programme scoring ------------------------------------------------------------------------
def test_programme_branch_scores_lemmas_and_obstructions(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="P", problem="an open problem", objective="advance it", tags=["programme"]))
        s.start_epoch(Epoch(branch_id="P", model="test"))
        for i in (1, 2):
            s.add_claim(Claim(id=f"L{i}", branch_id="P", statement="lemma",
                              rigor=Rigor.FORMALLY_PROVED, evidence=[Evidence(kind="lean", summary="kernel ok")]))
        s.add_obstruction(ob(branch_id="P"))
        from quod import trace as trace_mod
        a = trace_mod.analyze(s, "P")
        # two proved lemmas (1.0 each) + one open obstruction (0.25); not clamped below a proof
        assert a["programme"] is True
        assert a["progress_points"] == pytest.approx(2.25)
        assert a["lemmas_proved"] == 2 and a["obstructions"]["open"] == 1


def test_non_programme_branch_stays_clamped(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="Q", problem="a putnam problem", objective="prove it"))
        s.start_epoch(Epoch(branch_id="Q", model="test"))
        s.add_claim(Claim(id="T", branch_id="Q", statement="the target", tags=["target"]))
        for i in range(6):
            s.add_claim(Claim(id=f"L{i}", branch_id="Q", statement="lemma",
                              rigor=Rigor.FORMALLY_PROVED, evidence=[Evidence(kind="lean", summary="kernel ok")]))
        from quod import trace as trace_mod
        # 0.5 per lemma over an unproved target, clamped below a proof however many lemmas there are
        assert trace_mod.analyze(s, "Q")["progress_points"] == 3.0


# -- critic mode policy -----------------------------------------------------------------------
def test_kernel_proved_frozen_target_gets_compressed_not_restated():
    from quod.critic import choose_mode
    from quod.models import FormalClaim
    frozen = Claim(id="T", branch_id="B", statement="target", tags=["target"],
                   rigor=Rigor.FORMALLY_PROVED, evidence=[Evidence(kind="lean", summary="ok")],
                   formal=FormalClaim(lean="theorem t : True := by trivial", frozen=["theorem t : True"]))
    assert choose_mode(frozen, []) == "compressor"
    restated = frozen.model_copy(update={"id": "R", "tags": ["target", "restated"]})
    assert choose_mode(restated, []) == "representation"


# -- wall-time accounting ---------------------------------------------------------------------
def test_long_tool_calls_are_not_counted_as_idle(root):
    """A gap between turns is idle only when nothing was running in it: a 20-minute Lean compile is work."""
    from datetime import datetime, timedelta, timezone

    from quod import trace as trace_mod
    t0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="W", problem="p", objective="o", tags=["programme"]))
        ep = s.start_epoch(Epoch(branch_id="W", model="test"))
        s.replace_turns(ep.id, [
            {"idx": 0, "ts": t0.isoformat(), "input": 1, "cache_read": 0, "cache_create": 0, "output": 1, "context": 1},
            {"idx": 1, "ts": (t0 + timedelta(minutes=20)).isoformat(), "input": 1, "cache_read": 0,
             "cache_create": 0, "output": 1, "context": 1},
        ])
        s.add_stream_tool_calls(ep.id, "W", [
            {"ts": (t0 + timedelta(seconds=10)).isoformat(), "seconds": 1180.0, "tool": "Bash",
             "kind": "lean.check", "summary": "lake build", "ok": True},
        ])
        a = trace_mod.analyze(s, "W")
        assert a["phases"]["idle_s"] == 0.0


# -- upstream contributions are not toolbox proposals -------------------------------------------
def test_contribution_lifecycle_and_url_requirement(root):
    from quod.models import Contribution
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        c = s.add_contribution(Contribution(
            target="mathlib", title="three-square theorem", branch_id="B",
            statement="Legendre's three-square theorem: n is a sum of three squares iff n is not 4^a(8b+7)",
            why="the four-square theorem is in Mathlib but the three-square case is not, which blocked a reduction",
            have="claim X, artifact artifacts/B/three_sq.lean"))
        assert c.id == 1 and c.status == "candidate"
        assert [x.id for x in s.contributions(target="mathlib", status="candidate")] == [1]
        with pytest.raises(StateError):
            s.set_contribution_status(1, "submitted")  # needs the url
        with pytest.raises(StateError):
            s.set_contribution_status(1, "merged-ish")
        assert s.set_contribution_status(1, "drafted").status == "drafted"
        done = s.set_contribution_status(1, "submitted", url="https://example.invalid/pull/1")
        assert (done.status, done.url) == ("submitted", "https://example.invalid/pull/1")
        assert s.set_contribution_status(1, "merged").url == "https://example.invalid/pull/1"


def test_contribution_demands_a_precise_statement():
    from quod.models import Contribution
    with pytest.raises(ValueError):
        Contribution(target="mathlib", title="t", statement="missing lemma", why="it would have helped a lot here")


def test_ledger_notes_when_a_proposal_is_really_an_upstream_gap(root):
    assert run("init")[0] == 0
    code, out_ = run("ledger", "add", "infra", "--title", "Mathlib lacks a surface measure on the 2-sphere",
                     "--rationale", "the divergence-theorem route cannot be formalized without it, so the proof "
                                    "needed a 400-line workaround")
    assert code == 0 and "quod contribute add" in out_
    code, out_ = run("ledger", "add", "bug", "--title", "attack hangs on a symbolic sum",
                     "--rationale", "the instance engine has no per-step wall clock and ran for twenty minutes")
    assert code == 0 and "quod contribute" not in out_


def test_obstruction_of_kind_missing_mathlib_points_upstream(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    code, out_ = run("obstruction", "add", "O1", "-b", "B", "--kind", "missing_mathlib",
                     "--statement", "Mathlib has no Legendre three-square theorem, only the four-square one",
                     "--unblock", "a Lean proof of the three-square theorem, or its addition to Mathlib")
    assert code == 0 and "quod contribute add mathlib" in out_
