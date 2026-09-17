"""Tests for ``sar_orch.eval.run_analysis_report`` (Reef-C1 report generator).

Synthetic mini runs cover the pinned contracts of the card:
- full schema: run / metrics / stability / subtasks / tool_errors /
  failure_signatures, values cross-checked against the fixture rows;
- degradation, not failure: missing artifacts and missing columns yield
  ``null`` + notes, never a crash or a silent 0 fill;
- the evaluator-private truth dir is never read (sentinel stays untouched);
- determinism: same inputs -> byte-identical report (no timestamps/randomness);
- Python-literal CSV cells (``['idle_heartbeat', '']``) parse via
  ``ast.literal_eval`` — the real ``trajectory.csv`` format;
- signature semantics: cancel_churn window join via
  ``coordinator/events_dsp_*.ndjson`` (duplicate cancels deduped, unmatched
  cancels flagged), cancel_storm step threshold, timeout_burst, idle_tail
  (trailing real-action-free steps, finished runs excluded) and
  agent_never_active.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sar_orch.eval.run_analysis_report import (
    DEFAULT_OUTPUT,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    build_report,
    main,
    write_artifact,
)

TRAJECTORY_HEADERS = [
    "Step",
    "Actions",
    "Successes",
    "Observations",
    "Coverage",
    "TransportRate",
    "Finished",
    "MapRecall",
    "Freshness",
    "TimeoutAgents",
    "NoOpSource",
    "RunID",
    "MaxSteps",
    "RemainingSteps",
    "WallTimeSinceStart",
    "StepDurationMs",
    "ErrorTypes",
    "CompletedSubtasksDelta",
    "EndReason",
]

SUBTASK_HEADERS = [
    "RunID",
    "Step",
    "SubtaskID",
    "Status",
    "AssignedTo",
    "Subtask",
    "CreatedAt",
    "UpdatedAt",
    "FailureClass",
    "Details",
]

INTERACTION_HEADERS = [
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "LLMInput",
    "LLMInputChars",
    "LLMOutput",
    "Thinking",
    "RunID",
    "CorrelationID",
    "EventType",
    "ToolLatencyMs",
    "Success",
    "ErrorType",
]

RUN_ID = "sar-scene3-agents4-seed42-c1test"


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in records)


def _trajectory_rows() -> list[dict]:
    """6 steps x 4 agents; David never performs a real action."""
    return [
        {
            "Step": 1,
            "Actions": ["Explore()", "Explore()", "NoOp()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.1,
            "TransportRate": 0.0,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 0.0,
            "TimeoutAgents": [],
            "NoOpSource": ["", "", "idle_heartbeat", "idle_heartbeat"],
            "WallTimeSinceStart": 10.0,
            "StepDurationMs": 10.0,
            "EndReason": "max_steps_reached",
        },
        {
            "Step": 2,
            "Actions": ["Explore()", "Explore()", "Explore()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.3,
            "TransportRate": 0.2,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 2.0,
            "TimeoutAgents": [],
            "NoOpSource": ["", "", "", "idle_heartbeat"],
            "WallTimeSinceStart": 20.0,
            "StepDurationMs": 20.0,
            "EndReason": "max_steps_reached",
        },
        {
            "Step": 3,
            "Actions": ["NoOp()", "NoOp()", "NoOp()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.3,
            "TransportRate": 0.4,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 4.0,
            "TimeoutAgents": [0, 1],
            "NoOpSource": [
                "timeout_injected",
                "timeout_injected",
                "idle_heartbeat",
                "idle_heartbeat",
            ],
            "WallTimeSinceStart": 30.0,
            "StepDurationMs": 30.0,
            "EndReason": "max_steps_reached",
        },
        {
            "Step": 4,
            "Actions": ["UseSupply(Fire1, Water)", "Explore()", "Explore()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.6,
            "TransportRate": 0.6,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 6.0,
            "TimeoutAgents": [],
            "NoOpSource": ["", "", "", "llm"],
            "WallTimeSinceStart": 40.0,
            "StepDurationMs": 40.0,
            "EndReason": "max_steps_reached",
        },
        {
            "Step": 5,
            "Actions": ["NoOp()", "NoOp()", "NoOp()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.6,
            "TransportRate": 0.7,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 8.0,
            "TimeoutAgents": [],
            "NoOpSource": ["llm", "idle_heartbeat", "idle_heartbeat", "idle_heartbeat"],
            "WallTimeSinceStart": 50.0,
            "StepDurationMs": 50.0,
            "EndReason": "max_steps_reached",
        },
        {
            "Step": 6,
            "Actions": ["NoOp()", "NoOp()", "NoOp()", "NoOp()"],
            "Successes": [True, True, True, True],
            "Observations": ["obs"] * 4,
            "Coverage": 0.7,
            "TransportRate": 0.8,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 10.0,
            "TimeoutAgents": [],
            "NoOpSource": ["llm", "idle_heartbeat", "idle_heartbeat", "llm"],
            "WallTimeSinceStart": 60.0,
            "StepDurationMs": 60.0,
            "EndReason": "max_steps_reached",
        },
    ]


def _subtask_rows() -> list[dict]:
    def row(
        step: int, subtask_id: str, status: str, who: str, failure: str = ""
    ) -> dict:
        return {
            "RunID": RUN_ID,
            "Step": step,
            "SubtaskID": subtask_id,
            "Status": status,
            "AssignedTo": who,
            "Subtask": f"{status} {subtask_id}",
            "CreatedAt": "",
            "UpdatedAt": "",
            "FailureClass": failure,
            "Details": "",
        }

    return [
        row(0, "dispatch-1", "assigned", "Alice"),
        row(0, "dispatch-2", "assigned", "Bob"),
        row(3, "dsp_a", "canceled", "Worker"),
        row(3, "dsp_b", "canceled", "Worker"),
        row(4, "dsp_c", "completed", "Worker"),
        row(5, "dsp_d", "failed", "Worker", "budget"),
    ]


def _interaction_rows() -> list[dict]:
    def row(tool: str, success: str, error: str) -> dict:
        return {
            "Step": 1,
            "Agent": "Alice",
            "ToolName": tool,
            "ToolArgs": "{}",
            "Action": f"{tool}()",
            "Observation": "obs",
            "LLMInput": "",
            "LLMInputChars": 0,
            "LLMOutput": "",
            "Thinking": "",
            "RunID": RUN_ID,
            "CorrelationID": "",
            "EventType": "tool_result",
            "ToolLatencyMs": 1.0,
            "Success": success,
            "ErrorType": error,
        }

    return [
        row("explore", "True", ""),
        row("get_supply", "False", "tool_timeout"),
        row("get_supply", "False", "tool_timeout"),
        row("carry_person", "False", ""),
        row("use_supply", "False", "fire_not_found"),
    ]


def _event_records() -> list[dict]:
    """Two measurable cancels (one in-window) + one unmatched + one duplicate."""
    return [
        {
            "agent": "Coordinator",
            "event_type": "assign_task",
            "step": 0,
            "ts": 900.0,
            "payload": {"message_type": "assign_task", "related_task_id": "dsp_a"},
        },
        {
            "agent": "Coordinator",
            "event_type": "cancel_task",
            "step": 3,
            "payload": {"related_task_id": "dsp_a"},
            "ts": 1030.0,
        },
        {
            # duplicate cancel of the same task: deduped, earliest ts kept
            "agent": "Coordinator",
            "event_type": "cancel_task",
            "step": 3,
            "payload": {"related_task_id": "dsp_a"},
            "ts": 1031.5,
        },
        {
            "agent": "Coordinator",
            "event_type": "cancel_task",
            "step": 3,
            "payload": {"related_task_id": "dsp_b"},
            "ts": 1200.0,
        },
        {
            # no per-dispatch stream for dsp_x -> unmatched
            "agent": "Coordinator",
            "event_type": "cancel_task",
            "step": 5,
            "payload": {"related_task_id": "dsp_x"},
            "ts": 1210.0,
        },
    ]


def _build_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "20260910_000018_s3_s42_a4"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "metadata.json",
        {
            "scene": 3,
            "agent_count": 4,
            "seed": 42,
            "model": "deepseek-v4-flash",
            "prompt_version": "baseline",
            "code_commit": "deadbeef",
            "run_id": RUN_ID,
            "max_steps": 30,
            "truth_dir": str(tmp_path / "truth" / run_dir.name),
        },
    )
    _write_json(
        run_dir / "run_metrics.json",
        {
            "coverage": 0.7,
            "transport_rate": 0.8,
            "steps": 6,
            "finished": False,
            "elapsed_seconds": 123.4567,
            "end_reason": "max_steps_reached",
            "max_steps": 30,
            "run_id": RUN_ID,
        },
    )
    _write_json(
        run_dir / "eval_metrics.json",
        {
            "l0_task": {"transport_rate": 0.8},
            "l2_planning": {"load_balance_b": 0.512345},
            "l4_cost": {"effective_billed_tokens": 1000.5},
            "meta": {"agent_names": ["Alice", "Bob", "Charlie", "David"]},
        },
    )
    _write_csv(run_dir / "trajectory.csv", TRAJECTORY_HEADERS, _trajectory_rows())
    _write_csv(run_dir / "subtasks.csv", SUBTASK_HEADERS, _subtask_rows())
    _write_csv(
        run_dir / "agent_interactions.csv", INTERACTION_HEADERS, _interaction_rows()
    )
    _write_jsonl(run_dir / "events.ndjson", _event_records())
    for name, stamp in (("dsp_a", 1000.0), ("dsp_b", 1000.0)):
        _write_jsonl(
            run_dir / "coordinator" / f"events_{name}.ndjson",
            [
                {
                    "ts": stamp,
                    "task_id": name,
                    "event_type": "task_created",
                    "state": "DISPATCHING",
                }
            ],
        )
    for name in ("Alice", "Bob", "Charlie", "David"):
        (run_dir / "workers" / name).mkdir(parents=True, exist_ok=True)
    return run_dir


def _signature(payload: dict, kind: str) -> dict:
    for entry in payload["failure_signatures"]:
        if entry["kind"] == kind:
            return entry
    raise AssertionError(f"signature kind {kind!r} missing from report")


# --------------------------------------------------------------------------
# full report
# --------------------------------------------------------------------------


def test_full_report_sections_and_values(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = build_report(run_dir)

    assert payload["schema_version"] == 1
    assert payload["truth_read"] is False
    assert payload["missing_artifacts"] == []
    assert payload["notes"] == []

    run = payload["run"]
    assert run["run_id"] == RUN_ID
    assert run["scene"] == 3
    assert run["agents"] == 4
    assert run["seed"] == 42
    assert run["model"] == "deepseek-v4-flash"
    assert run["prompt_version"] == "baseline"
    assert run["code_commit"] == "deadbeef"
    assert run["end_reason"] == "max_steps_reached"
    assert run["steps"] == 6
    assert run["max_steps"] == 30
    assert run["finished"] is False
    assert run["wall_clock_s"] == 123.457

    metrics = payload["metrics"]
    assert metrics["transport_rate"] == 0.8
    assert metrics["coverage_final"] == 0.7
    assert metrics["load_balance_b"] == 0.512345
    assert metrics["effective_billed_tokens"] == 1000.5
    assert metrics["notes"] == []

    stability = payload["stability"]
    assert stability["steps_observed"] == 6
    assert stability["agent_slots"] == 24
    # real actions: step1 2 + step2 3 + step4 3 (David never acts)
    assert stability["real_action_slots"] == 8
    assert stability["timeout_agents_slots"] == 2
    assert stability["timeout_steps"] == 1
    assert stability["timeout_source"] == "TimeoutAgents"
    assert stability["timeout_agents_ratio"] == round(2 / 24, 6)
    assert stability["noop_source"] == {
        "llm": 4,
        "idle_heartbeat": 10,
        "timeout_injected": 2,
        "other": 0,
    }

    subtasks = payload["subtasks"]
    assert subtasks["assigned"] == 2
    assert subtasks["canceled"] == 2
    assert subtasks["completed"] == 1
    assert subtasks["failed"] == 1
    assert subtasks["other"] == 0
    assert subtasks["total"] == 6
    assert subtasks["cancel_rate"] == round(2 / 6, 6)

    tool_errors = payload["tool_errors"]
    assert tool_errors["total_calls"] == 5
    assert tool_errors["failed_calls"] == 4
    assert tool_errors["failure_rate"] == 0.8
    assert tool_errors["top_k"] == [
        {"tool": "get_supply", "error": "tool_timeout", "count": 2},
        {"tool": "carry_person", "error": "unclassified", "count": 1},
        {"tool": "use_supply", "error": "fire_not_found", "count": 1},
    ]


def test_failure_signatures_values(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = build_report(run_dir)

    churn = _signature(payload, "cancel_churn")
    # distinct cancels: dsp_a (30s), dsp_b (200s), dsp_x (unmatched)
    assert churn["count"] == 1
    assert churn["evidence"]["cancel_events"] == 3
    assert churn["evidence"]["measurable"] == 2
    assert churn["evidence"]["unmatched"] == 1
    assert churn["evidence"]["in_window"] == 1
    assert churn["evidence"]["in_window_task_ids"] == ["dsp_a"]
    assert churn["evidence"]["delay_s"] == {"min": 30.0, "p50": 115.0, "max": 200.0}
    assert churn["evidence"]["window_s"] == 60.0

    storm = _signature(payload, "cancel_storm")
    assert storm["count"] == 0  # step 3 holds 2 distinct cancels < threshold 3
    assert storm["evidence"]["storm_steps"] == []
    assert storm["evidence"]["max_cancels_in_step"] == 2
    assert storm["evidence"]["target_kinds"] == {"dispatch": 3, "other": 0}

    burst = _signature(payload, "timeout_burst")
    assert burst["count"] == 1
    assert burst["evidence"]["burst_steps"] == [3]
    assert burst["evidence"]["max_timeout_agents_in_step"] == 2

    idle_tail = _signature(payload, "idle_tail")
    # steps 5-6 carry no real action on an unfinished run
    assert idle_tail["count"] == 2
    assert idle_tail["evidence"]["start_step"] == 5
    assert idle_tail["evidence"]["finished"] is False
    assert idle_tail["evidence"]["tail_noop_source"] == {
        "llm": 3,
        "idle_heartbeat": 5,
        "timeout_injected": 0,
    }

    never_active = _signature(payload, "agent_never_active")
    assert never_active["count"] == 1
    assert never_active["evidence"]["agents"] == 4
    assert never_active["evidence"]["never_active_indices"] == [3]
    assert never_active["evidence"]["never_active_names"] == ["David"]
    assert never_active["evidence"]["names_source"] == "workers_dir"


def test_finished_run_has_no_idle_tail(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = build_report(run_dir)
    assert _signature(payload, "idle_tail")["count"] == 2

    run_metrics = json.loads((run_dir / "run_metrics.json").read_text(encoding="utf-8"))
    run_metrics["finished"] = True
    _write_json(run_dir / "run_metrics.json", run_metrics)

    finished_payload = build_report(run_dir)
    idle_tail = _signature(finished_payload, "idle_tail")
    assert idle_tail["count"] == 0
    assert idle_tail["evidence"]["finished"] is True
    assert "auto-no-op" in idle_tail["evidence"]["note"]


# --------------------------------------------------------------------------
# CLI + determinism
# --------------------------------------------------------------------------


def test_main_writes_report_inside_run_dir(tmp_path, capsys):
    run_dir = _build_run(tmp_path)
    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "run_analysis_report: wrote" in out

    report_path = run_dir / DEFAULT_OUTPUT
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["run"]["run_id"] == RUN_ID
    # only the report artifact is added next to the run's own files
    files = sorted(entry.name for entry in run_dir.iterdir() if entry.is_file())
    assert DEFAULT_OUTPUT in files


def test_report_is_byte_identical_across_runs(tmp_path):
    run_dir = _build_run(tmp_path)
    first = write_artifact(run_dir, build_report(run_dir), tmp_path / "first.json")
    second = write_artifact(run_dir, build_report(run_dir), tmp_path / "second.json")
    assert first.read_bytes() == second.read_bytes()
    assert b"timestamp" not in first.read_bytes()


def test_cli_threshold_overrides(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = build_report(
        run_dir,
        churn_window_s=250.0,
        cancel_storm_threshold=2,
        timeout_burst_threshold=2,
        tool_error_top_k=1,
    )
    assert _signature(payload, "cancel_churn")["count"] == 2  # dsp_a + dsp_b
    assert _signature(payload, "cancel_storm")["count"] == 1  # step 3
    assert len(payload["tool_errors"]["top_k"]) == 1

    assert (
        main(["--results-dir", str(run_dir), "--output", str(tmp_path / "o.json")])
        == EXIT_OK
    )
    # non-positive window is an argparse usage error (exit code 2)
    with pytest.raises(SystemExit) as excinfo:
        main(["--results-dir", str(run_dir), "--churn-window-s", "0"])
    assert excinfo.value.code == 2


def test_missing_results_dir_is_invalid_input(tmp_path, capsys):
    assert main(["--results-dir", str(tmp_path / "nope")]) == EXIT_INVALID_INPUT
    assert "results dir not found" in capsys.readouterr().err


def test_dir_without_any_artifact_is_invalid_input(tmp_path, capsys):
    empty = tmp_path / "empty_run"
    empty.mkdir()
    assert main(["--results-dir", str(empty)]) == EXIT_INVALID_INPUT
    assert "no run artifacts found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# degradation, not failure
# --------------------------------------------------------------------------


def test_missing_artifacts_degrade_to_null(tmp_path, capsys):
    run_dir = tmp_path / "bare_run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run_metrics.json",
        {"transport_rate": 0.5, "coverage": 0.25, "steps": 3, "finished": False},
    )
    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    payload = json.loads((run_dir / DEFAULT_OUTPUT).read_text(encoding="utf-8"))

    assert payload["run"]["scene"] is None
    assert payload["run"]["agents"] is None
    assert payload["run"]["end_reason"] is None
    assert payload["run"]["steps"] == 3
    assert payload["metrics"]["transport_rate"] == 0.5
    assert payload["metrics"]["coverage_final"] == 0.25
    assert payload["metrics"]["load_balance_b"] is None
    assert payload["metrics"]["effective_billed_tokens"] is None
    assert payload["metrics"]["notes"]  # notes explain the degradation
    assert payload["stability"]["noop_source"] is None
    assert payload["stability"]["timeout_agents_ratio"] is None
    assert payload["subtasks"]["cancel_rate"] is None
    assert payload["tool_errors"]["top_k"] is None
    for entry in payload["failure_signatures"]:
        assert entry["count"] is None
        assert entry["evidence"]["status"] == "unavailable"
    assert payload["missing_artifacts"] == [
        "agent_interactions.csv",
        "eval_metrics.json",
        "events.ndjson",
        "metadata.json",
        "subtasks.csv",
        "trajectory.csv",
    ]
    assert "run.end_reason: absent" in " ".join(payload["notes"])


def test_legacy_trajectory_without_noop_source_column(tmp_path):
    run_dir = _build_run(tmp_path)
    headers = [column for column in TRAJECTORY_HEADERS if column != "NoOpSource"]
    legacy_rows = [
        {key: value for key, value in row.items() if key != "NoOpSource"}
        for row in _trajectory_rows()
    ]
    _write_csv(run_dir / "trajectory.csv", headers, legacy_rows)

    payload = build_report(run_dir)
    stability = payload["stability"]
    assert stability["noop_source"] is None
    # TimeoutAgents still measures the ratio and the burst signature
    assert stability["timeout_agents_ratio"] == round(2 / 24, 6)
    assert _signature(payload, "timeout_burst")["count"] == 1
    # Actions-based signatures stay available
    assert _signature(payload, "idle_tail")["count"] == 2
    assert _signature(payload, "agent_never_active")["count"] == 1
    assert any("NoOpSource column absent" in note for note in stability["notes"])


def test_tool_errors_without_success_column(tmp_path):
    run_dir = _build_run(tmp_path)
    headers = [column for column in INTERACTION_HEADERS if column != "Success"]
    legacy_rows = [
        {key: value for key, value in row.items() if key != "Success"}
        for row in _interaction_rows()
    ]
    _write_csv(run_dir / "agent_interactions.csv", headers, legacy_rows)

    payload = build_report(run_dir)
    tool_errors = payload["tool_errors"]
    assert tool_errors["top_k"] is None
    assert tool_errors["failed_calls"] is None
    assert any("Success column absent" in note for note in tool_errors["notes"])


def test_metrics_missing_eval_sections_are_noted(tmp_path):
    run_dir = _build_run(tmp_path)
    _write_json(run_dir / "eval_metrics.json", {"meta": {"agent_names": ["A"]}})

    payload = build_report(run_dir)
    metrics = payload["metrics"]
    assert metrics["load_balance_b"] is None
    assert metrics["effective_billed_tokens"] is None
    assert any("l2_planning.load_balance_b absent" in note for note in metrics["notes"])
    assert any(
        "l4_cost.effective_billed_tokens absent" in note for note in metrics["notes"]
    )


def test_idle_tail_without_finished_flag_is_noted(tmp_path):
    run_dir = _build_run(tmp_path)
    run_metrics = json.loads((run_dir / "run_metrics.json").read_text(encoding="utf-8"))
    del run_metrics["finished"]
    _write_json(run_dir / "run_metrics.json", run_metrics)

    payload = build_report(run_dir)
    idle_tail = _signature(payload, "idle_tail")
    assert idle_tail["count"] == 2
    assert idle_tail["evidence"]["finished"] is None
    assert "finished absent" in idle_tail["evidence"]["note"]


def test_cancel_churn_unmeasurable_without_dispatch_streams(tmp_path):
    run_dir = _build_run(tmp_path)
    for path in (run_dir / "coordinator").glob("events_dsp_*.ndjson"):
        path.unlink()

    payload = build_report(run_dir)
    churn = _signature(payload, "cancel_churn")
    assert churn["count"] is None
    assert churn["evidence"]["unmatched"] == 3
    assert churn["evidence"]["measurable"] == 0
    assert (
        "no cancel event matched a dispatch creation timestamp"
        in churn["evidence"]["note"]
    )


def test_no_cancel_events_means_zero_churn(tmp_path):
    run_dir = _build_run(tmp_path)
    records = [
        record for record in _event_records() if record["event_type"] != "cancel_task"
    ]
    _write_jsonl(run_dir / "events.ndjson", records)

    payload = build_report(run_dir)
    churn = _signature(payload, "cancel_churn")
    assert churn["count"] == 0
    assert churn["evidence"]["cancel_events"] == 0
    assert "no cancel_task event recorded" in churn["evidence"]["note"]
    assert _signature(payload, "cancel_storm")["count"] == 0


def test_unknown_subtask_status_is_counted_as_other(tmp_path):
    run_dir = _build_run(tmp_path)
    rows = _subtask_rows()
    rows.append(
        {
            "RunID": RUN_ID,
            "Step": 5,
            "SubtaskID": "dsp_e",
            "Status": "running",
            "AssignedTo": "Worker",
            "Subtask": "running dsp_e",
            "CreatedAt": "",
            "UpdatedAt": "",
            "FailureClass": "",
            "Details": "",
        }
    )
    _write_csv(run_dir / "subtasks.csv", SUBTASK_HEADERS, rows)

    payload = build_report(run_dir)
    subtasks = payload["subtasks"]
    assert subtasks["other"] == 1
    assert subtasks["total"] == 7
    assert subtasks["cancel_rate"] == round(2 / 7, 6)
    assert any("unknown status value" in note for note in subtasks["notes"])


# --------------------------------------------------------------------------
# truth isolation
# --------------------------------------------------------------------------


def test_truth_dir_is_never_read(tmp_path):
    run_dir = _build_run(tmp_path)
    truth_dir = tmp_path / "truth" / run_dir.name
    sentinel = "TRUTH-SENTINEL-3f4618db"
    _write_jsonl(
        truth_dir / "truth_trace.jsonl",
        [{"position": [1, 2, 3], "secret": sentinel}],
    )
    _write_json(
        truth_dir / "truth_manifest.json",
        {"run_id": RUN_ID, "secret": sentinel},
    )
    before = {
        path.name: path.read_bytes()
        for path in sorted(truth_dir.iterdir())
        if path.is_file()
    }

    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    report_text = (run_dir / DEFAULT_OUTPUT).read_text(encoding="utf-8")

    assert sentinel not in report_text
    assert "truth_trace" not in report_text
    assert "truth_manifest" not in report_text
    after = {
        path.name: path.read_bytes()
        for path in sorted(truth_dir.iterdir())
        if path.is_file()
    }
    assert after == before
    # the run dir gained exactly the report artifact
    assert sorted(
        entry.name for entry in run_dir.iterdir() if entry.is_file()
    ) == sorted(
        [
            "agent_interactions.csv",
            "eval_metrics.json",
            "events.ndjson",
            "metadata.json",
            DEFAULT_OUTPUT,
            "run_metrics.json",
            "subtasks.csv",
            "trajectory.csv",
        ]
    )


def test_nonexistent_truth_dir_is_not_touched(tmp_path):
    run_dir = _build_run(tmp_path)
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    metadata["truth_dir"] = str(tmp_path / "does" / "not" / "exist")
    _write_json(run_dir / "metadata.json", metadata)

    payload = build_report(run_dir)
    assert payload["truth_read"] is False
    assert not (tmp_path / "does").exists()
