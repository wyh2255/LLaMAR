"""Tests for CoordinatorSARHooks pre_llm ordering — expected to fail until Phase 5.

Phase 0 contract tests: the pre_llm sequence must be:
  1. prepare_runtime_state(agent.llm) — async, may call LLM for summary
  2. refresh_runtime_state() — sync, pinned projection
  3. prune_history()
  4. assemble()

Also tests that AsyncStatePreparer protocol is optional (plain providers work),
and that summary set during prepare is visible after assemble.
"""

from typing import Any
from unittest.mock import MagicMock

from Agent.router_agent.context import ContextManager
from Agent.router_agent.schema import Message
from Agent.router_agent.state_provider import RuntimeState


# ── Import / existence tests ───────────────────────────────────────────


def test_coordinator_hooks_has_pre_llm():
    """CoordinatorSARHooks must exist and be importable."""
    from Agent.router_agent.hooks import CoordinatorSARHooks

    ctx = MagicMock()
    hooks = CoordinatorSARHooks(ctx=ctx)
    assert hasattr(hooks, "pre_llm")


# ── Phase 0 contract: prepare_runtime_state before refresh ────────────


async def test_hooks_pre_llm_calls_prepare_runtime_state():
    """pre_llm must call prepare_runtime_state before refresh_runtime_state.

    Uses a real order-spy (not MagicMock) so awaiting prepare_runtime_state
    does not raise TypeError in Phase 5.

    Expected to FAIL until Phase 5 adds prepare_runtime_state() to
    ContextManager and hooks call it in the right order.
    """
    from Agent.router_agent.hooks import CoordinatorSARHooks

    class _OrderSpyContextManager:
        """Records method calls in order for strict sequencing assertions."""

        def __init__(self) -> None:
            self.events: list[str] = []
            self._llm_arg: Any = None

        async def prepare_runtime_state(self, llm_client: Any) -> None:
            self.events.append("prepare_runtime_state")
            self._llm_arg = llm_client

        def refresh_runtime_state(self) -> None:
            self.events.append("refresh_runtime_state")

        def prune_history(self, messages: list) -> None:
            self.events.append("prune_history")

        def assemble(self, system_prompt: str, messages: list) -> list:
            self.events.append("assemble")
            return [Message(role="user", content="assembled")]

    ctx = _OrderSpyContextManager()
    hooks = CoordinatorSARHooks(ctx=ctx)

    class FakeAgent:
        llm = "fake_client"
        messages = []
        system_prompt = ""

    agent = FakeAgent()
    result = await hooks.pre_llm(agent, [])

    # Verify call order: prepare_runtime_state before refresh_runtime_state
    prepare_calls = [e for e in ctx.events if e == "prepare_runtime_state"]
    refresh_calls = [e for e in ctx.events if e == "refresh_runtime_state"]
    assert len(prepare_calls) >= 1, "prepare_runtime_state was never called"
    assert len(refresh_calls) >= 1, "refresh_runtime_state was never called"
    prep_idx = ctx.events.index("prepare_runtime_state")
    refr_idx = ctx.events.index("refresh_runtime_state")
    assert prep_idx < refr_idx, (
        f"prepare_runtime_state at {prep_idx} must come before "
        f"refresh_runtime_state at {refr_idx}"
    )
    # llm argument was passed through
    assert ctx._llm_arg == "fake_client", (
        f"Expected llm='fake_client', got {ctx._llm_arg!r}"
    )
    assert result is not None


# ── Plain provider compatibility (GREEN regression) ────────────────────


class _PlainProvider:
    """A minimal StateProvider with only snapshot() — no AsyncStatePreparer."""

    def __init__(self) -> None:
        self._called = 0

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        self._called += 1
        return RuntimeState(
            version=1,
            env_step=1,
            observed_at=0.0,
            payload={
                "step_budget": {"current_step": 1, "max_steps": 50, "remaining": 49},
                "state_mode": "semantic",
            },
        )


