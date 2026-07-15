"""Tests for message_envelope — envelope protocol, HMAC, replay guard, authz, classifiers."""

from __future__ import annotations

import json
import time
import threading
from datetime import datetime, timezone, timedelta

import pytest

from a2a.shared.message_envelope import (
    PROTOCOL,
    VERSION,
    MessageKind,
    MessageEnvelope,
    AuthorizationContext,
    sign_envelope,
    verify_envelope,
    IdempotencyGuard,
    EnvelopeExpiredError,
    SignatureInvalidError,
    MalformedEnvelopeError,
    ReplayError,
    PeerRole,
    AuthzDeniedError,
    authorize_envelope,
    is_envelope_payload,
    pack_message,
    unpack_message,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret() -> bytes:
    return b"super-secret-key-32bytes!"


@pytest.fixture
def auth_ctx() -> AuthorizationContext:
    return AuthorizationContext(
        local_worker_id="worker-alpha",
        coordinator_id="coordinator-1",
        current_team_id="sar-team-7",
        current_team_epoch=1,
        current_team_members=frozenset({"worker-alpha", "worker-beta", "worker-gamma"}),
    )


@pytest.fixture
def envelope(secret: bytes) -> MessageEnvelope:
    e = MessageEnvelope(
        kind=MessageKind.TASK,
        sender_id="coordinator-1",
        recipient_id="worker-alpha",
        team_id="sar-team-7",
        team_epoch=1,
        body={"content": "search grid sector 4"},
    )
    return sign_envelope(e, secret)


# ---------------------------------------------------------------------------
# MessageEnvelope basics
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    e = MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="b")
    assert e.protocol == PROTOCOL
    assert e.version == VERSION
    assert e.message_id is not None
    assert e.sent_at is not None
    assert e.sent_at.tzinfo is not None
    assert e.signature is None
    assert e.body == {}


def test_protocol_validation() -> None:
    with pytest.raises(ValueError, match="Unsupported protocol"):
        MessageEnvelope(
            protocol="bad", kind=MessageKind.MAIL, sender_id="a", recipient_id="b"
        )


# ---------------------------------------------------------------------------
# Field validation (version, nonblank, team coherence, expires_at)
# ---------------------------------------------------------------------------


def test_version_mismatch() -> None:
    with pytest.raises(ValueError, match="Unsupported version"):
        MessageEnvelope(
            version="0.9", kind=MessageKind.MAIL, sender_id="a", recipient_id="b"
        )


def test_blank_sender_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        MessageEnvelope(kind=MessageKind.MAIL, sender_id="", recipient_id="b")


def test_blank_recipient_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="   ")


def test_blank_message_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        MessageEnvelope(
            kind=MessageKind.MAIL, sender_id="a", recipient_id="b", message_id=""
        )


def test_blank_team_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        MessageEnvelope(
            kind=MessageKind.MAIL, sender_id="a", recipient_id="b", team_id="   "
        )


def test_negative_team_epoch_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        MessageEnvelope(
            kind=MessageKind.MAIL,
            sender_id="a",
            recipient_id="b",
            team_id="t",
            team_epoch=-1,
        )


def test_team_epoch_without_team_id_rejected() -> None:
    with pytest.raises(ValueError, match="team_epoch requires team_id"):
        MessageEnvelope(
            kind=MessageKind.MAIL,
            sender_id="a",
            recipient_id="b",
            team_epoch=1,
        )


def test_team_id_without_epoch_allowed() -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL, sender_id="a", recipient_id="b", team_id="t"
    )
    assert e.team_id == "t"
    assert e.team_epoch is None


def test_expires_at_before_sent_at_rejected() -> None:
    with pytest.raises(ValueError, match="expires_at must be >= sent_at"):
        MessageEnvelope(
            kind=MessageKind.MAIL,
            sender_id="a",
            recipient_id="b",
            sent_at=datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc),
        )


def test_expires_at_equal_to_sent_at_allowed() -> None:
    t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=t,
        expires_at=t,
    )
    assert e.expires_at == e.sent_at


def test_expires_at_none_allowed() -> None:
    e = MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="b")
    assert e.expires_at is None


# ---------------------------------------------------------------------------
# UTC normalization in canonical serialisation
# ---------------------------------------------------------------------------


def test_canonical_normalizes_utc() -> None:
    aware_utc = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    offset_p5 = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime(2026, 7, 15, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))),
    )
    assert (
        aware_utc.model_dump_canonical()["sent_at"]
        == offset_p5.model_dump_canonical()["sent_at"]
    )
    assert aware_utc.model_dump_canonical()["sent_at"].endswith("+00:00")


