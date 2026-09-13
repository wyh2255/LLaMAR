#!/usr/bin/env bash
# Baseline-20 (first baseline, 2026-09-10 拍板): scene 3 × agents{2,3,4,5} × seeds{0,10,20,30,40}
# 模式全开: --long-term-mode read（memory read_port / truth recorder / diagnosis 随附自动接线）
# 载体: 直接循环调 experiment.py（benchmark.py 不透传 long-term 参数，弃用）
# 并发: 2 lanes，端口基址错开（lane A 8080/8191，lane B 9080/9191）
# Usage: nohup bash sar_orch/run_baseline_s3.sh > sar_orch/results/baseline_s3_nohup.log 2>&1 &
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BASE="${ROOT}/sar_orch/results/baseline_s3_${TS}"
mkdir -p "$BASE"
echo "BASELINE_DIR=${BASE}" | tee "${BASE}/LAUNCH_INFO"
echo "started_at=$(date -Is)" >> "${BASE}/LAUNCH_INFO"

run_one() {
  local agents=$1 seed=$2 cport=$3 aport=$4
  local dir="${BASE}/agents_${agents}/seed_${seed}"
  mkdir -p "$dir"
  echo "[$(date +%H:%M:%S)] START agents=${agents} seed=${seed} (ports ${cport}/${aport})"
  env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:${PYTHONPATH:-}" \
    uv run python sar_orch/experiment.py \
      --scene 3 --agents "$agents" --seed "$seed" \
      --long-term-mode read \
      --coordinator-port "$cport" --agent-base-port "$aport" \
      --log-dir "$dir" \
      > "${dir}/driver.log" 2>&1
  local rc=$?
  echo "[$(date +%H:%M:%S)] DONE  agents=${agents} seed=${seed} rc=${rc}"
  return $rc
}

lane() {
  # $1=lane name, $2=coordinator port, $3=agent base port, $4...="agents:seed" pairs
  local name=$1 cport=$2 aport=$3; shift 3
  local fail=0
  for combo in "$@"; do
    local a=${combo%%:*} s=${combo##*:}
    run_one "$a" "$s" "$cport" "$aport" || fail=$((fail+1))
  done
  echo "[$(date +%H:%M:%S)] lane ${name} finished, failed=${fail}"
  return $fail
}

SEEDS="0 10 20 30 40"
COMBOS_A=""; for s in $SEEDS; do COMBOS_A="$COMBOS_A 2:$s 5:$s"; done
COMBOS_B=""; for s in $SEEDS; do COMBOS_B="$COMBOS_B 3:$s 4:$s"; done

lane A 8080 8191 $COMBOS_A &
PID_A=$!
lane B 9080 9191 $COMBOS_B &
PID_B=$!
wait $PID_A; FAIL_A=$?
wait $PID_B; FAIL_B=$?

echo "finished_at=$(date -Is)" >> "${BASE}/LAUNCH_INFO"
echo "failed=$((FAIL_A + FAIL_B))/20" >> "${BASE}/LAUNCH_INFO"

# ── 聚合：逐 run 读 run_metrics.json + summary.csv token 列 ──────────────
python3 - "$BASE" <<'EOF'
import csv, json, sys
from pathlib import Path

base = Path(sys.argv[1])
rows = []
for d in sorted(base.glob("agents_*/seed_*")):
    agents = int(d.parent.name.split("_")[1]); seed = int(d.name.split("_")[1])
    row = {"agents": agents, "seed": seed, "status": "FAIL",
           "steps": "", "coverage": "", "transport_rate": "", "finished": "",
           "end_reason": "", "elapsed_min": "", "total_tokens": "", "mem_gate": ""}
    rm = d / "run_metrics.json"
    if rm.exists():
        try:
            m = json.loads(rm.read_text())
            row.update(status="ok",
                       steps=m.get("steps", ""), coverage=m.get("coverage", ""),
                       transport_rate=m.get("transport_rate", ""),
                       finished=m.get("finished", ""), end_reason=m.get("end_reason", ""),
                       elapsed_min=round((m.get("elapsed_seconds") or 0) / 60, 1),
                       mem_gate=(m.get("memory_terminal") or {}).get("acceptance_gate", ""))
        except Exception as e:
            row["status"] = f"FAIL(parse:{e})"
    sc = d / "summary.csv"
    if sc.exists():
        try:
            with open(sc) as f:
                srow = list(csv.DictReader(f))[-1]
            row["total_tokens"] = sum(int(v) for k, v in srow.items()
                                      if k.endswith("TotalTokens") and v and v != "None")
        except Exception:
            pass
    rows.append(row)

cols = ["agents", "seed", "status", "steps", "coverage", "transport_rate",
        "finished", "end_reason", "elapsed_min", "total_tokens", "mem_gate"]
out = base / "baseline_summary.tsv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
    w.writeheader(); w.writerows(rows)

ok = [r for r in rows if r["status"] == "ok"]
print(f"\n=== BASELINE SUMMARY ({len(ok)}/{len(rows)} ok) -> {out} ===")
for r in rows:
    print("\t".join(str(r[c])[:12] for c in cols))
if ok:
    def avg(k): 
        vals = [float(r[k]) for r in ok if r[k] != ""]
        return round(sum(vals) / len(vals), 4) if vals else None
    print(f"\navg coverage={avg('coverage')}  avg transport={avg('transport_rate')}  "
          f"avg tokens={avg('total_tokens')}  avg elapsed_min={avg('elapsed_min')}")
EOF

echo "=== BASELINE COMPLETE: ${BASE} (failed=$((FAIL_A + FAIL_B))/20) ==="
exit $((FAIL_A + FAIL_B))
