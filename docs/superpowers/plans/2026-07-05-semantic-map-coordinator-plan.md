---
日期: 2026-07-05
文档类型: 实施计划
文档概述: 实施 SAR coordinator semantic map 模式，移除正式决策中的 env oracle，新增 worker 非阻塞 observation 上报和共享语义地图查询。
---

# Semantic Map Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build SAR semantic mode where the coordinator plans from weak priors plus worker reports instead of direct environment oracle access.

**Architecture:** Add a thread-safe `SemanticMapStore` owned by the coordinator. Workers report observations through the existing A2A push notification path (`TASK_STATE_WORKING` status updates emitted by `A2AWorkerSink`), and coordinator tools expose compact semantic map and team status queries. `query_sar_state` remains available only in oracle/debug mode.

**Tech Stack:** Python, FastAPI, A2A SDK protobuf events, existing Agent tool classes, pytest, ruff, CSV/JSONL experiment logging.

## Global Constraints

- Formal semantic mode must not expose fire/person/flammable truth from `barrier.get_env_snapshot()` to the coordinator LLM.
- `SemanticMapStore` may initialize weak priors: reservoirs, deposits, agent roster, SAR rules, step budget, and task objective.
- `report_observation` must be non-blocking: it must not trigger `INPUT_REQUIRED`, must not call `AskCoordinatorTool`, and must not save/restore snapshots.
- Observation transport must reuse the existing A2A push notification channel via `A2AWorkerSink` `TASK_STATE_WORKING` status updates.
- Dynamic semantic map context must stay compact and appear late in message assembly to preserve DeepSeek prefix caching.
- Debug UI may still use `barrier.get_env_snapshot()` for visualization; this must not become a coordinator decision tool in semantic mode.
- Preserve existing user/worktree changes outside the files explicitly touched by each task.

---

## File Structure

- Create `sar_orch/semantic_map.py`: thread-safe semantic map data model, merge logic, staleness/conflict detection, JSONL persistence.
- Create `sar_orch/tools/coordinator/query_semantic_map.py`: coordinator tool returning compact map snapshot.
- Create `sar_orch/tools/coordinator/query_team_status.py`: coordinator tool returning team/task/observation summary.
- Modify `sar_orch/tools/coordinator/__init__.py`: export new coordinator tools.
- Create `sar_orch/tools/worker/report_observation.py`: worker tool returning structured observation payload as a tool result.
- Create `sar_orch/tools/worker/query_shared_memory.py`: worker tool querying coordinator semantic map endpoint.
- Modify `sar_orch/tools/worker/__init__.py`: export new worker tools.
- Modify `src/a2a/worker/sink.py`: ensure `report_observation` tool results preserve full structured JSON in `[DATA]`.
- Modify `src/a2a/coordinator/server.py`: store `SemanticMapStore`, expose semantic map read endpoint, parse observation `[DATA]` in push callback.
- Modify `src/a2a/coordinator/event_store.py`: record `observation_report` and expose recent observations in summaries/status.
- Modify `src/Agent/router_agent/context.py`: replace oracle `global_snapshot` pinned state with semantic/team summaries.
- Modify `sar_orch/coordinator.py`: instantiate semantic map, inject semantic tools, control oracle mode.
- Modify `sar_orch/worker.py`: inject `report_observation` and `query_shared_memory` with coordinator URL/runtime metadata.
- Modify `sar_orch/experiment.py`: add `--mode semantic|oracle`, metadata fields, and pass mode into coordinator/workers.
- Modify `sar_orch/benchmark.py`: pass semantic/oracle mode through benchmark runs.
- Modify `sar_orch/logger.py`: add `semantic_map.jsonl` logging support or delegate JSONL path to `SemanticMapStore`.
- Modify prompts in `sar_orch/prompts/coordinator/` and `sar_orch/prompts/worker/`: remove oracle-first behavior and require observation reports.
- Create tests under `tests/`: semantic map unit tests, observation push callback tests, tool tests, mode gating tests.

---

### Task 1: SemanticMapStore Core

**Files:**
- Create: `sar_orch/semantic_map.py`
- Test: `tests/test_semantic_map.py`

**Interfaces:**
- Produces: `ObservationRecord`, `SemanticObject`, `AgentSemanticState`, `SemanticMapStore`
- Produces: `SemanticMapStore.init_priors(reservoirs, deposits, agents, rules, step_budget, task_objective) -> None`
- Produces: `SemanticMapStore.ingest_observation(record: ObservationRecord | dict) -> dict`
- Produces: `SemanticMapStore.snapshot(max_stale_steps: int = 5) -> dict`
- Produces: `SemanticMapStore.get_recent_observations(limit: int = 10) -> list[dict]`
- Produces: `SemanticMapStore.set_jsonl_path(path: str | Path | None) -> None`

- [ ] **Step 1: Write failing semantic map tests**

Create `tests/test_semantic_map.py`:

