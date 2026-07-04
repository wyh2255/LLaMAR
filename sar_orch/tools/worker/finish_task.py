"""Finish task tool for SAR workers."""

from Agent.router_agent.tools.base import Tool, ToolResult


class FinishTaskTool(Tool):
    """Call this when your assigned subtask is complete."""

    name = "finish_task"
    description = (
        "Call this when your assigned subtask is complete. "
        "Sends a completion signal to the coordinator with the result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "success": {
                "type": "boolean",
                "description": "Whether the subtask succeeded",
            },
            "summary": {
                "type": "string",
                "description": "What you did and the outcome",
            },
            "task_description": {
                "type": "string",
                "description": "Description of the task you completed",
            },
        },
        "required": ["success", "summary", "task_description"],
    }

    def __init__(self, barrier=None, agent_idx: int = 0):
        """Initialize FinishTaskTool.

        Args:
            barrier: SARBarrier instance (unused, kept for uniform tool interface).
            agent_idx: Agent index (unused, kept for uniform tool interface).
        """
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(
        self, success: bool, summary: str, task_description: str
    ) -> ToolResult:
        """Return a task completion signal."""
        return ToolResult(
            success=True,
            content=f"[TASK COMPLETE] {summary}",
            task_complete=True,
            mission_success=success,
        )
