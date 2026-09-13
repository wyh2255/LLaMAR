"""P3 read-only temporal flow query tool (main plan §5 / R6 / frozen contract P0).

Data source: ``store.temporal_events`` (``store.py:834-841``), sequence
ascending.  **View filter (R6):** events whose ``event_type`` starts with
``diagnosis.`` are excluded — diagnosis audit events are for evaluation /
audit only and never enter the reviewer input.  Strictly read-only; schema
exposes only the optional ``after_sequence`` cursor.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from Agent.router_agent.tools.base import Tool, ToolResult

__all__ = ["QueryTemporalFlowTool"]


class QueryTemporalFlowTool(Tool):
    """Read-only committed temporal event flow of one scope, sequence ascending.

    Each returned event carries at least event_id / sequence / event_type /
    actor_id / payload.  ``after_sequence`` narrows the view to events with a
    strictly greater sequence (incremental consumption).
    """

    name = "query_temporal_flow"
    description = (
        "Query the committed temporal event flow of the current scope in "
        "sequence order, optionally after a sequence cursor. Read-only view; "
        "diagnostic audit events are excluded from this view."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "after_sequence": {
                "type": "integer",
                "description": (
                    "Optional cursor; only events with sequence strictly "
                    "greater than this value are returned."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, store: Any | None = None, scope_id: str = "scope") -> None:
        self._store = store
        self._scope_id = scope_id

    async def execute(
        self, after_sequence: int | None = None, **kwargs: Any
    ) -> ToolResult:
        if self._store is None:
            return ToolResult(
                success=False, error="query_temporal_flow: store not wired"
            )
        cursor = int(after_sequence) if after_sequence is not None else 0
        events = [
            evt
            for evt in self._store.temporal_events(self._scope_id)
            if evt["sequence"] > cursor
            and not str(evt.get("event_type", "")).startswith("diagnosis.")
        ]
        return ToolResult(
            success=True,
            content=json.dumps(events, ensure_ascii=False, default=str),
        )
