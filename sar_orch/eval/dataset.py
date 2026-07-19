import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AgentInteraction:
    step: int
    agent: str
    tool_name: str
    tool_args: str
    action: str
    observation: str
    llm_input: str
    llm_output: str
    thinking: str
    error_type: str
    tool_latency_ms: str

    inventory: Optional[dict] = None
    position: Optional[tuple[int, int, int]] = None


@dataclass
class Dispatch:
    step: int
    subtask: str
    assigned_to: str
    correlation_id: str
    worker_task_id: str
    event_type: str


@dataclass
class SubtaskRecord:
    step: int
    subtask_id: str
    status: str
    assigned_to: str
    subtask: str
    failure_class: str


@dataclass
class StepRecord:
    step: int
    actions: list[str]
    successes: list[bool]
    timeout_agents: list[int]
    coverage: float
    transport_rate: float
    finished: bool
    end_reason: str
    completed_subtasks_delta: list[str]
    interactions: list[AgentInteraction] = field(default_factory=list)
    dispatches: list[Dispatch] = field(default_factory=list)
    subtasks: list[SubtaskRecord] = field(default_factory=list)


@dataclass
class EpisodeDataset:
    run_dir: Path
    metadata: dict
    steps: dict[int, StepRecord]
    summary: dict[str, str | float | int]
    token_usage_rows: list[dict]
    semantic_map_log: list[dict]
    map_summaries: list[dict]
    agent_names: list[str]
    subtask_records: list[SubtaskRecord]
    dispatches: list[Dispatch]

    grader_skips: list[dict] = field(default_factory=list)

    def get_step(self, step: int) -> Optional[StepRecord]:
        return self.steps.get(step)

    @property
    def last_step(self) -> Optional[StepRecord]:
        if not self.steps:
            return None
        return self.steps[max(self.steps.keys())]


_INVENTORY_RE = re.compile(r"I am holding (\{.*?\})")
_POSITION_RE = re.compile(r"co-ordinates:\s*(\([^)]+\))")


def _parse_observation(
    text: str,
) -> tuple[Optional[dict], Optional[tuple[int, int, int]]]:
    inventory = None
    position = None

    m = _INVENTORY_RE.search(text)
    if m:
        try:
            inventory = json.loads(m.group(1).replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            pass

    m = _POSITION_RE.search(text)
    if m:
        try:
            pos = tuple(int(x.strip()) for x in m.group(1).strip("()").split(","))
            if len(pos) == 3:
                position = pos
        except (ValueError, TypeError):
            pass

    return inventory, position


def _load_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def _parse_csv_list(value: str):
    if not value or value == "[]":
        return []
    try:
        return json.loads(value.replace("'", '"'))
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import ast

        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, SyntaxError):
        return []


