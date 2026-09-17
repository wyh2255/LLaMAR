"""Tests for ``sar_orch.eval.run_eval_metrics`` (sar-metrics v1 P0 collector).

Synthetic mini runs cover the pinned contracts of the cards:
- Load Balance B = ``min(s_i)/(max(s_i)+1e-4)`` over *successful real* actions
  (NoOp of any origin never counts as labour), ``null`` when all ``s_i`` = 0;
- Progress/Coverage AUC = per-step curve mean (area / steps);
- cache: hit rate ``ΣH/ΣP``, miss ratio ``Σ(P−H)/ΣP`` (the ``CacheMissTokens``
  column is never trusted), effective billed ``= Σ(P−H)+ΣC+0.1×ΣH``;
- legacy runs missing ``NoOpSource`` / ``Status`` / ``Success`` columns degrade
  to ``null`` + ``missing_columns`` instead of raising ``KeyError``;
- the G-layer gate judgement (six red-line items + total ``pass|fail``), including
  the ``unknown.ndjson`` rule (benign ``raw_request`` only = pass, any task
  event = fail) and the truth manifest run_id cross-check.

P1-① additions (same file):
- ``map_recall_v2``: parent-object recall of the canonical ``spatial.jsonl``
  projection (``_Region_`` cells folded) against the truth parent set
  (``parent_fire`` inverse mapping), with the ``scene_config.json`` denominator
  fallback + truth-vs-scene_config consistency warning;
- ``freshness_canonical`` (max ``env_step`` based) vs the renamed
  ``freshness_legacy_mean`` (trajectory column) kept side by side;
- ``map_agent_discovery`` over the mixed ``semantic_map.jsonl`` body
  (exporter-rebuilt ``object`` rows + legacy teardown ``observation`` residue);
- the deprecated trajectory ``MapRecall`` column is always flagged and the
  ``deprecated_metrics`` annotation points at its successor.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sar_orch.eval.run_eval_metrics import (
    DEFAULT_OUTPUT,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    evaluate,
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

TOKEN_HEADERS = [
    "Step",
    "Agent",
    "PromptTokens",
    "CompletionTokens",
    "TotalTokens",
    "CacheHitTokens",
    "CacheMissTokens",
    "RunID",
    "LLMLatencyMs",
    "Model",
    "PromptVersion",
    "Status",
]

AGENT_HEADERS = [
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

ROUTER_HEADERS = [
    "Step",
    "Subtask",
    "AssignedTo",
    "RunID",
    "CorrelationID",
    "WorkerTaskID",
    "EventType",
    "Success",
    "ErrorType",
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
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _trajectory_rows(*, with_noop_source: bool = True) -> list[dict]:
    rows = [
        {
            "Step": 1,
            "Actions": ["Explore()", "NoOp()"],
            "Successes": [True, True],
            "Observations": ["obs", "obs"],
            "Coverage": 0.0,
            "TransportRate": 0.0,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 0.0,
            "TimeoutAgents": [],
            "NoOpSource": ["", "idle_heartbeat"],
            "StepDurationMs": 10.0,
            "CompletedSubtasksDelta": [],
        },
        {
            "Step": 2,
            "Actions": ["UseSupply(Fire1, Water)", "Explore()"],
            "Successes": [True, True],
            "Observations": ["obs", "obs"],
            "Coverage": 0.5,
            "TransportRate": 0.5,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 2.0,
            "TimeoutAgents": [],
            "NoOpSource": ["", ""],
            "StepDurationMs": 20.0,
            "CompletedSubtasksDelta": ["UseSupply(Fire1, Water)"],
        },
        {
            "Step": 3,
            "Actions": ["NoOp()", "NoOp()"],
            "Successes": [True, True],
            "Observations": ["obs", "obs"],
            "Coverage": 0.5,
            "TransportRate": 0.5,
            "Finished": False,
            "MapRecall": 0.0,
            "Freshness": 4.0,
            "TimeoutAgents": [1],
            "NoOpSource": ["llm", "timeout_injected"],
            "StepDurationMs": 30.0,
            "CompletedSubtasksDelta": [],
        },
    ]
    for row in rows:
        row.update(
            {
                "RunID": "run-unit",
                "MaxSteps": 3,
                "RemainingSteps": 0,
                "WallTimeSinceStart": 60.0,
                "ErrorTypes": ["", ""],
                "EndReason": "max_steps_reached",
            }
        )
        if not with_noop_source:
            row.pop("NoOpSource")
    return rows


def _token_rows(*, with_status: bool = True) -> list[dict]:
    rows = [
        {
            "Step": 0,
            "Agent": "Coordinator",
            "PromptTokens": 1000,
            "CompletionTokens": 100,
            "TotalTokens": 1100,
            "CacheHitTokens": 200,
            "CacheMissTokens": 0,
            "RunID": "run-unit",
            "LLMLatencyMs": 0.0,
            "Model": "unit-model",
            "PromptVersion": "baseline",
            "Status": "ok",
        },
        {
            "Step": 1,
            "Agent": "Alice",
            "PromptTokens": 1000,
            "CompletionTokens": 50,
            "TotalTokens": 1050,
            "CacheHitTokens": 300,
            "CacheMissTokens": 0,
            "RunID": "run-unit",
            "LLMLatencyMs": 0.0,
            "Model": "unit-model",
            "PromptVersion": "baseline",
            "Status": "error",
        },
    ]
    if not with_status:
        for row in rows:
            row.pop("Status")
    return rows


def _agent_rows(*, with_success: bool = True) -> list[dict]:
    rows = [
        {
            "Step": 1,
            "Agent": "Alice",
            "ToolName": "explore",
            "ToolArgs": "{}",
            "Action": "Explore()",
            "Observation": "obs",
            "LLMInput": "",
            "LLMInputChars": 10,
            "LLMOutput": "",
            "Thinking": "",
            "RunID": "run-unit",
            "CorrelationID": "c1",
            "EventType": "tool_result",
            "ToolLatencyMs": 12.5,
            "Success": True,
            "ErrorType": "",
        },
        {
            "Step": 1,
            "Agent": "Bob",
            "ToolName": "report_observation",
            "ToolArgs": "{}",
            "Action": "ReportObservation()",
            "Observation": "obs",
            "LLMInput": "",
            "LLMInputChars": 10,
            "LLMOutput": "",
            "Thinking": "",
            "RunID": "run-unit",
            "CorrelationID": "c2",
            "EventType": "tool_result",
            "ToolLatencyMs": 25.0,
            "Success": True,
            "ErrorType": "",
        },
    ]
    if not with_success:
        for row in rows:
            row.pop("Success")
    return rows


def _build_run(tmp_path: Path, *, legacy: bool = False, **overrides) -> Path:
    """Create a minimal synthetic run directory (modern by default).

    ``legacy=True`` mimics a 2026-07 style run: the trajectory table has no
    ``NoOpSource`` column, ``token_usage`` has no ``Status``, the agent table has
    no ``Success``, ``metadata.json`` has no ``truth_dir`` and the newer memory
    evaluator artifacts are simply absent.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    trajectory_headers = list(TRAJECTORY_HEADERS)
    token_headers = list(TOKEN_HEADERS)
    agent_headers = list(AGENT_HEADERS)
    if legacy:
        trajectory_headers.remove("NoOpSource")
        token_headers.remove("Status")
        agent_headers.remove("Success")

    _write_csv(
        run_dir / "trajectory.csv",
        trajectory_headers,
        _trajectory_rows(with_noop_source=not legacy),
    )
    _write_csv(run_dir / "token_usage.csv", token_headers, _token_rows(with_status=not legacy))
    _write_csv(
        run_dir / "agent_interactions.csv",
        agent_headers,
        _agent_rows(with_success=not legacy),
    )
    _write_csv(
        run_dir / "router_interactions.csv",
        ROUTER_HEADERS,
        [
            {
                "Step": 0,
                "Subtask": "assign",
                "AssignedTo": "Alice",
                "RunID": "run-unit",
                "CorrelationID": "",
                "WorkerTaskID": "dsp-1",
                "EventType": "assign_task",
                "Success": True,
                "ErrorType": "",
            },
            {
                "Step": 0,
                "Subtask": "assign",
                "AssignedTo": "Bob",
                "RunID": "run-unit",
                "CorrelationID": "",
                "WorkerTaskID": "dsp-2",
                "EventType": "assign_task",
                "Success": True,
                "ErrorType": "",
            },
            {
                "Step": 2,
                "Subtask": "cancel",
                "AssignedTo": "Bob",
                "RunID": "run-unit",
                "CorrelationID": "",
                "WorkerTaskID": "dsp-2",
                "EventType": "cancel_task",
                "Success": True,
                "ErrorType": "",
            },
            {
                "Step": 1,
                "Subtask": "plan",
                "AssignedTo": "Coordinator",
                "RunID": "run-unit",
                "CorrelationID": "",
                "WorkerTaskID": "",
                "EventType": "update_plan",
                "Success": True,
                "ErrorType": "",
            },
        ],
    )
    _write_csv(
        run_dir / "subtasks.csv",
        SUBTASK_HEADERS,
        [
            {
                "RunID": "run-unit",
                "Step": 0,
                "SubtaskID": "dispatch-1",
                "Status": "assigned",
                "AssignedTo": "Alice",
                "Subtask": "explore",
                "CreatedAt": "",
                "UpdatedAt": "",
                "FailureClass": "",
                "Details": "",
            },
            {
                "RunID": "run-unit",
                "Step": 0,
                "SubtaskID": "dispatch-2",
                "Status": "assigned",
                "AssignedTo": "Bob",
                "Subtask": "explore",
                "CreatedAt": "",
                "UpdatedAt": "",
                "FailureClass": "",
                "Details": "",
            },
        ],
    )

    run_metrics = {
        "coverage": 0.5,
        "transport_rate": 0.5,
        "steps": 3,
        "finished": False,
        "elapsed_seconds": 60.0,
        "end_reason": "max_steps_reached",
        "run_id": "run-unit",
        "max_steps": 3,
        "memory_terminal": {"acceptance_gate": "pass"},
        "truth_dir": str(tmp_path / "truth"),
    }
    if legacy:
        run_metrics.pop("memory_terminal")
        run_metrics.pop("truth_dir")
    run_metrics.update(overrides.pop("run_metrics", {}))
    _write_json(run_dir / "run_metrics.json", run_metrics)

    metadata = {
        "run_id": "run-unit",
        "scene": 3,
        "agent_count": 2,
        "seed": 42,
        "model": "unit-model",
        "provider": "openai",
        "code_commit": "deadbee",
        "max_steps": 3,
    }
    metadata.update(overrides.pop("metadata", {}))
    truth_dir = None
    if not legacy:
        truth_dir = tmp_path / "truth"
        _write_json(truth_dir / "truth_manifest.json", {"run_id": "run-unit"})
        _write_jsonl(truth_dir / "truth_trace.jsonl", [{"step": 1}])
        metadata["truth_dir"] = str(truth_dir)
    metadata.update(overrides.pop("metadata_extra", {}))
    _write_json(run_dir / "metadata.json", metadata)

    # agent tracing surface used for the agent-name resolution
    for name in ("Alice", "Bob"):
        (run_dir / "workers" / name / name / "context").mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        run_dir / "workers" / "Alice" / "Alice" / "context" / "prune_events.ndjson",
        [{"step": 1}, {"step": 2}],
    )
    _write_jsonl(
        run_dir / "workers" / "Bob" / "Bob" / "context" / "prune_events.ndjson", [{"step": 1}]
    )

    _write_jsonl(run_dir / "coordinator" / "unknown.ndjson", [{"event": "raw_request"}])
    _write_jsonl(
        run_dir / "coordinator" / "events_dsp-1.ndjson",
        [
            {"event_type": "observation_report"},
            {"event_type": "observation_report"},
            {"event_type": "help_request"},
        ],
    )
    _write_jsonl(run_dir / "semantic_map.jsonl", [{"event_type": "observation_ingested"}])

    if legacy:
        return run_dir

    _write_json(
        run_dir / "memory_acceptance.json",
        {
            "framework_error_counts": {
                "worker_busy": 0,
                "task_not_routable_yet": 0,
                "unknown_task_id": 0,
            }
        },
    )
    _write_json(
        run_dir / "long_term_memory_quality.json",
        {"metrics": {"forbidden_truth_violation_count": 0}},
    )
    _write_json(
        run_dir / "memory_projection_quality.json",
        {
            "metric_status": "measured",
            "worker_report_quality": {"precision": 1.0, "recall": 1.0, "false_claim_count": 0},
            "memory_integration_quality": {"precision": 0.5, "evidence_traceability_rate": 1.0},
        },
    )

    return run_dir


