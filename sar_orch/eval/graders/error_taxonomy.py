from __future__ import annotations

from typing import TYPE_CHECKING

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


def grade_error_taxonomy(episode: EpisodeDataset) -> list[GradeResult]:
    taxonomy: dict[str, int] = {}
    failure_details: list[dict] = []
    unmapped_failures: list[dict] = []
    parse_miss = {"names": 0, "inventory": 0, "position": 0}

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        timeout_indices = set(sr.timeout_agents)

        for agent_idx, agent_name in enumerate(episode.agent_names):
            traj_success = episode.get_agent_success(step_num, agent_idx)
            if traj_success is not False:
                continue

            ai = episode.get_interaction(step_num, agent_name)
            if ai is None or ai.action_name not in SAR_ACTION_NAMES:
                unmapped_failures.append(
                    {
                        "step": step_num,
                        "agent": agent_name,
                        "interaction_action": ai.action if ai else None,
                        "reason": "no interaction row"
                        if ai is None
                        else f"action '{ai.action_name}' not a SAR env action",
                    }
                )
                continue

            category, detail = _classify_failure(
                ai, agent_idx, timeout_indices, parse_miss
            )
            taxonomy[category] = taxonomy.get(category, 0) + 1
            failure_details.append(
                {
                    "step": step_num,
                    "agent": agent_name,
                    "action": ai.action,
                    "category": category,
                    "detail": detail,
                    "evidence_ref": f"agent_interactions.csv:L{ai.csv_line}",
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
                "unmapped_failures": unmapped_failures,
                "parse_miss_counts": parse_miss,
                "interaction_radius_reference": round(INTERACTION_RADIUS, 2),
            },
            evidence_ref="trajectory.csv:Successes, agent_interactions.csv",
        )
    ]

    return results


def _classify_failure(
    ai: AgentInteraction,
    agent_idx: int,
    timeout_indices: set[int],
    parse_miss: dict,
) -> tuple[str, str]:
    # 1. infrastructure: agent in timeout list
    if agent_idx in timeout_indices:
        return (
            "infrastructure",
            "Agent timed out — action auto-filled as NoOp/failure",
        )

    target = ai.action_args[0] if ai.action_args else ""

    # 2. not_visible: target not in Names list
    if target and ai.action_name not in ("NoOp", "Explore", "Move"):
        if not ai.visible_names:
            parse_miss["names"] = parse_miss.get("names", 0) + 1
        elif target not in ai.visible_names:
            return (
                "not_visible",
                f"Target '{target}' not in visible Names list: {ai.visible_names[:5]}...",
            )

    # 3. restricted_action: carrying person + non-allowed action
    if ai.inventory is not None and ai.inventory.get("Person", 0) > 0:
        if ai.action_name not in MOVEMENT_ACTIONS | CARRY_DROP_ACTIONS | {"NoOp"}:
            return (
                "restricted_action",
                f"Agent carrying person but executed {ai.action}",
            )
    elif ai.inventory is None:
        parse_miss["inventory"] = parse_miss.get("inventory", 0) + 1

    # 4. obstacle_blocked: Move/Explore failures (direction not an object)
    if ai.action_name in ("Move", "Explore"):
        return (
            "obstacle_blocked",
            f"Movement action '{ai.action}' failed — likely blocked by obstacle or boundary",
        )

    # 5. not_interactable: target visible but likely out of range
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

    # 6. unknown: fallback
    return ("unknown", f"Action '{ai.action}' failed — no matching rule")


ERROR_TAXONOMY_NAME = "ErrorTaxonomy"
