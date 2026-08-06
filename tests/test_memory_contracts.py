"""Phase 0 memory contracts: canonical scope serialization, namespaced
relations, freshness, and the no-control-plane-mutation fence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from Agent.environment_state import EnvironmentStateView, Freshness
from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    MemoryConfigError,
    MemoryRef,
    MemoryRefValidationError,
    MemoryRelation,
    MemoryScopeV1,
    canonical_json_bytes,
    control_transition_digest,
)
from a2a.coordinator.memory.store import MemoryService, MemoryStore


def test_memory_scope_v1_scope_id_is_sha256_of_canonical_json():
    scope = MemoryScopeV1("llamar", "run-1", "ctx-1", 3)
    canonical = json.dumps(
        {
            "project_id": "llamar",
            "experiment_id": "run-1",
            "context_id": "ctx-1",
            "runtime_epoch": 3,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    assert scope.canonical_bytes() == canonical
    assert scope.scope_id == hashlib.sha256(canonical).hexdigest()

    # Same tuple always serializes to the same scope id.
    assert MemoryScopeV1("llamar", "run-1", "ctx-1", 3).scope_id == scope.scope_id

    # Any field difference yields a distinct scope id.
    other = MemoryScopeV1("llamar", "run-1", "ctx-1", 4)
    assert other.scope_id != scope.scope_id
    assert (
        other.scope_id
        == hashlib.sha256(
            canonical_json_bytes(
                {
                    "project_id": "llamar",
                    "experiment_id": "run-1",
                    "context_id": "ctx-1",
                    "runtime_epoch": 4,
                }
            )
        ).hexdigest()
    )


def test_namespaced_relations_carry_read_only_control_refs(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))

    relation = MemoryRelation(
        relation_id="rel-1",
        scope_id=scope.scope_id,
        from_ref=MemoryRef(namespace="memory", id="spatial-1"),
        relation_type="observed_by",
        to_ref=MemoryRef(namespace="control", id="dsp_1"),
        source_event_id="evt-1",
    )
    store.add_relation(relation)

    rows = store.relations_for_scope(scope.scope_id)
    assert len(rows) == 1
    assert rows[0].from_ref.namespace == "memory"
    assert rows[0].to_ref.namespace == "control"
    assert rows[0].to_ref.id == "dsp_1"

    with pytest.raises(MemoryRefValidationError):
        MemoryRef(namespace="carrier", id="x")
    with pytest.raises(MemoryRefValidationError):
        MemoryRef(namespace="memory", id="")


def test_freshness_enum_is_strictly_three_valued():
    assert {member.value for member in Freshness} == {
        "FRESH",
        "STALE",
        "UNAVAILABLE",
    }

    unavailable = EnvironmentStateView.unavailable("scope closed")
    assert unavailable.freshness is Freshness.UNAVAILABLE

    stale = EnvironmentStateView.stale("old projection", source_revision=5)
    assert stale.freshness is Freshness.STALE
    assert stale.source_revision == 5

    fresh = EnvironmentStateView.fresh(source_revision=9)
    assert fresh.freshness is Freshness.FRESH


def test_memory_service_forbids_control_plane_mutation(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))
    service = MemoryService(store)

    updated = service.update(
        scope_id=scope.scope_id,
        domain="temporal",
        ref=MemoryRef(namespace="control", id="dsp_1"),
        payload={"state": "COMPLETED"},
    )
    assert updated.ok is False
    assert updated.error == "control_mutation_forbidden"

    deleted = service.delete(
        scope_id=scope.scope_id,
        domain="temporal",
        ref=MemoryRef(namespace="control", id="dsp_1"),
    )
    assert deleted.ok is False
    assert deleted.error == "control_mutation_forbidden"

    created = service.create(
        scope_id=scope.scope_id,
        domain="temporal",
        ref=MemoryRef(namespace="control", id="dsp_1"),
    )
    assert created.ok is False
    assert created.error == "control_mutation_forbidden"

    # No public MemoryStore method can transition a dispatch.
    mutators = {
        name
        for name in dir(store)
        if name in {"apply_physical_status", "transition_dispatch", "mutate_dispatch"}
    }
    assert not mutators


def test_control_transition_digest_is_deterministic_and_excludes_raw_body():
    fields = {
        "context_id": "ctx-1",
        "runtime_epoch": 2,
        "dispatch_id": "dsp_1",
        "control_revision": 1,
        "previous_state": "PREPARED",
        "state": "DISPATCHING",
        "source": "dispatch",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "result_digest": None,
    }
    first = control_transition_digest(fields)
    second = control_transition_digest(dict(fields))
    assert first == second
    assert first == hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
    assert "SECRET_BODY" not in first


def test_memory_config_validates_local_absolute_root():
    config = MemoryConfig(
        experiment_id="run-1", memory_root=Path("/tmp/llamar-mem")
    ).validate()
    assert config.project_id == "llamar"
    assert config.db_path == Path("/tmp/llamar-mem/memory/memory.sqlite3")

    with pytest.raises(MemoryConfigError) as exc:
        MemoryConfig(experiment_id="run-1", memory_root=Path("relative/mem")).validate()
    assert exc.value.code == "invalid_memory_root"

    with pytest.raises(MemoryConfigError) as exc:
        MemoryConfig(
            experiment_id="run-1", memory_root=Path("s3://bucket/mem")
        ).validate()
    assert exc.value.code == "invalid_memory_root"

    with pytest.raises(MemoryConfigError):
        MemoryConfig(experiment_id="", memory_root=Path("/tmp/llamar-mem")).validate()
