from __future__ import annotations

from typing import TYPE_CHECKING

from sar_orch.eval.graders.base import GradeResult

if TYPE_CHECKING:
    from sar_orch.eval.dataset import AgentInteraction, EpisodeDataset

SAR_ACTION_NAMES = {
    "Explore",
    "NavigateTo",
    "Move",
    "GetSupply",
    "UseSupply",
    "Carry",
    "DropOff",
    "StoreSupply",
    "ClearInventory",
    "NoOp",
}

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
    violations: list[dict] = []
    repeat_counters: dict[str, list[int]] = {}
    noop_counts: dict[str, int] = {}
    sar_action_counts: dict[str, int] = {}
    timeout_agents_set: dict[int, set[str]] = {}
    parse_miss_counts = {"names": 0, "inventory": 0, "position": 0}

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        timeout_agents = {
            episode.agent_names[i]
            for i in sr.timeout_agents
            if i < len(episode.agent_names)
        }
        timeout_agents_set[step_num] = timeout_agents

        for agent_name in episode.agent_names:
            ai = episode.get_interaction(step_num, agent_name)
            if ai is None:
                continue
            if ai.action_name not in SAR_ACTION_NAMES:
                continue
            if agent_name in timeout_agents:
                continue

            agent_idx = episode.agent_names.index(agent_name)
            traj_success = episode.get_agent_success(step_num, agent_idx)

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

            if traj_success is not None and not traj_success:
                key = f"{agent_name}:{ai.action}"
                if key not in repeat_counters:
                    repeat_counters[key] = []
                repeat_counters[key].append(step_num)

    _check_repeat_failures(repeat_counters, violations, episode)

    _check_excessive_noop(
        noop_counts, sar_action_counts, violations, timeout_agents_set, episode
    )

    results = []
    if violations:
        results.append(
            GradeResult(
                grader="ConstraintGrader",
                level="action",
                passed=False,
                score=max(0.0, 1.0 - len(violations) * 0.05),
                detail={
                    "violation_count": len(violations),
                    "violations": violations,
                    "parse_miss_counts": parse_miss_counts,
                },
                evidence_ref="agent_interactions.csv",
            )
        )
    else:
        results.append(
            GradeResult(
                grader="ConstraintGrader",
                level="action",
                passed=True,
                score=1.0,
                detail={
                    "violation_count": 0,
                    "violations": [],
                    "parse_miss_counts": parse_miss_counts,
                },
                evidence_ref="agent_interactions.csv",
            )
        )

    return results


def _check_empty_supply(ai: "AgentInteraction", violations: list, parse_miss: dict):
    if ai.inventory is None:
        parse_miss["inventory"] = parse_miss.get("inventory", 0) + 1
        return
    supply_type = ai.action_args[1] if len(ai.action_args) > 1 else ""
    amt = _inventory_resource_amount(ai.inventory, supply_type)
    if amt <= 0:
        violations.append(
            {
                "rule": "empty_supply_use",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "high",
                "detail": f"Used supply type '{supply_type}' but inventory has 0 (inv={ai.inventory})",
                "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
            }
        )


def _check_hallucinated_nav(ai: "AgentInteraction", violations: list, parse_miss: dict):
    if not ai.visible_names:
        parse_miss["names"] = parse_miss.get("names", 0) + 1
        return
    target = ai.action_args[0] if ai.action_args else ""
    if target and target not in ai.visible_names:
        violations.append(
            {
                "rule": "hallucinated_nav_target",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "high",
                "detail": f"NavigateTo('{target}') but target not in visible Names list: {ai.visible_names}",
                "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
            }
        )


def _check_full_inventory_get(
    ai: "AgentInteraction", violations: list, parse_miss: dict
):
    if ai.inventory is None:
        parse_miss["inventory"] = parse_miss.get("inventory", 0) + 1
        return
    capacity = 3
    occupied = sum(ai.inventory.values())
    if occupied >= capacity:
        violations.append(
            {
                "rule": "full_inventory_get",
                "step": ai.step,
                "agent": ai.agent,
                "action": ai.action,
                "severity": "low",
                "detail": f"GetSupply with full inventory (occupied={occupied}/{capacity}, inv={ai.inventory})",
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
