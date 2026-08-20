"""Tests for SendMessageTool — Coordinator communication facade."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.agent_registry import (
    AgentInfo,
    AgentNotFoundError,
    AgentRegistry,
    AgentStatus,
)
from a2a.coordinator.task_store import TaskStore


@pytest.fixture
def mock_store():
    store = MagicMock()
    store._dispatch_to_worker = {}
    store._worker_to_dispatch = {}
    store.dispatched_count = 0
    store.max_tasks = 10
    return store


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get = MagicMock()
    return registry


@pytest.fixture
def tool(mock_store, mock_registry):
    return SendMessageTool(
        store=mock_store,
        registry=mock_registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )


class TestSendMessageToolProperties:
    def test_name(self, tool):
        assert tool.name == "send_message"

    def test_parameters_schema(self, tool):
        schema = tool.parameters
        assert schema["properties"]["message_type"]["enum"] == [
            "assign_task",
            "reply_to_help",
            "cancel_task",
            "activate_plan_node",
        ]
        assert "message_type" in schema["required"]


@pytest.mark.asyncio
async def test_invalid_message_type(tool):
    result = await tool.execute(message_type="notify")
    assert result.success is False
    assert result.error == "invalid_message_type"


@pytest.mark.asyncio
async def test_assign_task_missing_who(tool):
    result = await tool.execute(message_type="assign_task", content="do something")
    assert result.success is False
    assert result.error == "missing_who"


@pytest.mark.asyncio
async def test_assign_task_missing_content(tool):
    result = await tool.execute(message_type="assign_task", who="Alice")
    assert result.success is False
    assert result.error == "missing_content"


@pytest.mark.asyncio
async def test_assign_task_worker_not_found(tool, mock_registry):
    mock_registry.get.side_effect = AgentNotFoundError("Alice")
    result = await tool.execute(
        message_type="assign_task",
        who="Alice",
        content="do something",
    )
    assert result.success is False
    assert result.error == "worker_not_found"


@pytest.mark.asyncio
async def test_assign_task_routes_to_dispatch(tool, mock_registry):
    mock_registry.get.return_value = AgentInfo(
        agent_id="Alice",
        description="Test worker",
        endpoint="http://alice:8090",
        status=AgentStatus.ONLINE,
    )
    with patch.object(
        tool._dispatch_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="dispatched")),
    ) as mock_execute:
        result = await tool.execute(
            message_type="assign_task",
            who="Alice",
            content="Navigate to reservoir",
            related_task_id="alice-task",
        )
    assert result.success is True
    assert result.content == "dispatched"
    mock_execute.assert_awaited_once_with(
        agent_id="Alice",
        prompt="Navigate to reservoir",
        task_id="alice-task",
    )


@pytest.mark.asyncio
async def test_reply_to_help_missing_related_task_id(tool):
    result = await tool.execute(message_type="reply_to_help", content="help")
    assert result.success is False
    assert result.error == "missing_related_task_id"


@pytest.mark.asyncio
async def test_reply_to_help_unknown_task(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = None
    result = await tool.execute(
        message_type="reply_to_help",
        related_task_id="missing",
        content="help",
    )
    assert result.success is False
    assert result.error == "unknown_task_id"


@pytest.mark.asyncio
async def test_reply_to_help_not_yet_routed(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = "alice-task"
    mock_store._dispatch_to_worker["alice-task"] = ""
    result = await tool.execute(
        message_type="reply_to_help",
        related_task_id="alice-task",
        content="help",
    )
    assert result.success is False
    assert result.error == "task_not_routable_yet"


@pytest.mark.asyncio
async def test_reply_to_help_routes_to_respond(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = "alice-task"
    mock_store._dispatch_to_worker["alice-task"] = "worker-uuid"
    with patch.object(
        tool._respond_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="replied")),
    ) as mock_execute:
        result = await tool.execute(
            message_type="reply_to_help",
            related_task_id="alice-task",
            content="Go north",
        )
    assert result.success is True
    assert result.content == "replied"
    mock_execute.assert_awaited_once_with(task_id="alice-task", response="Go north")


@pytest.mark.asyncio
async def test_cancel_task_missing_related_task_id(tool):
    result = await tool.execute(message_type="cancel_task")
    assert result.success is False
    assert result.error == "missing_related_task_id"


@pytest.mark.asyncio
async def test_cancel_task_unknown_task(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = None
    result = await tool.execute(
        message_type="cancel_task",
        related_task_id="missing",
    )
    assert result.success is False
    assert result.error == "unknown_task_id"


@pytest.mark.asyncio
async def test_cancel_task_not_yet_routed(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = "alice-task"
    mock_store._dispatch_to_worker["alice-task"] = ""
    result = await tool.execute(
        message_type="cancel_task",
        related_task_id="alice-task",
    )
    assert result.success is False
    assert result.error == "task_not_routable_yet"


@pytest.mark.asyncio
async def test_cancel_task_routes_to_cancel(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = "alice-task"
    mock_store._dispatch_to_worker["alice-task"] = "worker-uuid"
    with patch.object(
        tool._cancel_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="cancelled")),
    ) as mock_execute:
        result = await tool.execute(
            message_type="cancel_task",
            related_task_id="alice-task",
        )
    assert result.success is True
    assert result.content == "cancelled"
    mock_execute.assert_awaited_once_with(task_id="alice-task")


class TestWorkerBusyProtection:
    """Test that SendMessageTool prevents duplicate dispatch to busy workers."""

    @pytest.mark.asyncio
    async def test_assign_task_to_busy_worker_returns_error(self):
        """Dispatching to a worker with active tasks should fail."""
        store = TaskStore("test request", router=None)
        registry = AgentRegistry()
        registry.register(
            AgentInfo(
                agent_id="Alice",
                description="Test worker",
                endpoint="http://localhost:8001",
                capabilities=["sar"],
            )
        )

        # Simulate an active task for Alice
        store.add_adhoc_node(
            "alice-task-1", worker_id="Alice", description="Active task"
        )
        store.set_state("alice-task-1", "running")

        tool = SendMessageTool(store, registry)
        result = await tool.execute(
            message_type="assign_task",
            who="Alice",
            content="New task",
        )

        assert not result.success
        assert result.error == "worker_busy"
        assert "alice-task-1" in result.content

    @pytest.mark.asyncio
    async def test_assign_task_to_idle_worker_succeeds(self):
        """Dispatching to an idle worker should succeed."""
        store = TaskStore("test request", router=None)
        registry = AgentRegistry()
        registry.register(
            AgentInfo(
                agent_id="Bob",
                description="Test worker",
                endpoint="http://localhost:8002",
                capabilities=["sar"],
            )
        )

        tool = SendMessageTool(store, registry)
        # Note: This will fail at dispatch level (no real worker), but should pass busy check
        result = await tool.execute(
            message_type="assign_task",
            who="Bob",
            content="New task",
        )

        # Should not fail with worker_busy
        assert result.error != "worker_busy"

    @pytest.mark.asyncio
    async def test_update_plan_marks_removed_as_canceled(self):
        """Removed plan nodes should be marked as canceled."""
        store = TaskStore("test request", router=None)

        # Add initial plan
        store.update_plan(
            [
                {"task_id": "task-1", "worker_id": "Alice", "description": "Task 1"},
                {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
            ]
        )
        store.set_state("task-1", "running")

        # Update plan, removing task-1
        result = store.update_plan(
            [
                {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
            ]
        )

        assert "task-1" in result["removed"]
        node = store.get_node("task-1")
        assert node.state == "canceled"


# ---------------------------------------------------------------------------
# Phase 2: secure mode refuses dispatch to workers without signed push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_task_async_refuses_worker_without_push_capability():
    """In secure mode a worker whose AgentCard lacks push capability must fail
    with the typed ``memory_auth_not_configured`` error before any dispatch."""
    from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry, AgentStatus
    from a2a.coordinator.memory.callback_auth import MemoryAuthNotConfiguredError
    from a2a.coordinator.router import RouterAgent

    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker without signer",
            endpoint="http://localhost:8991",
            capabilities=["sar"],
            status=AgentStatus.ONLINE,
            push_notifications=False,
        )
    )
    router = RouterAgent(
        registry=registry,
        coordinator_secret=b"coordinator-secret-0123456789abcdef",
    )
    with pytest.raises(MemoryAuthNotConfiguredError) as exc:
        await router.send_task_async(
            "Alice",
            "do the thing",
            "http://coordinator/a2a/push-callback",
            "dsp_1",
            context_id="ctx-1",
        )
    assert exc.value.code == "memory_auth_not_configured"


@pytest.mark.asyncio
async def test_send_task_async_allows_worker_with_push_capability():
    """A worker advertising push capability passes the gate (dispatch proceeds
    to the SDK client path, which raises for a missing endpoint, not the auth
    gate)."""
    from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry, AgentStatus
    from a2a.coordinator.router import RouterAgent

    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Bob",
            description="worker with signer",
            endpoint="http://localhost:8992",
            capabilities=["sar"],
            status=AgentStatus.ONLINE,
            push_notifications=True,
        )
    )
    router = RouterAgent(
        registry=registry,
        coordinator_secret=b"coordinator-secret-0123456789abcdef",
    )
    with pytest.raises(Exception) as exc:
        await router.send_task_async(
            "Bob",
            "do the thing",
            "http://coordinator/a2a/push-callback",
            "dsp_2",
            context_id="ctx-1",
        )
    assert exc.value.__class__.__name__ != "MemoryAuthNotConfiguredError"


# ---------------------------------------------------------------------------
# Registration-race tolerance: cancel / reply within the dispatch-binding window
# ---------------------------------------------------------------------------


def _real_store_with_runtime():
    """A TaskStore attached to a real MissionRuntime (physical authority)."""
    from a2a.coordinator.mission_runtime import MissionRuntimeManager
    from a2a.coordinator.task_store import TaskStore

    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-race")
    store = TaskStore("request", router=None)
    store.attach_runtime(runtime)
    return store


@pytest.mark.asyncio
async def test_cancel_task_prepared_dispatch_is_idempotent_local_cleanup():
    """Canceling a dispatch that was allocated but NEVER sent (PREPARED) is an
    idempotent LOCAL cleanup: it must succeed and free the worker, never
    surface task_not_routable_yet (which made the LLM loop on zombie
    dispatches like dsp_a076dd95 in scene_2_agents_4)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _real_store_with_runtime()
    dispatch = store.create_physical_dispatch("david-explore", "David")
    assert dispatch is not None
    assert str(dispatch.state.value) == "PREPARED"

    registry = AgentRegistry()
    tool = SendMessageTool(
        store=store,
        registry=registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    result = await tool.execute(
        message_type="cancel_task", related_task_id=dispatch.dispatch_id
    )
    assert result.success is True
    assert result.content == (
        f"Task '{dispatch.dispatch_id}' was never dispatched to a worker; "
        "canceled locally (no remote request sent)."
    )
    # Physical dispatch reached a terminal CANCELED state.
    assert str(store.get_dispatch(dispatch.dispatch_id).state.value) == "CANCELED"
    # No worker request could have been sent (no worker_task_id existed).


@pytest.mark.asyncio
async def test_cancel_task_prepared_dispatch_after_send_in_flight_is_transient():
    """A dispatch whose send is genuinely in flight (DISPATCHING) but whose
    worker_task_id binding has not landed must surface task_not_routable_yet
    (transient), not an idempotent cancel — the worker may already hold it."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _real_store_with_runtime()
    dispatch = store.create_physical_dispatch("bob-fire", "Bob")
    store.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    registry = AgentRegistry()
    tool = SendMessageTool(
        store=store,
        registry=registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    result = await tool.execute(
        message_type="cancel_task", related_task_id=dispatch.dispatch_id
    )
    assert result.success is False
    assert result.error == "task_not_routable_yet"
    # Still non-terminal: the send is in flight.
    assert str(store.get_dispatch(dispatch.dispatch_id).state.value) == "DISPATCHING"


@pytest.mark.asyncio
async def test_cancel_task_tolerates_binding_window_then_routes_to_cancel():
    """A cancel during the DISPATCHING window waits briefly for the worker_task_id
    binding to land, then routes to the real cancel — the registration race is
    invisible instead of surfacing task_not_routable_yet."""
    from unittest.mock import AsyncMock

    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _real_store_with_runtime()
    dispatch = store.create_physical_dispatch("alice-fire", "Alice")
    store.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    registry = AgentRegistry()
    tool = SendMessageTool(
        store=store,
        registry=registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    with patch.object(
        tool._cancel_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="cancelled")),
    ) as mock_execute:
        # The binding lands "concurrently" right after the first poll.
        async def _land_binding():
            store.register_worker_task_id(dispatch.dispatch_id, "wt-race")

        task = asyncio.create_task(_land_binding())
        result = await tool.execute(
            message_type="cancel_task", related_task_id=dispatch.dispatch_id
        )
        await task
    assert result.success is True
    assert result.content == "cancelled"
    mock_execute.assert_awaited_once_with(task_id=dispatch.dispatch_id)


@pytest.mark.asyncio
async def test_reply_to_help_tolerates_binding_window_then_routes_to_respond():
    """reply_to_help during the DISPATCHING window waits briefly for the binding
    then routes to respond instead of surfacing task_not_routable_yet."""
    from unittest.mock import AsyncMock

    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _real_store_with_runtime()
    dispatch = store.create_physical_dispatch("charlie-help", "Charlie")
    store.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    registry = AgentRegistry()
    tool = SendMessageTool(
        store=store,
        registry=registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    with patch.object(
        tool._respond_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="replied")),
    ) as mock_execute:

        async def _land_binding():
            store.register_worker_task_id(dispatch.dispatch_id, "wt-help")

        task = asyncio.create_task(_land_binding())
        result = await tool.execute(
            message_type="reply_to_help",
            related_task_id=dispatch.dispatch_id,
            content="Go north",
        )
        await task
    assert result.success is True
    assert result.content == "replied"
    mock_execute.assert_awaited_once_with(
        task_id=dispatch.dispatch_id, response="Go north"
    )


# ---------------------------------------------------------------------------
# Declared-but-never-dispatched MissionGraph node cancel
# ---------------------------------------------------------------------------


def _store_with_graph(nodes, *, attach_runtime=False):
    """A TaskStore with declared MissionGraph nodes (optionally runtime-bound)."""
    store = (
        _real_store_with_runtime()
        if attach_runtime
        else TaskStore("request", router=None)
    )
    store.replace_mission_graph(nodes)
    return store


@pytest.mark.asyncio
async def test_cancel_declared_never_dispatched_node_succeeds():
    """Canceling a declared-but-never-dispatched graph node must succeed as an
    idempotent LOCAL cancel instead of failing with unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph([{"task_id": "bob-standby", "participant_ids": ["Bob"]}])
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="bob-standby"
    )
    assert result.success is True
    assert "never dispatched" in result.content
    assert result.data == {"logical_node_id": "bob-standby", "canceled_locally": True}
    node = store.get_mission_node("bob-standby")
    assert node is not None
    assert node.state == "canceled"


