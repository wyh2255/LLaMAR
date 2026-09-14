"""Balance (original-paper load balance) aggregation tests.

B = min(s_i) / (max(s_i) + 1e-4), s_i = agent i's successful non-NoOp action
count read from the run's trajectory.csv.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.aggregate import (
    aggregate,
    compute_balance,
    read_agent_success_counts,
    seed_balance,
)

TRAJECTORY_FIELDS = [
    "Step",
    "Actions",
    "Successes",
    "Coverage",
    "TransportRate",
    "Finished",
]

EXPECTED_TSV_FIELDS = [
    "scene",
    "agents",
    "seed",
    "steps",
    "balance",
    "coverage",
    "success_rate",
    "transport_rate",
    "end_reason",
    "failure_class",
    "max_steps",
    "elapsed_seconds",
    "run_id",
    "model",
    "prompt_version",
]


def _write_trajectory(path: Path, steps: list[tuple[list[str], list[bool]]]) -> None:
    """Write a trajectory.csv; ``steps`` is one (actions, successes) pair per step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRAJECTORY_FIELDS)
        writer.writeheader()
        for index, (actions, successes) in enumerate(steps, start=1):
            writer.writerow(
                {
                    "Step": index,
                    "Actions": actions,
                    "Successes": successes,
                    "Coverage": 0.5,
                    "TransportRate": 0.25,
                    "Finished": False,
                }
            )


def _write_result(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "finished": False,
                "steps": 10,
                "max_steps": 10,
                "coverage": 0.5,
                "transport_rate": 0.25,
                "elapsed_seconds": 12.0,
                "end_reason": "max_steps_reached",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )


def _seed_dir(
    tmp_path: Path, *, scene: int = 1, agents: int = 2, name: str = "seed_42"
) -> Path:
    directory = tmp_path / "benchmark" / f"scene_{scene}" / f"agents_{agents}" / name
    directory.mkdir(parents=True)
    return directory


def _read_rows(output: Path) -> list[dict]:
    with output.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_balanced_agents_balance_close_to_one(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path)
    _write_trajectory(
        seed_dir / "trajectory.csv",
        [
            (["Explore()", "GetSupply()"], [True, True]),
            (["Explore()", "GetSupply()"], [True, True]),
            (["NavigateTo(1, 2, 3)", "UseSupply()"], [True, True]),
        ],
    )

    assert read_agent_success_counts(seed_dir / "trajectory.csv") == [3, 3]

    balance = seed_balance(seed_dir)
    assert balance is not None
    assert balance < 1.0
    assert abs(balance - 3 / 3.0001) < 1e-12


def test_zero_success_agent_gives_zero_balance(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path)
    _write_trajectory(
        seed_dir / "trajectory.csv",
        [
            (["Explore()", "Explore()"], [True, False]),
            (["NavigateTo(0, 0, 0)", "Explore()"], [True, False]),
        ],
    )

    assert seed_balance(seed_dir) == 0.0


def test_noop_is_never_counted(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path)
    _write_trajectory(
        seed_dir / "trajectory.csv",
        [
            (["NoOp()", "NoOp()"], [True, True]),  # idle heartbeat / timeout fill
            (["NoOp()", "Explore()"], [True, True]),  # llm-chosen NoOp vs real action
            (["GetSupply()", "NoOp()"], [True, True]),
        ],
    )

    assert read_agent_success_counts(seed_dir / "trajectory.csv") == [1, 1]


def test_all_zero_success_counts_yield_null_balance():
    assert compute_balance([0, 0]) is None
    assert compute_balance([]) is None
    assert compute_balance(None) is None


def test_missing_trajectory_leaves_balance_null(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path)
    _write_result(seed_dir / "result.json")

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))

    rows = _read_rows(output)
    assert len(rows) == 1
    assert rows[0]["balance"] == ""
    assert list(rows[0].keys()) == EXPECTED_TSV_FIELDS


def test_balance_read_from_benchmark_experiment_logs(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path)
    _write_result(seed_dir / "result.json")
    _write_trajectory(
        seed_dir / "experiment_logs" / "trajectory.csv",
        [
            (["Explore()", "NoOp()"], [True, True]),
            (["NoOp()", "Explore()"], [True, True]),
        ],
    )

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))

    rows = _read_rows(output)
    assert len(rows) == 1
    assert abs(float(rows[0]["balance"]) - 1 / 1.0001) < 1e-12


def test_retry_backup_dirs_are_skipped(tmp_path: Path):
    seed_dir = _seed_dir(tmp_path, name="seed_0")
    _write_result(seed_dir / "result.json")
    _write_trajectory(
        seed_dir / "trajectory.csv",
        [(["Explore()", "GetSupply()"], [True, True])],
    )
    backup_dir = _seed_dir(tmp_path, name="seed_0_pass_0")
    _write_result(backup_dir / "result.json")

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))

    rows = _read_rows(output)
    assert [row["seed"] for row in rows] == ["0"]


def test_unparsable_rows_are_ignored_and_warned(tmp_path: Path, capsys):
    seed_dir = _seed_dir(tmp_path)
    # Real corruption shape found in the 07-05 sweep grid: the header line is
    # duplicated as a data row, so every cell holds a bare column name.
    (seed_dir / "trajectory.csv").write_text(
        '"Step","Actions","Successes"\n"Step","Actions","Successes"\n',
        encoding="utf-8",
    )

    assert read_agent_success_counts(seed_dir / "trajectory.csv") is None
    assert seed_balance(seed_dir) is None

    captured = capsys.readouterr().out
    assert "unparsable trajectory row" in captured


def test_agent_count_mismatch_warns_and_uses_list_length(tmp_path: Path, capsys):
    seed_dir = _seed_dir(tmp_path)
    _write_trajectory(
        seed_dir / "trajectory.csv",
        [(["Explore()", "GetSupply()"], [True, True])],
    )

    balance = seed_balance(seed_dir, {"agent_count": 4})

    assert abs(balance - 1 / 1.0001) < 1e-12  # computed from the 2-agent lists
    captured = capsys.readouterr().out
    assert "metadata agent_count=4" in captured
    assert "list length 2" in captured
