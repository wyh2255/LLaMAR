"""AI2ThorExperimentLogger — CSV/NDJSON experiment artifacts (env-contract P5-3).

The assembly-contract logger for AI2Thor runs.  ``orchestration.assembly``
consumes it through the same interface SAR's ``ExperimentLogger`` provides
(``set_run_context`` / ``write_metadata`` / ``log_step`` / ``flush_summary`` /
``set_end_reason`` / ``freeze_terminal`` / ``get_log_dir`` / ``close``) and the
generic coordinator/worker skeletons additionally call ``log_event`` /
``log_subtask`` / ``log_router_interaction`` / ``log_token_usage`` /
``log_agent_interaction`` / ``log_coordinator_state`` at runtime.

Artifacts under ``log_dir``:

- ``metadata.json``        — run metadata (``write_metadata``)
- ``trajectory.csv``       — one row per executed env step (``log_step``)
- ``agent_interactions.csv`` — worker tool calls (per tool_result event)
- ``router_interactions.csv`` — coordinator dispatch history
- ``token_usage.csv``      — one row per LLM request (token accounting)
- ``subtasks.csv``         — subtask/mission lifecycle rows
- ``events.ndjson``        — coordinator send_message semantics events
- ``summary.csv``          — single-row aggregate (incremental, crash-safe)

The SAR trajectory-only columns (MapRecall / Freshness) are deliberately not
emitted: AI2Thor has no semantic-map recall channel — the corresponding
per-step values live in ``coverage`` / ``transport_rate`` /
``CompletedSubtasksDelta``.  ``AI2THOR_TRAJECTORY_HEADERS`` is the single
source for the trailer-relevant column order.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

#: ``trajectory.csv`` columns (assembly ``log_step`` fields, AI2Thor subset).
AI2THOR_TRAJECTORY_HEADERS = [
    "Step",
    "Actions",
    "Successes",
    "Observations",
    "Coverage",
    "TransportRate",
    "Finished",
    "TimeoutAgents",
    "NoOpSource",
    "RunID",
    "MaxSteps",
    "RemainingSteps",
    "WallTimeSinceStart",
    "StepDurationMs",
    "ErrorTypes",
    "CompletedSubtasksDelta",
    "EndReason",
]

_HEADERS: dict[str, list[str]] = {
    "trajectory": AI2THOR_TRAJECTORY_HEADERS,
    "agent_interactions": [
        "Step",
        "Agent",
        "ToolName",
        "ToolArgs",
        "Action",
        "Observation",
        "LLMInput",
        "LLMInputChars",
        "LLMOutput",
        "Thinking",
        "RunID",
        "CorrelationID",
        "EventType",
        "ToolLatencyMs",
        "Success",
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
        "Success",
        "ErrorType",
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
        "Status",
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


class AI2ThorExperimentLogger:
    """Write AI2Thor experiment logs under ``log_dir``.

    Thread-safe: the coordinator/worker skeletons call the interaction/token
    methods from their own threads, while the assembly poll loop calls
    ``log_step`` from the main event loop.
    """

    def __init__(
        self,
        experiment_name: str = "ai2thor_experiment",
        log_dir: str | None = None,
    ) -> None:
        self.experiment_name = experiment_name
        self._log_dir = Path(log_dir) if log_dir is not None else Path.cwd()
        os.makedirs(self._log_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._files: dict[str, Any] = {}
        self._writers: dict[str, csv.DictWriter] = {}

        # Run context (defaults for RunID / Model / PromptVersion columns).
        self._default_run_id: str = ""
        self._default_model: str = ""
        self._default_prompt_version: str = ""

        # Aggregate state for summary.csv.
        self._step_count: int = 0
        self._last_coverage: float = 0.0
        self._last_transport_rate: float = 0.0
        self._finished: bool = False
        self._end_reason: str = ""
        self._total_agent_interactions: int = 0
        self._total_router_interactions: int = 0
        self._token_accumulator: dict[str, dict[str, int]] = {}

        # Trajectory row buffer for EndReason backfill (set_end_reason).
        self._trajectory_rows: list[dict] = []

        # Post-terminal freeze: outcome rows stop being appended (in-flight
        # rounds during teardown must not pollute run-terminal CSVs).
        self._terminal_frozen: bool = False

    # ------------------------------------------------------------------
    # Run context / metadata
    # ------------------------------------------------------------------

    def set_run_context(
        self, run_id: str = "", model: str = "", prompt_version: str = "baseline"
    ) -> None:
        """Set default field values inherited by subsequent log calls."""
        self._default_run_id = run_id
        self._default_model = model
        self._default_prompt_version = prompt_version

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        """Write run-level metadata as ``metadata.json``."""
        path = self._log_dir / "metadata.json"
        with self._lock, open(path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2, sort_keys=True)

    # ------------------------------------------------------------------
    # Trajectory (assembly log_step)
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
        noop_sources: list | None = None,
        map_recall: float = 0.0,  # SAR-only; accepted and ignored
        freshness: float = 0.0,  # SAR-only; accepted and ignored
        run_id: str = "",
        max_steps: int = 0,
        remaining_steps: int = 0,
        wall_time_since_start: float = 0.0,
        step_duration_ms: float = 0.0,
        error_types: list | None = None,
        completed_subtasks_delta: list | None = None,
        end_reason: str = "",
    ) -> None:
        """Append one row to ``trajectory.csv`` (one executed env step)."""
        del map_recall, freshness  # SAR-only channels; not part of AI2Thor CSV
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
                "NoOpSource": noop_sources or [],
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

    def set_end_reason(self, end_reason: str) -> None:
        """Backfill ``EndReason`` on all trajectory rows and rewrite the CSV."""
        with self._lock:
            self._end_reason = end_reason
            for row in self._trajectory_rows:
                row["EndReason"] = end_reason
            self._ensure_file("trajectory")
            path = self._log_dir / "trajectory.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=AI2THOR_TRAJECTORY_HEADERS, quoting=csv.QUOTE_ALL
                )
                writer.writeheader()
                writer.writerows(self._trajectory_rows)

    # ------------------------------------------------------------------
    # Agent / router interactions
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
        llm_input_chars: int = 0,
        llm_output: str = "",
        thinking: str = "",
        run_id: str = "",
        correlation_id: str = "",
        event_type: str = "",
        tool_latency_ms: float = 0.0,
        error_type: str = "",
        success: bool | None = None,
        error_code: str = "",
    ) -> None:
        """Append a row to ``agent_interactions.csv`` (worker tool call)."""
        with self._lock:
            if self._terminal_frozen:
                return
            self._ensure_file("agent_interactions")
            row = {
                "Step": step,
                "Agent": agent,
                "ToolName": tool_name,
                "ToolArgs": tool_args,
                "Action": action,
                "Observation": observation,
                "LLMInput": llm_input,
                "LLMInputChars": llm_input_chars,
                "LLMOutput": llm_output,
                "Thinking": thinking,
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "EventType": event_type,
                "ToolLatencyMs": tool_latency_ms,
                "Success": "" if success is None else str(success),
                "ErrorType": error_code if error_code else error_type,
            }
            self._writers["agent_interactions"].writerow(row)
            self._files["agent_interactions"].flush()
            self._total_agent_interactions += 1

    def log_router_interaction(
        self,
        step: int,
        subtask: str,
        assigned_to: str,
        run_id: str = "",
        correlation_id: str = "",
        worker_task_id: str = "",
        event_type: str = "",
        success: bool | None = None,
        error_code: str = "",
    ) -> None:
        """Append a row to ``router_interactions.csv`` (coordinator dispatch)."""
        with self._lock:
            if self._terminal_frozen:
                return
            self._ensure_file("router_interactions")
            row = {
                "Step": step,
                "Subtask": subtask,
                "AssignedTo": assigned_to,
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "WorkerTaskID": worker_task_id,
                "EventType": event_type,
                "Success": "" if success is None else str(success),
                "ErrorType": error_code,
            }
            self._writers["router_interactions"].writerow(row)
            self._files["router_interactions"].flush()
            self._total_router_interactions += 1

    def log_coordinator_state(
        self,
        step: int,
        state_summary: str,
        run_id: str = "",
        correlation_id: str = "",
    ) -> None:
        """Append a coordinator state snapshot row to ``agent_interactions.csv``.

        Generic coordinator surfaces (SAR's ``query_sar_state``) route through
        this method; AI2Thor has no oracle query tool, so in practice this is
        a contract-completeness surface.
        """
        with self._lock:
            if self._terminal_frozen:
                return
            self._ensure_file("agent_interactions")
            row = {
                "Step": step,
                "Agent": "Coordinator",
                "ToolName": "coordinator_state",
                "ToolArgs": state_summary[:2000],
                "Action": "",
                "Observation": "",
                "LLMInput": "",
                "LLMInputChars": 0,
                "LLMOutput": "",
                "Thinking": "",
                "RunID": run_id or self._default_run_id,
                "CorrelationID": correlation_id,
                "EventType": "coordinator_state",
                "ToolLatencyMs": 0.0,
                "Success": "",
                "ErrorType": "",
            }
            self._writers["agent_interactions"].writerow(row)
            self._files["agent_interactions"].flush()

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
        status: str = "ok",
    ) -> None:
        """Append a row to ``token_usage.csv`` (one row per LLM request)."""
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
                "Status": status,
            }
            self._writers["token_usage"].writerow(row)
            self._files["token_usage"].flush()

            acc = self._token_accumulator.setdefault(
                agent,
                {"prompt": 0, "completion": 0, "total": 0, "cache_hit": 0, "cache_miss": 0},
            )
            acc["prompt"] += prompt_tokens
            acc["completion"] += completion_tokens
            acc["total"] += total_tokens
            acc["cache_hit"] += cache_hit_tokens
            acc["cache_miss"] += cache_miss_tokens

    # ------------------------------------------------------------------
    # Events / subtasks
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Append a line to ``events.ndjson``."""
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

    def log_subtask(self, subtask_id: str, status: str, **fields: Any) -> None:
        """Append a row to ``subtasks.csv`` (subtask / mission lifecycle)."""
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
    # Lifecycle
    # ------------------------------------------------------------------

    def freeze_terminal(self) -> None:
        """Freeze outcome CSVs against post-terminal rows (teardown safety)."""
        with self._lock:
            self._terminal_frozen = True

    def flush_summary(self) -> None:
        """Write ``summary.csv`` incrementally (overwrites each call).

        Keeps the aggregate available even if the process is killed (shell
        timeout) before ``close()``.
        """
        self._write_summary()

    def close(self) -> None:
        """Write ``summary.csv`` and close all open file handles."""
        self.flush_summary()
        with self._lock:
            for fh in self._files.values():
                if not fh.closed:
                    fh.close()
            self._writers.clear()
            self._files.clear()

    def get_log_dir(self) -> str:
        """Return the absolute path of the log directory."""
        return str(self._log_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_file(self, name: str) -> None:
        """Lazily open a CSV file and write its header on first access."""
        if name in self._files:
            return
        path = self._log_dir / f"{name}.csv"
        # Persistent append handle by design (closed in ``close()``), mirroring
        # the SAR ExperimentLogger — later rows may arrive from other threads.
        fh = open(path, "a", newline="", encoding="utf-8")  # noqa: SIM115
        writer = csv.DictWriter(fh, fieldnames=_HEADERS[name], quoting=csv.QUOTE_ALL)
        if os.path.getsize(path) == 0:
            writer.writeheader()
        self._files[name] = fh
        self._writers[name] = writer

    def _write_summary(self) -> None:
        """Write the single-row ``summary.csv`` aggregate (crash-safe)."""
        token_columns: list[str] = []
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
            "EndReason",
            "TotalAgentInteractions",
            "TotalRouterInteractions",
        ] + token_columns

        row: dict[str, Any] = {
            "ExperimentName": self.experiment_name,
            "LogDir": str(self._log_dir),
            "TotalSteps": self._step_count,
            "FinalCoverage": self._last_coverage,
            "FinalTransportRate": self._last_transport_rate,
            "Finished": self._finished,
            "EndReason": self._end_reason,
            "TotalAgentInteractions": self._total_agent_interactions,
            "TotalRouterInteractions": self._total_router_interactions,
        }
        for agent_name, acc in sorted(self._token_accumulator.items()):
            row[f"{agent_name}PromptTokens"] = acc["prompt"]
            row[f"{agent_name}CompletionTokens"] = acc["completion"]
            row[f"{agent_name}TotalTokens"] = acc["total"]
            row[f"{agent_name}CacheHitTokens"] = acc.get("cache_hit", 0)
            row[f"{agent_name}CacheMissTokens"] = acc.get("cache_miss", 0)

        path = self._log_dir / "summary.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerow(row)


__all__ = ["AI2THOR_TRAJECTORY_HEADERS", "AI2ThorExperimentLogger"]
