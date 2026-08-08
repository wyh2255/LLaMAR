"""SendMessageTool — Coordinator 与 Worker 通信的统一门面工具。

将 dispatch_task / respond_worker / cancel_task 三种语义收敛为单一入口，
减少 LLM 工具选择负担。内部继续复用现有底层实现，保留 A2A 任务生命周期语义。

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

    将 `dispatch_task`、`respond_worker`、`cancel_task`、`activate_plan_node` 四种语义
    收敛为单一入口，但内部继续复用现有底层实现，保留 A2A 任务生命周期和日志语义。
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
            "For DAG-managed activation, set message_type='activate_plan_node' and provide "
            "`related_task_id=<logical_id>`; participants, objective, and assignments are "
            "read from the declared MissionGraph. "
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
                    "enum": [
                        "assign_task",
                        "reply_to_help",
                        "cancel_task",
                        "activate_plan_node",
                    ],
                    "description": (
                        "assign_task: dispatch a new task to `who`. "
                        "activate_plan_node: activate a declared DAG node by logical_id. "
                        "reply_to_help: respond to an INPUT_REQUIRED worker task. "
                        "cancel_task: cancel an existing worker task."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Required for assign_task and reply_to_help. "
                        "Complete instruction or response text. "
                        "Ignored for cancel_task and activate_plan_node."
                    ),
                },
                "who": {
                    "type": "string",
                    "description": (
                        "Required only for assign_task: the target worker ID "
                        "(e.g. Alice). For other message types this is ignored."
                    ),
                },
                "related_task_id": {
                    "type": "string",
                    "description": (
                        "Required for activate_plan_node, reply_to_help, and cancel_task. "
                        "For activate_plan_node this is the logical node ID declared in the MissionGraph. "
                        "For other operations this is the existing dispatch task id."
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
        if message_type == "activate_plan_node":
            return await self._handle_activate_plan_node(related_task_id)
        if message_type == "reply_to_help":
            return await self._handle_reply_to_help(content, related_task_id)
        if message_type == "cancel_task":
            return await self._handle_cancel_task(related_task_id)
        return ToolResult(
            success=False,
            content=(
                f"Invalid message_type '{message_type}'. "
                "Use 'assign_task', 'activate_plan_node', 'reply_to_help', or 'cancel_task'."
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

        if self._runtime_attached():
            return await self._handle_runtime_assign_task(content, who, related_task_id)

        busy = await self._check_worker_busy(who)
        if busy is not None:
            return busy

        result = await self._dispatch_tool.execute(
            agent_id=who,
            prompt=content,
            task_id=related_task_id,
        )
        if result.success:
            data = dict(result.data or {})
            data.setdefault("queued", False)
            data.setdefault("deferred", False)
            data.setdefault("worker_id", who)
            result = ToolResult(
                success=True,
                content=result.content or "",
                error=None,
                task_complete=bool(getattr(result, "task_complete", False)),
                mission_success=getattr(result, "mission_success", None),
                data=data,
            )
        return result

    def _runtime_attached(self) -> bool:
        runtime = getattr(self._store, "_runtime", None)
        return isinstance(getattr(runtime, "dispatches", None), dict)

    def _ensure_deferred_activation_hook(self) -> None:
        """Install DispatchTaskTool-backed activation callback once per store."""
        existing = getattr(self._store, "_deferred_activation_callback", None)
        if existing is not None:
            return
        setter = getattr(self._store, "set_deferred_activation_callback", None)
        if not callable(setter):
            return

        async def _activate(
            worker_id: str, content: str, related_task_id: str | None
        ) -> ToolResult:
            return await self._dispatch_tool.execute(
                agent_id=worker_id,
                prompt=content,
                task_id=related_task_id,
            )

        setter(_activate)

    async def _handle_runtime_assign_task(
        self,
        content: str,
        who: str,
        related_task_id: str | None,
    ) -> ToolResult:
        """Runtime path: direct dispatch when idle; LWW deferred slot when busy.

        Never auto-cancels active physical dispatches. One deferred assignment
        slot per worker is context-owned in TaskStore; further assigns coalesce.
        """
        self._ensure_deferred_activation_hook()
        active_ids = list(self._store.get_active_tasks_by_worker(who))
        if active_ids:
            meta = self._store.enqueue_deferred_assignment(
                who, content, related_task_id
            )
            coalesced = bool(meta.get("coalesced"))
            return ToolResult(
                success=True,
                content=(
                    f"Worker '{who}' is busy with active dispatch(s) {active_ids}. "
                    f"Assignment deferred (last-write-wins"
                    f"{'; coalesced' if coalesced else ''}). "
                    "It will dispatch automatically when the worker becomes free."
                ),
                data={
                    "queued": True,
                    "deferred": True,
                    "worker_id": who,
                    "active_dispatch_ids": active_ids,
                    "coalesced": coalesced,
                    "replaced_deferred": coalesced,
                    "previous_content": meta.get("previous_content"),
                },
            )

        result = await self._dispatch_tool.execute(
            agent_id=who,
            prompt=content,
            task_id=related_task_id,
        )
        if result.success:
            data = dict(result.data or {})
            data["queued"] = False
            data["deferred"] = False
            data["worker_id"] = who
            result = ToolResult(
                success=True,
                content=result.content or "",
                error=None,
                task_complete=bool(getattr(result, "task_complete", False)),
                mission_success=getattr(result, "mission_success", None),
                data=data,
            )
        return result

    async def _check_worker_busy(self, who: str) -> ToolResult | None:
        """Legacy busy guard: reject assign when PlanNode is running/dispatched.

        返回 None 表示 worker 空闲，可以继续 dispatch。
        返回 ToolResult 表示 worker 忙碌，包含具体任务信息。

        注意：只检查 running/dispatched 状态（worker 已确认接收），
        不检查 pending 状态（coordinator 计划状态，worker 可能尚未收到）。
        这避免了初始 dispatch 时被自己的 update_plan 阻塞。
        """
        active_tasks = [
            node.task_id
            for node in self._store.get_plan()
            if node.worker_id == who and node.state in ("running", "dispatched")
        ]

        if active_tasks:
            return ToolResult(
                success=False,
                content=(
                    f"Worker '{who}' already has active task(s): {active_tasks}. "
                    f"Cancel the existing task(s) before dispatching a new one. "
                    f"Use send_message(message_type='cancel_task', related_task_id='{active_tasks[0]}') to cancel."
                ),
                error="worker_busy",
            )
        return None

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
        if not isinstance(dispatch_id, str):
            compat = getattr(self._store, "resolve_compat_dispatch_id", None)
            candidate = compat(related_task_id) if callable(compat) else None
            dispatch_id = candidate if isinstance(candidate, str) else None
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{related_task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )
        worker_task_id = (
            self._store.get_worker_task_id(dispatch_id)
            if isinstance(
                getattr(getattr(self._store, "_runtime", None), "dispatches", None),
                dict,
            )
            else self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        )
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
            task_id=dispatch_id,
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
        if not isinstance(dispatch_id, str):
            compat = getattr(self._store, "resolve_compat_dispatch_id", None)
            candidate = compat(related_task_id) if callable(compat) else None
            dispatch_id = candidate if isinstance(candidate, str) else None
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{related_task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )
        worker_task_id = (
            self._store.get_worker_task_id(dispatch_id)
            if isinstance(
                getattr(getattr(self._store, "_runtime", None), "dispatches", None),
                dict,
            )
            else self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        )
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=(
                    f"Task '{dispatch_id}' has not yet been acknowledged by the worker. "
                    "Wait a moment and retry."
                ),
                error="task_not_routable_yet",
            )
        return await self._cancel_tool.execute(task_id=dispatch_id)

    async def _handle_activate_plan_node(
        self,
        related_task_id: str | None,
    ) -> ToolResult:
        """Handle activate_plan_node: DAG gate + claim + Team ACK + fan-out.

        ``related_task_id`` is the logical node ID declared in MissionGraph.
        Participants, objective, and assignments are read from the graph,
        not from ``who`` or ``content``.
        """
        if not related_task_id:
            return ToolResult(
                success=False,
                content="activate_plan_node requires `related_task_id` (logical node ID).",
                error="missing_related_task_id",
            )

        store = self._store
        runtime = getattr(store, "_runtime", None)
        mission_graph = getattr(store, "_mission_graph", None)

        if runtime is None or mission_graph is None:
            return ToolResult(
                success=False,
                content="MissionRuntime or MissionGraph not attached.",
                error="runtime_unavailable",
            )

        # Retrieve the Coordinator-lifetime TeamPartitionService wired through
        # MissionRuntimeManager (set by CoordinatorServer.set_team_partition_service).
        manager = getattr(runtime, "_manager", None)
        team_service = getattr(manager, "_team_partition_service", None) if manager else None

        try:
            result = await runtime.activate_plan_node(
                str(related_task_id),
                mission_graph,
                team_service=team_service,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                content=f"activate_plan_node failed: {exc}",
                error="activation_error",
            )

        if not result.get("success"):
            error = result.get("error") or "activation_error"
            reason = result.get("reason", "")
            msg = f"Activation failed: {error}"
            if reason:
                msg += f" ({reason})"
            return ToolResult(
                success=False,
                content=msg,
                error=error,
                data={"error": error, "reason": reason, "result": result},
            )

        dispatches = result.get("dispatches", [])
        dispatch_summary = "; ".join(
            f"{d['worker_id']}:{d['dispatch_id']}:{d.get('worker_task_id', '')}"
            for d in dispatches
        )
        return ToolResult(
            success=True,
            content=(
                f"Node '{related_task_id}' activated. "
                f"Team: {result.get('team_id', 'N/A')} "
                f"(epoch {result.get('team_epoch', 'N/A')}). "
                f"Dispatches: {dispatch_summary}"
            ),
            data={
                "node_id": related_task_id,
                "team_id": result.get("team_id"),
                "team_epoch": result.get("team_epoch"),
                "dispatches": dispatches,
                "result": result,
            },
        )
