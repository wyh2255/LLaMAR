#!/bin/bash
# Background monitor: checks benchmark progress every 10 minutes
# Usage: bash sar_orch/monitor_benchmark.sh &
# Log: sar_orch/results/benchmark_monitor.log

LOG="/home/wyh/daily_work/LLaMAR/sar_orch/results/benchmark_monitor.log"
CHECK_SCRIPT="/home/wyh/daily_work/LLaMAR/sar_orch/check_benchmark.sh"
BENCHMARK_DIR="/home/wyh/daily_work/LLaMAR/sar_orch/results/benchmark"

MIN_WAIT=120  # Wait at least 2 min before declaring a crash
START_TIME=$(date +%s)

while true; do
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
  
  # Count results
  if [ -d "$BENCHMARK_DIR" ]; then
    total=$(find "$BENCHMARK_DIR" -name "result.json" 2>/dev/null | wc -l)
    success=$(find "$BENCHMARK_DIR" -name "result.json" -exec grep -l '"status": "success"' {} \; 2>/dev/null | wc -l)
    timeout=$(find "$BENCHMARK_DIR" -name "result.json" -exec grep -l '"status": "timeout"' {} \; 2>/dev/null | wc -l)
    failed=$(find "$BENCHMARK_DIR" -name "result.json" -exec grep -l '"status": "failed"' {} \; 2>/dev/null | wc -l)
    
    echo "  Progress: $total/100 (success=$success timeout=$timeout failed=$failed)" >> "$LOG"
    
    # Check if benchmark process still running
    now=$(date +%s)
    pid=$(pgrep -f "benchmark.py" 2>/dev/null | head -1)
    if [ -z "$pid" ]; then
      elapsed=$((now - START_TIME))
      if [ "$elapsed" -lt "$MIN_WAIT" ]; then
        echo "  (startup grace period: ${elapsed}s < ${MIN_WAIT}s, skipping crash check)" >> "$LOG"
      elif [ "$total" -lt 100 ]; then
        echo "  !!! BENCHMARK CRASHED (stalled at $total/100 after ${elapsed}s). Restarting with --resume..." >> "$LOG"
        # Kill leftover agent/coordinator processes
        ps aux | grep -E "a2a-worker|coordinator" | grep -v grep | awk '{print $2}' | xargs -r kill 2>/dev/null
        sleep 3
        cd /home/wyh/daily_work/LLaMAR
        nohup env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
          uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 600 --resume \
          >> /home/wyh/daily_work/LLaMAR/sar_orch/results/benchmark_nohup.log 2>&1 &
        echo "  Restarted with PID: $!" >> "$LOG"
      elif [ "$total" -ge 100 ]; then
        echo "  BENCHMARK COMPLETE ($total/100)" >> "$LOG"
        echo "" >> "$LOG"
        echo "=== FINAL SUMMARY ===" >> "$LOG"
        bash "$CHECK_SCRIPT" >> "$LOG" 2>&1
        exit 0
      fi
    fi
    
    # Show last 3 completed
    find "$BENCHMARK_DIR" -name "result.json" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -3 | awk '{print $2}' | while read f; do
      dir=$(dirname "$f")
      scene=$(echo "$dir" | grep -oP 'scene_\K\d+')
      agents=$(echo "$dir" | grep -oP 'agents_\K\d+')
      seed=$(echo "$dir" | grep -oP 'seed_\K\d+')
      status=$(grep -oP '"status": "\K[^"]+' "$f" 2>/dev/null || echo "?")
      echo "    scene=$scene agents=$agents seed=$seed → $status" >> "$LOG"
    done
  else
    echo "  No results yet (benchmark dir not created)" >> "$LOG"
  fi
  
  echo "" >> "$LOG"
  sleep 600
done
