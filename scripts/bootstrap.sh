#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PIXI_VERSION="0.79.0"
if ! command -v pixi >/dev/null 2>&1; then
  echo "Pixi is required. Install pixi ${PIXI_VERSION} (or newer) from the official prefix-dev/pixi release, then rerun." >&2
  echo "This script intentionally does not curl|sh arbitrary installers." >&2
  exit 2
fi
actual="$(pixi --version | awk '{print $2}')"
[ "$actual" = "$PIXI_VERSION" ] || echo "note: recipe validated for pixi $PIXI_VERSION; found $actual"
if [ -f pixi.lock ]; then
  pixi install --locked
else
  echo "No pixi.lock yet: solving from pixi.toml (maintainers: run \`pixi lock\` and commit it)."
  pixi install
fi
pixi run init
pixi run doctor
pixi run test
pixi run demo
echo
echo "Green."
pixi run --quiet quod doctor | python3 -c 'import sys,json
for h in json.load(sys.stdin).get("hints", []): print("  !", h)'
echo
echo "Next: pixi run agent   (first launch shows Claude Code's trust dialog — accept it, then /orient)"
