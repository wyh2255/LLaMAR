"""Phase 0 durable ControlTransitionJournal contracts.

Every accepted lifecycle transition must get a dispatch-local monotonic
``control_revision`` plus a durable journal entry persisted atomically with the
control-state snapshot (temp+fsync+replace).  The journal must survive a crash
even when the MemoryLifecycleBridge never ran, and its canonical digest must not
include the raw callback body.
"""

from __future__ import annotations

import json

from a2a.coordinator.memory.contracts import (
    ControlTransitionJournalEntry,
    control_transition_digest,
)
from a2a.coordinator.mission_runtime import MissionRuntimeManager, PhysicalState


def _manager(tmp_path):
    return MissionRuntimeManager(state_path=tmp_path / "coordinator-state.json")


def test_control_revision_is_monotonic_and_dispatch_local(tmp_path):
    manager = _manager(tmp_path)
    runtime = manager.admit("ctx-rev")
    dispatch_a = runtime.create_dispatch("logical-a", "Alice")
    dispatch_b = runtime.create_dispatch("logical-b", "Bob")
    runtime.register_worker_task(dispatch_a.dispatch_id, "w-a")
    runtime.register_worker_task(dispatch_b.dispatch_id, "w-b")

    runtime.apply_physical_status(
        dispatch_a.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(
        dispatch_b.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(
        dispatch_a.dispatch_id, "ACCEPTED", source="acceptance"
    )

    assert runtime.control_revision_of(dispatch_a.dispatch_id) == 2
    assert runtime.control_revision_of(dispatch_b.dispatch_id) == 1

    entries = manager.control_journal_entries(dispatch_id=dispatch_a.dispatch_id)
    assert [entry.control_revision for entry in entries] == [1, 2]
    assert [entry.previous_state for entry in entries] == ["PREPARED", "DISPATCHING"]
    assert [entry.state for entry in entries] == ["DISPATCHING", "ACCEPTED"]
    assert len(entries) == len({entry.transition_id for entry in entries})


def test_rejected_transition_does_not_advance_revision_or_journal(tmp_path):
    manager = _manager(tmp_path)
    runtime = manager.admit("ctx-reject")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "w")

    before = runtime.control_revision_of(dispatch.dispatch_id)
    # PREPARED -> COMPLETED is not a legal transition.
    returned = runtime.apply_physical_status(
        dispatch.dispatch_id, "COMPLETED", source="rogue_callback"
    )

    assert returned.state is PhysicalState.PREPARED
    assert runtime.control_revision_of(dispatch.dispatch_id) == before
    assert manager.control_journal_entries(dispatch_id=dispatch.dispatch_id) == []


def test_journal_digest_excludes_raw_callback_body(tmp_path):
    manager = _manager(tmp_path)
    runtime = manager.admit("ctx-body")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "w")

    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id,
        "ACCEPTED",
        source="callback",
        result={"SECRET_BODY": "hunter2", "token": "deadbeef"},
    )

    entry = runtime.last_control_entry(dispatch.dispatch_id)
    assert entry is not None
    assert entry.state == "ACCEPTED"
    assert entry.result_digest is not None

    fields = {
        "context_id": entry.context_id,
        "runtime_epoch": entry.runtime_epoch,
        "dispatch_id": entry.dispatch_id,
        "control_revision": entry.control_revision,
        "previous_state": entry.previous_state,
        "state": entry.state,
        "source": entry.source,
        "observed_at": entry.observed_at,
        "result_digest": entry.result_digest,
    }
    assert entry.journal_sha256 == control_transition_digest(fields)

    serialized = json.dumps(entry.to_dict())
    assert "hunter2" not in serialized
    assert "deadbeef" not in serialized
    assert "SECRET_BODY" not in serialized

    # The digest is recomputable from the persisted fields.
    rebuilt = ControlTransitionJournalEntry.from_dict(entry.to_dict())
    assert rebuilt.journal_sha256 == entry.journal_sha256
    assert rebuilt.transition_id == entry.transition_id


def test_receipt_seam_delivers_journal_outside_runtime_lock(tmp_path):
    manager = _manager(tmp_path)
    runtime = manager.admit("ctx-seam")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "w")

    receipts = []

    def sink(entry):
        # The sink must never run while the runtime lock is held by this thread.
        receipts.append((entry, runtime._lock._is_owned()))

    runtime.attach_receipt_sink(sink)

    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    assert len(receipts) == 1
    entry, lock_held = receipts[0]
    assert entry.control_revision == 1
    assert lock_held is False
    # Pending queue is drained after delivery.
    assert runtime.take_pending_receipts() == []


def test_journal_survives_crash_before_bridge_runs(tmp_path):
    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-crash")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-crash")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="callback")
    runtime.apply_physical_status(dispatch.dispatch_id, "RUNNING", source="callback")
    assert dispatch.state is PhysicalState.RUNNING

    # Simulate a crash: the bridge never ran and no receipt was consumed.
    del manager

    restarted = MissionRuntimeManager(state_path=state_path)
    entries = restarted.control_journal_entries()
    assert len(entries) == 3
    assert len({entry.transition_id for entry in entries}) == 3
    assert [entry.state for entry in entries] == ["DISPATCHING", "ACCEPTED", "RUNNING"]

    # The journal is the reconciliation source of truth: dispatch-local
    # revisions survive and PhysicalDispatch.state stays the control truth.
    recovered_epoch = restarted.recover()
    assert recovered_epoch > 0
    recovered = restarted.active_runtime
    assert recovered is not None
    assert recovered.control_revision_of(dispatch.dispatch_id) == 3
    restored = recovered.get_dispatch(dispatch.dispatch_id)
    assert restored is not None
    assert restored.state is PhysicalState.RUNNING


def test_restart_reconciliation_writes_missing_control_receipts_once(tmp_path):
    """Phase 2: the journal ↔ control_receipt diff is the reconciliation source;
    matching receipts never produce a second event/outbox."""
    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import (
        MemoryIngestor,
        MemoryLifecycleBridge,
        MemoryScopeFactory,
    )
    from a2a.coordinator.memory.recovery import MemoryRecovery
    from a2a.coordinator.memory.store import MemoryStore

    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-recon")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-recon")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    entries = manager.control_journal_entries(dispatch_id=dispatch.dispatch_id)
    assert len(entries) == 1

    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(store, scope_factory)
    bridge = MemoryLifecycleBridge(ingestor)
    ingestor.set_bridge(bridge)
    ingestor.activate_scope("ctx-recon", 0)
    scope_id = scope_factory.resolve("ctx-recon", 0).scope_id

    recovery = MemoryRecovery(store, ingestor, scope_factory, bridge)
    assert recovery.reconcile_control_journal(entries) == 1
    assert len(store.control_receipts(scope_id)) == 1
    control_events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"].startswith("control.")
    ]
    assert len(control_events) == 1

    # Same journal replayed on a second restart: no duplicate.
    assert recovery.reconcile_control_journal(entries) == 0
    assert len(store.control_receipts(scope_id)) == 1
    assert len(store.temporal_events(scope_id)) == 1
    assert len(store.outbox_entries(scope_id)) == 1
