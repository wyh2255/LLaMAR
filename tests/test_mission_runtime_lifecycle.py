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
