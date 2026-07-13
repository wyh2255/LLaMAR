from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
        self._jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self.max_observations = max_observations
        self.reservoirs: dict[str, SemanticObject] = {}
        self.deposits: dict[str, SemanticObject] = {}
        self.fires: dict[str, SemanticObject] = {}
        self.persons: dict[str, SemanticObject] = {}
        self.agents: dict[str, AgentSemanticState] = {}
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

    def update_step_budget(self, *, current_step: int, max_steps: int) -> None:
        with self._lock:
            self.step_budget = {
                "current_step": current_step,
                "max_steps": max_steps,
                "remaining": max(0, max_steps - current_step),
            }

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
            obj = self._merge_locked(rec)
            rec_dict = rec.to_dict()
            self.observations.append(rec_dict)
            if len(self.observations) > self.max_observations:
                self.observations = self.observations[-self.max_observations :]
            self._append_jsonl_locked(
                "observation_ingested",
                {"observation": rec_dict, "object": obj.to_dict()},
            )
            return obj.to_dict()

    def get_recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.observations[-limit:])

    def snapshot(self, max_stale_steps: int = 5) -> dict[str, Any]:
        with self._lock:
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

    def _merge_locked(self, rec: ObservationRecord) -> SemanticObject:
        target = self._target_dict(rec.object_type)
        key = self._record_key(rec)
        existing = target.get(key)
        if existing is None:
            existing = SemanticObject(
                object_type=rec.object_type,
                name=rec.name or key,
                position=rec.normalized_position(),
            )
            target[key] = existing
        for attr_key, attr_value in rec.attributes.items():
            old_value = existing.attributes.get(attr_key)
            if (
                old_value is not None
                and old_value != attr_value
                and rec.step == existing.last_seen_step
            ):
                existing.conflict = True
            if rec.step >= existing.last_seen_step or self._status_rank(
                str(attr_value)
            ) >= self._status_rank(str(old_value)):
                existing.attributes[attr_key] = attr_value
        status = rec.attributes.get("status")
        if status is not None and (
            rec.step >= existing.last_seen_step
            or self._status_rank(str(status)) >= self._status_rank(existing.status)
        ):
            existing.status = str(status)
        if rec.normalized_position() is not None:
            existing.position = rec.normalized_position()
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

    def _target_dict(self, object_type: str) -> dict[str, SemanticObject]:
        if object_type == "fire":
            return self.fires
        if object_type == "person":
            return self.persons
        if object_type == "reservoir":
            return self.reservoirs
        if object_type == "deposit":
            return self.deposits
        return self.fires if object_type == "flammable" else self.persons

    def _record_key(self, rec: ObservationRecord) -> str:
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
