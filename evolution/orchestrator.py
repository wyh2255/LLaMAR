#!/usr/bin/env python3
"""
LLaMAR Agent Evolution Orchestrator

Runs the evolution loop:
  1. Run experiment with current prompts
  2. Collect results
  3. Call OpenCode evolution engine (Node.js) to generate improved prompts
  4. Compare metrics between generations
  5. Repeat

Usage:
  python orchestrator.py --generations 3
  python orchestrator.py --generations 1 --results-dir ../sar_orch/results/sar_experiment_20260705_171652
  python orchestrator.py --generations 3 --scene 1 --agents 2 --seed 42 --model deepseek-v4-flash
"""

import argparse
import json
import os
import sys
import subprocess
import csv
from pathlib import Path
from datetime import datetime

# ─── Paths ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent  # LLaMAR/
EVOLUTION_DIR = ROOT / "evolution"
PROMPT_DIR = ROOT / "sar_orch" / "prompts"
GEN_DIR = EVOLUTION_DIR / "prompts"
RESULTS_BASE = ROOT / "sar_orch" / "results"


def parse_args():
    parser = argparse.ArgumentParser(description="LLaMAR Agent Evolution Orchestrator")
    parser.add_argument("--generations", type=int, default=3, help="Number of evolution rounds")
    parser.add_argument("--scene", type=int, default=1)
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--api-base", default="https://api.deepseek.com")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--results-dir", default=None,
                        help="Skip experiment, use existing results dir")
    parser.add_argument("--target", default="coordinator",
                        choices=["coordinator", "worker", "all"],
                        help="Which prompt to evolve")
    parser.add_argument("--skip-experiment", action="store_true",
                        help="Only run evolution analysis without running experiment")
    return parser.parse_args()


def load_metrics(results_dir: Path) -> dict:
    """Load summary metrics from an experiment results directory."""
    metrics = {"dir": str(results_dir)}

    # Try summary.tsv first, then summary.csv
    for fname in ["summary.tsv", "summary.csv"]:
        path = results_dir / fname
        if path.exists():
            with open(path) as f:
                reader = csv.DictReader(f) if fname.endswith(".csv") else csv.DictReader(f, delimiter="\t")
                for row in reader:
                    for k, v in row.items():
                        try:
                            metrics[k] = float(v)
                        except (ValueError, TypeError):
                            metrics[k] = v
            break

    # Load metadata
    meta_path = results_dir / "metadata.json"
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
            metrics["prompt_version"] = meta.get("prompt_version", "unknown")
            metrics["scene"] = meta.get("scene")
            metrics["agent_count"] = meta.get("agent_count")
            metrics["seed"] = meta.get("seed")

    return metrics


def pretty_metrics(metrics: dict) -> str:
    """Format metrics for display."""
    parts = []
    for key in ["success_rate", "finished", "transport_rate", "coverage",
                "steps", "total_tokens", "end_reason"]:
        if key in metrics and metrics[key] is not None:
            parts.append(f"{key}={metrics[key]}")
    return ", ".join(parts) if parts else str(metrics)


