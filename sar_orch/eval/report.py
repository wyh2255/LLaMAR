from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.base import GradeResult

EXPECTED_GRADERS = [
    "OutcomeGrader",
    "StateGrader",
    "ConstraintGrader",
    "ErrorTaxonomy",
    "TrajectoryGrader",
]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


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
    return str(val)


def _suggestions(report: dict) -> list[str]:
    suggestions: list[str] = []
    tx = report.get("failure_taxonomy", {})
    violations = report.get("constraint_violations", [])
    traj_checks = report.get("trajectory_checks", [])
    judge = report.get("llm_judge", {})
    ep = report.get("episode", {})

    # collect counts by violation rule and taxonomy category
    vrule_counts: dict[str, int] = {}
    for v in violations:
        r = v.get("rule", "unknown")
        vrule_counts[r] = vrule_counts.get(r, 0) + 1

    total_failures = sum(tx.values())
    top_tax = max(tx, key=lambda k: tx[k]) if tx else None
    top_vrule = (
        max(vrule_counts, key=lambda k: vrule_counts[k]) if vrule_counts else None
    )

    # taxonomy-based suggestions
    tax_suggestions = {
        "not_visible": "Coordinator should confirm target visibility before dispatching — teach agent to query shared state for target existence before issuing NavigateTo/UseSupply",
        "empty_supply_use": "Worker should check inventory before UseSupply — add 'check inventory first' step to firefighting prompts",
        "hallucinated_nav_target": "Strengthen target validation in prompts — require coordinator/worker to verify target exists in visible Names list before navigating",
        "obstacle_blocked": "Add path-planning or retry-with-alternative-direction strategy for Move/Explore commands",
        "not_interactable": "Dispatch should consider agent-target proximity — query position before issuing interaction commands",
        "restricted_action": "Enforce carry-state constraints in prompts — agent carrying person should only Move/NavigateTo/DropOff",
        "infrastructure": "Investigate barrier/network timeout causes — reduce per-step timeout or add retry logic",
        "unknown": "Review unclassified failures manually — high unknown ratio indicates taxonomy rules need expansion",
    }
    if top_tax and top_tax in tax_suggestions:
        pct = tx[top_tax] / total_failures * 100 if total_failures > 0 else 100
        suggestions.append(
            f"**{top_tax}** is the most common failure category ({tx[top_tax]}/{total_failures}, {pct:.0f}%). "
            f"{tax_suggestions[top_tax]}."
        )

    # violation-rule-based suggestions
    vrule_suggestions = {
        "empty_supply_use": "Worker should verify inventory has required resource before UseSupply — add inventory check step to agent prompts",
        "hallucinated_nav_target": "Coordinator/worker must verify target exists in current visible Names list before issuing NavigateTo — consider adding a 'lookup target' tool",
        "excessive_noop": "Coordinator should issue specific action instructions instead of 'standby/NoOp' — worker auto-NoOp should have a lower ratio with concrete task assignments",
        "restricted_action_violation": "Prompts must enforce that carrying agents only use Move/NavigateTo/DropOff/NoOp — add carry-state guardrails",
        "repeat_failure_loop": "Add deadlock detection in worker loop — after 3 consecutive failures, agent should try alternative action or request coordinator guidance",
        "full_inventory_get": "Worker should check inventory capacity before GetSupply — skip or clear inventory first",
    }
    if top_vrule and top_vrule in vrule_suggestions:
        suggestions.append(
            f"**{top_vrule}** is the most common constraint violation ({vrule_counts[top_vrule]} occurrences). "
            f"{vrule_suggestions[top_vrule]}"
        )

    # trajectory check suggestions
    for tc in traj_checks:
        ck = tc.get("check", "")
        if tc.get("passed") is False:
            if ck == "rescue_flow":
                suggestions.append(
                    "**Rescue flow incomplete**: Ensure `DropOff` tool is available and correctly named in agent toolset. "
                    "Coordinator must assign ≥2 agents to carry the same person and follow up with deposit navigation instructions."
                )
            elif ck == "fire_flow":
                suggestions.append(
                    "**Fire flow violation**: GetSupply must precede UseSupply for each resource type. "
                    "Add resource acquisition step before firefighting in agent prompts."
                )
            elif ck == "coop_carry":
                suggestions.append(
                    "**Cooperative carry missing**: Assign ≥2 agents to `Carry` the same person in the same step. "
                    "Coordinator should coordinate synchronized carry dispatches."
                )
        if ck == "exploration_efficiency":
            repeat_ratio = tc.get("detail", {}).get("repeat_ratio", 0)
            if isinstance(repeat_ratio, (int, float)) and repeat_ratio > 0.3:
                suggestions.append(
                    f"**Exploration efficiency low** (repeat ratio {repeat_ratio:.1%}): "
                    f"Coordinator should track already-visited targets to reduce redundant NavigateTo dispatches."
                )

    # judge-based suggestions
    dispatch = judge.get("dispatch", {})
    obs = judge.get("observation", {})
    dp_rate = dispatch.get("pass_rate")
    if dp_rate is not None and dp_rate < 0.5:
        suggestions.append(
            f"**Dispatch pass rate low** ({dp_rate:.0%}): Coordinator should issue dispatches every step, "
            f"not only when a dispatch event occurs. Implement a tick-based loop that checks for completed tasks "
            f"and reassigns agents."
        )
    hr = obs.get("hallucination_rate")
    if hr is not None and hr > 0.1:
        suggestions.append(
            f"**Observation hallucination rate high** ({hr:.0%}): Strengthen the observation pipeline — "
            f"require workers to validate each object claim against current explore() output before reporting."
        )
    for tc in traj_checks:
        ck = tc.get("check", "")
        if ck == "rescue_flow" and tc.get("passed") is False:
            suggestions.append(
                "**Rescue flow incomplete**: Ensure `DropOff` tool is available and correctly named in agent toolset. "
                "Coordinator must assign ≥2 agents to carry the same person and follow up with deposit navigation instructions."
            )

    # map overhead
    mor = ep.get("map_overhead_ratio", 0)
    if isinstance(mor, (int, float)) and mor > 0.1:
        suggestions.append(
            f"**Map overhead ratio {mor:.1%} is high**: Reduce MapAgent/MapSummarizer invocation frequency "
            f"or increase the interval between semantic map updates."
        )

    # step efficiency
    se = ep.get("step_efficiency", 0)
    if isinstance(se, (int, float)) and se < 0.5:
        suggestions.append(
            f"**Step efficiency low** ({se:.2f} completed tasks/step): Coordinator should issue more focused, "
            f"high-value dispatches and avoid NoOp-heavy stretches."
        )

    if not suggestions:
        suggestions.append("No actionable improvement suggestions identified.")

    return list(dict.fromkeys(suggestions))  # deduplicate preserving order


