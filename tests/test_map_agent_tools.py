"""Contract tests for Map Agent tools (Phase A).

These tests verify the expected contract between Map Agent MCP tools
and their callers (worker agents).  Each test sets up a SemanticMapStore
with known priors + observations and exercises MapAgentTools methods.
"""

import pytest

from sar_orch.map import SemanticMapStore
from sar_orch.map_agent.tools import MapAgentTools


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def populated_store() -> SemanticMapStore:
    """Return a SemanticMapStore populated with realistic SAR data."""
    store = SemanticMapStore()
    store.init_priors(
        reservoirs=[
            {
                "name": "ReservoirYork",
                "position": [1, 2, 0],
                "resource_type": "Water",
                "supply_type": "water",
            },
            {
                "name": "QuarryPond",
                "position": [10, 10, 0],
                "resource_type": "Sand",
                "supply_type": "sand",
            },
            {
                "name": "LakeTahoe",
                "position": [3, 20, 0],
                "resource_type": "Water",
                "supply_type": "water",
            },
        ],
        deposits=[
            {
                "name": "SupplyDepotA",
                "position": [5, 5, 0],
                "inventory": {"blanket": 10, "medkit": 5},
            },
            {
                "name": "SupplyDepotB",
                "position": [20, 3, 0],
                "inventory": {"blanket": 5, "medkit": 2},
            },
        ],
        agents=[
            {"agent_id": "Alice", "name": "Alice"},
            {"agent_id": "Bob", "name": "Bob"},
            {"agent_id": "Charlie", "name": "Charlie"},
        ],
        rules={"Chemical": "Sand", "Electric": "CO2", "ClassA": "Water"},
        step_budget={"current_step": 10, "max_steps": 120, "remaining": 110},
        task_objective="Extinguish all fires and rescue all persons",
    )
    # Ingest fire observations
    store.ingest_observation({
        "reporter": "Alice",
        "step": 5,
        "object_type": "fire",
        "name": "CaldorFire_Region_1",
        "position": [4, 4, 0],
        "attributes": {
            "fire_type": "Chemical",
            "intensity": "High",
            "status": "active",
            "parent_fire": "CaldorFire",
        },
    })
    store.ingest_observation({
        "reporter": "Bob",
        "step": 7,
        "object_type": "fire",
        "name": "CaldorFire_Region_2",
        "position": [5, 5, 0],
        "attributes": {
            "fire_type": "Chemical",
            "intensity": "Medium",
            "status": "active",
            "parent_fire": "CaldorFire",
        },
    })
    store.ingest_observation({
        "reporter": "Charlie",
        "step": 3,
        "object_type": "fire",
        "name": "MosquitoFire",
        "position": [15, 8, 0],
        "attributes": {
            "fire_type": "ClassA",
            "intensity": "Low",
            "status": "active",
        },
    })
    # Ingest person observations
    store.ingest_observation({
        "reporter": "Alice",
        "step": 6,
        "object_type": "person",
        "name": "Person1",
        "position": [2, 3, 0],
        "attributes": {"status": "trapped"},
    })
    store.ingest_observation({
        "reporter": "Bob",
        "step": 8,
        "object_type": "person",
        "name": "Person2",
        "position": [8, 12, 0],
        "attributes": {"status": "safe"},
    })
    store.ingest_observation({
        "reporter": "Charlie",
        "step": 4,
        "object_type": "person",
        "name": "Person3",
        "position": [18, 6, 0],
        "attributes": {"status": "trapped"},
    })
    return store


@pytest.fixture
def tools(populated_store: SemanticMapStore) -> MapAgentTools:
    return MapAgentTools(populated_store)


@pytest.fixture
def empty_tools() -> MapAgentTools:
    return MapAgentTools(SemanticMapStore())


# ── get_fire_info ────────────────────────────────────────────────────────────

