"""SARBarrier advance=False non-advancing placeholder semantics.

Idle heartbeats must only occupy an agent's slot without pairing into a
real step: when EVERY agent submits advance=False, the barrier waits
indefinitely instead of executing env.step() — no step counter burn, no
60s timeout fill. A step is executed only once a real action
(advance=True) appears, or when a missing agent hits the per-step
timeout.

The internal _action_queue now stores (action, advance) tuples; these
tests drive submit_action() end-to-end through its asyncio paths with a
monkeypatched env (same style as test_sar_barrier_observability.py).
"""

from __future__ import annotations

import asyncio

from sar_orch.barrier import SARBarrier


def _make_barrier(num_agents: int, monkeypatch) -> SARBarrier:
    """Build a barrier whose env.step is stubbed out (no real simulation)."""
    barrier = SARBarrier(num_agents=num_agents, scene=1, seed=42)

    def fake_step(actions):
        return ["act"] * num_agents, [True] * num_agents

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)
    return barrier


async def test_all_placeholders_do_not_advance_until_real_action(monkeypatch):
    """Both agents submit advance=False -> no step, no timeout burn.

    A subsequent advance=True submission must execute exactly one step
    with the real action taking effect (the other agent's placeholder
    becomes NoOp())."""
    barrier = _make_barrier(2, monkeypatch)

    t0 = asyncio.create_task(barrier.submit_action(0, "NoOp", advance=False))
    t1 = asyncio.create_task(barrier.submit_action(1, "NoOp", advance=False))
    await asyncio.sleep(0.05)

    # All-idle placeholder state: step counter untouched and no timeout
    # injection even though both submissions are pending.
    assert barrier._step_counter == 0
    assert len(barrier._action_queue) == 2

    t2 = asyncio.create_task(barrier.submit_action(0, "NavigateTo(X)", advance=True))
    await asyncio.gather(t0, t1, t2)

    assert barrier._step_counter == 1
    log = barrier.get_last_step_log()
    assert log["actions"] == ["NavigateTo(X)", "NoOp()"]
    assert log["timeout_agents"] == []


async def test_placeholder_plus_real_action_advances_normally(monkeypatch):
    """One advance=False + one advance=True -> normal step (+1)."""
    barrier = _make_barrier(2, monkeypatch)

    t0 = asyncio.create_task(barrier.submit_action(0, "NoOp", advance=False))
    await asyncio.sleep(0.02)
    t1 = asyncio.create_task(barrier.submit_action(1, "Move(E)", advance=True))

    r0, r1 = await asyncio.gather(t0, t1)
    assert barrier._step_counter == 1
    assert barrier.get_last_step_log()["actions"] == ["NoOp()", "Move(E)"]
    assert r0["success"] is True and r1["success"] is True


async def test_missing_agent_timeout_fills_noop_and_advances(monkeypatch):
    """advance=True submitted, partner never submits -> timeout fills
    NoOp (advance=True system injection) and the step executes.

    STEP_TIMEOUT is shrunk via monkeypatch so the test never waits 60s.
    """
    monkeypatch.setattr(SARBarrier, "STEP_TIMEOUT", 0.2)
    barrier = _make_barrier(2, monkeypatch)

    t0 = asyncio.create_task(barrier.submit_action(0, "NavigateTo(X)", advance=True))
    await asyncio.sleep(0.05)
    # Queue not full yet: step must NOT have executed before the timeout.
    assert barrier._step_counter == 0

    r0 = await t0
    assert barrier._step_counter == 1
    log = barrier.get_last_step_log()
    assert log["actions"] == ["NavigateTo(X)", "NoOp()"]
    assert log["timeout_agents"] == [1]
    assert r0["success"] is True


async def test_task_noop_with_default_advance_still_consumes_step(monkeypatch):
    """A task-internal LLM NoOp (advance defaults to True) must keep
    consuming one step — only idle heartbeats opt out via advance=False."""
    barrier = _make_barrier(1, monkeypatch)

    result = await barrier.submit_action(0, "NoOp")

    assert barrier._step_counter == 1
    assert barrier.get_last_step_log()["actions"] == ["NoOp()"]
    assert result["success"] is True


async def test_stop_wakes_all_placeholder_waiters_without_advancing(monkeypatch):
    """All-idle waiters blocked on advance=False must be woken by
    barrier.stop() and return finished=True without burning a step."""
    barrier = _make_barrier(2, monkeypatch)

    t0 = asyncio.create_task(barrier.submit_action(0, "NoOp", advance=False))
    t1 = asyncio.create_task(barrier.submit_action(1, "NoOp", advance=False))
    await asyncio.sleep(0.05)
    assert barrier._step_counter == 0

    barrier.stop()

    r0, r1 = await asyncio.gather(t0, t1)
    assert barrier._step_counter == 0
    assert r0["finished"] is True and r1["finished"] is True
