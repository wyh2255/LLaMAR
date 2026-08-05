from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sar_orch.eval.report import FAILURE_DIAGNOSTIC_BUCKETS


# ── math helpers ──────────────────────────────────────────────────────────


def _nCk(n: int, k: int) -> float:
    if k < 0 or k > n:
        return 0.0
    return float(math.comb(n, k))


def pass_at_k(n: int, c: int, k: int) -> float | None:
    if k > n or n == 0:
        return None
    if c == 0:
        return 0.0
    if c == n:
        return 1.0
    return 1.0 - _nCk(n - c, k) / _nCk(n, k)


def pass_k(n: int, c: int, k: int) -> float | None:
    if k > n or n == 0:
        return None
    return (c / n) ** k


# ── stats helpers ──────────────────────────────────────────────────────────


def _fmt(val: Any, fmt: str = "") -> str:
    if val is None:
        return "-"
    if fmt == ".1%":
        return f"{float(val):.1%}"
    if fmt == ".1f":
        return f"{float(val):.1f}"
    if fmt == ".2f":
        return f"{float(val):.2f}"
    if fmt == ".3f":
        return f"{float(val):.3f}"
    if fmt == ".4f":
        return f"{float(val):.4f}"
    if fmt == "d":
        return str(int(val))
    if fmt == ".0f":
        return f"{float(val):.0f}"
    if fmt == ".2%":
        return f"{float(val):.2%}"
    return str(val)


def _ci_cell(
    mean: Any, low: Any, high: Any, fmt: str = ".1%"
) -> str:
    """把 (均值, 下界, 上界) 渲染成 `μ [lo, hi]` 形式的表格单元。"""
    if mean is None:
        return "-"
    if low is None or high is None:
        return _fmt(mean, fmt)
    return f"{_fmt(mean, fmt)} [{_fmt(low, fmt)}, {_fmt(high, fmt)}]"


def _ci_cell_stat(stat: Any, fmt: str = ".1%") -> str:
    """从 `_numeric_stats` 结果渲染带置信区间的单元。"""
    if not isinstance(stat, dict):
        return "-"
    return _ci_cell(stat.get("mean"), stat.get("ci95_low"), stat.get("ci95_high"), fmt)


def _mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def _numeric_stats(vals: list[float]) -> dict[str, float | None]:
    """把一列数值压成 {mean, std, min, max, ci95_low, ci95_high}。

    **无数据时全部字段为 None，而不是 0.0。** `episode_stats` 的每个键都由
    `aggregate_group` 无条件建出来，所以"这个指标一次都没解析成功"不会表现为
    键缺失，只会表现为键里的值 —— 若那个值是 0.0，下游根本分不清
    "无数据" 与 "真的是 0"。后果不是漏报而是**反向误报**：门禁把
    lower-is-better 指标（total_tokens）的基线 908955 与 actual 0.0 相比，
    会判成 ~100% 的"改进"而放行，真实情况却是"我们没有数据、无从判断"。
    与 DESIGN P5 一致：缺失指标必须报 skip，绝不能当 0 分参与判定。

    注意区分：`[]`（无数据）→ mean=None；`[0.0, 0.0]`（真实的零）→ mean=0.0。
    这两者必须保持可区分，不要"顺手"合并。
    """
    clean = [v for v in vals if v is not None]
    if not clean:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "ci95_low": None,
            "ci95_high": None,
        }
    low, high = _t_interval(clean)
    return {
        "mean": _mean(clean),
        "std": _std(clean),
        "min": min(clean),
        "max": max(clean),
        "ci95_low": low,
        "ci95_high": high,
    }


# ── 95% 置信区间 ───────────────────────────────────────────────────────────
# 论文 §5 要求"报告均值 + 95% 置信区间"，其中 SR 为二项指标，使用
# Clopper-Pearson 区间；其余连续指标使用 t 分布区间。参考实现见
# meta/result_analysis/confidence_intervals.py（依赖 scipy/statsmodels）。
# 此处用标准库等价实现，避免给评测链路引入新依赖。


def _t_ppf(p: float, df: int) -> float:
    """Student-t 分布的分位数，二分求解 CDF 的反函数。

    df >= 1 时 t 的 CDF 可由正则化不完全 Beta 函数表示：
    对 t > 0，CDF(t) = 1 - 0.5 * I_x(df/2, 1/2)，其中 x = df/(df+t^2)。
    """
    if df < 1:
        return float("nan")
    lo, hi = 0.0, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2
        x = df / (df + mid * mid)
        cdf = 1.0 - 0.5 * _betainc_reg(df / 2.0, 0.5, x)
        if cdf < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _t_interval(data: list[float], confidence: float = 0.95) -> tuple[float, float]:
    """连续指标的 t 分布置信区间（等价于 scipy 的 stats.sem + stats.t.ppf）。"""
    n = len(data)
    mean = _mean(data)
    if n < 2:
        return mean, mean
    # 样本标准差（ddof=1），与 scipy.stats.sem 一致
    var = sum((x - mean) ** 2 for x in data) / (n - 1)
    sem = math.sqrt(var / n)
    if sem == 0.0:
        return mean, mean
    margin = _t_ppf((1 + confidence) / 2, n - 1) * sem
    return mean - margin, mean + margin


