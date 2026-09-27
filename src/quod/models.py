"""Typed research objects.

The rigor ladder is enforced here, not in prose: a claim can only move to a rigor whose
required evidence kind is present, only upward, and `refuted` is terminal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Rigor(StrEnum):
    SPECULATIVE = "speculative"
    EMPIRICAL = "empirical"
    NUMERICALLY_CERTIFIED = "numerically_certified"
    SYMBOLICALLY_VERIFIED = "symbolically_verified"
    SOLVER_VERIFIED = "solver_verified"
    FORMALLY_PROVED = "formally_proved"
    REFUTED = "refuted"


# Partial order. symbolic and solver are peers: neither dominates the other.
RIGOR_RANK: dict[Rigor, int] = {
    Rigor.SPECULATIVE: 0,
    Rigor.EMPIRICAL: 1,
    Rigor.NUMERICALLY_CERTIFIED: 2,
    Rigor.SYMBOLICALLY_VERIFIED: 3,
    Rigor.SOLVER_VERIFIED: 3,
    Rigor.FORMALLY_PROVED: 4,
    Rigor.REFUTED: 99,
}

# Which evidence kinds are allowed to carry a claim to a given rigor.
REQUIRED_EVIDENCE: dict[Rigor, set[str]] = {
    # GPU, neural and floating-point evidence lives here and cannot climb: a network's output, an annealing
    # run's best configuration and a PINN residual are discoveries, never certificates. They leave this rung
    # only when an exact engine or the kernel re-derives the same fact.
    Rigor.EMPIRICAL: {"numeric", "instance_check", "finite_difference", "sampling", "gpu_batch",
                      "gpu_search", "neural_numeric", "neural_suggestion"},
    Rigor.NUMERICALLY_CERTIFIED: {"interval", "certified_numeric"},
    Rigor.SYMBOLICALLY_VERIFIED: {"cas"},
    Rigor.SOLVER_VERIFIED: {"smt"},
    Rigor.FORMALLY_PROVED: {"lean"},
    Rigor.REFUTED: {"counterexample"},
}

Domain = Literal["N", "N+", "Z", "Q", "R", "R+", "C"]


class FormalClaim(BaseModel):
    """A claim the deterministic engine can read.

    `variables` maps a symbol name to a domain. `hypotheses` and `conclusion` are SymPy-parsable
    expressions over those symbols (see docs/CLAIMS.md). `lean` is an optional Lean 4 statement
    (a `theorem ... := by` block) that the formal layer will try to check.
    """

    variables: dict[str, Domain] = Field(default_factory=dict)
    hypotheses: list[str] = Field(default_factory=list)
    conclusion: str = ""
    lean: str | None = None
    # Frozen declaration signatures (from `quod source open`): a proof may fill `sorry`s but must not alter these.
    frozen: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_content(self) -> "FormalClaim":
        if not self.conclusion.strip() and not (self.lean or "").strip():
            raise ValueError("a formal block needs a SymPy `conclusion`, a `lean` statement, or both")
        return self

    @property
    def engine_readable(self) -> bool:
        return bool(self.conclusion.strip())


class Evidence(BaseModel):
    kind: str
    summary: str
    artifact: str | None = None
    produced_by: str = "unknown"
    metadata: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)


class Claim(BaseModel):
    id: str
    branch_id: str
    statement: str
    formal: FormalClaim | None = None
    rigor: Rigor = Rigor.SPECULATIVE
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    def evidence_kinds(self) -> set[str]:
        return {e.kind for e in self.evidence}

    def promotion_error(self, target: Rigor) -> str | None:
        """Return None if the claim may move to `target`, else the exact reason it may not."""
        if self.rigor == Rigor.REFUTED:
            return "refuted is terminal; a changed statement is a new claim (use --supersedes)"
        if target == Rigor.SPECULATIVE:
            return "cannot demote to speculative"
        if target == Rigor.REFUTED:
            if self.rigor == Rigor.FORMALLY_PROVED:
                return "claim is formally proved; a refutation means the Lean statement or the engine is wrong — record a ledger entry instead"
            needed = REQUIRED_EVIDENCE[target]
        else:
            if target == self.rigor:
                return f"already at {target}"
            if RIGOR_RANK[target] <= RIGOR_RANK[self.rigor]:
                return f"cannot move {self.rigor} -> {target}; rigor only moves up (peers are lateral)"
            needed = REQUIRED_EVIDENCE[target]
        if not (needed & self.evidence_kinds()):
            return f"{target} requires evidence of kind {sorted(needed)}; present: {sorted(self.evidence_kinds())}"
        return None


class FailedStrategy(BaseModel):
    strategy: str
    reason: str
    obstruction: str | None = None
    artifact: str | None = None
    created_at: str = Field(default_factory=utcnow)

    @field_validator("reason")
    @classmethod
    def _specific(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("reason must be specific (>= 20 chars); 'approach failed' is not state")
        return v


ObstructionKind = Literal[
    "missing_mathlib",      # the result exists in the literature but not in Mathlib
    "missing_theory",       # no known mathematics closes this step
    "complexity",           # the route is correct but the computation/search is out of reach
    "dependency",           # blocked on an unproved claim of ours
    "formalization",        # known on paper; the obstacle is expressing/automating it in Lean
    "statement",            # the target statement itself is not yet pinned down formally
]


class Obstruction(BaseModel):
    """A precise, durable record of what blocks progress — the only permitted alternative to a proof.

    An obstruction is a *result*: it names the blocked step, what was tried, and what would remove it.
    "Too hard" is not an obstruction; "Mathlib has no surface measure on S², so the route via the
    divergence theorem cannot be formalized until `μH[2]` restricted to `sphere 0 1` exists" is.
    """

    id: str
    branch_id: str
    kind: ObstructionKind
    statement: str          # what exactly is blocked
    tried: list[str] = Field(default_factory=list)
    unblock: str            # the concrete thing that would remove it
    claim_id: str | None = None
    status: Literal["open", "cleared"] = "open"
    cleared_how: str | None = None
    created_at: str = Field(default_factory=utcnow)

    @field_validator("statement", "unblock")
    @classmethod
    def _specific(cls, v: str) -> str:
        if len(v.strip()) < 25:
            raise ValueError("obstructions must be specific (>= 25 chars): name the blocked step and what would remove it")
        return v

    @field_validator("unblock")
    @classmethod
    def _not_despair(cls, v: str) -> str:
        bad = ("impossible", "too hard", "beyond", "hopeless", "cannot be done", "give up", "not feasible")
        if any(b in v.lower() for b in bad) and " if " not in v.lower():
            raise ValueError(f"`unblock` must name a concrete thing that would remove the obstruction, not a verdict on difficulty ({v!r})")
        return v


AttackOutcome = Literal["survived", "refuted", "weakened", "inconclusive"]


class AttackReport(BaseModel):
    claim_id: str
    branch_id: str
    attacker: str  # "engine:<tool>" or "model:<name>"
    kind: str  # instance_check | smt | cas | known_result | hidden_assumption | llm_critique
    outcome: AttackOutcome
    details: str
    artifact: str | None = None
    created_at: str = Field(default_factory=utcnow)


class BranchCheckpoint(BaseModel):
    branch_id: str
    epoch_id: int | None = None
    objective: str
    established_claims: list[str] = Field(default_factory=list)
    open_goals: list[str] = Field(default_factory=list)
    active_assumptions: list[str] = Field(default_factory=list)
    obstructions: list[str] = Field(default_factory=list)
    failed_strategies: list[FailedStrategy] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    auto: bool = False
    created_at: str = Field(default_factory=utcnow)


BranchStatus = Literal["open", "proved", "refuted", "closed", "abandoned"]


class Branch(BaseModel):
    id: str
    problem: str
    objective: str
    parent: str | None = None
    status: BranchStatus = "open"
    budget_tokens: int | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


class Epoch(BaseModel):
    id: int | None = None
    branch_id: str
    model: str = "unknown"
    role: str = "proposer"
    started_at: str = Field(default_factory=utcnow)
    ended_at: str | None = None
    end_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0


class LibraryEntry(BaseModel):
    id: str
    statement: str
    formal: FormalClaim | None = None
    lean_path: str | None = None
    rigor: Rigor
    tags: list[str] = Field(default_factory=list)
    source_branch: str | None = None
    source_claim: str | None = None
    notes: str = ""
    created_at: str = Field(default_factory=utcnow)


ProposalKind = Literal["tool", "prompt", "doctrine", "bug", "infra", "library"]

ContributionTarget = Literal["mathlib", "formal-conjectures", "oeis", "other"]
ContributionStatus = Literal["candidate", "drafted", "submitted", "merged", "declined", "superseded"]


class Contribution(BaseModel):
    """Something that belongs *upstream*, not in this harness: a lemma Mathlib lacks, a `sorry` a conjecture
    repository still carries, a sequence comment OEIS should have.

    This is not a toolbox proposal. A `ToolboxProposal` asks for the harness to change; a `Contribution` says
    the harness worked and produced something another project should own. It carries what the run already has —
    a kernel-accepted artifact or claim — so the pull request is a packaging job, not a re-derivation.
    """

    id: int | None = None
    target: ContributionTarget
    title: str
    statement: str          # what should exist upstream
    why: str                # why it is missing / what its absence blocked here
    have: str = ""          # what this run already produced: claim ids, artifact paths, library entries
    status: ContributionStatus = "candidate"
    url: str | None = None  # the pull request / issue, once it exists
    branch_id: str | None = None
    claim_id: str | None = None
    created_at: str = Field(default_factory=utcnow)

    @field_validator("statement", "why")
    @classmethod
    def _specific(cls, v: str) -> str:
        if len(v.strip()) < 25:
            raise ValueError("say precisely what should exist upstream and why it is missing (>= 25 chars)")
        return v


class ToolboxProposal(BaseModel):
    id: int | None = None
    kind: ProposalKind
    title: str
    rationale: str
    evidence: str = ""
    status: Literal["open", "accepted", "rejected", "done"] = "open"
    branch_id: str | None = None
    created_at: str = Field(default_factory=utcnow)
