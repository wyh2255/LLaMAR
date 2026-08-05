from __future__ import annotations

from typing import TYPE_CHECKING

from sar_orch.eval.graders.base import GradeResult

if TYPE_CHECKING:
    from sar_orch.eval.dataset import EpisodeDataset

MOVEMENT_ACTIONS = {"Explore", "NavigateTo", "Move"}


def grade_trajectory(episode: EpisodeDataset) -> list[GradeResult]:
    results: list[GradeResult] = []

    rescue_result = _check_rescue_flow(episode)
    results.append(rescue_result)

    fire_result = _check_fire_flow(episode)
    results.append(fire_result)

    coop_result = _check_coop_carry(episode)
    results.append(coop_result)

    exploration_result = _check_exploration_efficiency(episode)
    results.append(exploration_result)

    return results


def _check_rescue_flow(episode: EpisodeDataset) -> GradeResult:
    carried_persons: set[str] = set()
    deposit_nav_agents: set[str] = set()
    dropoff_succeeded = False

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for agent_idx, act in enumerate(sr.actions):
            agent_name = (
                episode.agent_names[agent_idx]
                if agent_idx < len(episode.agent_names)
                else ""
            )
            act_name, act_args = _parse_action_str(act)

            if act_name == "Carry" and sr.successes[agent_idx]:
                person_target = act_args[0] if act_args else "unknown"
                carried_persons.add(f"{person_target}:{agent_name}")

            if act_name == "NavigateTo" and sr.successes[agent_idx]:
                target = act_args[0] if act_args else ""
                if "deposit" in target.lower() or "Deposit" in target:
                    deposit_nav_agents.add(agent_name)

            if act_name == "DropOff" and sr.successes[agent_idx]:
                dropoff_succeeded = True

    evidence = {
        "carry_events": sorted(carried_persons),
        "agents_navigated_to_deposit": sorted(deposit_nav_agents),
        "dropoff_succeeded": dropoff_succeeded,
        "rescue_flow": ("Carry → ≥2 NavigateTo(deposit) → DropOff"),
    }

    if not carried_persons:
        passed = True
        evidence["note"] = "No carry events observed (no rescue needed)"
    elif dropoff_succeeded and len(deposit_nav_agents) >= 2:
        passed = True
        evidence["note"] = "Rescue flow complete"
    elif dropoff_succeeded:
        passed = True
        evidence["note"] = (
            "DropOff succeeded but fewer than 2 agents NavigateTo(deposit)"
        )
    else:
        passed = False
        evidence["note"] = (
            f"Carry occurred ({len(carried_persons)} events) but DropOff did not succeed "
            f"or insufficient NavigateTo(deposit) ({len(deposit_nav_agents)} agents)"
        )

    return GradeResult(
        grader="TrajectoryGrader",
        level="step",
        passed=passed,
        score=1.0 if passed else 0.0,
        detail={"check": "rescue_flow", **evidence},
        evidence_ref="trajectory.csv:Actions,Successes",
    )


def _check_fire_flow(episode: EpisodeDataset) -> GradeResult:
    get_supply_records: set[tuple[str, str]] = set()
    use_supply_illegal: list[dict] = []

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for agent_idx, act in enumerate(sr.actions):
            act_name, act_args = _parse_action_str(act)
            agent_name = (
                episode.agent_names[agent_idx]
                if agent_idx < len(episode.agent_names)
                else ""
            )

            if act_name == "GetSupply" and sr.successes[agent_idx]:
                source = act_args[0] if len(act_args) > 0 else ""
                ai = episode.get_interaction(step_num, agent_name)
                if ai and len(ai.action_args) > 1:
                    stype = ai.action_args[1]
                else:
                    stype = ""
                if stype:
                    get_supply_records.add((source, stype))

            if act_name == "UseSupply":
                fire_target = act_args[0] if len(act_args) > 0 else ""
                ai = episode.get_interaction(step_num, agent_name)
                if ai and len(ai.action_args) > 1:
                    stype = ai.action_args[1]
                else:
                    stype = act_args[1] if len(act_args) > 1 else ""
                if stype:
                    has_matching_get = any(
                        src_type[1] == stype for src_type in get_supply_records
                    )
                else:
                    has_matching_get = bool(get_supply_records)

                if not has_matching_get:
                    use_supply_illegal.append(
                        {
                            "step": step_num,
                            "agent": agent_name,
                            "action": act,
                            "detail": (
                                f"UseSupply({fire_target}, {stype}) "
                                f"without prior successful GetSupply of {stype}"
                            ),
                        }
                    )

    evidence = {
        "get_supply_records": sorted(f"{s}:{t}" for s, t in get_supply_records),
        "illegal_use_supply_count": len(use_supply_illegal),
        "illegal_use_supply": use_supply_illegal,
    }

    passed = len(use_supply_illegal) == 0
    return GradeResult(
        grader="TrajectoryGrader",
        level="step",
        passed=passed,
        score=1.0 if passed else 0.0,
        detail={"check": "fire_flow", **evidence},
        evidence_ref="trajectory.csv:Actions,Successes",
    )


