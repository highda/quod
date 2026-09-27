---
name: attack
description: ATTACK phase — run the deterministic engines on a claim, then hunt hidden assumptions yourself. Use before any promotion past empirical.
---
For claim `$ARGUMENTS`:
1. `quod attack <claim>` and read every step. A refutation ends it: record the counterexample's meaning in a `quod fail-strategy` if a strategy depended on the claim.
2. If `smt` says `unsupported`, restate the claim polynomially or split it; if `cas` is inconclusive, try a sharper `formal` (add hypotheses that you believe are implicit — then they are explicit).
3. List, in writing, the assumptions the statement makes that are not in `hypotheses`. If any changes the meaning, supersede the claim with one that states it (`--assume` / `hypotheses`) and attack that instead.
4. If the claim survived and matters, run `/critic <claim>`.
