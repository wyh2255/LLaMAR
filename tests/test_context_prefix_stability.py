"""P1 cache optimization — prefix-stability contract tests (M1 + M4).

Covers, for both context-manager copies (worker / router):

* default ``count_window`` keeps the legacy behavior byte-for-byte
  (worker count window, phase-1 middle-zone truncation);
* ``prefix_stable`` is append-only — nothing already written is removed,
  rewritten, or left-shifted;
* phase 1 / phase 3 guards under ``prefix_stable``;
* the extreme-overflow watermark (tail-only truncation, newest first);
* the contract invariant: the prefix segment is identical across a prune call
  even when the legacy compression would have fired.

Contract: ``.agents/context-prefix-stability.md``.
"""

from __future__ import annotations

import copy
import json

import pytest

from Agent.router_agent import context as router_agent_context
from Agent.worker_agent import context as worker_agent_context

MODULES = [
    pytest.param(worker_agent_context, id="worker"),
    pytest.param(router_agent_context, id="router"),
]


def pytest_generate_tests(metafunc):
    """Parametrize every test that asks for ``mod`` over both copies."""
    if "mod" in metafunc.fixturenames:
        metafunc.parametrize("mod", MODULES)


PLACEHOLDER = "[Old tool output cleared to save context space]"


def _make(mod, token_limit: int = 1_000_000, log_dir=None, **cfg_kwargs):
    cfg = mod.ContextConfig(**cfg_kwargs)
    return mod.ContextManager(config=cfg, token_limit=token_limit, log_dir=log_dir)


def _snapshot(messages):
    """Role/content/tool_call_id tuple per message — copy-safe comparison."""
    return [
        (
            m.role,
            tuple(m.content) if isinstance(m.content, list) else m.content,
            m.tool_call_id,
        )
        for m in messages
    ]


def _exec_history(mod, pairs: int = 20, content_len: int = 8):
    """assistant/tool pairs after one user task message."""
    msgs = [mod.Message(role="user", content="head-task")]
    for k in range(pairs):
        msgs.append(mod.Message(role="assistant", content=f"a{k}"))
        msgs.append(mod.Message(role="tool", content=f"{k}" + "x" * content_len))
    return msgs


def _phase1_trigger_history(mod, token_limit: int = 1000):
    """Same shape as tests/test_context_compression.py: phase-1 threshold met."""
    msgs = [mod.Message(role="user", content="A" * 1000) for _ in range(3)]
    msgs += [mod.Message(role="tool", content="B" * 300) for _ in range(10)]
    msgs += [mod.Message(role="user", content="ok") for _ in range(20)]
    return msgs


def _read_events(log_dir, name):
    path = log_dir / "context" / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ──────────────────────────────────────────────────────────────
# Default policy (count_window) — legacy behavior pinned
# ──────────────────────────────────────────────────────────────


class TestDefaultPolicyUnchanged:
    def test_default_config_value(self):
        assert worker_agent_context.ContextConfig().prune_policy == "count_window"
        assert worker_agent_context.ContextConfig().prune_overflow_ratio == 1.0

    def test_worker_count_window_still_prunes(self, tmp_path):
        """Worker count window (recent_messages=12) removes old exec messages."""
        mod = worker_agent_context
        ctx = _make(mod, token_limit=10_000_000, log_dir=tmp_path)
        msgs = _exec_history(mod, pairs=20)  # 40 exec messages
        ctx.prune_history(msgs)

        # last 12 exec messages kept + the leading user task message
        assert len(msgs) == 13
        assert msgs[1].content == "a14"

        events = _read_events(tmp_path, "prune_events.ndjson")
        assert [e["policy"] for e in events] == ["count_prune"]
        assert events[0]["trigger"] == "count_recent_window"
        assert events[0]["pruned_count"] == 28

    def test_phase1_still_rewrites_middle_by_default(self, tmp_path, mod):
        """Legacy path: phase 1 truncates mid-zone tool outputs when triggered."""
        ctx = _make(mod, token_limit=1000, log_dir=tmp_path)
        msgs = _phase1_trigger_history(mod)
        ctx.prune_history(msgs)

        middle = [
            m
            for m in msgs[3:13]
            if isinstance(m.content, str) and m.content == PLACEHOLDER
        ]
        assert middle, "legacy phase 1 did not rewrite the middle zone"
        events = _read_events(tmp_path, "prune_events.ndjson")
        assert any(e["policy"] == "phase1_truncate" for e in events)