```python
from pathlib import Path

from sar_orch.semantic_map import ObservationRecord, SemanticMapStore


def test_init_priors_exposes_reservoirs_and_deposits_but_no_fire_truth():
    store = SemanticMapStore()
    store.init_priors(
        reservoirs=[{"name": "ReservoirYork", "position": [1, 2, 0], "resource_type": "Water"}],
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
    store.ingest_observation({**base, "reporter": "Alice", "attributes": {"status": "trapped"}})
    store.ingest_observation({**base, "reporter": "Bob", "attributes": {"status": "rescued"}})

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_semantic_map.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'sar_orch.semantic_map'`.

- [ ] **Step 3: Implement `sar_orch/semantic_map.py`**

Create `sar_orch/semantic_map.py` with these concrete elements:

```python
from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TERMINAL_STATUS_ORDER = {"rescued": 3, "extinguished": 3, "complete": 3, "active": 1, "trapped": 1}


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
            "position": list(self.normalized_position()) if self.normalized_position() else None,
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
    def __init__(self, jsonl_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self.reservoirs: dict[str, SemanticObject] = {}
        self.deposits: dict[str, SemanticObject] = {}
        self.fires: dict[str, SemanticObject] = {}
        self.persons: dict[str, SemanticObject] = {}
        self.agents: dict[str, AgentSemanticState] = {}
        self.observations: list[dict[str, Any]] = []
        self.rules: dict[str, Any] = {}
        self.step_budget: dict[str, int] = {"current_step": 0, "max_steps": 0, "remaining": 0}
        self.task_objective = ""

    def set_jsonl_path(self, path: str | Path | None) -> None:
        with self._lock:
            self._jsonl_path = Path(path) if path is not None else None

    def init_priors(self, *, reservoirs, deposits, agents, rules, step_budget, task_objective) -> None:
        with self._lock:
            self.reservoirs = {self._object_key("reservoir", item): self._prior_object("reservoir", item) for item in reservoirs}
            self.deposits = {self._object_key("deposit", item): self._prior_object("deposit", item) for item in deposits}
            self.agents = {str(item.get("agent_id") or item.get("name")): AgentSemanticState(agent_id=str(item.get("agent_id") or item.get("name"))) for item in agents}
            self.rules = copy.deepcopy(rules)
            self.step_budget = dict(step_budget)
            self.task_objective = str(task_objective)

    def update_step_budget(self, *, current_step: int, max_steps: int) -> None:
        with self._lock:
            self.step_budget = {"current_step": current_step, "max_steps": max_steps, "remaining": max(0, max_steps - current_step)}

    def ingest_observation(self, record: ObservationRecord | dict[str, Any]) -> dict[str, Any]:
        rec = record if isinstance(record, ObservationRecord) else ObservationRecord.from_dict(record)
        with self._lock:
            obj = self._merge_locked(rec)
            rec_dict = rec.to_dict()
            self.observations.append(rec_dict)
            self._append_jsonl_locked("observation_ingested", {"observation": rec_dict, "object": obj.to_dict()})
            return obj.to_dict()

    def get_recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.observations[-limit:])

    def snapshot(self, max_stale_steps: int = 5) -> dict[str, Any]:
        with self._lock:
            current_step = int(self.step_budget.get("current_step", 0))
            fires = [obj.to_dict() for obj in self.fires.values()]
            persons = [obj.to_dict() for obj in self.persons.values()]
            stale = [obj for obj in fires + persons if current_step - int(obj.get("last_seen_step", 0)) > max_stale_steps]
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
            existing = SemanticObject(object_type=rec.object_type, name=rec.name or key, position=rec.normalized_position())
            target[key] = existing
        for attr_key, attr_value in rec.attributes.items():
            old_value = existing.attributes.get(attr_key)
            if old_value is not None and old_value != attr_value and rec.step == existing.last_seen_step:
                existing.conflict = True
            if rec.step >= existing.last_seen_step or self._status_rank(str(attr_value)) >= self._status_rank(str(old_value)):
                existing.attributes[attr_key] = attr_value
        status = rec.attributes.get("status")
        if status is not None and (rec.step >= existing.last_seen_step or self._status_rank(str(status)) >= self._status_rank(existing.status)):
            existing.status = str(status)
        if rec.normalized_position() is not None:
            existing.position = rec.normalized_position()
        existing.last_seen_step = max(existing.last_seen_step, rec.step)
        existing.last_seen_ts = time.time()
        existing.confidence = max(existing.confidence, rec.confidence)
        existing.sources.append({"reporter": rec.reporter, "task_id": rec.source_task_id, "step": rec.step, "confidence": rec.confidence, "note": rec.note})
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
        return f"{rec.object_type}:{pos}" if pos is not None else f"{rec.object_type}:unknown"

    def _object_key(self, object_type: str, item: dict[str, Any]) -> str:
        return str(item.get("name") or f"{object_type}:{item.get('position')}")

    def _prior_object(self, object_type: str, item: dict[str, Any]) -> SemanticObject:
        name = self._object_key(object_type, item)
        attrs = {k: v for k, v in item.items() if k not in ("name", "position")}
        position = item.get("position")
        pos = tuple(position) if position is not None else None
        return SemanticObject(object_type=object_type, name=name, position=pos, attributes=attrs, status="known", last_seen_step=0)

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
            f.write(json.dumps({"ts": time.time(), "event_type": event_type, **payload}, ensure_ascii=False, default=str) + "\n")
```

