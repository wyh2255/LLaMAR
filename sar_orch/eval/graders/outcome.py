from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.base import GradeResult


def _parse_csv_list(value: str):
    if not value or value == "[]":
        return []
    try:
        return eval(value)
    except Exception:
        return []


def _compute_balance(episode: EpisodeDataset) -> float:
    agent_success_counts: dict[int, int] = {}
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for i, (act, suc) in enumerate(zip(sr.actions, sr.successes)):
            if act in ("NoOp()", "Idle", "Done"):
                continue
            if suc:
                agent_success_counts[i] = agent_success_counts.get(i, 0) + 1

    if not agent_success_counts:
        return 0.0
    vals = list(agent_success_counts.values())
    return min(vals) / max(vals) if max(vals) > 0 else 0.0


def grade_outcome(episode: EpisodeDataset) -> list[GradeResult]:
    results: list[GradeResult] = []

    last = episode.last_step
    if last is None:
        return results

    final_coverage = last.coverage
    final_transport_rate = last.transport_rate
    finished = last.finished
    total_steps = max(episode.steps.keys()) if episode.steps else 0
    end_reason = last.end_reason

    # subtask count
    completed_count = sum(1 for s in episode.subtask_records if s.status == "completed")
    total_subtasks = len(episode.subtask_records)

    # token aggregation
    token_usage_rows = episode.token_usage_rows
    total_tokens = 0
    agent_tokens: dict[str, int] = {}
    map_tokens = 0
    map_agent_tokens = 0
    map_summarizer_tokens = 0
    for row in token_usage_rows:
        agent = row.get("Agent", "")
        tt = int(row.get("TotalTokens", 0))
        total_tokens += tt
        agent_tokens[agent] = agent_tokens.get(agent, 0) + tt
    map_agent_tokens = agent_tokens.get("MapAgent", 0)
    map_summarizer_tokens = agent_tokens.get("MapSummarizer", 0)
    map_tokens = map_agent_tokens + map_summarizer_tokens

    # completed from trajectory CompletedSubtasksDelta (the actual checker completions)
    trajectory_completed = sum(
        len(sr.completed_subtasks_delta) for sr in episode.steps.values()
    )

    # step efficiency = subtask count / total steps
    step_efficiency = total_subtasks / total_steps if total_steps > 0 else 0.0

    # token efficiency = total tokens / trajectory-observed completions
    token_efficiency = (
        total_tokens / trajectory_completed if trajectory_completed > 0 else 0.0
    )

    # map overhead ratio
    map_overhead = map_tokens / total_tokens if total_tokens > 0 else 0.0

    # balance
    balance = _compute_balance(episode)

    # progress curve data from CompletedSubtasksDelta
    progress_curve = []
    cumulative = 0
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        delta = sr.completed_subtasks_delta
        cumulative += len(delta)
        progress_curve.append(
            {"step": step_num, "delta": len(delta), "cumulative": cumulative}
        )

    episode_result = GradeResult(
        grader="OutcomeGrader",
        level="episode",
        passed=None,
        score=None,
        detail={
            "final_coverage": final_coverage,
            "final_transport_rate": final_transport_rate,
            "finished": finished,
            "total_steps": total_steps,
            "end_reason": end_reason,
            "completed_subtasks_csv": completed_count,
            "completed_subtasks_trajectory": trajectory_completed,
            "total_subtasks": total_subtasks,
            "step_efficiency": step_efficiency,
            "total_tokens": total_tokens,
            "agent_tokens": agent_tokens,
            "map_tokens": map_tokens,
            "map_agent_tokens": map_agent_tokens,
            "map_summarizer_tokens": map_summarizer_tokens,
            "map_overhead_ratio": map_overhead,
            "token_efficiency": token_efficiency,
            "balance": balance,
            "progress_curve": progress_curve,
        },
        evidence_ref="trajectory.csv,summary.csv,token_usage.csv,subtasks.csv",
    )
    results.append(episode_result)

    return results


OUTCOME_GRADER_NAME = "OutcomeGrader"
