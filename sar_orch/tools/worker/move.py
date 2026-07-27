"""Move tool — move one step in a cardinal direction."""

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier


class MoveTool(Tool):
    """Move one step in a cardinal direction."""

    name = "move"
    description = "Move one step in a cardinal direction."
    parameters: dict = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": [
                    "Up",
                    "Down",
                    "Left",
                    "Right",
                    "Center",
                ],
                "description": "Direction to move — Up, Down, Left, Right, Center",
            },
        },
        "required": ["direction"],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, direction: str, **kwargs) -> ToolResult:
        action = f"Move({direction})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        return tool_result_from_barrier(result)
