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

    def __init__(self, store: TaskStore, coordinator_host: str = "localhost", coordinator_port: int = 8080):
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
            "The task runs in the background. Call collect_results([task_id]) "
            "to retrieve the output. For parallel execution, dispatch multiple "
            "tasks before calling collect_results."
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
        """非阻塞派发子任务。"""
        # 守卫：最大任务数
        if self._store.dispatched_count >= self._store.max_tasks:
            return ToolResult(
                success=False,
                error=(
                    f"Max tasks limit reached ({self._store.max_tasks}). "
                    "Cannot dispatch more tasks."
                ),
            )

        # 生成 task_id
        if task_id is None:
            task_id = f"dispatch-{self._store.dispatched_count + 1}"

        # 如果 task_id 未在计划中声明，自动补 ad-hoc 节点
        existing = self._store.get_node(task_id)
        if existing is None:
            self._store.add_adhoc_node(
                task_id=task_id,
                worker_id=agent_id,
                description=f"Ad-hoc: {prompt[:80]}",
            )
        elif existing.worker_id != agent_id:
            # 已声明但 worker_id 不同，更新
            existing.worker_id = agent_id

        # 创建后台任务（push notification 模式）
        future = self._store.register_future(task_id)
        callback_url = f"http://{self._coordinator_host}:{self._coordinator_port}/a2a/push-callback"

        from a2a.coordinator.event_store import event_store
        event_store.append(task_id, "task_created", state="DISPATCHED")

        async def _dispatch_with_error_handling():
            try:
                result = await self._store._router.send_task_async(
                    agent_id, prompt, callback_url, task_id,
                    context_id=self._store.context_id,
                )
                if not result:
                    raise RuntimeError(f"Worker returned empty response for task {task_id}")
            except Exception as e:
                if not future.done():
                    future.set_exception(e)

        asyncio.create_task(_dispatch_with_error_handling())
        self._store._dispatched_count += 1  # noqa: SLF001

        # 系统回写状态
        self._store.set_state(task_id, "running")

        return ToolResult(
            success=True,
            content=(
                f"Task '{task_id}' dispatched to '{agent_id}'. "
                f'Call collect_results(["{task_id}"]) to retrieve the result.'
            ),
        )
