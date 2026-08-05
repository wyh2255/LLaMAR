from __future__ import annotations

from typing import TYPE_CHECKING

from sar_orch.eval.dataset import ENV_ACTION_NAMES
from sar_orch.eval.graders.base import GradeResult

if TYPE_CHECKING:
    from sar_orch.eval.dataset import AgentInteraction, EpisodeDataset

MOVEMENT_ACTIONS = {"Explore", "NavigateTo", "Move"}
CARRY_DROP_ACTIONS = {"Carry", "DropOff"}
ALLOWED_WHEN_CARRYING = MOVEMENT_ACTIONS | CARRY_DROP_ACTIONS | {"NoOp"}

EXCESSIVE_NOOP_THRESHOLD = 0.3

SUPPLY_TYPE_MAP = {"SAND": "Sand", "WATER": "Water", "A": "Sand", "B": "Water"}


def _inventory_resource_amount(inv: dict, supply_type: str) -> int:
    if inv is None:
        return 0
    resource_name = SUPPLY_TYPE_MAP.get(supply_type.upper(), supply_type.capitalize())
    return inv.get(resource_name, 0)


def grade_constraint(episode: EpisodeDataset) -> list[GradeResult]:
    """action-level 约束检查，判定单位 = 环境 action attempt（P1.2）。

    - 所有 action-level 规则遍历 `sr.interactions` 的**全部** SAR 环境 action
      attempt，不再取 `get_interaction()` 首行代表（query-first 时代表行是
      query，后续真实环境 action 会被漏掉）。
    - query / map / skill / report_observation 等非环境工具在白名单前被过滤，
      不参与任何 violation / repeat / noop 计数。
    - `error_observation=True` 的 SAR action 不进任何 rule / repeat counter /
      NoOp 分子分母 —— 其错误归因只属于 ErrorTaxonomy 的 tool-execution bucket。
    - repeat-failure counter 只由非 error-observation 的 `succeeded is False`
      更新，且同 step 重复 attempt 先去重 step（防单步重试伪造跨步循环）。
    - NoOp 比例的分子/分母同样基于非 timeout、非 error-observation 的 SAR
      attempts，detail 标明 `evaluation_unit: "environment_action_attempt"`。
    """
    violations: list[dict] = []
    repeat_counters: dict[str, list[int]] = {}
    noop_counts: dict[str, int] = {}
    sar_action_counts: dict[str, int] = {}
    timeout_agents_set: dict[int, set[str]] = {}
    # 报告 schema 面：保留动作后的 names/inventory/position 键（`compare_reports`
    # 会把消失的键判为差异），新增 *_before 计数器。与 inventory_before 落地时
    # 保留 inventory 键的做法一致。
    parse_miss_counts = {
        "names": 0,
        "names_before": 0,
        "inventory": 0,
        "position": 0,
        "inventory_before": 0,
        "supply_type": 0,
    }

    # 预建 agent 索引，不再在遍历路径里 .index()；未知 agent 的 attempt 照常
    # 判定并留 grader_skips 证据，不抛 ValueError。
    agent_index_by_name = {name: i for i, name in enumerate(episode.agent_names)}

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        timeout_agents = {
            episode.agent_names[i]
            for i in sr.timeout_agents
            if i < len(episode.agent_names)
        }
        timeout_agents_set[step_num] = timeout_agents

        for ai in sr.interactions:
            if ai.action_name not in ENV_ACTION_NAMES:
                continue
            if ai.agent in timeout_agents:
                continue
            if ai.error_observation:
                # 唯一错误归因属于 ErrorTaxonomy 的 tool-execution bucket。
                continue

            agent_name = ai.agent
            if agent_name not in agent_index_by_name:
                # 未知 agent：无 trajectory 槽可查，attempt 照常判定，留证据。
                episode.grader_skips.append(
                    {
                        "grader": "ConstraintGrader",
                        "reason": (
                            f"agent_interactions.csv:L{ai.csv_line} step={ai.step} "
                            f"agent={agent_name}: agent not in metadata agent_names; "
                            f"trajectory slot unavailable, attempt still graded"
                        ),
                    }
                )

            is_noop = ai.action_name == "NoOp"
            if is_noop:
                noop_counts[agent_name] = noop_counts.get(agent_name, 0) + 1
            sar_action_counts[agent_name] = sar_action_counts.get(agent_name, 0) + 1

            if ai.action_name == "UseSupply":
                _check_empty_supply(ai, violations, parse_miss_counts)

            if ai.action_name == "NavigateTo":
                _check_hallucinated_nav(ai, violations, parse_miss_counts)

            if ai.inventory is not None and ai.inventory.get("Person", 0) > 0:
                if ai.action_name not in ALLOWED_WHEN_CARRYING:
                    violations.append(
                        {
                            "rule": "restricted_action_violation",
                            "step": step_num,
                            "agent": agent_name,
                            "action": ai.action,
                            "severity": "medium",
                            "detail": f"Agent carrying person but executed {ai.action} (not in {sorted(ALLOWED_WHEN_CARRYING)})",
                            "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
                        }
                    )

            if ai.action_name == "GetSupply":
                _check_full_inventory_get(ai, violations, parse_miss_counts)

            # repeat counter 只由非 error-observation 的 succeeded=False 更新；
            # 同 step 重复 attempt 先去重 step（P1.2.4）。
            if ai.succeeded is False:
                key = f"{agent_name}:{ai.action}"
                steps = repeat_counters.setdefault(key, [])
                if not steps or steps[-1] != step_num:
                    steps.append(step_num)

    _check_repeat_failures(repeat_counters, violations, episode)

    _check_excessive_noop(
        noop_counts, sar_action_counts, violations, timeout_agents_set, episode
    )

    detail = {
        "violation_count": len(violations),
        "violations": violations,
        "parse_miss_counts": parse_miss_counts,
        # P1.2.5：action-level 统计的判定单位是环境 action attempt。
        "evaluation_unit": "environment_action_attempt",
    }
    if violations:
        results = [
            GradeResult(
                grader="ConstraintGrader",
                level="action",
                passed=False,
                score=max(0.0, 1.0 - len(violations) * 0.05),
                detail=detail,
                evidence_ref="agent_interactions.csv",
            )
        ]
    else:
        results = [
            GradeResult(
                grader="ConstraintGrader",
                level="action",
                passed=True,
                score=1.0,
                detail=detail,
                evidence_ref="agent_interactions.csv",
            )
        ]

    return results


