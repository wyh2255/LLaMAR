"""env-contract P4-3：通用 Worker 骨架经 EnvPack 契约装配（fake pack + SAR 实况）。

两层证据：

1. ``test_generic_worker_assembles_through_env_pack`` —— 用一个与 SAR 无关的
   fake pack 驱动 ``OrchestratorWorker.start()``：证明骨架对环境零硬依赖，
   装配窗口按契约顺序调用工厂（state provider → 运行期接线 → 能力标签 →
   工具/服务装配内 session 工厂），各产物确实进了 ``create_worker_a2a_server``
   注入面，且环境包在 SAR 缺席时工具/MCP 不再被隐式加载。
2. 凭据/信封链路 —— ``env_file`` 装配面 + peer-mail 存储与回调签名器注入。
3. SAR 侧事实测试 —— ``SAREnvPack`` 的 worker 面产物（工具注册表装配逐条等价、
   能力标签、Action 格式化）与 ``SARWorker`` 薄壳委派。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestration.env_pack import EnvPack, WorkerEnv
from orchestration.worker import OrchestratorWorker


class _FakePack(EnvPack):
    """非 SAR 的最小环境包：记录调用顺序并回填 ctx 供断言。"""

    name = "fake"

    def __init__(self):
        self.calls: list[str] = []
        self.ctx_at_provider = None

    def build_worker_state_provider(self, ctx):
        assert isinstance(ctx, WorkerEnv)
        self.ctx_at_provider = ctx
        self.calls.append("provider")
        return ("PROVIDER",)

    def attach_worker_runtime(self, ctx):
        assert ctx is self.ctx_at_provider
        self.calls.append("runtime")

    @property
    def worker_capabilities(self):
        self.calls.append("capabilities")
        return ["fake-cap"]

    async def build_worker_tools(self, ctx):
        assert ctx is self.ctx_at_provider
        self.calls.append("tools")
        return ["T"]

    def build_session_factory(self, *, role):
        self.calls.append(f"session:{role}")
        return ("SESSION",)


class _FakeBarrier:
    """最小 barrier：首次空闲心跳即返回 finished，run() 循环立即退出。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def submit_action(self, agent_idx, action, *, advance=True, source=None):
        self.calls.append((agent_idx, action, advance, source))
        return {"finished": True}


@pytest.fixture()
def fake_a2a(monkeypatch):
    """拦截 worker 服务/客户端与收尾资源，捕获 create_worker_a2a_server 注入面。"""
    captured: dict = {}

    class FakeServer:
        def __init__(self):
            self.should_exit = False
            self.executor = SimpleNamespace()

        async def serve(self):
            return None

    def fake_create_worker_a2a_server(**kwargs):
        captured.update(kwargs)
        return FakeServer()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def connect(self):
            captured["connected"] = True

        async def disconnect(self):
            captured["disconnected"] = True

    async def no_shutdown(server, task):
        captured["shutdown"] = (server, task)

    async def no_cleanup(registry):
        captured["cleanup"] = registry

    monkeypatch.setattr(
        "a2a.worker.a2a_server.create_worker_a2a_server",
        fake_create_worker_a2a_server,
    )
    monkeypatch.setattr(
        "a2a.worker.coordinator_client.CoordinatorWebSocketClient", FakeClient
    )
    monkeypatch.setattr("orchestration.worker.shutdown_uvicorn_server", no_shutdown)
    monkeypatch.setattr("orchestration.worker.cleanup_mcp_connections", no_cleanup)
    return captured