def test_canonical_naive_treated_as_utc() -> None:
    naive = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime(2026, 7, 15, 12, 0, 0),
    )
    result = naive.model_dump_canonical()
    assert result["sent_at"].endswith("+00:00")
    assert "2026-07-15T12:00:00" in result["sent_at"]


def test_verify_rejects_naive_datetime(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime(2026, 7, 15, 12, 0, 0),
    )
    sign_envelope(e, secret)
    with pytest.raises(MalformedEnvelopeError, match="timezone-aware"):
        verify_envelope(e, secret)


def test_verify_rejects_naive_expires_at(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
    )
    # set naive expires_at directly to bypass model validation
    e.expires_at = datetime(2026, 7, 15, 12, 0, 0)
    sign_envelope(e, secret)
    with pytest.raises(MalformedEnvelopeError, match="timezone-aware"):
        verify_envelope(e, secret)


def test_offset_equivalent_signatures(secret: bytes) -> None:
    same = dict(
        kind=MessageKind.MAIL, sender_id="a", recipient_id="b", message_id="fixed-id"
    )
    utc_e = MessageEnvelope(
        **same,
        sent_at=datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    offset_e = MessageEnvelope(
        **same,
        sent_at=datetime(2026, 7, 15, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))),
    )
    sign_envelope(utc_e, b"key")
    sign_envelope(offset_e, b"key")
    assert utc_e.signature == offset_e.signature


def test_model_dump_canonical(envelope: MessageEnvelope) -> None:
    data = envelope.model_dump_canonical()
    assert data["protocol"] == PROTOCOL
    assert data["kind"] == "task"
    assert data["sender_id"] == "coordinator-1"
    assert data["recipient_id"] == "worker-alpha"
    assert "sent_at" in data
    assert "signature" not in data


# ---------------------------------------------------------------------------
# HMAC sign / verify
# ---------------------------------------------------------------------------


def test_sign_and_verify(envelope: MessageEnvelope, secret: bytes) -> None:
    verify_envelope(envelope, secret)
    assert envelope.signature is not None


def test_verify_missing_signature(secret: bytes) -> None:
    e = MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="b")
    with pytest.raises(MalformedEnvelopeError, match="Missing signature"):
        verify_envelope(e, secret)


def test_verify_wrong_length_signature(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        signature="tooshort",
    )
    with pytest.raises(MalformedEnvelopeError, match="exactly 64 hex characters"):
        verify_envelope(e, secret)


def test_verify_non_hex_signature(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        signature="x" * 64,
    )
    with pytest.raises(MalformedEnvelopeError, match="valid hex string"):
        verify_envelope(e, secret)


def test_verify_tampered_body(envelope: MessageEnvelope, secret: bytes) -> None:
    envelope.body["content"] = "tampered"
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(envelope, secret)


def test_verify_tampered_kind(envelope: MessageEnvelope, secret: bytes) -> None:
    envelope.kind = MessageKind.MAIL
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(envelope, secret)


def test_verify_tampered_sender(envelope: MessageEnvelope, secret: bytes) -> None:
    envelope.sender_id = "impostor"
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(envelope, secret)


def test_verify_wrong_secret(envelope: MessageEnvelope) -> None:
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(envelope, b"wrong-secret")


def test_sign_empty_secret_raises(secret: bytes) -> None:
    e = MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="b")
    with pytest.raises(ValueError, match="non-empty"):
        sign_envelope(e, b"")


def test_verify_empty_secret_raises(envelope: MessageEnvelope) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        verify_envelope(envelope, b"")


def test_verify_negative_skew_raises(envelope: MessageEnvelope, secret: bytes) -> None:
    with pytest.raises(ValueError, match="max_skew_seconds must be >= 0"):
        verify_envelope(envelope, secret, max_skew_seconds=-1)


# ---------------------------------------------------------------------------
# HMAC verified BEFORE temporal checks
# ---------------------------------------------------------------------------


