#!/usr/bin/env python3
"""SAR 端到端实验运行器（End-to-end SAR experiment runner using my_a2a framework）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from Agent.sandbox import SandboxPolicy
from a2a.shared.env_loader import load_env_file
from sar_orch.barrier import SARBarrier
from sar_orch.logger import ExperimentLogger
from sar_orch.worker import SARWorker
from sar_orch.coordinator import SARCoordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")

# Port assignments for up to 6 agents
AGENT_PORTS: dict[str, int] = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080

# Paths
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COORDINATOR_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "coordinator")
_WORKER_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "worker")

# Default results root (all experiment outputs go under sar_orch/results/)
_RESULTS_ROOT = os.path.join(_PROJECT_ROOT, "sar_orch", "results")


def _get_git_commit() -> str:
    """Best-effort read of the current git short SHA."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def build_run_metadata(
    *,
    run_id: str,
    scene: int,
    num_agents: int,
    seed: int,
    model: str,
    provider: str,
    api_base: str,
    max_steps: int,
    wall_clock_limit: float,
    sandbox_profile: str,
    coordinator_prompts: str,
    worker_prompts: str,
    code_commit: str = "",
) -> dict:
    return {
        "run_id": run_id,
        "env_name": "SAR",
        "scenario_id": f"scene_{scene}",
        "scene": scene,
        "seed": seed,
        "agent_count": num_agents,
        "model": model,
        "provider": provider,
        "api_base": api_base,
        "max_steps": max_steps,
        "wall_clock_timeout": wall_clock_limit,
        "sandbox_profile": sandbox_profile,
        "task_objective": "Extinguish all fires and rescue all persons",
        "success_criteria": "SAR checker subtasks complete",
        "coordinator_prompts": coordinator_prompts,
        "worker_prompts": worker_prompts,
        "prompt_version": "baseline",
        "code_commit": code_commit or _get_git_commit(),
    }


