"""Tests for ``AI2ThorExperimentLogger`` (env-contract P5-3).

The logger is the assembly-contract consumer surface: the generic poll loop
calls ``log_step`` / ``flush_summary`` / ``set_end_reason`` / ``freeze_terminal``
while coordinator/worker threads call ``log_event`` / ``log_subtask`` /
``log_router_interaction`` / ``log_token_usage``.  These tests pin the CSV /
NDJSON artifact shapes under a run directory.
"""

from __future__ import annotations

import ast
import csv
import json
from pathlib import Path

import pytest

from ai2thor_orch.logger import (
    AI2THOR_TRAJECTORY_HEADERS,
    AI2ThorExperimentLogger,
)

pytestmark = pytest.mark.unit


def _read_rows(path: Path) -> list[dict]:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def test_logger_writes_all_artifacts(tmp_path):
    logger = AI2ThorExperimentLogger(
        experiment_name="3_transport_groceries", log_dir=str(tmp_path)
    )
    logger.set_run_context(run_id="rid-1", model="test-model", prompt_version="baseline")
    logger.write_metadata({"run_id": "rid-1", "task_id": "3_transport_groceries"})

    # Two executed rounds (assembly poll-loop surface).
    logger.log_step(
        1,
        actions=["move_ahead", "rotate_left"],
        successes=[True, True],
        observations=["ok", "ok"],
        coverage=0.25,
        transport_rate=0.0,
        finished=False,
        noop_sources=["", ""],
        max_steps=8,
        remaining_steps=7,
        wall_time_since_start=1.5,
        step_duration_ms=120.0,
    )
    logger.log_step(
        2,
        actions=["pickup:Bread_1", "move_right"],
        successes=[True, False],
        observations=["picked", "blocked"],
        coverage=0.5,
        transport_rate=0.25,
        finished=False,
        timeout_agents=[1],
        noop_sources=["", "timeout_injected"],
        max_steps=8,
        remaining_steps=6,
        error_types=["not_visible"],
    )

    # Runtime surfaces used by the generic coordinator/worker skeletons.
    logger.log_agent_interaction(
        step=1,
        agent="Alice",
        tool_name="move",
        tool_args="ahead",
        observation="ok",
        success=True,
    )
    logger.log_router_interaction(
        step=1, subtask="s1", assigned_to="Alice", event_type="assign_task", success=True
    )
    logger.log_coordinator_state(step=1, state_summary="scene=FloorPlan1")
    logger.log_token_usage(
        step=1, agent="Alice", prompt_tokens=100, completion_tokens=20, total_tokens=120
    )
    logger.log_token_usage(
        step=1, agent="Alice", prompt_tokens=50, completion_tokens=10, total_tokens=60
    )
    logger.log_event("assign_task", {"to": "Alice"}, step=1)
    logger.log_subtask("s1", "assigned", step=1, assigned_to="Alice", subtask="deliver bread")

    # Terminal ordering as the assembly does it: freeze, end_reason backfill, close.
    logger.freeze_terminal()
    logger.set_end_reason("max_steps_reached")
    logger.close()

    # ── trajectory.csv ────────────────────────────────────────────────────
    traj = _read_rows(tmp_path / "trajectory.csv")
    assert len(traj) == 2
    assert list(traj[0].keys()) == AI2THOR_TRAJECTORY_HEADERS
    assert ast.literal_eval(traj[0]["Actions"]) == ["move_ahead", "rotate_left"]
    assert ast.literal_eval(traj[1]["TimeoutAgents"]) == [1]
    assert ast.literal_eval(traj[1]["NoOpSource"]) == ["", "timeout_injected"]
    assert traj[0]["RunID"] == "rid-1"
    assert traj[1]["MaxSteps"] == "8"
    # EndReason backfilled on every row by set_end_reason().
    assert {r["EndReason"] for r in traj} == {"max_steps_reached"}

    # ── summary.csv (single-row aggregate, crash-safe) ────────────────────
    summary = _read_rows(tmp_path / "summary.csv")
    assert len(summary) == 1
    row = summary[0]
    assert row["ExperimentName"] == "3_transport_groceries"
    assert row["LogDir"] == str(tmp_path)
    assert row["TotalSteps"] == "2"
    assert row["FinalCoverage"] == "0.5"
    assert row["FinalTransportRate"] == "0.25"
    assert row["Finished"] == "False"
    assert row["EndReason"] == "max_steps_reached"
    assert row["TotalRouterInteractions"] == "1"
    # agent_interactions rows: one tool call + one coordinator_state snapshot.
    assert row["TotalAgentInteractions"] == "1"
    # Per-agent token accumulators (task_metrics off, LLM requests counted).
    assert row["AlicePromptTokens"] == "150"
    assert row["AliceCompletionTokens"] == "30"
    assert row["AliceTotalTokens"] == "180"

    # ── token_usage.csv: one row per LLM request ──────────────────────────
    tokens = _read_rows(tmp_path / "token_usage.csv")
    assert len(tokens) == 2
    assert tokens[0]["Agent"] == "Alice" and tokens[0]["TotalTokens"] == "120"
    assert tokens[0]["RunID"] == "rid-1"
    assert tokens[0]["Model"] == "test-model"  # default from set_run_context

    # ── events.ndjson ─────────────────────────────────────────────────────
    events = [
        json.loads(line)
        for line in (tmp_path / "events.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["event_type"] == "assign_task"
    assert events[0]["run_id"] == "rid-1"
    assert events[0]["step"] == 1
    assert events[0]["payload"] == {"to": "Alice"}

    # ── subtasks.csv / metadata.json ──────────────────────────────────────
    subtasks = _read_rows(tmp_path / "subtasks.csv")
    assert len(subtasks) == 1
    assert subtasks[0]["SubtaskID"] == "s1"
    assert subtasks[0]["Status"] == "assigned"
    assert subtasks[0]["AssignedTo"] == "Alice"
    meta = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "rid-1"

    # ── get_log_dir contract ──────────────────────────────────────────────
    assert logger.get_log_dir() == str(tmp_path)


def test_freeze_terminal_blocks_post_terminal_rows(tmp_path):
    """Teardown races must not append rows to run-terminal outcome CSVs."""
    logger = AI2ThorExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(run_id="rid-2")
    logger.log_agent_interaction(
        step=1, agent="Alice", tool_name="move", tool_args="ahead", success=True
    )
    logger.freeze_terminal()
    logger.log_agent_interaction(
        step=2, agent="Alice", tool_name="move", tool_args="ahead", success=True
    )
    logger.log_router_interaction(step=2, subtask="s2", assigned_to="Bob")
    logger.flush_summary()
    logger.close()

    assert len(_read_rows(tmp_path / "agent_interactions.csv")) == 1
    # Frozen before the first router row: the file is lazily created on first
    # unfrozen write, so it must not exist (or hold zero rows).
    router_csv = tmp_path / "router_interactions.csv"
    assert not router_csv.exists() or _read_rows(router_csv) == []
