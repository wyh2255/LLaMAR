"""A2ASendMailTool -- worker zero-SAR-step tool for sending peer-to-peer mail.

Sends a signed MAIL envelope to another worker in the same team.
Delegates to ``WorkerPeerSenderService`` which validates the recipient
against the current team roster, signs with the peer shared secret, and
waits for a terminal ACK from the recipient worker's
``EnvelopeAwareAdapter``.

The sender must be injected at construction; lazy construction is not
supported because ``SARWorker`` already creates the sender only when
``enable_peer_mail=True``.

Security guarantees:
- Recipient must be in the same team (validated synchronously).
- Endpoint comes exclusively from team roster (never caller-supplied).
- Envelope is HMAC-signed with the peer team secret.
- Does NOT create a task on the coordinator (no ``TaskStore`` dispatch).
- Does NOT consume a SAR step.

Limitations:
- Sending to self is rejected.
- CancelTask is not available for peer mail -- see documentation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Agent.worker_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from a2a.worker.peer_sender import WorkerPeerSenderService

logger = logging.getLogger(__name__)


class A2ASendMailTool(Tool):
    """Send peer-to-peer mail to another worker in the same team.

    Delegates delivery to the WorkerPeerSenderService.  Does NOT consume
    a SAR step.
    """

    def __init__(self, sender: "WorkerPeerSenderService") -> None:
        if sender is None:
            raise TypeError("sender is required")
        self._sender = sender

    name = "a2a_send_mail"
    description = (
        "Send a mail message to another worker in your current team. "
        "The recipient must be a member of the same team (validated before sending). "
        "This is NOT a task assignment -- the recipient receives the message in "
        "their mailbox and responds at their discretion. "
        "Does NOT consume a SAR step."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": (
                        "Target worker ID (e.g. 'Bob'). Must be a current team member."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "Mail subject line (max 256 characters, may be empty)."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Mail body content (max 65536 characters, may be empty)."
                    ),
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
        # Parameter presence validation (deeper validation in sender)
        if not recipient_id or not recipient_id.strip():
            return ToolResult(
                success=False,
                content="recipient_id is required",
                data={"recipient_id": recipient_id},
            )

        try:
            result = await self._sender.send_mail(
                recipient_id=recipient_id,
                subject=subject,
                body=body,
            )
        except Exception as exc:
            logger.warning("a2a_send_mail to '%s' failed: %s", recipient_id, exc)
            return ToolResult(
                success=False,
                content=f"Failed to send mail: {exc}",
                data={
                    "recipient_id": recipient_id,
                    "outcome": "rejected",
                },
            )

        if result.get("success"):
            return ToolResult(
                success=True,
                content=(
                    f"Mail delivered to '{recipient_id}' "
                    f"(id={result.get('message_id', '?')})"
                ),
                data={
                    "recipient_id": recipient_id,
                    "message_id": result.get("message_id"),
                    "outcome": "sent",
                },
            )
        return ToolResult(
            success=False,
            content=(
                f"Failed to deliver mail to '{recipient_id}': "
                f"{result.get('error', 'unknown')}"
            ),
            data={
                "recipient_id": recipient_id,
                "outcome": "delivery_failed",
                "error": result.get("error", "unknown"),
            },
        )
