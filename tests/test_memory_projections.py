"""Phase 3 projection contracts: C1-C6 normalized Worker evidence reduction.

Each accepted evidence is Temporal-first (one TemporalEvent per evidence
bundle) and reducer-second (field-level Spatial/Embodied projection update
inside the same canonical transaction).  Tests assert:

C1 - a structured Worker observation forms the current fact and advances
     canonical / entity / view revisions and as_of_sequence.
C2 - late older-step evidence only enters Temporal audit (ignored_out_of_order)
     and never regresses the current field or any revision.
C3 - same-step, same-priority, same-confidence conflicting evidence is marked
     CONFLICTED (both candidates visible; sequence never picks a winner).
C4 - cross-epoch / closed-scope callbacks are typed ``scope_closed`` /
     ``unknown_scope`` with zero domain writes and no scope reopen.
C5 - field-level source policy partially materializes one event per field
     (higher-priority claim wins; superseded_by_higher_authority audit).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig, NormalizedProjectionInputV1
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _scope_id_of(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


def _input(
    scope_id,
    *,
    event_id="evt_1",
    domain="spatial",
    entity_id="FireA",
    entity_type="fire",
    field_name="intensity",
    value: Any = "High",
    env_step: int | None = 8,
    provenance="worker_sensor_tool",
    confidence=1.0,
    actor_id="alice",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id=actor_id,
        provenance=provenance,
        domain=domain,
        entity_id=entity_id,
        entity_type=entity_type,
        field_name=field_name,
        value=value,
        confidence=confidence,
    )


def _canonical_event_id(store, scope_id, evidence_id):
    """The minted canonical Temporal event UUID for a given evidence identity."""
    for evt in store.temporal_events(scope_id):
        payload = json.loads(evt["payload"])
        if payload.get("evidence_id") == evidence_id:
            return evt["event_id"]
    raise AssertionError(f"no temporal evidence event for evidence_id={evidence_id!r}")


# ---------------------------------------------------------------------------
# C1 - structured Worker observation forms the current fact
# ---------------------------------------------------------------------------


def test_c1_structured_observation_forms_current_fact(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    inp = _input(scope_id, field_name="intensity", value="High", env_step=8)
    result = ingestor.ingest_projection([inp])

    assert result.status == "ok"
    assert result.committed_revision == 1
    assert result.outcomes and result.outcomes[0].outcome == "material"

    # Temporal first: exactly one evidence event.
    assert store.temporal_event_count(scope_id) == 1
    events = store.temporal_events(scope_id)
    assert events[0]["event_type"] == "evidence.projection"
    assert json.loads(events[0]["payload"])["env_step"] == 8

    # Canonical memory_revision advances per accepted bundle.
    assert store.revision_of(scope_id) == 1

    # Projection field materialized with full provenance.  The field event_id
    # is the canonical Temporal event UUID (joinable), evidence_id is retained
    # separately.
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["outcome"] == "material"
    assert field["value"] == "High"
    assert field["env_step"] == 8
    assert field["provenance"] == "worker_sensor_tool"
    assert field["event_id"] == _canonical_event_id(store, scope_id, "evt_1")
    assert field["evidence_id"] == "evt_1"

    # Entity revision + as_of_sequence + view snapshot revision advance.
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 1
    assert store.entity_as_of_sequence(scope_id, "spatial", "FireA") == 1
    assert store.view_revision_of(scope_id) == 1

    # Relation traceable from Temporal -> Spatial entity using the same
    # canonical event id (Temporal -> projection join works).
    rels = store.relations_for_scope(scope_id)
    rel = next(r for r in rels if r.relation_type == "about")
    assert rel.source_event_id == field["event_id"]
    assert rel.from_ref.id == field["event_id"]
    assert rel.from_ref.namespace == "memory"
    assert rel.to_ref.id == "spatial:FireA"

    # No MissionRuntime / control-plane mutation.
    assert store.control_receipts(scope_id) == []


def test_c1_embodied_node_field_uses_observed_by_relation(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    inp = _input(
        scope_id,
        event_id="evt_pos",
        domain="embodied",
        entity_id="Alice",
        entity_type="agent",
        field_name="position",
        value=[3, 4, 0],
        env_step=8,
    )
    result = ingestor.ingest_projection([inp])
    assert result.status == "ok"

    field = store.projection_field(scope_id, "embodied", "Alice", "position")
    assert field["value"] == [3, 4, 0]
    assert field["event_id"] == _canonical_event_id(store, scope_id, "evt_pos")
    assert field["evidence_id"] == "evt_pos"
    assert store.entity_revision_of(scope_id, "embodied", "Alice") == 1
    assert store.view_revision_of(scope_id) == 1

    rels = store.relations_for_scope(scope_id)
    assert any(
        r.relation_type == "observed_by" and r.source_event_id == field["event_id"]
        for r in rels
    )


# ---------------------------------------------------------------------------
# C2 - late older-step evidence only enters Temporal audit
# ---------------------------------------------------------------------------


def test_c2_late_evidence_only_temporal_audit(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection([_input(scope_id, value="High", env_step=8)]).status
        == "ok"
    )

    result = ingestor.ingest_projection(
        [_input(scope_id, value="Low", env_step=5, event_id="evt_late")]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "ignored_out_of_order"

    # Temporal + canonical advance.
    assert store.temporal_event_count(scope_id) == 2
    assert store.revision_of(scope_id) == 2

    # Current field unchanged.
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["outcome"] == "material"
    assert field["value"] == "High"
    assert field["env_step"] == 8

    # Entity revision / as_of_sequence / view revision unchanged.
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 1
    assert store.entity_as_of_sequence(scope_id, "spatial", "FireA") == 1
    assert store.view_revision_of(scope_id) == 1

    # Audit-visible ignored_out_of_order relation, tied to the late event's
    # canonical id.
    rels = store.relations_for_scope(scope_id)
    late_id = _canonical_event_id(store, scope_id, "evt_late")
    assert any(
        r.relation_type == "ignored_out_of_order" and r.source_event_id == late_id
        for r in rels
    )


def test_c2_evidence_without_env_step_never_overwrites_current_field(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection([_input(scope_id, value="High", env_step=8)]).status
        == "ok"
    )

    # A report without env_step is Temporal evidence only; it must not regress
    # the existing physical field.
    result = ingestor.ingest_projection(
        [_input(scope_id, value="Low", env_step=None, event_id="evt_nostep")]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "ignored_out_of_order"
    assert store.temporal_event_count(scope_id) == 2

    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["value"] == "High"
    assert field["env_step"] == 8
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 1
    assert store.view_revision_of(scope_id) == 1


# ---------------------------------------------------------------------------
# C3 - same-step same-priority same-confidence conflict is explicit
# ---------------------------------------------------------------------------


def test_c3_same_step_same_priority_conflict_is_explicit(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection(
            [
                _input(
                    scope_id,
                    value="High",
                    env_step=6,
                    event_id="evt_a",
                    actor_id="alice",
                )
            ]
        ).status
        == "ok"
    )

    result = ingestor.ingest_projection(
        [_input(scope_id, value="Low", env_step=6, event_id="evt_b", actor_id="bob")]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "conflicted"

    evt_a_id = _canonical_event_id(store, scope_id, "evt_a")
    evt_b_id = _canonical_event_id(store, scope_id, "evt_b")
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["outcome"] == "conflicted"
    # Both candidates are visible in the default conflict / evidence view using
    # canonical Temporal event ids.
    assert set(field["conflict_event_ids"]) == {evt_a_id, evt_b_id}
    # The current value keeps its ORIGINAL holder (evt_a), never relabelled
    # with the incoming candidate.
    assert field["event_id"] == evt_a_id
    assert field["evidence_id"] == "evt_a"
    assert field["provenance"] == "worker_sensor_tool"

    # Explicit conflict candidates/provenance stored without selecting a winner.
    candidates = {c["event_id"]: c for c in field["conflict_candidates"]}
    assert set(candidates) == {evt_a_id, evt_b_id}
    assert candidates[evt_a_id]["evidence_id"] == "evt_a"
    assert candidates[evt_b_id]["evidence_id"] == "evt_b"
    assert candidates[evt_a_id]["value"] == "High"
    assert candidates[evt_b_id]["value"] == "Low"
    assert candidates[evt_b_id]["provenance"] == "worker_sensor_tool"
    assert candidates[evt_b_id]["confidence"] == 1.0

    # Entity revision + as_of_sequence + snapshot revision advance.
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 2
    assert store.entity_as_of_sequence(scope_id, "spatial", "FireA") == 2
    assert store.view_revision_of(scope_id) == 2

    # FRESH is orthogonal to CONFLICTED: a later fresh event does not reset the
    # conflict state by sequence order alone.
    assert store.temporal_event_count(scope_id) == 2


def test_c3_equal_value_is_no_change_not_conflict(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection(
            [_input(scope_id, value="High", env_step=6, event_id="evt_a")]
        ).status
        == "ok"
    )
    result = ingestor.ingest_projection(
        [_input(scope_id, value="High", env_step=6, event_id="evt_b")]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "no_change"
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["outcome"] == "material"
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 1


# ---------------------------------------------------------------------------
# C4 - cross-epoch / closed-scope callback zero domain writes
# ---------------------------------------------------------------------------


def test_c4_closed_scope_is_typed_and_writes_nothing(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    assert ingestor.close_scope("ctx-1", 0) is True

    result = ingestor.ingest_projection([_input(scope_id, event_id="evt_after_close")])
    assert result.status == "scope_closed"

    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.relations_for_scope(scope_id) == []
    assert store.outbox_entries(scope_id) == []


def test_c4_unknown_scope_is_typed_and_writes_nothing(ingestor, store, scope_factory):
    unknown_scope = scope_factory.resolve("ctx-99", 9).scope_id
    result = ingestor.ingest_projection([_input(unknown_scope, event_id="evt_ghost")])
    assert result.status == "unknown_scope"
    # The unknown scope is never created; no projection/revision/outbox writes.
    assert store.get_scope(unknown_scope) is None
    assert store.projection_fields(unknown_scope) == []
    assert store.revision_of(unknown_scope) == 0
    assert store.outbox_entries(unknown_scope) == []


def test_c4_closed_scope_is_never_reopened(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    ingestor.close_scope("ctx-1", 0)
    with pytest.raises(Exception, match="scope_tuple_reuse"):
        ingestor.activate_runtime_scope("ctx-1", 0)
    # Old scope callback still refused after the failed reopen.
    result = ingestor.ingest_projection([_input(scope_id, event_id="evt_again")])
    assert result.status == "scope_closed"
    assert store.temporal_event_count(scope_id) == 0


# ---------------------------------------------------------------------------
# C5 - field-level Worker evidence policy (partial materialization)
# ---------------------------------------------------------------------------


def test_c5_higher_priority_field_claim_wins_and_lower_is_superseded(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    # Higher-priority structured local tool result first (inventory).
    assert (
        ingestor.ingest_projection(
            [
                _input(
                    scope_id,
                    event_id="evt_tool",
                    domain="embodied",
                    entity_id="Alice",
                    entity_type="agent",
                    field_name="inventory",
                    value={"water": 5},
                    env_step=8,
                    provenance="worker_sensor_tool",
                )
            ]
        ).status
        == "ok"
    )

    # Lower-priority free report at the same env_step claims a different value.
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_report",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="inventory",
                value={"water": 1},
                env_step=8,
                provenance="worker_observation",
            )
        ]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "superseded_by_higher_authority"

    field = store.projection_field(scope_id, "embodied", "Alice", "inventory")
    assert field["value"] == {"water": 5}
    assert field["outcome"] == "material"
    assert field["provenance"] == "worker_sensor_tool"
    assert field["event_id"] == _canonical_event_id(store, scope_id, "evt_tool")
    assert field["evidence_id"] == "evt_tool"

    rels = store.relations_for_scope(scope_id)
    report_id = _canonical_event_id(store, scope_id, "evt_report")
    assert any(
        r.relation_type == "superseded_by_higher_authority"
        and r.source_event_id == report_id
        for r in rels
    )
    # Lower-priority claim did not advance any revision.
    assert store.entity_revision_of(scope_id, "embodied", "Alice") == 1
    assert store.view_revision_of(scope_id) == 1


def test_c5_same_event_partially_materializes_per_field(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    # One authenticated event carries three field claims:
    #  - inventory: low-priority free report conflicts with the existing
    #    higher-priority tool result -> superseded.
    #  - battery / localization: telemetry is the field's highest online source.
    assert (
        ingestor.ingest_projection(
            [
                _input(
                    scope_id,
                    event_id="evt_tool",
                    domain="embodied",
                    entity_id="Alice",
                    entity_type="agent",
                    field_name="inventory",
                    value={"water": 5},
                    env_step=8,
                    provenance="worker_sensor_tool",
                )
            ]
        ).status
        == "ok"
    )

    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_telemetry",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="inventory",
                value={"water": 1},
                env_step=8,
                provenance="worker_observation",
            ),
            _input(
                scope_id,
                event_id="evt_telemetry",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="battery",
                value=0.9,
                env_step=8,
                provenance="worker_telemetry",
            ),
            _input(
                scope_id,
                event_id="evt_telemetry",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="localization_quality",
                value="good",
                env_step=8,
                provenance="worker_telemetry",
            ),
        ]
    )
    assert result.status == "ok"

    # inventory keeps the higher-priority tool result.
    inventory = store.projection_field(scope_id, "embodied", "Alice", "inventory")
    assert inventory["value"] == {"water": 5}
    assert inventory["outcome"] == "material"

    # battery / localization updated from telemetry, each with own provenance.
    battery = store.projection_field(scope_id, "embodied", "Alice", "battery")
    assert battery["value"] == 0.9
    assert battery["provenance"] == "worker_telemetry"

    loc = store.projection_field(scope_id, "embodied", "Alice", "localization_quality")
    assert loc["value"] == "good"
    assert loc["provenance"] == "worker_telemetry"

    # Node revision and view revision advance because visible fields changed.
    assert store.entity_revision_of(scope_id, "embodied", "Alice") == 3
    assert store.view_revision_of(scope_id) == 3

    # One event, one Temporal record.
    assert store.temporal_event_count(scope_id) == 2


def test_c5_newer_step_wins_regardless_of_priority(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection(
            [_input(scope_id, value="High", env_step=8, event_id="evt_a")]
        ).status
        == "ok"
    )
    # A lower-priority but newer-step observation wins on recency.
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                value="Low",
                env_step=10,
                event_id="evt_b",
                provenance="worker_observation",
            )
        ]
    )
    assert result.status == "ok"
    assert result.outcomes[0].outcome == "material"
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["value"] == "Low"
    assert field["env_step"] == 10


# ---------------------------------------------------------------------------
# Revision separation: canonical vs entity vs view
# ---------------------------------------------------------------------------


def test_canonical_entity_view_revisions_are_separate(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    # Two accepted bundles: canonical = 2, entity/view only advance once (late
    # evidence does not change the visible projection).
    assert (
        ingestor.ingest_projection([_input(scope_id, value="High", env_step=8)]).status
        == "ok"
    )
    assert (
        ingestor.ingest_projection([_input(scope_id, value="Low", env_step=5)]).status
        == "ok"
    )

    assert store.revision_of(scope_id) == 2
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 1
    assert store.view_revision_of(scope_id) == 1

    # A materializing bundle advances all three.
    assert (
        ingestor.ingest_projection([_input(scope_id, value="Low", env_step=9)]).status
        == "ok"
    )
    assert store.revision_of(scope_id) == 3
    assert store.entity_revision_of(scope_id, "spatial", "FireA") == 2
    assert store.view_revision_of(scope_id) == 2


# ---------------------------------------------------------------------------
# Bundle fencing: mixed scope / epoch projection bundles are rejected before
# any domain write (H1 card §3.1 step 1)
# ---------------------------------------------------------------------------


def test_mixed_scope_bundle_is_rejected_with_zero_writes(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    other_scope = scope_factory.resolve("ctx-2", 0).scope_id
    result = ingestor.ingest_projection(
        [
            _input(scope_id, event_id="evt_a", value="High"),
            _input(other_scope, event_id="evt_b", value="Low"),
        ]
    )
    assert result.status == "mixed_scope_bundle"
    # Nothing was created in either scope.
    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.outbox_entries(scope_id) == []


def test_mixed_runtime_epoch_bundle_is_rejected(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_a",
                sequence=0,
                env_step=8,
                actor_id="alice",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="FireA",
                entity_type="fire",
                field_name="intensity",
                value="High",
                runtime_epoch=0,
            ),
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_b",
                sequence=0,
                env_step=8,
                actor_id="bob",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="FireA",
                entity_type="fire",
                field_name="intensity",
                value="Low",
                runtime_epoch=1,
            ),
        ]
    )
    assert result.status == "mixed_epoch_bundle"
    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0


def test_runtime_epoch_mismatch_is_rejected(ingestor, store, scope_factory):
    """A single provided epoch that contradicts the scope's stored epoch is a
    typed ``runtime_epoch_mismatch`` with zero domain writes."""
    scope_id = _scope_id_of(scope_factory)
    inp = NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id="evt_wrong_epoch",
        sequence=0,
        env_step=8,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain="spatial",
        entity_id="FireA",
        entity_type="fire",
        field_name="intensity",
        value="High",
        runtime_epoch=7,
    )
    result = ingestor.ingest_projection([inp])
    assert result.status == "runtime_epoch_mismatch"
    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.projection_fields(scope_id) == []


def test_matching_runtime_epoch_is_accepted(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    inp = NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id="evt_epoch_ok",
        sequence=0,
        env_step=8,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain="spatial",
        entity_id="FireA",
        entity_type="fire",
        field_name="intensity",
        value="High",
        runtime_epoch=0,
    )
    result = ingestor.ingest_projection([inp])
    assert result.status == "ok"
    assert store.temporal_event_count(scope_id) == 1


# ---------------------------------------------------------------------------
# Projection bundle idempotency: a repeated evidence bundle is a typed
# duplicate and never creates another Temporal / revision / outbox row
# ---------------------------------------------------------------------------


def test_repeated_evidence_bundle_is_typed_duplicate_with_no_new_rows(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    bundle = [
        _input(scope_id, event_id="evt_once", value="High", env_step=8),
        _input(
            scope_id,
            event_id="evt_once",
            domain="embodied",
            entity_id="Alice",
            entity_type="agent",
            field_name="position",
            value=[1, 2, 0],
            env_step=8,
        ),
    ]
    first = ingestor.ingest_projection(bundle)
    assert first.status == "ok"
    assert store.temporal_event_count(scope_id) == 1
    revision_after_first = store.revision_of(scope_id)
    outbox_after_first = len(store.outbox_entries(scope_id))

    second = ingestor.ingest_projection(bundle)
    assert second.status == "duplicate"
    assert second.event_ids == first.event_ids
    assert second.committed_revision == first.committed_revision

    # No new Temporal event, revision bump, or outbox row.
    assert store.temporal_event_count(scope_id) == 1
    assert store.revision_of(scope_id) == revision_after_first
    assert len(store.outbox_entries(scope_id)) == outbox_after_first

    # A DIFFERENT bundle (new evidence) is still accepted.
    third = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_again",
                value="Low",
                env_step=9,
            )
        ]
    )
    assert third.status == "ok"
    assert store.temporal_event_count(scope_id) == 2


def test_repeated_bundle_keeps_single_evidence_event_and_projection(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    bundle = [_input(scope_id, event_id="evt_sticky", value="High", env_step=8)]
    assert ingestor.ingest_projection(bundle).status == "ok"
    assert ingestor.ingest_projection(bundle).status == "duplicate"

    events = store.temporal_events(scope_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "evidence.projection"
    fields = store.projection_fields(scope_id)
    assert len(fields) == 1
    assert fields[0]["value"] == "High"


# ---------------------------------------------------------------------------
# Correlation identity: authenticated correlation wins; otherwise a
# deterministic evidence correlation is used — never a fabricated dispatch id
# ---------------------------------------------------------------------------


def test_authenticated_correlation_is_preserved(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    inp = NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id="evt_corr",
        sequence=0,
        env_step=8,
        actor_id="alice",
        provenance="worker_sensor_tool",
        domain="spatial",
        entity_id="FireA",
        entity_type="fire",
        field_name="intensity",
        value="High",
        dispatch_id="dsp_42",
        worker_task_id="worker-1",
        correlation_id="dispatch:dsp_42",
    )
    result = ingestor.ingest_projection([inp])
    assert result.status == "ok"

    events = store.temporal_events(scope_id)
    assert events[0]["correlation_id"] == "dispatch:dsp_42"
    assert events[0]["dispatch_id"] == "dsp_42"
    assert events[0]["worker_task_id"] == "worker-1"
    # External evidence identity retained for causation/audit.
    assert events[0]["causation_id"] == "evidence:evt_corr"


def test_evidence_correlation_never_claims_a_fabricated_dispatch_id(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_nocorr",
                sequence=0,
                env_step=8,
                actor_id="alice",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="FireA",
                entity_type="fire",
                field_name="intensity",
                value="High",
            )
        ]
    )
    assert result.status == "ok"
    events = store.temporal_events(scope_id)
    corr = events[0]["correlation_id"]
    # Deterministic evidence correlation — never a fake dispatch: prefix.
    assert corr.startswith("evidence:")
    assert not corr.startswith("dispatch:")


# ---------------------------------------------------------------------------
# Temporal -> projection join: one canonical event id everywhere
# ---------------------------------------------------------------------------


def test_projection_field_joins_to_temporal_event(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    assert (
        ingestor.ingest_projection(
            [_input(scope_id, event_id="evt_join", value="High", env_step=8)]
        ).status
        == "ok"
    )
    canonical = _canonical_event_id(store, scope_id, "evt_join")
    temporal = {e["event_id"]: e for e in store.temporal_events(scope_id)}[canonical]
    assert temporal["event_type"] == "evidence.projection"

    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    # Every projection reference uses the same canonical Temporal event id.
    assert field["event_id"] == canonical
    assert field["evidence_id"] == "evt_join"
    rels = store.relations_for_scope(scope_id)
    assert any(
        r.source_event_id == canonical
        and r.from_ref.id == canonical
        and r.to_ref.id == "spatial:FireA"
        for r in rels
    )
    outcomes = store.projection_outcomes(scope_id)
    assert any(o["event_id"] == canonical for o in outcomes)
