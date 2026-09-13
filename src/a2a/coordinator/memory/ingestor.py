"""Phase 2 MemoryIngestor — sole mutation owner for authenticated callbacks,
control-lifecycle receipts, and trusted supervision events.

Canonical writes happen in a single ``BEGIN IMMEDIATE`` SQLite transaction:
idempotency ledger + temporal event + projection/revision + outbox are all-or-
nothing.  The ingestor never mutates MissionRuntime; it only consumes the
runtime's acceptance/rejection result and journal receipts.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from a2a.coordinator.memory.contracts import (
    ONLINE_PROVENANCE_ALLOWLIST,
    ControlTransitionJournalEntry,
    MemoryConfig,
    MemoryContractError,
    MemoryScopeV1,
    NormalizedProjectionInputV1,
    canonical_json_bytes,
    digest_bytes,
    digest_payload,
    online_truth_forbidden,
    scan_forbidden_truth_fields,
)
from a2a.coordinator.memory.projections import (
    MemoryProjectionReducer,
    ProjectionIngestResult,
)
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore, ScopeActivationStatus

logger = logging.getLogger(__name__)


def callback_idempotency_key(
    scope_id: str,
    dispatch_id: str,
    worker_task_id: str,
    callback_kind: str,
    normalized_state: str,
    body_sha256: str,
) -> str:
    return digest_bytes(
        canonical_json_bytes(
            {
                "scope_id": scope_id,
                "dispatch_id": dispatch_id,
                "worker_task_id": worker_task_id,
                "callback_kind": callback_kind,
                "normalized_state": normalized_state,
                "body_sha256": body_sha256,
            }
        )
    )


def control_idempotency_key(scope_id: str, journal_sha256: str) -> str:
    return digest_bytes(canonical_json_bytes(["control", scope_id, journal_sha256]))


def supervision_idempotency_key(scope_id: str, event_id: str) -> str:
    return digest_bytes(canonical_json_bytes(["supervision", scope_id, event_id]))


def projection_idempotency_key(
    scope_id: str, inputs: list[NormalizedProjectionInputV1]
) -> str:
    """Deterministic identity of one projection evidence bundle.

    Derived from the canonical scope and the full ordered evidence identity
    (evidence event_id + domain/entity/field + redacted value digest), so the
    same evidence bundle always maps to the same key and a repeated bundle
    returns a typed duplicate with zero new Temporal / revision / outbox rows.
    """
    evidence = sorted(
        (
            inp.event_id,
            inp.env_step,
            inp.domain,
            inp.entity_id,
            inp.entity_type,
            inp.field_name,
            digest_payload(inp.value),
        )
        for inp in inputs
    )
    return digest_bytes(canonical_json_bytes(["projection", scope_id, evidence]))


def coordinator_decision_idempotency_key(
    scope_id: str,
    event_type: str,
    correlation_id: str,
    worker_task_id: str,
    content: str,
) -> str:
    """Deterministic identity of one coordinator decision (main plan §3.1).

    Reuses the canonical-JSON SHA-256 primitive (contracts.py:308-324):
    ``digest_bytes(canonical_json_bytes([...]))`` with a dedicated
    ``"coordinator_decision"`` namespace, the canonical scope and the
    decision identity (event_type + correlation/worker ids + full content),
    so the same decision always maps to the same key and a replayed
    ``_log_send_message`` / ``update_plan`` call claims the original event
    via ``idempotency_ledger`` with zero duplicate Temporal rows.
    """
    return digest_bytes(
        canonical_json_bytes(
            [
                "coordinator_decision",
                scope_id,
                event_type,
                correlation_id,
                worker_task_id,
                content,
            ]
        )
    )


def evidence_correlation_id(
    inp: NormalizedProjectionInputV1, scope_id: str
) -> str:
    """Authenticated correlation when available; otherwise a deterministic
    evidence correlation that never claims a dispatch ID."""
    if inp.correlation_id:
        return inp.correlation_id
    return f"evidence:{scope_id[:16]}:{inp.event_id[:16]}"


def truth_scan_denied(inputs: list[NormalizedProjectionInputV1]) -> str | None:
    """Return ``online_truth_forbidden`` when any value masks a forbidden term.

    H1-INV-1 gate #2: even an allowlisted Worker provenance may carry an
    oracle / ground_truth / direct-world field inside its value.  The returned
    marker is generic (never echoes the raw term or value).
    """
    for inp in inputs:
        if scan_forbidden_truth_fields(inp.value):
            return online_truth_forbidden
    return None


class IdempotencyConflictError(RuntimeError):
    """Same idempotency key, different canonical payload digest."""

    code = "idempotency_conflict"


class MemoryScopeReuseError(RuntimeError):
    """Admission-time fail-closed: a closed scope tuple must never be reopened.

    The caller must generate a new ``context_id``; reopening a closed scope is a
    durable fence violation and would pollute the canonical projection.
    """

    code = "scope_tuple_reuse"

    def __init__(self, scope_id: str, context_id: str, runtime_epoch: int) -> None:
        self.scope_id = scope_id
        self.context_id = context_id
        self.runtime_epoch = runtime_epoch
        super().__init__(
            "scope_tuple_reuse: closed memory scope cannot be reopened "
            f"(context_id={context_id}, runtime_epoch={runtime_epoch})"
        )


class ControlReceiptConflictError(RuntimeError):
    """Same control transition, different journal digest."""

    code = "control_receipt_conflict"


@dataclass(frozen=True)
class AuthenticatedCallbackEnvelope:
    """Trusted identity derived from control state, never from the body."""

    scope_id: str
    dispatch_id: str
    worker_task_id: str
    actor_id: str
    runtime_epoch: int
    callback_kind: str
    normalized_state: str = ""
    body_sha256: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    idempotency_key: str = ""

    @classmethod
    def build(
        cls,
        *,
        scope_id: str,
        dispatch_id: str,
        worker_task_id: str,
        actor_id: str,
        runtime_epoch: int,
        callback_kind: str,
        normalized_state: str,
        body_sha256: str,
    ) -> AuthenticatedCallbackEnvelope:
        correlation_id = f"dispatch:{dispatch_id}"
        causation_id = f"callback:{worker_task_id}:{body_sha256[:16]}"
        key = callback_idempotency_key(
            scope_id,
            dispatch_id,
            worker_task_id,
            callback_kind,
            normalized_state,
            body_sha256,
        )
        return cls(
            scope_id=scope_id,
            dispatch_id=dispatch_id,
            worker_task_id=worker_task_id,
            actor_id=actor_id,
            runtime_epoch=runtime_epoch,
            callback_kind=callback_kind,
            normalized_state=normalized_state,
            body_sha256=body_sha256,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=key,
        )


@dataclass(frozen=True)
class CallbackIngestResult:
    status: str  # ok | duplicate | scope_closed | unknown_scope | online_truth_forbidden
    event_id: str | None = None
    receipt_sha256: str | None = None
    committed_revision: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class ControlReceiptResult:
    status: str  # ok | duplicate | scope_closed | unknown_scope
    event_id: str | None = None
    committed_revision: int | None = None
    reason: str = ""


class MemoryScopeFactory:
    """Derives canonical scopes from trusted Coordinator config + control state."""

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config

    def resolve(self, context_id: str, runtime_epoch: int) -> MemoryScopeV1:
        return MemoryScopeV1(
            project_id=self._config.project_id,
            experiment_id=self._config.experiment_id,
            context_id=context_id,
            runtime_epoch=runtime_epoch,
        )

    def activate(self, store: MemoryStore, context_id: str, runtime_epoch: int) -> str:
        result = store.activate_scope(self.resolve(context_id, runtime_epoch))
        if result.scope_id is None:  # pragma: no cover - activation always assigns
            raise RuntimeError("memory scope activation failed")
        return result.scope_id


class MemoryIngestor:
    """Consumes authenticated envelopes / journal receipts / supervision events.

    The only allowed domain writes are Temporal events plus the paired
    idempotency/control receipts, scope revision and outbox rows inside one
    canonical transaction.  Reducers (Spatial/Embodied projections) are Phase 3.
    """

    def __init__(
        self,
        store: MemoryStore,
        scope_factory: MemoryScopeFactory,
        *,
        redaction: RedactionPolicy | None = None,
    ) -> None:
        self._store = store
        self._scope_factory = scope_factory
        self._redaction = redaction or RedactionPolicy()
        self._bridge: MemoryLifecycleBridge | None = None
        self._active_bundle: list[ControlTransitionJournalEntry] | None = None

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def scope_factory(self) -> MemoryScopeFactory:
        return self._scope_factory

    @property
    def redaction(self) -> RedactionPolicy:
        return self._redaction

    def set_bridge(self, bridge: MemoryLifecycleBridge | None) -> None:
        self._bridge = bridge

    # ── scope lifecycle ────────────────────────────────────────────────

    def activate_scope(self, context_id: str, runtime_epoch: int) -> str:
        return self._scope_factory.activate(self._store, context_id, runtime_epoch)

    def activate_runtime_scope(self, context_id: str, runtime_epoch: int) -> str:
        """Activate the canonical scope for a newly admitted runtime.

        Derived strictly from trusted control state (admitted ``context_id`` +
        active runtime ``epoch``), never from callback data.  A closed tuple
        fails closed with ``MemoryScopeReuseError`` so the admission is rejected
        instead of reopening a closed scope.
        """
        scope = self._scope_factory.resolve(context_id, runtime_epoch)
        result = self._store.activate_scope(scope)
        if result.status is ScopeActivationStatus.SCOPE_TUPLE_REUSE:
            raise MemoryScopeReuseError(scope.scope_id, context_id, runtime_epoch)
        if result.scope_id is None:  # pragma: no cover - defensive
            raise MemoryScopeReuseError(scope.scope_id, context_id, runtime_epoch)
        return result.scope_id

    def close_scope(self, context_id: str, runtime_epoch: int) -> bool:
        scope = self._scope_factory.resolve(context_id, runtime_epoch)
        return self._store.close_scope(scope.scope_id)

    def scope_id_for(self, context_id: str, runtime_epoch: int) -> str:
        return self._scope_factory.resolve(context_id, runtime_epoch).scope_id

    # ── journal receipt seam ───────────────────────────────────────────

    @contextmanager
    def callback_bundle(self):
        """Capture callback-origin journal receipts for the in-flight bundle.

        While active, ``receipt_sink`` appends to the collector instead of
        enqueueing to the bridge, so a callback-origin transition is bundled
        once and never double-enqueued.
        """
        previous = self._active_bundle
        collector: list[ControlTransitionJournalEntry] = []
        self._active_bundle = collector
        try:
            yield collector
        finally:
            self._active_bundle = previous

    def receipt_sink(self, entry: ControlTransitionJournalEntry) -> None:
        """Lock-external runtime receipt seam installed on active MissionRuntime."""
        if self._active_bundle is not None:
            self._active_bundle.append(entry)
        elif self._bridge is not None:
            self._bridge.enqueue(entry)

    # ── canonical callback ingestion ───────────────────────────────────

    def ingest_callback(
        self,
        envelope: AuthenticatedCallbackEnvelope,
        sanitized_payload: dict[str, Any],
        control_receipts: list[ControlTransitionJournalEntry] | tuple = (),
        projection_inputs: list[NormalizedProjectionInputV1] | None = None,
    ) -> CallbackIngestResult:
        """Bundle callback idempotency + event + control receipts + outbox.

        When ``projection_inputs`` is provided the normalized Worker evidence
        is reduced in the SAME canonical transaction (Temporal first, reducer
        second), so the callback event, control receipts, evidence events and
        projection writes are all-or-nothing.  Any forbidden truth term in the
        evidence denies the whole bundle with zero domain writes.
        """
        projection_inputs = projection_inputs or []
        if truth_scan_denied(projection_inputs) is not None:
            self._store.record_security_audit(
                "online_truth_forbidden",
                reason=(
                    "online_truth_forbidden truth_field_scan "
                    f"scope_digest={digest_payload({'scope': envelope.scope_id})[:16]}"
                ),
                digest_prefix=digest_payload(
                    {"scope": envelope.scope_id}
                )[:16],
            )
            return CallbackIngestResult(
                online_truth_forbidden, reason=online_truth_forbidden
            )
        with self._store.canonical_transaction() as tx:
            if not tx.scope_exists(envelope.scope_id):
                return CallbackIngestResult("unknown_scope", reason="unknown_scope")
            if tx.scope_closed(envelope.scope_id):
                return CallbackIngestResult("scope_closed", reason="scope_closed")
            payload_digest = digest_payload(sanitized_payload)
            claim, existing = tx.claim_callback_idempotency(
                envelope.scope_id, envelope.idempotency_key, payload_digest
            )
            if claim == "conflict":
                raise IdempotencyConflictError(
                    "idempotency_conflict: same key, different payload digest"
                )
            if claim == "duplicate":
                assert existing is not None
                return CallbackIngestResult(
                    "duplicate",
                    event_id=existing["event_id"],
                    receipt_sha256=existing["receipt_sha256"],
                    committed_revision=existing["committed_revision"],
                )

            sequence = tx.next_sequence(envelope.scope_id)
            event_id = self._append_callback_event(
                tx, envelope, sanitized_payload, sequence
            )
            for entry in control_receipts:
                self._claim_and_append_control(tx, envelope.scope_id, entry)
            if projection_inputs:
                _evidence_event_ids, _evidence_outcomes = self._reduce_evidence_in_tx(
                    tx,
                    envelope.scope_id,
                    projection_inputs,
                    correlation_default=envelope.correlation_id,
                )
                tx.write_outbox(
                    outbox_id=tx.new_outbox_id(),
                    scope_id=envelope.scope_id,
                    event_id=_evidence_event_ids[0],
                    export_kind=f"evidence.{projection_inputs[0].domain}",
                    payload_sha256=digest_payload(
                        {
                            "event_ids": list(_evidence_event_ids),
                            "projection_inputs": [
                                inp.event_id for inp in projection_inputs
                            ],
                        }
                    ),
                )
            revision = tx.bump_revision_in_tx(envelope.scope_id)
            receipt_sha256 = digest_payload(
                {"event_id": event_id, "committed_revision": revision}
            )
            tx.insert_idempotency_ledger(
                scope_id=envelope.scope_id,
                idempotency_key=envelope.idempotency_key,
                event_id=event_id,
                payload_digest=payload_digest,
                receipt_sha256=receipt_sha256,
                committed_revision=revision,
            )
            tx.write_outbox(
                outbox_id=tx.new_outbox_id(),
                scope_id=envelope.scope_id,
                event_id=event_id,
                export_kind=f"temporal.{envelope.callback_kind}",
                payload_sha256=payload_digest,
            )
            return CallbackIngestResult(
                "ok",
                event_id=event_id,
                receipt_sha256=receipt_sha256,
                committed_revision=revision,
            )

    def _claim_and_append_control(
        self, tx: MemoryStore, scope_id: str, entry: ControlTransitionJournalEntry
    ) -> str | None:
        claim, existing = tx.claim_control_receipt(
            scope_id, entry.dispatch_id, entry.control_revision, entry.journal_sha256
        )
        if claim == "conflict":
            raise ControlReceiptConflictError(
                "control_receipt_conflict: journal digest mismatch"
            )
        if claim == "duplicate":
            return None  # matching receipt already exists; skip control branch
        sequence = tx.next_sequence(scope_id)
        event_id = self._append_control_event(tx, scope_id, entry, sequence)
        revision = tx.bump_revision_in_tx(scope_id)
        tx.insert_control_receipt(
            scope_id=scope_id,
            dispatch_id=entry.dispatch_id,
            control_revision=entry.control_revision,
            journal_sha256=entry.journal_sha256,
            event_id=event_id,
            committed_revision=revision,
        )
        tx.write_outbox(
            outbox_id=tx.new_outbox_id(),
            scope_id=scope_id,
            event_id=event_id,
            export_kind="control.lifecycle",
            payload_sha256=digest_payload(
                {"event_id": event_id, "committed_revision": revision}
            ),
        )
        return event_id

    # ── canonical control receipt ingestion (bridge / reconciliation) ──

    def ingest_control_receipt(
        self, entry: ControlTransitionJournalEntry
    ) -> ControlReceiptResult:
        """Write the control-lifecycle bundle for one durable journal receipt.

        Shared by the MemoryLifecycleBridge (internal-origin transitions) and
        restart reconciliation.  Matching receipts return the original event and
        never add a new event/outbox; digest mismatches fail loud.
        """
        with self._store.canonical_transaction() as tx:
            scope_id = self._scope_factory.resolve(
                entry.context_id, entry.runtime_epoch
            ).scope_id
            if not tx.scope_exists(scope_id):
                return ControlReceiptResult("unknown_scope", reason="unknown_scope")
            if tx.scope_closed(scope_id):
                return ControlReceiptResult("scope_closed", reason="scope_closed")
            event_id = self._claim_and_append_control(tx, scope_id, entry)
            if event_id is None:
                existing = tx.control_receipt_for(
                    scope_id, entry.dispatch_id, entry.control_revision
                )
                return ControlReceiptResult(
                    "duplicate",
                    event_id=existing["event_id"] if existing else None,
                    committed_revision=existing["committed_revision"]
                    if existing
                    else None,
                )
            revision = tx.revision_of(scope_id)
            return ControlReceiptResult(
                "ok", event_id=event_id, committed_revision=revision
            )

    # ── event append helpers ───────────────────────────────────────────

    def _append_callback_event(
        self,
        tx: MemoryStore,
        envelope: AuthenticatedCallbackEnvelope,
        sanitized_payload: dict[str, Any],
        sequence: int,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        payload_text = json.dumps(sanitized_payload, ensure_ascii=False, default=str)[
            :8000
        ]
        return tx.append_temporal_event(
            event_id=tx.new_event_id("evt"),
            scope_id=envelope.scope_id,
            sequence=sequence,
            event_type=f"callback.{envelope.callback_kind}",
            occurred_at=now,
            ingested_at=now,
            actor_id=envelope.actor_id,
            logical_task_id=None,
            dispatch_id=envelope.dispatch_id,
            worker_task_id=envelope.worker_task_id,
            tool_call_id=None,
            success=None,
            error=None,
            payload=payload_text,
            causation_id=envelope.causation_id,
            correlation_id=envelope.correlation_id,
            idempotency_key=envelope.idempotency_key,
        )

    def _append_control_event(
        self,
        tx: MemoryStore,
        scope_id: str,
        entry: ControlTransitionJournalEntry,
        sequence: int,
    ) -> str:
        ingested_at = datetime.now(timezone.utc).isoformat()
        return tx.append_temporal_event(
            event_id=tx.new_event_id("ctl"),
            scope_id=scope_id,
            sequence=sequence,
            event_type=f"control.{entry.source}.{entry.state}",
            occurred_at=entry.observed_at,
            ingested_at=ingested_at,
            actor_id="system",
            logical_task_id=None,
            dispatch_id=entry.dispatch_id,
            worker_task_id=None,
            tool_call_id=None,
            success=None,
            error=None,
            payload=json.dumps(
                {
                    "journal_sha256": entry.journal_sha256,
                    "result_digest": entry.result_digest,
                }
            ),
            causation_id=f"control:{entry.journal_sha256[:16]}",
            correlation_id=f"dispatch:{entry.dispatch_id}",
            idempotency_key=control_idempotency_key(scope_id, entry.journal_sha256),
        )

    # ── Phase 3: canonical projection ingestion (Temporal first, reducer second)

    def ingest_projection(
        self,
        inputs: list[NormalizedProjectionInputV1],
    ) -> ProjectionIngestResult:
        """Reduce normalized Worker evidence into Spatial/Embodied projections.

        H1-INV-1 gate: every candidate's provenance must be in
        :data:`ONLINE_PROVENANCE_ALLOWLIST` AND its value must not mask an
        oracle / ground_truth / direct-world field.  A single violation fails
        the whole bundle closed with a typed ``online_truth_forbidden`` result
        and zero domain writes (no Temporal event, projection, relation,
        revision or outbox); only a redacted security diagnostic is recorded.

        Bundle fencing: every input must share the same ``scope_id`` and, when
        provided, the same ``runtime_epoch`` that matches the scope.  Repeated
        evidence bundles (same canonical bundle identity) return a typed
        ``duplicate`` with no new Temporal / revision / outbox rows.

        Accepted evidence is Temporal-first (one TemporalEvent per evidence
        event_id) and reducer-second, all inside one canonical transaction.
        """
        if not inputs:
            return ProjectionIngestResult("invalid_input", reason="empty_inputs")
        scope_id = inputs[0].scope_id

        # Bundle fencing before any domain write: mixed scope or epoch bundles
        # are rejected with a typed result and no side effects.
        for inp in inputs:
            if inp.scope_id != scope_id:
                return ProjectionIngestResult(
                    "mixed_scope_bundle", reason="mixed_scope_bundle"
                )
        epochs = {inp.runtime_epoch for inp in inputs if inp.runtime_epoch is not None}
        if len(epochs) > 1:
            return ProjectionIngestResult(
                "mixed_epoch_bundle", reason="mixed_epoch_bundle"
            )

        # H1-INV-1 gate #1: deny any forbidden provenance, and gate #2: deny any
        # allowlisted provenance whose value masks a direct-world/oracle field.
        # Redacted diagnostic only; zero domain writes.
        for inp in inputs:
            if inp.provenance not in ONLINE_PROVENANCE_ALLOWLIST:
                self._record_truth_denial(scope_id, inp.event_id)
                return ProjectionIngestResult(
                    online_truth_forbidden, reason=online_truth_forbidden
                )
        if truth_scan_denied(inputs) is not None:
            self._record_truth_denial(scope_id, inputs[0].event_id)
            return ProjectionIngestResult(
                online_truth_forbidden, reason=online_truth_forbidden
            )

        try:
            for inp in inputs:
                inp.validate()
        except MemoryContractError as exc:
            return ProjectionIngestResult("invalid_input", reason=str(exc))

        with self._store.canonical_transaction() as tx:
            if not tx.scope_exists(scope_id):
                return ProjectionIngestResult("unknown_scope", reason="unknown_scope")
            if tx.scope_closed(scope_id):
                return ProjectionIngestResult("scope_closed", reason="scope_closed")

            scope_row = tx.get_scope(scope_id)
            if scope_row is not None and epochs:
                stored_epoch = int(scope_row["runtime_epoch"])
                if next(iter(epochs)) != stored_epoch:
                    return ProjectionIngestResult(
                        "runtime_epoch_mismatch", reason="runtime_epoch_mismatch"
                    )

            bundle_key = projection_idempotency_key(scope_id, inputs)
            bundle_digest = digest_payload(
                {"bundle": bundle_key, "inputs": [inp.event_id for inp in inputs]}
            )
            claim, existing = tx.claim_callback_idempotency(
                scope_id, bundle_key, bundle_digest
            )
            if claim == "conflict":
                raise IdempotencyConflictError(
                    "idempotency_conflict: projection bundle digest mismatch"
                )
            if claim == "duplicate":
                assert existing is not None
                return ProjectionIngestResult(
                    "duplicate",
                    event_ids=(existing["event_id"],),
                    committed_revision=existing["committed_revision"],
                )

            event_ids, outcomes = self._reduce_evidence_in_tx(
                tx,
                scope_id,
                inputs,
                correlation_default=None,
            )
            revision = tx.bump_revision_in_tx(scope_id)
            tx.insert_idempotency_ledger(
                scope_id=scope_id,
                idempotency_key=bundle_key,
                event_id=event_ids[0],
                payload_digest=bundle_digest,
                receipt_sha256=digest_payload(
                    {"event_ids": list(event_ids), "committed_revision": revision}
                ),
                committed_revision=revision,
            )
            tx.write_outbox(
                outbox_id=tx.new_outbox_id(),
                scope_id=scope_id,
                event_id=event_ids[0],
                export_kind=f"evidence.{inputs[0].domain}",
                payload_sha256=digest_payload(
                    {"event_ids": list(event_ids), "committed_revision": revision}
                ),
            )
            return ProjectionIngestResult(
                "ok",
                event_ids=event_ids,
                committed_revision=revision,
                outcomes=outcomes,
            )

    def _record_truth_denial(self, scope_id: str, event_id: str) -> None:
        """Redacted security diagnostic for an H1-INV-1 denial.

        Never echoes the raw candidate value or the forbidden term; only the
        marker and a digest prefix are retained.
        """
        self._store.record_security_audit(
            "online_truth_forbidden",
            reason=(
                "online_truth_forbidden "
                f"scope_digest={digest_payload({'scope': scope_id, 'event': event_id})[:16]}"
            ),
            digest_prefix=digest_payload({"scope": scope_id, "event": event_id})[
                :16
            ],
        )

    def _reduce_evidence_in_tx(
        self,
        tx: MemoryStore,
        scope_id: str,
        inputs: list[NormalizedProjectionInputV1],
        *,
        correlation_default: str | None,
    ) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        """Append one immutable Temporal evidence event per evidence bundle and
        reduce every field claim into the projection, all within the open
        canonical transaction.  Returns (event_ids, field outcomes)."""
        reducer = MemoryProjectionReducer(self._store)
        outcomes: list[Any] = []
        event_ids: list[str] = []
        groups: dict[str, list[NormalizedProjectionInputV1]] = {}
        for inp in inputs:
            groups.setdefault(inp.event_id, []).append(inp)
        for group in groups.values():
            sequence = tx.next_sequence(scope_id)
            event_id = self._append_projection_event(
                tx,
                scope_id,
                group[0],
                sequence,
                correlation_default=correlation_default,
            )
            event_ids.append(event_id)
            for inp in group:
                safe = replace(
                    inp,
                    sequence=sequence,
                    value=self._redaction.redactor.redact_data(inp.value),
                )
                outcomes.append(reducer.reduce(safe, event_id))
        return tuple(event_ids), tuple(outcomes)

    def _append_projection_event(
        self,
        tx: MemoryStore,
        scope_id: str,
        inp: NormalizedProjectionInputV1,
        sequence: int,
        *,
        correlation_default: str | None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        event_id = tx.new_event_id("evt")
        safe_value = self._redaction.redactor.redact_data(inp.value)
        tx.append_temporal_event(
            event_id=event_id,
            scope_id=scope_id,
            sequence=sequence,
            event_type="evidence.projection",
            occurred_at=now,
            ingested_at=now,
            actor_id=inp.actor_id,
            logical_task_id=None,
            dispatch_id=inp.dispatch_id,
            worker_task_id=inp.worker_task_id,
            tool_call_id=None,
            success=None,
            error=None,
            payload=json.dumps(
                {
                    "env_step": inp.env_step,
                    "domain": inp.domain,
                    "entity_id": inp.entity_id,
                    "field_name": inp.field_name,
                    "evidence_id": inp.event_id,
                    "provenance": inp.provenance,
                    "value": safe_value,
                },
                ensure_ascii=False,
                default=str,
            )[:8000],
            causation_id=f"evidence:{inp.event_id}",
            correlation_id=(
                correlation_default or evidence_correlation_id(inp, scope_id)
            ),
            idempotency_key=None,
        )
        return event_id

    # ── coordinator decision events (main plan §3.1, P1) ──────────────

    def append_decision_event(
        self,
        scope_id: str,
        event_type: str,
        *,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        worker_task_id: str | None = None,
        causation_id: str | None = None,
        idempotency_key: str,
    ) -> str | None:
        """Canonical coordinator-decision Temporal event (fail-closed).

        Runs inside one ``canonical_transaction``: an unknown or closed
        scope returns ``None`` with zero writes (ingestor.py:366-369
        pattern); the ``idempotency_ledger`` claim dedupes replayed
        decisions (fresh → append + ledger row, duplicate → original
        event_id, conflict → ``IdempotencyConflictError`` fail-loud).
        ``payload`` is redacted with the ingestor's RedactionPolicy before
        it lands in ``temporal_event.payload`` (R3, exploration 01 §6).
        """
        with self._store.canonical_transaction() as tx:
            if not tx.scope_exists(scope_id):
                return None
            if tx.scope_closed(scope_id):
                return None
            payload_digest = digest_payload(payload)
            claim, existing = tx.claim_callback_idempotency(
                scope_id, idempotency_key, payload_digest
            )
            if claim == "conflict":
                raise IdempotencyConflictError(
                    "idempotency_conflict: same key, different payload digest"
                )
            if claim == "duplicate":
                assert existing is not None
                return existing["event_id"]
            sequence = tx.next_sequence(scope_id)
            event_id = self._append_decision_event(
                tx,
                scope_id,
                event_type,
                sequence=sequence,
                payload=payload,
                correlation_id=correlation_id,
                worker_task_id=worker_task_id,
                causation_id=causation_id,
                idempotency_key=idempotency_key,
            )
            revision = tx.bump_revision_in_tx(scope_id)
            tx.insert_idempotency_ledger(
                scope_id=scope_id,
                idempotency_key=idempotency_key,
                event_id=event_id,
                payload_digest=payload_digest,
                receipt_sha256=digest_payload(
                    {"event_id": event_id, "committed_revision": revision}
                ),
                committed_revision=revision,
            )
            return event_id

    def _append_decision_event(
        self,
        tx: MemoryStore,
        scope_id: str,
        event_type: str,
        *,
        sequence: int,
        payload: dict[str, Any],
        correlation_id: str | None,
        worker_task_id: str | None,
        causation_id: str | None,
        idempotency_key: str,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        event_id = tx.new_event_id("evt")
        safe_payload = self._redaction.redactor.redact_data(payload)
        tx.append_temporal_event(
            event_id=event_id,
            scope_id=scope_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=now,
            ingested_at=now,
            actor_id="Coordinator",
            logical_task_id=None,
            dispatch_id=None,
            worker_task_id=worker_task_id,
            tool_call_id=None,
            success=None,
            error=None,
            payload=json.dumps(safe_payload, ensure_ascii=False, default=str)[
                :8000
            ],
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return event_id


class MemoryLifecycleBridge:
    """Consumes journal receipts for internal-origin transitions.

    callback-origin transitions bypass this bridge (they are bundled directly by
    the push-callback handler); internal-origin transitions (dispatch/cancel/
    watchdog) enqueue here and are written via the same canonical bundle.
    """

    def __init__(self, ingestor: MemoryIngestor) -> None:
        self._ingestor = ingestor
        self._queue: deque[ControlTransitionJournalEntry] = deque()

    @property
    def ingestor(self) -> MemoryIngestor:
        return self._ingestor

    def enqueue(self, entry: ControlTransitionJournalEntry) -> None:
        self._queue.append(entry)
        self.drain()

    def drain(self) -> int:
        processed = 0
        while self._queue:
            entry = self._queue.popleft()
            try:
                self._ingestor.ingest_control_receipt(entry)
            except Exception:
                logger.exception(
                    "MemoryLifecycleBridge failed to ingest control receipt "
                    "dispatch=%s revision=%s",
                    entry.dispatch_id,
                    entry.control_revision,
                )
            processed += 1
        return processed

    def pending_count(self) -> int:
        return len(self._queue)

    def reconcile(self, entries: list[ControlTransitionJournalEntry]) -> int:
        """Restart reconciliation: journal ↔ control_receipt diff.

        Every missing entry is written with the same lifecycle bundle; matching
        receipts return the original event without a second event/outbox.
        """
        written = 0
        for entry in entries:
            result = self._ingestor.ingest_control_receipt(entry)
            if result.status == "ok":
                written += 1
        return written


class SupervisionEventAdapter:
    """dispatch-bound adapter injected into TaskWatchdog's supervision sink.

    Writes each supervision event as exactly one canonical TemporalEvent using
    ``idempotency_key = sha256(scope_id, "supervision", event_id)``.  Unknown /
    closed scopes keep only a redacted local diagnostic and never write Memory.

    The canonical scope is derived from the **trusted** ``dispatch.context_id``
    plus the ``runtime_epoch`` passed explicitly by the caller (MissionRuntime
    epoch) — never from the supervision event payload.  A missing
    ``runtime_epoch`` fails closed with zero writes.
    """

    def __init__(
        self,
        ingestor: MemoryIngestor,
        scope_factory: MemoryScopeFactory,
        store: MemoryStore,
    ) -> None:
        self._ingestor = ingestor
        self._scope_factory = scope_factory
        self._store = store

    def __call__(
        self,
        event: dict[str, Any],
        *,
        dispatch: Any,
        runtime_epoch: int | None = None,
    ) -> None:
        if runtime_epoch is None:
            logger.warning(
                "supervision event ignored (missing trusted runtime epoch): "
                "event_id=%s",
                str(event.get("event_id", ""))[:16],
            )
            return
        scope = self._scope_factory.resolve(
            dispatch.context_id,
            runtime_epoch,
        )
        scope_id = scope.scope_id
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return
        if not self._store.scope_exists(scope_id):
            logger.warning(
                "supervision event ignored (unknown scope): event_id=%s",
                event_id[:16],
            )
            return
        with self._store.canonical_transaction() as tx:
            if tx.scope_closed(scope_id):
                logger.warning(
                    "supervision event ignored (closed scope): event_id=%s",
                    event_id[:16],
                )
                return
            idempotency_key = supervision_idempotency_key(scope_id, event_id)
            payload_digest = digest_payload(event)
            claim, existing = tx.claim_callback_idempotency(
                scope_id, idempotency_key, payload_digest
            )
            if claim == "duplicate":
                return
            if claim == "conflict":
                raise IdempotencyConflictError(
                    "idempotency_conflict: supervision event digest mismatch"
                )
            sequence = tx.next_sequence(scope_id)
            now = datetime.now(timezone.utc).isoformat()
            event_type = event.get("event_type") or "supervision_event"
            canonical_event_id = tx.append_temporal_event(
                event_id=tx.new_event_id("sup"),
                scope_id=scope_id,
                sequence=sequence,
                event_type=f"supervision.{event_type}",
                occurred_at=now,
                ingested_at=now,
                actor_id=event.get("worker_id") or "system",
                logical_task_id=None,
                dispatch_id=event.get("dispatch_id") or dispatch.dispatch_id,
                worker_task_id=getattr(dispatch, "worker_task_id", None),
                tool_call_id=None,
                success=None,
                error=None,
                payload=json.dumps(event, ensure_ascii=False, default=str),
                causation_id=f"supervision:{event_id}",
                correlation_id=f"dispatch:{dispatch.dispatch_id}",
                idempotency_key=idempotency_key,
            )
            revision = tx.bump_revision_in_tx(scope_id)
            tx.insert_idempotency_ledger(
                scope_id=scope_id,
                idempotency_key=idempotency_key,
                event_id=canonical_event_id,
                payload_digest=payload_digest,
                receipt_sha256=digest_payload(
                    {"event_id": canonical_event_id, "committed_revision": revision}
                ),
                committed_revision=revision,
            )
            tx.write_outbox(
                outbox_id=tx.new_outbox_id(),
                scope_id=scope_id,
                event_id=canonical_event_id,
                export_kind="supervision",
                payload_sha256=payload_digest,
            )


__all__ = [
    "AuthenticatedCallbackEnvelope",
    "CallbackIngestResult",
    "ControlReceiptConflictError",
    "ControlReceiptResult",
    "IdempotencyConflictError",
    "MemoryIngestor",
    "MemoryLifecycleBridge",
    "MemoryScopeFactory",
    "MemoryScopeReuseError",
    "ProjectionIngestResult",
    "SupervisionEventAdapter",
    "callback_idempotency_key",
    "control_idempotency_key",
    "evidence_correlation_id",
    "projection_idempotency_key",
    "supervision_idempotency_key",
    "truth_scan_denied",
]
