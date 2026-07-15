"""Tests for CoordinatorSenderService."""

from __future__ import annotations

import json

import pytest

from a2a.coordinator.sender_service import CoordinatorSenderService
from a2a.types.a2a_pb2 import StreamResponse
from google.protobuf.json_format import ParseDict


def _fake_task_response(task_id: str, state: int = 3) -> StreamResponse:
    """Build StreamResponse with task field.  3=TASK_STATE_COMPLETED, 4=FAILED."""
    return ParseDict({"task": {"id": task_id, "status": {"state": state}}}, StreamResponse())


def _fake_status_update_response(task_id: str, state: int = 3) -> StreamResponse:
    """Build StreamResponse with status_update field."""
    return ParseDict(
        {"statusUpdate": {"taskId": task_id, "status": {"state": state}}},
        StreamResponse(),
    )


class _OkClient:
    """Fake client that yields a single terminal task response."""

    def __init__(self, response: StreamResponse | None = None):
        self._response = response or _fake_task_response("t-ok")
        self._sent_messages: list = []
        self.closed = False

    async def send_message(self, request):
        self._sent_messages.append(request)
        yield self._response

    async def close(self):
        self.closed = True


class _ErrClient:
    """Fake client whose send_message yields a task then raises."""

    async def send_message(self, request):
        yield _fake_task_response("t-err")
        raise ConnectionError("Connection refused")

    async def close(self):
        pass


@pytest.fixture
def secret() -> bytes:
    return b"test-coordinator-secret-32bytes!!"


@pytest.fixture
def sender(secret: bytes) -> CoordinatorSenderService:
    return CoordinatorSenderService(coordinator_secret=secret, coordinator_id="Coordinator")


@pytest.mark.asyncio
async def test_send_envelope_success(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient()
    result = await sender.send_envelope('{"p":"a2a-peer"}', "http://w:9999/")
    assert result["success"] is True
    assert result["task_id"] == "t-ok"


@pytest.mark.asyncio
async def test_send_control_completed(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient(_fake_task_response("t-1", 3))
    from a2a.shared.message_envelope import MessageEnvelope, MessageKind
    env = MessageEnvelope(kind=MessageKind.MAIL, sender_id="C", recipient_id="W", body={"content": "hi"})
    result = await sender.send_control(env, "http://w:9999/")
    assert result["success"] is True
    assert result["task_id"] == "t-1"


@pytest.mark.asyncio
async def test_send_control_status_update(sender: CoordinatorSenderService):
    """send_control detects terminal state via status_update field."""
    sender._client_cache["http://w:9999/"] = _OkClient(_fake_status_update_response("t-su", 3))
    from a2a.shared.message_envelope import MessageEnvelope, MessageKind
    env = MessageEnvelope(kind=MessageKind.MAIL, sender_id="C", recipient_id="W", body={"content": "hi"})
    result = await sender.send_control(env, "http://w:9999/")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_send_control_failed(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient(_fake_task_response("t-fail", 4))  # 4=FAILED
    from a2a.shared.message_envelope import MessageEnvelope, MessageKind
    env = MessageEnvelope(kind=MessageKind.MAIL, sender_id="C", recipient_id="W", body={"content": "hi"})
    result = await sender.send_control(env, "http://w:9999/")
    assert result["success"] is False
    assert "TASK_STATE_FAILED" in result.get("task_status", "")


@pytest.mark.asyncio
async def test_send_mail(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient()
    result = await sender.send_mail(recipient_id="W", recipient_endpoint="http://w:9999/", subject="Hi", body="Msg")
    assert result["success"] is True
    msg = sender._client_cache["http://w:9999/"]._sent_messages[0]
    data = json.loads(msg.message.parts[0].text)
    assert data["kind"] == "mail"
    assert data["sender_id"] == "Coordinator"
    assert data["sent_at"] != data["expires_at"]
    assert "signature" in data


@pytest.mark.asyncio
async def test_send_team_update(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient()
    result = await sender.send_team_update(
        worker_id="W", worker_endpoint="http://w:9999/",
        team_id="t-7", epoch=3, members=["W"], endpoints={"W": "http://w:9999/"},
        team_secret=b"team-secret-32bytes-for-testing!",
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_send_team_revoke(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient()
    result = await sender.send_team_revoke(worker_id="W", worker_endpoint="http://w:9999/", team_id="t-7", epoch=3)
    assert result["success"] is True


@pytest.mark.asyncio
async def test_send_signed_task_has_signature(sender: CoordinatorSenderService):
    sender._client_cache["http://w:9999/"] = _OkClient()
    result = await sender.send_signed_task(recipient_id="W", recipient_endpoint="http://w:9999/", prompt="do it")
    assert result["success"] is True
    msg = sender._client_cache["http://w:9999/"]._sent_messages[0]
    data = json.loads(msg.message.parts[0].text)
    assert data["kind"] == "task"
    assert "signature" in data
    assert len(data["signature"]) == 64


def test_empty_secret_raises():
    with pytest.raises(ValueError, match="non-empty"):
        CoordinatorSenderService(coordinator_secret=b"")


def test_zero_timeout_raises():
    with pytest.raises(ValueError, match="> 0"):
        CoordinatorSenderService(coordinator_secret=b"1234567890123456", default_timeout=0)


@pytest.mark.asyncio
async def test_close_cleans_clients(sender: CoordinatorSenderService):
    c = _OkClient()
    sender._client_cache["http://w:9999/"] = c
    await sender.close()
    assert c.closed
