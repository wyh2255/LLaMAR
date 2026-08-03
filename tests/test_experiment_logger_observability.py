from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.logger import ExperimentLogger
from sar_orch.experiment import build_run_metadata, classify_end_reason
from sar_orch.aggregate import classify_failure


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_logger_writes_metadata_and_extended_step_fields(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.write_metadata(
        {
            "run_id": "run-1",
            "env_name": "SAR",
            "scenario_id": "scene_1",
            "seed": 42,
            "agent_count": 2,
            "model": "fake-model",
            "prompt_version": "baseline",
        }
    )
    logger.log_step(
        step_num=1,
        actions=["NoOp()", "NoOp()"],
        successes=[True, True],
        observations=["obs-a", "obs-b"],
        coverage=0.25,
        transport_rate=0.5,
        finished=False,
        timeout_agents=[],
        run_id="run-1",
        max_steps=10,
        remaining_steps=9,
        wall_time_since_start=1.5,
        step_duration_ms=25.0,
        error_types=["", ""],
        completed_subtasks_delta=["NavigateTo(ReservoirUtah)"],
        end_reason="",
    )
    logger.close()

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == "run-1"
    assert metadata["scenario_id"] == "scene_1"

    rows = read_csv(tmp_path / "trajectory.csv")
    assert rows[0]["RunID"] == "run-1"
    assert rows[0]["MaxSteps"] == "10"
    assert rows[0]["RemainingSteps"] == "9"
    assert rows[0]["StepDurationMs"] == "25.0"
    assert "NavigateTo(ReservoirUtah)" in rows[0]["CompletedSubtasksDelta"]


def test_logger_writes_events_and_subtasks(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_event(
        "dispatch",
        run_id="run-1",
        step=2,
        agent="Coordinator",
        correlation_id="corr-1",
        payload={"worker": "Alice"},
    )
    logger.log_subtask(
        subtask_id="dispatch-1",
        status="assigned",
        run_id="run-1",
        step=2,
        assigned_to="Alice",
        subtask="Explore the west side",
    )
    logger.close()

    event_line = (tmp_path / "events.ndjson").read_text(encoding="utf-8").strip()
    event = json.loads(event_line)
    assert event["event_type"] == "dispatch"
    assert event["run_id"] == "run-1"
    assert event["correlation_id"] == "corr-1"
    assert event["payload"] == {"worker": "Alice"}

    rows = read_csv(tmp_path / "subtasks.csv")
    assert rows[0]["SubtaskID"] == "dispatch-1"
    assert rows[0]["Status"] == "assigned"
    assert rows[0]["AssignedTo"] == "Alice"


def test_build_run_metadata_contains_reproducibility_fields():
    metadata = build_run_metadata(
        run_id="run-1",
        scene=1,
        num_agents=2,
        seed=42,
        model="fake-model",
        provider="openai",
        api_base="https://example.invalid",
        max_steps=120,
        wall_clock_limit=600.0,
        sandbox_profile="workspace",
        coordinator_prompts="/repo/sar_orch/prompts/coordinator",
        worker_prompts="/repo/sar_orch/prompts/worker",
    )
    assert metadata["run_id"] == "run-1"
    assert metadata["env_name"] == "SAR"
    assert metadata["scenario_id"] == "scene_1"
    assert metadata["agent_count"] == 2
    assert metadata["success_criteria"] == "SAR checker subtasks complete"


def test_classify_end_reason_orders_specific_causes():
    assert (
        classify_end_reason(
            finished=True,
            steps=5,
            max_steps=10,
            elapsed_seconds=20.0,
            wall_clock_limit=600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=False,
        )
        == "success"
    )
    assert (
        classify_end_reason(
            finished=False,
            steps=10,
            max_steps=10,
            elapsed_seconds=20.0,
            wall_clock_limit=600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=False,
        )
        == "max_steps_reached"
    )
    assert (
        classify_end_reason(
            finished=False,
            steps=3,
            max_steps=10,
            elapsed_seconds=601.0,
            wall_clock_limit=600.0,
            a2a_done=False,
            a2a_error=False,
            coordinator_error=False,
        )
        == "wall_clock_timeout"
    )


def test_logger_accepts_correlation_fields_for_interactions(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_agent_interaction(
        step=3,
        agent="Alice",
        tool_name="navigate_to",
        tool_args='{"target":"ReservoirUtah"}',
        action="NavigateTo(ReservoirUtah)",
        observation="Arrived",
        run_id="run-1",
        correlation_id="Alice-tool-1",
        event_type="tool_result",
        tool_latency_ms=12.5,
        error_type="",
    )
    logger.log_router_interaction(
        step=3,
        subtask="Go to reservoir",
        assigned_to="Alice",
        run_id="run-1",
        correlation_id="dispatch-1",
        worker_task_id="dispatch-1",
        event_type="dispatch_task",
    )
    logger.log_token_usage(
        step=3,
        agent="Alice",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        run_id="run-1",
        llm_latency_ms=20.0,
        model="fake-model",
        prompt_version="baseline",
    )
    logger.close()

    assert (
        read_csv(tmp_path / "agent_interactions.csv")[0]["CorrelationID"]
        == "Alice-tool-1"
    )
    assert (
        read_csv(tmp_path / "router_interactions.csv")[0]["WorkerTaskID"]
        == "dispatch-1"
    )
    assert read_csv(tmp_path / "token_usage.csv")[0]["LLMLatencyMs"] == "20.0"


def test_logger_coordinator_state_has_all_columns(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_coordinator_state(
        step=4,
        state_summary='{"coverage": 0.5, "transport_rate": 0.3}',
        run_id="run-2",
        correlation_id="coord-state-4",
    )
    logger.close()

    rows = read_csv(tmp_path / "agent_interactions.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["Step"] == "4"
    assert row["Agent"] == "Coordinator"
    assert row["ToolName"] == "coordinator_state"
    assert row["EventType"] == "coordinator_state"
    assert row["RunID"] == "run-2"
    assert row["CorrelationID"] == "coord-state-4"
    assert row["ToolLatencyMs"] == "0.0"
    assert row["ErrorType"] == ""


def test_logger_set_run_context_defaults_run_id(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(
        run_id="ctx-run-1", model="ctx-model", prompt_version="ctx-pv"
    )
    logger.log_step(
        step_num=1,
        actions=["NoOp()"],
        successes=[True],
        observations=["obs"],
        coverage=0.5,
        transport_rate=0.5,
        finished=False,
        run_id="",
    )
    logger.log_agent_interaction(
        step=1,
        agent="Alice",
        tool_name="navigate_to",
        tool_args="{}",
        run_id="",
    )
    logger.log_coordinator_state(
        step=1,
        state_summary="{}",
        run_id="",
    )
    logger.log_router_interaction(
        step=1,
        subtask="test",
        assigned_to="Alice",
        run_id="",
    )
    logger.log_token_usage(
        step=1,
        agent="Alice",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        run_id="",
    )
    logger.close()

    traj = read_csv(tmp_path / "trajectory.csv")
    assert traj[0]["RunID"] == "ctx-run-1"
    interactions = read_csv(tmp_path / "agent_interactions.csv")
    run_ids = {r["RunID"] for r in interactions}
    assert run_ids == {"ctx-run-1"}
    router = read_csv(tmp_path / "router_interactions.csv")
    assert router[0]["RunID"] == "ctx-run-1"
    token = read_csv(tmp_path / "token_usage.csv")
    assert token[0]["RunID"] == "ctx-run-1"
    assert token[0]["Model"] == "ctx-model"
    assert token[0]["PromptVersion"] == "ctx-pv"


def test_logger_set_run_context_explicit_overrides_default(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(run_id="default-run")
    logger.log_step(
        step_num=1,
        actions=["NoOp()"],
        successes=[True],
        observations=["obs"],
        coverage=0.5,
        transport_rate=0.5,
        finished=False,
        run_id="explicit-run",
    )
    logger.close()
    traj = read_csv(tmp_path / "trajectory.csv")
    assert traj[0]["RunID"] == "explicit-run"


def test_logger_set_end_reason_backfills_all_rows(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    for i in range(3):
        logger.log_step(
            step_num=i + 1,
            actions=["NoOp()"],
            successes=[True],
            observations=["obs"],
            coverage=0.1 * (i + 1),
            transport_rate=0.1 * (i + 1),
            finished=False,
        )
    rows_before = read_csv(tmp_path / "trajectory.csv")
    assert rows_before[0]["EndReason"] == ""
    assert rows_before[2]["EndReason"] == ""

    logger.set_end_reason("max_steps_reached")
    logger.close()

    rows_after = read_csv(tmp_path / "trajectory.csv")
    assert len(rows_after) == 3
    for r in rows_after:
        assert r["EndReason"] == "max_steps_reached"


def test_logger_set_end_reason_preserves_existing_data(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(run_id="end-reason-test")
    logger.log_step(
        step_num=1,
        actions=["NavigateTo(A)"],
        successes=[True],
        observations=["Arrived"],
        coverage=0.5,
        transport_rate=0.3,
        finished=False,
        wall_time_since_start=1.0,
    )
    logger.log_step(
        step_num=2,
        actions=["NoOp()"],
        successes=[True],
        observations=["Done"],
        coverage=0.8,
        transport_rate=0.6,
        finished=True,
        wall_time_since_start=2.0,
    )
    logger.set_end_reason("success")
    logger.close()

    rows = read_csv(tmp_path / "trajectory.csv")
    assert rows[0]["Step"] == "1"
    assert rows[0]["Actions"] == "['NavigateTo(A)']"
    assert rows[0]["Coverage"] == "0.5"
    assert rows[0]["EndReason"] == "success"
    assert rows[1]["Step"] == "2"
    assert rows[1]["Actions"] == "['NoOp()']"
    assert rows[1]["Finished"] == "True"
    assert rows[1]["EndReason"] == "success"


def test_code_commit_in_build_run_metadata():
    metadata = build_run_metadata(
        run_id="run-1",
        scene=1,
        num_agents=2,
        seed=42,
        model="fake",
        provider="openai",
        api_base="http://fake",
        max_steps=100,
        wall_clock_limit=600.0,
        sandbox_profile="off",
        coordinator_prompts="/prompts/c",
        worker_prompts="/prompts/w",
    )
    assert "code_commit" in metadata
    assert isinstance(metadata["code_commit"], str)


def test_code_commit_explicit_value():
    metadata = build_run_metadata(
        run_id="run-1",
        scene=1,
        num_agents=2,
        seed=42,
        model="fake",
        provider="openai",
        api_base="http://fake",
        max_steps=100,
        wall_clock_limit=600.0,
        sandbox_profile="off",
        coordinator_prompts="/prompts/c",
        worker_prompts="/prompts/w",
        code_commit="abc1234",
    )
    assert metadata["code_commit"] == "abc1234"


def test_classify_failure_maps_stopped_before_success_to_framework():
    assert classify_failure("stopped_before_success", finished=False) == "framework"


def test_logger_event_uses_run_id_from_context(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(run_id="run-ctx")
    logger.log_event("dispatch", payload={"worker": "Alice"})
    logger.close()

    event_line = (tmp_path / "events.ndjson").read_text(encoding="utf-8").strip()
    event = json.loads(event_line)
    assert event["run_id"] == "run-ctx"


def test_logger_event_explicit_run_id_overrides_context(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.set_run_context(run_id="default-run")
    logger.log_event("dispatch", payload={}, run_id="override")
    logger.close()

    event_line = (tmp_path / "events.ndjson").read_text(encoding="utf-8").strip()
    event = json.loads(event_line)
    assert event["run_id"] == "override"


def test_metadata_records_state_mode(tmp_path):
    logger = ExperimentLogger(experiment_name="test", log_dir=str(tmp_path))
    logger.write_metadata({"state_mode": "semantic"})

    metadata = json.loads((tmp_path / "metadata.json").read_text())

    assert metadata["state_mode"] == "semantic"
