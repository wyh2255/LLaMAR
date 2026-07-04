"""Finish task tool for SAR coordinator."""

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

    def __init__(self, store=None):
        """Initialize FinishTaskTool.

        Args:
            store: TaskStore instance (optional). If provided, mark_finished is called.
        """
        self._store = store

    async def execute(self, success: bool, summary: str) -> ToolResult:
        """Return a mission completion signal."""
        if self._store is not None and hasattr(self._store, "mark_finished"):
            self._store.mark_finished(success)
        return ToolResult(
            success=True,
            content=f"[MISSION COMPLETE] {summary}",
            task_complete=True,
            mission_success=success,
        )
