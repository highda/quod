import pytest

from quod.models import Branch, BranchCheckpoint, Claim, Epoch, Evidence, FailedStrategy, Rigor, ToolboxProposal
from quod.state import StateError, StateStore


@pytest.fixture
def store(root):
    with StateStore(root / "state" / "t.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        yield s


def test_claim_requires_branch(store):
    with pytest.raises(StateError):
        store.add_claim(Claim(id="X", branch_id="nope", statement="s"))


def test_promote_enforced(store):
    store.add_claim(Claim(id="C", branch_id="B", statement="s"))
    with pytest.raises(StateError):
        store.promote("C", Rigor.EMPIRICAL)
    store.add_evidence("C", Evidence(kind="numeric", summary="ok"))
    assert store.promote("C", Rigor.EMPIRICAL).rigor == Rigor.EMPIRICAL
    with pytest.raises(StateError):
        store.promote("C", Rigor.FORMALLY_PROVED)
    store.refute("C", "n=0", "test")
    assert store.get_claim("C").rigor == Rigor.REFUTED
    with pytest.raises(StateError):
        store.promote("C", Rigor.EMPIRICAL)


def test_cannot_create_at_rigor_without_evidence(store):
    with pytest.raises(StateError):
        store.add_claim(Claim(id="C", branch_id="B", statement="s", rigor=Rigor.EMPIRICAL))
    store.add_claim(Claim(id="C", branch_id="B", statement="s", rigor=Rigor.EMPIRICAL,
                          evidence=[Evidence(kind="numeric", summary="x")]))


def test_supersedes_and_depends_must_exist(store):
    with pytest.raises(StateError):
        store.add_claim(Claim(id="C", branch_id="B", statement="s", supersedes="ghost"))
    with pytest.raises(StateError):
        store.add_claim(Claim(id="C", branch_id="B", statement="s", depends_on=["ghost"]))


def test_epochs_and_auto_checkpoint(store):
    store.start_epoch(Epoch(branch_id="B", model="opus"))
    store.checkpoint(BranchCheckpoint(branch_id="B", objective="o", open_goals=["g"], next_actions=["a", "b", "c"]))
    store.record_failure("B", FailedStrategy(strategy="s", reason="a specific reason of enough length"))
    cp = store.auto_checkpoint("B", "compaction")
    assert cp.auto and cp.open_goals == ["g"] and cp.next_actions == ["a", "b", "c"]
    assert len(cp.failed_strategies) == 1
    assert store.latest_checkpoint("B", include_auto=False).auto is False
    ep = store.end_epoch("B", "stop", tokens_in=10, tokens_out=5)
    assert ep.tokens_in == 10 and ep.end_reason == "stop"
    assert store.current_epoch("B") is None


def test_ledger(store):
    p = store.propose(ToolboxProposal(kind="tool", title="t", rationale="r"))
    assert p.id == 1
    assert store.set_proposal_status(1, "accepted").status == "accepted"
    assert store.proposals("accepted")[0].title == "t"


def test_branch_status_validated(store):
    with pytest.raises(StateError):
        store.set_branch_status("B", "done", "agent typo")
    with pytest.raises(StateError):  # closing needs recorded work (see test_persistence)
        store.set_branch_status("B", "closed", "ok")
    assert store.set_branch_status("B", "closed", "ok", force=True).status == "closed"
