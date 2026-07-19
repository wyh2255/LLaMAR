"""Map Agent tools — trim SemanticMapStore snapshots into worker-friendly formats.

Each tool queries a SemanticMapStore snapshot and returns a structured
dict suitable for LLM consumption by a worker agent.  Internal metadata
(sources, confidence, timestamps) are stripped; derived fields
(nearest_reservoir_with_sand, carriers, mentioned_objects) are computed.
"""

from __future__ import annotations

import math
import re
from typing import Any

from sar_orch.map import SemanticMapStore


def _euc_3d(
    a: list[float] | tuple[float, ...] | None, b: list[float] | tuple[float, ...] | None
) -> float:
    """Euclidean distance between two 3D points.  Returns inf if either is missing."""
    if a is None or b is None:
        return float("inf")
    return math.sqrt(sum((float(ai) - float(bi)) ** 2 for ai, bi in zip(a[:3], b[:3])))


def _trim_semantic_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Remove internal metadata fields from a SemanticObject dict."""
    return {
        k: v
        for k, v in obj.items()
        if k
        not in (
            "sources",
            "confidence",
            "last_seen_ts",
            "last_seen_step",
            "recent_observations",
            "object_type",
            "conflict",
        )
    }


class MapAgentTools:
    """Bundled Map Agent query tools backed by a SemanticMapStore.

    Usage::

        tools = MapAgentTools(store)
        fire_info = tools.get_fire_info("CaldorFire")
    """

    def __init__(self, store: SemanticMapStore) -> None:
        self._store = store

    # ── public API ───────────────────────────────────────────────────────────

    def get_fire_info(self, fire_name: str | None = None) -> dict[str, Any]:
        """Get fire-suppression info for *fire_name* (trimmed).

        Returns {} for unknown names.  When *fire_name* is None returns the
        first known fire (or {} if none).
        """
        snapshot = self._store.snapshot()
        fires = snapshot["known_dynamic_objects"]["fires"]
        rules: dict[str, str] = snapshot.get("rules", {})
        reservoirs = snapshot["known_priors"]["reservoirs"]

        if fire_name is None:
            obj = fires[0] if fires else None
        else:
            obj = next((f for f in fires if f.get("name") == fire_name), None)

        if obj is None:
            return {}

        fire_type = obj.get("attributes", {}).get("fire_type", "")
        observed_cells = obj.get("attributes", {}).get("observed_cells", [])

        return _build_fire_result(obj, fire_type, rules, observed_cells, reservoirs)

    def get_person_info(self, person_name: str | None = None) -> dict[str, Any]:
        """Get person-rescue info for *person_name* (trimmed).

        Returns {} for unknown names.  When *person_name* is None returns the
        first known person (or {} if none).
        """
        snapshot = self._store.snapshot()
        persons = snapshot["known_dynamic_objects"]["persons"]
        agents = snapshot.get("agents", [])
        deposits = snapshot["known_priors"]["deposits"]

        if person_name is None:
            obj = persons[0] if persons else None
        else:
            obj = next((p for p in persons if p.get("name") == person_name), None)

        if obj is None:
            return {}

        return _build_person_result(obj, agents, deposits)

    def get_reservoir_info(self, supply_type: str | None = None) -> dict[str, Any]:
        """List reservoirs, optionally filtered by *supply_type*."""
        snapshot = self._store.snapshot()
        reservoirs = snapshot["known_priors"]["reservoirs"]

        if supply_type:
            filtered = [
                r
                for r in reservoirs
                if r.get("attributes", {}).get("supply_type", "").lower()
                == supply_type.lower()
            ]
        else:
            filtered = list(reservoirs)

        return {"reservoirs": [_trim_semantic_object(r) for r in filtered]}

    def get_task_context(self, task_description: str | None) -> dict[str, Any]:
        """Extract mentioned object names from a *task_description*."""
        if not task_description:
            return {"mentioned_objects": []}

        snapshot = self._store.snapshot()
        fires = snapshot["known_dynamic_objects"]["fires"]
        persons = snapshot["known_dynamic_objects"]["persons"]
        reservoirs = snapshot["known_priors"]["reservoirs"]
        deposits = snapshot["known_priors"]["deposits"]

        # Build a set of known object names
        known_names: set[str] = set()
        for obj_list in (fires, persons, reservoirs, deposits):
            for obj in obj_list:
                name = obj.get("name", "")
                if name:
                    known_names.add(name)
                # Also check attributes for parent_fire references
                parent = obj.get("attributes", {}).get("parent_fire", "")
                if parent:
                    known_names.add(parent)

        desc = task_description or ""
        mentioned: list[str] = []
        for name in sorted(known_names, key=len, reverse=True):
            if re.search(re.escape(name), desc, re.IGNORECASE):
                mentioned.append(name)

        return {"mentioned_objects": mentioned}


# ── module-level convenience functions (backed by a global singleton store) ──

_default_tools: MapAgentTools | None = None


def configure_tools(store: SemanticMapStore) -> None:
    """Set the global store used by module-level convenience wrappers."""
    global _default_tools
    _default_tools = MapAgentTools(store)


def get_fire_info(fire_name: str | None = None) -> dict[str, Any]:
    """Module-level convenience — delegates to *configure_tools* store."""
    if _default_tools is None:
        return {}
    return _default_tools.get_fire_info(fire_name)


def get_person_info(person_name: str | None = None) -> dict[str, Any]:
    """Module-level convenience — delegates to *configure_tools* store."""
    if _default_tools is None:
        return {}
    return _default_tools.get_person_info(person_name)


def get_reservoir_info(supply_type: str | None = None) -> dict[str, Any]:
    """Module-level convenience — delegates to *configure_tools* store."""
    if _default_tools is None:
        return {}
    return _default_tools.get_reservoir_info(supply_type)


def get_task_context(task_description: str | None = None) -> dict[str, Any]:
    """Module-level convenience — delegates to *configure_tools* store."""
    if _default_tools is None:
        return {}
    return _default_tools.get_task_context(task_description)


# ── internal helpers ─────────────────────────────────────────────────────────


def _build_fire_result(
    obj: dict[str, Any],
    fire_type: str,
    rules: dict[str, str],
    observed_cells: list[dict[str, Any]],
    reservoirs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a trimmed fire result with computed fields."""
    result = _trim_semantic_object(obj)
    result["fire_type"] = fire_type
    result["required_supply"] = rules.get(fire_type, "unknown")
    result["regions"] = (
        [
            {
                "name": c.get("name", ""),
                "position": c.get("position"),
            }
            for c in observed_cells
        ]
        if observed_cells
        else []
    )
    result["nearest_reservoir_with_sand"] = _nearest_reservoir(
        obj.get("position"), reservoirs, "sand"
    )
    # Ensure position is present (may be in attributes or top-level)
    if "position" not in result:
        result["position"] = obj.get("position")
    return result


