#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 缓存优化 P1 快测驱动（回归 benchmark 第 1 轮）
# 规格: .hermes/spec/cache-hit-rate/20260912-cache-opt-p1.md
# 面板: a2s30 · a3s30 · a4s10 · a5s0 · a5s40（scene 3，固定 5 组，全 packapi 批）
# 设计: treatment = 现场跑 prefix_stable armed；control = v1 历史 run（不重跑）
#       除 --prune-policy prefix_stable 外，其余参数与 v1（run_baseline_s3.sh）完全一致
# 口径: P−H（token_usage.csv 全请求 sum(H)/sum(P)；CacheMissTokens 列有坑，不采用）
#       有效计费 = (P−H) + 0.1H
# 可恢复: 重复执行自动跳过已完成 combo（run_metrics.json 存在）；未完成残料让位 .prev_*
# 用法: nohup bash sar_orch/run_cacheopt_quick.sh > sar_orch/results/cacheopt_quick_nohup.log 2>&1 &
# ─────────────────────────────────────────────────────────────────────────────
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BASE="${ROOT}/sar_orch/results/cacheopt_p1_quick_${TS}"
V1="${ROOT}/sar_orch/results/baseline_s3_20260910_004658"
mkdir -p "$BASE"
{
  echo "QUICK_DIR=${BASE}"
  echo "v1_baseline=${V1}"
  echo "panel=a2s30 a3s30 a4s10 a5s0 a5s40"
  echo "treatment_flags=--long-term-mode read --prune-policy prefix_stable"
  echo "started_at=$(date -Is)"
} | tee "${BASE}/LAUNCH_INFO"

