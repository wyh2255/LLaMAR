"""Regression tests for experiment step-budget propagation.

P4-4: the experiment assembly moved to ``orchestration.assembly`` — monkeypatch
targets follow the implementation location (barrier → ``SAREnvPack`` 工厂；
coordinator/worker → ``orchestration.assembly`` 骨架类；sandbox/env loader →
装配层），断言口径不变（值链路 + 收尾顺序）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import sar_orch.experiment as experiment
from sar_orch.coordinator import SARCoordinator


def test_coordinator_initial_budget_uses_configured_max_steps():
    # Default memory_read_mode is read_port since H3 retirement (2026-08-10);
    # a protected callback secret is required for the secure memory modes.
    coordinator = SARCoordinator(
        max_steps=1, coordinator_secret=bytes(range(32))
    )

    assert coordinator._initial_step_budget() == {
        "current_step": 0,
        "max_steps": 1,
        "remaining": 1,
    }


class _FakeEnvPack:
    """最小 SAR EnvPack 替身：记录构造面并产出 FakeBarrier。"""

    name = "sar"

    def __init__(self, *, barrier_cls, **kwargs) -> None:
        self._barrier_cls = barrier_cls
        self.kwargs = dict(kwargs)

    def build_barrier(self, *, num_agents, seed, **env_params):
        return self._barrier_cls(num_agents=num_agents, seed=seed, **env_params)

    @property
    def coordinator_prompts_dir(self):
        return self.kwargs.get("coordinator_prompts_dir")

    @property
    def worker_prompts_dir(self):
        return self.kwargs.get("worker_prompts_dir")


@pytest.mark.asyncio
async def test_run_experiment_forwards_max_steps_to_coordinator(monkeypatch, tmp_path):
    coordinator_configs: list[dict] = []
    cleanup_events: list[str] = []
    pack_configs: list[dict] = []

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
            self._observation_source = None

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

    class FakePack(_FakeEnvPack):
        def __init__(self, **kwargs) -> None:
            pack_configs.append(kwargs)
            super().__init__(barrier_cls=FakeBarrier, **kwargs)

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(experiment, "SAREnvPack", FakePack)
    monkeypatch.setattr(experiment, "ExperimentLogger", FakeExperimentLogger)
    monkeypatch.setattr(
        "orchestration.assembly.OrchestratorCoordinator", FakeCoordinator
    )
    monkeypatch.setattr("orchestration.assembly.OrchestratorWorker", FakeWorker)
    monkeypatch.setattr(
        "orchestration.assembly.SandboxPolicy", FakeSandboxPolicy
    )
    monkeypatch.setattr(
        "orchestration.assembly.load_env_file", lambda _path: {}
    )
    monkeypatch.setattr(experiment.asyncio, "sleep", no_sleep)

    await experiment.run_experiment(
        scene=1,
        num_agents=1,
        max_steps=1,
        log_dir=str(tmp_path / "run"),
    )

    assert coordinator_configs[0]["max_steps"] == 1
    # P4-4: map_summary 路径经 EnvPack 携带（消费方从构造器参数移到契约面）
    assert pack_configs[0]["map_summary_path"] == str(
        tmp_path / "run" / "map_summary.jsonl"
    )
    assert cleanup_events == ["barrier", "worker", "coordinator"]


@pytest.mark.asyncio
async def test_run_experiment_forwards_memory_read_mode_to_worker(
    monkeypatch, tmp_path
):
    """``run_experiment`` must propagate ``memory_read_mode`` to every
    worker so a read_port/shadow worker uses the authenticated
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
            self._observation_source = None

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

    class FakePack(_FakeEnvPack):
        def __init__(self, **kwargs) -> None:
            super().__init__(barrier_cls=FakeBarrier, **kwargs)

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(experiment, "SAREnvPack", FakePack)
    monkeypatch.setattr(experiment, "ExperimentLogger", FakeExperimentLogger)
    monkeypatch.setattr(
        "orchestration.assembly.OrchestratorCoordinator", FakeCoordinator
    )
    monkeypatch.setattr("orchestration.assembly.OrchestratorWorker", FakeWorker)
    monkeypatch.setattr(
        "orchestration.assembly.SandboxPolicy", FakeSandboxPolicy
    )
    monkeypatch.setattr(
        "orchestration.assembly.load_env_file", lambda _path: {}
    )
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


def test_state_provider_fallback_budget_uses_effective_max_steps():
    """P4-4 口径收口：无语义地图的 fallback 投影反映**实际生效预算**。

    迁移前 fallback 直接展示 ``barrier.env.task_timeout``（scene_1=1200 /
    scene_2-5=35），与实际生效的 ``PAPER_MAX_STEPS``（30）矛盾，会误导
    coordinator 的 step-budget 决策；装配层经 EnvPack 注入真实 ``max_steps``
    后，legacy 口径只在未注入（None，既有构造点/测试替身）时保留。
    """
    from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

    class _SceneEnv:
        task_timeout = 1200

    class _Barrier:
        _step_counter = 3
        env = _SceneEnv()

        def is_finished(self) -> bool:
            return False

    provider = SARCoordinatorStateProvider(
        barrier=_Barrier(), semantic_map=None, max_steps=30
    )
    expected = {"current_step": 3, "max_steps": 30, "remaining": 27}
    assert provider.snapshot().payload["step_budget"] == expected
    assert provider.mission_graph_snapshot()["step_budget"] == expected

    # 未注入 max_steps（既有构造点）→ 保持 legacy task_timeout 口径
    legacy = SARCoordinatorStateProvider(barrier=_Barrier(), semantic_map=None)
    assert legacy.snapshot().payload["step_budget"]["max_steps"] == 1200
    assert (
        legacy.mission_graph_snapshot()["step_budget"]["max_steps"] == 1200
    )
