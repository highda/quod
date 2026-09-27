# Quod

*Claude erat demonstrandum.*

A Claude-driven, computation-first harness for mathematics research on Apple Silicon.

It is a **hybrid neuro-symbolic** setup rather than a trained prover: the model proposes, deterministic engines
and the Lean kernel decide, and no part of the pipeline is fine-tuned or searches a tactic space. The leverage
is in tooling, enforced discipline and measurement — which is also the limit, so read any comparison with
search-based provers with that in mind.

Two roles — a **proposer** and an isolated **adversary**, with an optional delegated **formalizer** — operate a
durable research state through one CLI, `quod`. Claims are written in a small formal language the engine can
read; the engine (SymPy, Z3, mpmath intervals, FLINT/Sage, MLX, Lean 4 + Mathlib) expands, tests, refutes and
certifies them at CPU cost instead of token cost. Rigor moves only upward and only with the right kind of
evidence; **proved means the Lean kernel accepted it**. Verified results compound in a cross-problem library.
Model context is disposable; state is not.

Read in this order: `CLAUDE.md` (doctrine — what an agent must do), `docs/ARCHITECTURE.md` (how the pieces fit),
`docs/CONTRACTS.md` (the guarantees and demands: ladder, kernel, roles, state, exit codes),
`docs/CLAIMS.md` (the claim language), `docs/EVALUATION.md` (how to measure a change to the harness).

## Setup (Apple-silicon Mac)
Install Pixi (recipe validated with 0.79.0; 0.81.x works), then:
```sh
./scripts/bootstrap.sh          # solve/install env, init state, tests, ladder demo
./scripts/formal-setup.sh       # optional: Lean 4.33.1 + Mathlib via elan + build cache
```
No installer is piped to a shell; provider auth stays in the Claude CLI.

## Run
```sh
pixi run agent                  # Claude Code as proposer; /orient to begin
pixi run agent fable            # …or as another model
quod --help
```
**First launch:** Claude Code asks whether you trust this directory. Accept — until you do, it ignores
`.claude/settings.json`, so the allowlist, hooks and skills are inactive. `quod doctor` reports
`claude_workspace_trusted` and prints a hint while this is pending.
`.claude/` ships the permission allowlist, hooks (dossier on session start, auto-checkpoint on
compaction/stop), skills (`/orient /claim /attack /critic /checkpoint /ledger`) and the `adversary` subagent.

## Models and roles
Every role is configurable in `quod.toml`, per shell with `QUOD_PROPOSER_MODEL` / `QUOD_ADVERSARY_MODEL` /
`QUOD_FORMALIZER_MODEL`, or per invocation (`pixi run agent <model>`, `quod critic --model`,
`quod formalize --model`). `quod config` shows what resolved and where each value came from.

| role | what it does | what matters |
|---|---|---|
| **proposer** | the main session: strategy, claims, tool use, formalization | the strongest reasoner you can afford |
| **adversary** | isolated per-claim critic, tools off, sees dossier + claim only | a *different* model from the proposer, so blind spots don't coincide |
| **formalizer** | handed one claim, has to make the Lean kernel accept it (`quod formalize <claim> --attach`) | whichever model is best at Lean, which need not be the best proposer |
| **overseer** | reserved (branch/budget scheduler); today it is you plus the hooks and the enforced ladder | — |

```toml
[models]
proposer   = "fable"     # research loop
adversary  = "opus"      # anything but the proposer's model
formalizer = "opus"      # unset = the proposer formalizes, exactly as if the role did not exist
```
Deciding *what* to prove and getting Lean to agree are different skills, which is why the formalizer exists;
leaving it unset changes nothing. A delegated session's own claim of success is never evidence — the harness
re-runs the kernel on the file it produced and re-checks any frozen statement. The same model in two seats
works, but then independence rests on context isolation alone, and `quod config` says so.

## How a run flows

```
            ┌──────────────────────── one epoch ────────────────────────┐
  dossier ──► ORIENT ──► PROPOSE ──► EXECUTE ──► ATTACK ──► DECIDE ──► CHECKPOINT ──► epoch end
   (state)     │           │           │          │  │         │            │
               │           │           │          │  │         │            └─► next epoch reads this
               │           │           │          │  │         │
               │           │           │          │  │         ├─► child branch          (split the problem)
               │           │           │          │  │         ├─► quod obstruction add  (blocked, and why)
               │           │           │          │  │         └─► quod branch status    (proved / refuted / closed)
               │           │           │          │  │
               │           │           │          │  └─► quod critic ──► isolated adversary (no tools)
               │           │           │          └────► engines: instances → CAS → SMT → Lean
               │           │           └───────────────► quod formalize ──► delegated Lean session
               │           └───────────────────────────► quod propose: N approaches, engines kill the losers
               └───────────────────────────────────────► quod library search · quod strategy match

  every step writes state ─► state/harness.sqlite3  ·  artifacts/  ·  formal/Quod/
  the kernel alone promotes to `formally_proved`; nothing else does
```

