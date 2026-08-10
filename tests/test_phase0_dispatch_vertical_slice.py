"""RED contracts for the rejected Phase 0 dispatch lifecycle vertical slice."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.builtin_tools.dispatch_task import DispatchTaskTool
from a2a.builtin_tools.query_task_events import QueryTaskEventsTool
from a2a.builtin_tools.respond_worker import RespondWorkerTool
from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry, AgentStatus
from a2a.coordinator.event_store import event_store
from a2a.coordinator.mission_runtime import (
    MissionAdmissionError,
    MissionRuntimeManager,
    PhysicalState,
)
from a2a.coordinator.server import create_server
from a2a.coordinator.task_store import (
    TaskStore,
    _global_future_registry,
    _worker_to_dispatch_map,
)
from a2a.coordinator.supervision_state_store import SupervisionStateStore
from a2a.coordinator.task_watchdog import TaskWatchdog
from a2a.coordinator.worker_registry import WorkerRegistry
from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from sar_orch.map import SemanticMapStore


class FakeRouter:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.before_network: tuple[str, str, PhysicalState] | None = None

    async def send_task_async(
        self,
        agent_id: str,
        prompt: str,
        callback_url: str,
        task_id: str,
        *,
        context_id: str | None = None,
    ) -> str:
        dispatches = list(self.store._runtime.dispatches.values())  # noqa: SLF001
        assert len(dispatches) == 1
        dispatch = dispatches[0]
        self.before_network = (task_id, dispatch.dispatch_id, dispatch.state)
        await asyncio.sleep(0)
        return "worker-task-live"


class MultiDispatchRouter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_task_async(
        self,
        agent_id: str,
        prompt: str,
        callback_url: str,
        task_id: str,
        *,
        context_id: str | None = None,
    ) -> str:
        self.calls.append(task_id)
        return f"worker-task-{len(self.calls)}"


@pytest.mark.asyncio
async def test_production_dispatch_materializes_physical_id_before_io_and_routes_callbacks():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-live")
    store = TaskStore("request", router=None, context_id="ctx-live")
    router = FakeRouter(store)
    store._router = router  # noqa: SLF001
    store.attach_runtime(runtime)

    result = await DispatchTaskTool(store).execute(
        agent_id="Alice",
        prompt="inspect the area",
        task_id="logical-live",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert result.success is True
    dispatch = next(iter(runtime.dispatches.values()))
    assert dispatch.dispatch_id.startswith("dsp_")
    assert dispatch.state is PhysicalState.DISPATCHING
    assert router.before_network == (
        dispatch.dispatch_id,
        dispatch.dispatch_id,
        PhysicalState.DISPATCHING,
    )
    assert dispatch.worker_task_id == "worker-task-live"
    assert dispatch.dispatch_id in result.content

    submitted = manager.handle_callback(
        "ctx-live", "worker-task-live", "TASK_STATE_SUBMITTED"
    )
    working = manager.handle_callback(
        "ctx-live", "worker-task-live", "TASK_STATE_WORKING"
    )
    assert submitted.status == "ok"
    assert working.status == "ok"
    assert dispatch.state is PhysicalState.RUNNING
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_runtime_dispatch_does_not_use_module_global_future_or_worker_maps():
    _global_future_registry.clear()
    _worker_to_dispatch_map.clear()
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-local")
    store = TaskStore("request", router=None, context_id="ctx-local")
    store._router = FakeRouter(store)  # noqa: SLF001
    store.attach_runtime(runtime)

    await DispatchTaskTool(store).execute("Alice", "do it", task_id="logical-local")
    await asyncio.sleep(0)

    assert _global_future_registry == {}
    assert _worker_to_dispatch_map == {}
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_physical_identity_is_used_by_sync_watchdog_cancel_reply_and_query():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-physical")
    store = TaskStore("request", router=None, context_id="ctx-physical")
    store.attach_runtime(runtime)
    dispatch = runtime.create_dispatch("logical-physical", "Alice")
    store.add_adhoc_node("logical-physical", worker_id="Alice")
    store.register_worker_task_id(dispatch.dispatch_id, "worker-physical")
    runtime.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="test")

    changed = store.sync_task_states({"worker-physical": "TASK_STATE_WORKING"})
    assert changed == ["logical-physical"]
    assert dispatch.state is PhysicalState.RUNNING

    watchdog = TaskWatchdog(
        worker_registry=WorkerRegistry(),
        event_store=event_store,
        supervision_store=SupervisionStateStore(),
    )
    watchdog.set_task_store(store)
    watchdog.set_runtime(runtime)
    await watchdog._check_all()
    assert watchdog._supervision_store.get(dispatch.dispatch_id) is not None  # noqa: SLF001

    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )

    class CancelClient:
        async def cancel_task(self, request):
            assert dispatch.state is PhysicalState.CANCEL_PENDING
            return type("Task", (), {"status": type("Status", (), {"state": 5})()})()

        async def close(self):
            return None

    async def create_client(*args, **kwargs):
        return CancelClient()

    # The native cancellation adapter must fence first and finalize only after
    # the known remote result is returned.
    import a2a.builtin_tools.cancel_task as cancel_module

    old_create_client = cancel_module.create_client
    cancel_module.create_client = create_client
    try:
        result = await CancelTaskTool(store, registry).execute(dispatch.dispatch_id)
    finally:
        cancel_module.create_client = old_create_client
    assert result.success is True
    assert dispatch.state is PhysicalState.CANCELED

    sent_messages: list[str] = []

    class ReplyClient:
        async def close(self):
            return None

        async def send_message(self, request):
            sent_messages.append(request.message.task_id)
            yield object()

    async def create_reply_client(*args, **kwargs):
        return ReplyClient()

    import a2a.builtin_tools.respond_worker as respond_module

    old_reply_client = respond_module.create_client
    respond_module.create_client = create_reply_client
    try:
        reply = await RespondWorkerTool(store, registry).execute(
            dispatch.dispatch_id, "resume"
        )
    finally:
        respond_module.create_client = old_reply_client
    assert reply.success is True
    assert sent_messages == ["worker-physical"]

    event_store.append("worker-physical", "help_request", text="need help")
    query = await QueryTaskEventsTool(store).execute([dispatch.dispatch_id], timeout=0)
    assert json.loads(query.content)[0]["task_id"] == dispatch.dispatch_id

    provider = SARCoordinatorStateProvider(event_store=event_store)
    provider.set_task_store(store)
    snapshot = provider.snapshot()
    assert snapshot.payload["task_status_view"][0]["dispatch_id"] == dispatch.dispatch_id
    assert snapshot.payload["task_status_view"][0]["worker_task_id"] == "worker-physical"

    # Reply/cancel/query accept the opaque physical ID, never the logical node ID.
    assert store.get_dispatch(dispatch.dispatch_id) is dispatch
    assert store.get_dispatch("logical-physical") is None
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_runtime_state_is_authoritative_for_provider_without_event_store():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-provider-canonical")
    store = TaskStore("request", router=None, context_id="ctx-provider-canonical")
    store.attach_runtime(runtime)
    dispatch = runtime.create_dispatch("logical-canonical", "Alice")
    runtime.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")
    runtime.apply_physical_status(
        dispatch.dispatch_id,
        "TASK_STATE_COMPLETED",
        source="runtime",
        result="runtime-result",
    )

    provider = SARCoordinatorStateProvider(event_store=event_store)
    provider.set_task_store(store)
    provider.set_runtime(runtime)

    snapshot = provider.snapshot()

    assert snapshot.payload["task_status_view"] == [
        {
            "dispatch_id": dispatch.dispatch_id,
            "worker_task_id": "",
            "worker_id": "Alice",
            "state": "COMPLETED",
            "latest_result": "runtime-result",
            "help_request": "",
            "updated_at": 0.0,
            "acknowledged_by_coordinator": False,
        }
    ]
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_watchdog_uses_runtime_terminal_state_without_event_store():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-watchdog-canonical")
    store = TaskStore("request", router=None, context_id="ctx-watchdog-canonical")
    store.attach_runtime(runtime)
    dispatch = runtime.create_dispatch("logical-watchdog", "Alice")
    runtime.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "TASK_STATE_COMPLETED", source="runtime"
    )

    supervision_store = SupervisionStateStore()
    watchdog = TaskWatchdog(
        worker_registry=WorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    watchdog.set_task_store(store)
    watchdog.set_runtime(runtime)

    await watchdog._check_all()

    supervision = supervision_store.get(dispatch.dispatch_id)
    assert supervision is not None
    assert supervision.terminal is True
    assert supervision.supervision_state == "TERMINAL"
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_agentic_cancellation_releases_admission_via_runtime_finalizer():
    from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
    from a2a.coordinator.router import RouterAgent
    manager = MissionRuntimeManager()
    registry = AgentRegistry()
    executor = CoordinatorAgentExecutor(
        registry=registry,
        router=RouterAgent(registry=registry),
        mission_runtime_manager=manager,
    )

    async def cancel_submit(*args, **kwargs):
        raise asyncio.CancelledError

    class FakeEventQueue:
        async def enqueue_event(self, event):
            return None

    executor._controller.submit = cancel_submit  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await executor._execute_agentic(
            "request",
            "task-cancel",
            "ctx-cancel",
            FakeEventQueue(),
        )

    assert manager.active_context_id is None


@pytest.mark.asyncio
async def test_recovery_reconstructs_persisted_dispatch_and_blocks_admission_until_cancel_resolution(tmp_path):
    state_path = tmp_path / "control-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-before-restart")
    dispatch = runtime.create_dispatch("logical-restart", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-restart")
    runtime.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="test")

    cancel_calls: list[tuple[str, str]] = []

    async def fake_canceler(worker_id: str, worker_task_id: str):
        cancel_calls.append((worker_id, worker_task_id))
        return "TASK_STATE_CANCELED"

    recovered_manager = MissionRuntimeManager(
        state_path=state_path,
        cancel_adapter=fake_canceler,
    )
    recovered_manager.recover()

    assert recovered_manager.active_runtime is not None
    recovered_dispatch = recovered_manager.active_runtime.get_dispatch(dispatch.dispatch_id)
    assert recovered_dispatch is not None
    assert recovered_dispatch.state is PhysicalState.DISPATCHING
    with pytest.raises(MissionAdmissionError, match="mission_already_active"):
        recovered_manager.admit("ctx-after-restart")

    await recovered_manager.reconcile_recovery()
    assert cancel_calls == [("Alice", "worker-restart")]
    assert recovered_dispatch.state is PhysicalState.CANCELED
    assert recovered_manager.active_runtime is None
    assert recovered_manager.admit("ctx-after-restart").context_id == "ctx-after-restart"


@pytest.mark.asyncio
async def test_recovery_without_remote_adapter_retains_cancel_pending_and_blocks_admission(
    tmp_path,
):
    state_path = tmp_path / "control-state-pending.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-pending-restart")
    dispatch = runtime.create_dispatch("logical-pending", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-pending")
    runtime.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="test")

    recovered_manager = MissionRuntimeManager(state_path=state_path)
    recovered_manager.recover()
    assert await recovered_manager.reconcile_recovery() is False
    assert recovered_manager.active_runtime is not None
    recovered_dispatch = recovered_manager.active_runtime.get_dispatch(
        dispatch.dispatch_id
    )
    assert recovered_dispatch.state is PhysicalState.CANCEL_PENDING
    persisted = json.loads(state_path.read_text())
    assert persisted["dispatches"][0]["state"] == "CANCEL_PENDING"
    with pytest.raises(MissionAdmissionError, match="mission_already_active"):
        recovered_manager.admit("ctx-blocked")


@pytest.mark.asyncio
async def test_push_callback_route_uses_dispatch_created_by_production_tool(tmp_path):
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    runtime = server.mission_runtime_manager.admit("ctx-route-production")
    store = TaskStore("request", router=None, context_id="ctx-route-production")
    router = FakeRouter(store)
    store._router = router  # noqa: SLF001
    store.attach_runtime(runtime)
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    result = await DispatchTaskTool(store).execute("Alice", "inspect")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatch = next(iter(runtime.dispatches.values()))
    assert dispatch.dispatch_id in result.content

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json={
                "statusUpdate": {
                    "taskId": "worker-task-live",
                    "contextId": "ctx-route-production",
                    "status": {"state": "TASK_STATE_SUBMITTED"},
                }
            },
        )

    assert response.json()["status"] == "ok"
    assert dispatch.state is PhysicalState.ACCEPTED
    await runtime.abort("test_cleanup")


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


def _legacy_report_text(observation: dict) -> str:
    data = {
        "ev": "tool_result",
        "tool_name": "report_observation",
        "success": True,
        "content": json.dumps(observation),
    }
    return f"[Result] report_observation: observed\n[DATA]\n{json.dumps(data)}"


def _structured_obs_text(observations: list[dict], tool_name: str = "navigate_to") -> str:
    data = {
        "ev": "tool_result",
        "tool_name": tool_name,
        "success": True,
        "content": "Arrived.",
        "structured_data": {"observations": observations},
    }
    return f"[Result] {tool_name}: Arrived.\n[DATA]\n{json.dumps(data)}"


@pytest.mark.asyncio
async def test_active_push_callback_ingests_worker_observation(tmp_path):
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    runtime = server.mission_runtime_manager.admit("ctx-observation-active")
    store = TaskStore("request", router=None, context_id="ctx-observation-active")
    store._router = FakeRouter(store)
    store.attach_runtime(runtime)
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    result = await DispatchTaskTool(store).execute("Alice", "inspect")
    assert result.success
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatch = next(iter(runtime.dispatches.values()))
    dispatch_id = dispatch.dispatch_id
    dispatch_worker_task_id = dispatch.worker_task_id
    assert dispatch_worker_task_id == "worker-task-live"

    observation = {
        "reporter": "Alice",
        "step": 4,
        "object_type": "fire",
        "name": "FireA",
        "position": [1, 1, 0],
        "attributes": {"status": "active"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=dispatch_worker_task_id,
                context_id="ctx-observation-active",
                text=_legacy_report_text(observation),
            ),
        )

    assert response.json()["status"] == "ok"
    assert event_store.get_recent_observations()[0]["name"] == "FireA"
    assert server._semantic_map.fires["FireA"].position == (1, 1, 0)
    assert runtime.get_dispatch(dispatch_id).state is PhysicalState.RUNNING
    # EventStore key matches status_update dispatch_id semantics
    summary = event_store.get_summary(task_ids={dispatch_id})
    assert "OBSERVATION" in summary
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_repeated_working_callbacks_ingest_distinct_observations(tmp_path):
    """stale_transition WORKING callbacks must still ingest new observations."""
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    runtime = server.mission_runtime_manager.admit("ctx-obs-repeat")
    store = TaskStore("request", router=None, context_id="ctx-obs-repeat")
    store._router = FakeRouter(store)
    store.attach_runtime(runtime)
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    await DispatchTaskTool(store).execute("Alice", "inspect")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatch = next(iter(runtime.dispatches.values()))
    worker_task_id = dispatch.worker_task_id
    dispatch_id = dispatch.dispatch_id

    obs1 = {
        "reporter": "Alice",
        "step": 1,
        "object_type": "fire",
        "name": "Fire1",
        "position": [1, 0, 0],
        "attributes": {"intensity": "High"},
    }
    obs2 = {
        "reporter": "Alice",
        "step": 2,
        "object_type": "person",
        "name": "Person1",
        "position": [2, 0, 0],
        "attributes": {"status": "trapped"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=worker_task_id,
                context_id="ctx-obs-repeat",
                text=_legacy_report_text(obs1),
            ),
        )
        r2 = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=worker_task_id,
                context_id="ctx-obs-repeat",
                text=_legacy_report_text(obs2),
            ),
        )

    assert r1.json()["status"] == "ok"
    assert r2.json()["status"] == "ok"
    recent = event_store.get_recent_observations()
    names = {o["name"] for o in recent}
    assert names >= {"Fire1", "Person1"}
    assert "Fire1" in server._semantic_map.fires
    assert "Person1" in server._semantic_map.persons
    assert dispatch.state is PhysicalState.RUNNING
    summary = event_store.get_summary(task_ids={dispatch_id})
    assert "OBSERVATION" in summary
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_active_push_callback_ingests_structured_data_observations(tmp_path):
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    runtime = server.mission_runtime_manager.admit("ctx-obs-structured")
    store = TaskStore("request", router=None, context_id="ctx-obs-structured")
    store._router = FakeRouter(store)
    store.attach_runtime(runtime)
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    await DispatchTaskTool(store).execute("Alice", "inspect")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatch = next(iter(runtime.dispatches.values()))
    dispatch_id = dispatch.dispatch_id

    obs = {
        "reporter": "Alice",
        "step": 7,
        "object_type": "fire",
        "name": "StructFire",
        "position": [3, 3, 0],
        "attributes": {"intensity": "Medium"},
        "confidence": 1.0,
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=dispatch.worker_task_id,
                context_id="ctx-obs-structured",
                text=_structured_obs_text([obs]),
            ),
        )

    assert response.json()["status"] == "ok"
    assert event_store.get_recent_observations()[0]["name"] == "StructFire"
    assert server._semantic_map.fires["StructFire"].position == (3, 3, 0)
    assert "OBSERVATION" in event_store.get_summary(task_ids={dispatch_id})
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_observation_ingest_is_idempotent_for_same_step_key(tmp_path):
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    runtime = server.mission_runtime_manager.admit("ctx-obs-idempotent")
    store = TaskStore("request", router=None, context_id="ctx-obs-idempotent")
    store._router = FakeRouter(store)
    store.attach_runtime(runtime)
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    await DispatchTaskTool(store).execute("Alice", "inspect")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatch = next(iter(runtime.dispatches.values()))

    obs = {
        "reporter": "Alice",
        "step": 9,
        "object_type": "fire",
        "name": "DupFire",
        "position": [0, 0, 0],
        "attributes": {"intensity": "High"},
    }
    text = _legacy_report_text(obs)

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=dispatch.worker_task_id,
                context_id="ctx-obs-idempotent",
                text=text,
            ),
        )
        await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id=dispatch.worker_task_id,
                context_id="ctx-obs-idempotent",
                text=text,
            ),
        )

    dups = [
        o
        for o in event_store.get_recent_observations()
        if o.get("name") == "DupFire"
    ]
    assert len(dups) == 1
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_legacy_no_runtime_push_callback_ingests_observation(tmp_path):
    """Pre-admission path (no active_runtime) must still ingest observations."""
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())
    assert server.mission_runtime_manager.active_runtime is None

    observation = {
        "reporter": "Bob",
        "step": 3,
        "object_type": "fire",
        "name": "LegacyFire",
        "position": [5, 5, 0],
        "attributes": {"status": "active"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id="worker-legacy-1",
                context_id=None,
                text=_legacy_report_text(observation),
            ),
        )

    assert response.json()["status"] == "ok"
    recent = event_store.get_recent_observations()
    assert any(o.get("name") == "LegacyFire" for o in recent)
    assert "LegacyFire" in server._semantic_map.fires
    # Legacy path keys EventStore by worker task id (dispatch_id falls back)
    summary = event_store.get_summary(task_ids={"worker-legacy-1"})
    assert "OBSERVATION" in summary


@pytest.mark.asyncio
async def test_legacy_callback_uses_canonical_dispatch_key_for_status_and_observation(
    tmp_path,
):
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())

    store = TaskStore("legacy request", router=None)
    store.add_adhoc_node("dispatch-legacy", worker_id="Bob")
    store.register_worker_task_id("dispatch-legacy", "worker-legacy-mapped")
    server._task_watchdog.set_task_store(store)  # noqa: SLF001

    observation = {
        "reporter": "Bob",
        "step": 3,
        "object_type": "fire",
        "name": "MappedLegacyFire",
        "position": [5, 5, 0],
        "attributes": {"status": "active"},
    }

    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/a2a/push-callback",
            json=_obs_status_payload(
                task_id="worker-legacy-mapped",
                context_id=None,
                text=_legacy_report_text(observation),
            ),
        )

    assert response.json()["status"] == "ok"
    summary = event_store.get_summary(task_ids={"dispatch-legacy"})
    assert "STATUS" in summary
    assert "OBSERVATION" in summary
    assert "OBSERVATION" not in event_store.get_summary(
        task_ids={"worker-legacy-mapped"}
    )


@pytest.mark.asyncio
async def test_observation_deduplication_is_scoped_to_mission_context(tmp_path):
    event_store.clear()
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.set_semantic_map(SemanticMapStore())

    async def _ingest_once(context_id: str) -> None:
        runtime = server.mission_runtime_manager.admit(context_id)
        store = TaskStore("request", router=None, context_id=context_id)
        store._router = FakeRouter(store)
        store.attach_runtime(runtime)
        server._task_watchdog.set_task_store(store)  # noqa: SLF001

        await DispatchTaskTool(store).execute("Alice", "inspect")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        dispatch = next(iter(runtime.dispatches.values()))
        observation = {
            "reporter": "Alice",
            "step": 1,
            "object_type": "fire",
            "name": "SameFireInNewMission",
            "position": [1, 1, 0],
            "attributes": {"status": "active"},
        }

        transport = httpx.ASGITransport(app=server._app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/a2a/push-callback",
                json=_obs_status_payload(
                    task_id=dispatch.worker_task_id,
                    context_id=context_id,
                    text=_legacy_report_text(observation),
                ),
            )
        assert response.json()["status"] == "ok"
        await runtime.abort("test_cleanup")

    await _ingest_once("ctx-dedup-one")
    await _ingest_once("ctx-dedup-two")

    observations = [
        item
        for item in event_store.get_recent_observations()
        if item.get("name") == "SameFireInNewMission"
    ]
    assert len(observations) == 2


@pytest.mark.asyncio
async def test_shutdown_finalizer_is_handed_to_owning_server_loop(tmp_path):
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    owner_loop = asyncio.get_running_loop()
    server._owner_loop = owner_loop  # noqa: SLF001
    called_loops: list[asyncio.AbstractEventLoop] = []

    async def finalizer(reason: str = "coordinator_shutdown"):
        called_loops.append(asyncio.get_running_loop())

    server._shutdown_on_owner_loop = finalizer  # noqa: SLF001
    await asyncio.to_thread(asyncio.run, server.shutdown())
    assert called_loops == [owner_loop]


@pytest.mark.asyncio
async def test_terminal_physical_dispatch_releases_busy_and_allows_next_dispatch_same_worker():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-reuse-worker")
    router = MultiDispatchRouter()
    store = TaskStore("request", router=router, context_id="ctx-reuse-worker")
    store.attach_runtime(runtime)
    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )
    facade = SendMessageTool(store, registry)

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    await asyncio.sleep(0)
    dispatch = runtime.get_dispatch(first.data["dispatch_id"])
    assert dispatch is not None
    store.apply_physical_status(dispatch.dispatch_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(dispatch.dispatch_id, "TASK_STATE_WORKING", source="test")
    store.apply_physical_status(dispatch.dispatch_id, "TASK_STATE_CANCELED", source="test")

    assert dispatch.state is PhysicalState.CANCELED

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="second"
    )

    assert second.success is True
    await asyncio.sleep(0)
    assert second.data["dispatch_id"] != dispatch.dispatch_id
    assert router.calls == [dispatch.dispatch_id, second.data["dispatch_id"]]
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_runtime_assign_defers_instead_of_auto_cancel_replace():
    """Runtime active assign defers LWW; no cancel thrash; no worker_busy."""
    cancel_calls: list[tuple[str, str]] = []

    async def fake_cancel(worker_id: str, worker_task_id: str):
        cancel_calls.append((worker_id, worker_task_id))
        return "TASK_STATE_CANCELED"

    manager = MissionRuntimeManager(cancel_adapter=fake_cancel)
    runtime = manager.admit("ctx-reassign-success")
    router = MultiDispatchRouter()
    store = TaskStore("request", router=router, context_id="ctx-reassign-success")
    store.attach_runtime(runtime)
    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )
    facade = SendMessageTool(store, registry)

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first mission"
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert first.success is True
    old_id = first.data["dispatch_id"]
    old = runtime.get_dispatch(old_id)
    assert old is not None
    assert old.worker_task_id is not None
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")
    assert old.state is PhysicalState.RUNNING

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="replacement mission"
    )
    await asyncio.sleep(0)

    assert second.success is True
    assert second.error is None
    assert second.error != "worker_busy"
    assert second.data is not None
    assert second.data.get("queued") is True
    assert second.data.get("deferred") is True
    assert second.data.get("worker_id") == "Alice"
    assert cancel_calls == []
    assert old.state is PhysicalState.RUNNING
    assert len(runtime.dispatches) == 1
    assert store.get_active_tasks_by_worker("Alice") == [old_id]

    store.apply_physical_status(old_id, "TASK_STATE_COMPLETED", source="test")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(runtime.dispatches) == 2
    new_ids = [d for d in runtime.dispatches if d != old_id]
    assert len(new_ids) == 1
    assert store.get_active_tasks_by_worker("Alice") == new_ids
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_runtime_assign_coalesces_deferred_without_cancel_pending():
    """Second assign while active coalesces deferred; never replacement_cancel_pending."""
    manager = MissionRuntimeManager()  # no cancel_adapter
    runtime = manager.admit("ctx-reassign-pending")
    router = MultiDispatchRouter()
    store = TaskStore("request", router=router, context_id="ctx-reassign-pending")
    store.attach_runtime(runtime)
    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )
    facade = SendMessageTool(store, registry)

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first mission"
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    old_id = first.data["dispatch_id"]
    old = runtime.get_dispatch(old_id)
    assert old is not None
    assert old.worker_task_id is not None
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="replacement mission"
    )

    assert second.success is True
    assert second.error is None
    assert second.error != "worker_busy"
    assert second.error != "replacement_cancel_pending"
    assert second.data is not None
    assert second.data.get("queued") is True
    assert old.state is PhysicalState.RUNNING
    assert len(runtime.dispatches) == 1
    assert store.get_active_tasks_by_worker("Alice") == [old_id]
    assert len(router.calls) == 1
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_legacy_store_without_runtime_still_rejects_busy_worker():
    """C: legacy TaskStore (no runtime) retains original worker_busy guard."""
    store = TaskStore("request", router=None)
    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )
    store.add_adhoc_node("alice-task-1", worker_id="Alice", description="Active")
    store.set_state("alice-task-1", "running")
    facade = SendMessageTool(store, registry)

    result = await facade.execute(
        message_type="assign_task", who="Alice", content="new task"
    )

    assert result.success is False
    assert result.error == "worker_busy"
    assert "alice-task-1" in result.content


@pytest.mark.asyncio
async def test_callback_and_sync_terminal_status_project_legacy_plan_nodes_deterministically():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-projection")
    store = TaskStore("request", router=None, context_id="ctx-projection")
    store.attach_runtime(runtime)

    callback_node = store.add_adhoc_node("logical-callback", worker_id="Alice")
    callback_dispatch = runtime.create_dispatch("logical-callback", "Alice")
    store.register_worker_task_id(callback_dispatch.dispatch_id, "worker-callback")
    store.apply_physical_status(callback_dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    store.apply_physical_status(callback_dispatch.dispatch_id, "TASK_STATE_SUBMITTED", source="dispatch")
    store.apply_physical_status(callback_dispatch.dispatch_id, "TASK_STATE_WORKING", source="dispatch")
    callback_result = manager.handle_callback(
        "ctx-projection", "worker-callback", "TASK_STATE_CANCELED"
    )

    sync_node = store.add_adhoc_node("logical-sync", worker_id="Bob")
    sync_dispatch = runtime.create_dispatch("logical-sync", "Bob")
    store.register_worker_task_id(sync_dispatch.dispatch_id, "worker-sync")
    store.apply_physical_status(sync_dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    store.apply_physical_status(sync_dispatch.dispatch_id, "TASK_STATE_SUBMITTED", source="dispatch")

    changed = store.sync_task_states({"worker-sync": "TASK_STATE_COMPLETED"})

    assert callback_result.status == "ok"
    assert callback_node.state == "canceled"
    assert sync_node.state == "done"
    assert changed == ["logical-sync"]
    await runtime.abort("test_cleanup")
