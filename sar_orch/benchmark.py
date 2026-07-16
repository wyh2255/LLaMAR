#!/usr/bin/env python3
"""SAR Benchmark — batch runner for full experimental sweep.

Usage:
    uv run python sar_orch/benchmark.py --concurrency 2

Runs all 100 combinations (5 scenes × 4 agent counts × 5 seeds) and
saves results under ``sar_orch/results/benchmark/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_benchmark")

# Paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESULTS_DIR = _PROJECT_ROOT / "sar_orch" / "results" / "benchmark"

# Experiment matrix
SCENES = [1, 2, 3, 4, 5]
AGENT_COUNTS = [2, 3, 4, 5]
SEEDS = [0, 10, 20, 30, 40]

# Dynamic port allocation — each run gets one block:
#   base       : coordinator HTTP
#   base + 1   : coordinator A2A
#   base + 2+  : worker A2A ports
_MAX_AGENTS = 6
_PORT_BLOCK_SIZE = 10
_PORT_SCAN_START = 50000
_PORT_SCAN_END = 59999

# Global shutdown state for graceful cancellation
_active_procs: list[asyncio.subprocess.Process] = []
_shutdown_event = asyncio.Event()

# ── Real-time progress tracking ──────────────────────────────────────────
_PROGRESS_FILE = _RESULTS_DIR / "progress.json"
_GLOBAL_PROGRESS: dict[str, int | float] = {
    "total": 0,
    "running": 0,
    "success": 0,
    "failed": 0,
    "timeout": 0,
    "skipped": 0,
}
_GLOBAL_PROGRESS_LOCK = threading.Lock()


def _write_progress() -> None:
    """Atomically write current progress snapshot to disk."""
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = dict(_GLOBAL_PROGRESS)
    snapshot["timestamp"] = time.time()
    tmp = _PROGRESS_FILE.with_suffix(".tmp.json")
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2)
    tmp.rename(_PROGRESS_FILE)


def _print_progress_bar() -> None:
    """Print a compact progress bar to stderr (no trailing newline)."""
    p = _GLOBAL_PROGRESS
    done = p["success"] + p["failed"] + p["timeout"] + p["skipped"]
    total = p["total"]
    if total == 0:
        return
    pct = done / total * 100
    bar_len = 20
    filled = int(bar_len * done / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    elapsed = time.time() - _GLOBAL_PROGRESS.get("_start", time.time())
    eta = (elapsed / max(done, 1)) * (total - done) if done else 0
    print(
        f"\r[{bar}] {done}/{total} ({pct:.0f}%)  "
        f"✓{p['success']} ✗{p['failed']} ⏱{p['timeout']} ⊘{p['skipped']}  "
        f"▶{p['running']}  "
        f"{elapsed:.0f}s elapsed{' · ETA ' + str(int(eta)) + 's' if done else ''}  ",
        file=sys.stderr,
        end="",
        flush=True,
    )


def _signal_handler(signum: int, frame) -> None:
    """Print progress on SIGUSR1 (``kill -USR1 <pid>``)."""
    _print_progress_bar()
    print(file=sys.stderr)  # trailing newline


def _shutdown_handler() -> None:
    """Trigger graceful shutdown on SIGINT/SIGTERM."""
    if _shutdown_event.is_set():
        return
    logger.warning("Shutdown signal received; stopping benchmark...")
    _shutdown_event.set()
    for proc in list(_active_procs):
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _is_port_free(host: str, port: int) -> bool:
    """Return True if the port can be bound (free or reusable TIME_WAIT)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        return False


def _ports_for_block(base: int, agents: int) -> list[int]:
    """Return all ports that a run with ``agents`` will bind."""
    coordinator_port = base
    agent_base_port = base + 2
    return [coordinator_port, coordinator_port + 1] + [
        agent_base_port + i for i in range(agents)
    ]


async def _find_free_blocks(count: int, agents: int) -> list[int]:
    """Scan the configured range for ``count`` free port blocks."""
    blocks: list[int] = []
    candidate = _PORT_SCAN_START
    while candidate + _PORT_BLOCK_SIZE <= _PORT_SCAN_END and len(blocks) < count:
        ports = _ports_for_block(candidate, agents)
        checks = [_is_port_free("localhost", p) for p in ports]
        if all(checks):
            blocks.append(candidate)
            candidate += _PORT_BLOCK_SIZE
        else:
            candidate += _PORT_BLOCK_SIZE
    if len(blocks) < count:
        raise RuntimeError(f"Only found {len(blocks)} free port blocks (need {count})")
    return blocks


