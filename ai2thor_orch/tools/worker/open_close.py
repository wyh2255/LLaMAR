"""open_close tool — open or close an object by alias.

Converts the human-readable alias to the raw AI2Thor objectId via
AliasRegistry before submitting the action.
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry


class OpenCloseTool(Tool):
    """Open or close an object (e.g., fridge, cabinet) by its visible alias."""

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
        return "open_close"

    @property
    def description(self) -> str:
        return "Open or close an object (e.g., fridge, cabinet) by its visible alias."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "object_alias": {
                    "type": "string",
                    "description": "The visible alias of the object to open or close (e.g. Fridge_1, Cabinet_2).",
                },
                "action": {
                    "type": "string",
                    "enum": ["open", "close"],
                    "description": "Whether to open or close the object.",
                },
            },
            "required": ["object_alias", "action"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        object_alias: str,
        action: str,
        **kwargs: Any,
    ) -> ToolResult:
        raw_id = self._alias_registry.raw(object_alias)
        if raw_id is None:
            return ToolResult(
                success=False,
                error=f"Unknown object alias: {object_alias}. Ensure the object is visible.",
            )

        if action == "open":
            action_str = f"OpenObject({raw_id})"
        elif action == "close":
            action_str = f"CloseObject({raw_id})"
        else:
            return ToolResult(
                success=False,
                error=f"Invalid action: {action}. Must be 'open' or 'close'.",
            )

        result = await self._barrier.submit_action(self._agent_idx, action_str)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error="" if result.success else f"Failed to {action} {object_alias}",
        )
