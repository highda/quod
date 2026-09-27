"""Wide search for counterexamples and witnesses: one array pass over a integer grid, exact recheck afterwards.

The GPU decides nothing. It narrows a space of 10^8 candidates to the handful that survive, and every survivor
is re-evaluated in exact arithmetic before it is recorded — a floating-point near-miss is not a solution, and a
floating-point witness is not a witness.
"""
from __future__ import annotations

import itertools
import json
import math
import time
from dataclasses import dataclass, field

import sympy as sp

from . import gpu
from .claims import parse
from .models import Claim, Evidence, FormalClaim
from .paths import artifacts_dir

CHUNK = 1 << 20  # candidates per array pass; keeps peak memory modest on a 16 GB machine


@dataclass
class SearchReport:
    backend: str
    kind: str                      # counterexample | witness
    checked: int = 0
    seconds: float = 0.0
    counterexamples: list[dict] = field(default_factory=list)
    witnesses: list[dict] = field(default_factory=list)
    exhausted: bool = False        # the whole requested box was covered
    unsupported: str | None = None
    artifact: str | None = None

    def summary(self) -> str:
        if self.unsupported:
            return f"array backend declined: {self.unsupported}"
        found = len(self.counterexamples) if self.kind == "counterexample" else len(self.witnesses)
        return (f"{self.backend}: {self.checked:,} candidates in {self.seconds:.1f}s, {found} "
                f"{self.kind}{'s' if found != 1 else ''}{' (box exhausted)' if self.exhausted else ''}")


def _ranges(fc: FormalClaim, box: dict[str, tuple[int, int]] | None, default: tuple[int, int]) -> dict[str, tuple[int, int]]:
    lo_hi = {}
    for name, dom in fc.variables.items():
        if box and name in box:
            lo_hi[name] = box[name]
            continue
        lo, hi = default
        if dom == "N":
            lo = max(lo, 0)
        elif dom == "N+":
            lo = max(lo, 1)
        lo_hi[name] = (lo, hi)
    return lo_hi


def _exact_holds(hyps: list[sp.Basic], concl: sp.Basic, point: dict[sp.Symbol, int]) -> tuple[bool, bool]:
    """(hypotheses hold, conclusion holds) in exact arithmetic at an integer point."""

    def truth(e: sp.Basic) -> bool:
        v = sp.simplify(e.subs(point).doit())
        if v in (sp.true, sp.false):
            return bool(v)
        raise ValueError(f"exact recheck did not decide {e} at {point}")

    return (all(truth(h) for h in hyps), truth(concl))


