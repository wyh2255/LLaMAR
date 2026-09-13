"""Phase 2+5 recovery: control-journal reconciliation, scope fence and outbox
replay.

Restart order (plan §7.3 steps 1-7) is owned by the composition root; this
module provides the deterministic reconciliation seams:

1. journal ↔ ``control_receipt`` reconciliation (every durable journal entry
   missing from ``control_receipt`` is written with the same canonical
   lifecycle bundle; matching receipts return the original event and never add
   a second event/outbox);
2. the old scope is closed before a new epoch scope is activated (durable
   closed-scope fence: callbacks can never reopen it);
3. Phase 5 outbox replay: after a kill/restart the pending outbox rows are
   replayed by deterministically rebuilding the compatibility artifacts from
   the committed canonical set (never a blind append to legacy files) and then
   marking the rows ``exported``.

Recovery never performs automatic retention purge and never backfills
ambiguous legacy artifacts — they are kept read-only and marked
``legacy_unmigrated`` in the export manifest.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from a2a.coordinator.memory.contracts import ControlTransitionJournalEntry
from a2a.coordinator.memory.exporter import (
    CANONICAL_ARTIFACTS,
    MemoryExporter,
    MemoryExportReport,
    load_manifest,
    render_jsonl_bytes,
)

if TYPE_CHECKING:
    from a2a.coordinator.memory.ingestor import (
        MemoryIngestor,
        MemoryLifecycleBridge,
        MemoryScopeFactory,
    )
    from a2a.coordinator.memory.store import MemoryStore

logger = logging.getLogger(__name__)

__all__ = ["CanonicalVerification", "MemoryRecovery", "OutboxReplayResult"]


@dataclass(frozen=True)
class OutboxReplayResult:
    """Result of one outbox replay after a kill/restart."""

    scope_id: str
    pending: int
    replayed: int
    artifacts_consistent: bool
    scope_closed: bool
    revision: int
    manifest_sha256: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CanonicalVerification:
    """Read-only verification of the committed canonical set and fences."""

    scope_exists: bool
    scope_closed: bool
    revision: int
    pending_outbox: int
    manifest_present: bool
    artifacts_consistent: bool
    missing_artifacts: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.scope_exists and self.manifest_present and self.artifacts_consistent


def _artifact_digests(export_dir: Path) -> dict[str, str]:
    """Current SHA-256 of every canonical artifact file on disk."""
    export_dir = Path(export_dir)
    digests: dict[str, str] = {}
    for name in CANONICAL_ARTIFACTS:
        path = export_dir / name
        try:
            digests[name] = _sha256_of(path)
        except OSError:
            continue
    return digests


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryRecovery:
    """Phase 2+5 recovery facade: journal reconciliation, scope fence and
    outbox replay."""

    def __init__(
        self,
        store: MemoryStore,
        ingestor: MemoryIngestor,
        scope_factory: MemoryScopeFactory,
        bridge: MemoryLifecycleBridge,
        exporter: MemoryExporter | None = None,
    ) -> None:
        self._store = store
        self._ingestor = ingestor
        self._scope_factory = scope_factory
        self._bridge = bridge
        self._exporter = exporter or MemoryExporter(store)

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def exporter(self) -> MemoryExporter:
        return self._exporter

    def reconcile_control_journal(
        self, journal_entries: list[ControlTransitionJournalEntry]
    ) -> int:
        """Write every journal entry missing from ``control_receipt``.

        Matching receipts return the original event/receipt without a second
        event/outbox; journal digest mismatches fail loud with zero partial
        writes (the underlying canonical transaction rolls back).
        """
        written = 0
        for entry in journal_entries:
            result = self._ingestor.ingest_control_receipt(entry)
            if result.status == "ok":
                written += 1
            elif result.status == "duplicate":
                logger.info(
                    "control receipt already materialized: dispatch=%s rev=%s",
                    entry.dispatch_id,
                    entry.control_revision,
                )
            else:
                logger.warning(
                    "control receipt skipped (%s): dispatch=%s rev=%s",
                    result.status,
                    entry.dispatch_id,
                    entry.control_revision,
                )
        return written

    def close_old_scope(self, context_id: str, runtime_epoch: int) -> bool:
        """Durable closed-scope fence: callbacks can never reopen it."""
        return self._ingestor.close_scope(context_id, runtime_epoch)

    def activate_new_scope(self, context_id: str, runtime_epoch: int) -> str:
        """Activate the post-recovery scope for the new epoch."""
        return self._ingestor.activate_scope(context_id, runtime_epoch)

    # ── Phase 5: committed-set verification (read-only) ───────────────────

    def verify_committed_canonical(
        self, scope_id: str, export_dir: Path
    ) -> CanonicalVerification:
        """Verify the committed canonical set, scope fence and materialized
        artifacts without writing anything.

        A scope fence violation (a closed scope appearing active) or an
        artifact that does not match a deterministic rebuild of the committed
        canonical set is reported as inconsistent — nothing is repaired here.
        """
        scope = self._store.get_scope(scope_id)
        if scope is None:
            return CanonicalVerification(
                scope_exists=False,
                scope_closed=False,
                revision=0,
                pending_outbox=0,
                manifest_present=False,
                artifacts_consistent=False,
                missing_artifacts=list(CANONICAL_ARTIFACTS),
            )
        pending = [
            e for e in self._store.outbox_entries(scope_id) if e["status"] == "pending"
        ]
        manifest = load_manifest(export_dir)
        manifest_present = manifest is not None and (
            manifest.get("scope_id") == scope_id
        )
        expected = self._exporter.build_artifacts(scope_id)
        expected_bytes = {
            name: render_jsonl_bytes(records) for name, records in expected.items()
        }
        on_disk = _artifact_digests(export_dir)
        missing = [name for name in CANONICAL_ARTIFACTS if name not in on_disk]
        artifacts_consistent = not missing and all(
            on_disk[name] == _sha256_bytes(expected_bytes[name])
            for name in CANONICAL_ARTIFACTS
        )
        return CanonicalVerification(
            scope_exists=True,
            scope_closed=scope["closed_at"] is not None,
            revision=self._store.revision_of(scope_id),
            pending_outbox=len(pending),
            manifest_present=manifest_present,
            artifacts_consistent=artifacts_consistent,
            missing_artifacts=missing,
        )

    # ── Phase 5: outbox replay after kill/restart ─────────────────────────

    def replay_outbox(self, scope_id: str, export_dir: Path) -> OutboxReplayResult:
        """Deterministically replay pending outbox rows after a kill/restart.

        Outbox replay does NOT re-run the reducer or write new canonical
        events.  Pending rows are marked ``exported`` first, then the
        compatibility artifacts are deterministically rebuilt from the
        committed canonical set (temp + fsync + manifest + replace), so the
        materialized artifact always reflects the post-replay canonical state.
        The rebuild is unconditional (idempotent, at-least-once): an already
        consistent artifact is rewritten with identical bytes, which also
        repairs a tampered or missing artifact without any blind append.
        """
        export_dir = Path(export_dir)
        scope = self._store.get_scope(scope_id)
        if scope is None:
            return OutboxReplayResult(
                scope_id=scope_id,
                pending=0,
                replayed=0,
                artifacts_consistent=False,
                scope_closed=False,
                revision=0,
                reason="unknown_scope",
            )
        pending = [
            e for e in self._store.outbox_entries(scope_id) if e["status"] == "pending"
        ]
        replayed = 0
        for entry in pending:
            if self._store.mark_outbox_exported(entry["outbox_id"]):
                replayed += 1
        report = self._exporter.export_scope(scope_id, export_dir)
        return OutboxReplayResult(
            scope_id=scope_id,
            pending=len(pending),
            replayed=replayed,
            artifacts_consistent=self._materialized_match(report, export_dir),
            scope_closed=scope["closed_at"] is not None,
            revision=report.canonical_revision,
            manifest_sha256=report.manifest_sha256,
            reason="" if replayed == len(pending) else "outbox_rewrite_failed",
        )

    def _materialized_match(self, report: MemoryExportReport, export_dir: Path) -> bool:
        """True when every artifact on disk matches its manifest digest."""
        for name, entry in report.artifacts.items():
            path = Path(export_dir) / name
            try:
                if _sha256_of(path) != entry.artifact_sha256:
                    return False
            except OSError:
                return False
        return True

    # ── Phase 5: legacy artifact marking (never backfill) ─────────────────

    def mark_legacy_unmigrated(
        self,
        export_dir: Path,
        legacy_artifacts: dict[str, str],
        *,
        scope_id: str | None = None,
    ) -> Path:
        """Keep read-only legacy artifacts and mark them ``legacy_unmigrated``.

        Ambiguous historical JSONL/NDJSON artifacts are never auto-backfilled
        into canonical Memory; the manifest records them as read-only legacy
        artifacts so future migration can operate on them explicitly.
        """
        return self._exporter.mark_legacy_unmigrated(
            export_dir, legacy_artifacts, scope_id=scope_id
        )
