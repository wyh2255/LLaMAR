#!/bin/bash
# run_sar.sh — 一键启动 SAR 实验
# Usage: bash scripts/run_sar.sh [--scene 1] [--agents 2] [--seed 42]

set -e

cd "$(dirname "$0")/.."

echo "=== SAR Experiment ==="
echo "Scene: ${SCENE:-1}, Agents: ${AGENTS:-2}, Seed: ${SEED:-42}"

env no_proxy="localhost,0.0.0.0,127.0.0.1" uv run python sar_orch/experiment.py \
  --scene "${SCENE:-1}" \
  --agents "${AGENTS:-2}" \
  --seed "${SEED:-42}" \
  "$@"
