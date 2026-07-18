from pathlib import Path

from sar_orch.map import ObservationRecord, SemanticMapStore


def test_init_priors_exposes_reservoirs_and_deposits_but_no_fire_truth():
    store = SemanticMapStore()
    store.init_priors(
        reservoirs=[
            {"name": "ReservoirYork", "position": [1, 2, 0], "resource_type": "Water"}
        ],
        deposits=[{"name": "DepositA", "position": [3, 4, 0], "inventory": {}}],
        agents=[{"agent_id": "Alice"}],
        rules={"Chemical": "Sand"},
        step_budget={"current_step": 0, "max_steps": 120, "remaining": 120},
        task_objective="Extinguish all fires and rescue all persons",
    )

    snapshot = store.snapshot()

    assert snapshot["known_priors"]["reservoirs"][0]["name"] == "ReservoirYork"
    assert snapshot["known_priors"]["deposits"][0]["name"] == "DepositA"
    assert snapshot["known_dynamic_objects"]["fires"] == []
    assert snapshot["known_dynamic_objects"]["persons"] == []


def test_ingest_observation_adds_fire_with_source_and_step():
    store = SemanticMapStore()
    record = ObservationRecord(
        reporter="Alice",
        step=7,
        object_type="fire",
        name="CaldorFire_Region_1",
        position=(4, 4, 0),
        attributes={"fire_type": "Chemical", "intensity": "Medium", "status": "active"},
        confidence=1.0,
        source_task_id="alice-scout",
        note="Observed during scouting",
    )

    merged = store.ingest_observation(record)
    snapshot = store.snapshot()

    assert merged["name"] == "CaldorFire_Region_1"
    fire = snapshot["known_dynamic_objects"]["fires"][0]
    assert fire["attributes"]["fire_type"] == "Chemical"
    assert fire["last_seen_step"] == 7
    assert fire["sources"][0]["reporter"] == "Alice"


def test_newer_observation_updates_same_named_object():
    store = SemanticMapStore()
    store.ingest_observation(
        {
            "reporter": "Alice",
            "step": 3,
            "object_type": "fire",
            "name": "GreatFire_Region_1",
            "position": [5, 5, 0],
            "attributes": {"intensity": "High", "status": "active"},
        }
    )
    store.ingest_observation(
        {
            "reporter": "Bob",
            "step": 5,
            "object_type": "fire",
            "name": "GreatFire_Region_1",
            "position": [5, 5, 0],
            "attributes": {"intensity": "Low", "status": "active"},
        }
    )

    fire = store.snapshot()["known_dynamic_objects"]["fires"][0]

    assert fire["attributes"]["intensity"] == "Low"
    assert fire["last_seen_step"] == 5
    assert {source["reporter"] for source in fire["sources"]} == {"Alice", "Bob"}


def test_conflicting_same_step_observations_are_marked():
    store = SemanticMapStore()
    base = {
        "step": 6,
        "object_type": "person",
        "name": "Timmy",
        "position": [10, 10, 0],
    }
    store.ingest_observation(
        {**base, "reporter": "Alice", "attributes": {"status": "trapped"}}
    )
    store.ingest_observation(
        {**base, "reporter": "Bob", "attributes": {"status": "rescued"}}
    )

    person = store.snapshot()["known_dynamic_objects"]["persons"][0]

    assert person["conflict"] is True
    assert person["attributes"]["status"] == "rescued"


def test_stale_entries_are_reported():
    store = SemanticMapStore()
    store.ingest_observation(
        {
            "reporter": "Alice",
            "step": 1,
            "object_type": "fire",
            "name": "OldFire",
            "position": [1, 1, 0],
            "attributes": {"status": "active"},
        }
    )
    store.update_step_budget(current_step=10, max_steps=120)

    snapshot = store.snapshot(max_stale_steps=5)

    assert snapshot["stale_entries"][0]["name"] == "OldFire"


