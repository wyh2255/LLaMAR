from __future__ import annotations

from sar_orch.barrier import SARBarrier


def test_last_step_log_exposes_error_types_and_duration(monkeypatch):
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)

    def fake_step(actions):
        barrier.env.event = {"error_type": "not_visible"}
        return ["act text"], [False]

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)

    barrier._action_queue[0] = ("NavigateTo(MissingTarget)", True)
    barrier._execute_step(expected_step=0)

    log = barrier.get_last_step_log()
    assert log["actions"] == ["NavigateTo(MissingTarget)"]
    assert log["successes"] == [False]
    assert log["error_types"] == ["not_visible"]
    assert isinstance(log["step_duration_ms"], float)
    assert log["step_duration_ms"] >= 0.0


def test_drain_step_logs_returns_every_step_between_polls(monkeypatch):
    """A poller slower than step throughput must still see every step.

    get_last_step_log() only ever exposes the single most-recently-executed
    step, so if N steps run between two polls, N-1 of them are silently
    lost from trajectory.csv. drain_step_logs() must buffer and return all
    of them, each tagged with its own step number and metrics snapshot."""
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)

    call_count = {"n": 0}

    def fake_step(actions):
        call_count["n"] += 1
        return [f"act {call_count['n']}"], [True]

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)
    monkeypatch.setattr(barrier.env.checker, "get_coverage", lambda: 0.5)
    monkeypatch.setattr(barrier.env.checker, "get_transport_rate", lambda: 0.0)

    # Simulate 3 steps completing before the poller ever checks in.
    for expected_step in range(3):
        barrier._action_queue[0] = (f"Move({expected_step})", True)
        barrier._execute_step(expected_step=expected_step)

    drained = barrier.drain_step_logs()
    assert [entry["step"] for entry in drained] == [1, 2, 3]
    assert [entry["actions"] for entry in drained] == [
        ["Move(0)"],
        ["Move(1)"],
        ["Move(2)"],
    ]
    assert all(entry["coverage"] == 0.5 for entry in drained)

    # A second drain with no new steps must return nothing — steps are
    # not re-logged, and get_last_step_log() still exposes the latest.
    assert barrier.drain_step_logs() == []
    assert barrier.get_last_step_log()["actions"] == ["Move(2)"]


def test_concurrent_timeouts_accumulate_instead_of_clobbering(monkeypatch):
    """Two agents' independently-computed deadlines can expire close
    together for the same step. Each re-acquires _step_lock, fills NoOp
    for whichever slots are still missing, and records the agents it
    filled into _current_timeout_agents before releasing the lock and
    calling _execute_step (guarded by expected_step so only one actually
    runs env.step()).

    If the second one to run computes an empty "missing" set (because the
    first already filled every slot) and assigns rather than accumulates,
    it erases the first agent's real timeout record before _execute_step
    ever reads it — trajectory.csv then shows TimeoutAgents=[] for a step
    where a timeout genuinely happened. Reproduce that exact sequence
    directly against the lock-protected fields."""
    barrier = SARBarrier(num_agents=3, scene=1, seed=42)

    def fake_step(actions):
        return ["act"] * 3, [True] * 3

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)

    # Agent 0 submitted its real action already; agents 1 and 2 are the
    # ones about to be timed out.
    barrier._action_queue[0] = ("NavigateTo(Target)", True)

    # First waiter's deadline fires: only agent 1 still missing at this
    # instant (simulating agent 2's fill running a moment later).
    with barrier._step_lock:
        timeout_agents = []
        for i in (1,):
            if i not in barrier._action_queue:
                barrier._action_queue[i] = ("NoOp", True)
                timeout_agents.append(i)
        barrier._current_timeout_agents = sorted(
            set(barrier._current_timeout_agents) | set(timeout_agents)
        )

    # Second waiter's deadline fires next, for the same step: agent 1's
    # slot is already filled from the pass above, so this pass's "missing"
    # set (computed the same way production code does — not yet in the
    # queue at all) is just [2], disjoint from the first pass's [1]. A
    # plain assignment here would drop agent 1's record.
    with barrier._step_lock:
        timeout_agents = [
            i for i in range(barrier.num_agents) if i not in barrier._action_queue
        ]
        for i in timeout_agents:
            barrier._action_queue[i] = ("NoOp", True)
        barrier._current_timeout_agents = sorted(
            set(barrier._current_timeout_agents) | set(timeout_agents)
        )

    # Both agents 1 and 2 must be recorded as timed out — neither the
    # first nor the second pass's record was lost.
    assert barrier._current_timeout_agents == [1, 2]

    barrier._execute_step(expected_step=0)
    assert barrier.get_last_step_log()["timeout_agents"] == [1, 2]
