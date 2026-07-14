"""Tests for CoordinatorStateProvider and runtime state injection."""

import time
from unittest.mock import MagicMock

from Agent.router_agent.context import CoordinatorContextManager, CoordinatorPinnedState
from Agent.router_agent.schema import Message
from Agent.router_agent.state_provider import RuntimeState
from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from a2a.coordinator.supervision_state_store import SupervisionStateStore


class MockBarrier:
    def __init__(self, step=5, finished=False):
        self._step_counter = step
        self._finished = finished
        self.env = MagicMock()
        self.env.task_timeout = 50

    def is_finished(self):
        return self._finished

    def get_env_snapshot(self):
        return {"agents": [], "fires": [{"name": "F1"}]}


class MockSemanticMap:
    def __init__(self, step=5):
        self.step_budget = {"current_step": step, "max_steps": 50, "remaining": 45}
        self._snapshot = {
            "known_priors": {"reservoirs": [], "deposits": []},
            "known_dynamic_objects": {
                "fires": [{"name": "F1", "position": [1, 2, 0]}],
                "persons": [],
            },
            "agents": [{"agent_id": "Alice"}],
            "recent_observations": [],
            "stale_entries": [],
            "conflicts": [],
        }

    def snapshot(self):
        return self._snapshot


class MockEventStore:
    def __init__(self, task_state=None, observations=None):
        self._task_state = task_state or {
            "state": "RUNNING",
            "text": "",
            "updated_at": 0.0,
        }
        self._observations = observations or []

    def get_task_state(self, dispatch_id, worker_id=None):
        return {**self._task_state, "task_id": dispatch_id}

    def get_recent_observations(self, limit=10):
        return self._observations[-limit:]


class MockTaskStore:
    def __init__(self, nodes=None):
        self._nodes = nodes or []
        self._dispatch_to_worker = {}

    def get_plan(self):
        return self._nodes


class MockTaskNode:
    def __init__(self, task_id, worker_id=None, state="pending", result=None):
        self.task_id = task_id
        self.worker_id = worker_id
        self.state = state
        self.result = result


def test_provider_returns_runtime_state_payload():
    barrier = MockBarrier(step=5)
    semantic_map = MockSemanticMap(step=5)
    event_store = MockEventStore()

    provider = SARCoordinatorStateProvider(
        barrier=barrier,
        semantic_map=semantic_map,
        event_store=event_store,
        state_mode="semantic",
    )
    state = provider.snapshot()

    assert isinstance(state, RuntimeState)
    assert state.env_step == 5
    assert state.version == 5
    assert state.stale is False
    assert state.refresh_error == ""
    assert state.payload["state_mode"] == "semantic"
    assert state.payload["step_budget"]["current_step"] == 5
    assert state.payload["semantic_summary"] is not None
    assert state.payload["team_status_summary"] is not None
    assert state.payload["mission_finished"] is False


def test_provider_oracle_mode_uses_barrier_snapshot():
    barrier = MockBarrier(step=10)
    provider = SARCoordinatorStateProvider(
        barrier=barrier,
        semantic_map=None,
        event_store=None,
        state_mode="oracle",
    )
    state = provider.snapshot()

    assert state.payload["state_mode"] == "oracle"
    assert "global_snapshot" in state.payload
    assert state.payload["global_snapshot"]["fires"]


def test_provider_caches_snapshot_by_version():
    barrier = MockBarrier(step=3)
    semantic_map = MockSemanticMap(step=3)
    event_store = MockEventStore()

    provider = SARCoordinatorStateProvider(
        barrier=barrier, semantic_map=semantic_map, event_store=event_store
    )
    s1 = provider.snapshot()
    s2 = provider.snapshot()
    assert s1 is s2

    barrier._step_counter = 4
    semantic_map.step_budget["current_step"] = 4
    s3 = provider.snapshot()
    assert s3 is not s1
    assert s3.env_step == 4


