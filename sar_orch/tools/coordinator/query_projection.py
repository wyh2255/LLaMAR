"""P3 read-only projection query tool (main plan §5 / frozen contract P0).

Data source: ``MemoryReadPort.spatial_snapshot`` / ``embodied_snapshot``
(``sar_orch/environment_state_provider.py:109-115``).  The tool is strictly
read-only — executing it performs zero writes (frozen contract asserts
revision / temporal counts are unchanged).  The schema exposes only
``domain`` (required enum) and ``entity_id`` (optional) — no source-state
selection parameters (B8 / H1 spirit).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.environment_state_provider import MemoryReadPort

__all__ = ["QueryProjectionTool"]


class QueryProjectionTool(Tool):
    """Read-only canonical memory projection (spatial | embodied) for one scope.

    Output shape: ``entity_id -> {entity_type, fields: {field_name: row}}``
    where each row carries value / env_step / provenance / confidence /
    outcome / evidence_id / sequence.
    """

    name = "query_projection"
    description = (
        "Query the committed memory projection of the current scope "
        "(spatial or embodied), optionally narrowed to one entity. "
        "Read-only view of committed evidence; exposes no source-state "
        "selection parameters."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": ["spatial", "embodied"],
                "description": "Projection domain to read.",
            },
            "entity_id": {
                "type": "string",
                "description": (
                    "Optional entity id; when omitted the full projection "
                    "of the domain is returned."
                ),
            },
        },
        "required": ["domain"],
    }

    def __init__(self, store: Any | None = None, scope_id: str = "scope") -> None:
        self._store = store
        self._scope_id = scope_id

    async def execute(
        self, domain: str | None = None, entity_id: str | None = None, **kwargs: Any
    ) -> ToolResult:
        if self._store is None:
            return ToolResult(
                success=False, error="query_projection: store not wired"
            )
        if domain not in ("spatial", "embodied"):
            return ToolResult(
                success=False,
                error=(
                    f"query_projection: domain must be 'spatial' or 'embodied', "
                    f"got {domain!r}"
                ),
            )
        port = MemoryReadPort(self._store, self._scope_id)
        data = (
            port.spatial_snapshot() if domain == "spatial" else port.embodied_snapshot()
        )
        if entity_id is not None:
            data = {entity_id: data[entity_id]} if entity_id in data else {}
        return ToolResult(
            success=True,
            content=json.dumps(data, ensure_ascii=False, default=str),
        )
