"""Tests for AI2Thor context subclasses.

Tests cover:
- AI2ThorWorkerContextManager._render_environment_view() does NOT
  contain SAR fields (fires, persons, reservoirs)
- AI2ThorCoordinatorContextManager._render_environment_view() does NOT
  contain SAR fields
- Both renderers contain key scene/step/agents/objects information
"""

from __future__ import annotations

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController, make_default_metadata
from ai2thor_orch.state.context import (
    AI2ThorWorkerContextManager,
    AI2ThorCoordinatorContextManager,
)
from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider
from ai2thor_orch.state.coordinator_state_provider import (
    AI2ThorCoordinatorStateProvider,
)
from ai2thor_orch.visibility import AliasRegistry

# Key SAR fields that must NOT appear in AI2Thor context rendering
_SAR_KEYWORDS = [
    "fires",
    "persons",
    "reservoirs",
    "deposits",
    "extinguish",
    "rescue",
]


# ── Helpers ─────────────────────────────────────────────────────────────


def _setup_barrier_with_objects():
    """Create barrier with 2 agents and known objects."""
    reg = AliasRegistry()
    metadata = make_default_metadata(scene="FloorPlan1", num_agents=2, has_objects=True)
    controller = FakeController(metadata_override=metadata)
    executor = ControllerExecutor(controller)
    b = AI2ThorBarrier(
        num_agents=2,
        executor=executor,
        max_steps=50,
        step_timeout=5.0,
        alias_registry=reg,
    )
    return b, reg


# ── Worker Context Tests ────────────────────────────────────────────────


class TestAI2ThorWorkerContextManager:
    """Verify AI2Thor worker context rendering."""

    def test_no_sar_fields_in_env_view(self):
        """_render_environment_view() must NOT contain SAR-specific keywords."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        ctx = AI2ThorWorkerContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        assert env, "Environment view should not be empty"

        lower = env.lower()
        for kw in _SAR_KEYWORDS:
            assert kw not in lower, (
                f"SAR keyword '{kw}' found in worker environment view: {env[:200]}"
            )

    def test_contains_key_info(self):
        """Output must contain scene, step, agent name, and visible objects."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        ctx = AI2ThorWorkerContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        assert "Scene:" in env, f"Missing 'Scene:' in: {env}"
        assert "Step:" in env, f"Missing 'Step:' in: {env}"
        assert "Agent" in env, f"Missing agent info in: {env}"
        assert "Visible objects:" in env, f"Missing 'Visible objects:' in: {env}"

    def test_inventory_or_nothing(self):
        """Output shows inventory contents or 'nothing'."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        ctx = AI2ThorWorkerContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        assert "holding:" in env
        # At initial state inventory may be empty -> "nothing"
        assert "nothing" in env or "holding:" in env

    @pytest.mark.asyncio
    async def test_no_sar_fields_even_after_action(self):
        """Even after an action, no SAR fields should appear."""
        barrier, reg = _setup_barrier_with_objects()

        # Execute an action
        await barrier.submit_action(0, "MoveAhead")
        await barrier.submit_action(1, "MoveAhead")

        provider = AI2ThorWorkerStateProvider(barrier, 0)
        ctx = AI2ThorWorkerContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        lower = env.lower()
        for kw in _SAR_KEYWORDS:
            assert kw not in lower, (
                f"SAR keyword '{kw}' found after action: {env[:200]}"
            )


# ── Coordinator Context Tests ──────────────────────────────────────────


class TestAI2ThorCoordinatorContextManager:
    """Verify AI2Thor coordinator context rendering."""

    def test_no_sar_fields_in_env_view(self):
        """_render_environment_view() must NOT contain SAR-specific keywords."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorCoordinatorStateProvider(barrier)
        ctx = AI2ThorCoordinatorContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        assert env, "Environment view should not be empty"

        lower = env.lower()
        for kw in _SAR_KEYWORDS:
            assert kw not in lower, (
                f"SAR keyword '{kw}' found in coordinator environment view: {env[:200]}"
            )

    def test_contains_key_info(self):
        """Output must contain scene, step, agents, and objects of interest."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorCoordinatorStateProvider(barrier)
        ctx = AI2ThorCoordinatorContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        assert "Scene:" in env, f"Missing 'Scene:' in: {env}"
        assert "Step:" in env, f"Missing 'Step:' in: {env}"
        assert "Agents:" in env, f"Missing 'Agents:' in: {env}"
        assert "Objects of interest:" in env, (
            f"Missing 'Objects of interest:' in: {env}"
        )

    def test_agent_info_in_coordinator_view(self):
        """Coordinator view should show each agent with position and inventory."""
        barrier, reg = _setup_barrier_with_objects()
        provider = AI2ThorCoordinatorStateProvider(barrier)
        ctx = AI2ThorCoordinatorContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        # Should have agent names
        assert "Agent0" in env or "(" in env
        assert "holding:" in env

    @pytest.mark.asyncio
    async def test_no_sar_fields_after_action(self):
        """Even after actions, coordinator view has no SAR fields."""
        barrier, reg = _setup_barrier_with_objects()
        await barrier.submit_action(0, "MoveAhead")
        await barrier.submit_action(1, "RotateLeft")

        provider = AI2ThorCoordinatorStateProvider(barrier)
        ctx = AI2ThorCoordinatorContextManager(state_provider=provider)
        ctx.refresh_runtime_state()

        env = ctx._render_environment_view()
        lower = env.lower()
        for kw in _SAR_KEYWORDS:
            assert kw not in lower, (
                f"SAR keyword '{kw}' found after action: {env[:200]}"
            )
