"""DispatchTaskTool — 非阻塞派发子任务到 Worker。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore

logger = logging.getLogger(__name__)


class DispatchTaskTool(Tool):
    """非阻塞派发子任务到指定 Worker。

    立即返回 task_id，不等待结果。Agent 需调用 collect_results 等待。
    支持并行：连续多次 dispatch 后再 collect，任务并行执行。
    """

    def __init__(
        self,
        store: TaskStore,
        coordinator_host: str = "localhost",
        coordinator_port: int = 8080,
    ):
        self._store = store
        self._coordinator_host = coordinator_host
        self._coordinator_port = coordinator_port

    @property
    def name(self) -> str:
        return "dispatch_task"

    @property
    def description(self) -> str:
        return (
            "Dispatch a subtask to a worker and return immediately (non-blocking). "
            "The task runs in the background. Call query_task_events([task_id]) "
            "to check status and retrieve the result. For parallel execution, "
            "dispatch multiple tasks before calling query_task_events."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The worker ID to send the task to",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete, self-contained instruction for the worker",
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Unique task identifier. If omitted, auto-generated as "
                        "'dispatch-<n>'. Use descriptive kebab-case names."
                    ),
                },
            },
            "required": ["agent_id", "prompt"],
        }

    async def execute(
        self,
        agent_id: str,
        prompt: str,
        task_id: str | None = None,
    ) -> ToolResult:
        """Allocate a physical dispatch before starting the network task."""
        if self._store.dispatched_count >= self._store.max_tasks:
            return ToolResult(
                success=False,
                error=(
                    f"Max tasks limit reached ({self._store.max_tasks}). "
                    "Cannot dispatch more tasks."
                ),
            )

        logical_id = task_id or f"dispatch-{self._store.dispatched_count + 1}"

        # When MissionGraph has declared nodes, reject undeclared tasks.
        if self._store.mission_node_count > 0:
            mission_node = self._store.get_mission_node(logical_id)
            if mission_node is None:
                return ToolResult(
                    success=False,
                    error=f"undeclared_task: {logical_id} not in MissionGraph. "
                    f"Use update_plan first to declare it.",
                )
            if agent_id not in mission_node.participant_ids:
                return ToolResult(
                    success=False,
                    error=f"planned_worker_mismatch: {agent_id} is not a participant of {logical_id}. "
                    f"Declared participants: {mission_node.participant_ids}",
                )
            return ToolResult(
                success=False,
                content=(
                    f"Graph-managed node '{logical_id}' must be activated with "
                    "send_message(message_type='activate_plan_node', "
                    f"related_task_id='{logical_id}') instead of direct dispatch_task."
                ),
                error="graph_activation_required",
            )

        existing = self._store.get_node(logical_id)
        if existing is None:
            self._store.add_adhoc_node(
                task_id=logical_id,
                worker_id=agent_id,
                description=f"Ad-hoc: {prompt[:80]}",
            )
        elif existing.worker_id != agent_id:
            existing.worker_id = agent_id

        dispatch = self._store.create_physical_dispatch(logical_id, agent_id)
        physical_id = dispatch.dispatch_id if dispatch is not None else logical_id
        future = self._store.register_future(physical_id)
        if dispatch is not None:
            self._store.apply_physical_status(
                physical_id, "DISPATCHING", source="dispatch"
            )

        callback_url = (
            f"http://{self._coordinator_host}:{self._coordinator_port}"
            "/a2a/push-callback"
        )
        from a2a.coordinator.event_store import event_store

        event_store.append(
            physical_id,
            "task_created",
            context_id=self._store.context_id,
            state="DISPATCHING",
        )

        async def _dispatch_with_error_handling() -> None:
            try:
                worker_task_id = await self._store._router.send_task_async(  # noqa: SLF001
                    agent_id,
                    prompt,
                    callback_url,
                    physical_id,
                    context_id=self._store.context_id,
                )
                if not worker_task_id:
                    raise RuntimeError(
                        f"Worker returned empty response for task {physical_id}"
                    )
                self._store.register_worker_task_id(physical_id, worker_task_id)
            except Exception as exc:
                event_store.append(
                    physical_id,
                    "status_update",
                    context_id=self._store.context_id,
                    state="FAILED",
                    text=str(exc),
                )
                if dispatch is not None:
                    self._store.apply_physical_status(
                        physical_id, "FAILED", source="dispatch_error", result=exc
                    )
                elif not future.done():
                    future.set_exception(exc)

        asyncio.create_task(_dispatch_with_error_handling())
        self._store._dispatched_count += 1  # noqa: SLF001
        self._store.set_state(logical_id, "running")

        return ToolResult(
            success=True,
            content=(
                f"Task '{physical_id}' dispatched to '{agent_id}'. "
                f'Call query_task_events(["{physical_id}"]) to check status and retrieve the result.'
            ),
            data={
                "dispatch_id": physical_id,
                "logical_task_id": logical_id,
            },
        )