def test_tampered_sent_at_raises_signature_not_expiry(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    sign_envelope(e, secret)
    e.sent_at = datetime.now(timezone.utc) + timedelta(days=30)
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(e, secret, max_skew_seconds=300)


def test_tampered_expires_at_raises_signature_not_expiry(secret: bytes) -> None:
    e = MessageEnvelope(kind=MessageKind.MAIL, sender_id="a", recipient_id="b")
    sign_envelope(e, secret)
    e.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(SignatureInvalidError, match="HMAC signature mismatch"):
        verify_envelope(e, secret)


# ---------------------------------------------------------------------------
# Temporal checks
# ---------------------------------------------------------------------------


def test_verify_expired(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )
    sign_envelope(e, secret)
    with pytest.raises(EnvelopeExpiredError, match="skew"):
        verify_envelope(e, secret, max_skew_seconds=300)


def test_verify_future_skew(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime.now(timezone.utc) + timedelta(seconds=600),
    )
    sign_envelope(e, secret)
    with pytest.raises(EnvelopeExpiredError, match="skew"):
        verify_envelope(e, secret, max_skew_seconds=300)


def test_verify_expires_at(secret: bytes) -> None:
    sent = datetime.now(timezone.utc) - timedelta(minutes=2)
    expired = datetime.now(timezone.utc) - timedelta(seconds=10)
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=sent,
        expires_at=expired,
    )
    sign_envelope(e, secret)
    with pytest.raises(EnvelopeExpiredError, match="expired"):
        verify_envelope(e, secret, max_skew_seconds=3600)


