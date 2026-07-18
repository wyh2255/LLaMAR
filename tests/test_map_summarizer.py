"""Tests for MapSummarizer — expected to fail until Phase 4.

Phase 0 contract tests: define MapSummarizer API, trigger rules, single-flight,
timeout, JSONL persistence, token sink, and 150-char hard truncation.

The module sar_orch.map.summarizer does not yet exist, so every test function
is expected to fail with ImportError.

All tests use pytest's per-test temporary-path fixture only, with no manual cleanup.
Every async fake call uses the Phase 4 ``maybe_summarize`` signature:
  maybe_summarize(*, llm_client, env_step, map_revision, snapshot, map_delta)
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


# ── Import tests (fail until Phase 4) ──────────────────────────────────


def test_map_summarizer_module_import():
    """MapSummarizer lives in sar_orch.map.summarizer (not yet created).

    Expected ImportError: sar_orch.map package does not have the summarizer
    submodule.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    assert callable(MapSummarizer)


def test_summarizer_constructor_signature(tmp_path: Path) -> None:
    """MapSummarizer accepts keyword-only args per Phase 4 contract."""
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    summarizer = MapSummarizer(
        summary_path=tmp_path / "nonexistent.jsonl",
        token_usage_sink=lambda agent, **kw: None,
        trigger_interval=5,
        summary_timeout_seconds=5.0,
        max_summary_chars=150,
    )
    assert isinstance(summarizer, MapSummarizer)


def test_summary_trigger_reasons_tuples():
    """SummaryTrigger.reasons is a tuple of reason strings."""
    from sar_orch.map.summarizer import SummaryTrigger  # ImportError expected

    trigger = SummaryTrigger()
    assert isinstance(trigger.reasons, tuple)
    for r in trigger.reasons:
        assert isinstance(r, str)


def test_summary_trigger_preserves_all_detected_reasons(tmp_path: Path) -> None:
    """Trigger detection returns the typed object, not an unstructured bool/list."""
    from sar_orch.map.summarizer import MapSummarizer, SummaryTrigger

    summarizer = MapSummarizer(
        summary_path=tmp_path / "trigger.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )
    trigger = summarizer._determine_trigger(
        {
            "change_count": 4,
            "fires": {"intensity_changed": [{"name": "F1"}]},
            "persons": {"gained": [{"name": "P1"}]},
            "conflicts_new": [{"name": "F1"}],
            "stale_resolved": [{"name": "P0"}],
        },
        env_step=1,
    )

    assert isinstance(trigger, SummaryTrigger)
    assert trigger.reasons == (
        "fire_change",
        "person_change",
        "conflict",
        "stale",
        "periodic",
    )


# ── maybe_summarize contract tests ─────────────────────────────────────


async def test_maybe_summarize_returns_string(tmp_path: Path):
    """maybe_summarize() returns a string (empty on no-trigger, or summary text).

    Uses a fake LLM that records call count and a non-empty delta so the
    summarizer has a reason to trigger.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "summary.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )
    result = await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"intensity_changed": [{"name": "F1"}]}},
    )
    assert isinstance(result, str)


async def test_fake_llm_records_call_count(tmp_path: Path):
    """Fake LLM must record the number of generate() calls so tests can
    verify single-flight behavior.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "calls.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )
    _ = await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )
    assert fake.call_count == 1


# ── Single-flight ──────────────────────────────────────────────────────


