"""Regression gate — 二期：把 aggregate_report.json 变成 CI 可用的 pass/fail 判定。

判定权留在代码：门禁只读 aggregate 产物 + 基线 + 阈值配置，做纯函数比较，
不调用 LLM。两类检查：

- absolute：绝对下限/上限（无基线也能跑，防"从来就很差"）
- regression：与基线 aggregate_report.json 同组对比，只看退化幅度（防"越改越差"）

退出码：0 = 通过，1 = 有 fail 项。
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
        "balance_mean": 0.15,
        "violations_per_run": 5.0,
        "dispatch_pass_rate_mean": 0.15,
        "hallucination_rate_mean": 0.05,
    },
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

    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "fail": 0, "skip": 0, "warn": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
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


def _check_regression(
    label: str,
    metric: str,
    actual: float | None,
    baseline: float | None,
    tolerance: float,
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
            threshold=tolerance,
            reason="metric absent in current or baseline report",
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

    min_runs = int(config.get("min_runs", 0) or 0)
    absolute_cfg: dict[str, Any] = config.get("absolute", {}) or {}
    regression_cfg: dict[str, Any] = config.get("regression", {}) or {}

    for label in sorted(cur_groups):
        group = cur_groups[label]
        n = int(group.get("n") or 0)
        metrics = extract_metrics(group)

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
                check.reason += " (downgraded: n < min_runs)"
            result.checks.append(check)

        if base_metrics is not None:
            for metric, tol in sorted(regression_cfg.items()):
                check = _check_regression(
                    label,
                    metric,
                    metrics.get(metric),
                    base_metrics.get(metric),
                    float(tol),
                )
                if underpowered and check.status == "fail":
                    check.status = "warn"
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
    lines.append(f"# SAR Regression Gate — {verdict}")
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

    if args.warn_only:
        return 0
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
