---
name: blueprint
description: Decompose a hard target into a DAG of formally stated lemmas before proving anything (statement-first, leaves-first). Use when a target resists a direct proof or when the first Lean draft fails on structure rather than syntax.
---
Target: `$ARGUMENTS`. Do not attempt the whole proof again. Build a blueprint:

1. Write the informal proof in 5–10 numbered steps. Each step that is not a single tactic becomes a lemma.
2. For each lemma, add a claim with a Lean statement and `sorry` as the proof, wired with `--depends-on`:
   `quod claim add <TARGET>-L1 -b <branch> --statement "..." --formal '{"lean": "theorem l1 ... := by sorry"}'`
   then `quod attack <TARGET>-L1 --no-promote` — the verdict `statement_ok` means the statement compiles. Fix
   statements before proving anything; a blueprint whose statements do not compile is worthless.
3. Close leaves first (lemmas with no dependencies), cheapest automation first (`/formalize`). Use
   `quod formal search '<shape>'` to find the Mathlib lemma before writing a proof by hand.
4. Assemble: the target's proof cites the lemmas by name (all in one file for `quod formal attach`, since the
   scratch checker compiles a single file). A leaf that will not close after three distinct attempts is a
   blueprint error, not a tactic error: split it, or restate it, and record `quod fail-strategy` with the reason.
5. Every closed lemma with a kernel proof is a `quod library add` candidate if it is general.

Pattern that has worked for "count objects with property P(n)" targets (2015 B5, 2010 A1): define an abstract
ℕ-indexed recursion or closed form as its own `def`, prove its recurrence/closed form by induction (pure arithmetic),
then prove ONE bridge lemma `count n = recursion n` by a bijection or transfer-matrix argument. Keep the
combinatorial bijection and the arithmetic in separate lemmas; never do both inside one induction.
