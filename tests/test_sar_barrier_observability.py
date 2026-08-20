from __future__ import annotations

import asyncio

import pytest

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


# -- env.step() exception safety (poison-loop regression) ---------------------
#
# Before the fix, _execute_step() called self.env.step(actions) with no
# exception handling, so a raise (real upstream triggers: NavigateTo an
# unresolvable name -> None.get_radius(); UseSupply with a missing arg)
# skipped BOTH `self._step_counter += 1` and `self._action_queue.clear()`.
# Consequences: the step counter froze, and because the queue still held a
# full round, the next submit_action() by ANY agent saw
# len(_action_queue) == num_agents, immediately re-ran the same stale actions,
# and re-raised within milliseconds — repeatedly. Worse, an agent told "your
# action failed" could overwrite its own queue slot, so whichever value
# happened to sit there when the queue stopped raising was what actually
# executed against the environment (last-writer-wins corruption).


def _stub_env_text_hooks(barrier, monkeypatch):
    """Silence the text/checker hooks so tests isolate barrier bookkeeping."""
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)
    monkeypatch.setattr(barrier.env.checker, "get_coverage", lambda: 0.0)
    monkeypatch.setattr(barrier.env.checker, "get_transport_rate", lambda: 0.0)


def test_env_step_exception_does_not_propagate_and_marks_agent_failed(monkeypatch):
    """A raise inside env.step() degrades to a per-agent action failure.

    The step must still complete for everyone: agents that env.step() already
    processed keep their real recorded outcome, and the agent the exception
    fired on is reported as failed (with a non-empty error_type) rather than
    the exception escaping to the caller's tool as an opaque crash."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    _stub_env_text_hooks(barrier, monkeypatch)

    def raising_step(actions):
        # Mimic env.step()'s real behaviour: it walks agents in index order,
        # appending to the history dicts as it goes, and raises partway
        # through — so agent 0's action has already been applied.
        name0 = barrier.env.agent_names[0]
        barrier.env.action_history[name0].append(actions[0])
        barrier.env.action_success_history[name0].append(True)
        raise AttributeError("'NoneType' object has no attribute 'get_radius'")

    monkeypatch.setattr(barrier.env, "step", raising_step)

    barrier._action_queue[0] = ("Move(Up)", True)
    barrier._action_queue[1] = ("NavigateTo(NoSuchThing)", True)

    # Must NOT raise — that escape is what poisoned the barrier.
    barrier._execute_step(expected_step=0)

    log = barrier.get_last_step_log()
    assert log["actions"] == ["Move(Up)", "NavigateTo(NoSuchThing)"]
    # Agent 0 ran before the raise -> real outcome kept; agent 1 failed.
    assert log["successes"] == [True, False]
    assert log["error_types"][0] == ""
    assert log["error_types"][1] == "step_exception:AttributeError"
    # The failure is surfaced to the agent, not silently absorbed.
    assert "was not successful" in barrier.get_current_obs(1)


def test_env_step_exception_advances_step_counter_exactly_once(monkeypatch):
    """The counter must move by exactly 1: 0 froze the barrier, >1 skips steps."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    _stub_env_text_hooks(barrier, monkeypatch)
    monkeypatch.setattr(
        barrier.env, "step", lambda actions: (_ for _ in ()).throw(ValueError("boom"))
    )

    barrier._action_queue[0] = ("UseSupply(GreatFire)", True)
    barrier._action_queue[1] = ("NoOp", True)
    assert barrier._step_counter == 0

    barrier._execute_step(expected_step=0)

    assert barrier._step_counter == 1
    # Exactly one step log was buffered for this step, not zero and not two.
    assert [entry["step"] for entry in barrier.drain_step_logs()] == [1]


