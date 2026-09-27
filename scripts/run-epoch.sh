#!/bin/zsh
# Run one headless proposer epoch on a branch and record measured token usage.
#   scripts/run-epoch.sh <branch> [model] [max-turns] [max-epochs]
# QUOD_ABLATE=engines,adversary,strategy (env) runs a controlled ablation: the CLI refuses the disabled
# features and the prompt tells the proposer they are unavailable.
# With max-epochs > 1 the runner re-launches a fresh epoch (which ORIENTs from the last checkpoint) until the
# branch's target claim is formally_proved, the branch is closed, or the budget is spent.
# Logs to state/runs/<branch>.<timestamp>.jsonl (git-ignored). Sequential by design: Lean builds and the
# state DB are shared. Exit code is the Claude CLI's.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
branch="${1:?branch id}"
model="${2:-$(quod config models.proposer 2>/dev/null || echo opus)}"
turns="${3:-60}"   # measured: a 40-turn cap matched a 160-turn cap on PutnamBench at 13% less cost
# The CLI turn cap is not a reliable ceiling (a run has finished `success` past its nominal cap), so an epoch also gets a wall-clock budget. 0 disables it.
max_minutes="${QUOD_EPOCH_MAX_MIN:-120}"
max_epochs="${4:-1}"
mkdir -p state/runs

target_done() {
  (quod branch show "$branch" 2>/dev/null || pixi run --quiet quod branch show "$branch") | python3 -c '
import sys, json
d = json.load(sys.stdin)
closed = d["branch"]["status"] in ("closed", "proved", "refuted", "abandoned")
proved = any(c.get("rigor") == "formally_proved" and "target" in c.get("tags", []) for c in d["claims"])
sys.exit(0 if (closed or proved) else 1)'
}
export PATH="$HOME/.elan/bin:$PATH"
export QUOD_BRANCH="$branch"   # harness tool calls are attributed to this branch
ablate="${QUOD_ABLATE:-}"
ablation_note=""
case ",$ablate," in
  *,engines,*) ablation_note="$ablation_note ABLATION: the compute fabric is disabled for this run — do not run python/sage computations and do not call quod attack, quod claim check, quod formal hammer or quod propose (they will refuse); decide claims by reasoning and Lean only." ;;
esac
case ",$ablate," in
  *,adversary,*) ablation_note="$ablation_note ABLATION: the adversary is disabled — do not call quod critic." ;;
esac
case ",$ablate," in
  *,strategy,*) ablation_note="$ablation_note ABLATION: strategy cards are disabled — skip quod strategy match." ;;
esac
if [ -n "$ablate" ]; then
  # An ablation cell must not read another run's proof: move untracked formal/Quod/*.lean aside.
  stray=$(git ls-files --others --exclude-standard formal/Quod 2>/dev/null | grep '\.lean$' || true)
  if [ -n "$stray" ]; then
    mkdir -p state/attic
    echo "$stray" | while IFS= read -r f; do [ -f "$f" ] && mv "$f" "state/attic/$(basename "$f")"; done
    echo "{\"moved_stray_lean\": $(echo "$stray" | wc -l | tr -d ' ')}"
  fi
fi
epoch=0
code=0
while [ "$epoch" -lt "$max_epochs" ]; do
  epoch=$((epoch + 1))
  if [ "$epoch" -gt 1 ] && target_done; then break; fi
log="state/runs/${branch}.$(date +%Y%m%dT%H%M%S).jsonl"
prompt="You are the PROPOSER in the Quod harness. CLAUDE.md is your doctrine; .claude/skills/formalize/SKILL.md is your Lean guide. Work branch \`${branch}\` through ONE epoch: ORIENT (quod dossier -b ${branch}; quod strategy match -b ${branch}; quod epoch start -b ${branch} --model ${model}), then advance the branch objective. If the target resists a direct attempt, run quod propose -b ${branch} --n 12 once and work only its survivors.
NO SURRENDER: never stop because the target looks hard, famous or open. If you cannot prove the whole statement, prove the strongest reachable piece (special case, finite analogue, weaker bound) in Lean for real, and record the gap with quod obstruction add --kind ... --statement ... --unblock ... . Difficulty is not a result; an obstruction is. An epoch that records nothing is refused by quod epoch end, and quod branch status closed is refused without recorded failed strategies and an obstruction. Never do anything for show. Compute before you reason; write claims with formal blocks; quod attack every claim; record every failed strategy with the exact error; run quod critic only on claims the engines cannot settle or whose Lean statement restates the sentence. Reach formally_proved on the target when you can. When context grows heavy or you are stuck after several distinct approaches, CHECKPOINT (quod checkpoint create with exact obstructions and three next actions), then \`quod epoch end -b ${branch} --reason ...\` (tokens 0; the runner measures) and stop. Use \`pixi run --quiet quod ...\` and \`pixi run --quiet python ...\`. Never install anything. Never read state/ directly. Never hand-type engine-kind evidence.${ablation_note}"
printf '%s' "$prompt" | claude -p --model "$model" --output-format stream-json --verbose --max-turns "$turns" \
  --no-session-persistence > "$log" 2>&1 &
cli_pid=$!
if [ "$max_minutes" -gt 0 ]; then
  ( sleep $((max_minutes * 60)); kill -TERM "$cli_pid" 2>/dev/null && echo "{\"killed\": \"wall clock ${max_minutes}m\"}" ) &
  watchdog=$!
fi
wait "$cli_pid"
code=$?
[ -n "${watchdog:-}" ] && kill "$watchdog" 2>/dev/null
(quod epoch ingest -b "$branch" --usage-file "$log" || pixi run --quiet quod epoch ingest -b "$branch" --usage-file "$log") >/dev/null 2>&1
if [ "$code" -ne 0 ]; then
  # The agent never reached CHECKPOINT/epoch end, so close the epoch here or its wall time and handoff are
  # lost. epoch mark takes the state-derived auto-checkpoint first.
  q() { quod "$@" 2>/dev/null || pixi run --quiet quod "$@"; }
  q epoch mark -b "$branch" --reason "epoch cut off (exit $code)" >/dev/null 2>&1
  # no --usage-file here: the ingest above already wrote the measured tokens, and epoch end *adds* to them
  q epoch end -b "$branch" --reason "cut off by the runner (exit $code): no checkpoint from the agent" \
      --force >/dev/null 2>&1
fi
echo "{\"branch\": \"$branch\", \"epoch\": $epoch, \"exit\": $code, \"log\": \"$log\"}"
  target_done && break
done
exit $code