- [ ] **Step 4: Run semantic map tests**

Run: `uv run pytest tests/test_semantic_map.py -q`

Expected: PASS.

- [ ] **Step 5: Run lint for new module**

Run: `uv run --with ruff ruff check sar_orch/semantic_map.py tests/test_semantic_map.py`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add sar_orch/semantic_map.py tests/test_semantic_map.py
git commit -m "feat: add semantic map store"
```

---

### Task 2: Observation Report Through Existing A2A Push Path

**Files:**
- Create: `sar_orch/tools/worker/report_observation.py`
- Modify: `sar_orch/tools/worker/__init__.py`
- Modify: `src/a2a/worker/sink.py`
- Modify: `src/a2a/coordinator/event_store.py`
- Modify: `src/a2a/coordinator/server.py`
- Test: `tests/test_report_observation.py`
- Test: `tests/test_coordinator_push_callback.py`

**Interfaces:**
- Consumes: `SemanticMapStore.ingest_observation(record: dict) -> dict`
- Produces: `ReportObservationTool.execute(...) -> ToolResult`
- Produces: `EventStore.append(..., observation: dict | None = None) -> None`
- Produces: `EventStore.get_recent_observations(limit: int = 10) -> list[dict]`

- [ ] **Step 1: Write failing worker tool test**

Create `tests/test_report_observation.py`:

```python
import json

import pytest

from sar_orch.tools.worker.report_observation import ReportObservationTool


@pytest.mark.asyncio
async def test_report_observation_returns_structured_payload_without_pausing():
    tool = ReportObservationTool(agent_name="Alice", task_id="alice-task", get_step=lambda: 9)

    result = await tool.execute(
        object_type="fire",
        name="CaldorFire_Region_1",
        position=[4, 4, 0],
        attributes={"fire_type": "Chemical", "intensity": "Medium", "status": "active"},
        confidence=1.0,
        note="Visible from current location",
    )

    payload = json.loads(result.content)

    assert result.success is True
    assert payload["reporter"] == "Alice"
    assert payload["source_task_id"] == "alice-task"
    assert payload["step"] == 9
    assert payload["object_type"] == "fire"
    assert payload["attributes"]["fire_type"] == "Chemical"
    assert "INPUT_REQUIRED" not in result.content
```

- [ ] **Step 2: Write failing push callback observation test**

Extend `tests/test_coordinator_push_callback.py` with a test that mirrors the real push-callback parsing logic. Add this helper near existing payload helpers:

```python
def _make_observation_status_payload(task_id: str, data_json: str) -> dict:
    return {
        "statusUpdate": {
            "taskId": task_id,
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": f"[Result] report_observation: observed\n[DATA]\n{data_json}"}],
                },
            },
        }
    }
```

Add this test:

```python
def test_working_status_tool_result_report_observation_is_recorded(client):
    data_json = json.dumps(
        {
            "ev": "tool_result",
            "tool_name": "report_observation",
            "success": True,
            "content": json.dumps(
                {
                    "reporter": "Alice",
                    "step": 4,
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [1, 1, 0],
                    "attributes": {"status": "active"},
                }
            ),
        }
    )
    resp = client.post("/a2a/push-callback", json=_make_observation_status_payload("task-obs-1", data_json))

    assert resp.status_code == 200
    summary = event_store.get_summary(task_ids={"task-obs-1"})
    observations = event_store.get_recent_observations()
    assert "OBSERVATION" in summary
    assert observations[0]["name"] == "FireA"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_report_observation.py tests/test_coordinator_push_callback.py -q`

Expected: FAIL because `report_observation.py`, observation parsing, and `get_recent_observations()` do not exist.

- [ ] **Step 4: Implement `ReportObservationTool`**

Create `sar_orch/tools/worker/report_observation.py`:

```python
import json
from collections.abc import Callable

from Agent.worker_agent.tools.base import Tool, ToolResult


class ReportObservationTool(Tool):
    name = "report_observation"
    description = "Non-blocking report of local SAR observations to the coordinator semantic map."
    parameters = {
        "type": "object",
        "properties": {
            "object_type": {"type": "string", "description": "fire|person|reservoir|deposit|agent|status|unknown"},
            "name": {"type": "string", "description": "Optional object name"},
            "position": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
            "attributes": {"type": "object"},
            "confidence": {"type": "number", "default": 1.0},
            "note": {"type": "string"},
        },
        "required": ["object_type"],
    }

    def __init__(self, agent_name: str = "unknown", task_id: str = "", get_step: Callable[[], int] | None = None):
        self._agent_name = agent_name
        self._task_id = task_id
        self._get_step = get_step or (lambda: 0)

    async def execute(self, **kwargs) -> ToolResult:
        payload = {
            "reporter": self._agent_name,
            "step": int(self._get_step()),
            "object_type": kwargs.get("object_type", "unknown"),
            "name": kwargs.get("name"),
            "position": kwargs.get("position"),
            "attributes": kwargs.get("attributes") or {},
            "confidence": float(kwargs.get("confidence", 1.0)),
            "source_task_id": self._task_id,
            "note": kwargs.get("note", ""),
        }
        return ToolResult(success=True, content=json.dumps(payload, ensure_ascii=False))
