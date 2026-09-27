#!/usr/bin/env python3
"""Write local/reports/PROGRAMME.md from state: what the open-problem branches delivered.

    scripts/programme-report.py [--md] [branch ...]

With no branches, every branch tagged `programme`/`open-problem` is reported. Numbers come from
`quod trace` and the claim/obstruction tables; nothing here is typed by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quod import provenance, trace as trace_mod  # noqa: E402
from quod.models import Rigor  # noqa: E402
from quod.state import StateStore  # noqa: E402


def m(seconds: float | None) -> str:
    if not seconds:
        return "-"
    return f"{int(seconds // 60)}m"


def report(branches: list[str]) -> str:
    L: list[str] = [provenance.header_markdown("Open-problem programmes"),
                    "Branches imported from [formal-conjectures](https://github.com/google-deepmind/formal-conjectures) "
                    "and worked by headless epochs. The target statements are open, so the deliverables are "
                    "kernel-proved fragments and named obstructions; `quod trace` scores these branches accordingly "
                    "(1 point per kernel-proved lemma, 1 per cleared obstruction, 0.25 per newly named one).", "",
                    "Every proof below was accepted by the Lean kernel with axioms ⊆ {propext, Classical.choice, "
                    "Quot.sound} and no `sorry`. Each load-bearing claim also passed an isolated "
                    "`quod critic --mode representation` pass on faithfulness.", ""]
    with StateStore() as s:
        rows = []
        for bid in branches:
            b = s.require_branch(bid)
            a = trace_mod.analyze(s, bid)
            claims = s.claims(bid)
            proved = [c for c in claims if c.rigor == Rigor.FORMALLY_PROVED and "auto" not in c.tags]
            obs = s.obstructions(bid)
            rows.append((b, a, proved, obs))

        L += ["| branch | target | epochs | input | wall | kernel-proved | obstructions | progress | η /Mtok |",
              "|---|---|---|---|---|---|---|---|---|"]
        for b, a, proved, obs in rows:
            src = next((t for t in b.tags if t.endswith(".lean")), b.id)
            L.append(f"| `{b.id}` | {src} | {a['epochs']} | {a['tokens']['input']/1e6:.2f}M | {m(a['wall_s'])} | "
                     f"{len(proved)} | {len(obs)} | {a['progress_points']:.1f} | "
                     f"{a['eta_per_Mtok']:.2f}" + " |" if a["eta_per_Mtok"] else " - |")
        L.append("")

        for b, a, proved, obs in rows:
            L += [f"## `{b.id}`", "", f"**Target (open):** {b.problem.splitlines()[0][:300]}", ""]
            L.append("**Kernel-proved:**")
            L += [f"- `{c.id}` — {' '.join(c.statement.split())[:300]}" for c in proved] or ["- _none_"]
            L.append("")
            L.append("**Obstructions:**")
            for o in obs:
                L.append(f"- [{o.kind}, {o.status}] {' '.join(o.statement.split())[:300]}")
                L.append(f"  - unblock: {' '.join(o.unblock.split())[:300]}")
            if not obs:
                L.append("- _none_")
            L.append("")
            fails = s.failed_strategies(b.id)
            if fails:
                L.append("**Failed strategies recorded:**")
                L += [f"- {' '.join(f.strategy.split())[:120]} — {' '.join(f.reason.split())[:220]}" for f in fails]
                L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--md"]
    with StateStore() as s:
        ids = args or [b.id for b in s.branches() if {"programme", "open-problem"} & set(b.tags)]
    text = report(ids)
    if "--md" in sys.argv[1:]:
        p = Path("local/reports/PROGRAMME.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        print(f"wrote {p} ({len(text)} chars) for {len(ids)} branches")
    else:
        print(text)


if __name__ == "__main__":
    main()
