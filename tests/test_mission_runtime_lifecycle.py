"""Focused Phase 0 MissionRuntime lifecycle contracts."""

from __future__ import annotations

import json

import httpx
import pytest

from a2a.coordinator.event_store import event_store
from a2a.coordinator.mission_graph import MissionGraph
from a2a.coordinator.mission_runtime import (
    MissionAdmissionError,
    MissionRuntimeManager,
    PartitionTransition,
    PhysicalState,
)
from a2a.coordinator.server import create_server
from a2a.coordinator.team_partition_service import TeamPartitionService
from a2a.coordinator.worker_registry import WorkerRegistry
from a2a.shared.types import DistributedTask
from a2a.coordinator.task_queue import TaskQueue
from sar_orch.map import SemanticMapStore


def _obs_status_payload(
    *,
    task_id: str,
    context_id: str | None,
    text: str,
    state: str = "TASK_STATE_WORKING",
) -> dict:
    payload: dict = {
        "statusUpdate": {
            "taskId": task_id,
            "status": {
                "state": state,
                "message": {"parts": [{"text": text}]},
            },
        }
    }
    if context_id is not None:
        payload["statusUpdate"]["contextId"] = context_id
    return payload


def _report_observation_text(observation: dict) -> str:
    data = {
        "ev": "tool_result",
        "tool_name": "report_observation",
        "success": True,
        "content": json.dumps(observation),
    }
    return f"[Result] report_observation: observed\n[DATA]\n{json.dumps(data)}"


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
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
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
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
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