def run_experiment(args, gen: int, prompt_dir: Path) -> Path:
    """Run a single LLaMAR experiment with given prompts."""
    print(f"\n{'='*60}")
    print(f"  Running experiment — Gen {gen}")
    print(f"  Prompts: {prompt_dir}")
    print(f"  Scene={args.scene}, Agents={args.agents}, Seed={args.seed}")
    print(f"{'='*60}")

    # Build command
    cmd = [
        "uv", "run", "python", "sar_orch/experiment.py",
        f"--scene={args.scene}",
        f"--agents={args.agents}",
        f"--seed={args.seed}",
        f"--model={args.model}",
        f"--provider={args.provider}",
        f"--api-base={args.api_base}",
    ]
    if args.max_steps:
        cmd.append(f"--max-steps={args.max_steps}")

    env = os.environ.copy()
    env["no_proxy"] = "localhost,0.0.0.0,127.0.0.1"
    env["PYTHONPATH"] = f"src:{env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,  # 15 min max
        env=env,
    )

    print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
    if result.returncode != 0:
        print(f"  [STDERR]\n{result.stderr[-1000:]}")
        print(f"  ⚠ Experiment exited with code {result.returncode}")

    # Find the results directory (most recent one with matching pattern)
    result_dirs = sorted(
        [
            d for d in Path(RESULTS_BASE).iterdir() if d.is_dir() and (
                d.name.startswith("sar_experiment_") or
                (d.name[0].isdigit() and "_s" in d.name)
            )
        ],
        reverse=True
    )
    if result_dirs:
        return Path(result_dirs[0])
    else:
        print("  ⚠ Could not find experiment results directory")
        return Path(".")


def evolve_with_hermes(results_dir: Path, prompt_dir: Path, gen: int, target: str) -> dict:
    """Call the Hermes-native evolution engine (evolver.py)."""
    print(f"\n{'─'*60}")
    print(f"  Calling Hermes Evolution Engine — Gen {gen}")
    print(f"  Target: {target}")
    print(f"{'─'*60}")

    cmd = [
        sys.executable or "python3", "evolver.py",
        "--results", str(results_dir),
        "--prompt-dir", str(prompt_dir),
        "--gen", str(gen),
        "--target", target,
    ]

    result = subprocess.run(
        cmd,
        cwd=str(EVOLUTION_DIR),
        capture_output=True,
        text=True,
        timeout=600,  # 10 min for Hermes analysis
    )

    # Print output
    if result.stdout:
        main_output = result.stdout.split("---EVOLUTION_RESULT_START---")[0]
        if len(main_output) > 1000:
            print(f"\n{main_output[:500]}...\n[...]\n{main_output[-500:]}")
        else:
            print(f"\n{main_output}")

    if result.returncode != 0:
        print(f"\n  ⚠ Evolver exited with code {result.returncode}")
        if result.stderr:
            print(f"  STDERR: {result.stderr[:1000]}")
        return {"error": result.stderr, "generation": gen}

    # Parse structured result
    try:
        json_part = result.stdout.split("---EVOLUTION_RESULT_START---")[1]
        json_part = json_part.split("---EVOLUTION_RESULT_END---")[0]
        return json.loads(json_part)
    except (IndexError, json.JSONDecodeError) as e:
        print(f"  ⚠ Could not parse evolution result: {e}")
        return {"error": str(e), "generation": gen, "raw_output": result.stdout[-2000:]}


def compare_generations(prev_metrics: dict, curr_metrics: dict) -> dict:
    """Compare metrics between generations."""
    comparison = {}
    for key in ["success_rate", "finished", "transport_rate", "coverage",
                "total_tokens"]:
        p = prev_metrics.get(key)
        c = curr_metrics.get(key)
        if p is not None and c is not None:
            diff = c - p
            pct = (diff / p * 100) if p != 0 else 0
            comparison[key] = {"before": p, "after": c, "diff": diff, "pct_change": pct}
    return comparison


