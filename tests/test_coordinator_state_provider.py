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

    def get_step_budget(self):
        return self.step_budget

    def get_recent_observations(self, limit=5):
        return []

    def snapshot(self):
        return self._snapshot


class RevisionSemanticMap:
    """Minimal atomic map whose revision can advance without an env step."""

    def __init__(self) -> None:
        self.revision = 1

    def snapshot_with_revision(self) -> tuple[int, dict]:
        fires = []
        if self.revision >= 2:
            fires = [
                {
                    "name": "F1",
                    "position": [2, 2, 0],
                    "object_type": "fire",
                    "attributes": {"intensity": "Medium"},
                    "status": "active",
                    "conflict": False,
                }
            ]
        return self.revision, {
            "step_budget": {"current_step": 5, "max_steps": 50, "remaining": 45},
            "known_dynamic_objects": {"fires": fires, "persons": []},
        }

    def snapshot(self) -> dict:
        return self.snapshot_with_revision()[1]

    def get_step_budget(self) -> dict:
        return {"current_step": 5, "max_steps": 50, "remaining": 45}

    def get_recent_observations(self, limit=5) -> list:
        return []


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

    @property
    def mission_node_ids(self):
        return []

    def get_mission_node(self, logical_id):
        return None


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
    assert s1.env_step == s2.env_step
    assert s1.payload == s2.payload

    barrier._step_counter = 4
    semantic_map.step_budget["current_step"] = 4
    s3 = provider.snapshot()
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
                "agents": [{"agent_id": "Alice"}],
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


# ── Phase 0 contract tests: continuity state in provider ──────────────


def test_provider_has_prepare_for_llm():
    """SARCoordinatorStateProvider must implement AsyncStatePreparer protocol.

    Expected to FAIL until Phase 5 adds prepare_for_llm().
    """
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
    )
    assert hasattr(provider, "prepare_for_llm"), "Missing prepare_for_llm method"
    assert callable(provider.prepare_for_llm)


def test_provider_map_delta_in_payload():
    """RuntimeState payload must contain 'map_delta' and 'map_revision'.

    Expected to FAIL until Phase 5 adds map_delta/revision to snapshot().
    """
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
    )
    state = provider.snapshot()
    assert "map_delta" in state.payload, "map_delta missing from RuntimeState.payload"
    assert isinstance(state.payload["map_delta"], (dict, type(None))), (
        "map_delta must be a dict (or None for baseline)"
    )
    assert "map_revision" in state.payload, "map_revision missing"
    assert isinstance(state.payload["map_revision"], int), "map_revision must be int"
    assert state.payload["map_revision"] >= 0, "map_revision must be >= 0"


def test_provider_map_summary_in_payload():
    """RuntimeState payload must contain 'map_summary' and 'map_summary_revision'.

    Expected to FAIL until Phase 5 adds summary fields to snapshot().
    """
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
    )
    state = provider.snapshot()
    assert "map_summary" in state.payload, "map_summary missing"
    assert isinstance(state.payload["map_summary"], str), "map_summary must be str"
    assert "map_summary_revision" in state.payload, "map_summary_revision missing"
    assert isinstance(state.payload["map_summary_revision"], int)


def test_provider_map_revision_distinct_from_version():
    """RuntimeState must have a monotonic map_revision separate from version/env_step.

    Expected to FAIL until Phase 5 adds map_revision tracking.
    """
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=MockSemanticMap(),
    )
    state = provider.snapshot()
    assert "map_revision" in state.payload
    assert isinstance(state.payload["map_revision"], int)
    # map_revision must be >= 0
    assert state.payload["map_revision"] >= 0


async def test_provider_revision_change_bumps_runtime_version():
    """A map revision advances the runtime version even at the same env step."""

    class _RevisionFakeMap:
        def __init__(self) -> None:
            self.revision = 1

        def snapshot_with_revision(self) -> tuple[int, dict]:
            return self.revision, self._snapshot()

        def snapshot(self) -> dict:
            return self._snapshot()

        def _snapshot(self) -> dict:
            fires = []
            if self.revision == 2:
                fires = [{
                    "name": "F1", "position": [2, 2, 0], "object_type": "fire",
                    "attributes": {"intensity": "Medium"}, "status": "active",
                    "conflict": False,
                }]
            return {
                "step_budget": {"current_step": 5, "max_steps": 50, "remaining": 45},
                "known_dynamic_objects": {"fires": fires, "persons": []},
            }

        def get_step_budget(self) -> dict:
            return {"current_step": 5, "max_steps": 50, "remaining": 45}

        def get_recent_observations(self, limit=5) -> list:
            return []

    semantic_map = _RevisionFakeMap()
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(step=5), semantic_map=semantic_map,
    )

    # Phase 5 adds this method; today the await raises AttributeError.
    await provider.prepare_for_llm("fake_client")
    s1 = provider.snapshot()
    semantic_map.revision = 2
    await provider.prepare_for_llm("fake_client")
    s2 = provider.snapshot()

    assert s1.env_step == s2.env_step == 5
    assert s2.version > s1.version
    assert s2.payload["map_revision"] == 2
    assert s2.payload["map_delta"]["fires"]["gained"] == [
        {"name": "F1", "position": [2, 2, 0], "intensity": "Medium"}
    ]


