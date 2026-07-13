"""Navigate To tool — move toward any visible object by its ID."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class NavigateToTool(Tool):
    """Navigate to an object by its ID. Use this to move toward any visible object."""

    name = "navigate_to"
    description = (
        "Navigate to an object by its ID. Use this to move toward any visible object."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "target_id": {
                "type": "string",
                "description": "ID of the target object (e.g., WaterSource_1, GreatFire_Region_1)",
            },
        },
        "required": ["target_id"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, target_id: str, **kwargs) -> ToolResult:
        action = f"NavigateTo({target_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = result["observation"]
        agent = self._barrier.env.controller.get("agents", self._agent_idx)
        pos = agent.get_position()
        custom_content = (
            f"You have arrived at {target_id}.\n"
            f"Your position: ({pos[0]}, {pos[1]}, {pos[2]}).\n"
            f"{obs}"
        )
        return tool_result_from_barrier(result, overrides={"content": custom_content})
