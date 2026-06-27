"""Carry Person tool — pick up a trapped person (needs >=2 agents)."""

from Agent.router_agent.tools.base import Tool, ToolResult


class CarryPersonTool(Tool):
    """Pick up a trapped person. At least 2 agents must carry simultaneously to succeed."""

    name = "carry_person"
    description = "Pick up a trapped person. At least 2 agents must carry simultaneously to succeed."
    parameters: dict = {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "string",
                "description": "ID of the person to carry (e.g., LostTimmy)",
            },
        },
        "required": ["person_id"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, person_id: str, **kwargs) -> ToolResult:
        action = f"Carry({person_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return ToolResult(success=True, content=result["observation"])