@pytest.fixture
def run_dir(tmp_path) -> Path:
    return _build_run(tmp_path)


# --------------------------------------------------------------------------
# Load Balance B
# --------------------------------------------------------------------------


def test_load_balance_uses_successful_real_actions_only(run_dir):
    payload = evaluate(run_dir)
    l2 = payload["l2_planning"]

    # Alice: Explore (step1) + UseSupply (step2) = 2 real successes;
    # Bob: Explore (step2) = 1; every NoOp success (idle/llm/timeout) excluded.
    assert l2["load_balance_detail"]["successful_real_actions_per_agent"] == {
        "Alice": 2,
        "Bob": 1,
    }
    assert l2["load_balance_detail"]["min"] == 1
    assert l2["load_balance_detail"]["max"] == 2
    assert l2["load_balance_b"] == pytest.approx(1 / (2 + 1e-4))
    # action-level bookkeeping keeps the raw totals for cross-checks
    per_agent = payload["l3_execution"]["per_agent_action_success"]
    assert per_agent["Alice"]["successful_actions"] == 3
    assert per_agent["Bob"]["successful_actions"] == 3
    assert payload["l3_execution"]["action_success_rate"] == 1.0


def test_load_balance_null_when_no_real_labour(tmp_path):
    rows = _trajectory_rows()
    for row in rows:
        row["Successes"] = [True, True]
    for row in rows:
        row["Actions"] = ["NoOp()", "NoOp()"]
    run_dir = _build_run(tmp_path)
    _write_csv(run_dir / "trajectory.csv", TRAJECTORY_HEADERS, rows)

    payload = evaluate(run_dir)
    assert payload["l2_planning"]["load_balance_b"] is None
    assert payload["l2_planning"]["load_balance_detail"]["max"] == 0
    assert payload["metric_status"]["l2_planning.load_balance_b"] == "not_applicable"
    assert payload["l3_execution"]["noop_decomposition"]["real_action"] == 0
    assert payload["l3_execution"]["noop_decomposition"]["llm"] == 1
    assert payload["l3_execution"]["noop_decomposition"]["idle_heartbeat"] == 1
    assert payload["l3_execution"]["noop_decomposition"]["timeout_injected"] == 1


