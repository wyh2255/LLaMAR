"""Tests for AI2Thor worker restricted tools.

Tests cover:
- Each tool's name, description, and parameters schema are valid
- execute() returns ToolResult with correct success/error fields
- pickup/put/open_close correctly resolve alias→raw via AliasRegistry
- observation text is redacted (no raw objectId leakage)
"""

from __future__ import annotations

from typing import Any

import pytest

from Agent.worker_agent.tools.base import ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.executor.unity_controller import UnityController
from ai2thor_orch.tests.fakes import (
    FakeController,
    MockA2TController,
    make_default_metadata,
)
from ai2thor_orch.tools.worker.move import MoveTool
from ai2thor_orch.tools.worker.navigate import (
    NavigateTool,
    facing_yaw,
    nearest_candidates,
)
from ai2thor_orch.tools.worker.rotate import RotateTool
from ai2thor_orch.tools.worker.look import LookTool
from ai2thor_orch.tools.worker.pickup import PickupTool
from ai2thor_orch.tools.worker.put import PutTool
from ai2thor_orch.tools.worker.open_close import OpenCloseTool
from ai2thor_orch.tools.worker.done import DoneTool
from ai2thor_orch.visibility import AliasRegistry


# ── Helpers ─────────────────────────────────────────────────────────────


@pytest.fixture
def alias_registry():
    return AliasRegistry()


@pytest.fixture
def barrier(alias_registry):
    """Create a barrier with a single-agent FakeController."""
    controller = FakeController()
    executor = ControllerExecutor(controller)
    b = AI2ThorBarrier(
        num_agents=1,
        executor=executor,
        max_steps=50,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )
    return b


@pytest.fixture
def barrier_with_objects(alias_registry):
    """Barrier with metadata that includes known objects."""
    metadata = make_default_metadata(scene="FloorPlan1", num_agents=1, has_objects=True)
    controller = FakeController(metadata_override=metadata)
    executor = ControllerExecutor(controller)
    b = AI2ThorBarrier(
        num_agents=1,
        executor=executor,
        max_steps=50,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )
    return b


# ── Schema Tests ────────────────────────────────────────────────────────


class TestToolSchemas:
    """Verify name/description/parameters for each tool."""

    def test_move_schema(self, barrier, alias_registry):
        tool = MoveTool(barrier, 0, alias_registry)
        assert tool.name == "move"
        assert isinstance(tool.description, str) and len(tool.description) > 0
        params = tool.parameters
        assert params["type"] == "object"
        assert "direction" in params["properties"]
        assert params["properties"]["direction"]["enum"] == ["ahead", "back", "left", "right"]

    def test_rotate_schema(self, barrier, alias_registry):
        tool = RotateTool(barrier, 0, alias_registry)
        assert tool.name == "rotate"
        params = tool.parameters
        assert params["properties"]["direction"]["enum"] == ["left", "right"]

    def test_look_schema(self, barrier, alias_registry):
        tool = LookTool(barrier, 0, alias_registry)
        assert tool.name == "look"
        params = tool.parameters
        assert params["properties"]["direction"]["enum"] == ["up", "down"]

    def test_pickup_schema(self, barrier, alias_registry):
        tool = PickupTool(barrier, 0, alias_registry)
        assert tool.name == "pickup"
        params = tool.parameters
        assert "object_alias" in params["properties"]
        assert "object_alias" in params["required"]

    def test_put_schema(self, barrier, alias_registry):
        tool = PutTool(barrier, 0, alias_registry)
        assert tool.name == "put"
        params = tool.parameters
        assert "receptacle_alias" in params["properties"]
        assert "receptacle_alias" in params["required"]

    def test_open_close_schema(self, barrier, alias_registry):
        tool = OpenCloseTool(barrier, 0, alias_registry)
        assert tool.name == "open_close"
        params = tool.parameters
        assert "object_alias" in params["properties"]
        assert "action" in params["properties"]
        assert params["properties"]["action"]["enum"] == ["open", "close"]

    def test_done_schema(self, barrier, alias_registry):
        tool = DoneTool(barrier, 0, alias_registry)
        assert tool.name == "done"
        params = tool.parameters
        assert params["type"] == "object"
        # No required params for done
        assert "properties" in params

    def test_to_schema(self, barrier, alias_registry):
        """Verify to_schema() produces valid Anthropic-style schema."""
        tool = MoveTool(barrier, 0, alias_registry)
        schema = tool.to_schema()
        assert schema["name"] == "move"
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"

    def test_to_openai_schema(self, barrier, alias_registry):
        tool = RotateTool(barrier, 0, alias_registry)
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "rotate"


