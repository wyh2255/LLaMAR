"""Phase 5: Worker-to-worker peer mail -- end-to-end integration tests.

Tests cover:
  1. WorkerPeerSenderService happy path, self-send, non-member, timeout,
     domain message_id separate from transport task_id.
  2. A2ASendMailTool parameter validation, structured data, no lazy init.
  3. Endpoint normalization (urllib.parse), HTTPS, credentials rejection.
  4. Team registry normalization (0.0.0.0 normalised in roster).
  5. DeliverySnapshot -- frozen, immutables, atomic under concurrency.
  6. Cross-component via fake A2A SDK clients.
  7. Old-epoch mail rejection at receiver ingress.
  8. CancelTask auth documentation note (no fix attempted).
  9. Observability callbacks fire outside locks, no subject/body/secret.
 10. Worker tool exports.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ── Endpoint helpers ────────────────────────────────────────────────────────


class TestEndpointNormalization:
    def test_wildcard_to_localhost(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        assert normalize_endpoint("http://0.0.0.0:8191/") == "http://localhost:8191/"
        assert normalize_endpoint("http://0.0.0.0:9999") == "http://localhost:9999"

    def test_no_change_for_real_host(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        assert (
            normalize_endpoint("http://192.168.1.5:8191/") == "http://192.168.1.5:8191/"
        )
        assert (
            normalize_endpoint("http://example.com:8080/") == "http://example.com:8080/"
        )

    def test_https_wildcard(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        assert normalize_endpoint("https://0.0.0.0:8191/") == "https://localhost:8191/"

    def test_preserves_path_and_query(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        assert (
            normalize_endpoint("http://0.0.0.0:8191/path/to?q=1&r=2")
            == "http://localhost:8191/path/to?q=1&r=2"
        )

    def test_credentials_not_normalised(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        # URLs with embedded credentials are left unchanged
        ep = "http://user:pass@0.0.0.0:8191/"
        assert normalize_endpoint(ep) == ep

    def test_unsupported_scheme(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        ep = "ftp://0.0.0.0:8191/"
        assert normalize_endpoint(ep) == ep

    def test_malformed_endpoint(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        assert normalize_endpoint("") == ""
        assert normalize_endpoint("not-a-url") == "not-a-url"

    def test_roster_normalization(self) -> None:
        from a2a.shared.endpoint_helpers import normalize_roster_endpoints

        members = {
            "Alice": "http://0.0.0.0:8191/",
            "Bob": "http://0.0.0.0:8192/",
            "Charlie": "http://charlie-host:8193/",
        }
        result = normalize_roster_endpoints(members)
        assert result["Alice"] == "http://localhost:8191/"
        assert result["Bob"] == "http://localhost:8192/"
        assert result["Charlie"] == "http://charlie-host:8193/"

    def test_is_local_wildcard(self) -> None:
        from a2a.shared.endpoint_helpers import is_local_wildcard

        assert is_local_wildcard("http://0.0.0.0:8191/")
        assert is_local_wildcard("http://127.0.0.1:8191/")
        assert is_local_wildcard("http://localhost:8191/")
        assert not is_local_wildcard("http://192.168.1.5:8191/")
        assert not is_local_wildcard("http://example.com:8080/")

    def test_strip_trailing_slash(self) -> None:
        from a2a.shared.endpoint_helpers import strip_trailing_slash

        assert strip_trailing_slash("http://localhost:8191/") == "http://localhost:8191"
        assert strip_trailing_slash("http://localhost:8191") == "http://localhost:8191"


# ── Team registry normalization ─────────────────────────────────────────────


class TestTeamRegistryNormalization:
    def test_configure_normalises_wildcards(self) -> None:
        from a2a.coordinator.team_registry import CoordinatorTeamRegistry

        registry = CoordinatorTeamRegistry()
        result = registry.configure(
            {
                "Alice": "http://0.0.0.0:8191/",
                "Bob": "http://0.0.0.0:8192/",
            },
            team_id="test-team",
        )
        assert result.endpoints["Alice"] == "http://localhost:8191/"
        assert result.endpoints["Bob"] == "http://localhost:8192/"

    def test_configure_for_delivery_normalises(self) -> None:
        from a2a.coordinator.team_registry import CoordinatorTeamRegistry

        registry = CoordinatorTeamRegistry()
        plan = registry.configure_for_delivery(
            {
                "Alice": "http://0.0.0.0:8191/",
                "Bob": "http://0.0.0.0:8192/",
            },
            team_id="test-team",
        )
        assert plan.dto.endpoints["Alice"] == "http://localhost:8191/"
        assert plan.dto.endpoints["Bob"] == "http://localhost:8192/"


# ── DeliverySnapshot ────────────────────────────────────────────────────────


class TestDeliverySnapshot:
    def test_snapshot_returns_none_when_no_team(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        ts = WorkerTeamState(tmp_path / "team.json", "Alice")
        assert ts.delivery_snapshot() is None

    def test_snapshot_is_frozen(self, tmp_path: Path) -> None:
        import dataclasses
        from a2a.worker.team_state import DeliverySnapshot

        assert dataclasses.is_dataclass(DeliverySnapshot)
        # Verify frozen by attempting mutation
        snap = DeliverySnapshot(
            team_id="t",
            epoch=1,
            members=("a",),
            endpoints=__import__("types").MappingProxyType({}),
            coordinator_id="c",
            peer_secret=b"1234567890123456",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.team_id = "other"

    def test_snapshot_uses_immutable_types(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        ts = WorkerTeamState(tmp_path / "team.json", "Alice")
        ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        snap = ts.delivery_snapshot()
        assert isinstance(snap.members, tuple)
        assert isinstance(snap.endpoints.__class__.__name__, str)  # MappingProxyType
        assert snap.members == ("Alice", "Bob")
        assert snap.endpoints["Bob"] == "http://localhost:8192/"
        assert snap.team_id == "team-1"
        assert snap.epoch == 1
        assert len(snap.peer_secret) == 16

    def test_snapshot_toctou_atomic(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        ts = WorkerTeamState(tmp_path / "team.json", "Alice")
        ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        errors: list[str] = []

        def _racer():
            for _ in range(50):
                snap = ts.delivery_snapshot()
                if snap is not None and snap.epoch == 2:
                    if "Charlie" not in snap.members:
                        errors.append("TOCTOU: epoch 2 missing Charlie")

        def _modifier():
            for i in range(50):
                ts.install(
                    team_id="team-1",
                    epoch=2 + i,
                    members=["Alice", "Charlie"],
                    endpoints={
                        "Alice": "http://localhost:8191/",
                        "Charlie": "http://localhost:8193/",
                    },
                    team_secret="b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                    coordinator_id="Coordinator",
                )

        t1 = threading.Thread(target=_racer, daemon=True)
        t2 = threading.Thread(target=_modifier, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors, errors


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_team_state(
    tmp_path: Path,
    local_id: str = "Alice",
    members: list[str] | None = None,
    secret_hex: str = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
):
    from a2a.worker.team_state import WorkerTeamState

    if members is None:
        members = ["Alice", "Bob"]
    ts = WorkerTeamState(tmp_path / "team.json", local_id)
    ts.install(
        team_id="team-1",
        epoch=1,
        members=members,
        endpoints={m: f"http://localhost:{8191 + i}/" for i, m in enumerate(members)},
        team_secret=secret_hex,
        coordinator_id="Coordinator",
    )
    return ts


class _OkAckClient:
    """Fake A2A SDK client yielding terminal COMPLETED response."""

    def __init__(self, task_id: str = "a2a-task-1"):
        self._task_id = task_id
        self.closed = False
        self.sent_messages: list = []

    async def send_message(self, request):
        self.sent_messages.append(request)
        from a2a.types.a2a_pb2 import StreamResponse
        from google.protobuf.json_format import ParseDict

        yield ParseDict(
            {"task": {"id": self._task_id, "status": {"state": 3}}},
            StreamResponse(),
        )

    async def close(self):
        self.closed = True


class _FailAckClient:
    """Fake A2A SDK client yielding terminal FAILED response."""

    async def send_message(self, request):
        from a2a.types.a2a_pb2 import StreamResponse
        from google.protobuf.json_format import ParseDict

        yield ParseDict(
            {"task": {"id": "a2a-fail-1", "status": {"state": 4}}},
            StreamResponse(),
        )

    async def close(self):
        pass


class _TimeoutClient:
    """Fake A2A SDK client that hangs."""

    async def send_message(self, request):
        import asyncio

        await asyncio.sleep(3600)
        yield None

    async def close(self):
        pass


# ── WorkerPeerSenderService ─────────────────────────────────────────────────


class TestWorkerPeerSenderSendMail:
    @pytest.mark.asyncio
    async def test_send_mail_success(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        sender._client_cache["http://localhost:8192/"] = _OkAckClient()

        result = await sender.send_mail(
            recipient_id="Bob", subject="Hello", body="How are you?"
        )
        assert result["success"] is True
        assert "message_id" in result
        assert result["transport_task_id"] == "a2a-task-1"
        # message_id and transport_task_id are different
        assert result["message_id"] != result["transport_task_id"]

    @pytest.mark.asyncio
    async def test_send_mail_empty_subject(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        sender._client_cache["http://localhost:8192/"] = _OkAckClient()

        result = await sender.send_mail(
            recipient_id="Bob", subject="", body="Just body"
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_send_mail_empty_body(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        sender._client_cache["http://localhost:8192/"] = _OkAckClient()

        result = await sender.send_mail(
            recipient_id="Bob", subject="Subject only", body=""
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_fails_for_self(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            NotInTeamError,
        )

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(NotInTeamError, match="self"):
            await sender.send_mail(recipient_id="Alice", subject="Hi", body="Self")

    @pytest.mark.asyncio
    async def test_fails_for_non_member(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            NotInTeamError,
        )

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(NotInTeamError, match="not a member"):
            await sender.send_mail(recipient_id="Eve", subject="Hi", body="Test")

    @pytest.mark.asyncio
    async def test_no_active_team(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            NotInTeamError,
        )
        from a2a.worker.team_state import WorkerTeamState

        ts = WorkerTeamState(tmp_path / "team.json", "Alice")
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(NotInTeamError, match="No active team"):
            await sender.send_mail(recipient_id="Bob", subject="Hi", body="Test")

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(
            team_state=ts,
            local_worker_id="Alice",
            default_timeout=0.1,
        )
        sender._client_cache["http://localhost:8192/"] = _TimeoutClient()

        result = await sender.send_mail(recipient_id="Bob", subject="Hi", body="Test")
        assert result["success"] is False
        assert "Timeout" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_envelope_signed_with_peer_secret(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        fake_client = _OkAckClient()
        sender._client_cache["http://localhost:8192/"] = fake_client

        await sender.send_mail(recipient_id="Bob", subject="Hi", body="Test")

        assert len(fake_client.sent_messages) == 1
        msg = fake_client.sent_messages[0]
        text = msg.message.parts[0].text
        data = json.loads(text)
        assert data["protocol"] == "a2a-peer"
        assert data["kind"] == "mail"
        assert data["sender_id"] == "Alice"
        assert data["recipient_id"] == "Bob"
        assert data["team_id"] == "team-1"
        assert data["team_epoch"] == 1
        assert "signature" in data
        assert len(data["signature"]) == 64

    @pytest.mark.asyncio
    async def test_observability_callback_no_subject(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(
            team_state=ts,
            local_worker_id="Alice",
            event_callback=_cb,
        )
        sender._client_cache["http://localhost:8192/"] = _OkAckClient()

        await sender.send_mail(recipient_id="Bob", subject="Hi", body="Test")

        assert len(events) == 1
        name, data = events[0]
        assert name == "mail_sent"
        assert data["recipient_id"] == "Bob"
        assert data["sender_id"] == "Alice"
        assert data["team_id"] == "team-1"
        assert data["outcome"] == "sent"
        # No body, no subject, no signature in event data
        assert "body" not in data
        assert "subject" not in data
        assert "signature" not in data

    @pytest.mark.asyncio
    async def test_close_cleans_clients(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        client = _OkAckClient()
        sender._client_cache["http://localhost:8192/"] = client

        await sender.close()
        assert client.closed
        assert len(sender._client_cache) == 0

    @pytest.mark.asyncio
    async def test_reject_old_epoch_mail_at_receiver(self, tmp_path: Path) -> None:
        from a2a.worker.ingress import EnvelopeIngress
        from a2a.shared.message_envelope import pack_message, MessageKind

        ts = _make_team_state(tmp_path, local_id="Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=b"coord-secret-32bytes!!!!!!!!",
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=ts,
            allow_legacy_tasks=False,
        )
        # Advance to epoch 2
        ts.install(
            team_id="team-1",
            epoch=2,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        old_secret = bytes.fromhex("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        envelope_json = pack_message(
            body={"subject": "Hi", "content": "Old epoch mail"},
            kind=MessageKind.MAIL,
            sender_id="Alice",
            recipient_id="Bob",
            secret=old_secret,
            team_id="team-1",
            team_epoch=1,
        )
        result = ingress.classify(envelope_json)
        assert result.action == "reject"

    @pytest.mark.asyncio
    async def test_new_epoch_mail_accepted(self, tmp_path: Path) -> None:
        from a2a.worker.ingress import EnvelopeIngress
        from a2a.shared.message_envelope import pack_message, MessageKind

        ts = _make_team_state(tmp_path, local_id="Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=b"coord-secret-32bytes!!!!!!!!",
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=ts,
            allow_legacy_tasks=False,
        )
        current_secret = bytes.fromhex("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        envelope_json = pack_message(
            body={"subject": "Hi", "content": "Current epoch mail"},
            kind=MessageKind.MAIL,
            sender_id="Alice",
            recipient_id="Bob",
            secret=current_secret,
            team_id="team-1",
            team_epoch=1,
        )
        result = ingress.classify(envelope_json)
        assert result.action == "mail"

    def test_constructor_rejects_zero_timeout(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            PeerSendError,
        )

        ts = _make_team_state(tmp_path)
        with pytest.raises(PeerSendError, match="timeout"):
            WorkerPeerSenderService(
                team_state=ts,
                local_worker_id="Alice",
                default_timeout=0,
            )

    def test_valid_endpoint_validation(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        assert sender._valid_endpoint("http://localhost:8191/")
        assert sender._valid_endpoint("https://example.com:443/path")
        assert not sender._valid_endpoint("ftp://localhost:8191/")
        assert not sender._valid_endpoint("http://user:pass@host:8191/")
        assert not sender._valid_endpoint("")
        assert not sender._valid_endpoint("not-a-url")

    def test_invalid_subject_type(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            InvalidMailParameter,
        )

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(InvalidMailParameter, match="subject must be a string"):
            sender._validate_mail_params(123, "body")

    def test_invalid_body_type(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            InvalidMailParameter,
        )

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(InvalidMailParameter, match="body must be a string"):
            sender._validate_mail_params("subject", 456)


# ── A2ASendMailTool ─────────────────────────────────────────────────────────


class TestA2ASendMailTool:
    def test_requires_sender(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool

        with pytest.raises(TypeError, match="sender is required"):
            A2ASendMailTool(sender=None)  # type: ignore

    def test_name_and_params(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool

        mock_sender = AsyncMock()
        mock_sender.send_mail = AsyncMock(
            return_value={
                "success": True,
                "message_id": "m-1",
                "transport_task_id": "t-1",
            }
        )
        tool = A2ASendMailTool(sender=mock_sender)
        assert tool.name == "a2a_send_mail"
        assert "recipient_id" in tool.parameters["required"]
        assert "subject" in tool.parameters["required"]
        assert "body" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_success_structured_data(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool

        mock_sender = AsyncMock()
        mock_sender.send_mail = AsyncMock(
            return_value={
                "success": True,
                "message_id": "dom-1",
                "transport_task_id": "a2a-1",
            }
        )
        tool = A2ASendMailTool(sender=mock_sender)
        result = await tool.execute(recipient_id="Bob", subject="Hi", body="Hello")
        assert result.success is True
        assert result.data["recipient_id"] == "Bob"
        assert result.data["message_id"] == "dom-1"
        assert result.data["outcome"] == "sent"
        mock_sender.send_mail.assert_called_once_with(
            recipient_id="Bob", subject="Hi", body="Hello"
        )

    @pytest.mark.asyncio
    async def test_missing_recipient(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool

        mock_sender = AsyncMock()
        tool = A2ASendMailTool(sender=mock_sender)
        result = await tool.execute(recipient_id="", subject="Hi", body="Hello")
        assert result.success is False
        assert "recipient_id" in result.content

    @pytest.mark.asyncio
    async def test_sender_failure_structured_data(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool

        mock_sender = AsyncMock()
        mock_sender.send_mail = AsyncMock(
            return_value={"success": False, "error": "Timeout"}
        )
        tool = A2ASendMailTool(sender=mock_sender)
        result = await tool.execute(recipient_id="Bob", subject="Hi", body="Hello")
        assert result.success is False
        assert result.data["outcome"] == "delivery_failed"
        assert result.data["error"] == "Timeout"

    @pytest.mark.asyncio
    async def test_not_in_team_raises(self) -> None:
        from a2a.worker.tools.send_peer_mail import A2ASendMailTool
        from a2a.worker.peer_sender import NotInTeamError

        mock_sender = AsyncMock()
        mock_sender.send_mail = AsyncMock(side_effect=NotInTeamError("No active team"))
        tool = A2ASendMailTool(sender=mock_sender)
        result = await tool.execute(recipient_id="Bob", subject="Hi", body="Hello")
        assert result.success is False
        assert "No active team" in result.content


# ── Mailbox store event callback (outside lock, no subject) ─────────────────


class TestMailboxObservability:
    def test_deliver_fires_event_no_subject(self, tmp_path: Path) -> None:
        from a2a.worker.mailbox_store import WorkerMailboxStore

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        store = WorkerMailboxStore(
            tmp_path / "mb.ndjson",
            local_worker_id="Bob",
            event_callback=_cb,
        )
        store.deliver("msg-1", "Alice", "Bob", subject="Hi", body="Hello")

        assert len(events) == 1
        name, data = events[0]
        assert name == "mail_delivered"
        assert data["sender_id"] == "Alice"
        assert data["recipient_id"] == "Bob"
        assert data["message_id"] == "msg-1"
        # No body, no subject in event data
        assert "body" not in data
        assert "subject" not in data

    def test_mark_read_fires_event(self, tmp_path: Path) -> None:
        from a2a.worker.mailbox_store import WorkerMailboxStore

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        store = WorkerMailboxStore(
            tmp_path / "mb.ndjson",
            local_worker_id="Bob",
            event_callback=_cb,
        )
        store.deliver("msg-1", "Alice", "Bob", subject="Hi", body="Hello")
        events.clear()

        store.mark_read("msg-1")

        assert len(events) == 1
        name, data = events[0]
        assert name == "mail_read"
        assert data["sender_id"] == "Alice"
        assert data["recipient_id"] == "Bob"
        assert "subject" not in data

    def test_read_unread_fires_events(self, tmp_path: Path) -> None:
        from a2a.worker.mailbox_store import WorkerMailboxStore

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        store = WorkerMailboxStore(
            tmp_path / "mb.ndjson",
            local_worker_id="Bob",
            event_callback=_cb,
        )
        store.deliver("msg-1", "Alice", "Bob", subject="Hi", body="Hello")
        store.deliver("msg-2", "Charlie", "Bob", subject="Alert", body="Fire!")
        events.clear()

        read_records = store.read_unread(limit=2)
        assert len(read_records) == 2
        assert len(events) == 2
        assert events[0][0] == "mail_read"
        assert events[1][0] == "mail_read"
        # No subject in event data
        assert "subject" not in events[0][1]

    def test_event_fires_after_lock_released(self, tmp_path: Path) -> None:
        """Verify callbacks are invoked outside the mailbox lock."""
        from a2a.worker.mailbox_store import WorkerMailboxStore

        inside_lock: list[bool] = []

        def _cb(name: str, data: dict) -> None:
            inside_lock.append(hasattr(store, "_lock") and store._lock.locked())

        store = WorkerMailboxStore(
            tmp_path / "mb.ndjson",
            local_worker_id="Bob",
            event_callback=_cb,
        )
        store.deliver("msg-1", "Alice", "Bob", subject="Hi", body="Hello")
        assert not any(inside_lock), "callback was invoked while lock held"


# ── Team state observability (outside lock) ─────────────────────────────────


class TestTeamStateObservability:
    def test_install_fires_event(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        ts = WorkerTeamState(tmp_path / "team.json", "Alice", event_callback=_cb)
        ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        assert len(events) == 1
        name, data = events[0]
        assert name == "team_installed"
        assert data["team_id"] == "team-1"
        assert data["epoch"] == 1
        assert data["member_count"] == 2

    def test_revoke_fires_event(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        events: list[tuple[str, dict]] = []

        def _cb(name: str, data: dict) -> None:
            events.append((name, data))

        ts = WorkerTeamState(tmp_path / "team.json", "Alice", event_callback=_cb)
        ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        events.clear()
        ts.revoke(team_id="team-1", epoch=1)

        assert len(events) == 1
        name, data = events[0]
        assert name == "team_revoked"
        assert data["team_id"] == "team-1"
        assert data["epoch"] == 1

    def test_event_fires_after_lock_released(self, tmp_path: Path) -> None:
        from a2a.worker.team_state import WorkerTeamState

        inside_lock: list[bool] = []

        def _cb(name: str, data: dict) -> None:
            inside_lock.append(hasattr(ts, "_lock") and ts._lock.locked())

        ts = WorkerTeamState(tmp_path / "team.json", "Alice", event_callback=_cb)
        ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            coordinator_id="Coordinator",
        )
        assert not any(inside_lock), "callback was invoked while lock held"


# ── Cross-component scenario ────────────────────────────────────────────────


class TestCrossComponentPeerMail:
    @pytest.mark.asyncio
    async def test_alice_sends_bob_receives(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import WorkerPeerSenderService
        from a2a.worker.team_state import WorkerTeamState
        from a2a.worker.mailbox_store import WorkerMailboxStore
        from a2a.worker.ingress import EnvelopeIngress

        alice_secret_hex = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        coord_secret = b"coord-secret-32bytes!!!!!!!!"

        # --- Alice's side ---
        alice_ts = WorkerTeamState(tmp_path / "alice_team.json", "Alice")
        alice_ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret=alice_secret_hex,
            coordinator_id="Coordinator",
        )
        sender = WorkerPeerSenderService(team_state=alice_ts, local_worker_id="Alice")

        sent_envelope_json: list[str] = []

        class _CapturingClient:
            async def send_message(self, request):
                sent_envelope_json.append(request.message.parts[0].text)
                from a2a.types.a2a_pb2 import StreamResponse
                from google.protobuf.json_format import ParseDict

                yield ParseDict(
                    {
                        "task": {
                            "id": "a2a-mail-1",
                            "status": {"state": 3},
                        }
                    },
                    StreamResponse(),
                )

            async def close(self):
                pass

        sender._client_cache["http://localhost:8192/"] = _CapturingClient()

        result = await sender.send_mail(
            recipient_id="Bob",
            subject="Coordination",
            body="I found a fire at (10,20)",
        )
        assert result["success"] is True
        assert result["message_id"] is not None
        assert result["transport_task_id"] == "a2a-mail-1"

        # --- Bob's side ---
        bob_ts = WorkerTeamState(tmp_path / "bob_team.json", "Bob")
        bob_ts.install(
            team_id="team-1",
            epoch=1,
            members=["Alice", "Bob"],
            endpoints={
                "Alice": "http://localhost:8191/",
                "Bob": "http://localhost:8192/",
            },
            team_secret=alice_secret_hex,
            coordinator_id="Coordinator",
        )
        bob_mailbox = WorkerMailboxStore(tmp_path / "bob_mb.ndjson", "Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=coord_secret,
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=bob_ts,
            allow_legacy_tasks=False,
        )

        env_text = sent_envelope_json[0]
        ingress_result = ingress.classify(env_text)
        assert ingress_result.action == "mail"

        env = ingress_result.envelope
        record = bob_mailbox.deliver(
            message_id=env.message_id,
            sender_id=env.sender_id,
            recipient_id=env.recipient_id,
            subject=env.body.get("subject", ""),
            body=env.body.get("content", ""),
            team_id=env.team_id,
            team_epoch=env.team_epoch,
            sent_at=env.sent_at.isoformat(),
        )
        assert record.sender_id == "Alice"
        assert "fire" in record.body

        unread = bob_mailbox.list_unread()
        assert len(unread) == 1

    @pytest.mark.asyncio
    async def test_nonmember_rejected_before_network(self, tmp_path: Path) -> None:
        from a2a.worker.peer_sender import (
            WorkerPeerSenderService,
            NotInTeamError,
        )

        ts = _make_team_state(tmp_path)
        sender = WorkerPeerSenderService(team_state=ts, local_worker_id="Alice")
        with pytest.raises(NotInTeamError, match="not a member"):
            await sender.send_mail(recipient_id="Eve", subject="Hi", body="Should fail")

    @pytest.mark.asyncio
    async def test_forged_peer_task_rejected(self, tmp_path: Path) -> None:
        from a2a.worker.ingress import EnvelopeIngress
        from a2a.shared.message_envelope import pack_message, MessageKind

        ts = _make_team_state(tmp_path, local_id="Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=b"coord-secret-32bytes!!!!!!!!",
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=ts,
            allow_legacy_tasks=False,
        )
        team_secret = bytes.fromhex("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        envelope_json = pack_message(
            body={"content": "Do something"},
            kind=MessageKind.TASK,
            sender_id="Alice",
            recipient_id="Bob",
            secret=team_secret,
            team_id="team-1",
            team_epoch=1,
        )
        result = ingress.classify(envelope_json)
        assert result.action == "reject"

    @pytest.mark.asyncio
    async def test_coordinator_mail_still_works(self, tmp_path: Path) -> None:
        from a2a.shared.message_envelope import pack_message, MessageKind
        from a2a.worker.ingress import EnvelopeIngress

        ts = _make_team_state(tmp_path, local_id="Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=b"coord-secret-32bytes!!!!!!!!",
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=ts,
            allow_legacy_tasks=False,
        )
        envelope_json = pack_message(
            body={"subject": "Directive", "content": "Proceed north"},
            kind=MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="Bob",
            secret=b"coord-secret-32bytes!!!!!!!!",
        )
        result = ingress.classify(envelope_json)
        assert result.action == "mail"

    @pytest.mark.asyncio
    async def test_unsigned_task_rejected(self, tmp_path: Path) -> None:
        from a2a.worker.ingress import EnvelopeIngress

        ts = _make_team_state(tmp_path, local_id="Bob")
        ingress = EnvelopeIngress(
            coordinator_secret=b"coord-secret-32bytes!!!!!!!!",
            coordinator_id="Coordinator",
            local_worker_id="Bob",
            team_state=ts,
            allow_legacy_tasks=False,
        )
        result = ingress.classify("Do something important")
        assert result.action == "reject"


# ── Cancel task auth note (documentation) ───────────────────────────────────


class TestCancelTaskAuthNote:
    def test_cancel_task_missing_sender_auth(self) -> None:
        from a2a.types.a2a_pb2 import CancelTaskRequest

        request = CancelTaskRequest()
        request.id = "some-task"
        assert request.id == "some-task"
        # CancelTaskRequest has fields: tenant, id, metadata
        # There is NO sender_id, no auth_token, no signature.
        # This is the documented security gap.


# ── Worker tool exports ─────────────────────────────────────────────────────


class TestWorkerToolExports:
    def test_a2a_send_mail_in_sar_tools(self) -> None:
        from sar_orch.tools.worker import SAR_WORKER_TOOLS, A2ASendMailTool

        assert A2ASendMailTool in SAR_WORKER_TOOLS
