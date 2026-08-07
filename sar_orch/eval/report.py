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

#: attempt-family（P5）报告家族标识。workflow 产出的 attempt 报告与 legacy root
#: 报告以此区分，聚合/门禁据此永不混池（设计 §7.2 / §1.2-15）。
ATTEMPT_REPORT_FAMILY = "attempt-v2"

#: workflow `evidence/grader_results.json` 载荷 → attempt report `episode` 块映射。
#: 与 `merge_results` 的 episode_out 字段对齐，使 attempt aggregate 能复用
#: `aggregate_group` 的指标抽取（design §5.3：相同输入 SHA-256 必须稳定）。
EPISODE_DETAIL_FIELDS: dict[str, str] = {
    "coverage": "final_coverage",
    "coverage_verified": "coverage_verified",
    "transport_rate": "final_transport_rate",
    "subtask_completion_rate": "subtask_completion_rate",
    "finished": "finished",
    "steps": "total_steps",
    "total_tokens": "total_tokens",
    "balance": "balance",
    "idle_ratio": "idle_ratio",
    "tool_outcomes": "tool_outcomes",
    "timeout_steps": "timeout_steps",
    "end_reason": "end_reason",
    "step_efficiency": "step_efficiency",
    "token_efficiency": "token_efficiency",
    "completed_subtasks": "completed_subtasks_trajectory",
    "total_subtasks": "checker_subtask_total",
    "dispatch_count": "dispatch_count",
    "map_overhead_ratio": "map_overhead_ratio",
    "progress_curve": "progress_curve",
}


def project_episode_from_grader_results(results: list[dict]) -> dict:
    """从 workflow grader_results 载荷投影 attempt report 的 `episode` 指标块。

    只取 OutcomeGrader（与 legacy `merge_results` 同源的指标口径）；缺失 detail
    或非 OutcomeGrader 时返回空块 —— aggregate 下游按缺失指标处理，不伪造 0。
    """
    for r in results:
        if r.get("grader") != "OutcomeGrader":
            continue
        detail = r.get("detail") or {}
        return {key: detail.get(src) for key, src in EPISODE_DETAIL_FIELDS.items()}
    return {}


#: evaluator semantics 版本。Phase 0 冻结的字面量（P3.1），实施时不得临场决定。
#: 这是 **evaluator 版本**，不是 experiment `code_commit` —— 代码提交标识实验
#: 运行时的仓库状态，这里标识评测尺子本身（attempt-stream vs 旧首行/trajectory
#: 口径）。两者不可互相替代：同一次评测可能换了 code 却保留尺子，也可能只换了
#: 尺子（本次 P1）而 code 未动。
EVAL_SEMANTICS_VERSION = "attempt-stream-v1"

