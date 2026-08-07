"""Regression tests for experiment step-budget propagation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import sar_orch.experiment as experiment
from sar_orch.coordinator import SARCoordinator


def test_coordinator_initial_budget_uses_configured_max_steps():
    coordinator = SARCoordinator(max_steps=1)

    assert coordinator._initial_step_budget() == {
        "current_step": 0,
        "max_steps": 1,
        "remaining": 1,
    }


@pytest.mark.asyncio
async def test_run_experiment_forwards_max_steps_to_coordinator(monkeypatch, tmp_path):
    coordinator_configs: list[dict] = []
    cleanup_events: list[str] = []

    class FakeBarrier:
        def __init__(self, **_kwargs) -> None:
            self.stopped = False

        def is_finished(self) -> bool:
            return True

        def get_metrics(self) -> dict:
            return {
                "finished": False,
                "steps": 0,
                "coverage": 0.0,
                "transport_rate": 0.0,
            }

        def stop(self) -> None:
            self.stopped = True
            cleanup_events.append("barrier")

    class FakeExperimentLogger:
        def __init__(self, *, log_dir: str, **_kwargs) -> None:
            self._log_dir = log_dir

        def set_run_context(self, **_kwargs) -> None:
            pass

        def write_metadata(self, _metadata: dict) -> None:
            pass

        def get_log_dir(self) -> str:
            return self._log_dir

        def set_end_reason(self, _reason: str) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            coordinator_configs.append(kwargs)
            self._semantic_map = None

        async def start(self) -> None:
            return None

        async def submit_task(self, _description: str) -> None:
            return None

        def clear_sessions(self) -> None:
            pass

        async def stop(self) -> None:
            cleanup_events.append("coordinator")

    class FakeWorker:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def clear_sessions(self) -> None:
            pass

        def stop(self) -> None:
            cleanup_events.append("worker")

    class FakeSandboxPolicy:
        @staticmethod
        def workspace(**_kwargs):
            return SimpleNamespace()

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(experiment, "SARBarrier", FakeBarrier)
    monkeypatch.setattr(experiment, "ExperimentLogger", FakeExperimentLogger)
    monkeypatch.setattr(experiment, "SARCoordinator", FakeCoordinator)
    monkeypatch.setattr(experiment, "SARWorker", FakeWorker)
    monkeypatch.setattr(experiment, "SandboxPolicy", FakeSandboxPolicy)
    monkeypatch.setattr(experiment, "load_env_file", lambda _path: {})
    monkeypatch.setattr(experiment.asyncio, "sleep", no_sleep)

    await experiment.run_experiment(
        scene=1,
        num_agents=1,
        max_steps=1,
        log_dir=str(tmp_path / "run"),
    )

    assert coordinator_configs[0]["max_steps"] == 1
    assert coordinator_configs[0]["map_summary_path"] == str(
        tmp_path / "run" / "map_summary.jsonl"
    )
    assert cleanup_events == ["barrier", "worker", "coordinator"]


@pytest.mark.asyncio
async def test_run_experiment_forwards_memory_read_mode_to_worker(
    monkeypatch, tmp_path
):
    """``run_experiment`` must propagate ``memory_read_mode`` to every
    ``SARWorker`` so a read_port/shadow worker uses the authenticated
    /environment-state read path instead of silently defaulting to legacy."""
    worker_configs: list[dict] = []

    class FakeBarrier:
        def __init__(self, **_kwargs) -> None:
            self.stopped = False

        def is_finished(self) -> bool:
            return True

        def get_metrics(self) -> dict:
            return {
                "finished": False,
                "steps": 0,
                "coverage": 0.0,
                "transport_rate": 0.0,
            }

        def stop(self) -> None:
            self.stopped = True

    class FakeExperimentLogger:
        def __init__(self, *, log_dir: str, **_kwargs) -> None:
            self._log_dir = log_dir

        def set_run_context(self, **_kwargs) -> None:
            pass

        def write_metadata(self, _metadata: dict) -> None:
            pass

        def get_log_dir(self) -> str:
            return self._log_dir

        def set_end_reason(self, _reason: str) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self._semantic_map = None

        async def start(self) -> None:
            return None

        async def submit_task(self, _description: str) -> None:
            return None

        def clear_sessions(self) -> None:
            pass

        async def stop(self) -> None:
            return None

    class FakeWorker:
        def __init__(self, **kwargs) -> None:
            worker_configs.append(kwargs)

        def start(self) -> None:
            pass

        def clear_sessions(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeSandboxPolicy:
        @staticmethod
        def workspace(**_kwargs):
            return SimpleNamespace()

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(experiment, "SARBarrier", FakeBarrier)
    monkeypatch.setattr(experiment, "ExperimentLogger", FakeExperimentLogger)
    monkeypatch.setattr(experiment, "SARCoordinator", FakeCoordinator)
    monkeypatch.setattr(experiment, "SARWorker", FakeWorker)
    monkeypatch.setattr(experiment, "SandboxPolicy", FakeSandboxPolicy)
    monkeypatch.setattr(experiment, "load_env_file", lambda _path: {})
    monkeypatch.setattr(experiment.asyncio, "sleep", no_sleep)

    await experiment.run_experiment(
        scene=1,
        num_agents=2,
        max_steps=1,
        memory_read_mode="read_port",
        log_dir=str(tmp_path / "run"),
    )

    assert len(worker_configs) == 2
    for config in worker_configs:
        assert config["memory_read_mode"] == "read_port"
