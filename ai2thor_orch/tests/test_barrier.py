"""Tests for AI2ThorBarrier — round semantics, timeout, stop, run status."""

from __future__ import annotations

import asyncio

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController


@pytest.mark.asyncio
class TestNormalRound:
    """2 agents submit normally → round advances."""

    async def test_two_agents_round_advances(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=5.0,
        )

        # Both agents submit concurrently
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        result0, result1 = await asyncio.gather(agent_0(), agent_1())

        assert result0.success
        assert result1.success
        assert result0.action == "MoveAhead"
        assert result1.action == "RotateLeft"
        # Each agent's action triggers one controller.step() call
        assert ctrl.step_call_count == 2
        status = barrier.get_run_status()
        assert status.step == 1  # advanced after one round
        assert status.finished is False

    async def test_round_advances_step_counter(self):
        """Two rounds with 2 agents each."""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10, step_timeout=5.0,
        )

        # Round 1
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.get_run_status().step == 1
        assert ctrl.step_call_count == 2

        # Round 2
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.get_run_status().step == 2
        assert ctrl.step_call_count == 4


@pytest.mark.asyncio
class TestTimeout:
    """1 agent times out → auto NoOp, round still advances."""

    async def test_timeout_triggers_noop(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,  # short timeout
        )

        # Agent 0 submits, agent 1 never submits
        result0 = await barrier.submit_action(0, "MoveAhead")

        assert result0.success
        status = barrier.get_run_status()
        assert status.step == 1
        # Agent 1 should be in timeout_agents
        assert 1 in status.timeout_agents
        # 2 agents → 2 controller step() calls: MoveAhead + NoOp
        assert ctrl.step_call_count == 2

    async def test_only_late_agent_is_timeout(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=3,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,
        )

        # Agents 0 and 1 submit, agent 2 is late
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        await asyncio.gather(agent_0(), agent_1())

        status = barrier.get_run_status()
        # After timeout, both agents returned and round advanced
        assert status.step == 1
        # Agent 2 should be in timeout_agents (not 0 or 1)
        assert status.timeout_agents == [2], (
            f"Expected [2], got {status.timeout_agents} (step={status.step})"
        )

    async def test_timeout_does_not_hang(self):
        """Agent that timed out can submit next round normally."""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,
        )

        # Round 1: only agent 0 submits → agent 1 times out
        await barrier.submit_action(0, "MoveAhead")
        assert barrier.get_run_status().step == 1

        # Round 2: both submit normally
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        await asyncio.gather(agent_0(), agent_1())
        assert barrier.get_run_status().step == 2


@pytest.mark.asyncio
class TestRequestStop:
    """request_stop() → all blocking submit_action return immediately."""

    async def test_request_stop_wakes_waiters(self):
        ctrl = FakeController(delay_seconds=0.5)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=5.0,
        )

        # Agent 0 submits and blocks waiting for agent 1
        async def waiter():
            return await barrier.submit_action(0, "WaitAction")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # Let agent 0 start waiting

        # Stop from "outside"
        barrier.request_stop("test_cancel")
        result = await task

        assert result.success is False
        status = barrier.get_run_status()
        assert status.stopped is True
        assert status.stop_reason == "test_cancel"

    async def test_request_stop_is_idempotent(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        barrier.request_stop("first")
        barrier.request_stop("second")  # should be no-op
        assert barrier.get_run_status().stop_reason == "first"

    async def test_submit_after_stop_returns_empty(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        barrier.request_stop("done")
        result = await barrier.submit_action(0, "MoveAhead")
        assert result.success is False
        assert result.observation == ""


@pytest.mark.asyncio
class TestRunStatus:
    """get_run_status() field completeness."""

    async def test_fields_populated(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=50,
            step_timeout=5.0,
        )

        status = barrier.get_run_status()
        assert status.step == 0
        assert status.max_steps == 50
        assert status.finished is False
        assert status.stopped is False
        assert status.stop_reason == ""
        assert status.timeout_agents == []
        assert isinstance(status.domain_metrics, dict)

        # After one round
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        status = barrier.get_run_status()
        assert status.step == 1
        assert "round_success" in status.domain_metrics

    async def test_max_steps_reached(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=2, step_timeout=5.0
        )

        # Round 1
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.is_finished() is False

        # Round 2 — should finish
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.is_finished() is True
        status = barrier.get_run_status()
        assert status.step == 2
        assert status.finished is True


class TestIsFinished:
    def test_initial_not_finished(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )
        assert barrier.is_finished() is False

    def test_finished_after_stop(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )
        barrier.stop()
        assert barrier.is_finished() is True


class TestSnapshotPublic:
    def test_snapshot_returns_observation(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )

        # No round executed yet
        obs = barrier.snapshot_public(0)
        assert obs.agent_idx == 0
        assert obs.step == 0
        assert obs.text == ""

    @pytest.mark.asyncio
    async def test_snapshot_after_action(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )

        await barrier.submit_action(0, "MoveAhead")
        obs = barrier.snapshot_public(0)
        assert obs.step == 1
        assert obs.text != ""
        # visible_objects should be aliased
        for v in obs.visible_objects:
            assert "|" not in v  # no raw objectId

    def test_snapshot_invalid_agent(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        with pytest.raises(ValueError):
            barrier.snapshot_public(5)


class TestSnapshotCoordinator:
    @pytest.mark.asyncio
    async def test_coordinator_snapshot(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )

        snap = barrier.snapshot_coordinator()
        assert snap.round_no > 0
        assert len(snap.agents) == 2
        assert snap.step == 1
        # Objects should have aliases
        if snap.objects:
            assert "alias" in snap.objects[0]


class TestConstructor:
    def test_invalid_num_agents(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=0, executor=exec_, max_steps=10)

    def test_invalid_max_steps(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=0)

    def test_invalid_step_timeout(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10, step_timeout=0)
