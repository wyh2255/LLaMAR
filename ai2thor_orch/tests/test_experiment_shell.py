"""Offline tests for the AI2Thor experiment thin shell (env-contract P5-3).

The thin shell ``ai2thor_orch/experiment/`` only binds the environment side
(``Ai2ThorEnvPack`` / ``AI2ThorAssemblyHooks`` / ``AI2ThorExperimentLogger``)
into the generic assembly ``orchestration.assembly.run_assembly``.  The full
assembly loop is covered by the root suite (``tests/test_assembly_wiring.py``)
and exercised for real by the CLI (the P5-3 runbook's fake mode short run);
here ``run_assembly`` is stubbed so this suite stays offline and deterministic.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ai2thor_orch.assembly_hooks import AI2ThorAssemblyHooks
from ai2thor_orch.env_pack import Ai2ThorEnvPack
from ai2thor_orch.experiment import ai2thor_experiment
from ai2thor_orch.experiment.ai2thor_experiment import (
    _DEFAULT_TASK,
    run_experiment,
)
from ai2thor_orch.logger import AI2ThorExperimentLogger

pytestmark = pytest.mark.unit


class _RunAssemblySpy:
    """Async stub capturing the AssemblySpec and returning canned metrics."""

    def __init__(self) -> None:
        self.spec = None

    async def __call__(self, spec):
        self.spec = spec
        return {
            "run_id": spec.run_id,
            "steps": 3,
            "finished": False,
            "verified_completion": False,
            "end_reason": "max_steps_reached",
            "elapsed_seconds": 1.0,
        }


def test_run_experiment_builds_assembly_spec(tmp_path, monkeypatch):
    """run_experiment() assembles every env-side input into the spec."""
    spy = _RunAssemblySpy()
    monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

    result = asyncio.run(
        run_experiment(
            task_id="3_transport_groceries",
            scene="FloorPlan1",
            num_agents=2,
            seed=42,
            mode="fake",
            max_steps=8,
            log_dir=str(tmp_path),
        )
    )

    assert result["steps"] == 3 and result["end_reason"] == "max_steps_reached"

    spec = spy.spec
    assert spec is not None
    # Env pack: AI2Thor pack, and max_steps flows into the barrier params so
    # the barrier gate and the poll loop share one budget.
    assert isinstance(spec.env_pack, Ai2ThorEnvPack)
    assert spec.env_pack.name == "ai2thor"
    assert spec.env_params == {
        "scene": "FloorPlan1",
        "mode": "fake",
        "step_timeout": 60.0,
        "max_steps": 8,
    }
    assert spec.env_params["max_steps"] == spec.max_steps == 8
    assert spec.wall_clock_limit == 3600.0

    # Run identity / workers.
    assert spec.num_agents == 2
    assert spec.agent_names == ["Alice", "Bob"]
    assert spec.seed == 42
    assert spec.log_dir == str(tmp_path)
    assert spec.run_id.startswith("ai2thor-3_transport_groceries-agents2-seed42-")

    # P5-3 scope: semantic state injection, no canonical Memory / long-term.
    assert spec.state_mode == "semantic"
    assert spec.memory_read_mode == "legacy"
    assert spec.long_term_mode == "off"
    assert spec.sandbox_profile == "workspace"

    assert spec.task_description == _DEFAULT_TASK
    assert isinstance(spec.hooks, AI2ThorAssemblyHooks)
    assert spec.env_file and spec.env_file.endswith(".env")

    # metadata.json payload (written by the logger before round 1).
    md = spec.metadata
    assert md["task_id"] == "3_transport_groceries"
    assert md["scene"] == "FloorPlan1"
    assert md["num_agents"] == 2
    assert md["agent_names"] == ["Alice", "Bob"]
    assert md["seed"] == 42
    assert md["mode"] == "fake"
    assert md["max_steps"] == 8
    assert md["run_id"] == spec.run_id
    assert md["state_mode"] == "semantic"
    assert md["coverage_objects"]  # contract-derived, non-empty
    assert md["subtasks"]
    assert md["start_time"]

    # Logger factory honours the assembly contract: log_dir -> logger.
    out = spec.logger_factory(str(tmp_path))
    assert isinstance(out, AI2ThorExperimentLogger)
    assert out.get_log_dir() == str(tmp_path)


def test_run_experiment_default_log_dir_shape(monkeypatch):
    """Default log dir lands under repo logs/ with a run-identifying name."""
    spy = _RunAssemblySpy()
    monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

    asyncio.run(run_experiment(num_agents=3, seed=7, mode="fake", max_steps=5))

    assert spy.spec is not None
    log_dir = Path(spy.spec.log_dir)
    assert log_dir.parent == ai2thor_experiment._LOGS_ROOT
    assert log_dir.name.endswith("_3_transport_groceries_FloorPlan1_a3_seed7_fake")


def test_run_experiment_custom_task_prompt(monkeypatch, tmp_path):
    """coordinator_prompt overrides the initial task statement."""
    spy = _RunAssemblySpy()
    monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

    asyncio.run(run_experiment(log_dir=str(tmp_path), coordinator_prompt="Do X"))

    assert spy.spec is not None
    assert spy.spec.task_description == "Do X"


def test_cli_main_maps_flags_and_exit_code(monkeypatch, tmp_path):
    """``python -m ai2thor_orch.experiment`` keeps its CLI surface (P5-3)."""
    import ai2thor_orch.experiment.__main__ as cli

    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"verified_completion": False, "finished": False}

    monkeypatch.setattr(cli, "run_experiment", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "experiment",
            "--task",
            "3_transport_groceries",
            "--scene",
            "FloorPlan1",
            "--agents",
            "2",
            "--seed",
            "42",
            "--mode",
            "fake",
            "--max-steps",
            "8",
            "--log-dir",
            str(tmp_path),
            "--coordinator-port",
            "18080",
            "--agent-base-port",
            "18191",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli.main())
    assert excinfo.value.code == 1  # unfinished run -> non-zero exit

    assert captured["task_id"] == "3_transport_groceries"
    assert captured["scene"] == "FloorPlan1"
    assert captured["num_agents"] == 2
    assert captured["seed"] == 42
    assert captured["mode"] == "fake"
    assert captured["max_steps"] == 8
    assert captured["log_dir"] == str(tmp_path)
    assert captured["coordinator_port"] == 18080
    assert captured["agent_base_port"] == 18191

    # Paper-gauge success (tracker tally filled) -> exit 0; the verifier
    # audit field does not gate the signal (F-done semantics).
    async def fake_run_ok(**kwargs):
        return {"verified_completion": False, "finished": True}

    monkeypatch.setattr(cli, "run_experiment", fake_run_ok)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli.main())
    assert excinfo.value.code == 0

    # Verifier audit alone (tally unfilled) is NOT success -> exit 1.
    async def fake_run_audit_only(**kwargs):
        return {"verified_completion": True, "finished": False}

    monkeypatch.setattr(cli, "run_experiment", fake_run_audit_only)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli.main())
    assert excinfo.value.code == 1
