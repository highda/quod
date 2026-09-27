# Contracts

What the harness guarantees, and what it demands in return. These are enforced in code, not in prose: if a
statement here is false, that is a bug worth filing. Module paths are under `src/quod/`.

## 1. The rigor ladder

```
speculative → empirical → numerically_certified → {symbolically_verified | solver_verified} → formally_proved
                                                                                    refuted (terminal)
```

`models.Claim.promotion_error` is the single arbiter; `state.StateStore.promote/refute/add_claim` call it and
the CLI cannot bypass it.

| rung | evidence kinds that earn it |
|---|---|
| `empirical` | `numeric`, `instance_check`, `finite_difference`, `sampling`, `gpu_batch` |
| `numerically_certified` | `interval`, `certified_numeric` |
| `symbolically_verified` | `cas` |
| `solver_verified` | `smt` |
| `formally_proved` | `lean` |
| `refuted` | `counterexample` |

Invariants:

- Rigor moves **up only**. `symbolically_verified` and `solver_verified` are peers; neither dominates.
- `refuted` is terminal. A changed statement is a **new claim** with `--supersedes`, never an edit.
- A claim may not be *created* at a rung without the evidence that rung requires.
- Promotion checks evidence **kind**, never its prose. Hand-typed `cas`/`smt`/`lean`/`counterexample` evidence
  with a `--produced-by` that is not an engine is a fabrication, and the doctrine forbids it.

## 2. The kernel contract

`formal.check` is the only path to `formally_proved`, and it holds a file to all of:

- no `sorry` (a file containing it yields `statement_ok`/`statement_error` — a well-formed statement, not a
  proof), and none of `axiom`, `native_decide`, `sorryAx`, `implemented_by`, `extern`, `unsafe`;
- `#print axioms` for every declaration ⊆ `{propext, Classical.choice, Quot.sound}`;
- **frozen signatures** (`FormalClaim.frozen`) present verbatim — an imported target's statement may have its
  `sorry`s filled, never its statement altered;
- `compiled` is distinct from `proved`: a file that compiles but declares no theorem proved nothing;
- on attach, the **stored form must recompile**. The claim stores self-contained text (non-Mathlib imports
  kept), so a proof that only worked inside its original file is rejected.

Verdicts: `proved | compiled | statement_ok | statement_error | failed | unavailable`.
Budget: `[formal] timeout` in `quod.toml`, `QUOD_LEAN_TIMEOUT`, or `--timeout`.

**Faithfulness is not mechanical.** The kernel checks that the Lean statement holds, not that it says what the
claim's sentence says. A proof whose Lean statement is a restatement or a narrowing requires an adversary pass
on faithfulness before the claim is cited or added to the library.

## 3. The claim language

`FormalClaim` is the engine-readable contract (full grammar in `CLAIMS.md`): `variables` maps every symbol —
bound indices included — to a domain (`N`, `N+`, `Z`, `Q`, `R`, `R+`, `C`); `hypotheses` and `conclusion` are
SymPy-parsable over exactly those symbols; `lean` is an optional Lean 4 statement. No implicit symbol creation:
an undeclared name is an error, not a fresh symbol. Claims are parsed with SymPy evaluation **off**, so a
conclusion is never silently decided during parsing; engines decide it afterwards, and a conclusion SymPy
settles from domain assumptions alone is reported `trivial` rather than credited to the solver.

## 4. Engines

`attack.run` executes instances → CAS → SMT → Lean, records one `AttackReport` per step, attaches evidence and
promotes as far as the evidence allows, stopping at the first refutation. Each step has a wall-clock cap
(`--engine-timeout`, `QUOD_ENGINE_TIMEOUT`, default 180 s, `0` disables) and reports `inconclusive` on
overrun. An engine that cannot handle a claim says `unsupported`; it never approximates silently.

## 5. Roles

| role | isolation | tools | what its output is worth |
|---|---|---|---|
| proposer | the session itself | full | nothing by itself; its claims need evidence |
| adversary (`quod critic`) | fresh process, own cwd, no MCP, `QUOD_ROLE=adversary` so hooks stand down | **none** | a verdict `refuted \| weakened \| survived \| inconclusive` + concrete objection, stored as an attack |
| formalizer (`quod formalize`) | fresh process, no MCP, `QUOD_ROLE=formalizer` | Bash, Read, Write, Edit, Glob, Grep | **a file, not a verdict.** The harness re-runs the kernel on it and re-checks frozen signatures; the session's `RESULT: proved <path>` line is a pointer, never evidence |

