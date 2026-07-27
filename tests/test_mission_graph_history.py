"""MissionGraph replace-history (mission_graph.jsonl) tests."""

from __future__ import annotations

import json

import pytest

from a2a.coordinator.mission_graph import (
    MissionGraph,
    MissionGraphError,
    MissionGraphJsonlLogger,
)


def _spec(task_id: str, **overrides) -> dict:
    spec = {
        "task_id": task_id,
        "participant_ids": ["Alice"],
        "objective": f"objective-{task_id}",
        "depends_on": [],
        "status": "pending",
    }
    spec.update(overrides)
    return spec


class TestMissionGraphHistorySink:
    def test_replace_emits_history_with_diff_and_nodes(self):
        graph = MissionGraph()
        records: list[dict] = []
        graph.set_history_sink(records.append)

        graph.replace([_spec("a"), _spec("b", depends_on=["a"])])

        assert len(records) == 1
        rec = records[0]
        assert rec["revision"] == 1
        assert rec["changed"] is True
        assert sorted(n["logical_id"] for n in rec["nodes"]) == ["a", "b"]
        node_a = next(n for n in rec["nodes"] if n["logical_id"] == "a")
        assert node_a["participant_ids"] == ["Alice"]
        assert node_a["objective"] == "objective-a"
        assert node_a["state"] == "ready"
        node_b = next(n for n in rec["nodes"] if n["logical_id"] == "b")
        assert node_b["depends_on"] == ["a"]
        assert node_b["state"] == "blocked"
        assert [e["logical_id"] for e in rec["diff"]["added"]] == ["a", "b"]
        assert rec["state_counts"] == {"ready": 1, "blocked": 1}

    def test_identical_replace_marks_unchanged(self):
        graph = MissionGraph()
        graph.replace([_spec("a")])
        records: list[dict] = []
        graph.set_history_sink(records.append)

        graph.replace([_spec("a")])

        assert len(records) == 1
        rec = records[0]
        assert rec["changed"] is False
        assert rec["diff"]["preserved"] == ["a"]
        assert rec["diff"]["added"] == []
        assert rec["diff"]["removed"] == []
        assert rec["diff"]["modified"] == []

    def test_semantic_change_marked_as_modified_and_reset(self):
        graph = MissionGraph()
        graph.replace([_spec("a")])
        records: list[dict] = []
        graph.set_history_sink(records.append)

        graph.replace([_spec("a", objective="new-objective")])

        rec = records[0]
        assert rec["changed"] is True
        assert rec["diff"]["reset"] == ["a"]
        assert rec["diff"]["modified"][0]["logical_id"] == "a"
        changes = rec["diff"]["modified"][0]["changes"]
        assert changes["objective"]["before"] == "objective-a"
        assert changes["objective"]["after"] == "new-objective"

    def test_failed_replace_emits_nothing(self):
        graph = MissionGraph()
        graph.replace([_spec("a")])
        records: list[dict] = []
        graph.set_history_sink(records.append)

        with pytest.raises(MissionGraphError):
            graph.replace([_spec("a"), _spec("b", depends_on=["missing"])])

        assert records == []
        assert graph.revision == 1

    def test_sink_exception_does_not_break_replace(self):
        graph = MissionGraph()

        def _bad_sink(record: dict) -> None:
            raise RuntimeError("boom")

        graph.set_history_sink(_bad_sink)
        view = graph.replace([_spec("a")])
        assert view["revision"] == 1


class TestMissionGraphJsonlLogger:
    def test_writes_jsonl_with_step(self, tmp_path):
        path = tmp_path / "logs" / "mission_graph.jsonl"
        logger = MissionGraphJsonlLogger(path, step_getter=lambda: 7)
        graph = MissionGraph()
        graph.set_history_sink(logger)

        graph.replace([_spec("a")])
        graph.replace([_spec("a"), _spec("b", depends_on=["a"])])

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["step"] == 7
        assert first["revision"] == 1
        assert first["changed"] is True
        assert "ts" in first
        assert second["revision"] == 2
        assert [e["logical_id"] for e in second["diff"]["added"]] == ["b"]
        assert second["diff"]["preserved"] == ["a"]
        assert [n["logical_id"] for n in second["nodes"]] == ["a", "b"]

    def test_no_step_getter_writes_null_step(self, tmp_path):
        path = tmp_path / "mission_graph.jsonl"
        logger = MissionGraphJsonlLogger(path)
        graph = MissionGraph()
        graph.set_history_sink(logger)

        graph.replace([_spec("a")])

        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["step"] is None

    def test_no_sink_writes_nothing(self, tmp_path):
        graph = MissionGraph()
        view = graph.replace([_spec("a")])
        assert view["revision"] == 1
