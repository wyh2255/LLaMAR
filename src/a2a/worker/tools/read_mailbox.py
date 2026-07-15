"""ReadMailboxTool -- zero-SAR-step worker tool for reading mailbox messages.

Atomically selects unread messages and marks exactly those returned as read.
Returns structured data with subject/body/sender/sent_at/team metadata.
Does NOT call barrier.submit_action -- does NOT consume a SAR step.
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.worker.mailbox_store import WorkerMailboxStore, InvalidMailboxParameter
from Agent.worker_agent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

MAX_RETURNED_BODY_LENGTH = 500
DEFAULT_READ_LIMIT = 20


def _truncate_body(
    body: str, max_len: int = MAX_RETURNED_BODY_LENGTH
) -> tuple[str, bool, int]:
    """Return (body_or_preview, was_truncated, original_length)."""
    original_len = len(body)
    if original_len <= max_len:
        return (body, False, original_len)
    return (body[:max_len] + "...", True, original_len)


class ReadMailboxTool(Tool):
    """Read unread mailbox messages.

    Atomically marks returned messages as read. Does NOT consume a SAR step.
    """

    name = "read_mailbox"
    description = (
        "Read your unread mailbox messages. "
        "Optionally filter by specific message_ids or set a limit. "
        "Returned messages are automatically marked as read. "
        "Does NOT consume a SAR step - safe to call anytime."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of specific message IDs to read. "
                        "If omitted, reads unread messages."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (1..100).",
                    "default": DEFAULT_READ_LIMIT,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
        }

    def __init__(self, mailbox: WorkerMailboxStore) -> None:
        self._mailbox = mailbox

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_message_ids: Any = kwargs.get("message_ids")
        raw_limit: Any = kwargs.get("limit", DEFAULT_READ_LIMIT)

        message_ids: list[str] | None = None
        if raw_message_ids is not None:
            if not isinstance(raw_message_ids, list):
                return ToolResult(
                    success=False,
                    content="Invalid message_ids: must be a list of strings.",
                )
            nonblank = [m for m in raw_message_ids if isinstance(m, str) and m.strip()]
            if len(nonblank) != len(raw_message_ids):
                return ToolResult(
                    success=False,
                    content="Invalid message_ids: each ID must be a non-blank string.",
                )
            seen: set[str] = set()
            deduped: list[str] = []
            for mid in nonblank:
                if mid not in seen:
                    seen.add(mid)
                    deduped.append(mid)
            if not deduped:
                return ToolResult(
                    success=True,
                    content="No unread messages.",
                    data={"messages": [], "count": 0},
                )
            message_ids = deduped

        if raw_limit is not None:
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                return ToolResult(
                    success=False,
                    content="Invalid limit: must be an integer.",
                )
            if raw_limit < 1 or raw_limit > 100:
                return ToolResult(
                    success=False,
                    content="Invalid limit: must be between 1 and 100.",
                )

        try:
            records = self._mailbox.read_unread(
                limit=raw_limit,
                message_ids=message_ids,
                oldest_first=(message_ids is None),
            )
        except InvalidMailboxParameter as exc:
            return ToolResult(success=False, content=str(exc))

        if not records:
            return ToolResult(
                success=True,
                content="No unread messages.",
                data={"messages": [], "count": 0},
            )

        lines: list[str] = []
        result_data: list[dict[str, Any]] = []
        for r in records:
            body_preview, was_truncated, original_len = _truncate_body(r.body)
            team_info = ""
            if r.team_id:
                team_info = f" [team={r.team_id} epoch={r.team_epoch}]"
            lines.append(
                f"[{r.message_id}] From: {r.sender_id}{team_info} "
                f"Subject: {r.subject} Sent: {r.sent_at}\n"
                f"Body: {body_preview}"
            )
            result_data.append(
                {
                    "message_id": r.message_id,
                    "sender_id": r.sender_id,
                    "subject": r.subject,
                    "body": body_preview,
                    "body_truncated": was_truncated,
                    "body_original_length": original_len,
                    "sent_at": r.sent_at,
                    "received_at": r.received_at,
                    "team_id": r.team_id,
                    "team_epoch": r.team_epoch,
                }
            )

        return ToolResult(
            success=True,
            content=f"Read {len(records)} message(s):\n" + "\n---\n".join(lines),
            data={"messages": result_data, "count": len(records)},
        )