@pytest.mark.asyncio
async def test_terminal_dispatch_late_working_callback_still_ingests_observation(
    tmp_path,
):
    """LL-V01 baseline: observation ingestion is decoupled from the physical
    state machine.  A repeated WORKING callback after COMPLETED must still be
    ingested while the terminal dispatch state is not advanced or mutated."""
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    runtime = server.mission_runtime_manager.admit("ctx-terminal-obs")
    dispatch = runtime.create_dispatch("logical-terminal-obs", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-terminal-obs")
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
    assert dispatch.state is PhysicalState.COMPLETED

    observation = {
        "reporter": "Alice",
        "step": 5,
        "object_type": "fire",
        "name": "LateFire",
        "position": [4, 4, 0],
        "attributes": {"intensity": "Low"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id="worker-terminal-obs",
                context_id="ctx-terminal-obs",
                text=_report_observation_text(observation),
            ),
        )

    body = response.json()
    assert body["status"] == "ok"
    assert body["ingested_observations"] == 1
    names = {o["name"] for o in event_store.get_recent_observations()}
    assert "LateFire" in names
    assert "LateFire" in server._semantic_map.fires
    # Physical state machine is not advanced or mutated by the late callback.
    assert dispatch.state is PhysicalState.COMPLETED
    assert dispatch.result == "done"
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_late_callback_after_abort_before_new_admission_is_rejected(tmp_path):
    """LL-V01 guard: after abort() and before a new context is admitted, the
    push-callback falls into the legacy path, which performs no stale/terminal
    checks.  Callbacks from worker tasks of the aborted runtime must be
    rejected instead of written to the EventStore / SemanticMap."""
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    manager = server.mission_runtime_manager

    async def fake_canceler(worker_id: str, worker_task_id: str):
        return "TASK_STATE_CANCELED"

    manager.set_cancel_adapter(fake_canceler)
    old_runtime = manager.admit("ctx-aborted")
    old_dispatch = old_runtime.create_dispatch("logical-aborted", "Alice")
    old_runtime.register_worker_task(old_dispatch.dispatch_id, "worker-aborted")
    await old_runtime.abort("completed")
    assert manager.active_runtime is None

    observation = {
        "reporter": "Alice",
        "step": 6,
        "object_type": "fire",
        "name": "GhostFire",
        "position": [9, 9, 0],
        "attributes": {"intensity": "High"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id="worker-aborted",
                context_id="ctx-aborted",
                text=_report_observation_text(observation),
            ),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "aborted_worker_task"
    assert event_store.get_recent_observations() == []
    assert (
        event_store.get_summary(
            task_ids={"worker-aborted", old_dispatch.dispatch_id}
        )
        == ""
    )
    assert server._semantic_map.fires == {}
    assert any(
        d["reason"] == "aborted_worker_task_callback" for d in manager.diagnostics
    )


@pytest.mark.asyncio
async def test_stale_context_callback_writes_no_event_store_or_semantic_map(tmp_path):
    """LL-V01: extends test_stale_callback_route_cannot_mutate_new_context —
    a late callback routed to a stale context must be ignored without writing
    EventStore or the SemanticMap."""
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    manager = server.mission_runtime_manager

    async def fake_canceler(worker_id: str, worker_task_id: str):
        return "TASK_STATE_CANCELED"

    manager.set_cancel_adapter(fake_canceler)
    old_runtime = manager.admit("ctx-stale-old")
    old_dispatch = old_runtime.create_dispatch("logical-stale-old", "Alice")
    old_runtime.register_worker_task(old_dispatch.dispatch_id, "worker-stale-old")
    await old_runtime.abort("completed")

    new_runtime = manager.admit("ctx-stale-new")
    new_dispatch = new_runtime.create_dispatch("logical-stale-new", "Bob")
    new_runtime.register_worker_task(new_dispatch.dispatch_id, "worker-stale-new")

    observation = {
        "reporter": "Alice",
        "step": 7,
        "object_type": "fire",
        "name": "StaleFire",
        "position": [2, 2, 0],
        "attributes": {"intensity": "Medium"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id="worker-stale-old",
                context_id="ctx-stale-old",
                text=_report_observation_text(observation),
            ),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "stale_context"
    assert event_store.get_recent_observations() == []
    assert (
        event_store.get_summary(
            task_ids={"worker-stale-old", old_dispatch.dispatch_id}
        )
        == ""
    )
    assert server._semantic_map.fires == {}
    assert new_dispatch.state is PhysicalState.PREPARED

    await new_runtime.abort("test_cleanup")


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


# ---------------------------------------------------------------------------
# 问题 1 回归：多参与者节点激活前的 team 协议能力 fail-fast
# ---------------------------------------------------------------------------


def _all_ack_delivery(tr):
    return {w: True for w in tr.affected_workers}


@pytest.mark.asyncio
async def test_multi_participant_activation_fails_fast_when_worker_lacks_team_protocol():
    """任一 participant 未启用 team 协议（supports_team_protocol=False）时，
    多参与者节点激活必须快速失败：返回 team_setup_failed、不调用 team
    delivery adapter（不走 30s ACK saga）、不分配 dispatch。"""
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-team-cap-fail")

    # WorkerRegistry：Alice 支持 team 协议，Bob 不支持（enable_peer_mail=False）
    wreg = WorkerRegistry()
    wreg.register_from_ws("Alice", "http://alice:9000/", supports_team_protocol=True)
    wreg.register_from_ws("Bob", "http://bob:9000/", supports_team_protocol=False)
    runtime.set_worker_registry(wreg)

    # TeamPartitionService 接上 delivery adapter → team path 本应触发
    tps = TeamPartitionService()
    tps.ensure_singletons(["Alice", "Bob"])
    delivery_calls: list = []

    async def delivery(tr):
        delivery_calls.append(tr)
        return _all_ack_delivery(tr)

    tps.set_delivery_adapter(delivery)

    graph = MissionGraph()
    graph.replace(
        [
            {
                "logical_id": "rescue",
                "participant_ids": ["Alice", "Bob"],
                "objective": "Rescue together",
            },
        ]
    )

    result = await runtime.activate_plan_node("rescue", graph, team_service=tps)

    assert result["success"] is False
    assert result["error"] == "team_setup_failed"
    assert "Bob" in result["reason"]
    assert "lack team protocol support" in result["reason"]
    # 未走 Team ACK saga：delivery adapter 未被调用
    assert delivery_calls == []
    # 未分配 dispatch（fail-fast 发生在 prepare_activation 之前）
    assert runtime.dispatch_count == 0


@pytest.mark.asyncio
async def test_multi_participant_activation_proceeds_when_all_workers_support_team_protocol():
    """对照：所有 participant 都支持 team 协议时，注入 WorkerRegistry 不改变
    原有行为——正常走 prepare_activation → Team ACK saga → dispatch fan-out。"""
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-team-cap-ok")

    async def ok_adapter(worker_id, prompt, callback_url, dispatch_id, context_id):
        return f"wtid-{worker_id}"

    runtime.set_dispatch_adapter(ok_adapter)

    wreg = WorkerRegistry()
    wreg.register_from_ws("Alice", "http://alice:9000/", supports_team_protocol=True)
    wreg.register_from_ws("Bob", "http://bob:9000/", supports_team_protocol=True)
    runtime.set_worker_registry(wreg)

    tps = TeamPartitionService()
    tps.ensure_singletons(["Alice", "Bob"])
    delivery_calls: list = []

    async def delivery(tr):
        delivery_calls.append(tr)
        return _all_ack_delivery(tr)

    tps.set_delivery_adapter(delivery)

    graph = MissionGraph()
    graph.replace(
        [
            {
                "logical_id": "rescue",
                "participant_ids": ["Alice", "Bob"],
                "objective": "Rescue together",
            },
        ]
    )

    result = await runtime.activate_plan_node("rescue", graph, team_service=tps)

    assert result["success"] is True
    assert len(delivery_calls) == 1  # Team ACK saga 正常执行
    assert result["team_id"] is not None
    assert runtime.dispatch_count == 2
