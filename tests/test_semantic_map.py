from pathlib import Path

from sar_orch.semantic_map import ObservationRecord, SemanticMapStore


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
