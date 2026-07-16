"""ListTasksTool — lists all known task IDs with their current state."""

from __future__ import annotations

import json
import logging
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore

logger = logging.getLogger(__name__)


class ListTasksTool(Tool):
    """List all current task IDs with their state, worker, and dispatch mapping."""

    def __init__(self, store: TaskStore) -> None:
        super().__init__()
        self._store = store

    @property
    def name(self) -> str:
        return "list_tasks"

    @property
    def description(self) -> str:
        return (
            "List all known task IDs and their current state. "
            "Use this BEFORE calling cancel_task or query_task_events to discover "
            "the exact task_id values that the system knows about — do NOT invent task IDs yourself. "
            "Returns: each task's id, worker, state, and the system-level worker_task_id if dispatched."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self) -> ToolResult:
        tasks = []
        for node in self._store.get_plan():
            worker_task_id = self._store._dispatch_to_worker.get(node.task_id)  # noqa: SLF001
            entry = {
                "task_id": node.task_id,
                "worker": node.worker_id,
                "state": node.state,
                "status": node.status,
                "worker_task_id": worker_task_id or None,
                "description": node.description[:80] if node.description else "",
            }
            tasks.append(entry)

        # Also include reverse mapping entries for worker-assigned IDs
        dispatched = []
        for dispatch_id, worker_tid in self._store._dispatch_to_worker.items():  # noqa: SLF001
            node = self._store.get_node(dispatch_id)
            dispatched.append({
                "dispatch_id": dispatch_id,
                "worker_task_id": worker_tid,
                "worker": node.worker_id if node else "?",
                "state": node.state if node else "?",
            })

        result = {
            "task_count": len(tasks),
            "tasks": tasks,
            "dispatch_mappings": dispatched,
        }
        return ToolResult(
            success=True,
            content=json.dumps(result, indent=2, ensure_ascii=False),
        )
