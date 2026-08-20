"""Phase 5 acceptance metric tests for sar_orch.eval.memory_acceptance.

Covers the plan 10.1 contract: coverage/transport must be non-null from
``run_metrics.json``; failed tool rows are aggregated from the Phase 5
``{Success, ErrorType}`` outcome rows of BOTH producer CSVs; the gate enforces
``missing_error_code_rows=0`` and all three reported framework codes at 0.
The known-code set is the FULL taxonomy allowlist
(``Agent.error_taxonomy.FRAMEWORK_ERROR_CODES``): allowlisted non-reported codes
(e.g. ``graph_activation_required``) pass; any code outside the allowlist —
including the sentinels ``unclassified_tool_error`` and ``missing_error_code``
— exits nonzero printing code/count.  No runtime log grep.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from sar_orch.eval.memory_acceptance import (
    EXIT_ARTIFACTS_MISSING,
    EXIT_FRAMEWORK_ERRORS_PRESENT,
    EXIT_INSTRUMENTATION_MISSING,
    EXIT_INVALID_MEMORY_MANIFEST,
    EXIT_METRICS_MISSING,
    EXIT_OK,
    EXIT_UNKNOWN_ERROR_CODE,
    AcceptanceError,
    evaluate,
    gate,
    main,
)

AGENT_HEADERS = [
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "LLMInput",
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


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return d


@pytest.fixture
def store(run_dir):
    st = MemoryStore(run_dir / "memory" / "memory.sqlite3")
    yield st
    st.close()


@pytest.fixture
def scope_factory(run_dir):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=run_dir))


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _scope_id(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def _agent_row(**overrides):
    row = {
        "Step": 0,
        "Agent": "Alice",
        "ToolName": "get_agent_state",
        "ToolArgs": "{}",
        "Action": "",
        "Observation": "",
        "LLMInput": "",
        "LLMOutput": "",
        "Thinking": "",
        "RunID": "r1",
        "CorrelationID": "c1",
        "EventType": "tool_result",
        "ToolLatencyMs": "1.0",
        "Success": "true",
        "ErrorType": "",
    }
    row.update(overrides)
    return row


def _router_row(**overrides):
    row = {
        "Step": 0,
        "Subtask": "explore",
        "AssignedTo": "Alice",
        "RunID": "r1",
        "CorrelationID": "c2",
        "WorkerTaskID": "w1",
        "EventType": "assign_task",
        "Success": "true",
        "ErrorType": "",
    }
    row.update(overrides)
    return row


def _write_clean_csvs(run_dir):
    _write_csv(run_dir / "agent_interactions.csv", AGENT_HEADERS, [_agent_row()])
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])


def _write_metrics(
    run_dir, coverage: float | None = 1.0, transport_rate: float | None = 0.9, **extra
):
    data = {
        "coverage": coverage,
        "transport_rate": transport_rate,
        "steps": 10,
        "end_reason": "max_steps_reached",
        "run_id": "run-1",
    }
    data.update(extra)
    _write_json(run_dir / "run_metrics.json", data)


def _fake_scope_id():
    return hashlib.sha256(b"evaluator-test-scope").hexdigest()


def test_clean_run_accepts_with_db(store, ingestor, scope_factory, run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir, coverage=0.8, transport_rate=0.6)

    result, unknown = evaluate(run_dir)
    gate(result, unknown)

    assert unknown == {}
    assert result["schema_version"] == 1
    assert result["scope_id"] == _scope_id(scope_factory)
    assert result["memory_revision"] >= 0
    assert result["coverage"] == 0.8
    assert result["transport_rate"] == 0.6
    assert result["failed_tool_rows"] == 0
    assert result["missing_error_code_rows"] == 0
    assert result["framework_error_counts"] == {
        "worker_busy": 0,
        "task_not_routable_yet": 0,
        "unknown_task_id": 0,
    }


def test_clean_run_accepts_with_export_manifest(run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    scope_id = _fake_scope_id()
    memory = run_dir / "memory"
    memory.mkdir()
    manifest = {"schema_version": 1, "scope_id": scope_id, "canonical_revision": 42}
    manifest_path = memory / "export_manifest.json"
    _write_json(manifest_path, manifest)

    result, _unknown = evaluate(run_dir)
    gate(result, _unknown)

    assert result["scope_id"] == scope_id
    assert result["memory_revision"] == 42
    assert (
        result["export_manifest_sha256"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )


def test_worker_busy_failure_counted_and_gate_fails(
    store, ingestor, scope_factory, run_dir
):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="worker_busy")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert unknown == {}
    assert result["failed_tool_rows"] == 1
    assert result["framework_error_counts"]["worker_busy"] == 1

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_FRAMEWORK_ERRORS_PRESENT


def test_router_failure_counted(store, ingestor, scope_factory, run_dir):
    _write_csv(run_dir / "agent_interactions.csv", AGENT_HEADERS, [_agent_row()])
    _write_csv(
        run_dir / "router_interactions.csv",
        ROUTER_HEADERS,
        [_router_row(Success="false", ErrorType="task_not_routable_yet")],
    )
    _write_metrics(run_dir)

    result, _unknown = evaluate(run_dir)
    assert result["failed_tool_rows"] == 1
    assert result["framework_error_counts"]["task_not_routable_yet"] == 1


def test_both_csvs_failures_aggregate(store, ingestor, scope_factory, run_dir):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="worker_busy")],
    )
    _write_csv(
        run_dir / "router_interactions.csv",
        ROUTER_HEADERS,
        [_router_row(Success="false", ErrorType="unknown_task_id")],
    )
    _write_metrics(run_dir)

    result, _unknown = evaluate(run_dir)
    assert result["failed_tool_rows"] == 2
    assert result["framework_error_counts"] == {
        "worker_busy": 1,
        "task_not_routable_yet": 0,
        "unknown_task_id": 1,
    }


def test_missing_error_code_exits_instrumentation_missing(
    store, ingestor, scope_factory, run_dir
):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert result["missing_error_code_rows"] == 1

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_INSTRUMENTATION_MISSING
    assert "instrumentation_missing" in excinfo.value.message


def test_unknown_error_code_exits_with_code_and_count(
    store, ingestor, scope_factory, run_dir
):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="bogus_failure_code")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert result["failed_tool_rows"] == 1
    assert unknown == {"bogus_failure_code": 1}

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_UNKNOWN_ERROR_CODE
    assert "code=bogus_failure_code" in excinfo.value.message
    assert "count=1" in excinfo.value.message


def test_allowlisted_non_reported_code_counts_known_and_passes_gate(
    store, ingestor, scope_factory, run_dir
):
    """graph_activation_required is in the FULL taxonomy allowlist but is not a
    reported gate code; it must be counted as known and PASS the gate."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="graph_activation_required")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert unknown == {}
    assert result["failed_tool_rows"] == 1
    assert result["framework_error_counts"] == {
        "worker_busy": 0,
        "task_not_routable_yet": 0,
        "unknown_task_id": 0,
    }
    assert result["known_allowlisted_counts"] == {"graph_activation_required": 1}

    gate(result, unknown)  # must not raise


