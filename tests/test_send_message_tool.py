"""Tests for SendMessageTool — Coordinator communication facade."""

from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus, AgentNotFoundError, AgentRegistry
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
        registry.register(AgentInfo(
            agent_id="Alice",
            description="Test worker",
            endpoint="http://localhost:8001",
            capabilities=["sar"],
        ))

        # Simulate an active task for Alice
        store.add_adhoc_node("alice-task-1", worker_id="Alice", description="Active task")
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
        registry.register(AgentInfo(
            agent_id="Bob",
            description="Test worker",
            endpoint="http://localhost:8002",
            capabilities=["sar"],
        ))

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
        store.update_plan([
            {"task_id": "task-1", "worker_id": "Alice", "description": "Task 1"},
            {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
        ])
        store.set_state("task-1", "running")

        # Update plan, removing task-1
        result = store.update_plan([
            {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
        ])

        assert "task-1" in result["removed"]
        node = store.get_node("task-1")
        assert node.state == "canceled"
