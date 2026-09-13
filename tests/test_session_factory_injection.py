"""env-contract G7: session-factory injection seam at the assembly sites.

The kernel build functions (``build_router_controller`` / ``build_controller``)
accept ``session_factory``.  These tests prove that the assembly sites
(``CoordinatorAgentExecutor`` / ``AgentAdapter``):

1. pass an explicit factory — defaulting to a factory equivalent to the kernel
   default (``CoordinatorContextManager`` / ``WorkerContextManager``), and
2. really consume an injected factory: its product becomes the session bound to
   a ``context_id`` inside the controller's session store.
"""

from __future__ import annotations

from Agent.router_agent.context import CoordinatorContextManager
from Agent.worker_agent.context import WorkerContextManager


class _ProbeFactory:
    """Records calls; returns one distinctive sentinel product."""

    def __init__(self):
        self.calls = 0
        self.product = object()

    def __call__(self):
        self.calls += 1
        return self.product


# ── Router (coordinator) side ───────────────────────────────────────────────


def test_router_assembly_consumes_injected_session_factory():
    from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
    from a2a.coordinator.agent_registry import AgentRegistry

    probe = _ProbeFactory()
    executor = CoordinatorAgentExecutor(registry=AgentRegistry(), session_factory=probe)
    controller = executor._controller

    # 注入件被装配处透传到控制器（identity）
    assert controller._session_factory is probe

    # 真实消费：会话按 context_id 懒创建，产品即注入件返回值
    session = controller._get_session("ctx-probe")
    assert session is probe.product
    assert probe.calls == 1

    # 同一 context_id 复用已建会话，不重复调用工厂
    assert controller._get_session("ctx-probe") is probe.product
    assert probe.calls == 1


def test_router_assembly_default_session_factory_is_equivalent():
    from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
    from a2a.coordinator.agent_registry import AgentRegistry

    executor = CoordinatorAgentExecutor(registry=AgentRegistry())
    session = executor._controller._get_session("ctx-default")

    # 缺省装配显式传入的工厂与内核 default 等价（CoordinatorContextManager）
    assert isinstance(session, CoordinatorContextManager)


# ── Worker side ─────────────────────────────────────────────────────────────


def test_worker_assembly_consumes_injected_session_factory():
    from a2a.worker.agent_adapter import AgentAdapter

    probe = _ProbeFactory()
    adapter = AgentAdapter(session_factory=probe)
    controller = adapter._controller

    assert controller._session_factory is probe
    assert controller._get_session("ctx-probe") is probe.product
    assert probe.calls == 1


def test_worker_assembly_default_session_factory_is_equivalent():
    from a2a.worker.agent_adapter import AgentAdapter

    adapter = AgentAdapter()
    session = adapter._controller._get_session("ctx-default")

    assert isinstance(session, WorkerContextManager)
