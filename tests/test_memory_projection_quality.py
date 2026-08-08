"""Phase 5 projection-quality evaluator tests (H1 card C7).

Terminal-only, read-only comparison of the frozen Worker evidence / Memory
snapshot against an evaluator-private truth manifest/trace.  Covers same-step
worker report accuracy, projection freshness / conflict calibration and
evidence traceability, the null-rate / not_applicable rules, invalid-input
nonzero exits, and the hard boundary that the evaluator never writes canonical
Memory nor copies the raw truth trace into a candidate-readable path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig, NormalizedProjectionInputV1
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from sar_orch.eval.memory_projection_quality import (
    EXIT_INVALID_INPUT,
    EXIT_OK,
    ProjectionQualityError,
    evaluate,
    main,
    write_artifact,
)

PROJECT_ID = "llamar"
EXPERIMENT_ID = "run-1"
CONTEXT_ID = "ctx-1"
EPOCH = 0


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
    return MemoryScopeFactory(
        MemoryConfig(experiment_id=EXPERIMENT_ID, memory_root=run_dir)
    )


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope(CONTEXT_ID, EPOCH)
    return ing


def _scope_id(scope_factory):
    return scope_factory.resolve(CONTEXT_ID, EPOCH).scope_id


def _input(
    scope_id,
    *,
    event_id,
    domain="spatial",
    entity_id="FireA",
    entity_type="fire",
    field_name="intensity",
    value: object = "High",
    env_step=8,
    provenance="worker_sensor_tool",
    actor_id="alice",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id=actor_id,
        provenance=provenance,
        domain=domain,
        entity_id=entity_id,
        entity_type=entity_type,
        field_name=field_name,
        value=value,
        confidence=1.0,
    )


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_truth_manifest(evaluator_dir, scope_id, trace_name="truth_trace.jsonl"):
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evaluator_dir / "truth_manifest.json"
    _write_json(
        manifest_path,
        {"schema_version": 1, "scope_id": scope_id, "trace": trace_name},
    )
    return manifest_path


def _write_truth_trace(evaluator_dir, claims):
    trace_path = evaluator_dir / "truth_trace.jsonl"
    with open(trace_path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(claim, ensure_ascii=False) + "\n" for claim in claims)
    return trace_path


def _write_metrics(run_dir, end_reason="max_steps_reached", finished=False):
    _write_json(
        run_dir / "run_metrics.json",
        {
            "coverage": 0.9,
            "transport_rate": 0.8,
            "steps": 10,
            "end_reason": end_reason,
            "finished": finished,
        },
    )


def _file_set(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_completed_measured_with_correct_projection(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            )
        ]
    )
    _write_metrics(run_dir, end_reason="success", finished=True)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    artifact = evaluate(run_dir, manifest_path)

    assert artifact["schema_version"] == 1
    assert artifact["scope_id"] == scope_id
    assert artifact["terminal_status"] == "completed"
    assert artifact["metric_status"] == "measured"
    assert len(artifact["memory_manifest_sha256"]) == 64
    assert len(artifact["truth_trace_sha256"]) == 64
    assert artifact["evaluator_version"]

    worker = artifact["worker_report_quality"]
    assert worker["evaluated_report_count"] == 1
    assert worker["observable_field_count"] == 1
    assert worker["correct_field_count"] == 1
    assert worker["precision"] == 1.0
    assert worker["recall"] == 1.0

    memory = artifact["memory_integration_quality"]
    assert memory["evaluated_projection_field_count"] == 1
    assert memory["correct_projection_field_count"] == 1
    assert memory["stale_projection_count"] == 0
    assert memory["conflicted_field_count"] == 0
    assert memory["traceable_field_count"] == 1
    assert memory["precision"] == 1.0
    assert memory["recall"] == 1.0
    assert memory["conflict_precision"] == 0.0
    assert memory["evidence_traceability_rate"] == 1.0


def test_worker_precision_recall_and_staleness(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            ),
            _input(
                scope_id,
                event_id="evt_2",
                entity_id="FireA",
                field_name="position",
                value=[9, 9, 0],
                env_step=8,
            ),
            _input(
                scope_id,
                event_id="evt_3",
                entity_id="FireB",
                field_name="intensity",
                value="High",
                env_step=5,
            ),
        ]
    )
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            },
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "position",
                "value": [3, 4, 0],
            },
            {
                "step": 12,
                "domain": "spatial",
                "entity_id": "FireB",
                "field": "intensity",
                "value": "Low",
            },
        ],
    )

    artifact = evaluate(run_dir, manifest_path)

    worker = artifact["worker_report_quality"]
    assert worker["evaluated_report_count"] == 3
    assert worker["observable_field_count"] == 2
    assert worker["correct_field_count"] == 1
    assert worker["false_claim_count"] == 1
    assert worker["stale_report_count"] == 1
    assert worker["precision"] == pytest.approx(0.5)
    assert worker["recall"] == pytest.approx(0.5)

    memory = artifact["memory_integration_quality"]
    assert memory["evaluated_projection_field_count"] == 3
    assert memory["correct_projection_field_count"] == 1
    assert memory["stale_projection_count"] == 1
    assert memory["conflicted_field_count"] == 0
    assert memory["traceable_field_count"] == 3
    assert memory["precision"] == pytest.approx(1 / 3)
    assert memory["recall"] == 1.0
    assert memory["evidence_traceability_rate"] == 1.0


def test_same_step_conflict_calibration(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            )
        ]
    )
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_2",
                entity_id="FireA",
                field_name="intensity",
                value="Low",
                env_step=8,
                actor_id="bob",
            )
        ]
    )
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    artifact = evaluate(run_dir, manifest_path)

    memory = artifact["memory_integration_quality"]
    assert memory["evaluated_projection_field_count"] == 1
    assert memory["conflicted_field_count"] == 1
    assert memory["correct_projection_field_count"] == 0
    assert memory["conflict_precision"] == 1.0
    assert memory["evidence_traceability_rate"] == 1.0
    assert artifact["metric_status"] == "measured"

    worker = artifact["worker_report_quality"]
    assert worker["observable_field_count"] == 1
    assert worker["precision"] is not None
    assert worker["recall"] == 1.0


def test_stale_late_evidence_does_not_regress_projection(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            )
        ]
    )
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_2",
                entity_id="FireA",
                field_name="intensity",
                value="Low",
                env_step=5,
            )
        ]
    )
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    artifact = evaluate(run_dir, manifest_path)

    memory = artifact["memory_integration_quality"]
    assert memory["evaluated_projection_field_count"] == 1
    assert memory["correct_projection_field_count"] == 1
    assert memory["stale_projection_count"] == 0
    assert memory["conflicted_field_count"] == 0

    worker = artifact["worker_report_quality"]
    assert worker["stale_report_count"] == 0
    assert worker["observable_field_count"] == 1
    assert worker["correct_field_count"] == 1


def test_not_applicable_when_empty_truth_trace(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    artifact = evaluate(run_dir, manifest_path)

    assert artifact["metric_status"] == "not_applicable"
    assert artifact["worker_report_quality"]["precision"] is None
    assert artifact["worker_report_quality"]["recall"] is None
    assert artifact["memory_integration_quality"]["precision"] is None
    assert artifact["memory_integration_quality"]["recall"] is None
    assert artifact["memory_integration_quality"]["conflict_precision"] is None
    assert artifact["memory_integration_quality"]["evidence_traceability_rate"] is None


def test_not_applicable_when_no_evidence_or_projection(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    artifact = evaluate(run_dir, manifest_path)

    assert artifact["metric_status"] == "not_applicable"
    assert artifact["worker_report_quality"]["precision"] is None
    assert artifact["memory_integration_quality"]["evidence_traceability_rate"] is None


@pytest.mark.parametrize(
    ("end_reason", "finished", "expected"),
    [
        ("success", True, "completed"),
        ("completed", False, "completed"),
        ("max_steps_reached", False, "timeout"),
        ("wall_clock_timeout", False, "timeout"),
        ("framework_error", False, "failed"),
        ("environment_error", False, "failed"),
        ("cancelled", False, "cancelled"),
    ],
)
def test_terminal_status_mapping(
    ingestor,
    store,
    scope_factory,
    run_dir,
    tmp_path,
    end_reason,
    finished,
    expected,
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir, end_reason=end_reason, finished=finished)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    artifact = evaluate(run_dir, manifest_path)
    assert artifact["terminal_status"] == expected


def test_unknown_terminal_status_is_invalid(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir, end_reason="mystery_outcome")
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    with pytest.raises(ProjectionQualityError) as excinfo:
        evaluate(run_dir, manifest_path)
    assert excinfo.value.exit_code == EXIT_INVALID_INPUT
    assert "invalid terminal status" in excinfo.value.message


def test_scope_mismatch_is_invalid(ingestor, store, scope_factory, run_dir, tmp_path):
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, "deadbeef" * 16)
    _write_truth_trace(evaluator_dir, [])

    with pytest.raises(ProjectionQualityError) as excinfo:
        evaluate(run_dir, manifest_path)
    assert excinfo.value.exit_code == EXIT_INVALID_INPUT
    assert "scope_mismatch" in excinfo.value.message


def test_missing_canonical_db_is_invalid(run_dir, tmp_path):
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, "deadbeef" * 16)
    _write_truth_trace(evaluator_dir, [])

    with pytest.raises(ProjectionQualityError) as excinfo:
        evaluate(run_dir, manifest_path)
    assert excinfo.value.exit_code == EXIT_INVALID_INPUT
    assert "canonical memory DB not found" in excinfo.value.message


def test_missing_truth_manifest_is_invalid(run_dir, tmp_path):
    with pytest.raises(ProjectionQualityError) as excinfo:
        evaluate(run_dir, tmp_path / "missing" / "truth_manifest.json")
    assert excinfo.value.exit_code == EXIT_INVALID_INPUT


def test_truth_trace_override_argument(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(
        evaluator_dir, scope_id, trace_name="other.jsonl"
    )
    trace_path = tmp_path / ".evaluator" / "truth_trace.jsonl"
    _write_truth_trace(
        tmp_path / ".evaluator",
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    artifact = evaluate(run_dir, manifest_path, truth_trace_path=trace_path)
    assert artifact["metric_status"] == "not_applicable"
    assert (
        artifact["truth_trace_sha256"]
        == hashlib.sha256(trace_path.read_bytes()).hexdigest()
    )


def test_evaluator_only_writes_quality_artifact(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            )
        ]
    )
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    trace_path = _write_truth_trace(
        evaluator_dir,
        [
            {
                "step": 8,
                "domain": "spatial",
                "entity_id": "FireA",
                "field": "intensity",
                "value": "High",
            }
        ],
    )

    before = _file_set(run_dir)
    artifact = evaluate(run_dir, manifest_path)
    after = _file_set(run_dir)
    assert after == before

    write_artifact(run_dir, artifact)
    added = _file_set(run_dir) - before
    assert added == {"memory_projection_quality.json"}
    raw_trace = trace_path.read_text()
    assert raw_trace not in (run_dir / "memory_projection_quality.json").read_text()
    assert "truth_trace.jsonl" not in after


def test_memory_manifest_digest_is_deterministic(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_1",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
            )
        ]
    )
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    first = evaluate(run_dir, manifest_path)
    second = evaluate(run_dir, manifest_path)
    assert first["memory_manifest_sha256"] == second["memory_manifest_sha256"]
    assert first["truth_trace_sha256"] == second["truth_trace_sha256"]


def test_coordinator_memory_layout(run_dir, tmp_path):
    st = MemoryStore(run_dir / "coordinator" / "memory" / "memory.sqlite3")
    try:
        factory = MemoryScopeFactory(
            MemoryConfig(
                experiment_id=EXPERIMENT_ID, memory_root=run_dir / "coordinator"
            )
        )
        scope = factory.resolve(CONTEXT_ID, EPOCH)
        st.activate_scope(scope)
        scope_id = scope.scope_id
    finally:
        st.close()
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    artifact = evaluate(run_dir, manifest_path)
    assert artifact["scope_id"] == scope_id
    assert artifact["metric_status"] == "not_applicable"


def test_main_clean_exit_writes_artifact(
    ingestor, store, scope_factory, run_dir, tmp_path
):
    scope_id = _scope_id(scope_factory)
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    assert (
        main(["--results-dir", str(run_dir), "--truth-manifest", str(manifest_path)])
        == EXIT_OK
    )
    artifact = json.loads((run_dir / "memory_projection_quality.json").read_text())
    assert artifact["metric_status"] == "not_applicable"


def test_cli_script_clean_run(run_dir, tmp_path):
    st = MemoryStore(run_dir / "memory" / "memory.sqlite3")
    try:
        factory = MemoryScopeFactory(
            MemoryConfig(experiment_id=EXPERIMENT_ID, memory_root=run_dir)
        )
        scope = factory.resolve(CONTEXT_ID, EPOCH)
        st.activate_scope(scope)
        scope_id = scope.scope_id
    finally:
        st.close()
    _write_metrics(run_dir)
    evaluator_dir = tmp_path / ".evaluator"
    manifest_path = _write_truth_manifest(evaluator_dir, scope_id)
    _write_truth_trace(evaluator_dir, [])

    script = (
        Path(__file__).resolve().parent.parent
        / "sar_orch"
        / "eval"
        / "memory_projection_quality.py"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--results-dir",
            str(run_dir),
            "--truth-manifest",
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    artifact = json.loads((run_dir / "memory_projection_quality.json").read_text())
    assert artifact["terminal_status"] == "timeout"
    assert artifact["metric_status"] == "not_applicable"
    assert artifact["scope_id"] == scope_id
