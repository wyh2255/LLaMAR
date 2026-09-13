import ast
import csv
import json
import re
from pathlib import Path

from render_sar_report.models import (
    AgentTokenSummary,
    CoordinatorEvent,
    LLMTraceEvent,
    ReportData,
    RouterEvent,
    RunMeta,
    RunMetrics,
    SemanticObject,
    StepRecord,
    Subtask,
    TokenRecord,
)

_POSITION_RE = re.compile(
    r"I am at co-ordinates:\s*\(([-\d]+),\s*([-\d]+),\s*([-\d]+)\)"
)
_INVENTORY_RE = re.compile(r"I am holding\s+(\{[^}]+\})")


def _safe_literal_eval(value: str):
    try:
        return ast.literal_eval(value)
    except Exception:
        return value


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): (v or "") for k, v in row.items()})
    return rows


def _read_ndjson(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# Frozen `semantic_map.jsonl` compatibility contract (plan §7.3 / Phase 5).
# Every line is `{ts, event_type, observation, object}` where `observation`
# carries reporter/step/object_type/name/position/attributes/confidence/
# source_task_id/note and `object` is the Spatial projection after that event.
# Both the legacy SemanticMapStore writer and the canonical Memory exporter
# materialize this schema; the render consumer must read both.
FROZEN_SEMANTIC_MAP_TOP_KEYS = ("ts", "event_type", "observation", "object")
# The legacy SemanticMapStore writes agent observations with a 4th top-level
# key ``object_type`` instead of ``object`` (store.py ``_append_jsonl_locked``
# for ``object_type == "agent"``).  The render consumer tolerates both; the
# frozen canonical exporter always emits ``object``.
LEGACY_AGENT_TOP_KEYS = ("ts", "event_type", "observation", "object_type")
FROZEN_OBSERVATION_KEYS = (
    "reporter",
    "step",
    "object_type",
    "name",
    "position",
    "attributes",
    "confidence",
    "source_task_id",
    "note",
)
FROZEN_OBJECT_KEYS = (
    "object_type",
    "name",
    "position",
    "attributes",
    "status",
    "last_seen_step",
    "last_seen_ts",
    "sources",
    "confidence",
    "conflict",
    "conflicts",
    "field_last_seen_steps",
)


def verify_semantic_map_fixture(path: Path) -> dict:
    """Verify a ``semantic_map.jsonl`` file against the frozen field/order.

    Checks that every ``observation_ingested`` line follows the frozen
    top-level key order and that ``observation`` carries all frozen fields with
    the canonical key order.  ``ts`` may be a float (legacy writer) or an ISO
    string (canonical exporter); the render consumer never parses it.

    Returns a summary dict with ``record_count`` and ``violations``; raises
    nothing — callers decide whether a violation is fatal.
    """
    records = _read_ndjson(path)
    violations: list[str] = []
    for index, rec in enumerate(records, start=1):
        if rec.get("event_type") != "observation_ingested":
            continue
        top_keys = tuple(rec.keys())
        valid_top = (
            top_keys[: len(FROZEN_SEMANTIC_MAP_TOP_KEYS)] == FROZEN_SEMANTIC_MAP_TOP_KEYS
        ) or (
            top_keys[: len(LEGACY_AGENT_TOP_KEYS)] == LEGACY_AGENT_TOP_KEYS
        )
        if not valid_top:
            violations.append(f"line {index}: top-level key order mismatch: {top_keys}")
            continue
        obs = rec.get("observation") or {}
        obs_keys = tuple(obs.keys())
        if obs_keys != FROZEN_OBSERVATION_KEYS:
            violations.append(
                f"line {index}: observation key order mismatch: {obs_keys}"
            )
        for required in ("name", "object_type"):
            if not obs.get(required):
                violations.append(f"line {index}: observation missing {required!r}")
    return {"record_count": len(records), "violations": violations}


def _extract_position(text: str) -> tuple | None:
    m = _POSITION_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _extract_inventory(text: str) -> dict:
    m = _INVENTORY_RE.search(text)
    if m:
        try:
            return ast.literal_eval(m.group(1))
        except Exception:
            pass
    return {}


def _agent_names_from_summary(summary: dict) -> list[str]:
    # Summary header includes metrics then per-agent token columns like AlicePromptTokens
    names = []
    for key in summary.keys():
        if key.endswith("PromptTokens"):
            name = key[: -len("PromptTokens")]
            if name and name not in names:
                names.append(name)
    return names


def load_run_meta(results_dir: Path) -> RunMeta | None:
    path = results_dir / "metadata.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return RunMeta(
        run_id=data.get("run_id", ""),
        scene=data.get("scene", 0),
        agent_count=data.get("agent_count", 0),
        model=data.get("model", ""),
        seed=data.get("seed", 0),
        state_mode=data.get("state_mode", ""),
        success_criteria=data.get("success_criteria", ""),
        max_steps=data.get("max_steps", 0),
    )