def test_generic_worker_assembles_through_env_pack(fake_a2a, tmp_path):
    """骨架经 EnvPack 工厂装配：调用顺序、注入面、ctx 透传、心跳语义。"""
    pack = _FakePack()
    barrier = _FakeBarrier()
    worker = OrchestratorWorker(
        env_pack=pack,
        worker_id="w1",
        agent_name="Alice",
        agent_idx=0,
        barrier=barrier,
        log_dir=str(tmp_path / "w"),
        memory_read_mode="legacy",
        coordinator_url="ws://localhost:8080/",
    )
    worker.start()
    worker._thread.join(timeout=10)
    assert not worker._thread.is_alive(), "worker run() 未按 finished 心跳退出"

    # 装配窗口顺序（契约）：provider → runtime → capabilities（同步）→ tools →
    # session 工厂（工具装配后、服务构造参数求值时）
    assert pack.calls == [
        "provider",
        "runtime",
        "capabilities",
        "tools",
        "session:worker",
    ]

    # 契约产物全量进 create_worker_a2a_server 注入面
    assert fake_a2a["extra_tools"] == ["T"]
    assert fake_a2a["state_provider"] == ("PROVIDER",)
    assert fake_a2a["session_factory"] == ("SESSION",)
    assert fake_a2a["capabilities"] == ["fake-cap"]
    assert fake_a2a["prompts_dir"] is None
    assert fake_a2a["skills_dir"] is None
    assert fake_a2a["include_base_tools"] is False
    assert fake_a2a["require_explicit_completion"] is True
    assert fake_a2a["max_steps"] == 100

    # ctx 透传：运行参数 + 派生事实（http_url 换算 / coordinator_id 缺省）
    ctx = pack.ctx_at_provider
    assert ctx.worker_id == "w1"
    assert ctx.agent_name == "Alice"
    assert ctx.agent_idx == 0
    assert ctx.barrier is barrier
    assert ctx.log_dir == str(tmp_path / "w")
    assert ctx.model == "deepseek-v4-flash"
    assert ctx.coordinator_url == "ws://localhost:8080/"
    assert ctx.http_url == "http://localhost:8080"
    assert ctx.coordinator_id == "Coordinator"
    assert ctx.coordinator_secret is None
    assert ctx.memory_read_mode == "legacy"
    assert ctx.mcp_registry is worker._mcp_registry
    assert ctx.mailbox_store is None
    assert ctx.team_state_store is None
    assert ctx.peer_sender is None
    assert ctx.current_task_id == ""

    # 空闲心跳：advance=False + source=idle_heartbeat（任务活跃期间绝不提交）
    assert barrier.calls == [(0, "NoOp", False, "idle_heartbeat")]

    # 生命周期：连接 → 收尾（disconnect / shutdown / mcp cleanup 均被调用）
    assert fake_a2a["connected"] is True
    assert fake_a2a["disconnected"] is True
    assert fake_a2a["shutdown"][0] is worker._server
    assert fake_a2a["cleanup"] is worker._mcp_registry


def test_generic_worker_env_file_and_peer_mail_wiring(fake_a2a, monkeypatch, tmp_path):
    """env_file 装配面 + peer-mail 存储/签名器/team 协议上报全量注入。"""
    log_dir = tmp_path / "w"
    log_dir.mkdir()
    seen: dict = {}

    def fake_loader(path):
        seen["path"] = path
        return {"coordinator_secret": "secret-from-env-32bytes!!"}

    monkeypatch.setattr("orchestration.worker.load_env_file", fake_loader)

    pack = _FakePack()
    worker = OrchestratorWorker(
        env_pack=pack,
        worker_id="w2",
        agent_name="Bob",
        agent_idx=1,
        barrier=_FakeBarrier(),
        log_dir=str(log_dir),
        memory_read_mode="legacy",
        enable_peer_mail=True,
        coordinator_secret=None,
        env_file=tmp_path / ".env",
    )
    worker.start()
    worker._thread.join(timeout=10)
    assert not worker._thread.is_alive()

    # 凭据来自装配层指定的 env 文件（骨架不猜测路径）
    assert seen["path"] == str(tmp_path / ".env")

    # 信封装配全量（ENVELOPE-AWARE adapter 三件套 + 签名器）
    assert fake_a2a["envelope_ingress"] is not None
    assert fake_a2a["mailbox_store"] is not None
    assert fake_a2a["team_state_store"] is not None
    assert fake_a2a["callback_signer"] is not None
    assert fake_a2a["client_kwargs"]["supports_team_protocol"] is True

    # 解析后的 secret 进 ctx（state provider 的认证 read-port 依赖它）
    assert pack.ctx_at_provider.coordinator_secret == b"secret-from-env-32bytes!!"


def test_generic_worker_secure_mode_fails_closed_before_side_effects(
    fake_a2a, monkeypatch, tmp_path
):
    """read_port 无 secret → 构造期 fail-closed（typed），任何装配副作用之前。"""
    from a2a.coordinator.memory.callback_auth import MemoryAuthNotConfiguredError

    with pytest.raises(MemoryAuthNotConfiguredError) as exc:
        OrchestratorWorker(
            env_pack=_FakePack(),
            worker_id="w3",
            agent_name="Carol",
            agent_idx=0,
            barrier=None,
            log_dir=str(tmp_path / "w"),
            memory_read_mode="read_port",
        )
    assert exc.value.code == "memory_auth_not_configured"
    assert fake_a2a == {}


# ── SAR 侧事实：worker 面产物（P4-3 自 sar_orch/worker.py 迁入）──────────────


def _sar_worker_ctx(tmp_path, *, mailbox_store=None, peer_sender=None) -> WorkerEnv:
    return WorkerEnv(
        worker_id="w",
        agent_name="Alice",
        agent_idx=0,
        barrier=None,
        log_dir=str(tmp_path),
        model="deepseek-v4-flash",
        api_base="https://api.deepseek.com",
        api_key_env="OPENAI_API_KEY",
        memory_read_mode="legacy",
        exp_logger=None,
        coordinator_url="ws://localhost:8080",
        http_url="http://localhost:8080",
        coordinator_secret=None,
        mailbox_store=mailbox_store,
        peer_sender=peer_sender,
    )


