"""P3 read-only control journal query tool (main plan §5 / frozen contract P0).

Data source: ``store.control_journal_entries`` (``store.py:518-528``),
optionally filtered by ``dispatch_id``.  Strictly read-only; schema exposes
only the optional ``dispatch_id`` filter.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, ClassVar

from Agent.router_agent.tools.base import Tool, ToolResult

__all__ = ["QueryControlJournalTool"]


class QueryControlJournalTool(Tool):
    """Read-only control transition journal reader for one scope.

    Output: a list of journal entries, each carrying at least dispatch_id /
    state / source / control_revision / journal_sha256.  ``dispatch_id``
    narrows the view to one dispatch lineage.
    """

    name = "query_control_journal"
    description = (
        "Query the control transition journal of the current scope, "
        "optionally narrowed by dispatch id. Read-only journal entries."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "dispatch_id": {
                "type": "string",
                "description": (
                    "Optional dispatch id; when omitted all journal entries "
                    "of the scope are returned."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, store: Any | None = None, scope_id: str = "scope") -> None:
        self._store = store
        self._scope_id = scope_id

    async def execute(
        self, dispatch_id: str | None = None, **kwargs: Any
    ) -> ToolResult:
        if self._store is None:
            return ToolResult(
                success=False, error="query_control_journal: store not wired"
            )
        entries = self._store.control_journal_entries(dispatch_id=dispatch_id)
        rows = [asdict(entry) for entry in entries]
        return ToolResult(
            success=True,
            content=json.dumps(rows, ensure_ascii=False, default=str),
        )