def load_run_metrics(results_dir: Path) -> RunMetrics | None:
    path = results_dir / "run_metrics.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return RunMetrics(
        coverage=float(data.get("coverage", 0.0)),
        transport_rate=float(data.get("transport_rate", 0.0)),
        steps=int(data.get("steps", 0)),
        finished=bool(data.get("finished", False)),
        elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
        end_reason=str(data.get("end_reason", "")),
    )


def load_summary(results_dir: Path) -> tuple[dict, list[AgentTokenSummary]]:
    path = results_dir / "summary.csv"
    if not path.exists():
        return {}, []
    rows = _read_csv(path)
    if not rows:
        return {}, []
    summary = rows[0]
    agents = _agent_names_from_summary(summary)
    tokens = []
    for agent in agents:
        tokens.append(
            AgentTokenSummary(
                agent=agent,
                prompt=int(summary.get(f"{agent}PromptTokens", 0) or 0),
                completion=int(summary.get(f"{agent}CompletionTokens", 0) or 0),
                total=int(summary.get(f"{agent}TotalTokens", 0) or 0),
                cache_hit=int(summary.get(f"{agent}CacheHitTokens", 0) or 0),
                cache_miss=int(summary.get(f"{agent}CacheMissTokens", 0) or 0),
            )
        )
    return summary, tokens


def load_trajectory(results_dir: Path, agent_names: list[str]) -> list[StepRecord]:
    path = results_dir / "trajectory.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    records: list[StepRecord] = []
    for row in rows:
        try:
            step = int(row["Step"])
        except (KeyError, ValueError):
            continue
        actions = _safe_literal_eval(row.get("Actions", "[]"))
        successes = _safe_literal_eval(row.get("Successes", "[]"))
        observations = _safe_literal_eval(row.get("Observations", "[]"))
        if not isinstance(actions, list):
            actions = []
        if not isinstance(successes, list):
            successes = []
        if not isinstance(observations, list):
            observations = []
        record = StepRecord(step=step)
        for idx, name in enumerate(agent_names):
            record.actions[name] = actions[idx] if idx < len(actions) else ""
            record.successes[name] = successes[idx] if idx < len(successes) else False
            obs = observations[idx] if idx < len(observations) else ""
            record.observations[name] = obs
            record.positions[name] = _extract_position(obs)
            record.inventories[name] = _extract_inventory(obs)
        record.coverage = float(row.get("Coverage", 0) or 0)
        record.transport_rate = float(row.get("TransportRate", 0) or 0)
        record.timeout_agents = _safe_literal_eval(row.get("TimeoutAgents", "[]"))
        if not isinstance(record.timeout_agents, list):
            record.timeout_agents = []
        try:
            record.completed_subtasks_delta = int(
                row.get("CompletedSubtasksDelta", 0) or 0
            )
        except ValueError:
            record.completed_subtasks_delta = 0
        records.append(record)
    return records


def load_token_usage(results_dir: Path) -> list[TokenRecord]:
    path = results_dir / "token_usage.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    records = []
    for row in rows:
        try:
            records.append(
                TokenRecord(
                    step=int(row["Step"]),
                    agent=row["Agent"],
                    prompt=int(row.get("PromptTokens", 0) or 0),
                    completion=int(row.get("CompletionTokens", 0) or 0),
                    total=int(row.get("TotalTokens", 0) or 0),
                    cache_hit=int(row.get("CacheHitTokens", 0) or 0),
                    cache_miss=int(row.get("CacheMissTokens", 0) or 0),
                )
            )
        except (KeyError, ValueError):
            continue
    return records


