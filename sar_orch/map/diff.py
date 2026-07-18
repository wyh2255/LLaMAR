"""Pure-function MapDiffCalculator for semantic-map snapshots.

Phase 3 -- compares two plain snapshot dicts and produces a structured delta
dict with stable projection rules.  No I/O, no barrier state, no LLM calls.
"""

from __future__ import annotations

from typing import Any


class MapDiffCalculator:
    """Pure diff calculator for SemanticMapStore snapshots.

    Compares two snapshot dicts (fires, persons, conflicts, stale_entries)
    and produces a deterministic, structured delta dict.  No instance state.
    """

    @staticmethod
    def diff(
        prev: dict[str, Any] | None,
        curr: dict[str, Any],
        *,
        base_revision: int = 0,
        revision: int = 1,
    ) -> dict[str, Any] | None:
        """Compute structured delta between two snapshots.

        Args:
            prev: Previous snapshot dict, or *None* for a baseline (returns None).
            curr: Current snapshot dict.
            base_revision: Revision associated with *prev* when available.
            revision: Revision associated with *curr* when available.

        Returns:
            Full delta dict per the Phase 3 schema, or None when *prev* is
            None (baseline) or no semantic changes are detected.
        """
        if prev is None:
            return None

        env_step = int(curr.get("step_budget", {}).get("current_step", 0))

        # Index objects by (object_type, name) for stable alignment.
        prev_fires = _index_objects(prev, "fires")
        curr_fires = _index_objects(curr, "fires")
        prev_persons = _index_objects(prev, "persons")
        curr_persons = _index_objects(curr, "persons")

        fires_delta = _compute_category_delta(
            prev_fires, curr_fires, object_type="fire"
        )
        persons_delta = _compute_category_delta(
            prev_persons, curr_persons, object_type="person"
        )

        # Conflict set diff
        prev_conflict_keys = _identities_from_list(prev, "conflicts")
        curr_conflict_keys = _identities_from_list(curr, "conflicts")
        conflicts_new = [
            {"name": name}
            for _, name in sorted(curr_conflict_keys - prev_conflict_keys)
        ]
        conflicts_resolved = [
            {"name": name}
            for _, name in sorted(prev_conflict_keys - curr_conflict_keys)
        ]

        # Stale set diff
        prev_stale_by_identity = _entries_by_identity(prev, "stale_entries")
        curr_stale_by_identity = _entries_by_identity(curr, "stale_entries")
        stale_new_keys = sorted(
            curr_stale_by_identity.keys() - prev_stale_by_identity.keys()
        )
        stale_resolved_keys = sorted(
            prev_stale_by_identity.keys() - curr_stale_by_identity.keys()
        )
        stale_new = [curr_stale_by_identity[key] for key in stale_new_keys]
        stale_resolved = [{"name": name} for _, name in stale_resolved_keys]

        # Early exit when nothing changed
        change_count = _count_changes(
            fires_delta,
            persons_delta,
            conflicts_new,
            conflicts_resolved,
            stale_new,
            stale_resolved,
        )
        if change_count == 0:
            return None

        return {
            "env_step": env_step,
            "base_revision": base_revision,
            "revision": revision,
            "change_count": change_count,
            "fires": fires_delta,
            "persons": persons_delta,
            "conflicts_new": conflicts_new,
            "conflicts_resolved": conflicts_resolved,
            "stale_new": stale_new,
            "stale_resolved": stale_resolved,
        }


# ── internal helpers ──────────────────────────────────────────────────────


