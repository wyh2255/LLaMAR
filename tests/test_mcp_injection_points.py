"""MCP 集成注入点契约（P2b-2 / G3）。

内核（`src/a2a/coordinator/server.py`）不再 import `sar_orch.map_agent`，
环境侧 MCP 集成全部经两个注入点：

- ``map_mcp_mount_hook(app, semantic_map)`` —— 语义地图就绪时的挂载窗口
  （``set_semantic_map`` 与 ``_build_app`` 两处）；缺省 None：不挂载、静默跳过。
- ``mcp_session_lifecycle_provider() -> async context manager | None`` ——
  lifespan 启动段进入、收尾段对称退出；返回 None 表示本次不进入会话
  （SAR 侧对应"未挂载语义地图 → session_manager 不可用"的旧静默跳过）。

本文件验证：内核侧窗口时序与缺省容错（含驱动真实 lifespan），以及 SAR 侧
provider 的等价语义（未挂载 → None；已挂载 → ``session_manager.run()``）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from a2a.coordinator import server as server_mod
from a2a.coordinator.server import create_server


class _FakeExecutor:
    async def _periodic_state_sync(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass


class _FakeA2AServer:
    """Lifespan 里真实创建物之外的唯一重件（不监听的假 A2A server）。"""

    def __init__(self):
        self.should_exit = False
        self.executor = _FakeExecutor()

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0.01)


def _make_server(tmp_path, **kwargs):
    return create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path / "logs"),
        memory_read_mode="legacy",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# lifespan：会话生命周期进入 / 退出时序与容错
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_enters_and_exits_injected_mcp_lifecycle(tmp_path, monkeypatch):
    """provider 在启动段进入、收尾段对称退出（进入先于内部 a2a 装配，退出最后）。"""
    events = []

    @asynccontextmanager
    async def fake_lifecycle():
        events.append("mcp_enter")
        yield None
        events.append("mcp_exit")

    def fake_create_a2a_server(**kwargs):
        events.append("a2a_server_created")
        return _FakeA2AServer()

    monkeypatch.setattr(
        server_mod, "create_coordinator_a2a_server", fake_create_a2a_server
    )
    server = _make_server(
        tmp_path, mcp_session_lifecycle_provider=lambda: fake_lifecycle()
    )

    cm = server._app.router.lifespan_context(server._app)
    await cm.__aenter__()
    assert events == ["mcp_enter", "a2a_server_created"]
    await cm.__aexit__(None, None, None)
    assert events == ["mcp_enter", "a2a_server_created", "mcp_exit"]


@pytest.mark.asyncio
async def test_lifespan_tolerates_provider_returning_none(tmp_path, monkeypatch):
    """provider 返回 None（SAR 未挂载语义地图路径）→ 跳过会话，不报错。"""
    monkeypatch.setattr(
        server_mod, "create_coordinator_a2a_server", lambda **kwargs: _FakeA2AServer()
    )
    server = _make_server(tmp_path, mcp_session_lifecycle_provider=lambda: None)

    cm = server._app.router.lifespan_context(server._app)
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_lifespan_without_provider_stays_mcp_free(tmp_path, monkeypatch):
    """缺省（未注入）→ 不进入任何 MCP 会话，lifespan 正常起停。"""
    monkeypatch.setattr(
        server_mod, "create_coordinator_a2a_server", lambda **kwargs: _FakeA2AServer()
    )
    server = _make_server(tmp_path)

    cm = server._app.router.lifespan_context(server._app)
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 挂载窗口：set_semantic_map / _build_app
# ---------------------------------------------------------------------------


def test_create_server_passes_mcp_injection_points_through(tmp_path):
    hook = lambda app, sm: None
    provider = lambda: None
    server = _make_server(
        tmp_path, map_mcp_mount_hook=hook, mcp_session_lifecycle_provider=provider
    )
    assert server._map_mcp_mount_hook is hook
    assert server._mcp_session_lifecycle_provider is provider

    default = _make_server(tmp_path)
    assert default._map_mcp_mount_hook is None
    assert default._mcp_session_lifecycle_provider is None


def test_set_semantic_map_invokes_injected_hook(tmp_path):
    """注入 hook → 语义地图就绪时窗口调用一次 (app, store)；None 地图 → 不调用。"""
    from sar_orch.map import SemanticMapStore

    calls = []
    server = _make_server(
        tmp_path, map_mcp_mount_hook=lambda app, sm: calls.append((app, sm))
    )

    store = SemanticMapStore()
    server.set_semantic_map(store)
    assert calls == [(server._app, store)]
    assert server._semantic_map is store

    server.set_semantic_map(None)
    assert len(calls) == 1
    assert server._semantic_map is None


def test_set_semantic_map_without_hook_is_silent(tmp_path):
    """未注入 hook（缺省 None）→ 不挂载、不报错，ingest 引用照常设置。"""
    from sar_orch.map import SemanticMapStore

    server = _make_server(tmp_path)
    store = SemanticMapStore()
    server.set_semantic_map(store)  # 不得抛错，也不得 import sar_orch
    assert server._semantic_map is store


def test_build_app_mounts_when_semantic_map_preset(tmp_path):
    """app 构建窗口：语义地图已就位 + hook 存在 → 构建时挂载。"""
    from sar_orch.map import SemanticMapStore

    calls = []
    server = _make_server(
        tmp_path, map_mcp_mount_hook=lambda app, sm: calls.append((app, sm))
    )
    store = SemanticMapStore()
    server._semantic_map = store  # 模拟迟到装配路径

    app = server._build_app()
    assert calls == [(app, store)]


# ---------------------------------------------------------------------------
# SAR 侧 provider 等价语义
# ---------------------------------------------------------------------------


def test_map_agent_lifecycle_provider_none_when_unmounted(monkeypatch):
    """未挂载语义地图（session_manager 惰性未创建）→ RuntimeError 被容错为 None。"""
    from sar_orch.coordinator import build_map_agent_session_lifecycle
    from sar_orch.map_agent.server import mcp as map_mcp

    monkeypatch.setattr(map_mcp, "_session_manager", None)
    assert build_map_agent_session_lifecycle() is None


def test_map_agent_lifecycle_provider_returns_run_context_when_mounted():
    """已挂载 → 返回 session_manager.run() 异步上下文（不进入，仅取证）。"""
    from fastapi import FastAPI

    from sar_orch.coordinator import build_map_agent_session_lifecycle
    from sar_orch.map import SemanticMapStore
    from sar_orch.map_agent import mount_to_fastapi

    mount_to_fastapi(FastAPI(), SemanticMapStore())

    ctx = build_map_agent_session_lifecycle()
    assert ctx is not None
    assert hasattr(ctx, "__aenter__") and hasattr(ctx, "__aexit__")


@pytest.mark.asyncio
async def test_sar_style_injection_enters_real_mcp_session(tmp_path, monkeypatch):
    """SAR 等价组合端到端：真实 mount_to_fastapi + build_map_agent_session_lifecycle。

    语义地图就绪 → /mcp/map 挂载；lifespan 启动段真实进入 session manager 的
    ``run()`` 任务组；收尾段对称退出（与注入化之前的进入/退出路径相同）。
    """
    from sar_orch.coordinator import build_map_agent_session_lifecycle
    from sar_orch.map import SemanticMapStore
    from sar_orch.map_agent import mount_to_fastapi
    from sar_orch.map_agent.server import mcp as map_mcp

    monkeypatch.setattr(
        server_mod, "create_coordinator_a2a_server", lambda **kwargs: _FakeA2AServer()
    )
    server = _make_server(
        tmp_path,
        map_mcp_mount_hook=mount_to_fastapi,
        mcp_session_lifecycle_provider=build_map_agent_session_lifecycle,
    )
    server.set_semantic_map(SemanticMapStore())
    assert any(
        getattr(route, "path", None) == "/mcp/map" for route in server._app.routes
    )

    cm = server._app.router.lifespan_context(server._app)
    await cm.__aenter__()
    assert map_mcp.session_manager._has_started is True  # 启动段已进入会话
    await cm.__aexit__(None, None, None)