@pytest.mark.asyncio
async def test_cancel_declared_never_dispatched_node_idempotent():
    """Repeated cancel of a never-dispatched node stays successful (terminal)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [{"task_id": "david-standby", "participant_ids": ["David"]}]
    )
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    first = await tool.execute(
        message_type="cancel_task", related_task_id="david-standby"
    )
    second = await tool.execute(
        message_type="cancel_task", related_task_id="david-standby"
    )
    assert first.success is True
    assert second.success is True
    assert store.get_mission_node("david-standby").state == "canceled"


@pytest.mark.asyncio
async def test_cancel_declared_never_dispatched_node_blocks_dependents():
    """canceled is terminal: the node is not activatable and dependents stay
    blocked (dependency_incomplete) — no DAG deadlock, matching plan rollback."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [
            {"task_id": "bob-standby", "participant_ids": ["Bob"]},
            {
                "task_id": "recovery",
                "participant_ids": ["Alice"],
                "depends_on": ["bob-standby"],
            },
        ]
    )
    assert store.get_mission_node("recovery").state == "blocked"

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="bob-standby"
    )
    assert result.success is True
    # Canceled node is terminal and no longer activatable.
    assert store.get_mission_node("bob-standby").state == "canceled"
    ok, reason = store._mission_graph.can_activate("bob-standby")
    assert ok is False
    assert reason == "node_not_ready"
    # Dependents evaluate through existing dependency semantics: blocked, not
    # deadlocked (activatable with the stable dependency_incomplete reason).
    assert store.get_mission_node("recovery").state == "blocked"
    ok, reason = store._mission_graph.can_activate("recovery")
    assert ok is False
    assert reason == "dependency_incomplete"


