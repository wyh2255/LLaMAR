"""WorkerPeerSenderService -- send signed peer mail via A2A SDK.

This is the worker-side counterpart to ``CoordinatorSenderService``.
Instead of using the coordinator secret and arbitrary worker endpoints,
it derives the signing secret and recipient endpoint from the worker's
current team roster (``WorkerTeamState``).

Only ``MAIL`` envelopes are sent (workers cannot issue ``TASK`` /
``TEAM_UPDATE`` / ``TEAM_REVOKE``).  The service validates that:
- a team is active (team_id + epoch + secret all present),
- the recipient is a different team member,
- the recipient's endpoint comes exclusively from the team roster
  (never caller-supplied).

The A2A SDK ``send_message`` is used with ``return_immediately=False``
so the caller blocks until a terminal ACK (COMPLETED / FAILED /
REJECTED / CANCELED) is received from the peer worker's
``EnvelopeAwareAdapter``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from a2a.client import Client, ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
from a2a.utils.constants import TransportProtocol

from a2a.shared.message_envelope import (
    MessageEnvelope,
    MessageKind,
    sign_envelope,
)
from a2a.shared.response_parser import parse_stream_for_terminal
from a2a.worker.team_state import DeliverySnapshot, WorkerTeamState

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
EXPIRES_DELTA_SECONDS = 60.0

MAX_SUBJECT_LENGTH = 256
MAX_BODY_LENGTH = 65536

# ── Error types ────────────────────────────────────────────────────────


class PeerSendError(Exception):
    """Peer mail delivery failed."""


class NotInTeamError(PeerSendError):
    """No active team or sender/recipient not in roster."""


class InvalidMailParameter(PeerSendError):
    """Subject/body/endpoint validation failed."""


# ── Observability callback type ────────────────────────────────────────

PeerMailEventCallback = Callable[
    [str, dict[str, Any]],
    None,
]
"""Callback ``(event_name, event_data)`` for observability.

Event names: ``mail_sent``, ``mail_rejected``, ``mail_delivery_failed``.

Event data (no body, no secret, no signature, NO subject):

- ``mail_sent``: ``message_id``, ``sender_id``, ``recipient_id``,
  ``team_id``, ``team_epoch``, ``outcome`` (``"sent"``).
- ``mail_rejected``: ``recipient_id``, ``reason`` (security-safe),
  ``team_id``.
- ``mail_delivery_failed``: ``message_id``, ``recipient_id``, ``error``,
  ``team_id``.
