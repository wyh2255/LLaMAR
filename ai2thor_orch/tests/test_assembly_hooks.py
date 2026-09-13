"""Tests for ``AI2ThorAssemblyHooks`` (env-contract P5-3).

The hooks carry the AI2Thor-specific assembly-window artifacts moved out of
the retired ``AI2ThorExperiment``: the task contract snapshot
(``task_config.json``), the per-round verifier trace (``verifier_trace.ndjson``)
and the terminal ``summary.json`` (benchmark v2 consumer surface).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai2thor_orch.assembly_hooks import AI2ThorAssemblyHooks
from ai2thor_orch.contracts.task import load_task
from ai2thor_orch.contracts.types import RunStatus
from ai2thor_orch.env_pack import Ai2ThorEnvPack
from orchestration.assembly import AssemblySpec, AssemblyState

pytestmark = pytest.mark.unit

TASK_ID = "3_transport_groceries"


class _FakeBarrier:
    """Minimal barrier stand-in for the hooks' terminal reads."""

    def __init__(self, *, task_metrics=None, round_result=None, status=None):
        self._task_metrics = task_metrics or {}
        self._round_result = round_result
        self._status = status

    def last_round_result(self):
        return self._round_result

    def get_task_metrics(self) -> dict:
        return dict(self._task_metrics)

    def get_run_status(self) -> RunStatus:
        return self._status or RunStatus()


def _make_state(tmp_path: Path, *, barrier=None, max_steps: int = 8) -> AssemblyState:
    pack = Ai2ThorEnvPack(task_id=TASK_ID, scene="FloorPlan1", mode="fake")
    spec = AssemblySpec(
        env_pack=pack,
        log_dir=str(tmp_path),
        num_agents=2,
        agent_names=["Alice", "Bob"],
        seed=42,
        run_id="rid-p53",
        max_steps=max_steps,
    )
    return AssemblyState(
        spec=spec,
        env_pack=pack,
        exp_dir=tmp_path,
        run_id="rid-p53",
        max_steps=max_steps,
        barrier=barrier,
    )


def _make_hooks() -> AI2ThorAssemblyHooks:
    return AI2ThorAssemblyHooks(
        task_id=TASK_ID,
        scene="FloorPlan1",
        mode="fake",
        seed=42,
        num_agents=2,
        contract=load_task(TASK_ID, "FloorPlan1"),
    )


def test_on_environment_ready_writes_task_config(tmp_path):
    state = _make_state(tmp_path)
    _make_hooks().on_environment_ready(state)

    config = json.loads((tmp_path / "task_config.json").read_text(encoding="utf-8"))
    assert config["schema_version"] == 1
    assert config["task_id"] == TASK_ID
    assert config["scene"] == "FloorPlan1"
    assert config["mode"] == "fake"
    assert config["seed"] == 42
    assert config["num_agents"] == 2
    assert config["max_steps"] == 8
    assert config["subtasks"]
    assert config["coverage_objects"]
    # initial_inventory keys are stringified agent indices.
    assert all(isinstance(k, str) for k in config["initial_inventory"])


def test_on_step_appends_verifier_trace(tmp_path):
    state = _make_state(tmp_path)
    hooks = _make_hooks()

    hooks.on_step(
        state,
        step_num=1,
        step_log={
            "verified_completion": False,
            "coverage": 0.25,
            "transport_rate": 0.0,
            "interaction_coverage": 0.0,
            "finished": False,
        },
        drained_logs=[],
    )
    hooks.on_step(
        state,
        step_num=2,
        step_log={
            "verified_completion": True,
            "coverage": 1.0,
            "transport_rate": 0.5,
            "interaction_coverage": 1.0,
            "finished": True,
        },
        drained_logs=[],
    )

    lines = [
        json.loads(line)
        for line in (tmp_path / "verifier_trace.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["step"] for row in lines] == [1, 2]
    assert lines[0]["coverage"] == 0.25
    assert lines[1]["verified_completion"] is True
    assert lines[1]["finished"] is True


def test_finalize_artifacts_writes_summary_and_merges(tmp_path, monkeypatch):
    # verify_round is independently covered by test_verifier.py; stub it here
    # to pin the hooks' summary/merge contract, not the verifier internals.
    def fake_verify_round(round_result, contract):
        return {
            "verified_completion": True,
            "coverage": 1.0,
            "goal_coverage": 1.0,
            "details": {},
        }

    monkeypatch.setattr(
        "ai2thor_orch.verifier.verifier.verify_round", fake_verify_round
    )

    barrier = _FakeBarrier(
        task_metrics={
            "interaction_coverage": 0.5,
            "transport_rate": 0.25,
            "action_attempts": 4,
            "successful_actions": 3,
            "failed_actions": 1,
            "action_success_rate": 0.75,
            "timeout_count": 0,
            "timeout_rounds": 0,
            "balance": 0.9,
            "completed_subtask_count": 1,
            "total_subtasks": 2,
            "per_agent_successful_actions": {"Alice": 2, "Bob": 1},
        },
        round_result=object(),
        status=RunStatus(
            step=8, max_steps=8, finished=False, stopped=True, stop_reason="budget_exhausted"
        ),
    )
    state = _make_state(tmp_path, barrier=barrier)
    hooks = _make_hooks()

    merged = hooks.finalize_artifacts(
        state,
        final_metrics={
            "run_id": "rid-p53",
            "steps": 8,
            "finished": False,
            "max_steps": 8,
            "elapsed_seconds": 12.345,
        },
        end_reason="max_steps_reached",
    )

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == "rid-p53"
    assert summary["task_id"] == TASK_ID
    assert summary["scene"] == "FloorPlan1"
    assert summary["mode"] == "fake"
    assert summary["num_agents"] == 2
    assert summary["seed"] == 42
    assert summary["metric_schema_version"] == 2
    assert summary["max_steps"] == 8
    assert summary["rounds_completed"] == 8
    assert summary["finished"] is False
    assert summary["stopped"] is True
    assert summary["stop_reason"] == "budget_exhausted"
    assert summary["end_reason"] == "max_steps_reached"
    # Verifier terminal verdict (postcondition truth) = stubbed values.
    assert summary["verified_completion"] is True
    assert summary["coverage"] == 1.0
    assert summary["goal_coverage"] == 1.0
    # Task-metrics cumulative snapshot.
    assert summary["interaction_coverage"] == 0.5
    assert summary["transport_rate"] == 0.25
    assert summary["action_success_rate"] == 0.75
    assert summary["balance"] == 0.9
    assert summary["completed_subtask_count"] == 1
    assert summary["total_subtasks"] == 2
    assert summary["per_agent_successful_actions"] == {"Alice": 2, "Bob": 1}
    assert summary["elapsed_seconds"] == 12.35
    assert summary["log_dir"] == str(tmp_path)

    # Merged payload feeds run_metrics.json.
    assert merged["verified_completion"] is True
    assert merged["goal_coverage"] == 1.0
    assert merged["summary_json"] == str(tmp_path / "summary.json")


def test_finalize_artifacts_without_barrier(tmp_path):
    """No barrier (degenerate assembly) still produces a valid summary."""
    state = _make_state(tmp_path, barrier=None)
    merged = _make_hooks().finalize_artifacts(
        state,
        final_metrics={"steps": 0, "finished": False, "max_steps": 8, "elapsed_seconds": 0.0},
        end_reason="stopped_before_success",
    )

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["verified_completion"] is False
    assert summary["coverage"] == 0.0
    assert summary["rounds_completed"] == 0
    assert summary["end_reason"] == "stopped_before_success"
    assert merged["verified_completion"] is False
