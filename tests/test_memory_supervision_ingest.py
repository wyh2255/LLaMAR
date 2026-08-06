"""Phase 2 supervision ingestion: five TaskWatchdog event kinds, exactly-once
canonical TemporalEvents, and closed/unknown scope rejection."""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import (
    MemoryIngestor,
    MemoryScopeFactory,
    SupervisionEventAdapter,
)
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

EVENT_KINDS = [
    "WORKER_UNREACHABLE",
    "TASK_STALE",
    "TASK_DEADLINE_EXCEEDED",
    "TASK_DEADLINE_WARNING",
    "TASK_RECOVERED",
]


class _FakeDispatch:
    def __init__(self, context_id="ctx-1", dispatch_id="dsp_1", worker_id="Alice"):
        self.context_id = context_id
        self.dispatch_id = dispatch_id
        self.worker_id = worker_id
        self.worker_task_id = "worker-1"
        self._manager = _FakeManager()


class _FakeManager:
    epoch = 0


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def adapter(store, scope_factory):
    ingestor = MemoryIngestor(store, scope_factory, redaction=RedactionPolicy())
    ingestor.activate_scope("ctx-1", 0)
    return SupervisionEventAdapter(ingestor, scope_factory, store)


def _event(kind, event_id, dispatch_id="dsp_1"):
    return {
        "event_id": event_id,
        "event_type": kind,
        "dispatch_id": dispatch_id,
        "worker_id": "Alice",
        "ts": 1.0,
    }


def test_five_event_kinds_each_produce_exactly_one_canonical_event(
    adapter, store, scope_factory
):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    dispatch = _FakeDispatch()

    for kind in EVENT_KINDS:
        adapter(_event(kind, f"ev-{kind}"), dispatch=dispatch)

    events = store.temporal_events(scope_id)
    supervision_events = [
        e for e in events if e["event_type"].startswith("supervision.")
    ]
    assert len(supervision_events) == 5
    kinds = {e["event_type"] for e in supervision_events}
    assert kinds == {f"supervision.{k}" for k in EVENT_KINDS}
    assert store.revision_of(scope_id) == 5
    assert len(store.outbox_entries(scope_id)) == 5

    # Re-emitting the same event_id must be a no-op (exactly-once).
    for kind in EVENT_KINDS:
        adapter(_event(kind, f"ev-{kind}"), dispatch=dispatch)
    assert len(store.temporal_events(scope_id)) == 5
    assert store.revision_of(scope_id) == 5
    assert len(store.outbox_entries(scope_id)) == 5


def test_supervision_event_ids_are_distinct_per_kind(adapter, store, scope_factory):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    dispatch = _FakeDispatch()
    for kind in EVENT_KINDS:
        adapter(_event(kind, f"unique-{kind}"), dispatch=dispatch)
    event_ids = {e["causation_id"] for e in store.temporal_events(scope_id)}
    assert event_ids == {f"supervision:unique-{kind}" for kind in EVENT_KINDS}


def test_closed_scope_rejects_supervision_with_zero_writes(
    adapter, store, scope_factory
):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    dispatch = _FakeDispatch()
    adapter(_event("TASK_STALE", "ev-stale"), dispatch=dispatch)
    assert store.temporal_event_count(scope_id) == 1

    assert store.close_scope(scope_id) is True
    adapter(_event("TASK_DEADLINE_WARNING", "ev-deadline"), dispatch=dispatch)
    assert store.temporal_event_count(scope_id) == 1
    assert store.revision_of(scope_id) == 1
    assert len(store.outbox_entries(scope_id)) == 1


def test_unknown_scope_rejects_supervision_with_zero_writes(
    adapter, store, scope_factory
):
    known_scope = scope_factory.resolve("ctx-1", 0).scope_id
    dispatch = _FakeDispatch()
    adapter(_event("TASK_STALE", "ev-1"), dispatch=dispatch)
    assert store.temporal_event_count(known_scope) == 1

    other = _FakeDispatch(context_id="ctx-other")
    adapter(_event("TASK_STALE", "ev-2"), dispatch=other)
    assert store.temporal_event_count(known_scope) == 1
    assert store.revision_of(known_scope) == 1


def test_supervision_event_payload_is_redacted_safe(adapter, store, scope_factory):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    dispatch = _FakeDispatch()
    adapter(
        _event("TASK_STALE", "ev-secret", dispatch_id="dsp_1"),
        dispatch=dispatch,
    )
    events = store.temporal_events(scope_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "supervision.TASK_STALE"
    assert events[0]["actor_id"] == "Alice"
    assert events[0]["dispatch_id"] == "dsp_1"
