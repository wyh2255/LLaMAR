"""Tests for WorkerTeamState — epoch monotonicity, revoke, single-team, persistence."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from a2a.worker.team_state import (
    WorkerTeamState,
    TeamStateError,
    InvalidTeamParameter,
    TeamEpochRegressionError,
    StaleTeamUpdateError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "team_state.json"


@pytest.fixture
def team_state(state_file: Path) -> WorkerTeamState:
    return WorkerTeamState(state_file, local_worker_id="worker-alpha")


_VALID_SECRET = "aabbccdd00112233aabbccdd00112233"  # 32 hex chars


# ---------------------------------------------------------------------------
# Install / current
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_creates_state(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            team_id="sar-team-7",
            epoch=1,
            members=["worker-alpha", "worker-beta"],
            endpoints={
                "worker-alpha": "http://host1:8090",
                "worker-beta": "http://host2:8090",
            },
            team_secret=_VALID_SECRET,
            coordinator_id="Coordinator",
        )
        ts = team_state.current()
        assert ts is not None
        assert ts.team_id == "sar-team-7"
        assert ts.epoch == 1
        assert "worker-alpha" in ts.members
        assert ts.team_secret == _VALID_SECRET

    def test_epoch_monotonicity(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha", "b"], {}, _VALID_SECRET, "Coordinator"
        )
        team_state.install(
            "sar-team-7",
            2,
            ["worker-alpha", "b", "c"],
            {},
            _VALID_SECRET,
            "Coordinator",
        )
        ts = team_state.current()
        assert ts is not None
        assert ts.epoch == 2

    def test_epoch_regression_raises(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        with pytest.raises(StaleTeamUpdateError, match="not newer"):
            team_state.install(
                "sar-team-7", 3, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )

    def test_install_idempotent_same_epoch(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        ts = team_state.current()
        assert ts is not None
        assert ts.epoch == 1

    def test_install_conflicting_same_epoch_raises(
        self, team_state: WorkerTeamState
    ) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        with pytest.raises(TeamEpochRegressionError, match="different content"):
            team_state.install(
                "sar-team-7", 1, ["worker-alpha", "b"], {}, _VALID_SECRET, "Coordinator"
            )

    def test_monotonic_across_team_replacement(
        self, team_state: WorkerTeamState
    ) -> None:
        team_state.install(
            "sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        team_state.revoke("sar-team-7", 5)
        # New team must have epoch > last_generation.epoch (which is 5)
        with pytest.raises(StaleTeamUpdateError, match="not newer"):
            team_state.install(
                "sar-team-8", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )
        team_state.install(
            "sar-team-8", 6, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        ts = team_state.current()
        assert ts is not None
        assert ts.team_id == "sar-team-8"
        assert ts.epoch == 6


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_revoke_clears_state(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        team_state.revoke("sar-team-7", 1)
        assert team_state.current() is None

    def test_revoke_mismatch_rejected(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        with pytest.raises(InvalidTeamParameter, match="mismatch"):
            team_state.revoke("sar-team-7", 99)
        with pytest.raises(InvalidTeamParameter, match="mismatch"):
            team_state.revoke("other-team", 1)

    def test_revoke_no_active_team(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="No active team"):
            team_state.revoke("sar-team-7", 1)

    def test_stale_install_after_revoke(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        team_state.revoke("sar-team-7", 5)
        # Same team+epoch after revoke is stale
        with pytest.raises(StaleTeamUpdateError, match="not newer"):
            team_state.install(
                "sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_nonblank_team_id(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="team_id"):
            team_state.install(
                "", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )

    def test_epoch_nonnegative(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match=">= 0"):
            team_state.install(
                "t", -1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )

    def test_nonempty_members(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="non-empty"):
            team_state.install("t", 1, [], {}, _VALID_SECRET, "Coordinator")

    def test_member_ids_nonblank(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="non-blank"):
            team_state.install("t", 1, [""], {}, _VALID_SECRET, "Coordinator")

    def test_secret_min_hex_length(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="hex length"):
            team_state.install("t", 1, ["worker-alpha"], {}, "aabb", "Coordinator")

    def test_secret_valid_hex(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="valid hex"):
            team_state.install(
                "t",
                1,
                ["worker-alpha"],
                {},
                "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
                "Coordinator",
            )

    def test_coordinator_id_nonblank(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="coordinator_id"):
            team_state.install("t", 1, ["worker-alpha"], {}, _VALID_SECRET, "")

    def test_endpoints_only_for_members(self, team_state: WorkerTeamState) -> None:
        with pytest.raises(InvalidTeamParameter, match="non-member"):
            team_state.install(
                "t",
                1,
                ["worker-alpha"],
                {"intruder": "http://x"},
                _VALID_SECRET,
                "Coordinator",
            )


# ---------------------------------------------------------------------------
# to_auth_context / peer_secret_bytes
# ---------------------------------------------------------------------------


class TestToAuthContext:
    def test_with_active_team(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha", "b"], {}, _VALID_SECRET, "Coordinator"
        )
        ctx = team_state.to_auth_context()
        assert ctx is not None
        assert ctx.current_team_id == "sar-team-7"
        assert ctx.current_team_epoch == 1
        assert ctx.current_team_members == frozenset({"worker-alpha", "b"})

    def test_no_team(self, team_state: WorkerTeamState) -> None:
        assert team_state.to_auth_context() is None

    def test_peer_secret_bytes(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        assert team_state.peer_secret_bytes() == bytes.fromhex(_VALID_SECRET)

    def test_peer_secret_no_team(self, team_state: WorkerTeamState) -> None:
        assert team_state.peer_secret_bytes() is None


# ---------------------------------------------------------------------------
# Persistence (including last_generation)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_roundtrip(self, state_file: Path) -> None:
        s1 = WorkerTeamState(state_file, "worker-alpha")
        s1.install(
            "sar-team-7",
            2,
            ["worker-alpha", "b"],
            {"worker-alpha": "http://x"},
            _VALID_SECRET,
            "Coordinator",
        )

        s2 = WorkerTeamState(state_file, "worker-alpha")
        ts = s2.current()
        assert ts is not None
        assert ts.team_id == "sar-team-7"
        assert ts.epoch == 2
        assert ts.members == ["worker-alpha", "b"]
        assert ts.endpoints == {"worker-alpha": "http://x"}
        assert ts.team_secret == _VALID_SECRET

    def test_persistence_revoke(self, state_file: Path) -> None:
        s1 = WorkerTeamState(state_file, "worker-alpha")
        s1.install("sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator")
        s1.revoke("sar-team-7", 1)

        s2 = WorkerTeamState(state_file, "worker-alpha")
        assert s2.current() is None

    def test_last_generation_prevents_stale_after_restart(
        self, state_file: Path
    ) -> None:
        s1 = WorkerTeamState(state_file, "worker-alpha")
        s1.install("sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator")
        s1.revoke("sar-team-7", 5)

        s2 = WorkerTeamState(state_file, "worker-alpha")
        with pytest.raises(StaleTeamUpdateError, match="not newer"):
            s2.install(
                "sar-team-7", 5, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
            )

    def test_malformed_state_fails_closed(self, state_file: Path) -> None:
        state_file.write_text("{bad json}")
        with pytest.raises(TeamStateError, match="Failed to load"):
            WorkerTeamState(state_file, "worker-alpha")

    def test_file_permissions(self, state_file: Path) -> None:
        s = WorkerTeamState(state_file, "worker-alpha")
        s.install("sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator")
        mode = stat.S_IMODE(os.stat(str(state_file)).st_mode)
        assert mode & 0o077 == 0

    def test_current_defensive_copy(self, team_state: WorkerTeamState) -> None:
        team_state.install(
            "sar-team-7", 1, ["worker-alpha"], {}, _VALID_SECRET, "Coordinator"
        )
        c1 = team_state.current()
        c2 = team_state.current()
        assert c1 is not None and c2 is not None
        assert c1 is not c2
        assert c1.team_id == c2.team_id
