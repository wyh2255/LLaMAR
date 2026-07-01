"""No-Op tool — do nothing this step. Use when waiting or task is complete."""

from Agent.router_agent.tools.base import Tool, ToolResult


class NoOpTool(Tool):
    """Do nothing this step. Use when waiting for other agents or when your task is complete."""

    name = "no_op"
    description = "Do nothing this step. Use when waiting for other agents or when your task is complete."
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, **kwargs) -> ToolResult:
        try:
            result = await self._barrier.submit_action(self._agent_idx, "NoOp")
            content = result["observation"]
            if result.get("finished"):
                content += (
                    "\n[MISSION COMPLETE] All objectives achieved. "
                    "Return a success summary now."
                )
            else:
                content += (
                    f"\n[Step {result.get('step', '?')}] "
                    "Mission in progress — other agents may still be working. "
                    "Keep calling no_op() to stay synchronized."
                )
            return ToolResult(success=True, content=content)
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))
