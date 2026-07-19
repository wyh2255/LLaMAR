"""Tests for Phase 4 team coordination rendering in WorkerContextManager."""

from __future__ import annotations

from Agent.worker_agent.context import (
    ContextConfig,
    WorkerContextManager,
)
from Agent.worker_agent.schema import Message
from Agent.router_agent.state_provider import RuntimeState
from sar_orch.worker_state_provider import SARWorkerStateProvider


def _make_provider_with_teammates(teammates: list[dict]) -> SARWorkerStateProvider:
    """Create a SARWorkerStateProvider with pre-cached teammate data."""
    provider = SARWorkerStateProvider(barrier=None, agent_idx=0)
    provider._cached_teammates = teammates
    return provider


def _make_runtime_state_with_team(teammates: list[dict]) -> RuntimeState:
    """Create a RuntimeState with team_coordination payload."""
    return RuntimeState(
        version=(0, 0, ("", -1, False)),
        env_step=0,
        observed_at=0.0,
        payload={
            "team_coordination": {
                "teammates": teammates,
                "teammates_count": len(teammates),
            }
        },
    )


class TestRenderTeamCoordination:
    def test_no_teammates_returns_empty(self):
        """When no teammates exist, _render_team_coordination returns ''."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        # No runtime_state set → returns ""
        result = ctx._render_team_coordination()
        assert result == ""

    def test_empty_teammates_list_returns_empty(self):
        """Empty teammate list returns ''."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        ctx._runtime_state = _make_runtime_state_with_team([])
        result = ctx._render_team_coordination()
        assert result == ""

    def test_single_teammate_rendered(self):
        """Single teammate renders with position, inventory, task."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        teammates = [
            {
                "agent_id": "worker-beta",
                "position": [3, 4, 0],
                "inventory": ["Water", "Sand"],
                "current_task_id": "extinguish_fire",
                "task_state": "RUNNING",
                "is_carrying_person": False,
            }
        ]
        ctx._runtime_state = _make_runtime_state_with_team(teammates)
        result = ctx._render_team_coordination()

        assert "### Team Coordination" in result
        assert "worker-beta" in result
        assert "at (3, 4, 0)" in result
        assert "extinguish_fire (RUNNING)" in result
        assert "Water x1 + Sand x1" in result
        assert "[CARRYING PERSON]" not in result

    def test_carrying_person_marker(self):
        """Teammate carrying person shows [CARRYING PERSON]."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        teammates = [
            {
                "agent_id": "worker-beta",
                "position": [5, 5, 0],
                "inventory": ["Person"],
                "current_task_id": "rescue_person",
                "task_state": "RUNNING",
                "is_carrying_person": True,
            }
        ]
        ctx._runtime_state = _make_runtime_state_with_team(teammates)
        result = ctx._render_team_coordination()

        assert "[CARRYING PERSON]" in result
        assert "carrying Person" in result

    def test_multiple_teammates(self):
        """Multiple teammates render correctly."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        teammates = [
            {
                "agent_id": "worker-beta",
                "position": [1, 2, 0],
                "inventory": ["Water"],
                "current_task_id": "explore",
                "task_state": "RUNNING",
                "is_carrying_person": False,
            },
            {
                "agent_id": "worker-gamma",
                "position": [7, 8, 0],
                "inventory": ["Person"],
                "current_task_id": "rescue",
                "task_state": "RUNNING",
                "is_carrying_person": True,
            },
        ]
        ctx._runtime_state = _make_runtime_state_with_team(teammates)
        result = ctx._render_team_coordination()

        assert "worker-beta" in result
        assert "worker-gamma" in result
        assert "[CARRYING PERSON]" in result
        assert "Water x1" in result

    def test_renders_in_memory_block(self):
        """Team coordination section appears in assembled memory block."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        ctx._runtime_state = _make_runtime_state_with_team([
            {
                "agent_id": "worker-beta",
                "position": [3, 4, 0],
                "inventory": [],
                "current_task_id": "idle",
                "task_state": "WAITING",
                "is_carrying_person": False,
            }
        ])

        sys_prompt = "Test system prompt"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content if len(assembled) > 1 else ""

        assert "### Team Coordination" in mem_text
        assert "worker-beta" in mem_text

    def test_empty_inventory_rendered(self):
        """Empty inventory renders as 'empty'."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        teammates = [
            {
                "agent_id": "worker-beta",
                "position": [0, 0, 0],
                "inventory": [],
                "current_task_id": "",
                "task_state": "UNKNOWN",
                "is_carrying_person": False,
            }
        ]
        ctx._runtime_state = _make_runtime_state_with_team(teammates)
        result = ctx._render_team_coordination()

        assert "empty" in result
        assert "worker-beta" in result

    def test_concurrent_inventory_with_counts(self):
        """Multiple items with counts are rendered correctly."""
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid"),
            state_provider=None,
        )
        teammates = [
            {
                "agent_id": "worker-beta",
                "position": [2, 3, 0],
                "inventory": ["Water", "Water", "Sand"],
                "current_task_id": "firefight",
                "task_state": "RUNNING",
                "is_carrying_person": False,
            }
        ]
        ctx._runtime_state = _make_runtime_state_with_team(teammates)
        result = ctx._render_team_coordination()

        assert "Water x2" in result
        assert "Sand x1" in result

    def test_team_coordination_comes_from_runtime_state(self):
        """Verifies team_coordination payload origin from runtime state."""
        provider = _make_provider_with_teammates([
            {
                "agent_id": "worker-beta",
                "position": [3, 4, 0],
                "inventory": ["Water"],
                "current_task_id": "explore",
                "task_state": "RUNNING",
                "is_carrying_person": False,
            }
        ])
        state = provider.snapshot()
        tc = state.get("team_coordination", {})
        assert tc["teammates_count"] == 1
        assert tc["teammates"][0]["agent_id"] == "worker-beta"
