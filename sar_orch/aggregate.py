#!/usr/bin/env python3
"""Aggregate benchmark results into paper-format TSV.

Usage:
    uv run python sar_orch/aggregate.py [--input <benchmark_dir>] [--output <tsv_path>]

Output columns (matching original LLaMAR format):
    scene, agents, seed, steps, balance, coverage, success_rate, transport_rate
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from sar_orch.eval.aggregate import _is_retry_backup
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.outcome import compute_balance

_DEFAULT_INPUT = Path(__file__).resolve().parent / "results" / "benchmark"
_DEFAULT_OUTPUT = _DEFAULT_INPUT.parent / "benchmark_aggregated.tsv"


def classify_failure(end_reason: str, finished: bool) -> str:
    if finished:
        return "success"
    if end_reason in {"max_steps_reached", "wall_clock_timeout"}:
        return "budget"
    if end_reason in {
        "framework_error",
        "worker_timeout",
        "coordinator_finished_early",
        "stopped_before_success",
    }:
        return "framework"
    if end_reason == "environment_error":
        return "environment"
    return "unknown"


def _resolve_balance(seed_dir: Path, metrics: dict, agents: int) -> float | str:
    """按论文定义计算 Balance：min(s_i)/(max(s_i)+1e-4)。

    需要逐步的 per-agent 动作/成功记录，只存在于 run 目录的 trajectory.csv 里；
    seed 目录只有 result.json/summary.csv。优先用 result.json 记录的 log_dir，
    回退到 seed 目录本身。拿不到轨迹时返回空串而不是编一个数——
    一个错误的 balance 比缺失的 balance 更有害。
    """
    candidates = []
    log_dir = metrics.get("log_dir")
    if log_dir:
        candidates.append(Path(str(log_dir)))
    candidates.append(seed_dir)

    for run_dir in candidates:
        if not (run_dir / "trajectory.csv").exists():
            continue
        try:
            episode = load_episode(run_dir)
        except Exception:
            continue
        if not episode.steps:
            continue
        if agents > 0:
            # seed 目录路径里的 agents 是权威值，覆盖可能缺失的 metadata。
            episode.metadata = {**(episode.metadata or {}), "agent_count": agents}
        return compute_balance(episode)
    return ""


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
                if not seed_dir.is_dir() or not seed_dir.name.startswith("seed_"):
                    continue
                # benchmark.py 重试时把上一轮结果改名为 `seed_<N>_pass_<M>`。
                # 这些是被取代的历史尝试：计入会重复统计同一 (scene, agents,
                # seed)，且目录名无法解析成整数 seed。与 eval/aggregate.py
                # 的 scan_results 保持同一口径。
                if _is_retry_backup(seed_dir.name):
                    continue
                seed = int(seed_dir.name.replace("seed_", ""))

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

                rows.append(
                    {
                        "scene": scene,
                        "agents": agents,
                        "seed": seed,
                        "steps": steps,
                        "balance": _resolve_balance(seed_dir, metrics, agents),
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
