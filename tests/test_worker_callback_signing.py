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


# ---------------------------------------------------------------------------
# Startup dispatch-binding race: push callback before register_worker_task_id
# ---------------------------------------------------------------------------


class _ScriptedResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} rejected")


class _ScriptedClient:
    """Client that replays a scripted response sequence per POST call."""

    def __init__(self, script):
        self.requests: list = []
        self._script = list(script)

    async def post(self, url, content=None, headers=None, **kwargs):
        self.requests.append((url, content, headers))
        step = self._script.pop(0)
        return _ScriptedResponse(
            status=step.get("status", 200), payload=step.get("payload", {})
        )


def test_push_callback_retries_transient_unknown_worker_task_binding_race():
    """The first push callback races the coordinator's post-acceptance
    ``register_worker_task_id`` and is rejected as ``unknown_worker_task``.
    The sender retries (bounded) with a fresh nonce and an identical body and
    succeeds once the binding lands — never dropping the callback."""
    client = _ScriptedClient(
        [
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            },
            {"status": 200},
        ]
    )
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client,
        config_store=store,
        signer=signer,
        callback_retry_delay=0.0,
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 2
    bodies = [r[1] for r in client.requests]
    assert bodies[0] == bodies[1], "retry must send the identical body"
    proofs = [r[2][HEADER_PROOF] for r in client.requests]
    assert CallbackProofV1.nonce_of(proofs[0]) != CallbackProofV1.nonce_of(proofs[1]), (
        "retry must use a fresh nonce"
    )


def test_push_callback_genuine_rejection_is_not_retried():
    """A genuine authorization rejection (identity mismatch) is never retried —
    the sender fails closed after a single attempt."""
    client = _ScriptedClient(
        [
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "worker_mismatch"},
            },
            {"status": 200},
        ]
    )
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=signer
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 1
    assert client.requests[0][2][HEADER_WORKER_ID] == "Alice"


def test_push_callback_transient_rejection_gives_up_bounded():
    """A persistent transient rejection exhausts the bounded retry budget and
    fails closed — it never retries beyond the configured attempts."""
    client = _ScriptedClient(
        [
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            },
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            },
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            },
            {"status": 200},
        ]
    )
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client,
        config_store=store,
        signer=signer,
        callback_retry_attempts=3,
        callback_retry_delay=0.0,
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 3


def test_push_callback_survives_multi_second_delayed_registration(monkeypatch):
    """A registration that lands after a multi-second dispatch round-trip must
    NOT drop the callback: the exponential backoff keeps retrying (fresh nonce,
    identical body) until the coordinator's register_worker_task_id lands."""
    import asyncio as _asyncio

    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(float(delay))

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)

    # The coordinator rejects the callback for the first 5 attempts (a
    # multi-second registration window) before accepting on the 6th.
    client = _ScriptedClient(
        [
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            }
        ]
        * 5
        + [{"status": 200}]
    )
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client, config_store=store, signer=signer
    )

    _asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 6
    # Exponential backoff schedule 0.2 → 0.4 → 0.8 → 1.6 → 2.0: the retry
    # window spans ~5s, far beyond the original ~0.15s budget.
    assert sleeps == [0.2, 0.4, 0.8, 1.6, 2.0]
    bodies = [r[1] for r in client.requests]
    assert all(b == bodies[0] for b in bodies), "retries must keep the identical body"
    proofs = [r[2][HEADER_PROOF] for r in client.requests]
    nonces = [CallbackProofV1.nonce_of(p) for p in proofs]
    assert len(set(nonces)) == 6, "every retry must use a fresh nonce"


def test_push_callback_default_budget_stays_bounded_for_persistent_rejection():
    """A genuinely unbound/forged callback never retries forever: the default
    exponential budget is bounded (6 attempts) and the sender gives up."""
    client = _ScriptedClient(
        [
            {
                "status": 401,
                "payload": {"status": "rejected", "reason": "unknown_worker_task"},
            }
        ]
        * 10
    )
    signer = CallbackSigner("Alice", SECRET)
    store = _ConfigStore("http://coordinator/a2a/push-callback")
    sender = SignedPushNotificationSender(
        httpx_client=client,
        config_store=store,
        signer=signer,
        callback_retry_delay=0.0,
    )
    import asyncio

    asyncio.run(sender.send_notification("t-1", _event()))
    assert len(client.requests) == 6


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


# ---------------------------------------------------------------------------
# Phase 3 增补 —— AgentCard sensor metadata（只扩展 card fixture，不改签名契约）
# ---------------------------------------------------------------------------


def test_worker_card_sensors_generate_sensor_type_skill(tmp_path):
    """Phase 3：create_worker_a2a_server(sensors=[...]) 生成带
    sensor_type:<slug> metadata tags 的 AgentSkill（机制验证走 fixture；
    SAR 运行时不传 → 恒空）。"""
    from a2a.worker.a2a_server import create_worker_a2a_server

    server = create_worker_a2a_server(
        worker_id="Alice",
        port=8993,
        log_dir=tmp_path,
        sensors=["gps", "thermal"],
    )
    skill_tags = {tuple(sorted(s.tags)) for s in server.agent_card.skills}
    assert ("metadata", "sensor_type:gps", "sensor_type:thermal") in skill_tags


def test_worker_card_without_sensors_has_no_sensor_skill(tmp_path):
    """Phase 3：sensors 缺省/空 → 不生成 sensor metadata skill（SAR 恒空）。"""
    from a2a.worker.a2a_server import create_worker_a2a_server

    server = create_worker_a2a_server(
        worker_id="Bob",
        port=8994,
        log_dir=tmp_path,
    )
    for skill in server.agent_card.skills:
        assert not any(tag.startswith("sensor_type:") for tag in (skill.tags or []))