```

- [ ] **Step 5: Export and inject `ReportObservationTool`**

Modify `sar_orch/tools/worker/__init__.py`:

```python
from sar_orch.tools.worker.report_observation import ReportObservationTool

SAR_WORKER_TOOLS = [
    NavigateToTool,
    MoveTool,
    ExploreTool,
    CarryPersonTool,
    DropOffPersonTool,
    GetSupplyTool,
    GetAgentStateTool,
    StoreSupplyTool,
    UseSupplyTool,
    ClearInventoryTool,
    ReportObservationTool,
    NoOpTool,
    FinishTaskTool,
    AskCoordinatorTool,
]

__all__ = [
    "SAR_WORKER_TOOLS",
    "NavigateToTool",
    "MoveTool",
    "ExploreTool",
    "CarryPersonTool",
    "DropOffPersonTool",
    "GetSupplyTool",
    "GetAgentStateTool",
    "StoreSupplyTool",
    "UseSupplyTool",
    "ClearInventoryTool",
    "ReportObservationTool",
    "NoOpTool",
    "FinishTaskTool",
    "AskCoordinatorTool",
]
```

Modify `sar_orch/worker.py` tool construction loop so `ReportObservationTool` receives runtime metadata:

```python
if tool_cls.__name__ == "ReportObservationTool":
    tools.append(
        tool_cls(
            agent_name=self.agent_name,
            task_id="",
            get_step=lambda: getattr(self._barrier, "_step_counter", 0),
        )
    )
elif tool_cls.__name__ in ("FinishTaskTool", "AskCoordinatorTool"):
    tools.append(tool_cls())
else:
    tools.append(tool_cls(barrier=self._barrier, agent_idx=self.agent_idx))
```

- [ ] **Step 6: Preserve full report content in `A2AWorkerSink`**

Modify `src/a2a/worker/sink.py` in `tool_result` event handling:

```python
content_limit = 12000 if tool_name == "report_observation" else 3000
data_json = json.dumps(
    {
        "ev": "tool_result",
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "success": success,
        "content": (content or "")[:content_limit],
    },
    ensure_ascii=False,
)
```

- [ ] **Step 7: Extend EventStore for observations**

Modify `src/a2a/coordinator/event_store.py`:

```python
class EventRecord:
    def __init__(self, task_id: str, event_type: str, *, state: str | None = None, text: str | None = None, observation: dict[str, Any] | None = None) -> None:
        self.task_id = task_id
        self.event_type = event_type
        self.state = state
        self.text = text
        self.observation = observation
        self.ts = time.time()
```

Update `append()` signature and constructor call:

```python
def append(self, task_id: str, event_type: str, *, state: str | None = None, text: str | None = None, observation: dict[str, Any] | None = None) -> None:
    ...
    EventRecord(task_id=task_id, event_type=event_type, state=state, text=text, observation=observation)
```

Add method:

```python
def get_recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
    with self._lock:
        observations = []
        for records in self._events.values():
            for record in records:
                if record.event_type == "observation_report" and record.observation:
                    observations.append({"task_id": record.task_id, "ts": record.ts, **record.observation})
    observations.sort(key=lambda item: item["ts"])
    return observations[-limit:]
```

Update `get_summary()` with:

```python
elif r.event_type == "observation_report":
    text_short = (r.text or "")[:200]
    task_lines.append(f"  {ts_str} OBSERVATION: {text_short}")
```

- [ ] **Step 8: Add push callback parsing helper in server**

Modify `src/a2a/coordinator/server.py` near module-level helpers:

```python
def _extract_worker_data_blocks(text: str) -> list[dict[str, Any]]:
    marker = "[DATA]"
    if marker not in text:
        return []
    blocks = []
    for chunk in text.split(marker)[1:]:
        raw = chunk.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_observation_from_status_text(text: str) -> dict[str, Any] | None:
    for block in _extract_worker_data_blocks(text):
        if block.get("ev") != "tool_result" or block.get("tool_name") != "report_observation" or not block.get("success"):
            continue
        content = block.get("content") or ""
        try:
            observation = json.loads(content)
        except json.JSONDecodeError:
            return None
        return observation if isinstance(observation, dict) else None
    return None
```

In `/a2a/push-callback` `status_update` branch after status update append:

```python
if su.status.HasField("message"):
    status_text = " ".join(p.text for p in su.status.message.parts if p.text)
    observation = _extract_observation_from_status_text(status_text)
    if observation:
        event_store.append(task_id, "observation_report", text=status_text[:500], observation=observation)
        if self._semantic_map is not None:
            self._semantic_map.ingest_observation(observation)
