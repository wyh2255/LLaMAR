"""Tests for deterministic AI2Thor task-progress and reliability metrics."""

from __future__ import annotations

import pytest

from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import ActionResult, RoundResult
from ai2thor_orch.metrics.task_metrics import TaskMetricsTracker


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id="3_transport_groceries",
        scene="FloorPlan1",
        num_agents=2,
        subtasks=[
            "NavigateTo(Bread)",
            "PickupObject(Bread)",
            "OpenObject(Fridge)",
            "NavigateTo(Fridge, Bread)",
            "PutObject(Fridge, Bread)",
            "CloseObject(Fridge)",
        ],
        coverage_objects=["Bread", "Fridge"],
    )


def _result(
    agent_idx: int,
    action: str,
    *,
    success: bool,
    inventory: list[str] | None = None,
) -> ActionResult:
    return ActionResult(
        agent_idx=agent_idx,
        action=action,
        observation="",
        success=success,
        inventory=inventory or [],
    )


def test_tracker_separates_legacy_interaction_coverage_from_successful_progress():
    """Failed target interaction counts for legacy coverage but not task progress."""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(
                    0,
                    "PickupObject(Bread|-01.5|+00.9|+02.3)",
                    success=True,
                    inventory=["Bread"],
                ),
                _result(
                    1,
                    "OpenObject(Fridge|+01.0|+00.0|+02.0)",
                    success=False,
                ),
            ],
        )
    )

    assert snapshot["interaction_coverage"] == pytest.approx(1.0)
    assert snapshot["transport_rate"] == pytest.approx(2 / 6)
    assert snapshot["completed_subtasks"] == [
        "NavigateTo(Bread)",
        "PickupObject(Bread)",
    ]
    assert snapshot["action_attempts"] == 2
    assert snapshot["successful_actions"] == 1
    assert snapshot["failed_actions"] == 1
    assert snapshot["action_success_rate"] == pytest.approx(0.5)
    assert snapshot["balance"] == pytest.approx(0.0)


def test_tracker_uses_pre_action_inventory_for_put_and_tracks_cumulative_balance():
    """A successful put credits destination navigation and placement for held item."""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)
    tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(0, "PickupObject(Bread|id)", success=True, inventory=["Bread"]),
                _result(1, "OpenObject(Fridge|id)", success=False),
            ],
        )
    )

    snapshot = tracker.update(
        RoundResult(
            round_no=2,
            results=[
                _result(0, "PutObject(Fridge|id)", success=True),
                _result(1, "CloseObject(Fridge|id)", success=True),
            ],
        )
    )

    assert snapshot["transport_rate"] == pytest.approx(5 / 6)
    assert snapshot["completed_subtasks"] == [
        "NavigateTo(Bread)",
        "PickupObject(Bread)",
        "NavigateTo(Fridge, Bread)",
        "PutObject(Fridge, Bread)",
        "CloseObject(Fridge)",
    ]
    assert snapshot["successful_actions"] == 3
    assert snapshot["failed_actions"] == 1
    assert snapshot["action_success_rate"] == pytest.approx(0.75)
    assert snapshot["per_agent_successful_actions"] == {"0": 2, "1": 1}
    assert snapshot["balance"] == pytest.approx(0.5)


def test_tracker_excludes_timeout_noops_from_action_reliability_but_records_timeouts():
    """Framework-injected NoOps are operational overhead, not agent attempts."""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            timeout_agents=[1],
            results=[
                _result(0, "MoveAhead", success=True),
                _result(1, "NoOp", success=True),
            ],
        )
    )

    assert snapshot["action_attempts"] == 1
    assert snapshot["successful_actions"] == 1
    assert snapshot["failed_actions"] == 0
    assert snapshot["timeout_count"] == 1
    assert snapshot["timeout_rounds"] == 1
    assert snapshot["balance"] == pytest.approx(0.0)


def test_tracker_credits_conditional_open_subtask_using_pre_action_inventory():
    """Historical BaseChecker-style open subtasks retain their held-item guard."""
    tracker = TaskMetricsTracker(
        TaskContract(
            task_id="future_task",
            subtasks=["OpenObject(Drawer, KeyChain)"],
            coverage_objects=["Drawer", "KeyChain"],
            initial_inventory={0: ["KeyChain"]},
        ),
        num_agents=1,
    )

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[_result(0, "OpenObject(Drawer|id)", success=True)],
        )
    )

    assert snapshot["transport_rate"] == 1.0
    assert snapshot["completed_subtasks"] == ["OpenObject(Drawer, KeyChain)"]
