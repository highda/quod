#!/bin/zsh
# PreCompact / Stop: derive an automatic checkpoint from state for every branch with an open epoch.
# Never blocks the caller.
# Adversary sessions spawned by `quod critic` must not orient or checkpoint the proposer's branches.
[ "${QUOD_ROLE:-}" = "adversary" ] && exit 0
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/quod.sh" epoch mark --reason "${1:-unknown}" >/dev/null 2>&1 || true
exit 0
