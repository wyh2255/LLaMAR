"""Phase 2 MemoryIngestor contracts: idempotency receipts, canonical bundle
atomicity, retry semantics, crash / reconciliation, and secret redaction across
SQLite + EventStore NDJSON + SemanticMapStore JSONL sinks."""

from __future__ import annotations

import hashlib
import json

import pytest

from a2a.coordinator.event_store import EventStore
from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import (
    AuthenticatedCallbackEnvelope,
    IdempotencyConflictError,
    MemoryIngestor,
    MemoryLifecycleBridge,
    MemoryScopeFactory,
)
from a2a.coordinator.memory.recovery import MemoryRecovery
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from a2a.coordinator.mission_runtime import MissionRuntimeManager
from sar_orch.map import SemanticMapStore

SECRET = "SUPERSECRET_VALUE_9f2c1"
HMAC_HEX = "b" * 64


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=SECRET.encode("utf-8")),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _envelope(
    *,
    worker_task_id="worker-1",
    dispatch_id="dsp_1",
    state="RUNNING",
    body=b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_WORKING"}}}',
    scope_id="scope",
    actor="Alice",
    epoch=0,
):
    return AuthenticatedCallbackEnvelope.build(
        scope_id=scope_id,
        dispatch_id=dispatch_id,
        worker_task_id=worker_task_id,
        actor_id=actor,
        runtime_epoch=epoch,
        callback_kind="status_update",
        normalized_state=state,
        body_sha256=_sha256_bytes(body),
    )


