from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.outcome import grade_outcome
from sar_orch.eval.graders.state import grade_state
from sar_orch.eval.graders.constraint import grade_constraint
from sar_orch.eval.graders.error_taxonomy import grade_error_taxonomy
from sar_orch.eval.graders.trajectory import grade_trajectory

if TYPE_CHECKING:
    pass

GRADER_REGISTRY: dict[str, Any] = {
    "OutcomeGrader": grade_outcome,
    "StateGrader": grade_state,
    "ConstraintGrader": grade_constraint,
    "ErrorTaxonomy": grade_error_taxonomy,
    "TrajectoryGrader": grade_trajectory,
}

GRADER_NAMES = sorted(GRADER_REGISTRY.keys())

_episode: EpisodeDataset | None = None
_workspace_dir: Path | None = None


def _ensure_workspace() -> Path:
    if _workspace_dir is None:
        raise RuntimeError(
            "eval_workspace not initialized: call init_agent_env() first"
        )
    return _workspace_dir


@tool
def run_grader(name: str) -> str:
    """Run a single deterministic grader and save the result to workspace.

    Args:
        name: One of: OutcomeGrader, StateGrader, ConstraintGrader, ErrorTaxonomy, TrajectoryGrader

    Returns:
        Summary of the grader result.
    """
    ep = _get_episode()
    grader_fn = GRADER_REGISTRY.get(name)
    if grader_fn is None:
        return f"Error: unknown grader '{name}'. Available: {', '.join(GRADER_NAMES)}"

    results = grader_fn(ep)
    workspace = _ensure_workspace()
    grader_dir = workspace / "grader_results"
    grader_dir.mkdir(parents=True, exist_ok=True)

    out_path = grader_dir / f"{name}.json"
    out_data = [r.__dict__ for r in results]
    out_path.write_text(
        json.dumps(out_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    summaries = []
    for r in results:
        status = (
            "pass" if r.passed is True else ("fail" if r.passed is False else "N/A")
        )
        summaries.append(f"  [{status}] {r.grader} (level={r.level}, score={r.score})")
    return f"Ran {name} → {len(results)} result(s) saved to {out_path}\n" + "\n".join(
        summaries
    )


@tool
def run_all_graders() -> str:
    """Run all 5 deterministic graders and save results to workspace/grader_results/.

    Returns:
        Summary of all grader results.
    """
    lines = []
    for name in GRADER_NAMES:
        line = run_grader.invoke({"name": name})
        lines.append(line)
    return "\n---\n".join(lines)


@tool
def get_step_evidence(step: int) -> str:
    """Get trajectory row + agent interactions for a specific step.

    Args:
        step: The environment step number (1-based, matching trajectory.csv)

    Returns:
        Formatted summary of actions, successes, dispatches, and interactions.
    """
    ep = _get_episode()
    sr = ep.get_step(step)
    if sr is None:
        return (
            f"Error: step {step} not found. Available steps: {sorted(ep.steps.keys())}"
        )

    lines = [
        f"Step {step}:",
        f"  Actions: {sr.actions}",
        f"  Successes: {sr.successes}",
        f"  Coverage: {sr.coverage:.3f}, TransportRate: {sr.transport_rate:.3f}",
        f"  TimeoutAgents: {sr.timeout_agents}",
        f"  Finished: {sr.finished}, EndReason: {sr.end_reason}",
        f"  SubtasksDelta: {sr.completed_subtasks_delta}",
    ]

    if sr.dispatches:
        lines.append("  Dispatches:")
        for d in sr.dispatches:
            lines.append(f"    - {d.assigned_to}: {d.subtask}")

    if sr.interactions:
        lines.append("  Agent Interactions:")
        for ai in sr.interactions:
            success = (
                "OK"
                if step < len(ep.steps)
                and ep.get_agent_success(step, ep.agent_names.index(ai.agent))
                else "?"
            )
            lines.append(
                f"    {ai.agent} [{success}]: {ai.action_name}({', '.join(ai.action_args)})"
            )
            obs_preview = (
                ai.observation[:150].replace("\n", " ") if ai.observation else ""
            )
            lines.append(f"      Observation preview: {obs_preview}...")

    return "\n".join(lines)


@tool
def get_agent_trace(agent: str) -> str:
    """Get the full episode action trace for a specific agent.

    Args:
        agent: Agent name (e.g. Alice, Bob, Charlie, David)

    Returns:
        Step-by-step action summary for the agent.
    """
    ep = _get_episode()
    agent_names_lower = [a.lower() for a in ep.agent_names]
    if agent.lower() not in agent_names_lower:
        return f"Error: agent '{agent}' not found. Available: {ep.agent_names}"

    actions = []
    for step_num in sorted(ep.steps.keys()):
        sr = ep.steps[step_num]
        ai = ep.get_interaction(step_num, agent)
        agent_idx = ep.agent_names.index(agent)
        success = ep.get_agent_success(step_num, agent_idx)
        if ai:
            act_str = f"{ai.action_name}({', '.join(ai.action_args)})"
        else:
            act_str = sr.actions[agent_idx] if agent_idx < len(sr.actions) else "?"
        status = "✓" if success else ("✗" if success is False else "?")
        actions.append(f"  Step {step_num}: [{status}] {act_str}")

    return f"Agent: {agent}\n" + "\n".join(actions)


@tool
def get_dispatch_context(step: int) -> str:
    """Get dispatch context for a step — dispatches, team status, and map summary.

    This is the input for the DispatchJudge subagent.

    Args:
        step: The environment step number (1-based)

    Returns:
        Formatted dispatch context with map summary.
    """
    ep = _get_episode()
    sr = ep.get_step(step)
    if sr is None:
        return f"Error: step {step} not found"

    lines = [
        f"=== Dispatch Context for Step {step} ===",
        f"Coverage: {sr.coverage:.3f}, TransportRate: {sr.transport_rate:.3f}",
        f"Finished: {sr.finished}, Remaining steps: ?",
    ]

    lines.append("\nDispatches:")
    for d in sr.dispatches:
        lines.append(f"  {d.assigned_to}: {d.subtask}")

    lines.append("\nTeam Status (per agent):")
    for agent_name in ep.agent_names:
        ai = ep.get_interaction(step, agent_name)
        if ai:
            pos_str = f"pos={ai.position}" if ai.position else "pos=?"
            inv_str = f"inv={ai.inventory}" if ai.inventory else "inv=?"
            lines.append(f"  {agent_name}: {pos_str}, {inv_str}")
        else:
            lines.append(f"  {agent_name}: no interaction record")

    lines.append("\nMap Summary:")
    map_summary_text = _get_nearest_map_summary(ep, step)
    if map_summary_text:
        lines.append(f"  {map_summary_text}")
    else:
        lines.append("  (no map summary available for this step)")

    return "\n".join(lines)


@tool
def save_judge_verdict(judge_name: str, verdict_json: str) -> str:
    """Save a judge subagent verdict to the real filesystem workspace.

    Args:
        judge_name: Simple identifier like 'dispatch_step_5' or 'observation_Alice_8'
        verdict_json: JSON string of the verdict result

    Returns:
        Path where the verdict was saved, or error message if schema validation fails.
    """
    workspace = _ensure_workspace()
    judge_dir = workspace / "judge_results"
    judge_dir.mkdir(parents=True, exist_ok=True)
    clean_name = judge_name.replace("/", "_").replace("\\", "_")
    while "eval_workspace_judge_results_" in clean_name:
        clean_name = clean_name.replace("eval_workspace_judge_results_", "")
    out_path = judge_dir / f"{clean_name}.json"
    try:
        data = json.loads(verdict_json)
    except json.JSONDecodeError as e:
        out_path.write_text(verdict_json, encoding="utf-8")
        return f"Saved (raw text, JSON parse error: {e}) to {out_path}"

    # Schema validation for observation verdicts
    if "observ" in judge_name.lower():
        if "claims" not in data:
            return (
                "ERROR: observation verdict must contain a 'claims' key with a list of per-claim objects. "
                "Each claim must have: agent (str), step (int), claim (str), supported (bool), evidence (str). "
                'Example: {"claims": [{"agent": "Alice", "step": 1, "claim": "...", "supported": true, "evidence": "..."}], "summary": "..."}'
            )
        if not isinstance(data["claims"], list):
            return "ERROR: 'claims' must be a list. Got: " + str(type(data["claims"]))
        for i, claim in enumerate(data["claims"]):
            if not isinstance(claim, dict):
                return f"ERROR: claims[{i}] is not a dict"
            for key in ("claim", "supported", "evidence"):
                if key not in claim:
                    return f"ERROR: claims[{i}] missing required key '{key}'"
            if "agent" not in claim:
                return f"ERROR: claims[{i}] missing required key 'agent'"
            if "step" not in claim:
                return f"ERROR: claims[{i}] missing required key 'step'"
            if not isinstance(claim.get("supported"), bool):
                return f"ERROR: claims[{i}]['supported'] must be a boolean"

    # Schema validation for dispatch verdicts
    if "dispatch" in judge_name.lower():
        if "verdicts" not in data:
            return (
                "ERROR: dispatch verdict must contain a 'verdicts' key with a list of per-step objects. "
                "Each entry must have: step (int), full_coverage (str), role_match (str), "
                "map_awareness (str), step_budget_awareness (str), notes (str). "
                'Dimension values: "pass" | "fail" | "Unknown". '
                'Example: {"verdicts": [{"step": 1, "full_coverage": "pass", "role_match": "fail", '
                '"map_awareness": "pass", "step_budget_awareness": "Unknown", "notes": "..."}], '
                '"summary": "..."}'
            )
        if not isinstance(data["verdicts"], list):
            return "ERROR: 'verdicts' must be a list. Got: " + str(
                type(data["verdicts"])
            )
        dim_keys = (
            "full_coverage",
            "role_match",
            "map_awareness",
            "step_budget_awareness",
        )
        valid_vals = {"pass", "fail", "unknown"}
        for i, entry in enumerate(data["verdicts"]):
            if not isinstance(entry, dict):
                return f"ERROR: verdicts[{i}] is not a dict"
            if "step" not in entry:
                return f"ERROR: verdicts[{i}] missing required key 'step'"
            try:
                int(entry["step"])
            except (ValueError, TypeError):
                return f"ERROR: verdicts[{i}]['step'] must be an int or numeric string. Got: {entry['step']!r}"
            for dk in dim_keys:
                if dk not in entry:
                    return f"ERROR: verdicts[{i}] missing required key '{dk}'"
                val = entry[dk]
                if not isinstance(val, str) or val.lower() not in valid_vals:
                    return (
                        f"ERROR: verdicts[{i}]['{dk}'] must be 'pass', 'fail', or 'Unknown'. "
                        f"Got: {val!r}"
                    )
                low = val.lower()
                if low == "pass":
                    entry[dk] = "pass"
                elif low == "fail":
                    entry[dk] = "fail"
                else:
                    entry[dk] = "Unknown"

    # Force canonical filenames
    if "dispatch" in judge_name.lower():
        clean_name = "dispatch_full"
    elif "observ" in judge_name.lower():
        clean_name = "observation_full"
    out_path = judge_dir / f"{clean_name}.json"

    out_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return f"Saved to {out_path}"


@tool
def save_conclusion(text: str) -> str:
    """Write the final conclusion markdown to the real filesystem workspace.

    Args:
        text: Conclusion markdown content

    Returns:
        Path where conclusion was saved.
    """
    workspace = _ensure_workspace()
    out_path = workspace / "conclusion.md"
    out_path.write_text(text, encoding="utf-8")
    return f"Saved to {out_path}"


@tool
def get_observation_claims(agent: str, step: int) -> str:
    """Get observation claims context for an agent at a step.

    This is the input for the ObservationJudge subagent.
    Shows ALL interactions for the agent at this step, including every
    report_observation call with full claim details.

    Args:
        agent: Agent name
        step: The environment step number (1-based)

    Returns:
        Formatted observation claims vs ground truth.
    """
    ep = _get_episode()
    sr = ep.get_step(step)
    if sr is None:
        return f"Error: step {step} not found"

    agent_actions = [ai for ai in sr.interactions if ai.agent == agent]
    if not agent_actions:
        return f"Error: no interactions found for agent '{agent}' at step {step}"

    lines = [
        f"=== Observation Claims for {agent} at Step {step} ===",
        f"Total tool calls this step: {len(agent_actions)}",
    ]

    for idx, ai in enumerate(agent_actions):
        lines.append(f"\n--- Call {idx + 1}: {ai.tool_name}({ai.tool_args}) ---")
        lines.append(f"  Action: {ai.action}")
        lines.append(f"  Position: {ai.position}")
        lines.append(f"  Inventory: {ai.inventory}")
        lines.append(f"  Visible Names: {ai.visible_names}")

        if ai.tool_name == "report_observation":
            lines.append(f"  >> REPORT_OBSERVATION CLAIM: {ai.tool_args}")
            lines.append(
                f"  >> LLM Output: {ai.llm_output[:300] if ai.llm_output else '(empty)'}"
            )
        else:
            lines.append(
                f"  LLM Output: {ai.llm_output[:200] if ai.llm_output else '(empty)'}"
            )

    lines.append(
        "\n--- Environment Observation (ground truth, from first interaction) ---"
    )
    obs_text = agent_actions[0].observation if agent_actions else ""
    lines.append(obs_text if obs_text else "(empty)")

    return "\n".join(lines)


def _get_episode() -> EpisodeDataset:
    if _episode is None:
        raise RuntimeError(
            "eval_workspace not initialized: call init_agent_env() first"
        )
    return _episode


def _get_nearest_map_summary(ep: EpisodeDataset, step: int) -> str:
    summaries = ep.map_summaries
    if not summaries:
        return ""
    valid = [
        s
        for s in summaries
        if s.get("status") == "success" and s.get("env_step", 0) <= step
    ]
    if not valid:
        return ""
    nearest = max(valid, key=lambda s: s["env_step"])
    return nearest.get("summary", "")


def init_agent_env(episode: EpisodeDataset, workspace_dir: Path) -> None:
    global _episode, _workspace_dir
    _episode = episode
    _workspace_dir = workspace_dir


def materialize_workspace(episode: EpisodeDataset, workspace_dir: Path) -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)

    (workspace_dir / "grader_results").mkdir(exist_ok=True)

    steps_overview = []
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        steps_overview.append(
            {
                "step": step_num,
                "actions": sr.actions,
                "successes": sr.successes,
                "timeout_agents": sr.timeout_agents,
                "coverage": sr.coverage,
                "transport_rate": sr.transport_rate,
                "finished": sr.finished,
                "completed_subtasks_delta": sr.completed_subtasks_delta,
            }
        )
    (workspace_dir / "steps_overview.json").write_text(
        json.dumps(steps_overview, indent=2, default=str), encoding="utf-8"
    )

    failures = [s for s in steps_overview if not all(s["successes"])]
    (workspace_dir / "failure_steps.json").write_text(
        json.dumps(failures, indent=2, default=str), encoding="utf-8"
    )

    (workspace_dir / "metadata.json").write_text(
        json.dumps(
            {
                "scene": episode.metadata.get("scene"),
                "agents": episode.metadata.get("agent_count"),
                "seed": episode.metadata.get("seed"),
                "model": episode.metadata.get("model"),
                "state_mode": episode.metadata.get("state_mode"),
                "run_id": episode.metadata.get("run_id"),
                "agent_names": episode.agent_names,
                "total_steps": max(episode.steps.keys()) if episode.steps else 0,
                "total_tokens": sum(
                    int(r.get("TotalTokens", 0)) for r in episode.token_usage_rows
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (workspace_dir / "conclusion.md").write_text(
        "# Conclusion\n\n*(to be written by Eval Agent)*\n", encoding="utf-8"
    )

    print(f"  Workspace materialized at {workspace_dir}")
    print(f"    - steps_overview.json ({len(steps_overview)} steps)")
    print(f"    - failure_steps.json ({len(failures)} failure steps)")
    print("    - grader_results/ (will be populated by agent)")
    print("    - conclusion.md (will be written by agent)")
