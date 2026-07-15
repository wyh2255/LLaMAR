"""Tests for EnvelopeIngress — classification, auth, secret resolution, replay."""

from __future__ import annotations

import json

import pytest

from a2a.shared.message_envelope import (
    pack_message,
    MessageKind,
    AuthorizationContext,
)
from a2a.worker.ingress import EnvelopeIngress


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coord_secret() -> bytes:
    return b"coord-secret-32bytes!!"


@pytest.fixture
def peer_secret() -> bytes:
    return b"peer-secret-32bytes!!!!!"


class _FakeTeamState:
    """Minimal fake for WorkerTeamState used by ingress tests."""

    def __init__(self, secret: bytes | None, members: list[str] | None = None):
        self._secret = secret
        self._members = members or []

    def peer_secret_bytes(self) -> bytes | None:
        return self._secret

    def to_auth_context(self) -> AuthorizationContext | None:
        if self._secret is None:
            return None
        return AuthorizationContext(
            local_worker_id="worker-alpha",
            coordinator_id="Coordinator",
            current_team_id="sar-team-7",
            current_team_epoch=1,
            current_team_members=frozenset(self._members),
        )


@pytest.fixture
def no_team() -> _FakeTeamState:
    return _FakeTeamState(None)


@pytest.fixture
def with_team(peer_secret: bytes) -> _FakeTeamState:
    return _FakeTeamState(peer_secret, members=["worker-alpha", "worker-beta"])


@pytest.fixture
def coord_ingress(coord_secret: bytes, no_team: _FakeTeamState) -> EnvelopeIngress:
    return EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id="worker-alpha",
        team_state=no_team,
    )


@pytest.fixture
def peer_ingress(coord_secret: bytes, with_team: _FakeTeamState) -> EnvelopeIngress:
    return EnvelopeIngress(
        coordinator_secret=coord_secret,
        coordinator_id="Coordinator",
        local_worker_id="worker-alpha",
        team_state=with_team,
    )


# ---------------------------------------------------------------------------
# Legacy passthrough
# ---------------------------------------------------------------------------


class TestLegacy:
    def test_plain_text(self, coord_ingress: EnvelopeIngress) -> None:
        result = coord_ingress.classify("search grid sector 4")
        assert result.action == "legacy"
        assert result.body == {"content": "search grid sector 4"}
        assert result.envelope is None

    def test_non_envelope_json(self, coord_ingress: EnvelopeIngress) -> None:
        result = coord_ingress.classify('{"unrelated": true}')
        assert result.action == "legacy"
        assert result.envelope is None


# ---------------------------------------------------------------------------
# Coordinator messages
# ---------------------------------------------------------------------------


