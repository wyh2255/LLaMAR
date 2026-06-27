"""UpdatePlanTool — Agent 声明/修改编排计划（advisory，借鉴 TodoWrite）。"""

from __future__ import annotations

from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore


class UpdatePlanTool(Tool):
    """声明或修改编排计划。

    Agent 传入完整的计划节点列表（非增量）。
    系统自动 diff 并持久化，节点执行状态由系统维护（零漂移）。
    """

    def __init__(self, store: TaskStore):
        self._store = store

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Declare or modify the orchestration plan. "
            "Pass the FULL plan list (not a delta) — same convention as TodoWrite. "
            "Each node has: task_id (unique), worker_id (optional), description, "
            "depends_on (list of task_ids), status ('pending' or 'skipped'). "
            "Execution state (running/done/failed/verified) is managed by the system "
            "and preserved across updates. Returns a diff summary."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Unique task identifier (kebab-case)",
                            },
                            "worker_id": {
                                "type": "string",
                                "description": "Target worker ID (optional)",
                            },
                            "description": {
                                "type": "string",
                                "description": "Human-readable task description",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Task IDs this task depends on",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "skipped"],
                                "description": "Structural status (default: pending)",
                            },
                        },
                        "required": ["task_id"],
                    },
                    "description": "Full plan node list (replaces existing plan)",
                },
            },
            "required": ["plan"],
        }

    async def execute(self, plan: list[dict[str, Any]]) -> ToolResult:
        """替换计划并返回 diff 摘要。"""
        diff = self._store.update_plan(plan)
        summary = (
            f"Plan updated: {diff['nodes']} nodes, {diff['edges']} edges. "
            f"Added: {diff['added']}, Removed: {diff['removed']}, "
            f"Modified: {diff['modified']}, Pending: {diff['pending']}."
        )
        return ToolResult(success=True, content=summary)
