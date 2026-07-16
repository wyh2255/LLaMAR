#!/usr/bin/env python3
"""Launch SAR coordinator + workers for dashboard demo (no auto-submit).

Starts the infrastructure and waits for task submission via the Web UI
at http://localhost:8080/ui  (or the dashboard at http://localhost:8080/dashboard).

Press Ctrl+C to shut down.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
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
logger = logging.getLogger("sar_launch")

AGENT_PORTS: dict[str, int] = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COORDINATOR_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "coordinator")
_WORKER_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "worker")
_COORDINATOR_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_coordinator")
_WORKER_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_worker")


async def main():
    parser = argparse.ArgumentParser(description="Launch SAR dashboard server")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument(
        "--agents", type=int, default=2, help="Number of rescue robots (1-6)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--model", default="deepseek-v4-flash", help="LLM model")
    parser.add_argument("--provider", default="openai", help="LLM provider")
    parser.add_argument(
        "--api-base", default="https://api.deepseek.com", help="API base URL"
    )
    parser.add_argument("--max-steps", type=int, default=50, help="Max env steps")
    parser.add_argument(
        "--mode",
        choices=["semantic", "oracle"],
        default="semantic",
        help="Mode: semantic (semantic map) or oracle (ground truth)",
    )
    parser.add_argument(
        "--coordinator-port", type=int, default=8080, help="Coordinator port"
    )
    parser.add_argument(
        "--sandbox-profile",
        choices=["off", "workspace"],
        default="workspace",
        help="Sandbox isolation profile",
    )
    args = parser.parse_args()

    num_agents = args.agents
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]
    coordinator_port = args.coordinator_port

    logger.info("=" * 60)
    logger.info(
        "SAR Dashboard Launch: scene=%d, agents=%d, seed=%d",
        args.scene,
        num_agents,
        args.seed,
    )
    logger.info("Model: %s (provider=%s)", args.model, args.provider)
    logger.info("Mode: %s", args.mode)
    logger.info("=" * 60)

    # 1. Create SARBarrier
    barrier = SARBarrier(num_agents=num_agents, scene=args.scene, seed=args.seed)
    logger.info("SARBarrier initialized")

    # 2. Create experiment logger (for consistency, even though no auto-pilot)
    exp_logger = ExperimentLogger(experiment_name="sar_dashboard")
    logger.info("ExperimentLogger: %s", exp_logger.get_log_dir())

    # 3. Sandbox policy
    _project_root = Path(_PROJECT_ROOT)
    if args.sandbox_profile == "off":
        sandbox_policy = SandboxPolicy.off()
    else:
        sandbox_policy = SandboxPolicy.workspace(
            project_root=_project_root,
            workspace_dir="./workspace",
            write_roots=[Path(exp_logger.get_log_dir())],
        )

    # 4. Load .env
    _env = load_env_file(str(_project_root / ".env"))
    api_key_env = "OPENAI_API_KEY"
    if "api_key" in _env:
        os.environ[api_key_env] = _env["api_key"]

    # 5. Start coordinator (this injects barrier + semantic map into server)
    coordinator = SARCoordinator(
        host="0.0.0.0",
        port=coordinator_port,
        a2a_port=coordinator_port + 1,
        barrier=barrier,
        model=args.model,
        provider=args.provider,
        api_base=args.api_base,
        api_key_env=api_key_env,
        prompts_dir=_COORDINATOR_PROMPTS,
        log_dir=_COORDINATOR_LOG_DIR,
        exp_logger=exp_logger,
        sandbox_policy=sandbox_policy,
        state_mode=args.mode,
    )

    coord_task = asyncio.create_task(coordinator.start())
    await asyncio.sleep(3.0)

    # 6. Start workers
    workers: dict[str, SARWorker] = {}
    for i, name in enumerate(agent_names):
        port = AGENT_PORTS[name]
        worker = SARWorker(
            worker_id=name,
            agent_name=name,
            agent_idx=i,
            barrier=barrier,
            a2a_port=port,
            coordinator_url=f"ws://localhost:{coordinator_port}",
            model=args.model,
            provider=args.provider,
            api_base=args.api_base,
            api_key_env=api_key_env,
            prompts_dir=_WORKER_PROMPTS,
            log_dir=_WORKER_LOG_DIR,
            exp_logger=exp_logger,
            sandbox_policy=sandbox_policy,
        )
        workers[name] = worker
        worker.start()
        logger.info("Worker %s started on port %d", name, port)

    await asyncio.sleep(2.0)

    # 7. Start step_budget updater (coordinator needs this for context injection)
    async def _update_step_budget():
        """Keep the semantic map's step_budget in sync with barrier."""
        while True:
            try:
                if coordinator is not None and coordinator._semantic_map is not None:
                    current_step = getattr(barrier, "_step_counter", 0)
                    coordinator._semantic_map.update_step_budget(
                        current_step=current_step,
                        max_steps=args.max_steps,
                    )
            except Exception:
                pass
            await asyncio.sleep(1.0)

    budget_task = asyncio.create_task(_update_step_budget())

    logger.info("")
    logger.info("=== SAR Dashboard is ready! ===")
    logger.info("  Task console : http://localhost:%d/ui", coordinator_port)
    logger.info("  SAR Map      : http://localhost:%d/ui/map", coordinator_port)
    logger.info("  Dashboard    : http://localhost:%d/dashboard", coordinator_port)
    logger.info("")
    logger.info("Submit a task via the Task Console, then watch the Dashboard live.")
    logger.info("Press Ctrl+C to shut down.")
    logger.info("")

    # 8. Wait for shutdown signal — keep server alive
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received...")
        shutdown_event.set()
        budget_task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    budget_task.cancel()
    try:
        await budget_task
    except (asyncio.CancelledError, Exception):
        pass

    # 9. Cleanup
    logger.info("Shutting down...")
    for name, w in workers.items():
        w.clear_sessions()
        w.stop()
    if coordinator is not None:
        coordinator.clear_sessions()
        await coordinator.stop()
    barrier.stop()
    exp_logger.close()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