async def test_plain_provider_no_prepare_runtime_state():
    """A plain StateProvider (without AsyncStatePreparer) must still work.

    This is a regression test — the current pre_llm() only calls
    refresh_runtime_state/prune/assemble, and a plain provider without
    prepare_runtime_state should not raise.
    """
    from Agent.router_agent.hooks import CoordinatorSARHooks

    ctx = ContextManager(state_provider=_PlainProvider())
    hooks = CoordinatorSARHooks(ctx=ctx)

    class FakeAgent:
        llm = "fake_client"
        messages = [Message(role="user", content="hello")]
        system_prompt = "system prompt"

    agent = FakeAgent()
    # Should not raise — plain provider works through ContextManager
    result = await hooks.pre_llm(agent, [])
    assert result is not None
    # Verify assemble produced message output (the specific content depends
    # on whether CoordinatorPinnedState is configured; what matters is that
    # no AttributeError or TypeError is raised).
    assert isinstance(result, list)
    assert len(result) >= 1


# ── Prune/assemble regression (GREEN) ──────────────────────────────────


async def test_hooks_pre_llm_calls_prune_and_assemble():
    """pre_llm must call prune_history and assemble after state refresh.

    This is a regression test: the current implementation already does this,
    so the test should stay GREEN.
    """
    from unittest.mock import AsyncMock
    from Agent.router_agent.hooks import CoordinatorSARHooks

    ctx = MagicMock()
    ctx.prepare_runtime_state = AsyncMock()
    # Make assemble return a non-None list
    ctx.assemble.return_value = [MagicMock()]
    hooks = CoordinatorSARHooks(ctx=ctx)

    class FakeAgent:
        llm = "fake_client"
        messages = [MagicMock()]
        system_prompt = "system prompt"

    agent = FakeAgent()
    result = await hooks.pre_llm(agent, [])

    ctx.prune_history.assert_called_once()
    ctx.assemble.assert_called_once_with("system prompt", agent.messages)
    assert result is not None


# ── Phase 0 contract: summary visible after refresh (RED) ──────────────


async def test_pre_llm_revision_same_step_summary_visible():
    """The real hook prepares, projects, and renders a same-step map summary."""
    from Agent.router_agent.context import CoordinatorContextManager
    from Agent.router_agent.hooks import CoordinatorSARHooks

    class _PreparableProvider:
        def __init__(self) -> None:
            self._summary = ""

        async def prepare_for_llm(self, llm_client: Any) -> None:
            self._summary = "F1 intensity increased from Medium to High."

        def snapshot(self, context_id: str | None = None) -> RuntimeState:
            return RuntimeState(
                version=7,
                env_step=5,
                observed_at=0.0,
                payload={
                    "step_budget": {"current_step": 5, "max_steps": 50, "remaining": 45},
                    "semantic_summary": {
                        "known_dynamic_objects": {
                            "fires": [{"name": "F1", "attributes": {"intensity": "Medium"}}],
                            "persons": [],
                        },
                    },
                    "team_status_summary": {"workers": [{"agent_id": "Alice"}]},
                    "task_status_view": [],
                    "recent_changes": [],
                    "mission_finished": False,
                    "supervision": {},
                    "map_revision": 7,
                    "map_delta": {"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
                    "map_summary": self._summary,
                    "map_summary_revision": 7,
                },
            )

    provider = _PreparableProvider()
    ctx = CoordinatorContextManager(state_provider=provider)
    hooks = CoordinatorSARHooks(ctx=ctx)

    class FakeAgent:
        llm = "fake_client"
        messages = [Message(role="user", content="hello")]
        system_prompt = "SAR mission"

    result = await hooks.pre_llm(FakeAgent(), [])
    memory = "\n".join(
        message.content if isinstance(message.content, str) else str(message.content)
        for message in result
    )

    assert provider._summary == "F1 intensity increased from Medium to High."
    assert "### Map Summary (revision 7)" in memory
    assert "F1 intensity increased from Medium to High." in memory