# ──────────────────────────────────────────────────────────────
# prefix_stable — append-only
# ──────────────────────────────────────────────────────────────


class TestPrefixStableAppendOnly:
    def test_no_removal_no_shift(self, tmp_path, mod):
        """The count window must not fire: message list is byte-identical."""
        ctx = _make(
            mod, token_limit=10_000_000, log_dir=tmp_path, prune_policy="prefix_stable"
        )
        msgs = _exec_history(mod, pairs=20)
        before = copy.deepcopy(msgs)
        ctx.prune_history(msgs)

        assert _snapshot(msgs) == _snapshot(before)
        assert len(msgs) == 41
        assert _read_events(tmp_path, "prune_events.ndjson") == []
        assert _read_events(tmp_path, "discards.ndjson") == []

    def test_phase1_trigger_does_not_rewrite_prefix(self, tmp_path, mod):
        """Phase-1 trigger conditions are met but nothing is rewritten."""
        ctx = _make(
            mod, token_limit=1000, log_dir=tmp_path, prune_policy="prefix_stable"
        )
        msgs = _phase1_trigger_history(mod)
        before = copy.deepcopy(msgs)
        ctx.prune_history(msgs)

        assert _snapshot(msgs) == _snapshot(before)
        assert _read_events(tmp_path, "prune_events.ndjson") == []
        assert _read_events(tmp_path, "discards.ndjson") == []

    def test_unknown_policy_falls_back_to_legacy(self, tmp_path):
        """A typo'd policy must not silently enable the new mode."""
        mod = worker_agent_context
        ctx = _make(mod, token_limit=10_000_000, log_dir=tmp_path, prune_policy="typo")
        msgs = _exec_history(mod, pairs=20)
        ctx.prune_history(msgs)
        assert len(msgs) == 13  # count window ran (legacy fallback)


# ──────────────────────────────────────────────────────────────
# Extreme-overflow watermark
# ──────────────────────────────────────────────────────────────


class TestOverflowWatermark:
    def _watermark_history(self, mod, pairs: int = 20, content_len: int = 800):
        return _exec_history(mod, pairs=pairs, content_len=content_len)

    def test_below_watermark_is_noop(self, tmp_path, mod):
        ctx = _make(
            mod, token_limit=1_000_000, log_dir=tmp_path, prune_policy="prefix_stable"
        )
        msgs = self._watermark_history(mod)
        before = copy.deepcopy(msgs)
        ctx.prune_history(msgs)
        assert _snapshot(msgs) == _snapshot(before)
        assert _read_events(tmp_path, "prune_events.ndjson") == []

    def test_tail_only_newest_first_truncation(self, tmp_path, mod):
        ctx = _make(
            mod, token_limit=1000, log_dir=tmp_path, prune_policy="prefix_stable"
        )
        msgs = self._watermark_history(mod)
        before = copy.deepcopy(msgs)
        n = len(msgs)
        tail_start = n - ctx.config.recent_messages

        ctx.prune_history(msgs)

        truncated = [i for i in range(n) if msgs[i].content == PLACEHOLDER]
        assert truncated, "overflow watermark did not truncate anything"

        # 1) nothing outside the newest window was touched
        assert _snapshot(msgs[:tail_start]) == _snapshot(before[:tail_start])
        # 2) every truncated index lies in the newest window
        assert all(i >= tail_start for i in truncated)
        # 3) newest-first: if i was truncated, every later long candidate was too
        candidates = [
            i
            for i in range(tail_start, n)
            if before[i].role == "tool"
            and isinstance(before[i].content, str)
            and len(before[i].content) > 200
        ]
        for i in truncated:
            assert all(j in truncated for j in candidates if j > i)
        # 4) exactly one trace event, tagged as the watermark guard
        events = _read_events(tmp_path, "prune_events.ndjson")
        assert len(events) == 1
        assert events[0]["policy"] == "prefix_stable"
        assert events[0]["trigger"] == "overflow_watermark"
        assert events[0]["watermark_tokens"] == 1000
        discards = _read_events(tmp_path, "discards.ndjson")
        assert len(discards) == len(truncated)
        assert {d["reason"] for d in discards} == {"prefix_stable_overflow_truncate"}

    def test_watermark_is_configurable(self, tmp_path, mod):
        """ratio scales the watermark: 0.5 fires where 10.0 stays silent."""
        msgs_low = self._watermark_history(mod)
        ctx_low = _make(
            mod,
            token_limit=1000,
            log_dir=tmp_path / "low",
            prune_policy="prefix_stable",
            prune_overflow_ratio=0.5,
        )
        ctx_low.prune_history(msgs_low)
        events = _read_events(tmp_path / "low", "prune_events.ndjson")
        assert events and events[0]["watermark_tokens"] == 500

        msgs_high = self._watermark_history(mod)
        ctx_high = _make(
            mod,
            token_limit=1000,
            log_dir=tmp_path / "high",
            prune_policy="prefix_stable",
            prune_overflow_ratio=10.0,
        )
        ctx_high.prune_history(msgs_high)
        assert _read_events(tmp_path / "high", "prune_events.ndjson") == []


