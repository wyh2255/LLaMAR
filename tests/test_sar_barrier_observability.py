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

    barrier._action_queue[0] = "NavigateTo(MissingTarget)"
    barrier._execute_step(expected_step=0)

    log = barrier.get_last_step_log()
    assert log["actions"] == ["NavigateTo(MissingTarget)"]
    assert log["successes"] == [False]
    assert log["error_types"] == ["not_visible"]
    assert isinstance(log["step_duration_ms"], float)
    assert log["step_duration_ms"] >= 0.0
