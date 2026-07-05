"""CancelTaskTool — Coordinator 取消 Worker 正在运行的任务。"""

from __future__ import annotations

import logging
from typing import Any

from httpx import AsyncClient, Timeout
from a2a.client import create_client, ClientConfig
from a2a.types.a2a_pb2 import CancelTaskRequest, TaskState

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError

logger = logging.getLogger(__name__)


class CancelTaskTool(Tool):
    """Cancel a running worker task by its coordinator dispatch id."""

    def __init__(self, store: TaskStore, registry: AgentRegistry) -> None:
        super().__init__()
        self._store = store
        self._registry = registry

    @property
    def name(self) -> str:
        return "cancel_task"

    @property
    def description(self) -> str:
        return (
            "Cancel a running worker task. Provide the dispatch task_id "
            "used in dispatch_task(). The worker will stop its current "
            "Agent loop and its state will become CANCELED. After canceling, "
            "call query_task_events to confirm, then dispatch a new task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The dispatch task_id to cancel",
                },
            },
            "required": ["task_id"],
        }

    async def execute(self, task_id: str) -> ToolResult:
        dispatch_id = self._store.resolve_dispatch_id(task_id)
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )

        node = self._store.get_node(dispatch_id)
        if node is None or not node.worker_id:
            return ToolResult(
                success=False,
                content=f"Task '{dispatch_id}' has no assigned worker.",
                error="no_worker",
            )

        worker_task_id = self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=(
                    f"Task '{dispatch_id}' has not been dispatched to a worker yet. "
                    "Wait for the worker to acknowledge before canceling."
                ),
                error="not_yet_dispatched",
            )

        try:
            agent_info = self._registry.get(node.worker_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{node.worker_id}' not found in registry.",
                error="worker_not_found",
            )

        config = ClientConfig(
            streaming=False,
            httpx_client=AsyncClient(timeout=Timeout(30.0)),
        )

        try:
            client = await create_client(agent_info.endpoint, config)
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Failed to connect to worker: {e}",
                error="connection_failed",
            )

        try:
            request = CancelTaskRequest(id=worker_task_id)
            result_task = await client.cancel_task(request)
            state_name = TaskState.Name(result_task.status.state)
        except Exception as e:
            await client.close()
            return ToolResult(
                success=False,
                content=f"Cancel request failed: {e}",
                error="cancel_failed",
            )

        await client.close()
        return ToolResult(
            success=True,
            content=(
                f"Task '{task_id}' (worker task {worker_task_id}) "
                f"cancelled. Worker state: {state_name}."
            ),
        )
