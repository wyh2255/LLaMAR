"""Focused Phase 0 MissionRuntime lifecycle contracts."""

from __future__ import annotations

import json

import httpx
import pytest

from a2a.coordinator.mission_runtime import (
    MissionAdmissionError,
    MissionRuntimeManager,
    PartitionTransition,
    PhysicalState,
)
from a2a.coordinator.server import create_server
from a2a.shared.types import DistributedTask
from a2a.coordinator.task_queue import TaskQueue


@pytest.mark.asyncio
async def test_admission_rejects_second_context_before_runtime_replacement():
    manager = MissionRuntimeManager()
    first = manager.admit("ctx-first")
    first.marker = "must-survive"

    with pytest.raises(MissionAdmissionError, match="mission_already_active") as exc:
        manager.admit("ctx-second")

    assert exc.value.code == "mission_already_active"
    assert manager.active_runtime is first
    assert manager.active_runtime.marker == "must-survive"


@pytest.mark.asyncio
async def test_physical_ids_are_opaque_and_status_transition_is_monotonic():
    runtime = MissionRuntimeManager().admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-task", "Alice")

    assert dispatch.dispatch_id.startswith("dsp_")
    assert dispatch.dispatch_id != "logical-task"
    assert runtime.resolve_dispatch("logical-task") is None

    runtime.register_worker_task(dispatch.dispatch_id, "worker-task-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_SUBMITTED", source="acceptance"
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_WORKING", source="callback"
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id,
        "TASK_STATE_COMPLETED",
        source="callback",
        result="done",
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_WORKING", source="late_callback"
    )
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_FAILED", source="duplicate_callback"
    )

    assert dispatch.state is PhysicalState.COMPLETED
    assert dispatch.result == "done"
    assert len(runtime.diagnostics) == 2


@pytest.mark.asyncio
async def test_abort_is_idempotent_cleans_futures_and_releases_admission():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-abort")
    dispatch = runtime.create_dispatch("logical-task", "Alice")
    future = runtime.register_future(dispatch.dispatch_id)
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_WORKING", source="sync"
    )

    await runtime.abort("parent_timeout")
    await runtime.abort("duplicate_shutdown")

    assert dispatch.state is PhysicalState.CANCELED
    assert future.cancelled()
    assert manager.active_runtime is None


@pytest.mark.asyncio
async def test_abort_keeps_dispatching_without_worker_task_in_cancel_pending():
    """Phase 6: abort marks aborted=True and releases admission even when
    remote cleanup is incomplete (CANCEL_PENDING).  Recovery flag preserved."""
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-cancel-pending")
    dispatch = runtime.create_dispatch("logical-cancel-pending", "Alice")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    await runtime.abort("cancel_before_worker_task_registration")
    await runtime.abort("duplicate_cancel_before_reconciliation")

    assert dispatch.state is PhysicalState.CANCEL_PENDING
    assert runtime.aborted is True
    assert runtime._recovery_pending is True
    assert manager.active_context_id is None


