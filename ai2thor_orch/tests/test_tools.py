"""Tests for AI2Thor worker restricted tools.

Tests cover:
- Each tool's name, description, and parameters schema are valid
- execute() returns ToolResult with correct success/error fields
- pickup/put/open_close correctly resolve alias→raw via AliasRegistry
- observation text is redacted (no raw objectId leakage)
"""

from __future__ import annotations

import pytest

from Agent.worker_agent.tools.base import ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController, make_default_metadata
from ai2thor_orch.tools.worker.move import MoveTool
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