The adversary receives the dossier and the claim, never the proposer's transcript. Model identity never enters
claims, branches or evidence text. A role left unset in `quod.toml` falls back to the proposer's model, and the
behaviour is then identical to that role not existing.

## 6. State

`state/harness.sqlite3` (WAL) is authoritative; model context is disposable. Tables: `branches`, `epochs`,
`claims`, `checkpoints`, `attacks`, `obstructions`, `ledger` (harness proposals), `contributions` (upstream
gaps), `events` (append-only), `turns`, `tool_calls`.
`state/library.sqlite3` holds the cross-problem library (FTS5) and strategy cards. Both anchor to the harness
root (`QUOD_ROOT`, else the directory holding `pixi.toml` + `CLAUDE.md`) — never the working directory.

`quod` is the **only** writer. Reading the databases directly, or writing them by hand, voids every guarantee
above; the doctrine forbids it and the settings deny the paths.

## 7. Stopping conditions

An open target may be left open, but not silently:

- `quod epoch end` refuses (**exit 3**) an epoch in which nothing was recorded — no claim, evidence, attack,
  failed strategy, obstruction or ledger entry. `--force` ends it and logs an `epoch.empty` event.
- `quod branch status closed|abandoned` refuses without ≥ 2 distinct failed strategies **and** ≥ 1 obstruction.
  `proved`/`refuted` are never gated. `--force` is the human override and is logged.
- `Obstruction` requires a blocked step and an `unblock` of ≥ 25 characters each, and rejects a verdict on
  difficulty in `unblock`. `FailedStrategy.reason` requires ≥ 20 characters; "approach failed" is refused.
- `quod obstruction clear` requires ≥ 15 characters saying how it was removed.

## 8. Measurement

`quod trace` attributes progress by dependency closure and reports η. Progress is in target units: proof = 5;
otherwise partial credit clamped below a proof, except on branches tagged `programme`/`open-problem`, which
score 1 per kernel-proved lemma, 1 per cleared obstruction, 0.25 per newly named one. A gap between turns
longer than 300 s counts as idle **only** if no tool call spans it. Usage is summed over every result envelope
in a run log, and from per-turn usage when a run was cut off before writing one. Every generated report carries
a provenance header (`quod.provenance`); figures are comparable only within one header.

## 9. Ablation

`QUOD_ABLATE=engines,adversary,strategy` makes the affected commands **refuse** with exit 3 rather than
degrade, and the runner announces the restriction in the epoch prompt. A cell therefore cannot quietly use the
feature it is meant to be without.

## 10. Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | invalid request: unknown branch/claim, bad rigor transition, malformed claim, library rejection |
| 2 | the work was done and the answer is no: kernel did not accept, frozen statement violated, delegated formalization failed, demo rung misbehaved |
| 3 | refused by policy: feature ablated, empty epoch, duplicate ledger entry, difficulty gate |

JSON on stdout, diagnostics on stderr. Every command prints either a JSON object or Markdown intended to be
read back by a model without guessing.

## 11. Toolbox freeze, and the two outward channels

The toolbox does not change during a research run. What a run finds instead goes to one of two places, and they
are not interchangeable:

| channel | for | not for |
|---|---|---|
| `quod ledger add` | changes to **this harness**: a missing tool, a bad prompt, a doctrine gap, a bug, an engine that cannot handle a shape it should | anything another project owns |
| `quod contribute add <target>` | gaps in **another project** — a lemma Mathlib lacks, a `sorry` a conjecture repository carries, an OEIS comment — with `--have` naming the kernel-accepted artifact this run already produced | changes to the harness |

The ledger refuses a near-duplicate of an open entry and points at it (`quod ledger append <id>` adds evidence
to the existing one). A contribution moves `candidate → drafted → submitted → merged | declined | superseded`,
and `submitted`/`merged` require the pull-request url. Both are acted on by humans between runs; nothing is
worked around silently, and neither channel lets the agent modify the toolbox mid-run.
