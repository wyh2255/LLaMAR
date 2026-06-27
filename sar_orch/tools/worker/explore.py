"""Explore tool — explore unknown surrounding area, multiple steps at once."""

from Agent.router_agent.tools.base import Tool, ToolResult


class ExploreTool(Tool):
    """Explore unknown surrounding area. Multiple steps at once."""

    name = "explore"
    description = "Explore unknown surrounding area. Multiple steps at once."
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, **kwargs) -> ToolResult:
        action = "Explore()"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return ToolResult(success=True, content=result["observation"])
