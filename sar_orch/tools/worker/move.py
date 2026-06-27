"""Move tool — move one step in a cardinal direction or diagonal."""

from Agent.router_agent.tools.base import Tool, ToolResult


class MoveTool(Tool):
    """Move one step in a cardinal direction or diagonal."""

    name = "move"
    description = "Move one step in a cardinal direction or diagonal."
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
                    "UpLeft",
                    "UpRight",
                    "DownLeft",
                    "DownRight",
                    "Center",
                ],
                "description": "Direction to move — Up, Down, Left, Right, UpLeft, UpRight, DownLeft, DownRight, Center",
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
        return ToolResult(success=True, content=result["observation"])
