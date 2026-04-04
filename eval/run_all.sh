#!/usr/bin/env bash
# Run all evals sequentially. Usage: ./eval/run_all.sh
# Results saved to eval/results/{version}_{split}.json
# Logs saved to eval/results/run_all.log

set -e
cd "$(dirname "$0")/.."

# Load API key
export OPENAI_API_KEY=$(grep OPENAI_API_KEY .env | cut -d= -f2)

LOG="eval/results/run_all.log"
mkdir -p eval/results

echo "Starting full eval run at $(date)" | tee "$LOG"
echo "==========================================" | tee -a "$LOG"

runs=(
  "v1 dev 32"
  "v2 dev 32"
  "v1 multiturn 0"
  "v2 multiturn 0"
  "v3 multiturn 0"
)

for run in "${runs[@]}"; do
  read -r version split limit <<< "$run"

  echo "" | tee -a "$LOG"
  echo "[$(date '+%H:%M:%S')] Starting $version $split..." | tee -a "$LOG"

  args="--version $version --split $split --judge-model gpt-4.1"
  if [ "$limit" -gt 0 ] 2>/dev/null; then
    args="$args --limit $limit"
  fi

  python3 eval/eval.py $args 2>&1 | tee -a "$LOG"

  echo "[$(date '+%H:%M:%S')] Completed $version $split" | tee -a "$LOG"

  # Cooldown between runs to reset rate limits
  echo "Cooling down 60s..." | tee -a "$LOG"
  sleep 60
done

echo "" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
echo "All evals completed at $(date)" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# Print comparison
echo "Generating comparison..." | tee -a "$LOG"
python3 eval/eval.py --compare --split dev 2>&1 | tee -a "$LOG"
