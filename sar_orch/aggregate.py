#!/usr/bin/env python3
"""Aggregate benchmark results into paper-format TSV.

Usage:
    uv run python sar_orch/aggregate.py [--input <benchmark_dir>] [--output <tsv_path>]

Output columns (matching original LLaMAR format):
    scene, agents, seed, steps, balance, coverage, success_rate, transport_rate

``balance`` is the original-paper load balance
B = min(s_i) / (max(s_i) + 1e-4), where s_i counts agent i's successful
non-NoOp actions read from the run's ``trajectory.csv``.  When that data is
missing the column is left empty — never back-filled with a fake value.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
from collections.abc import Sequence
from pathlib import Path

_DEFAULT_INPUT = Path(__file__).resolve().parent / "results" / "benchmark"
_DEFAULT_OUTPUT = _DEFAULT_INPUT.parent / "benchmark_aggregated.tsv"

# Canonical benchmark cell directory is seed_<n>; retry attempts are backed up
# by benchmark.py as seed_<n>_pass_<k> and must not be aggregated as runs.
_SEED_DIR_RE = re.compile(r"^seed_(\d+)$")

# trajectory.csv lives at the run dir root when --log-dir points straight at a
# run dir, and under experiment_logs/ inside a benchmark grid cell.
_TRAJECTORY_RELPATHS = ("trajectory.csv", "experiment_logs/trajectory.csv")

_NOOP_ACTION = "NoOp"
_BALANCE_EPSILON = 1e-4


def classify_failure(end_reason: str, finished: bool) -> str:
    if finished:
        return "success"
    if end_reason in {"max_steps_reached", "wall_clock_timeout"}:
        return "budget"
    if end_reason in {
        "framework_error",
        "worker_timeout",
        "workers_dead",
        "coordinator_finished_early",
        "stopped_before_success",
    }:
        return "framework"
    if end_reason == "environment_error":
        return "environment"
    return "unknown"


def parse_action_name(action_repr: str) -> str:
    """Return the bare tool name of a recorded action ("Explore()" -> "Explore")."""
    text = str(action_repr).strip()
    paren = text.find("(")
    return text[:paren].strip() if paren != -1 else text


def compute_balance(success_counts: Sequence[int] | None) -> float | None:
    """Original-paper load balance B = min(s_i) / (max(s_i) + 1e-4).

    ``success_counts`` holds s_i — each agent's successful real action count,
    in agent registration order.  Returns None when the metric is undefined:
    no agents at all, or every agent has zero successful actions (0/0).
    """
    if not success_counts:
        return None
    counts = list(success_counts)
    max_count = max(counts)
    if max_count <= 0:
        return None
    return min(counts) / (max_count + _BALANCE_EPSILON)


def _parse_repr_list(raw: str | None) -> list | None:
    """Parse one per-agent list cell; None when it is not a list literal."""
    if raw is None:
        return None
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    return list(value) if isinstance(value, (list, tuple)) else None


def read_agent_success_counts(trajectory_path: str | Path) -> list[int] | None:
    """Count per-agent successful non-NoOp actions in a run's trajectory.csv.

    ``Actions`` / ``Successes`` are per-agent aligned list cells (order = agent
    registration order).  NoOp never counts as labour, whatever its source
    (llm / idle_heartbeat / timeout_injected).

    Returns None when there is no usable data — file missing, no data rows, or
    no row parsed — never a fabricated value.
    """
    path = Path(trajectory_path)
    if not path.exists():
        return None

    counts: list[int] | None = None
    rows = 0
    skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            actions = _parse_repr_list(row.get("Actions"))
            successes = _parse_repr_list(row.get("Successes"))
            if actions is None or successes is None or len(actions) != len(successes):
                skipped += 1
                continue
            if counts is None:
                counts = [0] * len(actions)
            elif len(actions) != len(counts):
                skipped += 1
                continue
            for index, (action, success) in enumerate(zip(actions, successes)):
                if success is True and parse_action_name(action) != _NOOP_ACTION:
                    counts[index] += 1

    if skipped:
        print(f"Warning: {path}: ignored {skipped}/{rows} unparsable trajectory row(s)")
    return counts


def _declared_agent_count(metadata: dict) -> int | None:
    value = metadata.get("agent_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def seed_balance(seed_dir: str | Path, metadata: dict | None = None) -> float | None:
    """Load balance for one seed/run directory; None when its data is missing.

    Agents come from the per-agent list length; ``metadata.json``'s
    ``agent_count`` is only a cross-check — on conflict the list length wins
    and a warning is printed.
    """
    directory = Path(seed_dir)
    trajectory = next(
        (
            directory / relpath
            for relpath in _TRAJECTORY_RELPATHS
            if (directory / relpath).exists()
        ),
        None,
    )
    counts = read_agent_success_counts(trajectory) if trajectory is not None else None
    if counts is None:
        return None

    declared = _declared_agent_count(metadata or {})
    if declared is not None and declared != len(counts):
        print(
            f"Warning: {directory}: metadata agent_count={declared} != per-agent "
            f"list length {len(counts)}; using list length"
        )
    return compute_balance(counts)


def aggregate(input_dir: str, output_path: str):
    """Scan benchmark results and write aggregated TSV."""
    base = Path(input_dir)
    if not base.exists():
        print(f"Error: input directory not found: {base}")
        return

    rows = []

    # Walk scene_{N}/agents_{N}/seed_{N}/result.json
    for scene_dir in sorted(base.iterdir()):
        if not scene_dir.is_dir() or not scene_dir.name.startswith("scene_"):
            continue
        scene = int(scene_dir.name.replace("scene_", ""))

        for agents_dir in sorted(scene_dir.iterdir()):
            if not agents_dir.is_dir() or not agents_dir.name.startswith("agents_"):
                continue
            agents = int(agents_dir.name.replace("agents_", ""))

            for seed_dir in sorted(agents_dir.iterdir()):
                if not seed_dir.is_dir():
                    continue
                seed_match = _SEED_DIR_RE.match(seed_dir.name)
                if seed_match is None:
                    continue  # retry backups (seed_<n>_pass_<k>) / stray dirs
                seed = int(seed_match.group(1))

                result_file = seed_dir / "result.json"
                if not result_file.exists():
                    continue

                try:
                    with open(str(result_file)) as f:
                        metrics = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                metadata_file = seed_dir / "metadata.json"
                metadata = {}
                if metadata_file.exists():
                    try:
                        with open(str(metadata_file), encoding="utf-8") as f:
                            metadata = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        metadata = {}

                steps = metrics.get("steps", 0)
                coverage = metrics.get("coverage", 0.0)
                transport_rate = metrics.get("transport_rate", 0.0)
                finished = metrics.get("finished", False)
                end_reason = metrics.get("end_reason", "")
                balance = seed_balance(seed_dir, metadata)

                rows.append(
                    {
                        "scene": scene,
                        "agents": agents,
                        "seed": seed,
                        "steps": steps,
                        "balance": "" if balance is None else balance,
                        "coverage": coverage,
                        "success_rate": 1.0 if finished else 0.0,
                        "transport_rate": transport_rate,
                        "end_reason": end_reason,
                        "failure_class": classify_failure(end_reason, finished),
                        "max_steps": metrics.get("max_steps", ""),
                        "elapsed_seconds": metrics.get("elapsed_seconds", ""),
                        "run_id": metrics.get("run_id", metadata.get("run_id", "")),
                        "model": metadata.get("model", ""),
                        "prompt_version": metadata.get("prompt_version", ""),
                    }
                )

    if not rows:
        print("No results found.")
        return

    # Sort by scene, agents, seed
    rows.sort(key=lambda r: (r["scene"], r["agents"], r["seed"]))

    fieldnames = [
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

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)

    # Print summary statistics
    total = len(rows)
    success = sum(1 for r in rows if r["success_rate"] > 0)
    avg_coverage = sum(r["coverage"] for r in rows) / total if total else 0
    avg_transport = sum(r["transport_rate"] for r in rows) / total if total else 0

    print(f"Aggregated {total} runs → {output_path}")
    print()
    print("  Per-scene summary:")
    for scene in sorted(set(r["scene"] for r in rows)):
        sc_rows = [r for r in rows if r["scene"] == scene]
        sc_success = sum(1 for r in sc_rows if r["success_rate"] > 0)
        sc_coverage = sum(r["coverage"] for r in sc_rows) / len(sc_rows)
        sc_transport = sum(r["transport_rate"] for r in sc_rows) / len(sc_rows)
        print(
            f"    Scene {scene}: {len(sc_rows)} runs, {sc_success} success, "
            f"coverage={sc_coverage:.2f}, transport={sc_transport:.2f}"
        )

    print()
    print(
        f"  Overall: {total} runs, {success} finished ({100 * success / total:.0f}%), "
        f"avg coverage={avg_coverage:.2f}, avg transport={avg_transport:.2f}"
    )


def main():
    parser = argparse.ArgumentParser(description="Aggregate benchmark results")
    parser.add_argument(
        "--input",
        type=str,
        default=str(_DEFAULT_INPUT),
        help="Benchmark results directory",
    )
    parser.add_argument(
        "--output", type=str, default=str(_DEFAULT_OUTPUT), help="Output TSV path"
    )
    args = parser.parse_args()
    aggregate(args.input, args.output)


if __name__ == "__main__":
    main()
