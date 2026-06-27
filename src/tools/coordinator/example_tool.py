"""
自定义 Tool 示例 — 分析任务复杂度，给路由决策提供额外信息。

用法：
  1. 将此文件放在 tools/coordinator/ 目录下
  2. 启动时添加 --tools-dir ./tools/coordinator
  3. RouterAgent 会自动加载此 tool，在路由循环中可用

每个 .py 文件应当暴露一个名为 'tool' 的 Tool 实例。
Tool 实例需要实现 name / description / parameters / execute 接口。
"""

from __future__ import annotations

from Agent.router_agent.tools.base import Tool, ToolResult


class AnalyzeTaskComplexityTool(Tool):
    """分析用户任务复杂度，输出建议的执行模式。"""

    @property
    def name(self) -> str:
        return "analyze_task_complexity"

    @property
    def description(self) -> str:
        return (
            "Analyze the complexity of a user request and suggest whether it should "
            "be executed as a single task, sequential subtasks, or parallel subtasks."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "The user's task description to analyze",
                },
            },
            "required": ["task_description"],
        }

    async def execute(self, task_description: str) -> ToolResult:
        """分析任务复杂度，返回建议的执行模式。"""
        word_count = len(task_description.split())
        has_and = " and " in task_description.lower()
        has_then = " then " in task_description.lower()

        if word_count < 2:
            return ToolResult(
                success=True,
                content='{"suggested_mode": "single", "confidence": "high", "reasoning": "Short task, likely single-step"}',
            )
        if has_and and has_then:
            return ToolResult(
                success=True,
                content='{"suggested_mode": "sequential", "confidence": "medium", "reasoning": "Has both parallel (and) and sequential (then) elements"}',
            )
        if has_and:
            return ToolResult(
                success=True,
                content='{"suggested_mode": "parallel", "confidence": "medium", "reasoning": "Contains parallel conjunctions (and)"}',
            )
        if has_then:
            return ToolResult(
                success=True,
                content='{"suggested_mode": "sequential", "confidence": "medium", "reasoning": "Contains sequential conjunctions (then)"}',
            )

        return ToolResult(
            success=True,
            content='{"suggested_mode": "single", "confidence": "low", "reasoning": "Defaulting to single mode"}',
        )


tool = AnalyzeTaskComplexityTool()
