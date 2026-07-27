"""AI2ThorExperiment — end-to-end experiment runner for AI2Thor tasks.

Supports two modes:

- ``fake`` (default): Uses FakeController and deterministic round-robin actions.
  No LLM, no Unity build required.  Suitable for integration tests and CI.

- ``unity``: Placeholder for real AI2Thor Controller.  Initialises the real
  Controller but requires a running Unity build.

Architecture (mirrors sar_orch/experiment.py):

    FakeController / Unity Controller
        → ControllerExecutor
            → AI2ThorBarrier
                → AliasRegistry
                    → worker tools (per agent)
                        → StateProviders (worker + coordinator)
                            → round loop (action submission → execution → logging)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.contracts.task import TaskContract, load_task
from ai2thor_orch.contracts.types import RoundResult, ActionResult
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.metrics.task_metrics import TaskMetricsTracker
from ai2thor_orch.tests.fakes import FakeController
from ai2thor_orch.verifier.verifier import verify_round
from ai2thor_orch.visibility import AliasRegistry

logger = logging.getLogger("ai2thor_experiment")

# Fake-mode deterministic action pool (round-robin across rounds)
_FAKE_ACTIONS = ["MoveAhead", "RotateLeft", "RotateRight", "LookUp", "LookDown"]

# Logs root directory (created under the repo root)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOGS_ROOT = os.path.join(_PROJECT_ROOT, "logs")


class AI2ThorExperiment:
    """End-to-end experiment runner for AI2Thor tasks.

    Args:
        task_id: Task identifier (e.g. ``"3_transport_groceries"``).
        scene: Scene number or name (e.g. ``"FloorPlan1"``).
        num_agents: Number of agents.
        seed: Random seed for reproducibility.
        mode: ``"fake"`` (default, uses FakeController) or ``"unity"`` (real Controller).
        max_steps: Maximum number of rounds.
        log_dir: Optional explicit log directory.  Auto-generated if not set.
    """

    def __init__(
        self,
        task_id: str = "3_transport_groceries",
        scene: str = "FloorPlan1",
        num_agents: int = 2,
        seed: int = 42,
        mode: str = "fake",
        max_steps: int = 50,
        log_dir: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.scene = scene
        self.num_agents = num_agents
        self.seed = seed
        self.mode = mode
        self.max_steps = max_steps

        # Load task contract
        self.contract: TaskContract = load_task(task_id, scene)

        # Create log directory
        if log_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            experiment_dir_name = (
                f"{timestamp}_{task_id}_s{scene}_a{num_agents}_seed{seed}_{mode}"
            )
            log_dir = str(Path(_LOGS_ROOT) / experiment_dir_name)
        self.log_dir = log_dir
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

        # Initialise controller and executor
        self._controller: Any = self._create_controller()
        self._executor: ControllerExecutor = ControllerExecutor(self._controller)
        self._alias_registry: AliasRegistry = AliasRegistry()

        # Initialise barrier
        self._barrier: AI2ThorBarrier = AI2ThorBarrier(
            num_agents=num_agents,
            executor=self._executor,
            max_steps=max_steps,
            alias_registry=self._alias_registry,
        )
        self._metrics_tracker = TaskMetricsTracker(self.contract, num_agents)

        # State
        self._round_no: int = 0
        self._last_round_result: RoundResult | None = None
        self._start_time: float = 0.0

        # CSV logging
        self._csv_path = Path(self.log_dir) / "summary.csv"
        self._csv_file: Any = None
        self._csv_writer: Any = None
        self._init_csv()

        # NDJSON logging
        self._events_path = Path(self.log_dir) / "events.ndjson"

    def _create_controller(self) -> Any:
        """Create the controller based on mode."""
        if self.mode == "fake":
            return FakeController()
        elif self.mode == "unity":
            # Placeholder for real Unity Controller
            # from ai2thor.controller import Controller
            # return Controller(scene=self.scene, ...)
            raise NotImplementedError(
                "Unity mode is a placeholder — not yet wired to a real Unity build."
            )
        else:
            raise ValueError(f"Unknown mode: {self.mode}. Use 'fake' or 'unity'.")

    def _init_csv(self) -> None:
        """Initialise the summary CSV with header."""
        self._csv_file = open(self._csv_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow([
            "ExperimentName",
            "LogDir",
            "Round",
            "TotalSteps",
            "Finished",
            "Coverage",
            "GoalCoverage",
            "InteractionCoverage",
            "TransportRate",
            "VerifiedCompletion",
            "CompletedSubtasks",
            "TotalSubtasks",
            "ActionAttempts",
            "SuccessfulActions",
            "FailedActions",
            "ActionSuccessRate",
            "TimeoutCount",
            "TimeoutRounds",
            "Balance",
            "ElapsedSeconds",
        ])
        self._csv_file.flush()

    async def _write_csv_row(
        self,
        round_no: int,
        finished: bool,
        goal_coverage: float,
        verified_completion: bool,
        progress_metrics: dict[str, Any],
        elapsed: float,
    ) -> None:
        """Write one row to the summary CSV."""
        if self._csv_writer is None:
            return
        self._csv_writer.writerow([
            self.task_id,
            self.log_dir,
            round_no,
            self._barrier.get_run_status().step,
            finished,
            round(goal_coverage, 4),
            round(goal_coverage, 4),
            round(float(progress_metrics["interaction_coverage"]), 4),
            round(float(progress_metrics["transport_rate"]), 4),
            verified_completion,
            progress_metrics["completed_subtask_count"],
            progress_metrics["total_subtasks"],
            progress_metrics["action_attempts"],
            progress_metrics["successful_actions"],
            progress_metrics["failed_actions"],
            round(float(progress_metrics["action_success_rate"]), 4),
            progress_metrics["timeout_count"],
            progress_metrics["timeout_rounds"],
            round(float(progress_metrics["balance"]), 4),
            round(elapsed, 2),
        ])
        self._csv_file.flush()

    async def _write_ndjson_event(self, event: dict[str, Any]) -> None:
        """Append one event to the NDJSON file."""
        with open(self._events_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    async def run(self) -> dict[str, Any]:
        """Run the experiment round loop.

        In ``fake`` mode, agents submit deterministic round-robin actions.

        Returns:
            Dict with keys:
                - ``verified_completion``: ``bool``
                - ``rounds``: ``int``
                - ``log_dir``: ``str``
                - ``finished``: ``bool``
                - ``coverage``: ``float``
                - ``elapsed_seconds``: ``float``
        """
        self._start_time = time.time()
        run_id = f"{self.task_id}-{uuid.uuid4().hex[:8]}"
        # An explicit benchmark log directory is reused across reruns. Start a
        # fresh timeline so events always belong to this run_id alone.
        self._events_path.write_text("")

        logger.info(
            "Experiment: task=%s scene=%s agents=%d seed=%d mode=%s max_steps=%d",
            self.task_id, self.scene, self.num_agents, self.seed,
            self.mode, self.max_steps,
        )
        logger.info("Log dir: %s", self.log_dir)

        # Write run metadata
        metadata = {
            "run_id": run_id,
            "task_id": self.task_id,
            "scene": self.scene,
            "num_agents": self.num_agents,
            "seed": self.seed,
            "mode": self.mode,
            "max_steps": self.max_steps,
            "coverage_objects": self.contract.coverage_objects,
            "subtasks": self.contract.subtasks,
            "start_time": datetime.now().isoformat(),
        }
        with open(Path(self.log_dir) / "run_meta.json", "w") as f:
            json.dump(metadata, f, indent=2)

        final_verified = False
        final_goal_coverage = 0.0
        final_progress_metrics = self._metrics_tracker.snapshot()

        try:
            # ── Round loop ──────────────────────────────────────────────────
            round_no = 0
            while round_no < self.max_steps:
                if self._barrier.is_finished():
                    break

                round_no += 1

                if self.mode == "fake":
                    action = _FAKE_ACTIONS[(round_no - 1) % len(_FAKE_ACTIONS)]
                else:
                    # In unity mode, we'd wait for LLM agent actions
                    action = "MoveAhead"

                # All agents submit the same action concurrently (gather avoids
                # sequential barrier wait where agent 0 waits for agent 1).
                coros = [
                    self._barrier.submit_action(agent_idx, action)
                    for agent_idx in range(self.num_agents)
                ]
                round_results: list[ActionResult] = await asyncio.gather(*coros)

                # Build RoundResult
                status = self._barrier.get_run_status()
                self._last_round_result = RoundResult(
                    round_no=round_no,
                    results=round_results,
                    timeout_agents=list(status.timeout_agents),
                    finished=status.finished,
                    domain_metrics=dict(status.domain_metrics),
                )

                progress_metrics = self._metrics_tracker.update(self._last_round_result)
                final_progress_metrics = progress_metrics

                # Verify round
                v_result = verify_round(self._last_round_result, self.contract)
                final_verified = bool(v_result["verified_completion"])
                final_goal_coverage = float(v_result["goal_coverage"])

                elapsed = time.time() - self._start_time

                # Log
                await self._write_csv_row(
                    round_no=round_no,
                    finished=status.finished,
                    goal_coverage=final_goal_coverage,
                    verified_completion=final_verified,
                    progress_metrics=progress_metrics,
                    elapsed=elapsed,
                )

                # NDJSON event
                event = {
                    "run_id": run_id,
                    "round": round_no,
                    "action": action,
                    "finished": status.finished,
                    "coverage": final_goal_coverage,
                    "goal_coverage": final_goal_coverage,
                    **progress_metrics,
                    "verified_completion": final_verified,
                    "elapsed_seconds": round(elapsed, 2),
                    "num_timeout_agents": len(status.timeout_agents),
                    "step": status.step,
                }
                await self._write_ndjson_event(event)

                logger.info(
                    "Round %2d | action=%s | goal=%.3f | interaction=%.3f | transport=%.3f | verified=%s | step=%d/%d | %.1fs",
                    round_no,
                    action,
                    final_goal_coverage,
                    progress_metrics["interaction_coverage"],
                    progress_metrics["transport_rate"],
                    final_verified,
                    status.step,
                    self.max_steps,
                    elapsed,
                )

                if final_verified:
                    logger.info("All coverage objects satisfied — finishing early.")
                    self._barrier.request_stop("all_coverage_satisfied")
                    break

            # ── Final flush ─────────────────────────────────────────────────
            elapsed_total = time.time() - self._start_time
            status = self._barrier.get_run_status()

            # Every completed round has already been flushed. A zero-round run
            # still needs one terminal row, but normal runs must not duplicate
            # their final timeline point.
            if round_no == 0:
                await self._write_csv_row(
                    round_no=round_no,
                    finished=status.finished,
                    goal_coverage=final_goal_coverage,
                    verified_completion=final_verified,
                    progress_metrics=final_progress_metrics,
                    elapsed=elapsed_total,
                )

            # Write summary.json
            summary = {
                "run_id": run_id,
                "task_id": self.task_id,
                "scene": self.scene,
                "num_agents": self.num_agents,
                "seed": self.seed,
                "mode": self.mode,
                "metric_schema_version": 2,
                "max_steps": self.max_steps,
                "rounds_completed": round_no,
                "finished": status.finished,
                "stopped": status.stopped,
                "stop_reason": status.stop_reason,
                "verified_completion": final_verified,
                "coverage": final_goal_coverage,
                "goal_coverage": final_goal_coverage,
                **final_progress_metrics,
                "elapsed_seconds": round(elapsed_total, 2),
                "log_dir": self.log_dir,
            }
            with open(Path(self.log_dir) / "summary.json", "w") as f:
                json.dump(summary, f, indent=2)

            logger.info(
                "Experiment finished: rounds=%d finished=%s verified=%s goal=%.3f interaction=%.3f transport=%.3f (%.1fs)",
                round_no,
                status.finished,
                final_verified,
                final_goal_coverage,
                final_progress_metrics["interaction_coverage"],
                final_progress_metrics["transport_rate"],
                elapsed_total,
            )

            return {
                "verified_completion": final_verified,
                "rounds": round_no,
                "log_dir": self.log_dir,
                "finished": status.finished,
                "coverage": final_goal_coverage,
                "goal_coverage": final_goal_coverage,
                **final_progress_metrics,
                "elapsed_seconds": round(elapsed_total, 2),
            }

        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """Release resources: stop barrier, close CSV."""
        try:
            self._barrier.stop()
        except Exception:
            pass
        try:
            self._executor.stop()
        except Exception:
            pass
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