def test_jsonl_persistence_records_ingest(tmp_path: Path):
    path = tmp_path / "semantic_map.jsonl"
    store = SemanticMapStore(jsonl_path=path)
    store.ingest_observation(
        {
            "reporter": "Alice",
            "step": 2,
            "object_type": "fire",
            "name": "FireA",
            "position": [2, 2, 0],
            "attributes": {"status": "active"},
        }
    )

    text = path.read_text(encoding="utf-8")

    assert "observation_ingested" in text
    assert "FireA" in text


# ── Phase 0 contract tests: out-of-order position guard ─────────────────

def test_out_of_order_fire_position_not_regressed():
    """Later step (8) position must not be overwritten by an older step (7).

    Expected to FAIL until Phase 2 adds the step guard in _merge_locked().
    Non-position attributes (intensity) can still be merged by existing rules.
    """
    store = SemanticMapStore()
    # Step 8 observation arrives first
    store.ingest_observation({
        "reporter": "Alice",
        "step": 8,
        "object_type": "fire",
        "name": "GreatFire",
        "position": [5, 5, 0],
        "attributes": {"intensity": "High", "status": "active"},
    })
    # Step 7 observation arrives later (out of order)
    store.ingest_observation({
        "reporter": "Bob",
        "step": 7,
        "object_type": "fire",
        "name": "GreatFire",
        "position": [3, 3, 0],
        "attributes": {"intensity": "Low", "status": "active"},
    })

    fire = store.snapshot()["known_dynamic_objects"]["fires"][0]
    # Phase 2 contract: step 8 position must survive
    assert fire["position"] == [5, 5, 0], (
        f"Expected position [5,5,0] (step 8), got {fire['position']}"
    )
    # Non-position attributes from the older step may still be merged
    # by the existing terminal/status rules — that's allowed.
    assert fire["attributes"]["intensity"] == "Low"


def test_out_of_order_person_position_not_regressed():
    """Person position must also not be rolled back by an older-step observation.

    Expected to FAIL until Phase 2 adds the step guard.
    """
    store = SemanticMapStore()
    # Step 8 observation arrives first
    store.ingest_observation({
        "reporter": "Alice",
        "step": 8,
        "object_type": "person",
        "name": "Timmy",
        "position": [10, 10, 0],
        "attributes": {"status": "trapped"},
    })
    # Step 5 observation arrives later (out of order)
    store.ingest_observation({
        "reporter": "Bob",
        "step": 5,
        "object_type": "person",
        "name": "Timmy",
        "position": [2, 2, 0],
        "attributes": {"status": "trapped"},
    })

    person = store.snapshot()["known_dynamic_objects"]["persons"][0]
    # Phase 2 contract: step 8 position must survive
    assert person["position"] == [10, 10, 0], (
        f"Expected position [10,10,0] (step 8), got {person['position']}"
    )


def test_out_of_order_agent_position_not_regressed():
    """Agent last_position must not be rolled back by an older-step observation.

    Expected to FAIL until Phase 2 adds the step guard.
    """
    store = SemanticMapStore()
    store.init_priors(
        reservoirs=[{"name": "R1", "position": [0, 0, 0]}],
        deposits=[],
        agents=[{"agent_id": "Bob"}],
        rules={},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="test",
    )
    # Bob reports his position at step 8
    store.ingest_observation({
        "reporter": "Bob",
        "step": 8,
        "object_type": "agent",
        "name": "Bob",
        "position": [10, 10, 0],
        "note": "at scene",
    })
    # An older observation arrives later (step 5)
    store.ingest_observation({
        "reporter": "Bob",
        "step": 5,
        "object_type": "agent",
        "name": "Bob",
        "position": [2, 2, 0],
        "note": "en route",
    })

    agent = store.agents.get("Bob")
    assert agent is not None
    # Phase 2 contract: step 8 position must survive
    assert agent.last_position == (10, 10, 0), (
        f"Expected last_position (10,10,0) (step 8), got {agent.last_position}"
    )
    # last_seen_step should stay at max (8)
    assert agent.last_seen_step == 8


# ── Phase 0 contract tests: revision tracking ──────────────────────────

