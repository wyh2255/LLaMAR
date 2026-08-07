"""Phase 0 minimal Coordinator-owned canonical Memory store (stdlib SQLite).

Owns the schema and the durable fences that later phases build on: memory
scopes (with the closed-scope reuse rule), namespaced relations, the
control-transition journal mirror, and a redacted security audit.  It exposes
no control-plane mutation: nothing here can transition a PhysicalDispatch.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import (
    ControlTransitionJournalEntry,
    MemoryRef,
    MemoryRelation,
    MemoryScopeV1,
    MemoryScopeValidationError,
    digest_payload,
)

__all__ = [
    "MemoryCommandResult",
    "MemoryService",
    "MemoryStore",
    "ScopeActivationResult",
    "ScopeActivationStatus",
    "scope_tuple_reuse",
]

scope_tuple_reuse = "scope_tuple_reuse"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_scope (
    scope_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    runtime_epoch INTEGER NOT NULL,
    closed_at TEXT,
    UNIQUE(project_id, experiment_id, context_id, runtime_epoch)
);
CREATE TABLE IF NOT EXISTS memory_revision (
    scope_id TEXT PRIMARY KEY REFERENCES memory_scope(scope_id),
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_relation (
    relation_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_scope(scope_id),
    from_namespace TEXT NOT NULL CHECK(from_namespace IN ('memory','control')),
    from_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_namespace TEXT NOT NULL CHECK(to_namespace IN ('memory','control')),
    to_id TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    source_event_id TEXT,
    confidence REAL
);
CREATE INDEX IF NOT EXISTS ix_memory_relation_scope ON memory_relation(scope_id);
CREATE TABLE IF NOT EXISTS control_transition_journal (
    context_id TEXT NOT NULL,
    runtime_epoch INTEGER NOT NULL,
    dispatch_id TEXT NOT NULL,
    control_revision INTEGER NOT NULL,
    previous_state TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    result_digest TEXT,
    journal_sha256 TEXT NOT NULL,
    PRIMARY KEY(context_id, runtime_epoch, dispatch_id, control_revision)
);
CREATE TABLE IF NOT EXISTS security_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    digest_prefix TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS callback_nonce (
    worker_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (worker_id, nonce)
);
CREATE INDEX IF NOT EXISTS ix_callback_nonce_expiry ON callback_nonce(expires_at);
CREATE TABLE IF NOT EXISTS temporal_event (
    event_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_scope(scope_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    logical_task_id TEXT,
    dispatch_id TEXT,
    worker_task_id TEXT,
    tool_call_id TEXT,
    success INTEGER,
    error TEXT,
    payload TEXT,
    causation_id TEXT,
    correlation_id TEXT,
    idempotency_key TEXT,
    supersedes_event_id TEXT,
    UNIQUE(scope_id, sequence),
    UNIQUE(scope_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_temporal_scope_seq ON temporal_event(scope_id, sequence);
CREATE INDEX IF NOT EXISTS ix_temporal_scope_dispatch ON temporal_event(scope_id, dispatch_id);
CREATE TABLE IF NOT EXISTS idempotency_ledger (
    scope_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_id TEXT,
    payload_digest TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    committed_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS control_receipt (
    scope_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    control_revision INTEGER NOT NULL,
    journal_sha256 TEXT NOT NULL,
    event_id TEXT,
    committed_revision INTEGER NOT NULL,
    PRIMARY KEY(scope_id, dispatch_id, control_revision)
);
CREATE TABLE IF NOT EXISTS memory_outbox (
    outbox_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    event_id TEXT,
    export_kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_scope ON memory_outbox(scope_id);
CREATE TABLE IF NOT EXISTS spatial_entity (
    scope_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    as_of_sequence INTEGER,
    PRIMARY KEY (scope_id, entity_id)
);
CREATE TABLE IF NOT EXISTS embodied_node (
    scope_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    as_of_sequence INTEGER,
    PRIMARY KEY (scope_id, node_id)
);
CREATE TABLE IF NOT EXISTS projection_field (
    scope_id TEXT NOT NULL,
    domain TEXT NOT NULL CHECK(domain IN ('spatial','embodied')),
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    field_name TEXT NOT NULL,
    value TEXT,
    env_step INTEGER,
    provenance TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    confidence REAL NOT NULL,
    event_id TEXT NOT NULL,
    evidence_id TEXT,
    sequence INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    conflict_event_ids TEXT,
    conflict_candidates TEXT,
    superseded_event_id TEXT,
    PRIMARY KEY (scope_id, domain, entity_id, field_name)
);
CREATE INDEX IF NOT EXISTS ix_projection_field_scope ON projection_field(scope_id);
CREATE INDEX IF NOT EXISTS ix_projection_field_event ON projection_field(event_id);
CREATE TABLE IF NOT EXISTS projection_outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    evidence_id TEXT,
    domain TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    superseded_event_id TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_projection_outcome_scope ON projection_outcome(scope_id);
CREATE TABLE IF NOT EXISTS memory_view_revision (
    scope_id TEXT PRIMARY KEY REFERENCES memory_scope(scope_id),
    snapshot_revision INTEGER NOT NULL DEFAULT 0
);
"""


