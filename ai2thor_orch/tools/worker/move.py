"""move tool — move agent in a cardinal direction.

Action mapping:
  ahead → MoveAhead
  back  → MoveBack
  left  → MoveLeft
  right → MoveRight
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry

_DIRECTION_MAP: dict[str, str] = {
    "ahead": "MoveAhead",
    "back": "MoveBack",
    "left": "MoveLeft",
    "right": "MoveRight",
}


class MoveTool(Tool):
    """Move the agent in a cardinal direction."""

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
        return "move"

    @property
    def description(self) -> str:
        return "Move the agent in a cardinal direction (ahead, back, left, right)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["ahead", "back", "left", "right"],
                    "description": "Direction to move.",
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
