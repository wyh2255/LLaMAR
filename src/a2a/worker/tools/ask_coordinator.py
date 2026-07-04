"""AskCoordinatorTool — Worker Agent 向 Coordinator 请求帮助。

抛出 NeedInputError 信号，由 Agent.run() 捕获后通过 A2A INPUT_REQUIRED 状态
通知 Coordinator。Coordinator 用标准 send_message 恢复任务。
"""

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from a2a.worker.need_input import NeedInputError


class AskCoordinatorTool(Tool):
    """暂停执行并向 Coordinator 请求帮助。

    调用后抛出 NeedInputError，Agent.run() 捕获后返回 RunResult(need_input=True)，
    AgentAdapter.execute() 调用 TaskUpdater.requires_input() 设置 A2A INPUT_REQUIRED 状态。
    Coordinator 回复后，任务从快照恢复，agent 继续执行。
    """

    @property
    def name(self) -> str:
        return "ask_coordinator"

    @property
    def description(self) -> str:
        return (
            "Pause execution and ask the coordinator for help or clarification. "
            "The coordinator will see your question and respond. Execution resumes "
            "after the coordinator replies."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or help request to send to the coordinator",
                },
            },
            "required": ["question"],
        }

    async def execute(self, question: str) -> ToolResult:
        raise NeedInputError(question)
