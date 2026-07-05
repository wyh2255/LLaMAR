"""Tests for CancelTaskTool."""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.coordinator.task_store import PlanNode
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus, AgentNotFoundError


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_node = MagicMock()
    store._dispatch_to_worker = {}
    store._worker_to_dispatch = {}
    return store


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get = MagicMock()
    return registry


@pytest.fixture
def tool(mock_store, mock_registry):
    return CancelTaskTool(store=mock_store, registry=mock_registry)


class TestCancelTaskToolProperties:
    def test_name(self, tool):
        assert tool.name == "cancel_task"

    def test_parameters_schema(self, tool):
        schema = tool.parameters
        assert "task_id" in schema["properties"]
        assert "task_id" in schema["required"]


@pytest.mark.asyncio
async def test_execute_cancels_worker_task(tool, mock_store, mock_registry):
    """正常流程：dispatch_id → worker_task_id → A2A cancel."""
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = "worker-uuid-42"
    mock_store.get_node.return_value = node
    mock_registry.get.return_value = AgentInfo(
        agent_id="worker-1",
        description="Test worker",
        endpoint="http://worker-1:8090",
        status=AgentStatus.ONLINE,
    )

    mock_task = MagicMock()
    mock_task.status.state = 5  # TASK_STATE_CANCELED

    mock_client = MagicMock()
    mock_client.cancel_task = AsyncMock(return_value=mock_task)
    mock_client.close = AsyncMock()

    patched_create_client = AsyncMock(return_value=mock_client)
    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        patched_create_client,
    ):
        result = await tool.execute(task_id="task-42")

    assert result.success is True
    assert "worker-uuid-42" in result.content
    mock_client.cancel_task.assert_called_once()
    patched_create_client.assert_awaited_once()
    assert patched_create_client.call_args[0][0] == "http://worker-1:8090"


@pytest.mark.asyncio
async def test_execute_task_not_found(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = None
    result = await tool.execute(task_id="nonexistent")
    assert result.success is False
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_execute_worker_not_in_registry(tool, mock_store, mock_registry):
    node = PlanNode(task_id="task-99", worker_id="lost-worker")
    mock_store.resolve_dispatch_id.return_value = "task-99"
    mock_store._dispatch_to_worker["task-99"] = "worker-uuid-99"
    mock_store.get_node.return_value = node
    mock_registry.get.side_effect = AgentNotFoundError("lost-worker")

    result = await tool.execute(task_id="task-99")
    assert result.success is False
    assert "not found in registry" in result.content


@pytest.mark.asyncio
async def test_execute_connection_failed(tool, mock_store, mock_registry):
    """create_client raises → ToolResult(success=False)."""
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = "worker-uuid-42"
    mock_store.get_node.return_value = node
    mock_registry.get.return_value = AgentInfo(
        agent_id="worker-1",
        description="Test worker",
        endpoint="http://worker-1:8090",
        status=AgentStatus.ONLINE,
    )

    patched_create_client = AsyncMock(side_effect=RuntimeError("Connection refused"))
    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        patched_create_client,
    ):
        result = await tool.execute(task_id="task-42")

    assert result.success is False
    assert "Failed to connect" in result.content


@pytest.mark.asyncio
async def test_execute_cancel_failed(tool, mock_store, mock_registry):
    """client.cancel_task raises → ToolResult(success=False); client.close still called."""
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = "worker-uuid-42"
    mock_store.get_node.return_value = node
    mock_registry.get.return_value = AgentInfo(
        agent_id="worker-1",
        description="Test worker",
        endpoint="http://worker-1:8090",
        status=AgentStatus.ONLINE,
    )

    mock_client = MagicMock()
    mock_client.cancel_task = AsyncMock(side_effect=RuntimeError("cancel failed"))
    mock_client.close = AsyncMock()

    patched_create_client = AsyncMock(return_value=mock_client)
    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        patched_create_client,
    ):
        result = await tool.execute(task_id="task-42")

    assert result.success is False
    assert "Cancel request failed" in result.content
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_not_yet_dispatched(tool, mock_store):
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = ""
    mock_store.get_node.return_value = node

    result = await tool.execute(task_id="task-42")
    assert result.success is False
    assert "not been dispatched" in result.content
