"""V1-Judge metrics for the SAR metric system (sar-metrics v1, D card).

Two LLM-judged metrics sharing one fail-closed Judge Task framework
(card decisions in ``.hermes/spec/sar-metrics/README.md`` §6 + the D card):

- :func:`evaluate_planning_path` — L2 dispatch path quality, LLM-identified
  deductions scored deterministically on a 0-100 weighted scale;
- :func:`evaluate_observation_ignore` — L3 contradiction rate of worker
  decisions against the latest observation.

:func:`evaluate_run` orchestrates the selected metrics into one
``judge_metrics.json`` payload; the CLI lives in ``__main__``::

    uv run python -m sar_orch.eval.judge --results-dir <run_dir> \\
        [--metrics planning_path,observation_ignore] [--sample-size 20] \\
        [--output <path>] [--env-file <path>]

Hard boundaries: read-only against the run dir (the judge artifact is the
only write), judge model config reuses the existing ``.env`` keys (never
introduces a new provider/model), and any judge/parse failure degrades a
sample or a metric to ``judge_error`` instead of crashing the CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .client import (  # noqa: F401 — re-exported public surface
    DEFAULT_JUDGE_TIMEOUT_SEC,
    MAX_JUDGE_ATTEMPTS,
    JudgeCallError,
    JudgeClient,
    JudgeCompletion,
    JudgeConfig,
    JudgeParseError,
    LLMJudgeClient,
    build_judge_client,
    extract_json_object,
    load_judge_env,
    resolve_judge_config,
    run_judge_json,
)
from .observation_ignore import (
    DEFAULT_SAMPLE_SIZE,
    evaluate_observation_ignore,
)
from .planning_path import evaluate_planning_path

SCHEMA_VERSION = 1
EVALUATOR_VERSION = "sar-judge-2.0.0"  # 2.0.0: planning_path score = deterministic 0-100 weighting
DEFAULT_OUTPUT = "judge_metrics.json"

METRIC_NAMES = ("planning_path", "observation_ignore")

__all__ = [
    "DEFAULT_OUTPUT",
    "DEFAULT_SAMPLE_SIZE",
    "EVALUATOR_VERSION",
    "METRIC_NAMES",
    "SCHEMA_VERSION",
    "evaluate_observation_ignore",
    "evaluate_planning_path",
    "evaluate_run",
    "write_artifact",
]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_run_id(run_dir: Path) -> str | None:
    from .artifacts import load_json

    metadata = load_json(run_dir / "metadata.json")
    if isinstance(metadata, dict):
        run_id = metadata.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return None


def _sum_usage(metrics: Mapping[str, Any]) -> dict[str, int]:
    totals = {
        "judge_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
    }
    for payload in metrics.values():
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        totals["judge_calls"] += int(usage.get("calls", 0) or 0)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_hit_tokens",
        ):
            totals[key] += int(usage.get(key, 0) or 0)
    return totals


def evaluate_run(
    run_dir: Path | str,
    *,
    metrics: Iterable[str] = METRIC_NAMES,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    client: JudgeClient | None = None,
    judge_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the selected judge metrics over one run directory.

    ``client=None`` records ``judge_unconfigured`` per metric (the caller had
    no usable judge model config).  Expected operational failures never raise:
    each metric degrades to a status inside the payload.
    """
    run_dir = Path(run_dir)
    selected = [name for name in metrics]
    unknown = [name for name in selected if name not in METRIC_NAMES]
    if unknown:
        raise ValueError(f"unknown judge metric(s): {', '.join(unknown)}")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "generated_at": _utc_now(),
        "run_dir": str(run_dir.resolve()),
        "run_id": _load_run_id(run_dir) if run_dir.is_dir() else None,
        "judge": dict(judge_info) if judge_info else None,
        "sample_size_requested": sample_size,
        "metrics": {},
    }
    results: dict[str, Any] = {}
    for name in selected:
        try:
            if name == "planning_path":
                result = evaluate_planning_path(run_dir, client)
            else:
                result = evaluate_observation_ignore(
                    run_dir, client, sample_size=sample_size
                )
        except Exception as exc:  # noqa: BLE001 — never crash the artifact
            result = {
                "status": "judge_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        results[name] = result
    payload["metrics"] = results
    payload["totals"] = _sum_usage(results)
    payload["totals"]["judge_error_count"] = sum(
        int(result.get("judge_error_count", 0) or 0) for result in results.values()
    )
    return payload


def write_artifact(
    run_dir: Path | str,
    payload: dict[str, Any],
    output_path: Path | str | None = None,
) -> Path:
    """Write the judge payload to ``judge_metrics.json`` (or ``--output``)."""
    import json

    run_dir = Path(run_dir)
    target = (
        Path(output_path)
        if output_path is not None
        else run_dir / DEFAULT_OUTPUT
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target
