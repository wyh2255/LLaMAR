"""ExperimentLogger -- CSV-based experiment tracking for SAR orchestration.

Records trajectory metrics, agent interactions, router interactions, and
experiment summaries into a timestamped results directory.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class ExperimentLogger:
    """Write experiment logs to CSV files under a timestamped results directory.

    Three CSV files are maintained:
      - trajectory.csv        : per-step metrics
      - agent_interactions.csv: per-agent tool-call details
      - router_interactions.csv: router subtask assignments

    A summary.csv is written on close().
    """

    def __init__(
        self,
        experiment_name: str = "sar_experiment",
        log_dir: str | None = None,
    ):
        """Initialize ExperimentLogger.

        Args:
            experiment_name: Name for this experiment run.
            log_dir: Explicit log directory. If None, a timestamped directory
                is created under ``sar_orch/results/``.
        """
        self.experiment_name = experiment_name

        if log_dir is not None:
            self._log_dir = Path(log_dir)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_dir = (
                Path(__file__).resolve().parent
                / "results"
                / f"{experiment_name}_{timestamp}"
            )

        os.makedirs(self._log_dir, exist_ok=True)

        # Track whether headers have been written for each file.
        self._headers_written: dict[str, bool] = {
            "trajectory": False,
            "agent_interactions": False,
            "router_interactions": False,
        }

        # File handles (opened lazily on first write).
        self._files: dict[str, Any] = {}
        self._writers: dict[str, csv.DictWriter] = {}

        # Accumulated summary data.
        self._step_count: int = 0
        self._last_coverage: float = 0.0
        self._last_transport_rate: float = 0.0
        self._finished: bool = False
        self._total_agent_interactions: int = 0
        self._total_router_interactions: int = 0

    # ------------------------------------------------------------------
    # Trajectory
    # ------------------------------------------------------------------

    def log_step(
        self,
        step_num: int,
        actions: list,
        successes: list,
        observations: list,
        coverage: float,
        transport_rate: float,
        finished: bool,
    ):
        """Append a row to trajectory.csv.

        Args:
            step_num: Current simulation step number.
            actions: List of action strings for each agent.
            successes: List of boolean success flags for each agent.
            observations: List of observation strings for each agent.
            coverage: Environment exploration coverage (0.0-1.0).
            transport_rate: Resource transport rate (0.0-1.0).
            finished: Whether the task is finished.
        """
        self._ensure_file("trajectory")
        row = {
            "Step": step_num,
            "Actions": actions,
            "Successes": successes,
            "Observations": observations,
            "Coverage": coverage,
            "TransportRate": transport_rate,
            "Finished": finished,
        }
        self._writers["trajectory"].writerow(row)
        self._files["trajectory"].flush()

        self._step_count = step_num
        self._last_coverage = coverage
        self._last_transport_rate = transport_rate
        self._finished = finished

    # ------------------------------------------------------------------
    # Agent interactions
    # ------------------------------------------------------------------

    def log_agent_interaction(
        self,
        step: int,
        agent: str,
        tool_name: str,
        tool_args: str,
        action: str = "",
        observation: str = "",
        llm_input: str = "",
        llm_output: str = "",
        thinking: str = "",
    ):
        """Append a row to agent_interactions.csv.

        Args:
            step: Current simulation step number.
            agent: Agent name (e.g. "Alice").
            tool_name: Name of the tool invoked.
            tool_args: JSON string of tool arguments.
            action: Action string submitted to the environment.
            observation: Observation received after the action.
            llm_input: LLM prompt or messages summary.
            llm_output: LLM response summary.
            thinking: LLM reasoning/thinking trace.
        """
        self._ensure_file("agent_interactions")
        row = {
            "Step": step,
            "Agent": agent,
            "ToolName": tool_name,
            "ToolArgs": tool_args,
            "Action": action,
            "Observation": observation,
            "LLMInput": llm_input,
            "LLMOutput": llm_output,
            "Thinking": thinking,
        }
        self._writers["agent_interactions"].writerow(row)
        self._files["agent_interactions"].flush()
        self._total_agent_interactions += 1

    # ------------------------------------------------------------------
    # Router interactions
    # ------------------------------------------------------------------

    def log_router_interaction(
        self,
        step: int,
        subtask: str,
        assigned_to: str,
    ):
        """Append a row to router_interactions.csv.

        Args:
            step: Current simulation step number.
            subtask: Description of the subtask assigned.
            assigned_to: Agent name the subtask was assigned to.
        """
        self._ensure_file("router_interactions")
        row = {
            "Step": step,
            "Subtask": subtask,
            "AssignedTo": assigned_to,
        }
        self._writers["router_interactions"].writerow(row)
        self._files["router_interactions"].flush()
        self._total_router_interactions += 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self):
        """Write summary.csv and close all open file handles."""
        self._write_summary()
        for fh in self._files.values():
            if not fh.closed:
                fh.close()
        self._writers.clear()
        self._files.clear()

    def get_log_dir(self) -> str:
        """Return the absolute path of the log directory.

        Returns:
            str: Path to the log directory.
        """
        return str(self._log_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_file(self, name: str):
        """Lazily open the CSV file and write headers on first access.

        Args:
            name: One of ``"trajectory"``, ``"agent_interactions"``,
                ``"router_interactions"``.
        """
        if name in self._files:
            return

        headers_map = {
            "trajectory": [
                "Step",
                "Actions",
                "Successes",
                "Observations",
                "Coverage",
                "TransportRate",
                "Finished",
            ],
            "agent_interactions": [
                "Step",
                "Agent",
                "ToolName",
                "ToolArgs",
                "Action",
                "Observation",
                "LLMInput",
                "LLMOutput",
                "Thinking",
            ],
            "router_interactions": [
                "Step",
                "Subtask",
                "AssignedTo",
            ],
        }

        path = self._log_dir / f"{name}.csv"
        fh = open(path, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=headers_map[name], quoting=csv.QUOTE_ALL)

        if os.path.getsize(path) == 0:
            writer.writeheader()

        self._files[name] = fh
        self._writers[name] = writer

    def _write_summary(self):
        """Write a single-row summary.csv with aggregate experiment metrics."""
        path = self._log_dir / "summary.csv"
        fieldnames = [
            "ExperimentName",
            "LogDir",
            "TotalSteps",
            "FinalCoverage",
            "FinalTransportRate",
            "Finished",
            "TotalAgentInteractions",
            "TotalRouterInteractions",
        ]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerow(
                {
                    "ExperimentName": self.experiment_name,
                    "LogDir": str(self._log_dir),
                    "TotalSteps": self._step_count,
                    "FinalCoverage": self._last_coverage,
                    "FinalTransportRate": self._last_transport_rate,
                    "Finished": self._finished,
                    "TotalAgentInteractions": self._total_agent_interactions,
                    "TotalRouterInteractions": self._total_router_interactions,
                }
            )
