"""finish_task tool — coordinator mission-completion signal for AI2Thor.

语义与 SAR 版 ``sar_orch/tools/coordinator/finish_task.py`` 对齐（verifier /
ledger 两条契约）：

- **verifier**（完成判定）：``completion_validator`` 只在 ``success=True`` 时
  求值，且必须报告任务确实完成；不通过则以 ``mission_not_finished`` 拒绝，
  协调器继续工作（fail-closed：validator 抛错 → 视为未完成）。
- **ledger**（记账）：``store`` 若提供 ``mark_finished(success)`` 则调用之
  （mission 级终态写入既有任务存储）。

AI2Thor 侧的完成真值由 EnvPack 装配时注入：``Ai2ThorEnvPack`` 把内核
``completion_validator``（barrier.is_finished 口径）替换为**现有 verifier
语义**的判定（``ai2thor_orch.verifier.verify_round`` 对 barrier 最近回合做
postcondition 校验）；环境真值不可达时回退内核 validator。工具本体与 SAR
逐行同构，判定来源与环境的解耦发生在装配处。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult


class FinishTaskTool(Tool):
    """Call this when the overall AI2Thor mission is complete."""

    def __init__(
        self,
        store: Any = None,
        *,
        completion_validator: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> None:
        """Initialize FinishTaskTool.

        Args:
            store: Mission task store (optional).  If provided and it exposes
                ``mark_finished``, the terminal mission verdict is recorded.
            completion_validator: Completion truth predicate.  Evaluated only
                for ``success=True`` and must report the mission is done
                (AI2Thor: existing verifier semantics, injected by the
                EnvPack).  ``None`` → no environment truth available, the
                success claim is taken as-is (SAR tool parity).
        """
        self._store = store
        self._completion_validator = completion_validator

    @property
    def name(self) -> str:
        return "finish_task"

    @property
    def description(self) -> str:
        return (
            "Call this when the overall AI2Thor mission is complete. "
            "Signals that the task objective has been achieved (or, with "
            "success=false, that the mission failed and no further actions "
            "are needed)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "Whether the overall mission succeeded",
                },
                "summary": {
                    "type": "string",
                    "description": "Mission summary",
                },
            },
            "required": ["success", "summary"],
        }

    async def execute(self, success: bool, summary: str) -> ToolResult:  # type: ignore[override]
        """Return a mission completion signal.

        ``success=True`` is accepted only when the injected completion truth
        check (existing AI2Thor verifier semantics) reports the mission is
        actually done; otherwise the call is rejected so the coordinator
        keeps working.  ``success=False`` is an honest mission failure and
        always terminal.
        """
        if success and self._completion_validator is not None:
            try:
                valid = self._completion_validator()
                if inspect.isawaitable(valid):
                    valid = await valid
            except Exception:  # noqa: BLE001 - fail closed: truth unavailable ≠ complete
                valid = False
            if not valid:
                return ToolResult(
                    success=False,
                    content=(
                        "[MISSION NOT COMPLETE] The AI2Thor completion truth "
                        "check reports that the mission is still unfinished. "
                        "Continue working."
                    ),
                    error="mission_not_finished",
                )
        if self._store is not None and hasattr(self._store, "mark_finished"):
            self._store.mark_finished(success)
        return ToolResult(
            success=True,
            content=f"[MISSION COMPLETE] {summary}",
            task_complete=True,
            mission_success=success,
        )
