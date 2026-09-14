"""UnityController 单测（env-contract P5-4）。

本机无 GPU —— 全部用 **ai2thor 5.0 形状的 mock controller** 覆盖可静态/单体验证
的部分：

1. **启动参数**：``unity_launch_options`` 的环境变量解析与显式参数优先；
2. **启动校验**：``agentCount`` 与 barrier ``num_agents`` 不一致即 fail-fast；
3. **动作映射**：7 件 worker 工具的全部动作串 + NoOp/Done 空动作 + 参数错误路径；
4. **事件归一化**：``MultiAgentEvent`` → barrier 消费面（``agents`` / ``objects`` /
   ``inventory``），含 objects 缺失回退与单 agent 事件；
5. **错误处理**：空手 PutObject 软失败 / ai2thor 调用级 ValueError 软失败 /
   基础设施异常上抛 / stop 幂等；
6. **executor 集成**：``ControllerExecutor.execute_step`` 的多 agent 槽位转发。

真机（真实 Unity / GPU）行为由 ``scripts/ai2thor_runtime_smoke.py --mode unity``
在 A100 上验证，本文件不依赖 ai2thor 运行。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.executor.unity_controller import (
    ActionMappingError,
    UnityController,
    _build_ai2thor_controller,
    unity_launch_options,
)
from ai2thor_orch.tests.fakes import MockA2TController

MUG_RAW_ID = "Mug|-01.5|+00.9|+02.3"
FRIDGE_RAW_ID = "Fridge|+00.0|+00.0|+01.0"

_ENV_KEYS = [
    "LLAMAR_AI2THOR_WIDTH",
    "LLAMAR_AI2THOR_HEIGHT",
    "LLAMAR_AI2THOR_HEADLESS",
    "LLAMAR_AI2THOR_PLATFORM",
    "LLAMAR_AI2THOR_X_DISPLAY",
    "LLAMAR_AI2THOR_GPU_DEVICE",
    "LLAMAR_AI2THOR_GRID_SIZE",
    "LLAMAR_AI2THOR_VISIBILITY",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 A100/本机环境变量，缺省即"未设置"。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ═══════════════════════════════════════════════════════════════════════════
# 测试工具
# ═══════════════════════════════════════════════════════════════════════════


def _make_controller(
    mock: MockA2TController | None = None, *, num_agents: int = 2, **kwargs: Any
) -> UnityController:
    """构造 UnityController（注入 mock 工厂，不起真机）。"""
    captured: dict[str, Any] = {}

    def _factory(options: dict[str, Any]) -> MockA2TController:
        captured.update(options)
        if mock is not None:
            return mock
        return MockA2TController(
            agent_count=int(options["agentCount"]), scene=str(options["scene"])
        )

    controller = UnityController(
        scene="FloorPlan1", num_agents=num_agents, controller_factory=_factory, **kwargs
    )
    controller._captured_options = captured  # type: ignore[attr-defined]
    return controller


# ═══════════════════════════════════════════════════════════════════════════
# 1. 启动参数（环境变量 ↔ 显式参数）
# ═══════════════════════════════════════════════════════════════════════════


class TestLaunchOptions:
    def test_defaults(self):
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

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLAMAR_AI2THOR_HEADLESS", "0")
        monkeypatch.setenv("LLAMAR_AI2THOR_PLATFORM", "cloud")
        monkeypatch.setenv("LLAMAR_AI2THOR_X_DISPLAY", ":99")
        monkeypatch.setenv("LLAMAR_AI2THOR_GPU_DEVICE", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEIGHT", "480")
        monkeypatch.setenv("LLAMAR_AI2THOR_GRID_SIZE", "0.1")
        monkeypatch.setenv("LLAMAR_AI2THOR_VISIBILITY", "2.0")

        options = unity_launch_options(scene="FloorPlan2", num_agents=3)
        assert options["headless"] is False
        assert options["platform"] == "CloudRendering"
        assert options["x_display"] == ":99"
        assert options["gpu_device"] == 1
        assert options["width"] == 640
        assert options["height"] == 480
        assert options["gridSize"] == 0.1
        assert options["visibilityDistance"] == 2.0
        assert options["agentCount"] == 3

    def test_explicit_args_beat_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLAMAR_AI2THOR_HEADLESS", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        options = unity_launch_options(
            scene="FloorPlan1", num_agents=1, headless=False, width=128
        )
        assert options["headless"] is False
        assert options["width"] == 128

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError, match="未知 platform"):
            unity_launch_options(
                scene="FloorPlan1", num_agents=1, platform="vulkan-9000"
            )

    def test_invalid_gpu_device_rejected(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLAMAR_AI2THOR_GPU_DEVICE", "not-a-number")
        with pytest.raises(ValueError):
            unity_launch_options(scene="FloorPlan1", num_agents=1)

    def test_zero_agents_rejected(self):
        with pytest.raises(ValueError, match="num_agents"):
            unity_launch_options(scene="FloorPlan1", num_agents=0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. 启动与生命周期
# ═══════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    def test_factory_receives_launch_options(self):
        controller = _make_controller(num_agents=2)
        captured = controller._captured_options  # type: ignore[attr-defined]
        assert captured["agentCount"] == 2
        assert captured["scene"] == "FloorPlan1"
        assert captured["headless"] is True
        assert controller.num_agents == 2
        assert controller.is_stopped is False

    def test_agent_count_mismatch_fails_fast(self):
        # 构建返回单 agent 事件的 build 替身 → 声明 2 agent 必须 fail-fast
        mock = MockA2TController(agent_count=1)
        with pytest.raises(RuntimeError, match="agentCount"):
            _make_controller(mock, num_agents=2)

    def test_single_agent_event_accepted(self):
        controller = _make_controller(num_agents=1)
        assert controller.num_agents == 1
        assert controller.last_metadata[0]["sceneName"] == "FloorPlan1"

    def test_stop_idempotent(self):
        mock = MockA2TController()
        controller = _make_controller(mock, num_agents=2)
        controller.stop()
        controller.stop()
        assert mock.stop_count == 1
        assert controller.is_stopped is True
        with pytest.raises(RuntimeError, match="stopped"):
            controller.step_for_agent(agent_idx=0, action="MoveAhead")

    def test_missing_ai2thor_raises_directive_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(sys.modules, "ai2thor.controller", None)
        with pytest.raises(ImportError, match="ai2thor-unity"):
            _build_ai2thor_controller({"scene": "FloorPlan1", "agentCount": 1})


# ═══════════════════════════════════════════════════════════════════════════
# 3. 动作映射
# ═══════════════════════════════════════════════════════════════════════════


class TestActionMapping:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("MoveAhead", {"action": "MoveAhead"}),
            ("MoveBack", {"action": "MoveBack"}),
            ("MoveLeft", {"action": "MoveLeft"}),
            ("MoveRight", {"action": "MoveRight"}),
            ("RotateLeft", {"action": "RotateLeft"}),
            ("RotateRight", {"action": "RotateRight"}),
            ("RotateLeft(45)", {"action": "RotateLeft", "degrees": 45}),
            ("LookUp(30)", {"action": "LookUp", "degrees": 30}),
            ("LookDown(30)", {"action": "LookDown", "degrees": 30}),
            (
                f"PickupObject({MUG_RAW_ID})",
                {"action": "PickupObject", "objectId": MUG_RAW_ID},
            ),
            (
                f"OpenObject({FRIDGE_RAW_ID})",
                {"action": "OpenObject", "objectId": FRIDGE_RAW_ID},
            ),
            (
                f"CloseObject({FRIDGE_RAW_ID})",
                {"action": "CloseObject", "objectId": FRIDGE_RAW_ID},
            ),
        ],
    )
    def test_plain_mappings(self, action: str, expected: dict[str, Any]):
        controller = _make_controller(num_agents=2)
        built = controller.build_action(action, 1)
        assert built == {**expected, "agentId": 1}

    @pytest.mark.parametrize("action", ["NoOp", "NoOp()", "Idle", "Done"])
    def test_empty_actions_map_to_pass(self, action: str):
        controller = _make_controller(num_agents=2)
        assert controller.build_action(action, 0) == {"action": "Pass", "agentId": 0}

    def test_dict_action_and_extras_passthrough(self):
        controller = _make_controller(num_agents=2)
        built = controller.build_action(
            {"action": "MoveAhead", "moveMagnitude": 0.5}, 1
        )
        assert built == {"action": "MoveAhead", "agentId": 1, "moveMagnitude": 0.5}

    def test_put_object_maps_target_container_as_object_id(self):
        """``PutObject(<container>)`` → 官方签名：objectId = 容器。

        RP2 真机：该 build 的 ``PutObject`` 不接受 ``receptacleObjectId``；
        ``objectId`` 即目标容器（手上持有物由仿真侧放入）。
        """
        mock = MockA2TController()
        controller = _make_controller(mock, num_agents=2)
        # agent 0 拿起 Mug（先走一次 pickup 让 inventory 有内容）
        controller.step_for_agent(agent_idx=0, action=f"PickupObject({MUG_RAW_ID})")
        built = controller.build_action(f"PutObject({FRIDGE_RAW_ID})", 0)
        assert built == {
            "action": "PutObject",
            "objectId": FRIDGE_RAW_ID,  # 目标容器（非手持物）
            "forceAction": True,  # Fridge：迁移前 base_env 的 issue #1210 约定
            "agentId": 0,
        }
        assert "receptacleObjectId" not in built

    def test_put_object_into_non_fridge_has_no_force_action(self):
        counter = {
            "objectId": "CounterTop|+00.0|+00.0|+00.0",
            "objectType": "CounterTop",
        }
        mock = MockA2TController(agent_count=1, objects=[counter])
        controller = _make_controller(mock, num_agents=1)
        # 走真实 pickup 步进，让 adapter 的持物缓存更新
        controller.step_for_agent(
            agent_idx=0, action=f"PickupObject({counter['objectId']})"
        )
        built = controller.build_action(f"PutObject({counter['objectId']})", 0)
        assert "forceAction" not in built
        assert "receptacleObjectId" not in built
        assert built["objectId"] == counter["objectId"]

    def test_put_object_step_succeeds_with_official_signature(self):
        """整步回归钉子（RP2）：新签名在 fake 上必须成功。

        修复前旧映射形状（``receptacleObjectId``）会被 fake 按真 build 参数
        白名单拒绝 → ``lastActionSuccess=False``；修复后 ``objectId`` 只传
        容器 → 成功且仿真状态里 Mug 落入 Fridge。
        """
        mock = MockA2TController(agent_count=1)
        controller = _make_controller(mock, num_agents=1)
        controller.step_for_agent(agent_idx=0, action=f"PickupObject({MUG_RAW_ID})")
        event = controller.step_for_agent(
            agent_idx=0, action=f"PutObject({FRIDGE_RAW_ID})"
        )
        assert event.metadata["lastActionSuccess"] is True
        sent = mock.steps[-1]
        assert sent["objectId"] == FRIDGE_RAW_ID
        assert "receptacleObjectId" not in sent
        mug = next(o for o in mock.objects if o["objectId"] == MUG_RAW_ID)
        assert mug["parentReceptacles"] == ["Fridge"]

    def test_legacy_receptacle_object_id_shape_rejected_by_fake(self):
        """RP2 回归钉子：旧映射形状（``receptacleObjectId``）离线即被拒绝。

        旧形状已不可能由 ``_map_put_object`` 产出；这里从 dict 透传路径显式
        构造它，证明 fake 的参数白名单会挡住 —— 即该缺陷当年在离线测试里
        就会红（RP2 教训：fake 不校验参数 → 真机才炸）。
        """
        mock = MockA2TController(agent_count=1)
        controller = _make_controller(mock, num_agents=1)
        controller.step_for_agent(agent_idx=0, action=f"PickupObject({MUG_RAW_ID})")
        event = controller.step_for_agent(
            agent_idx=0,
            action={
                "action": f"PutObject({FRIDGE_RAW_ID})",
                "objectId": MUG_RAW_ID,  # 旧形状：objectId 传手持物
                "receptacleObjectId": FRIDGE_RAW_ID,
            },
        )
        assert event.metadata["lastActionSuccess"] is False
        assert "invalid argument" in event.metadata["errorMessage"]
        assert "receptacleObjectId" in event.metadata["errorMessage"]

    def test_put_object_empty_hand_is_soft_failure(self):
        mock = MockA2TController(agent_count=1)
        controller = _make_controller(mock, num_agents=1)
        event = controller.step_for_agent(
            agent_idx=0, action=f"PutObject({FRIDGE_RAW_ID})"
        )
        assert event.metadata["lastActionSuccess"] is False
        assert "not holding" in event.metadata["errorMessage"].lower() or (
            "inventory" in event.metadata["errorMessage"].lower()
        )
        # 软失败不前进仿真
        assert mock.steps == []

    @pytest.mark.parametrize(
        "action",
        [
            "Teleport(x=1)",  # 未映射动作
            "PickupObject",  # 缺 objectId
            "PutObject",  # 缺 receptacle
            "LookUp(abc)",  # 度数非数值
            "Move Ahead",  # 非法格式
            "",
            123,  # type: ignore[arg-type]
        ],
    )
    def test_invalid_actions_raise_mapping_error(self, action: Any):
        controller = _make_controller(num_agents=1)
        with pytest.raises(ActionMappingError):
            controller.build_action(action, 0)

    def test_out_of_range_agent_idx_rejected(self):
        controller = _make_controller(num_agents=2)
        with pytest.raises(ValueError, match="out of range"):
            controller.build_action("MoveAhead", 2)


# ═══════════════════════════════════════════════════════════════════════════
# 4. 事件归一化（barrier 消费面）
# ═══════════════════════════════════════════════════════════════════════════


class TestEventNormalization:
    def test_multi_agent_metadata_surface(self):
        controller = _make_controller(num_agents=2)
        event = controller.step_for_agent(agent_idx=1, action="MoveAhead")
        metadata = event.metadata

        assert metadata["lastAction"] == "MoveAhead"
        assert metadata["lastActionSuccess"] is True
        assert metadata["agentId"] == 1

        agents = metadata["agents"]
        assert len(agents) == 2
        assert agents[0]["position"] == {"x": 0.0, "y": 0.9, "z": 0.0}
        assert agents[1]["position"] == {"x": 1.0, "y": 0.9, "z": 0.25}
        assert agents[1]["rotation"] == {"x": 0.0, "y": 90.0, "z": 0.0}
        assert isinstance(agents[0]["inventory"]["objects"], list)
        # verifier 走 metadata["objects"]（parentReceptacles 判定在 Fridge）
        assert any(obj["objectId"] == MUG_RAW_ID for obj in metadata["objects"])

    def test_per_agent_slice_not_active_agent(self):
        """归一化取的是该 agent 自己的 metadata（而非 MultiAgentEvent 的活跃者）。"""
        controller = _make_controller(num_agents=2)
        controller.step_for_agent(agent_idx=0, action="MoveAhead")
        event = controller.step_for_agent(agent_idx=1, action="MoveAhead")
        # agent 1 的自身视图里，自己的 z 前进了；agent 0 只在 agents 列表里
        assert event.metadata["agent"]["position"]["z"] == 0.25
        assert event.metadata["agents"][0]["position"]["z"] == 0.25
        assert event.metadata["agents"][1]["position"]["z"] == 0.25

    def test_objects_fallback_when_absent_from_per_agent_slice(self):
        """per-agent slice 缺 ``objects`` 时按顶层/兄弟回退（5.0 build 差异兜底）。"""

        class _BareEvent:
            def __init__(self, metadata: dict[str, Any]) -> None:
                self.metadata = metadata

        class _BareMulti:
            def __init__(self) -> None:
                objects = [{"objectId": MUG_RAW_ID, "objectType": "Mug"}]
                self.events = [
                    _BareEvent(
                        {"agentId": 0, "agent": {"position": {"x": 0, "y": 0, "z": 0}}}
                    ),
                    _BareEvent(
                        {"agentId": 1, "agent": {"position": {"x": 1, "y": 0, "z": 0}}}
                    ),
                ]
                self.metadata = {"objects": objects}

        class _BareController(MockA2TController):
            def step(self, action: dict[str, Any]) -> Any:
                self.steps.append(dict(action))
                return _BareMulti()

        controller = _make_controller(_BareController(), num_agents=2)
        event = controller.step_for_agent(agent_idx=0, action="MoveAhead")
        assert event.metadata["objects"][0]["objectId"] == MUG_RAW_ID
        assert len(event.metadata["agents"]) == 2

    def test_single_agent_event_normalized(self):
        controller = _make_controller(num_agents=1)
        event = controller.step_for_agent(agent_idx=0, action="MoveAhead")
        assert len(event.metadata["agents"]) == 1
        assert event.metadata["agents"][0]["position"]["z"] == 0.25

    def test_inventory_visible_in_agent_view(self):
        controller = _make_controller(num_agents=1)
        controller.step_for_agent(agent_idx=0, action=f"PickupObject({MUG_RAW_ID})")
        event = controller.step_for_agent(agent_idx=0, action="Pass")
        agents = event.metadata["agents"]
        assert agents[0]["inventory"]["objects"][0]["objectId"] == MUG_RAW_ID


# ═══════════════════════════════════════════════════════════════════════════
# 5. 错误处理
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_ai2thor_value_error_becomes_soft_failure(self):
        mock = MockA2TController(agent_count=1, fail_on={"MoveAhead"})
        controller = _make_controller(mock, num_agents=1)
        event = controller.step_for_agent(agent_idx=0, action="MoveAhead")
        assert event.metadata["lastActionSuccess"] is False
        assert "invalid argument" in event.metadata["errorMessage"]
        assert event.metadata["errorCode"] == "InvalidAction"
        # 后续 step 不受污染
        ok = controller.step_for_agent(agent_idx=0, action="Pass")
        assert ok.metadata["lastActionSuccess"] is True

    def test_infrastructure_error_propagates(self):
        mock = MockA2TController(agent_count=1, boom_on={"MoveAhead"})
        controller = _make_controller(mock, num_agents=1)
        with pytest.raises(RuntimeError, match="unity crashed"):
            controller.step_for_agent(agent_idx=0, action="MoveAhead")

    def test_mapping_error_propagates_loudly(self):
        controller = _make_controller(num_agents=1)
        with pytest.raises(ActionMappingError):
            controller.step_for_agent(agent_idx=0, action="Teleport(x=1)")


# ═══════════════════════════════════════════════════════════════════════════
# 6. ControllerExecutor 集成（槽位 → agentId 转发）
# ═══════════════════════════════════════════════════════════════════════════


class TestExecutorIntegration:
    def test_execute_step_routes_agents_by_slot(self):
        mock = MockA2TController()
        controller = _make_controller(mock, num_agents=2)
        executor = ControllerExecutor(controller)

        results = executor.execute_step(
            [{"action": "MoveAhead"}, {"action": "RotateLeft"}]
        )

        assert [step["action"] for step in mock.steps] == ["MoveAhead", "RotateLeft"]
        assert [step["agentId"] for step in mock.steps] == [0, 1]
        assert results[0]["agent_metadata"]["agentId"] == 0
        assert results[1]["agent_metadata"]["agentId"] == 1
        # barrier 的消费面：每份 metadata 都带 agents / objects
        for result in results:
            assert len(result["agent_metadata"]["agents"]) == 2
            assert result["agent_metadata"]["objects"]

    def test_executor_stop_delegates_to_controller(self):
        mock = MockA2TController()
        controller = _make_controller(mock, num_agents=2)
        executor = ControllerExecutor(controller)
        executor.stop()
        executor.stop()
        assert mock.stop_count == 1

    def test_plain_controller_without_step_for_agent_keeps_fake_contract(self):
        """无 ``step_for_agent`` 的 controller（FakeController）走单参 step 面。"""
        from ai2thor_orch.tests.fakes import FakeController

        fake = FakeController()
        executor = ControllerExecutor(fake)
        results = executor.execute_step([{"action": "MoveAhead"}])
        assert results[0]["agent_metadata"]["lastAction"] == "MoveAhead"
        assert fake.step_call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# 7. fake 参数白名单（RP2 回归：离线 fake 必须校验真 build 的动作参数）
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeArgumentSchema:
    def test_unknown_put_object_argument_rejected(self):
        """真 build 的 PutObject 不接受 receptacleObjectId → fake 同样拒绝。"""
        mock = MockA2TController(agent_count=1)
        event = mock.step(
            {
                "action": "PutObject",
                "objectId": MUG_RAW_ID,
                "receptacleObjectId": FRIDGE_RAW_ID,
                "agentId": 0,
            }
        )
        assert event.metadata["lastActionSuccess"] is False
        assert "invalid argument" in event.metadata["errorMessage"]
        assert "'receptacleObjectId'" in event.metadata["errorMessage"]

    def test_whitelisted_put_object_arguments_accepted(self):
        """白名单内参数（forceAction/placeStationary/randomSeed）不触发参数拒绝。"""
        mock = MockA2TController(agent_count=1)
        event = mock.step(
            {
                "action": "PutObject",
                "objectId": FRIDGE_RAW_ID,
                "forceAction": True,
                "placeStationary": True,
                "randomSeed": 0,
                "agentId": 0,
            }
        )
        # 空手 → 域内失败（Agent is not holding an object），但不是参数拒绝
        assert "invalid argument" not in event.metadata["errorMessage"]
