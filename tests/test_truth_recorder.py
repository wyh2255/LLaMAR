"""Phase 5 evaluator-private truth recorder tests (H1 card C7).

Covers canonical claim shape / naming alignment (position ``[x,y,z]``, readable
enums, normalized inventory, deposit supply counts), per-step recording with the
0-based observation-step convention, manifest digest correctness, the terminal-
only / evaluator-private boundary (nothing is written to canonical Memory /
Context / semantic map or agent-readable paths), legacy-mode skip behavior, and
an end-to-end recorder trace + synthetic canonical DB that makes
``memory_projection_quality`` produce ``metric_status=measured`` with sensible
numbers.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig, NormalizedProjectionInputV1
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from sar_orch.eval.memory_projection_quality import evaluate as pq_evaluate
from sar_orch.eval.truth_recorder import (
    MANIFEST_FILENAME,
    SCHEMA_VERSION,
    TRACE_FILENAME,
    TruthRecorder,
    _readable_enum,
)

PROJECT_ID = "llamar"
EXPERIMENT_ID = "run-1"
CONTEXT_ID = "ctx-1"
EPOCH = 0

CLAIM_KEYS = {"step", "domain", "entity_id", "field", "value"}


class _StubBarrier:
    """Minimal barrier: only exposes ``get_env_snapshot()`` (no live objects)."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def get_env_snapshot(self):
        return self._snapshot


def _obj(name, position, **extra):
    return {"name": name, "position": position, **extra}


def _snapshot(
    *, persons=(), agents=(), deposits=(), reservoirs=(), fires=(), flammables=()
):
    return {
        "agents": list(agents),
        "fires": list(fires),
        "persons": list(persons),
        "reservoirs": list(reservoirs),
        "deposits": list(deposits),
        "flammables": list(flammables),
    }


def _make_recorder(snapshot, tmp_path, **kwargs):
    return TruthRecorder(
        _StubBarrier(snapshot),
        tmp_path / "evaluator",
        run_id="run-test",
        scene=1,
        num_agents=2,
        seed=42,
        **kwargs,
    )


# ── claim shape / naming alignment ──────────────────────────────────────────


def test_claim_shape_and_canonical_naming(tmp_path):
    snapshot = _snapshot(
        persons=[
            _obj("LostPersonTimmy", [8, 22, 0], status="PersonStatus.GROUNDED", load=2)
        ],
        agents=[
            _obj("Alice", [9, 7, 0], inventory={"Sand": 0, "Water": 0, "Person": 0})
        ],
        deposits=[_obj("DepositFacility", [23, 12, 0])],
        reservoirs=[_obj("ReservoirUtah", [15, 5, 0])],
        flammables=[_obj("CaldorFire_Region_1", [4, 4, 0], intensity="Intensity.LOW")],
    )
    recorder = _make_recorder(snapshot, tmp_path)
    claims = recorder.record_step(1)

    assert claims, "expected at least one claim"
    by_key = {}
    for claim in claims:
        assert set(claim) == CLAIM_KEYS, claim
        assert isinstance(claim["step"], int)
        assert claim["domain"] in ("spatial", "embodied")
        assert claim["entity_id"]
        assert claim["field"]
        by_key[(claim["entity_id"], claim["field"])] = claim

    # Canonical 0-based observation step (record_step(1) -> obs step 0).
    assert {c["step"] for c in claims} == {0}

    # Position always [x, y, z] ints.
    assert by_key[("LostPersonTimmy", "position")]["value"] == [8, 22, 0]
    assert by_key[("Alice", "position")]["value"] == [9, 7, 0]

    # Person: readable status + integer load.
    assert by_key[("LostPersonTimmy", "status")]["value"] == "Grounded"
    assert by_key[("LostPersonTimmy", "load")]["value"] == 2

    # Agent: embodied domain + normalized sorted inventory.
    assert by_key[("Alice", "inventory")]["domain"] == "embodied"
    assert by_key[("Alice", "inventory")]["value"] == ["Person", "Sand", "Water"]

    # Spatial domain for scene objects.
    assert by_key[("DepositFacility", "position")]["domain"] == "spatial"
    assert by_key[("ReservoirUtah", "position")]["domain"] == "spatial"
    assert by_key[("CaldorFire_Region_1", "position")]["domain"] == "spatial"

    # Flammable intensity is normalized to the readable enum name.
    assert by_key[("CaldorFire_Region_1", "intensity")]["value"] == "Low"