def test_verify_within_skew_passes(secret: bytes) -> None:
    e = MessageEnvelope(
        kind=MessageKind.MAIL,
        sender_id="a",
        recipient_id="b",
        sent_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    sign_envelope(e, secret)
    verify_envelope(e, secret, max_skew_seconds=300)


# ---------------------------------------------------------------------------
# IdempotencyGuard
# ---------------------------------------------------------------------------


class TestIdempotencyGuard:
    def test_mark_and_check(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        g.check_and_mark("msg-1")
        with pytest.raises(ReplayError, match="Duplicate message_id"):
            g.check_and_mark("msg-1")

    def test_is_seen(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        g.check_and_mark("msg-1")
        assert g.is_seen("msg-1")
        assert not g.is_seen("msg-2")

    def test_forget(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        g.check_and_mark("msg-1")
        g.forget("msg-1")
        assert not g.is_seen("msg-1")
        g.check_and_mark("msg-1")

    def test_clear(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        g.check_and_mark("msg-1")
        g.clear()
        assert g.size() == 0
        g.check_and_mark("msg-1")

    def test_bounded_eviction_oldest(self) -> None:
        g = IdempotencyGuard(max_size=10, ttl_seconds=3600)
        for i in range(10):
            g.check_and_mark(f"msg-{i}")
        assert g.size() == 10
        g.check_and_mark("msg-new")
        assert g.size() <= 10
        assert g.size() < 10
        assert g.is_seen("msg-new")

    def test_capacity_never_exceeds_max_size(self) -> None:
        g = IdempotencyGuard(max_size=50, ttl_seconds=3600)
        for i in range(200):
            g.check_and_mark(f"msg-{i}")
        assert g.size() <= 50

    def test_ttl_expiry(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=0.1)
        g.check_and_mark("msg-1")
        assert g.is_seen("msg-1")
        time.sleep(0.15)
        assert not g.is_seen("msg-1")

    def test_concurrent_safety_distinct_ids(self) -> None:
        g = IdempotencyGuard(max_size=1000, ttl_seconds=3600)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            for i in range(100):
                try:
                    g.check_and_mark(f"t{n}-msg-{i}")
                except ReplayError:
                    pass
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert g.size() > 0

    def test_concurrent_same_id_exactly_one_succeeds(self) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        results: list[bool] = []
        rlock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            try:
                g.check_and_mark("same-id")
                with rlock:
                    results.append(True)
            except ReplayError:
                with rlock:
                    results.append(False)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 1

    def test_rejects_zero_max_size(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            IdempotencyGuard(max_size=0)

    def test_rejects_negative_max_size(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            IdempotencyGuard(max_size=-1)

    def test_rejects_zero_ttl(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            IdempotencyGuard(max_size=10, ttl_seconds=0)

    def test_rejects_negative_ttl(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            IdempotencyGuard(max_size=10, ttl_seconds=-1.0)


# ---------------------------------------------------------------------------
# Authorisation matrix — expanded with AuthorizationContext
# ---------------------------------------------------------------------------


class TestAuthorizeEnvelope:
    def test_coordinator_can_send_task(
        self, envelope: MessageEnvelope, auth_ctx: AuthorizationContext
    ) -> None:
        authorize_envelope(envelope, PeerRole.COORDINATOR, auth_ctx)

    def test_coordinator_wrong_sender_id_rejected(
        self, envelope: MessageEnvelope, auth_ctx: AuthorizationContext
    ) -> None:
        auth_wrong = AuthorizationContext(
            local_worker_id=auth_ctx.local_worker_id,
            coordinator_id="different-coordinator",
        )
        with pytest.raises(AuthzDeniedError, match="Coordinator sender_id"):
            authorize_envelope(envelope, PeerRole.COORDINATOR, auth_wrong)

    def test_recipient_mismatch_rejected(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.TASK,
                sender_id="coordinator-1",
                recipient_id="other-worker",
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="recipient"):
            authorize_envelope(e, PeerRole.COORDINATOR, auth_ctx)

    def test_worker_can_send_mail_in_team(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
                team_id="sar-team-7",
                team_epoch=1,
            ),
            secret,
        )
        authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_worker_cannot_send_task(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.TASK,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="not allowed"):
            authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_worker_cannot_send_to_self(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="worker-alpha",
                recipient_id="worker-alpha",
                team_id="sar-team-7",
                team_epoch=1,
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="cannot send"):
            authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_worker_no_active_team_rejected(self, secret: bytes) -> None:
        ctx = AuthorizationContext(local_worker_id="worker-alpha")
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="No active team"):
            authorize_envelope(e, PeerRole.WORKER, ctx)

    def test_worker_team_id_mismatch_rejected(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
                team_id="wrong-team",
                team_epoch=1,
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="team"):
            authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_worker_team_epoch_mismatch_rejected(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
                team_id="sar-team-7",
                team_epoch=99,
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="team"):
            authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_worker_not_in_team_rejected(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        e = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="intruder",
                recipient_id="worker-alpha",
                team_id="sar-team-7",
                team_epoch=1,
            ),
            secret,
        )
        with pytest.raises(AuthzDeniedError, match="not a member"):
            authorize_envelope(e, PeerRole.WORKER, auth_ctx)

    def test_unknown_role_rejected(
        self, envelope: MessageEnvelope, auth_ctx: AuthorizationContext
    ) -> None:
        with pytest.raises(AuthzDeniedError, match="Unknown peer role"):
            authorize_envelope(envelope, PeerRole.WORKER, auth_ctx, rules={})

    def test_custom_rules(self, auth_ctx: AuthorizationContext) -> None:
        custom = {PeerRole.WORKER: set(MessageKind)}
        e2 = sign_envelope(
            MessageEnvelope(
                kind=MessageKind.TASK,
                sender_id="worker-beta",
                recipient_id="worker-alpha",
                team_id="sar-team-7",
                team_epoch=1,
            ),
            b"secret",
        )
        authorize_envelope(e2, PeerRole.WORKER, auth_ctx, rules=custom)

    def test_no_system_role_enum(self) -> None:
        assert not hasattr(PeerRole, "SYSTEM")

    def test_auth_context_frozen_style(self) -> None:
        ctx = AuthorizationContext(local_worker_id="x")
        assert ctx.local_worker_id == "x"
        assert ctx.coordinator_id == "Coordinator"
        assert ctx.current_team_id is None
        assert ctx.current_team_epoch is None
        assert isinstance(ctx.current_team_members, frozenset)


# ---------------------------------------------------------------------------
# Downgrade prevention
# ---------------------------------------------------------------------------


class TestIsEnvelopePayload:
    def test_valid_envelope_json(self) -> None:
        assert is_envelope_payload(
            '{"protocol":"a2a-peer","kind":"task","sender_id":"c","recipient_id":"w"}'
        )

    def test_missing_kind_still_envelope_candidate(self) -> None:
        assert is_envelope_payload('{"protocol":"a2a-peer","sender_id":"c"}')

    def test_missing_sender_id_still_envelope_candidate(self) -> None:
        assert is_envelope_payload('{"protocol":"a2a-peer","kind":"task"}')

    def test_legacy_text(self) -> None:
        assert not is_envelope_payload("search grid sector 4")

    def test_empty_string(self) -> None:
        assert not is_envelope_payload("")

    def test_not_dict_json(self) -> None:
        assert not is_envelope_payload('["list", "of", "things"]')

    def test_missing_protocol_field(self) -> None:
        assert not is_envelope_payload('{"kind":"task","sender_id":"c"}')

    def test_wrong_protocol(self) -> None:
        assert not is_envelope_payload(
            '{"protocol":"other","kind":"task","sender_id":"c"}'
        )


class TestUnpackDowngradePrevention:
    def test_incomplete_envelope_raises(self, secret: bytes) -> None:
        with pytest.raises(MalformedEnvelopeError):
            unpack_message('{"protocol":"a2a-peer","sender_id":"c"}', secret)

    def test_wrong_version_raises(self, secret: bytes) -> None:
        with pytest.raises(MalformedEnvelopeError, match="version"):
            unpack_message(
                '{"protocol":"a2a-peer","version":"2.0","kind":"task","sender_id":"c","recipient_id":"w"}',
                secret,
            )

    def test_blank_sender_id_raises(self, secret: bytes) -> None:
        with pytest.raises(MalformedEnvelopeError, match="non-blank"):
            unpack_message(
                '{"protocol":"a2a-peer","kind":"task","sender_id":"","recipient_id":"w"}',
                secret,
            )

    def test_legacy_plain_text_passes_through(self, secret: bytes) -> None:
        body, env = unpack_message("plain hello", secret)
        assert body == {"content": "plain hello"}
        assert env is None

    def test_unrelated_json_passes_through(self, secret: bytes) -> None:
        body, env = unpack_message('{"unrelated": true}', secret)
        assert body == {"content": '{"unrelated": true}'}
        assert env is None


# ---------------------------------------------------------------------------
# pack_message / unpack_message
# ---------------------------------------------------------------------------


class TestPackUnpack:
    def test_roundtrip(self, secret: bytes) -> None:
        body = "search grid sector 4"
        packed = pack_message(
            body,
            MessageKind.TASK,
            sender_id="coordinator-1",
            recipient_id="worker-alpha",
            secret=secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        assert isinstance(packed, str)
        assert is_envelope_payload(packed)

        unpacked_body, envelope = unpack_message(packed, secret)
        assert unpacked_body == {"content": body}
        assert envelope is not None
        assert envelope.kind == MessageKind.TASK
        assert envelope.sender_id == "coordinator-1"
        assert envelope.recipient_id == "worker-alpha"

    def test_unpack_legacy(self, secret: bytes) -> None:
        body, envelope = unpack_message("search grid sector 4", secret)
        assert body == {"content": "search grid sector 4"}
        assert envelope is None

    def test_unpack_with_auth(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        packed = pack_message(
            "hello",
            MessageKind.MAIL,
            sender_id="worker-beta",
            recipient_id="worker-alpha",
            secret=secret,
            team_id="sar-team-7",
            team_epoch=1,
        )
        body, env = unpack_message(
            packed, secret, role=PeerRole.WORKER, auth_context=auth_ctx
        )
        assert env is not None

    def test_unpack_auth_denied(
        self, secret: bytes, auth_ctx: AuthorizationContext
    ) -> None:
        packed = pack_message(
            "admin stuff",
            MessageKind.TEAM_REVOKE,
            sender_id="coordinator-1",
            recipient_id="worker-alpha",
            secret=secret,
        )
        with pytest.raises(AuthzDeniedError):
            unpack_message(packed, secret, role=PeerRole.WORKER, auth_context=auth_ctx)

    def test_unpack_expired(self, secret: bytes) -> None:
        sent = datetime.now(timezone.utc) - timedelta(hours=1)
        expired = datetime.now(timezone.utc) - timedelta(seconds=10)
        e = MessageEnvelope(
            kind=MessageKind.MAIL,
            sender_id="a",
            recipient_id="b",
            sent_at=sent,
            expires_at=expired,
        )
        sign_envelope(e, secret)
        packed = e.model_dump_json()
        with pytest.raises(EnvelopeExpiredError):
            unpack_message(packed, secret)

    def test_unpack_with_idempotency_guard(self, secret: bytes) -> None:
        g = IdempotencyGuard(max_size=100, ttl_seconds=3600)
        packed = pack_message(
            "hello", MessageKind.MAIL, sender_id="a", recipient_id="b", secret=secret
        )
        body, env = unpack_message(packed, secret, idempotency_guard=g)
        assert env is not None
        with pytest.raises(ReplayError):
            unpack_message(packed, secret, idempotency_guard=g)

    def test_unpack_role_without_auth_context_raises(self, secret: bytes) -> None:
        packed = pack_message(
            "x", MessageKind.MAIL, sender_id="a", recipient_id="b", secret=secret
        )
        with pytest.raises(ValueError, match="role requires auth_context"):
            unpack_message(packed, secret, role=PeerRole.WORKER)

    def test_pack_binary_body(self, secret: bytes) -> None:
        packed = pack_message(
            json.dumps({"x": 1}),
            MessageKind.MAIL,
            sender_id="a",
            recipient_id="b",
            secret=secret,
        )
        unpacked, _ = unpack_message(packed, secret)
        assert unpacked == {"content": json.dumps({"x": 1})}


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy() -> None:
    assert issubclass(SignatureInvalidError, Exception)
    assert issubclass(ReplayError, Exception)
    assert issubclass(AuthzDeniedError, Exception)
    assert issubclass(EnvelopeExpiredError, Exception)
    assert issubclass(MalformedEnvelopeError, Exception)
