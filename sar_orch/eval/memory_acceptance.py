#!/usr/bin/env python3
"""Phase 5 run-acceptance evaluator for the Memory System Redesign.

Reads the run artifacts (``run_metrics.json``, ``agent_interactions.csv``,
``router_interactions.csv`` and the canonical Memory DB / export manifest) and
writes ``<results_dir>/memory_acceptance.json`` with the fixed acceptance
schema.  Failure outcomes are aggregated only from the structured
``{Success, ErrorType}`` outcome rows produced by the Phase 5 logger API; this
script never greps runtime logs.

Exit invariants (plan 10.1):
- ``coverage`` / ``transport_rate`` come from ``run_metrics.json`` and must be
  non-null (numeric).
- ``missing_error_code_rows=0`` and all three ``framework_error_counts`` must
  be 0 for a passing run.
- a failed outcome row without ``ErrorType`` exits with
  ``instrumentation_missing``; an unrecognised error code exits nonzero and
  prints the code/count.

Usage:
    uv run python sar_orch/eval/memory_acceptance.py --results-dir <results_dir>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

FRAMEWORK_ERROR_CODES = (
    "worker_busy",
    "task_not_routable_yet",
    "unknown_task_id",
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INSTRUMENTATION_MISSING = 3
EXIT_UNKNOWN_ERROR_CODE = 4
EXIT_FRAMEWORK_ERRORS_PRESENT = 5
EXIT_METRICS_MISSING = 6
EXIT_ARTIFACTS_MISSING = 7
EXIT_INVALID_MEMORY_MANIFEST = 8

_EXPORT_MANIFEST_CANDIDATES = (
    "coordinator/memory/export_manifest.json",
    "memory/export_manifest.json",
    "export_manifest.json",
)

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


class AcceptanceError(RuntimeError):
    """Input / gate failure carrying a stable process exit code."""

    def __init__(self, exit_code: int, message: str) -> None:
        self.exit_code = exit_code
        self.message = message
        super().__init__(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_run_metrics(results_dir: Path) -> tuple[float, float]:
    path = results_dir / "run_metrics.json"
    if not path.is_file():
        raise AcceptanceError(
            EXIT_METRICS_MISSING, f"run_metrics.json not found: {path}"
        )
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise AcceptanceError(
            EXIT_METRICS_MISSING, "run_metrics.json must be a JSON object"
        )
    coverage = data.get("coverage")
    transport_rate = data.get("transport_rate")
    if coverage is None or transport_rate is None:
        raise AcceptanceError(
            EXIT_METRICS_MISSING,
            "run_metrics.json coverage and transport_rate must be non-null",
        )
    try:
        return float(coverage), float(transport_rate)
    except (TypeError, ValueError):
        raise AcceptanceError(
            EXIT_METRICS_MISSING,
            "run_metrics.json coverage and transport_rate must be numeric",
        )


def _read_outcome_rows(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = set(reader.fieldnames or [])
        has_success = "Success" in fieldnames
        for row in reader:
            error_type = str(row.get("ErrorType", "") or "").strip()
            failed = False
            if has_success:
                success = str(row.get("Success", "") or "").strip().lower()
                failed = success in ("false", "0", "no")
            else:
                failed = bool(error_type)
            if failed:
                rows.append((path.name, error_type))
    return rows


def _aggregate_failures(results_dir: Path) -> dict[str, Any]:
    agent_csv = results_dir / "agent_interactions.csv"
    router_csv = results_dir / "router_interactions.csv"
    missing = [p.name for p in (agent_csv, router_csv) if not p.is_file()]
    if missing:
        raise AcceptanceError(
            EXIT_ARTIFACTS_MISSING,
            f"missing outcome artifacts: {sorted(missing)}",
        )
    failed_rows = _read_outcome_rows(agent_csv) + _read_outcome_rows(router_csv)

    counts = {code: 0 for code in FRAMEWORK_ERROR_CODES}
    unknown: dict[str, int] = {}
    missing_error_code_rows = 0
    for _source, error_type in failed_rows:
        if error_type == "":
            missing_error_code_rows += 1
        elif error_type in counts:
            counts[error_type] += 1
        else:
            unknown[error_type] = unknown.get(error_type, 0) + 1

    return {
        "failed_tool_rows": len(failed_rows),
        "missing_error_code_rows": missing_error_code_rows,
        "framework_error_counts": counts,
        "unknown_error_codes": unknown,
    }


def _iter_manifest_entries(manifest: Any) -> list[Any]:
    if isinstance(manifest, list):
        return manifest
    if isinstance(manifest, dict):
        artifacts = manifest.get("artifacts")
        if isinstance(artifacts, list):
            return artifacts
        return [manifest]
    return []


def _extract_scope_id(manifest: Any) -> str:
    ids = {
        str(entry["scope_id"])
        for entry in _iter_manifest_entries(manifest)
        if isinstance(entry, dict) and entry.get("scope_id")
    }
    if len(ids) == 1:
        return next(iter(ids))
    raise AcceptanceError(
        EXIT_INVALID_MEMORY_MANIFEST,
        f"export manifest must declare exactly one scope_id, found {sorted(ids)}",
    )


def _extract_memory_revision(manifest: Any) -> int:
    revisions: list[int] = []
    for entry in _iter_manifest_entries(manifest):
        if not isinstance(entry, dict):
            continue
        for key in ("canonical_revision", "memory_revision"):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                revisions.append(value)
    if not revisions:
        raise AcceptanceError(
            EXIT_INVALID_MEMORY_MANIFEST,
            "export manifest is missing canonical_revision / memory_revision",
        )
    return max(revisions)


def _scope_exists_in_db(db_path: Path, scope_id: str) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM memory_scope WHERE scope_id=?", (scope_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _read_scope_from_db(db_path: Path) -> tuple[str, int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT scope_id, closed_at FROM memory_scope ORDER BY scope_id"
        ).fetchall()
        if not rows:
            raise AcceptanceError(
                EXIT_INVALID_MEMORY_MANIFEST,
                "canonical memory DB has no scopes",
            )
        active = [scope_id for scope_id, closed_at in rows if closed_at is None]
        candidates = active or [scope_id for scope_id, _closed_at in rows]

        def revision(scope_id: str) -> int:
            row = conn.execute(
                "SELECT revision FROM memory_revision WHERE scope_id=?", (scope_id,)
            ).fetchone()
            return int(row[0]) if row else 0

        scope_id = min(candidates, key=lambda sid: (-revision(sid), sid))
        return scope_id, revision(scope_id)
    finally:
        conn.close()


def _resolve_memory_manifest(results_dir: Path) -> dict[str, Any]:
    manifest_path = next(
        (
            results_dir / candidate
            for candidate in _EXPORT_MANIFEST_CANDIDATES
            if (results_dir / candidate).is_file()
        ),
        None,
    )
    if manifest_path is not None:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        scope_id = _extract_scope_id(manifest)
        db_path = _find_canonical_db(results_dir)
        if db_path is not None and not _scope_exists_in_db(db_path, scope_id):
            raise AcceptanceError(
                EXIT_INVALID_MEMORY_MANIFEST,
                f"export manifest scope_id {scope_id} not found in canonical DB",
            )
        return {
            "scope_id": scope_id,
            "memory_revision": _extract_memory_revision(manifest),
            "export_manifest_sha256": _sha256_file(manifest_path),
        }

    db_path = _find_canonical_db(results_dir)
    if db_path is None:
        raise AcceptanceError(
            EXIT_INVALID_MEMORY_MANIFEST,
            "no export manifest and no canonical memory DB found under results dir",
        )
    scope_id, memory_revision = _read_scope_from_db(db_path)
    return {
        "scope_id": scope_id,
        "memory_revision": memory_revision,
        "export_manifest_sha256": None,
    }


def evaluate(
    results_dir: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Aggregate acceptance metrics and return (result, unknown_error_codes)."""
    results_dir = Path(results_dir)
    coverage, transport_rate = _load_run_metrics(results_dir)
    failures = _aggregate_failures(results_dir)
    memory = _resolve_memory_manifest(results_dir)
    result = {
        "schema_version": SCHEMA_VERSION,
        "scope_id": memory["scope_id"],
        "memory_revision": memory["memory_revision"],
        "export_manifest_sha256": memory["export_manifest_sha256"],
        "coverage": coverage,
        "transport_rate": transport_rate,
        "failed_tool_rows": failures["failed_tool_rows"],
        "missing_error_code_rows": failures["missing_error_code_rows"],
        "framework_error_counts": failures["framework_error_counts"],
    }
    return result, failures["unknown_error_codes"]


