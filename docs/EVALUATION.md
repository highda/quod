# Evaluating a change to the harness

A change to the harness is a claim about the harness, and needs the same discipline the doctrine demands of
mathematics: measure it, do not assert it. Every report below is generated from state and written into
`local/reports/`.

## What is measured

`quod trace -b <branch>` reconstructs one branch from the ingested run log:

- **phases** — exploration (start → first kernel-accepted declaration that survives into the final proof),
  recognition (→ first check of the target theorem), verification (→ target proved). Idle gaps are excluded,
  but a gap in which a tool call was still running counts as work, not idle.
- **attribution by dependency closure** — a claim counts as progress only if a declaration it produced appears
  in the target's final proof. Everything else is exploration cost.
- **η** — progress per Mtok and per hour. Progress is in *target units*, so splitting one proof into many
  lemmas earns nothing extra. On a branch tagged `programme`/`open-problem` the scoring changes, because the
  target will not close: one point per kernel-proved lemma, one per cleared obstruction, a quarter per newly
  named one.
- **cost** — input (including prompt-cache reads), output, cache hit ratio, per-tool wall time, context
  occupancy per turn. A run killed by a cap has no result envelope, so its usage is summed from its turns.

## Version context is part of the measurement

Every generated report opens with a provenance block — date, `quod` version, the models per role, the Lean
toolchain and Mathlib revision, Python and platform — written by `quod.provenance`. Numbers are comparable
only *within* one such block. A Mathlib bump changes which proofs are one `exact?` away; a model change moves
everything; a harness change is the thing under test. So:

- Regenerate before quoting a figure, and quote the header with it.
- Never merge rows measured under different headers into one table.
- Treat any report whose header predates the current pins as a historical trend line, not as a current result.
- The same applies to figures written into docs or issues by hand: say which version produced them.

## Suites

| instrument | what it answers | command |
|---|---|---|
| `bench/problems/*.toml` | does a harness variant rediscover known results, with names stripped? | `quod bench start <id>`, `quod bench score` |
| generated true/false statements | does it *decide* correctly, on statements no model can have memorised? | `quod bench generate --seed N`, `quod bench score-gen --seed N` |
| `quod source open putnam <id>` | competition mathematics with a Lean statement and the reference answer stripped on import | `scripts/run-epoch.sh <branch>` |
| `quod source open fc <path>` | open conjectures, imported and frozen; scored as programmes | `scripts/programme.sh <epochs> <branch>…` |

The generated suite is the one that cannot be recalled from training data. Treat competition results as a
check that the discipline holds end to end, never as evidence of reasoning ability; and note that published
leaderboards run whole benchmarks under pass@N or dollar budgets, which a per-problem table is not comparable
to. Reports: `scripts/bench-report.py`, `scripts/programme-report.py --md`.

## Ablation

`QUOD_ABLATE=engines,adversary,strategy` makes the CLI *refuse* the disabled features (exit 3) and tells the
runner's prompt they are unavailable, so a cell cannot quietly use what it is meant to be without.
`scripts/ablation.sh <reps> <problem-id>…` runs the cells — `full`, `no-engines`, `no-strategy`, `respawn`
(short epochs, re-launched) — sequentially, pausing when the five-hour quota window is nearly spent.
`scripts/ablation-report.py --md` writes the per-cell table, the per-problem grid and the paired comparisons.

Read it with the usual cautions: two repetitions bound the *sign* of an effect, not its size; within-cell
variance on the same problem can exceed the difference between cells; and a cell that solves everything has
measured cost, not capability. When a feature costs more than it returns on a band of problems, the answer is
usually to gate it on a difficulty signal (`quod difficulty`), not to delete it — the bands where it earns its
keep are the hard ones.

## Hygiene for a measured run

1. `pixi run doctor`, `pixi run test`, `pixi run demo` green first. A red demo invalidates everything after it.
2. Freeze the toolbox. Changes during a run go to the ledger (`quod ledger add`), not into the code.
3. One variable per campaign. The runners take the model from `quod.toml`, so a cell cannot silently run a
   different model than the one you configured.
4. Clear the working tree of other runs' proof files; ablation cells move stray untracked ones aside for you.
5. Give each epoch a budget. The CLI turn cap is not a reliable ceiling, so the runner also enforces a
   wall-clock budget (`QUOD_EPOCH_MAX_MIN`) and closes the epoch itself if the run is cut off.
