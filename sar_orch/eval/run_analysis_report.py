#!/usr/bin/env python3
"""Reef-C1 analysis report generator — one ``analysis_report.json`` per SAR run.

Reads a single SAR run directory (read-only) and emits a compact, deterministic
summary for the offline analysis agent (reef spec §4.6, implementation workflow
§B7-2, board card C1): run identity, headline metrics, stability (timeout / NoOp
decomposition), subtask lifecycle counts, tool-error top-k and a fixed set of
deterministic failure signatures.

Hard boundaries:
- read-only: the run dir is only ever written through ``analysis_report.json``;
- **the evaluator-private truth dir is never read** (``metadata.truth_dir`` is
  never opened — the generator has no truth input by construction; truth stays
  evaluator-only, see ``run_eval_metrics.py``);
- the evaluator collector is not imported, modified or re-implemented: its
  artifacts (``eval_metrics.json``) are consumed verbatim, and when they are
  absent the affected fields degrade to ``null`` instead of being re-derived;
- missing artifacts / missing columns never crash the generator: the affected
  field is ``null`` + an entry in ``missing_artifacts`` / section ``notes``
  (degrade, don't fail) — old runs legitimately lack ``NoOpSource``,
  ``Success``, ``eval_metrics.json`` …;
- deterministic: same inputs -> byte-identical ``analysis_report.json`` (no
  timestamps, no randomness, sorted counters/tie-breaks, ``sort_keys=True``).

Pinned conventions (verified against the historical run corpus):
- ``trajectory.csv`` list cells are Python literals (``ast.literal_eval``), not
  JSON: ``['idle_heartbeat', '', 'timeout_injected']``;
- ``NoOpSource`` is per-agent aligned with ``Actions`` / ``TimeoutAgents``;
  ``''`` marks a real action, the injected sources are ``llm`` /
  ``idle_heartbeat`` / ``timeout_injected``;
- ``subtasks.csv``: ``assigned`` rows carry ``dispatch-N`` ids while terminal
  rows (``canceled`` / ``completed`` / ``failed``) carry the dispatch uuid;
  runs whose mission never reaches ``finish_task`` legitimately end with
  unclosed ``assigned`` rows (mission terminal rows are coordinator-driven);
- ``agent_interactions.csv``: a failed call is ``Success == "False"``; the
  error signature is ``ErrorType`` (empty -> ``"unclassified"``).

Failure signature kinds (fixed set, always emitted in this order; ``count``
``null`` = not measurable from the artifacts present, ``0`` = measured absent):
- ``cancel_churn``       — cancels issued within ``--churn-window-s`` of the
  dispatch creation timestamp (join via ``coordinator/events_dsp_*.ndjson``);
- ``cancel_storm``       — steps with >= ``--cancel-storm-threshold`` distinct
  ``cancel_task`` events;
- ``timeout_burst``      — steps where >= ``--timeout-burst-threshold`` agents
  were NoOp-filled by the barrier timeout;
- ``idle_tail``          — trailing consecutive steps without a single real
  action on a run that did not finish (normal auto-no-op tails are excluded);
- ``agent_never_active`` — agents with zero real actions over the whole run.

Report shape (top-level keys):
- ``run``                identity: run_id / scene / agents / seed / model /
                         prompt_version / code_commit / end_reason / steps /
                         max_steps / finished / wall_clock_s;
- ``metrics``            transport_rate / coverage_final (run_metrics.json, else
                         the last trajectory row) + load_balance_b /
                         effective_billed_tokens (eval_metrics.json, verbatim);
- ``stability``          steps_observed / agent_slots / real_action_slots /
                         timeout_agents_slots / timeout_steps /
                         timeout_agents_ratio / noop_source counts;
- ``subtasks``           assigned / canceled / completed / failed / other /
                         total / cancel_rate (canceled / total);
- ``tool_errors``        total_calls / failed_calls / failure_rate / top_k
                         ``[{tool, error, count}]``;
- ``failure_signatures`` fixed-order ``[{kind, count, evidence}]``;
- ``missing_artifacts`` / ``notes`` / ``truth_read`` (always ``false``).

Usage:
    uv run python sar_orch/eval/run_analysis_report.py --results-dir <run_dir> \\
        [--output <path>] [--top-k 10] [--churn-window-s 60] \\
        [--cancel-storm-threshold 3] [--timeout-burst-threshold 2]
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENERATOR_VERSION = "run-analysis-report-1.0.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID_INPUT = 3

DEFAULT_OUTPUT = "analysis_report.json"

# Signature thresholds: module defaults are pinned here, the CLI only overrides
# them per invocation (report generation stays deterministic for fixed flags).
DEFAULT_CHURN_WINDOW_S = 60.0
DEFAULT_CANCEL_STORM_THRESHOLD = 3
DEFAULT_TIMEOUT_BURST_THRESHOLD = 2
DEFAULT_TOOL_ERROR_TOP_K = 10

_MAX_EVIDENCE_IDS = 50

_NOOP_ACTION_NAMES = {"noop", "no_op"}
_NOOP_SOURCE_KEYS = ("llm", "idle_heartbeat", "timeout_injected")
_SUBTASK_STATUSES = ("assigned", "canceled", "completed", "failed")

_EXPECTED_ARTIFACTS = (
    "metadata.json",
    "run_metrics.json",
    "eval_metrics.json",
    "trajectory.csv",
    "subtasks.csv",
    "agent_interactions.csv",
    "events.ndjson",
)

# Artifacts whose presence alone marks a directory as a run dir (``eval_metrics``
# is evaluator-side and therefore optional for report generation).
_CORE_ARTIFACTS = tuple(
    name for name in _EXPECTED_ARTIFACTS if name != "eval_metrics.json"
)


# --------------------------------------------------------------------------
# small typed readers (never raise on bad input)
# --------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_epoch(value: Any) -> float | None:
    """Epoch seconds from a float/int cell or an ISO-8601 string."""
    number = _as_float(value)
    if number is not None:
        return number
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return None
        try:
            return stamp.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _parse_list(cell: Any) -> list[Any] | None:
    """Parse a per-agent list cell (``Actions`` / ``TimeoutAgents`` / …)."""
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


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


class _Table:
    """A CSV artifact, remembering whether it was present at all."""

    __slots__ = ("columns", "name", "rows")

    def __init__(
        self, name: str, columns: list[str], rows: list[dict[str, str]]
    ) -> None:
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


# --------------------------------------------------------------------------
# report builder
# --------------------------------------------------------------------------


class _Report:
    """Build one ``analysis_report.json`` payload from a single run dir."""

    def __init__(
        self,
        results_dir: str | Path,
        *,
        churn_window_s: float = DEFAULT_CHURN_WINDOW_S,
        cancel_storm_threshold: int = DEFAULT_CANCEL_STORM_THRESHOLD,
        timeout_burst_threshold: int = DEFAULT_TIMEOUT_BURST_THRESHOLD,
        tool_error_top_k: int = DEFAULT_TOOL_ERROR_TOP_K,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.churn_window_s = float(churn_window_s)
        self.cancel_storm_threshold = int(cancel_storm_threshold)
        self.timeout_burst_threshold = int(timeout_burst_threshold)
        self.tool_error_top_k = int(tool_error_top_k)

        self.notes: list[str] = []
        self.missing = [
            name
            for name in _EXPECTED_ARTIFACTS
            if not (self.results_dir / name).is_file()
        ]

        self.metadata = self._load_json("metadata.json")
        self.run_metrics = self._load_json("run_metrics.json")
        self.eval_metrics = self._load_json("eval_metrics.json")
        self.trajectory = _load_table(self.results_dir / "trajectory.csv")
        self.subtasks = _load_table(self.results_dir / "subtasks.csv")
        self.interactions = _load_table(self.results_dir / "agent_interactions.csv")
        self.events = self._load_jsonl("events.ndjson")

    # -- loaders -----------------------------------------------------------

    def _load_json(self, name: str) -> Any:
        path = self.results_dir / name
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self.notes.append(f"{name}: present but unreadable — treated as missing")
            return None

    def _load_jsonl(self, name: str) -> list[dict[str, Any]] | None:
        path = self.results_dir / name
        if not path.is_file():
            return None
        records: list[dict[str, Any]] = []
        bad_lines = 0
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        bad_lines += 1
                        continue
                    if isinstance(record, dict):
                        records.append(record)
                    else:
                        bad_lines += 1
        except (OSError, UnicodeDecodeError):
            self.notes.append(f"{name}: present but unreadable — treated as missing")
            return None
        if bad_lines:
            self.notes.append(f"{name}: {bad_lines} unparsable line(s) skipped")
        return records

    # -- helpers -----------------------------------------------------------

    def _metadata(self) -> dict[str, Any]:
        return self.metadata if isinstance(self.metadata, dict) else {}

    def _run_metrics(self) -> dict[str, Any]:
        return self.run_metrics if isinstance(self.run_metrics, dict) else {}

    def _eval_section(self, key: str) -> dict[str, Any]:
        if not isinstance(self.eval_metrics, dict):
            return {}
        section = self.eval_metrics.get(key)
        return section if isinstance(section, dict) else {}

    def _sorted_trajectory_rows(self) -> list[dict[str, str]]:
        rows = self.trajectory.rows
        keyed: list[tuple[int, dict[str, str]]] = []
        for row in rows:
            step = _as_int(row.get("Step"))
            if step is None:
                # Un-orderable row set (or no Step column): keep file order.
                return list(rows)
            keyed.append((step, row))
        return [row for _step, row in sorted(keyed, key=lambda item: item[0])]

    def _trajectory_real_actions(self) -> dict[int, int] | None:
        """Per-step count of real (non-NoOp) actions, ``None`` if unmeasurable."""
        if not self.trajectory.exists or not self.trajectory.has("Actions"):
            return None
        per_step: dict[int, int] = {}
        for index, row in enumerate(self._sorted_trajectory_rows()):
            step = _as_int(row.get("Step"))
            actions = _parse_list(row.get("Actions"))
            if actions is None:
                continue
            per_step[step if step is not None else index] = sum(
                1 for action in actions if not _is_noop(action)
            )
        return per_step

    def _timeout_slots_per_step(self) -> tuple[dict[int, int], str] | None:
        """Per-step timeout-injected agent counts + the source column used."""
        if not self.trajectory.exists:
            return None
        if self.trajectory.has("TimeoutAgents"):
            per_step: dict[int, int] = {}
            for index, row in enumerate(self._sorted_trajectory_rows()):
                step = _as_int(row.get("Step"))
                agents = _parse_list(row.get("TimeoutAgents"))
                if agents is None:
                    continue
                per_step[step if step is not None else index] = len(agents)
            return per_step, "TimeoutAgents"
        if self.trajectory.has("NoOpSource"):
            per_step = {}
            for index, row in enumerate(self._sorted_trajectory_rows()):
                step = _as_int(row.get("Step"))
                sources = _parse_list(row.get("NoOpSource"))
                if sources is None:
                    continue
                count = sum(
                    1 for value in sources if str(value).strip() == "timeout_injected"
                )
                per_step[step if step is not None else index] = count
            return per_step, "NoOpSource"
        return None

    def _agent_names(self, slots: int) -> tuple[list[str] | None, str]:
        workers_dir = self.results_dir / "workers"
        workers: list[str] = []
        if workers_dir.is_dir():
            try:
                workers = sorted(
                    entry.name for entry in workers_dir.iterdir() if entry.is_dir()
                )
            except OSError:
                workers = []
        if slots and len(workers) == slots:
            return workers, "workers_dir"
        eval_meta = self._eval_section("meta")
        eval_names = eval_meta.get("agent_names")
        if slots and isinstance(eval_names, list) and len(eval_names) == slots:
            return [str(name) for name in eval_names], "eval_metrics.meta"
        return None, "unavailable"

    def _dispatch_created_ts(self) -> dict[str, float]:
        """``dsp_*`` dispatch id -> creation epoch (per-dispatch event stream)."""
        created: dict[str, float] = {}
        coordinator_dir = self.results_dir / "coordinator"
        if not coordinator_dir.is_dir():
            return created
        for path in sorted(coordinator_dir.glob("events_dsp_*.ndjson")):
            task_id: str | None = None
            stamp: float | None = None
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
                        if not isinstance(record, dict):
                            continue
                        task_id = _as_str(record.get("task_id")) or task_id
                        stamp = _as_epoch(record.get("ts"))
                        if stamp is not None:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if task_id is None:
                stem = path.name[len("events_") : -len(".ndjson")]
                task_id = stem if stem.startswith("dsp_") else None
            if stamp is None or not task_id:
                continue
            previous = created.get(task_id)
            if previous is None or stamp < previous:
                created[task_id] = stamp
        return created

    def _cancel_events(self) -> list[tuple[str, float | None, int | None]] | None:
        """Distinct ``cancel_task`` events as ``(task_id, ts, step)``, id-sorted."""
        if self.events is None:
            return None
        seen: dict[str, tuple[str, float | None, int | None]] = {}
        for record in self.events:
            if record.get("event_type") != "cancel_task":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            task_id = _as_str(payload.get("related_task_id"))
            if not task_id:
                continue
            stamp = _as_epoch(record.get("ts"))
            if stamp is None:
                stamp = _as_epoch(record.get("timestamp"))
            step = _as_int(record.get("step"))
            previous = seen.get(task_id)
            if previous is None:
                seen[task_id] = (task_id, stamp, step)
                continue
            # Repeated cancel of the same task: keep the earliest timestamp.
            if stamp is not None and (previous[1] is None or stamp < previous[1]):
                seen[task_id] = (
                    task_id,
                    stamp,
                    step if step is not None else previous[2],
                )
        return [seen[key] for key in sorted(seen)]

    # -- sections ----------------------------------------------------------

    def _run(self) -> dict[str, Any]:
        meta = self._metadata()
        run_metrics = self._run_metrics()
        rows = self._sorted_trajectory_rows()
        last = rows[-1] if rows else {}

        end_reason = _as_str(run_metrics.get("end_reason")) or _as_str(
            last.get("EndReason")
        )
        if end_reason is None:
            self.notes.append(
                "run.end_reason: absent in run_metrics.json and trajectory.csv"
            )

        steps = _as_int(run_metrics.get("steps"))
        if steps is None and rows:
            steps = len(rows)

        wall_clock = _as_float(run_metrics.get("elapsed_seconds"))
        if wall_clock is None:
            wall_clock = _as_float(last.get("WallTimeSinceStart"))

        return {
            "run_id": _as_str(meta.get("run_id")) or _as_str(run_metrics.get("run_id")),
            "scene": _as_int(meta.get("scene")),
            "agents": _as_int(meta.get("agent_count")),
            "seed": _as_int(meta.get("seed")),
            "model": _as_str(meta.get("model")),
            "prompt_version": _as_str(meta.get("prompt_version")),
            "code_commit": _as_str(meta.get("code_commit")),
            "end_reason": end_reason,
            "steps": steps,
            "max_steps": _as_int(run_metrics.get("max_steps"))
            or _as_int(meta.get("max_steps")),
            "finished": _as_bool(run_metrics.get("finished")),
            "wall_clock_s": _round(wall_clock, 3),
        }

    def _metrics(self) -> dict[str, Any]:
        run_metrics = self._run_metrics()
        rows = self._sorted_trajectory_rows()
        last = rows[-1] if rows else {}
        notes: list[str] = []

        transport = _as_float(run_metrics.get("transport_rate"))
        if transport is None:
            transport = _as_float(last.get("TransportRate"))
        coverage = _as_float(run_metrics.get("coverage"))
        if coverage is None:
            coverage = _as_float(last.get("Coverage"))

        l2 = self._eval_section("l2_planning")
        l4 = self._eval_section("l4_cost")
        load_balance = _as_float(l2.get("load_balance_b"))
        billed = _as_float(l4.get("effective_billed_tokens"))
        if "eval_metrics.json" in self.missing:
            notes.append(
                "eval_metrics.json missing: load_balance_b / effective_billed_tokens "
                "unavailable (they are evaluator-side values; run "
                "sar_orch/eval/run_eval_metrics.py to produce them)"
            )
        else:
            if load_balance is None:
                notes.append("eval_metrics.l2_planning.load_balance_b absent — null")
            if billed is None:
                notes.append(
                    "eval_metrics.l4_cost.effective_billed_tokens absent — null"
                )

        return {
            "transport_rate": _round(transport),
            "coverage_final": _round(coverage),
            "load_balance_b": _round(load_balance),
            "effective_billed_tokens": _round(billed, 3),
            "notes": notes,
        }

    def _stability(self) -> dict[str, Any]:
        notes: list[str] = []
        rows = self.trajectory.rows
        steps_observed = len(rows) if self.trajectory.exists else None

        agent_slots: int | None = None
        real_slots: int | None = None
        if self.trajectory.exists and self.trajectory.has("Actions"):
            agent_slots = 0
            real_slots = 0
            for row in self._sorted_trajectory_rows():
                actions = _parse_list(row.get("Actions"))
                if actions is None:
                    continue
                agent_slots += len(actions)
                real_slots += sum(1 for action in actions if not _is_noop(action))
        elif self.trajectory.exists:
            notes.append(
                "trajectory.csv: Actions column absent — agent slot counts unavailable"
            )

        timeout_source: str | None = None
        timeout_slots: int | None = None
        timeout_steps: int | None = None
        per_step = self._timeout_slots_per_step()
        if per_step is not None:
            per_step_counts, timeout_source = per_step
            timeout_slots = sum(per_step_counts.values())
            timeout_steps = sum(1 for count in per_step_counts.values() if count > 0)
        else:
            notes.append(
                "trajectory.csv: neither TimeoutAgents nor NoOpSource column present — "
                "timeout_agents_ratio unavailable"
            )

        noop_counts: dict[str, int] | None = None
        if self.trajectory.exists and self.trajectory.has("NoOpSource"):
            counter: Counter[str] = Counter()
            skipped = 0
            for row in self._sorted_trajectory_rows():
                sources = _parse_list(row.get("NoOpSource"))
                if sources is None:
                    skipped += 1
                    continue
                for value in sources:
                    key = str(value or "").strip()
                    if not key:
                        continue
                    counter[key if key in _NOOP_SOURCE_KEYS else "other"] += 1
            noop_counts = {key: counter.get(key, 0) for key in _NOOP_SOURCE_KEYS}
            noop_counts["other"] = counter.get("other", 0)
            if skipped:
                notes.append(
                    f"trajectory.csv: {skipped} NoOpSource cell(s) unparsable — skipped"
                )
        elif self.trajectory.exists:
            notes.append(
                "trajectory.csv: NoOpSource column absent (legacy run) — noop_source unavailable"
            )

        ratio = None
        if timeout_slots is not None and agent_slots:
            ratio = timeout_slots / agent_slots

        if real_slots is None and noop_counts is not None and agent_slots is not None:
            # Fallback when Actions is absent but NoOpSource is present: '' slots
            # are the non-NoOp ones.
            real_slots = max(
                agent_slots - sum(noop_counts[key] for key in _NOOP_SOURCE_KEYS), 0
            )
            notes.append(
                "stability.real_action_slots derived from NoOpSource (Actions column absent)"
            )

        return {
            "steps_observed": steps_observed,
            "agent_slots": agent_slots,
            "real_action_slots": real_slots,
            "timeout_agents_slots": timeout_slots,
            "timeout_steps": timeout_steps,
            "timeout_source": timeout_source,
            "timeout_agents_ratio": _round(ratio),
            "noop_source": noop_counts,
            "notes": notes,
        }

    def _subtasks(self) -> dict[str, Any]:
        if not self.subtasks.exists:
            return {
                "assigned": None,
                "canceled": None,
                "completed": None,
                "failed": None,
                "other": None,
                "total": None,
                "cancel_rate": None,
                "notes": ["subtasks.csv missing: subtask lifecycle counts unavailable"],
            }
        counts: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        for row in self.subtasks.rows:
            status = str(row.get("Status") or "").strip()
            if status in _SUBTASK_STATUSES:
                counts[status] += 1
            else:
                unknown[status or "(empty)"] += 1
        other = sum(unknown.values())
        total = sum(counts.values()) + other
        notes: list[str] = []
        if unknown:
            notes.append(
                "subtasks.csv: unknown status value(s) "
                f"{sorted(unknown)} counted under 'other'"
            )
        if total and counts["canceled"] == 0:
            notes.append("subtasks.csv: no terminal cancel rows")
        if counts["assigned"] and not (counts["completed"] or counts["failed"]):
            notes.append(
                "subtasks.csv: no finish_task terminal rows — 'assigned' rows without a "
                "terminal row are normal for runs whose mission is closed by the barrier "
                "checker instead of coordinator finish_task"
            )
        cancel_rate = (counts["canceled"] / total) if total else None
        return {
            "assigned": counts["assigned"],
            "canceled": counts["canceled"],
            "completed": counts["completed"],
            "failed": counts["failed"],
            "other": other,
            "total": total,
            "cancel_rate": _round(cancel_rate),
            "notes": notes,
        }

    def _tool_errors(self) -> dict[str, Any]:
        unavailable = {
            "total_calls": None,
            "failed_calls": None,
            "failure_rate": None,
            "top_k": None,
            "notes": [],
        }
        if not self.interactions.exists:
            unavailable["notes"] = [
                "agent_interactions.csv missing: tool error aggregation unavailable"
            ]
            return unavailable
        if not self.interactions.has("Success"):
            unavailable["notes"] = [
                (
                    "agent_interactions.csv: Success column absent (legacy schema) — "
                    "tool error aggregation unavailable"
                )
            ]
            return unavailable

        rows = self.interactions.rows
        failures: Counter[tuple[str, str]] = Counter()
        failed = 0
        for row in rows:
            if _as_str(row.get("Success")) != "False":
                continue
            failed += 1
            tool = _as_str(row.get("ToolName")) or "(unknown tool)"
            error = _as_str(row.get("ErrorType")) or "unclassified"
            failures[(tool, error)] += 1

        ordered = sorted(
            failures.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
        top_k = [
            {"tool": tool, "error": error, "count": count}
            for (tool, error), count in ordered[: self.tool_error_top_k]
        ]
        total = len(rows)
        notes: list[str] = []
        if failed == 0:
            notes.append("agent_interactions.csv: no failed tool call recorded")
        return {
            "total_calls": total,
            "failed_calls": failed,
            "failure_rate": _round((failed / total) if total else None),
            "top_k": top_k,
            "notes": notes,
        }

    # -- failure signatures ------------------------------------------------

    def _signature_cancel_churn(self) -> dict[str, Any]:
        cancels = self._cancel_events()
        if cancels is None:
            return {
                "kind": "cancel_churn",
                "count": None,
                "evidence": {
                    "status": "unavailable",
                    "reason": "events.ndjson missing — cancel events unmeasurable",
                },
            }
        created = self._dispatch_created_ts()
        measurable = 0
        unmatched = 0
        negatives = 0
        delays: list[tuple[float, str]] = []
        in_window: list[tuple[float, str]] = []
        for task_id, stamp, _step in cancels:
            origin = created.get(task_id)
            if stamp is None or origin is None:
                unmatched += 1
                continue
            delay = round(stamp - origin, 3)
            measurable += 1
            if delay < 0:
                negatives += 1
            delays.append((delay, task_id))
            if 0.0 <= delay <= self.churn_window_s:
                in_window.append((delay, task_id))

        evidence: dict[str, Any] = {
            "window_s": self.churn_window_s,
            "cancel_events": len(cancels),
            "measurable": measurable,
            "unmatched": unmatched,
            "in_window": len(in_window),
            "negative_delays": negatives,
            "in_window_task_ids": [task_id for _delay, task_id in sorted(in_window)][
                :_MAX_EVIDENCE_IDS
            ],
            "in_window_task_ids_truncated": len(in_window) > _MAX_EVIDENCE_IDS,
        }
        if delays:
            seconds = sorted(delay for delay, _task in delays)
            evidence["delay_s"] = {
                "min": seconds[0],
                "p50": round(statistics.median(seconds), 3),
                "max": seconds[-1],
            }
        count: int | None
        if not cancels:
            # events.ndjson is readable and holds no cancel at all: measured absent.
            count = 0
            evidence["note"] = "no cancel_task event recorded in events.ndjson"
        elif measurable == 0:
            count = None
            evidence["note"] = (
                "no cancel event matched a dispatch creation timestamp "
                "(coordinator/events_dsp_*.ndjson absent or cancels target plan nodes)"
            )
        else:
            count = len(in_window)
        return {"kind": "cancel_churn", "count": count, "evidence": evidence}

    def _signature_cancel_storm(self) -> dict[str, Any]:
        cancels = self._cancel_events()
        source = "events.ndjson"
        target_kinds: dict[str, int] | None = None
        if cancels is None:
            # Fallback: canceled subtask rows carry the cancelling step.
            if self.subtasks.exists and self.subtasks.has("Step", "Status"):
                source = "subtasks.csv"
                per_step: Counter[int] = Counter()
                for row in self.subtasks.rows:
                    if str(row.get("Status") or "").strip() != "canceled":
                        continue
                    step = _as_int(row.get("Step"))
                    if step is not None:
                        per_step[step] += 1
            else:
                return {
                    "kind": "cancel_storm",
                    "count": None,
                    "evidence": {
                        "status": "unavailable",
                        "reason": "events.ndjson and subtasks.csv both unusable — "
                        "cancel step distribution unmeasurable",
                    },
                }
        else:
            per_step = Counter()
            kinds: Counter[str] = Counter()
            for task_id, _stamp, step in cancels:
                kinds["dispatch" if task_id.startswith("dsp_") else "other"] += 1
                if step is not None:
                    per_step[step] += 1
            target_kinds = {
                "dispatch": kinds.get("dispatch", 0),
                "other": kinds.get("other", 0),
            }

        storm_steps = sorted(
            step
            for step, count in per_step.items()
            if count >= self.cancel_storm_threshold
        )
        evidence: dict[str, Any] = {
            "threshold": self.cancel_storm_threshold,
            "source": source,
            "cancel_events": sum(per_step.values()),
            "storm_steps": storm_steps,
            "max_cancels_in_step": max(per_step.values()) if per_step else 0,
        }
        if target_kinds is not None:
            evidence["target_kinds"] = target_kinds
        return {
            "kind": "cancel_storm",
            "count": len(storm_steps),
            "evidence": evidence,
        }

    def _signature_timeout_burst(self) -> dict[str, Any]:
        per_step = self._timeout_slots_per_step()
        if per_step is None:
            return {
                "kind": "timeout_burst",
                "count": None,
                "evidence": {
                    "status": "unavailable",
                    "reason": "trajectory.csv: neither TimeoutAgents nor NoOpSource "
                    "column present",
                },
            }
        counts, source = per_step
        burst_steps = sorted(
            step
            for step, count in counts.items()
            if count >= self.timeout_burst_threshold
        )
        return {
            "kind": "timeout_burst",
            "count": len(burst_steps),
            "evidence": {
                "threshold": self.timeout_burst_threshold,
                "source": source,
                "timeout_injected_slots": sum(counts.values()),
                "burst_steps": burst_steps,
                "max_timeout_agents_in_step": max(counts.values()) if counts else 0,
            },
        }

    def _signature_idle_tail(self) -> dict[str, Any]:
        per_step = self._trajectory_real_actions()
        if per_step is None:
            return {
                "kind": "idle_tail",
                "count": None,
                "evidence": {
                    "status": "unavailable",
                    "reason": "trajectory.csv: Actions column absent — real action "
                    "detection unmeasurable",
                },
            }
        run_metrics = self._run_metrics()
        finished = _as_bool(run_metrics.get("finished"))
        ordered_steps = sorted(per_step)
        if finished:
            return {
                "kind": "idle_tail",
                "count": 0,
                "evidence": {
                    "finished": True,
                    "note": "run finished — trailing NoOps are the normal auto-no-op path",
                    "tail_steps": 0,
                },
            }
        tail: list[int] = []
        for step in reversed(ordered_steps):
            if per_step[step] > 0:
                break
            tail.append(step)
        tail_steps = sorted(tail)
        evidence: dict[str, Any] = {
            "finished": finished,
            "tail_steps": len(tail_steps),
            "start_step": tail_steps[0] if tail_steps else None,
        }
        if finished is None:
            evidence["note"] = (
                "run_metrics.finished absent — tail counted without the "
                "finished-run auto-no-op exemption"
            )
        if tail_steps and self.trajectory.has("NoOpSource"):
            rows_by_step = {
                _as_int(row.get("Step")): row for row in self._sorted_trajectory_rows()
            }
            tail_counter: Counter[str] = Counter()
            for step in tail_steps:
                row = rows_by_step.get(step)
                sources = _parse_list(row.get("NoOpSource")) if row else None
                if sources is None:
                    continue
                for value in sources:
                    key = str(value or "").strip()
                    if key:
                        tail_counter[key] += 1
            evidence["tail_noop_source"] = {
                key: tail_counter.get(key, 0) for key in _NOOP_SOURCE_KEYS
            }
        return {"kind": "idle_tail", "count": len(tail_steps), "evidence": evidence}

    def _signature_agent_never_active(self) -> dict[str, Any]:
        if not self.trajectory.exists or not self.trajectory.has("Actions"):
            return {
                "kind": "agent_never_active",
                "count": None,
                "evidence": {
                    "status": "unavailable",
                    "reason": "trajectory.csv: Actions column absent — per-agent "
                    "activity unmeasurable",
                },
            }
        slots = 0
        per_agent: list[bool] = []
        for row in self._sorted_trajectory_rows():
            actions = _parse_list(row.get("Actions"))
            if actions is None:
                continue
            while len(per_agent) < len(actions):
                per_agent.append(False)
            slots = max(slots, len(actions))
            for index, action in enumerate(actions):
                if not _is_noop(action):
                    per_agent[index] = True
        never = [index for index, active in enumerate(per_agent) if not active]
        names, names_source = self._agent_names(slots)
        return {
            "kind": "agent_never_active",
            "count": len(never),
            "evidence": {
                "agents": slots,
                "never_active_indices": never,
                "never_active_names": (
                    [names[index] for index in never] if names is not None else None
                ),
                "names_source": names_source,
            },
        }

    def _failure_signatures(self) -> list[dict[str, Any]]:
        return [
            self._signature_cancel_churn(),
            self._signature_cancel_storm(),
            self._signature_timeout_burst(),
            self._signature_idle_tail(),
            self._signature_agent_never_active(),
        ]

    # -- assembly ----------------------------------------------------------

    def build(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generator": GENERATOR_VERSION,
            "truth_read": False,
            "run": self._run(),
            "metrics": self._metrics(),
            "stability": self._stability(),
            "subtasks": self._subtasks(),
            "tool_errors": self._tool_errors(),
            "failure_signatures": self._failure_signatures(),
            "missing_artifacts": sorted(self.missing),
            "notes": list(self.notes),
        }
        return payload


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def build_report(
    results_dir: str | Path,
    *,
    churn_window_s: float = DEFAULT_CHURN_WINDOW_S,
    cancel_storm_threshold: int = DEFAULT_CANCEL_STORM_THRESHOLD,
    timeout_burst_threshold: int = DEFAULT_TIMEOUT_BURST_THRESHOLD,
    tool_error_top_k: int = DEFAULT_TOOL_ERROR_TOP_K,
) -> dict[str, Any]:
    """Build the analysis report payload for one run directory."""
    return _Report(
        results_dir,
        churn_window_s=churn_window_s,
        cancel_storm_threshold=cancel_storm_threshold,
        timeout_burst_threshold=timeout_burst_threshold,
        tool_error_top_k=tool_error_top_k,
    ).build()


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


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reef-C1 analysis report generator: one analysis_report.json per SAR run "
            "(read-only; never reads the truth dir)"
        )
    )
    parser.add_argument("--results-dir", required=True, help="Run results directory")
    parser.add_argument(
        "--output",
        default=None,
        help=f"Output path (default: <results_dir>/{DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--top-k",
        type=_non_negative_int,
        default=DEFAULT_TOOL_ERROR_TOP_K,
        help=f"Tool-error top-k entries (default: {DEFAULT_TOOL_ERROR_TOP_K})",
    )
    parser.add_argument(
        "--churn-window-s",
        type=_positive_float,
        default=DEFAULT_CHURN_WINDOW_S,
        help=(
            "cancel_churn window: cancels within this many seconds of the dispatch "
            f"creation timestamp (default: {DEFAULT_CHURN_WINDOW_S:g})"
        ),
    )
    parser.add_argument(
        "--cancel-storm-threshold",
        type=_positive_int,
        default=DEFAULT_CANCEL_STORM_THRESHOLD,
        help=(
            "cancel_storm threshold: distinct cancel_task events in one step "
            f"(default: {DEFAULT_CANCEL_STORM_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--timeout-burst-threshold",
        type=_positive_int,
        default=DEFAULT_TIMEOUT_BURST_THRESHOLD,
        help=(
            "timeout_burst threshold: timeout-injected agents in one step "
            f"(default: {DEFAULT_TIMEOUT_BURST_THRESHOLD})"
        ),
    )
    return parser.parse_args(argv)


def _print_summary(payload: dict[str, Any], output: Path) -> None:
    run = payload["run"]
    metrics = payload["metrics"]
    stability = payload["stability"]
    subtasks = payload["subtasks"]
    tool_errors = payload["tool_errors"]
    print(f"run_analysis_report: wrote {output}")
    print(
        "  run: scene={scene} agents={agents} seed={seed} end_reason={end_reason} "
        "steps={steps} wall_clock_s={wall}".format(
            scene=run.get("scene"),
            agents=run.get("agents"),
            seed=run.get("seed"),
            end_reason=run.get("end_reason"),
            steps=run.get("steps"),
            wall=run.get("wall_clock_s"),
        )
    )
    print(
        "  metrics: transport_rate={transport} coverage_final={coverage} "
        "load_balance_b={balance} effective_billed_tokens={billed}".format(
            transport=metrics.get("transport_rate"),
            coverage=metrics.get("coverage_final"),
            balance=metrics.get("load_balance_b"),
            billed=metrics.get("effective_billed_tokens"),
        )
    )
    noop = stability.get("noop_source") or {}
    print(
        "  stability: timeout_ratio={ratio} noop(llm/idle/timeout/other)="
        "{llm}/{idle}/{timeout}/{other}".format(
            ratio=stability.get("timeout_agents_ratio"),
            llm=noop.get("llm"),
            idle=noop.get("idle_heartbeat"),
            timeout=noop.get("timeout_injected"),
            other=noop.get("other"),
        )
    )
    print(
        "  subtasks: assigned={assigned} canceled={canceled} completed={completed} "
        "failed={failed} cancel_rate={rate}".format(
            assigned=subtasks.get("assigned"),
            canceled=subtasks.get("canceled"),
            completed=subtasks.get("completed"),
            failed=subtasks.get("failed"),
            rate=subtasks.get("cancel_rate"),
        )
    )
    print(
        "  tool_errors: failed={failed}/{total} top_k={top}".format(
            failed=tool_errors.get("failed_calls"),
            total=tool_errors.get("total_calls"),
            top=(tool_errors.get("top_k") or [])[:3],
        )
    )
    signatures = ", ".join(
        f"{entry['kind']}={entry['count']}" for entry in payload["failure_signatures"]
    )
    print(f"  failure_signatures: {signatures}")
    if payload["missing_artifacts"]:
        print(f"  missing_artifacts: {payload['missing_artifacts']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(
            f"run_analysis_report: results dir not found: {results_dir}",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT
    if not any((results_dir / name).is_file() for name in _CORE_ARTIFACTS):
        print(
            "run_analysis_report: no run artifacts found under "
            f"{results_dir} (expected one of {list(_CORE_ARTIFACTS)})",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT
    payload = build_report(
        results_dir,
        churn_window_s=args.churn_window_s,
        cancel_storm_threshold=args.cancel_storm_threshold,
        timeout_burst_threshold=args.timeout_burst_threshold,
        tool_error_top_k=args.top_k,
    )
    output = write_artifact(results_dir, payload, args.output)
    _print_summary(payload, output)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