def _check_empty_supply(ai: "AgentInteraction", violations: list, parse_miss: dict):
    """UseSupply 时库存里没有该类物资 = 违规。

    必须用 inventory_before（动作**前**的快照）。用 ai.inventory（动作后）会把
    "成功用掉最后一单位"判成违规 —— 这是 E-2，20 个 run 的 65 起该类违规全是
    此原因造成的误报。规则意图本身是对的，坏的是取值时间点。
    """
    if ai.inventory_before is None:
        # 该 agent 的首条交互（无前序快照）或前序 observation 未解析出库存。
        # 无证据不构成指控：记 parse_miss，不判违规（漏报优于误报）。
        parse_miss["inventory_before"] = parse_miss.get("inventory_before", 0) + 1
        return
    supply_type = ai.action_args[1] if len(ai.action_args) > 1 else ""
    # action_args 已在 dataset 层按工具声明顺序规范化，故 [1] 就是 supply_type。
    # 但若该行 ToolArgs 缺失/不可解析（重排未生效），[1] 可能仍是火名 ——
    # 那样会拿火名去库存里查、必然得 0、造出误报。无法解析成已知物资类型时
    # 记 parse_miss 而不指控。
    if supply_type.upper() not in SUPPLY_TYPE_MAP:
        parse_miss["supply_type"] = parse_miss.get("supply_type", 0) + 1
        return
    amt = _inventory_resource_amount(ai.inventory_before, supply_type)
    if amt <= 0:
        violations.append(
            {
                "rule": "empty_supply_use",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "high",
                "detail": (
                    f"Used supply type '{supply_type}' but inventory has 0 "
                    f"(inv_before={ai.inventory_before})"
                ),
                "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
            }
        )