def test_provider_returns_stale_snapshot_on_failure():
    barrier = MockBarrier(step=2)
    provider = SARCoordinatorStateProvider(barrier=barrier, state_mode="semantic")
    # First call builds a snapshot
    s1 = provider.snapshot()
    # Then we break the barrier to simulate failure
    barrier._step_counter = object()  # type: ignore[assignment]
    s2 = provider.snapshot()
    assert s2.stale is True
    assert s2.refresh_error != ""
    assert s2.env_step == s1.env_step


def test_provider_builds_task_status_view_with_task_store():
    node = MockTaskNode("dispatch-1", worker_id="Alice", state="running")
    task_store = MockTaskStore([node])
    task_store._dispatch_to_worker["dispatch-1"] = "worker-uuid"

    event_store = MockEventStore(
        task_state={"state": "RUNNING", "text": "ok", "updated_at": 123.0}
    )
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
        event_store=event_store,
    )
    provider.set_task_store(task_store)
    state = provider.snapshot()

    view = state.payload["task_status_view"]
    assert len(view) == 1
    assert view[0]["dispatch_id"] == "dispatch-1"
    assert view[0]["worker_id"] == "Alice"
    assert view[0]["worker_task_id"] == "worker-uuid"
    assert view[0]["state"] == "RUNNING"


def test_provider_returns_empty_task_view_without_task_store():
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
        event_store=MockEventStore(),
    )
    state = provider.snapshot()
    assert state.payload["task_status_view"] == []


def test_provider_builds_recent_changes_from_observations():
    event_store = MockEventStore(
        observations=[
            {"object_type": "fire", "name": "F1", "step": 3, "note": "weakened"},
            {"object_type": "person", "name": "P2", "step": 4},
        ]
    )
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
        event_store=event_store,
    )
    state = provider.snapshot()
    changes = state.payload["recent_changes"]
    assert len(changes) == 2
    assert "F1" in changes[0]
    assert "P2" in changes[1]


def test_context_manager_projects_runtime_state_to_pinned():
    provider = MagicMock()
    provider.snapshot.return_value = RuntimeState(
        version=7,
        env_step=7,
        observed_at=time.monotonic(),
        payload={
            "step_budget": {"current_step": 7, "max_steps": 50, "remaining": 43},
            "semantic_summary": {"known_dynamic_objects": {"fires": []}},
            "team_status_summary": {"workers": [{"agent_id": "Alice"}]},
            "task_status_view": [
                {
                    "dispatch_id": "dispatch-1",
                    "worker_id": "Alice",
                    "state": "RUNNING",
                }
            ],
            "recent_changes": ["fire F1 observed at step 6"],
            "mission_finished": False,
        },
    )

    ctx = CoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()

    ps = ctx._pinned_state
    assert isinstance(ps, CoordinatorPinnedState)
    assert ps.step_budget["current_step"] == 7
    assert ps.semantic_summary == {"known_dynamic_objects": {"fires": []}}
    assert ps.team_status_summary == {"workers": [{"agent_id": "Alice"}]}
    assert ps.task_status_view[0]["dispatch_id"] == "dispatch-1"
    assert ps.recent_changes == ["fire F1 observed at step 6"]


def test_context_manager_renders_runtime_state_in_memory_block():
    provider = MagicMock()
    provider.snapshot.return_value = RuntimeState(
        version=8,
        env_step=8,
        observed_at=time.monotonic(),
        payload={
            "step_budget": {"current_step": 8, "max_steps": 50, "remaining": 42},
            "semantic_summary": {
                "known_dynamic_objects": {
                    "fires": [{"name": "F1"}],
                    "persons": [{"name": "P1"}],
                },
                "known_priors": {
                    "reservoirs": [{"name": "R1"}],
                    "deposits": [],
                },
                "stale_entries": [],
                "conflicts": [],
            },
            "team_status_summary": {"workers": [{"agent_id": "Alice"}]},
            "task_status_view": [],
            "recent_changes": [],
            "mission_finished": False,
        },
    )

    ctx = CoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()
    messages = ctx.assemble("system", [Message(role="system", content="system")])
    memory = messages[-1].content

    assert "Known fires: 1" in memory
    assert "Known persons: 1" in memory
    assert "Known reservoirs: 1" in memory
    assert "Workers: 1" in memory
    assert "Step: 8 / 50" in memory


