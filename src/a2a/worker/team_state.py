"""WorkerTeamState — single-team invariant with JSON persistence.

Stores one active team at a time.  Epoch must be monotonically increasing
across all updates, including team replacement.  Persists as a small atomic
JSON file with ``0600`` permissions to protect the plaintext team secret.

The persisted schema includes a ``last_generation`` field that survives
revocation, preventing stale (old-epoch) updates from being reinstalled
even after restart.

Malformed persisted state raises ``TeamStateError`` (fail-closed).

Limitation: the team_secret (peer-group shared secret) is stored on disk in
plaintext at the file-system permission level.  A production deployment
should use a dedicated secret store (e.g. a keyring or TPM-backed vault)
to avoid any plaintext-on-disk exposure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable
from types import MappingProxyType

from a2a.shared.message_envelope import AuthorizationContext


@dataclass(frozen=True)
class DeliverySnapshot:
    """Immutable atomic snapshot of current team state plus peer secret.

    Returned by ``WorkerTeamState.delivery_snapshot()`` under a single lock
    acquisition to guarantee TOCTOU-freedom.  All collection fields are
    immutable (tuple members, MappingProxyType endpoints, bytes secret) so
    callers cannot mutate team state through the snapshot.

    Ownership: the snapshot is a pure value object; callers may read freely
    without locks.
    """

    team_id: str
    epoch: int
    members: tuple[str, ...]
    endpoints: MappingProxyType  # str -> str view
    coordinator_id: str
    peer_secret: bytes


logger = logging.getLogger(__name__)

FILE_PERMISSIONS = 0o600
MIN_TEAM_SECRET_HEX_LEN = 32  # 16 bytes


class TeamStateError(Exception):
    """Base for team state errors."""


class InvalidTeamParameter(TeamStateError):
    """Invalid parameter value."""


class StaleTeamUpdateError(TeamStateError):
    """Update epoch is not newer than last seen generation."""


class TeamEpochRegressionError(TeamStateError):
    """Cannot install a team with conflicting same-epoch content."""


@dataclass
class TeamStateData:
    """Serializable representation of the team."""

    team_id: str
    epoch: int
    members: list[str]
    endpoints: dict[str, str]
    team_secret: str  # hex-encoded bytes
    coordinator_id: str


class WorkerTeamState:
    """Single-team invariant with JSON file persistence.

    Thread-safe.  Persists with ``0600`` permissions.

    Epoch monotonicity is enforced across ALL updates (including team
    replacement) to prevent replay attacks.  ``last_generation`` persists
    even after revoke so old updates cannot be reinstalled after restart.
    """

    TeamEventCallback = Callable[[str, dict[str, Any]], None]
    """Callback: ``(event_name, event_data)``.

    Event names: ``team_installed``, ``team_revoked``, ``team_rejected``.
    Event data excludes team secret and signature.
    """

    def __init__(
        self,
        path: str | Path,
        local_worker_id: str,
        coordinator_id: str = "Coordinator",
        *,
        event_callback: "TeamEventCallback | None" = None,
    ):
        self._path = Path(path)
        self._local_worker_id = local_worker_id
        self._coordinator_id = coordinator_id
        self._lock = threading.Lock()
        self._state: TeamStateData | None = None
        self._last_generation: tuple[str, int] | None = None
        self._event_callback = event_callback
        if self._path.exists():
            self._load()

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        team_id: str,
        epoch: int,
        members: list[str],
        endpoints: dict[str, str],
        team_secret: str,
        coordinator_id: str,
    ) -> None:
        self._validate_install_params(
            team_id,
            epoch,
            members,
            endpoints,
            team_secret,
            coordinator_id,
        )
        with self._lock:
            self._reject_stale(team_id, epoch)

            if (
                self._state is not None
                and self._state.team_id == team_id
                and self._state.epoch == epoch
            ):
                if (
                    self._state.members == list(members)
                    and self._state.endpoints == dict(endpoints)
                    and self._state.team_secret == team_secret
                ):
                    logger.info(
                        "Team '%s' epoch %d re-install (idempotent)",
                        team_id,
                        epoch,
                    )
                    return
                raise TeamEpochRegressionError(
                    f"Team '{team_id}' epoch {epoch} re-installed with "
                    f"different content"
                )

            self._state = TeamStateData(
                team_id=team_id,
                epoch=epoch,
                members=list(members),
                endpoints=dict(endpoints),
                team_secret=team_secret,
                coordinator_id=coordinator_id,
            )
            self._last_generation = (team_id, epoch)
            self._save()
            logger.info(
                "Team '%s' installed at epoch %d (%d members)",
                team_id,
                epoch,
                len(members),
            )

        # Event outside lock
        self._fire_event(
            "team_installed",
            {
                "team_id": team_id,
                "epoch": epoch,
                "member_count": len(members),
                "coordinator_id": coordinator_id,
            },
        )

    def revoke(self, team_id: str, epoch: int) -> None:
        """Revoke the active team.

        *team_id* and *epoch* must match the current active team.
        """
        with self._lock:
            if self._state is None:
                raise InvalidTeamParameter("No active team to revoke")
            if self._state.team_id != team_id or self._state.epoch != epoch:
                raise InvalidTeamParameter(
                    f"Revoke mismatch: active={self._state.team_id}:{self._state.epoch}, "
                    f"got {team_id}:{epoch}"
                )
            lg_team = self._state.team_id
            lg_epoch = self._state.epoch
            self._state = None
            self._last_generation = (lg_team, lg_epoch)
            self._save()
            logger.info("Team '%s' revoked at epoch %d", lg_team, lg_epoch)

        # Event outside lock
        self._fire_event(
            "team_revoked",
            {
                "team_id": lg_team,
                "epoch": lg_epoch,
            },
        )

    def current(self) -> TeamStateData | None:
        """Return a defensive copy of the current team state, or None."""
        with self._lock:
            if self._state is None:
                return None
            return TeamStateData(
                team_id=self._state.team_id,
                epoch=self._state.epoch,
                members=list(self._state.members),
                endpoints=dict(self._state.endpoints),
                team_secret=self._state.team_secret,
                coordinator_id=self._state.coordinator_id,
            )

    def to_auth_context(self) -> AuthorizationContext | None:
        """Build an AuthorizationContext from the current team, or None."""
        with self._lock:
            if self._state is None:
                return None
            return AuthorizationContext(
                local_worker_id=self._local_worker_id,
                coordinator_id=self._state.coordinator_id,
                current_team_id=self._state.team_id,
                current_team_epoch=self._state.epoch,
                current_team_members=frozenset(self._state.members),
            )

    def peer_secret_bytes(self) -> bytes | None:
        """Return the team's shared secret as raw bytes, or None."""
        with self._lock:
            if self._state is None:
                return None
            return bytes.fromhex(self._state.team_secret)

    def delivery_snapshot(self) -> DeliverySnapshot | None:
        """Return an atomic snapshot of team state + peer secret.

        Unlike calling ``current()`` then ``peer_secret_bytes()`` in
        separate lock acquisitions, this method returns both under a
        single lock so the caller sees a coherent view without TOCTOU
        races.  This is essential for signing peer mail envelopes where
        the epoch/team_id/secret must belong to the same generation.

        The returned ``DeliverySnapshot`` is frozen and uses immutable
        collection types (tuple members, MappingProxyType endpoints)
        so callers cannot mutate team state through the snapshot.

        Returns:
            ``DeliverySnapshot`` or ``None`` if no team is active.
        """
        with self._lock:
            if self._state is None:
                return None
            return DeliverySnapshot(
                team_id=self._state.team_id,
                epoch=self._state.epoch,
                members=tuple(self._state.members),
                endpoints=MappingProxyType(dict(self._state.endpoints)),
                coordinator_id=self._state.coordinator_id,
                peer_secret=bytes.fromhex(self._state.team_secret),
            )

    @property
    def active_team_id(self) -> str | None:
        with self._lock:
            return self._state.team_id if self._state else None

    # ── observability ─────────────────────────────────────────────────

    def _fire_event(self, name: str, data: dict[str, Any]) -> None:
        if self._event_callback is not None:
            try:
                self._event_callback(name, data)
            except Exception:
                logger.debug("Team state event callback failed", exc_info=True)

    # ── validation (instance method — needs local_worker_id) ────────────

    def _validate_install_params(
        self,
        team_id: str,
        epoch: int,
        members: list[str],
        endpoints: dict[str, str],
        team_secret: str,
        coordinator_id: str,
    ) -> None:
        if not team_id or not team_id.strip():
            raise InvalidTeamParameter("team_id must be non-blank")
        if epoch < 0:
            raise InvalidTeamParameter(f"epoch must be >= 0, got {epoch}")
        if not members:
            raise InvalidTeamParameter("members list must be non-empty")
        seen = set()
        for mid in members:
            if not mid or not mid.strip():
                raise InvalidTeamParameter(
                    f"member ID must be non-blank, got {mid!r}",
                )
            if mid in seen:
                raise InvalidTeamParameter(f"duplicate member ID '{mid}'")
            seen.add(mid)
        if self._local_worker_id not in seen:
            raise InvalidTeamParameter(
                f"local worker '{self._local_worker_id}' must be in members",
            )
        if not team_secret or len(team_secret) < MIN_TEAM_SECRET_HEX_LEN:
            raise InvalidTeamParameter(
                f"team_secret hex length must be >= {MIN_TEAM_SECRET_HEX_LEN}, "
                f"got {len(team_secret)}",
            )
        try:
            bytes.fromhex(team_secret)
        except ValueError as exc:
            raise InvalidTeamParameter(
                f"team_secret is not valid hex: {exc}",
            ) from exc
        if not coordinator_id or not coordinator_id.strip():
            raise InvalidTeamParameter("coordinator_id must be non-blank")
        for worker_id, url in endpoints.items():
            if worker_id not in seen:
                raise InvalidTeamParameter(
                    f"endpoint for non-member '{worker_id}'",
                )
            if not url or not url.strip():
                raise InvalidTeamParameter(
                    f"endpoint URL for '{worker_id}' must be non-blank",
                )

    def _reject_stale(self, team_id: str, epoch: int) -> None:
        """Raise if *epoch* is not newer than *last_generation*."""
        if self._last_generation is None:
            return
        lg_team, lg_epoch = self._last_generation
        if epoch > lg_epoch:
            return
        if (
            self._state is not None
            and self._state.team_id == team_id
            and self._state.epoch == epoch
        ):
            return
        raise StaleTeamUpdateError(
            f"Update team='{team_id}' epoch={epoch} is not newer than "
            f"last generation team='{lg_team}' epoch={lg_epoch}",
        )

    # ── persistence ─────────────────────────────────────────────────────

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"last_generation": None}
        if self._state is not None:
            data["state"] = asdict(self._state)
        else:
            data["state"] = None
        if self._last_generation is not None:
            data["last_generation"] = {
                "team_id": self._last_generation[0],
                "epoch": self._last_generation[1],
            }
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(str(tmp), FILE_PERMISSIONS)
        tmp.replace(self._path)

    def _load(self) -> None:
        """Load persisted state.  Validates with same rules; fail closed."""
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)

            lg = data.get("last_generation")
            if lg and lg.get("team_id"):
                team = lg["team_id"]
                epoch = lg["epoch"]
                if not isinstance(team, str) or not team.strip():
                    raise TeamStateError("last_generation has invalid team_id")
                if not isinstance(epoch, int) or epoch < 0:
                    raise TeamStateError("last_generation has invalid epoch")
                self._last_generation = (team, epoch)
            else:
                self._last_generation = None

            st = data.get("state")
            if st is None:
                self._state = None
            else:
                ts = TeamStateData(
                    team_id=st["team_id"],
                    epoch=st["epoch"],
                    members=st["members"],
                    endpoints=st.get("endpoints", {}),
                    team_secret=st["team_secret"],
                    coordinator_id=st.get(
                        "coordinator_id",
                        self._coordinator_id,
                    ),
                )
                # Validate with same rules
                self._validate_install_params(
                    ts.team_id,
                    ts.epoch,
                    ts.members,
                    ts.endpoints,
                    ts.team_secret,
                    ts.coordinator_id,
                )
                self._state = ts
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            TeamStateError,
        ) as exc:
            raise TeamStateError(
                f"Failed to load team state from {self._path}: {exc}",
            ) from exc

    @property
    def generation(self) -> tuple[str, int, bool]:
        """Team generation identifier that changes on every install/revoke.

        Returns ``(team_id, epoch, is_active)``:
        - ``team_id`` — last known team ID (``""`` if never installed).
        - ``epoch`` — epoch of last generation event (``-1`` if never installed).
        - ``is_active`` — ``True`` when a team is currently installed.

        Both ``install`` and ``revoke`` change the generation, so context
        refresh fires for either event.  The tuple is hashable so it can be
        used as part of a ``RuntimeState.version`` tuple.
        """
        with self._lock:
            if self._last_generation is None:
                return ("", -1, False)
            team_id, epoch = self._last_generation
            active = self._state is not None
            return (team_id, epoch, active)

    def clear(self) -> None:
        """Clear state (testing helper)."""
        with self._lock:
            self._state = None
            self._last_generation = None