# --------------------------------------------------------------------------
# AUC / L0
# --------------------------------------------------------------------------


def test_progress_and_coverage_auc_are_curve_means(run_dir):
    payload = evaluate(run_dir)
    l0 = payload["l0_task"]
    assert l0["progress_auc"] == pytest.approx((0.0 + 0.5 + 0.5) / 3)
    assert l0["coverage_auc"] == pytest.approx((0.0 + 0.5 + 0.5) / 3)
    assert l0["coverage"] == 0.5
    assert l0["transport_rate"] == 0.5
    assert l0["steps"] == 3
    assert l0["steps_to_success"] is None  # not finished
    assert l0["failure_class"] == "budget"
    assert l0["completed_subtasks"] == 1
    assert l0["subtask_total"] == 2


def test_finished_run_reports_steps_to_success_and_tokens_per_success(tmp_path):
    run_dir = _build_run(
        tmp_path,
        run_metrics={
            "finished": True,
            "end_reason": "success",
            "coverage": 0.5,
            "transport_rate": 0.5,
        },
    )
    payload = evaluate(run_dir)
    assert payload["l0_task"]["steps_to_success"] == 3
    assert payload["l0_task"]["failure_class"] == "success"
    # total tokens 2150 over 1 completed subtask
    assert payload["l4_cost"]["tokens_per_success"] == pytest.approx(2150.0)
    assert payload["metric_status"]["l4_cost.tokens_per_success"] == "measured"


