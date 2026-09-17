"""The reader: complete run directories grade, absent or corrupt ones do not."""

from __future__ import annotations

import json

import pytest
from reef_sar_adapter import trajectory
from reef_sar_adapter.trajectory import read_sar_run

RUN_METRICS = {
    "run_id": "sar-scene3-agents2-seed0-099a58c3",
    "steps": 35,
    "coverage": 0.42,
    "transport_rate": 0.5,
    "finished": False,
    "end_reason": "max_steps",
}
EVAL_METRICS = {
    "meta": {"steps": 35},
    "l2_planning": {"load_balance_b": 0.6},
    "l4_cost": {"effective_billed_tokens": 512345},
}


def _write(run_dir, name: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (run_dir / name).write_text(text, encoding="utf-8")


def _run_dir(tmp_path, *, metrics=RUN_METRICS, eval_metrics=EVAL_METRICS):
    run_dir = tmp_path / "sar" / "out"
    run_dir.mkdir(parents=True)
    if metrics is not None:
        _write(run_dir, "run_metrics.json", metrics)
    if eval_metrics is not None:
        _write(run_dir, "eval_metrics.json", eval_metrics)
    return run_dir


def test_complete_run_reads_two_events(tmp_path):
    events = read_sar_run(_run_dir(tmp_path))
    assert [event["type"] for event in events] == ["metrics", "run_meta"]
    metrics, meta = events
    assert metrics["run_metrics"] == RUN_METRICS
    assert metrics["eval_metrics"] == EVAL_METRICS
    assert meta["steps"] == 35
    assert meta["end_reason"] == "max_steps"


def test_missing_directory_reads_empty(tmp_path):
    assert read_sar_run(tmp_path / "not-a-run") == ()


@pytest.mark.parametrize("absent", ["run_metrics.json", "eval_metrics.json"])
def test_half_a_run_reads_empty(tmp_path, absent):
    """Either metric file missing means nothing gradeable: no fabricated half-score."""
    run_dir = _run_dir(tmp_path)
    (run_dir / absent).unlink()
    assert read_sar_run(run_dir) == ()


@pytest.mark.parametrize("name", ["run_metrics.json", "eval_metrics.json"])
def test_corrupt_file_raises(tmp_path, name):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, name, '{"steps": 3,')
    with pytest.raises(trajectory.TrajectoryError):
        read_sar_run(run_dir)


@pytest.mark.parametrize("name", ["run_metrics.json", "eval_metrics.json"])
def test_non_object_file_raises(tmp_path, name):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, name, [1, 2, 3])
    with pytest.raises(trajectory.TrajectoryError):
        read_sar_run(run_dir)


def test_run_meta_prefers_the_runners_own_file(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, "run_meta.json", {"scene": 3, "agents": 2, "seed": 0})
    _write(run_dir, "metadata.json", {"scene": 9, "agent_count": 9, "seed": 9})
    meta = read_sar_run(run_dir)[1]
    assert (meta["scene"], meta["agents"], meta["seed"]) == (3, 2, 0)


def test_run_meta_falls_back_to_the_experiments_metadata(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, "metadata.json", {"scene": 3, "agent_count": 4, "seed": 10, "model": "m"})
    meta = read_sar_run(run_dir)[1]
    assert (meta["scene"], meta["agents"], meta["seed"]) == (3, 4, 10)


def test_run_meta_without_a_source_leaves_the_identity_empty(tmp_path):
    meta = read_sar_run(_run_dir(tmp_path))[1]
    assert meta["scene"] is None and meta["agents"] is None and meta["seed"] is None
    assert meta["steps"] == 35  # run facts still come from run_metrics.json


def test_extra_artifacts_are_ignored(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, "summary.csv", "Step,Coverage\n1,0.1\n")
    _write(run_dir, "config.json", {"llm": {"model": "x"}})
    assert len(read_sar_run(run_dir)) == 2
