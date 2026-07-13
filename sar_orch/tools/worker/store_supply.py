"""Store Supply tool — store current supplies at a deposit."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class StoreSupplyTool(Tool):
    """Store your current supplies at a deposit."""

    name = "store_supply"
    description = "Store your current supplies at a deposit."
    parameters: dict = {
        "type": "object",
        "properties": {
            "deposit_id": {
                "type": "string",
                "description": "ID of the deposit to store supplies at",
            },
        },
        "required": ["deposit_id"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, deposit_id: str, **kwargs) -> ToolResult:
        action = f"StoreSupply({deposit_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return tool_result_from_barrier(result)
