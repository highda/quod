#!/bin/zsh
# Programme campaign: run several epochs on each open-problem branch, in order, respecting the quota window.
#   scripts/programme.sh <epochs-per-branch> <branch>...
# Branches are expected to exist already (`quod source open fc <path> --branch <id>`, which tags them
# open-problem/programme). Each epoch ORIENTs from the previous checkpoint, so the branch accumulates:
# kernel-proved lemmas, named obstructions, failed strategies with their exact reasons.
# Progress is read with `quod trace -b <branch>` (programme scoring) and `quod obstruction list`.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
export PATH="$HOME/.elan/bin:$PATH"
epochs="${1:?epochs per branch}"; shift
# The configured proposer, not a hardcoded one: quod.toml decides which model drives research.
model="${QUOD_PROPOSER_MODEL:-$(pixi run --quiet quod config models.proposer 2>/dev/null || echo opus)}"

quota_wait() {
  local last; last=$(ls -t state/runs/*.jsonl 2>/dev/null | head -1)
  [ -z "$last" ] && return 0
  python3 - "$last" <<'EOF'
import sys, json, time
last = None
for line in open(sys.argv[1]):
    if '"rate_limit_event"' in line:
        try: last = json.loads(line)["rate_limit_info"]
        except Exception: pass
if last:
    u = last.get("unifiedWindows", {}).get("five_hour", {})
    util, reset = u.get("utilization", 0), u.get("resetsAt", 0)
    if util >= 0.85 and reset > time.time():
        wait = int(reset - time.time()) + 90
        print(f"quota {util:.0%} of five-hour window; sleeping {wait//60} min until reset", flush=True)
        time.sleep(wait)
EOF
}

for b in "$@"; do
  pixi run --quiet quod branch show "$b" >/dev/null 2>&1 || { echo "no such branch: $b (open it from a source first)"; continue; }
  for k in $(seq 1 "$epochs"); do
    quota_wait
    echo "=== START $b epoch $k/$epochs ($model) $(date +%H:%M:%S)"
    pixi run --quiet ./scripts/run-epoch.sh "$b" "$model" 60 1
    echo "=== END $b epoch $k/$epochs $(date +%H:%M:%S)"
  done
  pixi run --quiet quod trace -b "$b" || true
done
echo "PROGRAMME_DONE"