#: 20 点 Gauss-Legendre 节点/权重（区间 [-1, 1]），用于不完全 Beta 积分。
_GL20_NODES = (
    -0.9931285991850949,
    -0.9639719272779138,
    -0.9122344282513259,
    -0.8391169718222188,
    -0.7463319064601508,
    -0.6360536807265150,
    -0.5108670019508271,
    -0.3737060887154195,
    -0.2277858511416451,
    -0.0765265211334973,
    0.0765265211334973,
    0.2277858511416451,
    0.3737060887154195,
    0.5108670019508271,
    0.6360536807265150,
    0.7463319064601508,
    0.8391169718222188,
    0.9122344282513259,
    0.9639719272779138,
    0.9931285991850949,
)
_GL20_WEIGHTS = (
    0.0176140071391521,
    0.0406014298003869,
    0.0626720483341091,
    0.0832767415767048,
    0.1019301198172404,
    0.1181945319615184,
    0.1316886384491766,
    0.1420961093183820,
    0.1491729864726037,
    0.1527533871307258,
    0.1527533871307258,
    0.1491729864726037,
    0.1420961093183820,
    0.1316886384491766,
    0.1181945319615184,
    0.1019301198172404,
    0.0832767415767048,
    0.0626720483341091,
    0.0406014298003869,
    0.0176140071391521,
)


def _betainc_reg(a: float, b: float, x: float, panels: int = 64) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)。

    以分段 Gauss-Legendre 积分求 ∫₀ˣ t^(a-1)(1-t)^(b-1) dt，再除以 B(a, b)。
    被积函数在端点可能发散（a<1 或 b<1），故用对称式把积分限压到较平缓的一侧。
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # 端点奇异性搬到另一侧：在 x 较大时用 I_x(a,b) = 1 - I_{1-x}(b,a)
    if x > 0.5 and a < b:
        return 1.0 - _betainc_reg(b, a, 1.0 - x, panels)

    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    total = 0.0
    # 靠近 0 的区间取更细的划分（几何加密），吸收 t^(a-1) 的奇异性
    edges = [x * (i / panels) ** 3 for i in range(panels + 1)]
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi <= lo:
            continue
        half, mid = (hi - lo) / 2.0, (hi + lo) / 2.0
        acc = 0.0
        for node, weight in zip(_GL20_NODES, _GL20_WEIGHTS):
            t = mid + half * node
            if t <= 0.0 or t >= 1.0:
                continue
            acc += weight * math.exp((a - 1.0) * math.log(t) + (b - 1.0) * math.log1p(-t))
        total += acc * half
    result = total / math.exp(lbeta)
    return min(1.0, max(0.0, result))


