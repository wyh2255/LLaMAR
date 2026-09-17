"""Tests for the AI2Thor verifier — postconditions and round-level verification."""

from __future__ import annotations

import pytest

from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import ActionResult, RoundResult
from ai2thor_orch.verifier.verifier import (
    verify_postconditions,
    verify_round,
)

# 真机形状（Bug C / RP3 实证：09-15 真机 120 步 run ``logs/rp3_long120b_seed42``）：
# ``parentReceptacles`` 存的是完整 objectId（``Fridge|-02.10|+00.00|+01.07``），
# 不是裸类型名。修复前 verifier 做列表精确成员匹配（``"Fridge" in [...]``）→ 真机永假
# （transport_rate 已到 0.59 时 ``coverage`` 仍恒 0.0）。以下 fixtures 统一用真机形状，
# 防止假数据再次偏离真 build。
FRIDGE_OBJECT_ID = "Fridge|-02.10|+00.00|+01.07"
COUNTERTOP_OBJECT_ID = "CounterTop|-01.06|+00.93|+02.61"


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id="3_transport_groceries",
        scene="FloorPlan1",
        num_agents=2,
        subtasks=[
            "NavigateTo(Bread)",
            "PickupObject(Bread)",
            "PutObject(Fridge, Bread)",
            "NavigateTo(Tomato)",
            "PickupObject(Tomato)",
            "PutObject(Fridge, Tomato)",
        ],
        coverage_objects=["Bread", "Fridge", "Tomato", "Lettuce", "Apple", "Potato"],
    )


def _make_metadata(objects: list[dict]) -> dict:
    """Build a minimal metadata dict with an objects list."""
    return {
        "agents": [
            {"name": "Agent0", "position": {"x": 0.0, "y": 0.0, "z": 0.0},
             "inventory": {"objects": []}},
            {"name": "Agent1", "position": {"x": 1.0, "y": 0.0, "z": 1.0},
             "inventory": {"objects": []}},
        ],
        "objects": objects,
        "lastActionSuccess": True,
        "lastAction": "Pass",
    }


