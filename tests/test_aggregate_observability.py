from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.aggregate import aggregate


def test_aggregate_includes_observability_columns(tmp_path: Path):
    run_dir = tmp_path / "benchmark" / "scene_1" / "agents_2" / "seed_42"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "finished": False,
                "steps": 10,
                "max_steps": 10,
                "coverage": 0.5,
                "transport_rate": 0.25,
                "elapsed_seconds": 123.0,
                "end_reason": "max_steps_reached",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metadata.json").write_text(
        json.dumps({"model": "fake-model", "prompt_version": "baseline"}),
        encoding="utf-8",
    )

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))

    with output.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    assert rows[0]["end_reason"] == "max_steps_reached"
    assert rows[0]["failure_class"] == "budget"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["model"] == "fake-model"
    assert rows[0]["prompt_version"] == "baseline"
