"""ReAct agent built on a LangGraph StateGraph — drop-in for src/Agent Agent.run().

复刻 Agent.run() 的全部语义：LLM→工具 ReAct 循环、step_callback 三类事件
(llm_response/tool_start/tool_result)、cancel_event 取消、max_steps 步数上限、
token 累计记账、token_limit 触发的消息历史摘要、AgentLogger 调试日志。
工具经 tools.adapter 包装现有 Tool 子类零改写接入。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, ToolException
from langgraph.graph import END, START, StateGraph

from Agent.router_agent.logger import AgentLogger
from Agent.router_agent.schema import FunctionCall, ToolCall as LegacyToolCall

from .messages import estimate_tokens, from_langchain, to_token_usage
from .tools.adapter import wrap_tool

logger = logging.getLogger(__name__)


class _AgentState(TypedDict):
    messages: list[BaseMessage]
    step: int
    cancelled: bool


class ReActAgent:
    """Single-agent ReAct loop mirroring Agent.run() semantics."""

    def __init__(
        self,
        llm: BaseChatModel,
        system_prompt: str,
        tools: list,
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
    ):
        self.llm = llm
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace info into system prompt (parity: agent.py:83-86)
        if "Current Workspace" not in system_prompt:
            system_prompt = (
                system_prompt
                + f"\n\n## Current Workspace\nYou are currently working in: "
                f"`{self.workspace_dir.absolute()}`\n"
                f"All relative paths will be resolved relative to this directory."
            )
        self.system_prompt = system_prompt

        # Wrap legacy Tool subclasses; pass through BaseTool instances unchanged
        self.tools: list[BaseTool] = [
            t if isinstance(t, BaseTool) else wrap_tool(t) for t in tools
        ]
        self._tools_by_name: dict[str, BaseTool] = {t.name: t for t in self.tools}
        self._bound_llm = llm.bind_tools(self.tools) if self.tools else llm

        self.messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
        self.logger = AgentLogger(log_dir=log_dir)

        # Token accounting (parity: agent.py cumulative fields)
        self.api_total_tokens: int = 0
        self.api_prompt_tokens: int = 0
        self.api_completion_tokens: int = 0
        self.cumulative_total_tokens: int = 0
        self.cumulative_prompt_tokens: int = 0
        self.cumulative_completion_tokens: int = 0
        self._skip_next_token_check: bool = False

        self.cancel_event: Optional[asyncio.Event] = None
        self._step_callback: Optional[Callable] = None
        self._graph = self._build_graph()

    # -- public API mirroring Agent -------------------------------------------

    def add_user_message(self, content: str) -> None:
        """Add a user message to history."""
        self.messages.append(HumanMessage(content=content))

    def get_history(self) -> list[BaseMessage]:
        """Get message history."""
        return list(self.messages)

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        step_callback: Optional[Callable] = None,
    ) -> str:
        """Execute the ReAct loop until task complete, cancelled, or max steps.

        Args mirror Agent.run(). step_callback receives three event types:
        "llm_response"(content, tool_calls, usage), "tool_start"(tool_name,
        arguments), "tool_result"(tool_name, success, content). Sync or async
        callbacks are both supported.
        """
        if cancel_event is not None:
            self.cancel_event = cancel_event
        self._step_callback = step_callback
        self._skip_next_token_check = False

        self.logger.start_new_run()

        init: _AgentState = {
            "messages": list(self.messages),
            "step": 0,
            "cancelled": False,
        }
        config = {"recursion_limit": self.max_steps * 3 + 5}
        final = await self._graph.ainvoke(init, config=config)
        self.messages = final["messages"]

        return self._extract_final_text(final["messages"])

    # -- graph construction ---------------------------------------------------

    def _build_graph(self):
        g = StateGraph(_AgentState)
        g.add_node("agent", self._agent_node)
        g.add_node("tools", self._tools_node)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", self._route_after_agent, ["tools", END])
        g.add_conditional_edges("tools", self._route_after_tools, ["agent", END])
        return g.compile()

    def _route_after_agent(self, st: _AgentState) -> str:
        if st.get("cancelled"):
            return END
        last = st["messages"][-1] if st["messages"] else None
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return END
        return "tools"

    def _route_after_tools(self, st: _AgentState) -> str:
        return END if st.get("cancelled") else "agent"

    # -- nodes ----------------------------------------------------------------

    async def _agent_node(self, st: _AgentState) -> dict:
        # Cancel check at step start (parity: _check_cancelled)
        if self._is_cancelled():
            msgs = list(st["messages"]) + [AIMessage(content="Task cancelled by user.")]
            return {"messages": msgs, "step": st["step"], "cancelled": True}

        # Max steps check at step start (parity: while step < max_steps)
        if st["step"] >= self.max_steps:
            msgs = list(st["messages"]) + [
                AIMessage(
                    content=f"Task couldn't be completed after {self.max_steps} steps."
                )
            ]
            return {"messages": msgs, "step": st["step"], "cancelled": False}

        # Summarize if token budget exceeded
        messages = await self._maybe_summarize(st["messages"])

        # Debug log: LLM request (convert to legacy Message for AgentLogger)
        try:
            self.logger.log_request(messages=from_langchain(messages), tools=self.tools)
        except Exception:
            logger.debug("AgentLogger.log_request failed", exc_info=True)

        response: AIMessage = await self._bound_llm.ainvoke(messages)

        # Token accounting from usage_metadata
        tu = to_token_usage(getattr(response, "usage_metadata", None))
        if tu is not None:
            self.api_total_tokens = tu.total_tokens
            self.api_prompt_tokens = tu.prompt_tokens
            self.api_completion_tokens = tu.completion_tokens
            self.cumulative_total_tokens += tu.total_tokens
            self.cumulative_prompt_tokens += tu.prompt_tokens
            self.cumulative_completion_tokens += tu.completion_tokens

        thinking = (
            getattr(response, "additional_kwargs", {}).get("reasoning_content")
            if hasattr(response, "additional_kwargs")
            else None
        )

        # Debug log: LLM response
        try:
            self.logger.log_response(
                content=str(response.content),
                thinking=thinking,
                tool_calls=self._lc_to_legacy_tool_calls(response.tool_calls),
                finish_reason=getattr(response, "response_stop_reason", None),
            )
        except Exception:
            logger.debug("AgentLogger.log_response failed", exc_info=True)

        # Fire llm_response step event (parity: agent.py:482-491).
        # tool_calls 转为 legacy ToolCall 格式 (有 .function.name)，对齐原版 Agent.run
        # 传给 step_callback 的格式，下游 _emit_agentic_event / AgentLogger 期望此格式。
        await self._fire(
            "llm_response",
            content=str(response.content),
            tool_calls=self._lc_to_legacy_tool_calls(response.tool_calls),
            usage=tu,
        )

        new_messages = messages + [response]
        return {"messages": new_messages, "step": st["step"] + 1, "cancelled": False}

    async def _tools_node(self, st: _AgentState) -> dict:
        messages = list(st["messages"])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            return {"messages": messages, "step": st["step"], "cancelled": False}
        tool_calls = last.tool_calls or []
        if not tool_calls:
            return {"messages": messages, "step": st["step"], "cancelled": False}

        cancelled = st.get("cancelled", False)
        for tc in tool_calls:
            # Cancel check before each tool (parity: agent.py:512-517)
            if self._is_cancelled():
                cancelled = True
                break

            tool_name = tc.get("name", "")
            arguments = tc.get("args", {}) or {}
            tc_id = tc.get("id", "")

            await self._fire("tool_start", tool_name=tool_name, arguments=arguments)

            tool = self._tools_by_name.get(tool_name)
            if tool is None:
                content_text = f"Error: Unknown tool: {tool_name}"
                success = False
            else:
                try:
                    result = await tool.ainvoke(arguments)
                    content_text = str(result)
                    success = True
                except ToolException as e:
                    content_text = f"Error: {e}"
                    success = False
                except Exception as e:  # noqa: BLE001
                    content_text = f"Error: {type(e).__name__}: {e}"
                    success = False

            # Debug log: tool result
            try:
                self.logger.log_tool_result(
                    tool_name=tool_name,
                    arguments=arguments,
                    result_success=success,
                    result_content=content_text if success else None,
                    result_error=content_text if not success else None,
                )
            except Exception:
                logger.debug("AgentLogger.log_tool_result failed", exc_info=True)

            await self._fire(
                "tool_result",
                tool_name=tool_name,
                success=success,
                content=content_text,
            )

            messages.append(
                ToolMessage(content=content_text, tool_call_id=tc_id, name=tool_name)
            )

            # Cancel check after each tool (parity: agent.py:628-633)
            if self._is_cancelled():
                cancelled = True
                break

        return {"messages": messages, "step": st["step"], "cancelled": cancelled}

    # -- helpers --------------------------------------------------------------

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    async def _fire(self, type_: str, **data: Any) -> None:
        """Invoke step_callback (sync or async). Exceptions are logged, not raised."""
        cb = self._step_callback
        if cb is None:
            return
        try:
            res = cb(type_, **data)
            if inspect.isawaitable(res):
                await res
        except Exception:
            logger.exception("step_callback(%s) failed", type_)

    @staticmethod
    def _lc_to_legacy_tool_calls(
        tool_calls: list | None,
    ) -> list[LegacyToolCall] | None:
        if not tool_calls:
            return None
        out = []
        for tc in tool_calls:
            out.append(
                LegacyToolCall(
                    id=tc.get("id", ""),
                    type="function",
                    function=FunctionCall(
                        name=tc.get("name", ""), arguments=tc.get("args", {})
                    ),
                )
            )
        return out

    async def _maybe_summarize(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Summarize history when token budget exceeded (parity: _summarize_messages)."""
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return messages

        estimated = estimate_tokens(messages)
        should = (
            estimated > self.token_limit or self.api_total_tokens > self.token_limit
        )
        if not should:
            return messages

        # Keep system prompt (index 0); keep all HumanMessages; summarize runs between.
        user_indices = [
            i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and i > 0
        ]
        if not user_indices:
            return messages

        new_messages: list[BaseMessage] = [messages[0]]
        for i, ui in enumerate(user_indices):
            new_messages.append(messages[ui])
            next_ui = (
                user_indices[i + 1] if i + 1 < len(user_indices) else len(messages)
            )
            run = messages[ui + 1 : next_ui]
            if run:
                summary_text = await self._summarize_run(run, i + 1)
                if summary_text:
                    new_messages.append(
                        SystemMessage(
                            content=f"[Assistant Execution Summary]\n\n{summary_text}"
                        )
                    )

        self._skip_next_token_check = True
        return new_messages

    async def _summarize_run(self, run: list[BaseMessage], round_num: int) -> str:
        """Summarize one execution round via a plain LLM call (no tools)."""
        run_text = self._format_run(run)
        try:
            resp = await self.llm.ainvoke(
                [
                    SystemMessage(
                        content="You are an assistant skilled at summarizing Agent execution processes."
                    ),
                    HumanMessage(
                        content=(
                            f"Please provide a concise summary of the following Agent execution process:\n\n"
                            f"{run_text}\n\nRequirements:\n"
                            "1. Focus on what tasks were completed and which tools were called\n"
                            "2. Keep key execution results and important findings\n"
                            "3. Be concise and clear, within 1000 words\n"
                            "4. Use English\n"
                            '5. Do not include "user" related content, only summarize the Agent\'s execution process'
                        )
                    ),
                ]
            )
            tu = to_token_usage(getattr(resp, "usage_metadata", None))
            if tu is not None:
                # Track summarization token usage (parity: _create_summary)
                self.cumulative_total_tokens += tu.total_tokens
                self.cumulative_prompt_tokens += tu.prompt_tokens
                self.cumulative_completion_tokens += tu.completion_tokens
            return str(resp.content)
        except Exception:
            logger.debug("summarize_run failed", exc_info=True)
            return run_text

    @staticmethod
    def _format_run(run: list[BaseMessage]) -> str:
        lines = []
        for m in run:
            if isinstance(m, AIMessage):
                lines.append(f"Assistant: {m.content}")
                if getattr(m, "tool_calls", None):
                    names = [tc.get("name", "") for tc in m.tool_calls]
                    lines.append(f"  → Called tools: {', '.join(names)}")
            elif isinstance(m, ToolMessage):
                lines.append(f"  ← Tool returned: {m.content}...")
        return "\n".join(lines)

    @staticmethod
    def _extract_final_text(messages: list[BaseMessage]) -> str:
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                return str(m.content) or ""
        return ""