def _check_hallucinated_nav(ai: "AgentInteraction", violations: list, parse_miss: dict):
    """NavigateTo 的目标在 agent **决定导航时**不可见 = 幻觉目标。

    必须用 visible_names_before（动作**前**的快照）。用 ai.visible_names（动作后、
    由环境在 step 之后生成）会犯与 E-2 (`_check_empty_supply`) /
    `_check_full_inventory_get` 完全同类的时序错误，且**双向**都错：
      · 误报：成功走到目标后，到达改变了周围可见集、目标本身掉出 Names 列表
        → 一次正确的导航被判成幻觉。实测 87 run 共 18 起。
      · 漏报：目标在动作前谁都看不见，但到达/探索后出现在列表里
        → 真正的幻觉逃脱指控。实测 3 起（含同一 step 两个 agent 同时逃脱）。
    规则意图（导航到看不见的东西 = 幻觉）本身是对的，坏的是取值时间点。
    """
    if ai.visible_names_before is None:
        # 该 agent 的首条交互（无前序快照）—— 无证据不构成指控：记 parse_miss，
        # 不判违规（漏报优于误报）。注意这里判 `is None` 而非真值：空列表是
        # "当时确实什么都看不见"，属于**可指控**证据，不能一并 bail 掉。
        parse_miss["names_before"] = parse_miss.get("names_before", 0) + 1
        return
    target = ai.action_args[0] if ai.action_args else ""
    if target and target not in ai.visible_names_before:
        violations.append(
            {
                "rule": "hallucinated_nav_target",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "high",
                "detail": (
                    f"NavigateTo('{target}') but target not in visible Names list "
                    f"before the action: {ai.visible_names_before}"
                ),
                "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
            }
        )


def _check_full_inventory_get(
    ai: "AgentInteraction", violations: list, parse_miss: dict
):
    """GetSupply 时库存已满 = 违规（浪费动作）。

    必须用 inventory_before（动作**前**的快照）。用 ai.inventory（动作后）会把
    "取到第 3 个单位、取完刚好满仓"的成功 GetSupply 误判成"满仓还硬取"——
    这是与 E-2 同类的时序缺陷：20 个 run 的 40 起该类违规全在成功的 GetSupply
    上产生（详见 `_check_empty_supply` 的说明）。规则意图（满仓还取=浪费）
    本身是对的，坏的是取值时间点。
    """
    if ai.inventory_before is None:
        # 该 agent 的首条交互（无前序快照）或前序 observation 未解析出库存。
        # 无证据不构成指控：记 parse_miss，不判违规（漏报优于误报）。
        parse_miss["inventory_before"] = parse_miss.get("inventory_before", 0) + 1
        return
    capacity = 3
    occupied = sum(ai.inventory_before.values())
    if occupied >= capacity:
        violations.append(
            {
                "rule": "full_inventory_get",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "low",
                "detail": (
                    f"GetSupply with full inventory (occupied={occupied}/{capacity}, "
                    f"inv_before={ai.inventory_before})"
                ),
                "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
            }
        )


def _check_repeat_failures(
    counter: dict[str, list[int]], violations: list, episode: EpisodeDataset
):
    for key, steps in counter.items():
        if len(steps) >= 3:
            consecutive = True
            for i in range(1, len(steps)):
                if steps[i] != steps[i - 1] + 1:
                    consecutive = False
                    break
            if consecutive:
                agent, action = key.split(":", 1)
                violations.append(
                    {
                        "rule": "repeat_failure_loop",
                        "step": steps[0],
                        "agent": agent,
                        "action": action,
                        "severity": "medium",
                        "detail": f"Same failed action repeated {len(steps)} consecutive steps (steps {steps[0]}-{steps[-1]})",
                        "evidence_ref": f"trajectory.csv (steps {steps[0]}-{steps[-1]})",
                    }
                )


def _check_excessive_noop(
    noop_counts: dict[str, int],
    sar_counts: dict[str, int],
    violations: list,
    timeout_agents_set: dict[int, set[str]],
    episode: EpisodeDataset,
):
    for agent_name in episode.agent_names:
        total = sar_counts.get(agent_name, 0)
        noops = noop_counts.get(agent_name, 0)
        if total > 0 and (noops / total) > EXCESSIVE_NOOP_THRESHOLD:
            violations.append(
                {
                    "rule": "excessive_noop",
                    "step": 0,
                    "agent": agent_name,
                    "action": "NoOp",
                    "severity": "low",
                    "detail": (
                        f"LLM-chosen NoOp ratio {noops}/{total} ({noops / total:.1%}) "
                        f"exceeds threshold {EXCESSIVE_NOOP_THRESHOLD:.0%} "
                        f"(TimeoutAgents excluded)"
                    ),
                    "evidence_ref": "agent_interactions.csv (aggregate)",
                }
            )


CONSTRAINT_GRADER_NAME = "ConstraintGrader"