class ScopeActivationStatus(str, Enum):
    ACTIVE = "active"
    SCOPE_TUPLE_REUSE = "scope_tuple_reuse"


@dataclass(frozen=True)
class ScopeActivationResult:
    status: ScopeActivationStatus
    scope_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class MemoryCommandResult:
    ok: bool
    error: str | None = None
    value: Any = None


def _row_to_relation(row: sqlite3.Row) -> MemoryRelation:
    data = dict(row)
    return MemoryRelation(
        relation_id=data["relation_id"],
        scope_id=data["scope_id"],
        from_ref=MemoryRef(namespace=data["from_namespace"], id=data["from_id"]),
        relation_type=data["relation_type"],
        to_ref=MemoryRef(namespace=data["to_namespace"], id=data["to_id"]),
        valid_from=data.get("valid_from"),
        valid_to=data.get("valid_to"),
        source_event_id=data.get("source_event_id"),
        confidence=data.get("confidence", 1.0),
    )


def _row_to_journal_entry(row: sqlite3.Row) -> ControlTransitionJournalEntry:
    data = dict(row)
    return ControlTransitionJournalEntry(
        context_id=data["context_id"],
        runtime_epoch=data["runtime_epoch"],
        dispatch_id=data["dispatch_id"],
        control_revision=data["control_revision"],
        previous_state=data["previous_state"],
        state=data["state"],
        source=data["source"],
        observed_at=data["observed_at"],
        result_digest=data["result_digest"],
        journal_sha256=data["journal_sha256"],
    )


