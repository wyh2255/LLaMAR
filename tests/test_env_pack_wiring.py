"""env-contract P4-2：通用骨架经 EnvPack 契约装配（fake pack + SAR 实况）。

两层证据：

1. ``test_generic_skeleton_assembles_through_env_pack`` —— 用一个与 SAR 无关的
   fake pack 驱动 ``OrchestratorCoordinator.start()``：证明骨架对环境零硬依赖，
   装配窗口按契约顺序调用工厂（观察源 → 摘要器 → state provider → 工具 →
   session 工厂 → 附属 LLM），且各产物确实进了 ``create_server`` 注入面。
2. SAR 侧事实测试 —— ``SAREnvPack`` 的工厂直通 identity（内核注入口四处 +
   session_factory 缺省 None）与 ``_semantic_map`` 兼容别名。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestration.coordinator import OrchestratorCoordinator
from orchestration.env_pack import CoordinatorEnv, EnvPack


class _FakePack(EnvPack):
    """非 SAR 的最小环境包：记录调用顺序并回填 ctx 前序产物断言。"""

    name = "fake"

    def __init__(self):
        self.calls: list[str] = []

    def build_observation_source(self, ctx):
        assert isinstance(ctx, CoordinatorEnv)
        self.ctx_at_observation = ctx
        self.calls.append("observation")
        return ("OBS",)

    def build_domain_summarizer(self, ctx):
        assert ctx.observation_source == ("OBS",)
        self.calls.append("summarizer")
        return ("SUMM",)

    def build_coordinator_state_provider(self, ctx):
        assert ctx.domain_summarizer == ("SUMM",)
        self.calls.append("provider")
        return SimpleNamespace()

    def build_coordinator_tools(self, ctx):
        self.calls.append("tools")
        return ["T"]

    def build_session_factory(self, *, role):
        self.calls.append(f"session:{role}")
        return ("SESSION",)

    def attach_auxiliary_llm(self, ctx):
        self.calls.append("aux_llm")


async def _no_sleep(_seconds):
    return None


@pytest.fixture()
def fake_server():
    calls = {"barrier": [], "run_control": [], "semantic": [], "ucq": []}
    server = SimpleNamespace(
        _router=SimpleNamespace(_system_prompt=None),
        _agent_registry=None,
        run=lambda: None,
        set_barrier=lambda b: calls["barrier"].append(b),
        set_run_control=lambda c: calls["run_control"].append(c),
        set_semantic_map=lambda m: calls["semantic"].append(m),
        set_user_command_queue=lambda q: calls["ucq"].append(q),
    )
    return server, calls


def test_generic_skeleton_assembles_through_env_pack(
    monkeypatch, tmp_path, fake_server
):
    server, scalls = fake_server
    pack = _FakePack()
    barrier = object()
    captured: dict = {}

    def fake_create_server(**kwargs):
        captured.update(kwargs)
        return server

    monkeypatch.setattr("a2a.coordinator.server.create_server", fake_create_server)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    coord = OrchestratorCoordinator(
        env_pack=pack,
        barrier=barrier,
        log_dir=str(tmp_path / "run"),
        state_mode="semantic",
        memory_read_mode="legacy",
    )
    asyncio.run(coord.start())

    # 装配窗口顺序（契约）：观察源 → 摘要器 → state provider → 工具 → session → 附属 LLM
    assert pack.calls == [
        "observation",
        "summarizer",
        "provider",
        "tools",
        "session:coordinator",
        "aux_llm",
    ]

    # 契约产物全量进 create_server 注入面
    assert captured["extra_tools"] == ["T"]
    assert captured["state_provider"] is coord._state_provider
    assert captured["session_factory"] == ("SESSION",)
    assert captured["prompts_dir"] is None
    assert captured["skills_dir"] is None
    assert captured["ui_dir"] is None
    assert captured["finish_task_tool_factory"] is None
    assert captured["environment_state_provider_factory"] is None
    assert captured["map_mcp_mount_hook"] is None
    assert captured["mcp_session_lifecycle_provider"] is None

    # 观测源经 ctx 回填后进入内核（非 None 才接 set_semantic_map）
    assert coord._observation_source == ("OBS",)
    assert scalls["semantic"] == [("OBS",)]
    assert scalls["barrier"][0] is barrier
    assert scalls["run_control"][0] is barrier
    assert scalls["ucq"][0] is coord._user_command_queue

    # ctx 透传：barrier / log_dir / state_mode 等运行时事实可达工厂
    ctx = pack.ctx_at_observation
    assert ctx.barrier is barrier
    assert ctx.log_dir == str(tmp_path / "run")
    assert ctx.state_mode == "semantic"
    assert ctx.max_steps == 50
    assert ctx.memory_read_mode == "legacy"
    assert ctx.user_command_queue is coord._user_command_queue


def test_sar_pack_factory_identity_and_defaults():
    """SAREnvPack：内核注入口四工厂 identity 直通；session_factory 缺省 None。"""
    from sar_orch.coordinator import (
        build_environment_state_provider,
        build_finish_task_tool,
        build_map_agent_session_lifecycle,
    )
    from sar_orch.env_pack import SAREnvPack
    from sar_orch.map_agent import mount_to_fastapi

    pack = SAREnvPack()
    assert isinstance(pack, EnvPack)
    assert pack.name == "sar"
    assert pack.finish_task_tool_factory is build_finish_task_tool
    assert pack.environment_state_provider_factory is build_environment_state_provider
    assert pack.map_mcp_mount_hook is mount_to_fastapi
    assert pack.mcp_session_lifecycle_provider is build_map_agent_session_lifecycle
    # P4-2 逐字等价：SAR 无环境 session 工厂 → None = 内核缺省（P4-1）
    assert pack.build_session_factory(role="coordinator") is None


def test_sar_coordinator_semantic_map_alias_reads_observation_source():
    """``_semantic_map`` 兼容别名（SAR 侧既有读者）指向 ``_observation_source``。"""
    from sar_orch.coordinator import SARCoordinator

    coord = SARCoordinator(barrier=None, memory_read_mode="legacy")
    assert coord._semantic_map is None
    assert coord._semantic_map is coord._observation_source
