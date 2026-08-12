"""Phase 0 canonical Memory contracts: scope identity, namespaced relations,
control-transition journal, and validated local MemoryConfig.

These contracts are pure data + deterministic serialization.  They own the
canonical UTF-8 JSON representation that scope ids and journal digests are
derived from.  Nothing here may mutate the control plane.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Agent.environment_state import Freshness

__all__ = [
    "DEFAULT_FIELD_SOURCE_POLICY",
    "FIELD_SOURCE_POLICY",
    "FORBIDDEN_TRUTH_TERMS",
    "LONG_TERM_MEMORY_KINDS",
    "LONG_TERM_MODE_VALUES",
    "ONLINE_PROVENANCE_ALLOWLIST",
    "POLICY_VERSION",
    "ControlTransitionJournalEntry",
    "Freshness",
    "LongTermConfigError",
    "LongTermMemoryCandidateV1",
    "LongTermMemoryConfig",
    "LongTermRuntimeConfig",
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
    "canonicalize_statement",
    "control_transition_digest",
    "derive_content_digest",
    "derive_memory_key",
    "digest_payload",
    "field_source_priority",
    "load_long_term_config",
    "normalize_inventory",
    "online_truth_forbidden",
    "reflection_run_idempotency_key",
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


def normalize_inventory(value: Any) -> list[str]:
    """Normalize worker-reported agent inventory into the canonical resource
    list (sorted, unique resource names).

    The legacy semantic map and the canonical projection reducer must persist
    the SAME semantic representation for worker-observed agent inventory.  The
    worker may report it as a list (``["Water", "Sand"]``), a count dict
    (``{"Water": 1}``), or a stringified literal (the barrier renders it as
    ``str({...})``).  Structured parsing only — never ``eval``: strings are
    decoded with :func:`ast.literal_eval` when possible, and anything
    unparseable collapses to an empty resource list instead of being persisted
    verbatim.
    """
    if isinstance(value, list):
        items = [str(item) for item in value]
    elif isinstance(value, dict):
        items = [str(key) for key in value]
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, dict):
            items = [str(key) for key in parsed]
        elif isinstance(parsed, list):
            items = [str(item) for item in parsed]
        else:
            return []
    else:
        return []
    return sorted({item for item in items if item})

# ── Field-level source policy (H1 card §3.1) ────────────────────────────────

# Field family -> ordered source classes (high -> low).  Priority is compared
# per field, never by whole event, and AgentRegistry static metadata
# (capability / sensor_type) never participates in dynamic arbitration.
FIELD_SOURCE_POLICY: dict[str, tuple[str, ...]] = {
    "position": (
        "worker_telemetry",
        "worker_sensor_tool",
        "worker_observation",
        "peer_report",
    ),
    "inventory": (
        "worker_telemetry",
        "worker_sensor_tool",
        "worker_observation",
        "peer_report",
    ),
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


# ── Long-term memory contracts (Phase 1) ────────────────────────────────────
#
# Frozen by the main plan §3.3 / D4-D6 / D9 (2026-08-12): the long-term kernel
# is a run-local independent SQLite file at ``<memory_root>/long_term/`` with
# its own schema + migration runner (cross-run storage supplement §3).  All
# key/digest derivation is deterministic over the **post-redaction** canonical
# statement so audits can recompute them; the model never chooses a key.

#: Restricted ``kind`` enum for long-term memories (validator rejects anything
#: outside this set; the long-term schema mirrors it with a CHECK constraint).
LONG_TERM_MEMORY_KINDS: tuple[str, ...] = (
    "strategy",
    "lesson",
    "hazard",
    "pattern",
    "status",
)

#: Code constant for the canonicalize/redaction policy; bumped manually when
#: either policy changes (model-invisible, D6/D9).
POLICY_VERSION = 1

#: D5 visibility modes; ``read`` is coordinator/system-principal only.
LONG_TERM_MODE_VALUES: tuple[str, ...] = ("off", "shadow", "read")

#: Leading/trailing punctuation removed by :func:`canonicalize_statement`.
#: Deliberately a *sentence-punctuation* set (not ``string.punctuation``): a
#: trailing ``)`` or ``]`` that belongs to content such as ``(1,2,0)`` is
#: preserved, while sentence-ending marks (``.`` / ``!`` / ``?`` / CJK
#: equivalents) are dropped for dedup.
_STATEMENT_EDGE_PUNCTUATION = ".,!?;:。，！？；：、…"

_WS_COLLAPSE_RE = re.compile(r"\s+")


def canonicalize_statement(statement: str) -> str:
    """Canonical form of a long-term statement (policy version 1).

    Frozen rule order: lowercase → strip → collapse whitespace → strip
    leading/trailing punctuation (main plan §3.3).  ``memory_key`` and
    ``content_digest`` are derived from the post-redaction text through this
    function, so a stored statement and its digests always agree.
    """
    text = statement.lower().strip()
    text = _WS_COLLAPSE_RE.sub(" ", text)
    return text.strip(_STATEMENT_EDGE_PUNCTUATION)


def derive_content_digest(statement: str) -> str:
    """``sha256(canonicalized(statement))`` over the final stored text."""
    return digest_bytes(canonicalize_statement(statement).encode("utf-8"))


def derive_memory_key(
    *,
    project_id: str,
    kind: str,
    policy_version: int,
    statement: str,
) -> str:
    """Deterministic system-derived memory key (model never sees/chooses it).

    ``sha256(project_id, kind, policy_version, canonicalized(statement))``;
    used only for same-run dedup and supersede association (main plan §3.3).
    """
    payload = {
        "project_id": project_id,
        "kind": kind,
        "policy_version": policy_version,
        "statement": canonicalize_statement(statement),
    }
    return digest_bytes(canonical_json_bytes(payload))


def reflection_run_idempotency_key(
    *,
    project_id: str,
    source_scope_id: str,
    memory_revision: int,
    snapshot_digest: str,
    policy_version: int,
) -> str:
    """Idempotency key for one reflection run over one source snapshot.

    Binds project_id + source_scope_id + snapshot.memory_revision +
    snapshot.snapshot_digest + policy_version (atomic-snapshot supplement
    §2.5): retrying the same snapshot yields zero duplicate persistence.
    """
    payload = {
        "project_id": project_id,
        "source_scope_id": source_scope_id,
        "memory_revision": memory_revision,
        "snapshot_digest": snapshot_digest,
        "policy_version": policy_version,
    }
    return digest_bytes(canonical_json_bytes(payload))


@dataclass(frozen=True)
class LongTermMemoryCandidateV1:
    """One validated reflection candidate (main plan §3.3).

    ``memory_key`` is derived by the system (never accepted from the model);
    ``kind`` is a restricted enum; ``source_refs`` are (scope_id, event_id)
    evidence pairs that must all sit inside the input window.
    """

    schema_version: int
    memory_key: str
    kind: str
    statement: str
    confidence: float
    source_refs: tuple[tuple[str, str], ...] | list[tuple[str, str]] = ()

    def validate(self) -> LongTermMemoryCandidateV1:
        """Fail-closed validation; raises typed :class:`MemoryContractError`."""
        if self.schema_version != 1:
            raise MemoryContractError(
                "invalid_schema_version",
                f"schema_version must be 1, got {self.schema_version!r}",
            )
        if self.kind not in LONG_TERM_MEMORY_KINDS:
            raise MemoryContractError(
                "invalid_kind",
                f"kind must be one of {LONG_TERM_MEMORY_KINDS!r}, got {self.kind!r}",
            )
        if not isinstance(self.memory_key, str) or not self.memory_key.strip():
            raise MemoryContractError("invalid_memory_key", "memory_key is required")
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise MemoryContractError("invalid_statement", "statement is required")
        if not isinstance(self.confidence, (int, float)) or not (
            0.0 <= self.confidence <= 1.0
        ):
            raise MemoryContractError(
                "invalid_confidence",
                f"confidence must be within [0,1], got {self.confidence!r}",
            )
        return self


@dataclass(frozen=True)
class LongTermMemoryConfig(MemoryConfig):
    """Run-local long-term memory configuration (D4/D5/D9).

    Reuses ``MemoryConfig`` fail-closed validation (relative path / URI
    rejection) and derives the run-local long-term database at
    ``<memory_root>/long_term/long_term.sqlite3`` — no separate root
    parameter.  ``long_term_mode`` defaults to ``off``.
    """

    long_term_mode: str = "off"

    @property
    def long_term_db_path(self) -> Path:
        return self.memory_root / "long_term" / "long_term.sqlite3"

    def validate(self) -> LongTermMemoryConfig:
        super().validate()
        if self.long_term_mode not in LONG_TERM_MODE_VALUES:
            raise MemoryConfigError(
                "invalid_long_term_mode",
                f"long_term_mode must be one of {LONG_TERM_MODE_VALUES!r}, "
                f"got {self.long_term_mode!r}",
            )
        return self


class LongTermConfigError(MemoryContractError):
    """D9: ``long_term.config`` is unparseable or carries an illegal value.

    Callers must force ``long_term_mode=off`` and record an audit entry after
    catching this; the loader never silently falls back to defaults.
    """

    code = "invalid_long_term_config"


@dataclass(frozen=True)
class LongTermRuntimeConfig:
    """Parsed tunables from ``long_term.config`` (D9, all optional).

    Every field has a frozen default; a missing file yields this default
    object with ``long_term_mode`` still ``off``.
    """

    max_events: int = 200
    max_chars: int = 8000
    task_complete: bool = True
    supervision_event: bool = True
    every_env_step: int = 5
    min_interval_sec: int = 30
    quality_enabled: bool = True
    reflection_sec: int = 60
    long_term_mode: str = "off"


_LONG_TERM_CONFIG_DEFAULTS: dict[tuple[str, str], tuple[str, Any, str]] = {
    # (ini section, ini key) -> (runtime field name, default, kind)
    # 注意：config 文件 key 与 LongTermRuntimeConfig 字段名解耦 —— [quality]
    # 下 ini key 是 ``enabled``，运行时字段是 ``quality_enabled``（F1）。
    ("window", "max_events"): ("max_events", 200, "int"),
    ("window", "max_chars"): ("max_chars", 8000, "int"),
    ("trigger", "task_complete"): ("task_complete", True, "bool"),
    ("trigger", "supervision_event"): ("supervision_event", True, "bool"),
    ("trigger", "every_env_step"): ("every_env_step", 5, "int"),
    ("trigger", "min_interval_sec"): ("min_interval_sec", 30, "int"),
    ("quality", "enabled"): ("quality_enabled", True, "bool"),
    ("timeout", "reflection_sec"): ("reflection_sec", 60, "int"),
}


def load_long_term_config(path: str | os.PathLike[str]) -> LongTermRuntimeConfig:
    """D9: parse the repository-root ``long_term.config`` (ini).

    - Missing file → defaults, ``long_term_mode`` stays ``off``;
    - unparseable ini or illegal value → :class:`LongTermConfigError` (typed;
      the caller must force ``long_term_mode=off`` + audit — never silent
      fallback);
    - absent sections/keys use defaults.
    """
    config_path = Path(path)
    if not config_path.exists():
        return LongTermRuntimeConfig()
    parser = configparser.ConfigParser()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except configparser.Error as exc:
        raise LongTermConfigError(
            "unparseable_config",
            f"long_term.config is not a parseable ini file: {exc}",
        ) from exc

    values: dict[str, Any] = {}
    for (section, key), (field_name, default, kind) in _LONG_TERM_CONFIG_DEFAULTS.items():
        if not parser.has_option(section, key):
            values[field_name] = default
            continue
        raw = parser.get(section, key).strip()
        if kind == "int":
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise LongTermConfigError(
                    "invalid_value",
                    f"[{section}] {key} = {raw!r} is not an integer",
                ) from exc
            if parsed < 0:
                raise LongTermConfigError(
                    "invalid_value",
                    f"[{section}] {key} = {raw!r} must be >= 0",
                )
        else:  # bool
            lowered = raw.lower()
            if lowered in ("true", "1", "yes", "on"):
                parsed = True
            elif lowered in ("false", "0", "no", "off"):
                parsed = False
            else:
                raise LongTermConfigError(
                    "invalid_value",
                    f"[{section}] {key} = {raw!r} is not a boolean",
                )
        values[field_name] = parsed
    return LongTermRuntimeConfig(**values)
