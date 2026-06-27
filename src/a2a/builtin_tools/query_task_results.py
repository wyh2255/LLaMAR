"""QueryTaskResultsTool — 查询已完成 DAG 任务的结果。

供 RouterAgent.plan_next() 在多轮重规划时使用，
让 LLM 可以查看之前任务的具体输出。
"""

from __future__ import annotations

from Agent.router_agent.tools.base import Tool, ToolResult


class QueryTaskResultsTool(Tool):
    """查询已完成任务的结果。

    构造时注入 completed_results (task_id → result_text) 的映射。
    Router 在重规划时可以通过此 Tool 深入了解之前任务的输出。
    """

    def __init__(self, results_store: dict[str, str]):
        self._results = results_store

    @property
    def name(self) -> str:
        return "query_task_results"

    @property
    def description(self) -> str:
        return (
            "Query the results of previously executed DAG tasks. "
            "Use this to inspect specific task outputs in detail before deciding "
            "whether to continue, re-plan, or stop. "
            "Specify task_ids to get specific results, or omit to get a summary of all completed tasks."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of task IDs to query. If empty or omitted, "
                        "returns a summary of all completed task IDs with truncated results."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, task_ids: list[str] | None = None) -> ToolResult:
        """查询任务结果。"""
        if not self._results:
            return ToolResult(success=True, content="No completed task results available yet.")

        if task_ids:
            # 查询指定任务
            lines = []
            for tid in task_ids:
                if tid in self._results:
                    text = self._results[tid]
                    truncated = text[:2000] + ("...[truncated]" if len(text) > 2000 else "")
                    lines.append(f"## {tid}\n{truncated}")
                else:
                    lines.append(f"## {tid}\n(task not found in completed results)")
            return ToolResult(success=True, content="\n\n".join(lines))
        else:
            # 返回所有已完成任务的摘要
            lines = []
            for tid, text in self._results.items():
                summary = text[:300].replace("\n", " ") + ("..." if len(text) > 300 else "")
                lines.append(f"- **{tid}**: {summary}")
            return ToolResult(
                success=True,
                content="Completed tasks:\n" + "\n".join(lines) if lines else "(none)"
            )
