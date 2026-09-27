"""Post-hoc analysis of a branch: where the minutes and tokens went.

Attribution is by *dependency closure*: a claim counts as progress only if a Lean declaration it produced
survives into the target's final proof. Everything else is exploration cost. From that:

  phases   exploration  = epoch start -> first kernel-accepted declaration that is in the closure
           recognition  = -> first check of the target theorem itself
           verification = -> target kernel-proved
  eta      weighted promotions of closure claims / (frontier input tokens * wall hours)
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from .formal import qualified_theorem_names, signatures
from .models import RIGOR_RANK, Rigor
from .state import StateStore


IDLE_GAP_S = 300  # a gap between turns longer than this is idle time (freeze, rate limit), not work


def _t(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    return (b - a).total_seconds() if a and b else None


def closure(store: StateStore, branch_id: str) -> dict:
    """Which claims' declarations appear in the target's final proof."""
    claims = store.claims(branch_id)
    b = store.require_branch(branch_id)
    programme = bool({"programme", "open-problem"} & set(b.tags))
    target = next((c for c in claims if "target" in c.tags), None)
    if target is None and not programme:
        # bench-style branch: the proved claim *is* the target. On a programme branch a proved claim is a
        # lemma on the way, never the goal, so no fallback there.
        target = next((c for c in claims if c.rigor == Rigor.FORMALLY_PROVED), None)
    proof = target.formal.lean if (target and target.formal and target.formal.lean) else ""
    proof_names = set(qualified_theorem_names(proof)) | set(signatures(proof))
    in_closure, outside = [], []
    for c in claims:
        if target and c.id == target.id:
            continue
        names = set(signatures(c.formal.lean)) if (c.formal and c.formal.lean) else set()
        short = {n.split(".")[-1] for n in names}
        hit = bool(names & proof_names) or any(f"{n}" in proof for n in short if len(n) > 3)
        (in_closure if hit else outside).append(c)
    return {"target": target, "proof": proof, "in_closure": in_closure, "outside": outside}


def analyze(store: StateStore, branch_id: str) -> dict:
    b = store.require_branch(branch_id)
    epochs = store.epochs(branch_id)
    events = list(reversed(store.events(branch_id, limit=100000)))
    cl = closure(store, branch_id)
    target = cl["target"]
    t0 = _t(epochs[0].started_at) if epochs else None
    t_end = _t(epochs[-1].ended_at) if epochs and epochs[-1].ended_at else None
    wall = sum((_secs(_t(e.started_at), _t(e.ended_at)) or 0) for e in epochs)
    tokens_in = sum(e.tokens_in for e in epochs)
    tokens_out = sum(e.tokens_out for e in epochs)

    # timeline of promotions, per claim
    promotions = [(ev["ts"], ev["payload"].get("claim"), ev["payload"].get("to")) for ev in events if ev["type"] == "claim.promoted"]
    closure_ids = {c.id for c in cl["in_closure"]}
    t_first_closure = next((_t(ts) for ts, cid, to in promotions if cid in closure_ids and to == Rigor.FORMALLY_PROVED), None)
    # auto-registered helpers carry a first-seen time only when an in-process Lean check recorded them
    auto_seen = [_t(e.metadata.get("first_seen")) for c in cl["in_closure"] if "auto" in c.tags
                 for e in c.evidence if e.kind == "lean" and e.metadata.get("first_seen")]
    if auto_seen and (t_first_closure is None or min(auto_seen) < t_first_closure):
        t_first_closure = min(auto_seen)
    t_target_proved = next((_t(ts) for ts, cid, to in promotions if target and cid == target.id and to == Rigor.FORMALLY_PROVED), None)

    # tool calls: first check touching the target theorem name
    calls = store.tool_calls(branch_id)
    tname = None
    if target and target.formal and target.formal.lean:
        names = qualified_theorem_names(target.formal.lean)
        tname = next((n for n in names if n.startswith("putnam") or n == names[-1]), None)
    t_first_target_check = None
    short = tname.split(".")[-1] if tname else None
    for c in calls:
        if c["kind"] in ("lean.check", "lean.attach", "lean.raw") and short and short in (c["summary"] or ""):
            t_first_target_check = _t(c["ts"])
            break
    if t_first_target_check is None:
        # fallback: first Lean compile of anything (in-process if present, else stream-observed), else first attach
        inproc = [c for c in calls if c["kind"] == "lean.check" and not c["tool"].startswith("stream:")]
        streamed = [c for c in calls if c["kind"] in ("lean.check", "lean.raw") and c["tool"].startswith("stream:")]
        attaches = [c for c in calls if c["kind"] == "lean.attach"]
        for group in (inproc, streamed, attaches):
            if group:
                t_first_target_check = _t(group[0]["ts"])
                break
    by_kind: dict[str, dict] = {}
    for c in calls:
        if c["tool"].startswith("stream:") or not any(x["tool"].startswith("stream:") for x in calls):
            k = by_kind.setdefault(c["kind"], {"calls": 0, "seconds": 0.0, "failed": 0})
            k["calls"] += 1
            k["seconds"] += c["seconds"] or 0
            k["failed"] += 0 if c["ok"] else 1
    lean_checks = [c for c in calls if c["kind"] == "lean.check" and not c["tool"].startswith("stream:")]

    # context occupancy per turn; active wall = inter-turn gaps with idle stretches (freezes, rate limits) excluded
    turns = [t for e in epochs if e.id for t in store.turns(e.id)]
    tts = sorted(_t(t["ts"]) for t in turns if t["ts"])
    # A long gap is idle only if nothing was running in it. A single `lake`/`lean` compile routinely exceeds
    # the threshold, and subtracting those minutes counted real work as a freeze.
    busy: list[tuple[datetime, datetime]] = []
    for c in calls:
        start = _t(c["ts"])
        if start is None:
            continue
        busy.append((start, start + timedelta(seconds=max(0.0, c["seconds"] or 0.0))))
    idle_gaps: list[tuple[datetime, float]] = []  # (gap start, idle seconds beyond the threshold)
    for a_, b_ in zip(tts, tts[1:]):
        gap = (b_ - a_).total_seconds()
        if gap <= IDLE_GAP_S:
            continue
        worked = 0.0
        for bs, be in busy:  # union is unnecessary: harness calls are sequential within an epoch
            lo, hi = max(a_, bs), min(b_, be)
            if hi > lo:
                worked += (hi - lo).total_seconds()
        quiet = gap - min(gap, worked)
        if quiet > IDLE_GAP_S:
            idle_gaps.append((a_, quiet - IDLE_GAP_S))
    idle = sum(g for _, g in idle_gaps)
    active_wall = max(0.0, wall - idle) if wall else wall

    def active_secs(a_: datetime | None, b_: datetime | None) -> float | None:
        """Elapsed seconds between two instants with idle gaps that fall inside the interval removed."""
        raw = _secs(a_, b_)
        if raw is None:
            return None
        lo, hi = min(a_, b_), max(a_, b_)
        inside = sum(g for start, g in idle_gaps if lo <= start <= hi)
        return (raw - inside) if raw >= 0 else (raw + inside)
    ctx = [t["context"] for t in turns]
    cache_read = sum(t["cache_read"] for t in turns)
    cache_new = sum(t["cache_create"] for t in turns)

    # progress in target units, so fragmenting a proof into many lemmas earns nothing extra:
    #   target formally proved -> 5; otherwise partial credit for kernel-proved lemma claims, capped below a proof
    weight = {Rigor.EMPIRICAL: 1, Rigor.NUMERICALLY_CERTIFIED: 2, Rigor.SYMBOLICALLY_VERIFIED: 3, Rigor.SOLVER_VERIFIED: 3,
              Rigor.FORMALLY_PROVED: 5}
    lemma_pts = sum(1 for c in store.claims(branch_id) if c.rigor == Rigor.FORMALLY_PROVED and "target" not in c.tags)
    obs = store.obstructions(branch_id)
    cleared = sum(1 for o in obs if o.status == "cleared")
    programme = bool({"programme", "open-problem"} & set(b.tags))
    if target and target.rigor == Rigor.FORMALLY_PROVED:
        progress = 5.0
    elif programme:
        # On an open problem the target will not close this epoch, and clamping progress below a proof would
        # score every honest run at zero — which is exactly the incentive that makes a model stop trying.
        # Here the deliverables *are* the kernel-proved fragments and the terrain: each proved lemma counts
        # in full, a cleared obstruction counts as a lemma, a newly named one counts a little.
        progress = 1.0 * lemma_pts + 1.0 * cleared + 0.25 * sum(1 for o in obs if o.status == "open")
    else:
        best = max((weight.get(c.rigor, 0) for c in [target] if c), default=0)
        progress = min(4.0, best + 0.5 * lemma_pts)
    wall_h = active_wall / 3600 if active_wall else None
    eta_tok = (progress / (tokens_in / 1e6)) if tokens_in else None
    eta_time = (progress / wall_h) if wall_h else None
    eta = (eta_tok / wall_h) if (eta_tok is not None and wall_h) else None

    fails = store.failed_strategies(branch_id)
    attacks = store.attacks(branch_id=branch_id)
    # Milestones are not ordered in practice: agents often compile the whole file (target included) before any
    # lemma claim is promoted, and sometimes register lemma claims after the target. Phases are defined on the
    # sorted milestones and clamped at zero:
    #   exploration  = start -> earliest of {first closure lemma proved, first target check}
    #   recognition  = gap between those two milestones (0 when the target was checked first)
    #   verification = first target check -> target proved
    marks = [x for x in (t_first_closure, t_first_target_check) if x]
    anchor = min(marks) if marks else None
    rec = active_secs(t_first_closure, t_first_target_check) if (t_first_closure and t_first_target_check) else (0.0 if anchor else None)
    ver = active_secs(t_first_target_check or anchor, t_target_proved)
    phases = {
        "exploration_s": active_secs(t0, anchor),
        "recognition_s": max(0.0, rec) if rec is not None else None,
        "verification_s": max(0.0, ver) if ver is not None else None,
        "idle_s": idle,
    }
    return {
        "branch": branch_id, "status": b.status, "epochs": len(epochs), "wall_s": active_wall, "wall_raw_s": wall,
        "tokens": {"input": tokens_in, "output": tokens_out, "cache_read": cache_read, "cache_new": cache_new,
                   "cache_hit_ratio": (cache_read / (cache_read + cache_new + max(1, tokens_in - cache_read - cache_new))) if turns else None},
        "turns": len(turns), "context": {"max": max(ctx) if ctx else None, "final": ctx[-1] if ctx else None,
                                         "mean": (sum(ctx) / len(ctx)) if ctx else None},
        "target": {"id": target.id if target else None, "rigor": target.rigor if target else None,
                   "proof_lines": cl["proof"].count("\n") + 1 if cl["proof"] else 0,
                   "proved_at_s": _secs(t0, t_target_proved)},
        "closure": {"claims_in": [c.id for c in cl["in_closure"]], "claims_outside": [c.id for c in cl["outside"]],
                    "first_closure_lemma_at_s": _secs(t0, t_first_closure),
                    "first_target_check_at_s": _secs(t0, t_first_target_check)},
        "phases": phases,
        "tools": by_kind,
        "lean_checks": {"n": len(lean_checks), "failed": sum(1 for c in lean_checks if not c["ok"]),
                        "seconds": sum(c["seconds"] or 0 for c in lean_checks)},
        "attacks": dict(Counter(a.attacker for a in attacks)),
        "critic": {"calls": sum(1 for a in attacks if a.kind.startswith("llm")),
                   "hits": sum(1 for a in attacks if a.kind.startswith("llm") and a.outcome in ("refuted", "weakened")),
                   "modes": dict(Counter(a.kind for a in attacks if a.kind.startswith("llm")))},
        "failed_strategies": len(fails),
        "programme": programme,
        "obstructions": {"open": sum(1 for o in obs if o.status == "open"), "cleared": cleared},
        "lemmas_proved": lemma_pts,
        "progress_points": progress, "eta": eta, "eta_per_Mtok": eta_tok, "eta_per_hour": eta_time,
    }


def render(a: dict) -> str:
    def m(s):
        return "-" if s is None else f"{int(s // 60)}m{int(s % 60):02d}s"
    L = [f"# Trace — {a['branch']} [{a['status']}]", "",
         f"epochs {a['epochs']} · wall {m(a['wall_s'])} · turns {a['turns']} · input {a['tokens']['input']/1e6:.2f}M "
         f"(cache hit {a['tokens']['cache_hit_ratio']:.0%}) · output {a['tokens']['output']/1e3:.0f}k" if a['tokens']['cache_hit_ratio'] is not None else
         f"epochs {a['epochs']} · wall {m(a['wall_s'])} · input {a['tokens']['input']/1e6:.2f}M · output {a['tokens']['output']/1e3:.0f}k",
         f"context: max {a['context']['max']} · mean {int(a['context']['mean']) if a['context']['mean'] else '-'}",
         f"target {a['target']['id']}: {a['target']['rigor']} · proof {a['target']['proof_lines']} lines · proved at {m(a['target']['proved_at_s'])}",
         "", "## Phases",
         f"exploration  {m(a['phases']['exploration_s'])}   (start → first kernel-accepted closure lemma)",
         f"recognition  {m(a['phases']['recognition_s'])}   (→ first check of the target theorem)",
         f"verification {m(a['phases']['verification_s'])}   (→ target proved)"
         + (f"\nidle         {m(a['phases']['idle_s'])}   (excluded from wall)" if a['phases'].get('idle_s') else ""),
         "", "## Attribution",
         f"in closure: {', '.join(a['closure']['claims_in']) or '-'}",
         f"outside:    {', '.join(a['closure']['claims_outside']) or '-'}",
         (f"progress {a['progress_points']:.1f} (target units) · η = {a['eta']:.2f} /(Mtok·h) · {a['eta_per_Mtok']:.2f} /Mtok · {a['eta_per_hour']:.1f} /h"
          if a['eta'] else f"progress {a['progress_points']} · η = -"),
         "", "## Tools (seconds are wall time of the call)"]
    for k, v in sorted(a["tools"].items(), key=lambda kv: -kv[1]["seconds"]):
        L.append(f"  {k:16s} calls {v['calls']:3d}  failed {v['failed']:3d}  {m(v['seconds'])}")
    L += ["", f"obstructions: {a['obstructions']['open']} open, {a['obstructions']['cleared']} cleared · "
          f"lemmas kernel-proved {a['lemmas_proved']}" + (" · programme scoring" if a.get("programme") else ""),
          f"lean checks (in-process) {a['lean_checks']['n']} ({a['lean_checks']['failed']} failed, {m(a['lean_checks']['seconds'])}) · "
          f"critic {a['critic']['hits']}/{a['critic']['calls']} hits · failed strategies {a['failed_strategies']}",
          f"engine attacks: {a['attacks']}"]
    return "\n".join(L)