def classify_end_reason(
    *,
    finished: bool,
    steps: int,
    max_steps: int,
    elapsed_seconds: float,
    wall_clock_limit: float,
    a2a_done: bool,
    a2a_error: bool,
    coordinator_error: bool,
) -> str:
    if finished:
        return "success"
    if coordinator_error or a2a_error:
        return "framework_error"
    if elapsed_seconds >= wall_clock_limit:
        return "wall_clock_timeout"
    if steps >= max_steps:
        return "max_steps_reached"
    if a2a_done:
        return "coordinator_finished_early"
    return "stopped_before_success"


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    provider: str = "openai",
    api_base: str = "https://api.deepseek.com",
    api_key_env: str = "OPENAI_API_KEY",
    max_steps: int | None = None,
    coordinator_port: int = 8080,
    agent_base_port: int = 8191,
    log_dir: str | None = None,
    sandbox_profile: str = "workspace",
    state_mode: str = "semantic",
    coordinator_prompt: str | None = None,
    enable_peer_mail: bool = False,
) -> dict:
    """Run one full SAR experiment.

    Args:
        max_steps: Maximum environment steps (step-based, like original LLaMAR).
            Defaults to scene's task_timeout value.
        coordinator_port: Port for the coordinator HTTP server.
        agent_base_port: Base port for agent A2A servers (each agent gets base + index).
        log_dir: Explicit log directory. If None, auto-generated timestamp dir.
        coordinator_prompt: Optional override for the initial task sent to the coordinator.
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Model: %s (provider=%s)", model, provider)
    logger.info("=" * 60)

    # 1. Create SARBarrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    max_steps = max_steps or 50
    logger.info("SARBarrier initialized -- max_steps=%d", max_steps)

    # 2. Create experiment log directory with unified naming convention
    if log_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        experiment_dir_name = f"{timestamp}_s{scene}_s{seed}_a{num_agents}"
        log_dir = str(Path(_RESULTS_ROOT) / experiment_dir_name)

    exp_dir = Path(log_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Create internal subdirectory structure
    coord_dir = exp_dir / "coordinator"
    workers_dir = exp_dir / "workers"
    supervision_dir = exp_dir / "supervision"
    coord_dir.mkdir(exist_ok=True)
    supervision_dir.mkdir(exist_ok=True)

    # Create per-worker subdirectories
    worker_log_dirs: dict[str, str] = {}
    for name in agent_names:
        wdir = workers_dir / name
        wdir.mkdir(parents=True, exist_ok=True)
        worker_log_dirs[name] = str(wdir)

    logger.info(
        "Experiment logs unified under: %s", exp_dir
    )

    # 3. Create experiment logger
    exp_logger = ExperimentLogger(experiment_name="sar_experiment", log_dir=str(exp_dir))

    run_id = f"sar-scene{scene}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"
    wall_clock_limit = 3600.0
    exp_logger.set_run_context(run_id=run_id, model=model, prompt_version="baseline")
    code_commit = _get_git_commit()
    metadata = build_run_metadata(
        run_id=run_id,
        code_commit=code_commit,
        scene=scene,
        num_agents=num_agents,
        seed=seed,
        model=model,
        provider=provider,
        api_base=api_base,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit,
        sandbox_profile=sandbox_profile,
        coordinator_prompts=_COORDINATOR_PROMPTS,
        worker_prompts=_WORKER_PROMPTS,
    )
    metadata["state_mode"] = state_mode
    metadata["oracle_mode"] = state_mode == "oracle"
    metadata["enable_peer_mail"] = enable_peer_mail
    exp_logger.write_metadata(metadata)

    # Create sandbox policy based on profile
    _project_root = Path(_PROJECT_ROOT)
    if sandbox_profile == "off":
        sandbox_policy = SandboxPolicy.off()
        logger.info("Sandbox: disabled (profile=off)")
    elif sandbox_profile == "workspace":
        _result_dir = Path(exp_logger.get_log_dir())
        sandbox_policy = SandboxPolicy.workspace(
            project_root=_project_root,
            workspace_dir="./workspace",
            write_roots=[_result_dir],
        )
        logger.info("Sandbox: workspace profile (write_roots=[%s])", _result_dir)
    else:
        raise ValueError(
            f"Invalid sandbox profile '{sandbox_profile}'. Must be 'off' or 'workspace'."
        )

    # Phase 4: generate per-run coordinator secret for peer mail
    if enable_peer_mail:
        import secrets
        coordinator_secret = secrets.token_bytes(32)
        logger.info("Peer mail enabled — coordinator secret generated")
    else:
        coordinator_secret = None

    workers: dict[str, SARWorker] = {}
    coordinator: SARCoordinator | None = None
    final_metrics: dict = {
        "finished": False,
        "steps": 0,
        "coverage": 0.0,
        "transport_rate": 0.0,
        "elapsed_seconds": 0.0,
    }

    try:
        # 2.5 Load .env and set API key env var (coordinator RouterAgent reads from env)
        _env = load_env_file(str(Path(__file__).parent.parent / ".env"))
        if "api_key" in _env:
            os.environ[api_key_env] = _env["api_key"]

        # 3. Create and start coordinator FIRST so workers can connect immediately
        coordinator = SARCoordinator(
            host="0.0.0.0",
            port=coordinator_port,
            a2a_port=coordinator_port + 1,
            barrier=barrier,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key_env=api_key_env,
            prompts_dir=_COORDINATOR_PROMPTS,
            log_dir=str(coord_dir),
            supervision_dir=str(supervision_dir),
            orchestration_mode="agentic",
            exp_logger=exp_logger,
            sandbox_policy=sandbox_policy,
            state_mode=state_mode,
            enable_peer_mail=enable_peer_mail,
            coordinator_secret=coordinator_secret,
        )

        logger.info("SARCoordinator starting on port %d", coordinator_port)
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for coordinator to bind ports
        await asyncio.sleep(3.0)

        # Redirect semantic_map.jsonl to the top-level experiment directory
        if coordinator._semantic_map is not None:
            coordinator._semantic_map.set_jsonl_path(str(exp_dir / "semantic_map.jsonl"))
            logger.info("semantic_map.jsonl path set to: %s", exp_dir / "semantic_map.jsonl")

        # 4. Create and start workers (they immediately connect to coordinator's WS)
        for i, name in enumerate(agent_names):
            port = agent_base_port + i
            worker = SARWorker(
                worker_id=name,
                agent_name=name,
                agent_idx=i,
                barrier=barrier,
                a2a_port=port,
                coordinator_url=f"ws://localhost:{coordinator_port}",
                model=model,
                provider=provider,
                api_base=api_base,
                api_key_env=api_key_env,
                prompts_dir=_WORKER_PROMPTS,
                log_dir=worker_log_dirs[name],
                exp_logger=exp_logger,
                sandbox_policy=sandbox_policy,
                enable_peer_mail=enable_peer_mail,
                coordinator_secret=coordinator_secret,
            )
            workers[name] = worker
            worker.start()
            logger.info("Worker %s started on port %d", name, port)

        # Give workers time to register with coordinator
        await asyncio.sleep(2.0)

        # 5. Submit initial task (fire-and-forget — poll barrier in parallel)
        task_description = coordinator_prompt or (
            "Extinguish all fires and rescue all persons"
        )
        logger.info("Submitting initial task: %s", task_description)
        a2a_task = asyncio.create_task(coordinator.submit_task(task_description))
        await asyncio.sleep(0.5)  # Let A2A start processing

        # 6. Poll barrier for completion (runs immediately, not blocked by A2A)
        # Uses STEP-BASED timeout (max_steps) like original LLaMAR, not wall-clock.
        # Wall-clock guard prevents indefinite stall from barrier timeouts.
        start_time = time.time()
        poll_interval = 2.0
        _last_step_logged = -1

        a2a_done = False
        a2a_error = False
        coordinator_error = False

        while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
            await asyncio.sleep(poll_interval)

            if coord_task.done():
                exc = coord_task.exception()
                if exc:
                    logger.error("Coordinator failed: %s", exc)
                    coordinator_error = True
                    break
            if a2a_task.done() and not a2a_task.cancelled():
                exc = a2a_task.exception()
                if exc:
                    logger.error("A2A orchestration failed: %s", exc)
                    a2a_error = True
                    break
                else:
                    logger.info("A2A orchestration completed; exiting poll loop")
                    a2a_done = True
                    break

            elapsed = time.time() - start_time
            if elapsed > wall_clock_limit:
                logger.warning(
                    "Wall-clock limit %.0fs reached, stopping (step %d/%d)",
                    wall_clock_limit,
                    barrier.get_metrics()["steps"],
                    max_steps,
                )
                break

            metrics = barrier.get_metrics()
            logger.info(
                "Step %d/%d | Coverage: %.2f | Transport: %.2f | Finished: %s | %.0fs",
                metrics["steps"],
                max_steps,
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
                elapsed,
            )

            # Log step to experiment logger (only when step advances)
            step_log = (
                barrier.get_last_step_log()
                if hasattr(barrier, "get_last_step_log")
                else None
            )
            if (
                step_log
                and step_log.get("actions")
                and metrics["steps"] > _last_step_logged
            ):
                exp_logger.log_step(
                    step_num=metrics["steps"],
                    actions=step_log.get("actions", []),
                    successes=step_log.get("successes", []),
                    observations=step_log.get("observations", []),
                    coverage=metrics["coverage"],
                    transport_rate=metrics["transport_rate"],
                    finished=metrics["finished"],
                    timeout_agents=step_log.get("timeout_agents", []),
                    run_id=run_id,
                    max_steps=max_steps,
                    remaining_steps=max(0, max_steps - metrics["steps"]),
                    wall_time_since_start=elapsed,
                    step_duration_ms=step_log.get("step_duration_ms", ""),
                    error_types=step_log.get("error_types", []),
                    completed_subtasks_delta=step_log.get(
                        "completed_subtasks_delta", []
                    ),
                    end_reason="",
                )
                # Incremental summary write — survives shell timeout kills
                exp_logger.flush_summary()
                if coordinator is not None and coordinator._semantic_map is not None:
                    coordinator._semantic_map.update_step_budget(
                        current_step=metrics["steps"],
                        max_steps=max_steps,
                    )
                _last_step_logged = metrics["steps"]

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        end_reason = classify_end_reason(
            finished=final_metrics["finished"],
            steps=final_metrics["steps"],
            max_steps=max_steps,
            elapsed_seconds=elapsed_total,
            wall_clock_limit=wall_clock_limit,
            a2a_done=a2a_done,
            a2a_error=a2a_error,
            coordinator_error=coordinator_error,
        )
        final_metrics["end_reason"] = end_reason
        exp_logger.set_end_reason(end_reason)
        final_metrics["run_id"] = run_id
        final_metrics["max_steps"] = max_steps

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d/%d steps",
                elapsed_total,
                final_metrics["steps"],
                max_steps,
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %d steps (limit %d), %.1f seconds",
                final_metrics["steps"],
                max_steps,
                elapsed_total,
            )

        # Cancel A2A orphan task if barrier finished before it
        if not a2a_task.done():
            a2a_task.cancel()
            try:
                await a2a_task
            except asyncio.CancelledError:
                pass

        return final_metrics

    finally:
        # Cleanup
        logger.info("Clearing agent sessions...")
        for name, worker in workers.items():
            worker.clear_sessions()
        if coordinator is not None:
            coordinator.clear_sessions()

        logger.info("Shutting down workers...")
        for name, worker in workers.items():
            worker.stop()
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            await coordinator.stop()
        logger.info("Shutting down barrier...")
        barrier.stop()

        exp_logger.close()
        final_metrics["log_dir"] = exp_logger.get_log_dir()
        logger.info("Experiment logs saved to: %s", exp_logger.get_log_dir())
        logger.info("Cleanup complete")


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument("--agents", type=int, default=2, help="Number of agents (1-6)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--model", type=str, default="deepseek-v4-flash", help="LLM model"
    )
    parser.add_argument("--provider", type=str, default="openai", help="LLM provider")
    parser.add_argument(
        "--api-base", type=str, default="https://api.deepseek.com", help="API base URL"
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Max environment steps (default: scene's task_timeout)",
    )
    parser.add_argument(
        "--coordinator-port",
        type=int,
        default=8080,
        help="Coordinator server port (default: 8080)",
    )
    parser.add_argument(
        "--agent-base-port",
        type=int,
        default=8191,
        help="Base port for agent A2A servers (default: 8191)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Explicit log directory (default: auto-generated timestamp dir)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="semantic",
        choices=["semantic", "oracle"],
        help="Coordinator state source mode (default: semantic)",
    )
    parser.add_argument(
        "--sandbox-profile",
        type=str,
        default="workspace",
        choices=["off", "workspace"],
        help="Sandbox profile: off|workspace (default: workspace)",
    )
    parser.add_argument(
        "--coordinator-prompt",
        type=str,
        default=None,
        help="Override the initial task prompt sent to the coordinator (for targeted testing)",
    )
    parser.add_argument(
        "--enable-peer-mail",
        action="store_true",
        default=False,
        help="Enable signed envelope peer messaging (Phase 4)",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            provider=args.provider,
            api_base=args.api_base,
            max_steps=args.max_steps,
            coordinator_port=args.coordinator_port,
            agent_base_port=args.agent_base_port,
            log_dir=args.log_dir,
            sandbox_profile=args.sandbox_profile,
            state_mode=args.mode,
            coordinator_prompt=args.coordinator_prompt,
            enable_peer_mail=args.enable_peer_mail,
        )
    )

    # Write metrics to JSON for subprocess caller
    log_dir = metrics.get("log_dir", "")
    if log_dir:
        metrics_file = os.path.join(log_dir, "run_metrics.json")
        with open(metrics_file, "w") as f:
            json.dump(metrics, f, indent=2, default=str)

    print(json.dumps(metrics, default=str))


if __name__ == "__main__":
    main()
