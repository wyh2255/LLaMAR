"""Regression gate — 二期：把 aggregate_report.json 变成 CI 可用的 pass/fail 判定。

判定权留在代码：门禁只读 aggregate 产物 + 基线 + 阈值配置，做纯函数比较，
不调用 LLM。两类检查：

- absolute：绝对下限/上限（无基线也能跑，防"从来就很差"）
- regression：与基线 aggregate_report.json 同组对比，只看退化幅度（防"越改越差"）

退出码：0 = 通过，1 = 有 fail 项，2 = 无法判定（配置/输入不可用，或
样本量不足导致本该 fail 的检查被降级为 warn —— 见 `GateResult.inconclusive`）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 指标定义 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricSpec:
    """一个可门禁的指标：canonical key + 方向 + 展示格式。"""

    key: str
    label: str
    higher_is_better: bool
    fmt: str


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("pass_at_1", "pass@1", True, ".1%"),
    MetricSpec("finished_rate", "Finished rate", True, ".1%"),
    MetricSpec("coverage_mean", "Coverage μ", True, ".1%"),
    MetricSpec("transport_rate_mean", "Transport μ", True, ".1%"),
    MetricSpec("balance_mean", "Balance μ", True, ".2f"),
    MetricSpec("violations_per_run", "Violations/run", False, ".2f"),
    MetricSpec("dispatch_pass_rate_mean", "Dispatch pass μ", True, ".1%"),
    MetricSpec("hallucination_rate_mean", "Hallucination μ", False, ".2%"),
    # 效率类。从设计之初就在 aggregate 里算好却从未接入门禁，后果是候选可以
    # "完成率不变、覆盖不变、违规不变，但步数/token 大幅上升"而完全不被察觉
    # —— 自进化回路会因此有滑向"更慢但一样能过"的动机。
    MetricSpec("total_tokens_mean", "Tokens μ", False, ".0f"),
    MetricSpec("step_efficiency_mean", "Step eff μ", True, ".2f"),
    # 诊断量：balance 的替代。与 balance 不同，它不惩罚角色分工。
    MetricSpec("idle_ratio_mean", "Idle ratio μ", False, ".1%"),
)

METRICS_BY_KEY: dict[str, MetricSpec] = {m.key: m for m in METRICS}


# ── 默认阈值配置 ───────────────────────────────────────────────────────────
#
# absolute: 每个指标的绝对边界。higher_is_better 指标写 min，反之写 max。
# regression: 相对基线的最大允许退化幅度（绝对差值，始终为正数）。
# min_runs: 每组最少 run 数，低于此值该组只告警不判定（样本太小）。

DEFAULT_CONFIG: dict[str, Any] = {
    "min_runs": 2,
    "absolute": {
        "pass_at_1": {"min": 0.0},
        "coverage_mean": {"min": 0.60},
        "transport_rate_mean": {"min": 0.30},
        "violations_per_run": {"max": 20.0},
    },
    "regression": {
        "pass_at_1": 0.20,
        "coverage_mean": 0.10,
        "transport_rate_mean": 0.10,
        "violations_per_run": 5.0,
        # 效率类回归项（新增，收紧门禁）。token 增幅超基线 20% 即 block。
        # 相对值而非绝对值：不同场景/团队规模的 token 量级差一个数量级，
        # 绝对阈值在小场景上会永不触发、在大场景上会永远触发。
        "total_tokens_mean": {"relative": 0.20},
    },
}

#: 已从 regression 移出的指标，及其依据。保留此表而不是删掉记录，是因为
#: "为什么不看这个指标" 比 "看哪些指标" 更容易在几轮之后被遗忘并误加回去。
#:
#: 两项均属 DESIGN P5 定义的「改判据」，已获显式批准（见 PROGRESS 用户决策）。
#: 原值**全部保留**在报告中，只是不再参与 pass/fail 判定。
DEMOTED_METRICS: dict[str, str] = {
    "balance_mean": (
        "降级理由不建立在相关性证据上：均值差异（失败 run 0.841 > 成功 run "
        "0.805，n=12 vs 8）补做 Mann-Whitney U 检验后 U=49.0、单侧 p=0.4846、"
        "rank-biserial 效应量 +0.021，未达显著（同口径对照 idle_ratio 的检验见"
        "PROGRESS：U=50.0、p=0.4539、+0.042，同样不显著）。真正的降级依据是"
        "独立于统计的结构性缺陷：min/max 公式结构性惩罚角色分工——一个 agent "
        "专职灭火、另一个专职搬人时 min/max 天然偏低，但那恰恰是好的协作。"
        "用它把门禁会把系统推向平均主义。公式与原值不动（论文 §5 定义），"
        "仅降级为诊断量。替代诊断量见 idle_ratio。"
    ),
    "dispatch_pass_rate_mean": (
        "评分锚点与被测系统的设计语义直接矛盾 —— coordinator 的 "
        "`NEVER re-dispatch to an agent with an active task` 是设计要求，"
        "judge 却判它违规。这是独立于统计的逻辑论证：均值上 finished run 高于 "
        "failed run，但未做显著性检验，不作为「与任务成功脱钩」这类相关性声称"
        "的依据。用它把门禁会奖励「迎合评分规则」。"
    ),
    "hallucination_rate_mean": (
        "同上：LLM judge 采样量在 0-38 间漂移，产出不足以进门禁。"
        "judge 整体降级为诊断信息（DESIGN P3：判定权留在代码）。"
    ),
}


def load_config(path: str | Path | None) -> dict[str, Any]:
    """读取门禁配置；未提供则用 DEFAULT_CONFIG。用户配置按段浅合并。"""
    cfg: dict[str, Any] = {
        "min_runs": DEFAULT_CONFIG["min_runs"],
        "absolute": dict(DEFAULT_CONFIG["absolute"]),
        "regression": dict(DEFAULT_CONFIG["regression"]),
    }
    if path is None:
        return cfg
    user = json.loads(Path(path).read_text(encoding="utf-8"))
    if "min_runs" in user:
        cfg["min_runs"] = int(user["min_runs"])
    for section in ("absolute", "regression"):
        if section in user:
            if user[section] is None:
                cfg[section] = {}
            else:
                cfg[section].update(user[section])
    unknown = sorted(
        (set(cfg["absolute"]) | set(cfg["regression"])) - set(METRICS_BY_KEY)
    )
    if unknown:
        raise ValueError(
            f"unknown gate metric(s): {unknown}. "
            f"Known metrics: {sorted(METRICS_BY_KEY)}"
        )
    return cfg


# ── 从 aggregate group 抽取 canonical 指标 ─────────────────────────────────


def group_label(group: dict[str, Any]) -> str:
    """组标签，作为基线对齐的键。"""
    key = group.get("key", {}) or {}
    return f"S{key.get('scene')}xA{key.get('agents')}"


def _stat_mean(group: dict[str, Any], field_name: str) -> float | None:
    stats = (group.get("episode_stats", {}) or {}).get(field_name)
    if not isinstance(stats, dict):
        return None
    val = stats.get("mean")
    return None if val is None else float(val)


def extract_metrics(group: dict[str, Any]) -> dict[str, float | None]:
    """把一个 aggregate group 压成 canonical 指标字典。

    缺失指标返回 None（下游标 skip 而非当 0 分判 fail）。
    """
    n = int(group.get("n") or 0)
    finished = group.get("finished_count")
    judge = group.get("llm_judge", {}) or {}
    dispatch = judge.get("dispatch_pass_rate", {}) or {}
    halluc = judge.get("observation_hallucination_rate", {}) or {}
    violations = group.get("constraint_violations", {}) or {}

    pak = (group.get("pass_at_k", {}) or {}).get("1")

    return {
        "pass_at_1": None if pak is None else float(pak),
        "finished_rate": (float(finished) / n) if n and finished is not None else None,
        "coverage_mean": _stat_mean(group, "coverage"),
        "transport_rate_mean": _stat_mean(group, "transport_rate"),
        "balance_mean": _stat_mean(group, "balance"),
        "violations_per_run": (
            None
            if violations.get("per_run_violations") is None
            else float(violations["per_run_violations"])
        ),
        "dispatch_pass_rate_mean": (
            None if dispatch.get("mean") is None else float(dispatch["mean"])
        ),
        "hallucination_rate_mean": (
            None if halluc.get("mean") is None else float(halluc["mean"])
        ),
        # 效率类与新诊断量。这几个在 aggregate 里早就算好了，只是从未被抽取
        # 到 canonical 字典里 —— 不补这一步，上面 METRICS 里加了也永远是 skip。
        "total_tokens_mean": _stat_mean(group, "total_tokens"),
        "step_efficiency_mean": _stat_mean(group, "step_efficiency"),
        "idle_ratio_mean": _stat_mean(group, "idle_ratio"),
    }


# ── 检查记录 ───────────────────────────────────────────────────────────────


@dataclass
class GateCheck:
    """一条门禁检查结果。status: pass | fail | skip | warn"""

    group: str
    check: str  # absolute | regression | meta
    metric: str
    status: str
    actual: float | None = None
    baseline: float | None = None
    threshold: float | None = None
    delta: float | None = None
    reason: str = ""
    #: True 表示这条检查**本来判 fail**，只因该组 n < min_runs 才被降级为 warn。
    #: 显式字段而非从 reason 字符串反解：reason 是给人读的，措辞会变；
    #: 判定用的信号不能挂在展示文本上。
    downgraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "check": self.check,
            "metric": self.metric,
            "status": self.status,
            "actual": self.actual,
            "baseline": self.baseline,
            "threshold": self.threshold,
            "delta": self.delta,
            "reason": self.reason,
            "downgraded": self.downgraded,
        }


@dataclass
class GateResult:
    passed: bool = True
    checks: list[GateCheck] = field(default_factory=list)
    generated_at: str = ""
    current_root: str = ""
    baseline_root: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[GateCheck]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def downgraded(self) -> list[GateCheck]:
        """本该 fail、只因样本量不足才被降级为 warn 的检查。"""
        return [c for c in self.checks if c.status == "warn" and c.downgraded]

    @property
    def inconclusive(self) -> bool:
        """无法判定：没有硬 fail，但有 fail 被 n < min_runs 掩盖了。

        为什么需要第三态：`passed = not failures` 在样本量不足时会把"数据显示
        全面崩塌"读成"通过"，因为每条 fail 都已降级为 warn。反过来无条件把
        `n < min_runs` 判 fail 也是错的 —— 一批 `--repeats 1` 的 smoke test
        若各项都在容差内，本就该报通过。所以这里既不动 `passed` 的含义
        （样本量充足时行为完全不变），也不制造假 fail，只把"这批数据不足以
        给出任一结论"这件事显式表达出来，交给调用方（CLI 退出码 2、
        markdown 横幅）去处理。
        """
        return bool(self.downgraded) and not self.failures

    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "fail": 0, "skip": 0, "warn": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "inconclusive": self.inconclusive,
            "downgraded_count": len(self.downgraded),
            "generated_at": self.generated_at,
            "current_root": self.current_root,
            "baseline_root": self.baseline_root,
            "config": self.config,
            "counts": self.counts(),
            "checks": [c.as_dict() for c in self.checks],
        }


# ── 评估 ───────────────────────────────────────────────────────────────────


def _check_absolute(
    label: str, metric: str, actual: float | None, bound: dict[str, Any]
) -> GateCheck:
    spec = METRICS_BY_KEY[metric]
    if actual is None:
        return GateCheck(
            group=label,
            check="absolute",
            metric=metric,
            status="skip",
            reason="metric absent in aggregate report",
        )
    lo = bound.get("min")
    hi = bound.get("max")
    if lo is not None and actual < float(lo):
        return GateCheck(
            group=label,
            check="absolute",
            metric=metric,
            status="fail",
            actual=actual,
            threshold=float(lo),
            reason=f"{spec.label} {actual:.4f} < min {float(lo):.4f}",
        )
    if hi is not None and actual > float(hi):
        return GateCheck(
            group=label,
            check="absolute",
            metric=metric,
            status="fail",
            actual=actual,
            threshold=float(hi),
            reason=f"{spec.label} {actual:.4f} > max {float(hi):.4f}",
        )
    return GateCheck(
        group=label,
        check="absolute",
        metric=metric,
        status="pass",
        actual=actual,
        threshold=float(lo) if lo is not None else float(hi) if hi is not None else None,
    )


def _resolve_tolerance(
    tol: float | dict[str, Any], baseline: float | None
) -> tuple[float | None, str]:
    """把配置里的容差解析成绝对值。

    支持两种写法：
    - `0.20` —— 绝对差值容差（原有语义，不变）
    - `{"relative": 0.20}` —— 相对基线的比例容差

    为什么需要相对形式：token 这类指标在不同场景/团队规模下量级差一个数量级
    （单 run 从数十万到数百万），绝对阈值在小场景上永不触发、在大场景上永远
    触发，等于没有门禁。

    基线为 0 或负时相对容差无意义（0 的 20% 还是 0，会让任何增长都判 fail），
    返回 None 让调用方标 skip 而不是制造假 fail。
    """
    if isinstance(tol, dict):
        rel = tol.get("relative")
        if rel is None:
            raise ValueError(f"regression tolerance dict needs 'relative': {tol!r}")
        if baseline is None or baseline <= 0:
            return None, f"relative tolerance needs baseline > 0 (got {baseline!r})"
        return abs(float(rel)) * baseline, f"{float(rel):.0%} of baseline"
    return float(tol), "absolute"


def _check_regression(
    label: str,
    metric: str,
    actual: float | None,
    baseline: float | None,
    tolerance: float | dict[str, Any],
) -> GateCheck:
    spec = METRICS_BY_KEY[metric]
    if actual is None or baseline is None:
        return GateCheck(
            group=label,
            check="regression",
            metric=metric,
            status="skip",
            actual=actual,
            baseline=baseline,
            threshold=None if isinstance(tolerance, dict) else float(tolerance),
            reason="metric absent in current or baseline report",
        )
    tolerance, tol_kind = _resolve_tolerance(tolerance, baseline)
    if tolerance is None:
        return GateCheck(
            group=label,
            check="regression",
            metric=metric,
            status="skip",
            actual=actual,
            baseline=baseline,
            reason=tol_kind,
        )
    # delta > 0 表示退化（对 higher_is_better 指标是下降，反之是上升）
    delta = (baseline - actual) if spec.higher_is_better else (actual - baseline)
    if delta > tolerance:
        arrow = "dropped" if spec.higher_is_better else "rose"
        return GateCheck(
            group=label,
            check="regression",
            metric=metric,
            status="fail",
            actual=actual,
            baseline=baseline,
            threshold=tolerance,
            delta=delta,
            reason=(
                f"{spec.label} {arrow} by {delta:.4f} "
                f"({baseline:.4f} → {actual:.4f}), tolerance {tolerance:.4f}"
            ),
        )
    return GateCheck(
        group=label,
        check="regression",
        metric=metric,
        status="pass",
        actual=actual,
        baseline=baseline,
        threshold=tolerance,
        delta=delta,
    )


def evaluate_gate(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    config: dict[str, Any],
) -> GateResult:
    """纯函数：当前 aggregate 报告 + 可选基线 + 配置 → 门禁判定。"""
    result = GateResult(
        generated_at=datetime.now(timezone.utc).isoformat(),
        current_root=str(current.get("root_dir", "")),
        baseline_root=str(baseline.get("root_dir", "")) if baseline else None,
        config=config,
    )

    cur_groups = {group_label(g): g for g in current.get("groups", []) or []}
    base_groups = (
        {group_label(g): g for g in baseline.get("groups", []) or []}
        if baseline
        else {}
    )

    if not cur_groups:
        result.checks.append(
            GateCheck(
                group="-",
                check="meta",
                metric="groups_present",
                status="fail",
                reason="current aggregate report has no groups (no eval_report.json found?)",
            )
        )
        result.passed = False
        return result

    # Batch completeness. `aggregate` already records run dirs it could not use
    # (`skipped_dirs`), but the gate never read them -- so a batch where a third of
    # the runs never produced a report still passed, on metrics computed from the
    # survivors. The runs that fail to produce a report are disproportionately the
    # ones that went badly (crash, timeout, gateway error), so their absence biases
    # every metric upward. Silence about that is worse than a slightly noisy gate.
    skipped = current.get("skipped_dirs") or []
    valid = int(current.get("valid_run_dirs") or 0)
    total = int(current.get("total_run_dirs_found") or 0)
    if skipped:
        # warn, not fail: an unevaluated dir is sometimes benign (a run still in
        # flight, a stray directory). It must be visible; it should not
        # unilaterally block. The ratio is what makes it actionable.
        ratio = (len(skipped) / total) if total else 0.0
        result.checks.append(
            GateCheck(
                group="-",
                check="meta",
                metric="batch_complete",
                status="warn",
                actual=float(valid),
                threshold=float(total),
                reason=(
                    f"{len(skipped)}/{total} run dir(s) produced no usable "
                    f"eval_report.json ({ratio:.0%}); metrics cover the "
                    f"{valid} surviving run(s) only and are biased upward if the "
                    f"missing ones failed. Skipped: {sorted(skipped)[:5]}"
                    + (" ..." if len(skipped) > 5 else "")
                ),
            )
        )

    min_runs = int(config.get("min_runs", 0) or 0)
    absolute_cfg: dict[str, Any] = config.get("absolute", {}) or {}
    regression_cfg: dict[str, Any] = config.get("regression", {}) or {}

    for label in sorted(cur_groups):
        group = cur_groups[label]
        n = int(group.get("n") or 0)
        metrics = extract_metrics(group)

        # P3.3：evaluator semantics version —— fail-closed 跨基线门禁。
        # 版本读取必须经 `(group.get("config") or {}).get("eval_semantics_version")`，
        # 兼容缺 `config` 键的 legacy aggregate fixture（config-less 组读作 None）。
        cur_ver = (group.get("config") or {}).get("eval_semantics_version")
        base_group = base_groups.get(label) if baseline is not None else None
        base_ver = (
            (base_group.get("config") or {}).get("eval_semantics_version")
            if base_group is not None
            else None
        )
        # 组内版本漂移：先于代表版本真值表检查。任一侧 config_issues 里带
        # eval_semantics_version 漂移都 fail —— 即使 group config 代表版本相同，
        # 组内混尺子的数字也不可池化、不可跨基线比较。
        cur_version_drift = any(
            i.get("field") == "eval_semantics_version"
            for i in (group.get("config_issues") or [])
        )
        base_version_drift = (
            any(
                i.get("field") == "eval_semantics_version"
                for i in (base_group.get("config_issues") or [])
            )
            if base_group is not None
            else False
        )
        version_incompatible = False
        if cur_version_drift or base_version_drift:
            version_incompatible = True
            side = "current" if cur_version_drift else "baseline"
            result.checks.append(
                GateCheck(
                    group=label,
                    check="meta",
                    metric="eval_semantics_version",
                    status="fail",
                    reason=(
                        f"evaluator semantics version drift within {side} group "
                        "(config_issues); runs are not poolable under one semantics"
                    ),
                )
            )
        elif base_group is not None and cur_ver != base_ver:
            # 真值表（P3.3）：None/None 允许数值 gate；None↔non-null 或不同
            # non-null 均 fail；相同 non-null 允许。`!=` 恰好覆盖全部四种情况
            # （None == None 不触发；None != "v1" 触发）。
            version_incompatible = True
            result.checks.append(
                GateCheck(
                    group=label,
                    check="meta",
                    metric="eval_semantics_version",
                    status="fail",
                    reason=(
                        f"evaluator semantics versions incompatible: current "
                        f"{cur_ver!r} vs baseline {base_ver!r}; numeric comparison "
                        "is not meaningful across semantics"
                    ),
                )
            )

        # 配置一致性：LLM 配置是恒定量，批内漂移是污染。这条判 **fail** 而非
        # warn —— 若只 warn，一批混了两个 model 的数据仍会以"通过"收场，
        # 而它的 CI 与均值已经不表示任何单一配置下的性能。判 fail 才能强制
        # 人去分批，这也是"配置为恒定量"这个决策唯一的机械保障。
        for issue in group.get("config_issues", []) or []:
            if issue.get("field") == "eval_semantics_version":
                # P3.3：版本漂移由上面专用 `eval_semantics_version` meta check
                # 负责（含 baseline 侧、且不受 min_runs 降级）。这里再产一条
                # `config:eval_semantics_version` 会重复且语义更弱 —— 跳过。
                continue
            values = issue.get("values", {})
            result.checks.append(
                GateCheck(
                    group=label,
                    check="meta",
                    metric=f"config:{issue.get('field')}",
                    status="warn" if issue.get("partial_record_only") else "fail",
                    reason=(
                        f"config drift within group: {issue.get('field')} has "
                        f"{len(values)} distinct values {sorted(values)}; "
                        "runs are not poolable into one CI"
                        + (
                            " (some runs simply lack this field -- likely older "
                            "data rather than real drift)"
                            if issue.get("partial_record_only")
                            else ""
                        )
                    ),
                )
            )

        underpowered = n < min_runs
        if underpowered:
            result.checks.append(
                GateCheck(
                    group=label,
                    check="meta",
                    metric="min_runs",
                    status="warn",
                    actual=float(n),
                    threshold=float(min_runs),
                    reason=(
                        f"only {n} run(s) < min_runs {min_runs}; "
                        "group checks downgraded to warn"
                    ),
                )
            )

        base_metrics = (
            extract_metrics(base_groups[label]) if label in base_groups else None
        )
        if baseline is not None and base_metrics is None:
            result.checks.append(
                GateCheck(
                    group=label,
                    check="meta",
                    metric="baseline_group",
                    status="warn",
                    reason="group absent in baseline; regression checks skipped",
                )
            )

        for metric, bound in sorted(absolute_cfg.items()):
            check = _check_absolute(label, metric, metrics.get(metric), bound)
            if underpowered and check.status == "fail":
                check.status = "warn"
                check.downgraded = True
                check.reason += " (downgraded: n < min_runs)"
            result.checks.append(check)

        if base_metrics is not None:
            if version_incompatible:
                # P3.3：语义不兼容时数值 delta 不可解释 —— 该 group 的 numeric
                # regression 全部 skip（保留上方 meta fail），不得产出任何
                # pass/fail 数值结论。absolute/generic 非版本行为不受影响。
                for metric, tol in sorted(regression_cfg.items()):
                    result.checks.append(
                        GateCheck(
                            group=label,
                            check="regression",
                            metric=metric,
                            status="skip",
                            actual=metrics.get(metric),
                            baseline=base_metrics.get(metric),
                            reason=(
                                "evaluator semantics versions incompatible; "
                                "numeric regression not comparable across semantics"
                            ),
                        )
                    )
            else:
                for metric, tol in sorted(regression_cfg.items()):
                    check = _check_regression(
                        label,
                        metric,
                        metrics.get(metric),
                        base_metrics.get(metric),
                        # 不在此处 float()：容差可以是 {"relative": ...} 形式，
                        # 由 _check_regression 解析（需要基线值才能算出绝对量）。
                        tol,
                    )
                    if underpowered and check.status == "fail":
                        check.status = "warn"
                        check.downgraded = True
                        check.reason += " (downgraded: n < min_runs)"
                    result.checks.append(check)

    for label in sorted(set(base_groups) - set(cur_groups)):
        result.checks.append(
            GateCheck(
                group=label,
                check="meta",
                metric="missing_group",
                status="warn",
                reason="group present in baseline but missing in current run",
            )
        )

    result.passed = not result.failures
    return result


# ── 输出 ───────────────────────────────────────────────────────────────────


def _fmt(val: float | None, fmt: str) -> str:
    if val is None:
        return "-"
    if fmt == ".1%":
        return f"{val:.1%}"
    if fmt == ".2%":
        return f"{val:.2%}"
    if fmt == ".2f":
        return f"{val:.2f}"
    return f"{val:.4f}"


_STATUS_MARK = {"pass": "✅", "fail": "❌", "warn": "⚠", "skip": "–"}


def render_gate_md(result: GateResult) -> str:
    lines: list[str] = []
    verdict = "PASS" if result.passed else "FAIL"
    if result.inconclusive:
        # 横幅不能读成干净的 PASS：人扫一眼 markdown 时看到的第一行就是结论，
        # 而这批数据恰恰不足以支撑任何结论。
        n_down = len(result.downgraded)
        verdict += f" (INCONCLUSIVE — {n_down} check(s) downgraded below min_runs)"
    lines.append(f"# SAR Regression Gate — {verdict}")
    lines.append("")
    if result.inconclusive:
        lines.append(
            "> ⚠ **判定不成立**：以下检查本应判 fail，仅因该组 run 数低于 "
            "`min_runs` 被降级为告警。这批样本既不足以证明通过、也不足以证明"
            "退化 —— 提高 `--repeats` 后重跑再下结论。"
        )
        lines.append("")
        for c in result.downgraded:
            lines.append(f"> - `{c.group}` **{c.metric}** ({c.check}): {c.reason}")
        lines.append("")
    lines.append(f"- **Current**: `{result.current_root}`")
    lines.append(f"- **Baseline**: `{result.baseline_root or '(none — absolute checks only)'}`")
    lines.append(f"- **Generated**: {result.generated_at}")
    counts = result.counts()
    lines.append(
        f"- **Checks**: {counts['pass']} pass · {counts['fail']} fail · "
        f"{counts['warn']} warn · {counts['skip']} skip"
    )
    lines.append("")

    if result.failures:
        lines.append("## 阻塞项")
        lines.append("")
        for c in result.failures:
            lines.append(f"- ❌ `{c.group}` **{c.metric}** ({c.check}): {c.reason}")
        lines.append("")

    if result.warnings:
        lines.append("## 告警项")
        lines.append("")
        for c in result.warnings:
            lines.append(f"- ⚠ `{c.group}` **{c.metric}** ({c.check}): {c.reason}")
        lines.append("")

    lines.append("## 全部检查")
    lines.append("")
    lines.append("| 组 | 检查 | 指标 | 实际 | 基线 | 阈值 | 状态 |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in result.checks:
        spec = METRICS_BY_KEY.get(c.metric)
        f = spec.fmt if spec else ".4f"
        label = spec.label if spec else c.metric
        lines.append(
            f"| {c.group} | {c.check} | {label} | {_fmt(c.actual, f)} | "
            f"{_fmt(c.baseline, f)} | {_fmt(c.threshold, f)} | "
            f"{_STATUS_MARK.get(c.status, c.status)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_gate_reports(
    result: GateResult, output: str | Path | None, default_dir: Path
) -> tuple[Path, Path]:
    if output:
        out = Path(output)
        json_path = out if out.suffix == ".json" else out.with_suffix(".json")
        md_path = out.with_suffix(".md")
    else:
        json_path = default_dir / "gate_report.json"
        md_path = default_dir / "gate_report.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result.as_dict(), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_gate_md(result), encoding="utf-8")
    return json_path, md_path


# ── 报告解析 ───────────────────────────────────────────────────────────────


def resolve_aggregate(path: str | Path, *, recompute: bool = True) -> dict[str, Any]:
    """把路径解析为 aggregate 报告 dict。

    - `*.json` → 直接读
    - 目录且含 aggregate_report.json → 读
    - 目录且无该文件 → recompute=True 时现场跑聚合（扫 eval_report.json）
    """
    from sar_orch.eval.aggregate import aggregate_all

    p = Path(path)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    if not p.exists():
        raise FileNotFoundError(f"aggregate source not found: {p}")
    existing = p / "aggregate_report.json"
    if existing.exists():
        return json.loads(existing.read_text(encoding="utf-8"))
    if not recompute:
        raise FileNotFoundError(f"aggregate_report.json not found under {p}")
    return aggregate_all(p)


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SAR Eval Agent — Regression Gate (二期)"
    )
    parser.add_argument(
        "--results-root",
        type=str,
        required=True,
        help="Results root (or an aggregate_report.json). Aggregation is computed "
        "on the fly when the report is absent.",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        help="Baseline results root or aggregate_report.json. Omit to run "
        "absolute checks only.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Gate threshold config JSON (keys: min_runs, absolute, regression). "
        "Defaults to built-in thresholds.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for gate_report.{json,md} (default: <results-root>/gate_report.*)",
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Always exit 0; report failures without blocking.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: invalid gate config: {exc}", file=sys.stderr)
        return 2

    try:
        current = resolve_aggregate(args.results_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load current aggregate: {exc}", file=sys.stderr)
        return 2

    baseline = None
    if args.baseline:
        try:
            baseline = resolve_aggregate(args.baseline)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: cannot load baseline aggregate: {exc}", file=sys.stderr)
            return 2

    result = evaluate_gate(current, baseline, config)

    src = Path(args.results_root)
    default_dir = src.parent if src.is_file() else src
    json_path, md_path = write_gate_reports(result, args.output, default_dir)

    counts = result.counts()
    if result.inconclusive:
        print(
            f"Gate: INCONCLUSIVE (underpowered — see warnings); "
            f"{len(result.downgraded)} check(s) would have failed at "
            f"n >= min_runs {config.get('min_runs')}"
        )
    else:
        print(f"Gate: {'PASS' if result.passed else 'FAIL'}")
    print(
        f"  {counts['pass']} pass · {counts['fail']} fail · "
        f"{counts['warn']} warn · {counts['skip']} skip"
    )
    for c in result.failures:
        print(f"  ❌ {c.group} {c.metric}: {c.reason}")
    for c in result.warnings:
        print(f"  ⚠  {c.group} {c.metric}: {c.reason}")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")

    # --warn-only 的语义是"永不阻塞"，对 inconclusive 同样适用。
    if args.warn_only:
        return 0
    # 2 = 无法判定，与既有的"输入/配置不可用"共用一个码：两者对 CI 是同一件事
    # —— 这次运行没有产生可信的 pass/fail 结论，不该当成通过放行。
    if result.inconclusive:
        return 2
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
