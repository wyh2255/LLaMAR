#!/usr/bin/env python3
"""SAR 端到端实验运行器（End-to-end SAR experiment runner using my_a2a framework）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time

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
_COORDINATOR_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_coordinator")
_WORKER_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_worker")


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
) -> dict:
    """Run one full SAR experiment.

    Args:
        max_steps: Maximum environment steps (step-based, like original LLaMAR).
            Defaults to scene's task_timeout value.
        coordinator_port: Port for the coordinator HTTP server.
        agent_base_port: Base port for agent A2A servers (each agent gets base + index).
        log_dir: Explicit log directory. If None, auto-generated timestamp dir.
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Model: %s (provider=%s)", model, provider)
    logger.info("=" * 60)

    # 1. Create SARBarrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    max_steps = max_steps or getattr(barrier.env, "task_timeout", 300)
    logger.info("SARBarrier initialized -- max_steps=%d", max_steps)

    # 2. Create experiment logger
    exp_logger = ExperimentLogger(experiment_name="sar_experiment", log_dir=log_dir)
    logger.info("ExperimentLogger initialized -- log dir: %s", exp_logger.get_log_dir())

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
            log_dir=_COORDINATOR_LOG_DIR,
            orchestration_mode="agentic",
            exp_logger=exp_logger,
        )

        logger.info("SARCoordinator starting on port %d", coordinator_port)
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for coordinator to bind ports
        await asyncio.sleep(3.0)

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
                log_dir=_WORKER_LOG_DIR,
                exp_logger=exp_logger,
            )
            workers[name] = worker
            worker.start()
            logger.info("Worker %s started on port %d", name, port)

        # Give workers time to register with coordinator
        await asyncio.sleep(2.0)

        # 5. Submit initial task (fire-and-forget — poll barrier in parallel)
        task_description = "Extinguish all fires and rescue all persons"
        logger.info("Submitting initial task: %s", task_description)
        a2a_task = asyncio.create_task(coordinator.submit_task(task_description))
        await asyncio.sleep(0.5)  # Let A2A start processing

        # 6. Poll barrier for completion (runs immediately, not blocked by A2A)
        # Uses STEP-BASED timeout (max_steps) like original LLaMAR, not wall-clock.
        # Wall-clock guard prevents indefinite stall from barrier timeouts.
        start_time = time.time()
        poll_interval = 2.0
        wall_clock_limit = 600.0  # 10 min safety net for single runs
        _last_step_logged = -1

        while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
            await asyncio.sleep(poll_interval)

            if coord_task.done():
                exc = coord_task.exception()
                if exc:
                    logger.error("Coordinator failed: %s", exc)
                    break
            if a2a_task.done() and not a2a_task.cancelled():
                exc = a2a_task.exception()
                if exc:
                    logger.error("A2A orchestration failed: %s", exc)
                    break
                else:
                    logger.info("A2A orchestration completed; exiting poll loop")
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
                )
                # Incremental summary write — survives shell timeout kills
                exp_logger.flush_summary()
                _last_step_logged = metrics["steps"]

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

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
