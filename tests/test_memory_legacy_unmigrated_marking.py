"""Phase 5 legacy-unmigrated marking surfaces (coordinator stores).

The legacy debug adapters (``EventStore``'s ``events_<task>.ndjson`` and
``SupervisionStateStore``'s ``supervision_<dispatch>.ndjson``) are NOT
canonical Memory exports and are never rewritten by the Memory exporter.  They
stay read-only on disk; the run-terminal export manifest marks each with
``legacy_unmigrated``.  These tests lock the enumeration surface the
CoordinatorServer uses for that marking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a.coordinator.event_store import EventStore
from a2a.coordinator.supervision_state_store import SupervisionStateStore


@pytest.mark.unit
def test_event_store_legacy_artifact_filenames_empty_without_log_dir(tmp_path):
    store = EventStore()  # no log_dir -> no legacy artifacts enumerated
    assert store.legacy_artifact_filenames() == []


@pytest.mark.unit
def test_event_store_legacy_artifact_filenames_matches_written_files(tmp_path):
    log_dir = tmp_path / "coord"
    log_dir.mkdir()
    store = EventStore(log_dir=str(log_dir))
    store.append("dsp_1", "status_update", state="WORKING")
    store.append("dsp_2", "artifact_update", text="some artifact")
    names = store.legacy_artifact_filenames()
    assert names == ["events_dsp_1.ndjson", "events_dsp_2.ndjson"]
    # Files stay on disk untouched (read-only legacy artifacts).
    assert (log_dir / "events_dsp_1.ndjson").is_file()
    assert (log_dir / "events_dsp_2.ndjson").is_file()


@pytest.mark.unit
def test_supervision_store_legacy_artifact_filenames(tmp_path):
    log_dir = tmp_path / "supervision"
    log_dir.mkdir()
    store = SupervisionStateStore(log_dir=str(log_dir))
    state = store.get_or_create("dsp_a", worker_id="alice")
    store.update("dsp_a", state)
    names = store.legacy_artifact_filenames()
    assert names == ["supervision_dsp_a.ndjson"]
    assert (log_dir / "supervision_dsp_a.ndjson").is_file()


@pytest.mark.unit
def test_supervision_store_legacy_artifact_filenames_empty_without_log_dir():
    store = SupervisionStateStore()
    assert store.legacy_artifact_filenames() == []


@pytest.mark.unit
def test_event_store_sorted_filenames_ignore_other_files(tmp_path):
    log_dir = tmp_path / "mixed"
    log_dir.mkdir()
    (log_dir / "unrelated.ndjson").write_text("{}", encoding="utf-8")
    store = EventStore(log_dir=str(log_dir))
    store.append("dsp_b", "status_update", state="COMPLETED")
    store.append("dsp_a", "status_update", state="WORKING")
    assert store.legacy_artifact_filenames() == [
        "events_dsp_a.ndjson",
        "events_dsp_b.ndjson",
    ]


@pytest.mark.unit
def test_legacy_artifact_path_helpers_return_relative_names_only(tmp_path):
    """The manifest marks filenames, never absolute paths (no traversal)."""
    log_dir = tmp_path / "nested"
    log_dir.mkdir()
    store = EventStore(log_dir=str(log_dir))
    store.append("dsp_x", "status_update", state="WORKING")
    for name in store.legacy_artifact_filenames():
        assert "/" not in name and ".." not in name
        assert Path(name).name == name
