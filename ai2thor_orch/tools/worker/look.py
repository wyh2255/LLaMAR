"""look tool — look up or down.

Action mapping:
  up   → LookUp(30)
  down → LookDown(30)

The default angle of 30 degrees matches the standard AI2Thor convention.
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry

_LOOK_ANGLE = "30"

_DIRECTION_MAP: dict[str, str] = {
    "up": f"LookUp({_LOOK_ANGLE})",
    "down": f"LookDown({_LOOK_ANGLE})",
}


class LookTool(Tool):
    """Look the agent up or down."""

    def __init__(
        self,
        barrier: AI2ThorBarrier,
        agent_idx: int,
        alias_registry: AliasRegistry,
    ) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx
        self._alias_registry = alias_registry

    @property
    def name(self) -> str:
        return "look"

    @property
    def description(self) -> str:
        return "Look up or down to change the camera angle."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to look.",
                },
            },
            "required": ["direction"],
        }

    async def execute(self, *, direction: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        action = _DIRECTION_MAP.get(direction)
        if action is None:
            return ToolResult(
                success=False,
                error=f"Invalid direction: {direction}. Must be one of {list(_DIRECTION_MAP.keys())}.",
            )

        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error="" if result.success else f"Action {action} failed",
        )
