"""Phase 2 worker callback signing contracts: SignedPushNotificationSender and
AgentCard capability consistency."""

from __future__ import annotations

import hashlib

import pytest

from a2a.coordinator.memory.callback_auth import (
    CallbackProofV1,
    MemoryAuthNotConfiguredError,
)
from a2a.helpers import new_text_message
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.worker.callback_sender import (
    HEADER_PROOF,
    HEADER_WORKER_ID,
    CallbackSigner,
    SignedPushNotificationSender,
)

SECRET = b"worker-coordinator-shared-secret-0123456789"
SECRET_TEXT = "SUPERSECRET_WORKER_9f2c1"


class _RecordingClient:
    def __init__(self):
        self.requests: list[tuple[str, bytes, dict]] = []

    async def post(self, url, content=None, headers=None, **kwargs):
        self.requests.append((url, content, headers))
        return _Response()


class _Response:
    def raise_for_status(self):
        return None


class _ConfigStore:
    def __init__(self, url, token=None):
        self._url = url
        self._token = token

    async def get_info_for_dispatch(self, task_id):
        class _Info:
            url = self._url
            token = self._token

        return [_Info()]


def _event():
    status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
    status.message.CopyFrom(new_text_message("working hard"))
    return TaskStatusUpdateEvent(task_id="t-1", context_id="ctx-1", status=status)


def test_callback_signer_signs_actual_body_bytes():
    signer = CallbackSigner("Alice", SECRET)
    body = b'{"task": {"id": "t-1"}}'
    proof = signer.sign(body)
    body_sha256 = hashlib.sha256(body).hexdigest()
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", body_sha256)
    assert ok is True
    assert reason == ""
    # Signer never leaks the secret.
    assert SECRET_TEXT not in proof
    assert SECRET.decode() not in proof


def test_callback_signer_rejects_short_or_missing_secret():
    with pytest.raises(MemoryAuthNotConfiguredError):
        CallbackSigner("Alice", b"short")
    with pytest.raises(MemoryAuthNotConfiguredError):
        CallbackSigner("Alice", None)
    with pytest.raises(MemoryAuthNotConfiguredError):
        CallbackSigner("Alice", b"")


def test_sender_binds_header_to_actual_posted_body():
    client = _RecordingClient()
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=signer
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))

    assert len(client.requests) == 1
    url, body, headers = client.requests[0]
    assert url == "http://coordinator/a2a/push-callback"
    proof = headers[HEADER_PROOF]
    assert headers[HEADER_WORKER_ID] == "Alice"

    body_sha256 = hashlib.sha256(body).hexdigest()
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", body_sha256)
    assert ok is True, reason


def test_retry_uses_new_nonce_but_same_body():
    client = _RecordingClient()
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=signer
    )
    import asyncio

    event = _event()
    asyncio.run(sender.send_notification("t-1", event))
    asyncio.run(sender.send_notification("t-1", event))
    assert len(client.requests) == 2

    bodies = [r[1] for r in client.requests]
    proofs = [r[2][HEADER_PROOF] for r in client.requests]
    assert bodies[0] == bodies[1], "retry must send the identical body"
    nonces = [CallbackProofV1.nonce_of(p) for p in proofs]
    assert nonces[0] != nonces[1], "retry must use a fresh nonce"
    body_sha256 = hashlib.sha256(bodies[0]).hexdigest()
    for proof in proofs:
        ok, _ = CallbackProofV1.verify(SECRET, proof, "Alice", body_sha256)
        assert ok is True


def test_secret_never_appears_in_request_or_status_text():
    client = _RecordingClient()
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=signer
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    url, body, headers = client.requests[0]
    joined = " ".join([url, body.decode("utf-8"), str(headers)])
    assert SECRET.decode("utf-8") not in joined
    assert SECRET_TEXT not in joined


def test_sender_without_signer_advertises_no_push_but_still_sends_legacy():
    client = _RecordingClient()
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=None
    )
    assert sender.signer_available() is False
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 1
    assert HEADER_PROOF not in client.requests[0][2]


def test_agent_card_push_capability_matches_signer_availability(tmp_path):
    from a2a.worker.a2a_server import create_worker_a2a_server

    signer = CallbackSigner("Alice", SECRET)
    with_signer = create_worker_a2a_server(
        worker_id="Alice",
        port=8991,
        log_dir=tmp_path,
        callback_signer=signer,
    )
    assert with_signer.agent_card.capabilities.push_notifications is True

    without = create_worker_a2a_server(
        worker_id="Bob",
        port=8992,
        log_dir=tmp_path,
        callback_signer=None,
    )
    assert without.agent_card.capabilities.push_notifications is False


def test_registry_captures_push_capability_from_agent_card():
    from a2a.coordinator.agent_registry import AgentRegistry

    registry = AgentRegistry()
    registry.register_from_agent_card(
        worker_id="Alice",
        endpoint="http://localhost:8991",
        agent_card={
            "name": "Alice",
            "description": "worker",
            "capabilities": {"streaming": True, "pushNotifications": False},
            "skills": [],
        },
    )
    info = registry.get("Alice")
    assert info.push_notifications is False

    registry2 = AgentRegistry()
    registry2.register_from_agent_card(
        worker_id="Bob",
        endpoint="http://localhost:8992",
        agent_card={
            "name": "Bob",
            "description": "worker",
            "capabilities": {"streaming": True, "pushNotifications": True},
            "skills": [],
        },
    )
    assert registry2.get("Bob").push_notifications is True


def test_sar_worker_secure_mode_fails_closed_without_secret():
    """SARWorker refuses to start in shadow/read_port without a protected
    coordinator secret (typed memory_auth_not_configured)."""
    from a2a.coordinator.memory.callback_auth import MemoryAuthNotConfiguredError
    from sar_orch.worker import SARWorker

    with pytest.raises(MemoryAuthNotConfiguredError) as exc:
        SARWorker(
            worker_id="Alice",
            agent_name="Alice",
            agent_idx=0,
            barrier=None,
            memory_read_mode="shadow",
        )
    assert exc.value.code == "memory_auth_not_configured"


def test_sar_worker_secure_mode_accepts_valid_secret():
    from sar_orch.worker import SARWorker

    worker = SARWorker(
        worker_id="Alice",
        agent_name="Alice",
        agent_idx=0,
        barrier=None,
        log_dir=None,
        memory_read_mode="shadow",
        coordinator_secret=SECRET,
    )
    assert worker._memory_read_mode == "shadow"


def test_sar_coordinator_secure_mode_fails_closed_without_secret():
    from a2a.coordinator.memory.callback_auth import MemoryAuthNotConfiguredError
    from sar_orch.coordinator import SARCoordinator

    with pytest.raises(MemoryAuthNotConfiguredError) as exc:
        SARCoordinator(
            host="localhost",
            port=8080,
            a2a_port=8081,
            barrier=None,
            memory_read_mode="shadow",
        )
    assert exc.value.code == "memory_auth_not_configured"


def test_sar_coordinator_secure_mode_accepts_valid_secret():
    from sar_orch.coordinator import SARCoordinator

    coord = SARCoordinator(
        host="localhost",
        port=8080,
        a2a_port=8081,
        barrier=None,
        memory_read_mode="shadow",
        coordinator_secret=SECRET,
        log_dir="/tmp/opencode/sar-coord-test",
        run_id="run-1",
    )
    assert coord._memory_read_mode == "shadow"