@pytest.mark.asyncio
async def test_cancel_totally_unknown_id_still_unknown_task_id_with_graph():
    """A declared graph does NOT swallow genuinely unknown ids: they must still
    fail with unknown_task_id (LLM hallucination is not silently accepted)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph([{"task_id": "bob-standby", "participant_ids": ["Bob"]}])
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="ghost-node"
    )
    assert result.success is False
    assert result.error == "unknown_task_id"
    # The real graph node was not touched by the bogus cancel.
    assert store.get_mission_node("bob-standby").state == "ready"


@pytest.mark.asyncio
async def test_cancel_dispatched_graph_node_keeps_cleanup_path():
    """A graph node that WAS dispatched (has physical dispatch bindings) must
    NOT take the graph-only local cancel: it keeps the physical cleanup path
    (here the PREPARED idempotent local cleanup keyed by dispatch_id)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [{"task_id": "bob-standby", "participant_ids": ["Bob"]}],
        attach_runtime=True,
    )
    dispatches = store.create_dispatches_for_activation("bob-standby", ["Bob"])
    assert len(dispatches) == 1
    dispatch_id = dispatches[0].dispatch_id

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="bob-standby"
    )
    assert result.success is True
    # Physical cleanup path: dispatch_id key, not the graph-local logical cancel.
    assert result.data.get("dispatch_id") == dispatch_id
    assert result.data.get("logical_node_id") is None
    # The physical dispatch reached a terminal state.
    assert str(store.get_dispatch(dispatch_id).state.value) == "CANCELED"


