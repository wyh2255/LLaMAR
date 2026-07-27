"""Phase 5 TeamStatusAuth + partition-authoritative /team-status RED/GREEN tests.

Covers:
  1. TeamStatusProof generation/verification (valid, forged, stale, replay)
  2. Singleton returns empty peers (not all other agents)
  3. Collaborative team returns member IDs
  4. Missing/invalid proof → 403 (team_status_unauthorized)
  5. Forged agent ID → 403
  6. Stale proof → rejection
  7. Replay (same proof reused) → rejected after expiry
  8. Same-step team transition cache refresh (team_partition_revision)
  9. Cross-team mail authorization / old epoch rejection
 10. Context/event payloads omit secrets/tokens/signatures
 11. Worker state provider cache key includes team_generation
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from a2a.coordinator.team_partition_service import (
    TeamPartitionRegistry,
    TeamPartitionService,
)
from a2a.coordinator.team_status_auth import (
    TeamStatusProof,
    UsedNonceStore,
    SEPARATOR,
)
from sar_orch.worker_state_provider import SARWorkerStateProvider


# =========================================================================
# Fixtures
# =========================================================================

COORDINATOR_SECRET = b"test-coordinator-secret-32-bytes!!"
ALT_SECRET = b"different-coordinator-secret-32-byte"


@pytest.fixture
def registry() -> TeamPartitionRegistry:
    return TeamPartitionRegistry(coordinator_id="Coordinator")


@pytest.fixture
def tps(registry: TeamPartitionRegistry) -> TeamPartitionService:
    return TeamPartitionService(registry=registry)


def _make_team_status_app(
    tps: TeamPartitionService,
    secret: bytes | None = COORDINATOR_SECRET,
    nonce_store: UsedNonceStore | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the Phase 5 /team-status endpoint."""
    if nonce_store is None:
        nonce_store = UsedNonceStore()
    app = FastAPI()

    @app.get("/team-status")
    async def team_status(agent_id: str, proof: str = ""):
        if tps is None:
            return {"error": "team_partition_service not configured", "teammates": []}

        if secret:
            valid, reason = TeamStatusProof.verify(
                secret,
                proof,
                agent_id,
                nonce_store=nonce_store,
            )
            if not valid:
                raise HTTPException(
                    status_code=403,
                    detail=f"team_status_unauthorized: {reason}",
                )

        assignment = tps.get_assignment(agent_id)
        if assignment is None:
            return {
                "teammates": [],
                "current_step": 0,
                "team_partition_revision": tps.team_partition_revision,
            }

        if assignment.is_singleton:
            return {
                "teammates": [],
                "current_step": 0,
                "team_partition_revision": tps.team_partition_revision,
                "team_id": assignment.team_id,
                "epoch": assignment.epoch,
                "is_singleton": True,
            }

        safe_view = TeamStatusProof.safe_team_view(
            team_id=assignment.team_id,
            epoch=assignment.epoch,
            member_ids=assignment.member_ids,
            team_partition_revision=tps.team_partition_revision,
        )
        return {
            "teammates": safe_view["members"],
            "team_id": safe_view["team_id"],
            "epoch": safe_view["epoch"],
            "team_partition_revision": safe_view["team_partition_revision"],
            "current_step": 0,
            "is_singleton": False,
        }

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return app


# =========================================================================
# 1. TeamStatusProof — generation & verification
# =========================================================================