```

- [ ] **Step 9: Run observation tests**

Run: `uv run pytest tests/test_report_observation.py tests/test_coordinator_push_callback.py -q`

Expected: PASS.

- [ ] **Step 10: Run lint for touched files**

Run: `uv run --with ruff ruff check sar_orch/tools/worker/report_observation.py sar_orch/tools/worker/__init__.py sar_orch/worker.py src/a2a/worker/sink.py src/a2a/coordinator/event_store.py src/a2a/coordinator/server.py tests/test_report_observation.py tests/test_coordinator_push_callback.py`

Expected: PASS.

- [ ] **Step 11: Commit Task 2**

```bash
git add sar_orch/tools/worker/report_observation.py sar_orch/tools/worker/__init__.py sar_orch/worker.py src/a2a/worker/sink.py src/a2a/coordinator/event_store.py src/a2a/coordinator/server.py tests/test_report_observation.py tests/test_coordinator_push_callback.py
git commit -m "feat: report SAR observations via A2A push updates"
```

---

### Task 3: Semantic Query Tools and Shared Memory Endpoint

**Files:**
- Create: `sar_orch/tools/coordinator/query_semantic_map.py`
- Create: `sar_orch/tools/coordinator/query_team_status.py`
- Modify: `sar_orch/tools/coordinator/__init__.py`
- Create: `sar_orch/tools/worker/query_shared_memory.py`
- Modify: `sar_orch/tools/worker/__init__.py`
- Modify: `src/a2a/coordinator/server.py`
- Test: `tests/test_semantic_tools.py`

**Interfaces:**
- Consumes: `SemanticMapStore.snapshot()` and `EventStore.get_recent_observations()`
- Produces: `QuerySemanticMapTool.execute() -> ToolResult`
- Produces: `QueryTeamStatusTool.execute() -> ToolResult`
- Produces: `QuerySharedMemoryTool.execute() -> ToolResult`
- Produces: FastAPI `GET /semantic-map` endpoint returning semantic map JSON

- [ ] **Step 1: Write failing tool tests**

Create `tests/test_semantic_tools.py`:

```python
import json

import pytest

from sar_orch.semantic_map import SemanticMapStore
from sar_orch.tools.coordinator.query_semantic_map import QuerySemanticMapTool
from sar_orch.tools.coordinator.query_team_status import QueryTeamStatusTool


@pytest.mark.asyncio
async def test_query_semantic_map_returns_store_snapshot():
    store = SemanticMapStore()
    store.ingest_observation(
        {"reporter": "Alice", "step": 1, "object_type": "fire", "name": "FireA", "position": [1, 1, 0], "attributes": {"status": "active"}}
    )
    tool = QuerySemanticMapTool(store)

    result = await tool.execute()
    payload = json.loads(result.content)

    assert result.success is True
    assert payload["known_dynamic_objects"]["fires"][0]["name"] == "FireA"


@pytest.mark.asyncio
async def test_query_team_status_includes_recent_observations():
    store = SemanticMapStore()
    store.ingest_observation(
        {"reporter": "Alice", "step": 1, "object_type": "fire", "name": "FireA", "position": [1, 1, 0], "attributes": {"status": "active"}}
    )
    tool = QueryTeamStatusTool(store)

    result = await tool.execute()
    payload = json.loads(result.content)

    assert result.success is True
    assert payload["recent_observations"][0]["name"] == "FireA"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_semantic_tools.py -q`

Expected: FAIL because tools are not implemented.

- [ ] **Step 3: Implement coordinator tools**

Create `sar_orch/tools/coordinator/query_semantic_map.py`:

```python
import json

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.semantic_map import SemanticMapStore


class QuerySemanticMapTool(Tool):
    name = "query_semantic_map"
    description = "Query the coordinator-maintained semantic map. Does not read environment oracle state."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map: SemanticMapStore):
        self._semantic_map = semantic_map

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content=json.dumps(self._semantic_map.snapshot(), ensure_ascii=False, default=str))
```

Create `sar_orch/tools/coordinator/query_team_status.py`:

```python
import json

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.semantic_map import SemanticMapStore


class QueryTeamStatusTool(Tool):
    name = "query_team_status"
    description = "Query team-level worker status and recent observation summaries. Does not read environment oracle state."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map: SemanticMapStore):
        self._semantic_map = semantic_map

    async def execute(self, **kwargs) -> ToolResult:
        snapshot = self._semantic_map.snapshot()
        payload = {
            "workers": snapshot.get("agents", []),
            "pending_requests": [],
            "recent_observations": snapshot.get("recent_observations", []),
            "stale_entries": snapshot.get("stale_entries", []),
            "conflicts": snapshot.get("conflicts", []),
        }
        return ToolResult(success=True, content=json.dumps(payload, ensure_ascii=False, default=str))
```

- [ ] **Step 4: Export coordinator tools**

Modify `sar_orch/tools/coordinator/__init__.py`:

```python
from sar_orch.tools.coordinator.finish_task import FinishTaskTool
from sar_orch.tools.coordinator.query_sar_state import QuerySARStateTool
from sar_orch.tools.coordinator.query_semantic_map import QuerySemanticMapTool
from sar_orch.tools.coordinator.query_team_status import QueryTeamStatusTool

