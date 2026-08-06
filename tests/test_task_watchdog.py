"""Tests for TaskWatchdog and SupervisionStateStore (Phase 3)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from a2a.coordinator.event_store import EventStore
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.worker_registry import WorkerRegistry
from a2a.coordinator.task_watchdog import TaskWatchdog, WatchdogConfig
from a2a.coordinator.supervision_state_store import SupervisionStateStore


class MockWorkerRegistry(WorkerRegistry):
    """WorkerRegistry with deterministic contact times for tests."""

    def __init__(self):
        super().__init__()
        self._contact_overrides: dict[str, float] = {}

    def override_contact(self, worker_id: str, ts: float) -> None:
        self._last_contact_at[worker_id] = ts


class MockBarrier:
    def __init__(self, step=0, coverage=0.0, transport_rate=0.0, finished=False):
        self._step_counter = step
        self._coverage = coverage
        self._transport_rate = transport_rate
        self._finished = finished

    def is_finished(self):
        return self._finished

    def get_metrics(self):
        return {
            "coverage": self._coverage,
            "transport_rate": self._transport_rate,
            "steps": self._step_counter,
            "finished": self._finished,
        }


@pytest.fixture
def event_store():
    return EventStore()


@pytest.fixture
def supervision_store(event_store):
    return SupervisionStateStore()


@pytest.fixture
def registry():
    return MockWorkerRegistry()


@pytest.fixture
def task_store():
    store = TaskStore(
        original_request="test", router=MagicMock(), max_tasks=10, context_id="ctx-1"
    )
    store.update_plan(
        [
            {
                "task_id": "dispatch-1",
                "worker_id": "Alice",
                "description": "test task",
            }
        ]
    )
    store.register_worker_task_id("dispatch-1", "worker-task-1")
    return store


@pytest.fixture
def watchdog(registry, event_store, supervision_store, task_store):
    barrier = MockBarrier(step=1)
    config = WatchdogConfig(
        task_stale_seconds=2.0,
        worker_unreachable_seconds=2.0,
        deadline_warning_seconds=5.0,
        task_hard_deadline_seconds=10.0,
        grace_period_seconds=0.0,
        watchdog_tick_seconds=0.05,
    )
    wd = TaskWatchdog(
        worker_registry=registry,
        event_store=event_store,
        supervision_store=supervision_store,
        barrier=barrier,
        config=config,
    )
    wd.set_task_store(task_store)
    return wd


# ------------------------------------------------------------------
# SupervisionStateStore tests
# ------------------------------------------------------------------


def test_supervision_store_get_or_create():
    store = SupervisionStateStore()
    state = store.get_or_create("dispatch-1", worker_id="Alice")
    assert state.dispatch_id == "dispatch-1"
    assert state.worker_id == "Alice"
    assert state.supervision_state == "HEALTHY"
    assert state.created_at > 0

    # Second call returns same state
    state2 = store.get_or_create("dispatch-1", worker_id="Bob")
    assert state2 is state
    assert state2.worker_id == "Alice"  # not overwritten


def test_supervision_store_acknowledge_event():
    store = SupervisionStateStore()
    state = store.get_or_create("dispatch-1")
    state.unacknowledged_events.append({"event_id": "ev-1", "event_type": "TASK_STALE"})
    state.active_alerts["TASK_STALE"] = "ev-1"
    store.update("dispatch-1", state)

    store.acknowledge_event("dispatch-1", "ev-1")
    state = store.get("dispatch-1")
    assert state.unacknowledged_events == []
    assert "TASK_STALE" not in state.active_alerts
    assert "ev-1" in state.acknowledged_event_ids


# ------------------------------------------------------------------
# TaskWatchdog progress/contact tests
# ------------------------------------------------------------------


def test_record_contact_updates_last_contact(event_store, supervision_store):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    wd.record_contact("dispatch-1", worker_id="Alice", worker_task_id="wt-1")
    state = supervision_store.get("dispatch-1")
    assert state.last_contact_at > 0


def test_record_progress_updates_last_progress_and_recovers_stale(
    event_store, supervision_store
):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    # Put task in STALE
    state = supervision_store.get_or_create("dispatch-1", worker_id="Alice")
    state.supervision_state = "STALE"
    state.active_alerts["TASK_STALE"] = "ev-1"
    state.unacknowledged_events.append({"event_id": "ev-1", "event_type": "TASK_STALE"})
    supervision_store.update("dispatch-1", state)

    before = state.last_progress_at
    wd.record_progress(
        "dispatch-1",
        worker_id="Alice",
        worker_task_id="wt-1",
        source="observation",
        step=5,
    )
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert state.last_progress_at > before
    assert state.supervision_state == "HEALTHY"
    assert "TASK_STALE" not in state.active_alerts
    assert any(e["event_type"] == "TASK_RECOVERED" for e in state.unacknowledged_events)


def test_record_state_change_marks_terminal(event_store, supervision_store):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    wd.record_state_change(
        "dispatch-1",
        worker_id="Alice",
        worker_task_id="wt-1",
        state_name="TASK_STATE_COMPLETED",
    )
    state = supervision_store.get("dispatch-1")
    assert state.terminal is True
    assert state.supervision_state == "TERMINAL"


# ------------------------------------------------------------------
# TaskWatchdog detection tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_stale_after_progress_timeout(watchdog, supervision_store):
    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"

    # Artificially age progress timestamp
    state.last_progress_at = time.monotonic() - 10.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    assert "TASK_STALE" in state.active_alerts


@pytest.mark.asyncio
async def test_detects_worker_unreachable(watchdog, registry, supervision_store):
    registry.register_from_ws("Alice", "http://localhost:9000")
    registry.override_contact("Alice", time.monotonic() - 10.0)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "WORKER_UNREACHABLE"
    assert "WORKER_UNREACHABLE" in state.active_alerts


@pytest.mark.asyncio
async def test_detects_deadline_exceeded(watchdog, supervision_store):
    cfg = watchdog._config
    state = supervision_store.get_or_create(
        "dispatch-1",
        worker_id="Alice",
        config={
            "stale_threshold_seconds": cfg.task_stale_seconds,
            "unreachable_threshold_seconds": cfg.worker_unreachable_seconds,
            "deadline_warning_seconds": cfg.deadline_warning_seconds,
            "hard_deadline_seconds": cfg.task_hard_deadline_seconds,
            "grace_period_seconds": cfg.grace_period_seconds,
        },
    )
    state.created_at = time.monotonic() - 20.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert state.supervision_state == "DEADLINE_EXCEEDED"
    assert "TASK_DEADLINE_EXCEEDED" in state.active_alerts


@pytest.mark.asyncio
async def test_terminal_task_skips_supervision(
    watchdog, event_store, supervision_store
):
    state = supervision_store.get_or_create("dispatch-1", worker_id="Alice")
    state.terminal = True
    state.last_progress_at = time.monotonic() - 100.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert "TASK_STALE" not in state.active_alerts


# ------------------------------------------------------------------
# TaskWatchdog ticker tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_start_stop():
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
    )
    await wd.start()
    assert wd.health()["running"] is True
    await wd.stop()
    assert wd.health()["running"] is False


@pytest.mark.asyncio
async def test_watchdog_ticker_records_latency():
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        config=WatchdogConfig(watchdog_tick_seconds=0.05),
    )
    await wd.start()
    await asyncio.sleep(0.15)
    await wd.stop()
    health = wd.health()
    assert health["last_check_at"] > 0
    assert health["last_check_latency_ms"] >= 0


# ------------------------------------------------------------------
# WorkerRegistry contact tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_registry_update_contact_and_heartbeat():
    registry = WorkerRegistry()
    registry.register_from_ws("Alice", "http://localhost:9000")
    before_contact = registry.get_last_contact_at("Alice")
    before_hb = registry.get("Alice").last_heartbeat

    await asyncio.sleep(0.01)
    registry.update_heartbeat("Alice")
    after_contact = registry.get_last_contact_at("Alice")
    after_hb = registry.get("Alice").last_heartbeat

    assert after_contact > before_contact
    assert after_hb > before_hb

    registry.update_contact("Alice")
    assert registry.get_last_contact_at("Alice") > after_contact


# ------------------------------------------------------------------
# Domain delta progress tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervision_events_route_to_memory_sink_with_dispatch():
    """Phase 2: every supervision emit reaches the dispatch-bound canonical
    writer (SupervisionEventAdapter) with the resolved PhysicalDispatch."""
    from a2a.coordinator.mission_runtime import MissionRuntimeManager

    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-1", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-task-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")

    captured: list = []

    def sink(event, dispatch):
        captured.append((event["event_type"], dispatch.dispatch_id))

    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        supervision_event_sink=sink,
    )
    wd.set_runtime(runtime)
    wd.set_task_store(
        TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    )

    kinds = [
        "WORKER_UNREACHABLE",
        "TASK_STALE",
        "TASK_DEADLINE_EXCEEDED",
        "TASK_DEADLINE_WARNING",
        "TASK_RECOVERED",
    ]
    for i, kind in enumerate(kinds):
        wd._emit_to_memory(
            {
                "event_id": f"ev-{i}",
                "event_type": kind,
                "dispatch_id": dispatch.dispatch_id,
                "worker_id": "Alice",
            }
        )

    assert [(k, d) for k, d in captured] == [
        (kind, dispatch.dispatch_id) for kind in kinds
    ]


def test_supervision_emit_without_dispatch_keeps_only_local_diagnostic():
    """When no dispatch can be resolved, the canonical sink is never called."""
    captured: list = []

    def sink(event, dispatch):
        captured.append(event)

    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        supervision_event_sink=sink,
    )
    wd._emit_to_memory({"event_id": "ev-1", "event_type": "TASK_STALE"})
    assert captured == []


@pytest.mark.asyncio
async def test_progress_refreshed_on_domain_delta():
    barrier = MockBarrier(step=2, coverage=0.1, transport_rate=0.0)
    store = SupervisionStateStore()
    event_store = EventStore()
    registry = MockWorkerRegistry()
    task_store = TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    task_store.update_plan([{"task_id": "dispatch-1", "worker_id": "Alice"}])

    wd = TaskWatchdog(
        worker_registry=registry,
        event_store=event_store,
        supervision_store=store,
        barrier=barrier,
        config=WatchdogConfig(grace_period_seconds=0.0),
    )
    wd.set_task_store(task_store)

    # First check establishes baseline metrics
    await wd._check_all()
    state = store.get("dispatch-1")
    first_progress = state.last_progress_at

    # Change domain metric
    barrier._coverage = 0.5
    await wd._check_all()
    state = store.get("dispatch-1")
    assert state.last_progress_at > first_progress
    assert state.last_progress_step == 2
