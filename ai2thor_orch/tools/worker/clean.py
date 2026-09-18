"""clean tool — clean a dirty object by alias.

Converts the human-readable alias to the raw AI2Thor objectId via
AliasRegistry before submitting the action (``CleanObject(objectId)`` —
AI2Thor/object_actions.py:258-263 的 feasible 动词，单 objectId 参数）。

真机语义：``CleanObject`` 要求在 SinkBasin 旁（原版 feasibility 由
``get_dirty_objects`` + 环境状态判定）。本工具**不做前置校验**——失败文案
（含 Unity 侧原始 errorMessage）原样经 :func:`action_failure_error` 回传，
由 worker 依错误自省调整（与 pickup 的不可见失败同款）。
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.tools.worker._barrier_helpers import action_failure_error
from ai2thor_orch.visibility import AliasRegistry


class CleanTool(Tool):
    """Clean a dirty object (e.g. Mug, Plate) by its visible alias."""

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
        return "clean"

    @property
    def description(self) -> str:
        return (
            "Clean a dirty object (e.g. Mug, Plate) by its visible alias. "
            "Stand beside a SinkBasin first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "object_alias": {
                    "type": "string",
                    "description": "The visible alias of the object to clean (e.g. Mug_1, Plate_2).",
                },
            },
            "required": ["object_alias"],
        }

    async def execute(self, *, object_alias: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        if not isinstance(object_alias, str) or not object_alias.strip():
            raise ValueError(
                f"clean: 'object_alias' must be a non-empty string, got {object_alias!r}"
            )

        raw_id = self._alias_registry.raw(object_alias)
        if raw_id is None:
            return ToolResult(
                success=False,
                error=f"Unknown object alias: {object_alias}. Ensure the object is visible.",
            )

        action = f"CleanObject({raw_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error=""
            if result.success
            else action_failure_error(f"Failed to clean {object_alias}", result),
        )