# ── Execution Tests ────────────────────────────────────────────────────


class TestMoveTool:
    @pytest.mark.asyncio
    async def test_move_ahead(self, barrier, alias_registry):
        tool = MoveTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="ahead")
        assert isinstance(result, ToolResult)
        assert result.success
        assert "MoveAhead" in result.content

    @pytest.mark.asyncio
    async def test_move_back(self, barrier, alias_registry):
        tool = MoveTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="back")
        assert result.success
        assert "MoveBack" in result.content

    @pytest.mark.asyncio
    async def test_move_invalid_direction(self, barrier, alias_registry):
        tool = MoveTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="sideways")
        assert not result.success
        assert "Invalid direction" in result.error

    @pytest.mark.asyncio
    async def test_move_observation_redacted(self, barrier_with_objects, alias_registry):
        """Verify observation text is redacted — no raw objectId leakage."""
        tool = MoveTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(direction="ahead")
        assert isinstance(result, ToolResult)
        assert result.success
        # Observation should contain no raw objectIds
        assert not alias_registry.is_raw_id_leaked(result.content), (
            f"Raw objectId leaked in observation: {result.content}"
        )


class TestRotateTool:
    @pytest.mark.asyncio
    async def test_rotate_left(self, barrier, alias_registry):
        tool = RotateTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="left")
        assert isinstance(result, ToolResult)
        assert result.success
        assert "RotateLeft" in result.content

    @pytest.mark.asyncio
    async def test_rotate_right(self, barrier, alias_registry):
        tool = RotateTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="right")
        assert result.success
        assert "RotateRight" in result.content

    @pytest.mark.asyncio
    async def test_rotate_invalid(self, barrier, alias_registry):
        tool = RotateTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="around")
        assert not result.success
        assert "Invalid direction" in result.error


class TestLookTool:
    @pytest.mark.asyncio
    async def test_look_up(self, barrier, alias_registry):
        tool = LookTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="up")
        assert isinstance(result, ToolResult)
        assert result.success
        assert "LookUp" in result.content

    @pytest.mark.asyncio
    async def test_look_down(self, barrier, alias_registry):
        tool = LookTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="down")
        assert result.success
        assert "LookDown" in result.content

    @pytest.mark.asyncio
    async def test_look_invalid(self, barrier, alias_registry):
        tool = LookTool(barrier, 0, alias_registry)
        result = await tool.execute(direction="side")
        assert not result.success


class TestPickupTool:
    @pytest.mark.asyncio
    async def test_pickup_known_alias(self, barrier_with_objects, alias_registry):
        """Pickup with a pre-registered alias resolves correctly."""
        # Pre-register the alias
        alias = alias_registry.register("Mug|-01.5|+00.9|+02.3")
        assert alias == "Mug_1"

        tool = PickupTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias)
        assert isinstance(result, ToolResult)
        assert result.success
        # The action string should contain the raw objectId
        assert "Mug" in result.content

    @pytest.mark.asyncio
    async def test_pickup_unknown_alias(self, barrier, alias_registry):
        tool = PickupTool(barrier, 0, alias_registry)
        result = await tool.execute(object_alias="NonExistent_99")
        assert not result.success
        assert "Unknown object alias" in result.error

    @pytest.mark.asyncio
    async def test_pickup_redacted_observation(self, barrier_with_objects, alias_registry):
        """Verify pickup observation has no raw objectId leaks."""
        alias = alias_registry.register("Mug|-01.5|+00.9|+02.3")
        tool = PickupTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias)
        assert not alias_registry.is_raw_id_leaked(result.content), (
            f"Raw objectId leaked in pickup observation: {result.content}"
        )


