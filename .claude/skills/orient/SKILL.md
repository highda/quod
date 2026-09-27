---
name: orient
description: ORIENT phase — read the branch dossier and library, start an epoch. Use at the beginning of any research session or when switching branch.
---
Run, in order, and read the output before anything else:
1. `quod branch list --status open` — pick the branch (argument `$ARGUMENTS` if given).
2. `quod dossier -b <branch>` — this is your memory. If it says AUTO, verify open goals and next actions against `quod claim list -b <branch>` before trusting them.
3. `quod library search "<two or three key terms from the objective>"` and `quod strategy match -b <branch>` —
   methodology cards whose structural trigger matches this problem, with their historical hit rates. If you use one,
   `quod strategy record <id> -b <branch> --lemma <claim-id>...` at CHECKPOINT so its outcome is accounted.
4. `quod epoch start -b <branch> --model <the model you are running as>`.
Then state in one paragraph: objective, established claims, the single most valuable next action, and the cheapest experiment that could kill it. Do not open the transcript of any previous session.