def test_snapshot_with_revision_method_exists():
    """snapshot_with_revision() is a Phase 2 addition — not yet implemented.

    Expected to FAIL with AttributeError because the method does not exist.
    """
    store = SemanticMapStore()
    result = store.snapshot_with_revision()
    assert isinstance(result, tuple), "Expected tuple[int, dict]"
    assert len(result) == 2
    rev, snap = result
    assert isinstance(rev, int)
    assert isinstance(snap, dict)


def test_revision_bumps_after_same_step_ingest():
    """Ingesting a new observation at the same env_step MUST bump revision.

    Expected to FAIL until Phase 2 implements snapshot_with_revision().
    """
    store = SemanticMapStore()
    store.update_step_budget(current_step=5, max_steps=50)

    store.ingest_observation({
        "reporter": "Alice",
        "step": 5,
        "object_type": "fire",
        "name": "FireA",
        "position": [1, 1, 0],
        "attributes": {"status": "active"},
    })
    rev1, _ = store.snapshot_with_revision()

    store.ingest_observation({
        "reporter": "Bob",
        "step": 5,
        "object_type": "fire",
        "name": "FireB",
        "position": [2, 2, 0],
        "attributes": {"status": "active"},
    })
    rev2, snap2 = store.snapshot_with_revision()

    assert rev2 > rev1, (
        f"Expected revision to increase after same-step ingest, got {rev1} -> {rev2}"
    )
    names = {f["name"] for f in snap2["known_dynamic_objects"]["fires"]}
    assert "FireB" in names


def test_revision_bumps_after_init_priors():
    """init_priors with valid data MUST bump revision from 0.

    Expected to FAIL until Phase 2 implements snapshot_with_revision() and
    init_priors bumps revision.
    """
    store = SemanticMapStore()
    rev0, _ = store.snapshot_with_revision()
    assert rev0 == 0

    store.init_priors(
        reservoirs=[{"name": "ReservoirYork", "position": [1, 2, 0], "resource_type": "Water"}],
        deposits=[{"name": "DepositA", "position": [3, 4, 0], "inventory": {}}],
        agents=[{"agent_id": "Alice"}],
        rules={"Chemical": "Sand"},
        step_budget={"current_step": 0, "max_steps": 120, "remaining": 120},
        task_objective="Extinguish all fires",
    )
    rev1, snap1 = store.snapshot_with_revision()

    assert rev1 > rev0, (
        f"Expected revision to increase after init_priors, got {rev0} -> {rev1}"
    )
    assert len(snap1["known_priors"]["reservoirs"]) == 1
    assert len(snap1["known_priors"]["deposits"]) == 1


def test_revision_bumps_after_observation():
    """Ingesting a valid observation MUST bump revision.

    Expected to FAIL until Phase 2 implements snapshot_with_revision().
    """
    store = SemanticMapStore()
    store.update_step_budget(current_step=3, max_steps=50)
    rev0, _ = store.snapshot_with_revision()

    store.ingest_observation({
        "reporter": "Alice",
        "step": 3,
        "object_type": "fire",
        "name": "FireX",
        "position": [4, 5, 0],
        "attributes": {"status": "active"},
    })
    rev1, _ = store.snapshot_with_revision()

    assert rev1 > rev0, (
        f"Expected revision to increase after observation, got {rev0} -> {rev1}"
    )


def test_revision_bumps_after_value_changing_step_budget():
    """update_step_budget with actually-different values MUST bump revision.

    Expected to FAIL until Phase 2 implements snapshot_with_revision().
    """
    store = SemanticMapStore()
    rev0, _ = store.snapshot_with_revision()

    store.update_step_budget(current_step=10, max_steps=120)
    rev1, _ = store.snapshot_with_revision()

    assert rev1 > rev0, (
        f"Expected revision to increase after step-budget change, got {rev0} -> {rev1}"
    )


