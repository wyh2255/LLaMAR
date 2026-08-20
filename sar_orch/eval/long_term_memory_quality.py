"""Phase 4 read-only long-term memory quality evaluator (main plan contract #7).

Compliance/evidence-quality metrics over the run-local long-term database,
single-run edition frozen 2026-08-12:

- ``source_traceability_rate``    — every ``long_term_support`` ref replays to
  a committed canonical ``temporal_event`` row;
- ``forbidden_truth_violation_count`` — fail-closed invariant, always 0;
- ``same_key_conflict_rate``      — memory_keys with >1 distinct content digest;
- ``supersede_chain_length``      — average ``supersedes_memory_id`` chain depth;
- ``reflection_latency_stats``    — p50 of completed-run latency + total model
  tokens (token usage channel: ``long_term_audit`` kind=``reflection_usage``).

The evaluator opens every database with ``mode=ro``
(``sqlite3.connect(f"file:{path}?mode=ro", uri=True)``) — any write attempt
raises ``sqlite3.OperationalError``.  It never writes the source / run /
project databases (their mtimes stay untouched); the only output is
``<results_dir>/long_term_memory_quality.json``.

A missing or empty long-term database is not an error: every metric degrades
to 0 / empty statistics and the artifact is still written.  ``truth_manifest``
must contain ``scope_id`` when provided (typed error otherwise, mirroring
``memory_projection_quality``).  Terminal-only by contract.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    MemoryContractError,
)

__all__ = [
    "LongTermQualityError",
    "evaluate_long_term_memory_quality",
    "forbidden_truth_violation_count",
    "reflection_latency_stats",
    "same_key_conflict_rate",
    "source_traceability_rate",
    "supersede_chain_length",
    "terminal_required",
]

#: Run-local long-term DB candidates (mirrors the canonical-DB search in
#: ``memory_projection_quality``; the coordinator derives the path from
#: ``MemoryConfig.memory_root``).
_LONG_TERM_DB_CANDIDATES = (
    "coordinator/long_term/long_term.sqlite3",
    "long_term/long_term.sqlite3",
)

#: Canonical source DB candidates (evidence replay target).
_SOURCE_DB_CANDIDATES = (
    "coordinator/memory/memory.sqlite3",
    "memory/memory.sqlite3",
)


class LongTermQualityError(MemoryContractError):
    """Typed evaluator input error (e.g. manifest missing ``scope_id``)."""

    code = "invalid_long_term_quality_input"


def _open_long_term_db(path: str | Path) -> sqlite3.Connection:
    """Open the long-term database strictly read-only.

    Any write (CREATE/INSERT/UPDATE/...) raises ``sqlite3.OperationalError``;
    the source/run/project databases are never touched by the evaluator.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ro_conn_or_none(path: str | Path) -> sqlite3.Connection | None:
    """Read-only connection, or ``None`` when the file is missing/unreadable."""
    try:
        return _open_long_term_db(path)
    except sqlite3.Error:
        return None


def _find_db(results_dir: Path, candidates: tuple[str, ...]) -> Path | None:
    for candidate in candidates:
        path = results_dir / candidate
        if path.is_file():
            return path
    return None


def _find_long_term_db(results_dir: Path) -> Path | None:
    return _find_db(results_dir, _LONG_TERM_DB_CANDIDATES)


def _find_source_db(results_dir: Path) -> Path | None:
    return _find_db(results_dir, _SOURCE_DB_CANDIDATES)


# ── metrics ─────────────────────────────────────────────────────────────────

