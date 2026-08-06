"""Agent hooks for router_agent.

Hooks provide extension points around the agent run loop. They are used by
context managers to assemble memory and observe tool results.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .context import ContextManager
from .schema import RunResult
from .tools.base import ToolResult


@runtime_checkable
class AgentHooks(Protocol):
    """Protocol for agent lifecycle hooks."""

    async def on_run_start(self, agent: Any, user_message: str) -> None:
        """Called at the start of run()."""
        ...

    async def on_run_end(self, agent: Any, result: RunResult) -> None:
        """Called just before run() returns."""
        ...

    async def pre_llm(self, agent: Any, messages: list) -> list:
        """Called before LLM generation.

        May modify the agent's message history and return the final list to
        send to the LLM.
        """
        ...

    async def post_llm(self, agent: Any, response: Any) -> None:
        """Called after LLM generation."""
        ...

    async def pre_tool(
        self, agent: Any, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Called before a tool is executed."""
        ...

    async def post_tool(
        self, agent: Any, tool_name: str, result: ToolResult
    ) -> ToolResult:
        """Called after a tool is executed. May mutate the result."""
        ...

    async def should_continue(self, agent: Any, step: int) -> bool:
        """Called each loop iteration to decide whether to continue."""
        ...


class CoordinatorSARHooks(AgentHooks):
    """SAR-specific hooks for the coordinator (router) agent."""

    def __init__(self, ctx: ContextManager):
        self._ctx = ctx
        self._last_tool_args: dict = {}

    async def on_run_start(self, agent: Any, user_message: str) -> None:
        """No-op for this implementation."""

    async def on_run_end(self, agent: Any, result: RunResult) -> None:
        """No-op for this implementation."""

    async def pre_llm(self, agent: Any, messages: list) -> list:
        """Refresh runtime state, apply token-based compression if needed, and assemble context memory.

        Flow:
        1. prepare_runtime_state() — async, may call LLM for map summary
        2. refresh_runtime_state() — pull fresh SAR state from provider
        3. prune_history() — Phase 1: truncate long old tool results when
           total estimated tokens exceed 50% of token_limit
        4. assemble() — build final message list:
           [system prompt] + [pruned history] + [Environment State]
        """
        await self._ctx.prepare_runtime_state(agent.llm)
        self._ctx.refresh_runtime_state()
        self._ctx.prune_history(agent.messages)
        return self._ctx.assemble(agent.system_prompt, agent.messages)

    async def post_llm(self, agent: Any, response: Any) -> None:
        """No-op for this implementation."""

    async def pre_tool(
        self, agent: Any, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Capture tool args for post_tool processing (e.g. skill name from get_skill)."""
        self._last_tool_args = args
        return args

    async def post_tool(
        self, agent: Any, tool_name: str, result: ToolResult
    ) -> ToolResult:
        """Observe the tool result, persist skill loads, and update context memory."""
        # Persist get_skill content into ContextManager so it survives message pruning
        if tool_name == "get_skill" and result.success:
            skill_name = self._last_tool_args.get("skill_name", "")
            if skill_name:
                self._ctx.on_skill_loaded(skill_name, result.content)
        self._ctx.observe(tool_name, result.content, result.success)
        if result.task_complete:
            agent._task_complete = True
            agent._mission_success = result.mission_success
        return result

    async def should_continue(self, agent: Any, step: int) -> bool:
        """Continue until an explicit completion signal is received."""
        return not getattr(agent, "_task_complete", False)
