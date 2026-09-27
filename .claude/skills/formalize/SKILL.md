---
name: formalize
description: Write a Lean 4 proof for a verified claim against the PINNED Mathlib and get it kernel-checked. Use when a claim reaches symbolically/solver verified, or when a Lean attempt was rejected.
---
Target claim: `$ARGUMENTS`. The toolchain is `formal/lean-toolchain` + the Mathlib tag in `formal/lakefile.toml`;
write for that version, not from memory. What the kernel rejected in past runs is in the dossier's failed strategies.

Syntax that differs from older training data (pinned Mathlib):
- Big operators bind with `∈`: `∑ k ∈ Finset.range n, f k`, `∏ i ∈ s, g i` (never `∑ k in`).
- Casts are explicit: `((n : ℕ) : ℝ)`; use `push_cast`, `norm_cast`, `exact_mod_cast`.
- Natural subtraction truncates; state over ℤ/ℝ or add hypotheses rather than fighting it.
- Renames seen on this toolchain: `push_neg` → `push Not`; `le_or_lt` → `le_or_gt`; `strictAnti_of_deriv_neg`
  no longer takes a `Continuous` argument. When a name is unknown, `exact?` or `#check @old_name` before guessing.

Find the lemma before writing it: `quod formal search 'Finset.sum_range_succ'` or by shape
`quod formal search '_ ^ 2 ≤ _ ^ 2 ↔ _'` (Loogle). Loogle indexes current Mathlib, which may be newer than the pinned one: confirm a name with
`quod formal check --statement '#check @Name'` (verdict `compiled`) before building on it. Inside Lean, `exact?`, `apply?`, `rw?`.
Read diagnostics with `quod formal check --raw ...` (errors first, plain text).

The checker rejects `axiom`, `native_decide`, `unsafe`, non-standard axioms (`#print axioms` must be within
propext / Classical.choice / Quot.sound) and any change to a frozen imported statement. Do not try to work around it.

Hammer: for a goal you cannot close, write its proof as `by hammer` and run
`quod formal hammer --statement '...'`. It returns `Try this:` tactic scripts (e.g. `grind only [sq_le_sq]`) found by
premise selection + aesop/grind/SMT; paste the suggestion in place of `hammer` and `quod formal check`. It will not
invent witnesses for existentials; give it closed goals. ~15 s per call.

Automation to try, cheapest first: `norm_num`, `simp`, `ring` / `ring_nf`, `omega` (linear ℕ/ℤ), `decide` (finite),
`positivity`, `nlinarith [sq_nonneg (a - b), ...]` (polynomial inequalities — feed it the squares), `linarith`,
`field_simp` then `ring`, `induction n with | zero => simp | succ m ih => rw [Finset.sum_range_succ, ih]; ring`,
`exact?` / `apply?` to find the Mathlib lemma, `aesop`.

Loop:
1. Draft with `sorry` first to check the *statement* compiles: `quod formal check --statement '...'` (verdict `sorry` = statement OK).
2. Replace `sorry` with the proof; `quod formal check` again. Read the error position, fix, repeat. Three rejections
   with the same error → record `quod fail-strategy` with the exact message and change approach.
3. On `proved`: `quod formal attach <claim> <path>` (it promotes and stores the accepted source). Then `quod library add`.

## Pinned-toolchain quirks
- `deriving Fintype` fails on enum inductives at the pinned Mathlib (the derived `enumList` lemma gets the
  wrong coercion depth). Write the instance by hand: `instance : Fintype X := ⟨{a, b, c}, by decide⟩`, or
  `Fintype.ofList [a, b, c] (by decide)`. Seen while building finite TM2 label/state types.
- A proof over an imported conjecture module must keep its own `import` lines: `quod formal attach` stores the
  text with non-Mathlib imports preserved, and re-checks it, so do not strip them when you hand it the file.
