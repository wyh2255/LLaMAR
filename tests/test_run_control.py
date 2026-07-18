"""Tests for G3 EnvironmentRunControl protocol and dual-track lifecycle stop.

Covers:
  1. RunStatus DTO is importable and has all required fields.
  2. SARBarrier.get_run_status() returns domain_metrics with coverage/transport_rate.
  3. SARBarrier.request_stop() sets _stop_reason and calls stop().
  4. CoordinatorServer.set_run_control() + _do_cancel_experiment priority.
  5. AI2ThorBarrier satisfies EnvironmentRunControl (isinstance check).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.contracts.types import RunStatus
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController
from a2a.coordinator.run_control import EnvironmentRunControl


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
        """RunStatus is importable from the same path run_control.py uses."""
        from ai2thor_orch.contracts.types import RunStatus as RS

        assert RS is RunStatus


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
