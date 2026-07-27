"""Tests for console user-command injection into the coordinator context."""

import threading

from Agent.router_agent.context import CoordinatorContextManager, CoordinatorPinnedState
from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from sar_orch.user_command_queue import UserCommandQueue


class MockBarrier:
    def __init__(self, step=5, finished=False):
        self._step_counter = step
        self._finished = finished
        self.env = type("Env", (), {"task_timeout": 50})()

    def is_finished(self):
        return self._finished

    def get_env_snapshot(self):
        return {"agents": [], "fires": []}


def _make_provider(queue=None) -> SARCoordinatorStateProvider:
    return SARCoordinatorStateProvider(
        barrier=MockBarrier(),
        semantic_map=None,
        event_store=None,
        state_mode="oracle",
        user_command_queue=queue,
    )


# ── UserCommandQueue ─────────────────────────────────────────────────────


def test_queue_put_and_drain_fifo():
    q = UserCommandQueue()
    q.put("first")
    q.put("second")
    assert len(q) == 2

    drained = q.drain()
    assert [c["text"] for c in drained] == ["first", "second"]
    assert all(c["source"] == "user" for c in drained)
    assert all(c["queued_at"] for c in drained)
    assert len(q) == 0
    assert q.drain() == []


def test_queue_bounded():
    q = UserCommandQueue(max_pending=3)
    for i in range(5):
        q.put(f"cmd-{i}")
    drained = q.drain()
    assert [c["text"] for c in drained] == ["cmd-2", "cmd-3", "cmd-4"]


def test_queue_thread_safe():
    q = UserCommandQueue()

    def producer(n):
        for i in range(100):
            q.put(f"t{n}-{i}")

    threads = [threading.Thread(target=producer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(q) == 50  # bounded at max_pending default
    assert len(q.drain()) == 50


# ── Provider injection ────────────────────────────────────────────────────


def test_snapshot_injects_user_commands_once():
    q = UserCommandQueue()
    provider = _make_provider(queue=q)
    provider.submit_user_command("focus on fire F1")

    snap1 = provider.snapshot()
    assert snap1.payload["user_commands"][0]["text"] == "focus on fire F1"

    # Drained — the next snapshot no longer carries the command
    snap2 = provider.snapshot()
    assert "user_commands" not in snap2.payload


def test_snapshot_without_queue_has_no_user_commands():
    provider = _make_provider(queue=None)
    snap = provider.snapshot()
    assert "user_commands" not in snap.payload


def test_submit_user_command_without_queue_returns_none():
    provider = _make_provider(queue=None)
    assert provider.submit_user_command("hello") is None


def test_user_command_bumps_runtime_version_within_same_step():
    q = UserCommandQueue()
    provider = _make_provider(queue=q)

    base = provider.snapshot().version
    provider.submit_user_command("recall Alice")
    bumped = provider.snapshot().version
    assert bumped > base


def test_mission_graph_snapshot_shape():
    provider = _make_provider()
    view = provider.mission_graph_snapshot()
    assert view["step_budget"]["max_steps"] == 50
    assert view["mission_finished"] is False
    assert view["mission_dag_view"] == []
    assert view["physical_dispatches_view"] == []
    assert view["task_status_view"] == []


# ── ContextManager projection + rendering ────────────────────────────────


def test_projection_sets_and_clears_user_commands():
    provider = _make_provider(queue=UserCommandQueue())
    provider.submit_user_command("send Bob to reservoir R1")

    ctx = CoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()
    ps = ctx._pinned_state
    assert isinstance(ps, CoordinatorPinnedState)
    assert ps.user_commands[0]["text"] == "send Bob to reservoir R1"

    # Next round: queue drained, pinned copy must be cleared
    ctx.refresh_runtime_state()
    assert ps.user_commands == []


def test_user_commands_rendered_in_memory_block():
    provider = _make_provider(queue=UserCommandQueue())
    provider.submit_user_command("prioritize rescuing persons")

    ctx = CoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()
    memory = ctx._render_current_state()

    assert "### User Commands" in memory
    assert "prioritize rescuing persons" in memory