def _index_objects(
    snapshot: dict[str, Any],
    category: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return ``{(object_type, name): object_dict}`` for *category*."""
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for obj in snapshot.get("known_dynamic_objects", {}).get(category, []):
        key = (obj.get("object_type", ""), obj.get("name", ""))
        indexed[key] = obj
    return indexed


def _compute_category_delta(
    prev: dict[tuple[str, str], dict[str, Any]],
    curr: dict[tuple[str, str], dict[str, Any]],
    *,
    object_type: str,
) -> dict[str, list[dict[str, Any]]]:
    """Delta for one object category (fires or persons).

    Returns a dict with ``gained``, ``lost``, ``status_changed``,
    ``position_changed``, ``attributes_changed``, and for fires also
    ``intensity_changed``.
    """
    gained: list[dict[str, Any]] = []
    intensity_changed: list[dict[str, Any]] = []
    status_changed: list[dict[str, Any]] = []
    position_changed: list[dict[str, Any]] = []
    attributes_changed: list[dict[str, Any]] = []

    prev_keys = set(prev.keys())
    curr_keys = set(curr.keys())

    # Gained objects
    for key in sorted(curr_keys - prev_keys):
        obj = curr[key]
        name = obj.get("name", "")
        if object_type == "fire":
            gained.append(
                {
                    "name": name,
                    "position": obj.get("position"),
                    "intensity": obj.get("attributes", {}).get("intensity"),
                }
            )
        else:
            gained.append(
                {
                    "name": name,
                    "position": obj.get("position"),
                    "status": obj.get("status"),
                }
            )

    # Objects present in both snapshots
    for key in sorted(prev_keys & curr_keys):
        p = prev[key]
        c = curr[key]
        name = p.get("name", "")

        # Intensity change (fire only)
        if object_type == "fire":
            p_int = p.get("attributes", {}).get("intensity")
            c_int = c.get("attributes", {}).get("intensity")
            if p_int != c_int:
                intensity_changed.append(
                    {"name": name, "old": p_int, "new": c_int}
                )

        # Top-level status change
        p_status = p.get("status")
        c_status = c.get("status")
        if p_status != c_status:
            status_changed.append(
                {"name": name, "old": p_status, "new": c_status}
            )

        # Position change
        p_pos = p.get("position")
        c_pos = c.get("position")
        if p_pos != c_pos:
            position_changed.append(
                {"name": name, "old": p_pos, "new": c_pos}
            )

        # Attributes change — exclude noise fields and double-counted keys
        excluded: set[str] = {"status", "observed_cells"}
        if object_type == "fire":
            excluded.add("intensity")
        p_attrs = {
            k: v
            for k, v in p.get("attributes", {}).items()
            if k not in excluded
        }
        c_attrs = {
            k: v
            for k, v in c.get("attributes", {}).items()
            if k not in excluded
        }
        all_keys = set(p_attrs.keys()) | set(c_attrs.keys())
        for attr_key in sorted(all_keys):
            p_val = p_attrs.get(attr_key)
            c_val = c_attrs.get(attr_key)
            if p_val != c_val:
                attributes_changed.append(
                    {"name": name, "key": attr_key, "old": p_val, "new": c_val}
                )

    result: dict[str, list[dict[str, Any]]] = {
        "gained": gained,
        "lost": [],
        "status_changed": status_changed,
        "position_changed": position_changed,
        "attributes_changed": attributes_changed,
    }
    if object_type == "fire":
        result["intensity_changed"] = intensity_changed
    return result


def _identities_from_list(
    snapshot: dict[str, Any],
    key: str,
) -> set[tuple[str, str]]:
    """Return ``(object_type, name)`` identities from a snapshot list."""
    return {
        (item.get("object_type", ""), item.get("name", ""))
        for item in snapshot.get(key, [])
    }


def _entries_by_identity(
    snapshot: dict[str, Any],
    key: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return ``{(object_type, name): entry}`` for a snapshot list."""
    return {
        (item.get("object_type", ""), item.get("name", "")): item
        for item in snapshot.get(key, [])
    }


def _count_changes(
    fires_delta: dict[str, list[dict[str, Any]]],
    persons_delta: dict[str, list[dict[str, Any]]],
    conflicts_new: list[dict[str, Any]],
    conflicts_resolved: list[dict[str, Any]],
    stale_new: list[dict[str, Any]],
    stale_resolved: list[dict[str, Any]],
) -> int:
    """Count all semantic object deltas and stale/conflict events.

    Never counts agent movement (agents are excluded from all category
    deltas).
    """
    total = 0
    for cat_delta in (fires_delta, persons_delta):
        total += len(cat_delta.get("gained", []))
        # lost is reserved -- always empty, not counted
        total += len(cat_delta.get("intensity_changed", []))
        total += len(cat_delta.get("status_changed", []))
        total += len(cat_delta.get("position_changed", []))
        total += len(cat_delta.get("attributes_changed", []))
    total += len(conflicts_new)
    total += len(conflicts_resolved)
    total += len(stale_new)
    total += len(stale_resolved)
    return total
