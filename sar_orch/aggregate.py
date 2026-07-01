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

_DEFAULT_INPUT = Path(__file__).resolve().parent / "results" / "benchmark"
_DEFAULT_OUTPUT = _DEFAULT_INPUT.parent / "benchmark_aggregated.tsv"


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
                seed = int(seed_dir.name.replace("seed_", ""))

                result_file = seed_dir / "result.json"
                if not result_file.exists():
                    continue

                try:
                    with open(str(result_file)) as f:
                        metrics = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                steps = metrics.get("steps", 0)
                coverage = metrics.get("coverage", 0.0)
                transport_rate = metrics.get("transport_rate", 0.0)
                finished = metrics.get("finished", False)

                rows.append({
                    "scene": scene,
                    "agents": agents,
                    "seed": seed,
                    "steps": steps,
                    "balance": 1.0 if finished else transport_rate,
                    "coverage": coverage,
                    "success_rate": 1.0 if finished else 0.0,
                    "transport_rate": transport_rate,
                })

    if not rows:
        print("No results found.")
        return

    # Sort by scene, agents, seed
    rows.sort(key=lambda r: (r["scene"], r["agents"], r["seed"]))

    fieldnames = ["scene", "agents", "seed", "steps", "balance", "coverage", "success_rate", "transport_rate"]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
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
        print(f"    Scene {scene}: {len(sc_rows)} runs, {sc_success} success, "
              f"coverage={sc_coverage:.2f}, transport={sc_transport:.2f}")

    print()
    print(f"  Overall: {total} runs, {success} finished ({100*success/total:.0f}%), "
          f"avg coverage={avg_coverage:.2f}, avg transport={avg_transport:.2f}")


def main():
    parser = argparse.ArgumentParser(description="Aggregate benchmark results")
    parser.add_argument("--input", type=str, default=str(_DEFAULT_INPUT),
                        help="Benchmark results directory")
    parser.add_argument("--output", type=str, default=str(_DEFAULT_OUTPUT),
                        help="Output TSV path")
    args = parser.parse_args()
    aggregate(args.input, args.output)


if __name__ == "__main__":
    main()
