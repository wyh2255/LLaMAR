"""Tests for SARWorkerStateProvider and Worker Context auto-refresh."""

import time
from unittest.mock import MagicMock

from Agent.worker_agent.context import (
    ContextConfig,
    WorkerContextManager,
    WorkerPinnedState,
)
from Agent.worker_agent.schema import Message
from Agent.router_agent.state_provider import RuntimeState
from sar_orch.worker_state_provider import SARWorkerStateProvider


class MockBarrier:
    def __init__(self, step=0, finished=False):
        self._step_counter = step
        self._finished = finished
        self.env = MagicMock()
        self.env.controller = MagicMock()
        self.env.controller.get = MagicMock()
        self.env.controller.get_inventory = MagicMock()

    def is_finished(self):
        return self._finished

    def get_current_obs(self, agent_idx):
        return "Fire at (1,2,0) intensity high\nPerson at (3,4,0) trapped"


def _make_mock_barrier_agent(pos=(2, 3, 0), inventory=None, step=0, finished=False):
    barrier = MockBarrier(step=step, finished=finished)
    agent = MagicMock()
    agent.get_position.return_value = pos
    barrier.env.controller.get.return_value = agent
    barrier.env.controller.get_inventory.return_value = inventory or []
    return barrier


def test_provider_returns_runtime_state_payload():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), inventory=["Water"], step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    state = provider.snapshot()

    assert isinstance(state, RuntimeState)
    assert state.version == (5, 0, ("", -1, False))
    assert state.env_step == 5
    assert not state.stale
    assert state.refresh_error == ""
    assert state.get("position") == (2, 3, 0)
    assert state.get("inventory") == ["Water"]
    assert state.get("step") == 5
    assert state.get("mission_status") == "in_progress"


def test_provider_version_caching():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    s2 = provider.snapshot()

    assert s1 is s2
    assert s1.version == (5, 0, ("", -1, False))


def test_provider_version_change_triggers_new_snapshot():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    assert s1.version == (5, 0, ("", -1, False))

    barrier._step_counter = 6
    s2 = provider.snapshot()
    assert s2.version == (6, 0, ("", -1, False))
    assert s2 is not s1


def test_provider_stale_fallback_on_error():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)
    s1 = provider.snapshot()
    assert not s1.stale

    # Force an outer-level exception (not caught by inner position/inventory try/except)
    barrier._step_counter = 6  # force new version
    barrier.is_finished = MagicMock(side_effect=RuntimeError("connection lost"))
    s2 = provider.snapshot()
    assert s2.stale
    assert "connection lost" in s2.refresh_error
    assert s2.get("position") == (2, 3, 0)  # previous data preserved


def test_provider_no_barrier():
    provider = SARWorkerStateProvider(barrier=None, agent_idx=0)
    state = provider.snapshot()
    assert state.version == (0, 0, ("", -1, False))
    assert state.get("position") is None
    assert state.get("step") == 0
    assert state.get("mission_status") == "in_progress"


def test_provider_empty_stale_on_first_failure_no_prior():
    provider = SARWorkerStateProvider(barrier=None, agent_idx=0)
    provider._barrier = MagicMock()  # broken barrier
    provider._barrier._step_counter = 0
    provider._barrier.is_finished.side_effect = RuntimeError("no env")

    state = provider.snapshot()
    assert state.stale
    assert state.version == 0
    assert state.payload == {}


def test_provider_observes_age_ms():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    # First snapshot has age_ms=0 (no prior snapshot)
    assert s1.get("age_ms", -1) == 0.0

    # Advance the env step so a new snapshot is created
    barrier._step_counter = 6
    barrier.env.controller.get.return_value.get_position.return_value = (2, 3, 0)

    time.sleep(0.01)
    s2 = provider.snapshot()
    # Second snapshot has positive age_ms (time since s1 was created)
    assert s2.get("age_ms", -1) > 0


def test_worker_context_manager_accepts_state_provider():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )

    result = ctx.refresh_runtime_state()
    assert result is not None
    assert isinstance(result, RuntimeState)
    assert ctx._runtime_state is not None
    assert ctx._runtime_state.get("position") == (4, 5, 0)
    assert ctx._runtime_state.get("inventory") == ["Sand"]


def test_worker_context_manager_no_provider():
    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid"),
        state_provider=None,
    )
    result = ctx.refresh_runtime_state()
    assert result is None


def test_worker_context_manager_projects_to_pinned_state():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    ps = ctx._pinned_state
    assert isinstance(ps, WorkerPinnedState)
    assert ps.position == (4, 5, 0)
    assert ps.inventory == ["Sand"]
    assert ps.step == 3
    assert ps.mission_status == "in_progress"
    assert ps.state_mode == "semantic"


def test_context_renders_injected_state():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    sys_prompt = "Test system prompt"
    messages = [Message(role="system", content=sys_prompt)]

    assembled = ctx.assemble(sys_prompt, messages)
    mem_text = assembled[-1].content if len(assembled) > 1 else ""

    assert "### Current State" in mem_text
    assert "Position:" in mem_text
    assert "(4, 5, 0)" in mem_text
    assert "Inventory:" in mem_text
    assert "Sand" in mem_text
    assert "Step: 3" in mem_text


def test_context_renders_without_provider():
    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=None,
    )

    sys_prompt = "Test"
    messages = [Message(role="system", content=sys_prompt)]
    assembled = ctx.assemble(sys_prompt, messages)

    mem_text = assembled[-1].content if len(assembled) > 1 else ""
    assert "### Current State" in mem_text


def test_extract_pinned_complements_auto_inject():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    assert ctx._pinned_state.position == (4, 5, 0)

    # Simulate tool result extract (NavigateTo changes position)
    tool_content = "[GPS] Position: (7, 8, 0) | Inventory: ['Water'] | Step: 4"
    extracted = ctx._extract_pinned("navigate_to", tool_content, True)
    assert extracted is not None
    assert extracted["position"] == (7, 8, 0)

    # Apply extraction
    ctx._pinned_state.position = extracted["position"]
    ctx._pinned_state.inventory = extracted["inventory"]
    ctx._pinned_state.step = extracted["step"]
    ctx.pinned.update(extracted)

    assert ctx._pinned_state.position == (7, 8, 0)

    # Next refresh restores auto-injected value
    barrier.env.controller.get.return_value.get_position.return_value = (4, 5, 0)
    ctx.refresh_runtime_state()
    assert ctx._pinned_state.position == (4, 5, 0)
