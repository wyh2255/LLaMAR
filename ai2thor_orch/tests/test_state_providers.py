"""Tests for AI2Thor state providers.

Tests cover:
- Worker provider snapshot payload contains required keys
- Worker provider payload has no SAR-specific keys (fires, persons, reservoirs)
- Coordinator provider snapshot payload contains required keys
- Coordinator provider payload has no SAR-specific keys
- RuntimeState version and env_step are correct
"""

from __future__ import annotations

import time

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController
from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider
from ai2thor_orch.state.coordinator_state_provider import (
    AI2ThorCoordinatorStateProvider,
)
from ai2thor_orch.visibility import AliasRegistry

_SAR_FIELDS = {"fires", "persons", "reservoirs", "known_fires", "known_persons"}


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def alias_registry():
    return AliasRegistry()


@pytest.fixture
def barrier(alias_registry):
    controller = FakeController()
    executor = ControllerExecutor(controller)
    b = AI2ThorBarrier(
        num_agents=2,
        executor=executor,
        max_steps=50,
        step_timeout=5.0,
        alias_registry=alias_registry,
    )
    return b


# ── Worker State Provider Tests ────────────────────────────────────────


class TestAI2ThorWorkerStateProvider:
    REQUIRED_WORKER_KEYS = {
        "position",
        "rotation",
        "inventory",
        "step",
        "visible_objects",
        "current_task",
        "mission_status",
        "scene",
        "agent_name",
        "agent_idx",
        "max_steps",
    }

    def test_snapshot_returns_runtime_state(self, barrier, alias_registry):
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        state = provider.snapshot()
        assert state is not None
        assert state.version >= 0
        assert state.env_step >= 0
        assert isinstance(state.payload, dict)

    def test_snapshot_payload_required_keys(self, barrier, alias_registry):
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        state = provider.snapshot()
        for key in self.REQUIRED_WORKER_KEYS:
            assert key in state.payload, f"Missing required payload key: {key}"

    def test_snapshot_no_sar_keys(self, barrier, alias_registry):
        """Verify the payload does NOT contain SAR-specific fields."""
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        state = provider.snapshot()
        for key in state.payload:
            for sar_key in _SAR_FIELDS:
                assert sar_key not in key.lower(), (
                    f"SAR-specific key leaked: {key}"
                )

    def test_snapshot_version_env_step(self, barrier, alias_registry):
        provider = AI2ThorWorkerStateProvider(barrier, 0)
        state = provider.snapshot()
        # Initial state: version=round_no=0, env_step=step=0
        assert state.version == 0
        assert state.env_step == 0

    @pytest.mark.asyncio
    async def test_snapshot_after_action(self, barrier, alias_registry):
        """After submitting an action, version/env_step should advance."""
        provider = AI2ThorWorkerStateProvider(barrier, 0)

        # Submit an action to advance the round
        result = await barrier.submit_action(0, "MoveAhead")
        # Also submit for agent 1 to advance the round
        result2 = await barrier.submit_action(1, "MoveAhead")

        state = provider.snapshot()
        assert state.version >= 1
        assert state.env_step >= 1

    def test_snapshot_agent_specific(self, barrier, alias_registry):
        """Each agent index should see different payload data."""
        provider0 = AI2ThorWorkerStateProvider(barrier, 0)
        provider1 = AI2ThorWorkerStateProvider(barrier, 1)
        state0 = provider0.snapshot()
        state1 = provider1.snapshot()
        assert state0.payload.get("agent_idx") == 0
        assert state1.payload.get("agent_idx") == 1
        assert state0.payload.get("agent_name") != state1.payload.get("agent_name")


# ── Coordinator State Provider Tests ───────────────────────────────────


class TestAI2ThorCoordinatorStateProvider:
    REQUIRED_COORDINATOR_KEYS = {
        "scene",
        "step_budget",
        "team_status_summary",
        "mission_finished",
        "run_status",
        "visible_objects",
    }

    def test_snapshot_returns_runtime_state(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        assert state is not None
        assert state.version >= 0
        assert isinstance(state.payload, dict)

    def test_snapshot_payload_required_keys(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        for key in self.REQUIRED_COORDINATOR_KEYS:
            assert key in state.payload, f"Missing required payload key: {key}"

    def test_snapshot_no_sar_keys(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        for key in state.payload:
            for sar_key in _SAR_FIELDS:
                assert sar_key not in key.lower(), (
                    f"SAR-specific key leaked: {key}"
                )

    def test_step_budget_structure(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        budget = state.payload["step_budget"]
        assert "current_step" in budget
        assert "max_steps" in budget
        assert "remaining" in budget
        assert budget["max_steps"] == 50
        assert budget["remaining"] == budget["max_steps"] - budget["current_step"]

    def test_team_status_summary(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        team = state.payload["team_status_summary"]
        assert "agents" in team
        assert len(team["agents"]) == 2  # 2 agents in fixture

    def test_run_status_in_payload(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        rs = state.payload["run_status"]
        assert "step" in rs
        assert "max_steps" in rs
        assert "finished" in rs
        assert "stopped" in rs

    @pytest.mark.asyncio
    async def test_snapshot_after_actions(self, barrier, alias_registry):
        """After actions, coordinator should see updated state."""
        provider = AI2ThorCoordinatorStateProvider(barrier)
        # Submit actions for both agents
        await barrier.submit_action(0, "MoveAhead")
        await barrier.submit_action(1, "RotateLeft")
        state = provider.snapshot()
        assert state.payload["step_budget"]["current_step"] >= 1

    def test_version_env_step_consistency(self, barrier, alias_registry):
        provider = AI2ThorCoordinatorStateProvider(barrier)
        state = provider.snapshot()
        # round_no and env_step match barrier state
        assert state.version == barrier.round_no
        assert state.env_step == 0  # No actions taken yet
