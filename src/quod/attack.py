"""Deterministic ATTACK phase: run every cheap engine against a formal claim, record reports as
evidence, and (optionally) promote along the ladder. Spends CPU, not tokens."""
from __future__ import annotations

import json
import os
import signal
import threading
from contextlib import contextmanager
from pathlib import Path

from . import engines, expand, formal
from .models import AttackReport, Claim, Evidence, Rigor
from .paths import artifacts_dir
from .state import StateError, StateStore


ENGINE_TIMEOUT_S = int(os.environ.get("QUOD_ENGINE_TIMEOUT", "180"))


class EngineTimeout(RuntimeError):
    pass


@contextmanager
def deadline(seconds: int, what: str):
    """Wall-clock cap for one engine step. A symbolic sum with an exponential summand can occupy SymPy for
    longer than the Lean proof of the same lemma takes; an engine that overruns reports
    `inconclusive` instead of holding the epoch."""
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _fire(signum, frame):  # noqa: ANN001, ARG001
        raise EngineTimeout(f"{what} exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _artifact(claim: Claim, name: str, payload: dict) -> str:
    p = artifacts_dir() / claim.branch_id / f"{claim.id}.{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str))
    return str(p.relative_to(artifacts_dir().parent))


def run(store: StateStore, claim_id: str, promote: bool = True, lean: bool = True,
        max_instances: int = 400, engine_timeout: int | None = None) -> dict:
    c = store.require_claim(claim_id)
    budget = ENGINE_TIMEOUT_S if engine_timeout is None else engine_timeout
    if c.rigor == Rigor.REFUTED:
        return {"claim": claim_id, "skipped": "already refuted"}
    if not c.formal:
        return {"claim": claim_id, "skipped": "claim has no `formal` block; the engine cannot read it (see docs/CLAIMS.md)"}
    out: dict = {"claim": claim_id, "start_rigor": c.rigor, "steps": []}
    if not c.formal.engine_readable:
        # Lean-only claim (e.g. an imported target): only the kernel can say anything.
        if lean and c.formal.lean:
            rep_l = formal.check(c.formal.lean, name_hint=c.id)
            if rep_l.verdict == "proved" and c.formal.frozen:
                bad = formal.frozen_violations(c.formal.frozen, c.formal.lean)
                if bad:
                    rep_l = formal.LeanReport("failed", "frozen statement violated: " + "; ".join(bad), rep_l.path)
            outcome = "survived" if rep_l.verdict == "proved" else "inconclusive"
            store.record_attack(AttackReport(claim_id=c.id, branch_id=c.branch_id, attacker="engine:lean", kind="lean",
                                             outcome=outcome, details=f"{rep_l.verdict}: {rep_l.details[:300]}", artifact=rep_l.path))
            out["steps"].append({"kind": "lean", "outcome": outcome, "details": f"{rep_l.verdict}: {rep_l.details[:300]}", "artifact": rep_l.path})
            if rep_l.verdict == "proved":
                store.add_evidence(c.id, Evidence(kind="lean", summary="kernel accepted", produced_by="engine:lean", artifact=rep_l.path))
                if promote:
                    try:
                        store.promote(c.id, Rigor.FORMALLY_PROVED)
                        out["steps"].append({"promoted_to": Rigor.FORMALLY_PROVED})
                        if "target" in c.tags:
                            out["helper_claims_registered"] = store.register_declarations(
                                c.id, c.formal.lean, store.first_seen_declarations(c.branch_id))
                    except StateError as e:
                        out["steps"].append({"promotion_skipped": str(e)})
        else:
            out["steps"].append({"skipped": "lean-only claim; no SymPy conclusion to expand"})
        out["final_rigor"] = store.require_claim(c.id).rigor
        return out

    def record(kind: str, attacker: str, outcome: str, details: str, artifact: str | None = None) -> None:
        store.record_attack(AttackReport(claim_id=c.id, branch_id=c.branch_id, attacker=attacker, kind=kind,
                                         outcome=outcome, details=details, artifact=artifact))
        out["steps"].append({"kind": kind, "outcome": outcome, "details": details, "artifact": artifact})

    def try_promote(target: Rigor) -> None:
        if not promote:
            return
        try:
            store.promote(c.id, target)
            out["steps"].append({"promoted_to": target})
        except StateError as e:
            out["steps"].append({"promotion_skipped": str(e)})

    # 1. instances
    try:
        with deadline(budget, "instance expansion"):
            rep = expand.instances(c.formal, max_instances=max_instances)
    except Exception as e:  # noqa: BLE001
        record("instance_check", "engine:expand", "inconclusive", f"expansion failed: {e}")
        out["final_rigor"] = store.require_claim(c.id).rigor
        return out
    art = _artifact(c, "instances", {"summary": rep.summary(), "counterexamples": rep.counterexamples})
    if rep.counterexamples:
        record("instance_check", "engine:expand", "refuted", rep.summary(), art)
        store.refute(c.id, f"instance counterexample {rep.counterexamples[0]}", "engine:expand", art)
        out["final_rigor"] = Rigor.REFUTED
        return out
    record("instance_check", "engine:expand", rep.outcome, rep.summary(), art)
    if rep.outcome == "survived":
        store.add_evidence(c.id, Evidence(kind="instance_check", summary=rep.summary(), produced_by="engine:expand", artifact=art))
        try_promote(Rigor.EMPIRICAL)

    # 2. CAS
    try:
        with deadline(budget, "CAS"):
            sym = expand.symbolic(c.formal)
    except EngineTimeout as e:
        sym = expand.SymbolicReport("inconclusive", "cas", str(e))
    if sym.verdict == "false":
        record("cas", "engine:sympy", "refuted", sym.details)
        store.refute(c.id, sym.details, "engine:sympy")
        out["final_rigor"] = Rigor.REFUTED
        return out
    record("cas", "engine:sympy", "survived" if sym.verdict == "verified" else "inconclusive", f"{sym.method}: {sym.details}")
    if sym.verdict == "verified":
        store.add_evidence(c.id, Evidence(kind="cas", summary=sym.details, produced_by="engine:sympy", metadata={"method": sym.method}))
        try_promote(Rigor.SYMBOLICALLY_VERIFIED)

    # 3. SMT
    try:
        with deadline(budget, "SMT"):
            smt = engines.smt(c.formal)
    except EngineTimeout as e:
        smt = engines.SmtReport("unknown", str(e))
    if smt.verdict == "counterexample":
        art = _artifact(c, "smt", {"model": smt.model, "details": smt.details})
        record("smt", "engine:z3", "refuted", f"{smt.details}: {smt.model}", art)
        store.refute(c.id, f"z3 model {smt.model}", "engine:z3", art)
        out["final_rigor"] = Rigor.REFUTED
        return out
    record("smt", "engine:z3", "survived" if smt.verdict == "verified" else "inconclusive", f"{smt.verdict}: {smt.details}")
    if smt.verdict == "verified":
        store.add_evidence(c.id, Evidence(kind="smt", summary=smt.details, produced_by="engine:z3"))
        try_promote(Rigor.SOLVER_VERIFIED)

    # 4. Lean
    if lean and c.formal.lean:
        rep_l = formal.check(c.formal.lean, name_hint=c.id)
        if rep_l.verdict == "proved":
            store.add_evidence(c.id, Evidence(kind="lean", summary="lean accepted the statement", produced_by="engine:lean",
                                              artifact=rep_l.path))
            record("lean", "engine:lean", "survived", "kernel accepted", rep_l.path)
            try_promote(Rigor.FORMALLY_PROVED)
        else:
            record("lean", "engine:lean", "inconclusive", f"{rep_l.verdict}: {rep_l.details[:300]}", rep_l.path)
    out["final_rigor"] = store.require_claim(c.id).rigor
    return out
