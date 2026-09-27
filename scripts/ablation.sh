#!/bin/zsh
# Controlled ablation campaign. Cells: full | no-engines | no-strategy | respawn.
#   scripts/ablation.sh <reps> <putnam_id>...        e.g. scripts/ablation.sh 2 putnam_1975_a1 putnam_2006_b2
# Each (cell, problem, rep) gets its own branch abl-<cell>-<prob>-r<k>, tagged ablation:<cell>, run sequentially.
# Before each run the last observed five-hour utilization is checked; above 85% the campaign sleeps until reset.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
export PATH="$HOME/.elan/bin:$PATH"
reps="${1:?reps}"; shift
# An ablation varies one feature at a time, so the model comes from configuration like everywhere else.
model="${QUOD_PROPOSER_MODEL:-$(pixi run --quiet quod config models.proposer 2>/dev/null || echo opus)}"
cells=(full no-engines no-strategy respawn)

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

for k in $(seq 1 "$reps"); do
  for pid in "$@"; do
    prob="${pid#putnam_}"; prob="${prob//_/-}"
    for cell in "${cells[@]}"; do
      b="abl-${cell}-${prob}-r${k}"
      if ! pixi run --quiet quod branch show "$b" >/dev/null 2>&1; then
        pixi run --quiet quod source open putnam "$pid" --branch "$b" >/dev/null || { echo "open failed: $b"; continue; }
        pixi run --quiet quod branch tag "$b" "ablation:${cell}" "ablation-rep:${k}" >/dev/null
      fi
      quota_wait
      echo "=== START $b $(date +%H:%M:%S)"
      case "$cell" in
        full)        QUOD_ABLATE=""          pixi run --quiet ./scripts/run-epoch.sh "$b" "$model" 160 1 ;;
        no-engines)  QUOD_ABLATE="engines"   pixi run --quiet ./scripts/run-epoch.sh "$b" "$model" 160 1 ;;
        no-strategy) QUOD_ABLATE="strategy"  pixi run --quiet ./scripts/run-epoch.sh "$b" "$model" 160 1 ;;
        respawn)     QUOD_ABLATE=""          pixi run --quiet ./scripts/run-epoch.sh "$b" "$model" 40 4 ;;
      esac
      echo "=== END $b $(date +%H:%M:%S)"
    done
  done
done
echo "ABLATION_DONE"
