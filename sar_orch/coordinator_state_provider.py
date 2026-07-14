from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from Agent.router_agent.state_provider import RuntimeState

if TYPE_CHECKING:
    from sar_orch.barrier import SARBarrier
    from sar_orch.semantic_map import SemanticMapStore
    from a2a.coordinator.supervision_state_store import SupervisionStateStore
    from a2a.coordinator.event_store import EventStore
    from a2a.coordinator.task_store import TaskStore


class SARCoordinatorStateProvider:
    """Read-only runtime state provider for the SAR Coordinator.

    Bridges SAR-specific backends (barrier, semantic map, event store, task
    store, supervision store) into the generic RuntimeState DTO consumed by
    CoordinatorContextManager. Implementations are lightweight snapshot reads;
    no I/O or sensor acquisition is triggered here.
    """

    def __init__(
        self,
        barrier: "SARBarrier | None" = None,
        semantic_map: "SemanticMapStore | None" = None,
        event_store: "EventStore | None" = None,
        state_mode: str = "semantic",
        supervision_state_store: "SupervisionStateStore | None" = None,
    ) -> None:
        self._barrier = barrier
        self._semantic_map = semantic_map
        self._event_store = event_store
        self._state_mode = state_mode
        self._supervision_state_store = supervision_state_store
        self._task_store: "TaskStore | None" = None
        self._last_version: int = -1
        self._last_snapshot: RuntimeState | None = None

    def set_task_store(self, task_store: "TaskStore | None") -> None:
        """Attach the per-request TaskStore once it is created."""
        self._task_store = task_store

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Uses the SAR env step as the version. If the env step has not changed
        since the last call, returns the cached snapshot to avoid redundant
        serialization. On refresh failure, returns the most recent snapshot with
        stale=True and refresh_error set; if no prior snapshot exists, returns
        an empty stale snapshot.
        """
        try:
            env_step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
            version = env_step
            if version == self._last_version and self._last_snapshot is not None:
                return self._last_snapshot

            payload: dict[str, Any] = {
                "state_mode": self._state_mode,
                "mission_finished": (
                    self._barrier.is_finished() if self._barrier is not None else False
                ),
            }

            # Step budget (semantic map is authoritative; barrier as fallback)
            if self._semantic_map is not None:
                payload["step_budget"] = dict(self._semantic_map.step_budget)
            elif self._barrier is not None:
                max_steps = getattr(self._barrier.env, "task_timeout", 50)
                payload["step_budget"] = {
                    "current_step": env_step,
                    "max_steps": max_steps,
                    "remaining": max(0, max_steps - env_step),
                }
            else:
                payload["step_budget"] = {
                    "current_step": 0,
                    "max_steps": 0,
                    "remaining": 0,
                }

            if self._state_mode == "semantic":
                payload["semantic_summary"] = (
                    self._semantic_map.snapshot()
                    if self._semantic_map is not None
                    else {}
                )
                payload["team_status_summary"] = self._build_team_status()
            elif self._state_mode == "oracle":
                payload["global_snapshot"] = (
                    self._barrier.get_env_snapshot()
                    if self._barrier is not None
                    else {}
                )

            payload["task_status_view"] = self._build_task_status_view()
            payload["recent_changes"] = self._build_recent_changes()
            payload["supervision"] = self._build_supervision_view()

            snapshot = RuntimeState(
                version=version,
                env_step=env_step,
                observed_at=time.monotonic(),
                payload=payload,
                stale=False,
                refresh_error="",
            )
            self._last_version = version
            self._last_snapshot = snapshot
            return snapshot
        except Exception as exc:  # pragma: no cover - defensive fallback
            if self._last_snapshot is not None:
                return replace(
                    self._last_snapshot,
                    stale=True,
                    refresh_error=str(exc),
                )
            return RuntimeState(
                version=0,
                env_step=0,
                observed_at=time.monotonic(),
                payload={},
                stale=True,
                refresh_error=str(exc),
            )

    def _build_team_status(self) -> dict[str, Any]:
        """Build a team-level summary from the semantic map."""
        if self._semantic_map is None:
            return {
                "workers": [],
                "pending_requests": [],
                "recent_observations": [],
                "stale_entries": [],
                "conflicts": [],
            }
        snap = self._semantic_map.snapshot()
        return {
            "workers": snap.get("agents", []),
            "pending_requests": [],
            "recent_observations": snap.get("recent_observations", []),
            "stale_entries": snap.get("stale_entries", []),
            "conflicts": snap.get("conflicts", []),
        }

    def _build_task_status_view(self) -> list[dict[str, Any]]:
        """Build a structured view of active/running tasks.

        If no TaskStore is attached (e.g., before agentic execution begins or
        during route planning), returns an empty list.
        """
        if self._event_store is None or self._task_store is None:
            return []

        views: list[dict[str, Any]] = []
        for node in self._task_store.get_plan():
            dispatch_id = node.task_id
            worker_id = node.worker_id or ""
            worker_task_id = self._task_store._dispatch_to_worker.get(dispatch_id, "")

            state_record = self._event_store.get_task_state(
                dispatch_id, worker_task_id or None
            )
            state = state_record.get("state", "UNKNOWN")
            latest_result = node.result or state_record.get("text", "")
            help_request = ""
            if state == "INPUT_REQUIRED":
                help_request = state_record.get("text", "")

            view: dict[str, Any] = {
                "dispatch_id": dispatch_id,
                "worker_task_id": worker_task_id,
                "worker_id": worker_id,
                "state": state,
                "latest_result": latest_result,
                "help_request": help_request,
                "updated_at": state_record.get("updated_at", ""),
                "acknowledged_by_coordinator": node.state != "pending",
            }

            # Merge supervision fields from SupervisionStateStore (Phase 3)
            if self._supervision_state_store is not None:
                sup = self._supervision_state_store.get(dispatch_id)
                if sup is not None:
                    view["supervision_state"] = sup.supervision_state
                    view["last_contact_at"] = sup.last_contact_at
                    view["last_progress_at"] = sup.last_progress_at
                    view["last_state_change_at"] = sup.last_state_change_at
                    view["active_alerts"] = dict(sup.active_alerts)
                    view["terminal"] = sup.terminal

            views.append(view)
        return views

    def _build_supervision_view(self) -> dict[str, Any]:
        """Build a supervision summary from SupervisionStateStore.

        Includes unacknowledged actionable events, active alerts, and watchdog
        health. Empty if no supervision store is attached.
        """
        if self._supervision_state_store is None:
            return {"alerts": [], "unacknowledged_events": []}

        alerts: list[dict[str, Any]] = []
        unacknowledged: list[dict[str, Any]] = []
        for state in self._supervision_state_store.list_states():
            if state.active_alerts:
                alerts.append(
                    {
                        "dispatch_id": state.dispatch_id,
                        "worker_id": state.worker_id,
                        "supervision_state": state.supervision_state,
                        "active_alerts": dict(state.active_alerts),
                        "last_contact_at": state.last_contact_at,
                        "last_progress_at": state.last_progress_at,
                        "terminal": state.terminal,
                    }
                )
            unacknowledged.extend(state.unacknowledged_events)

        return {
            "alerts": alerts,
            "unacknowledged_events": unacknowledged,
        }

    def _build_recent_changes(self) -> list[str]:
        """Summarize recent observations as human-readable change lines."""
        if self._event_store is None:
            return []
        recent_obs = self._event_store.get_recent_observations(limit=5)
        lines: list[str] = []
        for obs in recent_obs:
            obj_type = obs.get("object_type", "object")
            name = obs.get("name", "unknown")
            step = obs.get("step", 0)
            note = obs.get("note", "")
            if note:
                lines.append(f"{obj_type} {name} at step {step}: {note}")
            else:
                lines.append(f"{obj_type} {name} observed at step {step}")
        return lines
