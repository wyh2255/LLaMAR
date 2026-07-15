"""Versioned envelope protocol, HMAC-SHA256 auth, replay/idempotency guard.

Phase 1 building block for A2A peer messaging between coordinator and workers.
Envelope kinds: task, mail, team_update, team_revoke.
Cancellation remains native A2A CancelTask — not a SendMessage kind.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationError

PROTOCOL = "a2a-peer"
VERSION = "1.0"

# Fields included in HMAC canonical payload (everything except signature).
# Order here is for reference; canonical serialization sorts keys.
CANONICAL_FIELD_NAMES = (
    "protocol",
    "version",
    "kind",
    "sender_id",
    "recipient_id",
    "message_id",
    "sent_at",
    "expires_at",
    "team_id",
    "team_epoch",
    "body",
)

# Default max clock skew for sent_at (seconds).
DEFAULT_MAX_SKEW = 300.0


class MessageKind(str, Enum):
    TASK = "task"
    MAIL = "mail"
    TEAM_UPDATE = "team_update"
    TEAM_REVOKE = "team_revoke"


class EnvelopeError(Exception):
    """Base for envelope signing/verification failures."""


class SignatureInvalidError(EnvelopeError):
    """HMAC signature does not match."""


class EnvelopeExpiredError(EnvelopeError):
    """Message has expired or sent_at is outside allowed skew."""


class MalformedEnvelopeError(EnvelopeError):
    """Envelope fields are malformed or missing."""


class ReplayError(EnvelopeError):
    """Message has already been processed (replay detected)."""


class AuthzDeniedError(EnvelopeError):
    """Sender role is not authorized to send this message kind."""


class PeerRole(str, Enum):
    COORDINATOR = "coordinator"
    WORKER = "worker"


# --- Authorization context ---------------------------------------------------


@dataclass
class AuthorizationContext:
    """Identity and team state for envelope authorization.

    Supplied *after* secret selection/identity resolution (the caller
    determines ``PeerRole`` by trying the coordinator secret first, then
    the worker shared secret).  This documents a critical trust boundary:
    role and context are established **before** authorization rules are
    evaluated.
    """

    local_worker_id: str
    coordinator_id: str = "Coordinator"
    current_team_id: str | None = None
    current_team_epoch: int | None = None
    current_team_members: frozenset[str] = field(default_factory=frozenset)


# --- Default authorization rules (kind-based) --------------------------------

_AUTHZ_RULES: dict[PeerRole, set[MessageKind]] = {
    PeerRole.COORDINATOR: set(MessageKind),
    PeerRole.WORKER: {MessageKind.MAIL},
}


# --- MessageEnvelope model ---------------------------------------------------


class MessageEnvelope(BaseModel):
    """Versioned message envelope for coordinator-worker peer messaging."""

    protocol: str = PROTOCOL
    version: str = VERSION
    kind: MessageKind
    sender_id: str
    recipient_id: str
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    team_id: str | None = None
    team_epoch: int | None = None
    body: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: str) -> str:
        if v != PROTOCOL:
            raise ValueError(f"Unsupported protocol: {v}")
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v != VERSION:
            raise ValueError(f"Unsupported version: {v}")
        return v

    @field_validator("sender_id", "recipient_id", "message_id")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be non-blank")
        return v

    @field_validator("team_id")
    @classmethod
    def _nonblank_team_id(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("team_id must be non-blank when supplied")
        return v

    @field_validator("team_epoch")
    @classmethod
    def _nonnegative_epoch(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("team_epoch must be non-negative")
        return v

    @model_validator(mode="after")
    def _team_coherence(self) -> MessageEnvelope:
        if self.team_epoch is not None and self.team_id is None:
            raise ValueError("team_epoch requires team_id")
        return self

    @model_validator(mode="after")
    def _check_expires_at(self) -> MessageEnvelope:
        if self.expires_at is not None:
            try:
                if self.expires_at < self.sent_at:
                    raise ValueError("expires_at must be >= sent_at")
            except TypeError:
                raise ValueError("expires_at and sent_at must both be timezone-aware")
        return self

    def model_dump_canonical(self) -> dict[str, Any]:
        """Dump security-relevant fields for HMAC signing.

        All datetimes are normalized to UTC for deterministic output
        regardless of original timezone offset.
        """
        data: dict[str, Any] = {}
        for field_name in CANONICAL_FIELD_NAMES:
            val = getattr(self, field_name, None)
            if isinstance(val, Enum):
                data[field_name] = val.value
            elif isinstance(val, datetime):
                if val.tzinfo is None:
                    normalized = val.replace(tzinfo=timezone.utc)
                else:
                    normalized = val.astimezone(timezone.utc)
                data[field_name] = normalized.isoformat()
            else:
                data[field_name] = val
        return data


# --- Canonical serialisation -------------------------------------------------


def _canonical_bytes(envelope: MessageEnvelope) -> bytes:
    """Serialize security-relevant fields into canonical UTF-8 bytes.

    Uses sorted keys for deterministic output.
    ``signature`` is NOT in CANONICAL_FIELD_NAMES so it is automatically
    excluded — no mutation needed.
    """
    data = envelope.model_dump_canonical()
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return canonical.encode("utf-8")


# --- Sign / verify -----------------------------------------------------------


def sign_envelope(envelope: MessageEnvelope, secret: bytes) -> MessageEnvelope:
    """Sign envelope with HMAC-SHA256 using the given secret.

    Sets ``envelope.signature`` in place and returns the envelope for
    chaining.  The signature is computed over all CANONICAL_FIELD_NAMES
    fields.

    Raises ``ValueError`` if *secret* is empty.
    """
    if not secret:
        raise ValueError("secret must be non-empty")
    canonical = _canonical_bytes(envelope)
    sig = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    envelope.signature = sig
    return envelope


def verify_envelope(
    envelope: MessageEnvelope,
    secret: bytes,
    *,
    max_skew_seconds: float = DEFAULT_MAX_SKEW,
    idempotency_guard: IdempotencyGuard | None = None,
) -> None:
    """Verify HMAC signature, temporal validity, and replay status.

    HMAC is verified FIRST (before temporal checks)
    to prevent unauthenticated values from driving expiry decisions.

    Args:
        envelope: The envelope to verify.
        secret: Shared HMAC secret.
        max_skew_seconds: Maximum allowed |now - sent_at| skew.
        idempotency_guard: Optional guard to detect replays.

    Raises:
        ValueError: *secret* is empty or *max_skew_seconds* is negative.
        MalformedEnvelopeError: Missing / non-hex / wrong-length signature.
        SignatureInvalidError: HMAC mismatch.
        EnvelopeExpiredError: Skew exceeds limit or message has expired.
        ReplayError: Duplicate message_id (when guard is provided).
    """
    if not secret:
        raise ValueError("secret must be non-empty")
    if max_skew_seconds < 0:
        raise ValueError("max_skew_seconds must be >= 0")

    sig = envelope.signature
    if sig is None:
        raise MalformedEnvelopeError("Missing signature")
    # Exactly 64 hex chars (SHA256 hex digest)
    if len(sig) != 64:
        raise MalformedEnvelopeError(
            f"Signature must be exactly 64 hex characters, got {len(sig)}"
        )
    try:
        int(sig, 16)
    except ValueError:
        raise MalformedEnvelopeError("Signature must be a valid hex string")

    # --- HMAC verification FIRST ---
    # canonical_bytes excludes signature via CANONICAL_FIELD_NAMES — no mutation.
    canonical = _canonical_bytes(envelope)
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii")):
        raise SignatureInvalidError("HMAC signature mismatch")

    # --- Temporal checks SECOND ---
    sent_at = _normalize_to_utc(envelope.sent_at)
    now = datetime.now(timezone.utc)
    skew = abs((now - sent_at).total_seconds())
    if skew > max_skew_seconds:
        raise EnvelopeExpiredError(
            f"Message sent_at skew {skew:.1f}s exceeds {max_skew_seconds}s"
        )
    if envelope.expires_at is not None:
        expires = _normalize_to_utc(envelope.expires_at)
        if now > expires:
            raise EnvelopeExpiredError("Message has expired")

    # --- Replay check ---
    if idempotency_guard is not None:
        idempotency_guard.check_and_mark(envelope.message_id)


def _normalize_to_utc(dt: datetime) -> datetime:
    """Convert timezone-aware datetime to UTC.

    Raises MalformedEnvelopeError if datetime is naive.
    """
    if dt.tzinfo is None:
        raise MalformedEnvelopeError("Datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


# --- Replay guard ------------------------------------------------------------


class IdempotencyGuard:
    """Thread-safe replay/idempotency guard keyed by message_id.

    Maintains a bounded set of seen message_ids with TTL-based expiry.
    Expired entries are evicted lazily on each ``check_and_mark`` call.
    """

    def __init__(self, max_size: int = 10000, ttl_seconds: float = 3600.0):
        if max_size <= 0:
            raise ValueError("max_size must be > 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def check_and_mark(self, message_id: str) -> None:
        """Check message_id for replay and mark it as seen.

        Raises ``ReplayError`` if *message_id* was already seen and not
        expired.  Thread-safe.
        """
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            existing = self._seen.get(message_id)
            if existing is not None:
                raise ReplayError(
                    f"Duplicate message_id '{message_id}' (first seen "
                    f"{now - existing:.1f}s ago)"
                )
            if len(self._seen) >= self._max_size:
                evict_count = max(1, self._max_size // 4)
                self._evict_oldest(evict_count)
            self._seen[message_id] = now

    def is_seen(self, message_id: str) -> bool:
        """Check if *message_id* is already recorded (non-expired).  Read-only."""
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            return message_id in self._seen

    def forget(self, message_id: str) -> None:
        """Manually remove a *message_id* from the guard."""
        with self._lock:
            self._seen.pop(message_id, None)

    def clear(self) -> None:
        """Clear all seen message_ids."""
        with self._lock:
            self._seen.clear()

    def size(self) -> int:
        """Approximate number of tracked message_ids."""
        with self._lock:
            return len(self._seen)

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        expired = [mid for mid, ts in self._seen.items() if ts < cutoff]
        for mid in expired:
            del self._seen[mid]

    def _evict_oldest(self, count: int) -> None:
        """Evict the *count* oldest entries (by insertion timestamp)."""
        sorted_items = sorted(self._seen.items(), key=lambda x: x[1])
        for mid, _ in sorted_items[:count]:
            del self._seen[mid]


# --- Authorization -----------------------------------------------------------

_INVALID_RECIPIENT = "Envelope recipient does not match local worker"
_COORD_ID_MISMATCH = "Coordinator sender_id does not match configured coordinator_id"
_SELF_SEND = "Worker cannot send messages to itself"
_NO_TEAM = "No active team for worker authorization"
_TEAM_MISMATCH = "Envelope team fields do not match current team state"
_NOT_IN_TEAM = "Sender is not a member of the current team"


def authorize_envelope(
    envelope: MessageEnvelope,
    role: PeerRole,
    context: AuthorizationContext,
    *,
    rules: dict[PeerRole, set[MessageKind]] | None = None,
) -> None:
    """Check that ``role`` is authorized to send *envelope*.

    Authorization rules (evaluated in order):

    1. ``envelope.recipient_id`` must equal ``context.local_worker_id``.
    2. ``COORDINATOR`` role \\:
       - ``sender_id`` must equal ``context.coordinator_id``.
       - Any ``MessageKind`` allowed.
       - ``TEAM_UPDATE`` / ``TEAM_REVOKE`` carry a control payload;
         current team membership is **not** required.
    3. ``WORKER`` role \\:
       - Kind must be ``MAIL``.
       - ``sender_id`` must **not** equal ``local_worker_id``.
       - An active team must exist (``current_team_id`` / ``current_team_epoch``
         are not ``None``).
       - ``envelope.team_id`` and ``envelope.team_epoch`` must **exactly**
         match the context's current team state.
       - ``sender_id`` must be listed in ``current_team_members``.

    Args:
        envelope: The signed envelope to check.
        role: The sender's resolved peer role.
        context: Identity and team state for authorization.
        rules: Override kind-based rules (defaults to ``_AUTHZ_RULES``).

    Raises:
        AuthzDeniedError: Any check fails.
    """
    # 1. Recipient check (always)
    if envelope.recipient_id != context.local_worker_id:
        raise AuthzDeniedError(_INVALID_RECIPIENT)

    # 2. Kind-based check
    effective = rules if rules is not None else _AUTHZ_RULES
    allowed = effective.get(role)
    if allowed is None:
        raise AuthzDeniedError(f"Unknown peer role: {role}")
    if envelope.kind not in allowed:
        raise AuthzDeniedError(
            f"Role '{role.value}' is not allowed to send kind '{envelope.kind.value}'"
        )

    # 3. Role-specific checks
    if role is PeerRole.COORDINATOR:
        if envelope.sender_id != context.coordinator_id:
            raise AuthzDeniedError(_COORD_ID_MISMATCH)
        return

    if role is PeerRole.WORKER:
        if envelope.sender_id == context.local_worker_id:
            raise AuthzDeniedError(_SELF_SEND)
        if context.current_team_id is None or context.current_team_epoch is None:
            raise AuthzDeniedError(_NO_TEAM)
        if (
            envelope.team_id != context.current_team_id
            or envelope.team_epoch != context.current_team_epoch
        ):
            raise AuthzDeniedError(_TEAM_MISMATCH)
        if envelope.sender_id not in context.current_team_members:
            raise AuthzDeniedError(_NOT_IN_TEAM)


# --- Classifier / integration helpers ----------------------------------------


def is_envelope_payload(text: str) -> bool:
    """Detect whether *text* is a JSON-encoded MessageEnvelope.

    Any JSON object with ``protocol == PROTOCOL`` is treated as an envelope
    candidate.  Malformed or incomplete fields within such an object
    will raise during unpack/verify (downgrade prevention).

    Legacy plain text and unrelated JSON (no ``protocol`` key matching
    PROTOCOL) are left as legacy.
    """
    if not text.startswith("{"):
        return False
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("protocol") == PROTOCOL


def pack_message(
    body: str | dict,
    kind: MessageKind,
    sender_id: str,
    recipient_id: str,
    *,
    secret: bytes,
    team_id: str | None = None,
    team_epoch: int | None = None,
    expires_at: datetime | None = None,
    message_id: str | None = None,
) -> str:
    """Create a signed JSON envelope wrapping *body* as the inner content.

    *body* may be a ``str`` (which becomes ``{"content": body}``) or a
    ``dict`` (used as-is for control messages such as team_update).

    Returns the JSON string ready to be placed in an A2A ``Part(text=...)``
    (or any other text carrier).  The envelope is signed in place.
    """
    envelope = MessageEnvelope(
        kind=kind,
        sender_id=sender_id,
        recipient_id=recipient_id,
        team_id=team_id,
        team_epoch=team_epoch,
        expires_at=expires_at,
        body={"content": body} if isinstance(body, str) else body,
        message_id=message_id or uuid.uuid4().hex,
    )
    sign_envelope(envelope, secret)
    return envelope.model_dump_json()


def unpack_message(
    text: str,
    secret: bytes,
    *,
    max_skew_seconds: float = DEFAULT_MAX_SKEW,
    idempotency_guard: IdempotencyGuard | None = None,
    role: PeerRole | None = None,
    auth_context: AuthorizationContext | None = None,
) -> tuple[dict[str, Any], MessageEnvelope | None]:
    """Parse and verify an optional envelope from *text*.

    If *text* is an envelope payload (detected via :func:`is_envelope_payload`),
    it is deserialised, verified (signature, temporal, idempotency), and the
    inner body plus the envelope are returned.  Malformed envelope fields
    within a ``protocol == PROTOCOL`` object **raise** (downgrade prevention).

    If *text* is **not** an envelope (legacy plain-text or unrelated JSON),
    the body is returned as-is with ``envelope=None``.

    When *role* and *auth_context* are both provided, authorization is
    performed after verification.

    Returns:
        ``(body_dict, envelope_or_None)``.

    Raises:
        EnvelopeError subclasses on verification or authorization failure.
    """
    if not is_envelope_payload(text):
        return {"content": text}, None

    data = json.loads(text)
    try:
        envelope = MessageEnvelope(**data)
    except ValidationError as exc:
        raise MalformedEnvelopeError(str(exc)) from exc

    verify_envelope(
        envelope,
        secret,
        max_skew_seconds=max_skew_seconds,
        idempotency_guard=idempotency_guard,
    )
    if role is not None:
        if auth_context is None:
            raise ValueError("role requires auth_context")
        authorize_envelope(envelope, role, auth_context)

    return envelope.body, envelope