def test_allowlisted_non_reported_code_mixed_with_reported(
    store, ingestor, scope_factory, run_dir
):
    """An allowlisted non-reported code alongside a reported one: the reported
    code still fails with EXIT_FRAMEWORK_ERRORS_PRESENT; the allowlisted code is
    known (never reported as unknown)."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [
            _agent_row(Success="false", ErrorType="worker_busy"),
            _agent_row(Success="false", ErrorType="node_not_found"),
        ],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert unknown == {}
    assert result["framework_error_counts"]["worker_busy"] == 1
    assert result["known_allowlisted_counts"] == {"node_not_found": 1}

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_FRAMEWORK_ERRORS_PRESENT
    assert "worker_busy=1" in excinfo.value.message


def test_sentinel_unclassified_tool_error_exits_unknown(
    store, ingestor, scope_factory, run_dir
):
    """The sentinel ``unclassified_tool_error`` is NOT in the full allowlist and
    must be treated as unknown -> exit 4 printing code/count."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="unclassified_tool_error")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert result["missing_error_code_rows"] == 0
    assert unknown == {"unclassified_tool_error": 1}

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_UNKNOWN_ERROR_CODE
    assert "code=unclassified_tool_error" in excinfo.value.message


def test_sentinel_missing_error_code_literal_exits_unknown(
    store, ingestor, scope_factory, run_dir
):
    """A literal ``missing_error_code`` ErrorType value (as opposed to an empty
    ErrorType) is outside the allowlist -> unknown, exit 4."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="missing_error_code")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert result["missing_error_code_rows"] == 0
    assert unknown == {"missing_error_code": 1}

    with pytest.raises(AcceptanceError) as excinfo:
        gate(result, unknown)
    assert excinfo.value.exit_code == EXIT_UNKNOWN_ERROR_CODE
    assert "code=missing_error_code" in excinfo.value.message


def test_success_row_with_error_type_is_not_a_failure(
    store, ingestor, scope_factory, run_dir
):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="true", ErrorType="worker_busy")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    assert result["failed_tool_rows"] == 0
    assert result["framework_error_counts"]["worker_busy"] == 0
    assert unknown == {}


def test_null_coverage_rejected(run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir, coverage=None, transport_rate=0.9)
    with pytest.raises(AcceptanceError) as excinfo:
        evaluate(run_dir)
    assert excinfo.value.exit_code == EXIT_METRICS_MISSING


def test_null_transport_rate_rejected(run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir, coverage=0.9, transport_rate=None)
    with pytest.raises(AcceptanceError) as excinfo:
        evaluate(run_dir)
    assert excinfo.value.exit_code == EXIT_METRICS_MISSING


def test_missing_outcome_artifact_rejected(run_dir):
    _write_csv(run_dir / "agent_interactions.csv", AGENT_HEADERS, [_agent_row()])
    _write_metrics(run_dir)
    with pytest.raises(AcceptanceError) as excinfo:
        evaluate(run_dir)
    assert excinfo.value.exit_code == EXIT_ARTIFACTS_MISSING


def test_missing_memory_and_manifest_rejected(run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    with pytest.raises(AcceptanceError) as excinfo:
        evaluate(run_dir)
    assert excinfo.value.exit_code == EXIT_INVALID_MEMORY_MANIFEST


def test_export_manifest_scope_mismatch_with_db(store, ingestor, run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    memory = run_dir / "memory"
    manifest = {
        "schema_version": 1,
        "scope_id": _fake_scope_id(),
        "canonical_revision": 3,
    }
    _write_json(memory / "export_manifest.json", manifest)
    with pytest.raises(AcceptanceError) as excinfo:
        evaluate(run_dir)
    assert excinfo.value.exit_code == EXIT_INVALID_MEMORY_MANIFEST


def test_db_fallback_scope_and_revision(store, ingestor, scope_factory, run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    result, _unknown = evaluate(run_dir)
    assert result["scope_id"] == _scope_id(scope_factory)
    assert result["export_manifest_sha256"] is None


def test_db_at_coordinator_memory_layout(run_dir):
    st = MemoryStore(run_dir / "coordinator" / "memory" / "memory.sqlite3")
    try:
        factory = MemoryScopeFactory(
            MemoryConfig(experiment_id="run-1", memory_root=run_dir / "coordinator")
        )
        scope = factory.resolve("ctx-1", 0)
        st.activate_scope(scope)
        scope_id = scope.scope_id
    finally:
        st.close()
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)

    result, unknown = evaluate(run_dir)
    gate(result, unknown)
    assert result["scope_id"] == scope_id
    assert result["coverage"] == 1.0
    assert result["missing_error_code_rows"] == 0


def test_main_clean_exit_zero(store, ingestor, run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    assert main(["--results-dir", str(run_dir)]) == EXIT_OK
    artifact = json.loads((run_dir / "memory_acceptance.json").read_text())
    assert artifact["coverage"] == 1.0
    assert artifact["missing_error_code_rows"] == 0


def test_main_instrumentation_missing_exit(store, ingestor, run_dir):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)
    assert main(["--results-dir", str(run_dir)]) == EXIT_INSTRUMENTATION_MISSING
    artifact = json.loads((run_dir / "memory_acceptance.json").read_text())
    assert artifact["missing_error_code_rows"] == 1


def test_cli_script_clean_run(run_dir):
    _write_clean_csvs(run_dir)
    _write_metrics(run_dir)
    scope_id = _fake_scope_id()
    memory = run_dir / "memory"
    memory.mkdir()
    _write_json(
        memory / "export_manifest.json", {"scope_id": scope_id, "canonical_revision": 1}
    )

    script = (
        Path(__file__).resolve().parent.parent
        / "sar_orch"
        / "eval"
        / "memory_acceptance.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--results-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    artifact = json.loads((run_dir / "memory_acceptance.json").read_text())
    assert artifact["scope_id"] == scope_id
    assert artifact["coverage"] == 1.0
    assert artifact["framework_error_counts"]["worker_busy"] == 0


def test_cli_script_missing_error_code_exits_nonzero(run_dir):
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)
    scope_id = _fake_scope_id()
    memory = run_dir / "memory"
    memory.mkdir()
    _write_json(
        memory / "export_manifest.json", {"scope_id": scope_id, "canonical_revision": 1}
    )

    script = (
        Path(__file__).resolve().parent.parent
        / "sar_orch"
        / "eval"
        / "memory_acceptance.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--results-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_INSTRUMENTATION_MISSING
    assert "instrumentation_missing" in proc.stderr


def test_cli_script_sentinel_unknown_code_exits_four(run_dir):
    """A failed row whose ErrorType is the unclassified sentinel must exit with
    EXIT_UNKNOWN_ERROR_CODE and print the code/count."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="unclassified_tool_error")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)
    scope_id = _fake_scope_id()
    memory = run_dir / "memory"
    memory.mkdir()
    _write_json(
        memory / "export_manifest.json", {"scope_id": scope_id, "canonical_revision": 1}
    )

    script = (
        Path(__file__).resolve().parent.parent
        / "sar_orch"
        / "eval"
        / "memory_acceptance.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--results-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_UNKNOWN_ERROR_CODE
    assert "unknown_error_code" in proc.stderr
    assert "code=unclassified_tool_error" in proc.stderr


def test_cli_script_allowlisted_non_reported_code_passes(run_dir):
    """A failed row with an allowlisted non-reported code (graph_activation_required)
    is known and must NOT trigger the unknown/empty gates (exit 0)."""
    _write_csv(
        run_dir / "agent_interactions.csv",
        AGENT_HEADERS,
        [_agent_row(Success="false", ErrorType="graph_activation_required")],
    )
    _write_csv(run_dir / "router_interactions.csv", ROUTER_HEADERS, [_router_row()])
    _write_metrics(run_dir)
    scope_id = _fake_scope_id()
    memory = run_dir / "memory"
    memory.mkdir()
    _write_json(
        memory / "export_manifest.json", {"scope_id": scope_id, "canonical_revision": 1}
    )

    script = (
        Path(__file__).resolve().parent.parent
        / "sar_orch"
        / "eval"
        / "memory_acceptance.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--results-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    artifact = json.loads((run_dir / "memory_acceptance.json").read_text())
    assert artifact["known_allowlisted_counts"] == {"graph_activation_required": 1}
    assert artifact["framework_error_counts"]["worker_busy"] == 0
