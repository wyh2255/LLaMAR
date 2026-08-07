"""Phase 0 canonical Memory contracts: scope identity, namespaced relations,
control-transition journal, and validated local MemoryConfig.

These contracts are pure data + deterministic serialization.  They own the
canonical UTF-8 JSON representation that scope ids and journal digests are
derived from.  Nothing here may mutate the control plane.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Agent.environment_state import Freshness

__all__ = [
    "DEFAULT_FIELD_SOURCE_POLICY",
    "FIELD_SOURCE_POLICY",
    "FORBIDDEN_TRUTH_TERMS",
    "ONLINE_PROVENANCE_ALLOWLIST",
    "ControlTransitionJournalEntry",
    "Freshness",
    "MemoryConfig",
    "MemoryConfigError",
    "MemoryContractError",
    "MemoryRef",
    "MemoryRefValidationError",
    "MemoryRelation",
    "MemoryScopeV1",
    "MemoryScopeValidationError",
    "NormalizedProjectionInputV1",
    "ProjectionEntityRevision",
    "ProjectionViewRevision",
    "canonical_json_bytes",
    "control_transition_digest",
    "digest_payload",
    "field_source_priority",
    "online_truth_forbidden",
    "scan_forbidden_truth_fields",
]

# ── H1-INV-1 online provenance allowlist ────────────────────────────────────

# The only online sources the MemoryIngestor / reducer may consume.  Anything
# outside this set (barrier, oracle, ground_truth, checker, simulator, direct
# world snapshot) is denied with a typed ``online_truth_forbidden`` result and
# zero domain writes.
ONLINE_PROVENANCE_ALLOWLIST = frozenset(
    {
        "worker_sensor_tool",
        "worker_telemetry",
        "worker_observation",
        "peer_report",
        "registry",
        "control",
        "supervision",
    }
)

online_truth_forbidden = "online_truth_forbidden"

# Forbidden truth terms (case-insensitive) that may never appear in a Worker
# evidence value / field.  Even an *allowlisted* provenance carrying these
# masks a direct-world / oracle / ground-truth candidate and is rejected by
# H1-INV-1 with zero domain writes.
FORBIDDEN_TRUTH_TERMS: frozenset[str] = frozenset(
    {
        "oracle",
        "ground_truth",
        "ground-truth",
        "ground truth",
        "checker",
        "simulator",
        "sim_truth",
        "world_snapshot",
        "world snapshot",
        "direct_world",
        "direct world",
        "truth_trace",
        "coverage_truth",
        "env.controller",
        "get_env_snapshot",
        "object_priors",
    }
)


def scan_forbidden_truth_fields(obj: Any) -> bool:
    """Return True if ``obj`` (recursively) carries a forbidden truth term.

    A direct-world / oracle / ground-truth candidate may arrive masked inside
    an allowlisted Worker provenance (e.g. ``value={"ground_truth": ...}`` or a
    ``note`` quoting the simulator).  Conservative scan of dict keys and string
    values; ``True`` means the whole bundle must be denied before any reducer.
    """
    if isinstance(obj, str):
        lowered = obj.lower()
        return any(term in lowered for term in FORBIDDEN_TRUTH_TERMS)
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(term in lowered for term in FORBIDDEN_TRUTH_TERMS):
                return True
            if scan_forbidden_truth_fields(value):
                return True
        return False
    if isinstance(obj, (list, tuple)):
        return any(scan_forbidden_truth_fields(item) for item in obj)
    return False

# ── Field-level source policy (H1 card §3.1) ────────────────────────────────

# Field family -> ordered source classes (high -> low).  Priority is compared
# per field, never by whole event, and AgentRegistry static metadata
# (capability / sensor_type) never participates in dynamic arbitration.
FIELD_SOURCE_POLICY: dict[str, tuple[str, ...]] = {
    "position": ("worker_sensor_tool", "worker_observation", "peer_report"),
    "inventory": ("worker_sensor_tool", "worker_observation", "peer_report"),
    "scene_object": ("worker_sensor_tool", "worker_observation", "peer_report"),
    "battery": ("worker_telemetry", "worker_sensor_tool", "worker_observation"),
    "localization_quality": (
        "worker_telemetry",
        "worker_sensor_tool",
        "worker_observation",
    ),
    "node_telemetry": ("worker_telemetry", "worker_sensor_tool", "worker_observation"),
    "availability": (
        "control",
        "supervision",
        "worker_telemetry",
        "worker_observation",
    ),
    "heartbeat": ("control", "supervision", "worker_telemetry", "worker_observation"),
    "capability": ("registry",),
    "sensor_type": ("registry",),
}

DEFAULT_FIELD_SOURCE_POLICY = (
    "worker_sensor_tool",
    "worker_observation",
    "peer_report",
)


def field_source_priority(field_name: str, provenance: str) -> int:
    """Return the priority index (0 = highest) for a field's source class.

    Fields without a dedicated policy fall back to the default scene-object
    policy.  A provenance outside the policy ranks lowest; the reducer still
    admits the claim as Worker evidence (it is allowlisted), it just cannot
    outrank a higher-authority candidate for that field.
    """
    order = FIELD_SOURCE_POLICY.get(field_name, DEFAULT_FIELD_SOURCE_POLICY)
    try:
        return order.index(provenance)
    except ValueError:
        return len(order)


# ── Normalized projection input (Phase 3) ───────────────────────────────────


@dataclass(frozen=True)
class NormalizedProjectionInputV1:
    """One normalized field-level claim of authenticated Worker evidence.

    ``event_id`` is the **external evidence identity** (e.g. the callback
    causation id); the store mints a distinct canonical Temporal event UUID per
    evidence bundle and every projection field / relation / outcome references
    that canonical UUID so Temporal -> projection joins are stable.  The
    evidence identity is retained separately for causation / idempotency /
    audit.  ``sequence`` is the canonical scope sequence assigned by the store.

    ``correlation_id`` carries authenticated dispatch/correlation metadata when
    available (never fabricated); when absent the ingestor derives a
    deterministic evidence correlation that does not claim a dispatch ID.
    Field source priority is derived per field from
    :data:`FIELD_SOURCE_POLICY`, so the same provenance may rank differently
    for position vs battery.
    """

    scope_id: str
    event_id: str
    sequence: int
    env_step: int | None
    actor_id: str
    provenance: str
    domain: str  # spatial | embodied
    entity_id: str
    entity_type: str
    field_name: str
    value: Any
    confidence: float = 1.0
    runtime_epoch: int | None = None
    dispatch_id: str | None = None
    worker_task_id: str | None = None
    correlation_id: str | None = None

    @property
    def source_priority(self) -> int:
        return field_source_priority(self.field_name, self.provenance)

    def validate(self) -> NormalizedProjectionInputV1:
        missing: list[str] = []
        for name in ("scope_id", "event_id", "entity_id", "entity_type", "field_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
        if self.domain not in ("spatial", "embodied"):
            missing.append("domain")
        if self.provenance not in ONLINE_PROVENANCE_ALLOWLIST:
            missing.append(f"provenance:{self.provenance}")
        if missing:
            raise MemoryContractError(
                "invalid_projection_input",
                f"invalid projection input: {', '.join(missing)}",
            )
        return self


@dataclass(frozen=True)
class ProjectionEntityRevision:
    """Entity-level revision of the current visible projection (Phase 3)."""

    scope_id: str
    domain: str
    entity_id: str
    revision: int
    as_of_sequence: int | None = None


@dataclass(frozen=True)
class ProjectionViewRevision:
    """Viewer-visible projection revision (Phase 3)."""

    scope_id: str
    snapshot_revision: int
    as_of_sequence: int | None = None


def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic canonical UTF-8 JSON used for all memory digests."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_payload(payload: Any) -> str:
    """SHA-256 of a payload's canonical JSON (never the raw payload itself)."""
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return digest_bytes(serialized)


