"""Tests for MapDiffCalculator — expected to fail until Phase 3.

Phase 0 contract tests: define the MapDiffCalculator API and stable projection
behavior using plain dict snapshots.  The module sar_orch.map.diff does not yet
exist, so every test function is expected to fail with ImportError.

No guard clauses (``if delta is not None``) or weak assertions (``>= 1``) are
used — every assertion is exact about the expected delta shape and values.
"""


def test_map_diff_calculator_module_import():
    """MapDiffCalculator lives in sar_orch.map.diff (not yet created).

    Expected ImportError: sar_orch.map package does not have the diff submodule.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    assert callable(MapDiffCalculator.diff)


def test_map_diff_takes_snapshot_dicts_only():
    """diff() should accept two plain dict snapshots and return a full delta dict.

    The output must carry the full schema prescribed by the plan.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": {
            "fires": [],
            "persons": [],
        },
    }
    curr = {
        "step_budget": {"current_step": 6},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": "F1",
                    "position": [1, 2, 0],
                    "attributes": {"intensity": "Medium", "status": "active"},
                    "object_type": "fire",
                    "conflict": False,
                }
            ],
            "persons": [
                {
                    "name": "P1",
                    "position": [3, 4, 0],
                    "attributes": {"status": "trapped"},
                    "object_type": "person",
                    "conflict": False,
                }
            ],
        },
    }
    delta = MapDiffCalculator.diff(prev, curr)

    # Full schema from the plan
    assert isinstance(delta, dict)
    assert "env_step" in delta
    assert delta["env_step"] == 6
    assert "base_revision" in delta
    assert isinstance(delta["base_revision"], int)
    assert "revision" in delta
    assert isinstance(delta["revision"], int)
    assert "change_count" in delta
    assert isinstance(delta["change_count"], int)

    # Fires sub-structure
    assert "fires" in delta
    for sub_key in ("gained", "lost", "intensity_changed", "status_changed",
                    "position_changed", "attributes_changed"):
        assert sub_key in delta["fires"]
        assert isinstance(delta["fires"][sub_key], list)

    # Persons sub-structure
    assert "persons" in delta
    for sub_key in ("gained", "lost", "status_changed",
                    "position_changed", "attributes_changed"):
        assert sub_key in delta["persons"]
        assert isinstance(delta["persons"][sub_key], list)

    # Conflict / stale
    assert "conflicts_new" in delta
    assert "conflicts_resolved" in delta
    assert "stale_new" in delta
    assert "stale_resolved" in delta
    assert isinstance(delta["conflicts_new"], list)
    assert isinstance(delta["conflicts_resolved"], list)
    assert isinstance(delta["stale_new"], list)
    assert isinstance(delta["stale_resolved"], list)


def test_map_diff_preserves_explicit_revision_metadata() -> None:
    """Callers must preserve the actual compared map revisions."""
    from sar_orch.map.diff import MapDiffCalculator

    prev = _make_snapshot([], [])
    curr = _make_snapshot([("F1", "Medium", "active", [1, 2, 0])], [])

    delta = MapDiffCalculator.diff(
        prev,
        curr,
        base_revision=41,
        revision=42,
    )

    assert delta is not None
    assert delta["base_revision"] == 41
    assert delta["revision"] == 42


def test_map_diff_baseline_returns_none():
    """With no previous snapshot, diff() returns None (not a full 'gained' list)."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    curr = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {"fires": [], "persons": []},
    }
    delta = MapDiffCalculator.diff(None, curr)
    assert delta is None, "Baseline (no previous) should return None"


def test_map_diff_gained_detected():
    """New fires and persons appear in 'gained', change_count reflects them."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([], [])
    curr = _make_snapshot(
        [("F1", "Medium", "active", [1, 2, 0])],
        [("P1", "trapped", [5, 5, 0])],
    )
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["fires"]["gained"] == [
        {"name": "F1", "position": [1, 2, 0], "intensity": "Medium"}
    ]
    assert delta["persons"]["gained"] == [
        {"name": "P1", "position": [5, 5, 0], "status": "trapped"}
    ]
    assert delta["change_count"] == 2


def test_map_diff_orders_multi_object_deltas_deterministically() -> None:
    """Unordered snapshot collections must produce a stable lexical delta order."""
    from sar_orch.map.diff import MapDiffCalculator

    prev = _make_snapshot([], [])
    curr = _make_snapshot(
        [
            ("F2", "Medium", "active", [2, 2, 0]),
            ("F10", "Medium", "active", [10, 10, 0]),
            ("F1", "Medium", "active", [1, 1, 0]),
        ],
        [],
    )

    delta = MapDiffCalculator.diff(prev, curr)

    assert delta is not None
    assert [entry["name"] for entry in delta["fires"]["gained"]] == [
        "F1",
        "F10",
        "F2",
    ]