# ---------------------------------------------------------------------------
# Cancel/reply of dispatch ids that existed but were cleaned up (rolled back)
# ---------------------------------------------------------------------------


def _store_with_rolled_back_dispatches():
    """A runtime-attached store whose dispatches were allocated then rolled back
    while still PREPARED (e.g. a team_setup_failed activation compensation).

    Mirrors scene_2_agents_4: dsp_f379f13f / dsp_b6fbe293 existed (TASK_STALE →
    TASK_RECOVERED) but are absent from the live dispatch map and the final
    control-state journal.
    """
    from a2a.coordinator.mission_runtime import MissionRuntimeManager
    from a2a.coordinator.task_store import TaskStore

    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-rollback")
    store = TaskStore("request", router=None)
    store.attach_runtime(runtime)
    first = store.create_physical_dispatch("rescue-jeremy", "Alice")
    second = store.create_physical_dispatch("rescue-jeremy", "David")
    runtime.rollback_prepared_dispatches(
        [first.dispatch_id, second.dispatch_id]
    )
    # Live lookup must fail: the dispatch is gone from the active map.
    assert store.resolve_dispatch_id(first.dispatch_id) is None
    return store, first.dispatch_id, second.dispatch_id


@pytest.mark.asyncio
async def test_cancel_cleaned_up_dispatch_is_idempotent_success():
    """Canceling a dispatch id that DID exist but was already cleaned up (rolled
    back while PREPARED) is an idempotent success — never unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatch_id, _ = _store_with_rolled_back_dispatches()
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id=dispatch_id
    )
    assert result.success is True
    assert "no longer active" in result.content
    assert result.data == {
        "dispatch_id": dispatch_id,
        "state": "PREPARED",
        "idempotent": True,
        "cleaned_up": True,
    }
    # No worker task existed and none could have been contacted.
    assert store.get_dispatch(dispatch_id) is None


@pytest.mark.asyncio
async def test_cancel_cleaned_up_dispatch_is_repeatable():
    """Repeated cancel of a cleaned-up dispatch stays idempotent success."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatch_id, _ = _store_with_rolled_back_dispatches()
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    first = await tool.execute(
        message_type="cancel_task", related_task_id=dispatch_id
    )
    second = await tool.execute(
        message_type="cancel_task", related_task_id=dispatch_id
    )
    assert first.success is True
    assert second.success is True