def source_traceability_rate(long_term_db: str | Path, source_db: str | Path) -> float:
    """Fraction of ``long_term_support`` refs replayable to canonical evidence.

    Each (source_scope_id, source_event_id) ref must resolve to a committed
    ``temporal_event`` row in the source database.  Missing databases and
    empty ref sets degrade to 0.0 (fail closed).
    """
    conn = _ro_conn_or_none(long_term_db)
    if conn is None:
        return 0.0
    try:
        refs = conn.execute(
            "SELECT source_scope_id, source_event_id FROM long_term_support"
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    if not refs:
        return 0.0
    source = _ro_conn_or_none(source_db)
    if source is None:
        return 0.0
    traceable = 0
    try:
        for ref in refs:
            row = source.execute(
                "SELECT 1 FROM temporal_event WHERE scope_id=? AND event_id=?",
                (ref["source_scope_id"], ref["source_event_id"]),
            ).fetchone()
            if row is not None:
                traceable += 1
    except sqlite3.Error:
        traceable = 0
    finally:
        source.close()
    return traceable / len(refs)


def forbidden_truth_violation_count(long_term_db: str | Path) -> int:
    """FORBIDDEN_TRUTH_TERMS occurrences in stored statements/audit reasons.

    Fail-closed invariant: the reflection validator never lets truth through,
    so this metric must be 0 for a healthy run.  Missing DB → 0.
    """
    conn = _ro_conn_or_none(long_term_db)
    if conn is None:
        return 0
    try:
        statements = conn.execute("SELECT statement FROM long_term_memory").fetchall()
        reasons = conn.execute("SELECT reason FROM long_term_audit").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    count = 0
    for row in statements:
        lowered = str(row["statement"]).lower()
        if any(term in lowered for term in FORBIDDEN_TRUTH_TERMS):
            count += 1
    for row in reasons:
        lowered = str(row["reason"]).lower()
        if any(term in lowered for term in FORBIDDEN_TRUTH_TERMS):
            count += 1
    return count


def same_key_conflict_rate(long_term_db: str | Path) -> float:
    """Fraction of (project, scope, memory_key) groups with >1 distinct digest.

    A supersede chain legitimately moves a key across digests; this metric
    measures how often the same key carried conflicting content at once
    (healthy runs converge to 0.0).  Only ``status == 'published'`` rows are
    counted (M5): superseded chain members are history, not conflict.
    Missing/empty DB → 0.0.
    """
    conn = _ro_conn_or_none(long_term_db)
    if conn is None:
        return 0.0
    try:
        rows = conn.execute(
            "SELECT project_id, scope_id, memory_key, content_digest "
            "FROM long_term_memory WHERE status='published'"
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    groups: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        key = (str(row["project_id"]), str(row["scope_id"]), str(row["memory_key"]))
        groups.setdefault(key, set()).add(str(row["content_digest"]))
    if not groups:
        return 0.0
    conflicts = sum(1 for digests in groups.values() if len(digests) > 1)
    return conflicts / len(groups)


def supersede_chain_length(long_term_db: str | Path) -> float:
    """Average ``supersedes_memory_id`` chain depth (0 when empty)."""
    conn = _ro_conn_or_none(long_term_db)
    if conn is None:
        return 0.0
    try:
        rows = conn.execute(
            "SELECT memory_id, supersedes_memory_id FROM long_term_memory"
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    by_id = {str(row["memory_id"]): row["supersedes_memory_id"] for row in rows}
    depths: dict[str, int] = {}

    def _depth(memory_id: str) -> int:
        if memory_id in depths:
            return depths[memory_id]
        parent = by_id.get(memory_id)
        depth = 0 if parent is None else 1 + _depth(str(parent))
        depths[memory_id] = depth
        return depth

    lengths = [_depth(memory_id) for memory_id in by_id]
    return (sum(lengths) / len(lengths)) if lengths else 0.0


def reflection_latency_stats(long_term_db: str | Path) -> dict[str, Any]:
    """p50 completed-run latency (seconds) + total model tokens.

    Token usage is read from ``long_term_audit`` kind=``reflection_usage``
    (``tokens=N``) — the v1 usage channel recorded by the reflection runner
    outside any transaction.  Completed runs without a usage record (offline
    no-model runs, reason ``no_model_port_offline`` — the reason is not
    persisted, so the usage-audit link via ``digest_prefix`` is the
    discriminator) are excluded from the p50/runs statistics (M9).
    Missing/empty DB → zeros.
    """
    conn = _ro_conn_or_none(long_term_db)
    if conn is None:
        return {"p50_sec": 0.0, "total_tokens": 0, "runs": 0}
    latencies: list[float] = []
    tokens = 0
    try:
        runs = conn.execute(
            "SELECT created_at, updated_at, snapshot_digest FROM reflection_run "
            "WHERE status='completed'"
        ).fetchall()
        usage_prefixes = {
            str(row["digest_prefix"])
            for row in conn.execute(
                "SELECT digest_prefix FROM long_term_audit "
                "WHERE kind='reflection_usage'"
            ).fetchall()
        }
        for row in runs:
            if str(row["snapshot_digest"])[:8] not in usage_prefixes:
                # offline no-model completed run (no usage audit) — excluded
                continue
            try:
                start = datetime.fromisoformat(str(row["created_at"]))
                end = datetime.fromisoformat(str(row["updated_at"]))
                latencies.append(max(0.0, (end - start).total_seconds()))
            except (ValueError, TypeError):
                continue
        usage_rows = conn.execute(
            "SELECT reason FROM long_term_audit WHERE kind='reflection_usage'"
        ).fetchall()
        for row in usage_rows:
            match = re.search(r"tokens=(\d+)", str(row["reason"]))
            if match:
                tokens += int(match.group(1))
    except sqlite3.Error:
        return {"p50_sec": 0.0, "total_tokens": 0, "runs": 0}
    finally:
        conn.close()
    return {
        "p50_sec": statistics.median(latencies) if latencies else 0.0,
        "total_tokens": tokens,
        "runs": len(latencies),
    }


def terminal_required() -> bool:
    """This evaluator only runs after run terminal (post-hoc truth)."""
    return True


# ── artifact ────────────────────────────────────────────────────────────────

def evaluate_long_term_memory_quality(
    results_dir: str | Path,
    truth_manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate the run-local long-term DB and write the quality artifact.

    Output: ``<results_dir>/long_term_memory_quality.json``.  The source /
    run / project databases are opened read-only at most — never written
    (their mtimes are unchanged).  A provided ``truth_manifest`` must carry a
    non-empty ``scope_id`` (typed :class:`LongTermQualityError` otherwise).
    """
    results_dir = Path(results_dir)
    scope_id: str | None = None
    if truth_manifest is not None:
        manifest_path = Path(truth_manifest)
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise LongTermQualityError(
                "invalid_manifest", "truth manifest must be a JSON object"
            )
        candidate = manifest.get("scope_id")
        if not isinstance(candidate, str) or not candidate:
            raise LongTermQualityError(
                "missing_scope_id", "truth manifest must contain scope_id"
            )
        scope_id = candidate

    long_term_path = _find_long_term_db(results_dir)
    if long_term_path is None:
        metrics: dict[str, Any] = {
            "source_traceability_rate": 0.0,
            "forbidden_truth_violation_count": 0,
            "same_key_conflict_rate": 0.0,
            "supersede_chain_length": 0.0,
            "reflection_latency_stats": {"p50_sec": 0.0, "total_tokens": 0, "runs": 0},
        }
        long_term_path_str: str | None = None
    else:
        source_path = _find_source_db(results_dir)
        metrics = {
            "source_traceability_rate": source_traceability_rate(
                long_term_path,
                source_path if source_path is not None else Path("__missing__"),
            ),
            "forbidden_truth_violation_count": forbidden_truth_violation_count(
                long_term_path
            ),
            "same_key_conflict_rate": same_key_conflict_rate(long_term_path),
            "supersede_chain_length": supersede_chain_length(long_term_path),
            "reflection_latency_stats": reflection_latency_stats(long_term_path),
        }
        long_term_path_str = str(long_term_path)

    artifact: dict[str, Any] = {
        "evaluator": "long_term_memory_quality",
        "evaluator_version": 1,
        "scope_id": scope_id,
        "terminal_required": True,
        "long_term_db": long_term_path_str,
        "metrics": metrics,
    }
    output = results_dir / "long_term_memory_quality.json"
    output.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return artifact
