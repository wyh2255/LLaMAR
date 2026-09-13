"""Phase 3 exclusive TeamPartition tests.

Covers:
  1. Singleton coverage of online workers (I3)
  2. Two disjoint collaborative teams simultaneously — I4 regression
  3. Rejecting overlap including INSTALLED collaborative — I4 regression
  4. Fence lifecycle: claiming, installing, degrading, reconciling
  5. Monotonic global epoch across persistence/restart (I10)
  6. Full ACK delivery → INSTALLED
  7. ACK failure → COMPENSATING → COMPENSATED
  8. ACK failure + compensation failure → DEGRADED
  9. Durable release saga (prepare + ACK + complete)
 10. Service-level release_node_team async success/compensation/degraded
 11. Recovery / reconcile after restart
 12. Server wiring: WS_REGISTER creates singleton
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from a2a.coordinator.team_partition_service import (
    TeamPartitionRegistry,
    TeamPartitionService,
    TeamAssignment,
    PartitionTransition,
    TransitionStatus,
    ParticipantBusyError,
    TransitionError,
    singleton_team_id,
    _transition_holds_fence,
    _transition_releases_fence,
    _transition_to_dict,
    _dict_to_transition,
)
from a2a.coordinator.server import CoordinatorServer


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def registry() -> TeamPartitionRegistry:
    return TeamPartitionRegistry(coordinator_id="Coordinator")


@pytest.fixture
def service(registry: TeamPartitionRegistry) -> TeamPartitionService:
    return TeamPartitionService(registry=registry)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "team_partition_control.json"


# =========================================================================
# 1. Singleton coverage (I3)
# =========================================================================


class TestSingletonCoverage:
    def test_no_workers_initially(self, registry: TeamPartitionRegistry):
        snap = registry.snapshot()
        assert snap["workers"] == 0
        assert snap["global_epoch"] == 0

    def test_ensure_singletons_creates_solo_teams(
        self, registry: TeamPartitionRegistry
    ):
        created = registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        assert len(created) == 3

        for worker_id in ["Alice", "Bob", "Charlie"]:
            a = registry.get_assignment(worker_id)
            assert a is not None
            assert a.team_id == singleton_team_id(worker_id)
            assert a.is_singleton
            assert a.member_ids == [worker_id]

    def test_ensure_singletons_idempotent(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        e1 = registry.global_epoch
        created = registry.ensure_singletons(["Alice", "Bob"])
        assert len(created) == 0
        assert registry.global_epoch == e1

    def test_ensure_singletons_partial_new(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        e1 = registry.global_epoch
        created = registry.ensure_singletons(["Alice", "Bob"])
        assert len(created) == 1
        assert created[0].team_id == singleton_team_id("Bob")
        assert registry.global_epoch > e1

    def test_every_online_worker_has_exactly_one_team(
        self, registry: TeamPartitionRegistry
    ):
        online = ["Alice", "Bob", "Charlie", "David"]
        registry.ensure_singletons(online)

        rosters = registry.collect_team_rosters()
        team_ids = [r["team_id"] for r in rosters]
        assert len(team_ids) == len(online)
        for wid in online:
            assert singleton_team_id(wid) in team_ids

        for wid in online:
            a = registry.get_assignment(wid)
            assert a is not None
            assert len(a.member_ids) == 1
            assert a.member_ids[0] == wid


# =========================================================================
# 2. Two disjoint collaborative teams simultaneously — I4 regression
# =========================================================================


class TestDisjointCollaborativeTeams:
    def test_two_disjoint_teams_coexist(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie", "David"])

        t1 = registry.prepare_activation(
            node_id="rescue-alpha",
            members=["Alice", "Bob"],
            objective="Extinguish north fire",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t1.transition_id, w, success=True)
        installed_1 = registry.mark_installed(t1.transition_id)

        t2 = registry.prepare_activation(
            node_id="evacuate-beta",
            members=["Charlie", "David"],
            objective="Evacuate south building",
            context_id="ctx-2",
        )
        registry.mark_installing(t2.transition_id)
        for w in ["Charlie", "David"]:
            registry.record_ack(t2.transition_id, w, success=True)
        installed_2 = registry.mark_installed(t2.transition_id)

        assert installed_1.status == TransitionStatus.INSTALLED
        assert installed_2.status == TransitionStatus.INSTALLED

        team_1 = registry.get_assignment("Alice")
        team_2 = registry.get_assignment("Charlie")
        assert team_1 is not None and team_2 is not None
        assert team_1.team_id.startswith("team:rescue-alpha")
        assert team_2.team_id.startswith("team:evacuate-beta")
        assert team_1.team_id != team_2.team_id

    def test_rosters_reflect_collaborative_teams(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])

        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="Task A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t1.transition_id, w, success=True)
        registry.mark_installed(t1.transition_id)

        rosters = registry.collect_team_rosters()
        collab = [r for r in rosters if not r["is_singleton"]]
        singletons = [r for r in rosters if r["is_singleton"]]

        assert len(collab) == 1
        assert collab[0]["members"] == ["Alice", "Bob"] or collab[0]["members"] == [
            "Bob",
            "Alice",
        ]
        assert len(singletons) == 1
        assert singletons[0]["team_id"] == singleton_team_id("Charlie")


# =========================================================================
# 3. I4: INSTALLED collaborative retains exclusive claim; overlap rejected
# =========================================================================


class TestI4InstalledOverlapRejection:
    """I4 regression: an INSTALLED collaborative team must retain its claim.

    A Worker in an INSTALLED [Alice,Bob] team must NOT be claimable by
    [Alice,Charlie] until release clears the lease.  Disjoint teams
    ([Charlie,David]) remain valid.
    """

    def test_installed_collaborative_blocks_overlap(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        # INSTALLED collaborative [Alice,Bob] must still hold fence on Alice
        busy = registry.check_participants_free(["Alice"])
        assert "Alice" in busy, "INSTALLED collaborative must retain fence (I4)"

        # Overlapping activation must be rejected
        with pytest.raises(ParticipantBusyError, match="Alice"):
            registry.prepare_activation(
                node_id="team-b",
                members=["Alice", "Charlie"],
                objective="B",
                context_id="ctx-2",
            )

    def test_installed_collaborative_allows_disjoint(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob", "Charlie", "David"])
        t = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        # Disjoint [Charlie, David] must succeed
        t2 = registry.prepare_activation(
            node_id="team-c",
            members=["Charlie", "David"],
            objective="C",
            context_id="ctx-2",
        )
        assert t2 is not None

    def test_installed_collaborative_has_rosters(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        roster = registry.collect_team_rosters()
        collab = [r for r in roster if not r["is_singleton"]]
        assert len(collab) == 1
        assert "Alice" in collab[0]["members"]
        assert "Bob" in collab[0]["members"]

    def test_release_clears_installed_fence(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])

        t = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        assert "Alice" in registry.check_participants_free(["Alice"])

        # Release clears the fence
        release = registry.prepare_release("ctx-1")
        assert release is not None
        registry.mark_installing(release.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(release.transition_id, w, success=True)
        registry.complete_release(release.transition_id)

        assert "Alice" not in registry.check_participants_free(["Alice"])

        # After release, Alice can be reclaimed
        t2 = registry.prepare_activation(
            node_id="team-d",
            members=["Alice", "Charlie"],
            objective="D",
            context_id="ctx-3",
        )
        assert t2 is not None
        assert "Alice" in t2.affected_workers


# =========================================================================
# 4. Fence lifecycle helpers
# =========================================================================


class TestFenceHelpers:
    def test_transition_holds_fence(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        assert _transition_holds_fence(t)

        registry.mark_installing(t.transition_id)
        t2 = registry.get_transition(t.transition_id)
        assert t2 is not None and _transition_holds_fence(t2)

        registry.record_ack(t.transition_id, "Alice", success=True)
        installed = registry.mark_installed(t.transition_id)
        # Collaborative INSTALLED: holds fence
        assert _transition_holds_fence(installed)
        assert not _transition_releases_fence(installed)

    def test_singleton_installed_releases_fence(self):
        t_solo = PartitionTransition(
            transition_id="t",
            context_id="ctx",
            source_node_id="",
            before={},
            after={},
            affected_workers=["Alice"],
            epoch=1,
            status=TransitionStatus.INSTALLED,
            team_id=singleton_team_id("Alice"),
        )
        assert _transition_releases_fence(t_solo)
        assert not _transition_holds_fence(t_solo)

        # Without team_id (no team), treat as singleton release
        t_no_team = PartitionTransition(
            transition_id="t2",
            context_id="ctx",
            source_node_id="",
            before={},
            after={},
            affected_workers=["Alice"],
            epoch=2,
            status=TransitionStatus.INSTALLED,
        )
        assert _transition_releases_fence(t_no_team)
        assert not _transition_holds_fence(t_no_team)


# =========================================================================
# More overlap tests
# =========================================================================


class TestOverlapRejection:
    def test_cannot_claim_busy_worker(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        with pytest.raises(ParticipantBusyError, match="Alice"):
            registry.prepare_activation(
                node_id="team-b",
                members=["Alice", "Charlie"],
                objective="B",
                context_id="ctx-2",
            )

    def test_cannot_overlap_after_installing(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        with pytest.raises(ParticipantBusyError, match="Alice"):
            registry.prepare_activation(
                node_id="team-b",
                members=["Alice"],
                objective="B",
                context_id="ctx-2",
            )

    def test_cannot_overlap_during_compensating(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        registry.record_ack(t1.transition_id, "Alice", success=True)
        registry.record_ack(t1.transition_id, "Bob", success=False)
        registry.mark_compensating(t1.transition_id)

        with pytest.raises(ParticipantBusyError, match="Alice"):
            registry.prepare_activation(
                node_id="team-b",
                members=["Alice"],
                objective="B",
                context_id="ctx-2",
            )

    def test_can_reclaim_after_compensated(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        registry.record_ack(t1.transition_id, "Alice", success=True)
        registry.record_ack(t1.transition_id, "Bob", success=False)
        registry.mark_compensating(t1.transition_id)
        registry.record_ack(t1.transition_id, "Alice", success=True)
        registry.record_ack(t1.transition_id, "Bob", success=True)
        registry.mark_compensated(t1.transition_id)

        t2 = registry.prepare_activation(
            node_id="team-c",
            members=["Alice"],
            objective="C",
            context_id="ctx-2",
        )
        assert t2 is not None

    def test_degraded_blocks_new_claim(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        registry.mark_degraded(t1.transition_id)

        busy = registry.check_participants_free(["Alice"])
        assert "Alice" in busy, "DEGRADED must retain fence"


# =========================================================================
# 5. Monotonic global epoch across persistence/restart (I10)
# =========================================================================


class TestEpochPersistence:
    def test_epoch_monotonic_after_restart(
        self, registry: TeamPartitionRegistry, state_path: Path
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t1.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t1.transition_id, w, success=True)
        registry.mark_installed(t1.transition_id)

        e_before = registry.global_epoch
        registry.persist(state_path)

        r2 = TeamPartitionRegistry(coordinator_id="Coordinator")
        r2.load(state_path)
        assert r2.global_epoch == e_before

        r2.reconcile_after_recovery()
        assert r2.global_epoch >= e_before

    def test_epoch_monotonic_singleton(
        self, registry: TeamPartitionRegistry, state_path: Path
    ):
        registry.ensure_singletons(["Alice"])
        e1 = registry.global_epoch
        registry.persist(state_path)

        r2 = TeamPartitionRegistry(coordinator_id="Coordinator")
        r2.load(state_path)
        r2.ensure_singletons(["Alice"])
        assert r2.global_epoch == e1

        r2.ensure_singletons(["Bob"])
        assert r2.global_epoch > e1

    def test_epoch_never_resets(
        self, registry: TeamPartitionRegistry, state_path: Path
    ):
        registry.ensure_singletons(["Alice"])
        registry.advance_epoch()
        registry.advance_epoch()
        e_before = registry.global_epoch
        assert e_before >= 2

        registry.persist(state_path)

        r2 = TeamPartitionRegistry(coordinator_id="Coordinator")
        r2.load(state_path)
        assert r2.global_epoch == e_before
        assert r2.global_epoch > 0

        r2.reconcile_after_recovery()
        assert r2.global_epoch >= e_before

    def test_persistence_includes_transitions(
        self, registry: TeamPartitionRegistry, state_path: Path
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t1 = registry.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        registry.persist(state_path)

        r2 = TeamPartitionRegistry(coordinator_id="Coordinator")
        r2.load(state_path)
        loaded_t = r2.get_transition(t1.transition_id)
        assert loaded_t is not None
        assert loaded_t.transition_id == t1.transition_id

    def test_file_permissions(self, registry: TeamPartitionRegistry, state_path: Path):
        registry.ensure_singletons(["Alice"])
        registry.persist(state_path)
        mode = stat.S_IMODE(os.stat(str(state_path)).st_mode)
        assert mode & 0o077 == 0


# =========================================================================
# 6. Full ACK delivery
# =========================================================================


class TestAckDelivery:
    def test_full_ack_leads_to_installed(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)

        all_ok, failed, pending = registry.check_acks(t.transition_id)
        assert all_ok and failed == [] and pending == []

        installed = registry.mark_installed(t.transition_id)
        assert installed.status == TransitionStatus.INSTALLED

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None
            assert not a.is_singleton
            assert a.team_id == f"team:rescue:r{t.epoch}"

    def test_ack_partial_does_not_install(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob", "Charlie"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=True)

        all_ok, failed, pending = registry.check_acks(t.transition_id)
        assert not all_ok
        assert pending == ["Charlie"]

    def test_ack_rejected_leads_to_failure(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=False)

        all_ok, failed, pending = registry.check_acks(t.transition_id)
        assert not all_ok
        assert "Bob" in failed

    def test_ack_timeout_detected(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", timeout=True)

        all_ok, failed, pending = registry.check_acks(t.transition_id)
        assert not all_ok
        assert "Bob" in failed


# =========================================================================
# 7. Compensation
# =========================================================================


class TestCompensation:
    def test_compensating_after_failure(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=False)
        registry.mark_compensating(t.transition_id)

        comp_t = registry.get_transition(t.transition_id)
        assert comp_t is not None and comp_t.status == TransitionStatus.COMPENSATING

    def test_compensated_restores_singletons(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=False)
        registry.mark_compensating(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=True)

        compensated = registry.mark_compensated(t.transition_id)
        assert compensated.status == TransitionStatus.COMPENSATED

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None and a.is_singleton and a.member_ids == [wid]

    def test_compensation_fails_to_degraded(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=False)
        registry.mark_compensating(t.transition_id)
        registry.record_ack(t.transition_id, "Bob", success=False)

        degraded = registry.mark_degraded(t.transition_id)
        assert degraded.status == TransitionStatus.DEGRADED


# =========================================================================
# 8. DEGRADED path
# =========================================================================


class TestDegraded:
    def test_installing_to_degraded(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        degraded = registry.mark_degraded(t.transition_id)
        assert degraded.status == TransitionStatus.DEGRADED

    def test_compensating_to_degraded(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.mark_compensating(t.transition_id)
        degraded = registry.mark_degraded(t.transition_id)
        assert degraded.status == TransitionStatus.DEGRADED

    def test_degraded_blocks_claim(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.mark_degraded(t.transition_id)

        busy = registry.check_participants_free(["Alice"])
        assert "Alice" in busy

    def test_degraded_fences_released_by_reconcile(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.mark_degraded(t.transition_id)

        reconciled = registry.reconcile_after_recovery()
        assert len(reconciled) == 1

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None and a.is_singleton

        busy = registry.check_participants_free(["Alice"])
        assert "Alice" not in busy


# =========================================================================
# 9. Durable release saga
# =========================================================================


class TestDurableRelease:
    def test_prepare_release_no_workers(self, registry: TeamPartitionRegistry):
        release = registry.prepare_release("nonexistent")
        assert release is None

    def test_prepare_release_creates_transition_with_fences(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-mission",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        release = registry.prepare_release("ctx-mission")
        assert release is not None
        assert release.status == TransitionStatus.PREPARING
        assert len(release.affected_workers) == 2

        # Fences set for release
        assert "Alice" in registry.check_participants_free(["Alice"])

        # All workers share one epoch
        ep = release.epoch
        for wid in release.affected_workers:
            assert release.after[wid].epoch == ep

    def test_prepare_release_same_epoch_all_workers(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob", "Charlie"],
            objective="Rescue",
            context_id="ctx-mission",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob", "Charlie"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        release = registry.prepare_release("ctx-mission")
        assert release is not None
        epochs = {v.epoch for v in release.after.values()}
        assert len(epochs) == 1

    def test_complete_release_clears_fences(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-mission",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        release = registry.prepare_release("ctx-mission")
        assert release is not None
        registry.mark_installing(release.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(release.transition_id, w, success=True)
        done = registry.complete_release(release.transition_id)

        assert done.status == TransitionStatus.INSTALLED
        assert "Alice" not in registry.check_participants_free(["Alice"])

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None and a.is_singleton

    def test_release_epoch_larger_than_collaborative_epoch(
        self, registry: TeamPartitionRegistry
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-mission",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        installed = registry.mark_installed(t.transition_id)

        release = registry.prepare_release("ctx-mission")
        assert release is not None
        assert release.epoch > installed.epoch


# =========================================================================
# 10. Service-level release_node_team async tests
# =========================================================================


class TestServiceReleaseNodeTeam:
    @pytest.mark.asyncio
    async def test_release_success(self, service: TeamPartitionService):
        service.ensure_singletons(["Alice", "Bob"])
        t = service.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        # Manually ACK the activation (Phase 4 does this)
        service._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            service._registry.record_ack(t.transition_id, w, success=True)
        service._registry.mark_installed(t.transition_id)

        # Inject a delivery adapter that says all ACKs succeeded
        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        service.set_delivery_adapter(all_ack)

        result = await service.release_node_team("ctx-1")
        assert result is not None
        assert result.status == TransitionStatus.INSTALLED
        assert "Alice" not in service._registry.check_participants_free(["Alice"])
        for wid in ["Alice", "Bob"]:
            a = service.get_assignment(wid)
            assert a is not None and a.is_singleton

    @pytest.mark.asyncio
    async def test_release_no_workers(self, service: TeamPartitionService):
        result = await service.release_node_team("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_release_partial_failure_compensation_degraded(
        self, service: TeamPartitionService
    ):
        """Without delivery adapter, ACKs all timeout -> COMPENSATING -> DEGRADED."""
        service.ensure_singletons(["Alice", "Bob"])
        t = service.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        service._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            service._registry.record_ack(t.transition_id, w, success=True)
        service._registry.mark_installed(t.transition_id)

        # No delivery adapter -> timeouts -> DEGRADED
        result = await service.release_node_team("ctx-1")
        assert result is not None
        assert result.status == TransitionStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_release_compensation_success(self, service: TeamPartitionService):
        """Partial ACK failure on release leads to compensation that succeeds."""
        service.ensure_singletons(["Alice", "Bob"])
        t = service.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        service._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            service._registry.record_ack(t.transition_id, w, success=True)
        service._registry.mark_installed(t.transition_id)

        call_count = 0

        async def first_fail_then_comp(tr: PartitionTransition) -> dict[str, bool]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call (release delivery): one fails
                return {"Alice": True, "Bob": False}
            # Second call (compensation delivery): all succeed
            return {"Alice": True, "Bob": True}

        service.set_delivery_adapter(first_fail_then_comp)
        result = await service.release_node_team("ctx-1")
        assert result is not None
        assert result.status == TransitionStatus.COMPENSATED

        # After compensated release, workers back to collaborative (before state)
        # Wait — compensation reverts to the before state, which was the collaborative team.
        # The compensation sends TEAM_UPDATE restoring the original collaborative team.
        # Actually, for release: before=collaborative, after=singleton.
        # Compensation reverts from after to before: so back to collaborative.
        # But the transition is COMPENSATED, which releases fences.
        # So workers should be in the before (collaborative) state.
        for wid in ["Alice", "Bob"]:
            a = service.get_assignment(wid)
            assert a is not None
            assert not a.is_singleton  # Back to collaborative team

    @pytest.mark.asyncio
    async def test_release_constant_epoch(self, service: TeamPartitionService):
        """All workers in a release share the same epoch."""
        service.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = service.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob", "Charlie"],
            objective="Rescue",
            context_id="ctx-1",
        )
        service._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob", "Charlie"]:
            service._registry.record_ack(t.transition_id, w, success=True)
        service._registry.mark_installed(t.transition_id)

        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        service.set_delivery_adapter(all_ack)
        result = await service.release_node_team("ctx-1")
        assert result is not None
        epochs = {v.epoch for v in result.after.values()}
        assert len(epochs) == 1


# =========================================================================
# 11. Recovery / reconcile
# =========================================================================


class TestRecovery:
    def test_reconcile_non_terminal_transitions(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)

        active_before = registry.get_active_transitions()
        assert len(active_before) == 1

        reconciled = registry.reconcile_after_recovery()
        assert len(reconciled) >= 1

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None and a.is_singleton

        assert len(registry.get_active_transitions()) == 0

    def test_reconcile_clears_installed_fences(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        assert "Alice" in registry.check_participants_free(["Alice"])
        reconciled = registry.reconcile_after_recovery()
        assert len(reconciled) == 1
        assert "Alice" not in registry.check_participants_free(["Alice"])

    def test_reconcile_persistence_roundtrip(
        self, registry: TeamPartitionRegistry, state_path: Path
    ):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.persist(state_path)

        r2 = TeamPartitionRegistry(coordinator_id="Coordinator")
        r2.load(state_path)
        assert len(r2.get_active_transitions()) == 1

        reconciled = r2.reconcile_after_recovery()
        assert len(reconciled) >= 1

        for wid in ["Alice", "Bob"]:
            a = r2.get_assignment(wid)
            assert a is not None and a.is_singleton and a.epoch > t.epoch

        snap = r2.snapshot()
        assert snap["active_fences"] == {}

    def test_reconcile_fresh_start(self, registry: TeamPartitionRegistry):
        assert registry.reconcile_after_recovery() == []

    def test_reconcile_degraded_transitions(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.mark_degraded(t.transition_id)

        busy = registry.check_participants_free(["Alice"])
        assert "Alice" in busy

        reconciled = registry.reconcile_after_recovery()
        assert len(reconciled) == 1

        for wid in ["Alice", "Bob"]:
            a = registry.get_assignment(wid)
            assert a is not None and a.is_singleton

        assert registry.snapshot()["active_fences"] == {}

    def test_reconcile_also_handles_installed_fences(
        self, registry: TeamPartitionRegistry
    ):
        """Reconcile must clear INSTALLED fences even without crash recovery."""
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        registry.mark_installed(t.transition_id)

        assert "Alice" in registry.check_participants_free(["Alice"])
        reconciled = registry.reconcile_after_recovery()
        assert len(reconciled) >= 1
        assert "Alice" not in registry.check_participants_free(["Alice"])


# =========================================================================
# 12. Server wiring: WS_REGISTER creates singleton
# =========================================================================


class TestServerSingletonWiring:
    """Prove that a WS_REGISTER results in a singleton TeamAssignment."""

    @pytest.mark.asyncio
    async def test_register_via_handler_creates_singleton(self):
        """Exercise _handle_worker_message with a WS_REGISTER message.

        The handler must call ensure_singletons so the worker has a
        solo:<worker_id> team partition.  We mock _fetch_agent_card to
        avoid real HTTP.
        """
        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            log_dir="/tmp",
            memory_read_mode="legacy",
        )
        svc = server._team_partition_service
        assert svc.partition_snapshot()["workers"] == 0

        async def fake_fetch(url: str, **kw) -> dict | None:
            return None  # No AgentCard — test exercises the else branch

        server._fetch_agent_card = fake_fetch

        msg = {
            "type": "register",
            "payload": {"a2a_endpoint": "http://127.0.0.1:9999/"},
        }
        await server._handle_worker_message("Alice", msg)

        assert svc.partition_snapshot()["workers"] == 1
        a = svc.get_assignment("Alice")
        assert a is not None
        assert a.is_singleton
        assert a.team_id == singleton_team_id("Alice")

    @pytest.mark.asyncio
    async def test_server_owns_default_partition_service(self):
        """Server creates a default TeamPartitionService even without injection."""
        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            log_dir="/tmp",
            memory_read_mode="legacy",
        )
        assert server._team_partition_service is not None

    @pytest.mark.asyncio
    async def test_team_partition_service_injection_replaces_default(self):
        """set_team_partition_service replaces the default service."""
        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            log_dir="/tmp",
            memory_read_mode="legacy",
        )
        svc = TeamPartitionService()
        server.set_team_partition_service(svc)
        assert server._team_partition_service is svc

    @pytest.mark.asyncio
    async def test_default_service_accepts_registration(self):
        """The default TeamPartitionService must accept ensure_singletons.

        Without calling set_team_partition_service, the server's own
        default must still create solo partitions.
        """
        server = CoordinatorServer(
            host="127.0.0.1",
            port=0,
            a2a_port=0,
            log_dir="/tmp",
            memory_read_mode="legacy",
        )

        async def fake_fetch(url: str, **kw) -> dict | None:
            return None

        server._fetch_agent_card = fake_fetch
        msg = {
            "type": "register",
            "payload": {"a2a_endpoint": "http://127.0.0.1:9999/"},
        }
        await server._handle_worker_message("Bob", msg)

        a = server._team_partition_service.get_assignment("Bob")
        assert a is not None
        assert a.is_singleton
        assert a.team_id == singleton_team_id("Bob")


# =========================================================================
# Service-level tests
# =========================================================================


class TestTeamPartitionService:
    def test_service_ensures_singletons(self, service: TeamPartitionService):
        service.ensure_singletons(["Alice", "Bob"])
        snap = service.partition_snapshot()
        assert snap["workers"] == 2

    def test_service_prepare_activation(self, service: TeamPartitionService):
        service.ensure_singletons(["Alice", "Bob"])
        t = service.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        assert t is not None and t.status == TransitionStatus.PREPARING

    def test_service_collect_rosters(self, service: TeamPartitionService):
        service.ensure_singletons(["Alice", "Bob", "Charlie"])
        rosters = service.collect_team_rosters()
        assert len(rosters) == 3


# =========================================================================
# Transition state machine invariants
# =========================================================================


class TestTransitionStateMachine:
    def test_cannot_skip_preparing_to_installed(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        with pytest.raises(
            TransitionError, match="Cannot mark INSTALLED from PREPARING"
        ):
            registry.mark_installed(t.transition_id)

    def test_cannot_compensate_from_preparing(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        with pytest.raises(
            TransitionError, match="Cannot mark COMPENSATING from PREPARING"
        ):
            registry.mark_compensating(t.transition_id)

    def test_cannot_degrade_from_preparing(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        with pytest.raises(
            TransitionError, match="Cannot mark DEGRADED from PREPARING"
        ):
            registry.mark_degraded(t.transition_id)

    def test_cannot_mark_installing_twice(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        with pytest.raises(
            TransitionError, match="Cannot mark INSTALLING from INSTALLING"
        ):
            registry.mark_installing(t.transition_id)

    def test_cannot_mark_unknown_transition(self, registry: TeamPartitionRegistry):
        with pytest.raises(TransitionError, match="Unknown transition"):
            registry.mark_installing("nonexistent")

    def test_transition_status_enum_values(self):
        expected = {
            "PREPARING",
            "INSTALLING",
            "INSTALLED",
            "COMPENSATING",
            "COMPENSATED",
            "DEGRADED",
            "RELEASE_PREPARING",
        }
        assert {s.value for s in TransitionStatus} == expected


# =========================================================================
# TeamAssignment invariants
# =========================================================================


class TestTeamAssignment:
    def test_is_singleton(self):
        a = TeamAssignment(team_id="solo:Alice", epoch=1, member_ids=["Alice"])
        assert a.is_singleton

    def test_is_not_singleton(self):
        a = TeamAssignment(
            team_id="team:rescue:r42", epoch=42, member_ids=["Alice", "Bob"]
        )
        assert not a.is_singleton

    def test_singleton_team_id_helper(self):
        assert singleton_team_id("Alice") == "solo:Alice"
        assert singleton_team_id("Bob") != "solo:Alice"


# =========================================================================
# Serialization roundtrip
# =========================================================================


class TestSerialization:
    def test_transition_roundtrip(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        serialized = _transition_to_dict(t)
        deserialized = _dict_to_transition(serialized)

        assert deserialized.transition_id == t.transition_id
        assert deserialized.epoch == t.epoch
        assert deserialized.status == t.status
        assert len(deserialized.affected_workers) == 2
        assert "Alice" in deserialized.after

    def test_transition_roundtrip_with_secret(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="A",
            context_id="ctx-1",
        )
        serialized = _transition_to_dict(t)
        deserialized = _dict_to_transition(serialized)

        assert deserialized.team_secret == t.team_secret
        assert deserialized.team_id == t.team_id

    def test_transition_roundtrip_with_acks(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        registry.record_ack(t.transition_id, "Bob", success=False)

        t_updated = registry.get_transition(t.transition_id)
        assert t_updated is not None

        serialized = _transition_to_dict(t_updated)
        deserialized = _dict_to_transition(serialized)

        assert deserialized.member_deliveries["Alice"].ack_status == "acked"
        assert deserialized.member_deliveries["Bob"].ack_status == "rejected"


# =========================================================================
# Edge cases
# =========================================================================


class TestEdgeCases:
    def test_empty_members_prepare(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=[],
            objective="A",
            context_id="ctx-1",
        )
        assert len(t.affected_workers) == 0

    def test_single_member_collaborative(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice"])
        t = registry.prepare_activation(
            node_id="solo-task",
            members=["Alice"],
            objective="Do thing",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        registry.record_ack(t.transition_id, "Alice", success=True)
        installed = registry.mark_installed(t.transition_id)
        assert installed.status == TransitionStatus.INSTALLED

    def test_check_participants_free_all_free(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob"])
        busy = registry.check_participants_free(["Alice", "Bob"])
        assert busy == []

    def test_multiple_transitions_coexist(self, registry: TeamPartitionRegistry):
        registry.ensure_singletons(["Alice", "Bob", "Charlie", "David"])
        t1 = registry.prepare_activation(
            node_id="rescue-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        t2 = registry.prepare_activation(
            node_id="rescue-b",
            members=["Charlie", "David"],
            objective="B",
            context_id="ctx-2",
        )
        assert t1.transition_id != t2.transition_id
        assert t1.epoch != t2.epoch
