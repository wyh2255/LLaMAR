"""Experiment-side worker-liveness fail-fast (t_7c303cb1).

2026-09-17: every worker thread died on the startup MCP race (see
``tests/test_mcp_connect_resilience.py``) and the run still polled to
``max_steps`` — one LLM call per step, ~570s per scene — before ending as a
generic ``framework_error``.  The poll loop now checks worker liveness and
aborts with the explicit ``workers_dead`` end reason as soon as the whole team
is gone.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from sar_orch import experiment
from sar_orch.experiment import (
    classify_end_reason,
    dead_worker_report,
    worker_threads_all_dead,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _FakeWorker:
    def __init__(self, name: str, *, alive: bool, error: BaseException | None = None):
        self.agent_name = name
        self._alive = alive
        self.thread_error = error

    def is_alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def clear_sessions(self) -> None:
        pass


class _FakeBarrier:
    """Barrier whose step counter advances one step per poll (bounded run)."""

    _step_counter = 0

    def __init__(self, **_kwargs):
        self.stopped = False
        self._finished = False
        self._steps = 0
        self.poll_calls = 0

    def is_finished(self) -> bool:
        return self._finished

    def get_metrics(self) -> dict:
        self.poll_calls += 1
        self._steps += 1
        if self._steps >= 10_000:
            self._finished = True
        return {
            "finished": self._finished,
            "steps": self._steps,
            "coverage": 0.0,
            "transport_rate": 0.0,
        }

    def drain_step_logs(self) -> list:
        return []

    def stop(self) -> None:
        self.stopped = True


class _FakeExperimentLogger:
    def __init__(self, *, log_dir: str, **_kwargs):
        self._log_dir = log_dir
        self.end_reasons: list[str] = []

    def set_run_context(self, **_kwargs) -> None:
        pass

    def write_metadata(self, _metadata: dict) -> None:
        pass

    def log_step(self, **_kwargs) -> None:
        pass

    def flush_summary(self) -> None:
        pass

    def freeze_terminal(self) -> None:
        pass

    def get_log_dir(self) -> str:
        return self._log_dir

    def set_end_reason(self, reason: str) -> None:
        self.end_reasons.append(reason)

    def close(self) -> None:
        pass


class _FakeCoordinator:
    def __init__(self, **_kwargs):
        self._semantic_map = None
        self.submitted: list[str] = []

    async def start(self) -> None:
        return None

    async def submit_task(self, description: str) -> None:
        self.submitted.append(description)
        await asyncio.Event().wait()  # never completes: the run is aborted

    def clear_sessions(self) -> None:
        pass

    async def stop(self) -> None:
        return None


class _FakeSandboxPolicy:
    @staticmethod
    def workspace(**_kwargs):
        from types import SimpleNamespace

        return SimpleNamespace()


def _install_fakes(monkeypatch, workers: dict, exp_loggers: list, barriers: list):
    class _WorkerFactory:
        def __init__(self, **kwargs) -> None:
            self.agent_name = kwargs.get("agent_name", "?")
            self.kwargs = kwargs

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def clear_sessions(self) -> None:
            pass

        def is_alive(self) -> bool:
            return workers[self.agent_name].is_alive()

        @property
        def thread_error(self):
            return workers[self.agent_name].thread_error

    def _barrier_factory(**kwargs):
        barrier = _FakeBarrier(**kwargs)
        barriers.append(barrier)
        return barrier

    def _logger_factory(**kwargs):
        exp_logger = _FakeExperimentLogger(**kwargs)
        exp_loggers.append(exp_logger)
        return exp_logger

    real_sleep = asyncio.sleep

    async def _no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    async def _coordinator_ready_stub(_port: int) -> bool:
        # The 40s ASGI readiness gate is exercised against real coordinators;
        # these tests must not sit on its deadline with a fake coordinator.
        return True

    monkeypatch.setattr(experiment, "SARBarrier", _barrier_factory)
    monkeypatch.setattr(experiment, "ExperimentLogger", _logger_factory)
    monkeypatch.setattr(experiment, "SARCoordinator", _FakeCoordinator)
    monkeypatch.setattr(experiment, "SARWorker", _WorkerFactory)
    monkeypatch.setattr(experiment, "SandboxPolicy", _FakeSandboxPolicy)
    monkeypatch.setattr(experiment, "load_env_file", lambda _path: {})
    monkeypatch.setattr(experiment.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        experiment, "_wait_for_coordinator_ready", _coordinator_ready_stub
    )


# --------------------------------------------------------------------------
# classify_end_reason / helper units
# --------------------------------------------------------------------------


def test_classify_end_reason_reports_workers_dead_ahead_of_framework_error():
    assert (
        classify_end_reason(
            finished=False,
            steps=10,
            max_steps=50,
            elapsed_seconds=100.0,
            wall_clock_limit=3600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=False,
            workers_dead=True,
        )
        == "workers_dead"
    )
    # a finished mission still wins over dead threads
    assert (
        classify_end_reason(
            finished=True,
            steps=10,
            max_steps=50,
            elapsed_seconds=100.0,
            wall_clock_limit=3600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=False,
            workers_dead=True,
        )
        == "success"
    )
    # coordinator errors keep their own bucket when the team is still alive
    assert (
        classify_end_reason(
            finished=False,
            steps=10,
            max_steps=50,
            elapsed_seconds=100.0,
            wall_clock_limit=3600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=True,
            workers_dead=False,
        )
        == "framework_error"
    )


def test_worker_liveness_helpers():
    alive = _FakeWorker("Alice", alive=True)
    dead = _FakeWorker("Bob", alive=False, error=RuntimeError("boom"))

    assert worker_threads_all_dead({}) is False
    assert worker_threads_all_dead({"Alice": alive}) is False
    assert worker_threads_all_dead({"Alice": alive, "Bob": dead}) is False
    assert worker_threads_all_dead({"Bob": dead}) is True

    assert dead_worker_report({"Alice": alive, "Bob": dead}) == {"Bob": "RuntimeError: boom"}
    assert dead_worker_report({"Bob": _FakeWorker("Bob", alive=False)}) == {
        "Bob": "run thread exited (no error recorded)"
    }


# --------------------------------------------------------------------------
# run_experiment integration (faked barrier / coordinator / workers)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_experiment_aborts_immediately_when_team_is_dead(
    monkeypatch, tmp_path, caplog
):
    workers = {
        "Alice": _FakeWorker(
            "Alice", alive=False, error=RuntimeError("MCP start failed")
        ),
        "Bob": _FakeWorker("Bob", alive=False, error=RuntimeError("MCP start failed")),
    }
    exp_loggers: list = []
    barriers: list = []
    _install_fakes(monkeypatch, workers, exp_loggers, barriers)

    with caplog.at_level(logging.ERROR, logger="sar_orch.experiment"):
        metrics = await experiment.run_experiment(
            scene=1,
            num_agents=2,
            max_steps=200,
            log_dir=str(tmp_path / "run"),
        )

    assert metrics["end_reason"] == "workers_dead"
    assert exp_loggers[0].end_reasons == ["workers_dead"]
    assert metrics["dead_workers"] == {
        "Alice": "RuntimeError: MCP start failed",
        "Bob": "RuntimeError: MCP start failed",
    }
    # Aborted on the first poll instead of burning the 200-step budget.
    assert metrics["steps"] < 200
    assert barriers[0].poll_calls <= 3
    assert any("aborting run" in record.message for record in caplog.records)
    # run_metrics.json carries the explicit reason + per-worker failure.
    written = json.loads((tmp_path / "run" / "run_metrics.json").read_text(encoding="utf-8"))
    assert written["end_reason"] == "workers_dead"
    assert set(written["dead_workers"]) == {"Alice", "Bob"}


@pytest.mark.asyncio
async def test_run_experiment_keeps_polling_while_a_worker_is_alive(
    monkeypatch, tmp_path
):
    workers = {
        "Alice": _FakeWorker("Alice", alive=True),
        "Bob": _FakeWorker("Bob", alive=False),
    }
    exp_loggers: list = []
    barriers: list = []
    _install_fakes(monkeypatch, workers, exp_loggers, barriers)

    metrics = await experiment.run_experiment(
        scene=1,
        num_agents=2,
        max_steps=3,
        log_dir=str(tmp_path / "run"),
    )

    assert metrics["end_reason"] == "max_steps_reached"
    assert "dead_workers" not in metrics
