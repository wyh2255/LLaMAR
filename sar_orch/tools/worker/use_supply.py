"""Use Supply tool — use firefighting supply on a fire to extinguish it."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class UseSupplyTool(Tool):
    """Use firefighting supply on a fire to extinguish it."""

    name = "use_supply"
    description = "Use firefighting supply on a fire to extinguish it."
    parameters: dict = {
        "type": "object",
        "properties": {
            "fire_id": {
                "type": "string",
                "description": "ID of the fire to extinguish (use _Region suffix, e.g., GreatFire_Region_1)",
            },
            "supply_type": {
                "type": "string",
                "enum": ["Water", "Sand"],
                "description": "Type of supply to use — Water or Sand",
            },
        },
        "required": ["fire_id", "supply_type"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, fire_id: str, supply_type: str, **kwargs) -> ToolResult:
        action = f"UseSupply({fire_id}, {supply_type})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return tool_result_from_barrier(result)
