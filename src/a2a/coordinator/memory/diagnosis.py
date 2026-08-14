"""Phase 2 independent diagnosis kernel (main plan §3.2 / D3-D4 / D6-D7 / R2).

Contract source: 「系统健康诊断 · agentic 审查者」实施方案 §3.2（DiagnosisCandidateV1
输出契约）+ §6 P2（独立 store + validator + 防回声室 + 置信度门控）。本模块与
``reflection`` 的长期记忆通道**并存不替代**（A4）：诊断走独立短命 store
``<memory_root>/diagnosis/diagnosis.sqlite3``（模板 = ``LongTermMemoryStore``，
long_term.py:194-379），不参与长期记忆 supersede 链、不跨 run、不进投影事实域
（B6），也**不写** canonical temporal（``diagnosis.audit`` audit 事件由 P3 诊断
循环写入；本 Phase 只提供 ``DIAGNOSIS_AUDIT_EVENT_TYPE`` 常量）。

防回声室三重防护语义（2026-08-13 审查强化，R2/R6）：
① validator 拒绝指向既有诊断的 source_ref —— ``validate_diagnosis_response``
   对窗口内 ``event_type`` 以 ``diagnosis.`` 开头的事件引用返回
   ``echo_chamber_source_ref`` rejected（零诊断写入）；
② 诊断 audit 事件使用独立顶层前缀 ``diagnosis.audit``（不在
   ``_REFLECTION_EVENT_PREFIXES`` 四前缀 control./callback./evidence./
   supervision. 内）——反思收集器天然不收，诊断永不进反思输入窗口；
③ 时间线只读工具对诊断 audit 事件做视图过滤（P3 工具封装，本 Phase 不涉及）。

``validate_diagnosis_response`` 复用 reflection.py:134-211 纯校验段模式（形状 /
批归一化 / 缺字段 / 空 finding / truth 词 / ref 可验证性 / confidence fail-closed
全 rejected 路径，零领域写入），字段归属按冻结测试确认：LLM 侧只提供
target/finding/suggestion/confidence/source_refs，validator 派生 diagnosis_key
（SHA-256(canonical_json(scope_ids+target+kind+statement))，statement =
canonicalize(finding)）+ kind="diagnosis" + policy_version（str）。
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    POLICY_VERSION,
    MemoryContractError,
    canonical_json_bytes,
    canonicalize_statement,
    digest_bytes,
)

__all__ = [
    "DIAGNOSIS_AUDIT_EVENT_TYPE",
    "DiagnosisCandidateV1",
    "DiagnosisMemoryStore",
    "DiagnosisStoreError",
    "DiagnosisStoreLockBusyError",
    "DiagnosisValidationResult",
    "validate_diagnosis_response",
]

#: 防回声室 ②：诊断 audit 事件类型 = 独立顶层前缀（D9 2026-08-13 修订；原
#: ``evidence.diagnosis_audit`` 命名撤销——evidence.* 会被反思收集器回收形成回声）。
DIAGNOSIS_AUDIT_EVENT_TYPE = "diagnosis.audit"

#: WAL busy timeout per ``BEGIN IMMEDIATE`` attempt (milliseconds).
_BUSY_TIMEOUT_MS = 50
#: Bounded retry count for ``BEGIN IMMEDIATE`` before surfacing the typed
#: retryable status.
_BEGIN_IMMEDIATE_ATTEMPTS = 12

#: System-derived kind for every diagnosis candidate (D3).
_DIAGNOSIS_KIND = "diagnosis"

#: LLM-side candidate fields; the validator derives diagnosis_key / kind /
#: policy_version (frozen test contract, P0).
_CANDIDATE_FIELDS = ("target", "finding", "suggestion", "confidence", "source_refs")

#: target ∈ {coordinator, worker:<id>, system} —— oracle / 裸 worker（无 id）/
#: coordinator:<id> / system:<id> / 空串一律拒绝（§3.2）。
_DIAGNOSIS_TARGET_RE = re.compile(r"^(?:coordinator|system|worker:[^:]+)$")


class DiagnosisStoreError(MemoryContractError):
    """Stable typed error for diagnosis store failures."""

    code = "diagnosis_store_error"


class DiagnosisStoreLockBusyError(DiagnosisStoreError):
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
    """CREATE TABLE diagnosis (
        diagnosis_id TEXT PRIMARY KEY,
        scope_id TEXT NOT NULL,
        diagnosis_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        finding TEXT NOT NULL,
        suggestion TEXT NOT NULL,
        confidence REAL NOT NULL,
        policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(scope_id, diagnosis_key)
    )""",
    """CREATE TABLE diagnosis_support (
        diagnosis_id TEXT NOT NULL REFERENCES diagnosis(diagnosis_id),
        source_scope_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        PRIMARY KEY (diagnosis_id, source_scope_id, source_event_id)
    )""",
    """CREATE INDEX ix_diagnosis_scope
        ON diagnosis(scope_id, created_at)""",
)

#: Single schema-version authority: version -> migration.
_MIGRATIONS: dict[int, _Migration] = {
    1: _Migration(version=1, statements=_MIGRATION_001_STATEMENTS),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── validator ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiagnosisCandidateV1:
    """One validated diagnosis candidate (main plan §3.2 / D3).

    ``diagnosis_key`` / ``kind`` / ``policy_version`` are system-derived (never
    accepted from the model); ``target`` ∈ {coordinator, worker:<id>, system};
    ``source_refs`` are (scope_id, event_id) evidence pairs that must all sit
    inside the input window and must **not** point at prior diagnoses
    (防回声室 ①).  ``validate()`` fails closed on any structural violation.
    """

    schema_version: int
    diagnosis_key: str
    kind: str
    target: str
    finding: str
    suggestion: str
    confidence: float
    source_refs: tuple[tuple[str, str], ...] | list[tuple[str, str]] = ()
    policy_version: str = str(POLICY_VERSION)

    def validate(self) -> DiagnosisCandidateV1:
        """Fail-closed validation; raises typed :class:`MemoryContractError`."""
        if self.schema_version != 1:
            raise MemoryContractError(
                "invalid_schema_version",
                f"schema_version must be 1, got {self.schema_version!r}",
            )
        if self.kind != _DIAGNOSIS_KIND:
            raise MemoryContractError(
                "invalid_kind",
                f"kind must be {_DIAGNOSIS_KIND!r}, got {self.kind!r}",
            )
        if (
            not isinstance(self.diagnosis_key, str)
            or len(self.diagnosis_key) != 64
            or any(char not in "0123456789abcdef" for char in self.diagnosis_key)
        ):
            raise MemoryContractError(
                "invalid_diagnosis_key",
                "diagnosis_key must be a 64-hex SHA-256 digest",
            )
        if not isinstance(self.target, str) or not _DIAGNOSIS_TARGET_RE.match(
            self.target
        ):
            raise MemoryContractError(
                "invalid_target",
                f"target must be coordinator|worker:<id>|system, got {self.target!r}",
            )
        if not isinstance(self.finding, str) or not self.finding.strip():
            raise MemoryContractError("invalid_finding", "finding is required")
        if not isinstance(self.suggestion, str) or not self.suggestion.strip():
            raise MemoryContractError("invalid_suggestion", "suggestion is required")
        if not isinstance(self.confidence, (int, float)) or not (
            0.0 <= self.confidence <= 1.0
        ):
            raise MemoryContractError(
                "invalid_confidence",
                f"confidence must be within [0,1], got {self.confidence!r}",
            )
        refs = self.source_refs
        if not isinstance(refs, (tuple, list)):
            raise MemoryContractError("invalid_source_refs", "source_refs required")
        for ref in refs:
            if not (
                isinstance(ref, (tuple, list))
                and len(ref) == 2
                and isinstance(ref[0], str)
                and isinstance(ref[1], str)
            ):
                raise MemoryContractError(
                    "invalid_source_ref_shape",
                    f"source_ref must be a (scope_id, event_id) pair, got {ref!r}",
                )
        return self


@dataclass(frozen=True)
class DiagnosisValidationResult:
    """Deterministic validator outcome.

    The validator itself never writes to any store; ``diagnosis_written`` is
    therefore always 0 here — P3 wiring persists only accepted candidates and
    records the ``diagnosis.audit`` temporal event on acceptance.
    """

    status: str  # ok | rejected
    diagnosis_written: int = 0
    reason: str | None = None
    candidates: tuple[DiagnosisCandidateV1, ...] = ()


def _window_event_map(input_window: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """(scope_id, event_id) -> event dict map from a source window.

    Accepts the ``ScopeEventSnapshotV1`` shape (``.events`` of dicts with
    ``scope_id``/``event_id``/``event_type``) or a plain iterable of event
    dicts.  Anything else yields an empty map → any non-empty ref list is
    rejected (fail closed).  The map powers both ref verifiability and the
    echo-chamber check (event_type starting with ``diagnosis.``).
    """
    events = getattr(input_window, "events", None)
    if not isinstance(events, (list, tuple, set, frozenset)):
        if isinstance(input_window, (list, tuple, set, frozenset)):
            events = input_window
        else:
            events = None
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    if events is None:
        return mapping
    for event in events:
        if (
            isinstance(event, dict)
            and isinstance(event.get("scope_id"), str)
            and isinstance(event.get("event_id"), str)
        ):
            mapping[(event["scope_id"], event["event_id"])] = event
    return mapping


def _derive_diagnosis_key(
    *, scope_ids: tuple[str, ...], target: str, kind: str, finding: str
) -> str:
    """Deterministic system-derived diagnosis key (model never sees/chooses it).

    ``sha256(canonical_json({scope_ids, target, kind, statement}))`` where
    ``statement`` = :func:`canonicalize_statement` of the finding (§3.2).  The
    same input always yields the same key; a finding/target change yields a
    different key (frozen test contract).
    """
    payload = {
        "scope_ids": list(scope_ids),
        "target": target,
        "kind": kind,
        "statement": canonicalize_statement(finding),
    }
    return digest_bytes(canonical_json_bytes(payload))


def validate_diagnosis_response(
    response: Any, *, input_window: Any = None
) -> DiagnosisValidationResult:
    """Deterministic fail-closed validation of a diagnosis model response.

    ``response`` must carry ``function_call`` (single candidate dict or a
    batch list).  Every candidate must have target / finding / suggestion /
    confidence / source_refs; ``target`` ∈ {coordinator, worker:<id>, system};
    finding and suggestion must be non-empty and free of
    :data:`FORBIDDEN_TRUTH_TERMS`; every source ref must be verifiable against
    ``input_window`` (with no window provided, any non-empty ref list is
    rejected as unverifiable) and must **not** point at a prior diagnosis
    event (event_type starting with ``diagnosis.`` → 防回声室 rejected);
    confidence must be numeric within [0,1].  All rejection paths return
    ``diagnosis_written == 0``.  The system derives diagnosis_key / kind /
    policy_version on acceptance (fields are preserved verbatim).
    """

    def rejected(reason: str) -> DiagnosisValidationResult:
        return DiagnosisValidationResult(
            status="rejected", diagnosis_written=0, reason=reason
        )

    if not isinstance(response, dict):
        return rejected("invalid_response_shape")
    if "function_call" not in response:
        return rejected("missing_function_call")
    raw = response["function_call"]
    if isinstance(raw, dict):
        raw_candidates = [raw]
    elif isinstance(raw, (list, tuple)):
        raw_candidates = list(raw)
    else:
        return rejected("invalid_function_call_shape")
    if not raw_candidates:
        return rejected("missing_function_call")

    window_events = _window_event_map(input_window) if input_window is not None else None
    scope_ids = (
        tuple(sorted({scope_id for scope_id, _ in window_events}))
        if window_events is not None
        else ()
    )
    validated: list[DiagnosisCandidateV1] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            return rejected("invalid_candidate_shape")
        missing = [name for name in _CANDIDATE_FIELDS if name not in candidate]
        if missing:
            return rejected(f"missing_candidate_field:{','.join(missing)}")
        target = candidate["target"]
        if not isinstance(target, str) or not _DIAGNOSIS_TARGET_RE.match(target):
            return rejected("invalid_target")
        finding = candidate["finding"]
        if not isinstance(finding, str) or not finding.strip():
            return rejected("empty_finding")
        canonical_finding = canonicalize_statement(finding)
        if not canonical_finding:
            return rejected("empty_finding")
        if any(term in canonical_finding for term in FORBIDDEN_TRUTH_TERMS):
            return rejected("forbidden_truth_term")
        suggestion = candidate["suggestion"]
        if not isinstance(suggestion, str) or not suggestion.strip():
            return rejected("empty_suggestion")
        if any(term in canonicalize_statement(suggestion) for term in FORBIDDEN_TRUTH_TERMS):
            return rejected("forbidden_truth_term")
        refs = candidate["source_refs"]
        if not isinstance(refs, (list, tuple)):
            return rejected("invalid_source_refs")
        normalized_refs: list[tuple[str, str]] = []
        for ref in refs:
            if not (
                isinstance(ref, (list, tuple))
                and len(ref) == 2
                and isinstance(ref[0], str)
                and isinstance(ref[1], str)
            ):
                return rejected("invalid_source_ref_shape")
            normalized_refs.append((ref[0], ref[1]))
        if normalized_refs:
            if window_events is None:
                return rejected("unverifiable_source_ref")
            for ref in normalized_refs:
                if ref not in window_events:
                    return rejected("forged_source_ref")
                event = window_events[ref]
                event_type = event.get("event_type")
                if isinstance(event_type, str) and event_type.startswith("diagnosis."):
                    return rejected("echo_chamber_source_ref")
        confidence = candidate["confidence"]
        if not isinstance(confidence, (int, float)):
            return rejected("invalid_confidence")
        # F2/F3: 构造后必须过 DTO fail-closed 校验（含 confidence ∈ [0,1]），
        # 越界/非法值 → typed rejected，与 validator 其余路径一致。
        try:
            validated_candidate = DiagnosisCandidateV1(
                schema_version=1,
                diagnosis_key=_derive_diagnosis_key(
                    scope_ids=scope_ids,
                    target=target,
                    kind=_DIAGNOSIS_KIND,
                    finding=finding,
                ),
                kind=_DIAGNOSIS_KIND,
                target=target,
                finding=finding,
                suggestion=suggestion,
                confidence=float(confidence),
                source_refs=tuple(normalized_refs),
                policy_version=str(POLICY_VERSION),
            ).validate()
        except MemoryContractError as exc:
            return rejected(exc.code)
        validated.append(validated_candidate)
    return DiagnosisValidationResult(
        status="ok", diagnosis_written=0, candidates=tuple(validated)
    )


# ── independent short-lived store (template = LongTermMemoryStore) ─────────


class DiagnosisMemoryStore:
    """Run-local independent diagnosis store (main plan §3.2 / B3 / D6).

    ``db_path`` is the run-local diagnosis file, normally derived by
    :attr:`DiagnosisConfig.diagnosis_db_path`; it must be an absolute local
    path.  :meth:`open` bootstraps the ``schema_migrations`` authority and
    applies pending migrations transactionally; unknown future versions and
    digest mismatches fail closed (refuse to open).  Short-lived lifecycle:
    the store never participates in the long-term memory supersede chain,
    every row is namespaced by ``scope_id``, and the file lives under
    ``<memory_root>/diagnosis/`` — a fresh run/scope starts with zero
    diagnoses (不跨 run).
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── lifecycle ────────────────────────────────────────────────────────

    def open(self) -> DiagnosisMemoryStore:
        """Validate the path, connect, and run pending migrations.

        Raises :class:`DiagnosisStoreError` for relative/URI paths and for
        fail-closed migration states (unknown future version, digest
        mismatch, real migration failure).  Idempotent when already open.
        """
        with self._lock:
            if self._conn is not None:
                return self
            path = self._db_path
            if not path.is_absolute() or "://" in str(path):
                raise DiagnosisStoreError(
                    "invalid_db_path",
                    f"diagnosis db_path must be an absolute local path: {path!r}",
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
        """Close the main connection."""
        with self._lock:
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
            raise DiagnosisStoreError(
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

        Fail-closed paths (all raise :class:`DiagnosisStoreError`):
        - an applied version unknown to this binary (unknown future version);
        - stored migration digest mismatch;
        - a real migration error mid-way → explicit ROLLBACK (never
          ``executescript`` as a transaction boundary).
        """
        conn = self._conn
        assert conn is not None
        applied = self._applied_versions()
        for version in applied:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise DiagnosisStoreError(
                    "unknown_migration_version",
                    f"schema version {version} is not known to this binary "
                    f"(known: {sorted(_MIGRATIONS)}) — refusing to open",
                )
            stored = conn.execute(
                "SELECT migration_sha256 FROM schema_migrations WHERE version=?",
                (version,),
            ).fetchone()
            if stored is None or stored["migration_sha256"] != migration.digest:
                raise DiagnosisStoreError(
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
                    conn.execute(
                        "INSERT INTO schema_migrations "
                        "(version, applied_at, migration_sha256) VALUES (?,?,?)",
                        (version, _utc_now(), migration.digest),
                    )
            except sqlite3.Error as exc:
                raise DiagnosisStoreError(
                    "migration_failed",
                    f"schema migration {version:03d} failed and was rolled "
                    f"back: {exc}",
                ) from exc
            newly.append(version)
        return newly

    # ── transactions ─────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _immediate(self) -> Iterator[None]:
        """Short ``BEGIN IMMEDIATE`` transaction with bounded busy retry.

        Lock exhaustion raises :class:`DiagnosisStoreLockBusyError` (typed
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
            raise DiagnosisStoreLockBusyError(
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
            raise DiagnosisStoreError("store_not_open", "store must be open()ed first")

    # ── read / write ─────────────────────────────────────────────────────

    def diagnoses(self, scope_id: str) -> list[DiagnosisCandidateV1]:
        """Diagnoses of one scope, oldest first (P3 read port).

        Short-lived lifecycle: a fresh run/scope returns ``[]`` — rows are
        namespaced by ``scope_id`` and never cross runs (不跨 run, B3).
        """
        with self._lock:
            self._require_open()
            conn = self._conn
            assert conn is not None
            rows = conn.execute(
                "SELECT * FROM diagnosis WHERE scope_id=? "
                "ORDER BY created_at, diagnosis_id",
                (scope_id,),
            ).fetchall()
            result: list[DiagnosisCandidateV1] = []
            for row in rows:
                support = conn.execute(
                    "SELECT source_scope_id, source_event_id FROM diagnosis_support "
                    "WHERE diagnosis_id=? ORDER BY source_scope_id, source_event_id",
                    (row["diagnosis_id"],),
                ).fetchall()
                refs = tuple(
                    (str(s["source_scope_id"]), str(s["source_event_id"]))
                    for s in support
                )
                result.append(
                    DiagnosisCandidateV1(
                        schema_version=1,
                        diagnosis_key=str(row["diagnosis_key"]),
                        kind=str(row["kind"]),
                        target=str(row["target"]),
                        finding=str(row["finding"]),
                        suggestion=str(row["suggestion"]),
                        confidence=float(row["confidence"]),
                        source_refs=refs,
                        policy_version=str(row["policy_version"]),
                    )
                )
            return result

    def save_diagnoses(
        self, scope_id: str, candidates: Iterable[DiagnosisCandidateV1]
    ) -> int:
        """Persist validated candidates for one scope in one transaction.

        Returns the number of newly written rows (same-scope same-key
        duplicates are ignored, ``INSERT OR IGNORE`` on
        ``UNIQUE(scope_id, diagnosis_key)``).  Every candidate is
        re-validated fail-closed inside the transaction; any violation
        rolls the batch back with zero partial writes.
        """
        with self._lock:
            self._require_open()
            conn = self._conn
            assert conn is not None
            written = 0
            with self._immediate():
                for candidate in candidates:
                    candidate.validate()
                    diagnosis_id = uuid.uuid4().hex
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO diagnosis "
                        "(diagnosis_id, scope_id, diagnosis_key, kind, target, "
                        "finding, suggestion, confidence, policy_version, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            diagnosis_id,
                            scope_id,
                            candidate.diagnosis_key,
                            candidate.kind,
                            candidate.target,
                            candidate.finding,
                            candidate.suggestion,
                            candidate.confidence,
                            candidate.policy_version,
                            _utc_now(),
                        ),
                    )
                    if cursor.rowcount == 0:
                        continue
                    for source_scope_id, source_event_id in candidate.source_refs:
                        conn.execute(
                            "INSERT OR IGNORE INTO diagnosis_support "
                            "(diagnosis_id, source_scope_id, source_event_id) "
                            "VALUES (?,?,?)",
                            (diagnosis_id, source_scope_id, source_event_id),
                        )
                    written += 1
            return written
