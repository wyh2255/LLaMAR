"""Phase 2 recovery: control-journal bundle reconciliation and scope closure.

Restart order (plan §7.3 steps 1-7) is owned by the composition root; this
module provides the deterministic reconciliation seam: every durable journal
entry missing from ``control_receipt`` is written with the same canonical
lifecycle bundle, and the old scope is closed before a new epoch scope is
activated.  Phase 5 extends this with outbox replay and export recovery.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from a2a.coordinator.memory.contracts import ControlTransitionJournalEntry

if TYPE_CHECKING:
    from a2a.coordinator.memory.ingestor import (
        MemoryIngestor,
        MemoryLifecycleBridge,
        MemoryScopeFactory,
    )
    from a2a.coordinator.memory.store import MemoryStore

logger = logging.getLogger(__name__)

__all__ = ["MemoryRecovery"]


class MemoryRecovery:
    """Phase 2 recovery facade: journal reconciliation + scope-close fence."""

    def __init__(
        self,
        store: "MemoryStore",
        ingestor: "MemoryIngestor",
        scope_factory: "MemoryScopeFactory",
        bridge: "MemoryLifecycleBridge",
    ) -> None:
        self._store = store
        self._ingestor = ingestor
        self._scope_factory = scope_factory
        self._bridge = bridge

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
