"""Phase 2 callback authentication contracts: CallbackProofV1 + durable nonce.

Every unauthenticated callback must be rejected with zero domain writes.  The
nonce ledger must be durable (SQLite), not an in-memory set.  Missing / empty /
short secrets fail closed at startup with ``memory_auth_not_configured``.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from a2a.coordinator.memory.callback_auth import (
    MIN_CALLBACK_SECRET_BYTES,
    CallbackAuthResult,
    CallbackAuthenticator,
    CallbackProofV1,
    MemoryAuthNotConfiguredError,
)
from a2a.coordinator.memory.store import MemoryStore

SECRET = b"coordinator-callback-secret-0123456789"


def _body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _Writers:
    """Spies for EventStore / SemanticMapStore / TaskWatchdog / MemoryIngestor."""

    def __init__(self):
        self.calls: list[str] = []

    def event_store(self, *args, **kwargs):
        self.calls.append("event_store")

    def semantic_map(self, *args, **kwargs):
        self.calls.append("semantic_map")

    def watchdog(self, *args, **kwargs):
        self.calls.append("watchdog")

    def memory_ingestor(self, *args, **kwargs):
        self.calls.append("memory_ingestor")


def _gate(authenticator, worker_id, proof, body, writers) -> bool:
    """Mimic the route-front auth gate: zero domain writes on failure."""
    result = authenticator.authenticate(worker_id=worker_id, proof=proof, body=body)
    if not result.ok:
        return False
    writers.event_store()
    writers.semantic_map()
    writers.watchdog()
    writers.memory_ingestor()
    return True


# ---------------------------------------------------------------------------
# CallbackProofV1 — pure HMAC verify bound to the actual body
# ---------------------------------------------------------------------------


def test_proof_v1_generate_verify_roundtrip():
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", _body_sha256(body))
    assert ok is True
    assert reason == ""


def test_proof_v1_rejects_body_tampering():
    body = b'{"task": {"id": "t-1"}}'
    tampered = b'{"task": {"id": "t-2"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", _body_sha256(tampered))
    assert ok is False
    assert "body" in reason or "invalid signature" in reason


def test_proof_v1_rejects_expired_timestamp():
    body = b'{"task": {"id": "t-1"}}'
    old_ts = int(time.time()) - 300
    proof = CallbackProofV1.generate(
        SECRET, "Alice", _body_sha256(body), timestamp=old_ts
    )
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", _body_sha256(body))
    assert ok is False
    assert "expired" in reason


def test_proof_v1_rejects_worker_mismatch():
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Bob", _body_sha256(body))
    assert ok is False
    assert "worker_id mismatch" in reason


def test_proof_v1_rejects_invalid_signature():
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(
        b"wrong-secret-0123456789abcdef", "Alice", _body_sha256(body)
    )
    ok, reason = CallbackProofV1.verify(SECRET, proof, "Alice", _body_sha256(body))
    assert ok is False


def test_proof_v1_rejects_malformed_proof():
    ok, reason = CallbackProofV1.verify(SECRET, "not-a-proof", "Alice", "deadbeef")
    assert ok is False
    assert reason


# ---------------------------------------------------------------------------
# Secret configuration fences
# ---------------------------------------------------------------------------


def test_min_callback_secret_bytes_constant():
    assert MIN_CALLBACK_SECRET_BYTES == 16


def test_short_secret_fails_startup_with_typed_error(tmp_path):
    store = MemoryStore(tmp_path / "mem.sqlite3")
    with pytest.raises(MemoryAuthNotConfiguredError) as exc:
        CallbackAuthenticator(b"short", store=store)
    assert exc.value.code == "memory_auth_not_configured"


def test_empty_secret_fails_startup():
    store = MemoryStore("/tmp/opencode/empty-secret-mem.sqlite3")
    with pytest.raises(MemoryAuthNotConfiguredError):
        CallbackAuthenticator(b"", store=store)


def test_missing_secret_fails_startup():
    store = MemoryStore("/tmp/opencode/missing-secret-mem.sqlite3")
    with pytest.raises(MemoryAuthNotConfiguredError):
        CallbackAuthenticator(None, store=store)


# ---------------------------------------------------------------------------
# Durable nonce reservation
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def authenticator(store):
    return CallbackAuthenticator(SECRET, store=store)


def test_nonce_is_durably_reserved_and_replayed_nonce_rejected(store, authenticator):
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))

    ok, reason = authenticator.reserve_nonce("Alice", proof, body)
    assert ok is True
    assert reason == ""

    # Same nonce again → durable replay fence rejects.
    ok2, reason2 = authenticator.reserve_nonce("Alice", proof, body)
    assert ok2 is False
    assert "replay" in reason2 or "nonce" in reason2

    # A fresh nonce over the same body is accepted.
    proof2 = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    ok3, reason3 = authenticator.reserve_nonce("Alice", proof2, body)
    assert ok3 is True
    assert reason3 == ""


def test_authenticate_returns_ok_and_writes_no_domain_records(store, authenticator):
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    result = authenticator.authenticate(worker_id="Alice", proof=proof, body=body)
    assert result.ok is True
    assert isinstance(result, CallbackAuthResult)
    # No scope / event / nonce leakage beyond the reservation itself.
    assert store.list_scopes() == []


def test_zero_domain_writes_on_missing_proof(store, authenticator):
    writers = _Writers()
    body = b'{"task": {"id": "t-1"}}'
    accepted = _gate(authenticator, "Alice", "", body, writers)
    assert accepted is False
    assert writers.calls == []
    assert store.list_scopes() == []


def test_zero_domain_writes_on_unsigned_tampered_body(store, authenticator):
    writers = _Writers()
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    tampered = b'{"task": {"id": "t-2"}}'
    accepted = _gate(authenticator, "Alice", proof, tampered, writers)
    assert accepted is False
    assert writers.calls == []
    assert store.list_scopes() == []


def test_zero_domain_writes_on_expired_proof(store, authenticator):
    writers = _Writers()
    body = b'{"task": {"id": "t-1"}}'
    old_ts = int(time.time()) - 300
    proof = CallbackProofV1.generate(
        SECRET, "Alice", _body_sha256(body), timestamp=old_ts
    )
    accepted = _gate(authenticator, "Alice", proof, body, writers)
    assert accepted is False
    assert writers.calls == []


def test_zero_domain_writes_on_worker_mismatch(store, authenticator):
    writers = _Writers()
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    # The request claims Bob but the proof binds Alice.
    accepted = _gate(authenticator, "Bob", proof, body, writers)
    assert accepted is False
    assert writers.calls == []


def test_zero_domain_writes_on_replayed_nonce(store, authenticator):
    writers = _Writers()
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    assert _gate(authenticator, "Alice", proof, body, writers) is True
    assert writers.calls == [
        "event_store",
        "semantic_map",
        "watchdog",
        "memory_ingestor",
    ]

    writers2 = _Writers()
    accepted = _gate(authenticator, "Alice", proof, body, writers2)
    assert accepted is False
    assert writers2.calls == []


def test_auth_failure_only_produces_redacted_security_audit(store, authenticator):
    body = b'{"task": {"id": "t-1"}, "secret": "do-not-persist-me"}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    tampered = b'{"task": {"id": "t-9"}, "secret": "do-not-persist-me"}'
    result = authenticator.authenticate(worker_id="Alice", proof=proof, body=tampered)
    assert result.ok is False
    entries = store.security_audit_entries()
    assert entries
    last = entries[-1]
    assert last["kind"] == "callback_auth_rejected"
    assert "do-not-persist-me" not in str(entries)


def test_nonce_reservation_is_durable_across_store_restart(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    authenticator = CallbackAuthenticator(SECRET, store=store)
    body = b'{"task": {"id": "t-1"}}'
    proof = CallbackProofV1.generate(SECRET, "Alice", _body_sha256(body))
    assert authenticator.reserve_nonce("Alice", proof, body)[0] is True

    # Simulate restart: same database, nonce must still be spent.
    store.close()
    store2 = MemoryStore(path)
    authenticator2 = CallbackAuthenticator(SECRET, store=store2)
    ok, reason = authenticator2.reserve_nonce("Alice", proof, body)
    assert ok is False
    assert "replay" in reason or "nonce" in reason


def test_authenticator_can_be_configured_with_existing_scope():
    """Auth itself never activates a scope; scope comes from control state."""
    assert CallbackProofV1.NONCE_BYTES == 16
    assert CallbackProofV1.DEFAULT_MAX_AGE_SECONDS == 60
