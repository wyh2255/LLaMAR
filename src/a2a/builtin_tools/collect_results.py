"""CollectResultsTool — 阻塞等待已派发任务的结果。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore

logger = logging.getLogger(__name__)

# 截断阈值
_RESULT_TRUNCATE = 1500


class CollectResultsTool(Tool):
    """阻塞等待已派发任务的结果。

    等待所有指定 task_id 的后台任务完成，返回截断结果。
    完整结果存入 TaskStore.results 供 query_task_results 按需取。
    系统自动回写节点 state（done/failed）。
    """

    def __init__(self, store: TaskStore):
        self._store = store

    @property
    def name(self) -> str:
        return "collect_results"

    @property
    def description(self) -> str:
        return (
            "Wait for dispatched tasks to complete and return their results. "
            "Blocks until ALL specified tasks finish. Results are truncated "
            "(<=1500 chars); use query_task_results for full output. "
            "System auto-updates node state to done/failed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs to collect results for",
                },
            },
            "required": ["task_ids"],
        }

    async def execute(self, task_ids: list[str]) -> ToolResult:
        """阻塞等待所有任务完成，返回结果列表。"""
        # 收集 futures
        pending: list[asyncio.Future] = []
        missing: list[str] = []
        for tid in task_ids:
            fut = self._store._futures.get(tid)  # noqa: SLF001
            if fut is None:
                missing.append(tid)
            else:
                pending.append(fut)

        # 等待所有 future 完成
        results_raw = await asyncio.gather(*pending, return_exceptions=True)

        # 处理结果
        output: list[dict[str, Any]] = []

        # 先处理 missing 的 task
        for tid in missing:
            output.append(
                {
                    "task_id": tid,
                    "success": False,
                    "error": f"No dispatched task found for '{tid}'. Did you dispatch it?",
                }
            )
            self._store.set_state(tid, "failed")

        # 处理 future 结果
        for tid, raw in zip([t for t in task_ids if t not in missing], results_raw):
            if isinstance(raw, Exception):
                output.append(
                    {
                        "task_id": tid,
                        "success": False,
                        "error": f"{type(raw).__name__}: {raw}",
                    }
                )
                self._store.set_state(tid, "failed")
            elif isinstance(raw, str):
                # Push notification 路径：raw 已经是结果文本
                text = raw
                self._store._results[tid] = text  # noqa: SLF001
                truncated = text[:_RESULT_TRUNCATE]
                if len(text) > _RESULT_TRUNCATE:
                    truncated += (
                        "...[truncated, use query_task_results for full output]"
                    )
                output.append(
                    {
                        "task_id": tid,
                        "success": True,
                        "result": truncated,
                    }
                )
                self._store.set_state(tid, "done", result=truncated)
            else:
                # 从 StreamResponse 列表提取文本
                text = self._extract_text(raw)
                # 完整结果存入 store
                self._store._results[tid] = text  # noqa: SLF001
                # 截断用于返回
                truncated = text[:_RESULT_TRUNCATE]
                if len(text) > _RESULT_TRUNCATE:
                    truncated += (
                        "...[truncated, use query_task_results for full output]"
                    )
                output.append(
                    {
                        "task_id": tid,
                        "success": True,
                        "result": truncated,
                    }
                )
                self._store.set_state(tid, "done", result=truncated)

        content = json.dumps(output, ensure_ascii=False, indent=2)
        return ToolResult(success=True, content=content)

    @staticmethod
    def _extract_text(events: list[Any]) -> str:
        """从 StreamResponse 列表或 mock 数据中提取文本。

        优先使用 agent_executor._extract_all_text（处理真实 protobuf 事件）。
        如果是简单字符串列表（测试用），直接拼接。
        """
        if not events:
            return "(empty result)"
        # 检查是否是简单字符串列表（测试场景）
        if all(isinstance(e, str) for e in events):
            return " ".join(events)
        # 真实 StreamResponse 列表
        try:
            from a2a.coordinator.agent_executor import _extract_all_text

            return _extract_all_text(events)
        except Exception:
            return str(events)[:2000]
