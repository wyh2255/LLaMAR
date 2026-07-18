"""put tool — put a held object onto a receptacle by alias.

Converts the human-readable receptacle alias to the raw AI2Thor objectId
via AliasRegistry before submitting the action.
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry


class PutTool(Tool):
    """Put a held object onto a receptacle by its visible alias."""

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
        return "put"

    @property
    def description(self) -> str:
        return "Put a held object onto a receptacle by its visible alias."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "receptacle_alias": {
                    "type": "string",
                    "description": "The visible alias of the receptacle to put the object on (e.g. CounterTop_1, Table_2).",
                },
            },
            "required": ["receptacle_alias"],
        }

    async def execute(self, *, receptacle_alias: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        raw_id = self._alias_registry.raw(receptacle_alias)
        if raw_id is None:
            return ToolResult(
                success=False,
                error=f"Unknown receptacle alias: {receptacle_alias}. Ensure the receptacle is visible.",
            )

        action = f"PutObject({raw_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error="" if result.success else f"Failed to put object on {receptacle_alias}",
        )
