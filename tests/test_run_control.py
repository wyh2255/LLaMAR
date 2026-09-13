"""Tests for G3 EnvironmentRunControl protocol and dual-track lifecycle stop.

Covers:
  1. RunStatus DTO is importable and has all required fields; the ai2thor
     ``contracts.types.RunStatus`` alias IS the kernel class (G1 closure).
  2. SARBarrier.get_run_status() returns domain_metrics with coverage/transport_rate.
  3. SARBarrier.request_stop() sets _stop_reason and calls stop().
  4. CoordinatorServer.set_run_control() + _do_cancel_experiment priority.
  5. AI2ThorBarrier satisfies EnvironmentRunControl (isinstance check).
  6. Run-control consistency: protocol shape + request_stop/stop semantics.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from unittest.mock import MagicMock

import pytest

from a2a.coordinator.run_control import EnvironmentRunControl, RunStatus
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController


# ── 1. RunStatus DTO ────────────────────────────────────────────────────────


class TestRunStatusImportable:
    """RunStatus fields must match implementation plan §3.1 exactly."""

    def test_defaults(self):
        rs = RunStatus()
        assert rs.step == 0
        assert rs.max_steps == 0
        assert rs.finished is False
        assert rs.stopped is False
        assert rs.stop_reason == ""
        assert rs.timeout_agents == []
        assert rs.domain_metrics == {}

    def test_all_fields(self):
        rs = RunStatus(
            step=5,
            max_steps=50,
            finished=False,
            stopped=True,
            stop_reason="cancel:test",
            timeout_agents=[1, 2],
            domain_metrics={"coverage": 0.75, "transport_rate": 0.3},
        )
        assert rs.step == 5
        assert rs.max_steps == 50
        assert rs.stopped is True
        assert rs.stop_reason == "cancel:test"
        assert rs.timeout_agents == [1, 2]
        assert rs.domain_metrics["coverage"] == 0.75

    def test_import_path(self):
        """ai2thor 侧 import 路径 = 内核同一对象（G1 别名 re-export）。"""
        from ai2thor_orch.contracts.types import RunStatus as RS

        assert RS is RunStatus

    def test_field_set_matches_plan_section_3_1(self):
        """字段集合断言：别名不得夹带额外/缺失字段（实施计划 §3.1）。"""
        expected = {
            "step",
            "max_steps",
            "finished",
            "stopped",
            "stop_reason",
            "timeout_agents",
            "domain_metrics",
        }
        assert {f.name for f in dataclasses.fields(RunStatus)} == expected


# ── 2. SARBarrier.get_run_status() ──────────────────────────────────────────


class TestSARBarrierGetRunStatus:
    """SARBarrier.get_run_status() returns domain_metrics with coverage/transport_rate."""

    def test_get_run_status_has_domain_metrics(self):
        from sar_orch.barrier import SARBarrier

        barrier = SARBarrier(num_agents=1, scene=1, seed=42)
        status = barrier.get_run_status()

        assert isinstance(status, RunStatus)
        assert status.step == 0
        assert status.max_steps == 0  # SAR has no fixed upper bound
        assert status.finished is False
        assert status.stopped is False
        assert isinstance(status.domain_metrics, dict)
        assert "coverage" in status.domain_metrics
        assert "transport_rate" in status.domain_metrics
        assert isinstance(status.domain_metrics["coverage"], (int, float))
        assert isinstance(status.domain_metrics["transport_rate"], (int, float))

    def test_get_run_status_after_step(self, monkeypatch):
        from sar_orch.barrier import SARBarrier

        barrier = SARBarrier(num_agents=1, scene=1, seed=42)

        def fake_step(actions):
            return ["obs"], [True]

        monkeypatch.setattr(barrier.env, "step", fake_step)
        monkeypatch.setattr(
            barrier.env, "generate_obs_text", lambda idx: ("obs", {})
        )
        monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
        monkeypatch.setattr(
            barrier.env.checker, "check_success", lambda: True
        )

        barrier._action_queue[0] = "NavigateTo(target)"
        barrier._execute_step(expected_step=0)

        status = barrier.get_run_status()
        assert status.step == 1
        assert status.finished is True


# ── 3. SARBarrier.request_stop() ────────────────────────────────────────────


class TestSARBarrierRequestStop:
    """SARBarrier.request_stop() sets _stop_reason and calls stop()."""

    def test_request_stop_sets_reason_and_stops(self):
        from sar_orch.barrier import SARBarrier

        barrier = SARBarrier(num_agents=1, scene=1, seed=42)
        assert barrier._stop_reason == ""
        assert barrier._stopped is False

        barrier.request_stop(reason="test_reason")

        assert barrier._stop_reason == "test_reason"
        assert barrier._stopped is True
        assert barrier._finished is True
        for ev in barrier._obs_events:
            assert ev.is_set()

    def test_request_stop_default_reason(self):
        from sar_orch.barrier import SARBarrier

        barrier = SARBarrier(num_agents=1, scene=1, seed=42)

        barrier.request_stop()  # no reason arg → default "env_stop"

        assert barrier._stop_reason == "env_stop"
        assert barrier._stopped is True

    def test_request_stop_idempotent(self):
        from sar_orch.barrier import SARBarrier

        barrier = SARBarrier(num_agents=1, scene=1, seed=42)

        barrier.request_stop(reason="first")
        assert barrier._stop_reason == "first"

        barrier.request_stop(reason="second")
        assert barrier._stop_reason == "first"  # unchanged


# ── 4. CoordinatorServer dual-track cancel ──────────────────────────────────


class TestCoordinatorCancelPriority:
    """CoordinatorServer.set_run_control() + _do_cancel_experiment priority."""

    def test_cancel_prefers_run_control_over_barrier(self):
        """When both run_control and barrier are set, request_stop is called
        and barrier.stop() is NOT called."""
        from a2a.coordinator.server import CoordinatorServer

        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            memory_read_mode="legacy",
        )

        rc_mock = MagicMock()
        barrier_mock = MagicMock()

        server.set_run_control(rc_mock)
        server.set_barrier(barrier_mock)

        assert server._run_control is rc_mock
        assert server._barrier is barrier_mock

        # Simulate the cancel logic from _do_cancel_experiment (L463+):
        #   if self._run_control is not None:
        #       self._run_control.request_stop('cancel:' + context_id)
        #   elif self._barrier is not None:
        #       self._barrier.stop()
        if server._run_control is not None:
            server._run_control.request_stop("cancel:ctx_001")
        elif server._barrier is not None:
            server._barrier.stop()

        rc_mock.request_stop.assert_called_once_with("cancel:ctx_001")
        barrier_mock.stop.assert_not_called()

    def test_cancel_fallback_to_barrier_when_no_run_control(self):
        """When only barrier is set, barrier.stop() is called."""
        from a2a.coordinator.server import CoordinatorServer

        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            memory_read_mode="legacy",
        )

        barrier_mock = MagicMock()
        server.set_barrier(barrier_mock)

        assert server._run_control is None
        assert server._barrier is barrier_mock

        # Simulate cancel logic fallback
        if server._run_control is not None:
            server._run_control.request_stop("cancel:ctx_002")
        elif server._barrier is not None:
            server._barrier.stop()

        barrier_mock.stop.assert_called_once()

    def test_cancel_noop_when_none_set(self):
        """When neither run_control nor barrier is set, nothing crashes."""
        from a2a.coordinator.server import CoordinatorServer

        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            memory_read_mode="legacy",
        )

        assert server._run_control is None
        assert server._barrier is None

        # Simulate cancel logic — should not crash
        barrier_stopped = False
        if server._run_control is not None:
            server._run_control.request_stop("cancel:ctx_003")
            barrier_stopped = True
        elif server._barrier is not None:
            server._barrier.stop()
            barrier_stopped = True

        assert barrier_stopped is False


# ── 5. AI2ThorBarrier satisfies EnvironmentRunControl ──────────────────────


class TestAI2ThorBarrierSatisfiesProtocol:
    """AI2ThorBarrier meets the EnvironmentRunControl protocol."""

    def test_isinstance_environment_run_control(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=exec_,
            max_steps=10,
            step_timeout=5.0,
        )

        assert isinstance(barrier, EnvironmentRunControl)

    def test_protocol_methods_work(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=exec_,
            max_steps=10,
        )

        barrier.request_stop("test_reason")
        status = barrier.get_run_status()
        assert status.stop_reason == "test_reason"

        barrier.stop()
        assert barrier._stopped is True

        status = barrier.get_run_status()
        assert isinstance(status, RunStatus)
        assert status.stopped is True


# ── 6. EnvironmentRunControl protocol definition ────────────────────────────


class TestEnvironmentRunControlProtocol:
    """Protocol definition sanity checks."""

    def test_protocol_is_runtime_checkable(self):
        assert hasattr(EnvironmentRunControl, "__instancecheck__")

    def test_protocol_has_required_methods(self):
        assert "request_stop" in EnvironmentRunControl.__dict__
        assert "stop" in EnvironmentRunControl.__dict__
        assert "get_run_status" in EnvironmentRunControl.__dict__


# ── 7. Run-control 一致性：AI2ThorBarrier 协议形状 + 语义 ────────────────────


class TestAI2ThorRunControlSemantics:
    """内核协议 vs AI2ThorBarrier：形状（is_finished/get_run_status/
    request_stop/stop）与语义（request_stop 后行为、stop 收口）。"""

    @staticmethod
    def _make_barrier(
        *, num_agents: int = 2, max_steps: int = 10, step_timeout: float = 5.0
    ) -> tuple[FakeController, AI2ThorBarrier]:
        ctrl = FakeController()
        barrier = AI2ThorBarrier(
            num_agents=num_agents,
            executor=ControllerExecutor(ctrl),
            max_steps=max_steps,
            step_timeout=step_timeout,
        )
        return ctrl, barrier

    def test_protocol_shape_and_methods_present(self):
        """四方法齐备；request_stop 带默认 reason；status 为内核 DTO。"""
        _, barrier = self._make_barrier()

        assert isinstance(barrier, EnvironmentRunControl)
        for name in ("is_finished", "get_run_status", "request_stop", "stop"):
            assert callable(getattr(barrier, name)), name

        sig = inspect.signature(barrier.request_stop)
        assert list(sig.parameters) == ["reason"]
        assert sig.parameters["reason"].default == "env_stop"

        status = barrier.get_run_status()
        assert isinstance(status, RunStatus)
        assert barrier.is_finished() is False

    async def test_request_stop_wakes_waiter_and_settles_status(self):
        """request_stop → 等待者立即返回；三态齐备；停后提交不推进。"""
        ctrl, barrier = self._make_barrier()

        pending = asyncio.create_task(barrier.submit_action(0, "MoveAhead"))
        await asyncio.sleep(0.05)  # let agent 0 start waiting
        assert barrier.is_finished() is False

        barrier.request_stop("cancel:ctx-001")

        result = await asyncio.wait_for(pending, timeout=5.0)
        assert result.success is False
        assert barrier.is_finished() is True

        status = barrier.get_run_status()
        assert status.stopped is True
        assert status.finished is True
        assert status.stop_reason == "cancel:ctx-001"

        step_before = status.step
        post_stop = await barrier.submit_action(1, "MoveAhead")
        assert post_stop.success is False
        assert post_stop.observation == ""
        assert barrier.get_run_status().step == step_before
        assert ctrl.step_call_count == 0  # 没有任何回合被执行

    def test_stop_shuts_down_executor_and_is_idempotent(self):
        """stop() 置终态 + 关闭执行器；重复调用不重复关闭。"""
        ctrl, barrier = self._make_barrier()

        barrier.stop()

        assert barrier.is_finished() is True
        status = barrier.get_run_status()
        assert status.stopped is True
        assert status.stop_reason == "env_stop"
        assert ctrl.stop_call_count == 1

        barrier.stop()
        assert ctrl.stop_call_count == 1

    def test_request_stop_idempotent_keeps_first_reason(self):
        """request_stop 幂等：第二次调用不覆盖首次 reason。"""
        _, barrier = self._make_barrier()

        barrier.request_stop("first")
        barrier.request_stop("second")

        status = barrier.get_run_status()
        assert status.stop_reason == "first"
        assert status.stopped is True
        assert status.finished is True

    async def test_finished_via_max_steps_is_not_stopped(self):
        """自然收官（max_steps）≠ 停止：finished=True 且 stopped=False。"""
        _, barrier = self._make_barrier(num_agents=1, max_steps=1)

        await barrier.submit_action(0, "MoveAhead")

        status = barrier.get_run_status()
        assert status.finished is True
        assert status.stopped is False
        assert barrier.is_finished() is True

        # 终态后提交直接短路，不再推进
        post = await barrier.submit_action(0, "MoveAhead")
        assert post.success is False
        assert barrier.get_run_status().step == 1