def test_live_object_enrichment_with_real_barrier(tmp_path):
    from sar_orch.barrier import SARBarrier

    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    try:
        recorder = TruthRecorder(
            barrier,
            tmp_path / "evaluator",
            run_id="run-test",
            scene=1,
            num_agents=2,
            seed=42,
        )
        claims = recorder.record_step(1)
        by_key = {(c["entity_id"], c["field"]): c for c in claims}

        # Fields the barrier snapshot does NOT carry are enriched read-only.
        assert by_key[("LostPersonTimmy", "status")]["value"] == "Grounded"
        assert by_key[("LostPersonTimmy", "load")]["value"] == 2
        assert by_key[("ReservoirUtah", "resource_type")]["value"] == "Sand"
        assert by_key[("ReservoirYork", "resource_type")]["value"] == "Water"
        for resource in ("Sand", "Water", "Person"):
            assert by_key[("DepositFacility", resource)]["value"] == 0
        assert by_key[("CaldorFire_Region_1", "fire_type")]["value"] == "Chemical"
        assert by_key[("CaldorFire_Region_1", "parent_fire")]["value"] == "CaldorFire"
        assert by_key[("Alice", "inventory")]["value"] == ["Person", "Sand", "Water"]
        assert by_key[("Alice", "position")]["value"] == [9, 7, 0]

        # Unnamed flammable cells are never emitted (canonical needs a name).
        unnamed = [c for c in claims if c["entity_id"].startswith("Unknown")]
        assert not unnamed
    finally:
        barrier.stop()


# ── per-step recording ──────────────────────────────────────────────────────


def test_per_step_recording_and_idempotency(tmp_path):
    recorder = _make_recorder(
        _snapshot(persons=[_obj("LostPersonTimmy", [8, 22, 0])]), tmp_path
    )
    first = recorder.record_step(1)
    assert first and all(c["step"] == 0 for c in first)

    second = recorder.record_step(2)
    assert second and all(c["step"] == 1 for c in second)

    # Re-recording an older/equal step is a no-op (no duplicate trace rows).
    assert recorder.record_step(2) == []
    assert recorder.record_step(1) == []

    lines = recorder.trace_path.read_text().strip().splitlines()
    assert len(lines) == len(first) + len(second)


# ── manifest digest correctness ─────────────────────────────────────────────


def test_manifest_digest_and_required_keys(tmp_path):
    recorder = _make_recorder(
        _snapshot(persons=[_obj("LostPersonTimmy", [8, 22, 0])]), tmp_path
    )
    recorder.record_step(1)
    recorder.record_step(2)
    recorder.set_scope_id("a1b2" * 16)

    manifest = recorder.finalize("max_steps_reached")

    assert manifest is not None
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["scope_id"] == "a1b2" * 16
    assert manifest["trace"] == TRACE_FILENAME
    assert manifest["run_id"] == "run-test"
    assert manifest["scene"] == 1
    assert manifest["agents"] == 2
    assert manifest["seed"] == 42
    assert manifest["terminal_status"] == "max_steps_reached"
    assert manifest["evaluator_private"] is True
    assert manifest["generated_by"]
    assert len(manifest["truth_trace_sha256"]) == 64

    on_disk = json.loads(recorder.manifest_path.read_text())
    assert on_disk == manifest
    assert (
        manifest["truth_trace_sha256"]
        == hashlib.sha256(recorder.trace_path.read_bytes()).hexdigest()
    )

    # Evaluator's manifest contract: scope_id + trace resolve to the trace file.
    assert (recorder.manifest_path.parent / manifest["trace"]) == recorder.trace_path


# ── terminal-only / evaluator-private boundary ──────────────────────────────


def test_writes_only_evaluator_private_files(tmp_path):
    snapshot = _snapshot(
        persons=[_obj("LostPersonTimmy", [8, 22, 0])],
        agents=[_obj("Alice", [9, 7, 0], inventory={"Sand": 0})],
    )
    recorder = _make_recorder(snapshot, tmp_path)
    recorder.record_step(1)
    recorder.record_step(2)

    # During the run only the trace exists; nothing touches canonical paths.
    assert sorted(p.name for p in recorder.output_dir.iterdir()) == [TRACE_FILENAME]

    recorder.set_scope_id("a1b2" * 16)
    recorder.finalize("completed")
    assert sorted(p.name for p in recorder.output_dir.iterdir()) == sorted(
        [TRACE_FILENAME, MANIFEST_FILENAME]
    )

    # The recorder receives no canonical / context / semantic-map path at all.
    assert "memory.sqlite3" not in [p.name for p in recorder.output_dir.iterdir()]


# ── legacy-mode skip behavior ───────────────────────────────────────────────


def test_legacy_mode_skips_manifest_cleanly(tmp_path):
    recorder = _make_recorder(
        _snapshot(persons=[_obj("LostPersonTimmy", [8, 22, 0])]), tmp_path
    )
    recorder.record_step(1)

    # No canonical scope resolved (legacy memory mode): finalize is a clean no-op.
    assert recorder.finalize("success") is None
    assert not recorder.manifest_path.exists()
    # The trace is still valid evaluator-private evidence.
    assert recorder.trace_path.exists()