def test_env_step_exception_clears_action_queue_so_next_call_starts_fresh(monkeypatch):
    """The queue must be empty afterward, or the next call re-fires the step.

    This is the poison loop itself: with a stale full queue, an unrelated
    agent's next submit_action() would find len(queue) == num_agents already
    true, re-invoke _execute_step on the same actions, and re-raise instantly
    without ever waiting at the barrier."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    _stub_env_text_hooks(barrier, monkeypatch)

    calls = {"n": 0}

    def flaky_step(actions):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("'NoneType' object has no attribute 'get_radius'")
        return "obs", [True] * 2

    monkeypatch.setattr(barrier.env, "step", flaky_step)

    barrier._action_queue[0] = ("NavigateTo(NoSuchThing)", True)
    barrier._action_queue[1] = ("NoOp", True)
    barrier._execute_step(expected_step=0)

    assert barrier._action_queue == {}

    # A single unrelated submit cannot complete a 2-agent round, so it must
    # block at the barrier rather than instantly re-running the failed step.
    async def submit_alone():
        barrier.STEP_TIMEOUT = 30.0
        await asyncio.wait_for(barrier.submit_action(1, "NoOp"), timeout=0.4)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(submit_alone())

    # env.step() was NOT called a second time by that lone submission.
    assert calls["n"] == 1
    assert barrier._step_counter == 1
    assert barrier._action_queue == {1: ("NoOp", True)}


def test_resubmitted_action_replaces_abandoned_one_after_failure(monkeypatch):
    """Last-writer-wins corruption: the abandoned attempt must never execute.

    An agent whose action raised is told it failed and may reasonably try
    something entirely different. Previously its old value stayed queued, so
    whichever action sat in the slot when the queue stopped raising was the
    one that really hit the environment — the agent's abandoned attempt could
    silently execute a step later. Only the freshly submitted action may run."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    _stub_env_text_hooks(barrier, monkeypatch)

    executed: list[list[str]] = []

    def flaky_step(actions):
        executed.append(list(actions))
        if any("NoSuchThing" in a for a in actions):
            raise AttributeError("'NoneType' object has no attribute 'get_radius'")
        return "obs", [True] * 2

    monkeypatch.setattr(barrier.env, "step", flaky_step)

    # Round 1: agent 0's action raises.
    barrier._action_queue[0] = ("NavigateTo(NoSuchThing)", True)
    barrier._action_queue[1] = ("NoOp", True)
    barrier._execute_step(expected_step=0)
    assert barrier._action_queue == {}

    # Round 2: agent 0 abandons that action and submits a different one.
    barrier._action_queue[0] = ("Move(Up)", True)
    barrier._action_queue[1] = ("NoOp", True)
    barrier._execute_step(expected_step=1)

    assert executed == [
        ["NavigateTo(NoSuchThing)", "NoOp()"],
        ["Move(Up)", "NoOp()"],
    ]
    # The abandoned action ran exactly once (its own failed round) and was
    # never resurrected into a later step.
    assert sum("NoSuchThing" in a for step in executed for a in step) == 1
    assert barrier.get_last_step_log()["actions"] == ["Move(Up)", "NoOp()"]
    assert barrier._step_counter == 2


def test_submit_action_reports_failure_when_env_step_raised(monkeypatch):
    """The raising agent's own ToolResult must not claim success.

    Ordinary in-env failures (not_visible etc.) still report success=True and
    explain themselves in the observation text, but a step where env.step()
    raised applied nothing to the environment, so the caller must be told the
    round failed rather than treating it as a real completed turn."""
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)
    _stub_env_text_hooks(barrier, monkeypatch)
    monkeypatch.setattr(
        barrier.env,
        "step",
        lambda actions: (_ for _ in ()).throw(ValueError("not enough values")),
    )

    barrier.STEP_TIMEOUT = 0.5
    result = asyncio.run(barrier.submit_action(0, "UseSupply(GreatFire)"))

    assert result["success"] is False
    assert result["step"] == 1
    assert "was not successful" in result["observation"]
