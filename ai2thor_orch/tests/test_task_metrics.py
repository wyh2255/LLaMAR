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


def test_tracker_snapshot_missing_subtasks_starts_with_full_contract():
    """P0b：未完成动作时 missing_subtasks 就是 contract 全清单（保持原序）。"""
    contract = _make_contract()

    snapshot = TaskMetricsTracker(contract, num_agents=2).snapshot()

    assert snapshot["missing_subtasks"] == list(contract.subtasks)
    assert snapshot["completed_subtask_count"] == 0
    assert snapshot["total_subtasks"] == 6


def test_tracker_snapshot_missing_subtasks_after_partial_progress():
    """P0b：部分完成后 missing_subtasks 只剩缺项（按 contract 原序）。"""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(
                    0,
                    "PickupObject(Bread|id)",
                    success=True,
                    inventory=["Bread"],
                ),
                _result(1, "OpenObject(Fridge|id)", success=True),
            ],
        )
    )

    # pickup 记 NavigateTo(Bread)+PickupObject(Bread)，open 记 OpenObject(Fridge)
    assert snapshot["completed_subtask_count"] == 3
    assert snapshot["missing_subtasks"] == [
        "NavigateTo(Fridge, Bread)",
        "PutObject(Fridge, Bread)",
        "CloseObject(Fridge)",
    ]


def test_tracker_snapshot_item_progress_groups_by_contract_coverage_order():
    """P0b：item_progress 按 contract.coverage_objects 原序逐物品分组（含 door 标记）。"""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(0, "PickupObject(Bread|id)", success=True, inventory=["Bread"]),
                _result(1, "OpenObject(Fridge|id)", success=True),
            ],
        )
    )

    groups = snapshot["item_progress"]
    assert [group["item"] for group in groups] == ["Bread", "Fridge"]

    bread, fridge = groups
    assert (bread["total"], bread["completed"], bread["door"]) == (4, 2, False)
    assert bread["subtasks"] == [
        "NavigateTo(Bread)",
        "PickupObject(Bread)",
        "NavigateTo(Fridge, Bread)",
        "PutObject(Fridge, Bread)",
    ]
    assert bread["missing"] == [
        "NavigateTo(Fridge, Bread)",
        "PutObject(Fridge, Bread)",
    ]

    assert (fridge["total"], fridge["completed"], fridge["door"]) == (2, 1, True)
    assert fridge["subtasks"] == ["OpenObject(Fridge)", "CloseObject(Fridge)"]
    assert fridge["missing"] == ["CloseObject(Fridge)"]


def test_tracker_snapshot_item_progress_complete_state_is_empty_missing():
    """P0b：全部完成后 missing_subtasks 空、每物品分组 missing 也空（渲染成 N/N done）。"""
    tracker = TaskMetricsTracker(_make_contract(), num_agents=2)
    tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(0, "PickupObject(Bread|id)", success=True, inventory=["Bread"]),
                _result(1, "OpenObject(Fridge|id)", success=True),
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

    assert snapshot["missing_subtasks"] == []
    assert snapshot["completed_subtask_count"] == snapshot["total_subtasks"] == 6
    assert [
        (g["item"], g["completed"], g["total"]) for g in snapshot["item_progress"]
    ] == [
        ("Bread", 4, 4),
        ("Fridge", 2, 2),
    ]
    assert all(group["missing"] == [] for group in snapshot["item_progress"])


# ── W1/D4：动词别名归一 / 编号别名剥离 / 大小写不敏感 / 非 grocery 动词 ──


def test_tracker_normalises_pickobject_alias_and_pickup_case_variants():
    """``PickObject``（1 个 checker 的别名）与 ``PickUpObject``（9 个 checker 的
    大小写变体）都须与 ``PickupObject`` 动作配对成功——否则子任务永远记不满。"""
    contract = TaskContract(
        task_id="1_put_computer_book_remotecontrol_sofa",
        subtasks=["PickObject(RemoteControl)"],
        coverage_objects=["RemoteControl"],
    )
    tracker = TaskMetricsTracker(contract, num_agents=1)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(
                    0,
                    "PickupObject(RemoteControl|id)",
                    success=True,
                    inventory=["RemoteControl"],
                )
            ],
        )
    )

    assert snapshot["completed_subtasks"] == ["PickObject(RemoteControl)"]
    assert snapshot["transport_rate"] == pytest.approx(1.0)

    contract2 = TaskContract(
        task_id="synthetic",
        subtasks=["NavigateTo(Bread)", "PickUpObject(Bread)"],
        coverage_objects=["Bread"],
    )
    tracker2 = TaskMetricsTracker(contract2, num_agents=1)

    snapshot2 = tracker2.update(
        RoundResult(
            round_no=1,
            results=[
                _result(0, "PickupObject(Bread|id)", success=True, inventory=["Bread"])
            ],
        )
    )

    assert snapshot2["completed_subtasks"] == [
        "NavigateTo(Bread)",
        "PickUpObject(Bread)",
    ]


def test_tracker_strips_numbered_aliases_and_matches_case_insensitively():
    """动作参数带 ``_1`` 编号别名 / 拼写大小写不同（ButterKnife vs Butterknife）
    仍与 checker 子任务配对；coverage 命中同样大小写不敏感。"""
    contract = TaskContract(
        task_id="synthetic",
        subtasks=["PickupObject(Butterknife)"],
        coverage_objects=["ButterKnife"],
    )
    tracker = TaskMetricsTracker(contract, num_agents=1)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(
                    0,
                    "PickupObject(ButterKnife_1|-01.0|+00.0|+00.0)",
                    success=True,
                    inventory=["ButterKnife_1"],
                )
            ],
        )
    )

    assert snapshot["completed_subtasks"] == ["PickupObject(Butterknife)"]
    assert snapshot["interaction_coverage"] == pytest.approx(1.0)


def test_tracker_credits_navigate_and_completes_non_grocery_verbs():
    """Slice/Clean/Toggle 与 open 同待遇：成功动作同时给 NavigateTo 记账。"""
    contract = TaskContract(
        task_id="synthetic-nongrocery",
        subtasks=[
            "NavigateTo(Bread)",
            "SliceObject(Bread)",
            "NavigateTo(Plate)",
            "CleanObject(Plate)",
            "NavigateTo(Faucet)",
            "ToggleObjectOff(Faucet)",
        ],
        coverage_objects=["Bread", "Plate", "Faucet"],
    )
    tracker = TaskMetricsTracker(contract, num_agents=1)

    snapshot = tracker.update(
        RoundResult(
            round_no=1,
            results=[
                _result(0, "SliceObject(Bread_1|id)", success=True),
                _result(0, "CleanObject(Plate_2|id)", success=True),
                _result(0, "ToggleObjectOff(Faucet_1|id)", success=True),
            ],
        )
    )

    assert snapshot["completed_subtasks"] == [
        "NavigateTo(Bread)",
        "SliceObject(Bread)",
        "NavigateTo(Plate)",
        "CleanObject(Plate)",
        "NavigateTo(Faucet)",
        "ToggleObjectOff(Faucet)",
    ]
    assert snapshot["transport_rate"] == pytest.approx(1.0)
    assert snapshot["missing_subtasks"] == []
