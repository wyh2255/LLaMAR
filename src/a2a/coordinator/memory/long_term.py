"""Phase 1 run-local long-term memory kernel (independent of ``MemoryStore``).

Owns ``<memory_root>/long_term/long_term.sqlite3`` with its own schema and a
fail-closed migration runner (cross-run storage supplement §3).  Every row is
namespaced by ``project_id`` + ``scope_id``; all queries carry both.  The
store never touches the canonical run-local ``MemoryStore`` database.

Transaction contract: short ``BEGIN IMMEDIATE`` transactions with a bounded
busy retry; lock exhaustion surfaces as a typed retryable status
(``retryable_lock_busy``) with zero partial writes.  Model/network/export
calls never happen inside a transaction (P4 wiring keeps them outside).

Fault-injection / test helpers (``force_schema_version``,
``corrupt_migration_digest``, ``inject_failing_migration``,
``hold_write_lock``, ``seed_legacy_row_v1``, ``upgrade_to_latest``) exist to
drive the P0 contract surface; they are not part of the production trigger
path.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    LONG_TERM_MEMORY_KINDS,
    POLICY_VERSION,
    MemoryContractError,
    canonicalize_statement,
    derive_content_digest,
    digest_bytes,
    reflection_run_idempotency_key,
)

__all__ = [
    "LongTermMemoryStore",
    "LongTermStoreError",
    "LongTermStoreLockBusyError",
    "PublishMemoryResult",
    "ReflectionRunClaimResult",
]

#: WAL busy timeout per ``BEGIN IMMEDIATE`` attempt (milliseconds).
_BUSY_TIMEOUT_MS = 50
#: Bounded retry count for ``BEGIN IMMEDIATE`` before surfacing the typed
#: retryable status.
_BEGIN_IMMEDIATE_ATTEMPTS = 12

_REFERENCE_RUN_STATUSES = ("pending", "completed", "rejected", "failed", "timeout")


class LongTermStoreError(MemoryContractError):
    """Stable typed error for long-term store failures."""

    code = "long_term_store_error"


class LongTermStoreLockBusyError(LongTermStoreError):
    """``BEGIN IMMEDIATE`` exhausted its bounded retry — caller may retry.

    Raised only after every attempt failed; nothing was partially committed.
    """

    code = "lock_busy_retryable"


@dataclass(frozen=True)
class _Migration:
    """One schema migration: ordered statements + optional fault injection."""

    version: int
    statements: tuple[str, ...]
    fault_injected: bool = False
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "digest",
            digest_bytes("\n".join(self.statements).encode("utf-8")),
        )


_MIGRATION_001_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE long_term_revision (
        project_id TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, scope_id)
    )""",
    """CREATE TABLE reflection_run (
        run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        source_memory_revision INTEGER NOT NULL,
        snapshot_digest TEXT NOT NULL,
        policy_version INTEGER NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN ('pending','completed','rejected','failed','timeout')),
        window_end_sequence INTEGER NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, scope_id, source_memory_revision, snapshot_digest,
               policy_version)
    )""",
    """CREATE INDEX ix_reflection_run_scope
        ON reflection_run(project_id, scope_id)""",
    """CREATE TABLE long_term_memory (
        memory_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        memory_key TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        kind TEXT NOT NULL
            CHECK(kind IN ('strategy','lesson','hazard','pattern','status')),
        statement TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('candidate','published','superseded')),
        supersedes_memory_id TEXT,
        policy_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, scope_id, memory_key, content_digest)
    )""",
    """CREATE INDEX ix_long_term_memory_scope
        ON long_term_memory(project_id, scope_id, status)""",
    """CREATE TABLE long_term_support (
        memory_id TEXT NOT NULL REFERENCES long_term_memory(memory_id),
        source_scope_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        source_revision INTEGER,
        event_digest TEXT,
        PRIMARY KEY (memory_id, source_scope_id, source_event_id)
    )""",
    """CREATE TABLE long_term_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        reason TEXT NOT NULL,
        digest_prefix TEXT,
        at TEXT NOT NULL
    )""",
)

