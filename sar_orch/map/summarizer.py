"""MapSummarizer — budgeted, failure-isolated summary generation for map deltas.

Phase 4.  Uses asyncio.Lock for single-flight, asyncio.wait_for for timeout
isolation, and persists every attempt (success, timeout, or error) to a JSONL
file.  No coordinator, provider, or LLM client coupling.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from Agent.router_agent.schema import Message


@dataclass(frozen=True)
class SummaryTrigger:
    """Immutable trigger result carrying the reasons that caused a summary.

    ``reasons`` is a tuple of reason strings such as ``"fire_change"``,
    ``"person_change"``, ``"conflict"``, ``"stale"``, or ``"periodic"``.
    An empty tuple means no trigger.
    """

    reasons: tuple[str, ...] = ()


class MapSummarizer:
    """Generates short summaries of map deltas with budget, timeout, and
    single-flight guarantees.

    Keyword-only constructor.  No coordinator coupling.

    Parameters
    ----------
    summary_path:
        Path to the JSONL file where every attempt is persisted.
    token_usage_sink:
        Callable invoked with keyword arguments (agent, prompt_tokens,
        completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens)
        on every successful response that carries usage data.
    trigger_interval:
        Minimum number of environment steps between periodic summaries.
    summary_timeout_seconds:
        Maximum wall-clock time to wait for an LLM generate() call.
    max_summary_chars:
        Hard character limit for the summary text.
    """

    def __init__(
        self,
        *,
        summary_path: Path,
        token_usage_sink: Callable[..., None],
        trigger_interval: int = 5,
        summary_timeout_seconds: float = 30.0,
        max_summary_chars: int = 150,
    ) -> None:
        self._summary_path = Path(summary_path)
        self._token_usage_sink = token_usage_sink
        self._trigger_interval = trigger_interval
        self._summary_timeout = summary_timeout_seconds
        self._max_summary_chars = max_summary_chars

        self._lock = asyncio.Lock()
        self._last_attempted_revision: int | None = None
        self._last_success_revision: int | None = None
        self._last_attempt_step: int = -1
        self._last_summary: str = ""

    async def maybe_summarize(
        self,
        *,
        llm_client: Any,
        env_step: int,
        map_revision: int,
        snapshot: dict[str, Any],
        map_delta: dict[str, Any],
    ) -> str:
        """Optionally generate a summary for a map delta.

        Args:
            llm_client:  An async callable with a ``generate(**kwargs)`` method.
            env_step:    Current environment step number.
            map_revision:Monotonic revision counter of the semantic map.
            snapshot:    Current full snapshot dict from the semantic map store.
            map_delta:   Structured delta dict produced by ``MapDiffCalculator``.

        Returns:
            The last successful summary string (empty string if none yet).
            Never raises.
        """
        # ── Quick check outside lock ────────────────────────────────
        if (
            self._last_attempted_revision is not None
            and self._last_attempted_revision == map_revision
        ):
            return self._last_summary

        async with self._lock:
            # Double-check — another coroutine may have claimed this revision
            # between the quick check and lock acquisition.
            if self._last_attempted_revision == map_revision:
                return self._last_summary

            # No delta => nothing to summarise
            if map_delta is None:
                return self._last_summary

            trigger = self._determine_trigger(map_delta, env_step)
            if not trigger.reasons:
                return self._last_summary
            reasons = trigger.reasons

            # Atomically claim this revision before any await, guaranteeing
            # single-flight for the same revision.
            self._last_attempted_revision = map_revision
            self._last_attempt_step = env_step

        # ── Lock released before LLM call ───────────────────────────

        compact = self._build_compact_input(map_delta, snapshot)
        messages = self._build_messages(compact, env_step)

        try:
            response = await asyncio.wait_for(
                llm_client.generate(messages=messages),
                timeout=self._summary_timeout,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._persist(
                    map_delta, map_revision, env_step,
                    reasons, "timeout", self._last_summary, None,
                )
            return self._last_summary
        except Exception:
            async with self._lock:
                self._persist(
                    map_delta, map_revision, env_step,
                    reasons, "error", self._last_summary, None,
                )
            return self._last_summary

        # ── Successful response ────────────────────────────────────

        raw_content = getattr(response, "content", "") or ""
        summary = self._safe_truncate(raw_content)
        usage = getattr(response, "usage", None)

        async with self._lock:
            # Different revisions may overlap. A late response from an older
            # revision must not regress the summary retained for a newer one.
            if (
                self._last_success_revision is None
                or map_revision >= self._last_success_revision
            ):
                self._last_summary = summary
                self._last_success_revision = map_revision
            retained_summary = self._last_summary
            self._persist(
                map_delta, map_revision, env_step,
                reasons, "success", summary, usage,
            )

        # Token sink — errors must not break summary correctness
        if usage is not None:
            try:
                self._call_token_sink(usage)
            except Exception:
                pass

        return retained_summary

    # ── Trigger logic ──────────────────────────────────────────────────

    @staticmethod
    def _has_any(items: Any) -> bool:
        """Return True when *items* is a non-empty iterable."""
        return bool(items)

    def _determine_trigger(
        self,
        map_delta: dict[str, Any],
        env_step: int,
    ) -> SummaryTrigger:
        """Determine the typed trigger result from the map delta.

        An empty ``reasons`` tuple means no summary should be generated.
        """
        reasons: list[str] = []
        change_count = map_delta.get("change_count", 0)
        if change_count == 0:
            return SummaryTrigger()

        fires = map_delta.get("fires", {})
        persons = map_delta.get("persons", {})

        # ── High-priority reasons ───────────────────────────────
        if (
            self._has_any(fires.get("gained"))
            or self._has_any(fires.get("intensity_changed"))
            or self._has_any(fires.get("status_changed"))
        ):
            reasons.append("fire_change")

        if (
            self._has_any(persons.get("gained"))
            or self._has_any(persons.get("status_changed"))
        ):
            reasons.append("person_change")

        if (
            self._has_any(map_delta.get("conflicts_new"))
            or self._has_any(map_delta.get("conflicts_resolved"))
        ):
            reasons.append("conflict")

        if (
            self._has_any(map_delta.get("stale_new"))
            or self._has_any(map_delta.get("stale_resolved"))
        ):
            reasons.append("stale")

        # ── Periodic ───────────────────────────────────────────
        # Establish the first successful/attempted baseline, then trigger
        # periodically whenever the configured interval has elapsed.
        if self._last_attempt_step < 0:
            reasons.append("periodic")
        elif env_step - self._last_attempt_step >= self._trigger_interval:
            reasons.append("periodic")

        return SummaryTrigger(reasons=tuple(reasons))

    # ── Compact projection ────────────────────────────────────────────

    def _build_compact_input(
        self,
        map_delta: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a deterministic compact projection per Phase 4 spec.

        Excludes ``sources``, raw observations, and ``observed_cells``.
        """
        # Active objects — up to 10 fires+persons with stable fields only.
        # Sort by durable identity so an insertion-order difference cannot
        # perturb the prompt or summary output.
        known = snapshot.get("known_dynamic_objects", {})
        active: list[dict[str, Any]] = []
        objects = known.get("fires", []) + known.get("persons", [])
        for obj in sorted(
            objects,
            key=lambda item: (
                str(item.get("object_type", "")),
                str(item.get("name", "")),
            ),
        )[:10]:
            entry: dict[str, Any] = {
                "name": obj.get("name"),
                "object_type": obj.get("object_type"),
                "position": obj.get("position"),
                "status": obj.get("status"),
            }
            attrs = obj.get("attributes", {})
            if "intensity" in attrs:
                entry["intensity"] = attrs["intensity"]
            active.append(entry)

        # Stale/conflict entries — up to 5
        stale_conflicts: list[dict[str, Any]] = []
        stale_or_conflict = (
            snapshot.get("stale_entries", []) + snapshot.get("conflicts", [])
        )
        for entry in sorted(
            stale_or_conflict,
            key=lambda item: (
                str(item.get("object_type", "")),
                str(item.get("name", "")),
            ),
        )[:5]:
            stale_conflicts.append({
                "name": entry.get("name"),
                "object_type": entry.get("object_type"),
            })

        return {
            "delta": self._strip_noise(map_delta),
            "step_budget": self._strip_noise(snapshot.get("step_budget", {})),
            "active_objects": active,
            "stale_conflicts": stale_conflicts,
            "previous_summary": self._last_summary,
        }

    @classmethod
    def _strip_noise(cls, value: Any) -> Any:
        """Remove observation-only fields from nested compact projections."""
        excluded = {
            "confidence",
            "last_seen_ts",
            "observed_cells",
            "recent_observations",
            "sources",
        }
        if isinstance(value, dict):
            return {
                key: cls._strip_noise(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if key not in excluded
            }
        if isinstance(value, list):
            return [cls._strip_noise(item) for item in value]
        return value

    def _build_messages(
        self,
        compact: dict[str, Any],
        env_step: int,
    ) -> list[Message]:
        """Build LLM messages from the compact projection.

        Prompt in Chinese, changes-focused, non-speculative.
        """
        system_prompt = (
            "你是一个SAR（搜索与救援）地图摘要生成助手。"
            f"根据以下变化信息生成一段简洁的中文摘要，不超过{self._max_summary_chars}字。"
            "只描述已确认的事实变化，不要猜测原因或未来状态。"
        )
        user_prompt = (
            f"当前步数：{env_step}\n"
            f"变化信息：{json.dumps(compact, ensure_ascii=False, default=str)}\n"
            "请生成摘要："
        )
        return [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

    # ── Safe truncation ───────────────────────────────────────────────

    def _safe_truncate(self, text: str) -> str:
        """Truncate *text* to ``max_summary_chars`` at a character boundary.

        Python strings are Unicode-aware, so slicing at *max_summary_chars*
        never splits a multi-byte character.
        """
        if len(text) <= self._max_summary_chars:
            return text
        return text[: self._max_summary_chars]

    # ── Persistence ───────────────────────────────────────────────────

    @staticmethod
    def _normalise_usage(usage: Any | None) -> dict[str, int]:
        """Return the stable token-usage schema for object or mapping payloads."""
        fields = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
        )
        if isinstance(usage, Mapping):
            return {field: int(usage.get(field, 0) or 0) for field in fields}
        return {field: int(getattr(usage, field, 0) or 0) for field in fields}

    def _persist(
        self,
        map_delta: dict[str, Any],
        map_revision: int,
        env_step: int,
        reasons: tuple[str, ...],
        status: str,
        summary: str,
        usage: Any | None,
    ) -> None:
        """Append one JSONL entry for this attempt."""
        base_revision = map_delta.get("base_revision", 0) if map_delta else 0

        token_usage = self._normalise_usage(usage)

        entry = {
            "env_step": env_step,
            "base_revision": base_revision,
            "map_revision": map_revision,
            "trigger_reasons": list(reasons),
            "status": status,
            "summary": summary,
            "token_usage": token_usage,
            "timestamp": time.time(),
        }

        try:
            self._summary_path.parent.mkdir(parents=True, exist_ok=True)
            with self._summary_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            # Diagnostics are best-effort and never disrupt orchestration.
            pass

    # ── Token sink ────────────────────────────────────────────────────

    def _call_token_sink(self, usage: Any) -> None:
        """Call ``token_usage_sink`` with normalised keyword arguments.

        Always passes all five token fields with 0 fallback so the sink
        receives a consistent schema.
        """
        self._token_usage_sink(agent="MapSummarizer", **self._normalise_usage(usage))
