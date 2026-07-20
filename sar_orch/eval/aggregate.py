from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    if fmt == ".2f":
        return f"{float(val):.2f}"
    if fmt == ".4f":
        return f"{float(val):.4f}"
    if fmt == "d":
        return str(int(val))
    if fmt == ".0f":
        return f"{float(val):.0f}"
    if fmt == ".2%":
        return f"{float(val):.2%}"
    return str(val)


def _mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def _numeric_stats(vals: list[float]) -> dict[str, float]:
    clean = [v for v in vals if v is not None]
    if not clean:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": _mean(clean),
        "std": _std(clean),
        "min": min(clean),
        "max": max(clean),
    }


# ── scan / group ──────────────────────────────────────────────────────────


def scan_results(root_dir: Path) -> tuple[list[dict], list[str]]:
    reports: list[dict] = []
    skipped: list[str] = []
    if not root_dir.exists():
        return reports, [f"{root_dir} (not found)"]
    for child in sorted(root_dir.iterdir()):
        if not child.is_dir():
            continue
        report_path = child / "eval_report.json"
        if not report_path.exists():
            skipped.append(child.name)
            continue
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            reports.append(data)
        except (json.JSONDecodeError, OSError) as e:
            skipped.append(f"{child.name} (parse error: {e})")
    return reports, skipped


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


def aggregate_group(reports: list[dict]) -> dict[str, Any]:
    n = len(reports)
    meta0 = reports[0].get("metadata", {})
    key = {"scene": meta0.get("scene"), "agents": meta0.get("agents")}
    runs = [r.get("run_dir", "?") for r in reports]
    seeds = [r.get("metadata", {}).get("seed") for r in reports]

    c = sum(1 for r in reports if r.get("episode", {}).get("finished") is True)

    pass_at_k_dict: dict[str, float | None] = {}
    pass_k_dict: dict[str, float | None] = {}
    for k in range(1, n + 1):
        pass_at_k_dict[str(k)] = pass_at_k(n, c, k)
        pass_k_dict[str(k)] = pass_k(n, c, k)

    # collect numeric episode fields
    coverage_vals: list[float] = []
    transport_vals: list[float] = []
    balance_vals: list[float] = []
    token_eff_vals: list[float] = []
    step_eff_vals: list[float] = []
    total_token_vals: list[float] = []
    steps_vals: list[float] = []
    end_reasons: dict[str, int] = {}

    for r in reports:
        ep = r.get("episode", {})
        _maybe_add(coverage_vals, ep, "coverage")
        _maybe_add(transport_vals, ep, "transport_rate")
        _maybe_add(balance_vals, ep, "balance")
        _maybe_add(token_eff_vals, ep, "token_efficiency")
        _maybe_add(step_eff_vals, ep, "step_efficiency")
        _maybe_add(total_token_vals, ep, "total_tokens")
        _maybe_add(steps_vals, ep, "steps")
        er = ep.get("end_reason", "unknown") or "unknown"
        end_reasons[er] = end_reasons.get(er, 0) + 1

    episode_stats = {
        "coverage": _numeric_stats(coverage_vals),
        "transport_rate": _numeric_stats(transport_vals),
        "balance": _numeric_stats(balance_vals),
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
        "finished_count": c,
        "pass_at_k": pass_at_k_dict,
        "pass_k": pass_k_dict,
        "episode_stats": episode_stats,
        "end_reason_distribution": end_reasons,
        "failure_taxonomy": failure_taxonomy,
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
        _md("### 指标统计")
        _md()
        _md("| 指标 | mean | std | min | max |")
        _md("|---|---|---|---|---|")
        es = g["episode_stats"]
        for metric, label in [
            ("coverage", "Coverage"),
            ("transport_rate", "Transport Rate"),
            ("balance", "Balance"),
            ("token_efficiency", "Token Efficiency"),
            ("step_efficiency", "Step Efficiency"),
            ("total_tokens", "Total Tokens"),
            ("steps", "Steps"),
        ]:
            s = es.get(metric, {})
            _md(
                f"| {label} | {_fmt(s.get('mean'), '.4f')} | "
                f"{_fmt(s.get('std'), '.4f')} | "
                f"{_fmt(s.get('min'), '.4f')} | "
                f"{_fmt(s.get('max'), '.4f')} |"
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