@pytest.mark.asyncio
async def test_cancel_never_existing_id_still_unknown_task_id():
    """An id that NEVER existed must still fail with unknown_task_id — the
    historical dispatch resolution never swallows a hallucinated id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, _, _ = _store_with_rolled_back_dispatches()
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task",
        related_task_id="dsp_ghost-0000-0000-0000-000000000000",
    )
    assert result.success is False
    assert result.error == "unknown_task_id"


@pytest.mark.asyncio
async def test_cancel_active_dispatch_unchanged():
    """An ACTIVE dispatch keeps the normal cancel path: it is never treated as a
    cleaned-up historical dispatch."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _real_store_with_runtime()
    dispatch = store.create_physical_dispatch("alice-fire", "Alice")
    store.register_worker_task_id(dispatch.dispatch_id, "wt-alice")
    store.apply_physical_status(dispatch.dispatch_id, "DISPATCHING", source="dispatch")
    registry = AgentRegistry()
    tool = SendMessageTool(store=store, registry=registry)
    with patch.object(
        tool._cancel_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="cancelled")),
    ) as mock_execute:
        result = await tool.execute(
            message_type="cancel_task", related_task_id=dispatch.dispatch_id
        )
    assert result.success is True
    assert result.content == "cancelled"
    mock_execute.assert_awaited_once_with(task_id=dispatch.dispatch_id)


