"""Unit tests for ContextManager._compress_phase1."""

import copy

import pytest

from Agent.router_agent.context import ContextManager, ContextConfig
from Agent.router_agent.schema import Message, ToolCall, FunctionCall


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _long_content(length: int = 500) -> str:
    """Create a string longer than the 200-char truncation threshold."""
    return "A" * length


def _make_tool_calls(id_: str = "call_1") -> list[ToolCall]:
    """Create a single-element ToolCall list for an assistant message."""
    return [
        ToolCall(
            id=id_,
            type="function",
            function=FunctionCall(name="test", arguments={}),
        )
    ]


# ──────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────

class TestCompressPhase1:
    """Tests for _compress_phase1 token-based compression."""

    def test_noop_when_under_threshold(self):
        """No-op when total estimated tokens < 50% of token_limit."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1_000_000)
        messages = [Message(role="user", content="hello") for _ in range(25)]
        original = copy.deepcopy(messages)
        ctx._compress_phase1(messages)
        for orig, msg in zip(original, messages):
            assert orig.content == msg.content
            assert orig.role == msg.role

    def test_truncates_long_tool_results_in_middle_zone(self):
        """Long tool results in the middle zone are replaced with the placeholder.

        Also implicitly verifies that head (first 3) and tail (last 20) are
        *not* truncated, since they fall outside the middle zone.
        """
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)

        head = [Message(role="user", content=_long_content(1000)) for _ in range(3)]
        middle = [Message(role="tool", content=_long_content(300)) for _ in range(10)]
        tail = [Message(role="user", content="ok") for _ in range(20)]
        messages = head + middle + tail

        ctx._compress_phase1(messages)

        # --- Head (indices 0-2) must still have their original content ---
        for i in range(3):
            assert messages[i].content == _long_content(1000), (
                f"head msg {i} was modified"
            )

        # --- Tail (last 20) must still read "ok" ---
        for i in range(len(messages) - 20, len(messages)):
            assert messages[i].content == "ok", f"tail msg {i} was modified"

        # --- At least one tool message in the middle zone should be truncated ---
        truncated = sum(
            1
            for i in range(3, len(messages) - 20)
            if isinstance(messages[i].content, str)
            and messages[i].content
            == "[Old tool output cleared to save context space]"
        )
        assert truncated >= 1, (
            f"expected at least 1 truncated middle message, got {truncated}"
        )

    def test_protects_head(self):
        """First 3 messages are never truncated, even if they contain long tool content."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)

        # Index 2 is a **tool** message with long content — but it's in the head
        # zone, so it must be preserved.
        messages = [
            Message(role="user", content="head0"),
            Message(role="user", content="head1"),
            Message(role="tool", content=_long_content(2000)),
        ]
        # Middle: push total tokens over threshold
        for _ in range(10):
            messages.append(Message(role="tool", content=_long_content(500)))
        # Tail
        for _ in range(20):
            messages.append(Message(role="user", content="t"))

        ctx._compress_phase1(messages)

        # Head tool at index 2 must be preserved
        assert messages[2].content == _long_content(2000)

    def test_protects_tail(self):
        """Last 20 messages (count-based protection) are never truncated."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)

        head = [Message(role="user", content=_long_content(1000)) for _ in range(3)]
        middle = [Message(role="tool", content=_long_content(500)) for _ in range(5)]
        tail = [Message(role="user", content=f"tail_{i}") for i in range(20)]
        messages = head + middle + tail

        ctx._compress_phase1(messages)

        # Every tail message must carry its exact original content
        offset = len(head) + len(middle)
        for i, msg in enumerate(tail):
            assert messages[offset + i].content == msg.content, (
                f"tail msg {i} was modified"
            )

    def test_boundary_alignment(self):
        """Tail is extended backward to keep tool_call/tool_result pairs intact."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)

        messages = [
            Message(role="user", content=_long_content(600)),    # 0 — head
            Message(role="user", content=_long_content(600)),    # 1 — head
            Message(role="user", content=_long_content(600)),    # 2 — head
            # 3 — assistant with tool_calls (just past head_end, so it would
            #     normally be in the truncation zone)
            Message(
                role="assistant",
                content="i-am-a-tool-call",
                tool_calls=_make_tool_calls("call_1"),
            ),
        ]
        # Indices 4-21: filler
        for _ in range(18):
            messages.append(Message(role="user", content="filler"))
        # Index 22: tool result matching the call at index 3 (inside tail zone)
        messages.append(
            Message(role="tool", content=_long_content(500), tool_call_id="call_1")
        )
        # Indices 23-24: end
        for _ in range(2):
            messages.append(Message(role="user", content="end"))

        ctx._compress_phase1(messages)

        # After boundary alignment, the tool_call assistant at index 3 should
        # have been pulled into the protected zone → content preserved.
        assert messages[3].content == "i-am-a-tool-call"

    def test_non_string_content_skipped(self):
        """Tool messages with list[dict] content are never truncated."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)

        messages = []
        # Head: long content to cross threshold
        for _ in range(3):
            messages.append(Message(role="user", content=_long_content(1000)))
        # A tool message with list content (should be skipped by isinstance check)
        messages.append(
            Message(
                role="tool",
                content=[{"type": "text", "text": _long_content(500)}],
            )
        )
        # More tool messages with string content
        for _ in range(6):
            messages.append(Message(role="tool", content=_long_content(500)))
        # Tail
        for _ in range(20):
            messages.append(Message(role="user", content="x"))

        ctx._compress_phase1(messages)

        # The list-content tool message must still be a list
        assert isinstance(messages[3].content, list), (
            "tool message with list content was incorrectly truncated"
        )

    def test_empty_messages_list(self):
        """Empty messages list must not raise."""
        ctx = ContextManager(config=ContextConfig(), token_limit=1000)
        messages: list[Message] = []
        ctx._compress_phase1(messages)  # should not raise
        assert messages == []