class TestVerifyPostconditions:
    """verify_postconditions — final metadata check."""

    def test_all_in_fridge(self):
        """All grocery objects inside Fridge -> True."""
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            {"objectId": "Lettuce|...", "objectType": "Lettuce",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            {"objectId": "Apple|...", "objectType": "Apple",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            {"objectId": "Potato|...", "objectType": "Potato",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is True

    def test_objectid_parent_receptacles_matched_by_type_prefix(self):
        """Bug C / RP3 回归钉子：真机 ``parentReceptacles`` 存完整 objectId。

        出处：09-15 真机 120 步 run（``logs/rp3_long120b_seed42``）——真 build 写的是
        ``["Fridge|-02.10|+00.00|+01.07"]``，而修复前的精确成员匹配
        (``"Fridge" in parent_receptacles``) 永假：``transport_rate`` 已到 0.59 时
        ``coverage`` 仍恒 0.0。修复后按 ``|`` 前的类型名前缀匹配。
        """
        contract = _make_contract()
        groceries = ["Bread", "Tomato", "Lettuce", "Apple", "Potato"]
        objects = [
            {
                "objectId": f"{name}|-01.5|+00.9|+02.3",
                "objectType": name,
                "parentReceptacles": [FRIDGE_OBJECT_ID],
                "visible": True,
            }
            for name in groceries
        ]
        objects.append(
            {"objectId": FRIDGE_OBJECT_ID, "objectType": "Fridge",
             "parentReceptacles": [], "visible": True}
        )
        metadata = _make_metadata(objects)

        # 真机形状（完整 objectId）→ 判定在冰箱内
        assert verify_postconditions(metadata, contract) is True
        # round 级（Bug C 的实际爆点）：coverage 不再恒 0.0
        result = ActionResult(
            agent_idx=0,
            observation="PutObject succeeded",
            success=True,
            raw=metadata,
        )
        v = verify_round(RoundResult(round_no=3, results=[result]), contract)
        assert v["goal_coverage"] == pytest.approx(1.0)
        assert v["verified_completion"] is True

        # 历史 fake 形状（裸类型名）保持兼容
        legacy = [dict(o, parentReceptacles=["Fridge"]) for o in objects]
        assert verify_postconditions(_make_metadata(legacy), contract) is True

        # 其他容器的 objectId（同为完整形状）不得误命中
        elsewhere = [
            dict(o, parentReceptacles=[COUNTERTOP_OBJECT_ID]) for o in objects
        ]
        assert verify_postconditions(_make_metadata(elsewhere), contract) is False

    def test_none_in_fridge(self):
        """No objects inside Fridge -> False."""
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": [COUNTERTOP_OBJECT_ID], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": [COUNTERTOP_OBJECT_ID], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is False

    def test_partial_in_fridge(self):
        """Some grocery objects inside Fridge, some not -> False."""
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": [COUNTERTOP_OBJECT_ID], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        # Bread is in Fridge, Tomato is not, others not present -> False
        assert verify_postconditions(metadata, contract) is False

    def test_empty_objects(self):
        """Empty objects list -> False (nothing in fridge)."""
        metadata = _make_metadata([])
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is False

    def test_missing_coverage_objects(self):
        """Coverage objects not in metadata at all -> False."""
        objects = [
            {"objectId": "Chair|...", "objectType": "Chair",
             "parentReceptacles": [], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is False

    def test_fridge_ignored_in_check(self):
        """The Fridge object itself is not required to be inside 'Fridge'."""
        objects = [
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        # Only Fridge in metadata is fine — coverage_objects excludes "Fridge"
        # from the "must be in Fridge" check
        assert verify_postconditions(metadata, contract) is False  # no groceries at all

    def test_unsupported_task(self):
        """Unsupported task returns False."""
        contract = TaskContract(task_id="unknown_task")
        metadata = _make_metadata([])
        assert verify_postconditions(metadata, contract) is False


class TestVerifyRound:
    """verify_round — round-level verification."""

    def test_all_fields_present(self):
        """verify_round returns the stable goal-coverage contract."""
        contract = _make_contract()
        result = ActionResult(
            agent_idx=0,
            observation="MoveAhead succeeded",
            success=True,
            raw=_make_metadata([
                {"objectId": "Bread|...", "objectType": "Bread",
                 "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
            ]),
        )
        round_result = RoundResult(
            round_no=1,
            results=[result],
            finished=False,
        )
        v = verify_round(round_result, contract)
        assert "verified_completion" in v
        assert "coverage" in v
        assert "goal_coverage" in v
        assert "details" in v
        assert isinstance(v["verified_completion"], bool)
        assert isinstance(v["coverage"], float)
        assert v["goal_coverage"] == v["coverage"]
        assert isinstance(v["details"], dict)

    def test_no_satisfied_objects(self):
        """No objects in fridge -> coverage 0.0."""
        contract = _make_contract()
        result = ActionResult(
            agent_idx=0,
            observation="MoveAhead succeeded",
            success=True,
            raw=_make_metadata([
                {"objectId": "Bread|...", "objectType": "Bread",
                 "parentReceptacles": [COUNTERTOP_OBJECT_ID], "visible": True},
            ]),
        )
        round_result = RoundResult(round_no=1, results=[result])
        v = verify_round(round_result, contract)
        assert v["verified_completion"] is False
        assert v["coverage"] == 0.0

    def test_partial_coverage(self):
        """Only some objects in fridge -> partial coverage."""
        contract = _make_contract()
        result = ActionResult(
            agent_idx=0,
            observation="PutObject succeeded",
            success=True,
            raw=_make_metadata([
                {"objectId": "Bread|...", "objectType": "Bread",
                 "parentReceptacles": [FRIDGE_OBJECT_ID], "visible": True},
                {"objectId": "Tomato|...", "objectType": "Tomato",
                 "parentReceptacles": [COUNTERTOP_OBJECT_ID], "visible": True},
                {"objectId": "Fridge|...", "objectType": "Fridge",
                 "parentReceptacles": [], "visible": True},
            ]),
        )
        round_result = RoundResult(round_no=1, results=[result])
        v = verify_round(round_result, contract)
        assert v["verified_completion"] is False
        # 5 grocery objects: Bread, Tomato, Lettuce, Apple, Potato
        # Only Bread is in Fridge -> 1/5 = 0.2
        assert v["coverage"] == pytest.approx(0.2)

    def test_all_satisfied(self):
        """All objects in fridge -> verified_completion True."""
        contract = _make_contract()
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": [FRIDGE_OBJECT_ID]},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": []},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": [FRIDGE_OBJECT_ID]},
            {"objectId": "Lettuce|...", "objectType": "Lettuce",
             "parentReceptacles": [FRIDGE_OBJECT_ID]},
            {"objectId": "Apple|...", "objectType": "Apple",
             "parentReceptacles": [FRIDGE_OBJECT_ID]},
            {"objectId": "Potato|...", "objectType": "Potato",
             "parentReceptacles": [FRIDGE_OBJECT_ID]},
        ]
        result = ActionResult(
            agent_idx=0,
            observation="All done",
            success=True,
            raw=_make_metadata(objects),
        )
        round_result = RoundResult(round_no=5, results=[result])
        v = verify_round(round_result, contract)
        assert v["verified_completion"] is True
        assert v["coverage"] == pytest.approx(1.0)

    def test_details_structure(self):
        """Details dict has expected keys."""
        contract = _make_contract()
        result = ActionResult(
            agent_idx=0,
            observation="ok",
            success=True,
            raw=_make_metadata([]),
        )
        round_result = RoundResult(round_no=1, results=[result])
        v = verify_round(round_result, contract)
        details = v["details"]
        assert "grocery_objects" in details
        assert "in_fridge" in details
        assert "satisfied" in details
        assert "round_no" in details
        assert details["round_no"] == 1

    def test_unsupported_task_returns_no_completion(self):
        """Unsupported task returns verified_completion False."""
        contract = TaskContract(task_id="unknown")
        result = ActionResult(agent_idx=0, observation="", success=True)
        round_result = RoundResult(round_no=1, results=[result])
        v = verify_round(round_result, contract)
        assert v["verified_completion"] is False
