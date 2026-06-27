"""Get Agent State tool — GPS-like query returning current position and status without stepping."""

from Agent.router_agent.tools.base import Tool, ToolResult


class GetAgentStateTool(Tool):
    """Query current agent position, inventory, and surroundings. Like a GPS module — does NOT consume a step."""

    name = "get_agent_state"
    description = (
        "Query your current position, inventory, and nearby objects. "
        "Use this to check where you are and what you're carrying. "
        "Does NOT consume a step — safe to call anytime."
    )
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, **kwargs) -> ToolResult:
        agent = self._barrier.env.controller.get("agents", self._agent_idx)
        pos = agent.get_position()
        inventory = self._barrier.env.controller.get_inventory(self._agent_idx)
        obs = self._barrier.get_current_obs(self._agent_idx)
        return ToolResult(
            success=True,
            content=(
                f"[GPS] Position: ({pos[0]}, {pos[1]}, {pos[2]}) | "
                f"Inventory: {inventory} | "
                f"Step: {self._barrier._step_counter}\n"
                f"{obs}"
            ),
        )
