"""pickup tool — pick up an object by alias.

Converts the human-readable alias to the raw AI2Thor objectId via
AliasRegistry before submitting the action.
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry


class PickupTool(Tool):
    """Pick up an object by its visible alias."""

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
        return "pickup"

    @property
    def description(self) -> str:
        return "Pick up an object by its visible alias."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "object_alias": {
                    "type": "string",
                    "description": "The visible alias of the object to pick up (e.g. Mug_1, Apple_2).",
                },
            },
            "required": ["object_alias"],
        }

    async def execute(self, *, object_alias: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        raw_id = self._alias_registry.raw(object_alias)
        if raw_id is None:
            return ToolResult(
                success=False,
                error=f"Unknown object alias: {object_alias}. Ensure the object is visible.",
            )

        action = f"PickupObject({raw_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error="" if result.success else f"Failed to pick up {object_alias}",
        )