def test_map_diff_intensity_change_detected():
    """Intensity change is reported in fires.intensity_changed, not attributes."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([("F1", "Medium", "active", [1, 2, 0])], [])
    curr = _make_snapshot([("F1", "High", "active", [1, 2, 0])], [])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["fires"]["intensity_changed"] == [
        {"name": "F1", "old": "Medium", "new": "High"}
    ]
    assert delta["change_count"] == 1


def test_map_diff_fire_status_change_detected():
    """Fire status change is a status_changed, not an attributes change."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([("F1", "Medium", "active", [1, 2, 0])], [])
    curr = _make_snapshot([("F1", "Medium", "extinguished", [1, 2, 0])], [])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["fires"]["status_changed"] == [
        {"name": "F1", "old": "active", "new": "extinguished"}
    ]
    assert delta["change_count"] == 1


def test_map_diff_fire_position_change_detected() -> None:
    """Fire position change is reported in fires.position_changed.

    Expected ImportError until Phase 3.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([("F1", "Medium", "active", [1, 2, 0])], [])
    curr = _make_snapshot([("F1", "Medium", "active", [5, 5, 0])], [])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["fires"]["position_changed"] == [
        {"name": "F1", "old": [1, 2, 0], "new": [5, 5, 0]}
    ]
    assert delta["change_count"] == 1


def test_map_diff_stable_fields_excluded():
    """Noise fields (last_seen_ts, sources, confidence, recent_observations,
    attributes.observed_cells) must NOT trigger any semantic delta.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": "F1",
                    "attributes": {
                        "intensity": "Medium",
                        "status": "active",
                        "observed_cells": [{"name": "cell1"}],
                    },
                    "position": [1, 2, 0],
                    "object_type": "fire",
                    "status": "active",
                    "conflict": False,
                    "last_seen_ts": 100.0,
                    "sources": [{"reporter": "Alice"}],
                    "confidence": 0.8,
                }
            ],
            "persons": [],
        },
        "recent_observations": [{"step": 0}],
    }
    curr = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": "F1",
                    "attributes": {
                        "intensity": "Medium",
                        "status": "active",
                        "observed_cells": [{"name": "cell1"}, {"name": "cell2"}],
                    },
                    "position": [1, 2, 0],
                    "object_type": "fire",
                    "status": "active",
                    "conflict": False,
                    "last_seen_ts": 200.0,
                    "sources": [{"reporter": "Alice"}, {"reporter": "Bob"}],
                    "confidence": 0.9,
                }
            ],
            "persons": [],
        },
        "recent_observations": [{"step": 0}, {"step": 1}],
    }
    delta = MapDiffCalculator.diff(prev, curr)

    # All these fields changed but none should produce a semantic delta
    assert delta is None, (
        "Noise-only changes (last_seen_ts, sources, confidence, "
        "recent_observations, observed_cells) must not produce a delta"
    )


def test_map_diff_person_status_change():
    """Person terminal status change is reported in persons.status_changed."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([], [("P1", "trapped", [5, 5, 0])])
    curr = _make_snapshot([], [("P1", "rescued", [5, 5, 0])])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["persons"]["status_changed"] == [
        {"name": "P1", "old": "trapped", "new": "rescued"}
    ]
    assert delta["change_count"] == 1


def test_map_diff_person_position_change():
    """Person position change is reported in persons.position_changed."""
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([], [("P1", "trapped", [5, 5, 0])])
    curr = _make_snapshot([], [("P1", "trapped", [6, 6, 0])])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["persons"]["position_changed"] == [
        {"name": "P1", "old": [5, 5, 0], "new": [6, 6, 0]}
    ]
    assert delta["change_count"] == 1


def test_map_diff_person_attributes_change_detected() -> None:
    """Person attribute change (non-status) is reported in persons.attributes_changed.

    Expected ImportError until Phase 3.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [],
            "persons": [
                {
                    "name": "P1",
                    "position": [5, 5, 0],
                    "attributes": {"status": "trapped", "note": "ok"},
                    "object_type": "person",
                    "status": "trapped",
                    "conflict": False,
                }
            ],
        },
    }
    curr = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [],
            "persons": [
                {
                    "name": "P1",
                    "position": [5, 5, 0],
                    "attributes": {"status": "trapped", "note": "injured"},
                    "object_type": "person",
                    "status": "trapped",
                    "conflict": False,
                }
            ],
        },
    }
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta["persons"]["attributes_changed"] == [
        {"name": "P1", "key": "note", "old": "ok", "new": "injured"}
    ]
    assert delta["change_count"] == 1