# --------------------------------------------------------------------------
# cache / cost
# --------------------------------------------------------------------------


def test_cache_metrics_ignore_cache_miss_column(run_dir):
    payload = evaluate(run_dir)
    cost = payload["l4_cost"]
    # ΣP=2000, ΣH=500, ΣC=150 -> hit 0.25 / miss 0.75 (CacheMissTokens column says 0)
    assert cost["prompt_tokens"] == 2000
    assert cost["cache_hit_tokens"] == 500
    assert cost["cache_hit_rate"] == pytest.approx(0.25)
    assert cost["cache_miss_tokens_derived"] == 1500
    assert cost["cache_miss_ratio"] == pytest.approx(0.75)
    assert cost["effective_billed_tokens"] == pytest.approx(1500 + 150 + 0.1 * 500)
    assert cost["llm_request_count"] == 2
    assert cost["llm_error_count"] == 1
    assert cost["llm_error_rate"] == pytest.approx(0.5)
    assert cost["tokens_per_step"] == pytest.approx(2150 / 3)


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------


def test_gate_passes_on_clean_run(run_dir):
    gates = evaluate(run_dir)["gates"]
    assert set(gates) >= {
        "acceptance_gate",
        "forbidden_truth_violation_count",
        "framework_error_counts",
        "end_reason",
        "unknown_ndjson",
        "truth_manifest_run_id",
        "gate",
        "detail",
    }
    for item in (
        "acceptance_gate",
        "forbidden_truth_violation_count",
        "framework_error_counts",
        "end_reason",
        "unknown_ndjson",
        "truth_manifest_run_id",
    ):
        assert gates[item] == "pass", item
    assert gates["gate"] == "pass"


