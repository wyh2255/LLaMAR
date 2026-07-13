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

    async def on_run_start(self, agent: Any, user_message: str) -> None:
        """No-op for this implementation."""

    async def on_run_end(self, agent: Any, result: RunResult) -> None:
        """No-op for this implementation."""

    async def pre_llm(self, agent: Any, messages: list) -> list:
        """Refresh runtime state, prune history, and assemble context memory."""
        self._ctx.refresh_runtime_state()
        self._ctx.prune_history(agent.messages)
        return self._ctx.assemble(agent.system_prompt, agent.messages)

    async def post_llm(self, agent: Any, response: Any) -> None:
        """No-op for this implementation."""

    async def pre_tool(
        self, agent: Any, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Return args unchanged."""
        return args

    async def post_tool(
        self, agent: Any, tool_name: str, result: ToolResult
    ) -> ToolResult:
        """Observe the tool result and update context memory."""
        self._ctx.observe(tool_name, result.content, result.success)
        if result.task_complete:
            agent._task_complete = True
            agent._mission_success = result.mission_success
        return result

    async def should_continue(self, agent: Any, step: int) -> bool:
        """Continue until an explicit completion signal is received."""
        return not getattr(agent, "_task_complete", False)
