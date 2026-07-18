"""Tests for the AI2Thor verifier — postconditions and round-level verification."""

from __future__ import annotations

import pytest

from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import ActionResult, RoundResult
from ai2thor_orch.verifier.verifier import (
    verify_postconditions,
    verify_round,
)


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
             "parentReceptacles": ["Fridge"], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": ["Fridge"], "visible": True},
            {"objectId": "Lettuce|...", "objectType": "Lettuce",
             "parentReceptacles": ["Fridge"], "visible": True},
            {"objectId": "Apple|...", "objectType": "Apple",
             "parentReceptacles": ["Fridge"], "visible": True},
            {"objectId": "Potato|...", "objectType": "Potato",
             "parentReceptacles": ["Fridge"], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is True

    def test_none_in_fridge(self):
        """No objects inside Fridge -> False."""
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": ["CounterTop"], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": ["CounterTop"], "visible": True},
        ]
        metadata = _make_metadata(objects)
        contract = _make_contract()
        assert verify_postconditions(metadata, contract) is False

    def test_partial_in_fridge(self):
        """Some grocery objects inside Fridge, some not -> False."""
        objects = [
            {"objectId": "Bread|...", "objectType": "Bread",
             "parentReceptacles": ["Fridge"], "visible": True},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": [], "visible": True},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": ["CounterTop"], "visible": True},
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
        """verify_round returns all required fields."""
        contract = _make_contract()
        result = ActionResult(
            agent_idx=0,
            observation="MoveAhead succeeded",
            success=True,
            raw=_make_metadata([
                {"objectId": "Bread|...", "objectType": "Bread",
                 "parentReceptacles": ["Fridge"], "visible": True},
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
        assert "details" in v
        assert isinstance(v["verified_completion"], bool)
        assert isinstance(v["coverage"], float)
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
                 "parentReceptacles": ["CounterTop"], "visible": True},
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
                 "parentReceptacles": ["Fridge"], "visible": True},
                {"objectId": "Tomato|...", "objectType": "Tomato",
                 "parentReceptacles": ["CounterTop"], "visible": True},
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
             "parentReceptacles": ["Fridge"]},
            {"objectId": "Fridge|...", "objectType": "Fridge",
             "parentReceptacles": []},
            {"objectId": "Tomato|...", "objectType": "Tomato",
             "parentReceptacles": ["Fridge"]},
            {"objectId": "Lettuce|...", "objectType": "Lettuce",
             "parentReceptacles": ["Fridge"]},
            {"objectId": "Apple|...", "objectType": "Apple",
             "parentReceptacles": ["Fridge"]},
            {"objectId": "Potato|...", "objectType": "Potato",
             "parentReceptacles": ["Fridge"]},
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