def test_map_diff_filtered_attributes_only():
    """Attributes changes like 'note' or non-status keys appear in
    attributes_changed, not as status or position.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    curr_snap = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": "F1",
                    "position": [1, 2, 0],
                    "attributes": {"intensity": "Medium", "status": "active", "fire_type": "Chemical"},
                    "object_type": "fire",
                    "status": "active",
                    "conflict": False,
                }
            ],
            "persons": [],
        },
    }
    # Change prev to include old fire_type
    prev_snap = {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": "F1",
                    "position": [1, 2, 0],
                    "attributes": {"intensity": "Medium", "status": "active", "fire_type": "Electrical"},
                    "object_type": "fire",
                    "status": "active",
                    "conflict": False,
                }
            ],
            "persons": [],
        },
    }
    delta = MapDiffCalculator.diff(prev_snap, curr_snap)

    assert delta["fires"]["attributes_changed"] == [
        {"name": "F1", "key": "fire_type", "old": "Electrical", "new": "Chemical"}
    ]
    assert delta["change_count"] == 1


def test_map_diff_no_agent_movement():
    """Agent movement must NEVER appear in map_delta, and change_count must
    not count agent position changes.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": {"fires": [], "persons": []},
        "agents": [{"agent_id": "Alice", "last_position": [1, 2, 0]}],
    }
    curr = {
        "step_budget": {"current_step": 6},
        "known_dynamic_objects": {"fires": [], "persons": []},
        "agents": [{"agent_id": "Alice", "last_position": [3, 4, 0]}],
    }
    delta = MapDiffCalculator.diff(prev, curr)

    # Agent-only change — delta should be None (no semantic changes)
    assert delta is None, "Agent-only changes must not produce a delta"


def test_map_diff_stale_new_and_resolved() -> None:
    """Stale entries: stale_new when newly stale, stale_resolved when resolved.

    Two-phase assertion — not any().
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    # Phase 1: prev (no stale) -> curr (with stale)
    no_stale = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": {
            "fires": [{"name": "OldFire", "last_seen_step": 3, "object_type": "fire"}],
            "persons": [],
        },
        "stale_entries": [],
        "conflicts": [],
    }
    with_stale = {
        "step_budget": {"current_step": 10},
        "known_dynamic_objects": {
            "fires": [{"name": "OldFire", "last_seen_step": 3, "object_type": "fire"}],
            "persons": [],
        },
        "stale_entries": [{"name": "OldFire", "last_seen_step": 3}],
        "conflicts": [],
    }
    delta_new = MapDiffCalculator.diff(no_stale, with_stale)
    assert delta_new["stale_new"] == [{"name": "OldFire", "last_seen_step": 3}], (
        f"Expected stale_new=[{{\"name\": \"OldFire\", \"last_seen_step\": 3}}], "
        f"got {delta_new['stale_new']}"
    )
    assert delta_new["stale_resolved"] == [], (
        f"Expected stale_resolved=[], got {delta_new['stale_resolved']}"
    )

    # Phase 2: prev (with stale) -> curr (without stale)
    without_stale = {
        "step_budget": {"current_step": 15},
        "known_dynamic_objects": {
            "fires": [{"name": "OldFire", "last_seen_step": 14, "object_type": "fire"}],
            "persons": [],
        },
        "stale_entries": [],
        "conflicts": [],
    }
    delta_resolved = MapDiffCalculator.diff(with_stale, without_stale)
    assert delta_resolved["stale_new"] == [], (
        f"Expected stale_new=[], got {delta_resolved['stale_new']}"
    )
    assert delta_resolved["stale_resolved"] == [{"name": "OldFire"}], (
        f"Expected stale_resolved=[{{\"name\": \"OldFire\"}}], "
        f"got {delta_resolved['stale_resolved']}"
    )


def test_map_diff_conflict_new_and_resolved() -> None:
    """Conflicts appearing and resolving — two-phase assertion, strong exact match.

    Phase 1: prev (no conflict) -> curr (with conflict): conflicts_new has entry.
    Phase 2: prev (with conflict) -> curr (without conflict): conflicts_resolved has entry.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    # Phase 1: no conflict -> with conflict
    no_conflict = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": {
            "fires": [{"name": "F1", "object_type": "fire", "conflict": False}],
            "persons": [],
        },
        "conflicts": [],
    }
    with_conflict = {
        "step_budget": {"current_step": 6},
        "known_dynamic_objects": {
            "fires": [{"name": "F1", "object_type": "fire", "conflict": True}],
            "persons": [],
        },
        "conflicts": [{"name": "F1", "object_type": "fire"}],
    }
    delta_new = MapDiffCalculator.diff(no_conflict, with_conflict)
    assert delta_new["conflicts_new"] == [{"name": "F1"}], (
        f"Expected conflicts_new=[{{\"name\": \"F1\"}}], "
        f"got {delta_new['conflicts_new']}"
    )
    assert delta_new["conflicts_resolved"] == [], (
        f"Expected conflicts_resolved=[], got {delta_new['conflicts_resolved']}"
    )

    # Phase 2: with conflict -> without conflict
    without_conflict = {
        "step_budget": {"current_step": 7},
        "known_dynamic_objects": {
            "fires": [{"name": "F1", "object_type": "fire", "conflict": False}],
            "persons": [],
        },
        "conflicts": [],
    }
    delta_resolved = MapDiffCalculator.diff(with_conflict, without_conflict)
    assert delta_resolved["conflicts_new"] == [], (
        f"Expected conflicts_new=[], got {delta_resolved['conflicts_new']}"
    )
    assert delta_resolved["conflicts_resolved"] == [{"name": "F1"}], (
        f"Expected conflicts_resolved=[{{\"name\": \"F1\"}}], "
        f"got {delta_resolved['conflicts_resolved']}"
    )


