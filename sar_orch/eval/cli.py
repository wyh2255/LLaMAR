import argparse
import sys
from pathlib import Path

from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.outcome import grade_outcome
from sar_orch.eval.graders.state import grade_state
from sar_orch.eval.graders.constraint import grade_constraint
from sar_orch.eval.graders.error_taxonomy import grade_error_taxonomy
from sar_orch.eval.graders.trajectory import grade_trajectory
from sar_orch.eval.report import merge_results, write_report

ALL_GRADERS = [
    ("OutcomeGrader", grade_outcome),
    ("StateGrader", grade_state),
    ("ConstraintGrader", grade_constraint),
    ("ErrorTaxonomy", grade_error_taxonomy),
    ("TrajectoryGrader", grade_trajectory),
]


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment Eval Agent — M2")
    parser.add_argument(
        "--results-dir",
        type=str,
        required=True,
        help="Path to experiment results directory",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for eval_report.json (default: <results_dir>/eval_report.json)",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"Error: results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    episode = load_episode(results_dir)

    all_results = []
    for name, grader_fn in ALL_GRADERS:
        grader_results = grader_fn(episode)
        all_results.extend(grader_results)

    report = merge_results(episode, all_results)

    output_path = args.output or str(results_dir / "eval_report.json")
    write_report(report, output_path)
    print(f"eval report written to {output_path}")


if __name__ == "__main__":
    main()