def test_sar_pack_worker_capabilities_and_action_labels():
    """能力标签与 Action 格式化（迁移前 sar_orch/worker.py 逐字等价）。"""
    from sar_orch.env_pack import SAREnvPack

    pack = SAREnvPack()
    assert pack.worker_capabilities == ["sar", "navigation", "rescue", "firefighting"]

    # _build_action 语义：别名映射 + 参数 str(v) 以 ", " 连接；无参数 → Name()
    assert pack.format_worker_action("no_op", {}) == "NoOp()"
    assert pack.format_worker_action("no_op", {"x": 1}) == "NoOp(1)"
    assert (
        pack.format_worker_action("navigate_to", {"target_id": "Reservoir(1,2,0)"})
        == "NavigateTo(Reservoir(1,2,0))"
    )
    assert pack.format_worker_action("move", {"direction": "up"}) == "Move(up)"
    assert pack.format_worker_action("carry_person", {"person_id": 3}) == "CarryPerson(3)"
    assert (
        pack.format_worker_action("drop_off_person", {"target_id": "Deposit(9,9,0)"})
        == "DropOffPerson(Deposit(9,9,0))"
    )
    assert pack.format_worker_action("get_supply", {"reservoir_id": "R1"}) == "GetSupply(R1)"
    assert (
        pack.format_worker_action("store_supply", {"deposit_id": "D1"}) == "StoreSupply(D1)"
    )
    assert pack.format_worker_action("use_supply", {"supply": "Water"}) == "UseSupply(Water)"
    assert pack.format_worker_action("clear_inventory", {}) == "ClearInventory()"
    assert pack.format_worker_action("explore", {}) == "Explore()"
    # 未映射工具名直出
    assert pack.format_worker_action("report_observation", {"k": "v"}) == (
        "report_observation(v)"
    )


async def test_sar_pack_build_worker_tools_registry_equivalence(monkeypatch, tmp_path):
    """工具注册表装配逐条等价：16 件（含 mailbox/sender）/ 缺省剪枝 / MCP 容错。"""
    from Agent.worker_agent.tools import mcp_loader as mcp_loader_module
    from sar_orch.env_pack import SAREnvPack
    from sar_orch.tools.worker import SAR_WORKER_TOOLS

    pack = SAREnvPack()
    loaded: dict = {}

    async def fake_load(config_path, connection_registry=None):
        loaded["config_path"] = config_path
        loaded["registry"] = connection_registry
        return [SimpleNamespace(name="map_query")]

    monkeypatch.setattr(mcp_loader_module, "load_mcp_tools_async", fake_load)

    # 全量：mailbox + peer sender 提供 → 16 件 + 1 MCP
    ctx = _sar_worker_ctx(
        tmp_path, mailbox_store=object(), peer_sender=object()
    )
    tools = await pack.build_worker_tools(ctx)
    names = [t.name for t in tools]
    assert len(tools) == len(SAR_WORKER_TOOLS) + 1
    assert "read_mailbox" in names and "a2a_send_mail" in names
    assert names[-1] == "map_query"

    # MCP 配置导出：<log_dir>/mcp_<agent>.json 指向 coordinator http_url
    import json

    config_path = tmp_path / "mcp_Alice.json"
    assert loaded["config_path"] == str(config_path)
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["map_agent"]["url"] == (
        "http://localhost:8080/mcp/map"
    )
    assert loaded["registry"] is ctx.mcp_registry

    # ReportObservationTool 运行时依赖注入
    report = next(t for t in tools if t.name == "report_observation")
    assert report._agent_name == "Alice"
    assert report._task_id == ""

    # 缺省（无 peer mail 存储）：ReadMailboxTool / A2ASendMailTool 剪枝
    ctx2 = _sar_worker_ctx(tmp_path)
    tools2 = await pack.build_worker_tools(ctx2)
    names2 = [t.name for t in tools2]
    assert len(tools2) == len(SAR_WORKER_TOOLS) - 2 + 1
    assert "read_mailbox" not in names2 and "a2a_send_mail" not in names2

    # MCP 装载失败容错：告警并继续（核心工具不丢）
    async def failing_load(config_path, connection_registry=None):
        raise RuntimeError("map agent down")

    monkeypatch.setattr(mcp_loader_module, "load_mcp_tools_async", failing_load)
    tools3 = await pack.build_worker_tools(_sar_worker_ctx(tmp_path))
    assert len(tools3) == len(SAR_WORKER_TOOLS) - 2


