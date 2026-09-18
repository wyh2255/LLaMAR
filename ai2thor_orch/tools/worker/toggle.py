"""toggle tool — turn an object on or off by alias.

Converts the human-readable alias to the raw AI2Thor objectId via
AliasRegistry before submitting the action
(``ToggleObjectOn(objectId)`` / ``ToggleObjectOff(objectId)`` —
AI2Thor/object_actions.py:244-251 的 feasible 动词，单 objectId 参数；
``on: true | false`` 选择方向，与 ``open_close`` 的 ``action`` 参数同款)。
"""

from __future__ import annotations

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.tools.worker._barrier_helpers import action_failure_error
from ai2thor_orch.visibility import AliasRegistry


class ToggleTool(Tool):
    """Turn an object (e.g. Lamp, Faucet) on or off by its visible alias."""

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
        return "toggle"

    @property
    def description(self) -> str:
        return (
            "Turn an object on or off (e.g. Lamp, Faucet, StoveKnob) by its "
            "visible alias."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "object_alias": {
                    "type": "string",
                    "description": "The visible alias of the object to turn on or off (e.g. Lamp_1, Faucet_2).",
                },
                "on": {
                    "type": "boolean",
                    "description": "True to turn the object on, False to turn it off.",
                },
            },
            "required": ["object_alias", "on"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        object_alias: str,
        on: bool,
        **kwargs: Any,
    ) -> ToolResult:
        if not isinstance(object_alias, str) or not object_alias.strip():
            raise ValueError(
                f"toggle: 'object_alias' must be a non-empty string, got {object_alias!r}"
            )
        if not isinstance(on, bool):
            # 工具参数契约（PA-W2 卡）：非法参数一律 ValueError（测试断言
            # ValueError；这是工具输入校验而非类型分发）。
            raise ValueError(  # noqa: TRY004 - 参数契约口径
                f"toggle: 'on' must be a boolean (True/False), got {on!r}"
            )

        raw_id = self._alias_registry.raw(object_alias)
        if raw_id is None:
            return ToolResult(
                success=False,
                error=f"Unknown object alias: {object_alias}. Ensure the object is visible.",
            )

        if on:
            action = f"ToggleObjectOn({raw_id})"
            verb = "turn on"
        else:
            action = f"ToggleObjectOff({raw_id})"
            verb = "turn off"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = self._alias_registry.redact(result.observation)

        return ToolResult(
            success=result.success,
            content=obs,
            error=""
            if result.success
            else action_failure_error(f"Failed to {verb} {object_alias}", result),
        )
