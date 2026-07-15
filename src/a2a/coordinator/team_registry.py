"""Coordinator-side TeamRegistry with one-team invariant.

Thread-safe; accessed across server and orchestration threads.
Configure is atomic whole-roster replacement with validation.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from a2a.coordinator.agent_registry import AgentRegistry
    from a2a.coordinator.worker_registry import WorkerRegistry

from a2a.shared.endpoint_helpers import normalize_roster_endpoints

MIN_TEAM_SECRET_BYTES = 32


class TeamRegistryError(Exception):
    """Base for TeamRegistry errors."""


class ValidationError(TeamRegistryError):
    """Validation of member endpoints failed."""


@dataclass
class TeamDTO:
    """Defensive DTO for team state queries."""

    team_id: str
    epoch: int
    member_ids: list[str]
    endpoints: dict[str, str]
    objective: str | None = None

    @classmethod
    def from_internal(
        cls, team_id: str, epoch: int, members: dict[str, str], objective: str | None
    ) -> TeamDTO:
        return cls(
            team_id=team_id,
            epoch=epoch,
            member_ids=list(members.keys()),
            endpoints=dict(members),
            objective=objective,
        )


@dataclass
class DeliveryPlan:
    """Atomic snapshot of team state plus secret for delivery.

    Returned atomically by configure_for_delivery and disband_for_delivery
    so the caller gets one coherent view of DTO + secret without TOCTOU
    races.
    """

    dto: TeamDTO
    team_secret: bytes


class CoordinatorTeamRegistry:
    """Thread-safe coordinator-side team registry.

    Maintains exactly one team at a time.  Every ``configure()``
    call atomically replaces the entire roster there is no
    incremental add/remove.  Epoch increments on every mutation
    (configure or disband) so stale updates are rejected.
    """

    def __init__(self, coordinator_id: str = "Coordinator"):
        self._coordinator_id = coordinator_id
        self._lock = threading.Lock()
        self._team_id: str | None = None
        self._epoch: int = 0
        self._members: dict[str, str] = {}
        self._objective: str | None = None
        self._team_secret: bytes | None = None

    def configure(
        self,
        member_endpoints: dict[str, str],
        *,
        objective: str | None = None,
        agent_registry: AgentRegistry | None = None,
        worker_registry: WorkerRegistry | None = None,
        team_id: str | None = None,
    ) -> TeamDTO:
        """Atomically replace the entire team roster.

        Returns a TeamDTO (without secret).  For delivery, prefer
        ``configure_for_delivery`` which returns an atomic
        ``DeliveryPlan`` containing both DTO and secret.

        Raises:
            ValidationError: If any member fails validation.
            ValueError: If member_endpoints is empty or team_id blank.
        """
        return self.configure_for_delivery(
            member_endpoints=member_endpoints,
            objective=objective,
            agent_registry=agent_registry,
            worker_registry=worker_registry,
            team_id=team_id,
        ).dto

    def configure_for_delivery(
        self,
        member_endpoints: dict[str, str],
        *,
        objective: str | None = None,
        agent_registry: AgentRegistry | None = None,
        worker_registry: WorkerRegistry | None = None,
        team_id: str | None = None,
    ) -> DeliveryPlan:
        """Atomically replace roster and return a DeliveryPlan.

        Validation (registry checks) happens BEFORE the lock so we
        fail-fast without holding it.  Endpoints with wildcard hosts
        (``0.0.0.0``) are normalised to ``localhost`` so peers can reach
        each other.  The actual state mutation and DeliveryPlan
        construction happen under the same lock acquisition guaranteeing
        atomicity.
        """
        if not member_endpoints:
            raise ValueError("member_endpoints must be non-empty")
        self._validate_no_duplicates(member_endpoints)

        # Normalise wildcard hosts before any validation or storage
        normalised = normalize_roster_endpoints(member_endpoints)

        for worker_id, endpoint in normalised.items():
            self._validate_worker(worker_id, endpoint, agent_registry, worker_registry)

        new_secret = secrets.token_bytes(MIN_TEAM_SECRET_BYTES)
        new_team_id = team_id or f"team-{secrets.token_hex(4)}"
        if not new_team_id or not new_team_id.strip():
            raise ValueError("team_id must be non-blank")

        with self._lock:
            self._epoch += 1
            self._team_id = new_team_id
            self._members = dict(normalised)
            self._objective = objective
            self._team_secret = new_secret
            return DeliveryPlan(
                dto=TeamDTO.from_internal(
                    team_id=self._team_id,
                    epoch=self._epoch,
                    members=self._members,
                    objective=self._objective,
                ),
                team_secret=self._team_secret,
            )

    def disband(self) -> TeamDTO | None:
        """Disband the active team.

        Returns a DTO (without secret).  For the atomic plan (DTO +
        secret) use ``disband_for_delivery``.
        """
        plan = self.disband_for_delivery()
        return plan.dto if plan is not None else None

    def disband_for_delivery(self) -> DeliveryPlan | None:
        """Atomically disband and return prior DeliveryPlan.

        The prior team state is captured and the registry is cleared
        in a single lock acquisition.  The caller can then send revoke
        messages using the returned plan without TOCTOU races.
        """
        with self._lock:
            if self._team_id is None:
                return None
            plan = DeliveryPlan(
                dto=TeamDTO.from_internal(
                    team_id=self._team_id,
                    epoch=self._epoch,
                    members=self._members,
                    objective=self._objective,
                ),
                team_secret=self._team_secret,
            )
            self._epoch += 1
            self._team_id = None
            self._members = {}
            self._objective = None
            self._team_secret = None
            return plan

    def current(self) -> TeamDTO | None:
        """Return a defensive DTO of the current team, or None."""
        with self._lock:
            if self._team_id is None:
                return None
            return TeamDTO.from_internal(
                team_id=self._team_id,
                epoch=self._epoch,
                members=self._members,
                objective=self._objective,
            )

    def snapshot_for_delivery(self) -> DeliveryPlan | None:
        """Return one coherent snapshot of DTO + secret for delivery.

        Unlike calling ``current()`` then accessing secret separately,
        this guarantees the DTO and secret come from the same lock
        acquisition.
        """
        with self._lock:
            if self._team_id is None or self._team_secret is None:
                return None
            return DeliveryPlan(
                dto=TeamDTO.from_internal(
                    team_id=self._team_id,
                    epoch=self._epoch,
                    members=self._members,
                    objective=self._objective,
                ),
                team_secret=self._team_secret,
            )

    @property
    def current_secret(self) -> bytes | None:
        with self._lock:
            return self._team_secret

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def _validate_no_duplicates(self, member_endpoints: dict[str, str]) -> None:
        if len(member_endpoints) != len(set(member_endpoints.keys())):
            raise ValidationError("Duplicate worker IDs in member_endpoints")

    def _validate_worker(
        self,
        worker_id: str,
        endpoint: str,
        agent_registry: AgentRegistry | None,
        worker_registry: WorkerRegistry | None,
    ) -> None:
        if not worker_id or not worker_id.strip():
            raise ValidationError(f"Invalid worker_id: {worker_id!r}")
        if not endpoint or not endpoint.strip():
            raise ValidationError(f"Invalid endpoint for {worker_id}: {endpoint!r}")
        if agent_registry is not None:
            try:
                info = agent_registry.get(worker_id)
                if info is None:
                    raise ValidationError(
                        f"Worker '{worker_id}' not found in agent registry"
                    )
            except ValidationError:
                raise
            except Exception as exc:
                raise ValidationError(
                    f"Worker '{worker_id}' not found in agent registry: {exc}"
                ) from exc
        if worker_registry is not None:
            try:
                w = worker_registry.get(worker_id)
                from a2a.shared.types import WorkerStatus

                if w.status != WorkerStatus.ONLINE:
                    raise ValidationError(
                        f"Worker '{worker_id}' is not online (status={w.status.value})"
                    )
            except ValidationError:
                raise
            except Exception as exc:
                raise ValidationError(
                    f"Worker '{worker_id}' not found in worker registry: {exc}"
                ) from exc