def _check_coop_carry(episode: EpisodeDataset) -> GradeResult:
    sync_carries: list[dict] = []
    missing_carry: list[dict] = []

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        carry_agents: dict[str, list[str]] = {}

        for agent_idx, act in enumerate(sr.actions):
            act_name, act_args = _parse_action_str(act)
            agent_name = (
                episode.agent_names[agent_idx]
                if agent_idx < len(episode.agent_names)
                else ""
            )
            if act_name == "Carry" and sr.successes[agent_idx]:
                person = act_args[0] if act_args else "unknown"
                carry_agents.setdefault(person, []).append(agent_name)

        for person, agents in carry_agents.items():
            if len(agents) >= 2:
                sync_carries.append(
                    {
                        "step": step_num,
                        "person": person,
                        "agents": agents,
                    }
                )
            else:
                missing_carry.append(
                    {
                        "step": step_num,
                        "person": person,
                        "agents": agents,
                        "note": f"Only {len(agents)} agent(s) carrying {person}, need ≥2",
                    }
                )

    evidence = {
        "sync_carries": sync_carries,
        "missing_sync_carries": missing_carry,
    }

    passed = len(missing_carry) == 0
    return GradeResult(
        grader="TrajectoryGrader",
        level="step",
        passed=passed if sync_carries or missing_carry else None,
        score=1.0 if passed else 0.0,
        detail={"check": "coop_carry", **evidence},
        evidence_ref="trajectory.csv:Actions,Successes",
    )


def _check_exploration_efficiency(episode: EpisodeDataset) -> GradeResult:
    visited_targets: dict[str, set[str]] = {}
    total_nav_to = 0
    repeat_nav_to = 0

    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for agent_idx, act in enumerate(sr.actions):
            act_name, act_args = _parse_action_str(act)
            if act_name == "NavigateTo" and act_args:
                target = act_args[0]
                agent_name = (
                    episode.agent_names[agent_idx]
                    if agent_idx < len(episode.agent_names)
                    else ""
                )
                total_nav_to += 1
                visited_targets.setdefault(target, set())
                if agent_name in visited_targets[target]:
                    repeat_nav_to += 1
                else:
                    visited_targets[target].add(agent_name)

    repeat_ratio = repeat_nav_to / total_nav_to if total_nav_to > 0 else 0.0

    return GradeResult(
        grader="TrajectoryGrader",
        level="step",
        passed=None,
        score=1.0 - repeat_ratio,
        detail={
            "check": "exploration_efficiency",
            "total_navigate_to": total_nav_to,
            "repeat_navigate_to": repeat_nav_to,
            "repeat_ratio": round(repeat_ratio, 4),
            "note": (
                f"{repeat_nav_to}/{total_nav_to} NavigateTo calls were repeats "
                f"to already-visited targets"
            ),
        },
        evidence_ref="trajectory.csv:Actions",
    )


def _parse_action_str(action_str: str) -> tuple[str, list[str]]:
    import re

    m = re.match(r"^(\w+)\(([^)]*)\)$", action_str.strip())
    if not m:
        return action_str.strip(), []
    name = m.group(1)
    args_str = m.group(2)
    args = [a.strip() for a in args_str.split(",") if a.strip()]
    return name, args


TRAJECTORY_GRADER_NAME = "TrajectoryGrader"
