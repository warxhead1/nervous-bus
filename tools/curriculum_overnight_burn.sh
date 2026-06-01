#!/usr/bin/env bash
# curriculum_overnight_burn.sh — run N curriculum cycles back-to-back to
# build a sizable validated problem corpus overnight.
#
# The MiniMax coding plan caps at 14250 requests / 5h. Per cycle:
#   * synthesize: ~1 request (one batched generation of N problems)
#   * judge:      ~N requests (one judge call per generated problem)
# So a cycle of n=20 costs ~21 requests. 25 cycles ≈ 525 requests —
# well inside the cap. The CurriculumScheduler already wires
# RateBudgetGuard so judge calls fail-open if the cap is reached.
#
# After 9obz, save_problems shards per cycle_id so cycles never
# overwrite each other — safe to run as many as the budget allows.
#
# Usage:
#   tools/curriculum_overnight_burn.sh                 # default: 25 cycles, n=20
#   tools/curriculum_overnight_burn.sh 50 10           # 50 cycles, n=10
#   N_CYCLES=10 N=5 tools/curriculum_overnight_burn.sh # env-style override
#
# Recommended nohup invocation:
#   nohup tools/curriculum_overnight_burn.sh 25 20 \
#     > tools/burn-$(date +%F-%H%M).log 2>&1 &
#
# After the run, eyeball the corpus + drift:
#   ls autobench/benchmarks/curriculum/$(date +%F)/cycles/
#   python3 tools/curriculum_diversity_drift.py

set -euo pipefail

N_CYCLES="${1:-${N_CYCLES:-25}}"
N="${2:-${N:-20}}"
SLEEP_SECONDS="${SLEEP_SECONDS:-30}"  # brief pause between cycles
OUT_DIR="${OUT_DIR:-autobench/benchmarks/curriculum}"

cd "$(dirname "$0")/.."

if [[ -z "${MINIMAX_API_KEY:-}" ]]; then
    echo "ERROR: MINIMAX_API_KEY not set" >&2
    exit 2
fi

echo "=== curriculum overnight burn ==="
echo "  cycles:     $N_CYCLES"
echo "  per cycle:  $N problems (with --validate, so ~$((N+1)) req/cycle)"
echo "  total req:  ~$((N_CYCLES * (N + 1))) (cap 14250/5h)"
echo "  out dir:    $OUT_DIR"
echo "  sleep:      ${SLEEP_SECONDS}s between cycles"
echo "  start:      $(date -Is)"
echo

for i in $(seq 1 "$N_CYCLES"); do
    echo "─── cycle $i/$N_CYCLES at $(date -Is) ───"
    if python3 -m autobench.curriculum once \
        --n "$N" --validate --output-dir "$OUT_DIR"; then
        echo "  cycle $i ok"
    else
        rc=$?
        echo "  cycle $i FAILED rc=$rc — continuing"
    fi
    if (( i < N_CYCLES )); then
        sleep "$SLEEP_SECONDS"
    fi
done

echo
echo "=== burn complete at $(date -Is) ==="
DATE=$(date -I)
if [[ -d "$OUT_DIR/$DATE/cycles" ]]; then
    n_cycles=$(ls "$OUT_DIR/$DATE/cycles" | wc -l)
    n_rows=$(wc -l < "$OUT_DIR/$DATE/cases.jsonl" 2>/dev/null || echo 0)
    echo "  today: $n_cycles cycle shard(s), $n_rows total rows in roll-up"
fi