async def test_single_flight_same_revision(tmp_path: Path):
    """Same map_revision must NOT trigger two concurrent LLM calls.

    Uses _BlockingFakeLLM so both concurrent calls enter maybe_summarize
    before the event is released, verifying single-flight correctly deduplicates.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _BlockingFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "single_flight.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )

    delta = {"change_count": 1, "fires": {"gained": [{"name": "F1"}]}}
    coros = [
        summarizer.maybe_summarize(
            llm_client=fake, env_step=5, map_revision=42,
            snapshot={}, map_delta=delta,
        )
        for _ in range(2)
    ]
    # Give both coroutines a chance to enter maybe_summarize
    gather_task = asyncio.ensure_future(asyncio.gather(*coros))
    await asyncio.sleep(0.05)
    # Both have entered; verify only one LLM call was made
    assert fake.call_count == 1, (
        f"Expected 1 LLM call for same revision, got {fake.call_count}"
    )
    # Release and let them complete
    fake.release()
    results = await gather_task
    assert len(results) == 2
    assert all(isinstance(r, str) for r in results)
    # Still only one LLM call total
    assert fake.call_count == 1, (
        f"Expected 1 LLM call total, got {fake.call_count}"
    )


async def test_single_flight_different_revisions(tmp_path: Path):
    """Different map_revisions each get their own LLM call."""
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "multi_rev.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )

    delta = {"change_count": 1, "fires": {"gained": [{"name": "F1"}]}}
    r1 = await summarizer.maybe_summarize(
        llm_client=fake, env_step=5, map_revision=42,
        snapshot={}, map_delta=delta,
    )
    r2 = await summarizer.maybe_summarize(
        llm_client=fake, env_step=5, map_revision=43,
        snapshot={}, map_delta=delta,
    )
    assert isinstance(r1, str)
    assert isinstance(r2, str)
    assert fake.call_count == 2, (
        f"Expected 2 LLM calls for different revisions, got {fake.call_count}"
    )


async def test_late_old_revision_cannot_regress_latest_summary(tmp_path: Path):
    """An older in-flight result cannot overwrite a newer revision's summary."""
    from sar_orch.map.summarizer import MapSummarizer

    fake = _OutOfOrderFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "out_of_order.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )
    delta = {"change_count": 1, "fires": {"gained": [{"name": "F1"}]}}

    first = asyncio.create_task(
        summarizer.maybe_summarize(
            llm_client=fake, env_step=5, map_revision=41,
            snapshot={}, map_delta=delta,
        )
    )
    await fake.first_call_started.wait()
    second = await summarizer.maybe_summarize(
        llm_client=fake, env_step=6, map_revision=42,
        snapshot={}, map_delta=delta,
    )
    assert second == "revision 42"

    fake.release_first_call.set()
    await first
    retained = await summarizer.maybe_summarize(
        llm_client=fake, env_step=6, map_revision=42,
        snapshot={}, map_delta=delta,
    )
    assert retained == "revision 42"


# ── Timeout ────────────────────────────────────────────────────────────


async def test_summarizer_timeout_does_not_raise(tmp_path: Path):
    """Timeout in maybe_summarize must NOT propagate — returns last successful
    summary (or empty string if none).
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _SleepyFakeLLM(sleep_seconds=10.0)
    summarizer = MapSummarizer(
        summary_path=tmp_path / "timeout.jsonl",
        token_usage_sink=lambda agent, **kw: None,
        summary_timeout_seconds=0.01,  # Very short timeout
    )
    # Should not raise even with a timeout
    result = await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )
    assert isinstance(result, str)


async def test_summarizer_timeout_preserves_previous(tmp_path: Path):
    """A timeout on the same instance preserves its successful summary."""
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    summarizer = MapSummarizer(
        summary_path=tmp_path / "prev_timeout.jsonl",
        token_usage_sink=lambda agent, **kw: None,
        summary_timeout_seconds=0.01,
    )
    first = await summarizer.maybe_summarize(
        llm_client=_CountingFakeLLM(),
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )
    assert first, "First summary must not be empty"

    second = await summarizer.maybe_summarize(
        llm_client=_SleepyFakeLLM(sleep_seconds=10.0),
        env_step=6,
        map_revision=2,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F2"}]}},
    )
    assert second == first, (
        f"Expected {first!r} (previous successful summary), got {second!r}"
    )

    lines = (tmp_path / "prev_timeout.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["status"] == "timeout"


# ── JSONL persistence ─────────────────────────────────────────────────


async def test_summarizer_jsonl_persistence(tmp_path: Path):
    """Each attempt appends one JSONL line to the summary_path.

    The line must contain the full schema: base_revision, map_revision,
    trigger_reasons, status, summary, token_usage, timestamp.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    path = tmp_path / "map_summary.jsonl"
    summarizer = MapSummarizer(
        summary_path=path,
        token_usage_sink=lambda agent, **kw: None,
    )
    await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=10,
        map_revision=42,
        snapshot={},
        map_delta={
            "base_revision": 41,
            "change_count": 1,
            "fires": {"gained": [{"name": "F1"}]},
        },
    )

    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) >= 1
    entry = json.loads(lines[0])

    assert "env_step" in entry
    assert entry["env_step"] == 10
    assert "base_revision" in entry
    assert entry["base_revision"] == 41
    assert entry["map_revision"] == 42
    assert "trigger_reasons" in entry
    assert isinstance(entry["trigger_reasons"], list)
    assert "status" in entry
    assert isinstance(entry["status"], str)
    assert "summary" in entry
    assert isinstance(entry["summary"], str)
    assert "token_usage" in entry
    assert isinstance(entry["token_usage"], dict)
    assert "timestamp" in entry
    assert isinstance(entry["timestamp"], (int, float))


