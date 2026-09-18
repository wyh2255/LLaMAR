"""slice / clean / toggle worker 工具 + FakeController manipulation 支持（PA-W2）。

覆盖矩阵（卡面口径）：
- 三工具 schema / 参数形状；
- ``AI2THOR_WORKER_TOOLS`` 注册数 = 11（8 + slice/clean/toggle）；
- 每工具各覆盖：成功路径（经 barrier + FakeController 端到端提交真实动作串）、
  alias 未登记 fail-closed（与 pickup 同款文案）、坏参 ``ValueError``；
- toggle 的 on / off 双分支（``ToggleObjectOn`` / ``ToggleObjectOff``）；
- FakeController 对 4 个新动词的支持：``objectId`` 存在于场景 → 成功 +
  状态位翻转可查（``isSliced`` / ``isDirty`` / ``isToggled``）；不存在 →
  ``lastActionSuccess=False`` + 真机口径 ``errorMessage``。
"""

from __future__ import annotations

import pytest

from Agent.error_taxonomy import error_code_for_result
from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController
from ai2thor_orch.tools.worker.clean import CleanTool
from ai2thor_orch.tools.worker.slice import SliceTool
from ai2thor_orch.tools.worker.toggle import ToggleTool
from ai2thor_orch.visibility import AliasRegistry

#: 默认 fake 场景中的物体（见 ``make_default_metadata``）。
MUG_RAW_ID = "Mug|-01.5|+00.9|+02.3"
APPLE_RAW_ID = "Apple|+01.2|+00.5|+00.8"
#: 不在默认 fake 场景中的 objectId（动作级失败路径探针）。
MISSING_RAW_ID = "Banana|-05.0|+00.9|+03.0"

#: FakeController 对不可解析 objectId 的真机口径失败文案前缀。
_RECORDED_NOT_FOUND_PREFIX = "Target object not found within the specified visibility"


# ── Helpers ─────────────────────────────────────────────────────────────


@pytest.fixture
def alias_registry():
    return AliasRegistry()


@pytest.fixture
def fake_env(alias_registry):
    """``(barrier, controller, registry)`` —— 工具执行与 fake 状态断言共用。"""
    controller = FakeController()
    barrier = AI2ThorBarrier(
        num_agents=1,
        executor=ControllerExecutor(controller),
        max_steps=50,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )
    return barrier, controller, alias_registry


# ── Schema / registry ───────────────────────────────────────────────────


class TestManipToolSchemas:
    """三工具 name / description / parameters 形状。"""

    def test_slice_schema(self):
        tool = SliceTool(barrier=None, agent_idx=0, alias_registry=AliasRegistry())
        assert isinstance(tool, Tool)
        assert tool.name == "slice"
        assert tool.description
        params = tool.parameters
        assert params["type"] == "object"
        assert params["properties"]["object_alias"]["type"] == "string"
        assert params["required"] == ["object_alias"]

    def test_clean_schema(self):
        tool = CleanTool(barrier=None, agent_idx=0, alias_registry=AliasRegistry())
        assert tool.name == "clean"
        assert tool.description
        params = tool.parameters
        assert params["properties"]["object_alias"]["type"] == "string"
        assert params["required"] == ["object_alias"]

    def test_toggle_schema(self):
        tool = ToggleTool(barrier=None, agent_idx=0, alias_registry=AliasRegistry())
        assert tool.name == "toggle"
        assert tool.description
        params = tool.parameters
        assert params["properties"]["object_alias"]["type"] == "string"
        assert params["properties"]["on"]["type"] == "boolean"
        assert params["required"] == ["object_alias", "on"]


class TestWorkerToolRegistry:
    """``AI2THOR_WORKER_TOOLS`` 注册表含 3 件新工具（8 → 11）。"""

    def test_registry_exposes_eleven_tools(self):
        from ai2thor_orch.tools.worker import AI2THOR_WORKER_TOOLS

        assert len(AI2THOR_WORKER_TOOLS) == 11
        names = [
            tool_cls(barrier=None, agent_idx=0, alias_registry=None).name
            for tool_cls in AI2THOR_WORKER_TOOLS
        ]
        assert {"slice", "clean", "toggle"} <= set(names)
        assert names[-1] == "done"


# ── slice ───────────────────────────────────────────────────────────────


