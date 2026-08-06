"""QueryTaskEventsTool — 非阻塞查询 Worker 任务状态。

通过 EventStore 读取 push callback 已经写入的事件，返回每个任务
的最新状态（RUNNING / COMPLETED / FAILED / CANCELED / INPUT_REQUIRED）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.event_store import event_store
from a2a.coordinator.mission_runtime import PhysicalDispatch
from a2a.coordinator.task_store import TaskStore

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELED"}
_ACTIONABLE_STATES = _TERMINAL_STATES | {"INPUT_REQUIRED"}


class QueryTaskEventsTool(Tool):
    """查询已派发 Worker 任务的当前状态。

    与阻塞的 collect_results 不同，此工具只读取 EventStore 中已有的
    push callback 事件。支持一个短超时，等待某个任务进入可处理状态
    （terminal 或 INPUT_REQUIRED）后再返回，避免 LLM 空转。
    """

    def __init__(self, store: TaskStore):
        self._store = store

    @property
    def name(self) -> str:
        return "query_task_events"

    @property
    def description(self) -> str:
        return (
            "Debug tool: query dispatched worker task states. "
            "Task statuses are auto-injected into Environment State each round — "
            "this tool is rarely needed in normal operation. "
            "Returns states: RUNNING, COMPLETED, FAILED, CANCELED, or INPUT_REQUIRED. "
            "If INPUT_REQUIRED, use send_message(message_type='reply_to_help', "
            "related_task_id=..., content=...) to reply. "
            "If RUNNING, dispatch other tasks or query_sar_state before checking again. "
            "Optional timeout waits up to N seconds for an actionable status change."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of dispatched task IDs to query",
                },
                "timeout": {
                    "type": "number",
                    "default": 5.0,
                    "description": (
                        "Max seconds to wait for any task to become actionable "
                        "(terminal or INPUT_REQUIRED). 0 means return immediately."
                    ),
                },
            },
            "required": ["task_ids"],
        }

    async def execute(self, task_ids: list[str], timeout: float = 5.0) -> ToolResult:
        """查询任务状态，可选短超时等待 actionable 事件。"""
        if not task_ids:
            return ToolResult(success=True, content="[]")

        deadline = asyncio.get_event_loop().time() + max(0.0, timeout)

        while True:
            states = [self._query_one(tid) for tid in task_ids]

            # 如果已经有任务进入 terminal 或 INPUT_REQUIRED，立即返回
            if any(s["state"] in _ACTIONABLE_STATES for s in states):
                return ToolResult(
                    success=True,
                    content=json.dumps(states, ensure_ascii=False, indent=2),
                )

            # 超时或 timeout=0，返回当前状态
            if timeout <= 0 or asyncio.get_event_loop().time() >= deadline:
                return ToolResult(
                    success=True,
                    content=json.dumps(states, ensure_ascii=False, indent=2),
                )

            await asyncio.sleep(0.5)

    def _query_one(self, dispatch_id: str) -> dict[str, Any]:
        physical_id = self._store.resolve_dispatch_id(dispatch_id)
        if not isinstance(physical_id, str):
            compat = getattr(self._store, "resolve_compat_dispatch_id", None)
            candidate = compat(dispatch_id) if callable(compat) else None
            physical_id = candidate if isinstance(candidate, str) else dispatch_id
        runtime_attached = isinstance(
            getattr(getattr(self._store, "_runtime", None), "dispatches", None),
            dict,
        )
        worker_id = ""
        dispatch = (
            self._store.get_dispatch(physical_id) if runtime_attached else None
        )
        if dispatch is not None:
            worker_id = dispatch.worker_id
        elif not runtime_attached:
            worker_id = self._store._dispatch_to_worker.get(physical_id, "")  # noqa: SLF001
        state = event_store.get_task_state(physical_id, worker_id)
        if isinstance(dispatch, PhysicalDispatch):
            state["state"] = dispatch.state.value
            state["task_id"] = physical_id
            evidence = dispatch.result if dispatch.result is not None else dispatch.artifact
            if evidence is not None:
                state["text"] = str(evidence)
        return state
