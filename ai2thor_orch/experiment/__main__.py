#!/usr/bin/env python3
"""CLI entry point for ``python -m ai2thor_orch.experiment``.

Usage:
    python -m ai2thor_orch.experiment --task 3_transport_groceries --scene 1 --agents 2 --seed 42 --mode fake
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

from ai2thor_orch.experiment.ai2thor_experiment import AI2ThorExperiment


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI2Thor Experiment runner")
    parser.add_argument("--task", type=str, default="3_transport_groceries")
    parser.add_argument("--scene", type=str, default="FloorPlan1")
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mode", type=str, default="fake", choices=["fake", "unity"])
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--log-dir", type=str, default=None)
    args = parser.parse_args()

    exp = AI2ThorExperiment(
        task_id=args.task,
        scene=args.scene,
        num_agents=args.agents,
        seed=args.seed,
        mode=args.mode,
        max_steps=args.max_steps,
        log_dir=args.log_dir,
    )
    result = await exp.run()
    print(f"Result: {result}")
    sys.exit(0 if result.get("verified_completion") or result.get("finished") else 1)


if __name__ == "__main__":
    asyncio.run(main())