async def test_provider_same_revision_summary_visible():
    """A prepared summary remains visible on a later snapshot of that revision."""

    class _SummaryMap:
        def __init__(self) -> None:
            self.revision = 1

        def snapshot_with_revision(self) -> tuple[int, dict]:
            fires = [] if self.revision == 1 else [{
                "name": "F1", "position": [2, 2, 0], "object_type": "fire",
                "attributes": {"intensity": "Medium"}, "status": "active",
                "conflict": False,
            }]
            return self.revision, {
                "step_budget": {"current_step": 5, "max_steps": 50, "remaining": 45},
                "known_dynamic_objects": {"fires": fires, "persons": []},
            }

        def snapshot(self) -> dict:
            return self.snapshot_with_revision()[1]

        def get_step_budget(self) -> dict:
            return {"current_step": 5, "max_steps": 50, "remaining": 45}

        def get_recent_observations(self, limit=5) -> list:
            return []

    class _RecordingSummarizer:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def maybe_summarize(self, **kwargs) -> str:
            self.calls.append(kwargs)
            return "F1 has been discovered."

    semantic_map = _SummaryMap()
    summarizer = _RecordingSummarizer()
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(step=5),
        semantic_map=semantic_map,
        map_summarizer=summarizer,
    )

    await provider.prepare_for_llm("fake_client")  # revision 1 establishes baseline
    semantic_map.revision = 2
    await provider.prepare_for_llm("fake_client")
    s1 = provider.snapshot()
    s2 = provider.snapshot()

    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["map_revision"] == 2
    assert s1.payload["map_summary"] == "F1 has been discovered."
    assert s1.payload["map_summary_revision"] == 2
    assert s2.payload["map_summary"] == s1.payload["map_summary"]
    assert s2.payload["map_summary_revision"] == s1.payload["map_summary_revision"]


def test_provider_oracle_mode_no_semantic_fields():
    """Oracle mode must NOT expose map_revision, map_delta, or map_summary
    in the RuntimeState payload.

    Expected to FAIL until Phase 5 implements the oracle-mode filter.
    """
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(step=5),
        semantic_map=None,
        event_store=None,
        state_mode="oracle",
    )
    state = provider.snapshot()
    assert state.payload["state_mode"] == "oracle"
    # Semantic continuity fields must not leak
    assert "map_revision" not in state.payload, (
        "oracle mode should not expose map_revision"
    )
    assert "map_delta" not in state.payload, (
        "oracle mode should not expose map_delta"
    )
    assert "map_summary" not in state.payload, (
        "oracle mode should not expose map_summary"
    )


async def test_prepare_after_sync_snapshot_keeps_the_original_baseline():
    """A debug snapshot cannot make the first prepare silently skip its baseline."""
    semantic_map = RevisionSemanticMap()
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(step=5), semantic_map=semantic_map,
    )

    assert provider.snapshot().payload["map_revision"] == 1
    await provider.prepare_for_llm("fake_client")
    semantic_map.revision = 2
    await provider.prepare_for_llm("fake_client")

    state = provider.snapshot()
    assert state.payload["map_revision"] == 2
    assert state.payload["map_delta"]["fires"]["gained"] == [
        {"name": "F1", "position": [2, 2, 0], "intensity": "Medium"}
    ]


async def test_provider_version_advances_when_only_env_step_advances():
    """Runtime version remains monotonic even if map revision is unchanged."""
    barrier = MockBarrier(step=5)
    provider = SARCoordinatorStateProvider(
        barrier=barrier, semantic_map=RevisionSemanticMap(),
    )

    await provider.prepare_for_llm("fake_client")
    first = provider.snapshot()
    barrier._step_counter = 6
    await provider.prepare_for_llm("fake_client")
    second = provider.snapshot()

    assert second.env_step == 6
    assert second.version > first.version


def test_sync_snapshot_refreshes_atomic_revision_without_prepare():
    """Snapshot-only consumers see a same-step map revision refresh safely."""
    semantic_map = RevisionSemanticMap()
    provider = SARCoordinatorStateProvider(
        barrier=MockBarrier(step=5), semantic_map=semantic_map,
    )

    assert provider.snapshot().payload["map_revision"] == 1
    semantic_map.revision = 2
    assert provider.snapshot().payload["map_revision"] == 2


# ── Phase 0 contract tests: CoordinatorPinnedState continuity fields ───
# (moved from test_context_snapshot.py per review)

def test_pinned_state_has_map_revision():
    """CoordinatorPinnedState must include map_revision.

    Expected to FAIL until Phase 6 adds map_revision/map_delta/map_summary
    fields to CoordinatorPinnedState.
    """
    from Agent.router_agent.context import CoordinatorPinnedState

    ps = CoordinatorPinnedState()
    _ = ps.map_revision  # AttributeError expected: not yet defined
    assert isinstance(ps.map_revision, int)


def test_pinned_state_has_map_delta():
    from Agent.router_agent.context import CoordinatorPinnedState

    ps = CoordinatorPinnedState()
    _ = ps.map_delta  # AttributeError expected
    assert isinstance(ps.map_delta, dict)


def test_pinned_state_has_map_summary():
    from Agent.router_agent.context import CoordinatorPinnedState

    ps = CoordinatorPinnedState()
    _ = ps.map_summary  # AttributeError expected
    assert isinstance(ps.map_summary, str)


def test_pinned_state_has_map_summary_revision():
    from Agent.router_agent.context import CoordinatorPinnedState

    ps = CoordinatorPinnedState()
    _ = ps.map_summary_revision  # AttributeError expected
    assert isinstance(ps.map_summary_revision, int)