class TestSliceTool:
    @pytest.mark.asyncio
    async def test_slice_known_alias_success(self, fake_env):
        barrier, controller, registry = fake_env
        alias = registry.register(APPLE_RAW_ID)
        tool = SliceTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias)

        assert isinstance(result, ToolResult)
        assert result.success
        assert "SliceObject" in result.content
        assert alias in result.content
        # 实提交动作串逐字对齐（alias → raw objectId）。
        assert controller.actions_received[-1]["action"] == f"SliceObject({APPLE_RAW_ID})"
        assert not registry.is_raw_id_leaked(result.content)

    @pytest.mark.asyncio
    async def test_slice_unknown_alias_fail_closed(self, fake_env):
        barrier, controller, registry = fake_env
        tool = SliceTool(barrier, 0, registry)
        before = controller.step_call_count

        result = await tool.execute(object_alias="Ghost_42")

        assert not result.success
        assert "Unknown object alias" in result.error
        assert controller.step_call_count == before  # 未提交动作、未烧回合

    @pytest.mark.asyncio
    async def test_slice_empty_or_bad_alias_raises_value_error(self, fake_env):
        barrier, _, registry = fake_env
        tool = SliceTool(barrier, 0, registry)

        with pytest.raises(ValueError, match="non-empty string"):
            await tool.execute(object_alias="")
        with pytest.raises(ValueError, match="non-empty string"):
            await tool.execute(object_alias=None)

    @pytest.mark.asyncio
    async def test_slice_missing_scene_object_classifies_object_not_visible(self, fake_env):
        """场景中不存在的 objectId：动作失败文案原样回传且归入域类。"""
        barrier, _, registry = fake_env
        alias = registry.register(MISSING_RAW_ID)
        tool = SliceTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias)

        assert not result.success
        assert f"Failed to slice {alias}" in result.error
        assert _RECORDED_NOT_FOUND_PREFIX in result.error
        assert error_code_for_result(result) == "object_not_visible"
        assert not registry.is_raw_id_leaked(result.content)


# ── clean ───────────────────────────────────────────────────────────────


class TestCleanTool:
    @pytest.mark.asyncio
    async def test_clean_known_alias_success(self, fake_env):
        barrier, controller, registry = fake_env
        alias = registry.register(MUG_RAW_ID)
        tool = CleanTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias)

        assert result.success
        assert "CleanObject" in result.content
        assert controller.actions_received[-1]["action"] == f"CleanObject({MUG_RAW_ID})"
        # 状态位经 barrier 链路落到 fake（isDirty → False）。
        assert controller.object_states[MUG_RAW_ID]["isDirty"] is False
        assert not registry.is_raw_id_leaked(result.content)

    @pytest.mark.asyncio
    async def test_clean_unknown_alias_fail_closed(self, fake_env):
        barrier, controller, registry = fake_env
        tool = CleanTool(barrier, 0, registry)
        before = controller.step_call_count

        result = await tool.execute(object_alias="Ghost_42")

        assert not result.success
        assert "Unknown object alias" in result.error
        assert controller.step_call_count == before

    @pytest.mark.asyncio
    async def test_clean_empty_or_bad_alias_raises_value_error(self, fake_env):
        barrier, _, registry = fake_env
        tool = CleanTool(barrier, 0, registry)

        with pytest.raises(ValueError, match="non-empty string"):
            await tool.execute(object_alias="")
        with pytest.raises(ValueError, match="non-empty string"):
            await tool.execute(object_alias=123)


# ── toggle ──────────────────────────────────────────────────────────────


