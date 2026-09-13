"""Phase 5 Memory exporter: deterministic compatibility materialization.

The canonical SQLite store is the single source of truth.  In ``read_port``
mode this exporter deterministically rebuilds the JSONL compatibility
artifacts from *committed canonical records* for every
``(scope_id, artifact_kind, canonical_revision)``: build content in memory ->
write temp file -> fsync -> write manifest digest -> ``os.replace()`` to the
final path.  Compatibility is a promise about record schema, fields, ordering
and run-close readability — never "one physical append per callback".

The exporter is an at-least-once consumer: it never participates in a
canonical transaction, never claims exactly-once physical JSONL append, never
performs retention purge, and never rewrites artifacts owned by other writers
(mission_graph.jsonl, supervision_<dispatch>.ndjson, map_summary.jsonl,
events.ndjson + CSV, snapshot_<task>.json).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import digest_bytes, digest_payload
from a2a.coordinator.memory.store import MemoryStore

logger = logging.getLogger(__name__)

EXPORT_MANIFEST_FILENAME = "export_manifest.json"
EXPORT_SCHEMA_VERSION = 1

# Canonical artifact kinds (JSONL filenames).  ``semantic_map.jsonl`` is the
# legacy compatibility artifact; the rest are deterministic canonical exports.
ARTIFACT_TEMPORAL = "temporal.jsonl"
ARTIFACT_SPATIAL = "spatial.jsonl"
ARTIFACT_EMBODIED = "embodied.jsonl"
ARTIFACT_REVISION = "revision.jsonl"
ARTIFACT_OUTBOX = "outbox.jsonl"
ARTIFACT_RELATIONS = "relations.jsonl"
ARTIFACT_SEMANTIC_MAP = "semantic_map.jsonl"

CANONICAL_ARTIFACTS: tuple[str, ...] = (
    ARTIFACT_TEMPORAL,
    ARTIFACT_SPATIAL,
    ARTIFACT_EMBODIED,
    ARTIFACT_REVISION,
    ARTIFACT_OUTBOX,
    ARTIFACT_RELATIONS,
    ARTIFACT_SEMANTIC_MAP,
)

__all__ = [
    "ARTIFACT_EMBODIED",
    "ARTIFACT_OUTBOX",
    "ARTIFACT_RELATIONS",
    "ARTIFACT_REVISION",
    "ARTIFACT_SEMANTIC_MAP",
    "ARTIFACT_SPATIAL",
    "ARTIFACT_TEMPORAL",
    "CANONICAL_ARTIFACTS",
    "EXPORT_MANIFEST_FILENAME",
    "EXPORT_SCHEMA_VERSION",
    "ExportArtifactEntry",
    "MemoryExportError",
    "MemoryExportReport",
    "MemoryExporter",
    "load_manifest",
    "render_jsonl_bytes",
]


class MemoryExportError(RuntimeError):
    """Stable error for exporter failures (missing scope / unsafe export dir)."""

    code = "memory_export_error"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


@dataclass(frozen=True)
class ExportArtifactEntry:
    """One manifest entry for one materialized artifact.

    Every exporter output writes ``scope_id``, ``artifact_kind``,
    ``canonical_revision``, record count, canonical payload digest, artifact
    SHA-256 and schema version into ``export_manifest.json``.
    """

    artifact_kind: str
    scope_id: str
    canonical_revision: int
    record_count: int
    payload_sha256: str
    artifact_sha256: str
    schema_version: int = EXPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope_id": self.scope_id,
            "artifact_kind": self.artifact_kind,
            "canonical_revision": self.canonical_revision,
            "record_count": self.record_count,
            "payload_sha256": self.payload_sha256,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class MemoryExportReport:
    """Result of one deterministic scope export."""

    scope_id: str
    canonical_revision: int
    exported_at: str
    artifacts: dict[str, ExportArtifactEntry] = field(default_factory=dict)
    manifest_sha256: str = ""
    manifest_path: Path | None = None

    def artifact_entries(self) -> list[ExportArtifactEntry]:
        return sorted(self.artifacts.values(), key=lambda e: e.artifact_kind)


def _try_parse_json(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def render_jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    """Deterministic single-line-per-record UTF-8 JSONL bytes."""
    lines = [
        json.dumps(rec, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        for rec in records
    ]
    return "".join(lines).encode("utf-8")


def _write_temp(export_dir: Path, filename: str, data: bytes) -> Path:
    """Write ``data`` to a fsynced temp file in ``export_dir`` and return it."""
    export_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=str(export_dir)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return Path(tmp)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_replace(data: bytes, path: Path) -> None:
    """temp + fsync + replace: durable atomic write of a whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_temp(path.parent, path.name, data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def load_manifest(export_dir: Path) -> dict[str, Any] | None:
    """Read ``export_manifest.json``; None when missing or unparseable."""
    path = Path(export_dir) / EXPORT_MANIFEST_FILENAME
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _value_eq(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except TypeError:
        return a == b


def _field_claim(ev: dict[str, Any], source_priority: int) -> dict[str, Any]:
    return {
        "field_name": ev["field_name"],
        "value": ev["value"],
        "env_step": ev["env_step"],
        "source_priority": source_priority,
        "confidence": ev["confidence"],
        "sequence": ev["sequence"],
        "conflict_event_ids": None,
        "conflict_candidates": None,
    }


def _apply_claim(state: dict[str, dict[str, Any]], ev: dict[str, Any]) -> None:
    """Apply one spatial field claim deterministically (H1 comparison order).

    Mirrors the canonical reducer's comparison order (env_step, field source
    priority, confidence, value equality) so the exported semantic_map
    ``object`` reproduces the Spatial projection as of each event.
    """
    from a2a.coordinator.memory.contracts import field_source_priority

    fn = ev["field_name"]
    in_step = ev["env_step"]
    cur = state.get(fn)
    if cur is None:
        state[fn] = _field_claim(ev, field_source_priority(fn, ev["provenance"]))
        return
    cur_step = cur["env_step"]
    if cur_step is not None and (in_step is None or in_step < cur_step):
        return  # ignored_out_of_order
    if cur_step is None or (in_step is not None and in_step > cur_step):
        state[fn] = _field_claim(ev, field_source_priority(fn, ev["provenance"]))
        return
    in_prio = field_source_priority(fn, ev["provenance"])
    if in_prio < cur["source_priority"]:
        state[fn] = _field_claim(ev, field_source_priority(fn, ev["provenance"]))
        return
    if in_prio > cur["source_priority"]:
        return  # superseded_by_higher_authority
    if ev["confidence"] > cur["confidence"]:
        state[fn] = _field_claim(ev, field_source_priority(fn, ev["provenance"]))
        return
    if ev["confidence"] < cur["confidence"]:
        return
    if _value_eq(cur["value"], ev["value"]):
        return  # no_change
    # same step/priority/confidence, different value -> explicit conflict.
    # The current holder stays; the losing candidate is recorded.
    cur["conflict_event_ids"] = True
    candidates = list(cur.get("conflict_candidates") or [])
    candidates.append({"event_sequence": ev["sequence"], "value": ev["value"]})
    cur["conflict_candidates"] = candidates


class MemoryExporter:
    """Deterministic exporter from committed canonical Memory records."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def store(self) -> MemoryStore:
        return self._store

    # ── record builders (deterministic, no side effects) ───────────────────

    def build_artifacts(self, scope_id: str) -> dict[str, list[dict[str, Any]]]:
        """Return ``{artifact_kind: [record, ...]}`` for the committed set.

        Pure function of the canonical store: identical input always yields
        identical records.
        """
        return {
            ARTIFACT_TEMPORAL: self._temporal_records(scope_id),
            ARTIFACT_SPATIAL: self._projection_records(scope_id, "spatial"),
            ARTIFACT_EMBODIED: self._projection_records(scope_id, "embodied"),
            ARTIFACT_REVISION: self._revision_records(scope_id),
            ARTIFACT_OUTBOX: self._outbox_records(scope_id),
            ARTIFACT_RELATIONS: self._relation_records(scope_id),
            ARTIFACT_SEMANTIC_MAP: self._semantic_map_records(scope_id),
        }

    def _temporal_records(self, scope_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for evt in self._store.temporal_events(scope_id):
            row = dict(evt)
            success = row.get("success")
            row["success"] = True if success == 1 else False if success == 0 else None
            row["payload"] = _try_parse_json(row.get("payload"))
            records.append(row)
        return records

    def _projection_records(self, scope_id: str, domain: str) -> list[dict[str, Any]]:
        records = []
        for f in self._store.projection_fields(scope_id):
            if f["domain"] == domain:
                records.append(f)
        return records

    def _revision_records(self, scope_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        scope_row = self._store.get_scope(scope_id)
        if scope_row is not None:
            records.append(
                {
                    "kind": "scope",
                    "scope_id": scope_id,
                    "project_id": scope_row["project_id"],
                    "experiment_id": scope_row["experiment_id"],
                    "context_id": scope_row["context_id"],
                    "runtime_epoch": scope_row["runtime_epoch"],
                    "closed_at": scope_row["closed_at"],
                    "revision": self._store.revision_of(scope_id),
                }
            )
        records.append(
            {
                "kind": "view_revision",
                "scope_id": scope_id,
                "snapshot_revision": self._store.view_revision_of(scope_id),
            }
        )
        for ent in self._store.entity_revisions(scope_id):
            records.append(
                {
                    "kind": "entity_revision",
                    "scope_id": scope_id,
                    "domain": ent["domain"],
                    "entity_id": ent["entity_id"],
                    "entity_type": ent["entity_type"],
                    "revision": ent["revision"],
                    "as_of_sequence": ent["as_of_sequence"],
                }
            )
        return records

    def _outbox_records(self, scope_id: str) -> list[dict[str, Any]]:
        entries = [dict(e) for e in self._store.outbox_entries(scope_id)]
        entries.sort(key=lambda e: (e["created_at"], e["outbox_id"]))
        return entries

    def _relation_records(self, scope_id: str) -> list[dict[str, Any]]:
        records = []
        for rel in self._store.relations_for_scope(scope_id):
            records.append(
                {
                    "relation_id": rel.relation_id,
                    "scope_id": rel.scope_id,
                    "from_namespace": rel.from_ref.namespace,
                    "from_id": rel.from_ref.id,
                    "relation_type": rel.relation_type,
                    "to_namespace": rel.to_ref.namespace,
                    "to_id": rel.to_ref.id,
                    "valid_from": rel.valid_from,
                    "valid_to": rel.valid_to,
                    "source_event_id": rel.source_event_id,
                    "confidence": rel.confidence,
                }
            )
        return records

    def _semantic_map_records(self, scope_id: str) -> list[dict[str, Any]]:
        """Legacy-compatible ``{ts, event_type, observation, object}`` lines.

        Order follows canonical sequence.  ``object`` is the Spatial projection
        as of that event, reconstructed by replaying the committed spatial
        evidence events through the same H1 comparison order the canonical
        reducer uses.  ``observation`` always carries
        reporter/step/object_type/name/position/attributes/confidence/
        source_task_id/note.
        """

        # Current projection rows carry the authoritative entity_type.
        entity_types: dict[str, str] = {}
        for f in self._store.projection_fields(scope_id):
            if f["domain"] == "spatial" and f["entity_id"] not in entity_types:
                entity_types[f["entity_id"]] = f["entity_type"]

        events_by_entity: dict[str, list[dict[str, Any]]] = {}
        for evt in self._store.temporal_events(scope_id):
            if evt["event_type"] != "evidence.projection":
                continue
            payload = _try_parse_json(evt.get("payload"))
            if not isinstance(payload, dict) or payload.get("domain") != "spatial":
                continue
            entity_id = payload.get("entity_id")
            if not entity_id:
                continue
            events_by_entity.setdefault(entity_id, []).append(
                {
                    "sequence": int(evt["sequence"]),
                    "env_step": payload.get("env_step"),
                    "field_name": payload.get("field_name"),
                    "value": payload.get("value"),
                    "provenance": payload.get("provenance", ""),
                    "confidence": float(payload.get("confidence", 1.0)),
                    "actor_id": evt["actor_id"],
                    "occurred_at": evt["occurred_at"],
                    "worker_task_id": evt.get("worker_task_id"),
                    "dispatch_id": evt.get("dispatch_id"),
                }
            )

        rows: list[tuple[int, dict[str, Any]]] = []
        for entity_id, evs in events_by_entity.items():
            evs.sort(key=lambda e: e["sequence"])
            state: dict[str, dict[str, Any]] = {}
            entity_type = entity_types.get(entity_id, "unknown")
            for ev in evs:
                _apply_claim(state, ev)
                rows.append(
                    (
                        ev["sequence"],
                        self._semantic_map_line(entity_id, entity_type, ev, state),
                    )
                )
        rows.sort(key=lambda r: r[0])
        return [line for _seq, line in rows]

    def _semantic_map_line(
        self,
        entity_id: str,
        entity_type: str,
        ev: dict[str, Any],
        state: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """One semantic_map line: ``object`` = Spatial projection after ``ev``."""
        env_step = ev["env_step"]
        position = state["position"]["value"] if "position" in state else None
        attributes = {
            name: f["value"] for name, f in sorted(state.items()) if name != "position"
        }
        field_steps = {
            name: f["env_step"]
            for name, f in sorted(state.items())
            if f.get("env_step") is not None
        }
        conflict_fields = [
            name for name, f in sorted(state.items()) if f.get("conflict_event_ids")
        ]
        conflicts = [
            {
                "field_name": name,
                "value": state[name]["value"],
                "candidates": state[name].get("conflict_candidates") or [],
            }
            for name in conflict_fields
        ]
        source_task_id = ev.get("worker_task_id") or ev.get("dispatch_id") or ""
        observation = {
            "reporter": ev["actor_id"],
            "step": env_step,
            "object_type": entity_type,
            "name": entity_id,
            "position": position,
            "attributes": attributes,
            "confidence": ev["confidence"],
            "source_task_id": source_task_id,
            "note": "",
        }
        obj = {
            "object_type": entity_type,
            "name": entity_id,
            "position": position,
            "attributes": attributes,
            "status": "unknown",
            "last_seen_step": env_step,
            "last_seen_ts": ev["occurred_at"],
            "sources": [
                {
                    "reporter": ev["actor_id"],
                    "task_id": source_task_id,
                    "step": env_step,
                    "confidence": ev["confidence"],
                    "note": "",
                }
            ],
            "confidence": max(
                (float(f["confidence"]) for f in state.values()), default=1.0
            ),
            "conflict": bool(conflict_fields),
            "conflicts": conflicts,
            "field_last_seen_steps": field_steps,
        }
        return {
            "ts": ev["occurred_at"],
            "event_type": "observation_ingested",
            "observation": observation,
            "object": obj,
        }

    # ── export + manifest ─────────────────────────────────────────────────

    def _build_manifest(
        self,
        scope_id: str,
        revision: int,
        entries: dict[str, ExportArtifactEntry],
        exported_at: str,
        legacy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "scope_id": scope_id,
            "canonical_revision": revision,
            "exported_at": exported_at,
            "artifacts": {name: entries[name].to_dict() for name in sorted(entries)},
            "legacy_artifacts": dict(legacy or {}),
        }

    def export_scope(
        self, scope_id: str, export_dir: Path, *, exported_at: str | None = None
    ) -> MemoryExportReport:
        """Deterministically materialize all canonical artifacts for a scope.

        Atomic-write contract per ``(scope_id, artifact_kind,
        canonical_revision)``: content is built in memory, written to a temp
        file and fsynced, the manifest digest is written, then ``os.replace``
        moves each temp to its final path.  No retention purge, no append to
        existing files.
        """
        if not self._store.scope_exists(scope_id):
            raise MemoryExportError(f"unknown_scope: {scope_id}")
        export_dir = Path(export_dir)
        revision = self._store.revision_of(scope_id)
        artifacts = self.build_artifacts(scope_id)
        content = {
            name: render_jsonl_bytes(records) for name, records in artifacts.items()
        }
        temps: dict[str, Path] = {}
        for name, data in content.items():
            temps[name] = _write_temp(export_dir, name, data)
        entries = {
            name: ExportArtifactEntry(
                artifact_kind=name,
                scope_id=scope_id,
                canonical_revision=revision,
                record_count=len(artifacts[name]),
                payload_sha256=digest_payload(artifacts[name]),
                artifact_sha256=digest_bytes(data),
            )
            for name, data in content.items()
        }
        now = exported_at or datetime.now(timezone.utc).isoformat()
        manifest_payload = self._build_manifest(scope_id, revision, entries, now)
        manifest_bytes = json.dumps(
            manifest_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        manifest_path = export_dir / EXPORT_MANIFEST_FILENAME
        _atomic_replace(manifest_bytes, manifest_path)
        for name, tmp in temps.items():
            os.replace(tmp, export_dir / name)
        _fsync_dir(export_dir)
        return MemoryExportReport(
            scope_id=scope_id,
            canonical_revision=revision,
            exported_at=now,
            artifacts=entries,
            manifest_sha256=digest_bytes(manifest_bytes),
            manifest_path=manifest_path,
        )

    def mark_legacy_unmigrated(
        self,
        export_dir: Path,
        legacy_artifacts: dict[str, str],
        *,
        scope_id: str | None = None,
    ) -> Path:
        """Keep read-only legacy JSONL/NDJSON artifacts marked unmigrated.

        Historical artifacts whose scope/identity cannot be unambiguously
        resolved are NEVER backfilled into canonical Memory.  They stay on disk
        as read-only legacy artifacts and the manifest marks each with
        ``legacy_unmigrated``.  Nothing is deleted.
        """
        export_dir = Path(export_dir)
        manifest = load_manifest(export_dir)
        if manifest is None:
            manifest = {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "scope_id": scope_id,
                "canonical_revision": 0,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "artifacts": {},
                "legacy_artifacts": {},
            }
        legacy = dict(manifest.get("legacy_artifacts") or {})
        for filename, reason in legacy_artifacts.items():
            legacy[filename] = {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "artifact_kind": filename,
                "legacy_unmigrated": True,
                "reason": reason,
            }
        manifest["legacy_artifacts"] = legacy
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        manifest_path = export_dir / EXPORT_MANIFEST_FILENAME
        _atomic_replace(manifest_bytes, manifest_path)
        return manifest_path