def test_revision_unchanged_when_same_step_budget_value():
    """update_step_budget with same values MUST NOT bump revision.

    Expected to FAIL until Phase 2 implements snapshot_with_revision() and
    only bumps when values actually change.
    """
    store = SemanticMapStore()
    store.update_step_budget(current_step=5, max_steps=50)
    rev1, _ = store.snapshot_with_revision()

    # Call with same values — should not bump
    store.update_step_budget(current_step=5, max_steps=50)
    rev2, _ = store.snapshot_with_revision()

    assert rev2 == rev1, (
        f"Expected revision unchanged after same step-budget, got {rev1} -> {rev2}"
    )


def test_snapshot_with_revision_atomic_contains_new_object():
    """snapshot_with_revision() must atomically return revision + snapshot
    containing the newly ingested object within the same lock acquisition.

    Expected to FAIL until Phase 2.
    """
    store = SemanticMapStore()
    store.update_step_budget(current_step=7, max_steps=50)

    store.ingest_observation({
        "reporter": "Alice",
        "step": 7,
        "object_type": "person",
        "name": "Jane",
        "position": [8, 8, 0],
        "attributes": {"status": "trapped"},
    })
    rev, snap = store.snapshot_with_revision()

    assert isinstance(rev, int)
    assert rev > 0
    names = {p["name"] for p in snap["known_dynamic_objects"]["persons"]}
    assert "Jane" in names, (
        f"Snapshot should contain newly ingested person 'Jane', got {names}"
    )


def test_snapshot_equals_snapshot_with_revision_content() -> None:
    """Compatibility contract: snapshot() content must equal the dict from
    snapshot_with_revision()[1] once the latter is available.

    This prevents a Phase 2 implementation from adding snapshot_with_revision()
    as a new code path that diverges from the existing snapshot().

    Expected to FAIL until Phase 2 implements snapshot_with_revision().
    """
    store = SemanticMapStore()
    store.update_step_budget(current_step=5, max_steps=50)

    store.ingest_observation({
        "reporter": "Alice",
        "step": 5,
        "object_type": "fire",
        "name": "FireA",
        "position": [1, 1, 0],
        "attributes": {"status": "active"},
    })

    plain = store.snapshot()
    rev, with_rev = store.snapshot_with_revision()

    assert isinstance(rev, int)
    assert isinstance(with_rev, dict)
    # The dict from snapshot_with_revision()[1] must equal snapshot()
    assert with_rev == plain, (
        "snapshot_with_revision()[1] must equal snapshot() — "
        "the two APIs must return identical data"
    )


# ── Phase 1 legacy compatibility ─────────────────────────────────────────

def test_legacy_semantic_map_shim_exports_identical_symbols():
    """Legacy sar_orch.semantic_map shim must re-export the same class objects
    as the new sar_orch.map.store module (Phase 1 migration compatibility)."""
    from sar_orch.map.store import (
        AgentSemanticState as NewAgentSemanticState,
        ObservationRecord as NewObservationRecord,
        SemanticMapStore as NewSemanticMapStore,
        SemanticObject as NewSemanticObject,
        TERMINAL_STATUS_ORDER as NewTerminalStatusOrder,
    )
    from sar_orch.semantic_map import (
        AgentSemanticState as LegacyAgentSemanticState,
        ObservationRecord as LegacyObservationRecord,
        SemanticMapStore as LegacySemanticMapStore,
        SemanticObject as LegacySemanticObject,
        TERMINAL_STATUS_ORDER as LegacyTerminalStatusOrder,
    )
    assert LegacyAgentSemanticState is NewAgentSemanticState
    assert LegacyObservationRecord is NewObservationRecord
    assert LegacySemanticMapStore is NewSemanticMapStore
    assert LegacySemanticObject is NewSemanticObject
    assert LegacyTerminalStatusOrder is NewTerminalStatusOrder


def test_legacy_observation_publisher_shim_exports_identical_symbol():
    """Legacy sar_orch.observation_publisher shim must re-export the same
    class object as the new sar_orch.map.publisher module."""
    from sar_orch.map.publisher import WorkerReportPublisher as NewPublisher
    from sar_orch.observation_publisher import WorkerReportPublisher as LegacyPublisher

    assert LegacyPublisher is NewPublisher
