import json
from pathlib import Path

from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.base import GradeResult


def merge_results(
    episode: EpisodeDataset,
    results: list[GradeResult],
    grader_skips: list[dict] | None = None,
) -> dict:
    if grader_skips is None:
        grader_skips = episode.grader_skips

    episode_out = {}
    failure_taxonomy = {}
    constraint_violations = []
    trajectory_checks = []
    llm_judge = {}

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

    report = {
        "run_dir": str(episode.run_dir),
        "metadata": metadata_out,
        "episode": episode_out,
        "failure_taxonomy": failure_taxonomy,
        "constraint_violations": constraint_violations,
        "trajectory_checks": trajectory_checks,
        "llm_judge": llm_judge,
        "grader_skips": grader_skips,
        "grader_results": grader_results,
    }

    return report


def write_report(report: dict, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