class TestGetFireInfo:
    """Contract: get_fire_info returns a trimmed fire record suitable for workers."""

    def test_returns_trimmed_fields(self, tools: MapAgentTools):
        """Trimmed fire MUST exclude internal metadata fields."""
        result = tools.get_fire_info(fire_name="CaldorFire")
        assert "sources" not in result, "sources must be stripped"
        assert "confidence" not in result, "confidence must be stripped"
        assert "last_seen_ts" not in result, "last_seen_ts must be stripped"
        assert "recent_observations" not in result, "recent_observations must be stripped"
        # Derived / preserved fields that MUST be present
        assert "fire_type" in result, "fire_type must be present"
        assert "required_supply" in result, "required_supply must be present"
        assert "regions" in result, "regions (from observed_cells) must be present"
        assert "position" in result, "position must be present"

    def test_returns_correct_fire_type_and_supply(self, tools: MapAgentTools):
        """Chemical fire should map to Sand from rules."""
        result = tools.get_fire_info(fire_name="CaldorFire")
        assert result.get("fire_type") == "Chemical"
        assert result.get("required_supply") == "Sand"

    def test_returns_expanded_regions(self, tools: MapAgentTools):
        """observed_cells should be expanded into regions list."""
        result = tools.get_fire_info(fire_name="CaldorFire")
        regions = result.get("regions", [])
        assert len(regions) >= 2, f"Expected >=2 regions for CaldorFire, got {len(regions)}"
        region_names = {r["name"] for r in regions}
        assert "CaldorFire_Region_1" in region_names
        assert "CaldorFire_Region_2" in region_names

    def test_unknown_fire_returns_empty(self, tools: MapAgentTools):
        """Unknown fire name MUST return empty dict."""
        result = tools.get_fire_info(fire_name="NonExistentFire_XYZ")
        assert result == {}, f"Expected empty dict, got {result}"

    def test_missing_name_returns_first_fire(self, tools: MapAgentTools):
        """When fire_name is None, return first available fire (or empty)."""
        result = tools.get_fire_info(fire_name=None)
        assert isinstance(result, dict)
        assert result != {}, "Should return first fire when name is None"
        assert "fire_type" in result

    def test_includes_nearest_reservoir_with_sand(self, tools: MapAgentTools):
        """nearest_reservoir_with_sand MUST be computed from reservoir list."""
        result = tools.get_fire_info(fire_name="CaldorFire")
        nearest = result.get("nearest_reservoir_with_sand")
        assert nearest is not None, "nearest_reservoir_with_sand must be present"
        assert "name" in nearest
        assert "position" in nearest
        assert "distance" in nearest
        # QuarryPond (sand) is at (10,10,0), CaldorFire at (4,4,0) approx → distance ~8.49
        assert nearest["name"] == "QuarryPond"

    def test_empty_store_returns_empty(self, empty_tools: MapAgentTools):
        """When the store is empty, get_fire_info returns {}."""
        result = empty_tools.get_fire_info(fire_name="CaldorFire")
        assert result == {}


# ── get_person_info ─────────────────────────────────────────────────────────

class TestGetPersonInfo:
    """Contract: get_person_info returns trimmed person data for rescue coordination."""

    def test_returns_carriers_and_nearest_deposit(self, tools: MapAgentTools):
        """Person result MUST include status, carriers, and nearest_deposit."""
        result = tools.get_person_info(person_name="Person1")
        assert "status" in result, "status must be present"
        assert "carriers" in result, "carriers must be present"
        assert "nearest_deposit" in result, "nearest_deposit must be present"
        assert "position" in result, "position must be present"

    def test_returns_trapped_status(self, tools: MapAgentTools):
        """Person1 should have 'trapped' status."""
        result = tools.get_person_info(person_name="Person1")
        assert result.get("status") == "trapped"

    def test_carriers_are_sorted_by_distance(self, tools: MapAgentTools):
        """Carriers list should be sorted nearest-first."""
        result = tools.get_person_info(person_name="Person1")
        carriers = result.get("carriers", [])
        assert len(carriers) >= 2, f"Expected at least 2 carriers, got {len(carriers)}"
        # Person1 is at (2,3,0), so Alice (default pos) should be first
        distances = [c["distance"] for c in carriers]
        assert distances == sorted(distances), "Carriers must be sorted by distance"

    def test_unknown_person_returns_empty(self, tools: MapAgentTools):
        """Unknown person name MUST return empty dict."""
        result = tools.get_person_info(person_name="NonExistentPerson_XYZ")
        assert result == {}

    def test_missing_name_returns_first_person(self, tools: MapAgentTools):
        """When person_name is None, return first available person."""
        result = tools.get_person_info(person_name=None)
        assert isinstance(result, dict)
        assert result != {}
        assert "status" in result

    def test_empty_store_returns_empty(self, empty_tools: MapAgentTools):
        """When the store is empty, get_person_info returns {}."""
        result = empty_tools.get_person_info(person_name="Person1")
        assert result == {}


