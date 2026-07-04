"""Agent 统一控制器层。

参照 DeepSeek-Reasonix 的 Controller 架构：前端无关的编排层，所有前端都通过
AgentController.submit() 进入、通过 EventSink 单向收取事件。
"""

from .controller import (
    AgentController,
    AgentEngine,
    AgentFactory,
    SessionAPI,
    SessionFactory,
)
from .sink import CallbackSink, EventSink, NullSink, TeeSink

__all__ = [
    "AgentController",
    "AgentEngine",
    "AgentFactory",
    "SessionAPI",
    "SessionFactory",
    "EventSink",
    "CallbackSink",
    "NullSink",
    "TeeSink",
]
