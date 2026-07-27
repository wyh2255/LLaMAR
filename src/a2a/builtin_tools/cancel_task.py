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
from a2a.coordinator.mission_runtime import normalize_physical_state

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
        if not isinstance(dispatch_id, str):
            compat = getattr(self._store, "resolve_compat_dispatch_id", None)
            candidate = compat(task_id) if callable(compat) else None
            dispatch_id = candidate if isinstance(candidate, str) else None
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

        runtime_attached = isinstance(
            getattr(getattr(self._store, "_runtime", None), "dispatches", None),
            dict,
        )
        dispatch = self._store.get_dispatch(dispatch_id) if runtime_attached else None
        worker_id = (
            dispatch.worker_id
            if dispatch is not None
            else node.worker_id
        )
        worker_task_id = (
            self._store.get_worker_task_id(dispatch_id)
            if runtime_attached
            else self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        )

        # Idempotent terminal cancel: already-finalized dispatches must not
        # open a new A2A client (and leak EventQueueSource dispatch loops).
        if dispatch is not None:
            state = getattr(dispatch, "state", None)
            is_terminal = bool(getattr(state, "terminal", False))
            if is_terminal:
                state_name = getattr(state, "value", str(state))
                return ToolResult(
                    success=True,
                    content=(
                        f"Task '{task_id}' already terminal ({state_name}). "
                        "Cancel is idempotent; no remote request sent."
                    ),
                    data={
                        "dispatch_id": dispatch_id,
                        "state": state_name,
                        "idempotent": True,
                    },
                )

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
            agent_info = self._registry.get(worker_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{worker_id}' not found in registry.",
                error="worker_not_found",
            )

        # Fence locally before contacting the worker.  This is not terminal:
        # only the worker's returned state may finalize the physical dispatch.
        self._store.cancel_dispatch(dispatch_id, reason="explicit_cancel")

        # Own the httpx client so transport.close() does not race a shared pool,
        # and always close the A2A client (drains EventQueueSource dispatchers).
        httpx_client = AsyncClient(timeout=Timeout(30.0))
        config = ClientConfig(
            streaming=False,
            httpx_client=httpx_client,
        )
        client = None
        try:
            client = await create_client(agent_info.endpoint, config)
            request = CancelTaskRequest(id=worker_task_id)
            result_task = await client.cancel_task(request)
            state_name = TaskState.Name(result_task.status.state)
        except Exception as e:
            if client is None:
                return ToolResult(
                    success=False,
                    content=f"Failed to connect to worker: {e}",
                    error="connection_failed",
                )
            return ToolResult(
                success=False,
                content=f"Cancel request failed: {e}",
                error="cancel_failed",
            )
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.debug("cancel_task client.close failed", exc_info=True)
            try:
                await httpx_client.aclose()
            except Exception:
                logger.debug("cancel_task httpx.aclose failed", exc_info=True)

        normalized = normalize_physical_state(state_name)
        if normalized is not None and hasattr(self._store, "apply_physical_status"):
            self._store.apply_physical_status(
                dispatch_id,
                normalized,
                source="remote_cancel",
            )
        return ToolResult(
            success=True,
            content=(
                f"Task '{task_id}' (worker task {worker_task_id}) "
                f"cancelled. Worker state: {state_name}."
            ),
        )
