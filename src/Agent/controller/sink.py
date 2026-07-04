"""EventSink — 控制器的单一输出通道。

参照 DeepSeek-Reasonix 的 event.Sink：控制器把内核 step_callback 的三类事件
(llm_response / tool_start / tool_result) 单向发射到一个 EventSink，前端只需
实现自己的 Sink 把事件翻译到具体传输（A2A / CLI / 实验 logger）。

本模块只提供与传输无关的通用实现，不依赖任何前端库。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class EventSink(Protocol):
    """控制器输出通道协议。

    emit() 的签名与内核 Agent.run(step_callback=...) 完全一致：
    - "llm_response"(content, tool_calls, usage)
    - "tool_start"(tool_name, arguments)
    - "tool_result"(tool_name, success, content)
    实现内部的异常不应向控制器传播（由实现自行捕获记录）。
    """

    async def emit(self, type_: str, /, **data: Any) -> None: ...


class NullSink:
    """丢弃所有事件的 no-op sink。"""

    async def emit(self, type_: str, /, **data: Any) -> None:  # noqa: D401
        return None


class CallbackSink:
    """把已有的裸 step_callback 包装成 EventSink。

    兼容同步与异步回调（参照 core_agent.ReActAgent._fire 的处理）。回调异常
    只记录不抛出，避免影响 Agent 主循环。
    """

    def __init__(self, callback: Callable[..., Any | Awaitable[Any]] | None):
        self._callback = callback

    async def emit(self, type_: str, /, **data: Any) -> None:
        cb = self._callback
        if cb is None:
            return
        try:
            res = cb(type_, **data)
            if inspect.isawaitable(res):
                await res
        except Exception:
            logger.exception("CallbackSink callback(%s) failed", type_)


class TeeSink:
    """把事件扇出到多个 sink。

    复现现状里「传输 sink + 外部 step_callback 同时调用」的行为。任一子 sink
    抛出异常只记录，不影响其他 sink。
    """

    def __init__(self, sinks: list[EventSink]):
        self._sinks = [s for s in sinks if s is not None]

    async def emit(self, type_: str, /, **data: Any) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(type_, **data)
            except Exception:
                logger.exception("TeeSink sub-sink emit(%s) failed", type_)
