# Architecture

```
  claude (proposer)                        ─┐
  claude -p (adversary: no tools,           │
             dossier + claim only)          ├──►  quod CLI  ──►  state/*.sqlite3   (authoritative)
  claude -p (formalizer: Lean tools,        │        │            artifacts/       (large outputs)
             one claim)                    ─┘        │            local/reports/   (your write-ups)
                                                     │
  models per role: quod.toml                         ├──►  formal/   Lean 4 + Mathlib, the only "proved"
                                                     └──►  engines   SymPy · Z3 · mpmath/iv · FLINT · Sage · MLX
```

**State.** `state/harness.sqlite3` (WAL) holds branches, epochs, claims (with evidence), checkpoints,
attacks, obstructions, the toolbox ledger, the upstream-contribution catalog and an append-only event log. `state/library.sqlite3` is the cross-problem
library (FTS5-indexed). Both are anchored at the harness root (`QUOD_ROOT` or the directory containing
`pixi.toml` + `CLAUDE.md`), never the cwd.

**Rigor enforcement.** `models.Claim.promotion_error` is the single source of truth for what may move where.
`StateStore.promote/refute/add_claim` call it. The CLI cannot bypass it.

**Engine ATTACK.** `attack.run` executes instances → CAS → SMT → Lean, records an `AttackReport` per step,
attaches evidence and promotes as far as the evidence allows. It stops at the first refutation.

**Model adversary.** `critic.run` renders the dossier + claim into a fixed prompt and runs
`claude -p --tools "" --model <adversary>` (with `QUOD_ROLE=adversary`, which makes the hooks stand down); the verdict is stored as an attack by `model:<name>`.
Independence = context isolation + different model. `quod attackers` reports hit rates.

**Claude Code wiring.** `.claude/settings.json` allowlists `quod`/`pixi run`/`lake` and installs hooks:
`SessionStart` prints `quod doctor` + the open-branch dossiers into context; `PreCompact` and `Stop` run
`quod epoch mark`, an auto-checkpoint derived from state. Skills in `.claude/skills/` are the operating
cycle as commands (`/orient`, `/claim`, `/attack`, `/critic`, `/checkpoint`, `/ledger`).
`.claude/agents/adversary.md` is the in-session subagent variant (its `model:` line is set by hand).

**Instrumentation and search layer.** Every epoch's Claude CLI stream is ingested (`quod epoch ingest`):
per-turn context occupancy and per-tool-call wall time; harness tool calls are also timed in-process under
`QUOD_BRANCH`. `quod trace` attributes progress by dependency closure (helper declarations of an accepted proof are
auto-registered as claims), splits an epoch into exploration / recognition / verification with idle gaps removed,
and reports η in target units per Mtok and per hour. `quod strategy` keeps methodology cards (structural triggers,
procedures, anti-strategies) with outcomes computed from the closure; `quod strategy extract` distils them post-run
with a cheap model. `quod critic --mode` selects an attack mode (logical, assumption, counterexample,
representation, compressor, fresh) by a rule table on the claim's state. `quod propose` asks the model once for
many distinct approaches with machine-runnable experiments and lets the engines (and LeanHammer) kill or keep them.

**Persistence layer (anti-surrender).** Open problems are the normal case, so the harness makes stopping
expensive and being blocked cheap. `Obstruction` (`state.add_obstruction`/`clear_obstruction`) is a durable
record of a blocked step with its `unblock`; the model rejects a difficulty verdict in that field.
`set_branch_status` refuses `closed`/`abandoned` without two distinct failed strategies and an obstruction;
`epoch_activity` drives `quod epoch end`'s refusal of an epoch that recorded nothing (exit 3, `--force` logs
`epoch.empty`). `state.difficulty` is the gate on the expensive machinery: `quod strategy match` and
`quod propose` decline until a target has resisted (failed strategy, two Lean rejections, second epoch,
refutation, open obstruction) or the branch is tagged `hard`/`open-problem`/`programme`. `trace.analyze`
scores programme branches by kernel-proved lemmas and cleared obstructions rather than by a fraction of the
unreachable target, and never mistakes a proved lemma there for the goal.

**Where a run's output goes.** `state/` (SQLite), `artifacts/` (large outputs), `local/reports/` (report
generators, `quod ledger export`, `quod library export`) and run-written `formal/Quod/*.lean`. All git-ignored;
nothing in the tree depends on them.

**Delegated formalization.** `formalize.run` spawns a tool-enabled session (`QUOD_ROLE=formalizer`, no MCP) on
one claim, parses its `RESULT:` line for a path, then re-runs `formal.check` on that file and re-checks frozen
signatures itself; `--attach` continues through the normal attach/promote path. The role defaults to the
proposer's model, in which case the split changes nothing but the context.

**Backend policy.** CPU/exact for symbolic, integer, irregular, proof and high-precision work; MLX/Metal for
large batched approximate work (`compute.choose_backend`); GPU survivors are rechecked exactly.

**Kernel discipline.** `formal.check` refuses `axiom`/`native_decide`/`unsafe`, appends `#print axioms` and fails on
anything beyond propext/Classical.choice/Quot.sound, and enforces frozen statements on imported targets.
`formal.hammer` runs LeanHammer (a Lake dependency, before Mathlib so shared deps resolve to Mathlib's revs) and
returns its `Try this:` suggestions; final proofs never import Hammer. `formal.search` queries Loogle.

**Reproducibility.** `pixi.toml` pins top-level versions verified against conda-forge for `osx-arm64`/py3.14;
`pixi.lock` (to be generated on hardware) is authoritative. Lean/Mathlib are a separate pinned graph in
`formal/` with `lake-manifest.json` committed after the first `lake update`; `scripts/formal-setup.sh` uses
`lake exe cache get` so Mathlib is never built from source on a laptop.

**Benchmark (construction phase only).** `bench/problems/*.toml` are known results with names stripped.
`quod bench start <id>` opens a tagged branch; `quod bench score` reports proved claims per token and
attacker hit rates. Used to compare toolbox/doctrine variants; frozen during active research.