def load_episode(run_dir: str | Path) -> EpisodeDataset:
    run_dir = Path(run_dir)
    grader_skips = []

    metadata_path = run_dir / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        grader_skips.append({"grader": "dataset", "reason": "metadata.json missing"})

    agent_names_raw = metadata.get("agent_names", [])
    if not agent_names_raw:
        agent_names_raw = ["Alice", "Bob", "Charlie", "David"]

    traj_rows = _load_csv(run_dir / "trajectory.csv")
    router_rows = _load_csv(run_dir / "router_interactions.csv")
    subtask_rows = _load_csv(run_dir / "subtasks.csv")
    agent_rows = _load_csv(run_dir / "agent_interactions.csv")

    summary_rows = _load_csv(run_dir / "summary.csv")
    summary = {}
    if summary_rows:
        summary = dict(summary_rows[0])

    token_usage_rows = _load_csv(run_dir / "token_usage.csv")

    semantic_map_log = _load_jsonl(run_dir / "semantic_map.jsonl")
    if not semantic_map_log:
        grader_skips.append(
            {"grader": "dataset", "reason": "semantic_map.jsonl missing or empty"}
        )

    map_summaries = _load_jsonl(run_dir / "map_summary.jsonl")
    if not map_summaries:
        grader_skips.append(
            {"grader": "dataset", "reason": "map_summary.jsonl missing or empty"}
        )

    supervision_dir = run_dir / "supervision"
    if not supervision_dir.exists() or not any(supervision_dir.iterdir()):
        grader_skips.append(
            {"grader": "dataset", "reason": "supervision/ missing or empty"}
        )

    subtask_records: list[SubtaskRecord] = []
    for row in subtask_rows:
        raw_step = int(row.get("Step", 0))
        subtask_records.append(
            SubtaskRecord(
                step=raw_step + 1,
                subtask_id=row.get("SubtaskID", ""),
                status=row.get("Status", ""),
                assigned_to=row.get("AssignedTo", ""),
                subtask=row.get("Subtask", ""),
                failure_class=row.get("FailureClass", ""),
            )
        )

    dispatches: list[Dispatch] = []
    for row in router_rows:
        raw_step = int(row.get("Step", 0))
        dispatches.append(
            Dispatch(
                step=raw_step + 1,
                subtask=row.get("Subtask", ""),
                assigned_to=row.get("AssignedTo", ""),
                correlation_id=row.get("CorrelationID", ""),
                worker_task_id=row.get("WorkerTaskID", ""),
                event_type=row.get("EventType", ""),
            )
        )

    dispatches_by_step: dict[int, list[Dispatch]] = {}
    for d in dispatches:
        dispatches_by_step.setdefault(d.step, []).append(d)

    subtasks_by_step: dict[int, list[SubtaskRecord]] = {}
    for s in subtask_records:
        subtasks_by_step.setdefault(s.step, []).append(s)

    agent_interactions_by_step: dict[int, list[AgentInteraction]] = {}
    for row in agent_rows:
        step = int(row.get("Step", 0))
        obs_text = row.get("Observation", "")
        inv, pos = _parse_observation(obs_text)
        ai = AgentInteraction(
            step=step,
            agent=row.get("Agent", ""),
            tool_name=row.get("ToolName", ""),
            tool_args=row.get("ToolArgs", ""),
            action=row.get("Action", ""),
            observation=obs_text,
            llm_input=row.get("LLMInput", ""),
            llm_output=row.get("LLMOutput", ""),
            thinking=row.get("Thinking", ""),
            error_type=row.get("ErrorType", ""),
            tool_latency_ms=row.get("ToolLatencyMs", ""),
            inventory=inv,
            position=pos,
        )
        agent_interactions_by_step.setdefault(step, []).append(ai)

    steps: dict[int, StepRecord] = {}
    for row in traj_rows:
        step = int(row.get("Step", 0))
        actions = _parse_csv_list(row.get("Actions", "[]"))
        successes = _parse_csv_list(row.get("Successes", "[]"))
        successes_bool = [bool(s) for s in successes]
        timeout_agents = _parse_csv_list(row.get("TimeoutAgents", "[]"))
        coverage = float(row.get("Coverage", 0))
        transport_rate = float(row.get("TransportRate", 0))
        finished = row.get("Finished", "False").strip().lower() == "true"
        end_reason = row.get("EndReason", "")
        cst_delta = _parse_csv_list(row.get("CompletedSubtasksDelta", "[]"))

        sr = StepRecord(
            step=step,
            actions=actions,
            successes=successes_bool,
            timeout_agents=timeout_agents,
            coverage=coverage,
            transport_rate=transport_rate,
            finished=finished,
            end_reason=end_reason,
            completed_subtasks_delta=cst_delta,
            interactions=agent_interactions_by_step.get(step, []),
            dispatches=dispatches_by_step.get(step, []),
            subtasks=subtasks_by_step.get(step, []),
        )
        steps[step] = sr

    return EpisodeDataset(
        run_dir=run_dir,
        metadata=metadata,
        steps=steps,
        summary=summary,
        token_usage_rows=token_usage_rows,
        semantic_map_log=semantic_map_log,
        map_summaries=map_summaries,
        agent_names=agent_names_raw,
        subtask_records=subtask_records,
        dispatches=dispatches,
        grader_skips=grader_skips,
    )
