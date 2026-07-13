"""SendMessageTool — Coordinator 与 Worker 通信的统一门面工具。

阶段 1：保留底层 dispatch_task / respond_worker / cancel_task 实现，
在 LLM 工具层增加一个统一的 `send_message` 入口，减少 LLM 工具选择负担。

对于已有任务，`related_task_id` 是权威路由来源；执行层通过 TaskStore 完成
dispatch_id -> worker_id -> worker_task_id 的映射，不依赖 LLM 提供的 `who`。
"""

from __future__ import annotations

from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.builtin_tools.dispatch_task import DispatchTaskTool
from a2a.builtin_tools.respond_worker import RespondWorkerTool
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError
from a2a.coordinator.task_store import TaskStore


class SendMessageTool(Tool):
    """Coordinator 向 Worker 发送统一消息的门面工具。

    将 `dispatch_task`、`respond_worker`、`cancel_task` 三种语义收敛为单一入口，
    但内部继续复用现有底层实现，保留 A2A 任务生命周期和日志语义。
    """

    def __init__(
        self,
        store: TaskStore,
        registry: AgentRegistry,
        coordinator_host: str = "localhost",
        coordinator_port: int = 8080,
    ):
        self._store = store
        self._registry = registry
        self._coordinator_host = coordinator_host
        self._coordinator_port = coordinator_port

        # 底层工具实例，内部复用，不暴露给 LLM
        self._dispatch_tool = DispatchTaskTool(
            store,
            coordinator_host=coordinator_host,
            coordinator_port=coordinator_port,
        )
        self._respond_tool = RespondWorkerTool(store, registry)
        self._cancel_tool = CancelTaskTool(store, registry)

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return (
            "Unified communication gateway to workers. Use this instead of "
            "dispatch_task / respond_worker / cancel_task. "
            "For new tasks, set message_type='assign_task' and provide `who` and `content`. "
            "For existing tasks, set message_type='reply_to_help' or 'cancel_task' and provide "
            "`related_task_id`; the recipient worker is derived from the task store, not `who`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "enum": ["assign_task", "reply_to_help", "cancel_task"],
                    "description": (
                        "assign_task: dispatch a new task to `who`. "
                        "reply_to_help: respond to an INPUT_REQUIRED worker task. "
                        "cancel_task: cancel an existing worker task."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Required for assign_task and reply_to_help. "
                        "Complete instruction or response text. Ignored for cancel_task."
                    ),
                },
                "who": {
                    "type": "string",
                    "description": (
                        "Required only for assign_task: the target worker ID "
                        "(e.g. Alice). For reply_to_help and cancel_task, this is ignored "
                        "because the worker is derived from related_task_id."
                    ),
                },
                "related_task_id": {
                    "type": "string",
                    "description": (
                        "Optional for assign_task (used as explicit task id). "
                        "Required for reply_to_help and cancel_task: the existing dispatch task id."
                    ),
                },
            },
            "required": ["message_type"],
        }

    async def execute(
        self,
        message_type: str,
        content: str | None = None,
        who: str | None = None,
        related_task_id: str | None = None,
    ) -> ToolResult:
        if message_type == "assign_task":
            return await self._handle_assign_task(content, who, related_task_id)
        if message_type == "reply_to_help":
            return await self._handle_reply_to_help(content, related_task_id)
        if message_type == "cancel_task":
            return await self._handle_cancel_task(related_task_id)
        return ToolResult(
            success=False,
            content=(
                f"Invalid message_type '{message_type}'. "
                "Use 'assign_task', 'reply_to_help', or 'cancel_task'."
            ),
            error="invalid_message_type",
        )

    async def _handle_assign_task(
        self,
        content: str | None,
        who: str | None,
        related_task_id: str | None,
    ) -> ToolResult:
        if not who:
            return ToolResult(
                success=False,
                content="assign_task requires `who` (target worker ID).",
                error="missing_who",
            )
        if not content:
            return ToolResult(
                success=False,
                content="assign_task requires `content` (task instruction).",
                error="missing_content",
            )
        # Validate that target worker exists in registry early, matching dispatch_task semantics.
        try:
            self._registry.get(who)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{who}' not found in registry.",
                error="worker_not_found",
            )
        return await self._dispatch_tool.execute(
            agent_id=who,
            prompt=content,
            task_id=related_task_id,
        )

    async def _handle_reply_to_help(
        self,
        content: str | None,
        related_task_id: str | None,
    ) -> ToolResult:
        if not related_task_id:
            return ToolResult(
                success=False,
                content="reply_to_help requires `related_task_id` (existing task id).",
                error="missing_related_task_id",
            )
        if not content:
            return ToolResult(
                success=False,
                content="reply_to_help requires `content` (response text).",
                error="missing_content",
            )
        # Verify the task exists and has been routed to a worker before replying.
        dispatch_id = self._store.resolve_dispatch_id(related_task_id)
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{related_task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )
        worker_task_id = self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=(
                    f"Task '{dispatch_id}' has not yet been acknowledged by the worker. "
                    "Wait a moment and retry."
                ),
                error="task_not_routable_yet",
            )
        return await self._respond_tool.execute(
            task_id=related_task_id,
            response=content,
        )

    async def _handle_cancel_task(
        self,
        related_task_id: str | None,
    ) -> ToolResult:
        if not related_task_id:
            return ToolResult(
                success=False,
                content="cancel_task requires `related_task_id` (existing task id).",
                error="missing_related_task_id",
            )
        dispatch_id = self._store.resolve_dispatch_id(related_task_id)
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{related_task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )
        worker_task_id = self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=(
                    f"Task '{dispatch_id}' has not yet been acknowledged by the worker. "
                    "Wait a moment and retry."
                ),
                error="task_not_routable_yet",
            )
        return await self._cancel_tool.execute(task_id=related_task_id)