def _sv_key(v: dict) -> tuple:
    return (SEVERITY_ORDER.get(v.get("severity", "low"), 99), v.get("step", 0))


def merge_results(
    episode: EpisodeDataset,
    results: list[GradeResult],
    llm_judge: dict | None = None,
    grader_skips: list[dict] | None = None,
    conclusion: str | None = None,
) -> dict:
    if grader_skips is None:
        grader_skips = episode.grader_skips

    found_grader_names = {r.grader for r in results}
    for expected in EXPECTED_GRADERS:
        if expected not in found_grader_names:
            grader_skips.append(
                {
                    "grader": expected,
                    "reason": "missing — agent did not run this grader",
                }
            )

    episode_out = {}
    failure_taxonomy = {}
    constraint_violations = []
    trajectory_checks = []

    for r in results:
        d = r.detail
        if r.grader == "OutcomeGrader":
            episode_out = {
                "coverage": d.get("final_coverage"),
                "transport_rate": d.get("final_transport_rate"),
                "finished": d.get("finished"),
                "steps": d.get("total_steps"),
                "total_tokens": d.get("total_tokens"),
                "balance": d.get("balance"),
                "end_reason": d.get("end_reason"),
                "step_efficiency": d.get("step_efficiency"),
                "token_efficiency": d.get("token_efficiency"),
                "completed_subtasks": d.get("completed_subtasks_trajectory"),
                "total_subtasks": d.get("total_subtasks"),
                "map_overhead_ratio": d.get("map_overhead_ratio"),
                "progress_curve": d.get("progress_curve"),
            }

        if r.grader == "ErrorTaxonomy":
            failure_taxonomy = d.get("failure_taxonomy", {})

        if r.grader == "ConstraintGrader":
            constraint_violations = d.get("violations", [])

        if r.grader == "TrajectoryGrader":
            trajectory_checks.append(
                {
                    "check": d.get("check", r.grader),
                    "passed": r.passed,
                    "score": r.score,
                    "detail": d,
                }
            )

    metadata_out = {
        "scene": episode.metadata.get("scene"),
        "agents": episode.metadata.get("agent_count"),
        "seed": episode.metadata.get("seed"),
        "model": episode.metadata.get("model"),
        "state_mode": episode.metadata.get("state_mode"),
        "run_id": episode.metadata.get("run_id"),
    }

    grader_results = [r.__dict__ for r in results]

    report: dict[str, Any] = {
        "run_dir": str(episode.run_dir),
        "metadata": metadata_out,
        "episode": episode_out,
        "failure_taxonomy": failure_taxonomy,
        "constraint_violations": constraint_violations,
        "trajectory_checks": trajectory_checks,
        "llm_judge": llm_judge or {},
        "grader_skips": grader_skips,
        "grader_results": grader_results,
    }

    if conclusion:
        report["conclusion"] = conclusion

    return report


