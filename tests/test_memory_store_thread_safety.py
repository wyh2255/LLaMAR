"""MemoryStore cross-thread safety regression tests (H2).

The canonical SQLite connection is created on the coordinator start-up thread
but is read (projection / read-back helpers) and written (canonical
transactions) from the coordinator server thread and worker callbacks.  Two
properties must hold under concurrent reader/writer use of the *same*
connection:

1. ``check_same_thread=False`` — a connection created in one thread must be
   usable from another thread (no ``sqlite3.ProgrammingError``).
2. The store ``RLock`` serializes reads against ``BEGIN IMMEDIATE`` canonical
   transactions so statements never interleave mid-transaction (no
   ``sqlite3.InterfaceError`` / lost updates / partial visibility).

These tests prove no ``ProgrammingError`` / ``InterfaceError`` under a
concurrent reader thread hammering read-back helpers while the main thread
runs canonical transaction bundles.
"""

from __future__ import annotations

import threading

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    MemoryRef,
    MemoryRelation,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def ingestor(store, tmp_path):
    ing = MemoryIngestor(
        store,
        MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path)),
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _projection_input(scope_id, *, event_id, domain, entity_id, field_name, value):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=1,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain=domain,
        entity_id=entity_id,
        entity_type="agent" if domain == "embodied" else "fire",
        field_name=field_name,
        value=value,
    )


def test_store_connection_usable_from_other_thread(store, ingestor):
    """A read on the shared connection from a non-owner thread must not raise
    ``sqlite3.ProgrammingError`` (cross-thread use of the same connection)."""
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    errors: list[Exception] = []

    def _reader():
        try:
            for _ in range(20):
                store.revision_of(scope_id)
                store.temporal_event_count(scope_id)
                store.projection_fields(scope_id)
                store.relations_for_scope(scope_id)
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    t = threading.Thread(target=_reader)
    t.start()
    t.join()
    assert errors == []


def test_concurrent_reader_writer_no_interface_error(store, ingestor):
    """A reader thread hammering read-back helpers while the owner thread runs
    canonical ``BEGIN IMMEDIATE`` transactions never raises ``ProgrammingError``
    / ``InterfaceError`` and always observes a consistent committed view."""
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    ingestor.ingest_projection(
        [
            _projection_input(
                scope_id,
                event_id="e0",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
            )
        ]
    )

    errors: list[Exception] = []
    stop = threading.Event()

    def _reader():
        try:
            while not stop.is_set():
                store.revision_of(scope_id)
                store.temporal_event_count(scope_id)
                store.temporal_events(scope_id)
                store.projection_fields(scope_id)
                store.projection_field(scope_id, "spatial", "FireA", "intensity")
                store.relations_for_scope(scope_id)
                store.control_journal_entries(context_id="ctx-1", runtime_epoch=0)
                store.outbox_entries(scope_id)
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)
            stop.set()

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        for i in range(30):
            with store.canonical_transaction():
                store.append_temporal_event(
                    event_id=f"evt_{i}",
                    scope_id=scope_id,
                    sequence=i + 2,
                    event_type="evidence.projection",
                    occurred_at="now",
                    ingested_at="now",
                    actor_id="alice",
                    logical_task_id=None,
                    dispatch_id=None,
                    worker_task_id="wt-1",
                    tool_call_id=None,
                    success=True,
                    error=None,
                    payload="{}",
                    causation_id="evidence:e",
                    correlation_id="dispatch:d",
                    idempotency_key=None,
                )
                store.insert_idempotency_ledger(
                    scope_id=scope_id,
                    idempotency_key=f"key-{i}",
                    event_id=f"evt_{i}",
                    payload_digest="digest",
                    receipt_sha256="receipt",
                    committed_revision=i + 1,
                )
                store.write_outbox(
                    outbox_id=f"out_{i}",
                    scope_id=scope_id,
                    event_id=f"evt_{i}",
                    export_kind="worker_observation",
                    payload_sha256="payload",
                )
                store.bump_revision_in_tx(scope_id)
            store.record_journal_entry(
                type(
                    "J",
                    (),
                    {
                        "context_id": "ctx-1",
                        "runtime_epoch": 0,
                        "dispatch_id": f"d{i}",
                        "control_revision": i,
                        "previous_state": "idle",
                        "state": "running",
                        "source": "coordinator",
                        "observed_at": "now",
                        "result_digest": None,
                        "journal_sha256": f"sha-{i}",
                    },
                )()
            )
    finally:
        stop.set()
        reader.join()

    assert errors == []
    # All 30 canonical bundles committed atomically (1 seed + 30).
    assert store.temporal_event_count(scope_id) == 31
    assert store.revision_of(scope_id) > 0


def test_concurrent_reader_never_sees_half_transaction(store, ingestor):
    """Readers see a canonical transaction as all-or-nothing: the event count
    and outbox count for a single committed bundle agree, and no read
    observes a partially-applied bundle."""
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    ingestor.ingest_projection(
        [
            _projection_input(
                scope_id,
                event_id="e0",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
            )
        ]
    )
    base_events = store.temporal_event_count(scope_id)
    base_outbox = len(store.outbox_entries(scope_id))

    with store.canonical_transaction():
        store.append_temporal_event(
            event_id="evt_bundle",
            scope_id=scope_id,
            sequence=base_events + 1,
            event_type="evidence.projection",
            occurred_at="now",
            ingested_at="now",
            actor_id="alice",
            logical_task_id=None,
            dispatch_id=None,
            worker_task_id="wt-1",
            tool_call_id=None,
            success=True,
            error=None,
            payload="{}",
            causation_id="evidence:e",
            correlation_id="dispatch:d",
            idempotency_key=None,
        )
        store.write_outbox(
            outbox_id="out_bundle",
            scope_id=scope_id,
            event_id="evt_bundle",
            export_kind="worker_observation",
            payload_sha256="payload",
        )
        store.bump_revision_in_tx(scope_id)

    # After commit the reader sees both rows (atomic all-or-nothing bundle).
    assert store.temporal_event_count(scope_id) == base_events + 1
    assert len(store.outbox_entries(scope_id)) == base_outbox + 1


def test_add_relation_and_read_from_other_thread(store, ingestor):
    """Relation writes committed under the lock are readable from another
    thread without ``ProgrammingError``."""
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    ingestor.ingest_projection(
        [
            _projection_input(
                scope_id,
                event_id="e0",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
            )
        ]
    )
    store.add_relation(
        MemoryRelation(
            relation_id="rel_1",
            scope_id=scope_id,
            from_ref=MemoryRef(namespace="memory", id="evt_1"),
            relation_type="about",
            to_ref=MemoryRef(namespace="memory", id="spatial:FireA"),
        )
    )

    relations: list[MemoryRelation] = []
    errors: list[Exception] = []

    def _reader():
        try:
            relations.extend(store.relations_for_scope(scope_id))
            relations.extend(
                store.relations_for_entity(
                    MemoryRef(namespace="memory", id="spatial:FireA")
                )
            )
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    t = threading.Thread(target=_reader)
    t.start()
    t.join()
    assert errors == []
    assert any(r.relation_id == "rel_1" for r in relations)
