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


# ── Kernel server arc (env-contract P4-2): create-server factories ──────────
# 装配层把 Pack 提供的 session_factory 交给内核 create-server 工厂；后者必须
# 原样存下并透传到真实消费者（executor → controller 的会话工厂）。


def test_coordinator_server_stores_injected_session_factory():
    from a2a.coordinator.server import CoordinatorServer

    probe = _ProbeFactory()
    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        memory_read_mode="legacy",
        session_factory=probe,
    )

    assert server._session_factory is probe


def test_a2a_server_assembly_consumes_injected_session_factory():
    """``create_coordinator_a2a_server(session_factory=...)`` 直达 executor 控制器。"""
    from a2a.coordinator.a2a_server import create_coordinator_a2a_server

    probe = _ProbeFactory()
    server = create_coordinator_a2a_server(
        host="127.0.0.1", port=0, session_factory=probe
    )

    assert server.executor._controller._session_factory is probe
    assert server.executor._controller._get_session("ctx-kernel") is probe.product
    assert probe.calls == 1

