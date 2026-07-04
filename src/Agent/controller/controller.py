"""AgentController — 前端无关的统一编排层。

参照 DeepSeek-Reasonix 的 internal/control/controller.go：

- 前端只通过薄命令方法 submit()/cancel() 输入；
- 所有输出单向走注入的 EventSink；
- 「建哪个 Agent」由前端注入 agent_factory（保留 role/引擎覆盖扩展点）；
- 会话/上下文生命周期（context_id -> ContextManager + 锁）统一收敛在此。

控制器只依赖 Agent.* 内核，不 import 任何前端/传输库（a2a 等），保持分层。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from Agent.worker_agent.schema import Message, RunResult

from .sink import EventSink, NullSink

logger = logging.getLogger(__name__)


@runtime_checkable
class AgentEngine(Protocol):
    """控制器驱动的最小引擎协议。

    Agent 内核天然满足；LangGraph ReActAgent 也满足 add_user_message/run/
    get_history（缺 attach_context，由控制器用 hasattr 守卫）——即扩展点。
    """

    def add_user_message(self, content: str) -> None: ...

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        step_callback: Optional[Callable[..., Any]] = None,
    ) -> Any: ...

    def get_history(self) -> list[Message]: ...


# agent_factory(**kwargs) -> AgentEngine；kwargs 里可能含 extra_tools /
# system_prompt_override（由前端工厂决定接受哪些）。
AgentFactory = Callable[..., AgentEngine]
# session_factory() -> ContextManager（Worker/Coordinator 各自的实现）。
SessionFactory = Callable[[], Any]


@runtime_checkable
class SessionAPI(Protocol):
    """控制器对外契约（前端只依赖此协议，不碰 Agent 内部）。"""

    async def submit(
        self,
        context_id: str,
        query: str,
        sink: EventSink | None = None,
        *,
        extra_tools: list | None = None,
        system_prompt_override: str | None = None,
        task_id: str | None = None,
        initial_messages: list | None = None,
    ) -> RunResult: ...

    def cancel(self, context_id: str | None = None) -> None: ...

    def clear_sessions(self) -> None: ...

    def get_history(self, context_id: str | None = None) -> list[Message]: ...


class AgentController:
    """统一 Agent 编排层：会话存储 + run 驱动 + cancel + 完成语义。

    所有前端都通过 submit() 进入、通过注入的 EventSink 收取事件。
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        session_factory: SessionFactory,
        *,
        require_explicit_completion: bool = False,
    ):
        """初始化控制器。

        Args:
            agent_factory: 构建引擎的工厂。调用时可传 extra_tools /
                system_prompt_override（由工厂自行决定是否接受）。每次 submit
                都新建一个引擎实例，确保并发隔离。
            session_factory: 无参工厂，返回一个 ContextManager（会话记忆）。
            require_explicit_completion: 建完引擎后统一设置到引擎上（若引擎支持
                该属性），用于「只有显式 finish_task 才退出」的完成语义。
        """
        self._agent_factory = agent_factory
        self._session_factory = session_factory
        self._require_explicit_completion = require_explicit_completion

        # context_id -> ContextManager 会话存储（从前端上移，统一持有）
        self._sessions: dict[str, Any] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

        # context_id -> 正在运行的引擎与取消事件（支持多 session 并发 + cancel()）
        self._running_agents: dict[str, AgentEngine] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._submit_order: list[str] = []  # 记录 submit 顺序供 get_history 回溯

    # ------------------------------------------------------------------
    # 会话存储
    # ------------------------------------------------------------------
    def _get_session(self, context_id: str) -> Any:
        """获取或创建 context_id 对应的会话记忆。"""
        if context_id not in self._sessions:
            self._sessions[context_id] = self._session_factory()
        return self._sessions[context_id]

    def _get_session_lock(self, context_id: str) -> asyncio.Lock:
        """获取 context_id 对应的会话锁（串行化同一会话的并发 submit）。"""
        if context_id not in self._session_locks:
            self._session_locks[context_id] = asyncio.Lock()
        return self._session_locks[context_id]

    def clear_sessions(self) -> None:
        """清空所有会话存储（实验结束时调用）。"""
        self._sessions.clear()
        self._session_locks.clear()
        self._running_agents.clear()
        self._cancel_events.clear()
        self._submit_order.clear()

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------
    async def submit(
        self,
        context_id: str,
        query: str,
        sink: EventSink | None = None,
        *,
        extra_tools: list | None = None,
        system_prompt_override: str | None = None,
        cancel_event: asyncio.Event | None = None,
        task_id: str | None = None,
        initial_messages: list | None = None,
    ) -> RunResult:
        """驱动一次 Agent 运行。

        流程：取/建会话记忆 → 注入工厂建引擎 → attach_context（守卫）→
        add_user_message → run(step_callback=sink.emit) → 归一化结果。

        Args:
            context_id: 会话标识，决定复用哪份 ContextManager。
            query: 本轮用户输入。
            sink: 输出通道；None 时丢弃事件（NullSink）。
            extra_tools: 传给 agent_factory 的额外工具（coordinator 用）。
            system_prompt_override: 传给 agent_factory 的 system prompt 覆盖。
            cancel_event: 外部取消事件；不传则内部创建，供 cancel() 生效。

        Returns:
            RunResult（引擎返回 str 时归一化为 RunResult(content=...)）。
        """
        if sink is None:
            sink = NullSink()

        # 重置外部 cancel_event，避免前一次取消信号污染本次运行
        if cancel_event is not None:
            cancel_event.clear()

        async with self._get_session_lock(context_id):
            ctx = self._get_session(context_id)

            # 组织工厂参数：只在有值时传，兼容不接受这些参数的工厂
            factory_kwargs: dict[str, Any] = {}
            if extra_tools is not None:
                factory_kwargs["extra_tools"] = extra_tools
            if system_prompt_override is not None:
                factory_kwargs["system_prompt_override"] = system_prompt_override

            agent = self._agent_factory(**factory_kwargs)

            # 完成语义：若引擎支持该属性则统一设置（幂等）
            if hasattr(agent, "require_explicit_completion"):
                agent.require_explicit_completion = self._require_explicit_completion
            elif self._require_explicit_completion:
                logger.warning(
                    "AgentEngine %s lacks require_explicit_completion, "
                    "completion semantics disabled",
                    type(agent).__name__,
                )

            # 注入会话记忆——ReActAgent 无 attach_context，用 hasattr 守卫
            if ctx is not None:
                if hasattr(agent, "attach_context"):
                    agent.attach_context(ctx)
                else:
                    logger.warning(
                        "AgentEngine %s lacks attach_context, session memory lost",
                        type(agent).__name__,
                    )

            # 取消事件：外部优先，否则内部创建并挂到引擎，使 cancel() 真正生效
            ev = cancel_event or asyncio.Event()
            self._cancel_events[context_id] = ev
            self._running_agents[context_id] = agent
            self._submit_order.append(context_id)
            if hasattr(agent, "cancel_event"):
                agent.cancel_event = ev
            elif cancel_event is not None:
                logger.warning(
                    "AgentEngine %s lacks cancel_event, external cancel will not work",
                    type(agent).__name__,
                )

            # Restore from snapshot if provided (resume after input-required)
            if initial_messages:
                agent.messages = list(initial_messages)
                last = agent.messages[-1] if agent.messages else None
                if last and last.role == "assistant" and last.tool_calls:
                    agent.messages.append(
                        Message(
                            role="tool",
                            content=query,
                            tool_call_id=last.tool_calls[-1].id,
                            name=last.tool_calls[-1].function.name,
                        )
                    )
                else:
                    agent.add_user_message(query)
            else:
                agent.add_user_message(query)

            try:
                result = await agent.run(cancel_event=ev, step_callback=sink.emit)
            finally:
                self._running_agents.pop(context_id, None)
                self._cancel_events.pop(context_id, None)
                # _submit_order 保留供 get_history 回溯，不清除

            if result.need_input and task_id:
                ctx.save_snapshot(task_id, agent.messages)

            return self._normalize(result)

    def cancel(self, context_id: str | None = None) -> None:
        """请求取消正在运行的 agent。

        指定 context_id 时只取消对应会话；否则取消所有运行中的会话。
        可以从任意线程调用（内部处理 asyncio.Event 的线程安全性）。
        """
        if context_id is not None:
            ev = self._cancel_events.get(context_id)
            if ev is not None:
                self._signal_cancel(ev)
            return

        for ev in list(self._cancel_events.values()):
            self._signal_cancel(ev)

    @staticmethod
    def _signal_cancel(ev: asyncio.Event) -> None:
        """线程安全地设置取消事件。

        asyncio.Event.set() 内部通过 loop.call_soon() 唤醒 waiters，非线程安全。
        始终使用 call_soon_threadsafe() 确保从任意线程调用都安全。
        """
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            logger.warning("No running event loop, cancel signal lost")

    def get_history(self, context_id: str | None = None) -> list[Message]:
        """返回指定会话的消息历史。

        不传 context_id 时回溯最近一次 submit 的会话；
        无任何运行记录时返回空列表。
        """
        if context_id is not None:
            agent = self._running_agents.get(context_id)
            if agent is not None:
                return agent.get_history()
            return []

        # 回溯最近一次 submit（即使已结束也可以从 _submit_order 末尾回溯）
        # 注意：已结束的 agent 实例会被 pop，所以这里只查 running_agents
        for cid in reversed(self._submit_order):
            agent = self._running_agents.get(cid)
            if agent is not None:
                return agent.get_history()
        return []

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(result: Any) -> RunResult:
        """把引擎返回值归一化为 RunResult。

        Agent 内核返回 RunResult；LangGraph ReActAgent 返回 str，包装后统一
        暴露 .content 给下游消费方。
        """
        if isinstance(result, RunResult):
            return result
        if isinstance(result, str):
            return RunResult(content=result, success=None)
        # 其它意外类型：尽力取 content 字段，否则字符串化
        content = getattr(result, "content", None)
        if isinstance(content, str):
            return RunResult(content=content, success=getattr(result, "success", None))
        return RunResult(content=str(result), success=None)
