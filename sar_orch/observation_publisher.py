"""WorkerReportPublisher — lightweight worker-side observation dedup.

Filters out unchanged observations before they reach the A2A push chain,
reducing coordinator-side noise. The authoritative dedup is still performed
by SemanticMapStore._merge_locked() on the coordinator side.
"""

from __future__ import annotations

from collections.abc import Callable


def _observation_key(obs: dict) -> str:
    """Match SemanticMapStore._record_key for consistent dedup keys."""
    name = obs.get("name")
    if name:
        return str(name)
    pos = obs.get("position")
    if pos:
        return f"{obs.get('object_type', '?')}:{pos}"
    return f"{obs.get('object_type', '?')}:unknown"


def _obs_snapshot(obs: dict) -> dict:
    """Return the comparison-relevant subset of an observation."""
    return {
        "position": obs.get("position"),
        "attributes": obs.get("attributes"),
        "confidence": obs.get("confidence"),
    }


class WorkerReportPublisher:
    """Filters observations to only include new/changed items.

    Tracks the last-reported state of each observed object per agent.
    Called from the post_tool hook to strip duplicates from ToolResult.data
    before it reaches the A2A sink.
    """

    def __init__(
        self,
        agent_name: str = "unknown",
        step_provider: Callable[[], int] | None = None,
    ):
        self._agent_name = agent_name
        self._step_provider = step_provider or (lambda: 0)
        self._last_seen: dict[str, dict] = {}

    def filter_new_observations(self, data: dict | None) -> list[dict]:
        """Return only observations whose content has changed since last seen."""
        if not data or "observations" not in data:
            return []
        raw = data.get("observations", [])
        if not isinstance(raw, list):
            return []

        new_obs = []
        for obs in raw:
            if not isinstance(obs, dict):
                continue
            key = _observation_key(obs)
            prev = self._last_seen.get(key)
            curr = _obs_snapshot(obs)
            if prev != curr:
                self._last_seen[key] = curr
                new_obs.append(obs)

        return new_obs

    def apply_to_data(self, data: dict | None) -> dict | None:
        """Mutate data in-place to keep only new/changed observations.

        Preserves non-observation metadata (position, inventory) even when
        all observations are filtered as duplicates.
        """
        if not data:
            return None
        filtered = self.filter_new_observations(data)
        has_metadata = bool(data.get("position") or data.get("inventory"))
        if not filtered and not has_metadata:
            return None
        data["observations"] = filtered
        return data
