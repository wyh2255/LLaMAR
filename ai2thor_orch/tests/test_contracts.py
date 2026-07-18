"""Tests for data contracts — default values, serialization, immutability."""

from __future__ import annotations

import dataclasses
import json

import pytest

from ai2thor_orch.contracts.types import (
    ActionRequest,
    ActionResult,
    AgentPublicState,
    CoordinatorObservation,
    PublicObservation,
    RoundResult,
    RunStatus,
)


class TestActionRequest:
    def test_create(self):
        req = ActionRequest(agent_idx=0, action="MoveAhead", round_no=1)
        assert req.agent_idx == 0
        assert req.action == "MoveAhead"
        assert req.round_no == 1

    def test_frozen(self):
        req = ActionRequest(agent_idx=0, action="MoveAhead", round_no=1)
        with pytest.raises(AttributeError):
            req.action = "RotateLeft"  # type: ignore[misc]

    def test_serialize(self):
        req = ActionRequest(agent_idx=1, action="Pickup", round_no=2)
        d = dataclasses.asdict(req)
        assert d == {"agent_idx": 1, "action": "Pickup", "round_no": 2}
        # JSON round-trip
        assert json.loads(json.dumps(d)) == d


class TestActionResult:
    def test_defaults(self):
        r = ActionResult(agent_idx=0, observation="done", success=True)
        assert r.position is None
        assert r.inventory == []
        assert r.raw == {}

    def test_full(self):
        r = ActionResult(
            agent_idx=0,
            observation="moved",
            success=True,
            position=(1.0, 0.5, 2.0),
            inventory=["Mug", "Apple"],
            raw={"lastActionSuccess": True},
        )
        assert r.position == (1.0, 0.5, 2.0)
        assert r.inventory == ["Mug", "Apple"]
        assert r.raw == {"lastActionSuccess": True}

    def test_frozen(self):
        r = ActionResult(agent_idx=0, observation="ok", success=True)
        with pytest.raises(AttributeError):
            r.observation = "no"  # type: ignore[misc]


class TestRoundResult:
    def test_defaults(self):
        r = RoundResult(round_no=0)
        assert r.results == []
        assert r.timeout_agents == []
        assert r.finished is False
        assert r.domain_metrics == {}

    def test_with_results(self):
        results = [
            ActionResult(agent_idx=0, observation="a", success=True),
            ActionResult(agent_idx=1, observation="b", success=False),
        ]
        rr = RoundResult(
            round_no=1,
            results=results,
            timeout_agents=[1],
            finished=True,
            domain_metrics={"coverage": 0.5},
        )
        assert rr.round_no == 1
        assert len(rr.results) == 2
        assert rr.timeout_agents == [1]
        assert rr.finished is True
        assert rr.domain_metrics == {"coverage": 0.5}

    def test_json_roundtrip(self):
        rr = RoundResult(
            round_no=1,
            results=[ActionResult(agent_idx=0, observation="ok", success=True)],
        )
        d = dataclasses.asdict(rr)
        assert d["round_no"] == 1
        assert d["results"][0]["agent_idx"] == 0
        json.dumps(d)  # must not raise


class TestPublicObservation:
    def test_defaults(self):
        obs = PublicObservation(agent_idx=0, text="hello")
        assert obs.position is None
        assert obs.rotation is None
        assert obs.inventory == []
        assert obs.visible_objects == []
        assert obs.step == 0

    def test_with_rotation(self):
        obs = PublicObservation(
            agent_idx=1,
            text="You see a Mug",
            position=(0.0, 0.0, 0.0),
            rotation={"x": 0.0, "y": 90.0, "z": 0.0},
            inventory=["Mug"],
            visible_objects=["Mug_1"],
            step=3,
        )
        assert obs.rotation == {"x": 0.0, "y": 90.0, "z": 0.0}
        assert obs.visible_objects == ["Mug_1"]
        assert obs.step == 3


class TestCoordinatorObservation:
    def test_defaults(self):
        co = CoordinatorObservation()
        assert co.round_no == 0
        assert co.agents == []
        assert co.objects == []
        assert co.scene == ""
        assert co.step == 0
        assert co.max_steps == 0

    def test_full(self):
        co = CoordinatorObservation(
            round_no=2,
            agents=[{"name": "Alice", "position": None}],
            objects=[{"objectId": "Mug|...", "alias": "Mug_1"}],
            scene="FloorPlan1",
            step=5,
            max_steps=50,
        )
        assert co.round_no == 2
        assert co.scene == "FloorPlan1"


class TestAgentPublicState:
    def test_defaults(self):
        s = AgentPublicState()
        assert s.agent_idx == 0
        assert s.name == ""
        assert s.position is None
        assert s.rotation is None
        assert s.inventory == []
        assert s.current_task_id == ""
        assert s.status == ""

    def test_full(self):
        s = AgentPublicState(
            agent_idx=0,
            name="Alice",
            position=(1.0, 0.0, 2.0),
            rotation={"x": 0.0, "y": 45.0, "z": 0.0},
            inventory=["Mug"],
            current_task_id="task_001",
            status="active",
        )
        assert s.name == "Alice"
        assert s.status == "active"


class TestRunStatus:
    """RunStatus fields must match implementation plan §3.1 exactly."""

    def test_defaults(self):
        rs = RunStatus()
        assert rs.step == 0
        assert rs.max_steps == 0
        assert rs.finished is False
        assert rs.stopped is False
        assert rs.stop_reason == ""
        assert rs.timeout_agents == []
        assert rs.domain_metrics == {}

    def test_field_names_match_plan_section_3_1(self):
        """Explicitly verify every required field from §3.1."""
        expected = {
            "step",
            "max_steps",
            "finished",
            "stopped",
            "stop_reason",
            "timeout_agents",
            "domain_metrics",
        }
        fields = {f.name for f in dataclasses.fields(RunStatus)}
        assert fields == expected, f"Missing: {expected - fields}, Extra: {fields - expected}"

    def test_full(self):
        rs = RunStatus(
            step=5,
            max_steps=50,
            finished=False,
            stopped=True,
            stop_reason="cancelled",
            timeout_agents=[2],
            domain_metrics={"coverage": 0.8},
        )
        assert rs.step == 5
        assert rs.stop_reason == "cancelled"

    def test_frozen(self):
        rs = RunStatus(step=1, max_steps=10)
        with pytest.raises(AttributeError):
            rs.step = 2  # type: ignore[misc]
