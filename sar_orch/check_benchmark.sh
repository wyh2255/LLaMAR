#!/bin/bash
# Check benchmark progress
PROJECT_DIR="/home/wyh/daily_work/LLaMAR-sematic_map"
LOG="$PROJECT_DIR/sar_orch/results/benchmark_nohup.log"
RESULTS="$PROJECT_DIR/sar_orch/results/benchmark"

echo "=== Benchmark Progress ==="
echo ""
echo "--- Last 10 log lines ---"
tail -10 "$LOG" 2>/dev/null || echo "(log not found)"
echo ""

# Check results directory structure
if [ -d "$RESULTS" ]; then
    total=$(find "$RESULTS" -name "result.json" 2>/dev/null | wc -l)
    success=0; failed=0; timeout=0; skipped=0
    while IFS= read -r f; do
        status=$(grep -oP '"status": "\K[^"]+' "$f" 2>/dev/null)
        case "$status" in
            success) ((success++));;
            failed)  ((failed++));;
            timeout) ((timeout++));;
            skipped) ((skipped++));;
        esac
    done < <(find "$RESULTS" -name "result.json" 2>/dev/null)
    echo "--- Results Summary ---"
    echo "  Total:   $total"
    echo "  Success: $success"
    echo "  Timeout: $timeout"
    echo "  Failed:  $failed"
    echo "  Skipped: $skipped"
    echo "  Remain:  $((100 - total))"
    echo ""
    echo "--- Last completed ---"
    find "$RESULTS" -name "result.json" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -5 | awk '{print $2}' | while read f; do
        dir=$(dirname "$f")
        scene=$(echo "$dir" | grep -oP 'scene_\K\d+')
        agents=$(echo "$dir" | grep -oP 'agents_\K\d+')
        seed=$(echo "$dir" | grep -oP 'seed_\K\d+')
        status=$(grep -oP '"status": "\K[^"]+' "$f" 2>/dev/null || echo "?")
        echo "  scene=$scene agents=$agents seed=$seed → $status"
    done
else
    echo "No benchmark results yet."
fi
echo ""
echo "PID: $(pgrep -f benchmark.py 2>/dev/null || echo 'not running')"
