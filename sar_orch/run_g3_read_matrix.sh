#!/usr/bin/env bash
# G3 read 10-run matrix: 5 scenes × {2,4} agents × seed 42, --long-term-mode read
# Usage: bash sar_orch/run_g3_read_matrix.sh  (log tail in nohup log)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BASE="${ROOT}/sar_orch/results/long_term_memory_read_${TS}"
mkdir -p "$BASE"

run_one() {
  local scene=$1 agents=$2
  local dir="${BASE}/scene_${scene}_agents_${agents}"
  echo "[$(date +%H:%M:%S)] scene=${scene} agents=${agents} start"
  env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:${PYTHONPATH:-}" \
    uv run python sar_orch/experiment.py \
      --scene "$scene" --agents "$agents" --seed 42 \
      --max-steps 20 \
      --long-term-mode read \
      --log-dir "$dir" \
      > "${BASE}/run_scene${scene}_agents${agents}.log" 2>&1
  local rc=$?
  echo "[$(date +%H:%M:%S)] scene=${scene} agents=${agents} done rc=${rc}"
  return $rc
}

fail=0
for scene in 1 2 3 4 5; do
  for agents in 2 4; do
    run_one "$scene" "$agents" || fail=$((fail+1))
  done
done

echo "=== G3 read 10-run matrix complete: ${BASE} (failed=${fail}/10) ==="
exit $fail
