"""P3 read-only supervision count query tool (main plan §5 / frozen contract P0).

Data source: ``store.supervision_event_count`` (``store.py:952-961``) — the
committed ``supervision.*`` canonical event count.  Strictly read-only; no
input parameters.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from Agent.router_agent.tools.base import Tool, ToolResult

__all__ = ["QuerySupervisionTool"]


class QuerySupervisionTool(Tool):
    """Read-only supervision event counter for one scope.

    Output shape: ``{"count": N}`` where N is the committed number of
    ``supervision.*`` canonical events (non-supervision events are not
    counted).
    """

    name = "query_supervision"
    description = (
        "Query the committed supervision event count of the current scope. "
        "Read-only counter of supervision.* canonical events."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, store: Any | None = None, scope_id: str = "scope") -> None:
        self._store = store
        self._scope_id = scope_id

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._store is None:
            return ToolResult(
                success=False, error="query_supervision: store not wired"
            )
        count = self._store.supervision_event_count(self._scope_id)
        return ToolResult(success=True, content=json.dumps({"count": count}))