# ── Token sink ─────────────────────────────────────────────────────────


async def test_summarizer_token_sink(tmp_path: Path):
    """Successful summary with usage calls token_usage_sink with
    agent='MapSummarizer' and exact token numbers.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    sink_calls: list[dict[str, Any]] = []

    def _sink(**kw: Any) -> None:
        sink_calls.append(kw)

    fake = _CountingFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "token_sink.jsonl",
        token_usage_sink=_sink,
    )
    await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )

    assert len(sink_calls) == 1, f"Expected 1 sink call, got {len(sink_calls)}"
    call = sink_calls[0]
    assert call.get("agent") == "MapSummarizer", f"Got agent={call.get('agent')}"
    assert call.get("prompt_tokens") == 50, (
        f"Expected prompt_tokens=50, got {call.get('prompt_tokens')}"
    )
    assert call.get("completion_tokens") == 30, (
        f"Expected completion_tokens=30, got {call.get('completion_tokens')}"
    )
    assert call.get("total_tokens") == 80, (
        f"Expected total_tokens=80, got {call.get('total_tokens')}"
    )


async def test_summarizer_missing_usage_is_failure_isolated(tmp_path: Path) -> None:
    """A successful response without usage still persists and returns safely."""
    from sar_orch.map.summarizer import MapSummarizer

    sink_calls: list[dict[str, Any]] = []
    summarizer = MapSummarizer(
        summary_path=tmp_path / "missing_usage.jsonl",
        token_usage_sink=lambda **kw: sink_calls.append(kw),
    )

    result = await summarizer.maybe_summarize(
        llm_client=_MissingUsageFakeLLM(),
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )

    assert result == "Confirmed change."
    assert sink_calls == []
    entry = json.loads((tmp_path / "missing_usage.jsonl").read_text())
    assert entry["status"] == "success"
    assert entry["token_usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
    }


# ── Truncation ─────────────────────────────────────────────────────────


async def test_summarizer_150_char_truncation(tmp_path: Path):
    """Summary text is truncated to max_summary_chars (150) at a safe boundary."""
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _VerboseFakeLLM()
    summarizer = MapSummarizer(
        summary_path=tmp_path / "trunc.jsonl",
        token_usage_sink=lambda agent, **kw: None,
        max_summary_chars=150,
    )
    result = await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )
    assert result, "Summary must not be empty"
    assert len(result) <= 150, f"Summary length {len(result)} exceeds 150"


# ── Trigger reasons ────────────────────────────────────────────────────


async def test_summarizer_periodic_trigger(tmp_path: Path):
    """After trigger_interval steps, a periodic summary is triggered even
    without a high-priority delta change. Reads JSONL to verify
    trigger_reasons contains 'periodic'.
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    path = tmp_path / "periodic.jsonl"
    summarizer = MapSummarizer(
        summary_path=path,
        token_usage_sink=lambda agent, **kw: None,
        trigger_interval=2,
    )

    # First call at step 3 — triggers (initial state has no summary yet)
    r1 = await summarizer.maybe_summarize(
        llm_client=fake, env_step=3, map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )
    assert isinstance(r1, str)

    # Next call at step 5 — 2 steps apart, triggers periodic
    r2 = await summarizer.maybe_summarize(
        llm_client=fake, env_step=5, map_revision=2,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F2"}]}},
    )
    assert isinstance(r2, str)
    assert fake.call_count == 2

    # Read JSONL and verify trigger_reasons of the second entry
    lines = path.read_text().strip().splitlines()
    assert len(lines) >= 2
    entry = json.loads(lines[1])
    assert "periodic" in entry["trigger_reasons"], (
        f"Expected 'periodic' in trigger_reasons, got {entry['trigger_reasons']}"
    )


