#!/bin/zsh
# SessionStart: put the toolbox status and every open branch's dossier into context (ORIENT).
# stdout of a SessionStart hook is added to the model's context.
# Adversary sessions spawned by `quod critic` must not orient or checkpoint the proposer's branches.
[ "${QUOD_ROLE:-}" = "adversary" ] && exit 0
HERE="$(cd "$(dirname "$0")" && pwd)"
M="$HERE/quod.sh"
echo "## Harness doctor"
"$M" doctor 2>/dev/null || echo "(doctor unavailable — run pixi run doctor)"
echo
branches=$("$M" branch list --status open 2>/dev/null | python3 -c 'import sys,json
try:
    print(" ".join(b["id"] for b in json.load(sys.stdin)))
except Exception:
    pass')
if [ -z "$branches" ]; then
  echo "## No open branches. Start with: quod branch new <id> --problem ... --objective ..."
else
  for b in ${(z)branches}; do
    "$M" dossier -b "$b" --max-chars 6000 2>/dev/null
    echo
  done
  echo "_Reminder: start an epoch with \`quod epoch start -b <branch> --model <model>\` before working._"
fi
exit 0
