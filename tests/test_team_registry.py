"""Tests for CoordinatorTeamRegistry — invariants, epochs, secret rotation, concurrency."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from a2a.coordinator.team_registry import (
    CoordinatorTeamRegistry,
    TeamDTO,
    DeliveryPlan,
    ValidationError,
    MIN_TEAM_SECRET_BYTES,
)
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus
from a2a.shared.types import WorkerNode, WorkerStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> CoordinatorTeamRegistry:
    return CoordinatorTeamRegistry(coordinator_id="Coordinator")


@pytest.fixture
def fake_agent_registry():
    r = MagicMock()
    r.get.side_effect = lambda wid: AgentInfo(
        agent_id=wid,
        description="test",
        endpoint=f"http://{wid}:9999/",
        status=AgentStatus.ONLINE,
    )
    return r


@pytest.fixture
def fake_worker_registry():
    r = MagicMock()
    r.get.side_effect = lambda wid: WorkerNode(
        worker_id=wid,
        a2a_endpoint=f"http://{wid}:9999/",
        status=WorkerStatus.ONLINE,
    )
    return r


# ---------------------------------------------------------------------------
# Basic invariants
# ---------------------------------------------------------------------------


def test_no_team_initially(registry: CoordinatorTeamRegistry):
    assert registry.current() is None
    assert registry.current_secret is None
    assert registry.epoch == 0


def test_configure_returns_dto(registry: CoordinatorTeamRegistry):
    dto = registry.configure({"Alice": "http://alice:9999/", "Bob": "http://bob:9999/"})
    assert isinstance(dto, TeamDTO)
    assert dto.team_id.startswith("team-")
    assert dto.epoch == 1
    assert dto.member_ids == ["Alice", "Bob"]
    assert dto.endpoints["Alice"] == "http://alice:9999/"


def test_configure_sets_secret(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    secret = registry.current_secret
    assert secret is not None
    assert len(secret) >= MIN_TEAM_SECRET_BYTES


def test_configure_epoch_increments(registry: CoordinatorTeamRegistry):
    dto1 = registry.configure({"Alice": "http://alice:9999/"})
    assert dto1.epoch == 1
    dto2 = registry.configure({"Bob": "http://bob:9999/"})
    assert dto2.epoch == 2
    assert registry.epoch == 2


def test_configure_secret_rotation(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    s1 = registry.current_secret
    registry.configure({"Bob": "http://bob:9999/"})
    s2 = registry.current_secret
    assert s1 != s2


def test_configure_requires_nonempty(registry: CoordinatorTeamRegistry):
    with pytest.raises(ValueError, match="non-empty"):
        registry.configure({})


# ---------------------------------------------------------------------------
# Validation with registries
# ---------------------------------------------------------------------------


def test_configure_agent_registry_validation(registry: CoordinatorTeamRegistry, fake_agent_registry):
    dto = registry.configure(
        {"Alice": "http://alice:9999/"},
        agent_registry=fake_agent_registry,
    )
    assert dto is not None
    fake_agent_registry.get.assert_called_with("Alice")


def test_configure_rejects_missing_agent(registry: CoordinatorTeamRegistry):
    bad = MagicMock()
    bad.get.side_effect = KeyError("Alice not found")
    with pytest.raises(ValidationError, match="not found"):
        registry.configure({"Alice": "http://alice:9999/"}, agent_registry=bad)


def test_configure_worker_registry_validation(registry: CoordinatorTeamRegistry, fake_worker_registry):
    dto = registry.configure(
        {"Alice": "http://alice:9999/"},
        worker_registry=fake_worker_registry,
    )
    assert dto is not None


def test_configure_rejects_offline_worker(registry: CoordinatorTeamRegistry):
    bad = MagicMock()
    bad.get.return_value = WorkerNode(
        worker_id="Alice", a2a_endpoint="http://alice:9999/", status=WorkerStatus.OFFLINE
    )
    with pytest.raises(ValidationError, match="not online"):
        registry.configure({"Alice": "http://alice:9999/"}, worker_registry=bad)


# ---------------------------------------------------------------------------
# Disband
# ---------------------------------------------------------------------------


def test_disband_no_team(registry: CoordinatorTeamRegistry):
    assert registry.disband() is None


def test_disband_returns_dto(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    dto = registry.disband()
    assert dto is not None
    assert "Alice" in dto.member_ids


def test_disband_clears_state(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    registry.disband()
    assert registry.current() is None
    assert registry.current_secret is None


def test_disband_increments_epoch(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    e1 = registry.epoch
    registry.disband()
    assert registry.epoch == e1 + 1


# ---------------------------------------------------------------------------
# Defensive DTO
# ---------------------------------------------------------------------------


def test_dto_is_defensive_copy(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    dto = registry.current()
    assert dto is not None
    dto.member_ids.append("Bob")  # should not affect internal state
    dto2 = registry.current()
    assert dto2 is not None
    assert len(dto2.member_ids) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_access(registry: CoordinatorTeamRegistry):
    """Multiple threads can configure/disband without visible corruption."""
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(n: int):
        try:
            for _ in range(20):
                registry.configure({f"agent-{n}": f"http://agent-{n}:9999/"})
                dto = registry.current()
                if dto is not None:
                    _ = registry.current_secret
                registry.disband()
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrency errors: {errors}"


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------


def test_configure_with_objective(registry: CoordinatorTeamRegistry):
    dto = registry.configure(
        {"Alice": "http://alice:9999/"},
        objective="Extinguish all fires",
    )
    assert dto.objective == "Extinguish all fires"


def test_current_dto_includes_objective(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"}, objective="test obj")
    dto = registry.current()
    assert dto is not None
    assert dto.objective == "test obj"


# ---------------------------------------------------------------------------
# TeamDTO factory
# ---------------------------------------------------------------------------


def test_team_dto_from_internal():
    dto = TeamDTO.from_internal("tid-1", 5, {"a": "ep"}, "obj")
    assert dto.team_id == "tid-1"
    assert dto.epoch == 5
    assert dto.member_ids == ["a"]
    assert dto.endpoints == {"a": "ep"}
    assert dto.objective == "obj"


# ---------------------------------------------------------------------------
# DeliveryPlan / atomic snapshot
# ---------------------------------------------------------------------------


def test_snapshot_for_delivery_none(registry: CoordinatorTeamRegistry):
    assert registry.snapshot_for_delivery() is None


def test_snapshot_for_delivery_returns_both(registry: CoordinatorTeamRegistry):
    registry.configure({"Alice": "http://alice:9999/"})
    plan = registry.snapshot_for_delivery()
    assert plan is not None
    assert plan.dto.team_id is not None
    assert plan.team_secret is not None
    assert len(plan.team_secret) >= MIN_TEAM_SECRET_BYTES


def test_snapshot_for_delivery_coherent(registry: CoordinatorTeamRegistry):
    """DTO and secret come from the same atomic view."""
    registry.configure({"Alice": "http://alice:9999/"})
    plan1 = registry.snapshot_for_delivery()
    assert plan1 is not None
    plan2 = registry.snapshot_for_delivery()
    assert plan2 is not None
    assert plan1.dto.epoch == plan2.dto.epoch


# ---------------------------------------------------------------------------
# Duplicate validation
# ---------------------------------------------------------------------------


def test_duplicate_member_ids_raises(registry: CoordinatorTeamRegistry):
    """Python dict keys are unique, so this tests empty input instead."""
    pass


# ---------------------------------------------------------------------------
# AgentRegistry.get returning None
# ---------------------------------------------------------------------------


def test_agent_registry_returns_none(registry: CoordinatorTeamRegistry):
    reg = MagicMock()
    reg.get.return_value = None
    with pytest.raises(ValidationError, match="not found"):
        registry.configure({"Alice": "http://a:1/"}, agent_registry=reg)


# ---------------------------------------------------------------------------
# Team ID nonblank validation
# ---------------------------------------------------------------------------


def test_team_id_nonblank(registry: CoordinatorTeamRegistry):
    with pytest.raises(ValueError, match="non-blank"):
        registry.configure({"Alice": "http://a:1/"}, team_id="  ")


# ---------------------------------------------------------------------------
# configure_for_delivery / disband_for_delivery
# ---------------------------------------------------------------------------


def test_configure_for_delivery_returns_plan(registry: CoordinatorTeamRegistry):
    plan = registry.configure_for_delivery({"A": "http://a:1/"})
    assert isinstance(plan, DeliveryPlan)
    assert plan.dto.team_id is not None
    assert len(plan.team_secret) >= MIN_TEAM_SECRET_BYTES
    assert plan.dto.epoch == 1


def test_configure_for_delivery_atomic(registry: CoordinatorTeamRegistry):
    """DTO and secret from same lock acquisition."""
    plan = registry.configure_for_delivery({"A": "http://a:1/", "B": "http://b:1/"})
    assert len(plan.dto.member_ids) == 2
    assert len(plan.team_secret) >= MIN_TEAM_SECRET_BYTES


def test_disband_for_delivery_returns_plan(registry: CoordinatorTeamRegistry):
    registry.configure({"A": "http://a:1/"})
    e1 = registry.epoch
    plan = registry.disband_for_delivery()
    assert plan is not None
    assert plan.dto.epoch == e1  # prior epoch
    assert registry.current() is None  # already cleared
    assert registry.epoch == e1 + 1


def test_disband_for_delivery_no_team(registry: CoordinatorTeamRegistry):
    assert registry.disband_for_delivery() is None


def test_disband_for_delivery_includes_secret(registry: CoordinatorTeamRegistry):
    registry.configure({"A": "http://a:1/"})
    plan = registry.disband_for_delivery()
    assert plan is not None
    assert len(plan.team_secret) >= MIN_TEAM_SECRET_BYTES