@pytest.mark.asyncio
async def test_reply_cleaned_up_dispatch_is_idempotent_success():
    """Reply to a cleaned-up dispatch id is an idempotent success (analogous to
    cancel), never unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatch_id, _ = _store_with_rolled_back_dispatches()
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="reply_to_help",
        related_task_id=dispatch_id,
        content="Go north",
    )
    assert result.success is True
    assert "no reply sent" in result.content
    assert result.data == {
        "dispatch_id": dispatch_id,
        "state": "PREPARED",
        "idempotent": True,
    }


@pytest.mark.asyncio
async def test_reply_never_existing_id_still_unknown_task_id():
    """Reply to a never-existing id still fails with unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, _, _ = _store_with_rolled_back_dispatches()
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="reply_to_help",
        related_task_id="dsp_ghost-0000-0000-0000-000000000000",
        content="Go north",
    )
    assert result.success is False
    assert result.error == "unknown_task_id"


# ---------------------------------------------------------------------------
# Cancel/reply of an activated MissionGraph logical node (has dispatch bindings)
# ---------------------------------------------------------------------------


def _advance_dispatch(store, dispatch_id: str, target: str) -> None:
    """Advance a fresh PREPARED dispatch to *target* through legal transitions."""
    from a2a.coordinator.task_store import TaskStore

    assert isinstance(store, TaskStore)
    sequence = {
        "DISPATCHING": ["DISPATCHING"],
        "ACCEPTED": ["DISPATCHING", "ACCEPTED"],
        "RUNNING": ["DISPATCHING", "ACCEPTED", "RUNNING"],
        "COMPLETED": ["DISPATCHING", "ACCEPTED", "COMPLETED"],
        "FAILED": ["FAILED"],
        "CANCELED": ["CANCEL_PENDING", "CANCELED"],
    }[target]
    for state in sequence:
        store.apply_physical_status(dispatch_id, state, source="test_setup")


def _store_with_activated_node(node_id: str, participants: list[str]):
    """A runtime-attached store with a declared node activated to PREPARED."""
    from a2a.coordinator.mission_runtime import MissionRuntimeManager
    from a2a.coordinator.task_store import TaskStore

    manager = MissionRuntimeManager()
    runtime = manager.admit(f"ctx-{node_id}")
    store = TaskStore("request", router=None)
    store.attach_runtime(runtime)
    store.replace_mission_graph([{"task_id": node_id, "participant_ids": participants}])
    dispatches = store.create_dispatches_for_activation(node_id, list(participants))
    assert len(dispatches) == len(participants)
    return store, dispatches