def write_report(report: dict, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)


def write_report_md(report: dict, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def _md(s: str) -> None:
        lines.append(s)

    meta = report.get("metadata", {})
    ep = report.get("episode", {})
    tx = report.get("failure_taxonomy", {})
    violations = report.get("constraint_violations", [])
    traj_checks = report.get("trajectory_checks", [])
    judge = report.get("llm_judge", {})
    conclusion = report.get("conclusion")
    skips = report.get("grader_skips", [])

    _md(f"# SAR Eval Report — `{report.get('run_dir', '')}`")
    _md("")
    _md(
        f"- **Scene {meta.get('scene')}** · {meta.get('agents')} agents · seed {meta.get('seed')}"
    )
    _md(f"- **Model**: {meta.get('model')} · **Mode**: {meta.get('state_mode')}")
    _md(f"- **Run ID**: {meta.get('run_id', '-')}")
    _md("")

    # === 1. 指标总表 ===
    _md("## 1. 指标总表")
    _md("")
    _md("| 指标 | 值 | 说明 |")
    _md("|---|---|---|")
    _md(f"| Coverage | {_fmt(ep.get('coverage'), '.1%')} | 环境覆盖完成度 |")
    _md(
        f"| Transport Rate | {_fmt(ep.get('transport_rate'), '.1%')} | 物资运输完成度 |"
    )
    _md(f"| Finished | {_fmt(ep.get('finished'))} | 是否完成所有目标 |")
    _md(f"| Steps | {_fmt(ep.get('steps'), 'd')} | 总步数 |")
    _md(f"| End Reason | {_fmt(ep.get('end_reason'))} | 终止原因 |")
    _md(
        f"| Total Tokens | {_fmt(ep.get('total_tokens'), '.0f')} | 所有 LLM 调用总 token 数 |"
    )
    _md(
        f"| Balance | {_fmt(ep.get('balance'), '.2f')} | min(agent成功动作)/max(agent成功动作) |"
    )
    _md(
        f"| Step Efficiency | {_fmt(ep.get('step_efficiency'), '.2f')} | 已完工子任务/总步数 |"
    )
    _md(
        f"| Token Efficiency | {_fmt(ep.get('token_efficiency'), '.0f')} | 总 token/轨迹已完工子任务 |"
    )
    _md(
        f"| Map Overhead Ratio | {_fmt(ep.get('map_overhead_ratio'), '.1%')} | 地图管线 token 占比 |"
    )
    _md(
        f"| Completed Subtasks | {_fmt(ep.get('completed_subtasks'), 'd')}/{_fmt(ep.get('total_subtasks'), 'd')} | 轨迹累计已完工/总子任务 |"
    )
    _md("")

    # === 2. 失败归因分布 ===
    _md("## 2. 失败归因分布")
    _md("")
    total_fail = sum(tx.values())
    if total_fail > 0:
        _md(f"共 **{total_fail}** 次动作失败，分类如下：")
        _md("")
        _md("| 类别 | 次数 | 占比 | 说明 |")
        _md("|---|---|---|---|")
        cat_desc = {
            "not_visible": "目标不在可见列表",
            "not_interactable": "目标可见但超出交互范围",
            "obstacle_blocked": "被障碍物或边界阻挡",
            "restricted_action": "搬人状态下执行非法动作",
            "infrastructure": "系统超时自动填充",
            "unknown": "无法归因（需人工审查）",
        }
        for cat, cnt in sorted(tx.items(), key=lambda x: -x[1]):
            desc = cat_desc.get(cat, "")
            _md(f"| {cat} | {cnt} | {cnt / total_fail:.1%} | {desc} |")
        _md("")
    else:
        _md("无动作失败记录。")
        _md("")

    # unmapped_failures from grader_results
    for gr in report.get("grader_results", []):
        if gr.get("grader") == "ErrorTaxonomy":
            uf = gr.get("detail", {}).get("unmapped_failures", [])
            if uf:
                _md(f"⚠ 另有 **{len(uf)}** 条无法映射的失败记录（未归入上述分类）：")
                _md("")
                _md("| Step | Agent | Action | Reason |")
                _md("|---|---|---|---|")
                for u in uf:
                    _md(
                        f"| {u.get('step', '?')} | {u.get('agent', '?')} | {u.get('interaction_action', '-')} | {u.get('reason', '')} |"
                    )
                _md("")
            break

    # === 3. 严重违规 Top-N ===
    _md("## 3. 严重违规 Top-N")
    _md("")
    if violations:
        sorted_v = sorted(violations, key=_sv_key)
        _md(f"共 {len(violations)} 条约束违规，按严重度排列：")
        _md("")
        for i, v in enumerate(sorted_v, 1):
            sv = v.get("severity", "low")
            marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sv, "⚪")
            ref = v.get("evidence_ref", "")
            _md(
                f"{i}. {marker} **[{sv.upper()}]** Step {v.get('step', '?')} · {v.get('agent', '?')} — `{v.get('action', '?')}`"
            )
            _md(f"   - {v.get('detail', '')}")
            if ref:
                _md(f"   - Evidence: `{ref}`")
            _md("")
    else:
        _md("无约束违规记录。")
        _md("")

    # === 4. Judge 结果 ===
    _md("## 4. LLM Judge 结果")
    _md("")
    no_llm = not judge or (not judge.get("dispatch") and not judge.get("observation"))
    if no_llm:
        _md("> LLM Judge 未运行（`--no-llm-judge` 模式）。本节数据不可用。")
        _md("")
    else:
        jm = judge.get("judge_model", "?")
        sw = judge.get("same_model_warning", False)
        ff = judge.get("format_fallback", False)
        _md(
            f"- **Judge 模型**: `{jm}`"
            + (" ⚠ same as subject model (偏差警告)" if sw else "")
            + (" ⚠ format_fallback (非 canonical 格式降级)" if ff else "")
        )
        _md("")

        dispatch = judge.get("dispatch", {})
        obs = judge.get("observation", {})

        # Dispatch
        _md("### Dispatch 派遣质量")
        _md("")
        dp_steps = dispatch.get("sampled_steps", 0)
        dp_rate = dispatch.get("pass_rate")
        dp_rate_str = _fmt(dp_rate, ".1%") if dp_rate is not None else "-"
        _md(f"- 抽样 {dp_steps} 步, 整体 pass rate: **{dp_rate_str}**")
        _md("")
        verdicts = dispatch.get("verdicts", [])
        dim_labels = {
            "full_coverage": "全员覆盖",
            "role_match": "角色匹配",
            "map_awareness": "地图感知",
            "step_budget_awareness": "步数预算意识",
        }
        if verdicts:
            _md("| Step | " + " | ".join(dim_labels.values()) + " |")
            _md("|---|" + "|".join("---" for _ in dim_labels) + "|")
            for v in verdicts:
                vds = v.get("verdicts", {})
                cells = [str(v.get("step", "?"))]
                for dk in dim_labels:
                    val = vds.get(dk, "-")
                    cells.append(
                        "✅" if val == "pass" else "❌" if val == "fail" else str(val)
                    )
                _md("| " + " | ".join(cells) + " |")
            _md("")

        # Observation
        _md("### Observation 观测报告质量（幻觉检测）")
        _md("")
        hr = obs.get("hallucination_rate")
        hr_str = _fmt(hr, ".1%") if hr is not None else "-"
        sc = obs.get("sampled_claims", 0)
        _md(f"- 抽样 {sc} 条 claim, hallucination rate: **{hr_str}**")
        _md("")
        claims = obs.get("claims", [])
        unsupported = [c for c in claims if not c.get("supported", True)]
        if unsupported:
            _md(f"**{len(unsupported)} hallucinations（unsupported claims）：**")
            _md("")
            _md("| Agent | Step | Claim | Evidence |")
            _md("|---|---|---|---|")
            for c in unsupported:
                _md(
                    f"| {c.get('agent', '?')} | {c.get('step', '?')} | {c.get('claim', '')} | {c.get('evidence', '')} |"
                )
            _md("")

    # === 5. Trajectory Checks ===
    _md("## 5. 轨迹约束检查")
    _md("")
    if traj_checks:
        _md("| 检查项 | 结果 | 得分 | 详情 |")
        _md("|---|---|---|---|")
        for tc in traj_checks:
            ck = tc.get("check", "")
            passed = tc.get("passed")
            score = tc.get("score")
            detail = tc.get("detail", {})
            note = detail.get("note", "")
            status = "✅" if passed is True else "❌" if passed is False else "N/A"
            score_str = _fmt(score, ".2f") if score is not None else "-"
            _md(f"| {ck} | {status} | {score_str} | {note} |")
        _md("")

    # === 6. Agent 结论 ===
    _md("## 6. Agent 分析结论")
    _md("")
    if conclusion:
        for c_line in conclusion.strip().splitlines():
            _md(c_line)
        _md("")
    elif no_llm:
        _md("> Agent 分析未运行（`--no-llm-judge` 模式）。无结论文本。")
        _md("")
    else:
        _md("> Agent 结论文件缺失（`eval_workspace/conclusion.md` 不存在）。")
        _md("")

    # === 7. 建议改进点 ===
    _md("## 7. 建议改进点")
    _md("")
    suggestions = _suggestions(report)
    for i, s in enumerate(suggestions, 1):
        _md(f"{i}. {s}")
        _md("")

    # === 8. Grader Skips ===
    if skips:
        _md("## 附录: Grader 跳过记录")
        _md("")
        _md("| Grader | 原因 |")
        _md("|---|---|")
        for sk in skips:
            _md(f"| {sk.get('grader', '?')} | {sk.get('reason', '')} |")
        _md("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_both_reports(
    report: dict, output_arg: str | Path | None, results_dir: Path
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
        json_path = results_dir / "eval_report.json"
        md_path = results_dir / "eval_report.md"

    write_report(report, json_path)
    write_report_md(report, md_path)
    return json_path, md_path
