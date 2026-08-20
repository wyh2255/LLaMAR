"""Acceptance regression tests for structured SAR invalid-action handling.

Regression for candidate 5cfce81, run scene_5_agents_4: the LLM passed a raw
coordinate ``[18,4,0]`` as ``target_id`` to ``navigate_to``. SAR GridEngine's
``Controller.raw_step`` did ``target_obj.get_radius()`` on a non-resolved
target (``None``), raising an uncaught ``AttributeError`` that poisoned the
entire barrier step — another agent's valid ``use_supply`` in the same step
failed too, and both surfaced as ``unclassified_tool_error``.

Expectations after the fix:
- unknown targets / invalid directions / invalid supply types produce a
  structured ``event['success']=False`` with a machine-readable ``error_type``
  (``invalid_target`` / ``invalid_direction`` / ``invalid_supply_type`` /
  ``invalid_action``) instead of an exception;
- a poisoned action never aborts other agents' actions in the same step;
- the codes are allowlisted in ``Agent.error_taxonomy`` so classification
  yields the code itself (never ``unclassified_tool_error``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from Agent.error_taxonomy import (
    FRAMEWORK_ERROR_CODES,
    classify_error,
    error_code_for_result,
)
from sar_orch.barrier import SARBarrier
from sar_orch.tools.worker.navigate_to import NavigateToTool


@pytest.fixture
def barrier():
    b = SARBarrier(num_agents=2, scene=1, seed=42)
    b.STEP_TIMEOUT = 5.0
    yield b
    b.stop()


@pytest.fixture
def env(barrier):
    return barrier.env


# ---------------------------------------------------------------------------
# NavigateTo: unknown target id / raw coordinate
# ---------------------------------------------------------------------------


async def test_navigate_to_raw_coordinate_returns_structured_failure(barrier):
    """The exact production path that crashed on 5cfce81: LLM passes a raw
    coordinate string as target_id. The tool must return success=False with
    the allowlisted invalid_target code, never unclassified_tool_error."""
    tool = NavigateToTool(barrier, 0)
    result = await tool.execute(target_id="[18,4,0]")
    assert result.success is False
    assert result.error == "invalid_target"
    assert classify_error(result.error) == "invalid_target"
    assert error_code_for_result(result) == "invalid_target"
    assert error_code_for_result(result) != "unclassified_tool_error"
    assert "Failed to navigate" in result.content


async def test_navigate_to_unknown_name_returns_structured_failure(barrier):
    tool = NavigateToTool(barrier, 0)
    result = await tool.execute(target_id="NoSuchObject_42")
    assert result.success is False
    assert result.error == "invalid_target"
    assert error_code_for_result(result) == "invalid_target"


def test_raw_step_navigate_to_none_target(env):
    """parse_action resolves an unknown name to to_target_id=None; raw_step
    must return a structured failure instead of AttributeError."""
    event = env.controller.raw_step("NavigateTo", agent_idx=0, to_target_id=None)
    assert event["success"] is False
    assert event["error_type"] == "invalid_target"
    assert "invalid_target" in event["info"]


# ---------------------------------------------------------------------------
# Move: invalid direction must not assert
# ---------------------------------------------------------------------------


def test_raw_step_move_invalid_direction_returns_structured_failure(env):
    """'UpLeft' is in TO_DELTA_MAP (so it passes the visibility gate) but is
    not MOVABLE_CARDINAL_DIRECTIONS — previously an AssertionError."""
    event = env.controller.raw_step("Move", agent_idx=0, to_target_id="UpLeft")
    assert event["success"] is False
    assert event["error_type"] == "invalid_direction"
    assert "invalid_direction" in event["info"]


def test_env_step_move_invalid_direction_no_assertion(env):
    _, successes = env.step(["Move(UpLeft)", "NoOp()"])
    assert successes == [False, True]
    assert env.per_agent_error_types == ["invalid_direction", ""]


# ---------------------------------------------------------------------------
# A poisoned action must not fail other agents' actions in the same step
# ---------------------------------------------------------------------------


def test_poisoned_action_does_not_fail_other_agent_in_env_step(env):
    """Agent 0's malformed NavigateTo no longer aborts agent 1's NoOp."""
    _, successes = env.step(["NavigateTo([18,4,0])", "NoOp()"])
    assert successes == [False, True]
    assert env.per_agent_error_types[0] == "invalid_target"
    assert env.per_agent_error_types[1] == ""


async def test_poisoned_action_does_not_fail_other_agent_in_barrier(barrier):
    """Both agents' submit_action must complete; only the poisoned agent is
    marked failed, and only for the poisoned agent is success propagated."""
    results = await asyncio.gather(
        barrier.submit_action(0, "NavigateTo([1,2,3])"),
        barrier.submit_action(1, "NoOp()"),
    )
    poisoned, healthy = results[0], results[1]
    assert poisoned["success"] is False
    assert poisoned["error"] == "invalid_target"
    assert healthy["success"] is True
    assert "error" not in healthy


# ---------------------------------------------------------------------------
# Remaining crash branches in the action dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "kwargs", "expected_code"),
    [
        ("Carry", {"from_target_id": None}, "invalid_target"),
        ("DropOff", {"from_target_id": None, "to_target_id": None}, "invalid_target"),
        ("StoreSupply", {"to_target_id": None}, "invalid_target"),
        ("UseSupply", {"to_target_id": None, "supply_type": "Water"}, "invalid_target"),
        ("GetSupply", {"from_target_id": None}, "invalid_target"),
    ],
)
def test_other_crashing_branches_return_structured_failure(
    env, action, kwargs, expected_code
):
    event = env.controller.raw_step(action, agent_idx=0, **kwargs)
    assert event["success"] is False
    assert event["error_type"] == expected_code
    assert expected_code in event["info"]


def test_dropoff_valid_target_but_missing_deposit_is_structured_failure(env):
    """from_target_id resolves (any visible object) but deposit id is None
    → invalid_target, not crash."""
    visible = env.controller.get_globally_visible_ids(0)
    assert visible, "agent should see at least one object at reset"
    event = env.controller.raw_step(
        "DropOff", agent_idx=0, from_target_id=visible[0], to_target_id=None
    )
    assert event["success"] is False
    assert event["error_type"] == "invalid_target"
    assert "invalid_target" in event["info"]


def test_invalid_supply_type_returns_structured_failure(env):
    """An unrecognized supply_type previously raised KeyError inside raw_step."""
    event = env.controller.raw_step(
        "UseSupply", agent_idx=0, to_target_id=None, supply_type="Foam"
    )
    assert event["success"] is False
    assert event["error_type"] == "invalid_supply_type"


def test_env_step_isolates_exceptions_into_structured_failure(env, monkeypatch):
    """Even a non-target crash in one agent's step must be isolated so the
    other agents' actions still execute and the step completes."""
    real_step = env.controller.step

    def crashing_step(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.controller, "step", crashing_step)
    _, successes = env.step(["NavigateTo(GreatFire_Region_1)", "NoOp()"])
    assert successes == [False, False]
    assert all(t == "invalid_action" for t in env.per_agent_error_types)
    monkeypatch.setattr(env.controller, "step", real_step)


