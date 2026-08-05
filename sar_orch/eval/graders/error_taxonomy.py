from __future__ import annotations

from typing import TYPE_CHECKING

from sar_orch.eval.dataset import ENV_ACTION_NAMES
from sar_orch.eval.graders.base import GradeResult

if TYPE_CHECKING:
    from sar_orch.eval.dataset import AgentInteraction, EpisodeDataset

"""
Interaction radius constants from SAR/core.py:
- Agent visibility: 3*sqrt(2) ≈ 4.24 (core.py:1863, set_radius for agents)
- Reservoir radius: 3*sqrt(2) ≈ 4.24 (core.py:1832)
- Deposit radius:  3*sqrt(2) ≈ 4.24 (core.py:1838)
- Person radius:   FIND_PROBABILITY * max_corner_distance (core.py:1859, variable)
- Fire radius:     variable (set via procedural_generation, core.py:985)

Note: The `sees()` method (core.py:399-401) checks `self.position.within_radius(othr.position)`
which evaluates to `euclidean_distance <= self.radius`. For `not_interactable` classification
in ErrorTaxonomy, the interaction check is target-object-centric:
  - fire.sees(agent)  → fire's radius vs agent position (core.py:2233)
  - deposit.sees(agent) → deposit's radius vs agent position (core.py:2251)
  - reservoir.sees(agent) → reservoir's radius vs agent position (core.py:2268)

Since target object positions are not available in CSV exports, exact distance computation
is not feasible. This implementation uses heuristic classification:
  - target in Names list + not timeout + not restricted → likely not_interactable
True distance-based classification would require agent_interactions.csv to include
target object coordinates.
"""

# Standard interaction radius for common object types
# (used as reference value, not directly computable from CSV data)
INTERACTION_RADIUS = 3 * 2**0.5  # ≈ 4.24

MOVEMENT_ACTIONS = {"Explore", "NavigateTo", "Move"}
CARRY_DROP_ACTIONS = {"Carry", "DropOff"}
RESTRICTED_ACTIONS_SET = {"GetSupply", "UseSupply", "StoreSupply", "ClearInventory"}


