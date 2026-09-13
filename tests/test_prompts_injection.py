"""env-contract G6: prompts roots are an explicit, injectable assembly input.

``run_experiment`` resolves ``coordinator_prompts_dir`` / ``worker_prompts_dir``
(``None`` → the current in-tree ``sar_orch/prompts/...`` layout) and passes the
resolved value into the SAR assembly (``SARCoordinator`` / ``SARWorker``).
These tests capture the assembled objects with sentinel-raising stubs and
assert the resolved value actually reaches each constructor.
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


def test_experiment_passes_default_coordinator_prompts(monkeypatch, tmp_path):
    """No override → the current in-tree coordinator prompts dir (status quo)."""
    captured: dict = {}

    def fake_coordinator(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()

    monkeypatch.setattr(experiment_module, "SARCoordinator", fake_coordinator)
    with pytest.raises(_StopAfterCapture):
        asyncio.run(experiment_module.run_experiment(**_base_kwargs(tmp_path)))

    assert captured["prompts_dir"] == experiment_module._COORDINATOR_PROMPTS


def test_experiment_passes_injected_coordinator_prompts(monkeypatch, tmp_path):
    """Explicit override → passed through to the coordinator constructor."""
    captured: dict = {}
    target = str(tmp_path / "coordinator_prompts")
    Path(target).mkdir()

    def fake_coordinator(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()

    monkeypatch.setattr(experiment_module, "SARCoordinator", fake_coordinator)
    with pytest.raises(_StopAfterCapture):
        asyncio.run(
            experiment_module.run_experiment(
                coordinator_prompts_dir=target, **_base_kwargs(tmp_path)
            )
        )

    assert captured["prompts_dir"] == target


def test_experiment_passes_injected_worker_prompts(monkeypatch, tmp_path):
    """Worker side: the resolved worker prompts dir reaches SARWorker."""
    captured: dict = {}

    class _FakeCoordinator:
        _semantic_map = None

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

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(experiment_module, "SARCoordinator", _FakeCoordinator)
    monkeypatch.setattr(experiment_module, "SARWorker", fake_worker)

    target = str(tmp_path / "worker_prompts")
    with pytest.raises(_StopAfterCapture):
        asyncio.run(
            experiment_module.run_experiment(
                worker_prompts_dir=target, **_base_kwargs(tmp_path)
            )
        )

    assert captured["prompts_dir"] == target


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
