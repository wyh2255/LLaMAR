"""Semantic Map store — in-memory map of observed SAR environment state.

Migrated from sar_orch.semantic_map (Phase 1).  Phase 2 adds the defensive
redaction boundary before an observation is persisted to the JSONL artifact.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.redaction import RedactionPolicy

# Defensive boundary: semantic_map.jsonl never carries raw secrets.
_REDACTION = RedactionPolicy()


TERMINAL_STATUS_ORDER = {
    "rescued": 3,
    "extinguished": 3,
    "complete": 3,
    "active": 1,
    "trapped": 1,
}


@dataclass
class ObservationRecord:
    reporter: str
    step: int
    object_type: str
    name: str | None = None
    position: tuple[int, int, int] | list[int] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    source_task_id: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObservationRecord":
        return cls(
            reporter=str(data.get("reporter", "unknown")),
            step=int(data.get("step", 0)),
            object_type=str(data.get("object_type", "unknown")),
            name=data.get("name"),
            position=data.get("position"),
            attributes=dict(data.get("attributes") or {}),
            confidence=float(data.get("confidence", 1.0)),
            source_task_id=str(data.get("source_task_id", "")),
            note=str(data.get("note", "")),
        )

    def normalized_position(self) -> tuple[int, int, int] | None:
        if self.position is None:
            return None
        values = tuple(int(v) for v in self.position)
        if len(values) != 3:
            raise ValueError("position must contain exactly 3 coordinates")
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "reporter": self.reporter,
            "step": self.step,
            "object_type": self.object_type,
            "name": self.name,
            "position": list(self.normalized_position())
            if self.normalized_position()
            else None,
            "attributes": dict(self.attributes),
            "confidence": self.confidence,
            "source_task_id": self.source_task_id,
            "note": self.note,
        }


@dataclass
class SemanticObject:
    object_type: str
    name: str
    position: tuple[int, int, int] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "unknown"
    last_seen_step: int = 0
    last_seen_ts: float = field(default_factory=time.time)
    sources: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    conflict: bool = False
    #: Per-field conflict records (C3 semantics): each entry is
    #: ``{"field_name", "value" (retained current holder), "candidates" (all
    #: claimed values, holder first)}``.  Exposed through ``to_dict()`` so the
    #: coordinator shadow normalizer can emit canonical-compatible freshness
    #: conflict information.  ``conflict`` remains the aggregate boolean flag.
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    #: Attribute keys claimed by a DIRECT observation of this entity (vs.
    #: cell-derived consensus).  Internal merge bookkeeping, never serialized.
    _direct_attrs: set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "name": self.name,
            "position": list(self.position) if self.position else None,
            "attributes": copy.deepcopy(self.attributes),
            "status": self.status,
            "last_seen_step": self.last_seen_step,
            "last_seen_ts": self.last_seen_ts,
            "sources": copy.deepcopy(self.sources),
            "confidence": self.confidence,
            "conflict": self.conflict,
            "conflicts": copy.deepcopy(self.conflicts),
        }


@dataclass
class AgentSemanticState:
    agent_id: str
    last_position: tuple[int, int, int] | None = None
    inventory: dict[str, Any] = field(default_factory=dict)
    current_task_id: str = ""
    task_state: str = "UNKNOWN"
    last_seen_step: int = 0
    last_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "last_position": list(self.last_position) if self.last_position else None,
            "inventory": copy.deepcopy(self.inventory),
            "current_task_id": self.current_task_id,
            "task_state": self.task_state,
            "last_seen_step": self.last_seen_step,
            "last_message": self.last_message,
        }


class SemanticMapStore:
    def __init__(
        self,
        jsonl_path: str | Path | None = None,
        max_observations: int = 1000,
    ) -> None:
        self._lock = threading.Lock()
        self._revision: int = 0
        self._jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self.max_observations = max_observations
        self.reservoirs: dict[str, SemanticObject] = {}
        self.deposits: dict[str, SemanticObject] = {}
        self.fires: dict[str, SemanticObject] = {}
        self.persons: dict[str, SemanticObject] = {}
        self.agents: dict[str, AgentSemanticState] = {}
        self._unknown_types: dict[str, SemanticObject] = {}
        self._ground_truth_names: frozenset[str] = frozenset()
        self.observations: list[dict[str, Any]] = []
        self.rules: dict[str, Any] = {}
        self.step_budget: dict[str, int] = {
            "current_step": 0,
            "max_steps": 0,
            "remaining": 0,
        }
        self.task_objective = ""

    def set_jsonl_path(self, path: str | Path | None) -> None:
        with self._lock:
            self._jsonl_path = Path(path) if path is not None else None

    def init_priors(
        self, *, reservoirs, deposits, agents, rules, step_budget, task_objective
    ) -> None:
        with self._lock:
            self.reservoirs = {
                self._object_key("reservoir", item): self._prior_object(
                    "reservoir", item
                )
                for item in reservoirs
            }
            self.deposits = {
                self._object_key("deposit", item): self._prior_object("deposit", item)
                for item in deposits
            }
            self.agents = {
                str(item.get("agent_id") or item.get("name")): AgentSemanticState(
                    agent_id=str(item.get("agent_id") or item.get("name"))
                )
                for item in agents
            }
            self.rules = copy.deepcopy(rules)
            self.step_budget = dict(step_budget)
            self.task_objective = str(task_objective)
            self._revision += 1

    def update_step_budget(self, *, current_step: int, max_steps: int) -> None:
        with self._lock:
            new_budget = {
                "current_step": current_step,
                "max_steps": max_steps,
                "remaining": max(0, max_steps - current_step),
            }
            if new_budget != self.step_budget:
                self.step_budget = new_budget
                self._revision += 1

    def get_step_budget(self) -> dict[str, int]:
        with self._lock:
            return dict(self.step_budget)

    def set_ground_truth(self, object_names: list[str]) -> None:
        """Set the ground-truth set of all discoverable object names (for MapRecall)."""
        with self._lock:
            self._ground_truth_names = frozenset(object_names)

    def map_recall(self) -> float:
        """Fraction of ground-truth objects that have been discovered by the semantic map.

        Compares known dynamic objects (fires, persons) plus always-known priors
        (reservoirs, deposits) against the ground-truth set.
        Returns 0.0 if no ground truth has been set.
        """
        with self._lock:
            if not self._ground_truth_names:
                return 0.0
            known = {
                obj.name
                for obj in list(self.fires.values())
                + list(self.persons.values())
                + list(self.reservoirs.values())
                + list(self.deposits.values())
            }
            if not known:
                return 0.0
            return len(known & self._ground_truth_names) / len(self._ground_truth_names)

    def freshness(self) -> float:
        """Average number of steps since each object was last observed.

        Lower = fresher data.  Returns 0.0 if no objects tracked.
        """
        with self._lock:
            current = self.step_budget.get("current_step", 0)
            all_objs = (
                list(self.fires.values())
                + list(self.persons.values())
                + list(self.reservoirs.values())
                + list(self.deposits.values())
            )
            if not all_objs:
                return 0.0
            ages = [current - max(o.last_seen_step, 0) for o in all_objs]
            return sum(ages) / len(ages)

    def set_max_observations(self, n: int) -> None:
        with self._lock:
            self.max_observations = n

    def ingest_observation(
        self, record: ObservationRecord | dict[str, Any]
    ) -> dict[str, Any]:
        rec = (
            record
            if isinstance(record, ObservationRecord)
            else ObservationRecord.from_dict(record)
        )
        with self._lock:
            # Agent observations update AgentSemanticState directly
            if rec.object_type == "agent":
                agent = self.agents.get(rec.name or "")
                if agent is not None:
                    if (
                        rec.normalized_position() is not None
                        and rec.step >= agent.last_seen_step
                    ):
                        agent.last_position = rec.normalized_position()
                    agent.last_seen_step = max(agent.last_seen_step, rec.step)
                    if rec.note:
                        agent.last_message = _REDACTION.sanitize_event(rec.note)
                rec_dict = _REDACTION.redactor.redact_data(rec.to_dict())
                self.observations.append(rec_dict)
                # Trim observations list to prevent unbounded growth
                if len(self.observations) > self.max_observations:
                    self.observations = self.observations[-self.max_observations :]
                self._revision += 1
                self._append_jsonl_locked(
                    "observation_ingested",
                    {"observation": rec_dict, "object_type": "agent"},
                )
                return rec_dict

            target = self._target_dict(rec.object_type)
            key = self._record_key(rec)
            prev = target.get(key)
            if prev is not None:
                prev_attrs = dict(prev.attributes)
                prev_pos = prev.position
                prev_status = prev.status
                prev_conf = prev.confidence
            else:
                prev_attrs = prev_pos = prev_status = prev_conf = None
            obj = self._merge_locked(rec)
            self._revision += 1
            rec_dict = _REDACTION.redactor.redact_data(rec.to_dict())
            safe_object = _REDACTION.redactor.redact_data(obj.to_dict())
            is_new = prev is None
            if is_new or self._is_observation_noteworthy(
                rec, prev_attrs, prev_pos, prev_status, prev_conf
            ):
                self.observations.append(rec_dict)
                if len(self.observations) > self.max_observations:
                    self.observations = self.observations[-self.max_observations :]
            self._append_jsonl_locked(
                "observation_ingested",
                {"observation": rec_dict, "object": safe_object},
            )
            return safe_object

    def get_recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.observations[-limit:])

    def snapshot(self, max_stale_steps: int = 5) -> dict[str, Any]:
        """Return the current semantic map snapshot.

        Delegates to snapshot_with_revision() for consistency; only the
        snapshot dict is returned.  Compatible with existing callers.
        """
        return self.snapshot_with_revision(max_stale_steps)[1]

    def snapshot_with_revision(
        self, max_stale_steps: int = 5
    ) -> tuple[int, dict[str, Any]]:
        """Atomically return (revision, snapshot) under the store lock.

        The revision and snapshot are produced within a single lock
        acquisition, guaranteeing consistency for callers that need to
        correlate version with content.
        """
        with self._lock:
            return self._revision, self._build_snapshot_locked(max_stale_steps)

    def worker_public_snapshot(
        self, viewer_id: str, max_stale_steps: int = 5
    ) -> dict[str, Any]:
        """Worker-scoped public view of the semantic map (Phase 4 ACL).

        A worker may only see its own Embodied state (position / inventory)
        plus shared scene facts.  Other agents appear as safe identity-only
        entries — no position / inventory / task internals leak.  This is the
        ACL boundary for any worker-facing map projection; the full
        ``snapshot()`` stays coordinator/UI-only.
        """
        with self._lock:
            base = self._build_snapshot_locked(max_stale_steps)
            agents = base.get("agents", [])
            safe_agents: list[dict[str, Any]] = []
            for agent in agents:
                if agent.get("agent_id") == viewer_id:
                    safe_agents.append(agent)
                else:
                    safe_agents.append({"agent_id": agent.get("agent_id", "")})
            base["agents"] = safe_agents
            return base

    def _build_snapshot_locked(self, max_stale_steps: int = 5) -> dict[str, Any]:
        # NOTE: caller must hold self._lock
        current_step = int(self.step_budget.get("current_step", 0))
        fires = [obj.to_dict() for obj in self.fires.values()]
        persons = [obj.to_dict() for obj in self.persons.values()]
        stale = [
            obj
            for obj in fires + persons
            if current_step - int(obj.get("last_seen_step", 0)) > max_stale_steps
        ]
        conflicts = [obj for obj in fires + persons if obj.get("conflict")]
        return {
            "step_budget": dict(self.step_budget),
            "task_objective": self.task_objective,
            "rules": copy.deepcopy(self.rules),
            "known_priors": {
                "reservoirs": [obj.to_dict() for obj in self.reservoirs.values()],
                "deposits": [obj.to_dict() for obj in self.deposits.values()],
            },
            "known_dynamic_objects": {"fires": fires, "persons": persons},
            "agents": [agent.to_dict() for agent in self.agents.values()],
            "recent_observations": copy.deepcopy(self.observations[-10:]),
            "stale_entries": stale,
            "conflicts": conflicts,
            "unknowns": self._unknowns_locked(),
        }

    #: Attribute keys a cell observation may contribute to the PARENT entity as
    #: a derived consensus value (the region-level summary LLM rendering reads).
    #: Everything else a cell reports lives only on the cell entry, so it never
    #: clobbers the parent's direct position/attributes.
    _CELL_DERIVED_PARENT_KEYS: frozenset[str] = frozenset({"fire_type"})

    def _merge_locked(self, rec: ObservationRecord) -> SemanticObject:
        target = self._target_dict(rec.object_type)
        key = self._record_key(rec)
        is_cell = rec.object_type == "fire" and bool(rec.attributes.get("parent_fire"))
        existing = target.get(key)
        if existing is None:
            existing = SemanticObject(
                object_type=rec.object_type,
                name=key if is_cell else (rec.name or key),
                # A cell observation never seeds the parent's direct position.
                position=None if is_cell else rec.normalized_position(),
            )
            target[key] = existing
        if is_cell:
            self._merge_cell_locked(existing, rec)
        else:
            self._merge_direct_locked(existing, rec)
        existing.last_seen_step = max(existing.last_seen_step, rec.step)
        existing.last_seen_ts = time.time()
        existing.confidence = max(existing.confidence, rec.confidence)
        existing.sources.append(
            {
                "reporter": rec.reporter,
                "task_id": rec.source_task_id,
                "step": rec.step,
                "confidence": rec.confidence,
                "note": rec.note,
            }
        )
        # Trim sources list to prevent unbounded growth
        if len(existing.sources) > 50:
            existing.sources = existing.sources[-50:]
        return existing

    def _merge_direct_locked(
        self, existing: SemanticObject, rec: ObservationRecord
    ) -> None:
        """Merge a direct (region / standalone / non-cell) observation.

        A direct claim is authoritative for the entity's own field: it always
        supersedes cell-derived consensus.  Once a field is directly claimed,
        a same-step differing claim retains the current holder and marks the
        field conflicted (C3) instead of last-write-wins.
        """
        direct_attrs = existing._direct_attrs
        for attr_key, attr_value in rec.attributes.items():
            if attr_key == "status":
                continue  # handled below, keeps top-level status in sync
            if attr_key not in direct_attrs:
                existing.attributes[attr_key] = attr_value
                direct_attrs.add(attr_key)
                continue
            old_value = existing.attributes.get(attr_key)
            same_step = rec.step == existing.last_seen_step
            if old_value is not None and old_value != attr_value and same_step:
                self._record_conflict(existing, attr_key, old_value, attr_value)
                continue
            new_rank = self._status_rank(str(attr_value))
            if rec.step >= existing.last_seen_step or (
                new_rank > 0 and new_rank >= self._status_rank(str(old_value))
            ):
                existing.attributes[attr_key] = attr_value
        status = rec.attributes.get("status")
        if status is not None:
            new_status = str(status)
            if "status" not in direct_attrs:
                existing.status = new_status
                existing.attributes["status"] = new_status
                direct_attrs.add("status")
            else:
                old_attr_status = existing.attributes.get("status")
                same_step = rec.step == existing.last_seen_step
                if (
                    old_attr_status is not None
                    and old_attr_status != new_status
                    and same_step
                ):
                    self._record_conflict(
                        existing, "status", old_attr_status, new_status
                    )
                else:
                    new_status_rank = self._status_rank(new_status)
                    old_status = existing.status
                    if rec.step >= existing.last_seen_step or (
                        new_status_rank > 0
                        and new_status_rank >= self._status_rank(old_status)
                    ):
                        existing.status = new_status
                        existing.attributes["status"] = new_status
        new_pos = rec.normalized_position()
        if new_pos is not None:
            same_step = rec.step == existing.last_seen_step
            if (
                existing.position is not None
                and existing.position != new_pos
                and same_step
            ):
                self._record_conflict(
                    existing, "position", list(existing.position), list(new_pos)
                )
            elif rec.step >= existing.last_seen_step:
                existing.position = new_pos

    def _merge_cell_locked(
        self, existing: SemanticObject, rec: ObservationRecord
    ) -> None:
        """Merge a cell observation into its own ``observed_cells`` entry.

        The cell's position/attributes update the cell entry only — the parent's
        direct position/attributes are never clobbered by cell evidence.  Only
        region-shared keys in ``_CELL_DERIVED_PARENT_KEYS`` (e.g. ``fire_type``)
        contribute a derived consensus value to the parent, and only when the
        parent has no direct claim for that key.
        """
        cells = existing.attributes.setdefault("observed_cells", [])
        cell_name = rec.name or ""
        cell = None
        for c in cells:
            if isinstance(c, dict) and c.get("name") == cell_name:
                cell = c
                break
        if cell is None:
            cell = {"name": cell_name, "attributes": {}, "last_seen_step": 0}
            cells.append(cell)
        cell_attrs = cell.setdefault("attributes", {})
        for attr_key, attr_value in rec.attributes.items():
            if attr_key == "parent_fire":
                continue
            old_value = cell_attrs.get(attr_key)
            same_step = rec.step == cell.get("last_seen_step", 0)
            if old_value is not None and old_value != attr_value and same_step:
                self._record_conflict(cell, attr_key, old_value, attr_value)
                continue
            if rec.step >= cell.get("last_seen_step", 0):
                cell_attrs[attr_key] = attr_value
        new_pos = rec.normalized_position()
        if new_pos is not None:
            same_step = rec.step == cell.get("last_seen_step", 0)
            old_pos = cell.get("position")
            if old_pos is not None and old_pos != list(new_pos) and same_step:
                self._record_conflict(cell, "position", old_pos, list(new_pos))
            elif rec.step >= cell.get("last_seen_step", 0):
                cell["position"] = list(new_pos)
        cell["last_seen_step"] = max(cell.get("last_seen_step", 0), rec.step)

        for key in self._CELL_DERIVED_PARENT_KEYS:
            cell_value = rec.attributes.get(key)
            if cell_value is None:
                continue
            if key in existing._direct_attrs:
                # A cell never overrides a directly-observed region claim.
                continue
            old_value = existing.attributes.get(key)
            same_step = rec.step == existing.last_seen_step
            if old_value is not None and old_value != cell_value and same_step:
                self._record_conflict(existing, key, old_value, cell_value)
            elif rec.step >= existing.last_seen_step:
                existing.attributes[key] = cell_value

    @staticmethod
    def _record_conflict(
        holder: SemanticObject | dict[str, Any],
        field_name: str,
        current_value: Any,
        incoming_value: Any,
    ) -> None:
        """Record a same-step field conflict: the current holder is retained and
        both claims are tracked without selecting a winner (C3 semantics)."""
        if isinstance(holder, dict):
            holder["conflict"] = True
            records = holder.setdefault("conflicts", [])
        else:
            holder.conflict = True
            records = holder.conflicts
        for r in records:
            if r.get("field_name") == field_name:
                if incoming_value not in r["candidates"]:
                    r["candidates"].append(incoming_value)
                return
        records.append(
            {
                "field_name": field_name,
                "value": current_value,
                "candidates": [current_value, incoming_value],
            }
        )

    def _is_observation_noteworthy(
        self,
        rec: ObservationRecord,
        prev_attrs: dict[str, Any] | None,
        prev_pos: tuple[int, int, int] | None,
        prev_status: str | None,
        prev_conf: float | None,
    ) -> bool:
        if prev_attrs is None:
            return True
        position = rec.normalized_position()
        pos_changed = (
            position is not None and prev_pos is not None and position != prev_pos
        )
        attrs_changed = any(prev_attrs.get(k) != v for k, v in rec.attributes.items())
        status_changed = (
            rec.attributes.get("status") is not None
            and rec.attributes["status"] != prev_status
        )
        confidence_increased = rec.confidence > (prev_conf or 0.0)
        return bool(
            pos_changed or attrs_changed or status_changed or confidence_increased
        )

    def _target_dict(self, object_type: str) -> dict[str, SemanticObject]:
        mapping = {
            "fire": self.fires,
            "person": self.persons,
            "reservoir": self.reservoirs,
            "deposit": self.deposits,
            "flammable": self.fires,
        }
        return mapping.get(object_type, self._unknown_types)

    def _record_key(self, rec: ObservationRecord) -> str:
        if rec.object_type == "fire":
            parent = rec.attributes.get("parent_fire", "")
            if parent:
                return parent
        if rec.name:
            return rec.name
        pos = rec.normalized_position()
        return (
            f"{rec.object_type}:{pos}"
            if pos is not None
            else f"{rec.object_type}:unknown"
        )

    def _object_key(self, object_type: str, item: dict[str, Any]) -> str:
        return str(item.get("name") or f"{object_type}:{item.get('position')}")

    def _prior_object(self, object_type: str, item: dict[str, Any]) -> SemanticObject:
        name = self._object_key(object_type, item)
        attrs = {k: v for k, v in item.items() if k not in ("name", "position")}
        position = item.get("position")
        pos = tuple(position) if position is not None else None
        return SemanticObject(
            object_type=object_type,
            name=name,
            position=pos,
            attributes=attrs,
            status="known",
            last_seen_step=0,
        )

    def _status_rank(self, value: str | None) -> int:
        return TERMINAL_STATUS_ORDER.get((value or "").lower(), 0)

    def _unknowns_locked(self) -> list[str]:
        unknowns = []
        if not self.fires:
            unknowns.append("fire locations incomplete")
        if not self.persons:
            unknowns.append("person rescue status unknown")
        return unknowns

    def _append_jsonl_locked(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._jsonl_path is None:
            return
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self._jsonl_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"ts": time.time(), "event_type": event_type, **payload},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )


__all__ = [
    "AgentSemanticState",
    "ObservationRecord",
    "SemanticMapStore",
    "SemanticObject",
    "TERMINAL_STATUS_ORDER",
]