class TestToggleTool:
    @pytest.mark.asyncio
    async def test_toggle_on_branch(self, fake_env):
        barrier, controller, registry = fake_env
        alias = registry.register(MUG_RAW_ID)
        tool = ToggleTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias, on=True)

        assert result.success
        assert "ToggleObjectOn" in result.content
        assert controller.actions_received[-1]["action"] == f"ToggleObjectOn({MUG_RAW_ID})"
        assert controller.object_states[MUG_RAW_ID]["isToggled"] is True

    @pytest.mark.asyncio
    async def test_toggle_off_branch(self, fake_env):
        barrier, controller, registry = fake_env
        alias = registry.register(MUG_RAW_ID)
        tool = ToggleTool(barrier, 0, registry)

        result = await tool.execute(object_alias=alias, on=False)

        assert result.success
        assert "ToggleObjectOff" in result.content
        assert controller.actions_received[-1]["action"] == f"ToggleObjectOff({MUG_RAW_ID})"
        assert controller.object_states[MUG_RAW_ID]["isToggled"] is False

    @pytest.mark.asyncio
    async def test_toggle_unknown_alias_fail_closed(self, fake_env):
        barrier, controller, registry = fake_env
        tool = ToggleTool(barrier, 0, registry)
        before = controller.step_call_count

        result = await tool.execute(object_alias="Ghost_42", on=True)

        assert not result.success
        assert "Unknown object alias" in result.error
        assert controller.step_call_count == before

    @pytest.mark.asyncio
    async def test_toggle_bad_on_raises_value_error(self, fake_env):
        barrier, _, registry = fake_env
        tool = ToggleTool(barrier, 0, registry)

        with pytest.raises(ValueError, match="'on' must be a boolean"):
            await tool.execute(object_alias="Mug_1", on="yes")
        with pytest.raises(ValueError, match="'on' must be a boolean"):
            await tool.execute(object_alias="Mug_1", on=1)

    @pytest.mark.asyncio
    async def test_toggle_empty_alias_raises_value_error(self, fake_env):
        barrier, _, registry = fake_env
        tool = ToggleTool(barrier, 0, registry)

        with pytest.raises(ValueError, match="non-empty string"):
            await tool.execute(object_alias="", on=True)


# ── FakeController manipulation 支持 ────────────────────────────────────


class TestFakeControllerManipulationSupport:
    """FakeController 对 4 个新动词的成功/失败语义与状态位账本。"""

    def test_slice_sets_is_sliced_and_mirrors_into_metadata(self):
        controller = FakeController()

        event = controller.step({"action": f"SliceObject({APPLE_RAW_ID})"})

        assert event.metadata["lastActionSuccess"] is True
        assert controller.object_states[APPLE_RAW_ID]["isSliced"] is True
        mirrored = next(
            obj
            for obj in event.metadata["objects"]
            if obj["objectId"] == APPLE_RAW_ID
        )
        assert mirrored["isSliced"] is True

    def test_clean_sets_is_dirty_false(self):
        controller = FakeController()

        event = controller.step({"action": f"CleanObject({MUG_RAW_ID})"})

        assert event.metadata["lastActionSuccess"] is True
        assert controller.object_states[MUG_RAW_ID]["isDirty"] is False

    def test_toggle_on_then_off_flips_is_toggled(self):
        controller = FakeController()

        on_event = controller.step({"action": f"ToggleObjectOn({MUG_RAW_ID})"})
        assert on_event.metadata["lastActionSuccess"] is True
        assert controller.object_states[MUG_RAW_ID]["isToggled"] is True

        off_event = controller.step({"action": f"ToggleObjectOff({MUG_RAW_ID})"})
        assert off_event.metadata["lastActionSuccess"] is True
        assert controller.object_states[MUG_RAW_ID]["isToggled"] is False

    def test_missing_object_fails_with_recorded_message(self):
        controller = FakeController()

        event = controller.step({"action": f"SliceObject({MISSING_RAW_ID})"})

        assert event.metadata["lastActionSuccess"] is False
        assert _RECORDED_NOT_FOUND_PREFIX in event.metadata["errorMessage"]
        assert controller.object_states == {}

    def test_state_projection_persists_across_failed_step(self):
        """失败步的场景状态投影照常（状态位是场景视图，与动作成败无关）。"""
        controller = FakeController()
        controller.step({"action": f"SliceObject({APPLE_RAW_ID})"})

        event = controller.step({"action": f"SliceObject({MISSING_RAW_ID})"})

        assert event.metadata["lastActionSuccess"] is False
        apple = next(
            obj
            for obj in event.metadata["objects"]
            if obj["objectId"] == APPLE_RAW_ID
        )
        assert apple["isSliced"] is True

    def test_missing_object_id_argument_fails(self):
        controller = FakeController()

        event = controller.step({"action": "CleanObject"})

        assert event.metadata["lastActionSuccess"] is False
        assert "requires an objectId" in event.metadata["errorMessage"]

    def test_other_actions_behaviour_unchanged(self):
        """未列入 4 动词的动作保持既有宽松语义（回归钉子）。"""
        controller = FakeController()

        event = controller.step({"action": f"PickupObject({MISSING_RAW_ID})"})

        assert event.metadata["lastActionSuccess"] is True
        assert controller.object_states == {}