What leaves a run lands in its own catalog, never mixed with another:

```
  a gap in THIS harness ──► quod ledger add ─────► state: ledger        ──► local/reports/LEDGER.md
  a gap in ANOTHER repo ──► quod contribute add ─► state: contributions ──► local/reports/CONTRIBUTIONS.md
                                                   candidate → drafted → submitted → merged | declined
  verified results ──────► quod library add ─────► state: library       ──► local/reports/LIBRARY.md
  where time and tokens went ──► quod trace · scripts/*-report.py       ──► local/reports/*.md
```

## Engines: guess wide, verify exactly
Anything wide and approximate belongs on the GPU; anything that decides truth does not. Metal proposes,
exact arithmetic and the kernel dispose.

```sh
quod search <claim> --mode counterexample|witness --box "n=1:100000"   # array scan, survivors rechecked exactly
quod certify <claim>                     # sum-of-squares certificate → rational identity → nlinarith hint
quod conjecture 1,1,2,5,14,42            # closed forms and recurrences, fitted exactly over ℚ, validated
quod premise index                       # one index per pinned Mathlib revision
quod premise search "Finset sum range"   # candidate lemmas before you write a proof
quod neural-hammer <claim>               # optional: local model drafts Lean, the kernel decides
```

- **`search`** compiles a claim's formal block to array operations and scans an integer box; a hit is
  re-evaluated in exact arithmetic before it counts, so a floating-point near-miss is never a solution. A
  counterexample refutes the claim; a clean sweep is `gpu_search` evidence, which is `empirical` and stays there
  — an exhausted finite box is not a proof of the general statement.
- **`certify`** looks for `p = Σ cᵢ sᵢ²` with rational `cᵢ`: alternating projections on Metal guess the Gram
  matrix, the guess is rounded to ℚ, and the identity is confirmed symbolically. The numeric stage carries no
  rigor; what you get is `cas` evidence and the `nlinarith [sq_nonneg …]` hint that turns a Lean search into a
  check.
- **`conjecture`** fits polynomials, constant-coefficient recurrences, hypergeometric ratios and scaled forms
  exactly over ℚ, validating each candidate on terms the fit never saw, and invents nothing from noise.
- **`premise`** indexes the pinned Mathlib (measured: 184,574 declarations, 94.5 MB, 47 s to build, queries in
  tens of milliseconds). The index is keyed to `(encoder, Mathlib revision)`, so it is rebuilt only when you
  bump the pin — and then only for files whose contents changed. The default encoder needs no weights and
  retrieves lexically through FTS5; a neural encoder is a drop-in for semantic retrieval.
- **`neural-hammer`** is an extra, off until you configure `[neural] prover_model`: idle compute put to work on
  the routine end. A local quantised prover will not out-reason a frontier model and will miss most hard goals,
  but a candidate costs GPU seconds and no tokens and the kernel rejects a wrong one for free, so the honest
  metric is kernel-accepted proofs per wall hour rather than pass@1. It loads, generates, unloads, and only then
  compiles; a running `lake` build or insufficient memory blocks the pass.

Weights are **linked, not shipped**:
```sh
quod neural presets              # known-good targets, with sizes and what each is for
quod neural pull prover-7b       # …or any Hugging Face repo id, or point the config at a local directory
quod neural budget prover-7b     # what a pass needs, and whether the machine has it right now
```
The memory guard is derived from the model rather than fixed: measured (or declared) weight footprint × a load
factor + a KV allowance scaled to the generation length + headroom. A 40 MB encoder and a 14 GB checkpoint get
different answers, which is the point.

## Measuring, not guessing
`quod trace -b <branch>` shows where an epoch's minutes and tokens went — exploration / recognition /
verification, tool costs, cache hit ratio, and η (progress per Mtok and per hour) — attributed by dependency
closure, so only work that survives into the final proof counts. `quod strategy match` retrieves methodology
cards learned from earlier runs by problem structure; `quod propose` tries many approaches at engine cost
before reasoning is spent; `quod critic --mode` picks the adversary's attack; `QUOD_ABLATE` disables a feature
so its contribution can be measured rather than assumed. See `docs/EVALUATION.md`.

