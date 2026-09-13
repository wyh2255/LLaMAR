"""Phase 5 compatibility materialization contracts (memory exporter).

Covers: deterministic export (same canonical set -> identical artifact bytes),
export manifest/digest contract, ``semantic_map.jsonl`` fixture field/order
compatibility with the legacy writer and the real ``render_sar_report``
consumer, and temp+fsync+replace atomic writes with no leftover temp files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.exporter import (
    CANONICAL_ARTIFACTS,
    EXPORT_MANIFEST_FILENAME,
    MemoryExporter,
    MemoryExportError,
    load_manifest,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "render-sar-report"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

try:
    from render_sar_report.loaders import (
        load_semantic_map,  # type: ignore[import-not-found]
    )

    RENDER_LOADER_AVAILABLE = True
except (ImportError, ModuleNotFoundError):  # pragma: no cover - environment dependent
    load_semantic_map = None  # type: ignore[assignment]
    RENDER_LOADER_AVAILABLE = False

pytestmark = pytest.mark.unit


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


@pytest.fixture
def scope_id(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


@pytest.fixture
def exporter(store):
    return MemoryExporter(store)


def _spatial_input(
    scope_id: str,
    *,
    event_id: str,
    env_step: int,
    entity_id: str = "FireA",
    entity_type: str = "fire",
    field_name: str = "intensity",
    value: Any = "High",
    provenance: str = "worker_sensor_tool",
    actor_id: str = "alice",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id=actor_id,
        provenance=provenance,
        domain="spatial",
        entity_id=entity_id,
        entity_type=entity_type,
        field_name=field_name,
        value=value,
    )


def _seed_scope(ingestor, scope_id) -> None:
    """A deterministic canonical set: FireA position/intensity across steps."""
    ingestor.ingest_projection(
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
    ingestor.ingest_projection(
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
    ingestor.ingest_projection(
        [
            _spatial_input(
                scope_id,
                event_id="evt_3",
                env_step=9,
                field_name="intensity",
                value="Low",
            )
        ]
    )


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Determinism + atomicity
# ---------------------------------------------------------------------------


def test_export_is_byte_deterministic(store, exporter, ingestor, scope_id, tmp_path):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    fixed_at = "2026-08-08T00:00:00+00:00"

    first = exporter.export_scope(scope_id, export_dir, exported_at=fixed_at)
    second = exporter.export_scope(scope_id, export_dir, exported_at=fixed_at)

    for name in CANONICAL_ARTIFACTS:
        assert (export_dir / name).read_bytes() == (export_dir / name).read_bytes()
        assert (
            first.artifacts[name].artifact_sha256
            == second.artifacts[name].artifact_sha256
        )
        assert (
            first.artifacts[name].payload_sha256
            == second.artifacts[name].payload_sha256
        )
    assert first.manifest_sha256 == second.manifest_sha256


def test_no_leftover_temp_files_after_export(
    store, exporter, ingestor, scope_id, tmp_path
):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    exporter.export_scope(scope_id, export_dir)
    leftovers = [p.name for p in export_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_replace_overwrites_existing_artifacts(
    store, exporter, ingestor, scope_id, tmp_path
):
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "semantic_map.jsonl").write_text("GARBAGE\n", encoding="utf-8")
    _seed_scope(ingestor, scope_id)
    exporter.export_scope(scope_id, export_dir)
    records = _read_records(export_dir / "semantic_map.jsonl")
    assert records and records[0]["event_type"] == "observation_ingested"


def test_export_unknown_scope_raises(exporter, tmp_path):
    with pytest.raises(MemoryExportError):
        exporter.export_scope("missing-scope-id", tmp_path / "export")


# ---------------------------------------------------------------------------
# Manifest + digest contract
# ---------------------------------------------------------------------------


def test_manifest_entries_contract(store, exporter, ingestor, scope_id, tmp_path):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    report = exporter.export_scope(scope_id, export_dir)

    manifest = load_manifest(export_dir)
    assert manifest is not None
    assert manifest["scope_id"] == scope_id
    assert manifest["canonical_revision"] == store.revision_of(scope_id)
    assert set(manifest["artifacts"]) == set(CANONICAL_ARTIFACTS)

    for name in CANONICAL_ARTIFACTS:
        entry = manifest["artifacts"][name]
        for key in (
            "schema_version",
            "scope_id",
            "artifact_kind",
            "canonical_revision",
            "record_count",
            "payload_sha256",
            "artifact_sha256",
        ):
            assert key in entry, f"manifest entry {name} missing {key}"
        assert entry["artifact_kind"] == name
        assert entry["scope_id"] == scope_id
        assert entry["canonical_revision"] == store.revision_of(scope_id)
        # record_count matches actual lines on disk
        assert entry["record_count"] == len(_read_records(export_dir / name))
        # artifact_sha256 matches the actual file bytes
        actual = hashlib_sha256((export_dir / name).read_bytes())
        assert entry["artifact_sha256"] == actual
        # manifest entry matches the in-memory report
        assert entry == report.artifacts[name].to_dict()


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Artifact contents
# ---------------------------------------------------------------------------


def test_artifact_contents(store, exporter, ingestor, scope_id, tmp_path):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    exporter.export_scope(scope_id, export_dir)

    temporal = _read_records(export_dir / "temporal.jsonl")
    # 3 evidence events, ordered by canonical sequence
    assert [e["sequence"] for e in temporal] == [1, 2, 3]
    assert all(e["event_type"] == "evidence.projection" for e in temporal)
    assert temporal[0]["payload"]["field_name"] == "position"
    assert temporal[0]["payload"]["value"] == [4, 4, 0]

    spatial = _read_records(export_dir / "spatial.jsonl")
    by_field = {f["field_name"]: f for f in spatial}
    assert by_field["position"]["value"] == [4, 4, 0]
    assert by_field["intensity"]["value"] == "Low"
    assert by_field["intensity"]["env_step"] == 9

    revision = _read_records(export_dir / "revision.jsonl")
    kinds = [r["kind"] for r in revision]
    assert "scope" in kinds and "view_revision" in kinds
    assert any(
        r["kind"] == "entity_revision" and r["domain"] == "spatial" for r in revision
    )
    scope_rev = next(r for r in revision if r["kind"] == "scope")
    assert scope_rev["revision"] == store.revision_of(scope_id)

    outbox = _read_records(export_dir / "outbox.jsonl")
    assert len(outbox) == 3
    assert all(e["status"] == "pending" for e in outbox)
    assert all(e["scope_id"] == scope_id for e in outbox)

    relations = _read_records(export_dir / "relations.jsonl")
    assert relations
    assert all(r["scope_id"] == scope_id for r in relations)
    assert all(r["relation_type"] in ("about",) for r in relations)

    embodied = _read_records(export_dir / "embodied.jsonl")
    assert embodied == []


# ---------------------------------------------------------------------------
# semantic_map.jsonl fixture compatibility
# ---------------------------------------------------------------------------


def test_semantic_map_fixture_field_and_order_compatibility(
    store, exporter, ingestor, scope_id, tmp_path
):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    exporter.export_scope(scope_id, export_dir)
    lines = _read_records(export_dir / "semantic_map.jsonl")

    # Order follows canonical sequence (env_step then sequence).
    assert len(lines) == 3
    assert [l["observation"]["step"] for l in lines] == [8, 8, 9]

    # Frozen top-level schema/field order (legacy writer emits the same keys).
    for line in lines:
        assert list(line.keys()) == ["ts", "event_type", "observation", "object"]
        assert line["event_type"] == "observation_ingested"
        assert list(line["observation"].keys()) == [
            "reporter",
            "step",
            "object_type",
            "name",
            "position",
            "attributes",
            "confidence",
            "source_task_id",
            "note",
        ]
        # object carries the full legacy-compatible shape.
        obj = line["object"]
        for key in (
            "object_type",
            "name",
            "position",
            "attributes",
            "status",
            "last_seen_step",
            "last_seen_ts",
            "sources",
            "confidence",
            "conflict",
            "conflicts",
            "field_last_seen_steps",
        ):
            assert key in obj, f"semantic_map object missing {key}"

    # As-of Spatial projection semantics.
    first, second, third = lines
    # evt_1 (position, step 8): object carries position but no intensity yet.
    assert first["observation"]["name"] == "FireA"
    assert first["observation"]["position"] == [4, 4, 0]
    assert first["observation"]["attributes"] == {}
    assert first["object"]["attributes"] == {}
    # evt_2 (intensity High, step 8): as-of projection now has both fields.
    assert second["object"]["position"] == [4, 4, 0]
    assert second["object"]["attributes"]["intensity"] == "High"
    assert "position" not in second["object"]["attributes"]
    # evt_3 (intensity Low, step 9): newer value wins.
    assert third["object"]["attributes"]["intensity"] == "Low"

    # observation requires reporter/step/object_type/name/position/attributes/
    # confidence/source_task_id/note.
    for line in lines:
        obs = line["observation"]
        assert obs["reporter"] == "alice"
        assert obs["object_type"] == "fire"
        assert "confidence" in obs
        assert "note" in obs


@pytest.mark.skipif(not RENDER_LOADER_AVAILABLE, reason="render_sar_report unavailable")
def test_semantic_map_readable_by_render_loader(
    store, exporter, ingestor, scope_id, tmp_path
):
    _seed_scope(ingestor, scope_id)
    export_dir = tmp_path / "export"
    exporter.export_scope(scope_id, export_dir)
    objects = load_semantic_map(export_dir)  # type: ignore[misc]
    assert len(objects) == 1
    obj = objects[0]
    assert obj.name == "FireA"
    assert obj.object_type == "fire"
    assert len(obj.observations) == 3


def test_legacy_unmigrated_marking_keeps_files_read_only(
    store, exporter, scope_id, tmp_path
):
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = export_dir / "semantic_map.jsonl"
    legacy_path.write_text('{"ts": 1, "legacy": true}\n', encoding="utf-8")

    exporter.mark_legacy_unmigrated(
        export_dir,
        {"semantic_map.jsonl": "ambiguous scope identity in historical artifact"},
    )

    # The file stays on disk untouched (no deletion, no backfill).
    assert legacy_path.read_text(encoding="utf-8") == '{"ts": 1, "legacy": true}\n'
    manifest = load_manifest(export_dir)
    assert manifest is not None
    entry = manifest["legacy_artifacts"]["semantic_map.jsonl"]
    assert entry["legacy_unmigrated"] is True
    assert "ambiguous scope identity" in entry["reason"]
    assert "semantic_map.jsonl" not in manifest["artifacts"]


def test_export_dir_missing_is_created(store, exporter, ingestor, scope_id, tmp_path):
    _seed_scope(ingestor, scope_id)
    nested = tmp_path / "a" / "b" / "export"
    exporter.export_scope(scope_id, nested)
    assert (nested / EXPORT_MANIFEST_FILENAME).exists()
    assert os.path.isdir(nested)
