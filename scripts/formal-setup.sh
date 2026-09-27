#!/bin/zsh
# Set up the pinned Lean/Mathlib project. Uses the Mathlib build cache; never compiles Mathlib from source.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/formal"
command -v elan >/dev/null || {
  echo "Install elan from the official leanprover/elan project, then rerun." >&2
  exit 2
}
elan toolchain install "$(cat lean-toolchain)"
if [ -f lake-manifest.json ]; then
  echo "lake-manifest.json present: reproducible dependency set."
else
  echo "No lake-manifest.json: resolving Mathlib rev from lakefile.toml. Commit the generated manifest."
  lake update
fi
lake exe cache get
lake build
# formal-conjectures' own libraries are not in the Mathlib cache; build the small support libs once
# so `import FormalConjectures.*` resolves. Individual conjecture files compile on demand.
lake build FormalConjecturesUtil FormalConjecturesForMathlib
# LeanHammer: premise selection + aesop/grind/Duper/cvc5. `quod formal hammer` loads the cvc5 FFI library itself.
lake build Hammer
echo "Lean OK. Re-run \`pixi run demo\`: identities should now reach formally_proved."