def load_subtasks(results_dir: Path) -> list[Subtask]:
    path = results_dir / "subtasks.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    subtasks = []
    for row in rows:
        try:
            subtasks.append(
                Subtask(
                    subtask_id=row.get("SubtaskID", ""),
                    step=int(row.get("Step", 0) or 0),
                    status=row.get("Status", ""),
                    assigned_to=row.get("AssignedTo", ""),
                    text=row.get("Subtask", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return subtasks


def load_router(results_dir: Path) -> list[RouterEvent]:
    path = results_dir / "router_interactions.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    events = []
    for row in rows:
        try:
            events.append(
                RouterEvent(
                    step=int(row.get("Step", 0) or 0),
                    subtask=row.get("Subtask", ""),
                    assigned_to=row.get("AssignedTo", ""),
                    event_type=row.get("EventType", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return events


def load_coordinator_events(results_dir: Path) -> list[CoordinatorEvent]:
    path = results_dir / "events.ndjson"
    records = _read_ndjson(path)
    events = []
    for rec in records:
        events.append(
            CoordinatorEvent(
                step=int(rec.get("step", 0)),
                event_type=str(rec.get("event_type", "")),
                agent=str(rec.get("agent", "")),
                payload=rec.get("payload", {}),
            )
        )
    return events


def load_semantic_map(results_dir: Path) -> list[SemanticObject]:
    """Load semantic_map.jsonl from the unified experiment results directory."""
    path = results_dir / "semantic_map.jsonl"
    records = _read_ndjson(path)
    objects: dict[str, SemanticObject] = {}
    for rec in records:
        if rec.get("event_type") != "observation_ingested":
            continue
        obs = rec.get("observation", {})
        obj = rec.get("object", {})
        name = obs.get("name") or obj.get("name")
        obj_type = obs.get("object_type") or obj.get("object_type")
        if not name:
            continue
        if name not in objects:
            objects[name] = SemanticObject(
                object_type=obj_type or "unknown",
                name=name,
            )
        objects[name].observations.append(obs)
        if obj.get("conflict") or rec.get("conflict"):
            objects[name].conflict = True
    return list(objects.values())


def load_worker_logs(results_dir: Path) -> dict[tuple[str, str], list[LLMTraceEvent]]:
    """Load worker LLM trace NDJSON files from ``results_dir/workers/<AgentName>/**/*.ndjson``."""
    base = results_dir / "workers"
    traces: dict[tuple[str, str], list[LLMTraceEvent]] = {}
    if not base.exists():
        return traces
    for file in base.rglob("*.ndjson"):
        agent = file.relative_to(base).parent.name
        task_id = file.stem
        records = _read_ndjson(file)
        key = (agent, task_id)
        traces[key] = []
        for rec in records:
            event_type = rec.get("event", "")
            event = LLMTraceEvent(
                task_id=task_id,
                agent=agent,
                ts=str(rec.get("ts", "")),
                event=event_type,
                content=str(rec.get("content", "")),
                tool_calls=rec.get("tool_calls", []),
                usage=rec.get("usage", {}),
                tool_name=str(rec.get("tool_name", "")),
                arguments=rec.get("arguments", {}),
                result=str(rec.get("result", "")),
                error=str(rec.get("error", "")),
            )
            traces[key].append(event)
    return traces


def load_report_data(results_dir: Path, logs_dir: Path) -> ReportData:
    data = ReportData()
    data.meta = load_run_meta(results_dir)
    data.metrics = load_run_metrics(results_dir)
    data.summary, data.agent_tokens = load_summary(results_dir)

    agent_names = _agent_names_from_summary(data.summary)
    if data.meta and not agent_names:
        agent_names = [f"Agent{i}" for i in range(1, data.meta.agent_count + 1)]

    data.steps = load_trajectory(results_dir, agent_names)
    data.tokens = load_token_usage(results_dir)
    data.subtasks = load_subtasks(results_dir)
    data.router_events = load_router(results_dir)
    data.coordinator_events = load_coordinator_events(results_dir)
    data.semantic_objects = load_semantic_map(results_dir)
    data.llm_traces = load_worker_logs(results_dir)

    if data.meta is None:
        data.warnings.append(f"metadata.json not found in {results_dir}")
    if data.metrics is None:
        data.warnings.append(f"run_metrics.json not found in {results_dir}")
    if not data.steps:
        data.warnings.append(f"trajectory.csv empty or missing in {results_dir}")
    return data
