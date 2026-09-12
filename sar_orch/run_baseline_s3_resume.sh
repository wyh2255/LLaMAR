#!/usr/bin/env bash
# Baseline-20 RESUME (2026-09-10 凌晨 429 限流后续跑)
# 只补跑 baseline_s3_20260910_004658 中未通过的 combo；遇到 5-hour quota 错误
# 解析 "Resets in Xhr Ymin" 睡到窗口重置再继续。换 provider 需用户拍板，故只做同 provider 重试。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BASE="${ROOT}/sar_orch/results/baseline_s3_20260910_004658"
DEADLINE=$(( $(date +%s) + 8*3600 ))   # 最迟跑到 ~09:50
MAX_PASSES=4

is_good() {  # $1=run dir → 0 if passed (满步数正常收尾)
  local rm="$1/run_metrics.json"
  [ -f "$rm" ] || return 1
  python3 - "$rm" <<'EOF'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
    ok = (m.get("end_reason") == "max_steps_reached" or m.get("finished")) and (m.get("steps") or 0) > 0
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
EOF
}

quota_sleep_sec() {  # $1=driver.log → 5h 窗口打印睡眠秒数；weekly 打印 -1；无配额错误打印 0
  tail -c 200000 "$1" | python3 -c "
import re, sys
t = re.sub(r'\s+', ' ', sys.stdin.read())   # 日志自动换行：先压平空白
if re.search(r'[Ww]eekly usage limit reached', t):
    print(-1); sys.exit()                    # 周配额：睡不过去，由调用方中止
m = None
for m in re.finditer(r'usage limit reached\.\s*Resets in\s+(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?', t):
    pass
if not m or not (m.group(1) or m.group(2)):
    print(0); sys.exit()
h = int(m.group(1) or 0); mi = int(m.group(2) or 0)
sec = h*3600 + mi*60 + 300   # +5min 余量
print(min(sec, 6*3600))
"
}

run_one() {
  local agents=$1 seed=$2 cport=$3 aport=$4
  local dir="${BASE}/agents_${agents}/seed_${seed}"
  # 备份失败现场（保留供诊断）
  if [ -d "$dir" ]; then
    local k=1; while [ -d "${dir}_fail${k}" ]; do k=$((k+1)); done
    mv "$dir" "${dir}_fail${k}"
  fi
  mkdir -p "$dir"
  echo "[$(date +%H:%M:%S)] RE-RUN agents=${agents} seed=${seed}"
  env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:${PYTHONPATH:-}" \
    uv run python sar_orch/experiment.py \
      --scene 3 --agents "$agents" --seed "$seed" \
      --long-term-mode read \
      --coordinator-port "$cport" --agent-base-port "$aport" \
      --log-dir "$dir" \
      > "${dir}/driver.log" 2>&1
  local rc=$?
  echo "[$(date +%H:%M:%S)] DONE   agents=${agents} seed=${seed} rc=${rc}"
  return $rc
}

lane() {  # $1=name $2=cport $3=aport $4...="agents:seed"
  local name=$1 cport=$2 aport=$3; shift 3
  local pass=0
  while [ $pass -lt $MAX_PASSES ] && [ "$(date +%s)" -lt "$DEADLINE" ]; do
    pass=$((pass+1))
    local pending=0
    for combo in "$@"; do
      local a=${combo%%:*} s=${combo##*:}
      local dir="${BASE}/agents_${a}/seed_${s}"
      is_good "$dir" && continue
      pending=$((pending+1))
      run_one "$a" "$s" "$cport" "$aport"
      if ! is_good "$dir"; then
        local ss; ss=$(quota_sleep_sec "${dir}/driver.log")
        if [ "$ss" = "-1" ]; then
          echo "[$(date +%H:%M:%S)] lane ${name}: WEEKLY 配额耗尽（4 天级），等也没用，lane 中止待人工决策"
          return 3
        elif [ "$ss" -gt 0 ]; then
          echo "[$(date +%H:%M:%S)] lane ${name}: quota 窗口耗尽，睡眠 ${ss}s 等待重置"
          sleep "$ss"
        fi
      fi
      [ "$(date +%s)" -ge "$DEADLINE" ] && break
    done
    [ "$pending" -eq 0 ] && break
  done
  echo "[$(date +%H:%M:%S)] lane ${name} resume loop exit (pass=${pass})"
}

SEEDS="0 10 20 30 40"
COMBOS_A=""; for s in $SEEDS; do COMBOS_A="$COMBOS_A 2:$s 5:$s"; done
COMBOS_B=""; for s in $SEEDS; do COMBOS_B="$COMBOS_B 3:$s 4:$s"; done

lane A 8080 8191 $COMBOS_A &
PID_A=$!
lane B 9080 9191 $COMBOS_B &
PID_B=$!
wait $PID_A; wait $PID_B

# 终态盘点 + 重新聚合（复用首次的聚合逻辑，内联重跑）
echo "resume_finished_at=$(date -Is)" >> "${BASE}/LAUNCH_INFO"
bash -c '
BASE="'"$BASE"'"
python3 - "$BASE" <<'"'"'EOF'"'"'
import csv, json, sys
from pathlib import Path
base = Path(sys.argv[1]); rows = []
for d in sorted(base.glob("agents_*/seed_*")):
    if "_fail" in d.name: continue
    agents = int(d.parent.name.split("_")[1]); seed = int(d.name.split("_")[1])
    row = {"agents": agents, "seed": seed, "status": "FAIL", "steps": "", "coverage": "",
           "transport_rate": "", "finished": "", "end_reason": "", "elapsed_min": "",
           "total_tokens": "", "mem_gate": ""}
    rm = d / "run_metrics.json"
    if rm.exists():
        try:
            m = json.loads(rm.read_text())
            row.update(status="ok", steps=m.get("steps",""), coverage=m.get("coverage",""),
                       transport_rate=m.get("transport_rate",""), finished=m.get("finished",""),
                       end_reason=m.get("end_reason",""),
                       elapsed_min=round((m.get("elapsed_seconds") or 0)/60,1),
                       mem_gate=(m.get("memory_terminal") or {}).get("acceptance_gate",""))
        except Exception as e:
            row["status"] = f"FAIL(parse:{e})"
    sc = d / "summary.csv"
    if sc.exists():
        try:
            with open(sc) as f: srow = list(csv.DictReader(f))[-1]
            row["total_tokens"] = sum(int(v) for k,v in srow.items() if k.endswith("TotalTokens") and v and v != "None")
        except Exception: pass
    rows.append(row)
cols = ["agents","seed","status","steps","coverage","transport_rate","finished","end_reason","elapsed_min","total_tokens","mem_gate"]
with open(base/"baseline_summary.tsv","w",newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, delimiter="\t"); w.writeheader(); w.writerows(rows)
good = [r for r in rows if r["status"]=="ok" and (r["finished"]==True or r["end_reason"]=="max_steps_reached")]
print(f"=== RESUME SUMMARY: {len(good)}/20 clean-pass ===")
for r in rows: print("\t".join(str(r[c])[:12] for c in cols))
EOF'
echo "=== RESUME COMPLETE ==="