#: ErrorTaxonomy 提升到 report 顶层的四个证据残差桶。这些是"无法用环境动作尺子
#: 归因"的证据残差（timeout 独占 slot / error-observation 工具异常 / trajectory
#: 失败但无已观察 attempt / 非 SAR 失败 query），**不是** observed environment
#: attempt 失败 —— 不得混入 failure_taxonomy 的百分比分母或 gate 数值指标。
FAILURE_DIAGNOSTIC_BUCKETS = (
    "infrastructure_timeout_failures",
    "tool_execution_failures",
    "unobserved_trajectory_failures",
    "unmapped_failures",
)

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

    # step efficiency —— 每步完成的 checker 子任务数。
    # 阈值 0.2 ≈ 每 5 步至少推进一个子任务。原先的 0.5（每 2 步一个）对 SAR
    # 过严：任务含大量必要的探索/导航步，实测健康 run 也只有 0.4 左右，
    # 会让这条建议恒定触发而失去判别力。
    se = ep.get("step_efficiency", 0)
    if isinstance(se, (int, float)) and se < 0.2:
        suggestions.append(
            f"**Step efficiency low** ({se:.2f} completed subtasks/step): Coordinator should issue more focused, "
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
    # 四个证据残差桶提升到 report 顶层（P3.1）。默认 0：旧 grader detail
    # 缺键时保持 0/兼容，不因键缺失而崩。
    failure_diagnostics = {bucket: 0 for bucket in FAILURE_DIAGNOSTIC_BUCKETS}
    constraint_violations = []
    trajectory_checks = []

    for r in results:
        d = r.detail
        if r.grader == "OutcomeGrader":
            episode_out = {
                "coverage": d.get("final_coverage"),
                # 成功感知覆盖率：与 coverage 并列的新口径，只统计**成功**
                # 交互过的目标对象。旧 coverage 保持论文口径不变（含
                # success-agnostic 子串匹配），两者刻意并存，勿合并。
                "coverage_verified": d.get("coverage_verified"),
                "transport_rate": d.get("final_transport_rate"),
                # 同值正名字段：TR 实为 checker 子任务完成率，见 outcome.py。
                "subtask_completion_rate": d.get("subtask_completion_rate"),
                "finished": d.get("finished"),
                "steps": d.get("total_steps"),
                "total_tokens": d.get("total_tokens"),
                "balance": d.get("balance"),
                "idle_ratio": d.get("idle_ratio"),
                # 逐工具计数：框架 A/B 的主要改进证据（SR 只作不退步约束，
                # 见 DESIGN 3.1b）。放进 episode 而非只留在 grader_results 里，
                # 是为了让对比工具无需理解 grader 结构就能读到。
                "tool_outcomes": d.get("tool_outcomes"),
                "timeout_steps": d.get("timeout_steps"),
                "end_reason": d.get("end_reason"),
                "step_efficiency": d.get("step_efficiency"),
                "token_efficiency": d.get("token_efficiency"),
                # checker 口径：分子分母必须同源，勿混入 dispatch 计数
                "completed_subtasks": d.get("completed_subtasks_trajectory"),
                "total_subtasks": d.get("checker_subtask_total"),
                "dispatch_count": d.get("dispatch_count"),
                "map_overhead_ratio": d.get("map_overhead_ratio"),
                "progress_curve": d.get("progress_curve"),
            }

        if r.grader == "ErrorTaxonomy":
            failure_taxonomy = d.get("failure_taxonomy", {})
            # 四个证据残差桶以数值计数表达。grader detail 以 list 表达
            # （逐条证据），report 层收敛为计数；旧 detail 缺键保持 0。
            for bucket in FAILURE_DIAGNOSTIC_BUCKETS:
                raw = d.get(bucket)
                if isinstance(raw, (list, tuple)):
                    failure_diagnostics[bucket] = len(raw)
                elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    failure_diagnostics[bucket] = int(raw)

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
        # 以下字段服务 aggregate 的**配置一致性检查**。LLM 配置已定为恒定量
        # （不作对比轴），故批内配置漂移是**污染**而非变量：不同 model/provider
        # 的 run 池化进同一个 CI，会把配置差异算进"方差"而报告上看不出来。
        # 必须逐 run 落盘才能在聚合时发现不一致 —— 只记第一个 run 的配置
        # 等于假定它们一致，而那正是需要被检查的事。
        "provider": episode.metadata.get("provider"),
        "api_base": episode.metadata.get("api_base"),
        "temperature": episode.metadata.get("temperature"),
        "llm_seed": episode.metadata.get("llm_seed"),
        "llm_seed_supported": episode.metadata.get("llm_seed_supported"),
        # prompt/skill 内容身份（T2）。框架 A/B 的对比轴就是它，
        # 故它在一批内**应当**一致，跨批**应当**不同。
        "prompt_hash": episode.metadata.get("prompt_hash"),
        "prompt_version": episode.metadata.get("prompt_version"),
        "code_commit": episode.metadata.get("code_commit"),
        "git_dirty": episode.metadata.get("git_dirty"),
        "max_steps": episode.metadata.get("max_steps"),
        # evaluator 版本（P3.1）：标识评测尺子本身，不等同 code_commit。
        # 每次 merge_results 都写死当前尺子版本，聚合/门禁据此拒绝跨语义混池。
        "eval_semantics_version": EVAL_SEMANTICS_VERSION,
    }

    grader_results = [r.__dict__ for r in results]

    report: dict[str, Any] = {
        "run_dir": str(episode.run_dir),
        "metadata": metadata_out,
        "episode": episode_out,
        "failure_taxonomy": failure_taxonomy,
        # 证据残差提升到顶层（P3.1）：四桶计数，独立于 failure_taxonomy，
        # 不得进入 gate 数值指标或 failure_taxonomy 百分比分母。
        "failure_diagnostics": failure_diagnostics,
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
    _md(
        f"| Coverage | {_fmt(ep.get('coverage'), '.1%')} | 环境覆盖完成度"
        "（**论文口径**：只匹配动作文本，不看动作是否成功）|"
    )
    _md(
        f"| Coverage (verified) | {_fmt(ep.get('coverage_verified'), '.1%')} | "
        "成功感知覆盖率：只计**成功**交互过的目标对象。低于上一行说明存在"
        "「念到名字但没做成」的动作；`-` 表示无从判定（scene 未知）|"
    )
    _md(
        f"| Transport Rate | {_fmt(ep.get('transport_rate'), '.1%')} | "
        "**= checker 子任务完成率**，非「运输」；分母经 `list(set(...))` 去重"
        "（`SAR/Scenes/checker.py:117`），随场景布局变化，跨场景不可直接比 |"
    )
    _md(f"| Finished | {_fmt(ep.get('finished'))} | 是否完成所有目标 |")
    _md(f"| Steps | {_fmt(ep.get('steps'), 'd')} | 总步数 |")
    _md(f"| End Reason | {_fmt(ep.get('end_reason'))} | 终止原因 |")
    _md(
        f"| Total Tokens | {_fmt(ep.get('total_tokens'), '.0f')} | 所有 LLM 调用总 token 数 |"
    )
    _md(
        f"| Balance | {_fmt(ep.get('balance'), '.3f')} | "
        "min(agent成功动作)/(max(agent成功动作)+1e-4)，论文 §5。"
        "**诊断量，不作优化目标** —— min/max 结构性惩罚角色分工，"
        "低值可能恰是好的协作，故已移出门禁。"
        "（曾据「失败 run 0.841 > 成功 run 0.805」称其与成功反相关，"
        "但补做 Mann-Whitney U 后 p=0.485、效应量 +0.021，未达显著 —— "
        "降级依据是上述结构性缺陷，不是相关性证据）|"
    )
    _md(
        f"| Idle Ratio | {_fmt(ep.get('idle_ratio'), '.1%')} | "
        "(NoOp + 失败动作)/总动作数。balance 的替代诊断量：衡量真正的浪费，"
        "不惩罚分工 |"
    )
    _md(
        f"| Step Efficiency | {_fmt(ep.get('step_efficiency'), '.2f')} | "
        "已完工子任务/总步数。**与 TR 分子同源**，独立信息量有限；"
        "仅在同 max_steps 下可比 |"
    )
    _md(
        f"| Token Efficiency | {_fmt(ep.get('token_efficiency'), '.0f')} | 总 token/轨迹已完工子任务 |"
    )
    _md(
        f"| Map Overhead Ratio | {_fmt(ep.get('map_overhead_ratio'), '.1%')} | 地图管线 token 占比 |"
    )
    _md(
        f"| Completed Subtasks | {_fmt(ep.get('completed_subtasks'), 'd')}"
        f"/{_fmt(ep.get('total_subtasks'), 'd')} | 环境 checker 已完工/总子任务（TR 的分子分母）|"
    )
    _md(
        f"| Dispatch Count | {_fmt(ep.get('dispatch_count'), 'd')} | "
        "协调器下发的任务条数（非环境子任务，不参与 TR）|"
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

    # === 2b. 证据残差（单列，不进失败归因百分比分母）===
    _md("### 证据残差")
    _md("")
    _md(
        "以下四桶是**无法用环境动作尺子归因**的证据残差（timeout 独占 slot / "
        "error-observation 工具异常 / trajectory 失败但无已观察 attempt / "
        "非 SAR 失败 query），与上方「动作失败归因」并列但**不计入其百分比分母**："
    )
    _md("")
    _md("| 诊断桶 | 计数 |")
    _md("|---|---|")
    diag = report.get("failure_diagnostics", {}) or {}
    for bucket in FAILURE_DIAGNOSTIC_BUCKETS:
        _md(f"| {bucket} | {_fmt(diag.get(bucket), 'd')} |")
    _md("")

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