# ── end-to-end: recorder trace -> projection-quality evaluator ──────────────


def _scope_id(scope_factory):
    return scope_factory.resolve(CONTEXT_ID, EPOCH).scope_id


def _input(
    scope_id, *, event_id, domain, entity_id, entity_type, field_name, value, env_step=0
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain=domain,
        entity_id=entity_id,
        entity_type=entity_type,
        field_name=field_name,
        value=value,
        confidence=1.0,
        runtime_epoch=0,
    )


def test_end_to_end_measured_with_sensible_numbers(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = MemoryStore(run_dir / "coordinator" / "memory" / "memory.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id=EXPERIMENT_ID, memory_root=run_dir / "coordinator")
    )
    scope_id = scope_factory.resolve(CONTEXT_ID, EPOCH).scope_id
    try:
        ingestor = MemoryIngestor(
            store,
            scope_factory,
            redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
        )
        ingestor.activate_scope(CONTEXT_ID, EPOCH)
        # Synthetic canonical projection for a subset of the recorded truth.
        result = ingestor.ingest_projection(
            [
                _input(
                    scope_id,
                    event_id="evt_pos",
                    domain="spatial",
                    entity_id="LostPersonTimmy",
                    entity_type="person",
                    field_name="position",
                    value=[8, 22, 0],
                ),
                _input(
                    scope_id,
                    event_id="evt_status",
                    domain="spatial",
                    entity_id="LostPersonTimmy",
                    entity_type="person",
                    field_name="status",
                    value="Grounded",
                ),
                _input(
                    scope_id,
                    event_id="evt_load",
                    domain="spatial",
                    entity_id="LostPersonTimmy",
                    entity_type="person",
                    field_name="load",
                    value=2,
                ),
                _input(
                    scope_id,
                    event_id="evt_alice_pos",
                    domain="embodied",
                    entity_id="Alice",
                    entity_type="agent",
                    field_name="position",
                    value=[9, 7, 0],
                ),
                _input(
                    scope_id,
                    event_id="evt_alice_inv",
                    domain="embodied",
                    entity_id="Alice",
                    entity_type="agent",
                    field_name="inventory",
                    value=["Person", "Sand", "Water"],
                ),
                _input(
                    scope_id,
                    event_id="evt_fire",
                    domain="spatial",
                    entity_id="CaldorFire_Region_1",
                    entity_type="fire",
                    field_name="intensity",
                    value="Low",
                ),
            ]
        )
        assert result.status == "ok", result
    finally:
        store.close()

    # run_metrics.json drives the evaluator's terminal-status mapping.
    (run_dir / "run_metrics.json").write_text(
        json.dumps({"finished": False, "end_reason": "max_steps_reached", "steps": 1}),
        encoding="utf-8",
    )

    # Record the truth and freeze the evaluator-private manifest against the
    # frozen canonical scope (exactly what the experiment terminal wiring does).
    recorder = TruthRecorder(
        _StubBarrier(
            _snapshot(
                persons=[
                    _obj(
                        "LostPersonTimmy",
                        [8, 22, 0],
                        status="PersonStatus.GROUNDED",
                        load=2,
                    )
                ],
                agents=[
                    _obj(
                        "Alice",
                        [9, 7, 0],
                        inventory={"Sand": 0, "Water": 0, "Person": 0},
                    )
                ],
                deposits=[_obj("DepositFacility", [23, 12, 0])],
                flammables=[
                    _obj("CaldorFire_Region_1", [4, 4, 0], intensity="Intensity.LOW")
                ],
            )
        ),
        tmp_path / "evaluator",
        run_id="run-test",
        scene=1,
        num_agents=2,
        seed=42,
    )
    recorder.record_step(1)
    recorder.set_scope_id(scope_id)
    manifest = recorder.finalize("max_steps_reached")
    assert manifest is not None
    manifest_path = recorder.manifest_path

    artifact = pq_evaluate(run_dir, manifest_path)

    assert artifact["metric_status"] == "measured"
    assert artifact["scope_id"] == scope_id
    assert artifact["terminal_status"] == "timeout"
    assert artifact["truth_trace_sha256"] == manifest["truth_trace_sha256"]

    worker = artifact["worker_report_quality"]
    assert worker["evaluated_report_count"] == 6
    assert worker["observable_field_count"] == 6
    assert worker["correct_field_count"] == 6
    assert worker["false_claim_count"] == 0
    assert worker["precision"] == 1.0
    assert worker["recall"] == 1.0

    memory = artifact["memory_integration_quality"]
    assert memory["evaluated_projection_field_count"] == 6
    assert memory["correct_projection_field_count"] == 6
    assert memory["stale_projection_count"] == 0
    assert memory["conflicted_field_count"] == 0
    assert memory["traceable_field_count"] == 6
    assert memory["precision"] == 1.0
    assert 0.0 < memory["recall"] < 1.0  # deposit/flammable extras are truth-only
    assert memory["evidence_traceability_rate"] == 1.0


# ── normalization helpers ───────────────────────────────────────────────────


def test_readable_enum_normalization():
    assert _readable_enum("Intensity.LOW") == "Low"
    assert _readable_enum("Intensity.MEDIUM") == "Medium"
    assert _readable_enum("Intensity.NONE") == "None"
    assert _readable_enum("PersonStatus.GROUNDED") == "Grounded"
    assert _readable_enum("PersonStatus.GRABBED") == "Grabbed"
    assert _readable_enum("Low") == "Low"
    assert _readable_enum(None) is None
    assert _readable_enum("") is None


# ── truth dir naming uniqueness (A4 P0) ─────────────────────────────────────


def test_default_truth_dir_unique_for_same_basename_log_dirs():
    """Two runs sharing a log-dir basename must get different truth dirs.

    Regression for the A4 P0 collision: driver passes ``agents_N/seed_M`` and
    benchmark passes ``scene_S/agents_A/seed_N``, both collapsing to basename
    ``seed_N`` — the old ``truth/<basename>`` rule mixed evaluator truth
    across runs.  The uuid8 (run_id tail) suffix must keep them apart.
    """
    from sar_orch.experiment import _default_truth_dir

    run_a = "sar-scene3-agents2-seed0-13564bed"
    run_b = "sar-scene3-agents5-seed0-099a58c3"
    d_a = _default_truth_dir("seed_0", run_a)
    d_b = _default_truth_dir("seed_0", run_b)
    assert d_a != d_b
    assert d_a.name == "seed_0-13564bed"
    assert d_b.name == "seed_0-099a58c3"
    # Same run_id is idempotent (resume/retry of the same run).
    assert _default_truth_dir("seed_0", run_a) == d_a
    # Auto-generated unique basenames also gain the suffix unconditionally.
    auto = _default_truth_dir("20260909_232036_s3_s42_a4", run_a)
    assert auto.name == "20260909_232036_s3_s42_a4-13564bed"


def test_truth_recorder_collision_failfast_mismatched_manifest(tmp_path):
    """A pre-existing truth dir whose manifest belongs to another run raises.

    Fail-closed: appending would silently mix two runs' evaluator truth.
    """
    recorder = TruthRecorder(
        _StubBarrier(_snapshot()), tmp_path / "evaluator",
        run_id="run-a", scene=1, num_agents=2, seed=42,
    )
    recorder.set_scope_id("a1b2" * 16)
    recorder.finalize("max_steps_reached")
    assert recorder.manifest_path.is_file()

    with pytest.raises(RuntimeError, match="collision"):
        TruthRecorder(
            _StubBarrier(_snapshot()), tmp_path / "evaluator",
            run_id="run-b", scene=1, num_agents=2, seed=42,
        )


def test_truth_recorder_collision_failfast_nonempty_trace(tmp_path):
    """A non-empty truth_trace.jsonl in the target dir raises even without a
    manifest (e.g. a crashed run that never finalized).
    """
    recorder = TruthRecorder(
        _StubBarrier(_snapshot()), tmp_path / "evaluator",
        run_id="run-a", scene=1, num_agents=2, seed=42,
    )
    assert recorder.record_step(1) == []  # empty snapshot → no claims written

    # Force a non-empty trace via a direct append (empty snapshot yields none).
    recorder._append_claims([{"step": 0, "domain": "spatial", "entity_id": "x",
                              "field": "y", "value": "z"}])
    assert recorder.trace_path.stat().st_size > 0

    with pytest.raises(RuntimeError, match="non-empty truth_trace"):
        TruthRecorder(
            _StubBarrier(_snapshot()), tmp_path / "evaluator",
            run_id="run-b", scene=1, num_agents=2, seed=42,
        )


def test_truth_recorder_empty_dir_reinit_allowed(tmp_path):
    """Same-run re-initialization on an empty dir stays legal (no data loss)."""
    out = tmp_path / "evaluator"
    TruthRecorder(_StubBarrier(_snapshot()), out, run_id="run-a",
                  scene=1, num_agents=2, seed=42)
    TruthRecorder(_StubBarrier(_snapshot()), out, run_id="run-a",
                  scene=1, num_agents=2, seed=42)  # still empty → OK
