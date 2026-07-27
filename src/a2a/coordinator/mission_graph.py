"""Pure logical MissionGraph DAG (Phase 1).

MissionGraph is the mission-scoped logical truth for agentic orchestration.
Physical dispatch records remain owned exclusively by MissionRuntime.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence

logger = logging.getLogger(__name__)

LogicalState = Literal[
    "planned",
    "blocked",
    "ready",
    "activating",
    "active",
    "completed",
    "failed",
    "canceled",
]

_TERMINAL_LOGICAL: frozenset[str] = frozenset({"completed", "failed", "canceled"})
_ACTIVE_CLAIM: frozenset[str] = frozenset({"activating", "active"})
_SUCCESSFUL_DEP: frozenset[str] = frozenset({"completed"})
_DECLARATIVE_STATUS: frozenset[str] = frozenset({"pending", "skipped"})


class MissionGraphError(ValueError):
    """Deterministic validation / lifecycle error for MissionGraph."""


@dataclass(frozen=True)
class MissionNodeSpec:
    """Declarative (LLM-owned) plan fields for one logical node.

    Nested collections are defensively copied and frozen so callers cannot
    mutate plan inputs after construction.
    """

    logical_id: str
    participant_ids: tuple[str, ...] = ()
    worker_id: str | None = None
    depends_on: tuple[str, ...] = ()
    assignments: Mapping[str, str] = field(default_factory=dict)
    objective: str = ""
    status: str = "pending"  # declarative only: pending | skipped

    def __post_init__(self) -> None:
        object.__setattr__(self, "participant_ids", tuple(self.participant_ids or ()))
        object.__setattr__(self, "depends_on", tuple(self.depends_on or ()))
        object.__setattr__(
            self,
            "assignments",
            MappingProxyType(dict(self.assignments or {})),
        )


@dataclass
class MissionNodeRuntime:
    """System-owned runtime view of one logical node."""

    logical_id: str
    participant_ids: list[str]
    depends_on: list[str]
    assignments: dict[str, str]
    objective: str
    status: str  # declarative status (pending/skipped)
    state: LogicalState = "planned"
    dispatch_ids: dict[str, str] = field(default_factory=dict)
    team_id: str | None = None
    team_epoch: int | None = None
    failure_reason: str = ""
    terminal_workers: set[str] = field(default_factory=set)
    results: dict[str, Any] = field(default_factory=dict)

    def copy_shallow(self) -> "MissionNodeRuntime":
        return MissionNodeRuntime(
            logical_id=self.logical_id,
            participant_ids=list(self.participant_ids),
            depends_on=list(self.depends_on),
            assignments=dict(self.assignments),
            objective=self.objective,
            status=self.status,
            state=self.state,
            dispatch_ids=dict(self.dispatch_ids),
            team_id=self.team_id,
            team_epoch=self.team_epoch,
            failure_reason=self.failure_reason,
            terminal_workers=set(self.terminal_workers),
            results=dict(self.results),
        )


def _normalize_spec(spec: MissionNodeSpec | dict[str, Any]) -> MissionNodeSpec:
    if isinstance(spec, MissionNodeSpec):
        participant_ids = list(spec.participant_ids)
        worker_id = spec.worker_id
        depends_on = list(spec.depends_on)
        assignments = dict(spec.assignments)
        objective = spec.objective
        status = spec.status
        logical_id = spec.logical_id
    else:
        logical_id = spec.get("logical_id") or spec.get("task_id")
        if not logical_id:
            raise MissionGraphError("missing logical_id")
        logical_id = str(logical_id)
        participant_ids = list(spec.get("participant_ids") or [])
        worker_id = spec.get("worker_id")
        depends_on = list(spec.get("depends_on") or [])
        assignments = dict(spec.get("assignments") or {})
        objective = str(spec.get("objective") or spec.get("description") or "")
        status = str(spec.get("status") or "pending")

    if not participant_ids and worker_id:
        participant_ids = [str(worker_id)]

    if status not in _DECLARATIVE_STATUS:
        raise MissionGraphError(
            f"status must be pending|skipped for {logical_id}, got {status!r}"
        )

    return MissionNodeSpec(
        logical_id=str(logical_id),
        participant_ids=tuple(participant_ids),
        worker_id=worker_id,
        depends_on=tuple(depends_on),
        assignments=assignments,
        objective=objective,
        status=status,
    )


def _semantic_key(node: MissionNodeRuntime | MissionNodeSpec) -> tuple:
    return (
        tuple(node.participant_ids),
        tuple(node.depends_on),
        node.objective,
        tuple(sorted(dict(node.assignments).items())),
        node.status,
    )


def _semantic_field_changes(
    previous: MissionNodeRuntime, spec: MissionNodeSpec
) -> dict[str, dict[str, Any]]:
    """Return before→after for fields that differ between old runtime and new spec."""
    changes: dict[str, dict[str, Any]] = {}
    old_participants = list(previous.participant_ids)
    new_participants = list(spec.participant_ids)
    if old_participants != new_participants:
        changes["participants"] = {
            "before": old_participants,
            "after": new_participants,
        }
    old_deps = list(previous.depends_on)
    new_deps = list(spec.depends_on)
    if old_deps != new_deps:
        changes["depends_on"] = {"before": old_deps, "after": new_deps}
    if previous.objective != spec.objective:
        changes["objective"] = {
            "before": previous.objective,
            "after": spec.objective,
        }
    old_assignments = dict(previous.assignments)
    new_assignments = dict(spec.assignments)
    if old_assignments != new_assignments:
        changes["assignments"] = {
            "before": old_assignments,
            "after": new_assignments,
        }
    if previous.status != spec.status:
        changes["status"] = {"before": previous.status, "after": spec.status}
    return changes


def _detect_cycle(nodes: dict[str, MissionNodeSpec]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise MissionGraphError(f"cycle detected involving {node_id}")
        visiting.add(node_id)
        for dep in nodes[node_id].depends_on:
            if dep in nodes:
                dfs(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        dfs(node_id)


def _validate_specs(
    specs: list[MissionNodeSpec | dict[str, Any]],
) -> dict[str, MissionNodeSpec]:
    if not isinstance(specs, list):
        raise MissionGraphError("specs must be a list")

    by_id: dict[str, MissionNodeSpec] = {}
    for raw in specs:
        node = _normalize_spec(raw)
        if not node.logical_id:
            raise MissionGraphError("empty logical_id")
        if node.logical_id in by_id:
            raise MissionGraphError(f"duplicate logical_id: {node.logical_id}")
        if not node.participant_ids:
            raise MissionGraphError(
                f"participant_ids must be non-empty for {node.logical_id}"
            )
        if len(node.participant_ids) != len(set(node.participant_ids)):
            raise MissionGraphError(
                f"participant_ids must be unique for {node.logical_id}"
            )
        for key in node.assignments:
            if key not in node.participant_ids:
                raise MissionGraphError(
                    f"assignment key {key!r} is not a declared participant "
                    f"of {node.logical_id}"
                )
        if node.logical_id in node.depends_on:
            raise MissionGraphError(
                f"self-dependency not allowed for {node.logical_id}"
            )
        by_id[node.logical_id] = node

    for node in by_id.values():
        for dep in node.depends_on:
            if dep not in by_id:
                raise MissionGraphError(
                    f"unknown dependency {dep!r} referenced by {node.logical_id}"
                )

    _detect_cycle(by_id)
    return by_id


def _node_history(node: MissionNodeRuntime) -> dict[str, Any]:
    """Serialize one logical node for the replace-history record."""
    return {
        "logical_id": node.logical_id,
        "state": node.state,
        "status": node.status,
        "participant_ids": list(node.participant_ids),
        "depends_on": list(node.depends_on),
        "assignments": dict(node.assignments),
        "objective": node.objective,
        "dispatch_ids": dict(node.dispatch_ids),
        "team_id": node.team_id,
        "team_epoch": node.team_epoch,
        "terminal_workers": sorted(node.terminal_workers),
        "failure_reason": node.failure_reason,
    }


class MissionGraphJsonlLogger:
    """Append MissionGraph replace-history records to a JSONL file.

    step_getter: optional callable returning the current environment step.
    Thread-safe for concurrent single-writer use.
    """

    def __init__(
        self,
        path: str | Path,
        step_getter: Callable[[], int] | None = None,
    ) -> None:
        self._path = Path(path)
        self._step_getter = step_getter
        self._lock = Lock()

    def __call__(self, record: dict[str, Any]) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": self._step_getter() if self._step_getter is not None else None,
            **record,
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class MissionGraph:
    """Pure logical DAG store for one Mission."""

    def __init__(self) -> None:
        self._nodes: dict[str, MissionNodeRuntime] = {}
        self._revision: int = 0
        self._lock = RLock()
        self._history_sink: Callable[[dict[str, Any]], None] | None = None

    @property
    def revision(self) -> int:
        return self._revision

    def set_history_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach an observer invoked with a history record after each successful replace()."""
        self._history_sink = sink

    def _emit_history(self, record: dict[str, Any]) -> None:
        sink = self._history_sink
        if sink is None:
            return
        try:
            sink(record)
        except Exception:
            logger.warning("MissionGraph history sink failed", exc_info=True)

    def replace(
        self, specs: Sequence[MissionNodeSpec | dict[str, Any]]
    ) -> dict[str, Any]:
        """Atomically replace the declarative graph after full validation."""
        normalized = _validate_specs(list(specs))
        with self._lock:
            old = self._nodes

            # Reject removal / semantic mutation of activating or active nodes.
            for old_id, old_node in old.items():
                if old_node.state not in _ACTIVE_CLAIM:
                    continue
                new_spec = normalized.get(old_id)
                if new_spec is None:
                    raise MissionGraphError(
                        f"cannot remove {old_node.state} node {old_id}"
                    )
                if list(new_spec.participant_ids) != list(old_node.participant_ids):
                    raise MissionGraphError(
                        f"cannot change participants of {old_node.state} node {old_id}"
                    )
                if list(new_spec.depends_on) != list(old_node.depends_on):
                    raise MissionGraphError(
                        f"cannot change dependencies of {old_node.state} node {old_id}"
                    )
                if new_spec.objective != old_node.objective:
                    raise MissionGraphError(
                        f"cannot change objective of {old_node.state} node {old_id}"
                    )
                if dict(new_spec.assignments) != dict(old_node.assignments):
                    raise MissionGraphError(
                        f"cannot change assignments of {old_node.state} node {old_id}"
                    )
                if new_spec.status != old_node.status:
                    raise MissionGraphError(
                        f"cannot change status of {old_node.state} node {old_id}"
                    )
                if new_spec.status == "skipped" and old_node.state in _ACTIVE_CLAIM:
                    raise MissionGraphError(
                        f"cannot mark {old_node.state} node {old_id} as skipped"
                    )

            added: list[dict[str, Any]] = []
            modified: list[dict[str, Any]] = []
            preserved: list[str] = []
            frozen: list[str] = []
            reset: list[str] = []

            merged: dict[str, MissionNodeRuntime] = {}
            for logical_id, spec in normalized.items():
                previous = old.get(logical_id)
                if previous is None:
                    added.append(
                        {
                            "logical_id": logical_id,
                            "participant_ids": list(spec.participant_ids),
                            "depends_on": list(spec.depends_on),
                            "objective": spec.objective,
                        }
                    )
                    runtime = MissionNodeRuntime(
                        logical_id=logical_id,
                        participant_ids=list(spec.participant_ids),
                        depends_on=list(spec.depends_on),
                        assignments=dict(spec.assignments),
                        objective=spec.objective,
                        status=spec.status,
                        state="planned",
                    )
                elif _semantic_key(previous) == _semantic_key(spec):
                    preserved.append(logical_id)
                    if previous.state in _ACTIVE_CLAIM:
                        frozen.append(logical_id)
                    runtime = previous.copy_shallow()
                    runtime.objective = spec.objective
                    runtime.assignments = dict(spec.assignments)
                    # Do not overwrite system state with declarative status except
                    # for non-active nodes where skipped is declarative.
                    if (
                        runtime.state not in _ACTIVE_CLAIM
                        and runtime.state not in _TERMINAL_LOGICAL
                    ):
                        runtime.status = spec.status
                    else:
                        # Keep declarative status only if not forcing skipped on active.
                        if runtime.state not in _ACTIVE_CLAIM:
                            runtime.status = spec.status
                    runtime.participant_ids = list(spec.participant_ids)
                    runtime.depends_on = list(spec.depends_on)
                else:
                    changes = _semantic_field_changes(previous, spec)
                    modified.append({"logical_id": logical_id, "changes": changes})
                    reset.append(logical_id)
                    runtime = MissionNodeRuntime(
                        logical_id=logical_id,
                        participant_ids=list(spec.participant_ids),
                        depends_on=list(spec.depends_on),
                        assignments=dict(spec.assignments),
                        objective=spec.objective,
                        status=spec.status,
                        state="planned",
                    )
                merged[logical_id] = runtime

            removed = sorted(old_id for old_id in old if old_id not in normalized)

            self._nodes = merged
            self._recompute_frontier()
            self._revision += 1
            view = self.snapshot_view()
            diff = {
                "added": added,
                "removed": removed,
                "modified": modified,
                "preserved": preserved,
                "frozen": frozen,
                "reset": reset,
            }
            view["diff"] = diff
            history = {
                "revision": self._revision,
                "changed": bool(added or removed or modified),
                "diff": diff,
                "ready": view["ready"],
                "state_counts": view["state_counts"],
                "nodes": [
                    _node_history(node)
                    for _, node in sorted(merged.items(), key=lambda kv: kv[0])
                ],
            }
        self._emit_history(history)
        return view

    def get_node(self, task_id: str) -> MissionNodeRuntime | None:
        with self._lock:
            node = self._nodes.get(task_id)
            return node.copy_shallow() if node is not None else None

    def get_ready_nodes(self) -> list[MissionNodeRuntime]:
        with self._lock:
            return [
                node.copy_shallow()
                for node in self._nodes.values()
                if node.state == "ready"
            ]

    def get_active_nodes(self) -> list[MissionNodeRuntime]:
        with self._lock:
            return [
                node.copy_shallow()
                for node in self._nodes.values()
                if node.state in _ACTIVE_CLAIM
            ]

    def can_activate(self, task_id: str) -> tuple[bool, str]:
        with self._lock:
            node = self._nodes.get(task_id)
            if node is None:
                return False, "node_not_found"
            if node.state != "ready":
                if node.state == "blocked":
                    return False, "dependency_incomplete"
                return False, "node_not_ready"
            return True, ""

    def mark_activating(self, task_id: str) -> None:
        with self._lock:
            node = self._require(task_id)
            if node.state in _TERMINAL_LOGICAL:
                raise MissionGraphError(f"node {task_id} is terminal")
            if node.state not in {"ready", "activating"}:
                raise MissionGraphError(
                    f"node {task_id} cannot enter activating from {node.state}"
                )
            node.state = "activating"

    def mark_active(self, task_id: str) -> None:
        with self._lock:
            node = self._require(task_id)
            if node.state in _TERMINAL_LOGICAL:
                raise MissionGraphError(f"node {task_id} is terminal")
            if node.state not in {"activating", "active", "ready"}:
                raise MissionGraphError(
                    f"node {task_id} cannot enter active from {node.state}"
                )
            node.state = "active"

    def attach_dispatch(self, task_id: str, worker_id: str, dispatch_id: str) -> None:
        with self._lock:
            node = self._require(task_id)
            if worker_id not in node.participant_ids:
                raise MissionGraphError(
                    f"worker {worker_id} is not a participant of {task_id}"
                )
            if node.state in _TERMINAL_LOGICAL:
                raise MissionGraphError(f"node {task_id} is terminal")
            node.dispatch_ids[worker_id] = dispatch_id

    def attach_dispatches(self, task_id: str, bindings: Mapping[str, str]) -> None:
        """Validate all worker/bindings first, then publish dispatch ids atomically."""
        with self._lock:
            node = self._require(task_id)
            if node.state in _TERMINAL_LOGICAL:
                raise MissionGraphError(f"node {task_id} is terminal")
            if not bindings:
                raise MissionGraphError("bindings must be non-empty")
            for worker_id, dispatch_id in bindings.items():
                if worker_id not in node.participant_ids:
                    raise MissionGraphError(
                        f"worker {worker_id} is not a participant of {task_id}"
                    )
                if not dispatch_id:
                    raise MissionGraphError(
                        f"empty dispatch_id for worker {worker_id} on {task_id}"
                    )
            # Publish only after full validation (all-or-none graph attachment).
            node.dispatch_ids = {
                **node.dispatch_ids,
                **{str(k): str(v) for k, v in bindings.items()},
            }

    def mark_dispatch_terminal(
        self,
        task_id: str,
        worker_id: str,
        terminal_state: str,
        result: Any = None,
    ) -> LogicalState:
        """Aggregate one participant physical terminal into the logical node.

        Returns the resulting logical state. Idempotent for already-terminal nodes
        and already-recorded worker terminals.
        """
        with self._lock:
            node = self._require(task_id)
            if node.state in _TERMINAL_LOGICAL:
                return node.state

            if worker_id not in node.participant_ids:
                raise MissionGraphError(
                    f"worker {worker_id} is not a participant of {task_id}"
                )

            terminal = str(terminal_state).upper()
            if terminal not in {"COMPLETED", "FAILED", "CANCELED", "CANCELLED"}:
                raise MissionGraphError(f"not a terminal state: {terminal_state}")

            if worker_id in node.terminal_workers:
                return node.state

            node.terminal_workers.add(worker_id)
            if result is not None:
                node.results[worker_id] = result

            if terminal in {"FAILED", "CANCELED", "CANCELLED"}:
                node.state = "failed"
                if not node.failure_reason:
                    node.failure_reason = str(result or terminal)
                self._recompute_frontier()
                return node.state

            # COMPLETED path
            if node.terminal_workers.issuperset(node.participant_ids):
                # All participants terminal; any non-completed would have failed above.
                node.state = "completed"
            elif node.state not in _ACTIVE_CLAIM:
                node.state = "active"
            self._recompute_frontier()
            return node.state

    def set_team_info(self, task_id: str, team_id: str, team_epoch: int) -> None:
        """Write team topology into a logical node for context projection."""
        with self._lock:
            node = self._require(task_id)
            node.team_id = team_id
            node.team_epoch = team_epoch

    def mark_canceled(self, task_id: str, reason: str = "") -> None:
        with self._lock:
            node = self._require(task_id)
            if node.state in _TERMINAL_LOGICAL:
                return
            node.state = "canceled"
            node.failure_reason = reason or node.failure_reason
            self._recompute_frontier()

    def get_claimed_workers(self) -> set[str]:
        """Return all workers currently claimed by activating/active nodes."""
        with self._lock:
            claimed: set[str] = set()
            for node in self._nodes.values():
                if node.state in _ACTIVE_CLAIM:
                    claimed.update(node.participant_ids)
            return claimed

    def participant_is_busy(self, worker_id: str) -> bool:
        """Check if a worker is claimed by any non-terminal activating/active node."""
        return worker_id in self.get_claimed_workers()

    def snapshot_view(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for node in self._nodes.values():
                counts[node.state] = counts.get(node.state, 0) + 1
            ready = sorted(
                n.logical_id for n in self._nodes.values() if n.state == "ready"
            )
            return {
                "revision": self._revision,
                "nodes": len(self._nodes),
                "state_counts": counts,
                "ready": ready,
                "node_ids": sorted(self._nodes),
            }

    def _require(self, task_id: str) -> MissionNodeRuntime:
        node = self._nodes.get(task_id)
        if node is None:
            raise MissionGraphError(f"node_not_found: {task_id}")
        return node

    def _dependency_successful(self, dep_id: str) -> bool:
        dep = self._nodes.get(dep_id)
        if dep is None:
            return False
        if dep.status == "skipped":
            return True
        return dep.state in _SUCCESSFUL_DEP

    def _recompute_frontier(self) -> None:
        for node in self._nodes.values():
            if node.state in _TERMINAL_LOGICAL or node.state in _ACTIVE_CLAIM:
                continue
            if node.status == "skipped":
                # Skipped is declarative exemption; keep non-active planned-ish state.
                node.state = "planned"
                continue
            if not node.depends_on:
                node.state = "ready"
                continue
            if all(self._dependency_successful(dep) for dep in node.depends_on):
                node.state = "ready"
            else:
                node.state = "blocked"