run_one() {
  local agents=$1 seed=$2 cport=$3 aport=$4
  local dir="${BASE}/agents_${agents}/seed_${seed}"
  if [ -f "${dir}/run_metrics.json" ]; then
    echo "[$(date +%H:%M:%S)] SKIP  agents=${agents} seed=${seed} (已完成)"
    return 0
  fi
  if [ -d "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    mv "$dir" "${dir}.prev_$(date +%H%M%S)"
  fi
  mkdir -p "$dir"
  echo "[$(date +%H:%M:%S)] START agents=${agents} seed=${seed} (ports ${cport}/${aport})"
  env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:${PYTHONPATH:-}" \
    uv run python sar_orch/experiment.py \
      --scene 3 --agents "$agents" --seed "$seed" \
      --long-term-mode read \
      --prune-policy prefix_stable \
      --coordinator-port "$cport" --agent-base-port "$aport" \
      --log-dir "$dir" \
      > "${dir}/driver.log" 2>&1
  local rc=$?
  echo "[$(date +%H:%M:%S)] DONE  agents=${agents} seed=${seed} rc=${rc}"
  return $rc
}

lane() {
  local name=$1 cport=$2 aport=$3; shift 3
  local fail=0
  for combo in "$@"; do
    local a=${combo%%:*} s=${combo##*:}
    run_one "$a" "$s" "$cport" "$aport" || fail=$((fail+1))
  done
  echo "[$(date +%H:%M:%S)] lane ${name} finished, failed=${fail}"
  return $fail
}

# 双 lane（与 baseline 相同端口基址）；按 v1 实测时长配平
lane A 8080 8191 2:30 5:0 &
PID_A=$!
lane B 9080 9191 3:30 4:10 5:40 &
PID_B=$!
wait $PID_A; FAIL_A=$?
wait $PID_B; FAIL_B=$?

echo "finished_at=$(date -Is)" >> "${BASE}/LAUNCH_INFO"
echo "failed=$((FAIL_A+FAIL_B))/5" >> "${BASE}/LAUNCH_INFO"

# ── 聚合：treatment vs v1（P−H 口径）+ 质量/健康/副作用审计 ──────────────────
python3 - "$BASE" "$V1" <<'PYEOF'
import csv, json, re, sys
from pathlib import Path

base, v1 = Path(sys.argv[1]), Path(sys.argv[2])
panel = [(2, 30, "a2s30"), (3, 30, "a3s30"), (4, 10, "a4s10"), (5, 0, "a5s0"), (5, 40, "a5s40")]

def tokens(d):
    p = d / "token_usage.csv"
    if not p.exists():
        return None
    P = H = n = 0
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            try:
                P += int(r["PromptTokens"]); H += int(r["CacheHitTokens"]); n += 1
            except (KeyError, ValueError, TypeError):
                pass
    return {"n": n, "P": P, "H": H, "hit": (H / P if P else 0.0),
            "billed": P - H, "eff": (P - H) + 0.1 * H}

def audit(d):
    o = {"steps": None, "end_reason": "", "coverage": None, "transport": None, "finished": None,
         "gate": "", "elapsed_min": None, "policy": "", "pe": 0, "ov": 0, "fw": 0, "tb": 0, "ftr": None, "ctx": None}
    rm = d / "run_metrics.json"
    if rm.exists():
        m = json.loads(rm.read_text())
        mt = m.get("memory_terminal") or {}
        acc = mt.get("acceptance") or {}
        o.update(steps=m.get("steps"), end_reason=m.get("end_reason"), coverage=m.get("coverage"),
                 transport=m.get("transport_rate"), finished=m.get("finished"),
                 gate=mt.get("acceptance_gate", ""),
                 elapsed_min=round((m.get("elapsed_seconds") or 0) / 60, 1))
        o["fw"] = sum(v for v in (acc.get("framework_error_counts") or {}).values() if isinstance(v, (int, float)))
        o["ftr"] = acc.get("failed_tool_rows")
    md = d / "metadata.json"
    if md.exists():
        o["policy"] = json.loads(md.read_text()).get("prune_policy", "")
    o["pe"] = sum(1 for f in d.rglob("context/prune_events.ndjson") for _ in open(f))
    for f in d.rglob("context/discards.ndjson"):
        for line in open(f, errors="ignore"):
            if "prefix_stable_overflow_truncate" in line:
                o["ov"] += 1
    lg = d / "driver.log"
    if lg.exists():
        t = lg.read_text(errors="ignore")
        o["tb"] = len(re.findall(r"Traceback \(most recent", t))
    ai = d / "agent_interactions.csv"
    if ai.exists():
        tot = cnt = 0
        with open(ai, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    tot += int(r["LLMInputChars"]); cnt += 1
                except (KeyError, ValueError, TypeError):
                    pass
        if cnt:
            o["ctx"] = round(tot / cnt)
    return o

rows = []
for a, s, name in panel:
    td = base / f"agents_{a}" / f"seed_{s}"
    vd = v1 / f"agents_{a}" / f"seed_{s}"
    T, V = tokens(td), tokens(vd)
    TA, VA = audit(td), audit(vd)
    rows.append({
        "combo": name,
        "v1_hit_%": round(V["hit"] * 100, 1) if V else "",
        "t_hit_%": round(T["hit"] * 100, 1) if T else "",
        "d_hit_pt": round((T["hit"] - V["hit"]) * 100, 1) if (T and V) else "",
        "v1_billed": V["billed"] if V else "",
        "t_billed": T["billed"] if T else "",
        "d_billed_%": round((T["billed"] / V["billed"] - 1) * 100, 2) if (T and V and V["billed"]) else "",
        "v1_P": V["P"] if V else "", "v1_H": V["H"] if V else "",
        "t_P": T["P"] if T else "", "t_H": T["H"] if T else "",
        "v1_steps": VA["steps"], "v1_reason": VA["end_reason"], "v1_gate": VA["gate"],
        "t_steps": TA["steps"], "t_reason": TA["end_reason"], "t_coverage": TA["coverage"],
        "t_transport": TA["transport"], "t_gate": TA["gate"], "t_policy": TA["policy"],
        "t_elapsed_min": TA["elapsed_min"], "t_prune_events": TA["pe"],
        "t_overflow_trunc": TA["ov"], "t_fw_err": TA["fw"], "t_failed_tool_rows": TA["ftr"],
        "t_traceback": TA["tb"], "v1_fw_err": VA["fw"], "v1_traceback": VA["tb"],
        "t_ctx_mean": TA["ctx"], "v1_ctx_mean": VA["ctx"],
    })

def agg(toks):
    P = sum(x["P"] for x in toks if x); H = sum(x["H"] for x in toks if x)
    return P, H, (H / P if P else 0.0), P - H

T_all = [tokens(base / f"agents_{a}" / f"seed_{s}") for a, s, _ in panel]
V_all = [tokens(v1 / f"agents_{a}" / f"seed_{s}") for a, s, _ in panel]
P1, H1, hr1, b1 = agg(V_all)
P2, H2, hr2, b2 = agg(T_all)

print()
print("== 主对比（P−H 口径，全请求 sum(H)/sum(P)）==")
print(f"{'combo':8}{'v1_hit':>8}{'t_hit':>8}{'d_pt':>7}{'v1_billed':>12}{'t_billed':>12}{'d_billed':>10}")
for r in rows:
    print(f"{r['combo']:8}{str(r['v1_hit_%']):>8}{str(r['t_hit_%']):>8}{str(r['d_hit_pt']):>7}"
          f"{str(r['v1_billed']):>12}{str(r['t_billed']):>12}{str(r['d_billed_%']):>10}")
print()
print(f"AGG v1   : P={P1:,} H={H1:,} hit={hr1:.1%} billed={b1:,} eff={b1 + 0.1 * H1:,.0f}")
print(f"AGG treat: P={P2:,} H={H2:,} hit={hr2:.1%} billed={b2:,} eff={b2 + 0.1 * H2:,.0f}")
if b1 and b2:
    print(f"AGG d_billed={((b2 / b1 - 1) * 100):+.2f}%  d_hit={(hr2 - hr1) * 100:+.1f}pt  "
          f"d_eff={(((b2 + 0.1 * H2) / (b1 + 0.1 * H1)) - 1) * 100:+.2f}%")
print()
print("== 质量 / 健康 / 副作用审计 ==")
for r in rows:
    print(f"{r['combo']}: steps={r['t_steps']} end={r['t_reason']} cov={r['t_coverage']} "
          f"trans={r['t_transport']} gate={r['t_gate']} policy={r['t_policy']} "
          f"pe={r['t_prune_events']} ov={r['t_overflow_trunc']} fw={r['t_fw_err']}(v1:{r['v1_fw_err']}) "
          f"ftr={r['t_failed_tool_rows']} tb={r['t_traceback']}(v1:{r['v1_traceback']}) "
          f"ctx={r['t_ctx_mean']}(v1:{r['v1_ctx_mean']})")
ctxs = [(r["v1_ctx_mean"], r["t_ctx_mean"]) for r in rows if r["v1_ctx_mean"] and r["t_ctx_mean"]]
if ctxs:
    mv = sum(x for x, _ in ctxs) / len(ctxs)
    mt = sum(y for _, y in ctxs) / len(ctxs)
    print(f"ctx_chars_mean: v1={mv:,.0f} treat={mt:,.0f} ratio={mt / mv:.3f}")

out = base / "quick_compare.tsv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter="\t")
    w.writeheader()
    w.writerows(rows)
print()
print(f"-> {out}")
PYEOF

echo "=== QUICK COMPLETE: ${BASE} (failed=$((FAIL_A+FAIL_B))/5) ==="
exit $((FAIL_A+FAIL_B))