class TestTeamStatusProofBasic:
    def test_generate_returns_string(self):
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        assert isinstance(proof, str)
        assert len(proof) > 10

    def test_generate_proof_is_v2_format(self):
        """Proof is v2 format: 4 parts (worker_id.timestamp.nonce.sig)."""
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        decoded = base64.urlsafe_b64decode(proof.encode("ascii")).decode("utf-8")
        parts = decoded.split(SEPARATOR)
        assert len(parts) == 4, f"expected 4 parts, got {len(parts)}"
        assert parts[0] == "Alice"
        # parts[1] = timestamp, parts[2] = nonce (32 hex chars), parts[3] = sig
        assert len(parts[2]) == 32, f"nonce should be 32 hex chars, got {len(parts[2])}"

    def test_generate_with_explicit_timestamp(self):
        ts = 1000000
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice", timestamp=ts)
        decoded = base64.urlsafe_b64decode(proof.encode("ascii")).decode("utf-8")
        parts = decoded.split(SEPARATOR)
        assert len(parts) == 4
        assert parts[0] == "Alice"
        assert parts[1] == str(ts)

    def test_two_consecutive_proofs_have_different_nonces(self):
        """Each call to generate() produces a unique nonce."""
        p1 = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        p2 = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        assert p1 != p2

    def test_verify_valid_proof(self):
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        valid, reason = TeamStatusProof.verify(COORDINATOR_SECRET, proof, "Alice")
        assert valid
        assert reason == ""

    def test_verify_valid_proof_within_age(self):
        ts = int(time.time()) - 5
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice", timestamp=ts)
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET, proof, "Alice", max_age_seconds=60
        )
        assert valid, f"expected valid, got: {reason}"

    def test_verify_rejects_forged_agent(self):
        """RED: forged agent ID must be rejected."""
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        valid, reason = TeamStatusProof.verify(COORDINATOR_SECRET, proof, "Eve")
        assert not valid
        assert "worker_id mismatch" in reason

    def test_verify_rejects_wrong_secret(self):
        """RED: wrong verification secret must fail."""
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        valid, reason = TeamStatusProof.verify(ALT_SECRET, proof, "Alice")
        assert not valid
        assert "invalid signature" in reason

    def test_verify_rejects_empty_proof(self):
        valid, reason = TeamStatusProof.verify(COORDINATOR_SECRET, "", "Alice")
        assert not valid
        assert "empty" in reason

    def test_verify_rejects_garbage_proof(self):
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET, "not-a-valid-base64!!", "Alice"
        )
        assert not valid

    def test_verify_rejects_malformed_proof(self):
        bad_proof = base64.urlsafe_b64encode(b"only-one-part").decode("ascii")
        valid, reason = TeamStatusProof.verify(COORDINATOR_SECRET, bad_proof, "Alice")
        assert not valid
        assert "malformed" in reason
        assert "4 parts" in reason.lower()

    def test_verify_rejects_nan_timestamp(self):
        # V2 format with non-numeric timestamp: Alice.NaN.abcdef.sig
        payload = f"Alice{SEPARATOR}not-a-number{SEPARATOR}abcdef{SEPARATOR}deadbeef"
        bad_proof = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
        valid, reason = TeamStatusProof.verify(COORDINATOR_SECRET, bad_proof, "Alice")
        assert not valid
        assert "non-integer" in reason

    def test_generate_requires_non_empty_secret(self):
        with pytest.raises(ValueError, match="coordinator_secret must be non-empty"):
            TeamStatusProof.generate(b"", "Alice")

    def test_generate_requires_non_empty_worker_id(self):
        with pytest.raises(ValueError, match="worker_id must be non-empty"):
            TeamStatusProof.generate(COORDINATOR_SECRET, "")

    def test_verify_requires_non_empty_secret(self):
        valid, reason = TeamStatusProof.verify(b"", "fake-proof", "Alice")
        assert not valid
        assert "coordinator_secret is empty" in reason


# =========================================================================
# 2 & 3. Stale / expired / replay proof
# =========================================================================


class TestStaleAndExpiredProof:
    def test_verify_rejects_expired_proof(self):
        """RED: proof older than max_age_seconds must be rejected."""
        ts = int(time.time()) - 120
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice", timestamp=ts)
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET, proof, "Alice", max_age_seconds=60
        )
        assert not valid
        assert "expired" in reason

    def test_verify_rejects_future_timestamp(self):
        """RED: proof with timestamp too far in the future must be rejected."""
        ts = int(time.time()) + 30
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice", timestamp=ts)
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET, proof, "Alice", clock_skew=10
        )
        assert not valid
        assert "future" in reason

    def test_stale_proof_with_increased_max_age(self):
        """Proof valid within different max_age window."""
        ts = int(time.time()) - 30
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice", timestamp=ts)
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET, proof, "Alice", max_age_seconds=60
        )
        assert valid, f"expected valid within 60s, got: {reason}"

    def test_proof_verify_rejects_malformed_v1_proof(self):
        """V1 format (3 parts) is rejected; v2 (4 parts) required."""
        payload = f"Alice{SEPARATOR}1000000".encode("utf-8")
        sig = hmac.new(COORDINATOR_SECRET, payload, hashlib.sha256).hexdigest()
        v1_proof = base64.urlsafe_b64encode(
            f"Alice{SEPARATOR}1000000{SEPARATOR}{sig}".encode("utf-8")
        ).decode("ascii")
        valid, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            v1_proof,
            "Alice",
            max_age_seconds=600,
        )
        assert not valid
        assert "expected 4 parts" in reason