def test_sar_pack_build_worker_state_provider_contract(tmp_path):
    """SARWorkerStateProvider 逐参构造（read_port 认证 URL / token 预算 / agent 名）。"""
    from sar_orch.env_pack import SAREnvPack
    from sar_orch.worker_state_provider import SARWorkerStateProvider

    pack = SAREnvPack()
    ctx = _sar_worker_ctx(tmp_path)
    ctx.coordinator_secret = b"secret-32bytes-0123456789abcdef"[:32]
    provider = pack.build_worker_state_provider(ctx)
    assert isinstance(provider, SARWorkerStateProvider)
    assert provider._barrier is None
    assert provider._agent_idx == 0
    assert provider._semantic_map_url == "http://localhost:8080"
    assert provider._coordinator_secret == ctx.coordinator_secret
    assert provider._memory_read_mode == "legacy"
    assert provider._token_limit == 80000
    assert provider._agent_name == "Alice"


def test_sar_worker_thin_shell_delegates_to_generic_skeleton():
    """``SARWorker`` 薄壳：签名面 + EnvPack 装配 + re-export identity。"""
    import sar_orch.worker as shell
    from orchestration.worker import ConfigurationError as GenericError
    from sar_orch.env_pack import SAREnvPack

    assert shell.ConfigurationError is GenericError

    prompts = "/tmp/sar-prompts/worker"
    worker = shell.SARWorker(
        worker_id="w",
        agent_name="Alice",
        agent_idx=0,
        barrier=None,
        memory_read_mode="legacy",
        prompts_dir=prompts,
    )
    assert isinstance(worker, OrchestratorWorker)
    assert isinstance(worker._env_pack, SAREnvPack)
    assert worker._env_pack.worker_prompts_dir == prompts
    assert worker._env_pack.worker_skills_dir == str(
        Path(prompts).parent.parent / "skills" / "worker"
    )
    # 现状 .env 位置：仓库根（迁移前 start() 内就地推导）
    assert worker._env_file == Path(shell.__file__).parent.parent / ".env"


def test_sar_worker_end_to_end_assembly_through_generic_skeleton(
    fake_a2a, monkeypatch, tmp_path
):
    """SAR 薄壳 × 通用骨架 × 真实 SAREnvPack：start() 全链装配等价。"""
    from Agent.worker_agent.tools import mcp_loader as mcp_loader_module
    from sar_orch.tools.worker import SAR_WORKER_TOOLS, _barrier_helpers
    from sar_orch.worker import SARWorker
    from sar_orch.worker_state_provider import SARWorkerStateProvider

    async def fake_load(config_path, connection_registry=None):
        return []

    monkeypatch.setattr(mcp_loader_module, "load_mcp_tools_async", fake_load)
    # 不读取真实仓库 .env（测试不写 os.environ；凭据链路另有专测）
    monkeypatch.setattr("orchestration.worker.load_env_file", lambda _p: {})
    # attach_worker_runtime 装配的发布器在 teardown 还原（避免跨测试污染）
    monkeypatch.setattr(_barrier_helpers, "_publisher", None)

    log_dir = tmp_path / "worker"
    log_dir.mkdir()
    worker = SARWorker(
        worker_id="Alice",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        a2a_port=8191,
        coordinator_url="ws://localhost:8080",
        prompts_dir=str(tmp_path / "prompts" / "worker"),
        log_dir=str(log_dir),
        memory_read_mode="legacy",
    )
    worker.start()
    worker._thread.join(timeout=10)
    assert not worker._thread.is_alive()

    # SAR 环境产物经 EnvPack 进注入面
    assert fake_a2a["capabilities"] == [
        "sar",
        "navigation",
        "rescue",
        "firefighting",
    ]
    assert isinstance(fake_a2a["state_provider"], SARWorkerStateProvider)
    assert fake_a2a["state_provider"]._agent_name == "Alice"
    assert fake_a2a["prompts_dir"] == Path(tmp_path / "prompts" / "worker")
    assert fake_a2a["skills_dir"] == tmp_path / "skills" / "worker"
    assert fake_a2a["session_factory"] is None
    # 14 件核心工具（无 peer mail → 剪枝 mailbox/sender 两件）
    tool_names = [t.name for t in fake_a2a["extra_tools"]]
    assert len(tool_names) == len(SAR_WORKER_TOOLS) - 2
    assert "no_op" in tool_names and "finish_task" in tool_names
    assert "read_mailbox" not in tool_names
    # MCP 配置导出 + 观测发布器装配
    assert (log_dir / "mcp_Alice.json").exists()
    assert _barrier_helpers._publisher is not None
    assert _barrier_helpers._publisher._agent_name == "Alice"
