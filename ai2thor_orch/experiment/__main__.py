#!/usr/bin/env python3
"""CLI entry point for ``python -m ai2thor_orch.experiment``.

Usage:
    python -m ai2thor_orch.experiment --task 3_transport_groceries \\
        --scene FloorPlan1 --agents 2 --seed 42 --mode fake
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from ai2thor_orch.experiment.ai2thor_experiment import run_experiment


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI2Thor Experiment runner")
    parser.add_argument("--task", type=str, default="3_transport_groceries")
    parser.add_argument("--scene", type=str, default="FloorPlan1")
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mode", type=str, default="fake", choices=["fake", "unity"])
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--log-dir", type=str, default=None)
    parser.add_argument(
        "--coordinator-port", type=int, default=8080, help="A2A coordinator port"
    )
    parser.add_argument(
        "--agent-base-port", type=int, default=8191, help="first worker A2A port"
    )
    parser.add_argument(
        "--wall-clock-limit",
        type=float,
        default=3600.0,
        help="wall-clock safety net in seconds",
    )
    args = parser.parse_args()

    result = await run_experiment(
        task_id=args.task,
        scene=args.scene,
        num_agents=args.agents,
        seed=args.seed,
        mode=args.mode,
        max_steps=args.max_steps,
        log_dir=args.log_dir,
        coordinator_port=args.coordinator_port,
        agent_base_port=args.agent_base_port,
        wall_clock_limit=args.wall_clock_limit,
    )
    print(f"Result: {result}")
    # 退出码 = 论文口径成功真值（tracker 动作证据账记满）；verifier 的
    # ``verified_completion`` 自 F-done 起只是审计字段，不再单独触发 0。
    sys.exit(0 if result.get("finished") else 1)


if __name__ == "__main__":
    asyncio.run(main())