def _beta_ppf(p: float, a: float, b: float) -> float:
    """Beta 分布分位数，对 I_x(a, b) 做二分反解。"""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _betainc_reg(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson_interval(
    successes: int, n: int, alpha: float = 0.05
) -> tuple[float | None, float | None]:
    """Clopper-Pearson（"exact"/beta）二项置信区间。

    等价于 statsmodels 的 proportion_confint(..., method="beta")，论文用它
    给 SR 报告 95% 置信区间。successes=0 时下界为 0，successes=n 时上界为 1。
    """
    if n <= 0:
        return None, None
    low = 0.0 if successes == 0 else _beta_ppf(alpha / 2, successes, n - successes + 1)
    high = 1.0 if successes == n else _beta_ppf(1 - alpha / 2, successes + 1, n - successes)
    return low, high


# ── scan / group ──────────────────────────────────────────────────────────


_PRUNE_DIRS = {"eval_workspace", "__pycache__"}

# 任一存在即说明这是一个 run 目录（而非中间层目录）
_RUN_MARKERS = ("trajectory.csv", "summary.csv", "metadata.json", "result.json")


def scan_results(root_dir: Path) -> tuple[list[dict], list[str]]:
    """递归查找 eval_report.json。

    支持扁平布局（`results/<run>/`）与 benchmark 嵌套布局
    (`results/benchmark/scene_X/agents_Y/seed_Z/`)。

    - 含 `eval_report.json` → 收录，不再下探
    - 否则含 run 标记文件（trajectory.csv 等）→ 这是个**未评测**的 run，记入
      skipped 并停止下探（否则会把 `workers/`、`coordinator/` 之类内部子目录
      误报成漏评目录）
    - 否则视为中间层目录，继续递归
    """
    reports: list[dict] = []
    skipped: list[str] = []
    if not root_dir.exists():
        return reports, [f"{root_dir} (not found)"]

    def walk(current: Path) -> None:
        report_path = current / "eval_report.json"
        if report_path.exists():
            try:
                reports.append(json.loads(report_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                skipped.append(f"{_rel(current, root_dir)} (parse error: {e})")
            return
        if any((current / m).exists() for m in _RUN_MARKERS):
            skipped.append(_rel(current, root_dir))
            return
        subdirs = [
            c
            for c in sorted(current.iterdir())
            if c.is_dir()
            and c.name not in _PRUNE_DIRS
            and not c.name.startswith(".")
            and not _is_retry_backup(c.name)
        ]
        if not subdirs:
            if current != root_dir:
                skipped.append(_rel(current, root_dir))
            return
        for child in subdirs:
            walk(child)

    walk(root_dir)
    return reports, skipped


def _is_retry_backup(name: str) -> bool:
    """`benchmark.py` 重试时把上一轮结果改名为 `seed_<N>_pass_<M>`。

    这些是被取代的历史尝试，计入聚合会重复计数同一 (scene, agents, seed)。
    """
    return name.startswith("seed_") and "_pass_" in name


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def group_by_key(reports: list[dict]) -> dict[tuple[int, int], list[dict]]:
    groups: dict[tuple[int, int], list[dict]] = {}
    for r in reports:
        meta = r.get("metadata", {})
        key = (meta.get("scene"), meta.get("agents"))
        if key[0] is None or key[1] is None:
            continue
        groups.setdefault(key, []).append(r)
    return dict(sorted(groups.items()))


# ── aggregation ───────────────────────────────────────────────────────────


#: 一批内必须保持一致的配置字段。LLM 配置已定为**恒定量**（不作对比轴），
#: 所以批内漂移是污染而不是变量 —— 把两种 model 的 run 池化进同一个 CI，
#: 测出的"方差"里混着配置差异，而报告上完全看不出来。
#:
#: `seed`（场景种子）**不在**此表内：它按设计在组内变化（`group_by_key` 只按
#: `(scene, agents)` 分组，seed 被池化），这是有意的。
#:
#: `prompt_hash` 在此表内：框架 A/B 的对比轴正是它，故**批内应当一致、
#: 跨批应当不同**。批内不一致意味着这批 run 混了两套 prompt。
#:
#: `eval_semantics_version`（P3.2）在此表内但走**专用分支**：全组 None 是
#: legacy-compatible（无 issue）；None ↔ 某版本、或多个 non-null 版本都是
#: **语义不兼容**，必须 fail-closed（`partial_record_only=False`），不得复用
#: 一般字段的 `len(non_null)<=1 → warn` 规则 —— 旧数据缺记录与"尺子不同"
#: 是两种完全不同的问题，后者说明这批 run 根本不该池化。
CONSISTENCY_FIELDS: tuple[str, ...] = (
    "model",
    "provider",
    "api_base",
    "temperature",
    "llm_seed_supported",
    "prompt_hash",
    "state_mode",
    "max_steps",
    "eval_semantics_version",
)


def check_config_consistency(reports: list[dict]) -> list[dict[str, Any]]:
    """找出组内取值不唯一的配置字段。

    返回每个不一致字段的 `{field, values, runs}`。空列表 = 全部一致。

    只报告、不抛异常：聚合本身仍要产出（否则一个字段不一致就拿不到任何数据），
    但调用方必须把它当作"这组数据不可池化"的信号。字段整组缺失（全为 None）
    不算不一致 —— 那是旧 run 没记这个字段，与"两个不同值"是不同的问题。
    """
    issues: list[dict[str, Any]] = []
    for field_name in CONSISTENCY_FIELDS:
        seen: dict[Any, list[str]] = {}
        for r in reports:
            val = (r.get("metadata", {}) or {}).get(field_name)
            seen.setdefault(val, []).append(r.get("run_dir", "?"))
        if len(seen) <= 1:
            continue
        non_null = [v for v in seen if v is not None]
        if field_name == "eval_semantics_version":
            # P3.2 专用分支：语义版本任何混合（None↔版本、多版本）都是
            # fail-closed 的语义不兼容。None 参与混合不是"部分缺记录"——
            # 旧尺子与新尺子的数字根本不可比，池化会制造假方差。
            issues.append(
                {
                    "field": field_name,
                    "values": {str(k): v for k, v in seen.items()},
                    "partial_record_only": False,
                }
            )
            continue
        # 全 None 之外只有一个真实取值 → 视为"部分 run 缺记录"，仍报告，
        # 但标出来它可能只是旧数据而非真的配置漂移。
        issues.append(
            {
                "field": field_name,
                "values": {str(k): v for k, v in seen.items()},
                "partial_record_only": len(non_null) <= 1,
            }
        )
    return issues


def aggregate_group(reports: list[dict]) -> dict[str, Any]:
    n = len(reports)
    meta0 = reports[0].get("metadata", {})
    key = {"scene": meta0.get("scene"), "agents": meta0.get("agents")}
    runs = [r.get("run_dir", "?") for r in reports]
    seeds = [r.get("metadata", {}).get("seed") for r in reports]
    # meta0 代表整组的前提是"组内配置一致"，而那恰恰是需要被检查的事。
    config_issues = check_config_consistency(reports)

    c = sum(1 for r in reports if r.get("episode", {}).get("finished") is True)

    # 论文 §5 的 Success Rate：所有子任务完成的 episode 占比（二项指标，
    # 置信区间用 Clopper-Pearson）。数值上等于 pass@1，这里显式命名以便
    # 与论文表格逐项对照。
    sr_low, sr_high = clopper_pearson_interval(c, n)
    success_rate = {
        "mean": (c / n) if n else 0.0,
        "successes": c,
        "n": n,
        "ci95_low": sr_low,
        "ci95_high": sr_high,
        "ci_method": "clopper-pearson",
    }

    pass_at_k_dict: dict[str, float | None] = {}
    pass_k_dict: dict[str, float | None] = {}
    for k in range(1, n + 1):
        pass_at_k_dict[str(k)] = pass_at_k(n, c, k)
        pass_k_dict[str(k)] = pass_k(n, c, k)

    # collect numeric episode fields
    coverage_vals: list[float] = []
    coverage_verified_vals: list[float] = []
    transport_vals: list[float] = []
    balance_vals: list[float] = []
    idle_ratio_vals: list[float] = []
    token_eff_vals: list[float] = []
    step_eff_vals: list[float] = []
    total_token_vals: list[float] = []
    steps_vals: list[float] = []
    end_reasons: dict[str, int] = {}

    for r in reports:
        ep = r.get("episode", {})
        _maybe_add(coverage_vals, ep, "coverage")
        # 旧格式 report 没有这个键 —— `_maybe_add` 跳过 None，空列表经
        # `_numeric_stats([])` 得到全 None，即"缺失"，不会被当成 0.0。
        _maybe_add(coverage_verified_vals, ep, "coverage_verified")
        _maybe_add(transport_vals, ep, "transport_rate")
        _maybe_add(balance_vals, ep, "balance")
        _maybe_add(idle_ratio_vals, ep, "idle_ratio")
        _maybe_add(token_eff_vals, ep, "token_efficiency")
        _maybe_add(step_eff_vals, ep, "step_efficiency")
        _maybe_add(total_token_vals, ep, "total_tokens")
        _maybe_add(steps_vals, ep, "steps")
        er = ep.get("end_reason", "unknown") or "unknown"
        end_reasons[er] = end_reasons.get(er, 0) + 1

    episode_stats = {
        "coverage": _numeric_stats(coverage_vals),
        # 成功感知覆盖率。与 coverage 并列，不替代它（论文可比性）。
        "coverage_verified": _numeric_stats(coverage_verified_vals),
        "transport_rate": _numeric_stats(transport_vals),
        "balance": _numeric_stats(balance_vals),
        # balance 的替代诊断量：衡量浪费动作，不惩罚角色分工。
        "idle_ratio": _numeric_stats(idle_ratio_vals),
        "token_efficiency": _numeric_stats(token_eff_vals),
        "step_efficiency": _numeric_stats(step_eff_vals),
        "total_tokens": _numeric_stats(total_token_vals),
        "steps": _numeric_stats(steps_vals),
    }

    # failure taxonomy
    tax_totals: dict[str, int] = {}
    for r in reports:
        tx = r.get("failure_taxonomy", {})
        for cat, cnt in (tx or {}).items():
            tax_totals[cat] = tax_totals.get(cat, 0) + cnt
    failure_taxonomy = {
        cat: {"total": cnt, "per_run": cnt / n if n else 0.0}
        for cat, cnt in sorted(tax_totals.items(), key=lambda x: -x[1])
    }

    # failure diagnostics —— 证据残差（P3.2）。独立于 failure_taxonomy /
    # constraint_violations 聚合：这些桶不是 observed environment attempt，
    # 不得混入 failure taxonomy 或 gate 数值指标。
    #
    # 结构：`failure_diagnostics` = {bucket: total}（保持既有 tests 期望的
    # bucket total 直接访问）；逐 run 明细独立放在 `failure_diagnostics_per_run`，
    # 不混入 total 字典，避免两种信息互相遮蔽。
    diag_totals: dict[str, int] = {b: 0 for b in FAILURE_DIAGNOSTIC_BUCKETS}
    diag_per_run: list[dict[str, Any]] = []
    for r in reports:
        fd = r.get("failure_diagnostics", {}) or {}
        per: dict[str, Any] = {"run_dir": r.get("run_dir", "?")}
        for b in FAILURE_DIAGNOSTIC_BUCKETS:
            raw = fd.get(b, 0)
            val = 0
            if isinstance(raw, (list, tuple)):
                val = len(raw)
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                val = int(raw)
            diag_totals[b] += val
            per[b] = val
        diag_per_run.append(per)
    failure_diagnostics = dict(diag_totals)

    # constraint violations
    vrule_totals: dict[str, int] = {}
    total_violations = 0
    for r in reports:
        vlist = r.get("constraint_violations", []) or []
        total_violations += len(vlist)
        for v in vlist:
            rule = v.get("rule", "unknown")
            vrule_totals[rule] = vrule_totals.get(rule, 0) + 1
    constraint_violations = {
        "by_rule": {
            rule: {"total": cnt, "per_run": cnt / n if n else 0.0}
            for rule, cnt in sorted(vrule_totals.items(), key=lambda x: -x[1])
        },
        "total_violations": total_violations,
        "per_run_violations": total_violations / n if n else 0.0,
    }

    # trajectory checks
    check_agg: dict[str, dict[str, int]] = {}
    for r in reports:
        tcs = r.get("trajectory_checks", []) or []
        for tc in tcs:
            ck = tc.get("check", "unknown")
            a = check_agg.setdefault(ck, {"passed": 0, "failed": 0, "na": 0})
            a["total"] = a.get("total", 0) + 1
            p = tc.get("passed")
            if p is True:
                a["passed"] += 1
            elif p is False:
                a["failed"] += 1
            else:
                a["na"] += 1
    for ck in check_agg:
        a = check_agg[ck]
        a["total"] = a.get("total", 0)
    # reorder keys for deterministic output
    trajectory_checks = {}
    for ck in sorted(check_agg):
        a = check_agg[ck]
        total_binary = a["passed"] + a["failed"]
        trajectory_checks[ck] = {
            "passed": a["passed"],
            "failed": a["failed"],
            "na": a["na"],
            "total": a["total"],
            "pass_rate": (a["passed"] / total_binary) if total_binary > 0 else None,
        }

    # LLM judge
    dp_rates: list[float] = []
    obs_rates: list[float] = []
    runs_missing_dispatch: list[str] = []
    runs_missing_obs: list[str] = []
    judge_models: dict[str, int] = {}
    same_model_runs: list[str] = []

    for r in reports:
        j = r.get("llm_judge", {}) or {}
        rd = r.get("run_dir", "?")
        if not j:
            runs_missing_dispatch.append(rd)
            runs_missing_obs.append(rd)
            continue
        jm = j.get("judge_model", "unknown")
        judge_models[jm] = judge_models.get(jm, 0) + 1
        if j.get("same_model_warning"):
            same_model_runs.append(rd)
        dp = j.get("dispatch", {}) or {}
        if dp.get("pass_rate") is not None:
            dp_rates.append(float(dp["pass_rate"]))
        else:
            runs_missing_dispatch.append(rd)
        ob = j.get("observation", {}) or {}
        if ob.get("hallucination_rate") is not None:
            obs_rates.append(float(ob["hallucination_rate"]))
        else:
            runs_missing_obs.append(rd)

    llm_judge_agg: dict[str, Any] = {}
    if dp_rates:
        llm_judge_agg["dispatch_pass_rate"] = _numeric_stats(dp_rates)
    if obs_rates:
        llm_judge_agg["observation_hallucination_rate"] = _numeric_stats(obs_rates)
    if runs_missing_dispatch:
        llm_judge_agg["runs_missing_dispatch"] = runs_missing_dispatch
    if runs_missing_obs:
        llm_judge_agg["runs_missing_observation"] = runs_missing_obs
    if judge_models:
        llm_judge_agg["judge_models"] = judge_models
    if same_model_runs:
        llm_judge_agg["same_model_warning_runs"] = same_model_runs

    return {
        "key": key,
        "n": n,
        "runs": runs,
        "seeds": seeds,
        # 组内共同配置（取自 meta0，仅在 config_issues 为空时才代表全组）。
        # 落盘的意义是让"这批用的什么配置"成为报告里可读的事实，
        # 而不是要去翻某个 run 的 metadata 才能知道。
        "config": {f: meta0.get(f) for f in CONSISTENCY_FIELDS},
        # 非空 = 这组**不可池化**：CI 与均值里混进了配置差异。
        "config_issues": config_issues,
        "poolable": not config_issues,
        "finished_count": c,
        "success_rate": success_rate,
        "pass_at_k": pass_at_k_dict,
        "pass_k": pass_k_dict,
        "episode_stats": episode_stats,
        "end_reason_distribution": end_reasons,
        "failure_taxonomy": failure_taxonomy,
        # 证据残差 total + 逐 run 明细（P3.2），与 failure_taxonomy 分开。
        "failure_diagnostics": failure_diagnostics,
        "failure_diagnostics_per_run": diag_per_run,
        "constraint_violations": constraint_violations,
        "trajectory_checks": trajectory_checks,
        "llm_judge": llm_judge_agg if llm_judge_agg else {},
    }


def _maybe_add(dest: list[float], ep: dict, field: str) -> None:
    v = ep.get(field)
    if v is not None:
        dest.append(float(v))


# ── top-level ─────────────────────────────────────────────────────────────


def aggregate_all(root_dir: Path, output: str | None = None) -> dict[str, Any]:
    reports, skipped = scan_results(root_dir)
    groups = group_by_key(reports)
    group_results = [aggregate_group(grp) for grp in groups.values()]

    return {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_run_dirs_found": len(reports) + len(skipped),
        "valid_run_dirs": len(reports),
        "skipped_dirs": skipped,
        "num_groups": len(group_results),
        "groups": group_results,
    }


# ── output ────────────────────────────────────────────────────────────────


def write_aggregate_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def write_aggregate_report_md(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def _md(s: str = "") -> None:
        lines.append(s)

    root = report.get("root_dir", "")
    ts = report.get("generated_at", "")

    _md("# SAR Aggregate Report — Multi-Episode Evaluation")
    _md()
    _md(f"- **Root**: `{root}`")
    _md(f"- **Generated**: {ts}")
    _md(
        f"- **Runs found**: {report.get('valid_run_dirs', 0)} valid / {report.get('total_run_dirs_found', 0)} total"
    )
    _md()

    groups = report.get("groups", [])

    if not groups:
        _md("> No evaluation data found under the given root directory.")
        _md()
        _md_skipped(report, _md)
        _write(lines, path)
        return

    # ── Overview table ──────────────────────────────────────────────────
    _md("## 总览")
    _md()
    _md(
        "| 组 | Scene×Agents | n | ✅ finished | pass@1 | pass^1 | "
        "Coverage μ | Transport μ |"
    )
    _md("|---|---|---|---|---|---|---|---|")

    for g in groups:
        k = g["key"]
        label = f"S{k['scene']}×A{k['agents']}"
        n = g["n"]
        fc = g["finished_count"]
        p1 = g["pass_at_k"].get("1")
        pk1 = g["pass_k"].get("1")
        cov = g["episode_stats"]["coverage"]
        tr = g["episode_stats"]["transport_rate"]
        _md(
            f"| {label} | {k['scene']}×{k['agents']} | {n} | {fc} | "
            f"{_fmt(p1, '.1%')} | {_fmt(pk1, '.1%')} | "
            f"{_fmt(cov['mean'], '.1%')} | {_fmt(tr['mean'], '.1%')} |"
        )
    _md()

    # ── 论文口径指标表（SR / TR / C / B / L + 95% CI）───────────────────
    _md("## 论文口径指标 (LLaMAR §5 Metrics)")
    _md()
    _md(
        "均值 + 95% 置信区间。SR 为二项指标，用 Clopper-Pearson 区间；"
        "其余为 t 分布区间。定义见论文 §5：SR=全部子任务完成的 episode 占比，"
        "TR=episode 内已完成子任务比例，C=与目标对象成功交互的比例，"
        "B=min(s_i)/(max(s_i)+1e-4)，L=团队高层动作步数。"
    )
    _md()
    _md(
        "⚠ C 列是**论文口径**：论文正文写的是「成功交互」，但作者参考实现"
        "（及本项目照搬的 `base_checker.check_coverage`）只做动作**文本**"
        "子串匹配、不看 success，Table 7 的已发布数字即由该版本产出。"
        "`C_verified` 列是并列新增的成功感知口径（只计成功交互），"
        "**不参与论文对比**，仅用于暴露「念到名字但没做成」的虚高。"
        "两列差距越大，说明 C 被失败动作抬得越多。"
    )
    _md()
    _md(
        "| 组 | SR (95% CI) | TR (95% CI) | C (95% CI) | "
        "C_verified (95% CI) | B (95% CI) | L (95% CI) |"
    )
    _md("|---|---|---|---|---|---|---|")
    for g in groups:
        k = g["key"]
        label = f"S{k['scene']}×A{k['agents']}"
        sr = g.get("success_rate", {})
        es = g["episode_stats"]
        _md(
            f"| {label} "
            f"| {_ci_cell(sr.get('mean'), sr.get('ci95_low'), sr.get('ci95_high'), '.1%')} "
            f"| {_ci_cell_stat(es.get('transport_rate'), '.1%')} "
            f"| {_ci_cell_stat(es.get('coverage'), '.1%')} "
            f"| {_ci_cell_stat(es.get('coverage_verified'), '.1%')} "
            f"| {_ci_cell_stat(es.get('balance'), '.3f')} "
            f"| {_ci_cell_stat(es.get('steps'), '.1f')} |"
        )
    _md()

    # ── Per-group detail ────────────────────────────────────────────────
    for idx, g in enumerate(groups):
        _md(f"## 组 {idx + 1}: Scene {g['key']['scene']} × Agents {g['key']['agents']}")
        _md()
        _md(f"- **n** = {g['n']}, **finished** = {g['finished_count']}")
        _md(f"- **Seeds**: {', '.join(str(s) for s in g['seeds'])}")
        _md()

        # pass@k / pass^k table
        _md("### pass@k / pass^k")
        _md()
        _md("| k | pass@k | pass^k |")
        _md("|---|---|---|")
        for k_str in sorted(g["pass_at_k"], key=int):
            pak = g["pass_at_k"][k_str]
            pk = g["pass_k"][k_str]
            _md(f"| {k_str} | {_fmt(pak, '.4f')} | {_fmt(pk, '.4f')} |")
        _md()

        # Numeric stats
        sr = g.get("success_rate", {})
        if sr:
            _md(
                f"- **Success Rate (SR)**: "
                f"{_ci_cell(sr.get('mean'), sr.get('ci95_low'), sr.get('ci95_high'), '.1%')} "
                f"({sr.get('successes')}/{sr.get('n')}, Clopper-Pearson)"
            )
            _md()

        _md("### 指标统计")
        _md()
        _md("| 指标 | mean | std | min | max | 95% CI |")
        _md("|---|---|---|---|---|---|")
        es = g["episode_stats"]
        for metric, label in [
            ("coverage", "Coverage"),
            ("coverage_verified", "Coverage (verified)"),
            ("transport_rate", "Transport Rate"),
            ("balance", "Balance"),
            ("token_efficiency", "Token Efficiency"),
            ("step_efficiency", "Step Efficiency"),
            ("total_tokens", "Total Tokens"),
            ("steps", "Steps"),
        ]:
            s = es.get(metric, {})
            ci = (
                f"[{_fmt(s.get('ci95_low'), '.4f')}, {_fmt(s.get('ci95_high'), '.4f')}]"
                if s.get("ci95_low") is not None
                else "-"
            )
            _md(
                f"| {label} | {_fmt(s.get('mean'), '.4f')} | "
                f"{_fmt(s.get('std'), '.4f')} | "
                f"{_fmt(s.get('min'), '.4f')} | "
                f"{_fmt(s.get('max'), '.4f')} | {ci} |"
            )
        _md()

        # End reason
        _md("### 终止原因分布")
        _md()
        erd = g.get("end_reason_distribution", {})
        if erd:
            total_er = sum(erd.values())
            _md("| 原因 | 次数 | 占比 |")
            _md("|---|---|---|")
            for reason, cnt in sorted(erd.items(), key=lambda x: -x[1]):
                _md(f"| {reason} | {cnt} | {cnt / total_er:.1%} |")
        else:
            _md("无终止原因记录。")
        _md()

        # Failure taxonomy
        _md("### 失败归因汇总")
        _md()
        tx = g.get("failure_taxonomy", {})
        if tx:
            total_f = sum(v["total"] for v in tx.values())
            _md(f"共 **{total_f}** 次失败，分类如下：")
            _md()
            _md("| 类别 | 总次数 | 每 run 均值 | 占比 |")
            _md("|---|---|---|---|")
            for cat, info in tx.items():
                _md(
                    f"| {cat} | {info['total']} | {info['per_run']:.2f} | "
                    f"{info['total'] / total_f:.1%} |"
                )
        else:
            _md("无失败记录。")
        _md()

        # Failure diagnostics —— 证据残差汇总（P3.2）。独立于上方的失败归因
        # 与下方的约束违规：这些桶不是 observed environment attempt，不得混入
        # failure_taxonomy 的分母或 gate 数值指标。
        _md("### 证据残差汇总")
        _md()
        diag = g.get("failure_diagnostics", {}) or {}
        diag_per = g.get("failure_diagnostics_per_run", []) or []
        _md(
            "以下四桶是**无法用环境动作尺子归因**的证据残差（timeout 独占 slot / "
            "error-observation 工具异常 / trajectory 失败但无已观察 attempt / "
            "非 SAR 失败 query），与上方「失败归因汇总」分开统计："
        )
        _md()
        _md("| 诊断桶 | 总次数 | 每 run 均值 |")
        _md("|---|---|---|")
        for b in FAILURE_DIAGNOSTIC_BUCKETS:
            total = diag.get(b, 0)
            per_run = total / g["n"] if g["n"] else 0.0
            _md(f"| {b} | {total} | {per_run:.2f} |")
        _md()
        if diag_per:
            _md("逐 run 明细：")
            _md()
            _md("| run_dir | " + " | ".join(FAILURE_DIAGNOSTIC_BUCKETS) + " |")
            _md("|---|" + "|".join("---" for _ in FAILURE_DIAGNOSTIC_BUCKETS) + "|")
            for row in diag_per:
                cells = " | ".join(str(row.get(b, 0)) for b in FAILURE_DIAGNOSTIC_BUCKETS)
                _md(f"| {row.get('run_dir', '?')} | {cells} |")
            _md()

        # Constraint violations
        _md("### 约束违规 Top")
        _md()
        cv = g.get("constraint_violations", {})
        by_rule = cv.get("by_rule", {})
        if by_rule:
            _md(
                f"总违规数: **{cv.get('total_violations', 0)}** (每 run 均值 {cv.get('per_run_violations', 0):.2f})"
            )
            _md()
            _md("| 规则 | 总次数 | 每 run 均值 |")
            _md("|---|---|---|")
            for rule, info in by_rule.items():
                _md(f"| {rule} | {info['total']} | {info['per_run']:.2f} |")
        else:
            _md("无约束违规记录。")
        _md()

        # Trajectory checks
        _md("### 轨迹约束检查")
        _md()
        tcs = g.get("trajectory_checks", {})
        if tcs:
            _md("| 检查项 | pass | fail | N/A | pass_rate |")
            _md("|---|---|---|---|---|")
            for ck, info in tcs.items():
                pr = (
                    _fmt(info.get("pass_rate"), ".1%")
                    if info.get("pass_rate") is not None
                    else "-"
                )
                _md(
                    f"| {ck} | {info['passed']} | {info['failed']} | {info['na']} | {pr} |"
                )
        else:
            _md("无轨迹约束检查记录。")
        _md()

        # LLM judge
        _md("### LLM Judge 聚合")
        _md()
        j = g.get("llm_judge", {})
        if j:
            jm = j.get("judge_models", {})
            if jm:
                _md(f"- **Judge 模型分布**: {jm}")
            sw = j.get("same_model_warning_runs", [])
            if sw:
                _md(f"- ⚠ **same_model_warning**: {len(sw)} run(s)")
            _md()
            dp = j.get("dispatch_pass_rate")
            if dp:
                _md("| 指标 | mean | std | min | max |")
                _md("|---|---|---|---|---|")
                _md(
                    f"| Dispatch Pass Rate | {_fmt(dp.get('mean'), '.1%')} | "
                    f"{_fmt(dp.get('std'), '.1%')} | "
                    f"{_fmt(dp.get('min'), '.1%')} | "
                    f"{_fmt(dp.get('max'), '.1%')} |"
                )
            ob = j.get("observation_hallucination_rate")
            if ob:
                if not dp:
                    _md("| 指标 | mean | std | min | max |")
                    _md("|---|---|---|---|---|")
                _md(
                    f"| Observation Hallucination Rate | {_fmt(ob.get('mean'), '.1%')} | "
                    f"{_fmt(ob.get('std'), '.1%')} | "
                    f"{_fmt(ob.get('min'), '.1%')} | "
                    f"{_fmt(ob.get('max'), '.1%')} |"
                )
            rmd = j.get("runs_missing_dispatch", [])
            if rmd:
                _md()
                _md(
                    f"⚠ **{len(rmd)} run(s) missing dispatch judge data**: {', '.join(rmd)}"
                )
            rmo = j.get("runs_missing_observation", [])
            if rmo:
                _md()
                _md(
                    f"⚠ **{len(rmo)} run(s) missing observation judge data**: {', '.join(rmo)}"
                )
        else:
            _md("> 本组无 LLM Judge 数据（所有 run 均无 judge 结果）。")
        _md()

    # ── Skipped dirs ────────────────────────────────────────────────────
    _md_skipped(report, _md)

    path.write_text("\n".join(lines), encoding="utf-8")


def _md_skipped(report: dict, _md) -> None:
    skipped = report.get("skipped_dirs", [])
    if skipped:
        _md("## 跳过目录")
        _md()
        _md(f"以下 **{len(skipped)}** 个目录不含 `eval_report.json`：")
        _md()
        for s in skipped:
            _md(f"- `{s}`")
        _md()


def _write(lines: list[str], path: Path) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def write_both_aggregate_reports(
    report: dict, output_arg: str | Path | None, root_dir: Path
) -> tuple[Path, Path]:
    if output_arg:
        output_arg = Path(output_arg)
        if output_arg.suffix == ".json":
            json_path = output_arg
            md_path = output_arg.with_suffix(".md")
        elif output_arg.suffix == ".md":
            json_path = output_arg.with_suffix(".json")
            md_path = output_arg
        else:
            json_path = output_arg.with_suffix(".json")
            md_path = output_arg.with_suffix(".md")
    else:
        json_path = root_dir / "aggregate_report.json"
        md_path = root_dir / "aggregate_report.md"

    write_aggregate_report(report, json_path)
    write_aggregate_report_md(report, md_path)
    return json_path, md_path


# ── CLI ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="SAR Eval Agent — Multi-Episode Aggregator (Phase 2)"
    )
    parser.add_argument(
        "--results-root",
        type=str,
        default="sar_orch/results",
        help="Root directory containing experiment result subdirectories (default: sar_orch/results)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for aggregate_report.{json,md} (default: <results-root>/aggregate_report.{json,md})",
    )
    args = parser.parse_args()

    root_dir = Path(args.results_root)

    print(f"Scanning {root_dir} for eval_report.json files...")
    report, skipped = scan_results(root_dir)
    print(f"  Found {len(report)} valid eval reports")

    agg = aggregate_all(root_dir, args.output)
    json_path, md_path = write_both_aggregate_reports(agg, args.output, root_dir)

    print("Aggregate report written to:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")
    print(f"\nGroups: {agg['num_groups']}")
    for g in agg.get("groups", []):
        k = g["key"]
        print(
            f"  S{k['scene']}×A{k['agents']}: n={g['n']}, "
            f"finished={g['finished_count']}, "
            f"pass@1={_fmt(g['pass_at_k'].get('1'), '.1%')}"
        )
    if skipped:
        print(f"\nSkipped ({len(skipped)} dirs without eval_report.json):")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