# ──────────────────────────────────────────────────────────────
# Phase 3 guard
# ──────────────────────────────────────────────────────────────


class TestPhase3Guard:
    async def test_refuses_under_prefix_stable(self, tmp_path, mod):
        ctx = _make(mod, token_limit=10, log_dir=tmp_path, prune_policy="prefix_stable")
        msgs = _phase1_trigger_history(mod)
        before = copy.deepcopy(msgs)
        # llm_client=None would explode if the pipeline tried to summarize
        assert await ctx.compress_with_llm(msgs, None) is False
        assert _snapshot(msgs) == _snapshot(before)

    async def test_default_policy_below_threshold_unchanged(self, mod):
        ctx = _make(mod, token_limit=1_000_000)
        msgs = _phase1_trigger_history(mod)
        before = copy.deepcopy(msgs)
        assert await ctx.compress_with_llm(msgs, None) is False
        assert _snapshot(msgs) == _snapshot(before)


# ──────────────────────────────────────────────────────────────
# Contract invariant (M4 deliverable 3)
# ──────────────────────────────────────────────────────────────


class TestContractInvariant:
    def test_prefix_segment_identical_across_prune(self, tmp_path, mod):
        """Compression-trigger conditions: prefix segment must stay identical.

        Prefix segment = everything outside the newest window; volatile content
        lives only in the trailing state block appended by ``assemble()``.
        """
        ctx = _make(
            mod, token_limit=1000, log_dir=tmp_path, prune_policy="prefix_stable"
        )
        msgs = _phase1_trigger_history(mod)
        before = copy.deepcopy(msgs)

        ctx.prune_history(msgs)
        ctx.prune_history(msgs)  # idempotent: a second pass must not change either

        assert _snapshot(msgs) == _snapshot(before)
        assert [m.role for m in msgs] == [m.role for m in before]

    def test_assemble_appends_state_block_only(self, mod):
        """The volatile state block is appended at the tail, never inserted."""
        ctx = _make(mod, prune_policy="prefix_stable")
        msgs = _exec_history(mod, pairs=6)
        before = copy.deepcopy(msgs)

        out = ctx.assemble("SYS", msgs)

        assert out[0].role == "system"
        history = out[1 : 1 + len(msgs) - 1]
        assert [m.role for m in history] == [m.role for m in msgs[1:]]
        assert [m.content for m in history] == [m.content for m in msgs[1:]]
        assert all(m.role == "user" for m in out[1 + len(msgs) - 1 :])
        assert _snapshot(msgs) == _snapshot(before)