async def test_summarizer_high_priority_trigger(tmp_path: Path):
    """High-priority changes (new person, terminal status, new fire) trigger
    a summary immediately, regardless of step interval. Reads JSONL to verify
    trigger_reasons contains the high-priority reason (e.g. person_change).
    """
    from sar_orch.map.summarizer import MapSummarizer  # ImportError expected

    fake = _CountingFakeLLM()
    path = tmp_path / "high_prio.jsonl"
    summarizer = MapSummarizer(
        summary_path=path,
        token_usage_sink=lambda agent, **kw: None,
        trigger_interval=100,  # long interval — won't trigger periodic
    )

    # Delta with a gained person — high-priority trigger
    delta = {
        "change_count": 1,
        "persons": {"gained": [{"name": "P1", "position": [5, 5, 0], "status": "trapped"}]},
    }
    result = await summarizer.maybe_summarize(
        llm_client=fake, env_step=10, map_revision=3,
        snapshot={}, map_delta=delta,
    )
    assert isinstance(result, str)
    assert fake.call_count == 1

    # Read JSONL and verify trigger_reasons contains a high-priority reason
    lines = path.read_text().strip().splitlines()
    assert len(lines) >= 1
    entry = json.loads(lines[0])
    reasons = entry["trigger_reasons"]
    assert "person_change" in reasons, (
        f"Expected 'person_change' in trigger_reasons, got {reasons}"
    )


async def test_summarizer_initial_low_priority_delta_triggers(tmp_path: Path) -> None:
    """The first semantic delta must not be suppressed for lacking a high-priority event."""
    from sar_orch.map.summarizer import MapSummarizer

    fake = _CountingFakeLLM()
    path = tmp_path / "initial_position.jsonl"
    summarizer = MapSummarizer(
        summary_path=path,
        token_usage_sink=lambda agent, **kw: None,
        trigger_interval=5,
    )

    await summarizer.maybe_summarize(
        llm_client=fake,
        env_step=1,
        map_revision=1,
        snapshot={},
        map_delta={
            "change_count": 1,
            "fires": {"position_changed": [{"name": "F1", "old": [1, 1, 0], "new": [2, 2, 0]}]},
        },
    )

    assert fake.call_count == 1
    assert "periodic" in json.loads(path.read_text())["trigger_reasons"]