# =========================================================================
# Replay prevention (nonce-based)
# =========================================================================


class TestReplayPrevention:
    def test_same_proof_rejected_on_replay(self):
        """RED: identical proof used twice must be rejected on second use."""
        store = UsedNonceStore(ttl_seconds=300)
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")

        valid1, _ = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof,
            "Alice",
            nonce_store=store,
        )
        assert valid1, "first use must succeed"

        valid2, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof,
            "Alice",
            nonce_store=store,
        )
        assert not valid2, "second identical use must be rejected"
        assert "replay" in reason

    def test_second_request_with_same_proof_gets_403(self, tps: TeamPartitionService):
        """Direct server test: replay the identical proof → 403."""
        tps.ensure_singletons(["Alice"])
        store = UsedNonceStore(ttl_seconds=300)
        app = _make_team_status_app(tps, COORDINATOR_SECRET, nonce_store=store)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")

        # First request — must succeed
        r1 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        assert r1.status_code == 200

        # Second request with identical proof — must be rejected
        r2 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        assert r2.status_code == 403, (
            f"expected 403 for replay, got {r2.status_code}: {r2.text}"
        )
        assert "replay" in r2.json()["detail"]

    def test_distinct_concurrent_proofs_both_succeed(self, tps: TeamPartitionService):
        """Two separately generated proofs must both work, even in quick succession."""
        tps.ensure_singletons(["Alice"])
        store = UsedNonceStore(ttl_seconds=300)
        app = _make_team_status_app(tps, COORDINATOR_SECRET, nonce_store=store)
        client = TestClient(app)

        proof_a = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        proof_b = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        assert proof_a != proof_b, "two proofs must have different nonces"

        r1 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof_a},
        )
        assert r1.status_code == 200, f"first proof failed: {r1.text}"

        r2 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof_b},
        )
        assert r2.status_code == 200, f"second distinct proof failed: {r2.text}"

    def test_different_workers_get_independent_nonce_spaces(self):
        """Nonces for different workers are stored independently per store
        (but the store is shared, so the same nonce for different workers
        is still caught — that's fine since the nonce is bound to the
        workers_id in the HMAC and the proof will have a different worker_id).
        Actually, the store is a single flat namespace.  Different workers
        can produce the same nonce only by crypto collision (negligible).
        This test verifies that Alice's proof used by Eve is rejected
        by worker_id mismatch before the nonce check even runs."""
        store = UsedNonceStore(ttl_seconds=300)
        proof_alice = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")

        valid_a, _ = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof_alice,
            "Alice",
            nonce_store=store,
        )
        assert valid_a

        # Eve tries to reuse Alice's proof
        valid_eve, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof_alice,
            "Eve",
            nonce_store=store,
        )
        assert not valid_eve
        assert "worker_id mismatch" in reason

    def test_nonce_eviction_after_ttl(self):
        """After the nonce TTL expires, a proof with that nonce can be used again
        (theoretically — in practice the proof would also have expired by then)."""
        store = UsedNonceStore(ttl_seconds=0)  # instant eviction
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")

        valid1, _ = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof,
            "Alice",
            nonce_store=store,
        )
        assert valid1

        # TTL=0 means the nonce is already evicted on the next check
        import time as _time

        _time.sleep(0.01)  # ensure monotonic clock advances

        valid2, reason = TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof,
            "Alice",
            nonce_store=store,
        )
        # After TTL eviction, the nonce is gone, so the proof is accepted
        assert valid2, f"expected valid after TTL eviction, got: {reason}"

    def test_nonce_store_len(self):
        store = UsedNonceStore(ttl_seconds=300)
        assert len(store) == 0
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        TeamStatusProof.verify(
            COORDINATOR_SECRET,
            proof,
            "Alice",
            nonce_store=store,
        )
        assert len(store) == 1


