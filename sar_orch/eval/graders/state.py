import json
from pathlib import Path

from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.base import GradeResult


def grade_state(episode: EpisodeDataset) -> list[GradeResult]:
    results: list[GradeResult] = []

    # 1. Rebuild completed subtasks from trajectory vs Finished
    completed_from_traj = _count_completed_from_trajectory(episode)
    finished_flag = False
    last = episode.last_step
    if last is not None:
        finished_flag = last.finished

    completed_from_subtasks = sum(
        1 for s in episode.subtask_records if s.status == "completed"
    )

    subtask_validation = GradeResult(
        grader="StateGrader",
        level="episode",
        passed=None,
        detail={
            "completed_subtasks_from_trajectory": completed_from_traj,
            "completed_subtasks_from_subtasks_csv": completed_from_subtasks,
            "finished": finished_flag,
            "reconciliation": (
                "consistent"
                if not finished_flag or completed_from_traj > 0
                else "inconsistent"
            ),
            "note": (
                f"轨迹累计完成 {completed_from_traj} 子任务, "
                f"subtasks.csv 记录 {completed_from_subtasks} 个 completed 状态, "
                f"Finished={finished_flag}"
            ),
        },
        evidence_ref="trajectory.csv:CompletedSubtasksDelta, subtasks.csv:Status",
    )
    results.append(subtask_validation)

    # 2. Cross-validate run_metrics.json vs summary.csv
    run_metrics_path = episode.run_dir / "run_metrics.json"
    metrics_validation = _cross_validate_metrics(episode, run_metrics_path)
    results.append(metrics_validation)

    # 3. Detect metric regressions
    regression_result = _detect_regressions(episode)
    results.append(regression_result)

    return results


def _count_completed_from_trajectory(episode: EpisodeDataset) -> int:
    total = 0
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        total += len(sr.completed_subtasks_delta)
    return total


def _cross_validate_metrics(
    episode: EpisodeDataset, run_metrics_path: Path
) -> GradeResult:
    issues = []
    if not run_metrics_path.exists():
        metrics_validation = GradeResult(
            grader="StateGrader",
            level="episode",
            passed=None,
            detail={
                "validation": "run_metrics.json missing — skip cross-validation",
                "coverage_match": None,
                "transport_rate_match": None,
                "finished_match": None,
                "steps_match": None,
            },
            evidence_ref="run_metrics.json",
        )
        return metrics_validation

    with run_metrics_path.open(encoding="utf-8") as f:
        run_metrics = json.load(f)

    last = episode.last_step
    if last is None:
        return GradeResult(
            grader="StateGrader",
            level="episode",
            passed=None,
            detail={"validation": "no trajectory steps — skip"},
            evidence_ref="trajectory.csv",
        )

    coverage_match = abs(run_metrics.get("coverage", -1) - last.coverage) < 0.001
    transport_match = (
        abs(run_metrics.get("transport_rate", -1) - last.transport_rate) < 0.001
    )
    finished_match = run_metrics.get("finished", None) == last.finished
    steps_match = run_metrics.get("steps", -1) == max(episode.steps.keys())
    end_reason_match = run_metrics.get("end_reason", "") == last.end_reason

    if not coverage_match:
        issues.append(
            f"coverage mismatch: run_metrics={run_metrics.get('coverage')}, trajectory={last.coverage}"
        )
    if not transport_match:
        issues.append(
            f"transport_rate mismatch: run_metrics={run_metrics.get('transport_rate')}, trajectory={last.transport_rate}"
        )
    if not finished_match:
        issues.append(
            f"finished mismatch: run_metrics={run_metrics.get('finished')}, trajectory={last.finished}"
        )
    if not steps_match:
        issues.append(
            f"steps mismatch: run_metrics={run_metrics.get('steps')}, trajectory={max(episode.steps.keys())}"
        )
    if not end_reason_match:
        issues.append(
            f"end_reason mismatch: run_metrics={run_metrics.get('end_reason')}, trajectory={last.end_reason}"
        )

    detail = {
        "run_metrics": {
            "coverage": run_metrics.get("coverage"),
            "transport_rate": run_metrics.get("transport_rate"),
            "finished": run_metrics.get("finished"),
            "steps": run_metrics.get("steps"),
            "end_reason": run_metrics.get("end_reason"),
        },
        "trajectory": {
            "coverage": last.coverage,
            "transport_rate": last.transport_rate,
            "finished": last.finished,
            "steps": max(episode.steps.keys()),
            "end_reason": last.end_reason,
        },
        "matches": {
            "coverage": coverage_match,
            "transport_rate": transport_match,
            "finished": finished_match,
            "steps": steps_match,
            "end_reason": end_reason_match,
        },
        "issues": issues,
        "passed": len(issues) == 0,
    }

    return GradeResult(
        grader="StateGrader",
        level="episode",
        passed=len(issues) == 0,
        score=1.0 if len(issues) == 0 else 0.0,
        detail=detail,
        evidence_ref="run_metrics.json, trajectory.csv",
    )


def _detect_regressions(episode: EpisodeDataset) -> GradeResult:
    regression_steps = []
    prev_coverage = -1.0
    prev_transport = -1.0

    sorted_steps = sorted(episode.steps.keys())
    for step_num in sorted_steps:
        sr = episode.steps[step_num]
        if sr.coverage < prev_coverage - 0.001:
            regression_steps.append(
                {
                    "step": step_num,
                    "metric": "coverage",
                    "from": prev_coverage,
                    "to": sr.coverage,
                }
            )
        if sr.transport_rate < prev_transport - 0.001:
            regression_steps.append(
                {
                    "step": step_num,
                    "metric": "transport_rate",
                    "from": prev_transport,
                    "to": sr.transport_rate,
                }
            )
        prev_coverage = sr.coverage
        prev_transport = sr.transport_rate

    return GradeResult(
        grader="StateGrader",
        level="episode",
        passed=len(regression_steps) == 0,
        score=1.0 if len(regression_steps) == 0 else 0.0,
        detail={
            "regression_count": len(regression_steps),
            "regression_steps": regression_steps,
            "note": (
                "No metric regressions detected"
                if not regression_steps
                else f"Detected {len(regression_steps)} metric regression(s)"
            ),
        },
        evidence_ref="trajectory.csv:Coverage,TransportRate",
    )


STATE_GRADER_NAME = "StateGrader"
