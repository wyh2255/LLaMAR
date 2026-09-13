"""Phase 3 projection reducer: normalized Worker evidence -> Spatial / Embodied
projection, executed inside the same canonical transaction (Temporal first,
reducer second).

The reducer implements the H1 comparison order per field:

1. scope / epoch fence          (enforced by the ingestor before this module)
2. env_step                    (older evidence never regresses; missing step
                               stays Temporal-only)
3. field source class priority (per-field policy, never whole-event)
4. confidence
5. sequence / event_id          (ordering only, never a truth tie-breaker)

Visible outcomes: ``material``, ``ignored_out_of_order``,
``superseded_by_higher_authority``, ``conflicted``, ``no_change``.  Only
material and conflicted change the current visible projection, so only they
advance entity / view revisions and ``as_of_sequence``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from a2a.coordinator.memory.contracts import (
    MemoryRef,
    MemoryRelation,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.store import MemoryStore

__all__ = [
    "MemoryProjectionReducer",
    "ProjectionFieldResult",
    "ProjectionIngestResult",
    "ProjectionOutcome",
]


class ProjectionOutcome(str, Enum):
    MATERIAL = "material"
    IGNORED_OUT_OF_ORDER = "ignored_out_of_order"
    SUPERSEDED_BY_HIGHER_AUTHORITY = "superseded_by_higher_authority"
    CONFLICTED = "conflicted"
    NO_CHANGE = "no_change"


@dataclass(frozen=True)
class ProjectionFieldResult:
    """Result of reducing one field claim."""

    field_name: str
    outcome: ProjectionOutcome
    entity_revision: int | None = None
    snapshot_revision: int | None = None
    as_of_sequence: int | None = None
    superseded_event_id: str | None = None
    visible_change: bool = False


@dataclass(frozen=True)
class ProjectionIngestResult:
    """Result of one canonical projection ingest bundle."""

    status: str  # ok | duplicate | online_truth_forbidden | scope_closed |
    # unknown_scope | mixed_scope_bundle | mixed_epoch_bundle |
    # runtime_epoch_mismatch | invalid_input
    event_ids: tuple[str, ...] = ()
    committed_revision: int | None = None
    outcomes: tuple[ProjectionFieldResult, ...] = ()
    reason: str = ""


def _value_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _values_equal(a: Any, b: Any) -> bool:
    return _value_json(a) == _value_json(b)


class MemoryProjectionReducer:
    """Deterministic field-level reducer over a canonical store transaction.

    All methods must be called while a ``canonical_transaction()`` is open on
    the store; none of them commit on their own.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def reduce(
        self, inp: NormalizedProjectionInputV1, event_id: str
    ) -> ProjectionFieldResult:
        current = self._store.get_projection_field(
            inp.scope_id, inp.domain, inp.entity_id, inp.field_name
        )
        if current is None:
            return self._materialize(inp, event_id, superseded_event_id=None)

        cur_step = current.get("env_step")
        in_step = inp.env_step

        # 2. env_step fence for scene-timed fields.  A missing env_step or an
        # older env_step can never overwrite an existing physical field.
        if cur_step is not None and (in_step is None or in_step < cur_step):
            return self._ignored(inp, event_id)

        if cur_step is None or in_step > cur_step:
            return self._materialize(inp, event_id, current.get("event_id"))

        # 3. same env_step: field source priority (per-field policy).
        in_prio = inp.source_priority
        cur_prio = int(current["source_priority"])
        if in_prio < cur_prio:
            return self._materialize(inp, event_id, current.get("event_id"))
        if in_prio > cur_prio:
            return self._superseded(inp, event_id, current)

        # 4. equal priority: confidence.
        if inp.confidence > float(current["confidence"]):
            return self._materialize(inp, event_id, current.get("event_id"))
        if inp.confidence < float(current["confidence"]):
            return self._superseded(inp, event_id, current)

        # 5. equal priority + equal confidence: value equality decides; a tie
        # with a different value is an explicit conflict (sequence never picks
        # a winner).
        if _values_equal(current["value"], inp.value):
            return self._no_change(inp, event_id)
        return self._conflict(inp, event_id, current)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _relation_type(self, inp: NormalizedProjectionInputV1) -> str:
        return "observed_by" if inp.domain == "embodied" else "about"

    def _add_relation(
        self,
        inp: NormalizedProjectionInputV1,
        event_id: str,
        relation_type: str,
    ) -> None:
        # ``event_id`` is the canonical Temporal event UUID; the relation points
        # at it so Temporal -> Spatial/Embodied joins are stable.  The external
        # evidence identity is retained in the relation source_event_id audit
        # column? No: source_event_id *is* the canonical event; the evidence id
        # stays on the Temporal event (causation_id) and in projection rows.
        self._store.add_relation_in_tx(
            MemoryRelation(
                relation_id=f"rel_{uuid.uuid4().hex}",
                scope_id=inp.scope_id,
                from_ref=MemoryRef(namespace="memory", id=event_id),
                relation_type=relation_type,
                to_ref=MemoryRef(
                    namespace="memory", id=f"{inp.domain}:{inp.entity_id}"
                ),
                source_event_id=event_id,
            )
        )

    def _record_outcome(
        self,
        inp: NormalizedProjectionInputV1,
        event_id: str,
        outcome: ProjectionOutcome,
        *,
        superseded_event_id: str | None = None,
    ) -> None:
        self._store.record_projection_outcome(
            scope_id=inp.scope_id,
            event_id=event_id,
            evidence_id=inp.event_id,
            domain=inp.domain,
            entity_id=inp.entity_id,
            field_name=inp.field_name,
            outcome=outcome.value,
            superseded_event_id=superseded_event_id,
        )

    def _candidate_record(
        self,
        *,
        event_id: str,
        evidence_id: str | None,
        provenance: str,
        confidence: float,
        value: Any,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "evidence_id": evidence_id,
            "provenance": provenance,
            "confidence": confidence,
            "value": value,
        }

    def _materialize(
        self,
        inp: NormalizedProjectionInputV1,
        event_id: str,
        superseded_event_id: str | None,
    ) -> ProjectionFieldResult:
        entity_rev = self._store.bump_entity_revision(
            inp.scope_id, inp.domain, inp.entity_id, inp.entity_type, inp.sequence
        )
        snap_rev = self._store.bump_view_revision(inp.scope_id)
        self._store.upsert_projection_field(
            scope_id=inp.scope_id,
            domain=inp.domain,
            entity_id=inp.entity_id,
            entity_type=inp.entity_type,
            field_name=inp.field_name,
            value=_value_json(inp.value),
            env_step=inp.env_step,
            provenance=inp.provenance,
            source_priority=inp.source_priority,
            confidence=inp.confidence,
            event_id=event_id,
            evidence_id=inp.event_id,
            sequence=inp.sequence,
            outcome=ProjectionOutcome.MATERIAL.value,
            conflict_event_ids=None,
            conflict_candidates=None,
            superseded_event_id=superseded_event_id,
        )
        self._record_outcome(
            inp,
            event_id,
            ProjectionOutcome.MATERIAL,
            superseded_event_id=superseded_event_id,
        )
        self._add_relation(inp, event_id, self._relation_type(inp))
        return ProjectionFieldResult(
            field_name=inp.field_name,
            outcome=ProjectionOutcome.MATERIAL,
            entity_revision=entity_rev,
            snapshot_revision=snap_rev,
            as_of_sequence=inp.sequence,
            superseded_event_id=superseded_event_id,
            visible_change=True,
        )

    def _ignored(
        self, inp: NormalizedProjectionInputV1, event_id: str
    ) -> ProjectionFieldResult:
        self._record_outcome(inp, event_id, ProjectionOutcome.IGNORED_OUT_OF_ORDER)
        self._add_relation(inp, event_id, "ignored_out_of_order")
        return ProjectionFieldResult(
            field_name=inp.field_name,
            outcome=ProjectionOutcome.IGNORED_OUT_OF_ORDER,
            visible_change=False,
        )

    def _superseded(
        self,
        inp: NormalizedProjectionInputV1,
        event_id: str,
        current: dict[str, Any],
    ) -> ProjectionFieldResult:
        superseded_event_id = current.get("event_id")
        self._record_outcome(
            inp,
            event_id,
            ProjectionOutcome.SUPERSEDED_BY_HIGHER_AUTHORITY,
            superseded_event_id=superseded_event_id,
        )
        self._add_relation(inp, event_id, "superseded_by_higher_authority")
        return ProjectionFieldResult(
            field_name=inp.field_name,
            outcome=ProjectionOutcome.SUPERSEDED_BY_HIGHER_AUTHORITY,
            superseded_event_id=superseded_event_id,
            visible_change=False,
        )

    def _conflict(
        self,
        inp: NormalizedProjectionInputV1,
        event_id: str,
        current: dict[str, Any],
    ) -> ProjectionFieldResult:
        # The existing value stays the current holder: its own event/provenance/
        # confidence/sequence are preserved and never relabelled with the
        # incoming candidate.  Both canonical event ids are tracked explicitly
        # as conflict candidates (with provenance) without selecting a winner.
        current_event = current.get("event_id") or event_id
        conflict_ids: list[str] = list(current.get("conflict_event_ids") or [])
        if current_event not in conflict_ids:
            conflict_ids.append(current_event)
        if event_id not in conflict_ids:
            conflict_ids.append(event_id)

        candidates = list(current.get("conflict_candidates") or [])
        candidate_ids = {
            str(c.get("event_id")) for c in candidates if isinstance(c, dict)
        }
        if current_event not in candidate_ids:
            candidates.append(
                self._candidate_record(
                    event_id=current_event,
                    evidence_id=current.get("evidence_id"),
                    provenance=current.get("provenance", inp.provenance),
                    confidence=float(current.get("confidence", inp.confidence)),
                    value=current.get("value"),
                )
            )
        if event_id not in candidate_ids:
            candidates.append(
                self._candidate_record(
                    event_id=event_id,
                    evidence_id=inp.event_id,
                    provenance=inp.provenance,
                    confidence=inp.confidence,
                    value=inp.value,
                )
            )

        entity_rev = self._store.bump_entity_revision(
            inp.scope_id, inp.domain, inp.entity_id, inp.entity_type, inp.sequence
        )
        snap_rev = self._store.bump_view_revision(inp.scope_id)
        self._store.upsert_projection_field(
            scope_id=inp.scope_id,
            domain=inp.domain,
            entity_id=inp.entity_id,
            entity_type=inp.entity_type,
            field_name=inp.field_name,
            value=_value_json(current["value"]),
            env_step=current.get("env_step"),
            provenance=current.get("provenance", inp.provenance),
            source_priority=int(current.get("source_priority", inp.source_priority)),
            confidence=float(current.get("confidence", inp.confidence)),
            event_id=current_event,
            evidence_id=current.get("evidence_id") or inp.event_id,
            sequence=int(current.get("sequence", inp.sequence)),
            outcome=ProjectionOutcome.CONFLICTED.value,
            conflict_event_ids=json.dumps(conflict_ids),
            conflict_candidates=json.dumps(candidates, ensure_ascii=False, default=str),
            superseded_event_id=None,
        )
        self._record_outcome(inp, event_id, ProjectionOutcome.CONFLICTED)
        self._add_relation(inp, event_id, self._relation_type(inp))
        return ProjectionFieldResult(
            field_name=inp.field_name,
            outcome=ProjectionOutcome.CONFLICTED,
            entity_revision=entity_rev,
            snapshot_revision=snap_rev,
            as_of_sequence=inp.sequence,
            visible_change=True,
        )

    def _no_change(
        self, inp: NormalizedProjectionInputV1, event_id: str
    ) -> ProjectionFieldResult:
        self._record_outcome(inp, event_id, ProjectionOutcome.NO_CHANGE)
        return ProjectionFieldResult(
            field_name=inp.field_name,
            outcome=ProjectionOutcome.NO_CHANGE,
            visible_change=False,
        )
