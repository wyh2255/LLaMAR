#!/usr/bin/env bash
# 全功能矩阵 runner：5 scenes × agents {2,4} × seed 42 × max_steps 20
# 全功能 = semantic 模式 + workspace sandbox + read_port 内存 + long-term read（含诊断自动接线）+ peer mail
# 并发 2（xargs -P2）。
# 关键：每组分配唯一端口块（coordinator_port = base, agent_base_port = base+2, base 步进 10），
#       避免并发端口冲突；清掉外层继承的 ROS PYTHONPATH（否则 import 污染）。
# Resume：目标目录已含有效 run_metrics.json（含 coverage 字段）则跳过。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
OUT="${1:-$ROOT/sar_orch/results/full_matrix_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT"
CMDS="$OUT/commands.sh"
: > "$CMDS"

PORT_BASE="${PORT_BASE:-9000}"
k=0
for scene in 1 2 3 4 5; do
  for agents in 2 4; do
    d="$OUT/scene_${scene}_agents_${agents}"
    # resume: 已有非 framework_error 的完整 run_metrics.json 才跳过
    if [ -f "$d/run_metrics.json" ] && grep -q '"coverage"' "$d/run_metrics.json" 2>/dev/null \
       && ! grep -q 'framework_error' "$d/run_metrics.json" 2>/dev/null; then
      echo "SKIP (已有数据): $d"
      k=$((k+1))
      continue
    fi
    mkdir -p "$d"
    cport=$((PORT_BASE + k*10))
    aport=$((cport + 2))
    cat >> "$CMDS" <<EOF
cd "$ROOT" && env -u PYTHONPATH no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:." \
  uv run python sar_orch/experiment.py --scene $scene --agents $agents --seed 42 \
    --max-steps 20 --mode semantic --memory-read-mode read_port \
    --long-term-mode read --enable-peer-mail \
    --coordinator-port $cport --agent-base-port $aport \
    --log-dir "$d" > "$d/stdout.log" 2>&1
EOF
    k=$((k+1))
  done
done

NCMDS=$(wc -l < "$CMDS")
if [ "$NCMDS" -eq 0 ]; then
  echo "无待跑 run（全部已有数据）。退出。"
  exit 0
fi

echo "=== 将执行 $NCMDS 条命令（并发 2）==="
cat "$CMDS"
echo "输出目录: $OUT"
echo "开始执行..."
xargs -P2 -I{} bash -c '{}' < "$CMDS"
echo "=== 全部完成 ==="