"""


# ── Main service ───────────────────────────────────────────────────────


class WorkerPeerSenderService:
    """Send signed MAIL envelopes to peer workers via A2A SDK.

    One instance per worker, created during ``SARWorker.start()`` when
    ``enable_peer_mail=True``.  The caller must ``await close()`` to
    release A2A SDK client connections.
    """

    def __init__(
        self,
        team_state: WorkerTeamState,
        local_worker_id: str,
        *,
        httpx_client: httpx.AsyncClient | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        event_callback: PeerMailEventCallback | None = None,
    ):
        if default_timeout <= 0:
            raise PeerSendError(f"default_timeout must be > 0, got {default_timeout}")
        self._team_state = team_state
        self._local_worker_id = local_worker_id
        self._httpx_client = httpx_client
        self._default_timeout = default_timeout
        self._event_callback = event_callback
        self._client_cache: dict[str, Client] = {}

    # ── public API ─────────────────────────────────────────────────────

    async def send_mail(
        self,
        recipient_id: str,
        subject: str,
        body: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a signed MAIL envelope to a peer worker.

        Returns the domain message_id on success, *not* the A2A transport
        task_id.  The transport task_id is available as ``transport_task_id``
        in the result dict for debugging.

        Observability callback fires exactly once per ``send_mail`` call,
        after the network round-trip completes (or synchronously for
        validation failures).  Callbacks fire outside any team state lock.

        Args:
            recipient_id: Target worker ID (must be in team roster).
            subject: Mail subject (max 256 chars, may be empty).
            body: Mail body (max 65536 chars, may be empty).
            timeout: Per-envelope timeout (default: 30s).

        Returns:
            ``{"success": True, "message_id": "<domain-id>",
              "transport_task_id": "<a2a-task-id>"}`` on ACK,
            ``{"success": False, "error": "<reason>"}`` on failure.

        Raises:
            NotInTeamError: No active team or validation fails
                synchronously (before network send).
            InvalidMailParameter: Subject/body/endpoint validation fails.
        """
        effective_timeout = self._resolve_timeout(timeout)
        self._validate_mail_params(subject, body)

        snap = self._team_state.delivery_snapshot()
        if snap is None:
            exc = NotInTeamError("No active team -- cannot send peer mail")
            self._fire_async(
                "mail_rejected",
                {
                    "recipient_id": recipient_id,
                    "reason": str(exc),
                },
            )
            raise exc
        self._validate_recipient(recipient_id, snap)

        envelope = self._build_envelope(
            recipient_id=recipient_id,
            subject=subject,
            body=body,
            team_id=snap.team_id,
            team_epoch=snap.epoch,
        )
        sign_envelope(envelope, snap.peer_secret)
        message_id = envelope.message_id

        recipient_endpoint = self._get_endpoint(recipient_id, snap)
        endpoint = self._normalise_endpoint(recipient_endpoint)

        transport_result = await self._do_send_control(
            env_json=envelope.model_dump_json(),
            recipient_endpoint=endpoint,
            timeout=effective_timeout,
        )

        if transport_result.get("success"):
            self._fire_async(
                "mail_sent",
                {
                    "message_id": message_id,
                    "sender_id": self._local_worker_id,
                    "recipient_id": recipient_id,
                    "team_id": snap.team_id,
                    "team_epoch": snap.epoch,
                    "outcome": "sent",
                },
            )
            return {
                "success": True,
                "message_id": message_id,
                "transport_task_id": transport_result.get("transport_task_id", ""),
            }

        self._fire_async(
            "mail_delivery_failed",
            {
                "message_id": message_id,
                "recipient_id": recipient_id,
                "error": transport_result.get("error", "unknown"),
                "team_id": snap.team_id,
            },
        )
        return {
            "success": False,
            "error": transport_result.get("error", "unknown"),
        }

    # ── async close ────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close all cached A2A SDK clients.

        Must be called during worker shutdown to prevent httpx client
        leaks.  Safe to call multiple times.
        """
        for endpoint, client in list(self._client_cache.items()):
            try:
                await client.close()
            except Exception:
                logger.debug("Error closing client for %s", endpoint, exc_info=True)
        self._client_cache.clear()
        logger.debug(
            "WorkerPeerSenderService closed (%d clients)",
            len(self._client_cache),
        )

    # ── internal helpers ───────────────────────────────────────────────

    def _resolve_timeout(self, timeout: float | None) -> float:
        t = timeout if timeout is not None else self._default_timeout
        if t <= 0:
            raise InvalidMailParameter(f"timeout must be > 0, got {t}")
        return t

    def _validate_mail_params(self, subject: str, body: str) -> None:
        if not isinstance(subject, str):
            raise InvalidMailParameter(
                f"subject must be a string, got {type(subject).__name__}"
            )
        if not isinstance(body, str):
            raise InvalidMailParameter(
                f"body must be a string, got {type(body).__name__}"
            )
        if len(subject) > MAX_SUBJECT_LENGTH:
            raise InvalidMailParameter(
                f"subject exceeds {MAX_SUBJECT_LENGTH} characters"
            )
        if len(body) > MAX_BODY_LENGTH:
            raise InvalidMailParameter(f"body exceeds {MAX_BODY_LENGTH} characters")

    def _validate_recipient(self, recipient_id: str, snap: DeliverySnapshot) -> None:
        if not recipient_id or not recipient_id.strip():
            raise NotInTeamError("recipient_id must be non-blank")
        if recipient_id == self._local_worker_id:
            raise NotInTeamError(f"Cannot send mail to self ('{recipient_id}')")
        if recipient_id not in snap.members:
            raise NotInTeamError(
                f"Recipient '{recipient_id}' is not a member of team "
                f"'{snap.team_id}' (members: {snap.members})"
            )

    def _get_endpoint(self, recipient_id: str, snap: DeliverySnapshot) -> str:
        ep = snap.endpoints.get(recipient_id)
        if not ep:
            raise NotInTeamError(
                f"Recipient '{recipient_id}' has no endpoint in team roster"
            )
        if not self._valid_endpoint(ep):
            raise InvalidMailParameter(
                f"Endpoint '{ep}' for '{recipient_id}' is not a valid "
                f"http/https URL with a hostname"
            )
        return ep

    @staticmethod
    def _valid_endpoint(endpoint: str) -> bool:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https"):
                return False
            if parsed.hostname is None or not parsed.hostname.strip():
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _normalise_endpoint(endpoint: str) -> str:
        from a2a.shared.endpoint_helpers import normalize_endpoint

        return normalize_endpoint(endpoint)

    def _build_envelope(
        self,
        recipient_id: str,
        subject: str,
        body: str,
        team_id: str,
        team_epoch: int,
    ) -> MessageEnvelope:
        sent_at = datetime.now(timezone.utc)
        expires_at = sent_at + timedelta(seconds=EXPIRES_DELTA_SECONDS)
        return MessageEnvelope(
            kind=MessageKind.MAIL,
            sender_id=self._local_worker_id,
            recipient_id=recipient_id,
            body={"subject": subject, "content": body},
            team_id=team_id,
            team_epoch=team_epoch,
            message_id=uuid.uuid4().hex,
            sent_at=sent_at,
            expires_at=expires_at,
        )

    async def _do_send_control(
        self,
        env_json: str,
        recipient_endpoint: str,
        timeout: float,
    ) -> dict[str, Any]:
        client = await self._get_client(recipient_endpoint)
        message = Message(role=Role.ROLE_USER, parts=[Part(text=env_json)])
        request = SendMessageRequest(message=message)
        request.configuration.return_immediately = False

        try:
            return await parse_stream_for_terminal(
                client.send_message(request), timeout
            )
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout after {timeout}s"}
        except Exception as exc:
            logger.warning("send_control to %s failed: %s", recipient_endpoint, exc)
            return {"success": False, "error": str(exc)}

    async def _get_client(self, endpoint: str) -> Client:
        if endpoint not in self._client_cache:
            hc = self._httpx_client or httpx.AsyncClient(
                timeout=httpx.Timeout(self._default_timeout + 5.0, connect=10.0)
            )
            config = ClientConfig(
                streaming=False,
                httpx_client=hc,
                supported_protocol_bindings=[
                    TransportProtocol.JSONRPC,
                    TransportProtocol.HTTP_JSON,
                ],
            )
            self._client_cache[endpoint] = await create_client(endpoint, config)
        return self._client_cache[endpoint]

    # ── Observability ──────────────────────────────────────────────────

    def _fire_async(self, name: str, data: dict[str, Any]) -> None:
        """Fire callback outside locks; safe to call from sync context."""
        if self._event_callback is not None:
            try:
                self._event_callback(name, data)
            except Exception:
                logger.debug("Peer mail event callback failed", exc_info=True)