class MemoryStore:
    """Coordinator-owned SQLite store for canonical Memory (Phase 0 scaffold)."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._db_path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA user_version=3")
        self._conn.executescript(_SCHEMA)

    # ── lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _begin_immediate(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ── scope lifecycle ────────────────────────────────────────────────

    def activate_scope(self, scope: MemoryScopeV1) -> ScopeActivationResult:
        try:
            scope.validate()
        except MemoryScopeValidationError as exc:
            self.record_security_audit(
                "scope_rejected",
                reason=str(exc),
                digest_prefix=self._scope_digest_prefix(scope),
            )
            raise
        scope_id = scope.scope_id
        with self._lock, self._begin_immediate():
            row = self._fetch_scope_by_tuple(scope)
            if row is not None:
                if row["closed_at"] is not None:
                    return ScopeActivationResult(
                        ScopeActivationStatus.SCOPE_TUPLE_REUSE,
                        scope_id=scope_id,
                        reason=scope_tuple_reuse,
                    )
                return ScopeActivationResult(
                    ScopeActivationStatus.ACTIVE, scope_id=row["scope_id"]
                )
            self._conn.execute(
                "INSERT INTO memory_scope "
                "(scope_id, project_id, experiment_id, context_id, "
                "runtime_epoch, closed_at) VALUES (?,?,?,?,?,NULL)",
                (
                    scope_id,
                    scope.project_id,
                    scope.experiment_id,
                    scope.context_id,
                    scope.runtime_epoch,
                ),
            )
            self._conn.execute(
                "INSERT INTO memory_revision (scope_id, revision) VALUES (?,0)",
                (scope_id,),
            )
        return ScopeActivationResult(ScopeActivationStatus.ACTIVE, scope_id=scope_id)

    def _fetch_scope_by_tuple(self, scope: MemoryScopeV1) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT scope_id, closed_at FROM memory_scope "
            "WHERE project_id=? AND experiment_id=? AND context_id=? "
            "AND runtime_epoch=?",
            (
                scope.project_id,
                scope.experiment_id,
                scope.context_id,
                scope.runtime_epoch,
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_scope(self, scope_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT scope_id, project_id, experiment_id, context_id, "
            "runtime_epoch, closed_at FROM memory_scope WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_scopes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT scope_id, closed_at FROM memory_scope ORDER BY scope_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def close_scope(self, scope_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memory_scope SET closed_at=? "
                "WHERE scope_id=? AND closed_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), scope_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ── revision ───────────────────────────────────────────────────────

    def revision_of(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT revision FROM memory_revision WHERE scope_id=?", (scope_id,)
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def bump_revision(self, scope_id: str) -> int:
        with self._lock:
            with self._begin_immediate():
                row = self._conn.execute(
                    "SELECT revision FROM memory_revision WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()
                current = int(row["revision"]) if row is not None else 0
                next_revision = current + 1
                self._conn.execute(
                    "INSERT INTO memory_revision (scope_id, revision) VALUES (?,?) "
                    "ON CONFLICT(scope_id) DO UPDATE SET revision=excluded.revision",
                    (scope_id, next_revision),
                )
            return next_revision

    # ── relations ──────────────────────────────────────────────────────

    def add_relation(self, relation: MemoryRelation) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_relation "
                "(relation_id, scope_id, from_namespace, from_id, relation_type, "
                "to_namespace, to_id, valid_from, valid_to, source_event_id, "
                "confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    relation.relation_id,
                    relation.scope_id,
                    relation.from_ref.namespace,
                    relation.from_ref.id,
                    relation.relation_type,
                    relation.to_ref.namespace,
                    relation.to_ref.id,
                    relation.valid_from,
                    relation.valid_to,
                    relation.source_event_id,
                    relation.confidence,
                ),
            )
            self._conn.commit()

    def relations_for_scope(self, scope_id: str) -> list[MemoryRelation]:
        rows = self._conn.execute(
            "SELECT * FROM memory_relation WHERE scope_id=? ORDER BY relation_id",
            (scope_id,),
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def relations_for_entity(self, ref: MemoryRef) -> list[MemoryRelation]:
        rows = self._conn.execute(
            "SELECT * FROM memory_relation "
            "WHERE (from_namespace=? AND from_id=?) OR (to_namespace=? AND to_id=?) "
            "ORDER BY relation_id",
            (ref.namespace, ref.id, ref.namespace, ref.id),
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    # ── control transition journal mirror ──────────────────────────────

    def record_journal_entry(self, entry: ControlTransitionJournalEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO control_transition_journal "
                "(context_id, runtime_epoch, dispatch_id, control_revision, "
                "previous_state, state, source, observed_at, result_digest, "
                "journal_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.context_id,
                    entry.runtime_epoch,
                    entry.dispatch_id,
                    entry.control_revision,
                    entry.previous_state,
                    entry.state,
                    entry.source,
                    entry.observed_at,
                    entry.result_digest,
                    entry.journal_sha256,
                ),
            )
            self._conn.commit()

    def control_journal_entries(
        self,
        *,
        context_id: str | None = None,
        runtime_epoch: int | None = None,
        dispatch_id: str | None = None,
    ) -> list[ControlTransitionJournalEntry]:
        sql = "SELECT * FROM control_transition_journal WHERE 1=1"
        params: list[Any] = []
        if context_id is not None:
            sql += " AND context_id=?"
            params.append(context_id)
        if runtime_epoch is not None:
            sql += " AND runtime_epoch=?"
            params.append(runtime_epoch)
        if dispatch_id is not None:
            sql += " AND dispatch_id=?"
            params.append(dispatch_id)
        sql += " ORDER BY runtime_epoch, dispatch_id, control_revision"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_journal_entry(row) for row in rows]

    # ── security audit ─────────────────────────────────────────────────

    def _scope_digest_prefix(self, scope: MemoryScopeV1) -> str:
        return digest_payload(
            {
                "project_id": scope.project_id,
                "experiment_id": scope.experiment_id,
                "context_id": scope.context_id,
                "runtime_epoch": scope.runtime_epoch,
            }
        )[:16]

    def record_security_audit(
        self, kind: str, reason: str, digest_prefix: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO security_audit (kind, reason, digest_prefix, at) "
                "VALUES (?,?,?,?)",
                (
                    kind,
                    reason,
                    digest_prefix,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()

    def security_audit_entries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, reason, digest_prefix FROM security_audit ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Phase 2: durable nonce reservation ──────────────────────────────

    def reserve_callback_nonce(
        self, worker_id: str, nonce: str, expires_at: float
    ) -> bool:
        """Du rably claim a callback nonce; False on replay.

        The claim is a short BEGIN IMMEDIATE transaction: expired entries are
        purged first, then the ``UNIQUE(worker_id, nonce)`` insert either wins
        or reports a replay.  No domain record is ever touched here.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM callback_nonce WHERE expires_at <= ?",
                    (time.time(),),
                )
                try:
                    self._conn.execute(
                        "INSERT INTO callback_nonce (worker_id, nonce, expires_at) "
                        "VALUES (?,?,?)",
                        (worker_id, nonce, expires_at),
                    )
                    self._conn.execute("COMMIT")
                    return True
                except sqlite3.IntegrityError:
                    self._conn.execute("ROLLBACK")
                    return False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Phase 2: canonical bundle transaction ───────────────────────────

    @contextlib.contextmanager
    def canonical_transaction(self) -> Iterator[MemoryStore]:
        """Open the single canonical SQLite transaction (BEGIN IMMEDIATE).

        Receipt + event + projection/revision + outbox are either all committed
        or all rolled back.  Callers must not perform network / LLM / file
        export inside this block.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # Transaction-scoped primitives — must be called inside canonical_transaction().

    def scope_closed(self, scope_id: str) -> bool:
        row = self._conn.execute(
            "SELECT closed_at FROM memory_scope WHERE scope_id=?", (scope_id,)
        ).fetchone()
        return row is not None and row["closed_at"] is not None

    def scope_exists(self, scope_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memory_scope WHERE scope_id=?", (scope_id,)
        ).fetchone()
        return row is not None

    def claim_callback_idempotency(
        self, scope_id: str, idempotency_key: str, payload_digest: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Claim a callback idempotency row within the open transaction.

        Returns ``("fresh", None)`` or ``("duplicate", row)`` / ``("conflict",
        row)``.  ``conflict`` means the same key arrived with a different
        canonical payload digest and must fail-loud.
        """
        row = self._conn.execute(
            "SELECT event_id, payload_digest, receipt_sha256, committed_revision "
            "FROM idempotency_ledger WHERE scope_id=? AND idempotency_key=?",
            (scope_id, idempotency_key),
        ).fetchone()
        if row is not None:
            data = dict(row)
            if data["payload_digest"] != payload_digest:
                return "conflict", data
            return "duplicate", data
        return "fresh", None

    def claim_control_receipt(
        self,
        scope_id: str,
        dispatch_id: str,
        control_revision: int,
        journal_sha256: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Claim a control receipt within the open canonical transaction.

        Returns ``("fresh", None)``, ``("duplicate", row)`` or ``("conflict",
        row)``.  A journal digest mismatch fails loud with zero partial writes.
        """
        row = self._conn.execute(
            "SELECT event_id, journal_sha256, committed_revision FROM control_receipt "
            "WHERE scope_id=? AND dispatch_id=? AND control_revision=?",
            (scope_id, dispatch_id, control_revision),
        ).fetchone()
        if row is not None:
            data = dict(row)
            if data["journal_sha256"] != journal_sha256:
                return "conflict", data
            return "duplicate", data
        return "fresh", None

    def next_sequence(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(sequence) AS m FROM temporal_event WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        return int(row["m"]) + 1 if row is not None and row["m"] is not None else 1

    def append_temporal_event(
        self,
        *,
        event_id: str,
        scope_id: str,
        sequence: int,
        event_type: str,
        occurred_at: str,
        ingested_at: str,
        actor_id: str,
        logical_task_id: str | None,
        dispatch_id: str | None,
        worker_task_id: str | None,
        tool_call_id: str | None,
        success: bool | None,
        error: str | None,
        payload: str | None,
        causation_id: str | None,
        correlation_id: str | None,
        idempotency_key: str | None,
        supersedes_event_id: str | None = None,
    ) -> str:
        self._conn.execute(
            "INSERT INTO temporal_event "
            "(event_id, scope_id, sequence, event_type, occurred_at, ingested_at, "
            "actor_id, logical_task_id, dispatch_id, worker_task_id, tool_call_id, "
            "success, error, payload, causation_id, correlation_id, idempotency_key, "
            "supersedes_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                scope_id,
                sequence,
                event_type,
                occurred_at,
                ingested_at,
                actor_id,
                logical_task_id,
                dispatch_id,
                worker_task_id,
                tool_call_id,
                (1 if success is True else (0 if success is False else None)),
                error,
                payload,
                causation_id,
                correlation_id,
                idempotency_key,
                supersedes_event_id,
            ),
        )
        return event_id

    def insert_idempotency_ledger(
        self,
        *,
        scope_id: str,
        idempotency_key: str,
        event_id: str,
        payload_digest: str,
        receipt_sha256: str,
        committed_revision: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO idempotency_ledger "
            "(scope_id, idempotency_key, event_id, payload_digest, receipt_sha256, "
            "committed_revision, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                scope_id,
                idempotency_key,
                event_id,
                payload_digest,
                receipt_sha256,
                committed_revision,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def insert_control_receipt(
        self,
        *,
        scope_id: str,
        dispatch_id: str,
        control_revision: int,
        journal_sha256: str,
        event_id: str,
        committed_revision: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO control_receipt "
            "(scope_id, dispatch_id, control_revision, journal_sha256, event_id, "
            "committed_revision) VALUES (?,?,?,?,?,?)",
            (
                scope_id,
                dispatch_id,
                control_revision,
                journal_sha256,
                event_id,
                committed_revision,
            ),
        )

    def write_outbox(
        self,
        *,
        outbox_id: str,
        scope_id: str,
        event_id: str | None,
        export_kind: str,
        payload_sha256: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO memory_outbox "
            "(outbox_id, scope_id, event_id, export_kind, payload_sha256, status, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (
                outbox_id,
                scope_id,
                event_id,
                export_kind,
                payload_sha256,
                "pending",
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def bump_revision_in_tx(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT revision FROM memory_revision WHERE scope_id=?", (scope_id,)
        ).fetchone()
        current = int(row["revision"]) if row is not None else 0
        next_revision = current + 1
        self._conn.execute(
            "INSERT INTO memory_revision (scope_id, revision) VALUES (?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET revision=excluded.revision",
            (scope_id, next_revision),
        )
        return next_revision

    # ── Phase 2: read-back helpers (outside any transaction) ────────────

    def temporal_events(self, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM temporal_event WHERE scope_id=? ORDER BY sequence",
            (scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def temporal_event_count(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM temporal_event WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def idempotency_receipt(
        self, scope_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT event_id, payload_digest, receipt_sha256, committed_revision "
            "FROM idempotency_ledger WHERE scope_id=? AND idempotency_key=?",
            (scope_id, idempotency_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def control_receipt_for(
        self, scope_id: str, dispatch_id: str, control_revision: int
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT event_id, journal_sha256, committed_revision FROM control_receipt "
            "WHERE scope_id=? AND dispatch_id=? AND control_revision=?",
            (scope_id, dispatch_id, control_revision),
        ).fetchone()
        return dict(row) if row is not None else None

    def control_receipts(self, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM control_receipt WHERE scope_id=? "
            "ORDER BY dispatch_id, control_revision",
            (scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def outbox_entries(self, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_outbox WHERE scope_id=? ORDER BY created_at",
            (scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def outbox_entry(self, outbox_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM memory_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    # ── Phase 3: projection primitives (tx-scoped, no commit) ───────────────

    def get_projection_field(
        self, scope_id: str, domain: str, entity_id: str, field_name: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM projection_field "
            "WHERE scope_id=? AND domain=? AND entity_id=? AND field_name=?",
            (scope_id, domain, entity_id, field_name),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        value = data.get("value")
        data["value"] = json.loads(value) if value is not None else None
        conflict = data.get("conflict_event_ids")
        data["conflict_event_ids"] = json.loads(conflict) if conflict else []
        candidates = data.get("conflict_candidates")
        data["conflict_candidates"] = (
            json.loads(candidates) if candidates else []
        )
        return data

    def upsert_projection_field(
        self,
        *,
        scope_id: str,
        domain: str,
        entity_id: str,
        entity_type: str,
        field_name: str,
        value: str,
        env_step: int | None,
        provenance: str,
        source_priority: int,
        confidence: float,
        event_id: str,
        evidence_id: str | None,
        sequence: int,
        outcome: str,
        conflict_event_ids: str | None,
        conflict_candidates: str | None,
        superseded_event_id: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO projection_field "
            "(scope_id, domain, entity_id, entity_type, field_name, value, env_step, "
            "provenance, source_priority, confidence, event_id, evidence_id, sequence, "
            "outcome, conflict_event_ids, conflict_candidates, superseded_event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_id, domain, entity_id, field_name) DO UPDATE SET "
            "value=excluded.value, env_step=excluded.env_step, provenance=excluded.provenance, "
            "source_priority=excluded.source_priority, confidence=excluded.confidence, "
            "event_id=excluded.event_id, evidence_id=excluded.evidence_id, "
            "sequence=excluded.sequence, outcome=excluded.outcome, "
            "conflict_event_ids=excluded.conflict_event_ids, "
            "conflict_candidates=excluded.conflict_candidates, "
            "superseded_event_id=excluded.superseded_event_id",
            (
                scope_id,
                domain,
                entity_id,
                entity_type,
                field_name,
                value,
                env_step,
                provenance,
                source_priority,
                confidence,
                event_id,
                evidence_id,
                sequence,
                outcome,
                conflict_event_ids,
                conflict_candidates,
                superseded_event_id,
            ),
        )

    def bump_entity_revision(
        self,
        scope_id: str,
        domain: str,
        entity_id: str,
        entity_type: str,
        as_of_sequence: int | None,
    ) -> int:
        if domain == "spatial":
            table, id_col = "spatial_entity", "entity_id"
        else:
            table, id_col = "embodied_node", "node_id"
        row = self._conn.execute(
            f"SELECT revision FROM {table} WHERE scope_id=? AND {id_col}=?",
            (scope_id, entity_id),
        ).fetchone()
        revision = (int(row["revision"]) + 1) if row is not None else 1
        self._conn.execute(
            f"INSERT INTO {table} (scope_id, {id_col}, entity_type, revision, "
            f"as_of_sequence) VALUES (?,?,?,?,?) "
            f"ON CONFLICT(scope_id, {id_col}) DO UPDATE SET "
            f"entity_type=excluded.entity_type, revision=excluded.revision, "
            f"as_of_sequence=excluded.as_of_sequence",
            (scope_id, entity_id, entity_type, revision, as_of_sequence),
        )
        return revision

    def bump_view_revision(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT snapshot_revision FROM memory_view_revision WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        revision = (int(row["snapshot_revision"]) + 1) if row is not None else 1
        self._conn.execute(
            "INSERT INTO memory_view_revision (scope_id, snapshot_revision) VALUES (?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET snapshot_revision=excluded.snapshot_revision",
            (scope_id, revision),
        )
        return revision

    def record_projection_outcome(
        self,
        *,
        scope_id: str,
        event_id: str,
        evidence_id: str | None,
        domain: str,
        entity_id: str,
        field_name: str,
        outcome: str,
        superseded_event_id: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO projection_outcome "
            "(scope_id, event_id, evidence_id, domain, entity_id, field_name, outcome, "
            "superseded_event_id, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                scope_id,
                event_id,
                evidence_id,
                domain,
                entity_id,
                field_name,
                outcome,
                superseded_event_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def add_relation_in_tx(self, relation: MemoryRelation) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO memory_relation "
            "(relation_id, scope_id, from_namespace, from_id, relation_type, "
            "to_namespace, to_id, valid_from, valid_to, source_event_id, "
            "confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                relation.relation_id,
                relation.scope_id,
                relation.from_ref.namespace,
                relation.from_ref.id,
                relation.relation_type,
                relation.to_ref.namespace,
                relation.to_ref.id,
                relation.valid_from,
                relation.valid_to,
                relation.source_event_id,
                relation.confidence,
            ),
        )

    # ── Phase 3: projection read-back helpers (outside any transaction) ─────

    def projection_field(
        self, scope_id: str, domain: str, entity_id: str, field_name: str
    ) -> dict[str, Any] | None:
        with self._lock:
            return self.get_projection_field(scope_id, domain, entity_id, field_name)

    def projection_fields(self, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM projection_field WHERE scope_id=? ORDER BY domain, entity_id, field_name",
            (scope_id,),
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            value = data.get("value")
            data["value"] = json.loads(value) if value is not None else None
            conflict = data.get("conflict_event_ids")
            data["conflict_event_ids"] = json.loads(conflict) if conflict else []
            candidates = data.get("conflict_candidates")
            data["conflict_candidates"] = (
                json.loads(candidates) if candidates else []
            )
            out.append(data)
        return out

    def entity_revision_of(self, scope_id: str, domain: str, entity_id: str) -> int:
        table, id_col = (
            ("spatial_entity", "entity_id")
            if domain == "spatial"
            else ("embodied_node", "node_id")
        )
        row = self._conn.execute(
            f"SELECT revision FROM {table} WHERE scope_id=? AND {id_col}=?",
            (scope_id, entity_id),
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def entity_as_of_sequence(
        self, scope_id: str, domain: str, entity_id: str
    ) -> int | None:
        table, id_col = (
            ("spatial_entity", "entity_id")
            if domain == "spatial"
            else ("embodied_node", "node_id")
        )
        row = self._conn.execute(
            f"SELECT as_of_sequence FROM {table} WHERE scope_id=? AND {id_col}=?",
            (scope_id, entity_id),
        ).fetchone()
        if row is None or row["as_of_sequence"] is None:
            return None
        return int(row["as_of_sequence"])

    def view_revision_of(self, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT snapshot_revision FROM memory_view_revision WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        return int(row["snapshot_revision"]) if row is not None else 0

    def projection_outcomes(self, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM projection_outcome WHERE scope_id=? ORDER BY id",
            (scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def new_event_id(prefix: str = "evt") -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def new_outbox_id() -> str:
        return f"out_{uuid.uuid4().hex}"


class MemoryService:
    """Coordinator-internal facade that guarantees no control-plane mutation."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def _reject_control(self, ref: MemoryRef | None) -> MemoryCommandResult | None:
        if ref is not None and ref.namespace == "control":
            return MemoryCommandResult(ok=False, error="control_mutation_forbidden")
        return None

    def create(
        self,
        *,
        scope_id: str,
        domain: str,
        ref: MemoryRef | None = None,
        payload: Any = None,
    ) -> MemoryCommandResult:
        rejected = self._reject_control(ref)
        if rejected is not None:
            return rejected
        return MemoryCommandResult(ok=True, value=payload)

    def update(
        self,
        *,
        scope_id: str,
        domain: str,
        ref: MemoryRef,
        expected_revision: int | None = None,
        payload: Any = None,
    ) -> MemoryCommandResult:
        rejected = self._reject_control(ref)
        if rejected is not None:
            return rejected
        return MemoryCommandResult(ok=False, error="unsupported_update")

    def delete(
        self, *, scope_id: str, domain: str, ref: MemoryRef
    ) -> MemoryCommandResult:
        rejected = self._reject_control(ref)
        if rejected is not None:
            return rejected
        return MemoryCommandResult(ok=False, error="unsupported_delete")

    def read(self, *, scope_id: str, kind: str = "scope") -> MemoryCommandResult:
        if kind == "scope":
            return MemoryCommandResult(ok=True, value=self._store.get_scope(scope_id))
        if kind == "relations":
            return MemoryCommandResult(
                ok=True, value=self._store.relations_for_scope(scope_id)
            )
        return MemoryCommandResult(ok=True, value=None)
