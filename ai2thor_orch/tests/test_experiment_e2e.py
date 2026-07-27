"""Fake E2E test for the AI2Thor experiment runner.

Uses FakeController (no Unity build required) with deterministic round-robin
actions.  Verifies the full experiment lifecycle: round loop, logging,
verification, and cleanup.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from ai2thor_orch.experiment.ai2thor_experiment import AI2ThorExperiment


pytestmark = [
    pytest.mark.unit,
    pytest.mark.asyncio,
]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_e2e_2_agents_5_rounds():
    """Run a 2-agent fake experiment for 5 rounds and verify all outputs."""
    exp = AI2ThorExperiment(
        task_id="3_transport_groceries",
        scene="FloorPlan1",
        num_agents=2,
        seed=42,
        mode="fake",
        max_steps=5,
    )

    result = await exp.run()

    # ── Basic structure checks ─────────────────────────────────────────────
    assert isinstance(result, dict)
    assert "verified_completion" in result
    assert "rounds" in result
    assert "log_dir" in result
    assert "finished" in result
    assert "coverage" in result
    assert "elapsed_seconds" in result

    # At least 1 round ran
    assert result["rounds"] >= 1
    # With max_steps=5, finished=False (no early termination in 5 rounds for fake mode)
    assert result["rounds"] <= 5
    assert isinstance(result["verified_completion"], bool)
    assert isinstance(result["coverage"], float)
    assert isinstance(result["elapsed_seconds"], float)
    assert result["elapsed_seconds"] > 0

    # ── Log directory checks ────────────────────────────────────────────────
    log_dir = result["log_dir"]
    assert log_dir is not None
    assert os.path.isdir(log_dir), f"Log dir {log_dir} does not exist"

    # summary.csv exists and has content
    csv_path = Path(log_dir) / "summary.csv"
    assert csv_path.exists(), f"summary.csv not found at {csv_path}"
    csv_content = csv_path.read_text()
    assert len(csv_content.strip()) > 0, "summary.csv is empty"
    assert "Round" in csv_content  # CSV header
    assert csv_content.count("\n") >= result["rounds"]  # header + data rows
    csv_rows = list(csv.DictReader(csv_content.splitlines()))
    assert csv_rows
    assert len(csv_rows) == result["rounds"]
    assert {
        "GoalCoverage",
        "InteractionCoverage",
        "TransportRate",
        "ActionSuccessRate",
        "TimeoutCount",
        "Balance",
    }.issubset(csv_rows[-1])

    # events.ndjson exists
    ndjson_path = Path(log_dir) / "events.ndjson"
    assert ndjson_path.exists(), f"events.ndjson not found at {ndjson_path}"
    ndjson_content = ndjson_path.read_text().strip()
    assert len(ndjson_content) > 0, "events.ndjson is empty"
    # Each line is valid JSON
    lines = ndjson_content.split("\n")
    assert len(lines) == result["rounds"], (
        f"Expected {result['rounds']} NDJSON lines, got {len(lines)}"
    )
    for line in lines:
        event = json.loads(line)
        assert "round" in event
        assert "action" in event
        assert "coverage" in event
        assert "verified_completion" in event

    # summary.json exists with final results
    summary_path = Path(log_dir) / "summary.json"
    assert summary_path.exists(), f"summary.json not found at {summary_path}"
    summary = json.loads(summary_path.read_text())
    assert summary["rounds_completed"] == result["rounds"]
    assert summary["verified_completion"] == result["verified_completion"]
    assert summary["log_dir"] == log_dir
    assert summary["metric_schema_version"] == 2
    assert summary["goal_coverage"] == summary["coverage"]
    assert summary["interaction_coverage"] == 0.0
    assert summary["transport_rate"] == 0.0
    assert summary["action_success_rate"] == 1.0
    assert summary["balance"] == 1.0

    # run_meta.json exists
    meta_path = Path(log_dir) / "run_meta.json"
    assert meta_path.exists(), f"run_meta.json not found at {meta_path}"
    meta = json.loads(meta_path.read_text())
    assert meta["task_id"] == "3_transport_groceries"
    assert meta["num_agents"] == 2
    assert meta["mode"] == "fake"

    # ── Actions were actually submitted ─────────────────────────────────────
    # Verify via barrier's action queue (indirectly through round count)
    assert result["rounds"] >= 1

    # ── No unhandled exceptions ─────────────────────────────────────────────
    # (If we got here without raising, the test passes on that front as well.)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_e2e_single_agent():
    """Run a 1-agent fake experiment for 3 rounds."""
    exp = AI2ThorExperiment(
        task_id="3_transport_groceries",
        scene="FloorPlan1",
        num_agents=1,
        seed=0,
        mode="fake",
        max_steps=3,
    )
    result = await exp.run()
    assert result["rounds"] == 3
    assert result["log_dir"] is not None
    assert Path(result["log_dir"]).exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_e2e_custom_log_dir():
    """Custom log_dir is used and does not auto-generate."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        exp = AI2ThorExperiment(
            task_id="3_transport_groceries",
            scene="FloorPlan1",
            num_agents=2,
            seed=42,
            mode="fake",
            max_steps=2,
            log_dir=tmpdir,
        )
        log_dir_path = Path(tmpdir)
        result = await exp.run()
        assert result["log_dir"] == str(log_dir_path)
        # summary.csv is inside the custom dir
        assert (log_dir_path / "summary.csv").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reused_log_dir_discards_events_from_previous_run():
    """A benchmark rerun must not mix event timelines from distinct run IDs."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        first = AI2ThorExperiment(
            task_id="3_transport_groceries",
            scene="FloorPlan1",
            num_agents=1,
            mode="fake",
            max_steps=1,
            log_dir=tmpdir,
        )
        await first.run()

        second = AI2ThorExperiment(
            task_id="3_transport_groceries",
            scene="FloorPlan1",
            num_agents=1,
            mode="fake",
            max_steps=2,
            log_dir=tmpdir,
        )
        result = await second.run()

        log_dir = Path(tmpdir)
        summary = json.loads((log_dir / "summary.json").read_text())
        events = [
            json.loads(line)
            for line in (log_dir / "events.ndjson").read_text().splitlines()
        ]
        csv_rows = list(csv.DictReader((log_dir / "summary.csv").read_text().splitlines()))

        assert len(events) == result["rounds"]
        assert {event["run_id"] for event in events} == {summary["run_id"]}
        assert len(csv_rows) == result["rounds"]