## The improvement ledger
The agent cannot change its own tools mid-run — the toolbox is frozen while research happens, so a measured run
stays measurable. What it *can* do is tell you what it needed and did not have:

```sh
quod ledger add tool|prompt|doctrine|bug|infra|library --title … --rationale … --evidence …
quod ledger list --status open        # read this before the next run
quod ledger append <id> --evidence … # another run hit the same thing
quod ledger set <id> accepted|rejected|done
quod ledger export                   # → local/reports/LEDGER.md
```

Each entry is a proposal with its evidence: the engine that timed out on a shape it should handle, the prompt
that misled it, the missing measurement, the doctrine gap it had to work around. A near-duplicate of an open
entry is refused and points at the existing one, so a recurring defect accumulates evidence instead of copies.
Working around a deficiency silently is what the doctrine forbids; filing it is the sanctioned move.

This is the channel that makes the harness improvable by whoever runs it: a fork learns what to build next from
its own ledger, and a hard problem often needs one specific tool that only the run itself can identify. Entries
are reviewed between runs and acted on by you, never by the agent mid-flight.

## Upstream contributions are a different channel
A lemma Mathlib does not have is not a request to change Quod — it is a pull request to Mathlib. Those are
tracked separately, with whatever the run already proved attached, so the PR is a packaging job rather than a
re-derivation:

```sh
quod contribute add mathlib --title … --statement … --why … --have "claim ids, artifact paths"
quod contribute list --status candidate
quod contribute set <id> submitted --url https://github.com/leanprover-community/mathlib4/pull/…
quod contribute export                 # → local/reports/CONTRIBUTIONS.md
```
Targets: `mathlib`, `formal-conjectures`, `oeis`, `other`. `quod obstruction add --kind missing_mathlib` says so
when it records the blocked step, and `quod ledger add` says so if a proposal reads like an upstream gap — the
ledger is for the harness, this is for everyone else's repositories.

## Open problems, and why a run cannot quietly give up
Most targets worth attacking are open. A harness that scores only completed proofs teaches its agent to stop
early and call the problem impossible, so Quod scores and enforces the alternative:

- `quod obstruction add` records a *named* obstruction — the blocked step, what was tried, and the concrete
  thing that would remove it. "Too hard" is rejected by the model; a verdict on difficulty is not an `unblock`.
- `quod epoch end` refuses an epoch that recorded nothing at all (exit 3), and `quod branch status closed`
  refuses a branch with fewer than two distinct failed strategies or no obstruction. `--force` exists for
  humans, and logs the override.
- Branches tagged `programme` / `open-problem` are scored by what they deliver: one point per kernel-proved
  lemma, one per cleared obstruction, a quarter per newly named one — not a fraction of an unreachable target.
- `quod difficulty -b <branch>` gates the token-expensive machinery: strategy cards and `quod propose` decline
  on a target that has not resisted yet, and are always available on a declared-hard branch.

## The ladder in one demo
```sh
pixi run demo
```
Solves the 1D harmonic oscillator numerically (→ `empirical`, honestly: no error bound), then drives two
identities through instance checks → CAS → SMT → Lean, and refutes a deliberately false negative control.

## Problem sources
`quod source search|show|open <oeis|putnam|fc>` opens a branch from OEIS (conjectured formulas), PutnamBench
(Lean statements, reference answers stripped on import) or google-deepmind/formal-conjectures (open
conjectures stated in Lean, imported and frozen). `bench/problems/*.toml` are blind rediscovery definitions —
known results with names stripped — used to compare harness variants during construction.

## Layout
```
CLAUDE.md            doctrine (read by every session)
.claude/             settings, hooks, skills, adversary agent
src/quod/            models · state · claims · expand · engines · attack · critic · formal · formalize ·
                     library · strategy · propose · trace · ingest · bench · sources · cli
formal/              pinned Lean/Mathlib project; Scratch/ receives checked snippets
bench/problems/      blind rediscovery benchmark definitions
docs/                ARCHITECTURE.md · CONTRACTS.md · CLAIMS.md · EVALUATION.md
scripts/             bootstrap · formal-setup · run-epoch · programme · ablation · report generators
state/ artifacts/    research state and large outputs (git-ignored)
local/               generated reports and notes (git-ignored)
```

## Status
Runs on Apple-silicon hardware with the pinned environment: Python 3.14, SymPy/Z3/mpmath/FLINT/Sage, MLX on
Metal, Lean 4.33.1 + Mathlib. `pixi run doctor`, `pixi run test` and `pixi run demo` are the gate before any
research run.

MIT.