@pytest.mark.asyncio
async def test_cancel_activated_then_completed_logical_node_is_idempotent_success():
    """Canceling a MissionGraph logical node that WAS activated and whose
    dispatch(es) have since completed is an idempotent success carrying the
    terminal state — never unknown_task_id (scene_2_agents_4: cancel of
    bob-townfire-assist / charlie-townfire-assist / david-fire-assist)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatches = _store_with_activated_node("bob-townfire-assist", ["Bob"])
    dispatch_id = dispatches[0].dispatch_id
    _advance_dispatch(store, dispatch_id, "COMPLETED")
    # The logical node still carries its physical dispatch binding.
    assert store.get_mission_node("bob-townfire-assist").dispatch_ids
    assert store.get_dispatch(dispatch_id).state.value == "COMPLETED"

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="bob-townfire-assist"
    )
    assert result.success is True
    assert "COMPLETED" in result.content
    assert "no active dispatches remain" in result.content
    assert result.data == {
        "logical_node_id": "bob-townfire-assist",
        "state": "COMPLETED",
        "dispatch_count": 1,
        "idempotent": True,
        "cleaned_up": True,
    }
    # The physical dispatch stays terminal; no worker was contacted.
    assert store.get_dispatch(dispatch_id).state.value == "COMPLETED"


@pytest.mark.asyncio
async def test_cancel_activated_logical_node_is_repeatable():
    """Repeated cancel of an activated-then-completed logical node stays an
    idempotent success."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatches = _store_with_activated_node("bob-townfire-assist", ["Bob"])
    _advance_dispatch(store, dispatches[0].dispatch_id, "COMPLETED")
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    first = await tool.execute(
        message_type="cancel_task", related_task_id="bob-townfire-assist"
    )
    second = await tool.execute(
        message_type="cancel_task", related_task_id="bob-townfire-assist"
    )
    assert first.success is True
    assert second.success is True


@pytest.mark.asyncio
async def test_reply_activated_then_completed_logical_node_is_idempotent_success():
    """Replying to an activated logical node whose dispatches have completed is
    an idempotent success carrying the terminal state (analogous to cancel)."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatches = _store_with_activated_node(
        "charlie-townfire-assist", ["Charlie"]
    )
    _advance_dispatch(store, dispatches[0].dispatch_id, "COMPLETED")

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="reply_to_help",
        related_task_id="charlie-townfire-assist",
        content="Go north",
    )
    assert result.success is True
    assert "COMPLETED" in result.content
    assert "no reply sent" in result.content
    assert result.data == {
        "logical_node_id": "charlie-townfire-assist",
        "state": "COMPLETED",
        "dispatch_count": 1,
        "idempotent": True,
    }


@pytest.mark.asyncio
async def test_cancel_activated_node_with_rolled_back_dispatches_is_idempotent_success():
    """An activated logical node whose dispatches were rolled back while still
    PREPARED (cleanup) is an idempotent cancel too — the physical records exist
    in dispatch history, so it is a real object, never unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatches = _store_with_activated_node("david-fire-assist", ["David"])
    dispatch_id = dispatches[0].dispatch_id
    store._runtime.rollback_prepared_dispatches([dispatch_id])
    assert store.get_dispatch(dispatch_id) is None

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="david-fire-assist"
    )
    assert result.success is True
    assert "PREPARED" in result.content
    assert result.data["logical_node_id"] == "david-fire-assist"
    assert result.data["state"] == "PREPARED"
    assert result.data["idempotent"] is True


