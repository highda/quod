---
name: claim
description: PROPOSE phase — draft a claim with a formal block, dry-run the engines, then add it. Use whenever you are about to assert something new.
---
Turn the statement in `$ARGUMENTS` (or the one you are about to make) into a claim the engine can read (docs/CLAIMS.md):
1. Write the `formal` JSON: every symbol declared with a domain, hypotheses as SymPy booleans, conclusion as a relation. If it truly cannot be expressed, say why in the statement and add `--tag informal`.
2. `quod claim check --formal '<json>'` — if instances refute it, do not add it; fix the statement.
3. `quod claim add <ID> -b <branch> --statement "<sentence>" --formal '<json>' [--assume ...] [--depends-on ...]`.
4. Immediately `quod attack <ID>`.
Claim ids: `C-<TOPIC>-<NNN>`. One claim, one statement. Lemmas are separate claims joined with `--depends-on`.
When you have several claims ready, add and attack them in ONE shell call (a `for`/heredoc), not one per turn.
