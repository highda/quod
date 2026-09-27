#!/bin/zsh
# Resolve the `quod` entry point without assuming the pixi env is activated.
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
# An explicit QUOD_ROOT (tests, multi-root setups) wins over the project dir.
export QUOD_ROOT="${QUOD_ROOT:-$ROOT}"
if command -v quod >/dev/null 2>&1; then
  exec quod "$@"
elif [ -x "$ROOT/.pixi/envs/default/bin/quod" ]; then
  exec "$ROOT/.pixi/envs/default/bin/quod" "$@"
elif command -v pixi >/dev/null 2>&1; then
  cd "$ROOT" && exec pixi run --quiet quod "$@"
else
  cd "$ROOT" && PYTHONPATH="$ROOT/src" exec python3 -m quod "$@"
fi
