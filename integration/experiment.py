#!/usr/bin/env python3
"""End-to-end MARoS x LLaMAR SAR experiment runner.

Usage:
    python3 integration/experiment.py --scene=1 --agents=3 --seed=42

Launches:
    1. SARBarrier (LLaMAR SAREnv)
    2. N x SARWorker (one per agent, per-agent LLM ReAct loop)
    3. SARCoordinator (MARoS RouterAgent task decomposition)

The Coordinator receives a task description (e.g., "Extinguish all fires and
rescue all persons"), decomposes it, and pushes subtasks to Workers.
Workers execute actions through the SARBarrier synchronously.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# -- Import path setup -----------------------------------------------------------
_llamar_root = Path(__file__).resolve().parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_a2a_lib = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

_maros_my_a2a = Path("/home/wyh/daily_work/MARoS/my_a2a/src")
if str(_maros_my_a2a) not in sys.path:
    sys.path.insert(0, str(_maros_my_a2a))

from integration.sar_barrier import SARBarrier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")


# Port assignments for up to 6 agents
AGENT_PORTS = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    coordinator_model: str = "claude-opus-4-5",
) -> dict:
    """Run one full SAR experiment.

    Returns metrics dict with keys: finished, steps, coverage, transport_rate, elapsed_seconds.
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Agent model: %s, Coordinator model: %s", model, coordinator_model)
    logger.info("=" * 60)

    # 1. Create barrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    logger.info("SARBarrier initialized -- env.task_timeout=%d", barrier.env.task_timeout)

    # 2. Create workers
    workers = {}
    for i, name in enumerate(agent_names):
        port = AGENT_PORTS[name]
        worker = _create_worker(
            agent_name=name,
            agent_idx=i,
            barrier=barrier,
            port=port,
            coordinator_url=f"ws://localhost:{COORDINATOR_PORT}",
            model=model,
        )
        workers[name] = worker
        worker.start()
        logger.info("Worker %s started on port %d", name, port)

    # Give workers a moment to start their HTTP servers
    await asyncio.sleep(1.0)

    # 3. Start coordinator
    from integration.coordinator.sar_coordinator import SARCoordinator

    coordinator = SARCoordinator(
        barrier=barrier,
        agent_names=agent_names,
        worker_ports={name: AGENT_PORTS[name] for name in agent_names},
        port=COORDINATOR_PORT,
        model=coordinator_model,
    )
    logger.info("SARCoordinator starting on port %d", COORDINATOR_PORT)

    start_time = time.time()

    try:
        # Start coordinator (it launches uvicorn in a background task)
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for task completion or timeout
        task_timeout = barrier.env.task_timeout
        poll_interval = 2.0
        elapsed = 0.0

        while not barrier.is_finished() and elapsed < task_timeout:
            await asyncio.sleep(poll_interval)
            elapsed = time.time() - start_time
            metrics = barrier.get_metrics()
            logger.info(
                "Step %d | Coverage: %.2f | Transport: %.2f | Finished: %s",
                metrics["steps"],
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
            )

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d steps",
                elapsed_total,
                final_metrics["steps"],
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %.1f seconds, %d steps",
                elapsed_total,
                final_metrics["steps"],
            )

        return final_metrics

    finally:
        # Cleanup
        logger.info("Shutting down workers...")
        for name, worker in workers.items():
            worker.stop()
        logger.info("Shutting down coordinator...")
        await coordinator.stop()
        logger.info("Shutting down barrier...")
        barrier.stop()
        logger.info("Cleanup complete")


def _create_worker(
    agent_name: str,
    agent_idx: int,
    barrier: SARBarrier,
    port: int,
    coordinator_url: str,
    model: str,
):
    """Factory to lazily import and create a SARWorker."""
    from integration.sar_workers.sar_worker import SARWorker

    return SARWorker(
        agent_name=agent_name,
        agent_idx=agent_idx,
        barrier=barrier,
        port=port,
        coordinator_url=coordinator_url,
        model=model,
    )


def main():
    parser = argparse.ArgumentParser(description="MARoS x LLaMAR SAR Experiment")
    parser.add_argument(
        "--scene", type=int, default=1, help="SAR scene number (1-5)"
    )
    parser.add_argument(
        "--agents", type=int, default=2, help="Number of agents (1-6)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-v4-flash",
        help="LLM model for workers",
    )
    parser.add_argument(
        "--coordinator-model",
        type=str,
        default="claude-opus-4-5",
        help="LLM model for coordinator",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            coordinator_model=args.coordinator_model,
        )
    )

    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS")
    print("=" * 60)
    print(f"  Finished:       {metrics['finished']}")
    print(f"  Steps:          {metrics['steps']}")
    print(f"  Coverage:       {metrics['coverage']:.2f}")
    print(f"  Transport Rate: {metrics['transport_rate']:.2f}")
    print(f"  Elapsed:        {metrics['elapsed_seconds']:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
