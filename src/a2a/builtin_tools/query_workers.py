"""QueryWorkersTool - 查询当前在线 Worker 及其能力。"""

from __future__ import annotations

from a2a.coordinator.agent_registry import AgentRegistry
from Agent.router_agent.tools.base import Tool, ToolResult


class QueryWorkersTool(Tool):
    """查询当前在线 Worker 的列表和能力。构造时注入 AgentRegistry（晚绑定）。"""

    def __init__(self, registry: AgentRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "query_workers"

    @property
    def description(self) -> str:
        return (
            "Query the list of currently online workers and their capabilities. "
            "Use this before assigning tasks to understand which workers are available."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self) -> ToolResult:
        """返回当前在线 Worker 的格式化列表。"""
        agents_text = self._registry.get_all_agents_prompt_text()
        if not agents_text:
            return ToolResult(success=True, content="No workers are currently online.")
        return ToolResult(success=True, content=agents_text)