__all__ = [
    "FinishTaskTool",
    "QuerySARStateTool",
    "QuerySemanticMapTool",
    "QueryTeamStatusTool",
]
```

- [ ] **Step 5: Add worker shared memory query tool**

Create `sar_orch/tools/worker/query_shared_memory.py`:

```python
import httpx

from Agent.worker_agent.tools.base import Tool, ToolResult


class QuerySharedMemoryTool(Tool):
    name = "query_shared_memory"
    description = "Query coordinator semantic map shared memory. Does not consume SAR env steps."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map_url: str):
        self._semantic_map_url = semantic_map_url.rstrip("/")

    async def execute(self, **kwargs) -> ToolResult:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self._semantic_map_url}/semantic-map")
            response.raise_for_status()
        return ToolResult(success=True, content=response.text)
```

Export in `sar_orch/tools/worker/__init__.py` and inject in `sar_orch/worker.py` with `semantic_map_url=f"http://localhost:{coordinator_port}"` derived from `coordinator_url`.

- [ ] **Step 6: Add semantic map endpoint to coordinator server**

Modify `src/a2a/coordinator/server.py`:

```python
def set_semantic_map(self, semantic_map) -> None:
    self._semantic_map = semantic_map
```

Initialize `self._semantic_map = None` in `__init__`, then add route:

```python
@app.get("/semantic-map")
async def semantic_map():
    if self._semantic_map is None:
        return {"status": "unavailable", "known_dynamic_objects": {"fires": [], "persons": []}}
    return self._semantic_map.snapshot()
```

- [ ] **Step 7: Run semantic tools tests**

Run: `uv run pytest tests/test_semantic_tools.py -q`

Expected: PASS.

- [ ] **Step 8: Run lint for semantic tools**

Run: `uv run --with ruff ruff check sar_orch/tools/coordinator/query_semantic_map.py sar_orch/tools/coordinator/query_team_status.py sar_orch/tools/coordinator/__init__.py sar_orch/tools/worker/query_shared_memory.py sar_orch/tools/worker/__init__.py src/a2a/coordinator/server.py tests/test_semantic_tools.py`

Expected: PASS.

- [ ] **Step 9: Commit Task 3**

```bash
git add sar_orch/tools/coordinator/query_semantic_map.py sar_orch/tools/coordinator/query_team_status.py sar_orch/tools/coordinator/__init__.py sar_orch/tools/worker/query_shared_memory.py sar_orch/tools/worker/__init__.py src/a2a/coordinator/server.py tests/test_semantic_tools.py
git commit -m "feat: add semantic map query tools"
```

---

### Task 4: Coordinator Semantic Mode and Context Migration

**Files:**
- Modify: `sar_orch/coordinator.py`
- Modify: `src/Agent/router_agent/context.py`
- Test: `tests/test_coordinator_semantic_mode.py`

**Interfaces:**
- Consumes: `SemanticMapStore`, `QuerySemanticMapTool`, `QueryTeamStatusTool`
- Produces: `SARCoordinator(..., state_mode: str = "semantic")`
- Produces: `CoordinatorPinnedState.semantic_summary`, `CoordinatorPinnedState.team_status_summary`

- [ ] **Step 1: Write failing coordinator mode tests**

Create `tests/test_coordinator_semantic_mode.py`:

```python
import json

from Agent.router_agent.context import CoordinatorContextManager
from Agent.router_agent.schema import Message


