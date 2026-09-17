"""Ai2ThorEnvPack 单测（env-contract P5-2）。

四层覆盖：

1. **pack 形状与守卫**：barrier 工厂（max_steps fail-fast / unknown env_params
   拒绝 / fake·unity 分支 / mode 校验）、state provider 类型、session 工厂
   （重建 ContextConfig、装配顺序守卫、未知角色）。
2. **worker 工具注册表**：``AI2THOR_WORKER_TOOLS`` 8 工具，全量绑定 barrier /
   agent_idx / ``barrier.alias_registry``（单一共享注册表实例）。
3. **verifier 背书的 finish_task 完成判定**：真回合（fake controller 注入
   metadata）→ 验收 / 拒绝 / fail-closed（真值不可达 ≠ 完成）/ 预装配回退内核
   validator / ``success=False`` 恒终态 / store ledger 记账。
4. **format_worker_action 一致性**：与工具实提交动作逐条对齐（方向型逐字、
   alias 型同动词且不泄 raw objectId）；另含 sar_orch 独立 import 探针。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ai2thor_orch.tests.fakes import FakeController, make_default_metadata

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 任务契约（AI2Thor/Tasks/3_transport_groceries/checker.py）的覆盖物。
GROCERIES = ["Bread", "Tomato", "Lettuce", "Apple", "Potato"]
FRIDGE_RAW_ID = "Fridge|+00.0|+00.0|+00.0"
MUG_RAW_ID = "Mug|-01.5|+00.9|+02.3"

MOVE_EXPECT: dict[str, str] = {
    "ahead": "MoveAhead",
    "back": "MoveBack",
    "left": "MoveLeft",
    "right": "MoveRight",
}
ROTATE_EXPECT: dict[str, str] = {"left": "RotateLeft", "right": "RotateRight"}
LOOK_EXPECT: dict[str, str] = {"up": "LookUp(30)", "down": "LookDown(30)"}


class _RecordingStore:
    """mission 级 ledger 记录桩（FinishTaskTool 的 store 透传面）。"""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def mark_finished(self, success: bool) -> None:
        self.calls.append(success)


def _grocery_metadata(*, in_fridge: bool) -> dict[str, Any]:
    """构造覆盖任务全部覆盖物的 controller metadata（in_fridge 控制放位结果）。"""
    metadata = make_default_metadata(
        scene="FloorPlan1", num_agents=1, has_objects=False
    )
    objects: list[dict[str, Any]] = [
        {
            "objectId": FRIDGE_RAW_ID,
            "objectType": "Fridge",
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "visible": True,
            "parentReceptacles": [],
        }
    ]
    for idx, name in enumerate(GROCERIES):
        objects.append(
            {
                "objectId": f"{name}|+0{idx}.0|+00.5|+00.0",
                "objectType": name,
                "position": {"x": float(idx), "y": 0.5, "z": 0.0},
                "visible": True,
                "parentReceptacles": ["Fridge"] if in_fridge else ["CounterTop"],
            }
        )
    metadata["objects"] = objects
    return metadata


def _build_pack(
    monkeypatch: pytest.MonkeyPatch, *, metadata: dict[str, Any] | None = None
) -> Any:
    """构造 pack；metadata 提供时把 ``create_controller`` 换成注入式 FakeController。"""
    import ai2thor_orch.env_pack as env_pack_mod

    if metadata is not None:

        def _factory(*, mode: str, scene: str, num_agents: int) -> FakeController:
            assert mode == "fake"
            return FakeController(metadata_override=metadata)

        monkeypatch.setattr(env_pack_mod, "create_controller", _factory)
    return env_pack_mod.Ai2ThorEnvPack()


def _coordinator_ctx(
    barrier: Any,
    *,
    state_mode: str = "semantic",
    memory_read_mode: str = "legacy",
    prune_policy: str = "count_window",
    log_dir: str | None = None,
) -> Any:
    from orchestration.env_pack import CoordinatorEnv

    return CoordinatorEnv(
        barrier=barrier,
        log_dir=log_dir,
        run_id="test-run",
        state_mode=state_mode,
        max_steps=50,
        model="test-model",
        api_base="http://localhost:1",
        api_key_env="TEST_KEY",
        exp_logger=None,
        event_store=None,
        supervision_state_store=None,
        user_command_queue=None,
        memory_read_mode=memory_read_mode,
        long_term_mode="off",
        long_term_store=None,
        diagnosis_store=None,
        diagnosis_config=None,
        prune_policy=prune_policy,
    )


def _worker_ctx(
    barrier: Any,
    *,
    agent_idx: int = 0,
    memory_read_mode: str = "legacy",
    prune_policy: str = "count_window",
    log_dir: str | None = None,
) -> Any:
    from orchestration.env_pack import WorkerEnv

    return WorkerEnv(
        worker_id="worker-0",
        agent_name="Alice",
        agent_idx=agent_idx,
        barrier=barrier,
        log_dir=log_dir,
        model="test-model",
        api_base="http://localhost:1",
        api_key_env="TEST_KEY",
        memory_read_mode=memory_read_mode,
        exp_logger=None,
        coordinator_url="ws://localhost:8080",
        http_url="http://localhost:8080",
        coordinator_secret=None,
        prune_policy=prune_policy,
    )


# ── 1. pack 形状与守卫 ─────────────────────────────────────────────────────


class TestPackSurface:
    """EnvPack 契约面的静态形状。"""

    def test_name_capabilities_and_hook_defaults(self, monkeypatch):
        from orchestration.env_pack import EnvPack

        pack = _build_pack(monkeypatch)
        assert isinstance(pack, EnvPack)
        assert pack.name == "ai2thor"
        assert pack.worker_capabilities == ["ai2thor", "navigation", "manipulation"]
        assert pack.ui_dir is None
        assert pack.build_observation_source(None) is None
        assert pack.build_domain_summarizer(None) is None
        assert pack.coordinator_skills_dir is None
        assert pack.worker_skills_dir is None
        assert Path(pack.coordinator_prompts_dir).is_dir()
        assert Path(pack.worker_prompts_dir).is_dir()

    def test_unsupported_task_fails_fast(self):
        from ai2thor_orch.env_pack import Ai2ThorEnvPack

        with pytest.raises(NotImplementedError):
            Ai2ThorEnvPack(task_id="2_open_all_cabinets")


class TestBarrierFactory:
    """barrier 工厂：wire 面 + 参数守卫 + 模式分支。"""

    def test_build_barrier_wires_controller_executor_registry(self, monkeypatch):
        from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
        from ai2thor_orch.visibility import AliasRegistry

        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=42, max_steps=5)
        assert isinstance(barrier, AI2ThorBarrier)
        assert barrier.num_agents == 1
        assert barrier.round_no == 0
        assert barrier.get_run_status().max_steps == 5
        # 共享别名单例：worker 工具与 barrier 观测脱敏读同一实例。
        assert isinstance(barrier.alias_registry, AliasRegistry)

    def test_env_params_override_mode_unity(self, monkeypatch):
        """env_params 的 mode 覆盖构造缺省 → 走 unity 分支（注入式 stub，不起真机）。"""
        from ai2thor_orch.executor import unity_controller as uc_mod

        seen: dict[str, Any] = {}

        class _UnityStub:
            def __init__(self, *, scene: str, num_agents: int) -> None:
                seen["scene"] = scene
                seen["num_agents"] = num_agents

        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)
        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(
            num_agents=2, seed=1, max_steps=3, mode="unity", scene="FloorPlan7"
        )
        assert barrier.num_agents == 2
        assert seen == {"scene": "FloorPlan7", "num_agents": 2}
        assert isinstance(barrier._executor._controller, _UnityStub)

    def test_max_steps_required(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        with pytest.raises(TypeError, match="max_steps"):
            pack.build_barrier(num_agents=1, seed=1)

    def test_unknown_env_params_rejected(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        with pytest.raises(TypeError, match="unexpected env_params"):
            pack.build_barrier(num_agents=1, seed=1, max_steps=3, foo="bar")

    def test_unity_mode_wires_unity_controller(self, monkeypatch):
        """unity 分支接线到 UnityController（注入 stub 断言入参，不起真机）。"""
        import ai2thor_orch.env_pack as env_pack_mod
        from ai2thor_orch.executor import unity_controller as uc_mod

        seen: dict[str, Any] = {}

        class _UnityStub:
            def __init__(self, *, scene: str, num_agents: int) -> None:
                seen["scene"] = scene
                seen["num_agents"] = num_agents

        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)
        controller = env_pack_mod.create_controller(
            mode="unity", scene="FloorPlan1", num_agents=3
        )
        assert isinstance(controller, _UnityStub)
        assert seen == {"scene": "FloorPlan1", "num_agents": 3}

    def test_unknown_mode_rejected(self):
        import ai2thor_orch.env_pack as env_pack_mod

        with pytest.raises(ValueError, match="Unknown mode"):
            env_pack_mod.create_controller(
                mode="nope", scene="FloorPlan1", num_agents=1
            )


# ── 2. worker 工具注册表 ───────────────────────────────────────────────────


class TestWorkerToolRegistry:
    """AI2THOR_WORKER_TOOLS 目录型注册表与 build_worker_tools 装配。"""

    def test_registry_shape(self):
        from Agent.worker_agent.tools.base import Tool
        from ai2thor_orch.tools.worker import AI2THOR_WORKER_TOOLS

        assert len(AI2THOR_WORKER_TOOLS) == 8
        names = []
        for tool_cls in AI2THOR_WORKER_TOOLS:
            assert issubclass(tool_cls, Tool)
            names.append(tool_cls(barrier=None, agent_idx=0, alias_registry=None).name)
        assert names == [
            "move",
            "rotate",
            "look",
            "navigate",
            "pickup",
            "put",
            "open_close",
            "done",
        ]

    async def test_build_worker_tools_binds_shared_registry(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        ctx = _worker_ctx(barrier)
        tools = await pack.build_worker_tools(ctx)
        assert len(tools) == 8
        for tool in tools:
            assert tool._barrier is barrier
            assert tool._alias_registry is barrier.alias_registry


# ── 3. state provider / session 工厂 ───────────────────────────────────────


class TestStateProvidersAndSessions:
    """state provider 工厂类型与 session 工厂（ContextConfig 重建 + 顺序守卫）。"""

    def test_coordinator_provider_type(self, monkeypatch):
        from ai2thor_orch.state.coordinator_state_provider import (
            AI2ThorCoordinatorStateProvider,
        )

        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        provider = pack.build_coordinator_state_provider(_coordinator_ctx(barrier))
        assert isinstance(provider, AI2ThorCoordinatorStateProvider)

    def test_worker_provider_type(self, monkeypatch):
        from ai2thor_orch.state.worker_state_provider import (
            AI2ThorWorkerStateProvider,
        )

        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        provider = pack.build_worker_state_provider(_worker_ctx(barrier, agent_idx=0))
        assert isinstance(provider, AI2ThorWorkerStateProvider)

    def test_coordinator_session_rebuilds_context_config(self, monkeypatch):
        from ai2thor_orch.state.context import AI2ThorCoordinatorContextManager

        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        ctx = _coordinator_ctx(
            barrier,
            state_mode="semantic",
            memory_read_mode="read_port",
            prune_policy="count_window",
            log_dir=None,
        )
        provider = pack.build_coordinator_state_provider(ctx)
        factory = pack.build_session_factory(role="coordinator")
        session = factory()
        assert isinstance(session, AI2ThorCoordinatorContextManager)
        # 重建的 ContextConfig 与骨架内联构造同参（P5-2 契约：运行参数回填）。
        assert session.config.strategy == "hybrid"
        assert session.config.recent_messages == 12
        assert session.config.pinned_enabled is True
        assert session.config.state_mode == "semantic"
        assert session.config.memory_read_mode == "read_port"
        assert session.config.prune_policy == "count_window"
        assert session.token_limit == 80000
        # state provider 必须随 session 注入（运行态注入载体）。
        assert session._state_provider is provider

    def test_worker_session_rebuilds_context_config(self, monkeypatch):
        from ai2thor_orch.state.context import AI2ThorWorkerContextManager

        pack = _build_pack(monkeypatch)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        ctx = _worker_ctx(
            barrier,
            agent_idx=0,
            memory_read_mode="legacy",
            prune_policy="count_window",
        )
        provider = pack.build_worker_state_provider(ctx)
        factory = pack.build_session_factory(role="worker")
        session = factory()
        assert isinstance(session, AI2ThorWorkerContextManager)
        # worker 侧内核骨架 state_mode 为常量 semantic。
        assert session.config.state_mode == "semantic"
        assert session.config.memory_read_mode == "legacy"
        assert session.config.prune_policy == "count_window"
        assert session.config.strategy == "hybrid"
        assert session.token_limit == 80000
        assert session._state_provider is provider

    def test_session_factory_requires_state_provider_first(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        with pytest.raises(RuntimeError, match="build_coordinator_state_provider"):
            pack.build_session_factory(role="coordinator")
        with pytest.raises(RuntimeError, match="build_worker_state_provider"):
            pack.build_session_factory(role="worker")

    def test_unknown_role_rejected(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        with pytest.raises(ValueError, match="Unknown role"):
            pack.build_session_factory(role="referee")


# ── 4. verifier 背书的 finish_task 完成判定 ────────────────────────────────


class TestFinishTaskCompletion:
    """finish_task 的 success=True 验收走现有 verifier（真回合真值）。"""

    async def test_accepts_when_verifier_confirms(self, monkeypatch):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=True))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        action_result = await barrier.submit_action(0, "MoveAhead")
        assert action_result.success

        store = _RecordingStore()
        tool = pack.finish_task_tool_factory(store)
        assert tool.name == "finish_task"
        result = await tool.execute(success=True, summary="Groceries in fridge.")
        assert result.success is True
        assert result.task_complete is True
        assert result.mission_success is True
        assert store.calls == [True]

    async def test_rejects_when_verifier_reports_incomplete(self, monkeypatch):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=False))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        await barrier.submit_action(0, "MoveAhead")

        store = _RecordingStore()
        tool = pack.finish_task_tool_factory(store)
        result = await tool.execute(success=True, summary="Claimed done.")
        assert result.success is False
        assert result.error == "mission_not_finished"
        assert "MISSION NOT COMPLETE" in result.content
        # 拒绝路径不写 ledger（未终态）。
        assert store.calls == []

    async def test_fail_closed_when_truth_unavailable(self, monkeypatch):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=True))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        await barrier.submit_action(0, "MoveAhead")

        def _boom() -> Any:
            raise RuntimeError("truth unavailable")

        monkeypatch.setattr(barrier, "last_round_result", _boom)
        tool = pack.finish_task_tool_factory(None)
        result = await tool.execute(success=True, summary="Claimed done.")
        assert result.success is False
        assert result.error == "mission_not_finished"

    async def test_success_false_is_terminal_despite_incomplete_truth(
        self, monkeypatch
    ):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=False))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        await barrier.submit_action(0, "MoveAhead")

        store = _RecordingStore()
        tool = pack.finish_task_tool_factory(store)
        result = await tool.execute(success=False, summary="Mission failed.")
        assert result.success is True
        assert result.task_complete is True
        assert result.mission_success is False
        assert store.calls == [False]

    async def test_falls_back_to_kernel_validator_before_barrier(self, monkeypatch):
        pack = _build_pack(monkeypatch)  # 未 build_barrier → 无环境真值

        rejected = await pack.finish_task_tool_factory(
            None, completion_validator=lambda: False
        ).execute(success=True, summary="Claimed done.")
        assert rejected.success is False
        assert rejected.error == "mission_not_finished"

        accepted = await pack.finish_task_tool_factory(
            None, completion_validator=lambda: True
        ).execute(success=True, summary="Done.")
        assert accepted.success is True
        assert accepted.task_complete is True


# ── 5. format_worker_action 一致性 ─────────────────────────────────────────


class TestFormatWorkerAction:
    """Action 标签与工具实提交动作逐条一致（P5-2 注册表/格式化契约）。"""

    async def test_direction_actions_match_submitted(self, monkeypatch):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=False))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=50)
        tools = {t.name: t for t in await pack.build_worker_tools(_worker_ctx(barrier))}
        ctrl = barrier._executor._controller

        for direction, expected in MOVE_EXPECT.items():
            result = await tools["move"].execute(direction=direction)
            assert result.success
            assert ctrl.actions_received[-1]["action"] == expected
            assert (
                pack.format_worker_action("move", {"direction": direction}) == expected
            )

        for direction, expected in ROTATE_EXPECT.items():
            await tools["rotate"].execute(direction=direction)
            assert ctrl.actions_received[-1]["action"] == expected
            assert (
                pack.format_worker_action("rotate", {"direction": direction})
                == expected
            )

        for direction, expected in LOOK_EXPECT.items():
            await tools["look"].execute(direction=direction)
            assert ctrl.actions_received[-1]["action"] == expected
            assert (
                pack.format_worker_action("look", {"direction": direction}) == expected
            )

        await tools["done"].execute()
        assert ctrl.actions_received[-1]["action"] == "Done"
        assert pack.format_worker_action("done", {}) == "Done"

    async def test_alias_actions_match_submitted_verbs(self, monkeypatch):
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=False))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=50)
        tools = {t.name: t for t in await pack.build_worker_tools(_worker_ctx(barrier))}
        ctrl = barrier._executor._controller

        mug_alias = barrier.alias_registry.register(MUG_RAW_ID)
        result = await tools["pickup"].execute(object_alias=mug_alias)
        assert result.success
        # 实提交走 raw id；标签保留可见 alias（不泄 raw）。
        assert ctrl.actions_received[-1]["action"] == f"PickupObject({MUG_RAW_ID})"
        fmt = pack.format_worker_action("pickup", {"object_alias": mug_alias})
        assert fmt == f"PickupObject({mug_alias})"
        assert MUG_RAW_ID not in fmt

        fridge_alias = barrier.alias_registry.register(FRIDGE_RAW_ID)
        await tools["put"].execute(receptacle_alias=fridge_alias)
        assert ctrl.actions_received[-1]["action"] == f"PutObject({FRIDGE_RAW_ID})"
        fmt = pack.format_worker_action("put", {"receptacle_alias": fridge_alias})
        assert fmt == f"PutObject({fridge_alias})"
        assert FRIDGE_RAW_ID not in fmt

        await tools["open_close"].execute(object_alias=fridge_alias, action="open")
        assert ctrl.actions_received[-1]["action"] == f"OpenObject({FRIDGE_RAW_ID})"
        assert (
            pack.format_worker_action(
                "open_close", {"object_alias": fridge_alias, "action": "open"}
            )
            == f"OpenObject({fridge_alias})"
        )

        await tools["open_close"].execute(object_alias=fridge_alias, action="close")
        assert ctrl.actions_received[-1]["action"] == f"CloseObject({FRIDGE_RAW_ID})"
        assert (
            pack.format_worker_action(
                "open_close", {"object_alias": fridge_alias, "action": "close"}
            )
            == f"CloseObject({fridge_alias})"
        )

    async def test_navigate_label_and_submission_match(self, monkeypatch):
        """navigate：标签 ``Teleport(<alias>)``，实提交为 Teleport dict 宏动作
        （position/rotation 参数与迁移前 NavigateTo 的 Teleport 同形状）。"""
        pack = _build_pack(monkeypatch, metadata=_grocery_metadata(in_fridge=False))
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=50)
        tools = {t.name: t for t in await pack.build_worker_tools(_worker_ctx(barrier))}
        ctrl = barrier._executor._controller

        # 第一回合先落一帧 metadata（navigate 的目标坐标读自最近回合）。
        await barrier.submit_action(0, "MoveAhead")

        bread_alias = barrier.alias_registry.register("Bread|+00.0|+00.5|+00.0")
        assert bread_alias == "Bread_1"
        result = await tools["navigate"].execute(target=bread_alias)
        assert result.success

        sent = ctrl.actions_received[-1]["raw"]
        assert set(sent) == {"action", "position", "rotation"}
        assert sent["action"] == "Teleport"
        assert set(sent["position"]) == {"x", "y", "z"}
        assert sent["rotation"]["y"] in {0.0, 90.0, 180.0, 270.0}
        # agentId 由 unity 执行层按 agent 槽位注入（ControllerExecutor →
        # build_action）；fake 链路 barrier 原样透传 dict 宏动作。
        # 映射层断言见 test_unity_controller.py::test_dict_action_and_extras_passthrough。

        fmt = pack.format_worker_action("navigate", {"target": bread_alias})
        assert fmt == f"Teleport({bread_alias})"
        # 标签与观测都不得出现 raw objectId。
        assert "|" not in fmt
        assert not barrier.alias_registry.is_raw_id_leaked(result.content)

    def test_unknown_tool_falls_back_to_base_format(self, monkeypatch):
        pack = _build_pack(monkeypatch)
        # 基类格式：name(args...) 逗号拼接。
        assert pack.format_worker_action("orbit", {"speed": 2}) == "orbit(2)"


# ── 6. sar_orch 独立 import 探针 ───────────────────────────────────────────


class TestImportIndependence:
    """ai2thor_orch.env_pack 不得引入 sar_orch（env-contract 依赖单向）。"""

    def test_import_env_pack_without_sar_orch(self):
        code = textwrap.dedent(
            """
            import sys

            import ai2thor_orch.env_pack  # noqa: F401

            leaked = sorted(
                m for m in sys.modules
                if m == 'sar_orch' or m.startswith('sar_orch.')
            )
            print('LEAKED=' + repr(leaked))
            sys.exit(1 if leaked else 0)
            """
        ).strip()
        env = dict(os.environ)
        env["PYTHONPATH"] = "src" + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
