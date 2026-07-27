from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from Agent.router_agent.state_provider import RuntimeState
from a2a.coordinator.team_status_auth import TeamStatusProof

if TYPE_CHECKING:
    from sar_orch.barrier import SARBarrier
    from a2a.worker.mailbox_store import WorkerMailboxStore
    from a2a.worker.team_state import WorkerTeamState


class SARWorkerStateProvider:
    """Read-only runtime state provider for SAR Workers.

    Reads position, inventory, step, and local observations directly from
    SARBarrier (no step consumption). Optionally fetches global semantic map
    summary via HTTP for known_fires / known_persons enrichment.

    Supports optional mailbox and team_state dependencies for Phase 3 peer
    messaging. When attached, the version tuple includes mailbox version and
    team generation so that context refreshes when mail arrives or the team
    changes, even within the same env step.

    Phase 5: authenticated /team-status via HMAC proof bound to worker ID.
    Cache key includes team_generation so same-step team transitions trigger
    a refresh.
    """

    def __init__(
        self,
        barrier: "SARBarrier | None" = None,
        agent_idx: int = 0,
        semantic_map_url: str | None = None,
        mailbox: "WorkerMailboxStore | None" = None,
        team_state: "WorkerTeamState | None" = None,
        coordinator_id: str = "Coordinator",
        coordinator_secret: bytes | None = None,
    ) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx
        self._semantic_map_url = (
            semantic_map_url.rstrip("/") if semantic_map_url else None
        )
        self._mailbox = mailbox
        self._team_state = team_state
        self._coordinator_id = coordinator_id
        self._coordinator_secret = coordinator_secret
        self._last_version: int | tuple = -1
        self._last_snapshot: RuntimeState | None = None

        self._agent_name: str = ""
        self._team_status_url: str | None = None
        self._cached_teammates: list[dict] = []
        self._last_team_status_cache_key: tuple = ()
        self._cached_team_status_revision: int = -1

        if self._semantic_map_url:
            self._team_status_url = self._semantic_map_url

    async def fetch_team_status_async(self) -> None:
        """Fetch team status from coordinator.

        Cache key is ``(env_step, team_generation, known_server_revision)``.
        A change in any component forces a re-fetch.  The server's returned
        ``team_partition_revision`` is stored and becomes part of the cache
        key on the next call, so a known-stale cache is always refreshed.
        """
        if not self._team_status_url or not self._agent_name:
            return
        env_step = (
            getattr(self._barrier, "_step_counter", 0)
            if self._barrier is not None
            else 0
        )
        team_gen = (
            self._team_state.generation
            if self._team_state is not None
            else ("", -1, False)
        )
        known_revision = self._cached_team_status_revision
        cache_key = (env_step, team_gen, known_revision)
        if cache_key == self._last_team_status_cache_key:
            return
        try:
            import httpx

            proof = (
                TeamStatusProof.generate(
                    self._coordinator_secret,
                    self._agent_name,
                )
                if self._coordinator_secret
                else ""
            )

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._team_status_url}/team-status",
                    params={
                        "agent_id": self._agent_name,
                        "proof": proof,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if "teammates" in data:
                    self._cached_teammates = data.get("teammates", [])
                elif "team_id" in data:
                    self._cached_teammates = []
                server_revision = data.get("team_partition_revision", -1)
                self._cached_team_status_revision = server_revision
                self._last_team_status_cache_key = cache_key
        except Exception:
            pass

    def force_refresh_team_status(self) -> None:
        """Force the next ``fetch_team_status_async`` call to re-fetch.

        Call when external knowledge (e.g. a TEAM_UPDATE envelope with a
        known newer partition revision) indicates the cached peers are
        stale but the generation tuple hasn't changed yet.
        """
        self._last_team_status_cache_key = ()

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Version is a tuple ``(env_step, mailbox_version, team_generation)``
        so that context refreshes when any of the three sources changes.
        If no mailbox/team_state is attached, the corresponding component
        defaults to 0 / ``("", -1, False)`` (the null team state).

        Exceptions from mailbox or team_state propagate to the outer
        catch, resulting in a stale snapshot with ``refresh_error`` set.
        """
        try:
            env_step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
            mailbox_ver = self._mailbox.version if self._mailbox is not None else 0
            team_gen = (
                self._team_state.generation
                if self._team_state is not None
                else ("", -1, False)
            )
            version = (env_step, mailbox_ver, team_gen)
            if version == self._last_version and self._last_snapshot is not None:
                return self._last_snapshot

            observed_at = time.monotonic()
            age_ms = 0.0
            if self._last_snapshot is not None:
                age_ms = (observed_at - self._last_snapshot.observed_at) * 1000.0

            payload: dict[str, Any] = {}
            payload["age_ms"] = age_ms

            if self._barrier is not None:
                try:
                    agent = self._barrier.env.controller.get("agents", self._agent_idx)
                    pos = agent.get_position()
                    payload["position"] = (pos[0], pos[1], pos[2])

                    inventory = self._barrier.env.controller.get_inventory(
                        self._agent_idx
                    )
                    payload["inventory"] = inventory

                    obs = self._barrier.get_current_obs(self._agent_idx)
                    payload["known_fires"] = self._extract_fires_from_obs(obs)
                    payload["known_persons"] = self._extract_persons_from_obs(obs)
                except Exception:
                    pass

            payload["step"] = env_step
            payload["mission_status"] = (
                ("complete" if self._barrier.is_finished() else "in_progress")
                if self._barrier is not None
                else "in_progress"
            )

            if self._mailbox is not None:
                payload["mailbox_summary"] = self._mailbox.summary(
                    coordinator_id=self._coordinator_id
                )

            if self._team_state is not None:
                ts = self._team_state.current()
                if ts is not None:
                    payload["team_summary"] = {
                        "team_id": ts.team_id,
                        "epoch": ts.epoch,
                        "members": list(ts.members),
                        "coordinator_id": ts.coordinator_id,
                    }

            payload["team_coordination"] = {
                "teammates": self._cached_teammates,
                "teammates_count": len(self._cached_teammates),
            }

            snapshot = RuntimeState(
                version=version,
                env_step=env_step,
                observed_at=observed_at,
                payload=payload,
                stale=False,
                refresh_error="",
            )
            self._last_version = version
            self._last_snapshot = snapshot
            return snapshot
        except Exception as exc:
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

    def _extract_fires_from_obs(self, obs: str) -> list[dict[str, Any]]:
        fires: list[dict[str, Any]] = []
        if not obs:
            return fires
        for line in obs.split("\n"):
            line = line.strip()
            if "fire" in line.lower() or "region" in line.lower():
                fires.append({"description": line[:200]})
        return fires

    def _extract_persons_from_obs(self, obs: str) -> list[dict[str, Any]]:
        persons: list[dict[str, Any]] = []
        if not obs:
            return persons
        for line in obs.split("\n"):
            line = line.strip()
            if "person" in line.lower():
                persons.append({"description": line[:200]})
        return persons
