"""Get Supply tool — collect firefighting supplies from a reservoir or deposit."""

from Agent.router_agent.tools.base import Tool, ToolResult


class GetSupplyTool(Tool):
    """Collect firefighting supplies from a reservoir or deposit."""

    name = "get_supply"
    description = "Collect firefighting supplies from a reservoir or deposit."
    parameters: dict = {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "ID of the reservoir or deposit to get supply from",
            },
            "supply_type": {
                "type": "string",
                "enum": ["Water", "Sand"],
                "description": "Type of supply — Water or Sand",
            },
        },
        "required": ["source_id", "supply_type"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, source_id: str, supply_type: str, **kwargs) -> ToolResult:
        if "reservoir" in source_id.lower():
            action = f"GetSupply({source_id})"
        else:
            action = f"GetSupply({source_id}, {supply_type})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return ToolResult(success=True, content=result["observation"])
