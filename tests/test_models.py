import pytest
from pydantic import ValidationError

from quod.models import Claim, Evidence, FailedStrategy, Rigor


def claim(rigor=Rigor.SPECULATIVE, kinds=()):
    return Claim(id="C", branch_id="B", statement="s", rigor=rigor, evidence=[Evidence(kind=k, summary=k) for k in kinds])


def test_promotion_requires_matching_evidence():
    assert claim().promotion_error(Rigor.EMPIRICAL) is not None
    assert claim(kinds=["numeric"]).promotion_error(Rigor.EMPIRICAL) is None
    assert claim(kinds=["numeric"]).promotion_error(Rigor.FORMALLY_PROVED) is not None
    assert claim(kinds=["lean"]).promotion_error(Rigor.FORMALLY_PROVED) is None


def test_rigor_only_moves_up():
    c = claim(Rigor.SYMBOLICALLY_VERIFIED, ["cas", "numeric"])
    assert "only moves up" in c.promotion_error(Rigor.EMPIRICAL)
    assert "already" in c.promotion_error(Rigor.SYMBOLICALLY_VERIFIED)
    c2 = claim(Rigor.SYMBOLICALLY_VERIFIED, ["cas", "smt"])
    assert c2.promotion_error(Rigor.SOLVER_VERIFIED) is not None  # lateral peer move rejected
    assert c.promotion_error(Rigor.SPECULATIVE) is not None


def test_refuted_is_terminal_and_proved_is_unrefutable():
    assert claim(Rigor.REFUTED, ["counterexample"]).promotion_error(Rigor.EMPIRICAL) is not None
    assert "ledger" in claim(Rigor.FORMALLY_PROVED, ["lean", "counterexample"]).promotion_error(Rigor.REFUTED)
    assert claim(Rigor.SOLVER_VERIFIED, ["smt", "counterexample"]).promotion_error(Rigor.REFUTED) is None


def test_failed_strategy_reason_must_be_specific():
    with pytest.raises(ValidationError):
        FailedStrategy(strategy="x", reason="failed")
    FailedStrategy(strategy="x", reason="the induction step needs a lemma we do not have")