def test_compact_projection_is_stable_and_excludes_noise(tmp_path: Path) -> None:
    """Compact LLM input is sorted and must not leak raw observation noise."""
    from sar_orch.map.summarizer import MapSummarizer

    summarizer = MapSummarizer(
        summary_path=tmp_path / "compact.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )
    compact = summarizer._build_compact_input(
        {
            "change_count": 1,
            "stale_new": [
                {
                    "name": "OldFire",
                    "sources": [{"note": "delta-secret-source"}],
                    "attributes": {"observed_cells": ["delta-secret-cell"]},
                }
            ],
        },
        {
            "known_dynamic_objects": {
                "fires": [
                    {
                        "name": "F2",
                        "object_type": "fire",
                        "sources": [{"note": "snapshot-secret-source"}],
                        "attributes": {"observed_cells": ["snapshot-secret-cell"]},
                    },
                    {"name": "F1", "object_type": "fire", "attributes": {}},
                ],
                "persons": [{"name": "P1", "object_type": "person", "attributes": {}}],
            },
            "recent_observations": [{"note": "raw-observation-secret"}],
            "stale_entries": [],
            "conflicts": [],
        },
    )

    assert [entry["name"] for entry in compact["active_objects"]] == ["F1", "F2", "P1"]
    serialized = json.dumps(compact, ensure_ascii=False)
    for secret in (
        "delta-secret-source",
        "delta-secret-cell",
        "snapshot-secret-source",
        "snapshot-secret-cell",
        "raw-observation-secret",
    ):
        assert secret not in serialized


async def test_summarizer_tolerates_unwritable_diagnostics_path(tmp_path: Path):
    """JSONL persistence failure cannot break coordinator-facing summary flow."""
    from sar_orch.map.summarizer import MapSummarizer

    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    summarizer = MapSummarizer(
        summary_path=blocked_parent / "summary.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )

    result = await summarizer.maybe_summarize(
        llm_client=_CountingFakeLLM(),
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )

    assert result == "Fire F1 intensity increased from Medium to High."


async def test_mapping_usage_is_normalized_for_jsonl_and_token_sink(tmp_path: Path):
    """Mapping-shaped usage retains values rather than silently becoming zero."""
    from sar_orch.map.summarizer import MapSummarizer

    sink_calls: list[dict[str, int | str]] = []
    summarizer = MapSummarizer(
        summary_path=tmp_path / "mapping_usage.jsonl",
        token_usage_sink=lambda **kwargs: sink_calls.append(kwargs),
    )
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cache_hit_tokens": 3,
        "cache_miss_tokens": 8,
    }

    await summarizer.maybe_summarize(
        llm_client=_CountingFakeLLM(usage=usage),
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )

    entry = json.loads((tmp_path / "mapping_usage.jsonl").read_text(encoding="utf-8"))
    assert entry["token_usage"] == usage
    assert sink_calls == [{"agent": "MapSummarizer", **usage}]


def test_summarizer_builds_router_message_objects(tmp_path: Path) -> None:
    """The real router LLM client requires ``Message`` objects, not dicts."""
    from Agent.router_agent.schema import Message
    from sar_orch.map.summarizer import MapSummarizer

    summarizer = MapSummarizer(
        summary_path=tmp_path / "messages.jsonl",
        token_usage_sink=lambda agent, **kw: None,
    )

    messages = summarizer._build_messages({}, env_step=5)

    assert all(isinstance(message, Message) for message in messages)


# ── Fakes ──────────────────────────────────────────────────────────────


class _MissingUsageFakeLLM:
    """Successful response with no ``usage`` attribute at all."""

    async def generate(self, **kwargs: Any) -> Any:
        return SimpleNamespace(content="Confirmed change.")


class _CountingFakeLLM:
    """Fake LLM that records the number of generate() calls and returns usage."""

    def __init__(self, usage: Any | None = None) -> None:
        self.call_count = 0
        self._usage = usage or _usage(50, 30, 80)

    async def generate(self, **kwargs: Any) -> Any:
        self.call_count += 1
        return _FakeResponse(
            "Fire F1 intensity increased from Medium to High.",
            usage=self._usage,
        )


class _SleepyFakeLLM:
    """Fake LLM that sleeps before returning, to exercise timeout."""

    def __init__(self, sleep_seconds: float = 10.0) -> None:
        self.sleep_seconds = sleep_seconds

    async def generate(self, **kwargs: Any) -> Any:
        await asyncio.sleep(self.sleep_seconds)
        return _FakeResponse("Delayed summary.", usage=_usage(10, 5, 15))


class _VerboseFakeLLM:
    """Fake LLM that returns a long response for truncation testing."""

    async def generate(self, **kwargs: Any) -> Any:
        return _FakeResponse(
            "A very long summary that definitely exceeds one hundred and fifty "
            "characters and should be truncated by the summarizer at a safe "
            "boundary so the coordinator can use it without hitting the token "
            "limit for the context memory block.",
            usage=_usage(10, 200, 210),
        )


class _BlockingFakeLLM:
    """Fake LLM that blocks on an asyncio.Event until released.

    Used to test single-flight: both concurrent calls enter maybe_summarize,
    but only one reaches generate() before the event is released.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self._event = asyncio.Event()

    async def generate(self, **kwargs: Any) -> Any:
        self.call_count += 1
        await self._event.wait()
        return _FakeResponse("Blocking summary.", usage=_usage(10, 10, 20))

    def release(self) -> None:
        self._event.set()


class _OutOfOrderFakeLLM:
    """Returns the newer result before releasing the older in-flight call."""

    def __init__(self) -> None:
        self.call_count = 0
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def generate(self, **kwargs: Any) -> Any:
        self.call_count += 1
        if self.call_count == 1:
            self.first_call_started.set()
            await self.release_first_call.wait()
            return _FakeResponse("revision 41", usage=_usage(10, 10, 20))
        return _FakeResponse("revision 42", usage=_usage(10, 10, 20))


def _usage(prompt_tokens: int, completion_tokens: int, total_tokens: int) -> Any:
    """Match the attribute-based usage object consumed by current Agent code."""
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
    )


class _FakeResponse:
    """Minimal fake LLM response, compatible with LLMResponse schema."""

    def __init__(self, content: str, usage: Any | None = None) -> None:
        self.content = content
        self.finish_reason = "stop"
        self.usage = usage
        self.thinking = None
        self.tool_calls = None