@pytest.mark.parametrize(
    "overrides, expected_item",
    [
        ({"run_metrics": {"memory_terminal": {"acceptance_gate": "measured"}}}, "acceptance_gate"),
        ({"run_metrics": {"end_reason": "framework_error"}}, "end_reason"),
        # t_7c303cb1: a run aborted because every worker thread died is a
        # framework failure and must fail the gate exactly like framework_error.
        ({"run_metrics": {"end_reason": "workers_dead"}}, "end_reason"),
        ({"lt_violations": 2}, "forbidden_truth_violation_count"),
        ({"framework_errors": {"worker_busy": 3}}, "framework_error_counts"),
        ({"truth_run_id": "other-run"}, "truth_manifest_run_id"),
    ],
)
def test_gate_fail_variants(tmp_path, overrides, expected_item):
    run_dir = _build_run(tmp_path)
    if "run_metrics" in overrides:
        metrics = json.loads((run_dir / "run_metrics.json").read_text(encoding="utf-8"))
        metrics.update(overrides["run_metrics"])
        _write_json(run_dir / "run_metrics.json", metrics)
    if "lt_violations" in overrides:
        _write_json(
            run_dir / "long_term_memory_quality.json",
            {"metrics": {"forbidden_truth_violation_count": overrides["lt_violations"]}},
        )
    if "framework_errors" in overrides:
        _write_json(
            run_dir / "memory_acceptance.json",
            {"framework_error_counts": overrides["framework_errors"]},
        )
    if "truth_run_id" in overrides:
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        truth_dir = Path(metadata["truth_dir"])
        _write_json(truth_dir / "truth_manifest.json", {"run_id": overrides["truth_run_id"]})

    gates = evaluate(run_dir)["gates"]
    assert gates[expected_item] == "fail"
    assert gates["gate"] == "fail"


def test_unknown_ndjson_benign_raw_request_passes_but_task_event_fails(run_dir):
    gates = evaluate(run_dir)["gates"]
    assert gates["unknown_ndjson"] == "pass"
    detail = gates["detail"]["unknown_ndjson"]
    assert detail["files"][0]["event_names"] == ["raw_request"]
    assert detail["files"][0]["non_benign_events"] == []

    _write_jsonl(
        run_dir / "workers" / "Alice" / "Alice" / "unknown.ndjson",
        [{"event": "task_start"}],
    )
    gates = evaluate(run_dir)["gates"]
    assert gates["unknown_ndjson"] == "fail"
    assert gates["gate"] == "fail"
    entries = {
        entry["path"]: entry for entry in gates["detail"]["unknown_ndjson"]["files"]
    }
    assert entries["coordinator/unknown.ndjson"]["non_benign_events"] == []
    worker_entry = entries["workers/Alice/Alice/unknown.ndjson"]
    assert worker_entry["event_names"] == ["task_start"]
    assert worker_entry["non_benign_events"] == ["task_start"]


def test_unverifiable_gate_items_are_null_and_fail_the_total(tmp_path):
    run_dir = _build_run(tmp_path, legacy=True)
    gates = evaluate(run_dir)["gates"]
    assert gates["acceptance_gate"] is None
    assert gates["forbidden_truth_violation_count"] is None
    assert gates["framework_error_counts"] is None
    assert gates["truth_manifest_run_id"] is None
    assert gates["end_reason"] == "pass"
    assert gates["unknown_ndjson"] == "pass"
    assert gates["gate"] == "fail"  # null = unverifiable -> not a pass


# --------------------------------------------------------------------------
# legacy tolerance
# --------------------------------------------------------------------------


