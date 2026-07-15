"""Tests for signed TASK dispatch and allow_legacy_tasks flag.

Includes RouterAgent send_task_async/push_task signing and
worker ingress allow_legacy_tasks behavior.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from a2a.shared.message_envelope import (
    MessageKind,
    pack_message,
    is_envelope_payload,
    unpack_message,
)
from a2a.worker.ingress import EnvelopeIngress


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coord_secret() -> bytes:
    return b"coord-secret-32bytes!!"


@pytest.fixture
def worker_id() -> str:
    return "worker-alpha"


# ---------------------------------------------------------------------------
# Signed task envelope construction
# ---------------------------------------------------------------------------


def test_signed_task_envelope_is_valid(coord_secret: bytes):
    body = {"content": "Do the thing", "task_id": "t-42"}
    packed = pack_message(
        body=body,
        kind=MessageKind.TASK,
        sender_id="Coordinator",
        recipient_id="worker-alpha",
        secret=coord_secret,
    )
    data = json.loads(packed)
    assert data["protocol"] == "a2a-peer"
    assert data["kind"] == "task"
    assert "signature" in data
    assert len(data["signature"]) == 64


def test_signed_task_detected_as_envelope(coord_secret: bytes):
    packed = pack_message(
        body="simple task",
        kind=MessageKind.TASK,
        sender_id="Coordinator",
        recipient_id="worker-alpha",
        secret=coord_secret,
    )
    assert is_envelope_payload(packed) is True


def test_signed_task_unpacks_correctly(coord_secret: bytes):
    packed = pack_message(
        body="simple task",
        kind=MessageKind.TASK,
        sender_id="Coordinator",
        recipient_id="worker-alpha",
        secret=coord_secret,
    )
    body, envelope = unpack_message(packed, coord_secret)
    assert body == {"content": "simple task"}
    assert envelope is not None
    assert envelope.kind == MessageKind.TASK


# ---------------------------------------------------------------------------
# RouterAgent signing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_send_task_async_signs_when_secret_set(coord_secret: bytes):
    """When coordinator_secret is set, send_task_async wraps prompt in a signed TASK envelope."""
    from a2a.coordinator.router import RouterAgent
    from a2a.coordinator.agent_registry import AgentRegistry, AgentInfo, AgentStatus

    agent_reg = MagicMock(spec=AgentRegistry)
    agent_reg.get.return_value = AgentInfo(
        agent_id="worker-alpha", description="t",
        endpoint="http://w:9999/", status=AgentStatus.ONLINE,
    )

    router = RouterAgent(
        registry=agent_reg,
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
    )

    # Mock the SDK client to capture the payload
    sent_payloads: list[str] = []

    class _MockClient:
        async def send_message(self, request):
            sent_payloads.append(request.message.parts[0].text)
            from a2a.types.a2a_pb2 import StreamResponse
            from google.protobuf.json_format import ParseDict
            msg = ParseDict(
                {"task": {"id": "t-987", "status": {"state": 4}}},
                StreamResponse(),
            )
            yield msg

        async def close(self):
            pass

    router._sdk_clients_non_streaming["worker-alpha"] = _MockClient()

    await router.send_task_async(
        agent_id="worker-alpha",
        prompt="Do the thing",
        callback_url="http://c:8080/cb",
        task_id="task-42",
    )

    assert len(sent_payloads) == 1
    payload = json.loads(sent_payloads[0])
    assert payload["protocol"] == "a2a-peer"
    assert payload["kind"] == "task"
    assert payload["body"]["content"] == "Do the thing"
    assert "signature" in payload


@pytest.mark.asyncio
async def test_router_send_task_async_legacy_when_no_secret():
    """When coordinator_secret is None, send_task_async sends raw prompt."""
    from a2a.coordinator.router import RouterAgent
    from a2a.coordinator.agent_registry import AgentRegistry, AgentInfo, AgentStatus

    agent_reg = MagicMock(spec=AgentRegistry)
    agent_reg.get.return_value = AgentInfo(
        agent_id="worker-alpha", description="t",
        endpoint="http://w:9999/", status=AgentStatus.ONLINE,
    )

    router = RouterAgent(registry=agent_reg, coordinator_secret=None)

    sent_text: str | None = None

    class _MockClient:
        async def send_message(self, request):
            nonlocal sent_text
            sent_text = request.message.parts[0].text
            from a2a.types.a2a_pb2 import StreamResponse
            from google.protobuf.json_format import ParseDict
            msg = ParseDict(
                {"task": {"id": "t-1", "status": {"state": 4}}},
                StreamResponse(),
            )
            yield msg

        async def close(self):
            pass

    router._sdk_clients_non_streaming["worker-alpha"] = _MockClient()

    await router.send_task_async(
        agent_id="worker-alpha",
        prompt="Do the thing",
        callback_url="http://c:8080/cb",
        task_id="task-42",
    )

    # Legacy: plain text, no envelope
    assert sent_text == "Do the thing"


# ---------------------------------------------------------------------------
# Signed task envelope expiry/uniqueness
# ---------------------------------------------------------------------------


def test_signed_task_envelope_has_expiry_and_uuid(coord_secret: bytes):
    """Signed task envelope has sent_at != expires_at and unique message_id."""
    from a2a.shared.message_envelope import MessageEnvelope, MessageKind, sign_envelope
    import uuid

    sent_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    e1 = MessageEnvelope(
        kind=MessageKind.TASK, sender_id="C", recipient_id="W",
        body={"content": "t1"}, message_id=uuid.uuid4().hex,
        sent_at=sent_at,
    )
    sign_envelope(e1, coord_secret)
    d1 = e1.model_dump()
    assert d1["sent_at"] != d1["expires_at"]  # expires_at set by MessageEnvelope default

    e2 = MessageEnvelope(
        kind=MessageKind.TASK, sender_id="C", recipient_id="W",
        body={"content": "t2"}, message_id=uuid.uuid4().hex,
        sent_at=sent_at,
    )
    sign_envelope(e2, coord_secret)
    assert e1.message_id != e2.message_id


# ---------------------------------------------------------------------------
# Router _sign_task_envelope helper tests
# ---------------------------------------------------------------------------


def test_sign_task_envelope_uses_uuid4():
    from a2a.coordinator.router import RouterAgent
    from a2a.coordinator.agent_registry import AgentRegistry, AgentInfo, AgentStatus
    from unittest.mock import MagicMock

    agent_reg = MagicMock(spec=AgentRegistry)
    agent_reg.get.return_value = AgentInfo(
        agent_id="w", description="t", endpoint="http://w:1/", status=AgentStatus.ONLINE,
    )
    router = RouterAgent(registry=agent_reg, coordinator_secret=b"1234567890123456")
    text = router._sign_task_envelope("w", "hello", {"task_id": "t-42"})
    import json
    data = json.loads(text)
    assert data["kind"] == "task"
    assert data["body"]["content"] == "hello"
    assert data["body"]["task_id"] == "t-42"
    assert "signature" in data
    assert data["sent_at"] != data["expires_at"]
    # message_id should be a 32-char hex (uuid4 hex)
    assert len(data["message_id"]) == 32
    import re
    assert re.match(r"^[0-9a-f]{32}$", data["message_id"])


# ---------------------------------------------------------------------------
# allow_legacy_tasks=True (default)
# ---------------------------------------------------------------------------


def test_legacy_allowed_default(coord_secret: bytes, worker_id: str):
    ingress = EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id=worker_id,
        allow_legacy_tasks=True,
    )
    result = ingress.classify("Just plain text")
    assert result.action == "legacy"
    assert result.body == {"content": "Just plain text"}


# ---------------------------------------------------------------------------
# allow_legacy_tasks=False — rejects plain text
# ---------------------------------------------------------------------------


def test_legacy_rejected_when_disabled(coord_secret: bytes, worker_id: str):
    ingress = EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id=worker_id,
        allow_legacy_tasks=False,
    )
    result = ingress.classify("Just plain text")
    assert result.action == "reject"
    assert "legacy" in (result.reason or "").lower()


# ---------------------------------------------------------------------------
# Signed task still accepted when legacy disabled
# ---------------------------------------------------------------------------


def test_signed_task_accepted_when_legacy_disabled(coord_secret: bytes, worker_id: str):
    ingress = EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id=worker_id,
        allow_legacy_tasks=False,
    )
    packed = pack_message(
        body="signed task content",
        kind=MessageKind.TASK,
        sender_id="Coordinator",
        recipient_id=worker_id,
        secret=coord_secret,
    )
    result = ingress.classify(packed)
    assert result.action == "task"
    assert result.body["content"] == "signed task content"


# ---------------------------------------------------------------------------
# Legacy compatibility: unsigned task still works by default
# ---------------------------------------------------------------------------


def test_unsigned_task_compatibility(coord_secret: bytes, worker_id: str):
    ingress = EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id=worker_id,
        allow_legacy_tasks=True,
    )
    result = ingress.classify("Do something important")
    assert result.action == "legacy"
