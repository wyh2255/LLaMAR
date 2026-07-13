from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from Agent.router_agent.state_provider import RuntimeState

if TYPE_CHECKING:
    from sar_orch.barrier import SARBarrier


class SARWorkerStateProvider:
    """Read-only runtime state provider for SAR Workers.

    Reads position, inventory, step, and local observations directly from
    SARBarrier (no step consumption). Optionally fetches global semantic map
    summary via HTTP for known_fires / known_persons enrichment.

    Uses the SAR env step as the version to avoid redundant refreshes within
    the same env step. Falls back to the last cached snapshot on error.
    """

    def __init__(
        self,
        barrier: "SARBarrier | None" = None,
        agent_idx: int = 0,
        semantic_map_url: str | None = None,
    ) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx
        # TODO: Phase 5 — fetch /semantic-map for global known_fires/persons summary
        self._semantic_map_url = (
            semantic_map_url.rstrip("/") if semantic_map_url else None
        )
        self._last_version: int = -1
        self._last_snapshot: RuntimeState | None = None

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Uses the SAR env step as the version. If the env step has not changed
        since the last call, returns the cached snapshot. On refresh failure,
        returns the most recent snapshot with stale=True and refresh_error set.
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
        """Extract fire information from barrier observation text."""
        fires: list[dict[str, Any]] = []
        if not obs:
            return fires
        for line in obs.split("\n"):
            line = line.strip()
            if "fire" in line.lower() or "region" in line.lower():
                fires.append({"description": line[:200]})
        return fires

    def _extract_persons_from_obs(self, obs: str) -> list[dict[str, Any]]:
        """Extract person information from barrier observation text."""
        persons: list[dict[str, Any]] = []
        if not obs:
            return persons
        for line in obs.split("\n"):
            line = line.strip()
            if "person" in line.lower():
                persons.append({"description": line[:200]})
        return persons
