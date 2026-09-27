---
name: critic
description: Spawn the isolated model adversary on a claim (dossier + claim only, different model). Use only on claims the engines could not settle or that others will depend on; never on kernel-proved claims.
---
`quod critic $ARGUMENTS` runs `claude -p` headless with the adversary prompt, tools off, on the adversary model, and records the verdict as an attack. Then:
- `refuted`: verify the counterexample with `quod claim check`; if it holds, `quod claim refute`.
- `weakened`: create the weaker claim with `--supersedes`, attack it.
- `survived`/`inconclusive`: note the weakest point it named in the next checkpoint's obstructions.
Faithfulness passes: when a claim was promoted to formally_proved via a Lean statement that restates the
sentence rather than being its own formal block, ask the critic explicitly whether the Lean statement *is* the
claim. A mismatch is a `weakened` verdict: supersede the claim with what was actually proved.
Alternatively delegate to the `adversary` subagent with only the branch id and claim id.
