"""Blind rediscovery benchmark: construction-phase instrument for measuring whether a change to the
toolbox/doctrine made the harness better. Not used during active research."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import Branch, Rigor
from .paths import bench_dir
from .state import StateStore

BENCH_TAG = "bench"


@dataclass
class Problem:
    id: str
    statement: str
    domain: str
    difficulty: int
    acceptance: str  # Lean statement the harness must prove (with `sorry` replaced)
    notes: str
    path: Path


def load(path: Path | None = None) -> list[Problem]:
    d = path or bench_dir()
    out = []
    for f in sorted(d.glob("*.toml")):
        data = tomllib.loads(f.read_text())
        out.append(Problem(id=data["id"], statement=data["statement"], domain=data.get("domain", ""),
                           difficulty=int(data.get("difficulty", 1)), acceptance=data["acceptance"],
                           notes=data.get("notes", ""), path=f))
    return out


def start(store: StateStore, problem: Problem, branch_id: str | None = None) -> Branch:
    bid = branch_id or f"bench-{problem.id}"
    return store.create_branch(Branch(
        id=bid, problem=problem.statement,
        objective="Prove the statement and get it accepted by Lean. Names of known results are withheld on purpose.",
        tags=[BENCH_TAG, problem.id, problem.domain],
    ))


def score(store: StateStore, tag: str = BENCH_TAG) -> dict:
    rows = []
    for b in store.branches():
        if tag not in b.tags or b.status == "abandoned":
            continue
        claims = store.claims(b.id)
        proved = [c.id for c in claims if c.rigor == Rigor.FORMALLY_PROVED]
        best = max((c.rigor for c in claims), key=lambda r: (r != Rigor.REFUTED, str(r)), default=None)
        toks = sum(e.tokens_in + e.tokens_out for e in store.epochs(b.id))
        attacks = store.attacks(branch_id=b.id)
        rows.append({
            "branch": b.id, "status": b.status, "proved_claims": len(proved), "best_rigor": best,
            "tokens": toks, "epochs": len(store.epochs(b.id)), "attacks": len(attacks),
            "refutations": sum(a.outcome == "refuted" for a in attacks),
            "proofs_per_Mtok": (len(proved) / toks * 1e6) if toks else None,
            "target_proved": any(c.rigor == Rigor.FORMALLY_PROVED and "target" in c.tags for c in claims),
        })
    total_proved = sum(r["proved_claims"] for r in rows)
    total_tok = sum(r["tokens"] for r in rows)
    return {"tag": tag, "branches": rows, "total_proved": total_proved, "total_tokens": total_tok,
            "targets_proved": sum(r["target_proved"] for r in rows), "n": len(rows),
            "proofs_per_Mtok": (total_proved / total_tok * 1e6) if total_tok else None,
            "attacker_stats": store.attack_stats()}