#: Single schema-version authority: version -> migration.  P4+ appends newer
#: versions here; ``inject_failing_migration`` registers fault-injected ones.
_MIGRATIONS: dict[int, _Migration] = {
    1: _Migration(version=1, statements=_MIGRATION_001_STATEMENTS),
}


@dataclass(frozen=True)
class PublishMemoryResult:
    """Outcome of :meth:`LongTermMemoryStore.publish_memory`."""

    status: str  # ok | duplicate | superseded | retryable_lock_busy
    memory_id: str | None = None
    supersedes_memory_id: str | None = None


@dataclass(frozen=True)
class ReflectionRunClaimResult:
    """Outcome of :meth:`LongTermMemoryStore.claim_reflection_run`."""

    status: str  # ok | duplicate | retryable_lock_busy
    run_id: str | None = None
    run_status: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_truth(text: str) -> str:
    """Redact FORBIDDEN_TRUTH_TERMS occurrences (audit never stores truth)."""
    for term in FORBIDDEN_TRUTH_TERMS:
        text = re.sub(re.escape(term), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


class LongTermMemoryStore:
    """Run-local independent long-term memory store (Phase 1 kernel).

    ``db_path`` is the run-local long-term file, normally derived by
    :attr:`LongTermMemoryConfig.long_term_db_path`; it must be an absolute
    local path.  :meth:`open` bootstraps the ``schema_migrations`` authority
    and applies pending migrations transactionally; unknown future versions
    and digest mismatches fail closed (refuse to open).
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._held_lock_conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── lifecycle ────────────────────────────────────────────────────────

    def open(self) -> LongTermMemoryStore:
        """Validate the path, connect, and run pending migrations.

        Raises :class:`LongTermStoreError` for relative/URI paths and for
        fail-closed migration states (unknown future version, digest
        mismatch, real migration failure).  Idempotent when already open.
        """
        with self._lock:
            if self._conn is not None:
                return self
            path = self._db_path
            if not path.is_absolute() or "://" in str(path):
                raise LongTermStoreError(
                    "invalid_db_path",
                    f"long-term db_path must be an absolute local path: {path!r}",
                )
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            conn = sqlite3.connect(
                str(path), isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            self._conn = conn
            try:
                self._ensure_version_table()
                self._run_migrations()
            except Exception:
                conn.close()
                self._conn = None
                raise
            return self

    def close(self) -> None:
        """Close the main connection and any fault-injected lock holder."""
        with self._lock:
            if self._held_lock_conn is not None:
                try:
                    self._held_lock_conn.close()
                except sqlite3.Error:
                    pass
                self._held_lock_conn = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── migration runner ─────────────────────────────────────────────────

    def _ensure_version_table(self) -> None:
        """Create ``schema_migrations`` before any migration runs.

        A database that already has business tables but no version table is
        inconsistent and fails closed.
        """
        conn = self._conn
        assert conn is not None
        has_version_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if has_version_table:
            return
        other_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ).fetchall()
        if other_tables:
            raise LongTermStoreError(
                "missing_version_table",
                "database has tables but no schema_migrations version table — "
                "inconsistent state; refusing to open",
            )
        with self._immediate():
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY,"
                " applied_at TEXT NOT NULL,"
                " migration_sha256 TEXT NOT NULL)"
            )

    def _applied_versions(self) -> list[int]:
        conn = self._conn
        assert conn is not None
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [int(row["version"]) for row in rows]

    def _run_migrations(self) -> list[int]:
        """Apply pending migrations; returns the newly applied versions.

        Fail-closed paths (all raise :class:`LongTermStoreError`):
        - an applied version unknown to this binary (unknown future version);
        - stored migration digest mismatch;
        - a real (non fault-injected) migration error mid-way → explicit
          ROLLBACK (never ``executescript`` as a transaction boundary).
        A fault-injected migration rolls back cleanly, is removed from the
        registry, and an audit entry is recorded; the store stays usable.
        """
        conn = self._conn
        assert conn is not None
        applied = self._applied_versions()
        for version in applied:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise LongTermStoreError(
                    "unknown_migration_version",
                    f"schema version {version} is not known to this binary "
                    f"(known: {sorted(_MIGRATIONS)}) — refusing to open",
                )
            stored = conn.execute(
                "SELECT migration_sha256 FROM schema_migrations WHERE version=?",
                (version,),
            ).fetchone()
            if stored is None or stored["migration_sha256"] != migration.digest:
                raise LongTermStoreError(
                    "migration_digest_mismatch",
                    f"schema migration {version} digest mismatch — refusing to open",
                )
        max_applied = max(applied) if applied else 0
        newly: list[int] = []
        for version in sorted(_MIGRATIONS):
            if version <= max_applied:
                continue
            migration = _MIGRATIONS[version]
            try:
                with self._immediate():
                    for statement in migration.statements:
                        conn.execute(statement)
                    if migration.fault_injected:
                        raise sqlite3.OperationalError(
                            f"injected fault: migration {version:03d} fails midway"
                        )
                    conn.execute(
                        "INSERT INTO schema_migrations "
                        "(version, applied_at, migration_sha256) VALUES (?,?,?)",
                        (version, _utc_now(), migration.digest),
                    )
            except sqlite3.Error as exc:
                if migration.fault_injected:
                    _MIGRATIONS.pop(version, None)
                    self.record_audit(
                        "migration_failed",
                        f"fault-injected migration {version:03d} rolled back "
                        f"cleanly: {exc}",
                        digest_prefix=migration.digest[:8],
                    )
                    continue
                raise LongTermStoreError(
                    "migration_failed",
                    f"schema migration {version:03d} failed and was rolled "
                    f"back: {exc}",
                ) from exc
            newly.append(version)
        return newly

    def upgrade_to_latest(self) -> list[int]:
        """Run pending migrations (idempotent); returns newly applied versions.

        Test/upgrade helper mirroring the open-time migration path.
        """
        with self._lock:
            self._require_open()
            return self._run_migrations()

    def applied_migrations(self) -> list[int]:
        """Applied schema versions in ascending order (exportable audit)."""
        with self._lock:
            self._require_open()
            return self._applied_versions()

    def migration_digests(self) -> dict[int, str]:
        """Known migration digest map (exportable for G2 audit)."""
        return {
            version: migration.digest
            for version, migration in sorted(_MIGRATIONS.items())
        }

    def list_tables(self) -> list[str]:
        """Business table names (excluding ``sqlite_%`` system tables)."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [str(row["name"]) for row in rows]

    # ── transactions ─────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _immediate(self) -> Iterator[None]:
        """Short ``BEGIN IMMEDIATE`` transaction with bounded busy retry.

        Lock exhaustion raises :class:`LongTermStoreLockBusyError` (typed
        retryable) — nothing is ever partially committed.  Model/network/
        export calls must happen outside this context.
        """
        conn = self._conn
        assert conn is not None
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_BEGIN_IMMEDIATE_ATTEMPTS):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                time.sleep(0.01 * (attempt + 1))
        else:
            raise LongTermStoreLockBusyError(
                "lock_busy_retryable",
                f"BEGIN IMMEDIATE busy after {_BEGIN_IMMEDIATE_ATTEMPTS} "
                f"attempts: {last_error}",
            )
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _require_open(self) -> None:
        if self._conn is None:
            raise LongTermStoreError("store_not_open", "store must be open()ed first")

    # ── reflection-run state machine ─────────────────────────────────────

    def claim_reflection_run(
        self,
        *,
        project_id: str,
        scope_id: str,
        source_memory_revision: int,
        snapshot_digest: str,
        policy_version: int,
        window_end_sequence: int,
    ) -> ReflectionRunClaimResult:
        """Exactly-once claim of a reflection run for one source snapshot.

        The idempotency key binds project_id + scope_id + source revision +
        snapshot digest + policy version: a retried claim of the same snapshot
        returns ``duplicate`` (zero new rows).  Lock exhaustion returns
        ``retryable_lock_busy``.
        """
        idempotency_key = reflection_run_idempotency_key(
            project_id=project_id,
            source_scope_id=scope_id,
            memory_revision=source_memory_revision,
            snapshot_digest=snapshot_digest,
            policy_version=policy_version,
        )
        self._require_open()
        try:
            with self._lock, self._immediate():
                row = self._conn.execute(
                    "SELECT run_id, status FROM reflection_run "
                    "WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    return ReflectionRunClaimResult(
                        status="duplicate",
                        run_id=row["run_id"],
                        run_status=row["status"],
                    )
                run_id = uuid.uuid4().hex
                now = _utc_now()
                self._conn.execute(
                    "INSERT INTO reflection_run (run_id, project_id, scope_id, "
                    "idempotency_key, source_memory_revision, snapshot_digest, "
                    "policy_version, status, window_end_sequence, truncated, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        project_id,
                        scope_id,
                        idempotency_key,
                        source_memory_revision,
                        snapshot_digest,
                        policy_version,
                        "pending",
                        window_end_sequence,
                        0,
                        now,
                        now,
                    ),
                )
            return ReflectionRunClaimResult(
                status="ok", run_id=run_id, run_status="pending"
            )
        except LongTermStoreLockBusyError:
            return ReflectionRunClaimResult(status="retryable_lock_busy")
        except sqlite3.IntegrityError:
            # F5: 并发窗口内同 idempotency_key 的 INSERT 撞 UNIQUE 约束
            # （SELECT 未见、INSERT 冲突）→ typed duplicate，零部分写入；
            # 与 publish 路径的幂等语义一致，绝不裸抛 IntegrityError。
            return ReflectionRunClaimResult(status="duplicate")

    def mark_reflection_run(self, run_id: str, status: str) -> None:
        """Transition a reflection run to ``status`` (typed, validated).

        The window cursor is derived as the max ``window_end_sequence`` over
        ``completed`` runs, so only ``completed`` advances it; rejected /
        failed / timeout runs never advance (main plan §3.4.3).
        """
        if status not in _REFERENCE_RUN_STATUSES:
            raise LongTermStoreError(
                "invalid_run_status",
                f"status must be one of {_REFERENCE_RUN_STATUSES!r}, "
                f"got {status!r}",
            )
        with self._lock, self._immediate():
            self._require_open()
            row = self._conn.execute(
                "SELECT status FROM reflection_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise LongTermStoreError(
                    "unknown_run", f"no reflection_run with run_id={run_id!r}"
                )
            # F6: completed 是终态（窗口游标 = MAX(window_end_sequence) WHERE
            # status='completed'）——离开 completed 会使派生游标回退，fail-closed
            # 直接拒绝；completed → completed 保持幂等允许。
            if row["status"] == "completed" and status != "completed":
                raise LongTermStoreError(
                    "invalid_run_status_transition",
                    f"cannot leave status 'completed' for run_id={run_id!r}",
                )
            self._conn.execute(
                "UPDATE reflection_run SET status=?, updated_at=? WHERE run_id=?",
                (status, _utc_now(), run_id),
            )

    def window_end_sequence(self, *, project_id: str, scope_id: str) -> int:
        """Last completed reflection window cursor for a scope (0 if none)."""
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT COALESCE(MAX(window_end_sequence), 0) AS cursor_value "
                "FROM reflection_run WHERE project_id=? AND scope_id=? "
                "AND status='completed'",
                (project_id, scope_id),
            ).fetchone()
            return int(row["cursor_value"])

    # ── memory publish / supersede ───────────────────────────────────────

    def publish_memory(
        self,
        *,
        project_id: str,
        scope_id: str,
        memory_key: str,
        statement: str,
        source_refs: (
            list[tuple[str, str] | tuple[str, str, int | None, str | None]]
            | tuple[tuple[str, str] | tuple[str, str, int | None, str | None], ...]
        ) = (),
        kind: str = "lesson",
        confidence: float = 1.0,
        policy_version: int = POLICY_VERSION,
    ) -> PublishMemoryResult:
        """Publish a validated memory (D6) or no-op on idempotent duplicate.

        - same key + same content digest → ``duplicate``, zero writes;
        - same key + new digest → new row supersedes the old one (explicit
          ``supersedes_memory_id``; old row → ``superseded``);
        - long-term revision increments on every content write;
        - lock exhaustion → ``retryable_lock_busy``, zero partial writes.

        G2-3（可选列语义）：``source_refs`` 每项既可以是二元组
        ``(source_scope_id, source_event_id)``，也可以是四元组
        ``(source_scope_id, source_event_id, source_revision, event_digest)``。
        四元组把 ``long_term_support.source_revision`` / ``event_digest`` 一并
        落库；二元组保持两列 NULL（合法，不报错）。探索结论（P5）：
        ``source_revision`` 已由 reflection publish 调用点回填
        （snapshot.memory_revision）；``event_digest`` 为可选列（canonical
        ``temporal_event`` 表无 per-event digest 字段）。
        """
        if kind not in LONG_TERM_MEMORY_KINDS:
            raise MemoryContractError(
                "invalid_kind",
                f"kind must be one of {LONG_TERM_MEMORY_KINDS!r}, got {kind!r}",
            )
        # F4: store 边界校验（与 LongTermMemoryCandidateV1.validate 同语义）——
        # confidence 必须是 [0,1] 数值、memory_key 非空，否则 typed 拒绝、绝不落库。
        if not isinstance(confidence, (int, float)) or not (
            0.0 <= confidence <= 1.0
        ):
            raise MemoryContractError(
                "invalid_confidence",
                f"confidence must be within [0,1], got {confidence!r}",
            )
        if not isinstance(memory_key, str) or not memory_key.strip():
            raise MemoryContractError(
                "invalid_memory_key",
                "memory_key must be a non-empty string",
            )
        content_digest = derive_content_digest(statement)
        try:
            with self._lock, self._immediate():
                self._require_open()
                existing = self._conn.execute(
                    "SELECT memory_id, content_digest FROM long_term_memory "
                    "WHERE project_id=? AND scope_id=? AND memory_key=? "
                    "AND status IN ('published','superseded') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (project_id, scope_id, memory_key),
                ).fetchone()
                if existing is not None and existing["content_digest"] == content_digest:
                    return PublishMemoryResult(
                        status="duplicate", memory_id=existing["memory_id"]
                    )
                memory_id = uuid.uuid4().hex
                now = _utc_now()
                supersedes_memory_id = (
                    existing["memory_id"] if existing is not None else None
                )
                self._conn.execute(
                    "INSERT INTO long_term_memory (memory_id, project_id, "
                    "scope_id, memory_key, content_digest, kind, statement, "
                    "confidence, status, supersedes_memory_id, policy_version, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        project_id,
                        scope_id,
                        memory_key,
                        content_digest,
                        kind,
                        canonicalize_statement(statement),
                        confidence,
                        "published",
                        supersedes_memory_id,
                        policy_version,
                        now,
                        now,
                    ),
                )
                if supersedes_memory_id is not None:
                    self._conn.execute(
                        "UPDATE long_term_memory SET status='superseded', "
                        "updated_at=? WHERE memory_id=?",
                        (now, supersedes_memory_id),
                    )
                for ref in source_refs:
                    if len(ref) not in (2, 4):
                        raise MemoryContractError(
                            "invalid_source_ref",
                            "source_ref entries must be (scope_id, event_id) "
                            "or (scope_id, event_id, source_revision, "
                            f"event_digest) tuples, got {ref!r}",
                        )
                    source_scope_id, source_event_id = ref[0], ref[1]
                    # G2-3: source_revision / event_digest 是可选回填列——
                    # 四元组携带则落库，二元组保持 NULL（合法）。主键仍是
                    # (memory_id, source_scope_id, source_event_id)。
                    source_revision = ref[2] if len(ref) == 4 else None
                    event_digest = ref[3] if len(ref) == 4 else None
                    self._conn.execute(
                        "INSERT INTO long_term_support (memory_id, "
                        "source_scope_id, source_event_id, source_revision, "
                        "event_digest) VALUES (?,?,?,?,?)",
                        (
                            memory_id,
                            source_scope_id,
                            source_event_id,
                            source_revision,
                            event_digest,
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO long_term_revision (project_id, scope_id, "
                    "revision, updated_at) VALUES (?,?,1,?) "
                    "ON CONFLICT(project_id, scope_id) DO UPDATE SET "
                    "revision = revision + 1, updated_at = excluded.updated_at",
                    (project_id, scope_id, now),
                )
            return PublishMemoryResult(
                status="superseded" if supersedes_memory_id else "ok",
                memory_id=memory_id,
                supersedes_memory_id=supersedes_memory_id,
            )
        except LongTermStoreLockBusyError:
            return PublishMemoryResult(status="retryable_lock_busy")
        except sqlite3.IntegrityError:
            # Content for this (key, digest) already persisted elsewhere
            # (e.g. candidate audit row): idempotent, zero new write.
            return PublishMemoryResult(status="duplicate")

    # ── read API ─────────────────────────────────────────────────────────

    def list_memories(self, *, project_id: str, scope_id: str) -> list[dict[str, Any]]:
        """All long-term memories of one project scope (candidate/published/
        superseded), oldest first."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM long_term_memory WHERE project_id=? AND scope_id=? "
                "ORDER BY created_at, memory_id",
                (project_id, scope_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def fetch_memory(
        self,
        scope_id: str,
        memory_key: str,
        *,
        project_id: str = "llamar",
    ) -> dict[str, Any] | None:
        """Fetch one memory row by scope + key (default project ``llamar``)."""
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT * FROM long_term_memory WHERE project_id=? AND scope_id=? "
                "AND memory_key=?",
                (project_id, scope_id, memory_key),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_support_refs(
        self, *, project_id: str, scope_id: str, memory_key: str
    ) -> list[tuple[str, str]]:
        """Replayable evidence chain: (source_scope_id, source_event_id) pairs."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT s.source_scope_id, s.source_event_id "
                "FROM long_term_support s "
                "JOIN long_term_memory m ON m.memory_id = s.memory_id "
                "WHERE m.project_id=? AND m.scope_id=? AND m.memory_key=? "
                "ORDER BY s.source_scope_id, s.source_event_id",
                (project_id, scope_id, memory_key),
            ).fetchall()
            return [
                (str(row["source_scope_id"]), str(row["source_event_id"]))
                for row in rows
            ]

    def list_support_rows(
        self, *, project_id: str, scope_id: str, memory_key: str
    ) -> list[dict[str, Any]]:
        """Full ``long_term_support`` rows, including the OPTIONAL backfill
        columns ``source_revision`` / ``event_digest`` (G2-3 可选列语义).

        The 2-tuple ``list_support_refs`` shape is preserved for existing
        consumers; this method exposes the complete row for audit / backfill
        verification.  Columns stay ``None`` when the entry was published
        without the extended 4-tuple.
        """
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT s.memory_id, s.source_scope_id, s.source_event_id, "
                "s.source_revision, s.event_digest "
                "FROM long_term_support s "
                "JOIN long_term_memory m ON m.memory_id = s.memory_id "
                "WHERE m.project_id=? AND m.scope_id=? AND m.memory_key=? "
                "ORDER BY s.source_scope_id, s.source_event_id",
                (project_id, scope_id, memory_key),
            ).fetchall()
            return [dict(row) for row in rows]

    def published_memories(
        self, *, project_id: str, scope_id: str
    ) -> list[dict[str, Any]]:
        """Published-only long-term memories, oldest first (Phase 5 #1 read).

        ``supersede`` flips the previous row to ``superseded``, so
        ``status='published'`` is exactly the current effective set for the
        coordinator read port.  Ordered by ``created_at`` then ``memory_id``
        for a stable coordinator summary.
        """
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM long_term_memory WHERE project_id=? AND scope_id=? "
                "AND status='published' ORDER BY created_at, memory_id",
                (project_id, scope_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def revision_of(self, *, project_id: str, scope_id: str) -> int:
        """Current long-term revision of a scope (0 when never written).

        Phase 5 #3: the coordinator's Environment State freshness carries
        ``long_term_revision`` so a long-term write at the same env step is
        observable metadata, independent of the memory projection revision.
        """
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT revision FROM long_term_revision "
                "WHERE project_id=? AND scope_id=?",
                (project_id, scope_id),
            ).fetchone()
            return int(row["revision"]) if row is not None else 0

    def list_reflection_runs(
        self, *, project_id: str, scope_id: str
    ) -> list[dict[str, Any]]:
        """Reflection runs of one project scope, oldest first."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM reflection_run WHERE project_id=? AND scope_id=? "
                "ORDER BY created_at, run_id",
                (project_id, scope_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_audit_entries(self) -> list[dict[str, Any]]:
        """Append-only audit entries, oldest first (redacted reasons)."""
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT id, kind, reason, digest_prefix, at "
                "FROM long_term_audit ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def record_audit(
        self, kind: str, reason: str, digest_prefix: str | None = None
    ) -> None:
        """Append an audit entry; ``reason`` is redacted against
        FORBIDDEN_TRUTH_TERMS (raw prompt / CoT / secret / truth never stored)."""
        with self._lock, self._immediate():
            self._require_open()
            self._conn.execute(
                "INSERT INTO long_term_audit (kind, reason, digest_prefix, at) "
                "VALUES (?,?,?,?)",
                (kind, _redact_truth(reason), digest_prefix, _utc_now()),
            )

    # ── fault-injection / test helpers (P0 contract surface) ─────────────

    def force_schema_version(self, version: int) -> None:
        """Test helper: replace applied-version records with ``version``.

        Simulates a future/unknown applied version; the next ``open()`` must
        fail closed.
        """
        with self._lock, self._immediate():
            self._require_open()
            self._conn.execute("DELETE FROM schema_migrations")
            self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, "
                "migration_sha256) VALUES (?,?,?)",
                (version, _utc_now(), digest_bytes(str(version).encode("utf-8"))),
            )

    def corrupt_migration_digest(self, version: int) -> None:
        """Test helper: corrupt the stored digest of an applied migration."""
        with self._lock, self._immediate():
            self._require_open()
            self._conn.execute(
                "UPDATE schema_migrations SET migration_sha256=? WHERE version=?",
                ("0" * 64, version),
            )

    def inject_failing_migration(self, version: int) -> None:
        """Test helper: register a fault-injected migration that fails midway.

        The next migration pass runs it, rolls it back, records an audit
        entry, and removes it from the registry (no partial write survives).
        """
        _MIGRATIONS[version] = _Migration(
            version=version,
            statements=("CREATE TABLE injected_partial (id INTEGER PRIMARY KEY);",),
            fault_injected=True,
        )

    def hold_write_lock(self) -> None:
        """Test helper: hold ``BEGIN IMMEDIATE`` on a separate connection.

        Simulates a concurrent writer; mutating calls on this store then
        return ``retryable_lock_busy`` after bounded retries.
        """
        with self._lock:
            self._require_open()
            if self._held_lock_conn is None:
                conn = sqlite3.connect(
                    str(self._db_path), isolation_level=None, check_same_thread=False
                )
                conn.execute("BEGIN IMMEDIATE")
                self._held_lock_conn = conn

    def release_write_lock(self) -> None:
        """Release the lock held by :meth:`hold_write_lock`."""
        with self._lock:
            if self._held_lock_conn is not None:
                try:
                    self._held_lock_conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                try:
                    self._held_lock_conn.close()
                except sqlite3.Error:
                    pass
                self._held_lock_conn = None

    def seed_legacy_row_v1(
        self,
        scope_id: str,
        memory_key: str,
        *,
        project_id: str = "llamar",
        statement: str | None = None,
        kind: str = "lesson",
    ) -> str:
        """Test helper: seed a v1-semantics memory row (upgrade survival).

        Writes a published row exactly as migration 001 would have, so
        simulated old-version databases can be upgraded and re-read.
        """
        memory_id = uuid.uuid4().hex
        text = statement if statement is not None else f"legacy {memory_key}"
        with self._lock, self._immediate():
            self._require_open()
            now = _utc_now()
            self._conn.execute(
                "INSERT INTO long_term_memory (memory_id, project_id, scope_id, "
                "memory_key, content_digest, kind, statement, confidence, status, "
                "supersedes_memory_id, policy_version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    project_id,
                    scope_id,
                    memory_key,
                    derive_content_digest(text),
                    kind,
                    text,
                    1.0,
                    "published",
                    None,
                    1,
                    now,
                    now,
                ),
            )
        return memory_id
