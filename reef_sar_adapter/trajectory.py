"""Trajectory reader: one sar_orch run directory -> the events the gate grades.

``descriptor.yaml`` names this module as a dotted reader reference
(``reef_sar_adapter.trajectory:read_sar_run``), which reef's
``trajectory.reader_for`` wraps in a callable reader. The runner lands the
run's gradeable artifacts in the trajectory directory (``sar/out``):

- ``run_metrics.json`` - the experiment's run-terminal metrics (``steps``,
  ``coverage``, ``transport_rate``, ``finished``, ``end_reason``, ...);
- ``eval_metrics.json`` - the sar-metrics collector's report, where the score
  formula's other two terms live (``l2_planning.load_balance_b``,
  ``l4_cost.effective_billed_tokens``).

Both are required. A directory missing either one is a run that produced
nothing gradeable, and it reads as the empty tuple, which makes the method
score it at the floor (``method.SCORE_FLOOR``) instead of grading it on half
its terms - an episode whose numbers are unknown must not out-score a healthy
one.

A file that exists but cannot be decoded raises ``TrajectoryError``, the
convention the bundled readers follow: reef turns that into a failed episode
(score ``-inf``, ``stage=trajectory``), so corruption is loud without
aborting the step's other episodes.

Events, in this order:

    {"type": "metrics", "run_metrics": {...}, "eval_metrics": {...}}
    {"type": "run_meta", "scene": ..., "agents": ..., "seed": ...,
     "steps": ..., "end_reason": ...}

``run_meta`` carries the episode identity for the step record and for round
reports; nothing in the score depends on it. Its ``scene``/``agents``/``seed``
come from ``run_meta.json`` - three fields the runner writes from the task
JSON it was invoked with - falling back to the run's own ``metadata.json``
(``scene``, ``seed``, ``agent_count``) when the runner copied one; absent
either, they are ``None``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # the reader runs inside the reef service environment
    from reef.harness.episodes.trajectory import TrajectoryError
except ImportError:  # standalone use, e.g. reading a run outside a reef install

    class TrajectoryError(Exception):  # type: ignore[no-redef]
        """Stand-in for reef's error type when reef is not importable."""


__all__ = [
    "EVAL_METRICS_FILE",
    "METADATA_FILE",
    "METRICS_EVENT",
    "RUN_META_EVENT",
    "RUN_META_FILE",
    "RUN_METRICS_FILE",
    "TrajectoryError",
    "read_sar_run",
]

RUN_METRICS_FILE = "run_metrics.json"
EVAL_METRICS_FILE = "eval_metrics.json"
#: The runner's own identity file, written from the task JSON (see module docstring).
RUN_META_FILE = "run_meta.json"
#: The experiment's own run metadata, read only as a fallback source.
METADATA_FILE = "metadata.json"

METRICS_EVENT = "metrics"
RUN_META_EVENT = "run_meta"


def read_sar_run(path: Path | str) -> tuple[dict[str, Any], ...]:
    """Read one SAR run directory into the gate's events (see module docstring)."""
    root = Path(path)
    run_metrics = _read_object(root / RUN_METRICS_FILE)
    eval_metrics = _read_object(root / EVAL_METRICS_FILE)
    if run_metrics is None or eval_metrics is None:
        # Nothing was produced (a crashed or unfinished episode reads as an
        # empty trajectory, never as a fabricated score).
        return ()
    return (
        {
            "type": METRICS_EVENT,
            "run_metrics": run_metrics,
            "eval_metrics": eval_metrics,
        },
        _run_meta(root, run_metrics),
    )


def _run_meta(root: Path, run_metrics: Mapping[str, Any]) -> dict[str, Any]:
    """The episode identity event: task identity where it was recorded, run facts here."""
    recorded = _read_object(root / RUN_META_FILE) or _read_object(root / METADATA_FILE) or {}
    return {
        "type": RUN_META_EVENT,
        "scene": recorded.get("scene"),
        "agents": _first(recorded, "agents", "agent_count"),
        "seed": recorded.get("seed"),
        "steps": run_metrics.get("steps"),
        "end_reason": run_metrics.get("end_reason"),
    }


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    """The first key present in ``mapping``, else None (a present null stays null)."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _read_object(file: Path) -> dict[str, Any] | None:
    """The JSON object ``file`` holds, or None when it does not exist.

    A present file that cannot be read or decoded raises ``TrajectoryError``:
    an unreadable metric is not a missing one.
    """
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TrajectoryError(f"cannot read {file}: {exc}") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrajectoryError(f"{file} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise TrajectoryError(f"{file} must hold a JSON object, got {type(decoded).__name__}")
    return decoded
