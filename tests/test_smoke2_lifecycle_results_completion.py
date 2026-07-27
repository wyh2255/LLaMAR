"""Strict Phase 0 smoke-2 contracts: MCP ownership, result projection, and SAR completion truth."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from Agent.router_agent.schema import RunResult
from Agent.worker_agent.tools import mcp_loader
from a2a.builtin_tools.query_task_events import QueryTaskEventsTool
from a2a.builtin_tools.query_task_results import QueryTaskResultsTool
from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
from a2a.coordinator.agent_registry import AgentRegistry
from a2a.coordinator.event_store import event_store
from a2a.coordinator.server import create_server
from a2a.coordinator.task_queue import TaskQueue
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.mission_runtime import MissionRuntimeManager
from a2a.shared.types import DistributedTask
from sar_orch.tools.coordinator.finish_task import FinishTaskTool
from sar_orch.worker import SARWorker


class _FakeMCPConnection:
    instances: list["_FakeMCPConnection"] = []

    def __init__(self, name: str, **kwargs) -> None:
        self.name = name
        self.tools = [f"tool-{name}"]
        self.disconnect_loops: list[asyncio.AbstractEventLoop] = []
        self.__class__.instances.append(self)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_loops.append(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_worker_mcp_registries_are_isolated_and_cleanup_stays_on_owner_loop(
    tmp_path, monkeypatch
):
    """Each caller registry owns only its connections; legacy global state is untouched."""
    monkeypatch.setattr(mcp_loader, "MCPServerConnection", _FakeMCPConnection)
    _FakeMCPConnection.instances.clear()
    mcp_loader._mcp_connections.clear()  # noqa: SLF001
    global_connection = _FakeMCPConnection("global")
    mcp_loader._mcp_connections.append(global_connection)  # noqa: SLF001

    config_a = tmp_path / "a.json"
    config_b = tmp_path / "b.json"
    config_a.write_text('{"mcpServers": {"worker-a": {"command": "fake"}}}')
    config_b.write_text('{"mcpServers": {"worker-b": {"command": "fake"}}}')
    registry_a: list = []
    registry_b: list = []
    owner_loop = asyncio.get_running_loop()

    try:
        await mcp_loader.load_mcp_tools_async(
            str(config_a), connection_registry=registry_a
        )
        await mcp_loader.load_mcp_tools_async(
            str(config_b), connection_registry=registry_b
        )
        assert [connection.name for connection in registry_a] == ["worker-a"]
        assert [connection.name for connection in registry_b] == ["worker-b"]
        assert mcp_loader._mcp_connections == [global_connection]  # noqa: SLF001

        await mcp_loader.cleanup_mcp_connections(registry_a)

        assert registry_a == []
        assert [connection.name for connection in registry_b] == ["worker-b"]
        assert global_connection.disconnect_loops == []
        assert _FakeMCPConnection.instances[1].disconnect_loops == [owner_loop]
    finally:
        mcp_loader._mcp_connections.clear()  # noqa: SLF001


@pytest.mark.asyncio
async def test_worker_shutdown_stops_a2a_before_awaiting_local_mcp_cleanup(monkeypatch):
    """The run coroutine awaits server shutdown before same-loop MCP cleanup."""
    worker = SARWorker(
        worker_id="Alice",
        agent_name="Alice",
        agent_idx=0,
        barrier=None,
    )
    worker._mcp_registry.append(object())  # noqa: SLF001
    owner_loop = asyncio.get_running_loop()
    events: list[str] = []

    class FakeClient:
        async def disconnect(self):
            assert asyncio.get_running_loop() is owner_loop
            events.append("coordinator_disconnect")

    worker._client = FakeClient()  # noqa: SLF001
    worker._server = object()  # noqa: SLF001
    worker._server_task = object()  # noqa: SLF001

    async def fake_shutdown(server, server_task):
        assert events == ["coordinator_disconnect"]
        assert server is worker._server  # noqa: SLF001
        assert server_task is worker._server_task  # noqa: SLF001
        events.append("a2a_active_tasks_stopped")

    async def fake_cleanup(registry):
        assert asyncio.get_running_loop() is owner_loop
        assert events == ["coordinator_disconnect", "a2a_active_tasks_stopped"]
        assert registry is worker._mcp_registry  # noqa: SLF001
        events.append("mcp_cleanup")

    monkeypatch.setattr("sar_orch.worker.shutdown_uvicorn_server", fake_shutdown)
    monkeypatch.setattr("sar_orch.worker.cleanup_mcp_connections", fake_cleanup)

    await worker._shutdown_run_resources()  # noqa: SLF001

    assert events == [
        "coordinator_disconnect",
        "a2a_active_tasks_stopped",
        "mcp_cleanup",
    ]


@pytest.mark.asyncio
async def test_terminal_artifact_projects_to_physical_and_logical_results_and_events():
    """Artifact evidence survives terminal status without a separate result field."""
    event_store.clear()
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-artifact")
    store = TaskStore("request", router=None, context_id="ctx-artifact")
    store.attach_runtime(runtime)
    store.add_adhoc_node("explore", worker_id="Alice")
    dispatch = store.create_physical_dispatch("explore", "Alice")
    store.register_worker_task_id(dispatch.dispatch_id, "worker-task-artifact")
    store.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    manager.handle_artifact(
        "ctx-artifact",
        "worker-task-artifact",
        "FIRE CaldorFire; PERSON Timmy",
    )
    manager.handle_callback(
        "ctx-artifact", "worker-task-artifact", "TASK_STATE_SUBMITTED"
    )
    manager.handle_callback(
        "ctx-artifact", "worker-task-artifact", "TASK_STATE_WORKING"
    )
    manager.handle_callback(
        "ctx-artifact", "worker-task-artifact", "TASK_STATE_COMPLETED"
    )

    assert dispatch.result is None
    assert dispatch.artifact == "FIRE CaldorFire; PERSON Timmy"
    assert store.get_node("explore").result == "FIRE CaldorFire; PERSON Timmy"

    results = await QueryTaskResultsTool(store.results).execute(
        [dispatch.dispatch_id, "explore"]
    )
    assert "FIRE CaldorFire" in results.content
    assert "PERSON Timmy" in results.content
    assert dispatch.dispatch_id in results.content
    assert "explore" in results.content

    events = await QueryTaskEventsTool(store).execute([dispatch.dispatch_id], timeout=0)
    event_payload = json.loads(events.content)[0]
    assert event_payload["state"] == "COMPLETED"
    assert "FIRE CaldorFire" in event_payload["text"]
    assert "PERSON Timmy" in event_payload["text"]
    await runtime.abort("test_cleanup")


class _MissionStore:
    def __init__(self) -> None:
        self.mission_success = None

    def mark_finished(self, success: bool) -> None:
        self.mission_success = success


@pytest.mark.asyncio
async def test_sar_finish_rejects_success_until_completion_validator_is_true():
    store = _MissionStore()
    tool = FinishTaskTool(store, completion_validator=lambda: False)

    result = await tool.execute(True, "all done")

    assert result.success is False
    assert result.task_complete is False
    assert store.mission_success is None


@pytest.mark.asyncio
async def test_sar_finish_true_validator_and_generic_no_validator_preserve_success():
    validated_store = _MissionStore()
    validated = await FinishTaskTool(
        validated_store, completion_validator=lambda: True
    ).execute(True, "all done")
    generic_store = _MissionStore()
    generic = await FinishTaskTool(generic_store).execute(True, "all done")

    assert validated.success is True
    assert validated.task_complete is True
    assert validated_store.mission_success is True
    assert generic.success is True
    assert generic.task_complete is True
    assert generic_store.mission_success is True


@pytest.mark.asyncio
async def test_sar_finish_failure_signal_keeps_existing_failure_semantics():
    store = _MissionStore()

    result = await FinishTaskTool(store, completion_validator=lambda: False).execute(
        False, "mission failed"
    )

    assert result.success is True
    assert result.task_complete is True
    assert result.mission_success is False
    assert store.mission_success is False


def test_create_coordinator_server_exposes_dynamic_sar_completion_validator():
    server = create_server(verifier_enabled=False)
    validator = server._completion_validator  # noqa: SLF001
    server.set_barrier(SimpleNamespace(is_finished=lambda: False))
    assert validator() is False
    server.set_barrier(SimpleNamespace(is_finished=lambda: True))
    assert validator() is True
    server.set_barrier(None)
    assert validator() is True


@pytest.mark.asyncio
async def test_executor_passes_completion_validator_to_runtime_sar_finish_tool():
    registry = AgentRegistry()
    queue = TaskQueue()
    queue.enqueue(DistributedTask("task-validator", "agent", "request"))
    queue.start("task-validator")

    def validator() -> bool:
        return False

    executor = CoordinatorAgentExecutor(
        registry=registry,
        task_queue=queue,
        completion_validator=validator,
    )
    captured_tools = []

    async def fake_submit(*args, **kwargs):
        captured_tools.extend(kwargs["extra_tools"])
        return RunResult(content="finished", success=True)

    executor._controller.submit = fake_submit  # noqa: SLF001

    class EventQueue:
        async def enqueue_event(self, event):
            return None

    await executor._execute_agentic(
        "request", "task-validator", "ctx-validator", EventQueue()
    )

    finish_tool = next(tool for tool in captured_tools if tool.name == "finish_task")
    assert finish_tool._completion_validator is validator  # noqa: SLF001


def test_create_coordinator_a2a_server_accepts_optional_completion_validator(
    monkeypatch,
):
    import a2a.coordinator.a2a_server as module

    captured: dict = {}

    class FakeExecutor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def clear_sessions(self):
            return None

    monkeypatch.setattr(module, "CoordinatorAgentExecutor", FakeExecutor)

    def validator() -> bool:
        return True

    module.create_coordinator_a2a_server(completion_validator=validator)

    assert captured["completion_validator"] is validator
