"""Finish task tool for SAR coordinator."""

import inspect
from collections.abc import Awaitable, Callable

from Agent.router_agent.tools.base import Tool, ToolResult


class FinishTaskTool(Tool):
    """Call this when the overall SAR mission is complete."""

    name = "finish_task"
    description = (
        "Call this when the overall SAR mission is complete. "
        "Signals that all fires are extinguished and all persons are rescued."
    )
    parameters = {
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

    def __init__(
        self,
        store=None,
        completion_validator: Callable[[], bool | Awaitable[bool]] | None = None,
    ):
        """Initialize FinishTaskTool.

        Args:
            store: TaskStore instance (optional). If provided, mark_finished is called.
            completion_validator: Optional SAR truth predicate. It is evaluated
                only for success=True and must report that the mission is done.
        """
        self._store = store
        self._completion_validator = completion_validator

    async def execute(self, success: bool, summary: str) -> ToolResult:
        """Return a mission completion signal."""
        if success and self._completion_validator is not None:
            try:
                valid = self._completion_validator()
                if inspect.isawaitable(valid):
                    valid = await valid
            except Exception:
                valid = False
            if not valid:
                return ToolResult(
                    success=False,
                    content=(
                        "[MISSION NOT COMPLETE] The SAR completion truth check "
                        "reports that the mission is still unfinished. Continue working."
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
