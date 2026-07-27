"""Phase 3 exclusive TeamPartition — Coordinator-owned partition topology.

TeamPartitionRegistry is the single authority for worker-to-team mapping.
Each online worker belongs to exactly one active team (default: solo:<id>).
Disjoint collaborative teams can coexist. Global epoch is monotonically
increasing across all transitions and persists across restarts.

PartitionTransition tracks the durable saga state machine:
  PREPARING -> INSTALLING -> INSTALLED (all ACKs)
                           -> COMPENSATING -> COMPENSATED (all compensation ACKs)
                                           -> DEGRADED (timeout/unrecoverable)

INSTALLED collaborative teams retain exclusive fences (I4); only
singleton INSTALLED or explicit release_node clears the claim.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MIN_TEAM_SECRET_BYTES = 32


class TransitionStatus(str, Enum):
    PREPARING = "PREPARING"
    INSTALLING = "INSTALLING"
    INSTALLED = "INSTALLED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    DEGRADED = "DEGRADED"
    RELEASE_PREPARING = "RELEASE_PREPARING"


_TERMINAL_TRANSITIONS = frozenset(
    {
        TransitionStatus.INSTALLED,
        TransitionStatus.COMPENSATED,
        TransitionStatus.DEGRADED,
    }
)

_SINGLETON_PREFIX = "solo:"
_COLLABORATIVE_PREFIX = "team:"


def singleton_team_id(worker_id: str) -> str:
    return f"{_SINGLETON_PREFIX}{worker_id}"


def _is_collaborative_team(team_id: str | None) -> bool:
    return bool(team_id and team_id.startswith(_COLLABORATIVE_PREFIX))


@dataclass
class TeamAssignment:
    team_id: str
    epoch: int
    member_ids: list[str]
    objective: str | None = None
    context_id: str | None = None
    node_id: str | None = None

    @property
    def is_singleton(self) -> bool:
        return self.team_id.startswith(_SINGLETON_PREFIX)


@dataclass
class MemberDelivery:
    worker_id: str
    ack_status: str = "pending"
    acked_at: str | None = None


@dataclass
class PartitionTransition:
    transition_id: str
    context_id: str
    source_node_id: str
    before: dict[str, TeamAssignment]
    after: dict[str, TeamAssignment]
    affected_workers: list[str]
    epoch: int
    status: TransitionStatus = TransitionStatus.PREPARING
    member_deliveries: dict[str, MemberDelivery] = field(default_factory=dict)
    team_secret: bytes | None = None
    team_id: str | None = None
    compensation_plan: str = ""
    reason: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ParticipantBusyError(RuntimeError):
    code = "participant_busy"

    def __init__(self, worker_id: str, detail: str = "") -> None:
        self.worker_id = worker_id
        self.detail = detail
        msg = (
            f"Worker {worker_id} is busy: {detail}"
            if detail
            else f"Worker {worker_id} is busy"
        )
        super().__init__(msg)


class TransitionError(RuntimeError):
    code = "transition_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
#  Lease / fence helpers — determine whether a transition holds a live claim
# ---------------------------------------------------------------------------


def _transition_holds_fence(t: PartitionTransition) -> bool:
    """Return True when *t* still holds an exclusive participant claim.

    Claimed (fenced) states:
      - PREPARING, INSTALLING: active saga
      - INSTALLED collaborative (team:...): exclusive membership active
      - COMPENSATING: active saga
      - DEGRADED: unreconciled fence

    Released states:
      - INSTALLED singleton (solo:...): released by definition
      - COMPENSATED: compensation completed, partitions restored
      - RELEASE_PREPARING: (intermediate, treated as busy)
    """
    if t.status in (
        TransitionStatus.PREPARING,
        TransitionStatus.INSTALLING,
        TransitionStatus.COMPENSATING,
    ):
        return True
    if t.status == TransitionStatus.INSTALLED:
        return _is_collaborative_team(t.team_id)
    if t.status == TransitionStatus.DEGRADED:
        return True
    return False


def _transition_releases_fence(t: PartitionTransition) -> bool:
    """Return True when completing *t* should release its workers' fences.

    Singleton/release INSTALLED and COMPENSATED release the claim.
    Collaborative INSTALLED, COMPENSATING, DEGRADED do not.
    """
    if t.status == TransitionStatus.INSTALLED:
        return not _is_collaborative_team(t.team_id)
    if t.status == TransitionStatus.COMPENSATED:
        return True
    return False


# ======================================================================
# TeamPartitionRegistry
# ======================================================================


class TeamPartitionRegistry:
    """Coordinator-owned exclusive partition registry.

    Thread-safe.  Maintains:
    - Exactly one active team per online worker (singleton or collaborative)
    - Disjoint collaborative teams never overlap (I4)
    - Monotonic global epoch across all transitions (I10)
    - Durable PartitionTransition journal with fence/lease lifecycle
    """

    def __init__(self, coordinator_id: str = "Coordinator") -> None:
        self._coordinator_id = coordinator_id
        self._lock = threading.RLock()
        self._global_epoch: int = 0
        self._team_partition_revision: int = 0
        self._partitions: dict[str, TeamAssignment] = {}
        self._transitions: dict[str, PartitionTransition] = {}
        self._fences: dict[str, str] = {}
        self._persisted_version: int = 0

    # ── Singleton management ──────────────────────────────────────────

    def ensure_singletons(self, online_workers: list[str]) -> list[TeamAssignment]:
        """Create solo:worker_id teams for all online workers not yet partitioned."""
        with self._lock:
            created: list[TeamAssignment] = []
            for worker_id in sorted(set(online_workers)):
                if worker_id not in self._partitions:
                    self._global_epoch += 1
                    assignment = TeamAssignment(
                        team_id=singleton_team_id(worker_id),
                        epoch=self._global_epoch,
                        member_ids=[worker_id],
                    )
                    self._partitions[worker_id] = assignment
                    created.append(assignment)
            if created:
                self._team_partition_revision += 1
            return created

    def get_assignment(self, worker_id: str) -> TeamAssignment | None:
        with self._lock:
            a = self._partitions.get(worker_id)
            if a is None:
                return None
            return TeamAssignment(
                team_id=a.team_id,
                epoch=a.epoch,
                member_ids=list(a.member_ids),
                objective=a.objective,
                context_id=a.context_id,
                node_id=a.node_id,
            )

    def collect_team_rosters(self) -> list[dict[str, Any]]:
        """Return one entry per unique team with aggregated members."""
        with self._lock:
            teams: dict[str, dict[str, Any]] = {}
            for worker_id, assignment in self._partitions.items():
                tid = assignment.team_id
                if tid not in teams:
                    teams[tid] = {
                        "team_id": tid,
                        "epoch": assignment.epoch,
                        "members": [],
                        "context_id": assignment.context_id,
                        "node_id": assignment.node_id,
                        "is_singleton": assignment.is_singleton,
                    }
                teams[tid]["members"].append(worker_id)
            result = list(teams.values())
            result.sort(key=lambda t: t["team_id"])
            return result

    @property
    def global_epoch(self) -> int:
        with self._lock:
            return self._global_epoch

    @property
    def team_partition_revision(self) -> int:
        """Monotonically increasing topology version for cache invalidation.

        Incremented on every partition assignment change (singleton creation,
        collaborative install, compensation revert, reconcile).  Used as cache
        key component for ``/team-status`` and Context Memory projections.
        """
        with self._lock:
            return self._team_partition_revision

    def advance_epoch(self) -> int:
        with self._lock:
            self._global_epoch += 1
            return self._global_epoch

    # ── Participant busy check ────────────────────────────────────────

    def check_participants_free(self, members: list[str]) -> list[str]:
        """Return list of busy workers; empty means all are free."""
        with self._lock:
            return self._check_participants_free_locked(members)

    # ── Activation lifecycle ──────────────────────────────────────────

    def prepare_activation(
        self,
        node_id: str,
        members: list[str],
        objective: str,
        context_id: str,
    ) -> PartitionTransition:
        """Pre-check and prepare transition, claiming all members exclusively.

        Raises ParticipantBusyError if any member is claimed by another
        non-terminal transition.
        """
        with self._lock:
            busy = self._check_participants_free_locked(members)
            if busy:
                raise ParticipantBusyError(
                    busy[0],
                    f"claimed by another transition (also busy: {busy[1:]})",
                )

            member_set = list(dict.fromkeys(members))
            self._global_epoch += 1
            new_epoch = self._global_epoch
            team_id = f"team:{node_id}:r{new_epoch}"
            team_secret = secrets.token_bytes(MIN_TEAM_SECRET_BYTES)

            before: dict[str, TeamAssignment] = {}
            after: dict[str, TeamAssignment] = {}
            for worker_id in member_set:
                existing = self._partitions.get(worker_id)
                if existing:
                    before[worker_id] = TeamAssignment(
                        team_id=singleton_team_id(worker_id),
                        epoch=existing.epoch,
                        member_ids=[worker_id],
                        context_id=existing.context_id,
                        node_id=existing.node_id,
                    )
                else:
                    before[worker_id] = TeamAssignment(
                        team_id=singleton_team_id(worker_id),
                        epoch=0,
                        member_ids=[worker_id],
                    )
                after[worker_id] = TeamAssignment(
                    team_id=team_id,
                    epoch=new_epoch,
                    member_ids=list(member_set),
                    objective=objective,
                    context_id=context_id,
                    node_id=node_id,
                )

            transition_id = f"tr_{uuid.uuid4().hex}"
            transition = PartitionTransition(
                transition_id=transition_id,
                context_id=context_id,
                source_node_id=node_id,
                before={k: v for k, v in before.items()},
                after={k: v for k, v in after.items()},
                affected_workers=list(member_set),
                epoch=new_epoch,
                status=TransitionStatus.PREPARING,
                team_secret=team_secret,
                team_id=team_id,
            )
            for worker_id in member_set:
                transition.member_deliveries[worker_id] = MemberDelivery(
                    worker_id=worker_id,
                    ack_status="pending",
                )

            self._transitions[transition_id] = transition
            for worker_id in member_set:
                self._fences[worker_id] = transition_id

            return self._copy_transition(transition)

    # ── Transition state advances ─────────────────────────────────────

    def mark_installing(self, transition_id: str) -> None:
        with self._lock:
            t = self._require_transition(transition_id)
            if t.status != TransitionStatus.PREPARING:
                raise TransitionError(f"Cannot mark INSTALLING from {t.status.value}")
            t.status = TransitionStatus.INSTALLING
            t.updated_at = datetime.now(timezone.utc).isoformat()

    def record_ack(
        self,
        transition_id: str,
        worker_id: str,
        *,
        success: bool = True,
        timeout: bool = False,
    ) -> None:
        with self._lock:
            t = self._transitions.get(transition_id)
            if t is None:
                return
            if worker_id not in t.member_deliveries:
                return
            if timeout:
                t.member_deliveries[worker_id] = MemberDelivery(
                    worker_id=worker_id,
                    ack_status="timeout",
                )
            elif success:
                t.member_deliveries[worker_id] = MemberDelivery(
                    worker_id=worker_id,
                    ack_status="acked",
                    acked_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                t.member_deliveries[worker_id] = MemberDelivery(
                    worker_id=worker_id,
                    ack_status="rejected",
                    acked_at=datetime.now(timezone.utc).isoformat(),
                )
            t.updated_at = datetime.now(timezone.utc).isoformat()

    def check_acks(self, transition_id: str) -> tuple[bool, list[str], list[str]]:
        """Return (all_acked, failed_workers, pending_workers)."""
        with self._lock:
            t = self._transitions.get(transition_id)
            if t is None:
                return False, [], []
            acked: list[str] = []
            failed: list[str] = []
            pending: list[str] = []
            for worker_id, md in t.member_deliveries.items():
                if md.ack_status == "acked":
                    acked.append(worker_id)
                elif md.ack_status in ("rejected", "timeout"):
                    failed.append(worker_id)
                else:
                    pending.append(worker_id)
            return len(failed) == 0 and len(pending) == 0, failed, pending

    def mark_installed(self, transition_id: str) -> PartitionTransition:
        with self._lock:
            t = self._require_transition(transition_id)
            if t.status != TransitionStatus.INSTALLING:
                raise TransitionError(f"Cannot mark INSTALLED from {t.status.value}")
            t.status = TransitionStatus.INSTALLED
            t.updated_at = datetime.now(timezone.utc).isoformat()
            for worker_id, after_assignment in t.after.items():
                self._partitions[worker_id] = after_assignment
            self._team_partition_revision += 1
            # Release fences only for singleton (non-collaborative) INSTALLED.
            # Collaborative INSTALLED retains exclusive claim per I4.
            if _transition_releases_fence(t):
                for worker_id in t.affected_workers:
                    self._fences.pop(worker_id, None)
            return self._copy_transition(t)

    def mark_compensating(self, transition_id: str) -> None:
        with self._lock:
            t = self._require_transition(transition_id)
            if t.status != TransitionStatus.INSTALLING:
                raise TransitionError(f"Cannot mark COMPENSATING from {t.status.value}")
            t.status = TransitionStatus.COMPENSATING
            t.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_compensated(self, transition_id: str) -> PartitionTransition:
        with self._lock:
            t = self._require_transition(transition_id)
            if t.status != TransitionStatus.COMPENSATING:
                raise TransitionError(f"Cannot mark COMPENSATED from {t.status.value}")
            t.status = TransitionStatus.COMPENSATED
            t.updated_at = datetime.now(timezone.utc).isoformat()
            for worker_id, before_assignment in t.before.items():
                self._partitions[worker_id] = before_assignment
            self._team_partition_revision += 1
            # COMPENSATED always releases fences (back to singleton / before state)
            for worker_id in t.affected_workers:
                self._fences.pop(worker_id, None)
            return self._copy_transition(t)

    def mark_degraded(self, transition_id: str) -> PartitionTransition:
        with self._lock:
            t = self._require_transition(transition_id)
            if t.status not in (
                TransitionStatus.INSTALLING,
                TransitionStatus.COMPENSATING,
            ):
                raise TransitionError(f"Cannot mark DEGRADED from {t.status.value}")
            t.status = TransitionStatus.DEGRADED
            t.updated_at = datetime.now(timezone.utc).isoformat()
            # DEGRADED retains fences per design §7.1
            return self._copy_transition(t)

    # ── Durable release saga ──────────────────────────────────────────

    def prepare_release(self, context_id: str) -> PartitionTransition | None:
        """Prepare a durable release transition for all workers in *context_id*.

        Returns a PREPARING transition (with fences set) or None if no
        collaborative workers exist for this context.  The caller must
        advance through the INSTALLING → INSTALLED | COMPENSATING saga
        to complete the release.
        """
        with self._lock:
            context_workers = [
                wid
                for wid, a in self._partitions.items()
                if a.context_id == context_id and not a.is_singleton
            ]
            if not context_workers:
                return None

            self._global_epoch += 1
            release_epoch = self._global_epoch

            before: dict[str, TeamAssignment] = {}
            after: dict[str, TeamAssignment] = {}
            for worker_id in context_workers:
                before[worker_id] = self._partitions[worker_id]
                after[worker_id] = TeamAssignment(
                    team_id=singleton_team_id(worker_id),
                    epoch=release_epoch,
                    member_ids=[worker_id],
                )

            transition_id = f"tr_release_{uuid.uuid4().hex}"
            transition = PartitionTransition(
                transition_id=transition_id,
                context_id=context_id,
                source_node_id="",
                before={k: v for k, v in before.items()},
                after={k: v for k, v in after.items()},
                affected_workers=list(context_workers),
                epoch=release_epoch,
                status=TransitionStatus.PREPARING,
                team_id=singleton_team_id("release"),
            )
            for worker_id in context_workers:
                transition.member_deliveries[worker_id] = MemberDelivery(
                    worker_id=worker_id,
                    ack_status="pending",
                )

            self._transitions[transition_id] = transition
            # Set fences — workers are claimed by the release saga until ACK
            for worker_id in context_workers:
                self._fences[worker_id] = transition_id

            return self._copy_transition(transition)

    def complete_release(self, transition_id: str) -> PartitionTransition:
        """Complete the release saga by applying the after assignments.

        Called after the service layer confirms all ACKs (or after
        compensation completes).  This is the same as mark_installed
        for a singleton transition, but isolated for clarity.
        """
        return self.mark_installed(transition_id)

    # ── Accessors ─────────────────────────────────────────────────────

    def get_transition(self, transition_id: str) -> PartitionTransition | None:
        with self._lock:
            t = self._transitions.get(transition_id)
            return self._copy_transition(t) if t is not None else None

    def get_active_transitions(self) -> list[PartitionTransition]:
        """Return transitions that still hold at least one active fence.

        Consult the ``_fences`` dict (the ground truth) rather than the
        static status check, because reconcile may clear fences without
        changing the transition status.
        """
        with self._lock:
            active_ids: set[str] = set(self._fences.values())
            return [
                self._copy_transition(t)
                for tid, t in self._transitions.items()
                if tid in active_ids
            ]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "global_epoch": self._global_epoch,
                "team_partition_revision": self._team_partition_revision,
                "workers": len(self._partitions),
                "teams": self._collect_team_rosters_locked(),
                "active_fences": dict(self._fences),
                "active_transitions_count": len(set(self._fences.values())),
                "total_transitions": len(self._transitions),
            }

    # ── Recovery / reconcile ─────────────────────────────────────────

    def reconcile_after_recovery(self) -> list[PartitionTransition]:
        """Resolve all transitions that hold fences with fresh singleton epoch.

        Every affected worker receives a singleton assignment with epoch
        strictly larger than any previously recorded epoch.  All fences
        are released.  Returns the list of transitions that were reconciled.
        """
        with self._lock:
            fenced = [
                t for t in self._transitions.values() if _transition_holds_fence(t)
            ]
            reconciled: list[PartitionTransition] = []
            for t in fenced:
                # Mark INSTALLING/COMPENSATING/PREPARING transitions as DEGRADED;
                # INSTALLED collaborative stays INSTALLED but loses fences.
                if t.status not in (
                    TransitionStatus.INSTALLED,
                    TransitionStatus.DEGRADED,
                ):
                    t.status = TransitionStatus.DEGRADED
                    t.updated_at = datetime.now(timezone.utc).isoformat()

                for worker_id in t.affected_workers:
                    self._fences.pop(worker_id, None)
                    self._global_epoch += 1
                    self._partitions[worker_id] = TeamAssignment(
                        team_id=singleton_team_id(worker_id),
                        epoch=self._global_epoch,
                        member_ids=[worker_id],
                    )
                if t.affected_workers:
                    self._team_partition_revision += 1
                reconciled.append(self._copy_transition(t))
            return reconciled

    # ── Persistence ───────────────────────────────────────────────────

    def persist(self, path: str | Path) -> None:
        with self._lock:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)

            payload = {
                "version": 2,
                "coordinator_id": self._coordinator_id,
                "global_epoch": self._global_epoch,
                "partitions": {
                    wid: _assignment_to_dict(a) for wid, a in self._partitions.items()
                },
                "transitions": {
                    tid: _transition_to_dict(t) for tid, t in self._transitions.items()
                },
                "fences": dict(self._fences),
                "persisted_version": self._persisted_version,
            }

            fd, tmp_path_str = tempfile.mkstemp(
                prefix=".team_partition.",
                suffix=".tmp",
                dir=str(path.parent),
            )
            try:
                os.fchmod(fd, 0o600)
                data = json.dumps(
                    payload, ensure_ascii=False, default=_serialize
                ).encode("utf-8")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path_str, path)
                os.chmod(path, 0o600)
                self._persisted_version += 1
            finally:
                try:
                    os.unlink(tmp_path_str)
                except FileNotFoundError:
                    pass

    def load(self, path: str | Path) -> None:
        path = Path(path)
        if not path.exists():
            return
        try:
            raw_text = path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load partition state from %s: %s", path, exc)
            return

        with self._lock:
            loaded_epoch = int(raw.get("global_epoch", 0))
            if loaded_epoch > self._global_epoch:
                self._global_epoch = loaded_epoch

            for wid, a_data in raw.get("partitions", {}).items():
                self._partitions[wid] = _dict_to_assignment(a_data)

            for tid, t_data in raw.get("transitions", {}).items():
                self._transitions[tid] = _dict_to_transition(t_data)

            self._fences = dict(raw.get("fences", {}))

    # ── Internal helpers ──────────────────────────────────────────────

    def _check_participants_free_locked(self, members: list[str]) -> list[str]:
        busy: list[str] = []
        for worker_id in members:
            fence_tid = self._fences.get(worker_id)
            if fence_tid is not None:
                t = self._transitions.get(fence_tid)
                if t is not None and _transition_holds_fence(t):
                    busy.append(worker_id)
        return busy

    def _require_transition(self, transition_id: str) -> PartitionTransition:
        t = self._transitions.get(transition_id)
        if t is None:
            raise TransitionError(f"Unknown transition: {transition_id}")
        return t

    def _copy_transition(self, t: PartitionTransition) -> PartitionTransition:
        return PartitionTransition(
            transition_id=t.transition_id,
            context_id=t.context_id,
            source_node_id=t.source_node_id,
            before={k: _copy_assignment(v) for k, v in t.before.items()},
            after={k: _copy_assignment(v) for k, v in t.after.items()},
            affected_workers=list(t.affected_workers),
            epoch=t.epoch,
            status=t.status,
            member_deliveries={
                wid: MemberDelivery(
                    worker_id=md.worker_id,
                    ack_status=md.ack_status,
                    acked_at=md.acked_at,
                )
                for wid, md in t.member_deliveries.items()
            },
            team_secret=t.team_secret,
            team_id=t.team_id,
            compensation_plan=t.compensation_plan,
            reason=t.reason,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )

    def _collect_team_rosters_locked(self) -> list[dict[str, Any]]:
        teams: dict[str, dict[str, Any]] = {}
        for worker_id, assignment in self._partitions.items():
            tid = assignment.team_id
            if tid not in teams:
                teams[tid] = {
                    "team_id": tid,
                    "epoch": assignment.epoch,
                    "members": [],
                    "context_id": assignment.context_id,
                    "node_id": assignment.node_id,
                    "is_singleton": assignment.is_singleton,
                }
            teams[tid]["members"].append(worker_id)
        result = list(teams.values())
        result.sort(key=lambda t: t["team_id"])
        return result


# ======================================================================
# TeamPartitionService
# ======================================================================

DeliveryAdapter = Callable[
    [PartitionTransition],
    Awaitable[dict[str, bool]],
]


class TeamPartitionService:
    """Coordinator-owned service wrapping TeamPartitionRegistry.

    Provides the orchestration API that Phase 4 activate_plan_node
    will call.  The delivery adapter (set via set_delivery_adapter) is
    the Phase 4 injection point for actual TEAM_UPDATE/REVOKE network
    delivery.
    """

    def __init__(
        self,
        registry: TeamPartitionRegistry | None = None,
        coordinator_id: str = "Coordinator",
    ) -> None:
        self._registry = registry or TeamPartitionRegistry(
            coordinator_id=coordinator_id,
        )
        self._delivery_adapter: DeliveryAdapter | None = None

    @property
    def registry(self) -> TeamPartitionRegistry:
        return self._registry

    @property
    def has_delivery_adapter(self) -> bool:
        """Whether a real TEAM_UPDATE/REVOKE delivery mechanism is wired.

        Without one, the Team ACK saga cannot possibly reach any worker, so
        every multi-participant activation would deterministically time out
        to DEGRADED -- which retains its fence forever by design (see
        _transition_holds_fence). Callers should skip the saga entirely
        rather than manufacture an un-clearable fence for a guarantee
        (inter-worker team notification) that isn't actually in effect.
        """
        return self._delivery_adapter is not None

    def set_delivery_adapter(self, adapter: DeliveryAdapter | None) -> None:
        self._delivery_adapter = adapter

    def ensure_singletons(self, online_workers: list[str]) -> list[TeamAssignment]:
        return self._registry.ensure_singletons(online_workers)

    def partition_snapshot(self) -> dict[str, Any]:
        return self._registry.snapshot()

    def get_assignment(self, worker_id: str) -> TeamAssignment | None:
        return self._registry.get_assignment(worker_id)

    @property
    def team_partition_revision(self) -> int:
        return self._registry.team_partition_revision

    def collect_team_rosters(self) -> list[dict[str, Any]]:
        return self._registry.collect_team_rosters()

    def prepare_activation(
        self,
        node_id: str,
        members: list[str],
        objective: str,
        context_id: str,
    ) -> PartitionTransition:
        return self._registry.prepare_activation(
            node_id,
            members,
            objective,
            context_id,
        )

    async def activate_node_team(
        self,
        transition: PartitionTransition,
        *,
        ack_timeout: float = 30.0,
    ) -> PartitionTransition:
        """Execute the Team ACK saga for one activation transition.

        Steps:
        1. Mark INSTALLING
        2. Send TEAM_UPDATE to all members (via delivery adapter)
        3. Await ACKs (via injected adapter or tracking records)
        4. All ACK -> INSTALLED
        5. Any fail -> COMPENSATING -> COMPENSATED or DEGRADED

        Returns the final PartitionTransition.
        """
        registry = self._registry
        registry.mark_installing(transition.transition_id)

        if self._delivery_adapter is not None:
            try:
                outcomes = await self._delivery_adapter(transition)
            except Exception as exc:
                logger.error(
                    "Delivery adapter failed for transition %s: %s",
                    transition.transition_id,
                    exc,
                )
                outcomes = {}
        else:
            outcomes = {}

        for worker_id in transition.affected_workers:
            if worker_id in outcomes:
                registry.record_ack(
                    transition.transition_id,
                    worker_id,
                    success=outcomes[worker_id],
                )
            else:
                registry.record_ack(
                    transition.transition_id,
                    worker_id,
                    timeout=True,
                )

        all_acked, failed, pending = registry.check_acks(transition.transition_id)

        if all_acked:
            return registry.mark_installed(transition.transition_id)

        registry.mark_compensating(transition.transition_id)
        compensation_outcomes = await self._run_compensation(transition)

        for worker_id in transition.affected_workers:
            comp_ok = compensation_outcomes.get(worker_id, False)
            if comp_ok:
                registry.record_ack(
                    transition.transition_id,
                    worker_id,
                    success=True,
                )

        comp_acked, comp_failed, comp_pending = registry.check_acks(
            transition.transition_id,
        )
        if comp_acked:
            return registry.mark_compensated(transition.transition_id)
        return registry.mark_degraded(transition.transition_id)

    async def release_node_team(
        self,
        context_id: str,
        *,
        ack_timeout: float = 30.0,
    ) -> PartitionTransition | None:
        """Durable release: restore all collaborative workers for *context_id* to singleton.

        Returns the final transition (INSTALLED, COMPENSATED, or DEGRADED)
        or None if no collaborative workers were found.

        The release saga:
        1. prepare_release() -> PREPARING transition with fences
        2. mark_installing
        3. deliver via configured adapter
        4. all ACK -> complete_release (INSTALLED, releases fences)
        5. fail -> compensate or DEGRADED
        """
        registry = self._registry
        release = registry.prepare_release(context_id)
        if release is None:
            return None

        registry.mark_installing(release.transition_id)

        if self._delivery_adapter is not None:
            try:
                outcomes = await self._delivery_adapter(release)
            except Exception as exc:
                logger.error(
                    "Release delivery adapter failed for %s: %s",
                    release.transition_id,
                    exc,
                )
                outcomes = {}
        else:
            outcomes = {}

        for worker_id in release.affected_workers:
            if worker_id in outcomes:
                registry.record_ack(
                    release.transition_id,
                    worker_id,
                    success=outcomes[worker_id],
                )
            else:
                registry.record_ack(
                    release.transition_id,
                    worker_id,
                    timeout=True,
                )

        all_acked, failed, pending = registry.check_acks(release.transition_id)
        if all_acked:
            return registry.complete_release(release.transition_id)

        # Compensation: re-send TEAM_UPDATE with original collaborative team
        registry.mark_compensating(release.transition_id)
        comp_outcomes = await self._run_compensation(release)

        for worker_id in release.affected_workers:
            comp_ok = comp_outcomes.get(worker_id, False)
            if comp_ok:
                registry.record_ack(
                    release.transition_id,
                    worker_id,
                    success=True,
                )

        comp_acked, comp_failed, comp_pending = registry.check_acks(
            release.transition_id,
        )
        if comp_acked:
            return registry.mark_compensated(release.transition_id)
        return registry.mark_degraded(release.transition_id)

    async def _run_compensation(
        self,
        transition: PartitionTransition,
    ) -> dict[str, bool]:
        """Run compensation: send reverse delivery to all affected workers.

        Phase 3 default: returns empty (no adapter) so compensation
        always fails, driving to DEGRADED.  Phase 4 injects actual
        network delivery.
        """
        if self._delivery_adapter is not None:
            try:
                compensation_transition = PartitionTransition(
                    transition_id=f"comp_{transition.transition_id}",
                    context_id=transition.context_id,
                    source_node_id=transition.source_node_id,
                    before=dict(transition.after),
                    after=dict(transition.before),
                    affected_workers=list(transition.affected_workers),
                    epoch=transition.epoch,
                    status=TransitionStatus.PREPARING,
                )
                return await self._delivery_adapter(compensation_transition)
            except Exception:
                return {}
        return {}


# ── Serialization helpers ──────────────────────────────────────────────


def _assignment_to_dict(a: TeamAssignment) -> dict[str, Any]:
    return {
        "team_id": a.team_id,
        "epoch": a.epoch,
        "member_ids": list(a.member_ids),
        "objective": a.objective,
        "context_id": a.context_id,
        "node_id": a.node_id,
    }


def _dict_to_assignment(d: dict[str, Any]) -> TeamAssignment:
    return TeamAssignment(
        team_id=str(d.get("team_id", "")),
        epoch=int(d.get("epoch", 0)),
        member_ids=list(d.get("member_ids", [])),
        objective=d.get("objective"),
        context_id=d.get("context_id"),
        node_id=d.get("node_id"),
    )


def _copy_assignment(a: TeamAssignment) -> TeamAssignment:
    return TeamAssignment(
        team_id=a.team_id,
        epoch=a.epoch,
        member_ids=list(a.member_ids),
        objective=a.objective,
        context_id=a.context_id,
        node_id=a.node_id,
    )


def _transition_to_dict(t: PartitionTransition) -> dict[str, Any]:
    return {
        "transition_id": t.transition_id,
        "context_id": t.context_id,
        "source_node_id": t.source_node_id,
        "before": {k: _assignment_to_dict(v) for k, v in t.before.items()},
        "after": {k: _assignment_to_dict(v) for k, v in t.after.items()},
        "affected_workers": list(t.affected_workers),
        "epoch": t.epoch,
        "status": t.status.value,
        "member_deliveries": {
            wid: {
                "worker_id": md.worker_id,
                "ack_status": md.ack_status,
                "acked_at": md.acked_at,
            }
            for wid, md in t.member_deliveries.items()
        },
        "team_secret": t.team_secret.hex() if t.team_secret else None,
        "team_id": t.team_id,
        "compensation_plan": t.compensation_plan,
        "reason": t.reason,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _dict_to_transition(d: dict[str, Any]) -> PartitionTransition:
    member_deliveries: dict[str, MemberDelivery] = {}
    for wid, md in d.get("member_deliveries", {}).items():
        member_deliveries[wid] = MemberDelivery(
            worker_id=md.get("worker_id", wid),
            ack_status=md.get("ack_status", "pending"),
            acked_at=md.get("acked_at"),
        )
    team_secret_raw = d.get("team_secret")
    team_secret: bytes | None = (
        bytes.fromhex(team_secret_raw) if isinstance(team_secret_raw, str) else None
    )
    before_raw = d.get("before", {})
    after_raw = d.get("after", {})

    return PartitionTransition(
        transition_id=str(d.get("transition_id", "")),
        context_id=str(d.get("context_id", "")),
        source_node_id=str(d.get("source_node_id", "")),
        before={k: _dict_to_assignment(v) for k, v in before_raw.items()},
        after={k: _dict_to_assignment(v) for k, v in after_raw.items()},
        affected_workers=list(d.get("affected_workers", [])),
        epoch=int(d.get("epoch", 0)),
        status=TransitionStatus(d.get("status", "PREPARING")),
        member_deliveries=member_deliveries,
        team_secret=team_secret,
        team_id=d.get("team_id"),
        compensation_plan=d.get("compensation_plan", ""),
        reason=d.get("reason", ""),
        created_at=str(d.get("created_at", datetime.now(timezone.utc).isoformat())),
        updated_at=str(d.get("updated_at", datetime.now(timezone.utc).isoformat())),
    )


def _serialize(obj: Any) -> str:
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)


__all__ = [
    "DeliveryAdapter",
    "MemberDelivery",
    "ParticipantBusyError",
    "PartitionTransition",
    "TeamAssignment",
    "TeamPartitionRegistry",
    "TeamPartitionService",
    "TransitionError",
    "TransitionStatus",
    "singleton_team_id",
]
