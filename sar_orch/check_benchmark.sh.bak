#!/bin/bash
# Check benchmark progress
LOG="/home/wyh/daily_work/LLaMAR/sar_orch/results/benchmark_nohup.log"
RESULTS="/home/wyh/daily_work/LLaMAR/sar_orch/results/benchmark"

echo "=== Benchmark Progress ==="
echo ""
echo "--- Last 10 log lines ---"
tail -10 "$LOG" 2>/dev/null || echo "(log not found)"
echo ""

# Check results directory structure
if [ -d "$RESULTS" ]; then
    total=$(find "$RESULTS" -name "result.json" 2>/dev/null | wc -l)
    success=$(grep -l '"status": "success"' "$RESULTS"/**/result.json 2>/dev/null | wc -l)
    failed=$(grep -l '"status": "failed"' "$RESULTS"/**/result.json 2>/dev/null | wc -l)
    timeout=$(grep -l '"status": "timeout"' "$RESULTS"/**/result.json 2>/dev/null | wc -l)
    skipped=$(grep -l '"status": "skipped"' "$RESULTS"/**/result.json 2>/dev/null | wc -l)
    echo "--- Results Summary ---"
    echo "  Total:   $total"
    echo "  Success: $success"
    echo "  Timeout: $timeout"
    echo "  Failed:  $failed"
    echo "  Skipped: $skipped"
    echo "  Remain:  $((100 - total))"
    echo ""
    echo "--- Last completed ---"
    ls -t "$RESULTS"/scene_*/agents_*/result.json 2>/dev/null | head -5 | while read f; do
        dir=$(dirname "$f")
        scene=$(echo "$dir" | grep -oP 'scene_\d+')
        agents=$(echo "$dir" | grep -oP 'agents_\d+')
        seed=$(echo "$dir" | grep -oP 'seed_\d+')
        status=$(grep -oP '"status": "\w+"' "$f" 2>/dev/null || echo 'status: "?"')
        echo "  $scene $agents $seed → $status"
    done
else
    echo "No benchmark results yet."
fi
echo ""
echo "PID: $(pgrep -f benchmark.py 2>/dev/null || echo 'not running')"
