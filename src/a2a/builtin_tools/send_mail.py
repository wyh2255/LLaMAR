"""SendMailTool — Coordinator sends signed mail to a worker.

No TaskStore dispatch_id or Watchdog state is created on the
coordinator side.  The worker's EnvelopeAwareAdapter delivers
the mail to the local mailbox without running an agent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Agent.router_agent.tools.base import Tool, ToolResult

from a2a.coordinator.sender_service import CoordinatorSenderService

if TYPE_CHECKING:
    from a2a.coordinator.agent_registry import AgentRegistry
    from a2a.coordinator.worker_registry import WorkerRegistry

logger = logging.getLogger(__name__)

MAX_SUBJECT_LENGTH = 256
MAX_BODY_LENGTH = 65536


class SendMailTool(Tool):
    """Coordinator sends mail to a worker.

    Validates the recipient is registered and online, then sends a
    signed MAIL envelope via the A2A sender and waits for terminal
    delivery ACK.
    """

    def __init__(
        self,
        sender: CoordinatorSenderService,
        worker_registry: WorkerRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
    ):
        self._sender = sender
        self._worker_registry = worker_registry
        self._agent_registry = agent_registry

    @property
    def name(self) -> str:
        return "send_mail"

    @property
    def description(self) -> str:
        return (
            "Send a mail message to a worker. "
            "Recipient must be a registered worker. "
            "The worker receives the mail in their mailbox — no task is created."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Target worker ID (e.g. 'Alice')",
                },
                "subject": {
                    "type": "string",
                    "description": "Mail subject line (max 256 chars)",
                },
                "body": {
                    "type": "string",
                    "description": "Mail body content (max 65536 chars)",
                },
            },
            "required": ["recipient_id", "subject", "body"],
        }

    async def execute(
        self,
        recipient_id: str,
        subject: str,
        body: str,
    ) -> ToolResult:
        if not recipient_id:
            return ToolResult(
                success=False,
                content="recipient_id is required",
                error="missing_recipient",
            )
        if not subject or not subject.strip():
            return ToolResult(
                success=False,
                content="subject is required",
                error="missing_subject",
            )
        if not body:
            return ToolResult(
                success=False,
                content="body is required",
                error="missing_body",
            )
        if len(subject) > MAX_SUBJECT_LENGTH:
            return ToolResult(
                success=False,
                content=f"subject exceeds {MAX_SUBJECT_LENGTH} characters",
                error="subject_too_long",
            )
        if len(body) > MAX_BODY_LENGTH:
            return ToolResult(
                success=False,
                content=f"body exceeds {MAX_BODY_LENGTH} characters",
                error="body_too_long",
            )

        # Resolve endpoint (fail-closed: registry required)
        endpoint: str | None = None
        if self._worker_registry is not None:
            try:
                w = self._worker_registry.get(recipient_id)
                from a2a.shared.types import WorkerStatus

                if w.status == WorkerStatus.OFFLINE:
                    return ToolResult(
                        success=False,
                        content=f"Worker '{recipient_id}' is OFFLINE",
                        error="worker_offline",
                    )
                endpoint = w.a2a_endpoint
            except Exception:
                pass
        if endpoint is None and self._agent_registry is not None:
            try:
                a = self._agent_registry.get(recipient_id)
                if a is not None:
                    endpoint = a.endpoint
            except Exception:
                pass
        if endpoint is None:
            return ToolResult(
                success=False,
                content=f"Recipient '{recipient_id}' not found in registry",
                error="recipient_not_found",
            )

        result = await self._sender.send_mail(
            recipient_id=recipient_id,
            recipient_endpoint=endpoint,
            subject=subject,
            body=body,
        )

        if result.get("success"):
            return ToolResult(
                success=True,
                content=f"Mail delivered to '{recipient_id}' — ACK received",
            )
        return ToolResult(
            success=False,
            content=f"Failed to deliver mail to '{recipient_id}': {result.get('error', 'unknown')}",
            error="send_failed",
        )