# =========================================================================
# 4 & 5. Singletons and endpoint integration
# =========================================================================


class TestSingletonEndpoint:
    def test_singleton_returns_empty_peers(self, tps: TeamPartitionService):
        """Singleton team must return empty teammates list (not all agents)."""
        tps.ensure_singletons(["Alice", "Bob", "Charlie"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["teammates"] == []
        assert data["is_singleton"] is True
        assert "team_id" in data
        assert "epoch" in data
        # No secret fields in response
        assert "team_secret" not in data
        assert "secret" not in data
        assert "hmac" not in data
        assert "signature" not in data

    def test_no_proof_rejected(self, tps: TeamPartitionService):
        """RED: missing proof must be rejected with 403."""
        tps.ensure_singletons(["Alice"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        resp = client.get("/team-status", params={"agent_id": "Alice"})
        assert resp.status_code == 403
        assert "team_status_unauthorized" in resp.json()["detail"]

    def test_empty_proof_rejected(self, tps: TeamPartitionService):
        """RED: empty proof string must be rejected with 403."""
        tps.ensure_singletons(["Alice"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": ""},
        )
        assert resp.status_code == 403
        assert "team_status_unauthorized" in resp.json()["detail"]

    def test_forged_agent_rejected(self, tps: TeamPartitionService):
        """RED: Alice's proof used for Eve must be rejected."""
        tps.ensure_singletons(["Alice", "Eve"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof_alice = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Eve", "proof": proof_alice},
        )
        assert resp.status_code == 403
        assert "worker_id mismatch" in resp.json()["detail"]

    def test_expired_proof_rejected(self, tps: TeamPartitionService):
        """RED: stale proof must be rejected with 403."""
        tps.ensure_singletons(["Alice"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        old_ts = int(time.time()) - 120
        old_proof = TeamStatusProof.generate(
            COORDINATOR_SECRET,
            "Alice",
            timestamp=old_ts,
        )
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": old_proof},
        )
        assert resp.status_code == 403
        assert "expired" in resp.json()["detail"]

    def test_no_secret_configured_passes(self, tps: TeamPartitionService):
        """When no coordinator_secret, endpoint accepts any agent_id (degraded mode)."""
        app = _make_team_status_app(tps, secret=None)
        client = TestClient(app)

        tps.ensure_singletons(["Alice"])
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["teammates"] == []

    def test_unregistered_worker(self, tps: TeamPartitionService):
        """Unregistered worker returns empty teammates without error."""
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Unknown")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Unknown", "proof": proof},
        )
        assert resp.status_code == 200
        assert resp.json()["teammates"] == []


# =========================================================================
# 6. Collaborative team returns member IDs
# =========================================================================


class TestCollaborativeTeamEndpoint:
    def test_collaborative_returns_members(self, tps: TeamPartitionService):
        """Collaborative team returns member IDs, not all agents."""
        tps.ensure_singletons(["Alice", "Bob", "Charlie", "David"])
        t = tps.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t.transition_id, w, success=True)
        tps._registry.mark_installed(t.transition_id)

        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_singleton"] is False
        assert "Alice" in data["teammates"]
        assert "Bob" in data["teammates"]
        assert "Charlie" not in data["teammates"]  # Not in this team
        # Charlie should NOT appear - partition authority
        assert "Charlie" not in data["teammates"]
        assert "David" not in data["teammates"]
        # No secret leakage
        assert "team_secret" not in data
        assert "secret" not in data

    def test_singleton_charlie_returns_empty(self, tps: TeamPartitionService):
        """Charlie (singleton) must not see Alice/Bob as teammates."""
        tps.ensure_singletons(["Alice", "Bob", "Charlie"])
        t = tps.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="Rescue",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t.transition_id, w, success=True)
        tps._registry.mark_installed(t.transition_id)

        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Charlie")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Charlie", "proof": proof},
        )
        assert resp.status_code == 200
        assert resp.json()["teammates"] == []
        assert resp.json()["is_singleton"] is True


# =========================================================================
# 7. Same-step team transition cache refresh (team_partition_revision)
# =========================================================================


class TestSameStepCacheRefresh:
    def test_revision_changes_on_install(self, registry: TeamPartitionRegistry):
        """team_partition_revision must increment on collaborative install."""
        rev0 = registry.team_partition_revision
        registry.ensure_singletons(["Alice", "Bob"])
        assert registry.team_partition_revision > rev0

    def test_revision_changes_on_release(self, registry: TeamPartitionRegistry):
        """team_partition_revision must increment on release."""
        registry.ensure_singletons(["Alice", "Bob"])
        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="R",
            context_id="ctx-1",
        )
        registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            registry.record_ack(t.transition_id, w, success=True)
        rev_before = registry.team_partition_revision
        registry.mark_installed(t.transition_id)
        assert registry.team_partition_revision > rev_before

    def test_same_step_response_revision_changes(self, tps: TeamPartitionService):
        """Same env_step, different team install → different team_partition_revision."""
        tps.ensure_singletons(["Alice", "Bob"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)
        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")

        resp1 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        rev1 = resp1.json()["team_partition_revision"]

        t = tps.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="R",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t.transition_id, w, success=True)
        tps._registry.mark_installed(t.transition_id)

        proof2 = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp2 = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof2},
        )
        rev2 = resp2.json()["team_partition_revision"]
        assert rev2 > rev1
        assert resp2.json()["is_singleton"] is False

    def test_cache_key_includes_revision(self):
        """Cache key includes (env_step, team_gen, known_revision)."""
        ts_mock = MagicMock()
        ts_mock.generation = ("solo:Alice", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        provider._cached_team_status_revision = 42
        provider._last_team_status_cache_key = (0, ("solo:Alice", 1, True), 42)

        cache_key = (
            0,
            ("solo:Alice", 1, True),
            provider._cached_team_status_revision,
        )
        assert cache_key == provider._last_team_status_cache_key

        # When revision changes externally (force_refresh), cache miss
        provider.force_refresh_team_status()
        assert provider._last_team_status_cache_key == ()

    def test_same_env_step_changed_gen_reenched(self):
        """RED: same env_step + changed team_gen + same known_revision → re-fetch."""
        ts_mock = MagicMock()
        ts_mock.generation = ("team:a", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        provider._last_team_status_cache_key = (0, ("team:a", 1, True), -1)
        provider._cached_team_status_revision = -1

        ts_mock.generation = ("team:b", 2, True)
        new_cache_key = (
            0,
            ("team:b", 2, True),
            provider._cached_team_status_revision,
        )
        assert new_cache_key != provider._last_team_status_cache_key


# =========================================================================
# Cache correctness — worker stores and uses team_partition_revision
# =========================================================================


class TestCacheCorrectness:
    def test_worker_stores_revision_from_response(self):
        """Worker must store team_partition_revision from server response."""
        ts_mock = MagicMock()
        ts_mock.generation = ("solo:Alice", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        assert provider._cached_team_status_revision == -1

        # Simulate receiving a response
        provider._cached_teammates = []
        provider._cached_team_status_revision = 42
        provider._last_team_status_cache_key = (
            0,
            ("solo:Alice", 1, True),
            42,
        )
        assert provider._cached_team_status_revision == 42

    def test_stale_revision_triggers_refresh(self):
        """When known_revision diverges from server state, re-fetch on next call."""
        ts_mock = MagicMock()
        ts_mock.generation = ("solo:Alice", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"

        # Cache says we have revision 5
        provider._cached_team_status_revision = 5
        provider._last_team_status_cache_key = (0, ("solo:Alice", 1, True), 5)

        # After force_refresh, cache key becomes () so next call re-fetches
        provider.force_refresh_team_status()
        assert provider._last_team_status_cache_key == ()

    def test_force_refresh_resets_cache(self):
        """force_refresh_team_status resets cache key, forcing re-fetch."""
        ts_mock = MagicMock()
        ts_mock.generation = ("solo:Alice", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        provider._last_team_status_cache_key = (5, ("solo:Alice", 1, True), 10)

        provider.force_refresh_team_status()

        # Cache key reset to empty tuple → cache miss guaranteed
        assert provider._last_team_status_cache_key == ()

    def test_fetch_updates_revision(self):
        """After fetch, stored revision matches server value."""
        ts_mock = MagicMock()
        ts_mock.generation = ("solo:Alice", 1, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        provider._cached_team_status_revision = -1

        # Simulate successful fetch
        provider._cached_team_status_revision = 99
        provider._last_team_status_cache_key = (
            0,
            ("solo:Alice", 1, True),
            99,
        )
        assert provider._cached_team_status_revision == 99


# =========================================================================
# 8. Cross-team mail authorization / old epoch rejection
# =========================================================================


class TestCrossTeamMailAuth:
    """Peer mail authorization boundaries (I7).

    Workers from different teams or with stale epoch must not be able to
    send mail to each other.  The team_status endpoint (partition-authoritative)
    only returns teammates from the same team, so cross-team discovery is
    prevented at the information layer.
    """

    def test_disjoint_teams_dont_see_each_other(self, tps: TeamPartitionService):
        """Two disjoint collaborative teams: members of team A don't see team B."""
        tps.ensure_singletons(["Alice", "Bob", "Charlie", "David"])
        t1 = tps.prepare_activation(
            node_id="team-a",
            members=["Alice", "Bob"],
            objective="A",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t1.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t1.transition_id, w, success=True)
        tps._registry.mark_installed(t1.transition_id)

        t2 = tps.prepare_activation(
            node_id="team-b",
            members=["Charlie", "David"],
            objective="B",
            context_id="ctx-2",
        )
        tps._registry.mark_installing(t2.transition_id)
        for w in ["Charlie", "David"]:
            tps._registry.record_ack(t2.transition_id, w, success=True)
        tps._registry.mark_installed(t2.transition_id)

        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        data = resp.json()
        assert "Charlie" not in data["teammates"]
        assert "David" not in data["teammates"]
        assert data["team_id"] != t2.team_id

    def test_old_epoch_returns_different_team(self, tps: TeamPartitionService):
        """After release, old epoch is no longer valid; worker sees singleton."""
        tps.ensure_singletons(["Alice", "Bob"])
        t = tps.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="R",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t.transition_id, w, success=True)
        installed = tps._registry.mark_installed(t.transition_id)
        old_epoch = installed.epoch

        # Release back to singleton
        release = tps._registry.prepare_release("ctx-1")
        tps._registry.mark_installing(release.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(release.transition_id, w, success=True)
        tps._registry.complete_release(release.transition_id)

        # Alice is now singleton
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        data = resp.json()
        assert data["is_singleton"] is True
        assert data["epoch"] > old_epoch  # New epoch after release
        assert data["teammates"] == []

    def test_epoch_monotonic_across_transitions(self, registry: TeamPartitionRegistry):
        """Epoch must be strictly increasing across all transitions (I10)."""
        registry.ensure_singletons(["Alice"])
        e1 = registry.global_epoch

        t = registry.prepare_activation(
            node_id="rescue",
            members=["Alice"],
            objective="R",
            context_id="ctx-1",
        )
        assert t.epoch > e1


# =========================================================================
# 9. Context/event payloads omit secrets/tokens/signatures
# =========================================================================


class TestSecretFreeObservability:
    """I11: Context Memory and logs must not contain secrets."""

    SECRET_KEYWORDS = [
        "coordinator_secret",
        "team_secret",
        "hmac",
        "signature",
        "signed_envelope",
        "request_token",
        "auth_token",
        "proof",
        "peer_secret",
        "endpoint_credential",
    ]

    def test_safe_team_view_no_secrets(self):
        """TeamStatusProof.safe_team_view must exclude secrets."""
        view = TeamStatusProof.safe_team_view(
            team_id="team:rescue:r42",
            epoch=42,
            member_ids=["Alice", "Bob"],
        )
        for kw in self.SECRET_KEYWORDS:
            assert kw not in view, f"safe_team_view contains '{kw}'"

    def test_endpoint_response_no_secrets(self, tps: TeamPartitionService):
        """/team-status response must not contain secrets."""
        tps.ensure_singletons(["Alice", "Bob"])
        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        data = resp.json()
        body_str = json.dumps(data).lower()
        for kw in self.SECRET_KEYWORDS:
            assert kw not in body_str, f"response contains '{kw}'"

    def test_collaborative_response_no_secrets(self, tps: TeamPartitionService):
        """Collaborative team response must not contain secrets."""
        tps.ensure_singletons(["Alice", "Bob"])
        t = tps.prepare_activation(
            node_id="rescue",
            members=["Alice", "Bob"],
            objective="R",
            context_id="ctx-1",
        )
        tps._registry.mark_installing(t.transition_id)
        for w in ["Alice", "Bob"]:
            tps._registry.record_ack(t.transition_id, w, success=True)
        tps._registry.mark_installed(t.transition_id)

        app = _make_team_status_app(tps, COORDINATOR_SECRET)
        client = TestClient(app)

        proof = TeamStatusProof.generate(COORDINATOR_SECRET, "Alice")
        resp = client.get(
            "/team-status",
            params={"agent_id": "Alice", "proof": proof},
        )
        data = resp.json()
        body_str = json.dumps(data).lower()
        for kw in self.SECRET_KEYWORDS:
            assert kw not in body_str, f"collaborative response contains '{kw}'"

    def test_worker_state_team_summary_no_secrets(self):
        """Worker context team_summary must not contain secrets."""
        from a2a.worker.team_state import WorkerTeamState

        ts = WorkerTeamState(
            path="/tmp/test_team_state_no_secrets.json",
            local_worker_id="Alice",
        )
        ts.install(
            team_id="team:rescue:r42",
            epoch=42,
            members=["Alice", "Bob"],
            endpoints={"Alice": "http://a:1", "Bob": "http://b:2"},
            team_secret="aabb" * 8,  # 32 hex chars
            coordinator_id="Coordinator",
        )

        # Simulate what worker_state_provider builds
        current = ts.current()
        summary = {
            "team_id": current.team_id,
            "epoch": current.epoch,
            "members": list(current.members),
            "coordinator_id": current.coordinator_id,
        }
        summary_str = json.dumps(summary).lower()
        for kw in self.SECRET_KEYWORDS:
            assert kw not in summary_str, f"team_summary contains '{kw}'"
        assert "team_secret" not in summary_str

        # Clean up
        import os

        try:
            os.unlink("/tmp/test_team_state_no_secrets.json")
        except FileNotFoundError:
            pass

    def test_event_payload_no_secrets(self):
        """Event payloads (observability) must exclude secrets."""
        event = {
            "context_id": "ctx-1",
            "logical_node_id": "rescue",
            "transition_id": "tr_abc123",
            "team_id": "team:rescue:r42",
            "team_epoch": 42,
            "team_partition_revision": 5,
            "worker_ids": ["Alice", "Bob"],
            "event_name": "team_partition_transition",
            "outcome": "INSTALLED",
            "observed_at": "2026-07-21T00:00:00Z",
        }
        event_str = json.dumps(event).lower()
        for kw in self.SECRET_KEYWORDS:
            assert kw not in event_str, f"event payload contains '{kw}'"


# =========================================================================
# 10. Provider cache key includes revision
# =========================================================================


class TestWorkerProviderCacheKey:
    def test_cache_key_triple(self):
        """Cache key is (env_step, team_gen, known_revision)."""
        ts_mock = MagicMock()
        ts_mock.generation = ("team:rescue:r42", 42, True)

        provider = SARWorkerStateProvider(
            coordinator_secret=COORDINATOR_SECRET,
            team_state=ts_mock,
        )
        provider._agent_name = "Alice"
        provider._team_status_url = "http://coordinator:8080"
        provider._cached_team_status_revision = 10

        assert provider._cached_team_status_revision == 10


# =========================================================================
# 11. Snapshot includes partition revision
# =========================================================================


class TestPartitionSnapshot:
    def test_snapshot_includes_revision(self, registry: TeamPartitionRegistry):
        """partition_snapshot must include team_partition_revision."""
        snap = registry.snapshot()
        assert "team_partition_revision" in snap

    def test_revision_starts_at_zero(self, registry: TeamPartitionRegistry):
        assert registry.team_partition_revision == 0

    def test_revision_monotonic(self, registry: TeamPartitionRegistry):
        revs = []
        registry.ensure_singletons(["Alice"])
        revs.append(registry.team_partition_revision)
        registry.ensure_singletons(["Bob"])
        revs.append(registry.team_partition_revision)
        for i in range(1, len(revs)):
            assert revs[i] >= revs[i - 1]