def gate(
    result: dict[str, Any], unknown_error_codes: dict[str, int] | None = None
) -> None:
    """Raise :class:`AcceptanceError` for every failing gate condition."""
    missing = int(result["missing_error_code_rows"])
    if missing > 0:
        raise AcceptanceError(
            EXIT_INSTRUMENTATION_MISSING,
            f"instrumentation_missing: {missing} failed tool row(s) have an empty error_code",
        )
    present = {
        code: int(count)
        for code, count in result["framework_error_counts"].items()
        if int(count) > 0
    }
    if present:
        summary = ", ".join(f"{code}={count}" for code, count in present.items())
        raise AcceptanceError(
            EXIT_FRAMEWORK_ERRORS_PRESENT, f"framework_error: {summary}"
        )
    unknown = unknown_error_codes or {}
    if unknown:
        summary = ", ".join(
            f"code={code} count={count}" for code, count in sorted(unknown.items())
        )
        raise AcceptanceError(EXIT_UNKNOWN_ERROR_CODE, f"unknown_error_code: {summary}")


def write_artifact(results_dir: Path, result: dict[str, Any]) -> Path:
    out_path = results_dir / "memory_acceptance.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return out_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Memory run acceptance evaluator (Phase 5)"
    )
    parser.add_argument("--results-dir", required=True, help="Run results directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(
            f"memory_acceptance: results dir not found: {results_dir}", file=sys.stderr
        )
        return EXIT_ARTIFACTS_MISSING
    try:
        result, unknown = evaluate(results_dir)
    except AcceptanceError as exc:
        print(f"memory_acceptance: {exc.message}", file=sys.stderr)
        return exc.exit_code
    write_artifact(results_dir, result)
    try:
        gate(result, unknown)
    except AcceptanceError as exc:
        print(f"memory_acceptance: {exc.message}", file=sys.stderr)
        return exc.exit_code
    print(
        "memory_acceptance: PASS "
        f"scope={result['scope_id'][:12]}... "
        f"coverage={result['coverage']} transport_rate={result['transport_rate']}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
