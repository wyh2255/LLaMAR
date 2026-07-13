"""Drop Off Person tool — drop a carried person at a safe deposit location."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class DropOffPersonTool(Tool):
    """Drop a carried person at a safe deposit location."""

    name = "drop_off_person"
    description = "Drop a carried person at a safe deposit location."
    parameters: dict = {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "string",
                "description": "ID of the person being carried",
            },
            "deposit_id": {
                "type": "string",
                "description": "ID of the deposit to drop them at",
            },
        },
        "required": ["person_id", "deposit_id"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, person_id: str, deposit_id: str, **kwargs) -> ToolResult:
        action = f"DropOff({deposit_id}, {person_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return tool_result_from_barrier(result)
