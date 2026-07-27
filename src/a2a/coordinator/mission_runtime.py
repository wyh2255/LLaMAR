"""Phase 0/4 mission lifecycle ownership, physical dispatch fencing, and DAG activation.

Phase 4 adds: activate_plan_node (DAG gate + atomic claim + Team ACK saga +
awaited fan-out) with cancellation compensation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class MissionAdmissionError(RuntimeError):
    """Stable error returned when a Coordinator already owns a mission."""

    code = "mission_already_active"

    def __init__(self, context_id: str, active_context_id: str | None) -> None:
        self.context_id = context_id
        self.active_context_id = active_context_id
        super().__init__(self.code)


class PhysicalState(str, Enum):
    """Canonical state for one physical Worker dispatch."""

    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    CANCEL_PENDING = "CANCEL_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self in {
            PhysicalState.COMPLETED,
            PhysicalState.FAILED,
            PhysicalState.CANCELED,
        }


_TERMINAL_STATES = {
    PhysicalState.COMPLETED,
    PhysicalState.FAILED,
    PhysicalState.CANCELED,
}

_ALLOWED_TRANSITIONS: dict[PhysicalState, set[PhysicalState]] = {
    PhysicalState.PREPARED: {
        PhysicalState.DISPATCHING,
        PhysicalState.CANCEL_PENDING,
        PhysicalState.FAILED,
    },
    PhysicalState.DISPATCHING: {
        PhysicalState.ACCEPTED,
        PhysicalState.RUNNING,
        PhysicalState.CANCEL_PENDING,
        PhysicalState.FAILED,
    },
    PhysicalState.ACCEPTED: {
        PhysicalState.RUNNING,
        PhysicalState.INPUT_REQUIRED,
        PhysicalState.CANCEL_PENDING,
        PhysicalState.COMPLETED,
        PhysicalState.FAILED,
        PhysicalState.CANCELED,
    },
    PhysicalState.RUNNING: {
        PhysicalState.INPUT_REQUIRED,
        PhysicalState.CANCEL_PENDING,
        PhysicalState.COMPLETED,
        PhysicalState.FAILED,
        PhysicalState.CANCELED,
    },
    PhysicalState.INPUT_REQUIRED: {
        PhysicalState.RUNNING,
        PhysicalState.CANCEL_PENDING,
        PhysicalState.COMPLETED,
        PhysicalState.FAILED,
        PhysicalState.CANCELED,
    },
    PhysicalState.CANCEL_PENDING: {
        PhysicalState.CANCELED,
        PhysicalState.FAILED,
    },
    PhysicalState.COMPLETED: set(),
    PhysicalState.FAILED: set(),
    PhysicalState.CANCELED: set(),
}


@dataclass
class PhysicalDispatch:
    """Coordinator-owned physical dispatch record."""

    dispatch_id: str
    context_id: str
    logical_node_id: str
    worker_id: str
    worker_task_id: str | None = None
    state: PhysicalState = PhysicalState.PREPARED
    artifact: str | None = None
    result: Any | None = None
    finalization_seq: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class PartitionTransition:
    """Durable protocol DTO/fence for a future TeamPartition saga.

    Phase 0 records the transition shape only.  It does not install teams or
    become a second roster authority.
    """

    transition_id: str
    context_id: str
    before_members: tuple[str, ...] = ()
    after_members: tuple[str, ...] = ()
    status: str = "PREPARING"
    member_acks: dict[str, str] = field(default_factory=dict)
    member_leases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CallbackResult:
    """Result of routing a callback through the active runtime fence."""

    status: str
    reason: str = ""
    dispatch_id: str | None = None


DispatchAdapter = Callable[
    [str, str, str, str, str],
    Awaitable[str],
]


class MissionRuntime:
    """Context-bound mutable mission state and physical dispatch registry."""

    def __init__(
        self,
        manager: "MissionRuntimeManager",
        context_id: str,
        *,
        diagnostic_limit: int,
        cancel_adapter: Callable[[str, str], Awaitable[Any]] | None = None,
        dispatch_adapter: DispatchAdapter | None = None,
    ) -> None:
        self._manager = manager
        self.context_id = context_id
        self._diagnostic_limit = diagnostic_limit
        self._dispatches: dict[str, PhysicalDispatch] = {}
        self._worker_to_dispatch: dict[str, str] = {}
        self._futures: dict[str, asyncio.Future[Any]] = {}
        self._diagnostics: deque[dict[str, Any]] = deque(maxlen=diagnostic_limit)
        self._lock = RLock()
        self._aborted = False
        self._abort_reason: str | None = None
        self._cancel_adapter = cancel_adapter
        self._dispatch_adapter = dispatch_adapter
        self._status_owner: Any | None = None
        self._recovery_pending = False
        self._transition_seq = 0
        self._team_partition_service: Any | None = None

    @property
    def dispatches(self) -> dict[str, PhysicalDispatch]:
        return self._dispatches

    @property
    def dispatch_count(self) -> int:
        """Number of physical dispatch records owned by this runtime."""
        return len(self._dispatches)

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics)

    @property
    def futures(self) -> dict[str, asyncio.Future[Any]]:
        return self._futures

    @property
    def aborted(self) -> bool:
        return self._aborted

    def _diagnose(self, reason: str, **details: Any) -> None:
        self._diagnostics.append(
            {
                "reason": reason,
                "context_id": self.context_id,
                "at": datetime.now(timezone.utc).isoformat(),
                **details,
            }
        )
        self._manager._diagnose(reason, context_id=self.context_id, **details)

    def create_dispatch(self, logical_node_id: str, worker_id: str) -> PhysicalDispatch:
        """Allocate an opaque physical ID; logical IDs never alias it."""
        with self._lock:
            if self._aborted:
                raise RuntimeError("mission_runtime_aborted")
            dispatch = self._allocate_dispatch_unlocked(logical_node_id, worker_id)
            self._persist()
            return dispatch

    def create_dispatches(
        self, logical_node_id: str, worker_ids: list[str]
    ) -> list[PhysicalDispatch]:
        """Atomically allocate PREPARED physical dispatches before any network I/O."""
        with self._lock:
            if self._aborted:
                raise RuntimeError("mission_runtime_aborted")
            if not worker_ids:
                raise ValueError("worker_ids must be non-empty")
            allocated: list[PhysicalDispatch] = []
            try:
                for worker_id in worker_ids:
                    allocated.append(
                        self._allocate_dispatch_unlocked(logical_node_id, worker_id)
                    )
                self._persist()
                return allocated
            except Exception:
                for dispatch in allocated:
                    self._dispatches.pop(dispatch.dispatch_id, None)
                raise

    def rollback_prepared_dispatches(self, dispatch_ids: list[str]) -> int:
        """Rollback an explicit batch of still-PREPARED physical records only.

        MissionRuntime remains the unique physical authority: callers may only
        request removal of IDs they allocated and that never left PREPARED.
        Returns the number of records removed.
        """
        if not dispatch_ids:
            return 0
        with self._lock:
            removed = 0
            for dispatch_id in dispatch_ids:
                dispatch = self._dispatches.get(dispatch_id)
                if dispatch is None:
                    continue
                if dispatch.state is not PhysicalState.PREPARED:
                    continue
                self._dispatches.pop(dispatch_id, None)
                removed += 1
            if removed:
                self._persist()
            return removed

    def _allocate_dispatch_unlocked(
        self, logical_node_id: str, worker_id: str
    ) -> PhysicalDispatch:
        dispatch_id = f"dsp_{uuid.uuid4()}"
        while dispatch_id in self._dispatches:
            dispatch_id = f"dsp_{uuid.uuid4()}"
        dispatch = PhysicalDispatch(
            dispatch_id=dispatch_id,
            context_id=self.context_id,
            logical_node_id=logical_node_id,
            worker_id=worker_id,
        )
        self._dispatches[dispatch_id] = dispatch
        return dispatch

    def restore_dispatch(self, payload: dict[str, Any]) -> PhysicalDispatch:
        """Rebuild one persisted physical dispatch without allocating a new ID."""
        dispatch = PhysicalDispatch(
            dispatch_id=str(payload["dispatch_id"]),
            context_id=str(payload.get("context_id", self.context_id)),
            logical_node_id=str(payload.get("logical_node_id", "")),
            worker_id=str(payload.get("worker_id", "")),
            worker_task_id=payload.get("worker_task_id"),
            state=normalize_physical_state(payload.get("state"))
            or PhysicalState.PREPARED,
            artifact=payload.get("artifact"),
            result=payload.get("result"),
            finalization_seq=int(payload.get("finalization_seq", 0)),
            created_at=str(
                payload.get("created_at", datetime.now(timezone.utc).isoformat())
            ),
        )
        self._dispatches[dispatch.dispatch_id] = dispatch
        if dispatch.worker_task_id:
            self._worker_to_dispatch[dispatch.worker_task_id] = dispatch.dispatch_id
        return dispatch

    def get_dispatch(self, dispatch_id: str) -> PhysicalDispatch | None:
        """Look up only an explicit physical dispatch ID."""
        return self._dispatches.get(dispatch_id)

    def resolve_dispatch(self, identifier: str) -> PhysicalDispatch | None:
        """Public physical lookup; never falls back to a logical ID."""
        return self._dispatches.get(identifier)

    def register_worker_task(self, dispatch_id: str, worker_task_id: str) -> None:
        with self._lock:
            dispatch = self._dispatches.get(dispatch_id)
            if dispatch is None:
                self._diagnose("unknown_dispatch", dispatch_id=dispatch_id)
                return
            if self._aborted:
                self._diagnose("callback_owner_released", dispatch_id=dispatch_id)
                return
            dispatch.worker_task_id = worker_task_id
            self._worker_to_dispatch[worker_task_id] = dispatch_id
            self._persist()

    def set_dispatch_adapter(self, adapter: DispatchAdapter | None) -> None:
        """Inject the adapter that sends an A2A task and awaits worker acceptance."""
        self._dispatch_adapter = adapter

    def bind_status_owner(self, owner: Any | None) -> None:
        """Bind the TaskStore that projects physical transitions to PlanNode.

        The runtime remains the physical authority.  The owner is an explicit
        compatibility adapter so every public runtime transition also updates
        the one derived legacy view without creating a second state machine.
        """
        with self._lock:
            self._status_owner = owner

    def resolve_worker_task(self, worker_task_id: str) -> PhysicalDispatch | None:
        dispatch_id = self._worker_to_dispatch.get(worker_task_id)
        return self._dispatches.get(dispatch_id) if dispatch_id else None

    def register_future(self, dispatch_id: str) -> asyncio.Future[Any]:
        with self._lock:
            if dispatch_id not in self._dispatches:
                raise KeyError(dispatch_id)
            future = asyncio.get_running_loop().create_future()
            self._futures[dispatch_id] = future
            self._persist()
            return future

    def record_artifact(self, dispatch_id: str, artifact: str) -> bool:
        with self._lock:
            dispatch = self._dispatches.get(dispatch_id)
            if dispatch is None:
                self._diagnose("unknown_dispatch", dispatch_id=dispatch_id)
                return False
            if dispatch.state.terminal:
                self._diagnose(
                    "post_terminal_artifact",
                    dispatch_id=dispatch_id,
                    state=dispatch.state.value,
                )
                return False
            dispatch.artifact = artifact
            self._persist()
            return True

    def apply_worker_status(
        self,
        worker_task_id: str,
        raw_state: Any,
        *,
        source: str,
        result: Any | None = None,
    ) -> PhysicalDispatch | None:
        dispatch = self.resolve_worker_task(worker_task_id)
        if dispatch is None:
            self._diagnose("unknown_worker_task", worker_task_id=worker_task_id)
            return None
        return self.apply_physical_status(
            dispatch.dispatch_id,
            raw_state,
            source=source,
            result=result,
        )

    def cancel_dispatch(self, dispatch_id: str, *, reason: str = "cancel") -> bool:
        """Enter the remote-cancellation fence without claiming remote success."""
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is None or dispatch.state.terminal:
            return False
        self.apply_physical_status(
            dispatch_id, PhysicalState.CANCEL_PENDING, source=reason
        )
        return True

    async def cancel_dispatch_remote(
        self, dispatch_id: str, *, reason: str = "cancel"
    ) -> PhysicalDispatch | None:
        """Cancel a known Worker task and apply only the returned remote state."""
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is None or dispatch.state.terminal:
            return dispatch
        state_before_cancel = dispatch.state
        self.cancel_dispatch(dispatch_id, reason=reason)
        if not dispatch.worker_task_id:
            if state_before_cancel in {
                PhysicalState.DISPATCHING,
                PhysicalState.CANCEL_PENDING,
            }:
                self._diagnose(
                    "remote_task_id_registration_pending",
                    dispatch_id=dispatch_id,
                )
                return dispatch
            return self.apply_physical_status(
                dispatch_id, PhysicalState.CANCELED, source=reason
            )
        if self._cancel_adapter is None:
            self._diagnose("remote_cancel_unavailable", dispatch_id=dispatch_id)
            return dispatch
        try:
            remote_state = await self._cancel_adapter(
                dispatch.worker_id, dispatch.worker_task_id
            )
        except Exception as exc:
            self._diagnose(
                "remote_cancel_failed", dispatch_id=dispatch_id, error=str(exc)
            )
            return dispatch
        normalized = normalize_physical_state(remote_state)
        if normalized not in _TERMINAL_STATES:
            self._diagnose(
                "remote_cancel_not_terminal",
                dispatch_id=dispatch_id,
                remote_state=str(remote_state),
            )
            return dispatch
        return self.apply_physical_status(
            dispatch_id, normalized, source=f"{reason}_remote"
        )

    def apply_physical_status(
        self,
        dispatch_id: str,
        raw_state: Any,
        *,
        source: str,
        observed_at: Any | None = None,
        result: Any | None = None,
    ) -> PhysicalDispatch | None:
        """Apply a physical transition through the attached compatibility adapter."""
        owner = self._status_owner
        if owner is not None:
            return owner.apply_physical_status(
                dispatch_id,
                raw_state,
                source=source,
                observed_at=observed_at,
                result=result,
            )
        return self._apply_physical_status(
            dispatch_id,
            raw_state,
            source=source,
            observed_at=observed_at,
            result=result,
        )

    def _apply_physical_status(
        self,
        dispatch_id: str,
        raw_state: Any,
        *,
        source: str,
        observed_at: Any | None = None,
        result: Any | None = None,
    ) -> PhysicalDispatch | None:
        """Apply the only canonical physical state transition."""
        state = normalize_physical_state(raw_state)
        with self._lock:
            dispatch = self._dispatches.get(dispatch_id)
            if dispatch is None:
                self._diagnose(
                    "unknown_dispatch", dispatch_id=dispatch_id, source=source
                )
                return None
            if state is None:
                self._diagnose(
                    "unknown_state", dispatch_id=dispatch_id, raw_state=str(raw_state)
                )
                return dispatch
            if state == dispatch.state:
                self._diagnose(
                    "duplicate_state",
                    dispatch_id=dispatch_id,
                    state=state.value,
                    source=source,
                )
                return dispatch
            if state not in _ALLOWED_TRANSITIONS[dispatch.state]:
                self._diagnose(
                    "stale_or_invalid_transition",
                    dispatch_id=dispatch_id,
                    old_state=dispatch.state.value,
                    new_state=state.value,
                    source=source,
                )
                return dispatch
            dispatch.state = state
            if state in _TERMINAL_STATES:
                self._transition_seq += 1
                dispatch.finalization_seq = self._transition_seq
                if result is not None:
                    dispatch.result = result
            self._persist()
            future = self._futures.get(dispatch_id)
            if state in _TERMINAL_STATES and future is not None and not future.done():
                self._finish_future(
                    future,
                    state,
                    result if result is not None else dispatch.artifact,
                )
            return dispatch

    def set_team_partition_service(self, service: Any | None) -> None:
        """Inject the Coordinator-lifetime TeamPartitionService for abort reconcile."""
        self._team_partition_service = service

    async def abort(self, reason: str) -> None:
        """Finalize a runtime (route_strategy §8).

        Freeze new activations → bounded-wait cancel non-terminal dispatches →
        reconcile TeamPartition → clear mappings → release admission.
        Idempotent, re-entrant-safe under lock.
        """
        with self._lock:
            if self._aborted:
                return
            self._abort_reason = reason
            self._aborted = True
            dispatches = list(self._dispatches.values())
            futures = list(self._futures.values())

        cancel_tasks: list[asyncio.Task[Any]] = []
        for dispatch in dispatches:
            if dispatch.state.terminal:
                continue
            cancel_tasks.append(
                asyncio.create_task(
                    self.cancel_dispatch_remote(dispatch.dispatch_id, reason="abort")
                )
            )
        if cancel_tasks:
            done, pending = await asyncio.wait(cancel_tasks, timeout=30.0)
            for task in pending:
                task.cancel()

        with self._lock:
            for dispatch in dispatches:
                if not dispatch.state.terminal:
                    self._apply_physical_status(
                        dispatch.dispatch_id,
                        PhysicalState.CANCEL_PENDING,
                        source="abort_bounded_wait_expired",
                    )

        for future in futures:
            if not future.done():
                self._finish_future(future, PhysicalState.CANCELED)

        if self._team_partition_service is not None:
            try:
                await self._team_partition_service.release_node_team(
                    self.context_id, ack_timeout=10.0
                )
            except Exception:
                logger.exception(
                    "TeamPartition release failed during abort for %s", self.context_id
                )

        with self._lock:
            self._worker_to_dispatch.clear()
            self._futures.clear()
            has_non_terminal = any(
                not d.state.terminal for d in self._dispatches.values()
            )
            self._recovery_pending = has_non_terminal
            self._persist()

        self._manager._release(self)

    # ── Phase 4: DAG activation ────────────────────────────────────────

    async def activate_plan_node(
        self,
        logical_id: str,
        mission_graph: Any,
        team_service: Any | None = None,
        *,
        prompt_factory: Callable[
            [str, str, dict[str, str]], str
        ] = lambda obj, wid, assignments: (
            f"{obj}\n\nAssignment for {wid}: {assignments.get(wid, '')}"
        ),
        ack_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Activate a logical DAG node (Phase 4, steps 1–4).

        Returns:
            success=True, node_id, team_id, team_epoch, dispatches on success.
            success=False, error=code on failure.
        """
        if self._aborted:
            return {"success": False, "error": "mission_runtime_aborted"}

        # Phase 1: DAG gate (read-only pre-checks)
        gate = self._run_dag_gate(logical_id, mission_graph, team_service)
        if not gate["pass"]:
            return {
                "success": False,
                "error": gate["error"],
                "reason": gate.get("reason", ""),
            }

        node = gate["node"]
        participant_ids: list[str] = node.participant_ids

        # Phase 2: atomic claim (under lock)
        claim = self._run_atomic_claim(
            logical_id, participant_ids, mission_graph, team_service
        )
        if not claim["success"]:
            return {
                "success": False,
                "error": claim["error"],
                "reason": claim.get("reason", ""),
            }

        dispatch_ids: list[str] = claim["dispatch_ids"]

        # Phase 3: Team ACK saga
        transition = claim.get("transition")
        team_id = None
        team_epoch = None
        if transition is not None and team_service is not None:
            team_result = await self._run_team_ack_saga(
                transition, team_service, ack_timeout=ack_timeout
            )
            if not team_result["success"]:
                self._rollback_claim(dispatch_ids, mission_graph, logical_id)
                return {
                    "success": False,
                    "error": team_result["error"],
                    "reason": team_result.get("reason", ""),
                    "transition_status": team_result.get("transition_status"),
                }
            team_id = team_result.get("team_id")
            team_epoch = team_result.get("team_epoch")
            # Write team topology into the graph node so context projection works.
            if team_id is not None and team_epoch is not None:
                mission_graph.set_team_info(logical_id, team_id, team_epoch)

        # Phase 4: awaited fan-out acceptance
        fan_out = await self.dispatch_prepared_many(
            dispatch_ids,
            logical_id,
            participant_ids,
            node,
            mission_graph,
            prompt_factory=prompt_factory,
        )
        if not fan_out["success"]:
            return {
                "success": False,
                "error": "dispatch_acceptance_failed",
                "reason": fan_out.get("reason", ""),
                "accepted": fan_out.get("accepted", []),
                "failed": fan_out.get("failed", []),
            }

        return {
            "success": True,
            "node_id": logical_id,
            "team_id": team_id,
            "team_epoch": team_epoch,
            "dispatches": fan_out.get("dispatches", []),
        }

    def _run_dag_gate(
        self,
        logical_id: str,
        mission_graph: Any,
        team_service: Any | None,
    ) -> dict[str, Any]:
        """Phase 1: read-only pre-checks before any claim."""
        node = mission_graph.get_node(logical_id)
        if node is None:
            return {"pass": False, "error": "node_not_found"}
        can, reason = mission_graph.can_activate(logical_id)
        if not can:
            if reason == "dependency_incomplete":
                return {"pass": False, "error": "dependency_incomplete"}
            return {"pass": False, "error": "node_not_ready", "reason": reason}
        if self._aborted:
            return {"pass": False, "error": "mission_runtime_aborted"}
        # Check MissionGraph-level claim: any participant in another active node?
        for participant_id in node.participant_ids:
            if mission_graph.participant_is_busy(participant_id):
                return {
                    "pass": False,
                    "error": "participant_busy",
                    "reason": f"worker {participant_id} is claimed by another active node",
                }
        # Check TeamPartition-level fence (applies to all nodes).
        if team_service is not None:
            busy = team_service.registry.check_participants_free(node.participant_ids)
            if busy:
                return {
                    "pass": False,
                    "error": "participant_busy",
                    "reason": f"workers {busy} have non-terminal transition lease",
                }
        return {"pass": True, "node": node}

    def _run_atomic_claim(
        self,
        logical_id: str,
        participant_ids: list[str],
        mission_graph: Any,
        team_service: Any | None,
    ) -> dict[str, Any]:
        """Phase 2: atomic participant claim under lock.

        Re-runs final competition check, creates PREPARED dispatches,
        prepares team transition.  Any failure after graph mutation
        rolls back dispatches and marks the node failed (terminal but
        re-plannable).  TeamPartition `prepare_activation` is called
        last because it creates a durable fence that cannot be silently
        undone — if it succeeds, we own the fence until release.
        """
        with self._lock:
            if self._aborted:
                return {"success": False, "error": "mission_runtime_aborted"}

            # Re-verify ready state under lock (competition arbitration point).
            can, reason = mission_graph.can_activate(logical_id)
            if not can:
                return {
                    "success": False,
                    "error": "participant_busy",
                    "reason": f"node no longer ready: {reason}",
                }

            # Race-check MissionGraph-level claim under lock
            for participant_id in participant_ids:
                if mission_graph.participant_is_busy(participant_id):
                    return {
                        "success": False,
                        "error": "participant_busy",
                        "reason": f"worker {participant_id} claimed by another active node",
                    }

            # Race-check TeamPartition fence under lock
            if team_service is not None:
                busy = team_service.registry.check_participants_free(participant_ids)
                if busy:
                    return {
                        "success": False,
                        "error": "participant_busy",
                        "reason": f"workers {busy} have non-terminal transition lease",
                    }

            # ── Preallocate dispatches (no graph mutation yet) ──
            allocated: list[PhysicalDispatch] = []
            try:
                for worker_id in participant_ids:
                    allocated.append(
                        self._allocate_dispatch_unlocked(logical_id, worker_id)
                    )
                dispatch_ids = [d.dispatch_id for d in allocated]
                bindings = {d.worker_id: d.dispatch_id for d in allocated}
            except Exception as exc:
                for d in allocated:
                    self._dispatches.pop(d.dispatch_id, None)
                return {
                    "success": False,
                    "error": "dispatch_allocation_failed",
                    "reason": str(exc),
                }

            # ── Graph mutation + state transition ──
            try:
                mission_graph.attach_dispatches(logical_id, bindings)
                mission_graph.mark_activating(logical_id)
            except Exception as exc:
                for d in allocated:
                    self._dispatches.pop(d.dispatch_id, None)
                return {
                    "success": False,
                    "error": "dispatch_allocation_failed",
                    "reason": str(exc),
                }

            # ── TeamPartition transition (durable fence) ──
            # Only meaningful when a real delivery adapter can reach workers.
            # Without one, the Team ACK saga can never collect an ACK, so it
            # always times out to DEGRADED -- which retains its fence
            # permanently by design. That would fence every participant of
            # every multi-worker node forever after the first activation,
            # with no code path to release it. MissionGraph's own
            # _ACTIVE_CLAIM state machine already provides the actual
            # exclusivity guarantee (no two nodes claim the same worker);
            # this fence only adds real value on top of that when
            # inter-worker TEAM_UPDATE/REVOKE notification is actually wired.
            transition = None
            node_obj = mission_graph.get_node(logical_id)
            if (
                team_service is not None
                and len(participant_ids) > 1
                and getattr(team_service, "has_delivery_adapter", True)
            ):
                try:
                    transition = team_service.prepare_activation(
                        node_id=logical_id,
                        members=list(participant_ids),
                        objective=str(
                            getattr(node_obj, "objective", "") if node_obj else ""
                        ),
                        context_id=self.context_id,
                    )
                except Exception as exc:
                    # Graph is mutated but no team fence exists.
                    # Rollback dispatches and mark node failed.
                    for d in allocated:
                        self._dispatches.pop(d.dispatch_id, None)
                    mission_graph.mark_canceled(
                        logical_id, reason=f"team_preparation_failed: {exc}"
                    )
                    return {
                        "success": False,
                        "error": "team_preparation_failed",
                        "reason": str(exc),
                    }

            return {
                "success": True,
                "dispatch_ids": dispatch_ids,
                "transition": transition,
                "team_id": transition.team_id if transition else None,
                "team_epoch": transition.epoch if transition else None,
            }

    def _rollback_claim(
        self,
        dispatch_ids: list[str],
        mission_graph: Any,
        logical_id: str,
    ) -> None:
        """Rollback claim after team ACK failure (Phase 3 compensation complete)."""
        with self._lock:
            self.rollback_prepared_dispatches(dispatch_ids)
            mission_graph.mark_canceled(logical_id, reason="team_setup_failed")

    async def _run_team_ack_saga(
        self,
        transition: Any,
        team_service: Any,
        *,
        ack_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Phase 3: execute Team ACK saga via TeamPartitionService.

        Returns success=True on INSTALLED, False on COMPENSATED/DEGRADED.
        """
        try:
            result = await team_service.activate_node_team(
                transition, ack_timeout=ack_timeout
            )
        except Exception as exc:
            return {
                "success": False,
                "error": "team_ack_failed",
                "reason": str(exc),
                "transition_status": "ERROR",
            }

        from a2a.coordinator.team_partition_service import (
            TransitionStatus as _ts,
        )

        status = result.status
        if status == _ts.INSTALLED:
            return {
                "success": True,
                "team_id": result.team_id,
                "team_epoch": result.epoch,
            }
        if status == _ts.COMPENSATED:
            return {
                "success": False,
                "error": "team_setup_failed",
                "reason": "Team ACK failed; compensation succeeded",
                "transition_status": "COMPENSATED",
            }
        return {
            "success": False,
            "error": "team_setup_failed",
            "reason": f"Team ACK failed; transition={status.value}",
            "transition_status": status.value,
        }

    async def dispatch_prepared_many(
        self,
        dispatch_ids: list[str],
        logical_id: str,
        participant_ids: list[str],
        node: Any,
        mission_graph: Any,
        *,
        prompt_factory: Callable[
            [str, str, dict[str, str]], str
        ] = lambda obj, wid, assignments: (
            f"{obj}\n\nAssignment for {wid}: {assignments.get(wid, '')}"
        ),
    ) -> dict[str, Any]:
        """Phase 4 awaited fan-out: dispatch to all participants concurrently.

        Waits for each A2A acceptance.  On Nth failure, cancels already-accepted
        dispatches via canonical state machine and marks logical node failed.
        """
        adapter = self._dispatch_adapter
        if adapter is None:
            # No adapter configured: fail all dispatches and the logical node.
            for d_id in dispatch_ids:
                self.apply_physical_status(
                    d_id,
                    "FAILED",
                    source="activate_plan_node",
                    result="no dispatch adapter configured",
                )
            mission_graph.mark_dispatch_terminal(
                logical_id,
                participant_ids[0],
                "FAILED",
                result="no dispatch adapter configured",
            )
            return {
                "success": False,
                "reason": "no dispatch adapter configured",
                "accepted": [],
                "failed": [
                    {"worker_id": wid, "success": False, "reason": "no adapter"}
                    for wid in participant_ids
                ],
            }

        objective = str(getattr(node, "objective", ""))
        assignments = dict(getattr(node, "assignments", {}))
        callback_url = ""

        accepted: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        team_id = getattr(node, "team_id", None)
        team_epoch = getattr(node, "team_epoch", None)

        async def _dispatch_one(worker_id: str) -> dict[str, Any]:
            dispatch_id_candidate = None
            with self._lock:
                for d_id, d in self._dispatches.items():
                    if d.worker_id == worker_id and d.logical_node_id == logical_id:
                        dispatch_id_candidate = d_id
                        break
            if dispatch_id_candidate is None:
                return {
                    "worker_id": worker_id,
                    "success": False,
                    "reason": "dispatch_not_found",
                }

            prompt = prompt_factory(objective, worker_id, assignments)

            self.apply_physical_status(
                dispatch_id_candidate, "DISPATCHING", source="activate_plan_node"
            )

            try:
                worker_task_id = await adapter(
                    worker_id,
                    prompt,
                    callback_url,
                    dispatch_id_candidate,
                    self.context_id,
                )
                if not worker_task_id:
                    self.apply_physical_status(
                        dispatch_id_candidate,
                        "FAILED",
                        source="dispatch_acceptance_failed",
                        result="empty worker_task_id",
                    )
                    return {
                        "worker_id": worker_id,
                        "success": False,
                        "reason": "empty_worker_task_id",
                    }
            except Exception as exc:
                self.apply_physical_status(
                    dispatch_id_candidate,
                    "FAILED",
                    source="dispatch_acceptance_error",
                    result=str(exc),
                )
                return {
                    "worker_id": worker_id,
                    "success": False,
                    "reason": str(exc),
                }

            self.register_worker_task(dispatch_id_candidate, worker_task_id)
            with self._lock:
                d = self._dispatches.get(dispatch_id_candidate)
                if d is not None and d.state is PhysicalState.DISPATCHING:
                    self._apply_physical_status(
                        dispatch_id_candidate,
                        "ACCEPTED",
                        source="activation_acceptance",
                    )
            return {
                "worker_id": worker_id,
                "success": True,
                "dispatch_id": dispatch_id_candidate,
                "worker_task_id": worker_task_id,
            }

        tasks = [_dispatch_one(wid) for wid in participant_ids]
        outcomes = await asyncio.gather(*tasks, return_exceptions=False)

        for outcome in outcomes:
            if outcome["success"]:
                accepted.append(outcome)
            else:
                failed.append(outcome)

        if failed:
            # Cancel already-accepted dispatches via remote adapter if available.
            for acc in accepted:
                dispatch_id = acc["dispatch_id"]
                d = self._dispatches.get(dispatch_id)
                if d is None or d.state.terminal:
                    continue
                # Use cancel_dispatch_remote which calls the cancel_adapter
                # and applies the remote outcome (CANCELED/FAILED/CANCEL_PENDING).
                await self.cancel_dispatch_remote(
                    dispatch_id, reason="fan_out_failure_compensation"
                )

            mission_graph.mark_dispatch_terminal(
                logical_id,
                failed[0]["worker_id"],
                "FAILED",
                result=failed[0].get("reason", "dispatch_acceptance_failed"),
            )
            return {
                "success": False,
                "reason": f"{len(failed)} participant(s) acceptance failed",
                "accepted": accepted,
                "failed": failed,
            }

        # All accepted: mark node active.
        mission_graph.mark_active(logical_id)
        with self._lock:
            for acc in accepted:
                d = self._dispatches.get(acc["dispatch_id"])
                if d is not None and d.state is PhysicalState.ACCEPTED:
                    self._apply_physical_status(
                        acc["dispatch_id"],
                        "RUNNING",
                        source="activation_complete",
                    )

        return {
            "success": True,
            "dispatches": accepted,
            "accepted": accepted,
            "failed": [],
            "team_id": team_id,
            "team_epoch": team_epoch,
        }

    def _finish_future(
        self, future: asyncio.Future[Any], state: PhysicalState, result: Any = None
    ) -> None:
        """Finalize a Future only on the event loop that owns it."""
        loop = future.get_loop()

        def _finish() -> None:
            if future.done():
                return
            if state is PhysicalState.COMPLETED:
                future.set_result(result)
            elif state is PhysicalState.FAILED:
                future.set_exception(RuntimeError(str(result or state.value)))
            else:
                future.cancel()

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            _finish()
        elif loop.is_running():
            loop.call_soon_threadsafe(_finish)

    async def reconcile(self) -> bool:
        """Resolve all persisted non-terminal dispatches after a restart."""
        for dispatch in list(self._dispatches.values()):
            if not dispatch.state.terminal:
                await self.cancel_dispatch_remote(
                    dispatch.dispatch_id, reason="recovery"
                )
        return all(dispatch.state.terminal for dispatch in self._dispatches.values())

    def _persist(self) -> None:
        self._manager._persist_runtime(self)


class ActiveMissionAdmission:
    """Single active-context admission gate owned by MissionRuntimeManager."""

    def __init__(self, manager: "MissionRuntimeManager") -> None:
        self._manager = manager

    def admit(self, context_id: str) -> MissionRuntime:
        return self._manager.admit(context_id)

    def release(self, runtime: MissionRuntime) -> None:
        self._manager._release(runtime)


class MissionRuntimeManager:
    """Coordinator-lifetime owner for one active MissionRuntime."""

    def __init__(
        self,
        state_path: str | os.PathLike[str] | None = None,
        *,
        diagnostic_limit: int = 100,
        cancel_adapter: Callable[[str, str], Awaitable[Any]] | None = None,
        dispatch_adapter: DispatchAdapter | None = None,
    ) -> None:
        self.state_path = Path(state_path) if state_path is not None else None
        self.diagnostic_limit = diagnostic_limit
        self._cancel_adapter = cancel_adapter
        self._dispatch_adapter = dispatch_adapter
        self._lock = RLock()
        self._active_runtime: MissionRuntime | None = None
        self._epoch = 0
        self._diagnostics: deque[dict[str, Any]] = deque(maxlen=diagnostic_limit)
        self._recovery_required = False
        self._persisted_state: dict[str, Any] = {}
        self.admission = ActiveMissionAdmission(self)
        self._load_control_state()

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def active_runtime(self) -> MissionRuntime | None:
        return self._active_runtime

    @property
    def active_context_id(self) -> str | None:
        """Return the admitted context ID, if any."""
        return self._active_runtime.context_id if self._active_runtime else None

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics)

    def _diagnose(self, reason: str, **details: Any) -> None:
        self._diagnostics.append(
            {
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
                **details,
            }
        )

    def _load_control_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._persisted_state = state
            self._epoch = int(state.get("epoch", 0))
            self._recovery_required = bool(state.get("active_context_id"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning(
                "Ignoring unreadable Coordinator control state: %s", self.state_path
            )

    def _state_payload(self, runtime: MissionRuntime | None = None) -> dict[str, Any]:
        runtime = runtime or self._active_runtime
        dispatches = []
        if runtime is not None:
            for dispatch in runtime.dispatches.values():
                dispatches.append(
                    {
                        "dispatch_id": dispatch.dispatch_id,
                        "logical_node_id": dispatch.logical_node_id,
                        "worker_id": dispatch.worker_id,
                        "worker_task_id": dispatch.worker_task_id,
                        "state": dispatch.state.value,
                        "context_id": dispatch.context_id,
                        "artifact": dispatch.artifact,
                        "result": dispatch.result,
                        "finalization_seq": dispatch.finalization_seq,
                        "created_at": dispatch.created_at,
                    }
                )
        return {
            "epoch": self._epoch,
            "active_context_id": runtime.context_id if runtime is not None else None,
            "dispatches": dispatches,
        }

    def _persist(self) -> None:
        if self.state_path is None:
            return
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            payload = json.dumps(self._state_payload(), ensure_ascii=False).encode(
                "utf-8"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
            try:
                dir_fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                logger.debug("Could not fsync control-state directory: %s", path.parent)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _persist_runtime(self, runtime: MissionRuntime) -> None:
        with self._lock:
            if self._active_runtime is runtime:
                self._persist()

    def set_cancel_adapter(
        self, cancel_adapter: Callable[[str, str], Awaitable[Any]] | None
    ) -> None:
        """Install the native remote cancellation adapter once registries exist."""
        self._cancel_adapter = cancel_adapter
        if self._active_runtime is not None:
            self._active_runtime._cancel_adapter = cancel_adapter

    def set_dispatch_adapter(
        self, dispatch_adapter: DispatchAdapter | None
    ) -> None:
        """Install the production A2A fan-out adapter for activate_plan_node."""
        self._dispatch_adapter = dispatch_adapter
        if self._active_runtime is not None:
            self._active_runtime.set_dispatch_adapter(dispatch_adapter)

    def set_team_partition_service(self, service: Any | None) -> None:
        """Inject the Coordinator-lifetime TeamPartitionService for activation + abort wiring."""
        self._team_partition_service = service
        if self._active_runtime is not None:
            self._active_runtime.set_team_partition_service(service)

    def admit(self, context_id: str) -> MissionRuntime:
        if not context_id:
            raise ValueError("context_id is required")
        with self._lock:
            if self._recovery_required and self._active_runtime is None:
                self.recover()
            if self._active_runtime is not None and not self._active_runtime.aborted:
                raise MissionAdmissionError(context_id, self._active_runtime.context_id)
            runtime = MissionRuntime(
                self,
                context_id,
                diagnostic_limit=self.diagnostic_limit,
                cancel_adapter=self._cancel_adapter,
                dispatch_adapter=getattr(self, "_dispatch_adapter", None),
            )
            runtime.set_team_partition_service(
                getattr(self, "_team_partition_service", None)
            )
            self._active_runtime = runtime
            self._recovery_required = False
            self._persist()
            return runtime

    acquire = admit

    def try_admit(self, context_id: str) -> tuple[MissionRuntime | None, str | None]:
        try:
            return self.admit(context_id), None
        except MissionAdmissionError as exc:
            return None, exc.code

    def _release(self, runtime: MissionRuntime) -> None:
        with self._lock:
            if self._active_runtime is runtime:
                self._active_runtime = None
                self._persist()

    async def abort(self, reason: str = "coordinator_shutdown") -> None:
        runtime = self._active_runtime
        if runtime is not None:
            await runtime.abort(reason)

    def recover(self) -> int:
        """Advance the epoch and reconstruct persisted physical dispatch facts."""
        with self._lock:
            persisted_epoch = self._epoch
            state = self._persisted_state
            if self.state_path is not None and self.state_path.exists():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    self._persisted_state = state
                    persisted_epoch = max(persisted_epoch, int(state.get("epoch", 0)))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            self._epoch = persisted_epoch + 1
            context_id = state.get("active_context_id")
            persisted_dispatches = state.get("dispatches", [])
            self._active_runtime = None
            self._recovery_required = False
            if context_id and persisted_dispatches:
                runtime = MissionRuntime(
                    self,
                    str(context_id),
                    diagnostic_limit=self.diagnostic_limit,
                    cancel_adapter=self._cancel_adapter,
                )
                runtime.set_team_partition_service(
                    getattr(self, "_team_partition_service", None)
                )
                for payload in persisted_dispatches:
                    runtime.restore_dispatch(payload)
                if any(not d.state.terminal for d in runtime.dispatches.values()):
                    runtime._recovery_pending = True
                    self._active_runtime = runtime
                    self._recovery_required = True
            self._persist()
            return self._epoch

    async def reconcile_recovery(self) -> bool:
        """Reconcile recovered dispatches before releasing new admission."""
        runtime = self._active_runtime
        if runtime is None:
            self._recovery_required = False
            return True
        complete = await runtime.reconcile()
        if complete:
            runtime._aborted = True
            runtime._recovery_pending = False
            self._release(runtime)
            self._recovery_required = False
        else:
            self._recovery_required = True
            self._persist()
        return complete

    async def recover_and_reconcile(self) -> int:
        """Startup hook: fence a newer epoch, then reconcile before admission."""
        epoch = self.recover()
        await self.reconcile_recovery()
        return epoch

    def handle_callback(
        self,
        context_id: str | None,
        worker_task_id: str,
        raw_state: Any,
        *,
        source: str = "callback",
        result: Any | None = None,
    ) -> CallbackResult:
        runtime = self._active_runtime
        if runtime is None:
            self._diagnose("unknown_context_callback", context_id=context_id)
            return CallbackResult("ignored", "unknown_context")
        if context_id != runtime.context_id:
            self._diagnose(
                "stale_context_callback",
                context_id=context_id,
                active_context_id=runtime.context_id,
            )
            return CallbackResult("ignored", "stale_context")
        dispatch = runtime.resolve_worker_task(worker_task_id)
        if dispatch is None:
            runtime._diagnose("unknown_worker_task", worker_task_id=worker_task_id)
            return CallbackResult("ignored", "unknown_worker_task")
        previous_state = dispatch.state
        runtime.apply_physical_status(
            dispatch.dispatch_id,
            raw_state,
            source=source,
            result=result,
        )
        if dispatch.state is previous_state:
            # Still return dispatch_id so callers can route observation/payload
            # ingestion independently of physical state transitions.
            return CallbackResult(
                "ignored", "stale_transition", dispatch_id=dispatch.dispatch_id
            )
        return CallbackResult("ok", dispatch_id=dispatch.dispatch_id)

    def handle_artifact(
        self, context_id: str | None, worker_task_id: str, artifact: str
    ) -> CallbackResult:
        runtime = self._active_runtime
        if runtime is None or context_id != runtime.context_id:
            reason = "unknown_context" if runtime is None else "stale_context"
            self._diagnose("stale_or_unknown_artifact", context_id=context_id)
            return CallbackResult("ignored", reason)
        dispatch = runtime.resolve_worker_task(worker_task_id)
        if dispatch is None or not runtime.record_artifact(
            dispatch.dispatch_id, artifact
        ):
            return CallbackResult("ignored", "unknown_worker_task")
        return CallbackResult("ok", dispatch_id=dispatch.dispatch_id)

    def _load_state_for_test(self) -> dict[str, Any]:
        """Small read-only inspection seam used by deterministic lifecycle tests."""
        return self._state_payload()


def normalize_physical_state(raw_state: Any) -> PhysicalState | None:
    """Normalize A2A/protobuf/string states without importing SDK internals."""
    if isinstance(raw_state, PhysicalState):
        return raw_state
    if hasattr(raw_state, "name"):
        raw_state = raw_state.name
    if isinstance(raw_state, int):
        try:
            from a2a.types.a2a_pb2 import TaskState

            raw_state = TaskState.Name(raw_state)
        except (ImportError, ValueError):
            return None
    if not isinstance(raw_state, str):
        return None
    value = raw_state.upper().removeprefix("TASK_STATE_")
    aliases = {
        "SUBMITTED": PhysicalState.ACCEPTED,
        "WORKING": PhysicalState.RUNNING,
        "RUNNING": PhysicalState.RUNNING,
        "INPUT_REQUIRED": PhysicalState.INPUT_REQUIRED,
        "CANCELED": PhysicalState.CANCELED,
        "CANCELLED": PhysicalState.CANCELED,
        "COMPLETED": PhysicalState.COMPLETED,
        "FAILED": PhysicalState.FAILED,
        "REJECTED": PhysicalState.FAILED,
        "AUTH_REQUIRED": PhysicalState.FAILED,
        "PREPARED": PhysicalState.PREPARED,
        "DISPATCHING": PhysicalState.DISPATCHING,
        "ACCEPTED": PhysicalState.ACCEPTED,
        "CANCEL_PENDING": PhysicalState.CANCEL_PENDING,
    }
    return aliases.get(value)


__all__ = [
    "ActiveMissionAdmission",
    "CallbackResult",
    "MissionAdmissionError",
    "MissionRuntime",
    "MissionRuntimeManager",
    "PartitionTransition",
    "PhysicalDispatch",
    "PhysicalState",
    "normalize_physical_state",
]