def _build_person_result(
    obj: dict[str, Any],
    agents: list[dict[str, Any]],
    deposits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a trimmed person result with computed fields."""
    result = _trim_semantic_object(obj)
    result["status"] = obj.get(
        "status", obj.get("attributes", {}).get("status", "unknown")
    )
    pos = obj.get("position")
    result["carriers"] = _nearest_agents(pos, agents)
    result["nearest_deposit"] = _nearest_deposit(pos, deposits)
    if "position" not in result:
        result["position"] = pos
    return result


def _nearest_reservoir(
    pos: list[float] | tuple[float, ...] | None,
    reservoirs: list[dict[str, Any]],
    supply_type: str = "sand",
) -> dict[str, Any] | None:
    """Find nearest reservoir with matching *supply_type*."""
    candidates = [
        r
        for r in reservoirs
        if r.get("attributes", {}).get("supply_type", "").lower() == supply_type.lower()
    ]
    if not candidates:
        return None
    closest = min(candidates, key=lambda r: _euc_3d(pos, r.get("position")))
    return {
        "name": closest.get("name", ""),
        "position": closest.get("position"),
        "distance": round(_euc_3d(pos, closest.get("position")), 2),
    }


def _nearest_agents(
    pos: list[float] | tuple[float, ...] | None,
    agents: list[dict[str, Any]],
    max_count: int = 3,
) -> list[dict[str, Any]]:
    """Return up to *max_count* agents nearest to *pos*, sorted by distance."""
    with_dist = [
        {
            "agent_id": a.get("agent_id", ""),
            "position": a.get("last_position"),
            "distance": round(_euc_3d(pos, a.get("last_position")), 2),
        }
        for a in agents
    ]
    with_dist.sort(key=lambda x: x["distance"])
    return with_dist[:max_count]


def _nearest_deposit(
    pos: list[float] | tuple[float, ...] | None,
    deposits: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find nearest deposit."""
    if not deposits:
        return None
    closest = min(deposits, key=lambda d: _euc_3d(pos, d.get("position")))
    return {
        "name": closest.get("name", ""),
        "position": closest.get("position"),
        "distance": round(_euc_3d(pos, closest.get("position")), 2),
    }
