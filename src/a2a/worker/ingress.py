"""Envelope ingress classifier — sits in front of AgentAdapter.

Classifies incoming request text as legacy plain-text, a signed envelope
(mail / task / team_update / team_revoke), or a reject.

Secret resolution does NOT trust ``sender_id`` alone: both the coordinator
secret and the peer secret (from team state) are tried and HMAC verification
determines identity.

Idempotency guard is applied **after** HMAC + temporal + authorization all
succeed — an unauthorized envelope does not consume its message_id.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from a2a.shared.message_envelope import (
    AuthzDeniedError,
    AuthorizationContext,
    EnvelopeError,
    IdempotencyGuard,
    MessageEnvelope,
    PeerRole,
    ReplayError,
    SignatureInvalidError,
    authorize_envelope,
    is_envelope_payload,
    verify_envelope,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from a2a.worker.team_state import WorkerTeamState

logger = logging.getLogger(__name__)


@dataclass
class IngressResult:
    """Result of ingress classification.

    ``action`` is one of:
      - ``"legacy"`` — plain text or non-envelope JSON (pass through)
      - ``"mail"`` — signed mail envelope
      - ``"task"`` — signed task envelope
      - ``"team_update"`` — signed team_update envelope
      - ``"team_revoke"`` — signed team_revoke envelope
      - ``"reject"`` — verification / authorization failed

    ``body`` contains the inner content dict.  ``envelope`` is the parsed
    ``MessageEnvelope`` (if applicable).  ``reason`` explains rejection.
    """

    action: str
    body: dict[str, Any]
    envelope: MessageEnvelope | None = None
    reason: str | None = None


class EnvelopeIngress:
    """Classify and verify incoming request text.

    Use ``classify()`` to determine the action for a piece of text.  The
    caller (``EnvelopeAwareAdapter``) then dispatches accordingly.

    Idempotency guard is applied **after** all verification and
    authorization checks pass — an envelope that fails temporal checks,
    is unauthorized, or has a bad signature does NOT consume its
    message_id.
    """

    def __init__(
        self,
        coordinator_secret: bytes,
        coordinator_id: str,
        local_worker_id: str,
        team_state: "WorkerTeamState | None" = None,
        *,
        max_skew_seconds: float = 300.0,
        idempotency_max_size: int = 10_000,
        idempotency_ttl: float = 3600.0,
        allow_legacy_tasks: bool = True,
    ):
        self._coordinator_secret = coordinator_secret
        self._coordinator_id = coordinator_id
        self._local_worker_id = local_worker_id
        self._team_state = team_state
        self._max_skew_seconds = max_skew_seconds
        self._idempotency_guard = IdempotencyGuard(
            max_size=idempotency_max_size,
            ttl_seconds=idempotency_ttl,
        )
        self._allow_legacy_tasks = allow_legacy_tasks

    @property
    def idempotency_guard(self) -> IdempotencyGuard:
        return self._idempotency_guard

    # ── classification ──────────────────────────────────────────────────

    def classify(self, text: str) -> IngressResult:
        """Classify *text* and return an ``IngressResult``.

        Steps:
          1. Detect envelope payload (``is_envelope_payload``).
          2. Deserialise into ``MessageEnvelope``.
          3. Try coordinator secret → ``PeerRole.COORDINATOR``.
          4. On HMAC mismatch, try peer secret → ``PeerRole.WORKER``.
          5. On verification success, authorize with Phase 1 policy.
          6. Only **after** steps 3-5 succeed, check idempotency guard.
          7. Reject on any failure with deterministic reason.

        Legacy (non-envelope) text returns ``action="legacy"`` immediately,
        unless ``allow_legacy_tasks`` is ``False``, in which case legacy
        text is rejected with ``action="reject"``.
        """
        if not is_envelope_payload(text):
            if not self._allow_legacy_tasks:
                return IngressResult(
                    action="reject",
                    body={},
                    reason="Legacy plain-text tasks rejected (allow_legacy_tasks=False)",
                )
            return IngressResult(action="legacy", body={"content": text})

        try:
            data = json.loads(text)
            envelope = MessageEnvelope(**data)
        except (json.JSONDecodeError, ValidationError) as exc:
            return IngressResult(
                action="reject",
                body={},
                reason=f"Malformed envelope: {exc}",
            )

        for secret, role in self._secrets_to_try():
            result = self._try_secret(envelope, secret, role)
            if result is not None:
                return result  # success or terminal rejection

        return IngressResult(
            action="reject",
            body={},
            reason="HMAC verification failed with all known secrets",
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _secrets_to_try(self) -> list[tuple[bytes, PeerRole]]:
        secrets: list[tuple[bytes, PeerRole]] = [
            (self._coordinator_secret, PeerRole.COORDINATOR),
        ]
        if self._team_state is not None:
            peer_bytes = self._team_state.peer_secret_bytes()
            if peer_bytes is not None:
                secrets.append((peer_bytes, PeerRole.WORKER))
        return secrets

    def _try_secret(
        self,
        envelope: MessageEnvelope,
        secret: bytes,
        role: PeerRole,
    ) -> IngressResult | None:
        """Try to verify + authorize + mark.

        Returns ``None`` if the HMAC doesn't match (try next secret).
        Returns an ``IngressResult`` (success or terminal reject) otherwise.
        """
        # 1. Verify WITHOUT idempotency guard
        try:
            verify_envelope(
                envelope,
                secret,
                max_skew_seconds=self._max_skew_seconds,
                idempotency_guard=None,
            )
        except SignatureInvalidError:
            return None  # HMAC mismatch → try next secret
        except EnvelopeError as exc:
            return IngressResult(
                action="reject",
                body={},
                envelope=envelope,
                reason=str(exc),
            )

        # 2. Authorize
        auth_ctx = self._build_auth_context(role)
        if auth_ctx is None:
            return IngressResult(
                action="reject",
                body={},
                envelope=envelope,
                reason="No authorization context available",
            )
        try:
            authorize_envelope(envelope, role, auth_ctx)
        except AuthzDeniedError as exc:
            return IngressResult(
                action="reject",
                body={},
                envelope=envelope,
                reason=str(exc),
            )

        # 3. Idempotency guard — only after verification + authorization
        try:
            self._idempotency_guard.check_and_mark(envelope.message_id)
        except ReplayError as exc:
            return IngressResult(
                action="reject",
                body={},
                envelope=envelope,
                reason=str(exc),
            )

        # All checks passed
        return IngressResult(
            action=envelope.kind.value,
            body=envelope.body,
            envelope=envelope,
        )

    def _build_auth_context(self, role: PeerRole) -> AuthorizationContext | None:
        if role is PeerRole.COORDINATOR:
            return AuthorizationContext(
                local_worker_id=self._local_worker_id,
                coordinator_id=self._coordinator_id,
            )
        if self._team_state is not None:
            ctx = self._team_state.to_auth_context()
            if ctx is not None:
                return ctx
        return AuthorizationContext(
            local_worker_id=self._local_worker_id,
            coordinator_id=self._coordinator_id,
        )
