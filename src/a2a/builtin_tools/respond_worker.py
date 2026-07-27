"""RespondWorkerTool — Coordinator Agent 回复 Worker 的帮助请求。

用标准 A2A send_message 恢复处于 INPUT_REQUIRED 状态的 Worker 任务。
"""

import logging
from typing import Any

import httpx
from a2a.client import create_client, ClientConfig
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import PlanNode, TaskStore
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError

logger = logging.getLogger(__name__)


class RespondWorkerTool(Tool):
    """回复 Worker 发起的帮助请求。

    通过 A2A send_message 向 Worker 发送回复，恢复处于 INPUT_REQUIRED 状态的任务。
    """

    def __init__(self, store: TaskStore, registry: AgentRegistry) -> None:
        self._store = store
        self._registry = registry

    @property
    def name(self) -> str:
        return "respond_worker"

    @property
    def description(self) -> str:
        return (
            "Respond to a worker's help request. Call this when a worker is "
            "asking for clarification (INPUT_REQUIRED status). Sends the response "
            "via A2A protocol to resume the worker's task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID of the worker that needs help",
                },
                "response": {
                    "type": "string",
                    "description": "The response/guidance to send back to the worker",
                },
            },
            "required": ["task_id", "response"],
        }

    async def execute(self, task_id: str, response: str) -> ToolResult:
        # Accept either dispatch task_id or worker-assigned UUID
        dispatch_id = self._store.resolve_dispatch_id(task_id)
        if not isinstance(dispatch_id, str):
            compat = getattr(self._store, "resolve_compat_dispatch_id", None)
            candidate = compat(task_id) if callable(compat) else None
            dispatch_id = candidate if isinstance(candidate, str) else None
        legacy_node = self._store.get_node(task_id)
        if dispatch_id is None and isinstance(legacy_node, PlanNode):
            # Legacy presentation compatibility is limited to an existing
            # logical node; physical runtime lookup remains exact-only.
            dispatch_id = task_id
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )

        node = self._store.get_node(dispatch_id)
        if node is None and hasattr(self._store, "get_node_for_dispatch"):
            node = self._store.get_node_for_dispatch(dispatch_id)
        if node is None or not node.worker_id:
            return ToolResult(
                success=False,
                content=f"Task '{dispatch_id}' has no assigned worker.",
                error="no_worker",
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
            streaming=True,
            httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(30.0)),
        )

        try:
            client = await create_client(agent_info.endpoint, config)
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Failed to connect to worker: {e}",
            )

        try:
            # The worker expects its own task UUID in the A2A message, not the dispatch id.
            worker_task_id = (
                self._store.get_worker_task_id(dispatch_id)
                if isinstance(
                    getattr(getattr(self._store, "_runtime", None), "dispatches", None),
                    dict,
                )
                else self._store._dispatch_to_worker.get(dispatch_id, dispatch_id)  # noqa: SLF001
            )
            message = Message(
                role=Role.ROLE_USER,
                parts=[Part(text=response)],
                task_id=worker_task_id,
            )
            request = SendMessageRequest(message=message)

            # Consume first event to confirm worker resumed, then return
            async for _ in client.send_message(request):
                break
        except Exception as e:
            await client.close()
            return ToolResult(
                success=False,
                content=f"Failed to send response to worker: {e}",
                error="send_failed",
            )

        await client.close()

        return ToolResult(
            success=True,
            content=f"Response sent to {task_id}: {response}",
        )
