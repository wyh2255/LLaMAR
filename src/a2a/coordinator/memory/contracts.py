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
    "ControlTransitionJournalEntry",
    "Freshness",
    "MemoryConfig",
    "MemoryConfigError",
    "MemoryContractError",
    "MemoryRef",
    "MemoryRefValidationError",
    "MemoryRelation",
    "MemoryScopeValidationError",
    "MemoryScopeV1",
    "canonical_json_bytes",
    "control_transition_digest",
    "digest_payload",
]


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

    def validate(self) -> "MemoryScopeV1":
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
    ) -> "ControlTransitionJournalEntry":
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
    def from_dict(cls, payload: dict[str, Any]) -> "ControlTransitionJournalEntry":
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

    def validate(self) -> "MemoryConfig":
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