def run(fc: FormalClaim, mode: str = "counterexample", box: dict[str, tuple[int, int]] | None = None,
        default_range: tuple[int, int] = (-64, 64), max_candidates: int = 4_000_000,
        want: int = 8, prefer_gpu: bool = True) -> SearchReport:
    """Scan an integer box for points where the conclusion fails (`counterexample`) or holds (`witness`).

    Only variables that occur free are searched; a claim whose conclusion the array backend cannot evaluate is
    reported `unsupported` rather than approximated.
    """
    b = gpu.backend(prefer_gpu)
    rep = SearchReport(backend=b.name, kind=mode)
    syms, hyps, concl = parse(fc)
    free = sorted({s for s in syms.values() if s in (concl.free_symbols | set().union(*[h.free_symbols for h in hyps]) if hyps else concl.free_symbols)},
                  key=lambda s: s.name)
    if not free:
        rep.unsupported = "no free variables to search"
        return rep
    try:
        kind, diff = gpu.relation_parts(concl)
        f_concl = gpu.compile_expr(diff, free, b)
        f_hyps = []
        for h in hyps:
            hk, hd = gpu.relation_parts(h)
            f_hyps.append((hk, gpu.compile_expr(hd, free, b)))
    except gpu.Unsupported as e:
        rep.unsupported = str(e)
        return rep

    ranges = _ranges(fc, box, default_range)
    sizes = [ranges[s.name][1] - ranges[s.name][0] + 1 for s in free]
    total = math.prod(sizes)
    budget = min(total, max_candidates)
    t0 = time.perf_counter()

    # walk the box in chunks: the leading variables are enumerated on the host, the last one vectorised
    last, head = free[-1], free[:-1]
    last_lo, last_hi = ranges[last.name]
    last_n = last_hi - last_lo + 1
    col_last_full = b.asarray([float(last_lo + i) for i in range(min(last_n, CHUNK))])
    head_ranges = [range(ranges[s.name][0], ranges[s.name][1] + 1) for s in head]

    for combo in (itertools.product(*head_ranges) if head else [()]):
        for start in range(0, last_n, CHUNK):
            n = min(CHUNK, last_n - start)
            col_last = col_last_full[:n] if start == 0 and n <= CHUNK else b.asarray([float(last_lo + start + i) for i in range(n)])
            cols = [b.asarray([float(v)] * n) for v in combo] + [col_last]
            mask = gpu.holds(kind, f_concl(cols), b)
            if mode == "counterexample":
                mask = ~mask
            for hk, hf in f_hyps:
                mask = mask & gpu.holds(hk, hf(cols), b)
            idx = [i for i, m in enumerate(b.to_list(mask)) if m]
            rep.checked += n
            for i in idx:
                point = {s: int(v) for s, v in zip(head, combo)}
                point[last] = int(last_lo + start + i)
                try:
                    hyp_ok, concl_ok = _exact_holds(hyps, concl, point)
                except ValueError:
                    continue  # the array pass flagged it; exact arithmetic could not settle it, so it is not a find
                named = {s.name: v for s, v in point.items()}
                if mode == "counterexample" and hyp_ok and not concl_ok:
                    rep.counterexamples.append(named)
                elif mode == "witness" and hyp_ok and concl_ok:
                    rep.witnesses.append(named)
                if len(rep.counterexamples) + len(rep.witnesses) >= want:
                    break
            if len(rep.counterexamples) + len(rep.witnesses) >= want or rep.checked >= budget:
                break
        if len(rep.counterexamples) + len(rep.witnesses) >= want or rep.checked >= budget:
            break

    rep.seconds = time.perf_counter() - t0
    rep.exhausted = rep.checked >= total
    return rep


def attack(store, claim_id: str, mode: str = "counterexample", **kw) -> dict:
    """Run a search for a claim and record what it found: a refutation (exact counterexample) or `gpu_search`
    evidence, which is `empirical` and cannot climb."""
    c: Claim = store.require_claim(claim_id)
    if not (c.formal and c.formal.engine_readable):
        return {"claim": claim_id, "skipped": "claim has no engine-readable formal block"}
    rep = run(c.formal, mode=mode, **kw)
    art = None
    if rep.counterexamples or rep.witnesses:
        p = artifacts_dir() / c.branch_id / f"{c.id}.search.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"summary": rep.summary(), "counterexamples": rep.counterexamples,
                                 "witnesses": rep.witnesses, "backend": rep.backend}, indent=2))
        art = str(p.relative_to(artifacts_dir().parent))
        rep.artifact = art
    out = {"claim": claim_id, "mode": mode, "backend": rep.backend, "checked": rep.checked,
           "seconds": round(rep.seconds, 2), "summary": rep.summary(), "artifact": art,
           "counterexamples": rep.counterexamples, "witnesses": rep.witnesses,
           "exhausted": rep.exhausted, "unsupported": rep.unsupported}
    if rep.counterexamples:
        store.refute(c.id, f"search counterexample {rep.counterexamples[0]}", f"engine:{rep.backend}", art)
        out["refuted"] = True
        return out
    if rep.unsupported:
        return out
    store.add_evidence(c.id, Evidence(kind="gpu_search", summary=rep.summary(), produced_by=f"engine:{rep.backend}",
                                      artifact=art, metadata={"exhausted": rep.exhausted, "mode": mode}))
    out["evidence"] = "gpu_search (empirical; an exhausted finite box still needs a proof for the general case)"
    return out
