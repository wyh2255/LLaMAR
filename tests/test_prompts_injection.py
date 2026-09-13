"""env-contract G6/P4-4: prompts roots are an explicit, injectable assembly input.

``run_experiment`` resolves ``coordinator_prompts_dir`` / ``worker_prompts_dir``
(``None`` → the current in-tree ``sar_orch/prompts/...`` layout) and hands them
to ``SAREnvPack`` —— P4-4 起 EnvPack 是唯一携带面（通用装配层从契约面消费；
构造器不再逐参数接收 ``prompts_dir``）。本组测试用 sentinel-raising stub 捕获
装配层构造面：既断言解析值进入 EnvPack，又断言 coordinator/worker 拿到的
``env_pack`` 就是同一个实例（消费链闭合）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import sar_orch.experiment as experiment_module


class _StopAfterCapture(Exception):
    """Sentinel raised by the stub constructors once kwargs are captured."""


def _base_kwargs(tmp_path: Path) -> dict:
    return {
        "scene": 1,
        "num_agents": 1,
        "seed": 42,
        "log_dir": str(tmp_path / "run"),
        "truth_output_dir": str(tmp_path / "truth"),
        "memory_read_mode": "legacy",
        "sandbox_profile": "off",
    }


class _FakeBarrier:
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


class _FakeEnvPack:
    """SAR EnvPack 替身：捕获构造参数并暴露 prompts 契约面。"""

    name = "sar"

    def __init__(self, *, captured: list, **kwargs) -> None:
        captured.append(kwargs)
        self.kwargs = dict(kwargs)

    def build_barrier(self, *, num_agents, seed, **env_params):
        return _FakeBarrier()

    @property
    def coordinator_prompts_dir(self):
        return self.kwargs.get("coordinator_prompts_dir")

    @property
    def worker_prompts_dir(self):
        return self.kwargs.get("worker_prompts_dir")


async def _no_sleep(_seconds):
    return None


def test_experiment_passes_default_coordinator_prompts(monkeypatch, tmp_path):
    """No override → the current in-tree coordinator prompts dir (status quo)."""
    captured: dict = {}
    pack_kwargs: list[dict] = []

    def fake_coordinator(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()

    def fake_pack(**kwargs):
        return _FakeEnvPack(captured=pack_kwargs, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(experiment_module, "SAREnvPack", fake_pack)
    monkeypatch.setattr(
        "orchestration.assembly.OrchestratorCoordinator", fake_coordinator
    )
    with pytest.raises(_StopAfterCapture):
        asyncio.run(experiment_module.run_experiment(**_base_kwargs(tmp_path)))

    assert pack_kwargs[0]["coordinator_prompts_dir"] == experiment_module._COORDINATOR_PROMPTS
    # 消费链闭合：coordinator 构造面拿到的正是同一个 EnvPack 实例
    assert isinstance(captured["env_pack"], _FakeEnvPack)
    assert captured["env_pack"].coordinator_prompts_dir == experiment_module._COORDINATOR_PROMPTS


def test_experiment_passes_injected_coordinator_prompts(monkeypatch, tmp_path):
    """Explicit override → passed through to the SAR EnvPack (and consumed)."""
    captured: dict = {}
    pack_kwargs: list[dict] = []
    target = str(tmp_path / "coordinator_prompts")
    Path(target).mkdir()

    def fake_coordinator(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()

    def fake_pack(**kwargs):
        return _FakeEnvPack(captured=pack_kwargs, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(experiment_module, "SAREnvPack", fake_pack)
    monkeypatch.setattr(
        "orchestration.assembly.OrchestratorCoordinator", fake_coordinator
    )
    with pytest.raises(_StopAfterCapture):
        asyncio.run(
            experiment_module.run_experiment(
                coordinator_prompts_dir=target, **_base_kwargs(tmp_path)
            )
        )

    assert pack_kwargs[0]["coordinator_prompts_dir"] == target
    assert captured["env_pack"].coordinator_prompts_dir == target


def test_experiment_passes_injected_worker_prompts(monkeypatch, tmp_path):
    """Worker side: the resolved worker prompts dir reaches the assembly EnvPack."""
    captured: dict = {}
    pack_kwargs: list[dict] = []

    class _FakeCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            return None

        async def stop(self):
            return None

        def clear_sessions(self):
            return None

    def fake_worker(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()

    def fake_pack(**kwargs):
        return _FakeEnvPack(captured=pack_kwargs, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(experiment_module, "SAREnvPack", fake_pack)
    monkeypatch.setattr(
        "orchestration.assembly.OrchestratorCoordinator", _FakeCoordinator
    )
    monkeypatch.setattr("orchestration.assembly.OrchestratorWorker", fake_worker)

    target = str(tmp_path / "worker_prompts")
    Path(target).mkdir()
    with pytest.raises(_StopAfterCapture):
        asyncio.run(
            experiment_module.run_experiment(
                worker_prompts_dir=target, **_base_kwargs(tmp_path)
            )
        )

    assert pack_kwargs[0]["worker_prompts_dir"] == target
    assert captured["env_pack"].worker_prompts_dir == target


def test_experiment_prompts_missing_dir_fails_fast(monkeypatch, tmp_path):
    """显式 prompts 目录不存在 → 硬错误（fail-fast，不静默回退到空 system prompt）。"""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            experiment_module.run_experiment(
                coordinator_prompts_dir=str(tmp_path / "missing"),
                **_base_kwargs(tmp_path),
            )
        )


def test_experiment_defaults_resolve_to_current_layout():
    """Guard: the module-level defaults are the current in-tree layout."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(experiment_module.__file__)))
    assert experiment_module._COORDINATOR_PROMPTS == os.path.join(
        root, "sar_orch", "prompts", "coordinator"
    )
    assert experiment_module._WORKER_PROMPTS == os.path.join(
        root, "sar_orch", "prompts", "worker"
    )
