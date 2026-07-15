"""Coordinator A2A sender service for signed envelope delivery.

Reusable by coordinator tools to send signed MAIL, TEAM_UPDATE,
TEAM_REVOKE, and TASK envelopes to workers via the A2A SDK.

Ordinary mail and team control messages do NOT create TaskStore
dispatch_id or Watchdog state the worker's EnvelopeAwareAdapter
handles them locally without running an agent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
EXPIRES_DELTA_SECONDS = 60.0


class SendError(Exception):
    """Envelope delivery failed."""


# Status helpers are in a2a.shared.response_parser


class CoordinatorSenderService:
    """Send signed envelopes to workers via A2A SDK."""

    def __init__(
        self,
        coordinator_secret: bytes,
        coordinator_id: str = "Coordinator",
        *,
        httpx_client: httpx.AsyncClient | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ):
        if not coordinator_secret:
            raise ValueError("coordinator_secret must be non-empty")
        if default_timeout <= 0:
            raise ValueError("default_timeout must be > 0")
        self._secret = coordinator_secret
        self._coordinator_id = coordinator_id
        self._httpx_client = httpx_client
        self._default_timeout = default_timeout
        self._client_cache: dict[str, Client] = {}

    def _resolve_timeout(self, timeout: float | None) -> float:
        t = timeout if timeout is not None else self._default_timeout
        if t <= 0:
            raise ValueError(f"timeout must be > 0, got {t}")
        return t

    def _sign_envelope(
        self, envelope: MessageEnvelope, envelope_json: str
    ) -> tuple[MessageEnvelope, str]:
        """Idempotent sign — sign_envelope modifies in-place and returns."""
        sign_envelope(envelope, self._secret)
        return envelope, envelope.model_dump_json()

    async def send_control(
        self,
        envelope: MessageEnvelope,
        worker_endpoint: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a control/mail envelope and wait for terminal delivery.

        Only ``task.status`` and ``status_update.status`` are inspected
        for terminal state; ``artifact_update`` and ``message`` are not
        treated as implicit completion.

        Returns:
            ``{"success": True, "transport_task_id": "<id>"}`` on COMPLETED,
            ``{"success": False, "error": "<reason>",
              "task_status": "<state>"}`` on terminal failure or timeout.
        """
        effective_timeout = self._resolve_timeout(timeout)
        sign_envelope(envelope, self._secret)
        env_json = envelope.model_dump_json()
        client = await self._get_client(worker_endpoint)
        message = Message(role=Role.ROLE_USER, parts=[Part(text=env_json)])
        request = SendMessageRequest(message=message)
        request.configuration.return_immediately = False

        try:
            result = await parse_stream_for_terminal(
                client.send_message(request), effective_timeout
            )
            # Remap transport_task_id -> task_id for backward compat
            if "transport_task_id" in result:
                result["task_id"] = result.pop("transport_task_id")
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout after {effective_timeout}s"}
        except Exception as exc:
            logger.warning("send_control to %s failed: %s", worker_endpoint, exc)
            return {"success": False, "error": str(exc)}

    async def send_envelope(
        self,
        envelope_json: str,
        worker_endpoint: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a pre-built signed envelope JSON with return_immediately=True.

        Returns immediately with the task ID if accepted.
        Does NOT wait for terminal delivery.
        """
        effective_timeout = self._resolve_timeout(timeout)
        client = await self._get_client(worker_endpoint)
        message = Message(role=Role.ROLE_USER, parts=[Part(text=envelope_json)])
        request = SendMessageRequest(message=message)
        request.configuration.return_immediately = True

        try:

            async def _send():
                async for stream_response in client.send_message(request):
                    if stream_response.HasField("task"):
                        return {"success": True, "task_id": stream_response.task.id}
                return {"success": False, "error": "No task in response"}

            return await asyncio.wait_for(_send(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout after {effective_timeout}s"}
        except Exception as exc:
            logger.warning("send_envelope to %s failed: %s", worker_endpoint, exc)
            return {"success": False, "error": str(exc)}

    # ── high-level helpers ─────────────────────────────────────────────

    def _make_envelope(
        self,
        kind: MessageKind,
        recipient_id: str,
        body: dict[str, Any],
        *,
        team_id: str | None = None,
        team_epoch: int | None = None,
    ) -> MessageEnvelope:
        sent_at = datetime.now(timezone.utc)
        expires_at = sent_at + timedelta(seconds=EXPIRES_DELTA_SECONDS)
        return MessageEnvelope(
            kind=kind,
            sender_id=self._coordinator_id,
            recipient_id=recipient_id,
            body=body,
            team_id=team_id,
            team_epoch=team_epoch,
            message_id=uuid.uuid4().hex,
            sent_at=sent_at,
            expires_at=expires_at,
        )

    async def send_mail(
        self,
        recipient_id: str,
        recipient_endpoint: str,
        subject: str,
        body: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        envelope = self._make_envelope(
            MessageKind.MAIL,
            recipient_id,
            {"subject": subject, "content": body},
        )
        return await self.send_control(envelope, recipient_endpoint, timeout=timeout)

    async def send_team_update(
        self,
        worker_id: str,
        worker_endpoint: str,
        team_id: str,
        epoch: int,
        members: list[str],
        endpoints: dict[str, str],
        team_secret: bytes,
        *,
        objective: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "team_id": team_id,
            "epoch": epoch,
            "members": members,
            "endpoints": endpoints,
            "team_secret": team_secret.hex(),
        }
        if objective is not None:
            body["objective"] = objective
        envelope = self._make_envelope(
            MessageKind.TEAM_UPDATE,
            worker_id,
            body,
            team_id=team_id,
            team_epoch=epoch,
        )
        return await self.send_control(envelope, worker_endpoint, timeout=timeout)

    async def send_team_revoke(
        self,
        worker_id: str,
        worker_endpoint: str,
        team_id: str,
        epoch: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        envelope = self._make_envelope(
            MessageKind.TEAM_REVOKE,
            worker_id,
            {"team_id": team_id, "epoch": epoch},
            team_id=team_id,
            team_epoch=epoch,
        )
        return await self.send_control(envelope, worker_endpoint, timeout=timeout)

    async def send_signed_task(
        self,
        recipient_id: str,
        recipient_endpoint: str,
        prompt: str,
        *,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a signed TASK envelope (return_immediately=True for TaskStore compat).

        Only effective when the worker's EnvelopeIngress has
        ``allow_legacy_tasks=False``, which forces envelope verification.
        """
        body: dict[str, Any] = {"content": prompt}
        if task_id is not None:
            body["task_id"] = task_id
        envelope = self._make_envelope(MessageKind.TASK, recipient_id, body)
        sign_envelope(envelope, self._secret)
        return await self.send_envelope(
            envelope.model_dump_json(), recipient_endpoint, timeout=timeout
        )

    # ── SDK client management ──────────────────────────────────────────

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

    async def close(self) -> None:
        for client in self._client_cache.values():
            try:
                await client.close()
            except Exception:
                pass
        self._client_cache.clear()
