"""Phase 2 callback authentication: CallbackProofV1 + durable nonce fence.

The ``/a2a/push-callback`` route is the single Worker→Coordinator Memory write
entry.  In ``shadow`` / ``read_port`` mode every callback must carry an
``X-A2A-Callback-Proof`` header whose canonical payload is
``worker_id.timestamp.nonce.body_sha256`` HMAC-SHA256(coordinator_secret, ...).
A verified nonce is durably reserved in SQLite ``callback_nonce``
(``UNIQUE(worker_id, nonce)``) so a replayed request can never double-write.

Authentication failures produce only a redacted security audit and cause zero
domain writer calls.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

from a2a.coordinator.memory.store import MemoryStore

__all__ = [
    "MIN_CALLBACK_SECRET_BYTES",
    "CallbackAuthResult",
    "CallbackAuthenticator",
    "CallbackProofV1",
    "MemoryAuthNotConfiguredError",
]

MIN_CALLBACK_SECRET_BYTES = 16

SEPARATOR = "."
NONCE_BYTES = 16
DEFAULT_MAX_AGE_SECONDS = 60
DEFAULT_CLOCK_SKEW_SECONDS = 10
DEFAULT_NONCE_TTL_SECONDS = 70


class MemoryAuthNotConfiguredError(RuntimeError):
    """Raised at startup when a secure memory mode has no valid callback secret."""

    code = "memory_auth_not_configured"


class CallbackProofV1:
    """Body-bound HMAC callback proof (base64 ``worker_id.ts.nonce.body_sha256.sig``).

    Unlike the unbound ``TeamStatusProof`` v2 format, this proof binds the exact
    serialized HTTP body so tampering is rejected before any writer runs.
    """

    SEPARATOR = SEPARATOR
    NONCE_BYTES = NONCE_BYTES
    DEFAULT_MAX_AGE_SECONDS = DEFAULT_MAX_AGE_SECONDS
    DEFAULT_CLOCK_SKEW_SECONDS = DEFAULT_CLOCK_SKEW_SECONDS

    @staticmethod
    def canonical_payload(
        worker_id: str, timestamp: int, nonce: str, body_sha256: str
    ) -> bytes:
        return (
            f"{worker_id}{SEPARATOR}{timestamp}{SEPARATOR}{nonce}{SEPARATOR}{body_sha256}"
        ).encode("utf-8")

    @staticmethod
    def generate(
        coordinator_secret: bytes,
        worker_id: str,
        body_sha256: str,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> str:
        if not coordinator_secret:
            raise MemoryAuthNotConfiguredError(
                "coordinator_secret is empty for CallbackProofV1"
            )
        if not worker_id:
            raise ValueError("worker_id must be non-empty")
        if not body_sha256:
            raise ValueError("body_sha256 must be non-empty")
        if timestamp is None:
            timestamp = int(time.time())
        if nonce is None:
            nonce = secrets.token_hex(NONCE_BYTES)
        payload = CallbackProofV1.canonical_payload(
            worker_id, timestamp, nonce, body_sha256
        )
        sig = hmac.new(coordinator_secret, payload, hashlib.sha256).hexdigest()
        combined = (
            f"{worker_id}{SEPARATOR}{timestamp}{SEPARATOR}{nonce}"
            f"{SEPARATOR}{body_sha256}{SEPARATOR}{sig}"
        )
        return base64.urlsafe_b64encode(combined.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(proof: str) -> tuple[str, str, str, str, str]:
        """Decode a proof into (worker_id, timestamp, nonce, body_sha256, sig)."""
        decoded = base64.urlsafe_b64decode(proof.encode("ascii")).decode("utf-8")
        parts = decoded.split(SEPARATOR)
        if len(parts) != 5:
            raise ValueError(f"malformed proof: expected 5 parts, got {len(parts)}")
        return parts[0], parts[1], parts[2], parts[3], parts[4]

    @staticmethod
    def nonce_of(proof: str) -> str:
        return CallbackProofV1.decode(proof)[2]

    @staticmethod
    def worker_id_of(proof: str) -> str:
        return CallbackProofV1.decode(proof)[0]

    @staticmethod
    def verify(
        coordinator_secret: bytes,
        proof: str,
        expected_worker_id: str,
        body_sha256: str,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        clock_skew: int = DEFAULT_CLOCK_SKEW_SECONDS,
    ) -> tuple[bool, str]:
        """Pure verification (no writes). Returns ``(ok, reason)``."""
        if not coordinator_secret:
            return False, "coordinator_secret is empty"
        if not proof:
            return False, "proof is empty"
        try:
            worker_id, timestamp_str, nonce, proof_body_sha256, sig_hex = (
                CallbackProofV1.decode(proof)
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return False, f"malformed proof: {exc}"
        if worker_id != expected_worker_id:
            return False, "worker_id mismatch"
        if not hmac.compare_digest(proof_body_sha256, body_sha256):
            return False, "body_sha256 mismatch (tampered body)"
        try:
            ts = int(timestamp_str)
        except ValueError:
            return False, "invalid timestamp (non-integer)"
        now = int(time.time())
        if now < ts - clock_skew:
            return False, "timestamp in future (clock skew)"
        if now > ts + max_age_seconds:
            return False, "proof expired"
        payload = CallbackProofV1.canonical_payload(
            worker_id, ts, nonce, proof_body_sha256
        )
        expected_sig = hmac.new(coordinator_secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, sig_hex):
            return False, "invalid signature"
        return True, ""


class CallbackAuthResult:
    """Result of the route-front authentication gate."""

    __slots__ = ("ok", "reason", "worker_id", "body_sha256")

    def __init__(
        self, ok: bool, reason: str = "", worker_id: str = "", body_sha256: str = ""
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.worker_id = worker_id
        self.body_sha256 = body_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.ok else "rejected",
            "reason": self.reason if not self.ok else "",
        }


class CallbackAuthenticator:
    """Coordinator-side authenticator backed by a durable SQLite nonce ledger.

    Construction validates the secret: missing / empty / <16 bytes raises
    ``MemoryAuthNotConfiguredError`` (startup fails closed for secure modes).
    """

    def __init__(
        self,
        secret: bytes | None,
        store: MemoryStore,
        *,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        clock_skew: int = DEFAULT_CLOCK_SKEW_SECONDS,
        nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS,
    ) -> None:
        if (
            not secret
            or not isinstance(secret, bytes)
            or len(secret) < MIN_CALLBACK_SECRET_BYTES
        ):
            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: callback secret must be at least "
                f"{MIN_CALLBACK_SECRET_BYTES} bytes"
            )
        self._secret = secret
        self._store = store
        self._max_age_seconds = max_age_seconds
        self._clock_skew = clock_skew
        self._nonce_ttl_seconds = nonce_ttl_seconds

    def _body_sha256(self, body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def verify_proof(self, worker_id: str, proof: str, body: bytes) -> tuple[bool, str]:
        """Step 0: pure HMAC verification with no writes."""
        return CallbackProofV1.verify(
            self._secret,
            proof,
            worker_id,
            self._body_sha256(body),
            max_age_seconds=self._max_age_seconds,
            clock_skew=self._clock_skew,
        )

    def reserve_nonce(
        self, worker_id: str, proof: str, body: bytes, now: int | None = None
    ) -> tuple[bool, str]:
        """Step 1: durably reserve the proof nonce; rejects replay."""
        try:
            _, _, nonce, _, _ = CallbackProofV1.decode(proof)
        except ValueError as exc:
            return False, f"malformed proof: {exc}"
        ts = int(time.time()) if now is None else now
        reserved = self._store.reserve_callback_nonce(
            worker_id, nonce, float(ts) + self._nonce_ttl_seconds
        )
        if not reserved:
            return False, "nonce already used (replay)"
        return True, ""

    def authenticate(
        self, worker_id: str, proof: str, body: bytes, now: int | None = None
    ) -> CallbackAuthResult:
        """Full route-front gate: verify then reserve.  Zero domain writes."""
        body_sha256 = self._body_sha256(body)
        ok, reason = self.verify_proof(worker_id, proof, body)
        if not ok:
            self._store.record_security_audit(
                "callback_auth_rejected",
                reason=reason,
                digest_prefix=body_sha256[:16],
            )
            return CallbackAuthResult(False, reason, worker_id, body_sha256)
        ok, reason = self.reserve_nonce(worker_id, proof, body, now=now)
        if not ok:
            self._store.record_security_audit(
                "callback_auth_rejected",
                reason=reason,
                digest_prefix=body_sha256[:16],
            )
            return CallbackAuthResult(False, reason, worker_id, body_sha256)
        return CallbackAuthResult(True, worker_id=worker_id, body_sha256=body_sha256)

    def record_rejection(
        self, reason: str, worker_id: str = "", body_sha256: str = ""
    ) -> None:
        self._store.record_security_audit(
            "callback_auth_rejected",
            reason=reason,
            digest_prefix=(body_sha256 or "")[:16] or None,
        )
