"""SAR-specific RouterAgent tools — replaces map_server query_map with SARBarrier query_sar_state."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# -- MARoS import path setup ------------------------------------------------
_maros_my_a2a = Path("/home/wyh/daily_work/MARoS/my_a2a/src")
if str(_maros_my_a2a) not in sys.path:
    sys.path.insert(0, str(_maros_my_a2a))

from openharness_a2a.coordinator.router_tools.base import RouterTool, ToolResult


class QuerySARStateTool(RouterTool):
    """Router tool: query the current SAR environment state.

    Replaces MARoS's query_map tool. Queries SARBarrier directly instead of
    ROS map_server.  Extends RouterTool for full compatibility with the
    RouterAgent.register_tool() / get_tool_schemas() interface.
    """

    name = "query_sar_state"
    description = (
        "Get the current state of the Search & Rescue environment. "
        "Returns all fires (position, intensity, type), persons (position, status), "
        "reservoirs (position, resource_type), deposits, and agent states (position, inventory). "
        "Use this to understand the current situation before assigning subtasks."
    )
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier: Any) -> None:
        self._barrier = barrier

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the query and return formatted state as ToolResult."""
        try:
            snapshot = self._barrier.get_env_snapshot()
            return ToolResult(
                success=True,
                content=json.dumps(snapshot, indent=2, default=str),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"query_sar_state failed: {e}",
            )