async def _wait_ports_released(
    ports: list[int], timeout: float = 10.0, interval: float = 0.5
) -> None:
    """Wait until all ports are free (released by the subprocess)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        checks = [_is_port_free("localhost", p) for p in ports]
        if all(checks):
            return
        await asyncio.sleep(interval)
    logger.warning("Ports not fully released after %ss: %s", timeout, ports)


@dataclass
class BenchmarkRun:
    """One experiment configuration and its result."""

    scene: int
    agents: int
    seed: int
    status: str = "pending"  # pending | running | success | failed | skipped
    metrics: dict | None = None
    error: str | None = None
    elapsed: float = 0.0
    log_dir: str = ""


def _read_summary_csv(exp_log_dir: str) -> dict:
    """Read partial metrics from summary.csv (fallback when run_metrics.json missing)."""
    csv_path = Path(str(exp_log_dir)) / "summary.csv"
    if not csv_path.exists():
        return {"finished": False, "steps": 0, "coverage": 0.0, "transport_rate": 0.0}
    try:
        with open(str(csv_path)) as f:
            header = [h.strip('"') for h in f.readline().strip().split(",")]
            vals = [v.strip('"') for v in f.readline().strip().split(",")]
        # CSV cols: ExperimentName,LogDir,TotalSteps,FinalCoverage,FinalTransportRate,Finished,...
        idx = {h: i for i, h in enumerate(header)}
        return {
            "finished": vals[idx["Finished"]] == "True",
            "steps": int(float(vals[idx["TotalSteps"]])),
            "coverage": float(vals[idx["FinalCoverage"]]),
            "transport_rate": float(vals[idx["FinalTransportRate"]]),
        }
    except (OSError, KeyError, ValueError, IndexError):
        return {"finished": False, "steps": 0, "coverage": 0.0, "transport_rate": 0.0}


def run_dir(run: BenchmarkRun) -> Path:
    """Return the result directory path for a given run."""
    return (
        _RESULTS_DIR
        / f"scene_{run.scene}"
        / f"agents_{run.agents}"
        / f"seed_{run.seed}"
    )


async def run_single(
    run: BenchmarkRun,
    sem: asyncio.Semaphore,
    port_queue: asyncio.Queue[int],
    run_timeout: int = 3600,
    max_steps: int = 50,
    mode: str = "semantic",
) -> BenchmarkRun:
    """Execute one experiment configuration, protected by semaphore."""
    async with sem:
        if _shutdown_event.is_set():
            run.status = "failed"
            run.error = "shutdown"
            return run

        base_port: int | None = None
        coordinator_port: int | None = None
        agent_base_port: int | None = None
        proc: asyncio.subprocess.Process | None = None
        stdout = stderr = b""
        log_dir = run_dir(run)
        start_t = time.time()

        try:
            base_port = await port_queue.get()
            coordinator_port = base_port
            agent_base_port = base_port + 2

            # Race-guard: verify the scanned ports are still free right before binding.
            ports = _ports_for_block(base_port, run.agents)
            checks = [_is_port_free("localhost", p) for p in ports]
            if not all(checks):
                occupied = [p for p, free in zip(ports, checks) if not free]
                logger.error(
                    "  ✗ scene=%d agents=%d seed=%d → PORT CONFLICT: %s",
                    run.scene,
                    run.agents,
                    run.seed,
                    occupied,
                )
                run.status = "failed"
                run.error = f"Port conflict: {occupied}"
                with _GLOBAL_PROGRESS_LOCK:
                    _GLOBAL_PROGRESS["failed"] += 1
                    _write_progress()
                _print_progress_bar()
                return run

            run.status = "running"
            with _GLOBAL_PROGRESS_LOCK:
                _GLOBAL_PROGRESS["running"] += 1
                _write_progress()
            _print_progress_bar()

            # Backup existing results if retrying (preserves round-0 logs)
            if log_dir.exists() and any(log_dir.iterdir()):
                attempt = 0
                while (log_dir.with_name(f"seed_{run.seed}_pass_{attempt}")).exists():
                    attempt += 1
                backup_dir = log_dir.with_name(f"seed_{run.seed}_pass_{attempt}")
                log_dir.rename(backup_dir)
                logger.debug("Backed up previous results to %s", backup_dir)

            # Ensure output directory exists
            os.makedirs(str(log_dir), exist_ok=True)

            # Write run metadata before starting
            meta = {"scene": run.scene, "agents": run.agents, "seed": run.seed}
            with open(str(log_dir / "meta.json"), "w") as f:
                json.dump(meta, f)

            exp_log_dir = str(log_dir)
            cmd = [
                sys.executable,
                "sar_orch/experiment.py",
                "--scene",
                str(run.scene),
                "--agents",
                str(run.agents),
                "--seed",
                str(run.seed),
                "--coordinator-port",
                str(coordinator_port),
                "--agent-base-port",
                str(agent_base_port),
                "--log-dir",
                exp_log_dir,
            ]
            if max_steps > 0:
                cmd.extend(["--max-steps", str(max_steps)])
            if mode != "semantic":
                cmd.extend(["--mode", mode])
            logger.debug("Starting: %s", " ".join(cmd))
            sub_env = dict(os.environ)
            sub_env["PYTHONPATH"] = (
                f"{_PROJECT_ROOT}:src:{sub_env.get('PYTHONPATH', '')}"
            )
            # Add conda lib path for OpenCV (libGLX.so.0, libGLEW.so.2.2 etc.)
            _conda_lib = "/home/user/anaconda3/envs/wyh_2/lib"
            sub_env["LD_LIBRARY_PATH"] = (
                f"{_conda_lib}:{sub_env.get('LD_LIBRARY_PATH', '')}"
            )
            for k in ["no_proxy", "NO_PROXY"]:
                sub_env[k] = "localhost,0.0.0.0,127.0.0.1"
            # Strip proxy vars since experiments connect to localhost + DeepSeek directly
            for k in ["http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"]:
                sub_env.pop(k, None)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(_PROJECT_ROOT),
                env=sub_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _active_procs.append(proc)
            logger.debug("Subprocess PID: %d", proc.pid)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=run_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                (log_dir / "stderr.log").write_bytes(
                    f"\n--- BENCHMARK TIMEOUT after {run_timeout}s ---\n".encode()
                )
                run.status = "timeout"
                run.elapsed = time.time() - start_t
                # Try to salvage partial data from summary.csv
                metrics = _read_summary_csv(exp_log_dir)
                run.metrics = metrics
                run.log_dir = metrics.get("log_dir", "")
                metrics["status"] = run.status
                metrics["elapsed"] = round(run.elapsed, 1)
                # Copy summary.csv if it exists
                src_csv = Path(str(exp_log_dir)) / "summary.csv"
                if src_csv.exists():
                    dst_csv = log_dir / "summary.csv"
                    dst_csv.write_text(src_csv.read_text())
                with open(str(log_dir / "result.json"), "w") as f:
                    json.dump(metrics, f, indent=2, default=str)
                logger.info(
                    "  ⏱ scene=%d agents=%d seed=%d → timeout after %d steps (%.1fs)",
                    run.scene,
                    run.agents,
                    run.seed,
                    metrics.get("steps", 0),
                    run.elapsed,
                )
                with _GLOBAL_PROGRESS_LOCK:
                    _GLOBAL_PROGRESS["running"] -= 1
                    _GLOBAL_PROGRESS["timeout"] += 1
                    _write_progress()
                _print_progress_bar()
                return run

            # Save subprocess output
            if stdout:
                (log_dir / "stdout.log").write_bytes(stdout)
            if stderr:
                (log_dir / "stderr.log").write_bytes(stderr)

            # Read metrics from run_metrics.json
            metrics_file = Path(str(exp_log_dir)) / "run_metrics.json"
            if metrics_file.exists():
                with open(str(metrics_file)) as f:
                    metrics = json.load(f)
            else:
                metrics = {
                    "finished": False,
                    "steps": 0,
                    "coverage": 0.0,
                    "transport_rate": 0.0,
                }

            run.metrics = metrics
            run.elapsed = time.time() - start_t
            # Step-based cutoff: not finished within max_steps → failed
            step_limit = max_steps if max_steps > 0 else 999999
            if metrics.get("finished"):
                run.status = "success"
            elif metrics.get("steps", 0) >= step_limit:
                run.status = "failed"
            else:
                run.status = "timeout"
            run.log_dir = metrics.get("log_dir", "")

            # Copy summary.csv to benchmark directory
            src_summary = Path(run.log_dir) / "summary.csv"
            if src_summary.exists():
                dst = log_dir / "summary.csv"
                dst.write_text(src_summary.read_text())

            # Write results metadata
            metrics["status"] = run.status
            metrics["elapsed"] = round(run.elapsed, 1)
            with open(str(log_dir / "result.json"), "w") as f:
                json.dump(metrics, f, indent=2, default=str)

            logger.info(
                "  ✓ scene=%d agents=%d seed=%d → %s (%d steps, %.1fs)",
                run.scene,
                run.agents,
                run.seed,
                run.status,
                metrics.get("steps", 0),
                run.elapsed,
            )
            with _GLOBAL_PROGRESS_LOCK:
                _GLOBAL_PROGRESS["running"] -= 1
                if run.status == "success":
                    _GLOBAL_PROGRESS["success"] += 1
                elif run.status == "failed":
                    _GLOBAL_PROGRESS["failed"] += 1
                else:
                    _GLOBAL_PROGRESS["timeout"] += 1
                _write_progress()
            _print_progress_bar()

        except asyncio.CancelledError:
            logger.warning(
                "  ⊘ scene=%d agents=%d seed=%d → cancelled",
                run.scene,
                run.agents,
                run.seed,
            )
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            run.status = "failed"
            run.error = "cancelled"
            raise

        except Exception as e:
            if stdout:
                (log_dir / "stdout.log").write_bytes(stdout)
            if stderr:
                (log_dir / "stderr.log").write_bytes(stderr)
            run.status = "failed"
            run.error = str(e)
            run.elapsed = time.time() - start_t
            with _GLOBAL_PROGRESS_LOCK:
                _GLOBAL_PROGRESS["running"] -= 1
                _GLOBAL_PROGRESS["failed"] += 1
                _write_progress()
            _print_progress_bar()
            logger.error(
                "  ✗ scene=%d agents=%d seed=%d → FAILED: %s",
                run.scene,
                run.agents,
                run.seed,
                e,
            )
            with open(str(log_dir / "error.log"), "w") as f:
                f.write(str(e) if str(e) else "TimeoutError (no message)")
        finally:
            if proc is not None and proc in _active_procs:
                _active_procs.remove(proc)
            if base_port is not None:
                if coordinator_port is not None and agent_base_port is not None:
                    ports = _ports_for_block(base_port, run.agents)
                    await _wait_ports_released(ports)
                await port_queue.put(base_port)

        return run


async def main():
    parser = argparse.ArgumentParser(description="SAR Benchmark: full sweep")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Number of concurrent experiments (default: 2)",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=0,
        help="Retries per failed run (default: 0)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip previously successful runs and retry failed ones",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=3600,
        help="Per-run wall-clock timeout in seconds — safety net (default: 3600)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="semantic",
        choices=["semantic", "oracle"],
        help="Coordinator state source mode (default: semantic)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Step-based cutoff: mark as failed if not finished by N steps (default: 50). "
        "Set to 0 to disable step-based cutoff and rely solely on --run-timeout.",
    )
    parser.add_argument(
        "--scene",
        type=int,
        nargs="+",
        default=None,
        help="Only run specific scene(s), e.g. --scene 5 or --scene 1 3 5",
    )
    args = parser.parse_args()

    # Build run list (optionally filtered by --scene)
    all_runs: list[BenchmarkRun] = []
    scenes_to_run = [s for s in SCENES if args.scene is None or s in args.scene]
    for scene in scenes_to_run:
        for agents in AGENT_COUNTS:
            for seed in SEEDS:
                all_runs.append(BenchmarkRun(scene=scene, agents=agents, seed=seed))

    # Resume: skip existing successful runs
    if args.resume:
        filtered = []
        for run in all_runs:
            res_file = run_dir(run) / "result.json"
            if res_file.exists():
                try:
                    with open(str(res_file)) as f:
                        data = json.load(f)
                    if data.get("status") == "success":
                        run.status = "skipped"
                        run.metrics = data
                        logger.debug(
                            "  - scene=%d agents=%d seed=%d → skipped (already done)",
                            run.scene,
                            run.agents,
                            run.seed,
                        )
                except (json.JSONDecodeError, KeyError):
                    pass  # Corrupted → re-run
            filtered.append(run)
        all_runs = filtered

    # ── Register signal handlers ──────────────────────────────────────────
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _shutdown_handler)
    loop.add_signal_handler(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGUSR1, _signal_handler)

    # ── Initialise global progress tracker ────────────────────────────────
    skipped = sum(1 for r in all_runs if r.status == "skipped")
    with _GLOBAL_PROGRESS_LOCK:
        _GLOBAL_PROGRESS["total"] = len(all_runs)
        _GLOBAL_PROGRESS["skipped"] = skipped
        _GLOBAL_PROGRESS["_start"] = time.time()
        _write_progress()

    pending = [r for r in all_runs if r.status == "pending"]
    logger.info(
        "Benchmark: %d total runs (%d pending, %d completed/skipped)",
        len(all_runs),
        len(pending),
        len(all_runs) - len(pending),
    )

    if not pending:
        logger.info("Nothing to run.")
        return

    # ── Allocate free port blocks up front ────────────────────────────────
    max_agents_in_batch = max(r.agents for r in pending)
    free_blocks = await _find_free_blocks(args.concurrency, max_agents_in_batch)
    logger.info(
        "Allocated %d free port block(s) for concurrency=%d (max agents=%d): %s",
        len(free_blocks),
        args.concurrency,
        max_agents_in_batch,
        free_blocks,
    )
    port_queue: asyncio.Queue[int] = asyncio.Queue()
    for base in free_blocks:
        await port_queue.put(base)

    # Run with retries
    sem = asyncio.Semaphore(args.concurrency)
    to_run = pending

    for attempt in range(args.retry + 1):
        if not to_run:
            break

        if _shutdown_event.is_set():
            logger.info("Shutdown requested; stopping before next batch.")
            break

        # When retrying, give released ports a moment to leave TIME_WAIT.
        if attempt > 0:
            logger.info("Waiting 2s for ports to clear before retry...")
            await asyncio.sleep(2)

        tasks = [
            asyncio.create_task(
                run_single(
                    r, sem, port_queue, args.run_timeout, args.max_steps, args.mode
                )
            )
            for r in to_run
        ]
        shutdown_wait = asyncio.create_task(_shutdown_event.wait())

        pending_futures = set(tasks) | {shutdown_wait}
        while pending_futures:
            done, pending_futures = await asyncio.wait(
                pending_futures, return_when=asyncio.FIRST_COMPLETED
            )

            if shutdown_wait in done:
                # Graceful shutdown: cancel pending tasks and kill active children.
                logger.warning(
                    "Shutdown signal received; cancelling %d task(s)...", len(tasks)
                )
                for task in tasks:
                    if not task.done():
                        task.cancel()
                for proc in list(_active_procs):
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                await asyncio.gather(
                    *[t for t in tasks if not t.done()], return_exceptions=True
                )
                for proc in list(_active_procs):
                    if proc.returncode is None:
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            pass
                for f in pending_futures:
                    f.cancel()
                break

            # Remove completed tasks from tracking.
            tasks = [t for t in tasks if not t.done()]
            if not tasks:
                # All runs in this attempt finished normally.
                shutdown_wait.cancel()
                for f in pending_futures:
                    f.cancel()
                break

        if _shutdown_event.is_set():
            break

        raw_results = []
        for task in tasks:
            exc = task.exception()
            if exc is not None:
                raw_results.append(exc)
            else:
                raw_results.append(task.result())

        results = []
        for r in raw_results:
            if isinstance(r, Exception):
                logger.error("  ✗ run failed with exception: %s", r)
            elif isinstance(r, BenchmarkRun):
                results.append(r)

        to_run = [r for r in results if r.status == "failed"]
        # Revert progress counts for retried runs so they get re-counted
        if to_run and attempt < args.retry:
            with _GLOBAL_PROGRESS_LOCK:
                _GLOBAL_PROGRESS["failed"] -= len(to_run)
                _GLOBAL_PROGRESS["running"] = 0
                _write_progress()
            print(file=sys.stderr)  # newline after final progress bar
            logger.info("Retrying %d failed run(s) ...", len(to_run))

    # Finalise progress
    print(file=sys.stderr)  # newline after last progress bar

    # Summary
    success_count = sum(1 for r in all_runs if r.status == "success")
    timeout_count = sum(1 for r in all_runs if r.status == "timeout")
    failed_count = sum(1 for r in all_runs if r.status == "failed")
    skipped_count = sum(1 for r in all_runs if r.status == "skipped")

    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)
    print(f"  Total:   {len(all_runs)}")
    print(f"  Success: {success_count}")
    print(f"  Timeout: {timeout_count}")
    print(f"  Failed:  {failed_count}")
    print(f"  Skipped: {skipped_count}")
    print(f"  Results: {_RESULTS_DIR}")
    print("=" * 60)

    # Write aggregate index
    index = []
    for r in all_runs:
        index.append(
            {
                "scene": r.scene,
                "agents": r.agents,
                "seed": r.seed,
                "status": r.status,
                "elapsed": r.elapsed,
                "error": r.error,
                "log_dir": r.log_dir,
            }
        )
    with open(str(_RESULTS_DIR / "index.json"), "w") as f:
        json.dump(index, f, indent=2, default=str)

    logger.info("Aggregate index written to: %s", _RESULTS_DIR / "index.json")


if __name__ == "__main__":
    asyncio.run(main())
