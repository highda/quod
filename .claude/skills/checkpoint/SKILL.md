---
name: checkpoint
description: CHECKPOINT phase — write the handoff the next epoch will read, then end the epoch. Use when context grows heavy, before stopping, or after any major result.
---
1. `quod claim list -b <branch>` to confirm the ledger of claims is what you think it is.
2. `quod checkpoint create -b <branch> --goal ... --assume ... --obstruction ... --next ... --next ... --next ... --artifact ... --notes "..."` — exactly three next actions, obstructions stated precisely (what is missing, not "hard").
3. Any deficiency you hit in the toolbox: `quod ledger add ...`.
4. `quod epoch end -b <branch> --reason "<why>" --tokens-in N --tokens-out N` (take the numbers from `/cost`).
Then stop. Do not start a new line of attack after checkpointing.