class TestPutTool:
    @pytest.mark.asyncio
    async def test_put_known_alias(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("CounterTop|+00.0|+00.0|+00.0")
        assert alias == "CounterTop_1"

        tool = PutTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(receptacle_alias=alias)
        assert isinstance(result, ToolResult)
        assert result.success

    @pytest.mark.asyncio
    async def test_put_unknown_alias(self, barrier, alias_registry):
        tool = PutTool(barrier, 0, alias_registry)
        result = await tool.execute(receptacle_alias="Mystery_99")
        assert not result.success
        assert "Unknown receptacle alias" in result.error

    @pytest.mark.asyncio
    async def test_put_redacted_observation(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("CounterTop|+00.0|+00.0|+00.0")
        tool = PutTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(receptacle_alias=alias)
        assert not alias_registry.is_raw_id_leaked(result.content)


class TestOpenCloseTool:
    @pytest.mark.asyncio
    async def test_open_known_alias(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("Fridge|-00.5|+01.0|+02.0")
        tool = OpenCloseTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias, action="open")
        assert isinstance(result, ToolResult)
        assert result.success

    @pytest.mark.asyncio
    async def test_close_known_alias(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("Cabinet|-01.0|+00.5|+01.0")
        tool = OpenCloseTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias, action="close")
        assert isinstance(result, ToolResult)
        assert result.success

    @pytest.mark.asyncio
    async def test_open_close_unknown_alias(self, barrier, alias_registry):
        tool = OpenCloseTool(barrier, 0, alias_registry)
        result = await tool.execute(object_alias="Ghost_42", action="open")
        assert not result.success
        assert "Unknown object alias" in result.error

    @pytest.mark.asyncio
    async def test_open_close_invalid_action(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("Fridge|-00.5|+01.0|+02.0")
        tool = OpenCloseTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias, action="toggle")
        assert not result.success
        assert "Invalid action" in result.error

    @pytest.mark.asyncio
    async def test_open_close_redacted(self, barrier_with_objects, alias_registry):
        alias = alias_registry.register("Fridge|-00.5|+01.0|+02.0")
        tool = OpenCloseTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute(object_alias=alias, action="open")
        assert not alias_registry.is_raw_id_leaked(result.content)


class TestDoneTool:
    @pytest.mark.asyncio
    async def test_done(self, barrier, alias_registry):
        tool = DoneTool(barrier, 0, alias_registry)
        result = await tool.execute()
        assert isinstance(result, ToolResult)
        assert result.success
        assert result.task_complete

    @pytest.mark.asyncio
    async def test_done_redacted(self, barrier_with_objects, alias_registry):
        """Even done with visible objects shouldn't leak raw IDs."""
        tool = DoneTool(barrier_with_objects, 0, alias_registry)
        result = await tool.execute()
        assert result.success
        assert not alias_registry.is_raw_id_leaked(result.content)


# ── Failure classification wiring ────────────────────────────────────────

# 录制样本（逐字取自 A100 首跑 trajectory.csv ErrorTypes；trace 已截断）
_RECORDED_VISIBILITY_FAILURE = (
    "NullReferenceException: Target object not found within the specified "
    "visibility.. trace:   at UnityStandardAssets.Characters.FirstPerson."
    "BaseFPSAgentController.getInteractableSimObjectFromId (System.String "
    "objectId, System.Boolean forceAction) [0x0007a]"
)
_RECORDED_BLOCKED_MOVE = (
    "StandardCounterHeightWidth is blocking Agent 1 from moving by "
    "(-0.2500, 0.0000, 0.0000)."
)
# 录制样本（逐字取自 RP4 真机 attempt1 @ ac74470，trajectory.csv 第 50-53 行
# ErrorTypes；trace 段截断，签名文案未改动）：
# 相机停在 +60 界残差 60.00002 后，teleportFull 对取用的该值抛异常。
_RECORDED_HORIZON_TELEPORT_FAILURE = (
    "ArgumentOutOfRangeException: Specified argument was out of the range of "
    "valid values.\\nParameter name: Each horizon must be in [-30:60]. You "
    "gave 60.00002.. trace:   at UnityStandardAssets.Characters.FirstPerson."
    "BaseFPSAgentController.teleportFull (UnityEngine.Vector3 position, "
    "UnityEngine.Vector3 rotation, System.Single horizon, System.Boolean "
    "forceAction)"
)
# LookUp/LookDown 越过 ±界的守卫拒绝（实测 down 形态；up 形态同函数对称分支）。
_RECORDED_LOOK_LIMIT_REFUSAL = (
    "can't look down beyond 60 degrees below the forward horizon"
)


class _HorizonFailingTeleportMock(MockA2TController):
    """Teleport 全候选回 horizon 越界异常原文（缺陷期 navigate 的失败形态探针）。"""

    def step(self, action: Any) -> Any:
        name = action.get("action") if isinstance(action, dict) else str(action)
        if name == "Teleport":
            self.steps.append(dict(action))
            self.last_event = self._make_event(
                "Teleport", success=False, message=_RECORDED_HORIZON_TELEPORT_FAILURE
            )
            return self.last_event
        return super().step(action)


class _FailingStepController(FakeController):
    """FakeController whose every step reports a domain failure."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__()
        self._failure_message = message
        self._failure_code = code

    def step(self, action_or_dict):
        event = super().step(action_or_dict)
        event.metadata["lastActionSuccess"] = False
        event.metadata["errorMessage"] = self._failure_message
        event.metadata["errorCode"] = self._failure_code
        return event


def _failing_barrier(message: str, code: str = "") -> AI2ThorBarrier:
    return AI2ThorBarrier(
        num_agents=1,
        executor=ControllerExecutor(_FailingStepController(message, code)),
        max_steps=5,
        step_timeout=5.0,
        alias_registry=AliasRegistry(),
    )


class TestFailureClassificationWiring:
    """Barrier-side errorMessage/errorCode must flow into the classifier via
    ToolResult.error (Agent.error_taxonomy), so ai2thor business failures
    classify to their domain category instead of unclassified_tool_error."""

    @pytest.mark.asyncio
    async def test_visibility_failure_classifies_object_not_visible(self):
        from Agent.error_taxonomy import error_code_for_result

        barrier = _failing_barrier(_RECORDED_VISIBILITY_FAILURE)
        registry = barrier.alias_registry
        alias = registry.register("Apple|-00.47|+01.15|+00.48")
        tool = PickupTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias)

        assert result.success is False
        assert _RECORDED_VISIBILITY_FAILURE in (result.error or "")
        assert error_code_for_result(result) == "object_not_visible"
        # 观测文本保持 alias 脱敏，不引入 raw objectId
        assert not registry.is_raw_id_leaked(result.content)

    @pytest.mark.asyncio
    async def test_blocked_move_classifies_navigation_blocked(self):
        from Agent.error_taxonomy import error_code_for_result

        barrier = _failing_barrier(_RECORDED_BLOCKED_MOVE)
        tool = MoveTool(barrier, 0, barrier.alias_registry)

        result = await tool.execute(direction="ahead")

        assert result.success is False
        assert _RECORDED_BLOCKED_MOVE in (result.error or "")
        assert error_code_for_result(result) == "navigation_blocked"

    @pytest.mark.asyncio
    async def test_empty_hand_soft_failure_classifies_state_mismatch(self):
        from Agent.error_taxonomy import error_code_for_result

        message = (
            "PutObject 要求该 agent 手上持有物体，但 agent 0 的 inventory 为空，"
            "无法放置到 Fridge|-02.10|+00.00|+01.07"
        )
        barrier = _failing_barrier(message, code="EmptyHand")
        registry = barrier.alias_registry
        alias = registry.register("Fridge|-02.10|+00.00|+01.07")
        tool = PutTool(barrier, 0, registry)

        result = await tool.execute(receptacle_alias=alias)

        assert result.success is False
        assert "[EmptyHand]" in (result.error or "")
        assert error_code_for_result(result) == "object_state_mismatch"

    @pytest.mark.asyncio
    async def test_horizon_limit_refusal_classifies_camera_horizon_out_of_range(self):
        """look 越界守卫拒绝（真机文案 + errorCode）→ camera_horizon_out_of_range。"""
        from Agent.error_taxonomy import error_code_for_result

        barrier = _failing_barrier(
            _RECORDED_LOOK_LIMIT_REFUSAL, code="LookDownCantExceedMin"
        )
        tool = LookTool(barrier, 0, barrier.alias_registry)

        result = await tool.execute(direction="down")

        assert result.success is False
        assert _RECORDED_LOOK_LIMIT_REFUSAL in (result.error or "")
        assert error_code_for_result(result) == "camera_horizon_out_of_range"

    @pytest.mark.asyncio
    async def test_navigate_horizon_exception_classifies_camera_horizon_out_of_range(
        self, alias_registry
    ):
        """navigate 失败链路上的 teleportFull horizon 异常原文 → 同类域码。

        载具 = ``_HorizonFailingTeleportMock``（Teleport 全候选回该异常），走
        真实 NavigateTool → barrier → unity 适配链，复刻缺陷期 attempt1 中
        ``agent_interactions.csv`` 记成 ``unclassified_tool_error`` 的那类失败。
        """
        from Agent.error_taxonomy import error_code_for_result

        mock = _HorizonFailingTeleportMock(agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        await barrier.submit_action(0, "Pass")  # 落帧（navigate 读对象坐标）

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target=mug_alias)

        assert result.success is False
        assert _RECORDED_HORIZON_TELEPORT_FAILURE in (result.error or "")
        assert error_code_for_result(result) == "camera_horizon_out_of_range"

    @pytest.mark.asyncio
    async def test_failure_without_detail_keeps_generic_fallback(self):
        """无 errorMessage/errorCode 时回退到通用文本，兜底仍落 unclassified。"""
        from Agent.error_taxonomy import error_code_for_result

        barrier = _failing_barrier("")
        tool = MoveTool(barrier, 0, barrier.alias_registry)

        result = await tool.execute(direction="back")

        assert result.success is False
        assert result.error == "Action MoveBack failed"
        assert error_code_for_result(result) == "unclassified_tool_error"


class TestBarrierFailureErrorComposition:
    """action_failure_error(): fallback × (errorMessage, errorCode) 组合。"""

    def test_composition_matrix(self):
        from ai2thor_orch.tools.worker._barrier_helpers import (
            action_failure_error,
        )

        class _Result:
            def __init__(self, raw):
                self.raw = raw

        assert action_failure_error("Failed", _Result(None)) == "Failed"
        assert action_failure_error("Failed", _Result({})) == "Failed"
        assert (
            action_failure_error("Failed", _Result({"errorMessage": "boom"}))
            == "Failed: boom"
        )
        assert (
            action_failure_error(
                "Failed", _Result({"errorMessage": "boom", "errorCode": "Code1"})
            )
            == "Failed: boom [Code1]"
        )
        assert (
            action_failure_error("Failed", _Result({"errorCode": "EmptyHand"}))
            == "Failed: [EmptyHand]"
        )
        # errorCode 已出现在 message 中时不重复追加 bracket 尾
        assert (
            action_failure_error(
                "Failed",
                _Result(
                    {"errorMessage": "EmptyHand violation", "errorCode": "EmptyHand"}
                ),
            )
            == "Failed: EmptyHand violation"
        )

    def test_object_not_visible_gets_action_hint(self):
        """object_not_visible 类失败：文本追加行动指引，归类结论不变。"""
        from Agent.error_taxonomy import classify_error
        from ai2thor_orch.tools.worker._barrier_helpers import (
            action_failure_error,
        )

        class _Result:
            def __init__(self, raw):
                self.raw = raw

        visibility = _Result(
            {
                "errorMessage": (
                    "Target object not found within the specified visibility"
                ),
                "errorCode": "NullReferenceException",
            }
        )
        text = action_failure_error("Failed to pick up Bread_1", visibility)
        assert "Not in view right now" in text
        assert "rotate/move" in text
        assert "do not retry the same alias blindly" in text
        # 归类结论不受指引后缀影响（仍是 C2a 的 object_not_visible）
        assert classify_error(text) == "object_not_visible"

    def test_non_visibility_failures_get_no_hint(self):
        """非 object_not_visible 类失败：文本与历史逐字一致（不追加指引）。"""
        from Agent.error_taxonomy import classify_error
        from ai2thor_orch.tools.worker._barrier_helpers import (
            action_failure_error,
        )

        class _Result:
            def __init__(self, raw):
                self.raw = raw

        blocked = _Result(
            {
                "errorMessage": (
                    "CounterTop is blocking Agent 1 from moving by (0.25, 0.0, 0.0)"
                )
            }
        )
        text = action_failure_error("Action MoveAhead failed", blocked)
        assert text == (
            "Action MoveAhead failed: CounterTop is blocking Agent 1 "
            "from moving by (0.25, 0.0, 0.0)"
        )
        assert classify_error(text) == "navigation_blocked"
        assert "Not in view right now" not in text


# ── F-nav: navigate 工具 ────────────────────────────────────────────────

#: Mug 的 raw objectId（MockA2TController 默认场景）与固定可达集。
MUG_RAW = "Mug|-01.5|+00.9|+02.3"
#: MockA2TController 的固定可达集（fakes._REACHABLE_POSITIONS 同源）。
REACHABLE = [
    {"x": -1.5, "y": 0.9, "z": 0.0},
    {"x": -1.25, "y": 0.9, "z": 0.25},
    {"x": -1.0, "y": 0.9, "z": 0.5},
]


def _object_entry(raw_id: str, object_type: str, x: float, z: float) -> dict[str, Any]:
    return {
        "objectId": raw_id,
        "objectType": object_type,
        "position": {"x": x, "y": 0.5, "z": z},
        "visible": True,
    }


def _fake_barrier(
    alias_registry: AliasRegistry,
    *,
    objects: list[dict[str, Any]] | None = None,
    num_agents: int = 1,
) -> AI2ThorBarrier:
    """FakeController 后端的 barrier（metadata_override 提供对象坐标）。"""
    metadata = make_default_metadata(scene="FloorPlan1", num_agents=num_agents)
    if objects is not None:
        metadata["objects"] = objects
    return AI2ThorBarrier(
        num_agents=num_agents,
        executor=ControllerExecutor(FakeController(metadata_override=metadata)),
        max_steps=20,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )


def _unity_mock_barrier(
    alias_registry: AliasRegistry, controller: MockA2TController
) -> AI2ThorBarrier:
    """MockA2TController（经 UnityController 归一化）后端的 barrier —— 全链路 e2e。"""
    unity = UnityController(
        scene="FloorPlan1",
        num_agents=controller.agent_count,
        controller_factory=lambda options: controller,
    )
    return AI2ThorBarrier(
        num_agents=controller.agent_count,
        executor=ControllerExecutor(unity),
        max_steps=20,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )


class _FlakyTeleportMock(MockA2TController):
    """前 ``fail_first`` 次 Teleport 返回软失败（候选 fallback 路径探针）。"""

    def __init__(self, *, fail_first: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._remaining_failures = fail_first

    def step(self, action: Any) -> Any:
        name = action.get("action") if isinstance(action, dict) else str(action)
        if name == "Teleport" and self._remaining_failures > 0:
            self._remaining_failures -= 1
            self.steps.append(dict(action))
            self.last_event = self._make_event(
                "Teleport", success=False, message="Teleport failed: target blocked"
            )
            return self.last_event
        return super().step(action)


class TestNavigateSchemaAndResolution:
    """navigate 的 schema 与别名解析（fail-closed）面。"""

    def test_schema(self, barrier, alias_registry):
        tool = NavigateTool(barrier, 0, alias_registry)
        assert tool.name == "navigate"
        assert isinstance(tool.description, str) and tool.description
        params = tool.parameters
        assert params["type"] == "object"
        assert "target" in params["properties"]
        assert params["required"] == ["target"]

    @pytest.mark.asyncio
    async def test_unknown_alias_fails_closed_without_burning_round(
        self, barrier, alias_registry
    ):
        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target="Ghost_42")
        assert result.success is False
        assert "Unknown object alias" in result.error
        # fail-closed：不提交动作、不烧回合
        assert barrier.round_no == 0
        assert barrier.get_run_status().step == 0

    @pytest.mark.asyncio
    async def test_bare_type_name_ambiguous_fails_closed(self, alias_registry):
        fridges = [
            _object_entry("Fridge|-02.10|+00.00|+01.07", "Fridge", -2.1, 1.07),
            _object_entry("Fridge|-01.00|+00.00|+01.07", "Fridge", -1.0, 1.07),
        ]
        barrier = _fake_barrier(alias_registry, objects=fridges)
        first = alias_registry.register(fridges[0]["objectId"])
        second = alias_registry.register(fridges[1]["objectId"])

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target="Fridge")

        assert result.success is False
        assert "Unknown object alias" in result.error
        assert first in result.error and second in result.error
        assert barrier.round_no == 0

    @pytest.mark.asyncio
    async def test_bare_type_name_unique_resolves(self, alias_registry):
        fridge = _object_entry("Fridge|-02.10|+00.00|+01.07", "Fridge", -2.1, 1.07)
        barrier = _fake_barrier(alias_registry, objects=[fridge])
        alias_registry.register(fridge["objectId"])  # 可见面注册（裸类型名解析前提）
        await barrier.submit_action(0, "Pass")  # 落一帧 metadata

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target="Fridge")

        assert result.success is True
        assert "Arrived beside Fridge." in result.content

    @pytest.mark.asyncio
    async def test_round_not_burned_before_any_metadata(self, barrier, alias_registry):
        """别名存在但当回合 metadata 尚未落帧 → fail-closed 且不烧回合。"""
        tool = NavigateTool(barrier, 0, alias_registry)
        alias_registry.register("Apple|+01.2|+00.5|+00.8")
        result = await tool.execute(target="Apple_1")
        assert result.success is False
        assert "Unknown object alias" in result.error
        assert barrier.round_no == 0


class TestNavigateFakeE2E:
    """fake e2e：navigate 后 agent 位置/朝向真实变化（MockA2T + UnityController 全链路）。"""

    @pytest.mark.asyncio
    async def test_navigate_teleports_agent_beside_target(self, alias_registry):
        mock = MockA2TController(agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        assert mug_alias == "Mug_1"

        # 正常情况下 alias 只可能来自已执行回合的可见面：先落一帧。
        await barrier.submit_action(0, "Pass")

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target=mug_alias)

        assert result.success is True
        assert not alias_registry.is_raw_id_leaked(result.content)
        # 最近可达点 = (-1.0, 0.9, 0.5)（x,z 平面距 Mug (-1.5, 2.3) 最近）
        assert barrier.snapshot_public(0).position == (-1.0, 0.9, 0.5)
        # 朝向 snap 到最近 90°（atan2(-0.5, 1.8) ≈ -15.5° → 0°）
        teleports = [s for s in mock.steps if s["action"] == "Teleport"]
        assert len(teleports) == 1
        assert teleports[0]["position"] == {"x": -1.0, "y": 0.9, "z": 0.5}
        assert teleports[0]["rotation"]["y"] == 0.0
        # 1 调用 = 1 barrier 回合（回合数 = prime 1 + navigate 1）
        assert barrier.get_run_status().step == 2

    @pytest.mark.asyncio
    async def test_reachable_positions_are_queried_once_per_run(self, alias_registry):
        mock = MockA2TController(agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        await barrier.submit_action(0, "Pass")

        tool = NavigateTool(barrier, 0, alias_registry)
        assert (await tool.execute(target=mug_alias)).success
        assert (await tool.execute(target=mug_alias)).success

        queries = [s for s in mock.steps if s["action"] == "GetReachablePositions"]
        assert len(queries) == 1  # 查询结果按 run 缓存
        teleports = [s for s in mock.steps if s["action"] == "Teleport"]
        assert len(teleports) == 2
        assert barrier.get_run_status().step == 3

    @pytest.mark.asyncio
    async def test_candidate_fallback_tries_next_nearest(self, alias_registry):
        mock = _FlakyTeleportMock(fail_first=1, agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        await barrier.submit_action(0, "Pass")

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target=mug_alias)

        assert result.success is True
        teleports = [s for s in mock.steps if s["action"] == "Teleport"]
        # 最近点失败 → 次近候选点重试（降序）
        assert [t["position"] for t in teleports] == [
            {"x": -1.0, "y": 0.9, "z": 0.5},
            {"x": -1.25, "y": 0.9, "z": 0.25},
        ]
        assert barrier.snapshot_public(0).position == (-1.25, 0.9, 0.25)

    @pytest.mark.asyncio
    async def test_all_candidates_fail_returns_actionable_error(self, alias_registry):
        mock = _FlakyTeleportMock(fail_first=3, agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        await barrier.submit_action(0, "Pass")

        tool = NavigateTool(barrier, 0, alias_registry)
        result = await tool.execute(target=mug_alias)

        assert result.success is False
        assert "Failed to navigate to Mug_1" in result.error
        assert "Move/rotate" in result.error
        # 至多 3 个候选点（3 次 Teleport、全部失败）
        teleports = [s for s in mock.steps if s["action"] == "Teleport"]
        assert len(teleports) == 3
        # 位置原地不动
        assert barrier.snapshot_public(0).position == (0.0, 0.9, 0.0)


class TestHorizonBoundaryRepro:
    """RP4 真机归因的离线复现与修复钉子（LookTool/NavigateTool ↔ 真机语义 mock 全链路）。

    真机现象（rp4_long120 attempt1 @ ac74470）：look 把相机压到 +60 界后 euler
    回读残留 60.00002 → ``teleportFull`` 严格校验把该 agent 之后每一步 Teleport
    全部拒绝（navigate 18 连败、跨 agent 传染、transport 停 1/22）。本类钉住
    修复后的行为：越界 look 仍被 build 拒（但现在归类为
    ``camera_horizon_out_of_range``），navigate 因映射层显式注入合法 horizon
    照常成功，并顺带把相机自愈回合法区间。
    """

    @pytest.mark.asyncio
    async def test_look_to_limit_then_navigate_survives(self, alias_registry):
        from Agent.error_taxonomy import error_code_for_result

        mock = MockA2TController(agent_count=1)
        barrier = _unity_mock_barrier(alias_registry, mock)
        mug_alias = alias_registry.register(MUG_RAW)
        await barrier.submit_action(0, "Pass")  # 落帧（navigate 读对象坐标）

        look = LookTool(barrier, 0, alias_registry)
        assert (await look.execute(direction="down")).success  # 30.00001
        assert (await look.execute(direction="down")).success  # 60.00002（+60 界残差）
        refused = await look.execute(direction="down")  # 越界：真 build 拒绝
        assert refused.success is False
        assert error_code_for_result(refused) == "camera_horizon_out_of_range"

        # 缺陷状态下 navigate 照常移动：Teleport 显式带夹取后的 horizon。
        navigate = NavigateTool(barrier, 0, alias_registry)
        result = await navigate.execute(target=mug_alias)
        assert result.success is True
        assert "Arrived beside Mug_1." in result.content
        teleports = [s for s in mock.steps if s["action"] == "Teleport"]
        assert teleports[-1]["horizon"] == 59.9  # 显式合法值（非 60.00002 残差）
        assert mock._camera_horizons[0] == 59.9  # 相机自愈写回

        # 自愈后 look 可继续工作（从 59.9 回撤到 29.9）。
        assert (await look.execute(direction="up")).success


class TestNavigateCandidateLogic:
    """候选点选择与朝向 snap 的纯函数面。"""

    def test_nearest_candidates_sorted_and_capped(self):
        position = {"x": -1.5, "z": 2.3}
        candidates = nearest_candidates(position, REACHABLE, limit=3)
        assert candidates == [
            {"x": -1.0, "y": 0.9, "z": 0.5},
            {"x": -1.25, "y": 0.9, "z": 0.25},
            {"x": -1.5, "y": 0.9, "z": 0.0},
        ]

    def test_nearest_candidates_limit_and_stability(self):
        reachable = [
            {"x": 1.0, "z": 1.0},
            {"x": -1.0, "z": 1.0},
            {"x": 0.0, "z": 2.0},
            {"x": 0.0, "z": 0.0},
        ]
        # 四点与目标 (0, 1) 的 d² 全为 1：按原始顺序稳定排序，取前 2
        candidates = nearest_candidates({"x": 0.0, "z": 1.0}, reachable, limit=2)
        assert candidates == [
            {"x": 1.0, "y": 0.0, "z": 1.0},
            {"x": -1.0, "y": 0.0, "z": 1.0},
        ]

    def test_nearest_candidates_skip_malformed_points(self):
        reachable = [
            {"x": -1.0, "z": 0.5},
            {"x": "bogus", "z": 0.5},
            {"z": 0.5},
            "not-a-dict",
        ]
        candidates = nearest_candidates({"x": -1.0, "z": 0.5}, reachable, limit=3)
        assert candidates == [{"x": -1.0, "y": 0.0, "z": 0.5}]

    @pytest.mark.parametrize(
        ("dx", "dz", "yaw"),
        [
            (0.0, 1.0, 0.0),  # 目标在 +Z
            (1.0, 0.0, 90.0),  # 目标在 +X
            (0.0, -1.0, 180.0),  # 目标在 -Z
            (-1.0, 0.0, 270.0),  # 目标在 -X
            (-0.5, 1.8, 0.0),  # ≈ -15.5° → snap 0°
            (-1.0, -1.0, 180.0),  # -135° → snap 180°
        ],
    )
    def test_facing_yaw_snaps_to_canonical_angles(self, dx, dz, yaw):
        assert facing_yaw(dx, dz) == yaw