def main():
    args = parse_args()

    print("╔══════════════════════════════════════════════════════╗")
    print("║     LLaMAR Agent Evolution Orchestrator             ║")
    print(f"║     Generations: {args.generations}")
    print(f"║     Target:      {args.target}")
    print(f"║     Scene:       {args.scene}")
    print(f"║     Agents:      {args.agents}")
    print(f"║     Seed:        {args.seed}")
    print(f"║     Model:       {args.model}")
    print("╚══════════════════════════════════════════════════════╝")

    os.makedirs(GEN_DIR, exist_ok=True)

    # Determine starting prompts and results
    if args.results_dir:
        # Skip experiment, use provided results
        results_dir = Path(args.results_dir)
        gen = 0
        prompt_dir = PROMPT_DIR  # original prompts
        print(f"\n  Using existing results: {results_dir}")
        metrics = load_metrics(results_dir)
        print(f"  Baseline metrics: {pretty_metrics(metrics)}")

        if args.skip_experiment:
            # Only run one evolution round without running experiment
            result = evolve_with_hermes(results_dir, prompt_dir, gen=1, target=args.target)
            if "error" in result:
                print(f"\n  ✖ Evolution failed: {result['error']}")
            else:
                print("\n  ✓ Evolution complete")
                print(f"  Analysis: {result.get('analysis', '')[:200]}...")
                print(f"  Generated files: {result.get('generated_files', [])}")
            return
    else:
        # Run baseline experiment
        print("\n  Running baseline experiment (Gen 0)...")
        results_dir = run_experiment(args, gen=0, prompt_dir=PROMPT_DIR)
        metrics = load_metrics(results_dir)
        print(f"  Baseline metrics: {pretty_metrics(metrics)}")

    # Evolution loop
    prev_metrics = metrics
    prev_results_dir = results_dir

    for gen in range(1, args.generations + 1):
        print(f"\n{'#'*60}")
        print(f"  EVOLUTION ROUND {gen}/{args.generations}")
        print(f"{'#'*60}")

        # Call OpenCode to evolve prompts
        evolution_result = evolve_with_hermes(
            prev_results_dir, PROMPT_DIR, gen, args.target
        )

        if "error" in evolution_result:
            print(f"\n  ✖ Evolution round {gen} failed, stopping.")
            break

        # Determine which prompt files were generated
        gen_prompt_dir = GEN_DIR / f"gen_{gen}"
        has_coord = (gen_prompt_dir / "coordinator" / "system.md").exists()
        has_worker = (gen_prompt_dir / "worker" / "system.md").exists()

        # If target is 'all' or 'coordinator', coordinator prompt should be generated
        if args.target in ("coordinator", "all") and has_coord:
            new_prompt_dir = gen_prompt_dir / "coordinator"
        elif args.target == "worker" and has_worker:
            new_prompt_dir = gen_prompt_dir / "worker"
        else:
            print(f"  ⚠ No prompt files generated for target '{args.target}'")
            print(f"  Files in {gen_prompt_dir}: {list(gen_prompt_dir.rglob('*'))}")
            break

        # Run experiment with evolved prompts
        new_results_dir = run_experiment(args, gen, new_prompt_dir)
        curr_metrics = load_metrics(new_results_dir)
        print(f"  Gen {gen} metrics: {pretty_metrics(curr_metrics)}")

        # Compare with previous generation
        comparison = compare_generations(prev_metrics, curr_metrics)
        if comparison:
            print(f"\n  Comparison vs Gen {gen-1}:")
            for key, vals in comparison.items():
                arrow = "↑" if vals["diff"] > 0 else "↓" if vals["diff"] < 0 else "→"
                print(f"    {key}: {vals['before']} → {vals['after']} ({vals['pct_change']:+.1f}%) {arrow}")

        # Write evolution summary
        summary_path = gen_prompt_dir / "evolution_summary.json"
        summary = {
            "generation": gen,
            "previous_results": str(prev_results_dir),
            "previous_metrics": prev_metrics,
            "current_results": str(new_results_dir),
            "current_metrics": curr_metrics,
            "comparison": comparison,
            "analysis": evolution_result.get("analysis", ""),
            "changes": evolution_result.get("changes", ""),
            "timestamp": datetime.now().isoformat(),
        }
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"  Summary: {summary_path}")

        # Rotate
        prev_metrics = curr_metrics
        prev_results_dir = new_results_dir

    print(f"\n{'='*60}")
    print(f"  Evolution complete after {args.generations} generation(s)")
    print(f"  Final results: {prev_results_dir}")
    print(f"  Final prompts: {GEN_DIR / f'gen_{args.generations}'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
