"""ExperimentLogger -- CSV-based experiment tracking for SAR orchestration.

Records trajectory metrics, agent interactions, router interactions, and
experiment summaries into a timestamped results directory.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
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

        # Thread safety for CSV writes from multiple worker threads
        self._lock = threading.Lock()

        # Track whether headers have been written for each file.
        self._headers_written: dict[str, bool] = {
            "trajectory": False,
            "agent_interactions": False,
            "router_interactions": False,
            "token_usage": False,
            "subtasks": False,
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

        # Per-agent token accumulator: {agent_name: {prompt, completion, total}}
        self._token_accumulator: dict[str, dict[str, int]] = {}

        # Default context for run_id, model, prompt_version
        self._default_run_id: str = ""
        self._default_model: str = ""
        self._default_prompt_version: str = ""

        # Trajectory row buffer for EndReason backfill
        self._trajectory_rows: list[dict] = []

    # ------------------------------------------------------------------
    # Run context
    # ------------------------------------------------------------------

    def set_run_context(
        self, run_id: str = "", model: str = "", prompt_version: str = "baseline"
    ):
        """Set default field values inherited by subsequent log calls.

        Args:
            run_id: Default RunID for all log methods.
            model: Default model for token_usage rows.
            prompt_version: Default prompt_version for token_usage rows.
        """
        self._default_run_id = run_id
        self._default_model = model
        self._default_prompt_version = prompt_version

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
        timeout_agents: list | None = None,
        run_id: str = "",
        max_steps: int = 0,
        remaining_steps: int = 0,
        wall_time_since_start: float = 0.0,
        step_duration_ms: float = 0.0,
        error_types: list | None = None,
        completed_subtasks_delta: list | None = None,
        end_reason: str = "",
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
            timeout_agents: List of agent indices that were auto-filled with
                NoOp due to barrier timeout (empty if all agents submitted).
            run_id: Experiment run identifier.
            max_steps: Maximum allowed steps for the task.
            remaining_steps: Steps remaining in the task.
            wall_time_since_start: Wall-clock seconds since experiment start.
            step_duration_ms: Duration of this step in milliseconds.
            error_types: Per-agent error type strings.
            completed_subtasks_delta: Subtask descriptions completed this step.
            end_reason: Reason the experiment ended.
        """
        with self._lock:
            self._ensure_file("trajectory")
            row = {
                "Step": step_num,
                "Actions": actions,
                "Successes": successes,
                "Observations": observations,
                "Coverage": coverage,
                "TransportRate": transport_rate,
                "Finished": finished,
                "TimeoutAgents": timeout_agents or [],
                "RunID": run_id or self._default_run_id,
                "MaxSteps": max_steps,
                "RemainingSteps": remaining_steps,
                "WallTimeSinceStart": wall_time_since_start,
                "StepDurationMs": step_duration_ms,
                "ErrorTypes": error_types or [],
                "CompletedSubtasksDelta": completed_subtasks_delta or [],
                "EndReason": end_reason,
            }
            self._writers["trajectory"].writerow(row)
            self._files["trajectory"].flush()
            self._trajectory_rows.append(row)

            self._step_count = step_num
            self._last_coverage = coverage
            self._last_transport_rate = transport_rate
            self._finished = finished

    def set_end_reason(self, end_reason: str):
        """Backfill EndReason on all trajectory rows and rewrite trajectory.csv.

        Args:
            end_reason: Final end reason string (e.g. "success", "max_steps_reached").
        """
        with self._lock:
            for row in self._trajectory_rows:
                row["EndReason"] = end_reason
            self._ensure_file("trajectory")
            path = self._log_dir / "trajectory.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                headers = [
                    "Step",
                    "Actions",
                    "Successes",
                    "Observations",
                    "Coverage",
                    "TransportRate",
                    "Finished",
                    "TimeoutAgents",
                    "RunID",
                    "MaxSteps",
                    "RemainingSteps",
                    "WallTimeSinceStart",
                    "StepDurationMs",
                    "ErrorTypes",
                    "CompletedSubtasksDelta",
                    "EndReason",
                ]
                writer = csv.DictWriter(fh, fieldnames=headers, quoting=csv.QUOTE_ALL)
                writer.writeheader()
                writer.writerows(self._trajectory_rows)
            self._files["trajectory"].flush()

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
        run_id: str = "",
        correlation_id: str = "",
        event_type: str = "",
        tool_latency_ms: float = 0.0,
        error_type: str = "",
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
            run_id: Experiment run identifier.
            correlation_id: Unique correlation ID for tracing.
            event_type: Type of event (e.g. "tool_result").
            tool_latency_ms: Tool execution latency in milliseconds.
            error_type: Error type string if the tool failed.
        """
        with self._lock:
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
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "EventType": event_type,
                "ToolLatencyMs": tool_latency_ms,
                "ErrorType": error_type,
            }
            self._writers["agent_interactions"].writerow(row)
            self._files["agent_interactions"].flush()
            self._total_agent_interactions += 1

    # ------------------------------------------------------------------
    # Coordinator state snapshot (query_sar_state)
    # ------------------------------------------------------------------

    def log_coordinator_state(
        self,
        step: int,
        state_summary: str,
        run_id: str = "",
        correlation_id: str = "",
    ):
        """Log the SAR state snapshot as seen by the coordinator.

        Writes to agent_interactions.csv with Agent="Coordinator" and
        ToolName="query_sar_state" for traceability.

        Args:
            step: Current simulation step number.
            state_summary: Structured JSON string of the SAR state.
            run_id: Experiment run identifier.
            correlation_id: Unique correlation ID for tracing.
        """
        with self._lock:
            self._ensure_file("agent_interactions")
            row = {
                "Step": step,
                "Agent": "Coordinator",
                "ToolName": "query_sar_state",
                "ToolArgs": state_summary[:2000],
                "Action": "",
                "Observation": "",
                "LLMInput": "",
                "LLMOutput": "",
                "Thinking": "",
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "EventType": "query_sar_state",
                "ToolLatencyMs": 0.0,
                "ErrorType": "",
            }
            self._writers["agent_interactions"].writerow(row)
            self._files["agent_interactions"].flush()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        """Write run-level metadata as a JSON file.

        Args:
            metadata: Dictionary of metadata key-value pairs.
        """
        path = self._log_dir / "metadata.json"
        with self._lock:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, ensure_ascii=False, indent=2, sort_keys=True)

    # ------------------------------------------------------------------
    # Event log (NDJSON)
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Append a line to events.ndjson.

        Args:
            event_type: Type of event (e.g. "dispatch", "complete").
            payload: Optional structured payload dict.
            fields: Additional keyword fields to include in the event row.
        """
        row = {
            "timestamp": time.time(),
            "event_type": event_type,
            "run_id": fields.pop("run_id", "") or self._default_run_id,
            **fields,
            "payload": payload or {},
        }
        with self._lock:
            path = self._log_dir / "events.ndjson"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # ------------------------------------------------------------------
    # Subtask tracking (CSV)
    # ------------------------------------------------------------------

    def log_subtask(self, subtask_id: str, status: str, **fields: Any) -> None:
        """Append a row to subtasks.csv.

        Args:
            subtask_id: Unique identifier for the subtask.
            status: Current status (e.g. "assigned", "in_progress", "completed").
            fields: Additional keyword fields.
        """
        with self._lock:
            self._ensure_file("subtasks")
            row = {
                "RunID": fields.get("run_id", "") or self._default_run_id,
                "Step": fields.get("step", ""),
                "SubtaskID": subtask_id,
                "Status": status,
                "AssignedTo": fields.get("assigned_to", ""),
                "Subtask": fields.get("subtask", ""),
                "CreatedAt": fields.get("created_at", ""),
                "UpdatedAt": fields.get("updated_at", time.time()),
                "FailureClass": fields.get("failure_class", ""),
                "Details": fields.get("details", ""),
            }
            self._writers["subtasks"].writerow(row)
            self._files["subtasks"].flush()

    # ------------------------------------------------------------------
    # Router interactions
    # ------------------------------------------------------------------

    def log_router_interaction(
        self,
        step: int,
        subtask: str,
        assigned_to: str,
        run_id: str = "",
        correlation_id: str = "",
        worker_task_id: str = "",
        event_type: str = "",
    ):
        """Append a row to router_interactions.csv.

        Args:
            step: Current simulation step number.
            subtask: Description of the subtask assigned.
            assigned_to: Agent name the subtask was assigned to.
            run_id: Experiment run identifier.
            correlation_id: Unique correlation ID for tracing.
            worker_task_id: Worker task ID for correlation.
            event_type: Type of event (e.g. "dispatch_task").
        """
        with self._lock:
            self._ensure_file("router_interactions")
            row = {
                "Step": step,
                "Subtask": subtask,
                "AssignedTo": assigned_to,
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "WorkerTaskID": worker_task_id,
                "EventType": event_type,
            }
            self._writers["router_interactions"].writerow(row)
            self._files["router_interactions"].flush()
            self._total_router_interactions += 1

    # ------------------------------------------------------------------
    # Token usage
    # ------------------------------------------------------------------

    def log_token_usage(
        self,
        step: int,
        agent: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        run_id: str = "",
        llm_latency_ms: float = 0.0,
        model: str = "",
        prompt_version: str = "",
    ):
        """Append a row to token_usage.csv.

        Args:
            step: Current simulation step number.
            agent: Agent name (e.g. "Alice", "Coordinator").
            prompt_tokens: Prompt tokens consumed in this LLM call.
            completion_tokens: Completion tokens generated in this LLM call.
            total_tokens: Total tokens consumed in this LLM call.
            cache_hit_tokens: Prompt tokens served from cache.
            cache_miss_tokens: Prompt tokens not in cache.
            run_id: Experiment run identifier.
            llm_latency_ms: LLM call latency in milliseconds.
            model: Model name used for the LLM call.
            prompt_version: Prompt version identifier.
        """
        with self._lock:
            self._ensure_file("token_usage")
            row = {
                "Step": step,
                "Agent": agent,
                "PromptTokens": prompt_tokens,
                "CompletionTokens": completion_tokens,
                "TotalTokens": total_tokens,
                "CacheHitTokens": cache_hit_tokens,
                "CacheMissTokens": cache_miss_tokens,
                "RunID": run_id or self._default_run_id,
                "LLMLatencyMs": llm_latency_ms,
                "Model": model or self._default_model,
                "PromptVersion": prompt_version or self._default_prompt_version,
            }
            self._writers["token_usage"].writerow(row)
            self._files["token_usage"].flush()

            # Accumulate per-agent totals
            acc = self._token_accumulator.setdefault(
                agent,
                {
                    "prompt": 0,
                    "completion": 0,
                    "total": 0,
                    "cache_hit": 0,
                    "cache_miss": 0,
                },
            )
            acc["prompt"] += prompt_tokens
            acc["completion"] += completion_tokens
            acc["total"] += total_tokens
            acc["cache_hit"] += cache_hit_tokens
            acc["cache_miss"] += cache_miss_tokens

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def flush_summary(self):
        """Write summary.csv incrementally (overwrites each call).

        Allows the summary to survive process termination (e.g. shell timeout).
        Safe to call repeatedly — only writes when summary data has changed.
        """
        self._write_summary()

    def close(self):
        """Write summary.csv and close all open file handles."""
        self.flush_summary()
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
                "TimeoutAgents",
                "RunID",
                "MaxSteps",
                "RemainingSteps",
                "WallTimeSinceStart",
                "StepDurationMs",
                "ErrorTypes",
                "CompletedSubtasksDelta",
                "EndReason",
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
                "RunID",
                "CorrelationID",
                "EventType",
                "ToolLatencyMs",
                "ErrorType",
            ],
            "router_interactions": [
                "Step",
                "Subtask",
                "AssignedTo",
                "RunID",
                "CorrelationID",
                "WorkerTaskID",
                "EventType",
            ],
            "token_usage": [
                "Step",
                "Agent",
                "PromptTokens",
                "CompletionTokens",
                "TotalTokens",
                "CacheHitTokens",
                "CacheMissTokens",
                "RunID",
                "LLMLatencyMs",
                "Model",
                "PromptVersion",
            ],
            "subtasks": [
                "RunID",
                "Step",
                "SubtaskID",
                "Status",
                "AssignedTo",
                "Subtask",
                "CreatedAt",
                "UpdatedAt",
                "FailureClass",
                "Details",
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

        # Build dynamic token columns from accumulator
        token_columns = []
        for agent_name in sorted(self._token_accumulator.keys()):
            token_columns.extend(
                [
                    f"{agent_name}PromptTokens",
                    f"{agent_name}CompletionTokens",
                    f"{agent_name}TotalTokens",
                    f"{agent_name}CacheHitTokens",
                    f"{agent_name}CacheMissTokens",
                ]
            )

        fieldnames = [
            "ExperimentName",
            "LogDir",
            "TotalSteps",
            "FinalCoverage",
            "FinalTransportRate",
            "Finished",
            "TotalAgentInteractions",
            "TotalRouterInteractions",
        ] + token_columns

        row = {
            "ExperimentName": self.experiment_name,
            "LogDir": str(self._log_dir),
            "TotalSteps": self._step_count,
            "FinalCoverage": self._last_coverage,
            "FinalTransportRate": self._last_transport_rate,
            "Finished": self._finished,
            "TotalAgentInteractions": self._total_agent_interactions,
            "TotalRouterInteractions": self._total_router_interactions,
        }

        # Populate token columns
        for agent_name, acc in sorted(self._token_accumulator.items()):
            row[f"{agent_name}PromptTokens"] = acc["prompt"]
            row[f"{agent_name}CompletionTokens"] = acc["completion"]
            row[f"{agent_name}TotalTokens"] = acc["total"]
            row[f"{agent_name}CacheHitTokens"] = acc.get("cache_hit", 0)
            row[f"{agent_name}CacheMissTokens"] = acc.get("cache_miss", 0)

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerow(row)