@pytest.mark.asyncio
async def test_cancel_mixed_active_and_terminal_logical_node_takes_normal_path():
    """A logical node with a MIX of terminal and still-live dispatches must NOT
    silently succeed: the cancel takes the normal remote path for the live
    dispatch, never an idempotent success while worker work is live."""
    from unittest.mock import AsyncMock

    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store, dispatches = _store_with_activated_node("rescue-jeremy", ["Alice", "Bob"])
    alice_dsp, bob_dsp = dispatches
    _advance_dispatch(store, alice_dsp.dispatch_id, "COMPLETED")
    store.register_worker_task_id(bob_dsp.dispatch_id, "wt-bob")
    _advance_dispatch(store, bob_dsp.dispatch_id, "RUNNING")
    assert store.get_dispatch(bob_dsp.dispatch_id).state.value == "RUNNING"

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    with patch.object(
        tool._cancel_tool,
        "execute",
        new=AsyncMock(return_value=MagicMock(success=True, content="cancelled")),
    ) as mock_execute:
        result = await tool.execute(
            message_type="cancel_task", related_task_id="rescue-jeremy"
        )
    assert result.success is True
    assert result.content == "cancelled"
    # Normal remote cancel routed to the still-live dispatch, not idempotent.
    mock_execute.assert_awaited_once_with(task_id=bob_dsp.dispatch_id)


@pytest.mark.asyncio
async def test_cancel_never_existing_logical_id_still_unknown_task_id_with_runtime():
    """With a runtime + declared graph attached, a never-existing id still fails
    with unknown_task_id — the logical-node historical resolution never swallows
    a hallucinated id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [{"task_id": "bob-standby", "participant_ids": ["Bob"]}],
        attach_runtime=True,
    )
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="ghost-node"
    )
    assert result.success is False
    assert result.error == "unknown_task_id"
    # The real graph node was not touched by the bogus cancel.
    assert store.get_mission_node("bob-standby").state == "ready"


@pytest.mark.asyncio
async def test_cancel_activated_logical_node_via_runtime_activation_path():
    """Cancel of an activated logical node via the REAL runtime activation path
    (MissionRuntime.activate_plan_node → _run_atomic_claim), which attaches
    bindings to the graph node but never to TaskStore._logical_to_dispatches.
    Mirrors scene_2_agents_4 exactly: david-fire-assist (dsp_f9315036) was
    activated and later COMPLETED, and cancel by logical id must be an
    idempotent success, never unknown_task_id."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [{"task_id": "david-fire-assist", "participant_ids": ["David"]}],
        attach_runtime=True,
    )

    async def _fake_dispatch(worker_id, prompt, callback_url, dispatch_id, context_id):
        return f"wt-{worker_id}"

    store._runtime.set_dispatch_adapter(_fake_dispatch)
    result = await store._runtime.activate_plan_node(
        "david-fire-assist", store._mission_graph
    )
    assert result["success"] is True
    dispatch_ids = [d["dispatch_id"] for d in result["dispatches"]]
    assert len(dispatch_ids) == 1
    # Production activation leaves TaskStore's logical→dispatch map untouched.
    assert store._logical_to_dispatches.get("david-fire-assist") is None
    # The graph node carries the physical binding.
    node = store.get_mission_node("david-fire-assist")
    assert node.dispatch_ids
    assert set(node.dispatch_ids.values()) == set(dispatch_ids)

    # The dispatch later completes (RUNNING → COMPLETED), still live in the map.
    _advance_dispatch(store, dispatch_ids[0], "COMPLETED")
    assert store.get_dispatch(dispatch_ids[0]).state.value == "COMPLETED"

    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="david-fire-assist"
    )
    assert result.success is True
    assert "COMPLETED" in result.content
    assert result.data["logical_node_id"] == "david-fire-assist"
    assert result.data["state"] == "COMPLETED"
    assert result.data["idempotent"] is True


@pytest.mark.asyncio
async def test_cancel_never_dispatched_node_keeps_local_cancel_path_with_runtime():
    """A declared-but-never-dispatched graph node keeps the existing idempotent
    LOCAL cancel path even with a runtime attached — the historical logical-node
    resolution must not shadow it."""
    from a2a.builtin_tools.send_message import SendMessageTool
    from a2a.coordinator.agent_registry import AgentRegistry

    store = _store_with_graph(
        [{"task_id": "charlie-standby", "participant_ids": ["Charlie"]}],
        attach_runtime=True,
    )
    tool = SendMessageTool(store=store, registry=AgentRegistry())
    result = await tool.execute(
        message_type="cancel_task", related_task_id="charlie-standby"
    )
    assert result.success is True
    assert result.data == {
        "logical_node_id": "charlie-standby",
        "canceled_locally": True,
    }
    assert store.get_mission_node("charlie-standby").state == "canceled"