def _scope_id_of(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


# ---------------------------------------------------------------------------
# Basic ingestion
# ---------------------------------------------------------------------------


def test_ingest_callback_writes_one_bundle(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    envelope = _envelope(scope_id=scope_id)
    sanitized = {"statusUpdate": {"taskId": "worker-1"}}
    result = ingestor.ingest_callback(envelope, sanitized)

    assert result.status == "ok"
    assert result.event_id
    assert result.committed_revision == 1

    events = store.temporal_events(scope_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "callback.status_update"
    assert events[0]["dispatch_id"] == "dsp_1"
    assert events[0]["worker_task_id"] == "worker-1"
    assert store.revision_of(scope_id) == 1
    assert len(store.outbox_entries(scope_id)) == 1
    assert store.idempotency_receipt(scope_id, envelope.idempotency_key) is not None


def test_same_body_new_nonce_returns_original_receipt(ingestor, store, scope_factory):
    """Retry must re-sign with a new nonce over the same body; the idempotency
    ledger returns the first receipt with no extra event / revision / outbox."""
    scope_id = _scope_id_of(scope_factory)
    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_WORKING"}}}'
    first = ingestor.ingest_callback(_envelope(scope_id=scope_id, body=body), {"ok": 1})
    assert first.status == "ok"

    retry = ingestor.ingest_callback(_envelope(scope_id=scope_id, body=body), {"ok": 1})
    assert retry.status == "duplicate"
    assert retry.event_id == first.event_id
    assert retry.receipt_sha256 == first.receipt_sha256
    assert retry.committed_revision == first.committed_revision

    assert store.temporal_event_count(scope_id) == 1
    assert store.revision_of(scope_id) == 1
    assert len(store.outbox_entries(scope_id)) == 1


def test_same_key_different_digest_conflicts_and_rolls_back(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_WORKING"}}}'
    envelope = _envelope(scope_id=scope_id, body=body)
    assert ingestor.ingest_callback(envelope, {"ok": 1}).status == "ok"

    # Same key but a different canonical payload digest.
    with pytest.raises(IdempotencyConflictError):
        ingestor.ingest_callback(envelope, {"ok": 2})

    # Conflict rolled back: exactly the original bundle remains.
    assert store.temporal_event_count(scope_id) == 1
    assert store.revision_of(scope_id) == 1
    assert len(store.outbox_entries(scope_id)) == 1


def test_unknown_and_closed_scope_write_nothing(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    scopes_before = len(store.list_scopes())

    unknown = ingestor.ingest_callback(
        _envelope(scope_id="does-not-exist", dispatch_id="dsp_x"), {"x": 1}
    )
    assert unknown.status == "unknown_scope"
    assert len(store.list_scopes()) == scopes_before

    baseline = store.temporal_event_count(scope_id)
    baseline_rev = store.revision_of(scope_id)
    baseline_outbox = len(store.outbox_entries(scope_id))
    assert ingestor.close_scope("ctx-1", 0) is True

    closed = ingestor.ingest_callback(
        _envelope(scope_id=scope_id, dispatch_id="dsp_closed"), {"x": 2}
    )
    assert closed.status == "scope_closed"
    assert store.temporal_event_count(scope_id) == baseline
    assert store.revision_of(scope_id) == baseline_rev
    assert len(store.outbox_entries(scope_id)) == baseline_outbox


def test_six_hundred_canonical_events_are_not_trimmed(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    for i in range(600):
        body = f'{{"statusUpdate":{{"taskId":"worker-1","status":{{"state":"TASK_STATE_WORKING"}},"i":{i}}}}}'.encode()
        result = ingestor.ingest_callback(
            _envelope(scope_id=scope_id, dispatch_id=f"dsp_{i}", body=body),
            {"i": i},
        )
        assert result.status == "ok"
    assert store.temporal_event_count(scope_id) == 600
    assert store.revision_of(scope_id) == 600
    sequences = [e["sequence"] for e in store.temporal_events(scope_id)]
    assert sequences == list(range(1, 601))


# ---------------------------------------------------------------------------
# Callback-origin control bundle — never double bridge-enqueued
# ---------------------------------------------------------------------------


def _manager_with_transition(tmp_path):
    manager = MissionRuntimeManager(state_path=tmp_path / "coordinator-state.json")
    runtime = manager.admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-1", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    return manager, runtime, dispatch


def test_callback_origin_receipts_bundled_not_bridge_enqueued(
    tmp_path, store, scope_factory
):
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    bridge = MemoryLifecycleBridge(ingestor)
    ingestor.set_bridge(bridge)
    ingestor.activate_scope("ctx-1", 0)
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id

    manager, runtime, dispatch = _manager_with_transition(tmp_path)
    runtime.attach_receipt_sink(ingestor.receipt_sink)

    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_ACCEPTED"}}}'
    envelope = AuthenticatedCallbackEnvelope.build(
        scope_id=scope_id,
        dispatch_id=dispatch.dispatch_id,
        worker_task_id="worker-1",
        actor_id="Alice",
        runtime_epoch=0,
        callback_kind="status_update",
        normalized_state="ACCEPTED",
        body_sha256=_sha256_bytes(body),
    )

    with ingestor.callback_bundle() as receipts:
        manager.handle_callback(
            "ctx-1", "worker-1", "ACCEPTED", source="push_callback", result="ok"
        )
    assert len(receipts) == 1  # ACCEPTED transition produced one journal entry

    result = ingestor.ingest_callback(envelope, {"ok": 1}, control_receipts=receipts)
    assert result.status == "ok"
    assert bridge.pending_count() == 0  # callback receipts were NOT bridge-enqueued

    events = store.temporal_events(scope_id)
    kinds = {e["event_type"] for e in events}
    assert "callback.status_update" in kinds
    assert any(e["event_type"].startswith("control.") for e in events)
    # one callback event + one control-lifecycle event
    assert len(events) == 2
    assert len(store.outbox_entries(scope_id)) == 2
    assert store.revision_of(scope_id) == 2


def test_control_receipt_at_most_one_bundle(tmp_path, store, scope_factory):
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    ingestor.activate_scope("ctx-1", 0)
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id

    manager, runtime, dispatch = _manager_with_transition(tmp_path)
    entry = runtime.last_control_entry(dispatch.dispatch_id)
    assert entry is not None

    first = ingestor.ingest_control_receipt(entry)
    assert first.status == "ok"
    second = ingestor.ingest_control_receipt(entry)
    assert second.status == "duplicate"
    assert second.event_id == first.event_id

    events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"].startswith("control.")
    ]
    assert len(events) == 1
    assert len(store.control_receipts(scope_id)) == 1
    assert len(store.outbox_entries(scope_id)) == 1


# ---------------------------------------------------------------------------
# Crash injection and restart reconciliation
# ---------------------------------------------------------------------------


def test_canonical_bundle_all_or_none_on_crash_before_commit(tmp_path, scope_factory):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope_id = scope_factory.activate(store, "ctx-1", 0)

    with pytest.raises(RuntimeError, match="crash before commit"):
        with store.canonical_transaction() as tx:
            tx.append_temporal_event(
                event_id="evt_crash",
                scope_id=scope_id,
                sequence=1,
                event_type="callback.status_update",
                occurred_at="now",
                ingested_at="now",
                actor_id="Alice",
                logical_task_id=None,
                dispatch_id="dsp_crash",
                worker_task_id="w",
                tool_call_id=None,
                success=None,
                error=None,
                payload="{}",
                causation_id="c",
                correlation_id="d",
                idempotency_key="k",
            )
            tx.bump_revision_in_tx(scope_id)
            tx.write_outbox(
                outbox_id="out_crash",
                scope_id=scope_id,
                event_id="evt_crash",
                export_kind="temporal",
                payload_sha256="x",
            )
            raise RuntimeError("crash before commit")

    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.outbox_entries(scope_id) == []
    assert store.outbox_entry("out_crash") is None


def test_restart_reconciliation_after_commit_before_publish(tmp_path):
    """Bridge never ran: journal entries must reconcile to one bundle each and
    a second restart must not duplicate events/outbox."""
    manager = MissionRuntimeManager(state_path=tmp_path / "coordinator-state.json")
    runtime = manager.admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-1", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")
    entries = manager.control_journal_entries(dispatch_id=dispatch.dispatch_id)
    assert len(entries) == 2

    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    bridge = MemoryLifecycleBridge(ingestor)
    ingestor.set_bridge(bridge)
    ingestor.activate_scope("ctx-1", 0)
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id

    recovery = MemoryRecovery(store, ingestor, scope_factory, bridge)
    written = recovery.reconcile_control_journal(entries)
    assert written == 2

    events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"].startswith("control.")
    ]
    assert len(events) == 2
    assert len(store.outbox_entries(scope_id)) == 2
    assert len(store.control_receipts(scope_id)) == 2

    # Second restart: reconciliation is a no-op for matching receipts.
    written2 = recovery.reconcile_control_journal(entries)
    assert written2 == 0
    assert store.temporal_event_count(scope_id) == 2
    assert len(store.outbox_entries(scope_id)) == 2

    # Old scope is closed; reconciliation of the old scope writes nothing new.
    recovery.close_old_scope("ctx-1", 0)
    assert recovery.reconcile_control_journal(entries) == 0
    assert store.temporal_event_count(scope_id) == 2


def test_control_receipt_digest_mismatch_fails_loud_with_zero_writes(
    tmp_path, scope_factory
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    scope_id = scope_factory.activate(store, "ctx-1", 0)

    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-1", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    entry = runtime.last_control_entry(dispatch.dispatch_id)
    assert entry is not None
    assert ingestor.ingest_control_receipt(entry).status == "ok"

    # A forged journal with the same transition id but a different digest.
    from a2a.coordinator.memory.contracts import ControlTransitionJournalEntry

    forged = ControlTransitionJournalEntry(
        context_id="ctx-1",
        runtime_epoch=0,
        dispatch_id=entry.dispatch_id,
        control_revision=entry.control_revision,
        previous_state=entry.previous_state,
        state=entry.state,
        source="forged",
        observed_at=entry.observed_at,
        result_digest=None,
        journal_sha256="f" * 64,
    )
    from a2a.coordinator.memory.ingestor import ControlReceiptConflictError

    with pytest.raises(ControlReceiptConflictError):
        ingestor.ingest_control_receipt(forged)

    assert store.temporal_event_count(scope_id) == 1
    assert store.revision_of(scope_id) == 1
    assert len(store.outbox_entries(scope_id)) == 1


def test_callback_then_restart_reconciliation_race_stays_single_bundle(
    tmp_path, scope_factory
):
    """A control receipt written by the callback bundle must be seen as a
    duplicate by a later restart reconciliation; the pre-callback DISPATCHING
    receipt (never bridged) is written exactly once.  Each (scope, dispatch,
    control_revision) keeps exactly one control event/receipt/outbox."""
    from a2a.coordinator.memory.ingestor import MemoryLifecycleBridge
    from a2a.coordinator.memory.recovery import MemoryRecovery

    store = MemoryStore(tmp_path / "memory.sqlite3")
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    bridge = MemoryLifecycleBridge(ingestor)
    ingestor.set_bridge(bridge)
    ingestor.activate_scope("ctx-1", 0)
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id

    manager, runtime, dispatch = _manager_with_transition(tmp_path)
    entries_before = manager.control_journal_entries(dispatch_id=dispatch.dispatch_id)
    assert len(entries_before) == 1  # DISPATCHING

    runtime.attach_receipt_sink(ingestor.receipt_sink)

    # Callback-origin path bundles the ACCEPTED control receipt.
    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_ACCEPTED"}}}'
    envelope = AuthenticatedCallbackEnvelope.build(
        scope_id=scope_id,
        dispatch_id=dispatch.dispatch_id,
        worker_task_id="worker-1",
        actor_id="Alice",
        runtime_epoch=0,
        callback_kind="status_update",
        normalized_state="ACCEPTED",
        body_sha256=_sha256_bytes(body),
    )
    with ingestor.callback_bundle() as receipts:
        manager.handle_callback(
            "ctx-1", "worker-1", "ACCEPTED", source="push_callback", result="ok"
        )
    assert len(receipts) == 1
    assert (
        ingestor.ingest_callback(envelope, {"ok": 1}, control_receipts=receipts).status
        == "ok"
    )

    # Restart reconciliation races against the callback-written ACCEPTED receipt:
    # it is a duplicate; the never-bridged DISPATCHING receipt is written once.
    recovery = MemoryRecovery(store, ingestor, scope_factory, bridge)
    all_entries = manager.control_journal_entries(dispatch_id=dispatch.dispatch_id)
    assert len(all_entries) == 2  # DISPATCHING + ACCEPTED
    written = recovery.reconcile_control_journal(all_entries)
    assert written == 1  # only DISPATCHING was missing

    control_events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"].startswith("control.")
    ]
    assert len(control_events) == 2  # one per control revision
    assert len(store.control_receipts(scope_id)) == 2
    assert {r["control_revision"] for r in store.control_receipts(scope_id)} == {1, 2}
    # callback event + 2 control events, no duplicates
    assert store.temporal_event_count(scope_id) == 3
    assert len(store.outbox_entries(scope_id)) == 3

    # A second reconciliation adds nothing.
    assert recovery.reconcile_control_journal(all_entries) == 0
    assert store.temporal_event_count(scope_id) == 3
    assert len(store.outbox_entries(scope_id)) == 3


# ---------------------------------------------------------------------------
# Redaction across all listed sinks
# ---------------------------------------------------------------------------


def test_valid_callback_never_persists_raw_secret_anywhere(
    ingestor, store, scope_factory, tmp_path
):
    scope_id = _scope_id_of(scope_factory)
    body_text = json.dumps(
        {
            "statusUpdate": {
                "taskId": "worker-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {
                        "parts": [
                            {
                                "text": (
                                    f"[Result] report_observation: obs\n"
                                    f"Authorization: Bearer {SECRET} hmac={HMAC_HEX}"
                                )
                            }
                        ]
                    },
                },
            }
        }
    ).encode("utf-8")

    sanitized = ingestor.redaction.sanitize_callback(json.loads(body_text))
    assert SECRET not in json.dumps(sanitized)
    assert HMAC_HEX not in json.dumps(sanitized)

    envelope = _envelope(
        scope_id=scope_id,
        body=body_text,
        state="RUNNING",
        worker_task_id="worker-1",
    )
    result = ingestor.ingest_callback(envelope, sanitized)
    assert result.status == "ok"

    # SQLite payload has no raw secret.
    events = store.temporal_events(scope_id)
    assert SECRET not in json.dumps(events)
    assert HMAC_HEX not in json.dumps(events)

    # Security audit has no raw secret.
    assert SECRET not in json.dumps(store.security_audit_entries())
    assert HMAC_HEX not in json.dumps(store.security_audit_entries())

    # Legacy EventStore NDJSON is sanitized (defensive boundary).
    es = EventStore(log_dir=str(tmp_path / "events"))
    es.append(
        "worker-1",
        "observation_report",
        text=f"Authorization: Bearer {SECRET} hmac={HMAC_HEX}",
        observation={"secret": SECRET, "name": "FireA"},
    )
    ndjson = (tmp_path / "events" / "events_worker-1.ndjson").read_text()
    assert SECRET not in ndjson
    assert HMAC_HEX not in ndjson
    # Observation is not serialized to NDJSON; check the in-memory view instead.
    observations = es.get_recent_observations()
    assert observations
    assert SECRET not in json.dumps(observations)
    assert observations[0]["name"] == "FireA"

    # SemanticMapStore JSONL is sanitized.
    sem = SemanticMapStore(jsonl_path=str(tmp_path / "semantic_map.jsonl"))
    sem.ingest_observation(
        {
            "reporter": "Alice",
            "step": 1,
            "object_type": "fire",
            "name": "FireA",
            "position": [1, 1, 0],
            "note": f"Authorization: Bearer {SECRET} hmac={HMAC_HEX}",
        }
    )
    sem.ingest_observation(
        {
            "reporter": "Alice",
            "step": 1,
            "object_type": "agent",
            "name": "Alice",
            "position": [0, 0, 0],
            "note": f"secret={SECRET}",
        }
    )
    jsonl_text = (tmp_path / "semantic_map.jsonl").read_text()
    assert SECRET not in jsonl_text
    assert HMAC_HEX not in jsonl_text
    assert "FireA" in jsonl_text


def test_ingestor_never_reads_control_state():
    """MemoryIngestor only consumes runtime results; it cannot mutate a dispatch."""
    ingestor_api = {name for name in dir(MemoryIngestor)}
    assert "apply_physical_status" not in ingestor_api
    assert "transition_dispatch" not in ingestor_api
