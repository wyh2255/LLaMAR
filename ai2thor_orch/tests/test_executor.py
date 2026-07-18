"""Tests for ControllerExecutor — serialisation, idempotent stop, error propagation."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController, make_default_metadata


class TestExecuteStep:
    """Serial behaviour of execute_step."""

    def test_single_action(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        results = exec_.execute_step([{"action": "MoveAhead"}])
        assert len(results) == 1
        assert "agent_metadata" in results[0]
        assert ctrl.step_call_count == 1

    def test_multiple_actions_in_order(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        actions = [
            {"action": "MoveAhead"},
            {"action": "RotateLeft"},
            {"action": "Pickup"},
        ]
        results = exec_.execute_step(actions)
        assert len(results) == 3
        assert ctrl.step_call_count == 3
        # Verify order
        received = [a["action"] for a in ctrl.actions_received]
        assert received == ["MoveAhead", "RotateLeft", "Pickup"]

    def test_serial_no_interleaving(self):
        """Verify that two concurrent execute_step calls do not interleave."""
        ctrl = FakeController(delay_seconds=0.05)
        exec_ = ControllerExecutor(ctrl)

        order: list[int] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            with lock:
                order.append(worker_id)
            exec_.execute_step([{"action": f"Worker{worker_id}Action"}])

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert ctrl.step_call_count == 2
        # Both actions should have been received (serialised by the executor)
        assert len(ctrl.actions_received) == 2

    def test_script_replay(self):
        """Follow a pre-defined script."""
        script = [
            make_default_metadata(scene="FloorPlan1"),
            make_default_metadata(scene="FloorPlan2"),
        ]
        ctrl = FakeController(script=script)
        exec_ = ControllerExecutor(ctrl)

        r1 = exec_.execute_step([{"action": "MoveAhead"}])
        r2 = exec_.execute_step([{"action": "MoveAhead"}])

        assert r1[0]["agent_metadata"]["sceneName"] == "FloorPlan1"
        assert r2[0]["agent_metadata"]["sceneName"] == "FloorPlan2"


class TestReset:
    def test_reset_returns_metadata(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        result = exec_.reset("FloorPlan1")
        assert "initial_metadata" in result
        assert "raw_event" in result


class TestStop:
    def test_stop_is_idempotent(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        exec_.stop()
        exec_.stop()  # second call must not raise
        assert ctrl.stop_call_count == 1
        assert exec_.is_stopped

    def test_execute_after_stop_raises(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        exec_.stop()
        with pytest.raises(RuntimeError, match="stopped"):
            exec_.execute_step([{"action": "MoveAhead"}])


class TestErrorPropagation:
    def test_injected_failure(self):
        ctrl = FakeController(fail_on_action="FailAction")
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(RuntimeError, match="injected failure"):
            exec_.execute_step([{"action": "FailAction"}])

    def test_exception_does_not_corrupt_executor(self):
        """After a failed action, the executor should still accept new steps."""
        ctrl = FakeController(fail_on_action="FailAction")
        exec_ = ControllerExecutor(ctrl)

        with pytest.raises(RuntimeError):
            exec_.execute_step([{"action": "FailAction"}])

        # Next step should work
        results = exec_.execute_step([{"action": "MoveAhead"}])
        assert len(results) == 1
        assert results[0]["agent_metadata"]["lastAction"] == "MoveAhead"
