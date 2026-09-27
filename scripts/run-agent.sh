#!/bin/zsh
# Start a Claude Code session on the harness as the proposer.
#   pixi run agent                 -> proposer model from quod.toml / QUOD_PROPOSER_MODEL (default opus)
#   pixi run agent fable           -> override for this session
#   pixi run agent opus --resume   -> extra args go to the Claude CLI
# ANTHROPIC_CLI overrides the binary.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
resolve() { quod config "models.$1" 2>/dev/null || PYTHONPATH="$ROOT/src" python3 -m quod config "models.$1" 2>/dev/null || echo opus; }
if [ $# -gt 0 ] && [[ "$1" != -* ]]; then
  model="$1"; shift
else
  model="$(resolve proposer)"
fi
case "$model" in
  opus|fable|sonnet|haiku|claude-*) ;;
  *) echo "usage: $0 [opus|fable|sonnet|haiku|<model-id>] [claude args...]" >&2; exit 2 ;;
esac
adversary="$(resolve adversary)"
cli="${ANTHROPIC_CLI:-claude}"
command -v "$cli" >/dev/null || { echo "$cli not found; install and log in to the Claude CLI first" >&2; exit 127; }
export QUOD_ROOT="$ROOT"
echo "Harness root: $ROOT"
echo "Proposer model: $model · adversary model: $adversary (quod.toml / QUOD_*_MODEL)"
[ "$model" = "$adversary" ] && echo "note: proposer and adversary are the same model; see README → Ideal configuration"
echo "Doctrine: CLAUDE.md · hooks and skills: .claude/"
exec "$cli" --model "$model" "$@"