def test_legacy_run_missing_columns_degrade_to_null(tmp_path):
    run_dir = _build_run(tmp_path, legacy=True)
    payload = evaluate(run_dir)

    assert "trajectory.csv:NoOpSource" in payload["missing_columns"]
    assert "token_usage.csv:Status" in payload["missing_columns"]
    assert "agent_interactions.csv:Success" in payload["missing_columns"]

    decomposition = payload["l3_execution"]["noop_decomposition"]
    assert decomposition["real_action"] == 3  # Explore + UseSupply + Explore
    assert decomposition["llm"] is None
    assert decomposition["idle_heartbeat"] is None
    assert decomposition["timeout_injected"] is None
    assert payload["metric_status"]["l3_execution.noop_decomposition"] == "missing_input"

    assert payload["l4_cost"]["llm_error_rate"] is None
    assert payload["l4_cost"]["llm_error_count"] is None
    assert payload["l3_execution"]["tool_call_success_rate"] is None
    assert payload["metric_status"]["meta.truth_input"] == "missing_input"
    # run_metrics-driven metrics still work
    assert payload["l0_task"]["coverage"] == 0.5
    assert payload["l2_planning"]["load_balance_b"] == pytest.approx(1 / (2 + 1e-4))


def test_instrumentation_flags_for_known_defects(run_dir):
    payload = evaluate(run_dir)
    assert payload["instrumentation_broken"] == ["llm_latency", "map_recall"]
    assert payload["l5_performance"]["llm_latency_ms"] is None
    assert payload["l1_state_chain"]["map_recall"]["all_zero"] is True
    assert payload["metric_status"]["l1_state_chain.map_recall"] == "instrumentation_broken"
    assert payload["metric_status"]["l5_performance.llm_latency"] == "instrumentation_broken"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_default_output_and_is_idempotent(run_dir, capsys):
    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    out_path = run_dir / DEFAULT_OUTPUT
    first = out_path.read_bytes()
    payload = json.loads(first)
    assert payload["meta"]["run_id"] == "run-unit"
    assert payload["meta"]["agent_names"] == ["Alice", "Bob"]
    assert payload["meta"]["agent_names_source"] == "workers_dir"

    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    assert out_path.read_bytes() == first  # idempotent overwrite

    custom = run_dir.parent / "custom.json"
    assert main(["--results-dir", str(run_dir), "--output", str(custom)]) == EXIT_OK
    assert json.loads(custom.read_text(encoding="utf-8")) == payload
    assert "run_eval_metrics: wrote" in capsys.readouterr().out


def test_write_artifact_helper_matches_evaluate(run_dir, tmp_path):
    payload = evaluate(run_dir)
    written = write_artifact(run_dir, payload, tmp_path / "nested" / "out.json")
    assert written.is_file()
    assert json.loads(written.read_text(encoding="utf-8")) == payload


def test_cli_rejects_missing_or_empty_dir(tmp_path, capsys):
    assert main(["--results-dir", str(tmp_path / "nope")]) == EXIT_INVALID_INPUT
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--results-dir", str(empty)]) == EXIT_INVALID_INPUT
    assert "no run artifacts found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# P1-① MapRecall v2 / freshness canonical / MapAgentDiscovery
# --------------------------------------------------------------------------


def _spatial_row(entity: str, *, step: int = 0) -> dict:
    return {
        "scope_id": "scope-unit",
        "domain": "spatial",
        "entity_id": entity,
        "entity_type": "fire",
        "field_name": "position",
        "value": [0, 0, 0],
        "env_step": step,
        "provenance": "worker_sensor_tool",
    }


def _truth_row(entity: str, *, field: str = "position", value=None, domain: str = "spatial") -> dict:
    return {"domain": domain, "entity_id": entity, "field": field, "step": 0, "value": value}


def _truth_dir_of(run_dir: Path) -> Path:
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    return Path(metadata["truth_dir"])


def test_map_recall_v2_folds_region_cells_to_parent_objects(tmp_path):
    run_dir = _build_run(tmp_path)
    fires = ["FireAlpha", "FireBeta", "FireGamma"]
    spatial = [_spatial_row(name) for name in fires]
    spatial += [
        _spatial_row(f"{name}_Region_{index}") for name in fires for index in range(1, 7)
    ]  # 18 Region cells
    spatial += [
        _spatial_row("LostPersonX"),
        _spatial_row("ReservoirA"),
        _spatial_row("ReservoirB"),
        _spatial_row("DepositX"),
    ]
    _write_jsonl(run_dir / "spatial.jsonl", spatial)
    truth = [
        _truth_row(f"{name}_Region_{index}", field="parent_fire", value=name)
        for name in fires
        for index in range(1, 7)
    ]
    truth += [
        _truth_row("LostPersonX"),
        _truth_row("ReservoirA"),
        _truth_row("ReservoirB"),
        _truth_row("DepositX"),
    ]
    _write_jsonl(_truth_dir_of(run_dir) / "truth_trace.jsonl", truth)

    v2 = evaluate(run_dir)["l1_state_chain"]["map_recall_v2"]
    # 3 fires + 1 person + 2 reservoirs + 1 deposit: Region cells fold away
    assert v2["numerator"] == 7
    assert v2["denominator"] == 7
    assert v2["recall"] == pytest.approx(1.0)
    assert v2["extra"] == []
    assert v2["missing"] == []
    assert v2["denominator_source"] == "truth_trace"
    assert v2["source_detail"]["rows"] == 25


