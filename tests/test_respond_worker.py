"""Tests for RespondWorkerTool — Coordinator → Worker 回复（A2A send_message 版）。"""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from a2a.builtin_tools.respond_worker import RespondWorkerTool
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
    return RespondWorkerTool(store=mock_store, registry=mock_registry)


class TestRespondWorkerToolProperties:
    def test_name(self, tool):
        assert tool.name == "respond_worker"

    def test_parameters_schema(self, tool):
        schema = tool.parameters
        assert "task_id" in schema["properties"]
        assert "response" in schema["properties"]
        assert "task_id" in schema["required"]
        assert "response" in schema["required"]


@pytest.mark.asyncio
async def test_execute_sends_a2a_message(tool, mock_store, mock_registry):
    """正常流程：查找 task → 查找 worker → A2A send_message 恢复。"""
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

    # Mock A2A client
    mock_client = MagicMock()

    # Simulate one stream response then stop
    async def fake_stream(*args, **kwargs):
        yield MagicMock()

    mock_client.send_message = fake_stream
    mock_client.close = AsyncMock()

    with patch(
        "a2a.builtin_tools.respond_worker.create_client",
        new=AsyncMock(return_value=mock_client),
    ):
        result = await tool.execute(task_id="task-42", response="Go to sector 7")

    assert result.success is True
    assert "task-42" in result.content


@pytest.mark.asyncio
async def test_execute_task_not_found(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = None
    result = await tool.execute(task_id="nonexistent", response="Hello")
    assert result.success is False
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_execute_worker_not_in_registry(tool, mock_store, mock_registry):
    node = PlanNode(task_id="task-99", worker_id="lost-worker")
    mock_store.get_node.return_value = node
    mock_registry.get.side_effect = AgentNotFoundError("lost-worker")

    result = await tool.execute(task_id="task-99", response="Hello")
    assert result.success is False
    assert "not found in registry" in result.content
