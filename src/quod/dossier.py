"""Render the compact branch dossier an epoch reads at ORIENT. Bounded in size by construction."""
from __future__ import annotations

from .models import Rigor
from .state import StateStore


def _clip(s: str, n: int = 240) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(store: StateStore, branch_id: str, max_chars: int = 12_000) -> str:
    b = store.require_branch(branch_id)
    cp = store.latest_checkpoint(branch_id)
    manual = store.latest_checkpoint(branch_id, include_auto=False)
    claims = store.claims(branch_id)
    attacks = store.attacks(branch_id=branch_id)
    epochs = store.epochs(branch_id)
    lines: list[str] = []
    lines += [f"# Dossier — branch `{b.id}` [{b.status}]", "", f"**Problem:** {b.problem}", f"**Objective:** {b.objective}"]
    if b.parent:
        lines.append(f"**Parent:** {b.parent}")
    tok = sum(e.tokens_in + e.tokens_out for e in epochs)
    lines.append(f"**Epochs:** {len(epochs)} · tokens logged: {tok}" + (f" / budget {b.budget_tokens}" if b.budget_tokens else ""))
    if cp:
        stale = " (AUTO — last manual checkpoint " + (manual.created_at if manual else "never") + ")" if cp.auto else ""
        lines.append(f"**Last checkpoint:** {cp.created_at}{stale}")
    lines.append("")

    by: dict[Rigor, list] = {}
    for c in claims:
        by.setdefault(c.rigor, []).append(c)
    lines.append("## Claims")
    order = [Rigor.FORMALLY_PROVED, Rigor.SOLVER_VERIFIED, Rigor.SYMBOLICALLY_VERIFIED, Rigor.NUMERICALLY_CERTIFIED,
             Rigor.EMPIRICAL, Rigor.SPECULATIVE, Rigor.REFUTED]
    if not claims:
        lines.append("_none_")
    for r in order:
        for c in by.get(r, []):
            dep = f" ⇐ {','.join(c.depends_on)}" if c.depends_on else ""
            f = " [formal]" if c.formal else ""
            lines.append(f"- `{c.id}` **{r}**{f}{dep}: {_clip(c.statement)}")
            if c.assumptions:
                lines.append(f"  - assumes: {_clip('; '.join(c.assumptions), 160)}")
            last = c.evidence[-1] if c.evidence else None
            if last:
                lines.append(f"  - last evidence ({last.kind}, {last.produced_by}): {_clip(last.summary, 160)}")
    lines.append("")

    if cp:
        for title, items in (("Open goals", cp.open_goals), ("Active assumptions", cp.active_assumptions),
                             ("Exact obstructions", cp.obstructions)):
            lines.append(f"## {title}")
            lines += [f"- {_clip(x)}" for x in items] or ["_none_"]
            lines.append("")
    obs = store.obstructions(branch_id)
    if obs:
        lines.append("## Obstructions")
        for o in obs:
            mark = "OPEN" if o.status == "open" else "cleared"
            lines.append(f"- `{o.id}` [{o.kind}, {mark}] {_clip(o.statement, 200)}")
            lines.append(f"  - unblock: {_clip(o.unblock, 200)}")
            if o.status == "cleared" and o.cleared_how:
                lines.append(f"  - cleared by: {_clip(o.cleared_how, 160)}")
        lines.append("")
    fails = store.failed_strategies(branch_id)
    lines.append("## Failed strategies (do not retry without a new idea)")
    lines += [f"- **{_clip(f.strategy, 100)}** — {_clip(f.reason, 200)}" + (f" [obstruction: {_clip(f.obstruction, 120)}]" if f.obstruction else "")
              for f in fails] or ["_none_"]
    lines.append("")
    if attacks:
        lines.append("## Recent attacks")
        for a in attacks[-8:]:
            lines.append(f"- {a.claim_id} ← {a.attacker} ({a.kind}): **{a.outcome}** — {_clip(a.details, 160)}")
        lines.append("")
    if cp:
        lines.append("## Next actions")
        lines += [f"{i}. {_clip(x)}" for i, x in enumerate(cp.next_actions, 1)] or ["_none — decide at PROPOSE_"]
        lines.append("")
        if cp.artifact_ids:
            lines.append("## Artifacts")
            lines += [f"- {a}" for a in cp.artifact_ids]
            lines.append("")
        if cp.notes:
            lines.append(f"> {_clip(cp.notes, 400)}")
            lines.append("")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 60] + "\n\n_[dossier truncated; query `quod claim list` for the rest]_\n"
    return out
