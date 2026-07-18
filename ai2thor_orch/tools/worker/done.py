"""done tool — signal task completion."""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.visibility import AliasRegistry


class DoneTool(Tool):
    """Signal that the current task is complete."""

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
        return "done"

    @property
    def description(self) -> str:
        return "Signal that the current task is complete and no further actions are needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        result = await self._barrier.submit_action(self._agent_idx, "Done")
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            task_complete=True,
            error="" if result.success else "Done action failed",
        )
