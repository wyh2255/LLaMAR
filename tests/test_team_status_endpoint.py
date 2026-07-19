"""Tests for Phase 4 /team-status endpoint on coordinator server."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_semantic_map(
    agents: list[dict] | None = None,
    current_step: int = 0,
) -> MagicMock:
    """Create a mock SemanticMapStore with snapshot returning the given agents."""
    mm = MagicMock()
    mm.snapshot.return_value = {
        "agents": agents or [],
        "step_budget": {"current_step": current_step},
    }
    return mm


def _make_mock_barrier(
    agents: list[dict] | None = None,
    has_env_snapshot: bool = True,
) -> MagicMock:
    """Create a mock SARBarrier."""
    mm = MagicMock()
    if has_env_snapshot:
        mm.get_env_snapshot.return_value = {
            "agents": agents or [],
        }
    else:
        # Simulate barrier without get_env_snapshot
        del mm.get_env_snapshot
        mm._step_counter = 0
    return mm


def _build_team_status_app(
    semantic_map: MagicMock | None = None,
    barrier: MagicMock | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the /team-status endpoint logic."""
    app = FastAPI()

    @app.get("/team-status")
    async def team_status(agent_id: str):
        if semantic_map is None or barrier is None:
            return {"teammates": [], "current_step": 0}

        env_snap = (
            barrier.get_env_snapshot()
            if hasattr(barrier, "get_env_snapshot")
            else {}
        )
        live = {
            a.get("name"): a
            for a in env_snap.get("agents", [])
            if isinstance(a, dict)
        }

        map_snap = (
            semantic_map.snapshot()
            if hasattr(semantic_map, "snapshot")
            else {}
        )
        teammates = []
        for agent in map_snap.get("agents", []):
            aid = agent["agent_id"]
            if aid == agent_id:
                continue
            live_data = live.get(aid, {})
            raw_inv = (
                live_data.get("inventory") or agent.get("inventory") or {}
            )
            if isinstance(raw_inv, dict):
                inv = [k for k, v in raw_inv.items() if v]
            else:
                inv = list(raw_inv) if raw_inv else []
            teammates.append(
                {
                    "agent_id": aid,
                    "position": live_data.get("position")
                    or agent.get("last_position"),
                    "inventory": inv,
                    "current_task_id": agent.get("current_task_id", ""),
                    "task_state": agent.get("task_state", "UNKNOWN"),
                    "is_carrying_person": bool(raw_inv.get("person"))
                    if isinstance(raw_inv, dict)
                    else "Person" in inv,
                }
            )
        return {
            "teammates": teammates,
            "current_step": map_snap.get("step_budget", {}).get(
                "current_step", 0
            ),
        }

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTeamStatusEndpoint:
    def test_no_semantic_map_or_barrier(self):
        """Without semantic map/barrier, returns empty teammates."""
        app = _build_team_status_app(semantic_map=None, barrier=None)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"teammates": [], "current_step": 0}

    def test_no_agents_in_semantic_map(self):
        """No agents in semantic map → empty list."""
        sm = _make_mock_semantic_map(agents=[])
        barrier = _make_mock_barrier()
        app = _build_team_status_app(semantic_map=sm, barrier=barrier)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        assert resp.status_code == 200
        assert resp.json() == {"teammates": [], "current_step": 0}

    def test_self_excluded(self):
        """Requesting agent should not appear in teammates list."""
        sm = _make_mock_semantic_map(
            agents=[
                {
                    "agent_id": "worker-alpha",
                    "inventory": {},
                    "last_position": None,
                    "current_task_id": "",
                    "task_state": "UNKNOWN",
                },
                {
                    "agent_id": "worker-beta",
                    "inventory": {"Water": 1},
                    "last_position": [3, 4, 0],
                    "current_task_id": "explore",
                    "task_state": "RUNNING",
                },
            ]
        )
        barrier = _make_mock_barrier()
        app = _build_team_status_app(semantic_map=sm, barrier=barrier)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        data = resp.json()
        assert len(data["teammates"]) == 1
        assert data["teammates"][0]["agent_id"] == "worker-beta"

    def test_multiple_teammates(self):
        """Multiple teammates returned correctly."""
        sm = _make_mock_semantic_map(
            agents=[
                {"agent_id": "worker-alpha", "inventory": {}},
                {
                    "agent_id": "worker-beta",
                    "inventory": {"Water": 1},
                    "last_position": [1, 2, 0],
                    "current_task_id": "task-1",
                    "task_state": "RUNNING",
                },
                {
                    "agent_id": "worker-gamma",
                    "inventory": {"Sand": 2, "person": "Person_1"},
                    "last_position": [7, 8, 0],
                    "current_task_id": "task-2",
                    "task_state": "BLOCKED",
                },
            ]
        )
        barrier = _make_mock_barrier()
        app = _build_team_status_app(semantic_map=sm, barrier=barrier)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        data = resp.json()
        assert len(data["teammates"]) == 2

        beta = [t for t in data["teammates"] if t["agent_id"] == "worker-beta"][0]
        assert beta["inventory"] == ["Water"]
        assert not beta["is_carrying_person"]

        gamma = [t for t in data["teammates"] if t["agent_id"] == "worker-gamma"][0]
        assert "person" in gamma["inventory"] or "Person" in gamma["inventory"]
        assert gamma["is_carrying_person"]

    def test_current_step_from_semantic_map(self):
        """current_step extracted from step_budget."""
        sm = _make_mock_semantic_map(agents=[], current_step=42)
        barrier = _make_mock_barrier()
        app = _build_team_status_app(semantic_map=sm, barrier=barrier)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        data = resp.json()
        assert data["current_step"] == 42

    def test_live_data_overrides_semantic_map(self):
        """Live barrier data takes priority over semantic map data."""
        sm = _make_mock_semantic_map(
            agents=[
                {
                    "agent_id": "worker-beta",
                    "inventory": {"Water": 1},
                    "last_position": [1, 2, 0],
                    "current_task_id": "old-task",
                    "task_state": "UNKNOWN",
                },
            ]
        )
        barrier = _make_mock_barrier(
            agents=[
                {
                    "name": "worker-beta",
                    "position": [9, 9, 0],
                    "inventory": {"Sand": 3},
                },
            ],
            has_env_snapshot=True,
        )
        app = _build_team_status_app(semantic_map=sm, barrier=barrier)
        client = TestClient(app)
        resp = client.get("/team-status", params={"agent_id": "worker-alpha"})
        data = resp.json()
        beta = data["teammates"][0]
        # Live position should override
        assert beta["position"] == [9, 9, 0]
        # Live inventory should override
        assert beta["inventory"] == ["Sand"]
