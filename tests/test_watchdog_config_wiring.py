"""C2b: watchdog 阈值注链端到端（hooks → 装配 → OrchestratorCoordinator → create_server）。

三个跳点各自可执行验证：

1. ``AI2ThorAssemblyHooks.coordinator_kwargs`` 携带真机校准配置；
2. ``run_assembly`` 把该 kwargs 透传给 coordinator 构造器（hooks 注入面）；
3. ``OrchestratorCoordinator.start()`` 把 ``watchdog_config`` 转发 ``create_server``
   —— 未注入时保持 ``None``（内核缺省，SAR 路径零变化）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ai2thor_orch.assembly_hooks import AI2ThorAssemblyHooks, build_watchdog_config
from ai2thor_orch.contracts.task import load_task
from ai2thor_orch.contracts.types import RunStatus
from orchestration.assembly import AssemblySpec, run_assembly
from orchestration.coordinator import OrchestratorCoordinator
from orchestration.env_pack import EnvPack

TASK_ID = "3_transport_groceries"


class _SteppingBarrier:
    """最小 barrier（装配层 poll 循环 + AI2Thor 终局钩子消费面）。"""

    def __init__(self) -> None:
        self.steps = 0

    def is_finished(self) -> bool:
        return self.steps >= 1

    def get_metrics(self) -> dict:
        return {
            "finished": self.is_finished(),
            "steps": self.steps,
            "coverage": 0.0,
            "transport_rate": 0.0,
        }

    def get_run_status(self) -> RunStatus:
        return RunStatus()

    def drain_step_logs(self) -> list:
        if self.is_finished():
            return []
        self.steps += 1
        return []

    def stop(self) -> None:
        pass


class _Pack(EnvPack):
    name = "ai2thor-c2b"

    def build_barrier(self, *, num_agents, seed, **env_params):
        return _SteppingBarrier()


class _FakeCoordinator:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def start(self):
        return None

    async def submit_task(self, description):
        await asyncio.Event().wait()  # 保持 in-flight，直到终态取消

    def clear_sessions(self):
        pass

    async def stop(self):
        pass


class _FakeWorker:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def clear_sessions(self):
        pass

    def stop(self):
        pass


async def _no_sleep(_seconds):
    return None


def test_ai2thor_hooks_config_reaches_coordinator_factory(monkeypatch, tmp_path):
    """跳点 1+2：AI2Thor hooks 的校准阈值经 run_assembly 到达 coordinator 构造器。"""
    captured: dict = {}

    def coordinator_factory(**kwargs):
        captured.update(kwargs)
        return _FakeCoordinator(**kwargs)

    hooks = AI2ThorAssemblyHooks(
        task_id=TASK_ID,
        scene="FloorPlan1",
        mode="fake",
        seed=42,
        num_agents=1,
        contract=load_task(TASK_ID, "FloorPlan1"),
    )
    spec = AssemblySpec(
        env_pack=_Pack(),
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=42,
        run_id="c2b-wiring",
        max_steps=1,
        coordinator_factory=coordinator_factory,
        worker_factory=_FakeWorker,
        hooks=hooks,
    )
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    asyncio.run(run_assembly(spec))

    config = captured["watchdog_config"]
    assert config.stale_requires_both is True
    assert config.no_progress_step_threshold == 10
    assert config.task_stale_seconds == 90.0


@pytest.mark.asyncio
async def test_orchestrator_coordinator_forwards_watchdog_config(monkeypatch, tmp_path):
    """跳点 3：``start()`` 把 ``watchdog_config``（及缺省 None）转发 create_server。"""
    import a2a.coordinator.server as server_mod

    captured: list[dict] = []

    def fake_create_server(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(server_mod, "create_server", fake_create_server)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _CoordPack(EnvPack):
        name = "fake-c2b"

        def build_coordinator_state_provider(self, ctx):
            return MagicMock()

    preset = build_watchdog_config()

    async def _start(**extra):
        coord = OrchestratorCoordinator(
            env_pack=_CoordPack(),
            log_dir=str(tmp_path),
            supervision_dir=str(tmp_path / "supervision"),
            memory_read_mode="legacy",
            **extra,
        )
        await coord.start()

    await _start(watchdog_config=preset)
    await _start()  # 缺省路径（SAR/未注入）

    assert captured[0]["watchdog_config"] is preset
    assert captured[1]["watchdog_config"] is None
