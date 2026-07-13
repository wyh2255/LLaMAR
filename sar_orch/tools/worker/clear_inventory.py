"""Clear Inventory tool — drop all carried items."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class ClearInventoryTool(Tool):
    """Clear your entire inventory (drop all carried items)."""

    name = "clear_inventory"
    description = "Clear your entire inventory (drop all carried items)."
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, **kwargs) -> ToolResult:
        action = "ClearInventory()"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return tool_result_from_barrier(result)
