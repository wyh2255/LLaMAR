"""F-frame 运行时关键帧捕获：开关 / 捕获点 / 装配接线离线测试（卡 t_617f228c）。

设计基线：``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md``
§1.4 逐条：

- env 开关 ``LLAMAR_AI2THOR_FRAMES``：置位强制 ``headless=False``（platform
  不动）；与**显式** headless 置真互斥 → 启动前 fail-fast（不静默丢帧）；
  未置位 = 现状（缺省口径逐字不变 —— 等价性证据在 TestFramesSwitch /
  TestZeroSideEffectsWhenOff）。
- 捕获点（仅 frame_store 注入时）：①init 帧（先 ``Pass`` 强制渲染，round 0）；
  ②语义关键动作**成功**后按 tag 记帧（失败 / 软失败不记帧）；③run 终结
  （``stop()``）每 agent ``final`` + 一张 ``overhead``（``ToggleMapView``
  往返）。回合编号与 barrier step 同口径（agent 0 动作 = 新回合开始；navigate
  的只读查询不计入）。
- 装配接线：``FRAMES=1`` + unity → ``run_dir`` / ``agent_names`` 到达
  ``FrameStore`` 并注入 UnityController；fake → 不接线（无帧源，零副作用）；
  experiment 层透传 agent_names。

替身：``MockA2TController`` 子类在事件上挂构造帧（每 agent 值 = 槽位号 + 1，
ToggleMapView 帧值 = 99），全程离线确定性。
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

import ai2thor_orch.env_pack as env_pack_mod
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.executor.unity_controller import (
    UnityController,
    frames_enabled,
    unity_launch_options,
)
from ai2thor_orch.frames import FrameStore
from ai2thor_orch.tests.fakes import MockA2TController

pytestmark = pytest.mark.unit

MUG = "Mug|-01.5|+00.9|+02.3"
FRIDGE = "Fridge|+00.0|+00.0|+01.0"
REACHABLE = {"x": -1.5, "y": 0.9, "z": 0.0}
ROTATION = {"x": 0.0, "y": 90.0, "z": 0.0}


@pytest.fixture(autouse=True)
def _clean_ai2thor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉外溢的 ``LLAMAR_AI2THOR_*`` 变量（各测试自持开关，确定性优先）。"""
    for name in (
        "FRAMES",
        "HEADLESS",
        "PLATFORM",
        "WIDTH",
        "HEIGHT",
        "GRID_SIZE",
        "VISIBILITY",
        "X_DISPLAY",
        "GPU_DEVICE",
    ):
        monkeypatch.delenv(f"LLAMAR_AI2THOR_{name}", raising=False)


class _FrameMockController(MockA2TController):
    """``MockA2TController`` + 每 agent 帧 + ToggleMapView（真机 CloudRendering 可用）。

    帧内容 = agent 槽位号 + 1（构造帧，便于断言「谁的事件取出的帧」）；
    ``ToggleMapView`` 帧 = 99（俯视图标记）。
    """

    def step(self, action: dict[str, Any]) -> Any:
        name = str(action.get("action", ""))
        if name == "ToggleMapView":
            self.steps.append(dict(action))
            self.last_event = self._make_event(name, success=True)
            event = self.last_event
        else:
            event = super().step(action)
        value = 99 if name == "ToggleMapView" else 0
        for idx, sub in enumerate(event.events):
            sub.frame = np.full((2, 2, 3), idx + 1 if value == 0 else value, np.uint8)
        return event


def _controller(mock: Any, *, store: Any = None, **kwargs: Any) -> UnityController:
    return UnityController(
        scene="FloorPlan1",
        num_agents=mock.agent_count,
        controller_factory=lambda options: mock,
        frame_store=store,
        **kwargs,
    )


def _run_round(controller: UnityController, a0: Any, a1: Any) -> None:
    """经 ``ControllerExecutor`` 跑一个回合（与 barrier 真实调用路径一致）。"""
    ControllerExecutor(controller).execute_step([a0, a1])


