#!/usr/bin/env python3
"""sar-metrics v1 P0 collector — one ``eval_metrics.json`` per run.

Reads a single SAR run directory (plus the evaluator-private ``truth_dir``
recorded in ``metadata.json``) and emits the full P0 metric set of the SAR
six-layer metric system (``.hermes/spec/sar-metrics/README.md`` §3) into
``<results_dir>/eval_metrics.json``.  Sibling of ``memory_acceptance.py`` /
``memory_projection_quality.py``: argparse CLI, stable exit codes, read-only.

Hard boundaries (spec §5 + card decisions):
- the run dir is only ever written through ``eval_metrics.json``; the canonical
  Memory DB, the truth dir and every other artifact stay untouched;
- evaluator-private truth is consumed post-hoc only (run_id cross-check); it is
  never copied into an agent-readable path and never fed back into a run;
- missing artifacts / missing columns never crash the collector: the affected
  metric is ``null`` and the gap is recorded in ``missing_columns`` /
  ``metric_status`` (old runs legitimately lack ``NoOpSource`` / ``Status`` …).

Metric conventions pinned here:
- ``progress_auc`` / ``coverage_auc`` = per-step curve area divided by the step
  count = mean of the per-step trajectory values (rectangle rule).
- ``load_balance_b`` = ``min(s_i) / (max(s_i) + 1e-4)`` with ``s_i`` = agent i's
  count of *successful real* actions (``Success=True`` and action name != NoOp,
  so ``idle_heartbeat`` / ``llm`` / ``timeout_injected`` NoOps never pad a
  worker's labour); ``null`` when every ``s_i`` is 0.
- cache: hit rate ``ΣCacheHitTokens/ΣPromptTokens``, miss ratio ``Σ(P−H)/ΣP``;
  the ``CacheMissTokens`` column is never read (known "writes 0 while hitting"
  bug); effective billed tokens ``= Σ(P−H) + ΣC + 0.1×ΣH``.
- ``llm_latency_ms`` is reported as ``null`` and flagged in
  ``instrumentation_broken`` while the ``LLMLatencyMs`` column is all-zero
  (known collection defect, fixed by another card).
- ``map_recall`` is collected verbatim from the trajectory column and flagged in
  ``instrumentation_broken`` while it is all-zero (``set_ground_truth()`` has no
  production caller today); this card neither redefines nor rewires it.
- ``same_step_action_homogeneity`` = mean, over the steps where >=2 agents
  submitted a real (non-NoOp) action, of the modal action-name share among those
  actions (the all-steps variant and the evaluated step count ride along);
  ``consecutive_ineffective_repeat_rate`` = per agent, share of real actions that
  repeat the agent's previous action name while both attempts failed.
- units: ``trajectory.csv``'s ``StepDurationMs`` column carries *seconds*
  (observed p50 = 17.0 s for the 515 s / 30-step baseline run); the collector
  reports the raw value as ``step_duration_sec`` and never rescales it.
- ``gates.gate`` = ``pass`` only when all six red-line items are ``pass``
  (``null`` = unverifiable, therefore not a pass).  The ``unknown.ndjson`` item
  follows the artifact semantics: a file whose lines are all benign
  ``raw_request`` records (executor pre-task ingest, query_preview + real
  task_id/context_id payload — the documented "恒落此文件" case) passes; any
  task-lifecycle/trace event in it is an attribution loss and fails.

Usage:
    uv run python sar_orch/eval/run_eval_metrics.py --results-dir <run_dir> \\
        [--output <path>]
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 1
EVALUATOR_VERSION = "run-eval-metrics-1.0.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID_INPUT = 3

DEFAULT_OUTPUT = "eval_metrics.json"

_CORE_ARTIFACTS = ("run_metrics.json", "trajectory.csv", "token_usage.csv")

_NOOP_ACTION_NAMES = {"noop", "no_op"}
_NON_WORKER_ROLES = {"Coordinator", "MapAgent", "MapSummarizer"}
_BENIGN_UNKNOWN_EVENTS = {"raw_request"}
_FRAMEWORK_GATE_CODES = ("worker_busy", "task_not_routable_yet", "unknown_task_id")

# Mirrors ``sar_orch/aggregate.py::classify_failure`` (kept local on purpose:
# that module is owned by another card and must not become a runtime import).
_FAILURE_END_REASONS = {
    "budget": {"max_steps_reached", "wall_clock_timeout"},
    "framework": {
        "framework_error",
        "worker_timeout",
        "coordinator_finished_early",
        "stopped_before_success",
    },
    "environment": {"environment_error"},
}
_GATE_FAILED_END_REASONS = {"framework_error", "environment_error"}


# --------------------------------------------------------------------------
# small value helpers
# --------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
        return None
    if isinstance(value, int):
        return bool(value)
    return None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _stat_block(values: Sequence[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values),
    }


def _parse_list(cell: Any) -> list[Any] | None:
    """Parse a per-agent list cell (``Actions`` / ``Successes`` / …)."""
    if cell is None:
        return None
    text = str(cell).strip()
    if text in {"", "[]"}:
        return []
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


def _action_name(action: Any) -> str:
    text = str(action or "").strip()
    head = text.split("(", 1)[0].strip()
    return head or text


def _is_noop(action: Any) -> bool:
    return _action_name(action).lower() in _NOOP_ACTION_NAMES


def _count_lines(path: Path) -> int | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except (OSError, UnicodeDecodeError):
        return None


def _ndjson_event_names(path: Path) -> list[str] | None:
    names: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(record, dict):
                    return None
                name = record.get("event") or record.get("event_type")
                names.append(str(name) if name else "")
    except (OSError, UnicodeDecodeError):
        return None
    return names


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


class _Table:
    """A CSV artifact, remembering whether it was present at all."""

    __slots__ = ("name", "columns", "rows")

    def __init__(self, name: str, columns: list[str], rows: list[dict[str, str]]) -> None:
        self.name = name
        self.columns = columns
        self.rows = rows

    @property
    def exists(self) -> bool:
        return bool(self.columns)

    def has(self, *columns: str) -> bool:
        return all(column in self.columns for column in columns)


def _load_table(path: Path) -> _Table:
    if not path.is_file():
        return _Table(path.name, [], [])
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = [column for column in (reader.fieldnames or []) if column]
            rows = [
                {key: value for key, value in row.items() if key is not None}
                for row in reader
            ]
    except (OSError, UnicodeDecodeError, csv.Error):
        return _Table(path.name, [], [])
    return _Table(path.name, columns, rows)


def _classify_failure(end_reason: Any, finished: bool | None) -> str | None:
    if finished is None or not isinstance(end_reason, str) or not end_reason.strip():
        return None
    if finished:
        return "success"
    reason = end_reason.strip()
    for label in ("budget", "framework", "environment"):
        if reason in _FAILURE_END_REASONS[label]:
            return label
    return "unknown"


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------


class _Collector:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self.metadata = _as_dict(_load_json(results_dir / "metadata.json"))
        self.run_metrics = _as_dict(_load_json(results_dir / "run_metrics.json"))
        self.memory_acceptance = _as_dict(_load_json(results_dir / "memory_acceptance.json"))
        self.projection_quality = _as_dict(
            _load_json(results_dir / "memory_projection_quality.json")
        )
        self.long_term_quality = _as_dict(
            _load_json(results_dir / "long_term_memory_quality.json")
        )
        self.trajectory = _load_table(results_dir / "trajectory.csv")
        self.token_usage = _load_table(results_dir / "token_usage.csv")
        self.agent_interactions = _load_table(results_dir / "agent_interactions.csv")
        self.router_interactions = _load_table(results_dir / "router_interactions.csv")
        self.subtasks = _load_table(results_dir / "subtasks.csv")

        self.missing_columns: list[str] = []
        self.instrumentation_broken: list[str] = []
        self.metric_status: dict[str, str] = {}

        self._agent_names: list[str] = []
        self._agent_names_source = "unavailable"
        self._steps: int | None = _as_int(self.run_metrics.get("steps"))
        if self._steps is None and self.trajectory.exists:
            self._steps = len(self.trajectory.rows)

    # -- bookkeeping -------------------------------------------------------

    def _status(self, key: str, status: str) -> None:
        self.metric_status[key] = status

    def _require(self, table: _Table, *columns: str) -> bool:
        missing = False
        for column in columns:
            if column in table.columns:
                continue
            missing = True
            token = f"{table.name}:{column}"
            if token not in self.missing_columns:
                self.missing_columns.append(token)
        return not missing

    # -- orchestration -----------------------------------------------------

    def build(self) -> dict[str, Any]:
        self._agent_names, self._agent_names_source = self._resolve_agent_names()
        scan = self._scan_trajectory()
        event_counts = self._event_store_counts()
        return {
            "meta": self._meta(),
            "l0_task": self._l0(scan),
            "l1_state_chain": self._l1(event_counts),
            "l2_planning": self._l2(scan, event_counts),
            "l3_execution": self._l3(scan),
            "l4_cost": self._l4(scan),
            "l5_performance": self._l5(scan),
            "gates": self._gates(),
            "instrumentation_broken": sorted(set(self.instrumentation_broken)),
            "missing_columns": sorted(set(self.missing_columns)),
            "metric_status": dict(sorted(self.metric_status.items())),
        }

    # -- inputs ------------------------------------------------------------

    def _resolve_agent_names(self) -> tuple[list[str], str]:
        slots = 0
        for row in self.trajectory.rows:
            actions = _parse_list(row.get("Actions"))
            if actions:
                slots = len(actions)
                break
        workers_dir = self.results_dir / "workers"
        workers = (
            sorted(entry.name for entry in workers_dir.iterdir() if entry.is_dir())
            if workers_dir.is_dir()
            else []
        )
        usage_names = sorted(
            {
                str(row.get("Agent") or "").strip()
                for row in self.token_usage.rows
                if str(row.get("Agent") or "").strip()
                and str(row.get("Agent") or "").strip() not in _NON_WORKER_ROLES
            }
        )
        if slots and len(workers) == slots:
            return workers, "workers_dir"
        if slots and len(usage_names) == slots:
            return usage_names, "token_usage"
        if slots:
            return [f"agent_{index}" for index in range(slots)], "index_fallback"
        if workers:
            return workers, "workers_dir"
        return usage_names, "token_usage" if usage_names else "unavailable"

    def _trajectory_finished_by_step(self) -> dict[int, bool]:
        by_step: dict[int, bool] = {}
        for row in self.trajectory.rows:
            step = _as_int(row.get("Step"))
            finished = _as_bool(row.get("Finished"))
            if step is not None and finished is not None:
                by_step[step] = finished
        return by_step

    def _event_store_counts(self) -> Counter[str] | None:
        coordinator_dir = self.results_dir / "coordinator"
        paths = sorted(coordinator_dir.glob("events_*.ndjson"))
        if not paths:
            return None
        counts: Counter[str] = Counter()
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict) and record.get("event_type"):
                            counts[str(record["event_type"])] += 1
            except (OSError, UnicodeDecodeError):
                continue
        return counts

    def _scan_trajectory(self) -> dict[str, Any]:
        """Single pass over trajectory.csv: per-agent labour + NoOp origin."""
        names = self._agent_names
        per_agent: dict[str, dict[str, int]] = {
            name: {"actions": 0, "successful_actions": 0, "successful_real_actions": 0}
            for name in names
        }
        noop_counts: Counter[str] = Counter()
        real_actions: list[dict[str, Any]] = []
        real_actions_by_step: dict[int, list[str]] = defaultdict(list)
        slot_total = 0
        parse_errors = 0

        has_noop_source = self.trajectory.has("NoOpSource")
        if self.trajectory.exists and not has_noop_source:
            self._require(self.trajectory, "NoOpSource")

        for row in self.trajectory.rows:
            actions = _parse_list(row.get("Actions"))
            successes = _parse_list(row.get("Successes"))
            sources = _parse_list(row.get("NoOpSource")) if has_noop_source else None
            if actions is None or successes is None:
                parse_errors += 1
                continue
            step = _as_int(row.get("Step"))
            for index, action in enumerate(actions):
                name = names[index] if index < len(names) else f"agent_{index}"
                bucket = per_agent.setdefault(
                    name,
                    {"actions": 0, "successful_actions": 0, "successful_real_actions": 0},
                )
                success = bool(successes[index]) if index < len(successes) else False
                slot_total += 1
                bucket["actions"] += 1
                if success:
                    bucket["successful_actions"] += 1
                action_name = _action_name(action)
                if _is_noop(action):
                    source = ""
                    if sources is not None and index < len(sources):
                        source = str(sources[index] or "").strip()
                    noop_counts[source or "unspecified"] += 1
                    continue
                if success:
                    bucket["successful_real_actions"] += 1
                real_actions.append(
                    {"step": step, "agent": name, "action": action_name, "success": success}
                )
                if step is not None:
                    real_actions_by_step[step].append(action_name)

        noop_sources_missing = self.trajectory.exists and not has_noop_source
        noop_total = slot_total - len(real_actions)
        decomposition: dict[str, Any] | None = None
        if self.trajectory.exists and not noop_sources_missing:
            decomposition = {
                "real_action": len(real_actions),
                "llm": noop_counts.get("llm", 0),
                "idle_heartbeat": noop_counts.get("idle_heartbeat", 0),
                "timeout_injected": noop_counts.get("timeout_injected", 0),
                "noop_unspecified": noop_counts.get("unspecified", 0),
                "noop_total": noop_total,
                "total_slots": slot_total,
                "ratios": {
                    "real_action": _ratio(len(real_actions), slot_total),
                    "llm": _ratio(noop_counts.get("llm", 0), slot_total),
                    "idle_heartbeat": _ratio(noop_counts.get("idle_heartbeat", 0), slot_total),
                    "timeout_injected": _ratio(
                        noop_counts.get("timeout_injected", 0), slot_total
                    ),
                },
            }
        elif self.trajectory.exists:
            decomposition = {
                "real_action": len(real_actions),
                "llm": None,
                "idle_heartbeat": None,
                "timeout_injected": None,
                "noop_unspecified": None,
                "noop_total": noop_total,
                "total_slots": slot_total,
                "ratios": None,
            }

        # same-step homogeneity: share of the modal real action name per step;
        # the headline value uses the steps where >=2 agents acted ("多 agent"),
        # the all-steps value is kept for transparency.
        homogeneous: list[float] = []
        homogeneous_multi: list[float] = []
        for step_actions in real_actions_by_step.values():
            if not step_actions:
                continue
            modal = Counter(step_actions).most_common(1)[0][1]
            share = modal / len(step_actions)
            homogeneous.append(share)
            if len(step_actions) >= 2:
                homogeneous_multi.append(share)

        # consecutive ineffective repeats, per agent, in step order
        repeats = 0
        previous: dict[str, tuple[str, bool]] = {}
        for entry in real_actions:
            agent = entry["agent"]
            last = previous.get(agent)
            if (
                last is not None
                and last[0] == entry["action"]
                and not last[1]
                and not entry["success"]
            ):
                repeats += 1
            previous[agent] = (entry["action"], bool(entry["success"]))

        return {
            "per_agent": per_agent,
            "real_actions": real_actions,
            "noop_counts": noop_counts,
            "noop_decomposition": decomposition,
            "slot_total": slot_total,
            "parse_errors": parse_errors,
            "homogeneity": {
                "value": _mean(homogeneous_multi),
                "evaluated_steps": len(homogeneous_multi),
                "all_steps_value": _mean(homogeneous),
                "all_steps_evaluated": len(homogeneous),
                "definition": (
                    "mean, over steps with >=2 real (non-NoOp) actions, of the modal "
                    "action-name share among those actions"
                ),
            },
            "ineffective_repeat_rate": _ratio(repeats, len(real_actions)),
            "ineffective_repeat_count": repeats,
        }

    # -- L0 ----------------------------------------------------------------

    def _l0(self, scan: dict[str, Any]) -> dict[str, Any]:
        metrics = self.run_metrics
        finished = _as_bool(metrics.get("finished"))
        coverage = _as_float(metrics.get("coverage"))
        transport = _as_float(metrics.get("transport_rate"))
        end_reason = metrics.get("end_reason")
        steps = self._steps

        curves_ok = False
        if self.trajectory.exists:
            curves_ok = self._require(self.trajectory, "Coverage", "TransportRate")
        progress_auc = coverage_auc = None
        if curves_ok:
            transport_curve = [
                value
                for value in (_as_float(row.get("TransportRate")) for row in self.trajectory.rows)
                if value is not None
            ]
            coverage_curve = [
                value
                for value in (_as_float(row.get("Coverage")) for row in self.trajectory.rows)
                if value is not None
            ]
            progress_auc = _mean(transport_curve)
            coverage_auc = _mean(coverage_curve)

        completed_subtasks = None
        if self.trajectory.exists:
            completed_subtasks = sum(
                len(items)
                for items in (
                    _parse_list(row.get("CompletedSubtasksDelta"))
                    for row in self.trajectory.rows
                )
                if items
            )
        subtask_total = None
        if completed_subtasks is not None and transport:
            subtask_total = round(completed_subtasks / transport)

        mission_rows = [
            row
            for row in self.subtasks.rows
            if str(row.get("SubtaskID") or "").strip() == "mission"
        ]
        mission_present = bool(mission_rows)
        mission_status = (
            str(mission_rows[-1].get("Status") or "").strip() if mission_present else None
        )
        mission_consistent = None
        if mission_status and finished is not None:
            mission_consistent = (mission_status == "completed") == finished

        if finished is None or coverage is None or transport is None:
            self._status("l0_task", "missing_input")
        else:
            self._status("l0_task", "measured")
        if not mission_present:
            self._status("l0_task.mission_consistency", "not_applicable")
        elif mission_consistent is None:
            self._status("l0_task.mission_consistency", "missing_input")
        else:
            self._status("l0_task.mission_consistency", "measured")
        self._status(
            "l0_task.auc_curves",
            "measured" if curves_ok else "missing_input",
        )

        return {
            "sr": finished,
            "coverage": coverage,
            "transport_rate": transport,
            "steps": steps,
            "steps_to_success": steps if finished else None,
            "progress_auc": progress_auc,
            "coverage_auc": coverage_auc,
            "end_reason": end_reason if isinstance(end_reason, str) else None,
            "failure_class": _classify_failure(end_reason, finished),
            "completed_subtasks": completed_subtasks,
            "subtask_total": subtask_total,
            "mission_consistency": {
                "mission_row_present": mission_present,
                "mission_status": mission_status,
                "consistent_with_finished": mission_consistent,
            },
        }

    # -- L1 ----------------------------------------------------------------

    def _l1(self, event_counts: Counter[str] | None) -> dict[str, Any]:
        rows = self.trajectory.rows

        map_recall: dict[str, Any] | None = None
        if self.trajectory.exists:
            if self._require(self.trajectory, "MapRecall"):
                values = [
                    value
                    for value in (_as_float(row.get("MapRecall")) for row in rows)
                    if value is not None
                ]
                if values:
                    map_recall = {
                        "mean": _mean(values),
                        "final": values[-1],
                        "max": max(values),
                        "column_present": True,
                        "all_zero": all(value == 0 for value in values),
                    }
                    if map_recall["all_zero"]:
                        self.instrumentation_broken.append("map_recall")
                        self._status("l1_state_chain.map_recall", "instrumentation_broken")
                    else:
                        self._status("l1_state_chain.map_recall", "measured")
                else:
                    self._status("l1_state_chain.map_recall", "missing_input")
            else:
                self._status("l1_state_chain.map_recall", "missing_input")
        else:
            self._status("l1_state_chain.map_recall", "missing_input")

        freshness = None
        if self.trajectory.exists and self._require(self.trajectory, "Freshness"):
            values = [
                value
                for value in (_as_float(row.get("Freshness")) for row in rows)
                if value is not None
            ]
            if values:
                freshness = {"mean": _mean(values), "final": values[-1], "max": max(values)}
                self._status("l1_state_chain.freshness", "measured")
            else:
                self._status("l1_state_chain.freshness", "missing_input")
        else:
            self._status("l1_state_chain.freshness", "missing_input")

        worker_quality = _as_dict(self.projection_quality.get("worker_report_quality")) or None
        integration_quality = _as_dict(
            self.projection_quality.get("memory_integration_quality")
        ) or None
        evidence_rate = (
            integration_quality.get("evidence_traceability_rate")
            if integration_quality
            else None
        )
        if worker_quality or integration_quality:
            quality_status = (
                "not_applicable"
                if self.projection_quality.get("metric_status") == "not_applicable"
                else "measured"
            )
        else:
            quality_status = "missing_input"
        self._status("l1_state_chain.projection_quality", quality_status)

        long_term_metrics = _as_dict(self.long_term_quality.get("metrics")) or None
        reflection = _as_dict(self.run_metrics.get("long_term_reflection"))
        if long_term_metrics:
            self._status("l1_state_chain.long_term_metrics", "measured")
        elif reflection.get("status") in (None, "off"):
            self._status("l1_state_chain.long_term_metrics", "not_applicable")
        else:
            self._status("l1_state_chain.long_term_metrics", "missing_input")

        report_calls = None
        if self.agent_interactions.exists:
            if self._require(self.agent_interactions, "ToolName"):
                report_calls = sum(
                    1
                    for row in self.agent_interactions.rows
                    if str(row.get("ToolName") or "").strip() == "report_observation"
                )
        observation_events = (
            event_counts.get("observation_report", 0) if event_counts is not None else None
        )
        semantic_map_lines = _count_lines(self.results_dir / "semantic_map.jsonl")
        self._status(
            "l1_state_chain.observation_chain",
            "measured" if report_calls is not None and observation_events is not None
            else "missing_input",
        )

        return {
            "map_recall": map_recall,
            "freshness": freshness,
            "worker_report_quality": worker_quality,
            "memory_integration_quality": integration_quality,
            "evidence_traceability_rate": evidence_rate,
            "long_term_metrics": long_term_metrics,
            "observation_chain": {
                "report_observation_calls": report_calls,
                "observation_report_events": observation_events,
                "semantic_map_ingest_events": semantic_map_lines,
            },
        }

    # -- L2 ----------------------------------------------------------------

    def _l2(
        self, scan: dict[str, Any], event_counts: Counter[str] | None
    ) -> dict[str, Any]:
        router_ok = self.router_interactions.exists and self._require(
            self.router_interactions, "EventType"
        )
        counts: Counter[str] = Counter()
        if router_ok:
            counts.update(
                str(row.get("EventType") or "").strip()
                for row in self.router_interactions.rows
            )
        router_status = "measured" if router_ok else "missing_input"

        assign = counts.get("assign_task", 0)
        cancel = counts.get("cancel_task", 0)
        reply_to_help = counts.get("reply_to_help", 0)
        update_plan = counts.get("update_plan", 0)
        activate = counts.get("activate_plan_node", 0)
        finish_calls = counts.get("finish_task", 0)
        help_requests = (
            event_counts.get("help_request", 0) if event_counts is not None else None
        )

        dispatch_intensity = _ratio(assign, self._steps) if router_ok else None
        cancel_rate = _ratio(cancel, assign) if router_ok else None
        help_response_rate = (
            _ratio(reply_to_help, help_requests) if router_ok and help_requests else None
        )
        plan_change_count = (update_plan + activate) if router_ok else None

        labour = {
            name: values["successful_real_actions"]
            for name, values in scan["per_agent"].items()
        }
        max_labour = max(labour.values()) if labour else 0
        min_labour = min(labour.values()) if labour else 0
        load_balance = (
            min_labour / (max_labour + 1e-4) if max_labour > 0 else None
        )

        finished_by_step = self._trajectory_finished_by_step()
        premature = 0
        evaluated = 0
        if router_ok:
            for row in self.router_interactions.rows:
                if str(row.get("EventType") or "").strip() != "finish_task":
                    continue
                step = _as_int(row.get("Step"))
                if step is None:
                    continue
                finished_at_step = finished_by_step.get(step)
                if finished_at_step is None:
                    continue
                evaluated += 1
                if not finished_at_step:
                    premature += 1
        premature_rate = _ratio(premature, evaluated) if evaluated else None

        assigned_agents = sorted(
            {
                str(row.get("AssignedTo") or "").strip()
                for row in self.subtasks.rows
                if str(row.get("Status") or "").strip() == "assigned"
                and str(row.get("AssignedTo") or "").strip()
            }
        )
        agent_count = _as_int(self.metadata.get("agent_count")) or len(self._agent_names)
        dispatch_coverage = _ratio(len(assigned_agents), agent_count) if agent_count else None

        self._status("l2_planning", router_status)
        self._status(
            "l2_planning.load_balance_b",
            "measured"
            if load_balance is not None
            else ("not_applicable" if labour else "missing_input"),
        )
        help_status = "missing_input"
        if help_response_rate is not None:
            help_status = "measured"
        elif router_ok and help_requests is not None:
            help_status = "not_applicable"
        self._status("l2_planning.help_response_rate", help_status)
        premature_status = "missing_input"
        if premature_rate is not None:
            premature_status = "measured"
        elif router_ok:
            premature_status = "not_applicable"
        self._status("l2_planning.premature_finish_rate", premature_status)

        return {
            "agent_names": self._agent_names,
            "counts": {
                "assign_task": assign if router_ok else None,
                "cancel_task": cancel if router_ok else None,
                "reply_to_help": reply_to_help if router_ok else None,
                "update_plan": update_plan if router_ok else None,
                "activate_plan_node": activate if router_ok else None,
                "finish_task": finish_calls if router_ok else None,
                "help_request_events": help_requests,
            },
            "dispatch_intensity_per_step": dispatch_intensity,
            "cancel_rate": cancel_rate,
            "help_response_rate": help_response_rate,
            "plan_change_count": plan_change_count,
            "load_balance_b": load_balance,
            "load_balance_detail": {
                "successful_real_actions_per_agent": labour,
                "min": min_labour,
                "max": max_labour,
                "formula": "min(s_i) / (max(s_i) + 1e-4)",
            },
            "premature_finish_rate": premature_rate,
            "premature_finish_detail": {
                "finish_task_calls": finish_calls if router_ok else None,
                "evaluated_calls": evaluated,
                "premature_calls": premature,
            },
            "dispatch_coverage": dispatch_coverage,
            "assigned_agents": assigned_agents,
        }

    # -- L3 ----------------------------------------------------------------

    def _l3(self, scan: dict[str, Any]) -> dict[str, Any]:
        per_agent_report: dict[str, Any] = {}
        total_actions = 0
        total_success = 0
        for name, values in scan["per_agent"].items():
            total_actions += values["actions"]
            total_success += values["successful_actions"]
            per_agent_report[name] = {
                **values,
                "rate": _ratio(values["successful_actions"], values["actions"]),
            }

        tool_success_rate = None
        tool_calls = None
        if self.agent_interactions.exists:
            tool_calls = len(self.agent_interactions.rows)
            if self._require(self.agent_interactions, "Success"):
                successes = sum(
                    1
                    for row in self.agent_interactions.rows
                    if str(row.get("Success") or "").strip().lower() in {"true", "1", "yes"}
                )
                tool_success_rate = _ratio(successes, tool_calls)

        first_coverage = last_coverage = None
        if self.trajectory.exists and self.trajectory.rows:
            coverage_values = [
                value
                for value in (_as_float(row.get("Coverage")) for row in self.trajectory.rows)
                if value is not None
            ]
            if coverage_values:
                first_coverage = coverage_values[0]
                last_coverage = coverage_values[-1]
        explore_calls = None
        if self.agent_interactions.exists and self._require(
            self.agent_interactions, "ToolName"
        ):
            explore_calls = sum(
                1
                for row in self.agent_interactions.rows
                if str(row.get("ToolName") or "").strip().lower() == "explore"
            )
        coverage_delta = (
            last_coverage - first_coverage
            if first_coverage is not None and last_coverage is not None
            else None
        )

        pruning: dict[str, Any] = {"per_agent": {}}
        prune_total = discard_total = 0
        workers_dir = self.results_dir / "workers"
        if workers_dir.is_dir():
            for agent_dir in sorted(entry for entry in workers_dir.iterdir() if entry.is_dir()):
                prune_path = agent_dir / agent_dir.name / "context" / "prune_events.ndjson"
                discard_path = agent_dir / agent_dir.name / "context" / "discards.ndjson"
                prunes = _count_lines(prune_path) if prune_path.is_file() else 0
                discards = _count_lines(discard_path) if discard_path.is_file() else 0
                pruning["per_agent"][agent_dir.name] = {
                    "prune_events": prunes,
                    "discards": discards,
                }
                prune_total += prunes or 0
                discard_total += discards or 0
        pruning["prune_event_count"] = prune_total if pruning["per_agent"] else None
        pruning["discard_count"] = discard_total if pruning["per_agent"] else None

        noop_status = "missing_input"
        if scan["noop_decomposition"] is not None:
            noop_status = (
                "measured"
                if scan["noop_decomposition"]["llm"] is not None
                else "missing_input"
            )
        self._status("l3_execution", "measured" if self.trajectory.exists else "missing_input")
        self._status("l3_execution.noop_decomposition", noop_status)

        return {
            "action_success_rate": _ratio(total_success, total_actions),
            "per_agent_action_success": per_agent_report,
            "tool_call_success_rate": tool_success_rate,
            "tool_call_count": tool_calls,
            "noop_decomposition": scan["noop_decomposition"],
            "same_step_action_homogeneity": scan["homogeneity"],
            "consecutive_ineffective_repeat_rate": scan["ineffective_repeat_rate"],
            "consecutive_ineffective_repeat_count": scan["ineffective_repeat_count"],
            "exploration_efficiency": {
                "coverage_start": first_coverage,
                "coverage_end": last_coverage,
                "coverage_delta": coverage_delta,
                "explore_calls": explore_calls,
                "coverage_gain_per_explore": _ratio(coverage_delta, explore_calls),
            },
            "context_pruning": pruning,
            "parse_error_rows": scan["parse_errors"],
            "action_total_slots": scan["slot_total"],
        }

    # -- L4 ----------------------------------------------------------------

    def _l4(self, scan: dict[str, Any]) -> dict[str, Any]:
        by_agent: dict[str, dict[str, Any]] = {}
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_hit_tokens": 0}
        requests = 0
        errors = None
        cache_miss_tokens = 0
        if self.token_usage.exists:
            columns_ok = self._require(
                self.token_usage,
                "PromptTokens",
                "CompletionTokens",
                "CacheHitTokens",
            )
            if self.token_usage.has("Status"):
                errors = 0
            requests = len(self.token_usage.rows)
            for row in self.token_usage.rows:
                agent = str(row.get("Agent") or "unknown").strip() or "unknown"
                bucket = by_agent.setdefault(
                    agent,
                    {
                        "requests": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cache_hit_tokens": 0,
                    },
                )
                prompt = _as_int(row.get("PromptTokens")) or 0
                completion = _as_int(row.get("CompletionTokens")) or 0
                total = _as_int(row.get("TotalTokens"))
                hit = _as_int(row.get("CacheHitTokens")) or 0
                bucket["requests"] += 1
                bucket["prompt_tokens"] += prompt
                bucket["completion_tokens"] += completion
                bucket["total_tokens"] += total if total is not None else prompt + completion
                bucket["cache_hit_tokens"] += hit
                totals["prompt_tokens"] += prompt
                totals["completion_tokens"] += completion
                totals["total_tokens"] += total if total is not None else prompt + completion
                totals["cache_hit_tokens"] += hit
                cache_miss_tokens += max(prompt - hit, 0)
                if errors is not None and str(row.get("Status") or "").strip() != "ok":
                    errors += 1
            if not columns_ok:
                totals = {key: None for key in totals}
                cache_miss_tokens = None

        total_tokens = totals.get("total_tokens")
        prompt_tokens = totals.get("prompt_tokens")
        hit_tokens = totals.get("cache_hit_tokens")
        completion_tokens = totals.get("completion_tokens")
        cache_hit_rate = (
            _ratio(hit_tokens, prompt_tokens) if prompt_tokens else None
        )
        cache_miss_ratio = (
            _ratio(cache_miss_tokens, prompt_tokens) if prompt_tokens else None
        )
        effective_billed = None
        if (
            prompt_tokens is not None
            and completion_tokens is not None
            and hit_tokens is not None
            and cache_miss_tokens is not None
        ):
            effective_billed = cache_miss_tokens + completion_tokens + 0.1 * hit_tokens
        error_rate = _ratio(errors, requests) if errors is not None else None

        transport = _as_float(self.run_metrics.get("transport_rate"))
        finished = _as_bool(self.run_metrics.get("finished"))
        completed_subtasks = None
        if self.trajectory.exists:
            completed_subtasks = sum(
                len(items)
                for items in (
                    _parse_list(row.get("CompletedSubtasksDelta"))
                    for row in self.trajectory.rows
                )
                if items
            )
        tokens_per_success = None
        if finished and completed_subtasks and total_tokens is not None:
            tokens_per_success = total_tokens / completed_subtasks

        reflection_stats = (
            _as_dict(
                _as_dict(self.long_term_quality.get("metrics")).get(
                    "reflection_latency_stats"
                )
            )
            or None
        )

        self._status("l4_cost", "measured" if self.token_usage.exists else "missing_input")
        self._status(
            "l4_cost.cache",
            "measured" if cache_hit_rate is not None else "missing_input",
        )
        self._status(
            "l4_cost.llm_error_rate",
            "measured" if error_rate is not None else "missing_input",
        )
        self._status(
            "l4_cost.tokens_per_success",
            "measured" if tokens_per_success is not None else "not_applicable",
        )
        self._status(
            "l4_cost.reflection_overhead",
            "measured" if reflection_stats else "not_applicable",
        )

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_hit_tokens": hit_tokens,
            "cache_miss_tokens_derived": cache_miss_tokens if prompt_tokens else None,
            "cache_hit_rate": cache_hit_rate,
            "cache_miss_ratio": cache_miss_ratio,
            "effective_billed_tokens": effective_billed,
            "by_agent": by_agent,
            "llm_request_count": requests if self.token_usage.exists else None,
            "llm_error_count": errors,
            "llm_error_rate": error_rate,
            "tool_call_count": len(self.agent_interactions.rows)
            if self.agent_interactions.exists
            else None,
            "tokens_per_step": _ratio(total_tokens, self._steps),
            "tokens_per_progress": _ratio(total_tokens, transport),
            "tokens_per_success": tokens_per_success,
            "completed_subtasks": completed_subtasks,
            "reflection_overhead": reflection_stats,
        }

    # -- L5 ----------------------------------------------------------------

    def _l5(self, scan: dict[str, Any]) -> dict[str, Any]:
        wall_clock = _as_float(self.run_metrics.get("elapsed_seconds"))

        step_durations: list[float] = []
        if self.trajectory.exists and self._require(self.trajectory, "StepDurationMs"):
            step_durations = [
                value
                for value in (
                    _as_float(row.get("StepDurationMs")) for row in self.trajectory.rows
                )
                if value is not None
            ]

        tool_latencies: list[float] = []
        if self.agent_interactions.exists and self._require(
            self.agent_interactions, "ToolLatencyMs"
        ):
            tool_latencies = [
                value
                for value in (
                    _as_float(row.get("ToolLatencyMs"))
                    for row in self.agent_interactions.rows
                )
                if value is not None
            ]

        llm_latency = None
        llm_latency_status = "missing_input"
        if self.token_usage.exists:
            if self._require(self.token_usage, "LLMLatencyMs"):
                latency_values = [
                    value
                    for value in (
                        _as_float(row.get("LLMLatencyMs")) for row in self.token_usage.rows
                    )
                    if value is not None
                ]
                if latency_values and any(value > 0 for value in latency_values):
                    llm_latency = _stat_block(latency_values)
                    llm_latency_status = "measured"
                elif latency_values:
                    # known collection defect: the column is written but always 0
                    self.instrumentation_broken.append("llm_latency")
                    llm_latency_status = "instrumentation_broken"
        self._status("l5_performance.llm_latency", llm_latency_status)

        timeout_injected = None
        if scan["noop_decomposition"] is not None:
            timeout_injected = scan["noop_decomposition"]["timeout_injected"]
        timeout_column = None
        if self.trajectory.exists and self._require(self.trajectory, "TimeoutAgents"):
            timeout_column = 0
            for row in self.trajectory.rows:
                items = _parse_list(row.get("TimeoutAgents"))
                if items:
                    timeout_column += len(items)
        slots = scan["slot_total"] or None

        error_count = None
        error_rate = None
        if self.token_usage.exists and self._require(self.token_usage, "Status"):
            error_count = sum(
                1
                for row in self.token_usage.rows
                if str(row.get("Status") or "").strip() != "ok"
            )
            error_rate = _ratio(error_count, len(self.token_usage.rows))

        framework_counts = _as_dict(self.memory_acceptance.get("framework_error_counts")) or None
        if framework_counts is not None:
            framework_counts = {
                code: framework_counts.get(code, 0) for code in _FRAMEWORK_GATE_CODES
            }

        self._status(
            "l5_performance", "measured" if self.trajectory.exists else "missing_input"
        )
        self._status(
            "l5_performance.step_duration",
            "measured" if step_durations else "missing_input",
        )
        self._status(
            "l5_performance.tool_latency",
            "measured" if tool_latencies else "missing_input",
        )

        return {
            "wall_clock_seconds": wall_clock,
            "step_duration_sec": _stat_block(step_durations),
            "step_duration_note": (
                "trajectory.csv StepDurationMs carries seconds (baseline p50 = 17.0 s); "
                "raw values are reported without rescaling"
            ),
            "tool_latency_ms": _stat_block(tool_latencies),
            "llm_latency_ms": llm_latency,
            "barrier_timeout": {
                "timeout_injected": timeout_injected,
                "timeout_agents_column_total": timeout_column,
                "rate": _ratio(timeout_injected, slots),
            },
            "llm_request_error_rate": error_rate,
            "llm_request_error_count": error_count,
            "framework_error_counts": framework_counts,
        }

    # -- gates -------------------------------------------------------------

    def _gates(self) -> dict[str, Any]:
        items: dict[str, str | None] = {}
        detail: dict[str, Any] = {}

        acceptance = _as_dict(self.run_metrics.get("memory_terminal")).get("acceptance_gate")
        if isinstance(acceptance, str) and acceptance.strip():
            items["acceptance_gate"] = "pass" if acceptance.strip() == "pass" else "fail"
        else:
            items["acceptance_gate"] = None
        detail["acceptance_gate"] = acceptance

        violations = _as_dict(self.long_term_quality.get("metrics")).get(
            "forbidden_truth_violation_count"
        )
        if isinstance(violations, (int, float)) and not isinstance(violations, bool):
            items["forbidden_truth_violation_count"] = (
                "pass" if int(violations) == 0 else "fail"
            )
        else:
            items["forbidden_truth_violation_count"] = None
        detail["forbidden_truth_violation_count"] = violations

        counts = _as_dict(self.memory_acceptance.get("framework_error_counts")) or None
        if counts is not None:
            offenders = {
                code: int(counts.get(code, 0))
                for code in _FRAMEWORK_GATE_CODES
                if int(counts.get(code, 0)) > 0
            }
            items["framework_error_counts"] = "fail" if offenders else "pass"
            detail["framework_error_counts"] = {
                code: counts.get(code, 0) for code in _FRAMEWORK_GATE_CODES
            }
        else:
            items["framework_error_counts"] = None
            detail["framework_error_counts"] = None

        end_reason = self.run_metrics.get("end_reason")
        if isinstance(end_reason, str) and end_reason.strip():
            items["end_reason"] = (
                "fail" if end_reason.strip() in _GATE_FAILED_END_REASONS else "pass"
            )
        else:
            items["end_reason"] = None
        detail["end_reason"] = end_reason if isinstance(end_reason, str) else None

        unknown_entries: list[dict[str, Any]] = []
        verdict = "pass"
        for path in sorted(self.results_dir.rglob("unknown.ndjson")):
            if not path.is_file():
                continue
            names = _ndjson_event_names(path)
            non_benign = (
                [name for name in names if name not in _BENIGN_UNKNOWN_EVENTS]
                if names is not None
                else ["<unreadable>"]
            )
            unknown_entries.append(
                {
                    "path": str(path.relative_to(self.results_dir)),
                    "bytes": path.stat().st_size,
                    "event_names": names,
                    "non_benign_events": non_benign,
                }
            )
            if non_benign:
                verdict = "fail"
        items["unknown_ndjson"] = verdict
        detail["unknown_ndjson"] = {
            "files": unknown_entries,
            "benign_event_names": sorted(_BENIGN_UNKNOWN_EVENTS),
            "rule": (
                "pass when no unknown.ndjson exists, or every file carries only the "
                "benign executor 'raw_request' ingest record; any task/trace event "
                "in unknown.ndjson is an attribution loss"
            ),
        }

        run_id = self.metadata.get("run_id")
        truth_dir_raw = self.metadata.get("truth_dir")
        truth_dir = Path(str(truth_dir_raw)) if truth_dir_raw else None
        manifest = (
            _as_dict(_load_json(truth_dir / "truth_manifest.json")) if truth_dir else {}
        )
        manifest_run_id = manifest.get("run_id")
        if isinstance(run_id, str) and run_id and isinstance(manifest_run_id, str) and manifest_run_id:
            items["truth_manifest_run_id"] = "pass" if run_id == manifest_run_id else "fail"
        else:
            items["truth_manifest_run_id"] = None
        detail["truth_manifest_run_id"] = {
            "run_id": run_id if isinstance(run_id, str) else None,
            "truth_manifest_run_id": manifest_run_id if isinstance(manifest_run_id, str) else None,
            "truth_dir": str(truth_dir) if truth_dir else None,
            "truth_dir_present": bool(truth_dir and truth_dir.is_dir()),
        }
        self._status(
            "meta.truth_input",
            "measured" if truth_dir and truth_dir.is_dir() else "missing_input",
        )

        all_pass = bool(items) and all(value == "pass" for value in items.values())
        return {
            **items,
            "gate": "pass" if all_pass else "fail",
            "gate_rule": "pass only when all six red-line items are pass (null = unverifiable -> fail)",
            "detail": detail,
        }

    # -- meta --------------------------------------------------------------

    def _meta(self) -> dict[str, Any]:
        metadata = self.metadata
        reflection = _as_dict(self.run_metrics.get("long_term_reflection"))
        truth_dir_raw = metadata.get("truth_dir")
        truth_dir = str(truth_dir_raw) if isinstance(truth_dir_raw, str) and truth_dir_raw else None
        return {
            "schema_version": SCHEMA_VERSION,
            "evaluator_version": EVALUATOR_VERSION,
            "results_dir": str(self.results_dir.resolve()),
            "metadata_present": bool(metadata),
            "run_id": metadata.get("run_id"),
            "scene": metadata.get("scene"),
            "agents": metadata.get("agent_count"),
            "seed": metadata.get("seed"),
            "model": metadata.get("model"),
            "provider": metadata.get("provider"),
            "code_commit": metadata.get("code_commit"),
            "max_steps": metadata.get("max_steps"),
            "memory_read_mode": metadata.get("memory_read_mode"),
            "long_term_mode": metadata.get("long_term_mode"),
            "state_mode": metadata.get("state_mode"),
            "prompt_version": metadata.get("prompt_version"),
            "long_term_reflection_status": reflection.get("status"),
            "truth_dir": truth_dir,
            "agent_names": self._agent_names,
            "agent_names_source": self._agent_names_source,
        }


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def evaluate(results_dir: str | Path) -> dict[str, Any]:
    """Collect every P0 metric for one run directory."""
    return _Collector(Path(results_dir)).build()


def write_artifact(
    results_dir: str | Path, payload: dict[str, Any], output: str | Path | None = None
) -> Path:
    """Write the JSON payload (idempotent overwrite) and return its path."""
    out_path = Path(output) if output else Path(results_dir) / DEFAULT_OUTPUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return out_path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="sar-metrics v1 P0 metric collector for one SAR run"
    )
    parser.add_argument("--results-dir", required=True, help="Run results directory")
    parser.add_argument(
        "--output",
        default=None,
        help=f"Output path (default: <results_dir>/{DEFAULT_OUTPUT})",
    )
    return parser.parse_args(argv)


def _print_summary(payload: dict[str, Any], output: Path) -> None:
    l0 = payload["l0_task"]
    l3 = payload["l3_execution"]
    l4 = payload["l4_cost"]
    noop = l3.get("noop_decomposition") or {}
    print(f"run_eval_metrics: wrote {output}")
    print(
        "  sr={sr} coverage={coverage} transport={transport} steps={steps} "
        "end_reason={end_reason} gate={gate}".format(
            sr=l0.get("sr"),
            coverage=l0.get("coverage"),
            transport=l0.get("transport_rate"),
            steps=l0.get("steps"),
            end_reason=l0.get("end_reason"),
            gate=payload["gates"].get("gate"),
        )
    )
    print(
        "  llm_requests={requests} llm_errors={errors} "
        "noop(real/llm/idle/timeout)={real}/{llm}/{idle}/{timeout} "
        "broken={broken} missing_columns={missing}".format(
            requests=l4.get("llm_request_count"),
            errors=l4.get("llm_error_count"),
            real=noop.get("real_action"),
            llm=noop.get("llm"),
            idle=noop.get("idle_heartbeat"),
            timeout=noop.get("timeout_injected"),
            broken=payload["instrumentation_broken"],
            missing=payload["missing_columns"],
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(
            f"run_eval_metrics: results dir not found: {results_dir}", file=sys.stderr
        )
        return EXIT_INVALID_INPUT
    if not any((results_dir / name).is_file() for name in _CORE_ARTIFACTS):
        print(
            "run_eval_metrics: no run artifacts found under "
            f"{results_dir} (expected one of {list(_CORE_ARTIFACTS)})",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT
    payload = evaluate(results_dir)
    output = write_artifact(results_dir, payload, args.output)
    _print_summary(payload, output)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