def test_provider_includes_supervision_in_task_status_view():
    node = MockTaskNode("dispatch-1", worker_id="Alice", state="running")
    task_store = MockTaskStore([node])
    task_store._dispatch_to_worker["dispatch-1"] = "worker-uuid"

    event_store = MockEventStore(
        task_state={"state": "RUNNING", "text": "ok", "updated_at": 123.0}
    )
    supervision_store = SupervisionStateStore()
    state = supervision_store.get_or_create(
        "dispatch-1", worker_id="Alice", worker_task_id="worker-uuid"
    )
    state.supervision_state = "STALE"
    state.active_alerts["TASK_STALE"] = "ev-1"
    state.last_contact_at = 100.0
    state.last_progress_at = 50.0
    state.last_state_change_at = 75.0
    supervision_store.update("dispatch-1", state)

    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
        event_store=event_store,
        supervision_state_store=supervision_store,
    )
    provider.set_task_store(task_store)
    snapshot = provider.snapshot()

    view = snapshot.payload["task_status_view"]
    assert len(view) == 1
    assert view[0]["supervision_state"] == "STALE"
    assert view[0]["last_contact_at"] == 100.0
    assert view[0]["last_progress_at"] == 50.0
    assert view[0]["last_state_change_at"] == 75.0
    assert view[0]["active_alerts"]["TASK_STALE"] == "ev-1"


def test_provider_builds_supervision_view():
    supervision_store = SupervisionStateStore()
    state = supervision_store.get_or_create("dispatch-1", worker_id="Alice")
    state.supervision_state = "WORKER_UNREACHABLE"
    state.active_alerts["WORKER_UNREACHABLE"] = "ev-2"
    state.unacknowledged_events.append(
        {
            "event_id": "ev-2",
            "event_type": "WORKER_UNREACHABLE",
            "dispatch_id": "dispatch-1",
        }
    )
    supervision_store.update("dispatch-1", state)

    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
        event_store=MockEventStore(),
        supervision_state_store=supervision_store,
    )
    snapshot = provider.snapshot()

    supervision = snapshot.payload["supervision"]
    assert len(supervision["alerts"]) == 1
    assert supervision["alerts"][0]["dispatch_id"] == "dispatch-1"
    assert supervision["alerts"][0]["supervision_state"] == "WORKER_UNREACHABLE"
    assert len(supervision["unacknowledged_events"]) == 1
    assert supervision["unacknowledged_events"][0]["event_type"] == "WORKER_UNREACHABLE"


def test_context_manager_renders_supervision_alerts():
    provider = MagicMock()
    provider.snapshot.return_value = RuntimeState(
        version=9,
        env_step=9,
        observed_at=time.monotonic(),
        payload={
            "step_budget": {"current_step": 9, "max_steps": 50, "remaining": 41},
            "semantic_summary": {
                "known_dynamic_objects": {"fires": []},
                "known_priors": {},
            },
            "team_status_summary": {"workers": []},
            "task_status_view": [],
            "recent_changes": [],
            "mission_finished": False,
            "supervision": {
                "alerts": [],
                "unacknowledged_events": [
                    {
                        "event_id": "ev-1",
                        "event_type": "TASK_STALE",
                        "dispatch_id": "dispatch-1",
                    }
                ],
            },
        },
    )

    ctx = CoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()
    messages = ctx.assemble("system", [Message(role="system", content="system")])
    memory = messages[-1].content

    assert "Supervision alerts:" in memory
    assert "TASK_STALE" in memory
    assert "dispatch-1" in memory