def _tags(store: FrameStore, agent_idx: int) -> list[str]:
    return [tag for _round, _frame, tag in store.entries(agent_idx)]


# ═══════════════════════════════════════════════════════════════════════════
# 1. env 开关（LLAMAR_AI2THOR_FRAMES）
# ═══════════════════════════════════════════════════════════════════════════


class TestFramesSwitch:
    def test_default_off_and_launch_options_unchanged(self) -> None:
        """未置位 = 现状：缺省口径逐字不变（无新增键；headless 缺省 True）。"""
        assert frames_enabled() is False
        options = unity_launch_options(scene="FloorPlan1", num_agents=2)
        assert options == {
            "scene": "FloorPlan1",
            "width": 300,
            "height": 300,
            "headless": True,
            "agentCount": 2,
            "gridSize": 0.25,
            "visibilityDistance": 1.5,
        }

    def test_flag_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw, expected in (("1", True), ("true", True), ("0", False), ("off", False)):
            monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", raw)
            assert frames_enabled() is expected

    def test_frames_forces_headless_false_platform_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_PLATFORM", "cloud")
        options = unity_launch_options(scene="FloorPlan1", num_agents=1)
        assert options["headless"] is False  # 帧捕获的唯一途径（强制）
        assert options["platform"] == "CloudRendering"  # platform 不动

    def test_frames_with_headless_zero_is_consistent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEADLESS", "0")
        options = unity_launch_options(scene="FloorPlan1", num_agents=1)
        assert options["headless"] is False

    def test_frames_with_explicit_headless_one_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEADLESS", "1")
        with pytest.raises(ValueError, match="FRAMES"):
            unity_launch_options(scene="FloorPlan1", num_agents=1)

    def test_frames_with_headless_param_true_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        with pytest.raises(ValueError, match="headless"):
            unity_launch_options(scene="FloorPlan1", num_agents=1, headless=True)

    def test_frames_with_headless_param_false_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        options = unity_launch_options(scene="FloorPlan1", num_agents=1, headless=False)
        assert options["headless"] is False

    def test_controller_startup_fails_fast_before_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """互斥校验挂启动路径：controller 工厂都不会被触达。"""
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEADLESS", "1")

        def _explode(options: dict[str, Any]) -> Any:
            raise AssertionError("controller factory must not be reached")

        with pytest.raises(ValueError, match="FRAMES"):
            UnityController(
                scene="FloorPlan1", num_agents=1, controller_factory=_explode
            )

    def test_launch_options_carry_forced_headless_to_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        seen: dict[str, Any] = {}
        mock = _FrameMockController(agent_count=1)

        def _factory(options: dict[str, Any]) -> Any:
            seen.update(options)
            return mock

        UnityController(
            scene="FloorPlan1",
            num_agents=1,
            controller_factory=_factory,
            frame_store=FrameStore(),
        )
        assert seen["headless"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 2. 默认关闭 = 零帧零副作用
# ═══════════════════════════════════════════════════════════════════════════


class TestZeroSideEffectsWhenOff:
    def test_no_frame_store_no_extra_steps(self) -> None:
        """无 store：不起 init Pass、不附帧、stop 不加任何动作（逐字节等价面）。"""
        mock = _FrameMockController(agent_count=2)
        controller = _controller(mock)  # store=None（缺省）
        assert controller.frame_store is None
        assert mock.steps == []  # init 帧捕获未激活：零额外步骤

        _run_round(controller, f"PickupObject({MUG})", "NoOp")
        assert len(mock.steps) == 2  # 只有回合本身的 2 个动作

        controller.stop()
        assert mock.stop_count == 1
        assert len(mock.steps) == 2  # stop 不产生 final/overhead 动作


# ═══════════════════════════════════════════════════════════════════════════
# 3. 捕获点（store 注入 = 开关开启）
# ═══════════════════════════════════════════════════════════════════════════


class TestInitCapture:
    def test_init_frames_via_pass_round_zero(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore()
        controller = _controller(mock, store=store)
        assert controller.frame_store is store

        # 先 Pass 强制渲染：每 agent 一次（初始 last_event 帧为 None 的实测对策）
        assert mock.steps == [
            {"action": "Pass", "agentId": 0},
            {"action": "Pass", "agentId": 1},
        ]
        assert _tags(store, 0) == ["init"]
        assert _tags(store, 1) == ["init"]
        assert store.latest(0)[0] == 0  # round 0：任何回合执行之前
        assert int(store.latest(0)[1][0, 0, 0]) == 1  # agent 0 自己的帧
        assert int(store.latest(1)[1][0, 0, 0]) == 2  # agent 1 自己的帧


class TestActionCapture:
    def test_pickup_success_records_tag(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)  # 序列断言用大 ring（默认 N=1 只留最新）
        controller = _controller(mock, store=store)

        _run_round(controller, f"PickupObject({MUG})", "NoOp")

        assert _tags(store, 0) == ["init", "pickup_ok"]
        assert store.latest(0)[0] == 1
        assert int(store.latest(0)[1][0, 0, 0]) == 1
        assert _tags(store, 1) == ["init"]  # NoOp 不记帧

    def test_put_open_close_tags(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(controller, f"PickupObject({MUG})", "NoOp")
        _run_round(controller, f"PutObject({FRIDGE})", "NoOp")
        _run_round(controller, f"OpenObject({FRIDGE})", "NoOp")
        _run_round(controller, f"CloseObject({FRIDGE})", "NoOp")

        assert _tags(store, 0) == ["init", "pickup_ok", "put_ok", "open_ok", "close_ok"]
        assert [r for r, _f, _t in store.entries(0)] == [0, 1, 2, 3, 4]

    def test_navigate_teleport_tag(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(
            controller,
            {"action": "Teleport", "position": REACHABLE, "rotation": ROTATION},
            "NoOp",
        )
        assert _tags(store, 0) == ["init", "navigate_ok"]

    def test_done_maps_to_done_tag(self) -> None:
        """``Done`` 映射层落到空动作 Pass，但帧 tag 取编排层原名。"""
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(controller, "Done", "NoOp")
        assert _tags(store, 0) == ["init", "done"]

    def test_non_semantic_actions_never_capture(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        for action in ("MoveAhead", "LookUp(30)", "RotateLeft", "NoOp", "NoOp()"):
            _run_round(controller, action, "NoOp")

        assert _tags(store, 0) == ["init"]
        assert _tags(store, 1) == ["init"]

    def test_failed_action_not_recorded(self) -> None:
        """失败动作（soft failure 事件）绝不记帧——捕获只挂成功路径。"""
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(controller, "PickupObject(Nonexistent|+00.0|+00.0|+00.0)", "NoOp")

        assert _tags(store, 0) == ["init"]

    def test_empty_hand_put_soft_failure_not_recorded(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(controller, "NoOp", f"PutObject({FRIDGE})")  # agent 1 空手
        assert _tags(store, 1) == ["init"]


class TestRoundNumbering:
    def test_rounds_increment_per_round_and_query_not_counted(self) -> None:
        """回合编号 = barrier step 口径；navigate 只读查询不占回合、不漂移。"""
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        _run_round(
            controller,
            {"action": "Teleport", "position": REACHABLE, "rotation": ROTATION},
            "NoOp",
        )
        # navigate 工具的只读查询（burn 无回合，与 barrier.query_reachable_positions 同路径）
        controller.step_for_agent(
            agent_idx=0, action={"action": "GetReachablePositions"}
        )
        _run_round(controller, "Done", "NoOp")

        assert [(r, t) for r, _f, t in store.entries(0)] == [
            (0, "init"),
            (1, "navigate_ok"),
            (2, "done"),
        ]

    def test_latest_is_newest_under_default_ring(self) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore()
        controller = _controller(mock, store=store)

        _run_round(controller, f"OpenObject({FRIDGE})", "NoOp")
        _run_round(controller, f"CloseObject({FRIDGE})", "NoOp")

        round_no, _frame = store.latest(0)
        assert round_no == 2  # N=1：读面始终最新


class _BrokenAfterInit(_FrameMockController):
    """构造后对所有 step 抛基础设施异常（收尾捕获 best-effort 的极端样本）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.broken = False

    def step(self, action: dict[str, Any]) -> Any:
        if self.broken:
            raise RuntimeError("unity crashed (simulated)")
        return super().step(action)


class TestRunEndCapture:
    def test_stop_captures_final_and_overhead(self, tmp_path) -> None:
        mock = _FrameMockController(agent_count=2)
        store = FrameStore(tmp_path, ring_size=8, agent_names=["Alice", "Bob"])
        controller = _controller(mock, store=store)

        _run_round(controller, "Done", "NoOp")
        controller.stop()

        # 步骤序列：两 agent final（Pass）+ overhead 的 ToggleMapView 往返。
        assert mock.steps[-4:] == [
            {"action": "Pass", "agentId": 0},
            {"action": "Pass", "agentId": 1},
            {"action": "ToggleMapView", "agentId": 0},
            {"action": "ToggleMapView", "agentId": 0},  # 拍完 toggle 回来
        ]
        assert _tags(store, 0) == ["init", "done", "final", "overhead"]
        assert _tags(store, 1) == ["init", "final"]
        # overhead 帧 = ToggleMapView 事件（标记值 99）；round = 已执行回合数。
        assert int(store.latest(0)[1][0, 0, 0]) == 99
        assert store.latest(0)[0] == 1

        # 落盘路径契约（设计 §1.4）：<run_dir>/frames/<AgentName>/round_<N>_<tag>.png
        alice = sorted(p.name for p in (tmp_path / "frames" / "Alice").iterdir())
        assert alice == [
            "round_0_init.png",
            "round_1_done.png",
            "round_1_final.png",
            "round_1_overhead.png",
        ]
        bob = sorted(p.name for p in (tmp_path / "frames" / "Bob").iterdir())
        assert bob == ["round_0_init.png", "round_1_final.png"]

    def test_stop_capture_is_best_effort_when_steps_fail(self) -> None:
        """收尾路径：渲染失败只跳过（stop 绝不再抛），Controller.stop 照常。"""
        mock = _BrokenAfterInit(agent_count=1)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)
        assert _tags(store, 0) == ["init"]

        mock.broken = True  # 构造之后所有 step（Pass / ToggleMapView）全部炸
        controller.stop()
        assert mock.stop_count == 1  # 底层 stop 仍执行
        assert _tags(store, 0) == ["init"]  # final/overhead 跳过，仅记日志

    def test_stop_idempotent_capture_once(self) -> None:
        mock = _FrameMockController(agent_count=1)
        store = FrameStore(ring_size=8)
        controller = _controller(mock, store=store)

        controller.stop()
        steps_after_first = list(mock.steps)
        controller.stop()  # 幂等：不重复捕获
        assert mock.steps == steps_after_first
        assert _tags(store, 0) == ["init", "final", "overhead"]


# ═══════════════════════════════════════════════════════════════════════════
# 4. 装配接线（env_pack / create_controller / experiment）
# ═══════════════════════════════════════════════════════════════════════════


class _UnityStub:
    """UnityController 替身：记录构造 kwargs（不起真机；不消费 frame_store 之外的面）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = dict(kwargs)


class TestEnvPackWiring:
    def _pack(self, **kwargs: Any) -> Any:
        return env_pack_mod.Ai2ThorEnvPack(**kwargs)

    def test_frames_on_unity_wires_store_with_run_dir_and_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from ai2thor_orch.executor import unity_controller as uc_mod

        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)

        pack = self._pack(run_dir=tmp_path, agent_names=["Alice", "Bob"])
        pack.build_barrier(num_agents=2, seed=42, max_steps=5, mode="unity")

        stub = pack.barrier._executor._controller
        store = stub.init_kwargs["frame_store"]
        assert isinstance(store, FrameStore)
        assert store.frames_dir == tmp_path / "frames"
        assert pack.frame_store is store

    def test_frames_off_call_shape_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """开关关（缺省）：create_controller 调用形状逐字不变 + 无 store。"""
        from ai2thor_orch.executor import unity_controller as uc_mod

        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)
        pack = self._pack(run_dir=tmp_path, agent_names=["Alice", "Bob"])
        pack.build_barrier(num_agents=2, seed=42, max_steps=5, mode="unity")

        stub = pack.barrier._executor._controller
        assert stub.init_kwargs == {"scene": "FloorPlan1", "num_agents": 2}
        assert pack.frame_store is None

    def test_frames_on_fake_is_zero_side_effect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """fake 无帧源：FRAMES=1 不接线（与开关关等价，零额外动作）。"""
        captured: dict[str, Any] = {}
        real = env_pack_mod.create_controller

        def _spy(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return real(**kwargs)

        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setattr(env_pack_mod, "create_controller", _spy)

        pack = self._pack(run_dir=tmp_path, agent_names=["Alice", "Bob"])
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=3)

        assert "frame_store" not in captured
        assert pack.frame_store is None
        controller = barrier._executor._controller
        assert controller.actions_received == []  # 零额外动作
        assert not (tmp_path / "frames").exists()  # 零落盘

    def test_create_controller_rejects_store_for_fake(self) -> None:
        with pytest.raises(ValueError, match="frame_store"):
            env_pack_mod.create_controller(
                mode="fake",
                scene="FloorPlan1",
                num_agents=1,
                frame_store=FrameStore(),
            )


class TestExperimentWiring:
    def test_run_experiment_passes_agent_names_to_pack(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """装配层透传：run 级 agent 名成为帧目录名（与 run_dir 同一接线点）。"""
        import ai2thor_orch.experiment.ai2thor_experiment as exp_mod

        captured: dict[str, Any] = {}

        async def _spy(spec: Any) -> dict:
            captured["spec"] = spec
            return {"run_id": spec.run_id, "steps": 0, "finished": False}

        monkeypatch.setattr(exp_mod, "run_assembly", _spy)
        asyncio.run(
            exp_mod.run_experiment(
                num_agents=2, seed=42, mode="fake", max_steps=4, log_dir=str(tmp_path)
            )
        )

        pack = captured["spec"].env_pack
        assert pack._agent_names == ["Alice", "Bob"]
        assert pack._run_dir == str(tmp_path)

    def test_run_experiment_frames_store_end_to_end_paths(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRAMES=1 时 build_barrier 建出的 store 落 <run_dir>/frames（unity 分支）。"""
        import ai2thor_orch.experiment.ai2thor_experiment as exp_mod
        from ai2thor_orch.executor import unity_controller as uc_mod

        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)
        captured: dict[str, Any] = {}

        async def _spy(spec: Any) -> dict:
            captured["spec"] = spec
            spec.env_pack.build_barrier(
                num_agents=spec.num_agents, seed=spec.seed, **spec.env_params
            )
            return {"run_id": spec.run_id, "steps": 0, "finished": False}

        monkeypatch.setattr(exp_mod, "run_assembly", _spy)
        asyncio.run(
            exp_mod.run_experiment(
                num_agents=2,
                seed=42,
                mode="unity",
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        store = captured["spec"].env_pack.frame_store
        assert isinstance(store, FrameStore)
        assert store.frames_dir == tmp_path / "frames"