def grade_error_taxonomy(episode: EpisodeDataset) -> list[GradeResult]:
    """Attempt-level 环境失败 taxonomy + 三条不可混合的证据残差通道。

    四条证据通道（P1.1，任一 (step, agent) 只落一个桶）：

    1. `failure_taxonomy` —— 已观察到的环境 action attempt 失败：
       非 timeout 槽内 `action_name in ENV_ACTION_NAMES`、`succeeded is False`、
       `error_observation is False`。不再以 trajectory.csv.Successes 作为
       observed-attempt 的失败门控（同 step 失败 attempt + 后续成功 action
       并存时，失败 attempt 必须保留）。
    2. `infrastructure_timeout_failures` —— TimeoutAgents 且 trajectory
       Successes=False 的 agent-slot 独占：无论是否有 interaction 都写入，
       raw attempts 仅作为 evidence 附在该 detail，绝不再进其他三个桶。
    3. `tool_execution_failures` —— error_observation=True 的任意工具行
       （SAR 或非 SAR）：结构性 harness 失败，不归因环境行为类别。
    4. `unobserved_trajectory_failures` —— 非 timeout 的 trajectory
       Successes=False 槽，且该 (step, agent) 没有任何 succeeded=False 的
       observed interaction（SAR/非 SAR/error 均算）时写入 generic residual；
       已有失败证据（taxonomy / tool / unmapped）的槽不得再创建 residual。
    5. `unmapped_failures` —— 仅非 SAR、succeeded=False、error_observation=False
       的工具失败（诊断桶，不混进环境 taxonomy）。

    `total_failures` 恒等于 `sum(failure_taxonomy.values())`；infrastructure /
    tool / residual 计数单独列出，禁止把不同来源相加伪装成环境失败率。
    """
    taxonomy: dict[str, int] = {}
    failure_details: list[dict] = []
    infrastructure_timeout_failures: list[dict] = []
    tool_execution_failures: list[dict] = []
    unobserved_trajectory_failures: list[dict] = []
    unmapped_failures: list[dict] = []
    parse_miss = {"names": 0, "inventory": 0, "position": 0}

    # 预建 agent 索引而不是重复 .index()：未知 agent 得到 None（无 trajectory
    # 槽可查），其 interaction 仍按 attempt 流处理，不抛 ValueError 也不静默漏掉。
    agent_index_by_name = {name: i for i, name in enumerate(episode.agent_names)}

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        timeout_indices = set(sr.timeout_agents)
        timeout_agent_names = {
            episode.agent_names[i]
            for i in timeout_indices
            if i < len(episode.agent_names)
        }

        # 该 step 的全部 interaction 按 agent 分组（完整 attempt 流，CSV 顺序）。
        interactions_by_agent: dict[str, list[AgentInteraction]] = {}
        for ai in sr.interactions:
            interactions_by_agent.setdefault(ai.agent, []).append(ai)

        # 判定的 agent 集合 = trajectory agent 槽 ∪ interaction 流里出现过的
        # agent（后者可能含 metadata 未收录的未知 agent）。
        agent_names_in_step = set(episode.agent_names) | set(interactions_by_agent)

        for agent_name in sorted(agent_names_in_step):
            agent_idx = agent_index_by_name.get(agent_name)
            traj_success = (
                episode.get_agent_success(step_num, agent_idx)
                if agent_idx is not None
                else None
            )
            traj_action = (
                episode.get_agent_action(step_num, agent_idx)
                if agent_idx is not None
                else None
            )
            rows = interactions_by_agent.get(agent_name, [])

            # 1) 仅 TimeoutAgents && trajectory.Successes=False 的 agent-slot
            #    由 infrastructure bucket 独占。TimeoutAgents 标记本身不足以
            #    吞掉 raw attempt：轨迹明确成功的 slot 仍按观察到的 attempt 归因。
            if agent_name in timeout_agent_names and traj_success is False:
                infrastructure_timeout_failures.append(
                    {
                        "step": step_num,
                        "agent": agent_name,
                        "trajectory_action": traj_action,
                        "trajectory_success": False,
                        "evidence_ref": "trajectory.csv:TimeoutAgents,Successes",
                        "reason": (
                            "agent in TimeoutAgents and trajectory "
                            "Successes=False: infrastructure failure (timeout), "
                            "attributed exclusively to this bucket"
                        ),
                        "attempts": [
                            {
                                "action": ai.action,
                                "succeeded": ai.succeeded,
                                "error_observation": ai.error_observation,
                                "evidence_ref": (
                                    f"agent_interactions.csv:L{ai.csv_line}"
                                ),
                            }
                            for ai in rows
                        ],
                    }
                )
                # infrastructure-owned slot 独占归因：跳过其余全部桶。
                continue

            # 2) 非 timeout slot：完整 attempt 流。
            failed_seen = False
            for ai in rows:
                if ai.succeeded is False:
                    failed_seen = True
                if ai.error_observation:
                    # error-observation 行（SAR 或非 SAR）→ 只进 tool bucket，
                    # 不归 obstacle/visibility 等环境行为类别，也不进 unmapped。
                    tool_execution_failures.append(
                        {
                            "step": step_num,
                            "agent": agent_name,
                            "action": ai.action,
                            "tool_name": ai.tool_name,
                            "succeeded": ai.succeeded,
                            "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
                            "detail": (
                                "tool execution error observation (Error: prefix) — "
                                "structural harness failure, not an environment "
                                "behaviour failure"
                            ),
                        }
                    )
                    continue
                if agent_idx is None:
                    # metadata 未收录的 agent 没有可映射的 trajectory slot；即使
                    # action 名属于环境词表，也不能伪造为可归因环境行为。error row
                    # 已在上方按更高优先级落入 tool-execution bucket。
                    if ai.succeeded is False:
                        unmapped_failures.append(
                            {
                                "step": step_num,
                                "agent": agent_name,
                                "interaction_action": ai.action,
                                "reason": (
                                    f"agent '{agent_name}' is not in metadata "
                                    "agent_names; trajectory slot unavailable"
                                ),
                                "evidence_ref": (
                                    f"agent_interactions.csv:L{ai.csv_line}"
                                ),
                            }
                        )
                    continue
                if ai.action_name in ENV_ACTION_NAMES:
                    if ai.succeeded is False:
                        category, detail = _classify_failure(ai, parse_miss)
                        taxonomy[category] = taxonomy.get(category, 0) + 1
                        failure_details.append(
                            {
                                "step": step_num,
                                "agent": agent_name,
                                "action": ai.action,
                                "category": category,
                                "detail": detail,
                                "evidence_ref": (
                                    f"agent_interactions.csv:L{ai.csv_line}"
                                ),
                            }
                        )
                elif ai.succeeded is False:
                    # 仅非 SAR + succeeded=False + 非 error → unmapped（诊断桶）。
                    unmapped_failures.append(
                        {
                            "step": step_num,
                            "agent": agent_name,
                            "interaction_action": ai.action,
                            "reason": (
                                f"action '{ai.action_name}' is not a SAR env action "
                                f"(failed non-SAR attempt)"
                            ),
                            "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
                        }
                    )

            # 3) 非 timeout 的 trajectory.Successes=False slot：仅当没有任何
            #    succeeded=False 的 observed interaction（SAR 或非 SAR，含
            #    error-observation）时写入 generic residual。失败 query 已有
            #    unmapped 证据、失败 env attempt 已进 taxonomy、error 行已进
            #    tool 桶 —— 都不得再同时创建 residual。
            if traj_success is False and not failed_seen:
                unobserved_trajectory_failures.append(
                    {
                        "step": step_num,
                        "agent": agent_name,
                        "trajectory_action": traj_action,
                        "trajectory_success": False,
                        "interaction_lines": [
                            f"agent_interactions.csv:L{ai.csv_line}" for ai in rows
                        ],
                        "reason": (
                            "trajectory Successes=False but no failed observed "
                            "interaction (SAR or non-SAR, incl. error-observation) "
                            "for this (step, agent)"
                        ),
                    }
                )

    total_failures = sum(taxonomy.values())
    results = [
        GradeResult(
            grader="ErrorTaxonomy",
            level="action",
            passed=None,
            score=None,
            detail={
                "failure_taxonomy": taxonomy,
                "total_failures": total_failures,
                "failure_details": failure_details,
                "infrastructure_timeout_failures": infrastructure_timeout_failures,
                "tool_execution_failures": tool_execution_failures,
                "unobserved_trajectory_failures": unobserved_trajectory_failures,
                "unmapped_failures": unmapped_failures,
                # 各桶计数单独列出：total_failures 只等于环境 taxonomy，
                # 禁止把 infrastructure/tool/residual 相加伪装成环境失败率。
                "failure_bucket_counts": {
                    "failure_taxonomy": total_failures,
                    "infrastructure_timeout_failures": len(
                        infrastructure_timeout_failures
                    ),
                    "tool_execution_failures": len(tool_execution_failures),
                    "unobserved_trajectory_failures": len(
                        unobserved_trajectory_failures
                    ),
                    "unmapped_failures": len(unmapped_failures),
                },
                "failure_bucket_units": {
                    "failure_taxonomy": "environment_action_attempt",
                    "infrastructure_timeout_failures": "agent_slot",
                    "tool_execution_failures": "tool_attempt",
                    "unobserved_trajectory_failures": "agent_slot",
                    "unmapped_failures": "tool_attempt",
                },
                "parse_miss_counts": parse_miss,
                "interaction_radius_reference": round(INTERACTION_RADIUS, 2),
            },
            evidence_ref="trajectory.csv:Successes, agent_interactions.csv",
        )
    ]

    return results