# ---------------------------------------------------------------------------
# Barrier defensive net: a crashing env.step never leaves the barrier poisoned
# ---------------------------------------------------------------------------


def test_barrier_completes_step_when_env_step_raises(barrier, monkeypatch):
    def fake_step(actions):
        raise RuntimeError("simulated env.step crash")

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)

    barrier._action_queue[0] = ("NavigateTo(Whatever)", True)
    barrier._execute_step(expected_step=0)  # must NOT raise

    log = barrier.get_last_step_log()
    assert log["successes"] == [False, False]
    assert log["error_types"] == [
        "step_exception:RuntimeError",
        "step_exception:RuntimeError",
    ]
    assert barrier._step_counter == 1


# ---------------------------------------------------------------------------
# Taxonomy allowlisting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["invalid_target", "invalid_direction", "invalid_supply_type", "invalid_action"],
)
def test_new_action_codes_are_allowlisted(code):
    assert code in FRAMEWORK_ERROR_CODES
    assert classify_error(code) == code


def _dummy_result(success, error):
    return SimpleNamespace(success=success, error=error)


def test_failed_result_classifies_to_allowlisted_code_not_sentinel():
    for code in ("invalid_target", "invalid_direction"):
        result = _dummy_result(success=False, error=code)
        assert error_code_for_result(result) == code
        assert error_code_for_result(result) != "unclassified_tool_error"