def test_map_recall_v2_truth_denominator_infers_parent_fire(tmp_path):
    run_dir = _build_run(tmp_path)
    _write_jsonl(
        run_dir / "spatial.jsonl",
        [_spatial_row("WildfireA"), _spatial_row("WildfireB")],
    )
    _write_jsonl(
        _truth_dir_of(run_dir) / "truth_trace.jsonl",
        [
            _truth_row("WildfireA_Region_1", field="parent_fire", value="WildfireA"),
            _truth_row("WildfireA_Region_2", field="parent_fire", value="WildfireA"),
            _truth_row("WildfireB_Region_1", field="parent_fire", value="WildfireB"),
            _truth_row("LostPersonY"),
        ],
    )

    v2 = evaluate(run_dir)["l1_state_chain"]["map_recall_v2"]
    # Region cells dedupe onto their parent_fire values; non-Region counts directly
    assert v2["denominator"] == 3
    assert v2["numerator"] == 2
    assert v2["recall"] == pytest.approx(2 / 3)
    assert v2["missing"] == ["LostPersonY"]
    assert v2["extra"] == []


def test_map_recall_v2_denominator_mismatch_warns_and_prefers_truth(tmp_path):
    run_dir = _build_run(tmp_path)
    names = ["FireAlpha", "LostPersonX", "ReservoirA", "ReservoirB", "DepositX"]
    _write_jsonl(run_dir / "spatial.jsonl", [_spatial_row(name) for name in names])
    _write_jsonl(
        _truth_dir_of(run_dir) / "truth_trace.jsonl",
        [_truth_row(name) for name in names],
    )
    _write_json(
        run_dir / "scene_config.json",
        {
            "objects": {
                "fires": [{"name": "FireAlpha"}, {"name": "GhostFire"}],
                "persons": [{"name": "LostPersonX"}],
                "reservoirs": [{"name": "ReservoirA"}, {"name": "ReservoirB"}],
                "deposits": [{"name": "DepositX"}],
            }
        },
    )

    v2 = evaluate(run_dir)["l1_state_chain"]["map_recall_v2"]
    check = v2["denominator_check"]
    assert check["truth_trace_count"] == 5
    assert check["scene_config_count"] == 6
    assert check["consistent"] is False
    assert "truth_trace denominator (5)" in check["warning"]
    assert v2["denominator"] == 5  # truth wins on mismatch
    assert v2["denominator_source"] == "truth_trace"
    assert v2["recall"] == pytest.approx(1.0)


def test_map_recall_v2_scene_config_fallback_denominator(tmp_path):
    run_dir = _build_run(tmp_path, legacy=True)  # no truth_dir anywhere
    _write_jsonl(
        run_dir / "spatial.jsonl",
        [
            _spatial_row("FireAlpha"),
            _spatial_row("LostPersonX"),
            _spatial_row("ReservoirA"),
            _spatial_row("DepositX"),
        ],
    )
    _write_json(
        run_dir / "scene_config.json",
        {
            "objects": {
                "fires": [{"name": "FireAlpha"}],
                "persons": [{"name": "LostPersonX"}],
                "reservoirs": [{"name": "ReservoirA"}],
                "deposits": [{"name": "DepositX"}],
            }
        },
    )

    v2 = evaluate(run_dir)["l1_state_chain"]["map_recall_v2"]
    assert v2["denominator_source"] == "scene_config"
    assert v2["denominator"] == 4
    assert v2["recall"] == pytest.approx(1.0)
    check = v2["denominator_check"]
    assert check["truth_trace_count"] is None
    assert check["scene_config_count"] == 4
    assert check["consistent"] is None


