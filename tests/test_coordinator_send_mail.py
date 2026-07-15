"""Tests for SendMailTool — coordinator mail delivery without TaskStore."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a.builtin_tools.send_mail import SendMailTool
from a2a.coordinator.sender_service import CoordinatorSenderService
from a2a.shared.types import WorkerNode, WorkerStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sender():
    s = MagicMock(spec=CoordinatorSenderService)
    s.send_mail = AsyncMock(return_value={"success": True, "task_id": "mail-t-1"})
    return s


@pytest.fixture
def mock_worker_registry():
    r = MagicMock()
    r.get.side_effect = lambda wid: WorkerNode(
        worker_id=wid,
        a2a_endpoint=f"http://{wid}:9999/",
        status=WorkerStatus.ONLINE,
    )
    return r


@pytest.fixture
def tool(mock_sender, mock_worker_registry):
    return SendMailTool(
        sender=mock_sender,
        worker_registry=mock_worker_registry,
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_name(tool: SendMailTool):
    assert tool.name == "send_mail"


def test_parameters(tool: SendMailTool):
    schema = tool.parameters
    assert "recipient_id" in schema["required"]
    assert "subject" in schema["required"]
    assert "body" in schema["required"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_recipient(tool: SendMailTool):
    result = await tool.execute(recipient_id="", subject="Hi", body="Hello")
    assert result.success is False
    assert result.error == "missing_recipient"


@pytest.mark.asyncio
async def test_missing_subject(tool: SendMailTool):
    result = await tool.execute(recipient_id="Alice", subject="", body="Hello")
    assert result.success is False
    assert result.error == "missing_subject"


@pytest.mark.asyncio
async def test_missing_body(tool: SendMailTool):
    result = await tool.execute(recipient_id="Alice", subject="Hi", body="")
    assert result.success is False
    assert result.error == "missing_body"


@pytest.mark.asyncio
async def test_subject_too_long(tool: SendMailTool):
    result = await tool.execute(recipient_id="Alice", subject="x" * 300, body="Hello")
    assert result.success is False
    assert result.error == "subject_too_long"


@pytest.mark.asyncio
async def test_body_too_long(tool: SendMailTool):
    result = await tool.execute(recipient_id="Alice", subject="Hi", body="x" * 70000)
    assert result.success is False
    assert result.error == "body_too_long"


@pytest.mark.asyncio
async def test_recipient_not_found(mock_sender):
    """SendMailTool without any registry — all workers are unknown."""
    tool = SendMailTool(sender=mock_sender)
    result = await tool.execute(recipient_id="Ghost", subject="Hi", body="Hello")
    assert result.success is False
    assert result.error == "recipient_not_found"


@pytest.mark.asyncio
async def test_worker_not_online(tool: SendMailTool, mock_worker_registry):
    mock_worker_registry.get.side_effect = None
    mock_worker_registry.get.return_value = WorkerNode(
        worker_id="Alice", a2a_endpoint="http://Alice:9999/", status=WorkerStatus.OFFLINE
    )
    result = await tool.execute(recipient_id="Alice", subject="Hi", body="Hello")
    assert result.success is False
    assert result.error == "worker_offline"


@pytest.mark.asyncio
async def test_worker_busy_is_reachable(tool: SendMailTool, mock_worker_registry, mock_sender):
    """BUSY workers should still receive mail."""
    mock_worker_registry.get.side_effect = None
    mock_worker_registry.get.return_value = WorkerNode(
        worker_id="Alice", a2a_endpoint="http://Alice:9999/", status=WorkerStatus.BUSY
    )
    result = await tool.execute(recipient_id="Alice", subject="Hi", body="Hello")
    assert result.success is True


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_mail_success(tool: SendMailTool, mock_sender):
    result = await tool.execute(
        recipient_id="Alice",
        subject="Status update",
        body="Mission is progressing well",
    )
    assert result.success is True
    assert "Alice" in result.content
    assert "ACK" in result.content
    assert "task_id" not in result.content

    mock_sender.send_mail.assert_awaited_once_with(
        recipient_id="Alice",
        recipient_endpoint="http://Alice:9999/",
        subject="Status update",
        body="Mission is progressing well",
    )


# ---------------------------------------------------------------------------
# No TaskStore involvement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_taskstore_side_effects(tool: SendMailTool, mock_sender):
    assert not hasattr(tool, "_store")
    assert not hasattr(tool, "_dispatch_tool")
    assert not hasattr(tool, "_cancel_tool")
    assert not hasattr(mock_sender, "_store")


# ---------------------------------------------------------------------------
# Delivery failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure(tool: SendMailTool, mock_sender):
    mock_sender.send_mail.return_value = {"success": False, "error": "worker offline"}
    result = await tool.execute(
        recipient_id="Alice",
        subject="Hi",
        body="Hello",
    )
    assert result.success is False
    assert result.error == "send_failed"


# ---------------------------------------------------------------------------
# Agent registry fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_registry_fallback():
    from a2a.coordinator.agent_registry import AgentInfo, AgentStatus

    class _FakeAgentRegistry:
        def get(self, wid):
            return AgentInfo(
                agent_id=wid, description="test",
                endpoint="http://agent-fallback:8888/",
                status=AgentStatus.ONLINE,
            )

    mock_sender = MagicMock(spec=CoordinatorSenderService)
    mock_sender.send_mail = AsyncMock(return_value={"success": True, "task_id": "t-1"})

    tool = SendMailTool(sender=mock_sender, agent_registry=_FakeAgentRegistry())
    result = await tool.execute(recipient_id="Alice", subject="Hi", body="Hello")
    assert result.success is True
    mock_sender.send_mail.assert_awaited_once()
    args = mock_sender.send_mail.await_args
    assert args.kwargs["recipient_endpoint"] == "http://agent-fallback:8888/"