# ── get_reservoir_info ───────────────────────────────────────────────────────

class TestGetReservoirInfo:
    """Contract: get_reservoir_info returns a list of reservoirs filtered by supply type."""

    def test_filter_by_supply_type(self, tools: MapAgentTools):
        """When supply_type is given, all returned reservoirs MUST match."""
        result = tools.get_reservoir_info(supply_type="sand")
        for r in result.get("reservoirs", []):
            attrs = r.get("attributes", {})
            assert attrs.get("supply_type") == "sand", (
                f"Expected supply_type='sand', got '{attrs.get('supply_type')}'"
            )

    def test_filter_returns_correct_count(self, tools: MapAgentTools):
        """There should be exactly 1 sand reservoir."""
        result = tools.get_reservoir_info(supply_type="sand")
        assert len(result["reservoirs"]) == 1
        assert result["reservoirs"][0]["name"] == "QuarryPond"

    def test_no_filter_returns_all(self, tools: MapAgentTools):
        """When supply_type is None, return all known reservoirs."""
        result = tools.get_reservoir_info(supply_type=None)
        assert "reservoirs" in result
        assert isinstance(result["reservoirs"], list)
        assert len(result["reservoirs"]) == 3, "Should return all 3 reservoirs"

    def test_no_filter_with_water(self, tools: MapAgentTools):
        """Filter by 'water' returns 2 reservoirs."""
        result = tools.get_reservoir_info(supply_type="water")
        assert len(result["reservoirs"]) == 2

    def test_unknown_supply_type_returns_empty_list(self, tools: MapAgentTools):
        """When no reservoir has the requested supply type, return empty list."""
        result = tools.get_reservoir_info(supply_type="liquid_helium")
        assert result == {"reservoirs": []}

    def test_empty_store_returns_empty_list(self, empty_tools: MapAgentTools):
        """When the store is empty, get_reservoir_info returns empty list."""
        result = empty_tools.get_reservoir_info(supply_type="sand")
        assert result == {"reservoirs": []}


# ── get_task_context ─────────────────────────────────────────────────────────

class TestGetTaskContext:
    """Contract: get_task_context extracts mentioned fire/person names from a task description."""

    def test_extracts_fire_name_from_task(self, tools: MapAgentTools):
        """Task mentioning a fire name MUST return it in mentioned_objects."""
        result = tools.get_task_context(
            task_description="extinguish CaldorFire in sector 7"
        )
        assert "mentioned_objects" in result
        assert any("CaldorFire" in obj for obj in result["mentioned_objects"]), (
            f"CaldorFire not found in {result['mentioned_objects']}"
        )

    def test_extracts_person_name(self, tools: MapAgentTools):
        """Task mentioning a person name MUST return it."""
        result = tools.get_task_context(
            task_description="rescue Person2 from the burning building"
        )
        assert any("Person2" in obj for obj in result["mentioned_objects"]), (
            f"Person2 not found in {result['mentioned_objects']}"
        )

    def test_empty_description_returns_empty_list(self, tools: MapAgentTools):
        """Empty description MUST return empty mentioned_objects list."""
        result = tools.get_task_context(task_description="")
        assert result == {"mentioned_objects": []}

    def test_none_description_returns_empty_list(self, tools: MapAgentTools):
        """None description MUST return empty mentioned_objects list."""
        result = tools.get_task_context(task_description=None)
        assert result == {"mentioned_objects": []}

    def test_multiple_mentioned_objects(self, tools: MapAgentTools):
        """Task referencing multiple objects MUST return all of them."""
        result = tools.get_task_context(
            task_description="help Person3 escape and then extinguish MosquitoFire"
        )
        assert len(result["mentioned_objects"]) >= 2
        names = set(result["mentioned_objects"])
        assert "Person3" in names, f"Person3 not in {names}"
        assert "MosquitoFire" in names, f"MosquitoFire not in {names}"

    def test_empty_store_returns_empty_list(self, empty_tools: MapAgentTools):
        """When the store is empty, get_task_context returns empty list."""
        result = empty_tools.get_task_context(task_description="rescue Person1")
        assert result == {"mentioned_objects": []}