def test_map_recall_v2_null_without_truth_and_scene_config(tmp_path):
    run_dir = _build_run(tmp_path, legacy=True)
    _write_jsonl(run_dir / "spatial.jsonl", [_spatial_row("FireAlpha")])

    payload = evaluate(run_dir)
    l1 = payload["l1_state_chain"]
    assert l1["map_recall_v2"] is None  # never 0-filled (0 would read as "found nothing")
    assert payload["metric_status"]["l1_state_chain.map_recall_v2"] == "missing_input"
    assert "map_recall_v2:truth_trace" in payload["missing_columns"]
    assert "map_recall_v2:scene_config" in payload["missing_columns"]
    assert "map_recall_v2:spatial.jsonl" not in payload["missing_columns"]


def test_freshness_canonical_uses_projection_env_steps(tmp_path):
    run_dir = _build_run(tmp_path)  # run_metrics steps = 3
    _write_jsonl(
        run_dir / "spatial.jsonl",
        [
            _spatial_row("FireAlpha", step=2),
            _spatial_row("FireAlpha", step=1),  # older observation: max stays 2
            _spatial_row("FireBeta", step=1),
            _spatial_row("FireGamma_Region_1", step=0),
            _spatial_row("LostPersonX", step=0),
        ],
    )

    payload = evaluate(run_dir)
    l1 = payload["l1_state_chain"]
    fc = l1["freshness_canonical"]
    assert fc["current_step"] == 3
    assert fc["per_entity"] == {
        "FireAlpha": 1,
        "FireBeta": 2,
        "FireGamma": 3,
        "LostPersonX": 3,
    }
    assert fc["mean"] == pytest.approx((1 + 2 + 3 + 3) / 4)
    # the renamed legacy column value stays alongside (fixture Freshness 0/2/4)
    assert l1["freshness_legacy_mean"] == pytest.approx(2.0)
    assert l1["freshness_legacy"]["mean"] == pytest.approx(2.0)
    assert payload["metric_status"]["l1_state_chain.freshness_legacy"] == "measured"
    assert payload["metric_status"]["l1_state_chain.freshness_canonical"] == "measured"


def test_map_agent_discovery_parses_mixed_semantic_map(tmp_path):
    run_dir = _build_run(tmp_path)
    _write_jsonl(
        run_dir / "spatial.jsonl",
        [_spatial_row("FireAlpha"), _spatial_row("LostPersonX")],
    )
    _write_jsonl(
        _truth_dir_of(run_dir) / "truth_trace.jsonl",
        [_truth_row("FireAlpha"), _truth_row("LostPersonX")],
    )
    _write_jsonl(
        run_dir / "semantic_map.jsonl",
        [
            # exporter-rebuilt rows carry a materialized ``object`` payload
            {
                "ts": "2026-09-10T00:00:00+00:00",
                "event_type": "observation_ingested",
                "observation": {"reporter": "Alice", "step": 0, "name": "FireAlpha_Region_1"},
                "object": {"name": "FireAlpha_Region_1"},
            },
            {
                "ts": "2026-09-10T00:00:01+00:00",
                "event_type": "observation_ingested",
                "observation": {"reporter": "Bob", "step": 1, "name": "LostPersonX"},
                "object": {"name": "LostPersonX"},
            },
            # legacy teardown residue: observation-only, past the step budget
            {
                "ts": 1788970178.4,
                "event_type": "observation_ingested",
                "observation": {"reporter": "Bob", "step": 32, "object_type": "agent", "name": "Charlie"},
                "object_type": "agent",
            },
        ],
    )
    with open(run_dir / "semantic_map.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{not json\n")  # malformed line must not crash the parser

    discovery = evaluate(run_dir)["l1_state_chain"]["map_agent_discovery"]
    assert discovery["numerator"] == 2  # FireAlpha_Region_1 folded, person direct
    assert discovery["denominator"] == 2
    assert discovery["recall"] == pytest.approx(1.0)
    assert discovery["extra"] == []  # observation-only names are not discovered objects
    detail = discovery["source_detail"]
    assert detail["rows"] == 4
    assert detail["object_rows"] == 2
    assert detail["observation_only_rows"] == 1
    assert detail["observation_only_names"] == ["Charlie"]
    assert detail["parse_errors"] == 1
    assert "same object set as l1_state_chain.map_recall_v2" in discovery["note"]


def test_deprecated_map_recall_trajectory_column_is_flagged(run_dir):
    payload = evaluate(run_dir)
    assert "map_recall" in payload["instrumentation_broken"]
    assert payload["deprecated_metrics"] == {
        "map_recall_trajectory": (
            "superseded by l1_state_chain.map_recall_v2（定义在 legacy store 且 "
            "ground-truth 接线已于 H1/ac10cc4 有意移除）"
        )
    }