def control_transition_digest(fields: dict[str, Any]) -> str:
    """Canonical digest over the named journal fields (raw body excluded)."""
    return digest_bytes(canonical_json_bytes(fields))


class MemoryContractError(RuntimeError):
    """Stable base error for memory contract violations."""

    code = "memory_contract_error"

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


class MemoryScopeValidationError(MemoryContractError):
    code = "missing_scope_field"


class MemoryRefValidationError(MemoryContractError):
    code = "invalid_ref"


class MemoryConfigError(MemoryContractError):
    code = "invalid_config"


@dataclass(frozen=True)
class MemoryScopeV1:
    """Canonical memory scope identity.

    ``scope_id`` is the SHA-256 of the canonical UTF-8 JSON of the four named
    fields.  Every field is required; a missing field fails closed and no domain
    write may proceed.
    """

    project_id: str
    experiment_id: str
    context_id: str
    runtime_epoch: int

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "experiment_id": self.experiment_id,
            "context_id": self.context_id,
            "runtime_epoch": self.runtime_epoch,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())

    def validate(self) -> MemoryScopeV1:
        missing: list[str] = []
        for name in ("project_id", "experiment_id", "context_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
        if (
            not isinstance(self.runtime_epoch, int)
            or isinstance(self.runtime_epoch, bool)
            or self.runtime_epoch < 0
        ):
            missing.append("runtime_epoch")
        if missing:
            raise MemoryScopeValidationError(
                "missing_scope_field", f"missing scope fields: {', '.join(missing)}"
            )
        return self

    @property
    def scope_id(self) -> str:
        self.validate()
        return digest_bytes(self.canonical_bytes())


@dataclass(frozen=True)
class MemoryRef:
    """Namespace-qualified reference used by MemoryRelation.

    ``namespace`` is either ``memory`` (a domain record) or ``control`` (a
    read-only reference to a control-plane ID such as a PhysicalDispatch).
    Control refs may be referenced but never mutated through Memory.
    """

    namespace: str
    id: str

    def __post_init__(self) -> None:
        if self.namespace not in ("memory", "control"):
            raise MemoryRefValidationError(
                "invalid_namespace",
                f"namespace must be 'memory' or 'control', got {self.namespace!r}",
            )
        if not isinstance(self.id, str) or not self.id.strip():
            raise MemoryRefValidationError("missing_ref_id", "ref id is required")


@dataclass(frozen=True)
class MemoryRelation:
    """Explicit relation between a memory/control record and another record."""

    relation_id: str
    scope_id: str
    from_ref: MemoryRef
    relation_type: str
    to_ref: MemoryRef
    valid_from: str | None = None
    valid_to: str | None = None
    source_event_id: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class ControlTransitionJournalEntry:
    """Immutable control-plane lifecycle journal entry.

    Persisted atomically with the control-state snapshot (temp+fsync+replace).
    ``journal_sha256`` is the canonical digest of the named fields (raw
    result/body excluded); ``result_digest`` is the only trace of the payload.
    """

    context_id: str
    runtime_epoch: int
    dispatch_id: str
    control_revision: int
    previous_state: str
    state: str
    source: str
    observed_at: str
    result_digest: str | None
    journal_sha256: str

    @property
    def transition_id(self) -> tuple[str, int, str, int]:
        """Primary key: (context_id, runtime_epoch, dispatch_id, control_revision)."""
        return (
            self.context_id,
            self.runtime_epoch,
            self.dispatch_id,
            self.control_revision,
        )

    def digest_fields(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "runtime_epoch": self.runtime_epoch,
            "dispatch_id": self.dispatch_id,
            "control_revision": self.control_revision,
            "previous_state": self.previous_state,
            "state": self.state,
            "source": self.source,
            "observed_at": self.observed_at,
            "result_digest": self.result_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.digest_fields(), "journal_sha256": self.journal_sha256}

    @classmethod
    def build(
        cls,
        *,
        context_id: str,
        runtime_epoch: int,
        dispatch_id: str,
        control_revision: int,
        previous_state: str,
        state: str,
        source: str,
        observed_at: str,
        result: Any | None,
    ) -> ControlTransitionJournalEntry:
        result_digest = digest_payload(result) if result is not None else None
        fields = {
            "context_id": context_id,
            "runtime_epoch": runtime_epoch,
            "dispatch_id": dispatch_id,
            "control_revision": control_revision,
            "previous_state": previous_state,
            "state": state,
            "source": source,
            "observed_at": observed_at,
            "result_digest": result_digest,
        }
        return cls(
            **fields,
            journal_sha256=control_transition_digest(fields),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ControlTransitionJournalEntry:
        fields = {
            "context_id": str(payload["context_id"]),
            "runtime_epoch": int(payload["runtime_epoch"]),
            "dispatch_id": str(payload["dispatch_id"]),
            "control_revision": int(payload["control_revision"]),
            "previous_state": str(payload["previous_state"]),
            "state": str(payload["state"]),
            "source": str(payload["source"]),
            "observed_at": str(payload["observed_at"]),
            "result_digest": payload.get("result_digest"),
            "journal_sha256": str(payload["journal_sha256"]),
        }
        return cls(**fields)


@dataclass(frozen=True)
class MemoryConfig:
    """Validated Coordinator-owned memory configuration.

    ``memory_root`` must be a local absolute path; paths are never accepted from
    a worker, LLM, or request.  The canonical database lives at
    ``<memory_root>/memory/memory.sqlite3``.
    """

    experiment_id: str
    memory_root: Path
    project_id: str = "llamar"

    @property
    def db_path(self) -> Path:
        return self.memory_root / "memory" / "memory.sqlite3"

    def validate(self) -> MemoryConfig:
        if not isinstance(self.experiment_id, str) or not self.experiment_id.strip():
            raise MemoryConfigError(
                "missing_experiment_id", "experiment_id is required"
            )
        root = self.memory_root
        if not isinstance(root, Path) or not root.is_absolute():
            raise MemoryConfigError(
                "invalid_memory_root", f"memory_root must be absolute: {root!r}"
            )
        if "://" in str(root):
            raise MemoryConfigError(
                "invalid_memory_root", "memory_root must be a local path"
            )
        return self


@dataclass(frozen=True)
class MemoryRevision:
    """Per-scope monotonic projection revision."""

    scope_id: str
    revision: int
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