@pytest.mark.asyncio
async def test_stale_callback_route_cannot_mutate_new_context(tmp_path):
    server = create_server(verifier_enabled=False, log_dir=str(tmp_path))
    manager = server.mission_runtime_manager

    async def fake_canceler(worker_id: str, worker_task_id: str):
        return "TASK_STATE_CANCELED"

    manager.set_cancel_adapter(fake_canceler)
    old_runtime = manager.admit("ctx-old")
    old_dispatch = old_runtime.create_dispatch("logical-old", "Alice")
    old_runtime.register_worker_task(old_dispatch.dispatch_id, "worker-old")
    await old_runtime.abort("completed")

    new_runtime = manager.admit("ctx-new")
    new_dispatch = new_runtime.create_dispatch("logical-new", "Bob")
    new_runtime.register_worker_task(new_dispatch.dispatch_id, "worker-new")

    payload = {
        "statusUpdate": {
            "taskId": "worker-old",
            "contextId": "ctx-old",
            "status": {"state": "TASK_STATE_FAILED"},
        }
    }
    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/a2a/push-callback", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert new_dispatch.state is PhysicalState.PREPARED
    assert len(manager.diagnostics) <= manager.diagnostic_limit

    await new_runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_push_callback_route_enters_canonical_transition(tmp_path):
    server = create_server(verifier_enabled=False, log_dir=str(tmp_path))
    runtime = server.mission_runtime_manager.admit("ctx-route")
    dispatch = runtime.create_dispatch("logical-route", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-route")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for state in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
            response = await client.post(
                "/a2a/push-callback",
                json={
                    "statusUpdate": {
                        "taskId": "worker-route",
                        "contextId": "ctx-route",
                        "status": {"state": state},
                    }
                },
            )
            assert response.json()["status"] == "ok"

        artifact_response = await client.post(
            "/a2a/push-callback",
            json={
                "artifactUpdate": {
                    "taskId": "worker-route",
                    "contextId": "ctx-route",
                    "artifact": {"parts": [{"text": "evidence"}]},
                }
            },
        )
        assert artifact_response.json()["status"] == "ok"
        assert dispatch.state is PhysicalState.RUNNING

        response = await client.post(
            "/a2a/push-callback",
            json={
                "statusUpdate": {
                    "taskId": "worker-route",
                    "contextId": "ctx-route",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                }
            },
        )
        assert response.json()["status"] == "ok"

        late = await client.post(
            "/a2a/push-callback",
            json={
                "statusUpdate": {
                    "taskId": "worker-route",
                    "contextId": "ctx-route",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
        )

    assert late.json()["status"] == "ignored"
    assert dispatch.state is PhysicalState.COMPLETED
    assert dispatch.artifact == "evidence"
    await runtime.abort("test_cleanup")


def test_control_state_persistence_is_atomic_private_and_recovery_advances_epoch(
    tmp_path,
):
    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    manager.admit("ctx-before-restart")
    before = json.loads(state_path.read_text())
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert before["active_context_id"] == "ctx-before-restart"

    recovered = MissionRuntimeManager(state_path=state_path)
    recovered_epoch = recovered.recover()
    assert recovered_epoch > before["epoch"]
    assert recovered.active_runtime is None
    assert recovered.admit("ctx-after-restart").context_id == "ctx-after-restart"


def test_control_journal_persisted_atomically_with_control_snapshot(tmp_path):
    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-snapshot")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "w")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    journal = state["journal"]
    assert len(journal) == 2
    assert journal[0]["control_revision"] == 1
    assert journal[0]["state"] == "DISPATCHING"
    assert journal[1]["control_revision"] == 2
    assert journal[1]["state"] == "ACCEPTED"
    assert journal[1]["journal_sha256"]

    # PhysicalDispatch.state remains the sole task-control truth.
    assert dispatch.state is PhysicalState.ACCEPTED
    assert state["dispatches"][0]["state"] == "ACCEPTED"


def test_partition_transition_is_only_a_durable_protocol_dto():
    transition = PartitionTransition(
        transition_id="tr-1",
        context_id="ctx-1",
        before_members=("Alice",),
        after_members=("Bob",),
    )
    assert transition.status == "PREPARING"
    assert transition.before_members == ("Alice",)
    assert transition.after_members == ("Bob",)
    assert transition.member_acks == {}


def test_task_queue_lists_tasks_by_context():
    queue = TaskQueue()
    queue.enqueue(DistributedTask("task-a", "agent", "a", context_id="ctx-1"))
    queue.enqueue(DistributedTask("task-b", "agent", "b", context_id="ctx-2"))
    queue.enqueue(DistributedTask("task-c", "agent", "c", context_id="ctx-1"))

    assert [task.task_id for task in queue.list_by_context("ctx-1")] == [
        "task-a",
        "task-c",
    ]


# ---------------------------------------------------------------------------
# Phase 2: journal receipt seam wiring on newly admitted runtimes
# ---------------------------------------------------------------------------


def test_runtime_created_hook_wires_receipt_sink_to_future_runtimes(tmp_path):
    """Internal-origin transitions reach the MemoryLifecycleBridge through the
    lock-external receipt seam installed by the runtime_created hook."""
    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import (
        MemoryIngestor,
        MemoryLifecycleBridge,
        MemoryScopeFactory,
    )
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.mission_runtime import MissionRuntimeManager

    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(store, scope_factory)
    bridge = MemoryLifecycleBridge(ingestor)
    ingestor.set_bridge(bridge)

    manager = MissionRuntimeManager(state_path=tmp_path / "state.json")
    manager.set_runtime_created_hook(
        lambda rt: rt.attach_receipt_sink(ingestor.receipt_sink)
    )
    ingestor.activate_scope("ctx-hook", 0)

    runtime = manager.admit("ctx-hook")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    # The bridge consumed the receipt and wrote one control-lifecycle event.
    scope_id = scope_factory.resolve("ctx-hook", 0).scope_id
    events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"].startswith("control.")
    ]
    assert len(events) == 1
    assert events[0]["event_type"] == "control.dispatch.DISPATCHING"
    assert bridge.pending_count() == 0
    assert len(store.control_receipts(scope_id)) == 1
    assert len(store.outbox_entries(scope_id)) == 1