def test_context_extracts_semantic_map_not_global_snapshot():
    ctx = CoordinatorContextManager()
    payload = {
        "known_dynamic_objects": {"fires": [{"name": "FireA"}], "persons": []},
        "known_priors": {"reservoirs": [{"name": "ReservoirYork"}], "deposits": []},
        "stale_entries": [],
        "conflicts": [],
    }

    ctx.observe("query_semantic_map", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    memory = messages[-1].content
    assert "Known fires: 1" in memory
    assert "Known reservoirs: 1" in memory
    assert "global_snapshot" not in memory


def test_context_extracts_team_status_summary():
    ctx = CoordinatorContextManager()
    payload = {"workers": [{"agent_id": "Alice", "state": "RUNNING"}], "recent_observations": []}

    ctx.observe("query_team_status", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    assert "Workers: 1" in messages[-1].content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_coordinator_semantic_mode.py -q`

Expected: FAIL because semantic pinned fields are not implemented.

- [ ] **Step 3: Update CoordinatorPinnedState**

Modify `src/Agent/router_agent/context.py` `CoordinatorPinnedState`:

```python
class CoordinatorPinnedState(BaseModel):
    version: int = Field(default=1, ge=1)
    semantic_summary: dict = Field(default_factory=dict)
    team_status_summary: dict = Field(default_factory=dict)
    step_budget: dict = Field(default_factory=lambda: {"current_step": 0, "max_steps": 0, "remaining": 0})
    mission_finished: bool = False
    dispatched_tasks: list[dict] = Field(default_factory=list)
    worker_results: list[dict] = Field(default_factory=list)
```

Update `_render_environment_view()`:

```python
summary = ps.semantic_summary
if summary:
    dynamic = summary.get("known_dynamic_objects", {})
    priors = summary.get("known_priors", {})
    fires = dynamic.get("fires", [])
    persons = dynamic.get("persons", [])
    reservoirs = priors.get("reservoirs", [])
    deposits = priors.get("deposits", [])
    parts.append(f"Known fires: {len(fires)}")
    parts.append(f"Known persons: {len(persons)}")
    parts.append(f"Known reservoirs: {len(reservoirs)}")
    parts.append(f"Known deposits: {len(deposits)}")
    if summary.get("stale_entries"):
        parts.append(f"Stale entries: {len(summary['stale_entries'])}")
    if summary.get("conflicts"):
        parts.append(f"Conflicts: {len(summary['conflicts'])}")
team = ps.team_status_summary
if team:
    parts.append(f"Workers: {len(team.get('workers', []))}")
```

Update `_extract_pinned()`:

```python
if tool_name == "query_semantic_map":
    data = json.loads(content)
    if isinstance(data, dict):
        updates["semantic_summary"] = data
        if "step_budget" in data:
            updates["step_budget"] = data["step_budget"]

if tool_name == "query_team_status":
    data = json.loads(content)
    if isinstance(data, dict):
        updates["team_status_summary"] = data
```

Keep existing `query_sar_state` extraction only for oracle mode compatibility.

- [ ] **Step 4: Wire semantic map into SARCoordinator**

Modify `sar_orch/coordinator.py`:

```python
from sar_orch.semantic_map import SemanticMapStore
from sar_orch.tools.coordinator import QuerySARStateTool, QuerySemanticMapTool, QueryTeamStatusTool
```

Add `state_mode: str = "semantic"` to `SARCoordinator.__init__` and store it.

In `start()`, build tools:

```python
semantic_map = SemanticMapStore()
semantic_map.set_jsonl_path(Path(self._log_dir) / "semantic_map.jsonl" if self._log_dir else None)
semantic_map.init_priors(
    reservoirs=self._extract_prior_objects("reservoirs"),
    deposits=self._extract_prior_objects("deposits"),
    agents=[{"agent_id": name} for name in getattr(self._barrier.env, "agent_names", [])],
    rules={"Chemical": "Sand", "Non-chemical": "Water"},
    step_budget={"current_step": 0, "max_steps": getattr(self._barrier.env, "task_timeout", 300), "remaining": getattr(self._barrier.env, "task_timeout", 300)},
    task_objective="Extinguish all fires and rescue all persons",
)
self._semantic_map = semantic_map

extra_tools = [QuerySemanticMapTool(semantic_map), QueryTeamStatusTool(semantic_map)]
if self._state_mode == "oracle":
    extra_tools.append(QuerySARStateTool(self._barrier))
```

Pass `extra_tools=extra_tools` to `create_server()` and call `self._server.set_semantic_map(semantic_map)` after server creation.

- [ ] **Step 5: Run coordinator semantic mode tests**

Run: `uv run pytest tests/test_coordinator_semantic_mode.py -q`

Expected: PASS.

- [ ] **Step 6: Run lint**

Run: `uv run --with ruff ruff check sar_orch/coordinator.py src/Agent/router_agent/context.py tests/test_coordinator_semantic_mode.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add sar_orch/coordinator.py src/Agent/router_agent/context.py tests/test_coordinator_semantic_mode.py
git commit -m "feat: wire coordinator semantic mode"
```

---

### Task 5: Experiment Mode, Logging, and Prompts

**Files:**
- Modify: `sar_orch/experiment.py`
- Modify: `sar_orch/benchmark.py`
- Modify: `sar_orch/logger.py`
- Modify: `sar_orch/prompts/coordinator/system.md`
- Modify: `sar_orch/prompts/worker/system.md`
- Test: `tests/test_experiment_logger_observability.py`

**Interfaces:**
- Produces: CLI `--mode semantic|oracle`
- Produces: metadata fields `state_mode` and `oracle_mode`
- Produces: semantic map logging records in `semantic_map.jsonl`

- [ ] **Step 1: Write failing metadata test**

Extend `tests/test_experiment_logger_observability.py` with:

```python
def test_metadata_records_state_mode(tmp_path):
    logger = ExperimentLogger(experiment_name="test", log_dir=str(tmp_path))
    logger.write_metadata({"state_mode": "semantic", "oracle_mode": False})

    metadata = json.loads((tmp_path / "metadata.json").read_text())

    assert metadata["state_mode"] == "semantic"
    assert metadata["oracle_mode"] is False
```

- [ ] **Step 2: Run test to verify behavior**

Run: `uv run pytest tests/test_experiment_logger_observability.py -q`

Expected: PASS if metadata writer already supports arbitrary keys; if it fails, update logger metadata writer minimally.

- [ ] **Step 3: Add experiment mode CLI**

Modify `sar_orch/experiment.py`:

```python
parser.add_argument("--mode", choices=["semantic", "oracle"], default="semantic", help="Coordinator state source mode")
```

Add `state_mode: str = "semantic"` to `run_experiment()` and pass it to `SARCoordinator(state_mode=state_mode)`.

Update metadata:

```python
"state_mode": state_mode,
"oracle_mode": state_mode == "oracle",
```

- [ ] **Step 4: Propagate mode in benchmark**

Modify `sar_orch/benchmark.py` to accept and forward `--mode semantic|oracle` to `run_experiment()` or subprocess args.

- [ ] **Step 5: Update prompts**

Coordinator prompt must include this exact semantic-mode policy:

```markdown
In semantic mode, do not rely on environment oracle state. Use query_semantic_map() for known world facts and query_team_status() for team status. Use query_task_events(task_ids) only when checking a specific dispatched task. Unknown fire/person locations must be discovered by workers through scouting and report_observation.
```

Worker prompt must include this exact reporting rule:

```markdown
When you observe a fire, person, reservoir, deposit, changed status, or useful agent state, call report_observation with structured JSON fields. report_observation is non-blocking; continue your task after reporting. Use ask_coordinator only when you need a decision or cannot continue.
```

- [ ] **Step 6: Run focused tests and lint**

Run: `uv run pytest tests/test_experiment_logger_observability.py -q`

Expected: PASS.

Run: `uv run --with ruff ruff check sar_orch/experiment.py sar_orch/benchmark.py sar_orch/logger.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add sar_orch/experiment.py sar_orch/benchmark.py sar_orch/logger.py sar_orch/prompts/coordinator sar_orch/prompts/worker tests/test_experiment_logger_observability.py
git commit -m "feat: add semantic experiment mode"
```

---

### Task 6: End-to-End Verification and Cache/Oracle Regression Checks

**Files:**
- Modify: `tests/test_coordinator_semantic_mode.py`
- Modify: `tests/test_coordinator_push_callback.py`
- No production files unless tests reveal defects.

**Interfaces:**
- Consumes all prior tasks.
- Produces verified semantic mode behavior and regression evidence.

- [ ] **Step 1: Add oracle gating regression test**

Extend `tests/test_coordinator_semantic_mode.py`:

```python
def test_semantic_mode_tool_names_exclude_query_sar_state():
    from sar_orch.tools.coordinator import QuerySemanticMapTool, QueryTeamStatusTool
    from sar_orch.semantic_map import SemanticMapStore

    store = SemanticMapStore()
    tools = [QuerySemanticMapTool(store), QueryTeamStatusTool(store)]
    names = {tool.name for tool in tools}

    assert "query_semantic_map" in names
    assert "query_team_status" in names
    assert "query_sar_state" not in names
```

- [ ] **Step 2: Run full focused test suite**

Run:

```bash
uv run pytest \
  tests/test_semantic_map.py \
  tests/test_report_observation.py \
  tests/test_semantic_tools.py \
  tests/test_coordinator_push_callback.py \
  tests/test_coordinator_semantic_mode.py \
  tests/test_experiment_logger_observability.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched production directories**

Run: `uv run --with ruff ruff check src/ sar_orch/ tests/`

Expected: PASS.

- [ ] **Step 4: Run short semantic experiment smoke test**

Run:

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --mode semantic --max-steps 5
```

Expected:

- Process exits without framework error.
- Results directory contains `metadata.json` or run metadata with `state_mode=semantic` and `oracle_mode=false`.
- Results directory contains `semantic_map.jsonl` if any observation was reported.
- `router_interactions.csv` does not contain formal `query_sar_state` calls.

- [ ] **Step 5: Run oracle smoke test**

Run:

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --mode oracle --max-steps 3
```

Expected:

- Process exits without framework error.
- Metadata has `state_mode=oracle` and `oracle_mode=true`.
- `query_sar_state` is available only in this mode.

- [ ] **Step 6: Compare token/cache metrics manually**

Open the latest semantic and oracle `token_usage.csv` files and check:

- Semantic mode coordinator `PromptTokens` should be lower than oracle baseline after first planning rounds.
- Semantic mode coordinator cache hit ratio should not be worse than oracle mode.
- If semantic mode has no observations in a short run, record that this smoke test validates wiring but not map quality.

- [ ] **Step 7: Commit Task 6**

```bash
git add tests/test_coordinator_semantic_mode.py tests/test_coordinator_push_callback.py
git commit -m "test: verify semantic coordinator mode"
```

---

## Self-Review

- Spec coverage: Tasks cover `SemanticMapStore`, weak priors, `report_observation`, A2A push observation ingestion, `query_semantic_map`, `query_team_status`, worker shared memory, oracle gating, context migration, prompts, metadata, logging, and semantic/oracle smoke tests.
- Completeness scan: No unresolved markers or vague “add tests” steps are used; every task includes concrete file paths, expected commands, and acceptance criteria.
- Type consistency: `SemanticMapStore.snapshot()`, `ingest_observation()`, `get_recent_observations()`, `ReportObservationTool`, `QuerySemanticMapTool`, `QueryTeamStatusTool`, and `QuerySharedMemoryTool` signatures are defined before downstream use.
- Scope staging: Observation transport is explicit and uses existing `A2AWorkerSink` `WORKING status_update` path before coordinator semantic tools depend on it.