def _classify_failure(ai: AgentInteraction, parse_miss: dict) -> tuple[str, str]:
    target = ai.action_args[0] if ai.action_args else ""

    # 1. not_visible: target not in Names list
    if target and ai.action_name not in ("NoOp", "Explore", "Move"):
        if not ai.visible_names:
            parse_miss["names"] = parse_miss.get("names", 0) + 1
        elif target not in ai.visible_names:
            return (
                "not_visible",
                f"Target '{target}' not in visible Names list: {ai.visible_names[:5]}...",
            )

    # 2. restricted_action: carrying person + non-allowed action
    if ai.inventory is not None and ai.inventory.get("Person", 0) > 0:
        if ai.action_name not in MOVEMENT_ACTIONS | CARRY_DROP_ACTIONS | {"NoOp"}:
            return (
                "restricted_action",
                f"Agent carrying person but executed {ai.action}",
            )
    elif ai.inventory is None:
        parse_miss["inventory"] = parse_miss.get("inventory", 0) + 1

    # 3. obstacle_blocked: Move/Explore failures (direction not an object)
    if ai.action_name in ("Move", "Explore"):
        return (
            "obstacle_blocked",
            f"Movement action '{ai.action}' failed — likely blocked by obstacle or boundary",
        )

    # 4. not_interactable: target visible but likely out of range
    #    Since we lack target positions from CSV, this is a heuristic:
    #    if target is visible and not a timeout/restricted issue, attribute
    #    failure to distance. True distance check would need target coords.
    if target and ai.visible_names and target in ai.visible_names:
        return (
            "not_interactable",
            f"Target '{target}' visible in Names list but interaction likely out of range "
            f"(needs target position data for exact distance check)",
        )
    if not target or ai.action_name in ("NoOp",):
        return (
            "unknown",
            f"Action type '{ai.action_name}' with no target — cannot classify",
        )

    # 5. unknown: fallback
    return ("unknown", f"Action '{ai.action}' failed — no matching rule")


ERROR_TAXONOMY_NAME = "ErrorTaxonomy"
