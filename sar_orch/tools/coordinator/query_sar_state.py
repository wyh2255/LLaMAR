"""Query SAR state tool for the coordinator."""

import json

from Agent.router_agent.tools.base import Tool, ToolResult


class QuerySARStateTool(Tool):
    """Tool that queries the current state of the SAR environment."""

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

    def __init__(self, barrier):
        self._barrier = barrier

    async def execute(self, **kwargs) -> ToolResult:
        snapshot = self._barrier.get_env_snapshot()
        return ToolResult(success=True, content=json.dumps(snapshot, indent=2, default=str))
