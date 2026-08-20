#!/usr/bin/env python3
"""Phase 5 terminal-only projection-quality evaluator (H1 card C7).

Runs only after a run reaches terminal and the canonical Memory snapshot is
frozen.  Compares the frozen Worker evidence (canonical Temporal evidence
events) and the frozen Memory projection snapshot against an evaluator-private
truth manifest / trace, then writes only ``<results_dir>/memory_projection_quality.json``.

Hard boundaries:
- Memory access is strictly read-only: the canonical SQLite store is opened
  with ``mode=ro`` and no SQLite / projection / revision / outbox / Context /
  compatibility artifact is ever written.
- the raw truth trace is only read from the evaluator-private manifest
  location and is never copied into the results dir or any candidate- /
  agent-readable path.
- oracle truth is used exclusively for post-hoc comparison, never as online
  correction: nothing is written back to Memory.

Metric semantics (plan 10.1 / H1 C7):
- ``worker_report_quality`` compares, per same-step truth claim, the latest
  Worker report observed at or before that step (accuracy, precision/recall,
  stale / false-claim calibration).
- ``memory_integration_quality`` compares the frozen projection fields against
  the truth claims (field match, freshness/staleness, conflict calibration and
  evidence traceability via the canonical Temporal event join).
- null rates only appear when ``metric_status=not_applicable``; invalid or
  missing inputs exit nonzero.

Usage:
    uv run python sar_orch/eval/memory_projection_quality.py \
        --results-dir <results_dir> \
        --truth-manifest <evaluator-private>/truth_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EVALUATOR_VERSION = "memory-projection-quality-1.0.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID_INPUT = 3

_TERMINAL_COMPLETED = ("success", "completed", "task_completed", "done")
_TERMINAL_TIMEOUT = (
    "max_steps_reached",
    "wall_clock_timeout",
    "coordinator_finished_early",
    "stopped_before_success",
)
_TERMINAL_FAILED = (
    "framework_error",
    "worker_timeout",
    "environment_error",
    "failed",
    "error",
)
_TERMINAL_CANCELLED = ("cancelled", "canceled", "aborted", "user_cancelled", "killed")

_WORKER_RATE_FIELDS = ("precision", "recall")
_MEMORY_RATE_FIELDS = (
    "precision",
    "recall",
    "conflict_precision",
    "evidence_traceability_rate",
)


class ProjectionQualityError(RuntimeError):
    """Invalid / missing input failure with a stable process exit code."""

    def __init__(self, exit_code: int, message: str) -> None:
        self.exit_code = exit_code
        self.message = message
        super().__init__(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _norm_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {key: _norm_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_norm_value(item) for item in value]
    return value


def _values_equal(left: Any, right: Any) -> bool:
    return _canonical_json(_norm_value(left)) == _canonical_json(_norm_value(right))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _count(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _terminal_status(results_dir: Path) -> str:
    metrics_path = results_dir / "run_metrics.json"
    if not metrics_path.is_file():
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, f"run_metrics.json not found: {metrics_path}"
        )
    with open(metrics_path, encoding="utf-8") as fh:
        metrics = json.load(fh)
    if not isinstance(metrics, dict):
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, "run_metrics.json must be a JSON object"
        )
    finished = bool(metrics.get("finished"))
    end_reason = str(metrics.get("end_reason") or "").strip().lower()
    if finished or end_reason in _TERMINAL_COMPLETED:
        return "completed"
    if end_reason in _TERMINAL_TIMEOUT:
        return "timeout"
    if end_reason in _TERMINAL_FAILED:
        return "failed"
    if end_reason in _TERMINAL_CANCELLED:
        return "cancelled"
    raise ProjectionQualityError(
        EXIT_INVALID_INPUT,
        f"invalid terminal status: finished={metrics.get('finished')!r} "
        f"end_reason={metrics.get('end_reason')!r}",
    )


def _load_truth_claims(trace_path: Path) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    with open(trace_path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise ProjectionQualityError(
                    EXIT_INVALID_INPUT, f"invalid JSON in truth trace at line {lineno}"
                )
            if not isinstance(record, dict):
                raise ProjectionQualityError(
                    EXIT_INVALID_INPUT,
                    f"truth trace record at line {lineno} must be a JSON object",
                )
            missing = [
                key
                for key in ("step", "domain", "entity_id", "field", "value")
                if key not in record
            ]
            if missing:
                raise ProjectionQualityError(
                    EXIT_INVALID_INPUT,
                    f"truth trace record at line {lineno} is missing {missing}",
                )
            try:
                step = int(record["step"])
            except (TypeError, ValueError):
                raise ProjectionQualityError(
                    EXIT_INVALID_INPUT,
                    f"truth trace record at line {lineno} has a non-integer step",
                )
            claims.append(
                {
                    "step": step,
                    "domain": str(record["domain"]),
                    "entity_id": str(record["entity_id"]),
                    "field": str(record["field"]),
                    "value": record["value"],
                }
            )
    return claims


_CANONICAL_DB_CANDIDATES = (
    "coordinator/memory/memory.sqlite3",
    "memory/memory.sqlite3",
)


def _find_canonical_db(results_dir: Path) -> Path | None:
    return next(
        (
            results_dir / candidate
            for candidate in _CANONICAL_DB_CANDIDATES
            if (results_dir / candidate).is_file()
        ),
        None,
    )


def _open_canonical_db(results_dir: Path) -> sqlite3.Connection:
    db_path = _find_canonical_db(results_dir)
    if db_path is None:
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT,
            "canonical memory DB not found under results dir",
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _require_scope(conn: sqlite3.Connection, scope_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM memory_scope WHERE scope_id=?", (scope_id,)
    ).fetchone()
    if row is None:
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT,
            f"scope_mismatch: manifest scope_id {scope_id} not found in frozen "
            "canonical memory DB",
        )


def _worker_reports(conn: sqlite3.Connection, scope_id: str) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT payload FROM temporal_event "
        "WHERE scope_id=? AND event_type='evidence.projection' ORDER BY sequence",
        (scope_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        env_step = payload.get("env_step")
        if env_step is None:
            continue
        reports.append(
            {
                "step": int(env_step),
                "domain": payload.get("domain"),
                "entity_id": payload.get("entity_id"),
                "field": payload.get("field_name"),
                "value": payload.get("value"),
            }
        )
    return reports


def _projection_fields(conn: sqlite3.Connection, scope_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT domain, entity_id, field_name, value, env_step, outcome, "
        "conflict_candidates, event_id FROM projection_field "
        "WHERE scope_id=? ORDER BY domain, entity_id, field_name",
        (scope_id,),
    ).fetchall()
    fields: list[dict[str, Any]] = []
    for row in rows:
        value = row["value"]
        conflicts = row["conflict_candidates"]
        fields.append(
            {
                "domain": row["domain"],
                "entity_id": row["entity_id"],
                "field": row["field_name"],
                "value": json.loads(value) if value else None,
                "env_step": row["env_step"],
                "outcome": row["outcome"],
                "conflict_candidates": json.loads(conflicts) if conflicts else [],
                "event_id": row["event_id"],
            }
        )
    return fields


def _temporal_event_ids(conn: sqlite3.Connection, scope_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT event_id FROM temporal_event WHERE scope_id=?", (scope_id,)
    ).fetchall()
    return {row["event_id"] for row in rows}


def _memory_snapshot_digest(
    conn: sqlite3.Connection, scope_id: str, projections: list[dict[str, Any]]
) -> str:
    revision = _count(
        conn, "SELECT revision FROM memory_revision WHERE scope_id=?", scope_id
    )
    temporal_count = _count(
        conn, "SELECT COUNT(*) FROM temporal_event WHERE scope_id=?", scope_id
    )
    outcome_count = _count(
        conn, "SELECT COUNT(*) FROM projection_outcome WHERE scope_id=?", scope_id
    )
    relation_count = _count(
        conn, "SELECT COUNT(*) FROM memory_relation WHERE scope_id=?", scope_id
    )
    payload = {
        "scope_id": scope_id,
        "memory_revision": revision,
        "projection_fields": [
            {
                "domain": p["domain"],
                "entity_id": p["entity_id"],
                "field": p["field"],
                "value": p["value"],
                "env_step": p["env_step"],
                "outcome": p["outcome"],
            }
            for p in projections
        ],
        "temporal_event_count": temporal_count,
        "projection_outcome_count": outcome_count,
        "relation_count": relation_count,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _empty_worker_report_quality() -> dict[str, Any]:
    return {
        "evaluated_report_count": 0,
        "observable_field_count": 0,
        "correct_field_count": 0,
        "false_claim_count": 0,
        "stale_report_count": 0,
        "precision": None,
        "recall": None,
    }


def _empty_memory_integration_quality() -> dict[str, Any]:
    return {
        "evaluated_projection_field_count": 0,
        "correct_projection_field_count": 0,
        "stale_projection_count": 0,
        "conflicted_field_count": 0,
        "traceable_field_count": 0,
        "precision": None,
        "recall": None,
        "conflict_precision": None,
        "evidence_traceability_rate": None,
    }


def _worker_report_quality(
    reports: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> dict[str, Any]:
    quality = _empty_worker_report_quality()
    for claim in claims:
        key = (claim["domain"], claim["entity_id"], claim["field"])
        matches = [
            report
            for report in reports
            if (report["domain"], report["entity_id"], report["field"]) == key
            and report["step"] <= claim["step"]
        ]
        if not matches:
            continue
        latest = max(matches, key=lambda report: report["step"])
        quality["evaluated_report_count"] += 1
        if latest["step"] == claim["step"]:
            quality["observable_field_count"] += 1
            if _values_equal(latest["value"], claim["value"]):
                quality["correct_field_count"] += 1
            else:
                quality["false_claim_count"] += 1
        else:
            quality["stale_report_count"] += 1
    return quality


def _memory_integration_quality(
    projections: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    traceable_event_ids: set[str],
) -> dict[str, Any]:
    by_key = {(p["domain"], p["entity_id"], p["field"]): p for p in projections}
    quality = _empty_memory_integration_quality()
    quality["correct_conflicted_count"] = 0
    for claim in claims:
        projection = by_key.get((claim["domain"], claim["entity_id"], claim["field"]))
        if projection is None:
            continue
        quality["evaluated_projection_field_count"] += 1
        if projection["event_id"] in traceable_event_ids:
            quality["traceable_field_count"] += 1
        conflicted = projection["outcome"] == "conflicted" or bool(
            projection.get("conflict_candidates")
        )
        if conflicted:
            quality["conflicted_field_count"] += 1
            if _values_equal(projection["value"], claim["value"]):
                quality["correct_conflicted_count"] += 1
        if (
            projection["env_step"] is not None
            and projection["env_step"] < claim["step"]
        ):
            quality["stale_projection_count"] += 1
        if (
            not conflicted
            and projection["env_step"] == claim["step"]
            and _values_equal(projection["value"], claim["value"])
        ):
            quality["correct_projection_field_count"] += 1
    return quality


def _finalize_quality(
    worker_quality: dict[str, Any],
    memory_quality: dict[str, Any],
    total_claims: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    observable = worker_quality["observable_field_count"]
    evaluated = memory_quality["evaluated_projection_field_count"]
    if total_claims == 0 or (observable == 0 and evaluated == 0):
        for field in _WORKER_RATE_FIELDS:
            worker_quality[field] = None
        for field in _MEMORY_RATE_FIELDS:
            memory_quality[field] = None
        memory_quality.pop("correct_conflicted_count", None)
        return worker_quality, memory_quality, "not_applicable"

    worker_quality["precision"] = _rate(
        worker_quality["correct_field_count"],
        worker_quality["correct_field_count"] + worker_quality["false_claim_count"],
    )
    worker_quality["recall"] = _rate(worker_quality["correct_field_count"], observable)
    memory_quality["precision"] = _rate(
        memory_quality["correct_projection_field_count"], evaluated
    )
    memory_quality["recall"] = _rate(evaluated, total_claims)
    memory_quality["conflict_precision"] = _rate(
        memory_quality.pop("correct_conflicted_count", 0),
        memory_quality["conflicted_field_count"],
    )
    memory_quality["evidence_traceability_rate"] = _rate(
        memory_quality["traceable_field_count"], evaluated
    )
    return worker_quality, memory_quality, "measured"


def evaluate(
    results_dir: str | Path,
    truth_manifest_path: str | Path,
    truth_trace_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare the frozen Worker evidence / Memory snapshot with the truth."""
    results_dir = Path(results_dir)
    manifest_path = Path(truth_manifest_path)
    if not manifest_path.is_file():
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, f"truth manifest not found: {manifest_path}"
        )
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, "truth manifest must be a JSON object"
        )

    scope_id = manifest.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, "truth manifest is missing scope_id"
        )

    evaluator_version = str(manifest.get("evaluator_version") or EVALUATOR_VERSION)

    if truth_trace_path is not None:
        trace_path = Path(truth_trace_path)
    else:
        trace_ref = manifest.get("trace")
        if not isinstance(trace_ref, str) or not trace_ref:
            raise ProjectionQualityError(
                EXIT_INVALID_INPUT, "truth manifest is missing trace path"
            )
        trace_path = manifest_path.parent / trace_ref
    if not trace_path.is_file():
        raise ProjectionQualityError(
            EXIT_INVALID_INPUT, f"truth trace not found: {trace_path}"
        )

    terminal_status = _terminal_status(results_dir)
    claims = _load_truth_claims(trace_path)
    truth_trace_sha256 = _sha256_file(trace_path)

    conn = _open_canonical_db(results_dir)
    try:
        _require_scope(conn, scope_id)
        worker_reports = _worker_reports(conn, scope_id)
        projections = _projection_fields(conn, scope_id)
        traceable_event_ids = _temporal_event_ids(conn, scope_id)
        memory_manifest_sha256 = _memory_snapshot_digest(conn, scope_id, projections)
    finally:
        conn.close()

    worker_quality, memory_quality, metric_status = _finalize_quality(
        _worker_report_quality(worker_reports, claims),
        _memory_integration_quality(projections, claims, traceable_event_ids),
        len(claims),
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "scope_id": scope_id,
        "terminal_status": terminal_status,
        "memory_manifest_sha256": memory_manifest_sha256,
        "truth_trace_sha256": truth_trace_sha256,
        "evaluator_version": evaluator_version,
        "metric_status": metric_status,
        "worker_report_quality": worker_quality,
        "memory_integration_quality": memory_quality,
    }


def write_artifact(results_dir: Path, artifact: dict[str, Any]) -> Path:
    out_path = results_dir / "memory_projection_quality.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return out_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminal-only memory projection quality evaluator (Phase 5)"
    )
    parser.add_argument("--results-dir", required=True, help="Run results directory")
    parser.add_argument(
        "--truth-manifest",
        required=True,
        help="Evaluator-private truth manifest (JSON) frozen after run terminal",
    )
    parser.add_argument(
        "--truth-trace",
        default=None,
        help="Optional override of the truth trace path declared in the manifest",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(
            f"memory_projection_quality: results dir not found: {results_dir}",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT
    try:
        artifact = evaluate(results_dir, args.truth_manifest, args.truth_trace)
    except ProjectionQualityError as exc:
        print(f"memory_projection_quality: {exc.message}", file=sys.stderr)
        return exc.exit_code
    write_artifact(results_dir, artifact)
    print(
        "memory_projection_quality: PASS "
        f"scope={artifact['scope_id'][:12]}... "
        f"metric_status={artifact['metric_status']}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
