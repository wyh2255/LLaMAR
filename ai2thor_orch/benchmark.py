#!/usr/bin/env python3
"""AI2Thor Benchmark — CLI runner for single or batched experiments.

Usage:
    uv run python -m ai2thor_orch.benchmark --task 3_transport_groceries --scene 1 --agents 2 --seed 42 --mode fake

Outputs an aggregate table of results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ai2thor_benchmark")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BENCHMARK_RESULTS_ROOT = _PROJECT_ROOT / "benchmark_results"


@dataclass
class BenchmarkRun:
    """One experiment configuration and its result."""

    task: str = "3_transport_groceries"
    scene: str = "FloorPlan1"
    agents: int = 2
    seed: int = 42
    mode: str = "fake"
    status: str = "pending"
    verified_completion: bool = False
    rounds: int = 0
    # Legacy compatibility: mirrors summary.json["coverage"] / goal coverage.
    coverage: float = 0.0
    goal_coverage: float = 0.0
    interaction_coverage: float = 0.0
    transport_rate: float = 0.0
    action_success_rate: float = 0.0
    timeout_count: int = 0
    balance: float = 1.0
    duration: float = 0.0
    log_dir: str = ""
    error: str | None = None


def _apply_summary_metrics(run: BenchmarkRun, data: dict[str, object]) -> None:
    """Populate benchmark fields from v2 summaries or legacy coverage-only logs."""
    coverage = _as_float(data.get("coverage", 0.0))
    run.verified_completion = bool(data.get("verified_completion", False))
    run.rounds = _as_int(data.get("rounds_completed", 0))
    run.coverage = coverage
    run.goal_coverage = _as_float(data.get("goal_coverage", coverage))
    run.interaction_coverage = _as_float(data.get("interaction_coverage", 0.0))
    run.transport_rate = _as_float(data.get("transport_rate", 0.0))
    run.action_success_rate = _as_float(data.get("action_success_rate", 0.0))
    run.timeout_count = _as_int(data.get("timeout_count", 0))
    run.balance = _as_float(data.get("balance", 1.0))
    log_dir = data.get("log_dir")
    if isinstance(log_dir, str):
        run.log_dir = log_dir


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _discover_tasks() -> list[str]:
    """Scan AI2Thor/Tasks/ to discover available tasks.

    Returns:
        List of task directory names (e.g. ``["3_transport_groceries"]``).
    """
    tasks_root = _PROJECT_ROOT / "AI2Thor" / "Tasks"
    if not tasks_root.is_dir():
        return []
    tasks = sorted(
        d.name
        for d in tasks_root.iterdir()
        if d.is_dir() and (d / "checker.py").exists()
    )
    return tasks


async def run_single(
    run: BenchmarkRun,
    run_timeout: int = 300,
    max_steps: int = 50,
) -> BenchmarkRun:
    """Run one experiment configuration.

    Starts the experiment as a subprocess (``-m ai2thor_orch.experiment``)
    and collects results.
    """
    start_t = time.time()

    # Build output directory under benchmark_results/
    result_dir = (
        _BENCHMARK_RESULTS_ROOT
        / run.task
        / f"scene_{run.scene}"
        / f"agents_{run.agents}"
        / f"seed_{run.seed}"
    )
    result_dir.mkdir(parents=True, exist_ok=True)
    exp_log_dir = str(result_dir / "logs")

    cmd = [
        sys.executable,
        "-m",
        "ai2thor_orch.experiment",
        "--task",
        run.task,
        "--scene",
        run.scene,
        "--agents",
        str(run.agents),
        "--seed",
        str(run.seed),
        "--mode",
        run.mode,
        "--max-steps",
        str(max_steps),
        "--log-dir",
        exp_log_dir,
    ]

    sub_env = dict(os.environ)
    sub_env["PYTHONPATH"] = f"{_PROJECT_ROOT}:src:{sub_env.get('PYTHONPATH', '')}"

    logger.info("Starting: %s", " ".join(cmd))
    run.status = "running"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(_PROJECT_ROOT),
        env=sub_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=run_timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        run.status = "timeout"
        run.duration = time.time() - start_t
        run.error = f"Timeout after {run_timeout}s"
        # Try reading partial summary.json
        summary_path = Path(exp_log_dir) / "summary.json"
        if summary_path.exists():
            try:
                data = json.loads(summary_path.read_text())
                _apply_summary_metrics(run, data)
            except Exception:
                pass
        logger.info(
            "  ⏱ %s scene=%s agents=%d seed=%d → timeout (%.1fs)",
            run.task, run.scene, run.agents, run.seed, run.duration,
        )
        return run

    # Save subprocess output
    if stdout:
        (result_dir / "stdout.log").write_bytes(stdout)
    if stderr:
        (result_dir / "stderr.log").write_bytes(stderr)

    # Read results from summary.json
    summary_path = Path(exp_log_dir) / "summary.json"
    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text())
            _apply_summary_metrics(run, data)
            run.status = "success" if data.get("finished") else "failed"
        except Exception as e:
            run.status = "failed"
            run.error = str(e)
    else:
        run.status = "failed"
        run.error = "No summary.json produced"

    run.duration = time.time() - start_t

    logger.info(
        "  ✓ %s scene=%s agents=%d seed=%d → %s (%d rounds, %.1fs, coverage=%.3f)",
        run.task, run.scene, run.agents, run.seed,
        run.status, run.rounds, run.duration, run.coverage,
    )

    # Write result.json
    result_data = asdict(run)
    result_data["duration"] = round(run.duration, 2)
    (result_dir / "result.json").write_text(json.dumps(result_data, indent=2, default=str))

    return run


def _print_table(runs: list[BenchmarkRun]) -> None:
    """Print an aggregate results table."""
    print()
    print("=" * 100)
    print(
        f"{'Task':30s} {'Scene':15s} {'Agents':8s} {'Seed':8s} {'Status':12s} "
        f"{'Rounds':8s} {'Duration':10s} {'Goal%':8s} {'Interact%':10s} "
        f"{'Transit%':9s} {'ActOK%':8s} {'Balance':8s} {'Timeouts':9s}"
    )
    print("=" * 100)
    for r in runs:
        status_str = r.status
        if r.verified_completion:
            status_str = "VERIFIED"
        print(
            f"{r.task:30s} {r.scene:15s} {r.agents:<8d} {r.seed:<8d} "
            f"{status_str:12s} {r.rounds:<8d} {r.duration:8.1f}s "
            f"{r.goal_coverage * 100:6.1f}% {r.interaction_coverage * 100:8.1f}% "
            f"{r.transport_rate * 100:7.1f}% {r.action_success_rate * 100:6.1f}% "
            f"{r.balance:7.3f} {r.timeout_count:<9d}"
        )
    print("=" * 100)

    # Summary counts
    total = len(runs)
    verified = sum(1 for r in runs if r.verified_completion)
    success = sum(1 for r in runs if r.status == "success")
    failed = sum(1 for r in runs if r.status == "failed")
    timeout = sum(1 for r in runs if r.status == "timeout")
    print(f"Total: {total} | Verified: {verified} | Success: {success} | Failed: {failed} | Timeout: {timeout}")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI2Thor Benchmark — run experiments and aggregate results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python -m ai2thor_orch.benchmark --task 3_transport_groceries --scene 1 --agents 2 --seed 42\n"
            "  uv run python -m ai2thor_orch.benchmark --task 3_transport_groceries --scene 1 --agents 2 --seed 42 --mode fake\n"
            "  uv run python -m ai2thor_orch.benchmark --task 3_transport_groceries --scene 1 --agents 2 3 4 --seed 42 43\n"
        ),
    )
    parser.add_argument(
        "--task",
        type=str,
        default="3_transport_groceries",
        help="Task ID (e.g. '3_transport_groceries')",
    )
    parser.add_argument(
        "--scene",
        type=int,
        default=1,
        help="Scene number (1-based, e.g. 1 -> FloorPlan1)",
    )
    parser.add_argument(
        "--agents",
        type=int,
        nargs="+",
        default=[2],
        help="Number of agents (space-separated for sweep: 2 3 4)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        nargs="+",
        default=[42],
        help="Random seeds (space-separated for sweep: 42 43 44)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="fake",
        choices=["fake", "unity"],
        help="Experiment mode: 'fake' (default) or 'unity' (real Controller)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Maximum steps per experiment (default: 50)",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=300,
        help="Per-run wall-clock timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="List available tasks and exit",
    )

    args = parser.parse_args()

    # ── List tasks mode ────────────────────────────────────────────────────
    if args.list_tasks:
        tasks = _discover_tasks()
        if tasks:
            print("Available tasks:")
            for t in tasks:
                print(f"  - {t}")
        else:
            print("No tasks found in AI2Thor/Tasks/")
        return

    # ── Build run list ─────────────────────────────────────────────────────
    runs: list[BenchmarkRun] = []
    for agents in args.agents:
        for seed in args.seed:
            runs.append(BenchmarkRun(
                task=args.task,
                scene=f"FloorPlan{args.scene}",
                agents=agents,
                seed=seed,
                mode=args.mode,
            ))

    if not runs:
        print("No runs to execute.")
        return

    logger.info(
        "Benchmark: %d run(s) to execute (task=%s, mode=%s, timeout=%ds)",
        len(runs), args.task, args.mode, args.run_timeout,
    )

    # ── Execute runs sequentially (no port contention) ──────────────────────
    completed: list[BenchmarkRun] = []
    for run in runs:
        try:
            result = await run_single(
                run,
                run_timeout=args.run_timeout,
                max_steps=args.max_steps,
            )
            completed.append(result)
        except asyncio.CancelledError:
            logger.warning("Benchmark cancelled.")
            break
        except Exception as e:
            logger.error("Run failed with exception: %s", e)
            run.status = "failed"
            run.error = str(e)
            completed.append(run)

    # ── Results ────────────────────────────────────────────────────────────
    _print_table(completed)

    # Write aggregate index
    index = []
    for r in completed:
        index.append(asdict(r))
    index_path = _BENCHMARK_RESULTS_ROOT / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2, default=str))
    logger.info("Results written to: %s", index_path)


if __name__ == "__main__":
    asyncio.run(main())