def test_map_diff_conflicts_are_aligned_by_object_type_and_name() -> None:
    """A same-named fire conflict differs from an existing person conflict."""
    from sar_orch.map.diff import MapDiffCalculator

    shared_objects = {
        "fires": [{"name": "Alex", "object_type": "fire", "conflict": False}],
        "persons": [{"name": "Alex", "object_type": "person", "conflict": False}],
    }
    prev = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": shared_objects,
        "conflicts": [{"name": "Alex", "object_type": "person"}],
    }
    curr = {
        "step_budget": {"current_step": 6},
        "known_dynamic_objects": shared_objects,
        "conflicts": [
            {"name": "Alex", "object_type": "person"},
            {"name": "Alex", "object_type": "fire"},
        ],
    }

    delta = MapDiffCalculator.diff(prev, curr)

    assert delta is not None
    assert delta["conflicts_new"] == [{"name": "Alex"}]
    assert delta["change_count"] == 1


def test_map_diff_stale_entries_are_aligned_by_object_type_and_name() -> None:
    """A same-named fire stale entry differs from an existing person entry."""
    from sar_orch.map.diff import MapDiffCalculator

    shared_objects = {
        "fires": [{"name": "Alex", "object_type": "fire"}],
        "persons": [{"name": "Alex", "object_type": "person"}],
    }
    prev = {
        "step_budget": {"current_step": 5},
        "known_dynamic_objects": shared_objects,
        "stale_entries": [
            {"name": "Alex", "object_type": "person", "last_seen_step": 3}
        ],
    }
    curr = {
        "step_budget": {"current_step": 6},
        "known_dynamic_objects": shared_objects,
        "stale_entries": [
            {"name": "Alex", "object_type": "person", "last_seen_step": 3},
            {"name": "Alex", "object_type": "fire", "last_seen_step": 2},
        ],
    }

    delta = MapDiffCalculator.diff(prev, curr)

    assert delta is not None
    assert delta["stale_new"] == [
        {"name": "Alex", "object_type": "fire", "last_seen_step": 2}
    ]
    assert delta["change_count"] == 1


def test_map_diff_lost_is_reserved() -> None:
    """'lost' field exists and is empty even when other semantic changes occur.

    Uses a non-None delta (intensity change) to assert lost == [] explicitly.
    """
    from sar_orch.map.diff import MapDiffCalculator  # ImportError expected

    prev = _make_snapshot([("F1", "Medium", "active", [1, 2, 0])], [])
    curr = _make_snapshot([("F1", "High", "active", [1, 2, 0])], [])
    delta = MapDiffCalculator.diff(prev, curr)

    assert delta is not None, "Intensity change must produce a delta"
    assert delta["fires"]["lost"] == [], (
        f"Expected lost to be [], got {delta['fires']['lost']}"
    )
    assert delta["persons"]["lost"] == []
    assert delta["change_count"] == 1


# ── helpers ───────────────────────────────────────────────────────────

def _make_snapshot(
    fires: list[tuple],
    persons: list[tuple],
    extra_fire_attrs: dict | None = None,
) -> dict:
    """Build a minimal semantic-map snapshot dict."""
    return {
        "step_budget": {"current_step": 0},
        "known_dynamic_objects": {
            "fires": [
                {
                    "name": name,
                    "attributes": {
                        "intensity": intensity,
                        "status": status,
                        **(extra_fire_attrs or {}),
                    },
                    "position": list(pos),
                    "object_type": "fire",
                    "status": status,
                    "conflict": False,
                }
                for name, intensity, status, pos in fires
            ],
            "persons": [
                {
                    "name": name,
                    "attributes": {"status": status},
                    "position": list(pos),
                    "object_type": "person",
                    "status": status,
                    "conflict": False,
                }
                for name, status, pos in persons
            ],
        },
    }
