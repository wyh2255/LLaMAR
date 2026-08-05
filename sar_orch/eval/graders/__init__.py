from sar_orch.eval.graders.base import Grader, GradeResult
from sar_orch.eval.graders.constraint import grade_constraint
from sar_orch.eval.graders.error_taxonomy import grade_error_taxonomy
from sar_orch.eval.graders.outcome import grade_outcome
from sar_orch.eval.graders.state import grade_state
from sar_orch.eval.graders.trajectory import grade_trajectory

# Canonical deterministic grader registry. Single source of truth for the CLI,
# the DeepAgent tool layer, and the benchmark regression gate — keep in sync
# with report.EXPECTED_GRADERS.
ALL_GRADERS: list[tuple[str, object]] = [
    ("OutcomeGrader", grade_outcome),
    ("StateGrader", grade_state),
    ("ConstraintGrader", grade_constraint),
    ("ErrorTaxonomy", grade_error_taxonomy),
    ("TrajectoryGrader", grade_trajectory),
]


def run_all_graders(episode) -> list[GradeResult]:
    """Run every deterministic grader and return the flattened GradeResult list."""
    results: list[GradeResult] = []
    for _name, grader_fn in ALL_GRADERS:
        results.extend(grader_fn(episode))
    return results


__all__ = [
    "ALL_GRADERS",
    "GradeResult",
    "Grader",
    "grade_constraint",
    "grade_error_taxonomy",
    "grade_outcome",
    "grade_state",
    "grade_trajectory",
    "run_all_graders",
]
