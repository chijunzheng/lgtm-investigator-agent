#!/usr/bin/env bash
# Run all evals sequentially. Usage: ./eval/run_all.sh
# Results saved to eval/results/{version}_{split}.json
# Summaries saved to eval/results/comparison_TIMESTAMP.txt
# Full log saved to eval/results/run_all.log

set -euo pipefail
cd "$(dirname "$0")/.."

# Use project venv
PYTHON=".venv/bin/python3"

# Load API key
export OPENAI_API_KEY=$(grep OPENAI_API_KEY .env | cut -d= -f2)

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG="eval/results/run_all.log"
COMPARISON="eval/results/comparison_${TIMESTAMP}.txt"
mkdir -p eval/results

echo "Starting eval run at $(date)" | tee "$LOG"
echo "==========================================" | tee -a "$LOG"

# Balanced benchmark: 8 scenarios per failure type (32 total)
# All 4 versions for fair comparison across payment/catalog/kafka/ad
runs=(
  "v1 balanced 0"
  "v2 balanced 0"
  "v3 balanced 0"
  "v4 balanced 0"
)

for run in "${runs[@]}"; do
  read -r version split limit <<< "$run"

  echo "" | tee -a "$LOG"
  echo "[$(date '+%H:%M:%S')] Starting $version $split..." | tee -a "$LOG"

  args="--version $version --split $split --judge-model gpt-4.1"
  if [ "$limit" -gt 0 ] 2>/dev/null; then
    args="$args --limit $limit"
  fi

  $PYTHON eval/eval.py $args 2>&1 | tee -a "$LOG"

  echo "[$(date '+%H:%M:%S')] Completed $version $split" | tee -a "$LOG"

  # Cooldown between runs to reset rate limits
  echo "Cooling down 60s..." | tee -a "$LOG"
  sleep 60
done

echo "" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
echo "All evals completed at $(date)" | tee -a "$LOG"

# Generate comparison
echo "" | tee -a "$LOG"
echo "Generating comparison to $COMPARISON ..." | tee -a "$LOG"

$PYTHON -c "
import json
from statistics import mean
from pathlib import Path
from datetime import datetime

RESULTS = Path('eval/results')
out = []

out.append('Investigate CLI — Eval Results')
out.append(f'Generated: {datetime.now().strftime(\"%Y-%m-%d %H:%M\")}')
out.append('Model: gpt-5.4 (agent) / gpt-4.1 (judge, n=1 with CoT)')
out.append('Hit metric: strict (checks diagnosis block only)')
out.append('')

# --- Single-turn ---
out.append('=' * 64)
out.append('  SINGLE-TURN (n=32, dev split)')
out.append('=' * 64)
out.append('')

def avg(rs, key):
    vals = [r[key] for r in rs if key in r]
    return mean(vals) if vals else 0

for v in ['v1', 'v2', 'v3', 'v4']:
    p = RESULTS / f'{v}_balanced.json'
    if not p.exists():
        continue
    with open(p) as f:
        rs = json.load(f)
    hits = sum(1 for r in rs if r['hit'])
    pay_h = sum(1 for r in rs if r['hit'] and r['expected_root_cause']=='payment')
    pay_t = sum(1 for r in rs if r['expected_root_cause']=='payment')
    cat_h = sum(1 for r in rs if r['hit'] and r['expected_root_cause']=='product-catalog')
    cat_t = sum(1 for r in rs if r['expected_root_cause']=='product-catalog')
    easy = f\"{sum(1 for r in rs if r['hit'] and r['difficulty']=='easy')}/{sum(1 for r in rs if r['difficulty']=='easy')}\"
    med = f\"{sum(1 for r in rs if r['hit'] and r['difficulty']=='medium')}/{sum(1 for r in rs if r['difficulty']=='medium')}\"
    hard = f\"{sum(1 for r in rs if r['hit'] and r['difficulty']=='hard')}/{sum(1 for r in rs if r['difficulty']=='hard')}\"
    all3 = sum(1 for r in rs if r.get('used_all_3_signals'))
    out.append(f'{v.upper()}:')
    out.append(f'  Hit rate:       {hits}/{len(rs)} ({hits/len(rs)*100:.0f}%)')
    out.append(f'  RCA/EV/RE:      {avg(rs,\"root_cause_accuracy\"):.2f} / {avg(rs,\"evidence_quality\"):.2f} / {avg(rs,\"reasoning_quality\"):.2f}')
    out.append(f'  Difficulty:     easy={easy}  medium={med}  hard={hard}')
    out.append(f'  Root cause:     payment={pay_h}/{pay_t}  catalog={cat_h}/{cat_t}')
    out.append(f'  Tool calls:     {avg(rs,\"total_tool_calls\"):.1f}  signals={avg(rs,\"signals_checked\"):.1f}  all3={all3}/{len(rs)}')
    out.append(f'  Tokens/cost:    {avg(rs,\"input_tokens\")+avg(rs,\"output_tokens\"):,.0f} tok  \${avg(rs,\"estimated_cost\"):.4f}  {avg(rs,\"latency_seconds\"):.0f}s')
    out.append('')

# --- Multi-turn ---
out.append('=' * 64)
out.append('  MULTI-TURN (n=8, 2-3 turns each)')
out.append('=' * 64)
out.append('')

for v in ['v1', 'v2', 'v3', 'v4']:
    p = RESULTS / f'{v}_multiturn.json'
    if not p.exists():
        continue
    with open(p) as f:
        rs = json.load(f)
    scored = [t for r in rs for t in r['turns'] if t.get('hit') is not None]
    hits = sum(1 for t in scored if t['hit'])
    rca = mean(t['root_cause_accuracy'] for t in scored if 'root_cause_accuracy' in t)
    ev = mean(t['evidence_quality'] for t in scored if 'evidence_quality' in t)
    re = mean(t['reasoning_quality'] for t in scored if 'reasoning_quality' in t)
    tokens = mean(r['final_context_tokens'] for r in rs)
    cost = mean(r['final_cost'] for r in rs)
    calls = mean(r['final_tool_calls'] for r in rs)
    latency = mean(r['total_latency'] for r in rs)
    cache = sum(r['final_cache_hits'] for r in rs)
    compact = sum(r['final_micro_compacted'] for r in rs)
    t1 = mean(r['turns'][0]['context_tokens_at_turn'] for r in rs)
    t2 = mean(r['turns'][1]['context_tokens_at_turn'] for r in rs)
    t3_vals = [r['turns'][2]['context_tokens_at_turn'] for r in rs if len(r['turns']) > 2]
    t3 = mean(t3_vals) if t3_vals else 0
    out.append(f'{v.upper()}:')
    out.append(f'  Hit rate:       {hits}/{len(scored)} ({hits/len(scored)*100:.0f}%)')
    out.append(f'  RCA/EV/RE:      {rca:.2f} / {ev:.2f} / {re:.2f}')
    out.append(f'  Context:        T1={t1:,.0f}  T2={t2:,.0f}  T3={t3:,.0f}  final={tokens:,.0f}')
    out.append(f'  Tool calls:     {calls:.1f}  cache={cache}  compacted={compact}')
    out.append(f'  Cost/latency:   \${cost:.4f}  {latency:.0f}s')
    out.append('')

with open('${COMPARISON}', 'w') as f:
    f.write('\n'.join(out) + '\n')

print(f'Comparison saved to ${COMPARISON}')
print()
print('\n'.join(out))
" 2>&1 | tee -a "$LOG"

echo "Done." | tee -a "$LOG"
