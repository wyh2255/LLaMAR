"""VerifyResultTool — 包装 VerifierAgent，Agent 自主调用验证。"""

from __future__ import annotations

import json
import logging
from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore

logger = logging.getLogger(__name__)


class VerifyResultTool(Tool):
    """验证已完成的任务输出。

    包装 VerifierAgent.verify()，让编排 Agent 在 loop 内自主决定
    何时验证、失败是否重试。系统自动回写节点 state（verified/failed）。
    """

    def __init__(self, store: TaskStore):
        self._store = store

    @property
    def name(self) -> str:
        return "verify_result"

    @property
    def description(self) -> str:
        return (
            "Verify a completed task's output against the original request. "
            "Returns pass/fail with issues and suggestions. "
            "Only verify critical outputs — not every task needs verification. "
            "If verification fails, consider re-dispatching with a refined prompt "
            "or a different worker."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to verify (must be already collected)",
                },
                "criteria": {
                    "type": "string",
                    "description": "Optional extra verification criteria",
                },
            },
            "required": ["task_id"],
        }

    async def execute(self, task_id: str, criteria: str | None = None) -> ToolResult:
        """验证任务输出。"""
        verifier = self._store._verifier  # noqa: SLF001
        if verifier is None:
            return ToolResult(
                success=False,
                content="Verifier is not configured. Cannot verify results.",
                error="verifier_not_configured",
            )

        node = self._store.get_node(task_id)
        if node is None:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in plan.",
                error="node_not_found",
            )

        # 从 results 取完整输出，fallback 到 node.result
        worker_output = self._store._results.get(task_id) or node.result or ""  # noqa: SLF001
        if not worker_output:
            return ToolResult(
                success=False,
                content=(
                    f"No output available for task '{task_id}'. Collect results first."
                ),
                error="no_output_available",
            )

        # subtask_prompt 用 node.description 近似（完整 prompt 未持久化）
        subtask_prompt = node.description
        if criteria:
            subtask_prompt += f"\n\nExtra criteria: {criteria}"

        try:
            report = await verifier.verify(
                original_request=self._store.original_request,
                subtask_prompt=subtask_prompt,
                worker_output=worker_output,
                task_id=task_id,
            )
        except Exception as e:
            logger.warning("Verification failed for %s: %s", task_id, e)
            return ToolResult(
                success=False,
                content=f"Verification error: {type(e).__name__}: {e}",
                error="verification_failed",
            )

        # 系统回写状态
        if report.passed:
            self._store.set_state(task_id, "verified")
        else:
            self._store.set_state(task_id, "failed")

        output = {
            "task_id": report.task_id,
            "passed": report.passed,
            "summary": report.summary,
            "issues": report.issues,
            "suggestions": report.suggestions,
        }
        return ToolResult(
            success=True, content=json.dumps(output, ensure_ascii=False, indent=2)
        )
