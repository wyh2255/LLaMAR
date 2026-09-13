"""Phase 5 recovery contracts: startup order, outbox replay after
kill/restart, committed canonical set + scope fence verification, and
legacy-unmigrated marking without automatic backfill or retention purge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a.coordinator.memory.contracts import (
    ControlTransitionJournalEntry,
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.exporter import (
    CANONICAL_ARTIFACTS,
    EXPORT_MANIFEST_FILENAME,
    MemoryExporter,
    load_manifest,
)
from a2a.coordinator.memory.ingestor import (
    MemoryIngestor,
    MemoryLifecycleBridge,
    MemoryScopeFactory,
)
from a2a.coordinator.memory.recovery import MemoryRecovery
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

pytestmark = pytest.mark.unit


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "memory.sqlite3"


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


def _build_recovery(
    store: MemoryStore, scope_factory: MemoryScopeFactory
) -> tuple[MemoryRecovery, MemoryIngestor]:
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    bridge = MemoryLifecycleBridge(ing)
    return MemoryRecovery(store, ing, scope_factory, bridge), ing


def _spatial_input(
    scope_id: str,
    *,
    event_id: str,
    env_step: int,
    field_name: str = "intensity",
    value: object = "High",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain="spatial",
        entity_id="FireA",
        entity_type="fire",
        field_name=field_name,
        value=value,
    )


def _seed_pending_outbox(db_path: Path, scope_factory: MemoryScopeFactory) -> str:
    """Create a store, activate a scope, ingest projections (pending outbox
    rows), then close the connection to simulate a crash before replay."""
    store = MemoryStore(db_path)
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    scope_id = ing.activate_scope("ctx-1", 0)
    ing.ingest_projection(
        [
            _spatial_input(
                scope_id,
                event_id="evt_1",
                env_step=8,
                field_name="position",
                value=[4, 4, 0],
            )
        ]
    )
    ing.ingest_projection(
        [
            _spatial_input(
                scope_id,
                event_id="evt_2",
                env_step=8,
                field_name="intensity",
                value="High",
            )
        ]
    )
    pending = [e for e in store.outbox_entries(scope_id) if e["status"] == "pending"]
    assert len(pending) == 2
    store.close()
    return scope_id


# ---------------------------------------------------------------------------
# Startup order: reconcile -> close old scope -> activate new scope -> replay
# ---------------------------------------------------------------------------


def test_startup_order_seams_compose(db_path, scope_factory):
    store = MemoryStore(db_path)
    recovery, ing = _build_recovery(store, scope_factory)
    old_scope_id = ing.activate_scope("ctx-1", 0)

    entries = [
        ControlTransitionJournalEntry.build(
            context_id="ctx-1",
            runtime_epoch=0,
            dispatch_id="d-1",
            control_revision=1,
            previous_state="PREPARED",
            state="DISPATCHING",
            source="dispatch",
            observed_at="2026-08-08T00:00:00+00:00",
            result=None,
        )
    ]
    # 3. journal <-> control_receipt reconciliation first.
    written = recovery.reconcile_control_journal(entries)
    assert written == 1
    receipts = store.control_receipts(old_scope_id)
    assert len(receipts) == 1

    # 5. close the reconciled old scope, then activate the new epoch scope.
    assert recovery.close_old_scope("ctx-1", 0) is True
    new_scope_id = recovery.activate_new_scope("ctx-1", 1)
    assert new_scope_id != old_scope_id
    # Scope fence: the closed tuple stays closed.
    old_scope = store.get_scope(old_scope_id)
    new_scope = store.get_scope(new_scope_id)
    assert old_scope is not None and old_scope["closed_at"] is not None
    assert new_scope is not None and new_scope["closed_at"] is None

    # 6. replay pending outbox deterministically.
    export_dir = db_path.parent / "export"
    result = recovery.replay_outbox(old_scope_id, export_dir)
    assert result.pending == 1
    assert result.replayed == 1
    assert result.artifacts_consistent
    assert (export_dir / EXPORT_MANIFEST_FILENAME).exists()

    # Replaying an already-exported scope is a no-op (at-least-once).
    second = recovery.replay_outbox(old_scope_id, export_dir)
    assert second.replayed == 0
    store.close()


# ---------------------------------------------------------------------------
# Outbox replay after kill/restart
# ---------------------------------------------------------------------------


def test_outbox_replay_after_restart_materializes_artifacts(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    # Restart: fresh store + recovery over the same durable DB.
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"

    before = [e for e in store.outbox_entries(scope_id) if e["status"] == "pending"]
    assert len(before) == 2

    result = recovery.replay_outbox(scope_id, export_dir)
    assert result.pending == 2
    assert result.replayed == 2
    assert result.artifacts_consistent
    assert result.manifest_sha256

    for name in CANONICAL_ARTIFACTS:
        assert (export_dir / name).exists(), f"{name} not materialized"
    assert all(
        e["status"] == "exported"
        for e in store.outbox_entries(scope_id)
        if e["outbox_id"] in {x["outbox_id"] for x in before}
    )
    # Replay never writes new canonical events.
    assert store.temporal_event_count(scope_id) == 2
    store.close()


def test_outbox_replay_is_idempotent_and_deterministic(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"

    first = recovery.replay_outbox(scope_id, export_dir)
    artifact_bytes = {
        name: (export_dir / name).read_bytes() for name in CANONICAL_ARTIFACTS
    }
    second = recovery.replay_outbox(scope_id, export_dir)

    assert first.replayed == 2
    assert second.pending == 0
    assert second.replayed == 0
    for name in CANONICAL_ARTIFACTS:
        assert (export_dir / name).read_bytes() == artifact_bytes[name]
    store.close()


def test_outbox_replay_unknown_scope_is_typed(db_path, scope_factory):
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    result = recovery.replay_outbox("missing-scope", db_path.parent / "export")
    assert result.reason == "unknown_scope"
    assert result.pending == 0
    assert not result.artifacts_consistent
    store.close()


# ---------------------------------------------------------------------------
# Committed canonical set + scope fence verification (read-only)
# ---------------------------------------------------------------------------


def test_verify_committed_canonical_ok_and_scope_fence(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"
    recovery.replay_outbox(scope_id, export_dir)

    ver = recovery.verify_committed_canonical(scope_id, export_dir)
    assert ver.ok()
    assert ver.scope_exists
    assert not ver.scope_closed
    assert ver.revision == 2
    assert ver.pending_outbox == 0
    assert ver.artifacts_consistent
    assert ver.missing_artifacts == []

    # Scope fence: closing the scope is durable and visible to verification.
    assert recovery.close_old_scope("ctx-1", 0) is True
    closed = recovery.verify_committed_canonical(scope_id, export_dir)
    assert closed.scope_closed
    # Closing changes the canonical revision set (closed_at), so the materialized
    # artifact is stale until the deterministic rebuild.
    assert not closed.artifacts_consistent
    recovery.replay_outbox(scope_id, export_dir)
    assert recovery.verify_committed_canonical(scope_id, export_dir).ok()
    store.close()


def test_verify_detects_tampered_artifact_and_replay_repairs(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"
    recovery.replay_outbox(scope_id, export_dir)

    assert recovery.verify_committed_canonical(scope_id, export_dir).ok()

    # Tamper with a materialized artifact.
    path = export_dir / "spatial.jsonl"
    original = path.read_bytes()
    path.write_bytes(b"tampered\n")

    ver = recovery.verify_committed_canonical(scope_id, export_dir)
    assert not ver.artifacts_consistent
    assert ver.missing_artifacts == []

    # Replay deterministically rebuilds from the committed canonical set.
    result = recovery.replay_outbox(scope_id, export_dir)
    assert result.artifacts_consistent
    assert path.read_bytes() == original
    assert recovery.verify_committed_canonical(scope_id, export_dir).ok()
    store.close()


def test_verify_missing_scope_reports_inconsistent(db_path, scope_factory):
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    ver = recovery.verify_committed_canonical("nope", db_path.parent / "export")
    assert not ver.scope_exists
    assert not ver.ok()
    assert sorted(ver.missing_artifacts) == sorted(CANONICAL_ARTIFACTS)
    store.close()


def test_verify_requires_manifest_for_current_scope(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"
    # Artifacts present but no manifest for this scope yet.
    recovery.exporter.export_scope(scope_id, export_dir)

    ver = recovery.verify_committed_canonical(scope_id, export_dir)
    assert ver.manifest_present
    assert ver.artifacts_consistent

    # A manifest whose scope_id does not match is treated as absent.
    foreign = db_path.parent / "foreign"
    foreign.mkdir(exist_ok=True)
    (foreign / EXPORT_MANIFEST_FILENAME).write_text(
        '{"scope_id": "someone-else", "artifacts": {}}', encoding="utf-8"
    )
    ver2 = recovery.verify_committed_canonical(scope_id, foreign)
    assert not ver2.manifest_present
    store.close()


# ---------------------------------------------------------------------------
# Legacy-unmigrated marking + no retention purge
# ---------------------------------------------------------------------------


def test_legacy_unmigrated_no_backfill_no_purge(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"
    recovery.replay_outbox(scope_id, export_dir)

    legacy_path = export_dir / "events.ndjson"
    legacy_path.write_text('{"legacy": true}\n', encoding="utf-8")
    events_before = store.temporal_event_count(scope_id)
    scopes_before = len(store.list_scopes())

    recovery.mark_legacy_unmigrated(
        export_dir,
        {"events.ndjson": "ambiguous historical scope identity"},
        scope_id=scope_id,
    )

    # Read-only legacy artifact is preserved, never backfilled or deleted.
    assert legacy_path.read_text(encoding="utf-8") == '{"legacy": true}\n'
    manifest = load_manifest(export_dir)
    assert manifest is not None
    entry = manifest["legacy_artifacts"]["events.ndjson"]
    assert entry["legacy_unmigrated"] is True
    assert "ambiguous historical scope identity" in entry["reason"]
    # events.ndjson is not a canonical Memory artifact.
    assert "events.ndjson" not in manifest["artifacts"]
    # No automatic backfill: canonical Memory is untouched.
    assert store.temporal_event_count(scope_id) == events_before
    assert len(store.list_scopes()) == scopes_before
    store.close()


def test_no_automatic_retention_purge(db_path, scope_factory):
    scope_id = _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    recovery, _ing = _build_recovery(store, scope_factory)
    export_dir = db_path.parent / "export"

    # Replay on an open scope, then verify a closed scope: no purge anywhere.
    recovery.replay_outbox(scope_id, export_dir)
    store.close()

    store = MemoryStore(db_path)
    recovery2, _ing = _build_recovery(store, scope_factory)
    recovery2.close_old_scope("ctx-1", 0)
    recovery2.verify_committed_canonical(scope_id, export_dir)
    recovery2.replay_outbox(scope_id, export_dir)

    # Completed/closed scopes are never auto-purged.
    assert len(store.list_scopes()) == 1
    assert store.temporal_event_count(scope_id) == 2
    assert len(store.outbox_entries(scope_id)) == 2
    assert all((export_dir / name).exists() for name in CANONICAL_ARTIFACTS)
    store.close()


def test_exporter_instance_injection(db_path, scope_factory):
    _seed_pending_outbox(db_path, scope_factory)
    store = MemoryStore(db_path)
    exporter = MemoryExporter(store)
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    recovery = MemoryRecovery(
        store, ing, scope_factory, MemoryLifecycleBridge(ing), exporter=exporter
    )
    assert recovery.exporter is exporter
    store.close()