class TestCoordinator:
    def test_task(self, coord_ingress: EnvelopeIngress, coord_secret: bytes) -> None:
        packed = pack_message(
            "search sector 4",
            MessageKind.TASK,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "task"
        assert result.body.get("content") == "search sector 4"
        assert result.envelope is not None

    def test_mail(self, coord_ingress: EnvelopeIngress, coord_secret: bytes) -> None:
        packed = pack_message(
            "hello from coordinator",
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "mail"

    def test_team_update(
        self, coord_ingress: EnvelopeIngress, coord_secret: bytes
    ) -> None:
        packed = pack_message(
            json.dumps({"team_id": "new-team", "epoch": 1, "members": ["a", "b"]}),
            MessageKind.TEAM_UPDATE,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "team_update"

    def test_team_revoke(
        self, coord_ingress: EnvelopeIngress, coord_secret: bytes
    ) -> None:
        packed = pack_message(
            "",
            MessageKind.TEAM_REVOKE,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "team_revoke"


# ---------------------------------------------------------------------------
# Worker peer messages (require team)
# ---------------------------------------------------------------------------


class TestWorkerPeer:
    def test_mail_from_peer(
        self, peer_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "hello peer",
            MessageKind.MAIL,
            sender_id="worker-beta",
            recipient_id="worker-alpha",
            secret=peer_secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        result = peer_ingress.classify(packed)
        assert result.action == "mail"

    def test_peer_without_team_rejected(
        self, coord_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "hello",
            MessageKind.MAIL,
            sender_id="worker-beta",
            recipient_id="worker-alpha",
            secret=peer_secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "reject"

    def test_peer_cannot_send_task(
        self, peer_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "do something",
            MessageKind.TASK,
            sender_id="worker-beta",
            recipient_id="worker-alpha",
            secret=peer_secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        result = peer_ingress.classify(packed)
        assert result.action == "reject"

    def test_intruder_rejected(
        self, peer_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "malicious",
            MessageKind.MAIL,
            sender_id="intruder",
            recipient_id="worker-alpha",
            secret=peer_secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        result = peer_ingress.classify(packed)
        assert result.action == "reject"


# ---------------------------------------------------------------------------
# Secret resolution: does NOT trust sender_id alone
# ---------------------------------------------------------------------------


class TestSecretResolution:
    def test_different_secret_rejected(
        self, coord_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "hi",
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=peer_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "reject"

    def test_sender_id_spoof_rejected(
        self, peer_ingress: EnvelopeIngress, coord_secret: bytes
    ) -> None:
        packed = pack_message(
            "spoof",
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = peer_ingress.classify(packed)
        assert result.action == "mail"

    def test_spoof_sender_id_wrong_secret(
        self, coord_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        packed = pack_message(
            "spoof",
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=peer_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "reject"


# ---------------------------------------------------------------------------
# Authorization before idempotency (unauthorized does NOT consume ID)
# ---------------------------------------------------------------------------


class TestAuthBeforeIdempotency:
    def test_unauthorized_does_not_consume_id(
        self, peer_ingress: EnvelopeIngress, peer_secret: bytes
    ) -> None:
        """An envelope that verifies but fails authorization should not mark ID."""
        packed = pack_message(
            "unauthorized task",
            MessageKind.TASK,  # workers cannot send TASK
            sender_id="worker-beta",
            recipient_id="worker-alpha",
            secret=peer_secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        r1 = peer_ingress.classify(packed)
        assert r1.action == "reject"

        # Same message replayed — should still be rejected but NOT as replay
        r2 = peer_ingress.classify(packed)
        assert r2.action == "reject"
        # Should NOT say "duplicate" — the ID was not consumed
        assert "duplicate" not in (r2.reason or "").lower()

    def test_ok_then_replay_still_rejected(
        self, coord_ingress: EnvelopeIngress, coord_secret: bytes
    ) -> None:
        packed = pack_message(
            "once",
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        r1 = coord_ingress.classify(packed)
        assert r1.action == "mail"

        r2 = coord_ingress.classify(packed)
        assert r2.action == "reject"
        assert "duplicate" in (r2.reason or "").lower()


# ---------------------------------------------------------------------------
# Malformed / rejects
# ---------------------------------------------------------------------------


class TestReject:
    def test_malformed_json(self, coord_ingress: EnvelopeIngress) -> None:
        result = coord_ingress.classify('{"protocol":"a2a-peer" broken}')
        assert result.action == "legacy"

    def test_missing_signature(self, coord_ingress: EnvelopeIngress) -> None:
        raw = '{"protocol":"a2a-peer","kind":"mail","sender_id":"Coordinator","recipient_id":"worker-alpha","message_id":"m1","sent_at":"2026-07-15T12:00:00+00:00"}'
        result = coord_ingress.classify(raw)
        assert result.action == "reject"
        assert result.reason is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_text(self, coord_ingress: EnvelopeIngress) -> None:
        result = coord_ingress.classify("")
        assert result.action == "legacy"

    def test_very_long_body(
        self, coord_ingress: EnvelopeIngress, coord_secret: bytes
    ) -> None:
        packed = pack_message(
            "x" * 50000,
            MessageKind.MAIL,
            sender_id="Coordinator",
            recipient_id="worker-alpha",
            secret=coord_secret,
        )
        result = coord_ingress.classify(packed)
        assert result.action == "mail"
