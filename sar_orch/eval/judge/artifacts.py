"""Read-only run-artifact readers and bounded digests for the judge tasks.

The judge input contract (card decision #2): agent behaviour comes ONLY from
the uuid/task-named AgentLogger ndjson files under ``workers/<Agent>/<Agent>/``
(``llm_request.messages`` carries the full input, ``llm_response`` the
decision).  Timestamp-named TaskLogger files and ``unknown.ndjson`` are never
read (empty ``messages`` would produce false negatives — proposer_log_guide.md
§3.6).

Digests are bounded on purpose: the full Environment State block averages
~14.9k chars per round (73% of it is the framework event feed under
``### Relevant Recent Events``), so the observation digest keeps only the
sections that carry the facts a decision can contradict and documents every
truncation to the judge.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

#: Marker appended whenever a digest had to truncate its source text.
TRUNCATION_MARKER = "[truncated]"

#: Per-message / per-section character caps for the observation digest.
TASK_INSTRUCTION_CAP = 1200
TOOL_OBSERVATION_CAP = 2000
TOOL_OBSERVATIONS_KEPT = 3  # trailing tool results kept (multi-call rounds)
RESPONSE_CONTENT_CAP = 1500
TOOL_ARGUMENT_CAP = 200
ENV_SECTION_CAPS = {
    "Spatial State": 6000,
    "Embodied State": 1500,
    "Task Execution State": 1000,
    "fallback": 8000,
}
#: Env-state sections deliberately excluded from the digest (framework noise).
ENV_SECTIONS_EXCLUDED = ("Relevant Recent Events", "Freshness / Conflicts / Evidence")

ROUTER_TIMELINE_BUDGET = 40000
SUBTASK_LOG_BUDGET = 15000

#: Worker tool names are taken from the live registry (sar_orch/tools/worker).
#: Action decisions that interact with the environment (or deliberately do
#: not, e.g. ``no_op``) — only rounds whose tool calls include one of these
#: are observation_ignore candidates.
ACTION_TOOLS = frozenset(
    {
        "navigate_to",
        "use_supply",
        "get_supply",
        "store_supply",
        "carry_person",
        "drop_off_person",
        "clear_inventory",
        "move",
        "explore",
        "no_op",
        "finish_task",
    }
)
#: Known meta / informational tools — reporting hallucination is measured by
#: memory_projection_quality (worker_report_quality), not here.
NON_ACTION_TOOLS = frozenset(
    {"get_agent_state", "report_observation", "query_shared_memory", "get_skill"}
)


def _clip(text: str, cap: int) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n{TRUNCATION_MARKER}"


def _clip_head_tail(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = int(cap * 0.6)
    tail = cap - head
    return text[:head] + f"\n{TRUNCATION_MARKER} ...\n" + text[-tail:]


def load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON object; return None when missing or unreadable."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_csv_rows(path: Path) -> list[dict[str, str]] | None:
    """Read a CSV into a list of dicts; return None when missing/unreadable."""
    if not path.is_file():
        return None
    import csv

    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, ValueError, csv.Error):
        return None


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    """Read an ndjson file into a list of dict events (bad lines skipped)."""
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


# --------------------------------------------------------------------------
# planning_path inputs
# --------------------------------------------------------------------------


def _position_text(position: Any) -> str:
    if isinstance(position, dict):
        return "({x},{y})".format(
            x=position.get("x"), y=position.get("y")
        )
    if isinstance(position, (list, tuple)) and len(position) >= 2:
        return f"({position[0]},{position[1]})"
    return "?"


def build_scene_digest(scene_config: dict[str, Any]) -> str:
    """Render the initial scene layout as a compact judge-readable digest."""
    objects = scene_config.get("objects")
    objects = objects if isinstance(objects, dict) else {}
    lines: list[str] = []
    grid = scene_config.get("grid")
    if isinstance(grid, dict):
        lines.append(
            "Grid: {w} x {h} (altitude {z}), {n} agents, seed {seed}".format(
                w=grid.get("width"),
                h=grid.get("height"),
                z=grid.get("altitude"),
                n=scene_config.get("num_agents"),
                seed=scene_config.get("seed"),
            )
        )
    agents = objects.get("agents") or []
    if agents:
        lines.append("Agents (start positions):")
        for agent in agents:
            lines.append(
                f"- {agent.get('name')} @ {_position_text(agent.get('position'))}"
            )
    fires = objects.get("fires") or []
    if fires:
        lines.append("Fires (all regions of a fire must be extinguished):")
        region_index: dict[str, list[dict[str, Any]]] = {}
        for flammable in objects.get("flammables") or []:
            name = flammable.get("name")
            if isinstance(name, str) and name:
                region_index.setdefault(str(flammable.get("parent_fire")), []).append(
                    flammable
                )
        for fire in fires:
            lines.append(
                "- {name}: {ftype} fire @ {pos}, initial average intensity {intensity},"
                " regions: {n}".format(
                    name=fire.get("name"),
                    ftype=fire.get("fire_type"),
                    pos=_position_text(fire.get("position")),
                    intensity=fire.get("average_intensity"),
                    n=len(fire.get("flammable_ids") or []),
                )
            )
            regions = region_index.get(str(fire.get("name")), [])[:20]
            for region in regions:
                lines.append(
                    f"  - {region.get('name')} @ {_position_text(region.get('position'))}"
                )
    reservoirs = objects.get("reservoirs") or []
    if reservoirs:
        lines.append("Reservoirs (infinite supply):")
        for reservoir in reservoirs:
            lines.append(
                "- {name}: {rtype} @ {pos}".format(
                    name=reservoir.get("name"),
                    rtype=reservoir.get("resource_type"),
                    pos=_position_text(reservoir.get("position")),
                )
            )
    for deposit in objects.get("deposits") or []:
        lines.append(
            f"Deposit: {deposit.get('name')} @ {_position_text(deposit.get('position'))}"
        )
    for person in objects.get("persons") or []:
        lines.append(
            "- Trapped person {name} @ {pos} (needs {load} robots to carry,"
            " delivered to the deposit)".format(
                name=person.get("name"),
                pos=_position_text(person.get("position")),
                load=person.get("load"),
            )
        )
    return "\n".join(lines)


def _budgeted_lines(lines: Sequence[str], budget: int) -> tuple[list[str], bool]:
    """Keep all lines when they fit the budget, else decimate uniformly."""
    total = sum(len(line) + 1 for line in lines)
    if total <= budget or not lines:
        return list(lines), False
    keep_every = max(2, math.ceil(total / budget))
    kept = [
        line
        for index, line in enumerate(lines)
        if index % keep_every == 0 or index == len(lines) - 1
    ]
    return kept, True


def render_router_timeline(
    rows: Sequence[dict[str, str]], *, budget: int = ROUTER_TIMELINE_BUDGET
) -> tuple[str, bool]:
    """Render router_interactions.csv rows as a step-ordered timeline."""
    lines = [
        "step={step} | {event} | to={who} | ok={ok} | err={err} | {sub}".format(
            step=row.get("Step", ""),
            event=row.get("EventType", ""),
            who=row.get("AssignedTo", ""),
            ok=row.get("Success", "") or "-",
            err=row.get("ErrorType", "") or "-",
            sub=str(row.get("Subtask", ""))[:110],
        )
        for row in rows
    ]
    kept, truncated = _budgeted_lines(lines, budget)
    return "\n".join(kept), truncated


def render_subtask_log(
    rows: Sequence[dict[str, str]], *, budget: int = SUBTASK_LOG_BUDGET
) -> tuple[str, bool]:
    """Render subtasks.csv lifecycle rows."""
    lines = [
        "step={step} | {sid} | {status} | to={who} | fail={fail} | {sub} | {details}".format(
            step=row.get("Step", ""),
            sid=str(row.get("SubtaskID", ""))[:40],
            status=row.get("Status", ""),
            who=row.get("AssignedTo", ""),
            fail=row.get("FailureClass", "") or "-",
            sub=str(row.get("Subtask", ""))[:110],
            details=str(row.get("Details", ""))[:80],
        )
        for row in rows
    ]
    kept, truncated = _budgeted_lines(lines, budget)
    return "\n".join(kept), truncated


# --------------------------------------------------------------------------
# observation_ignore inputs
# --------------------------------------------------------------------------


@dataclass
class WorkerRound:
    """One worker llm_request -> llm_response decision round."""

    agent: str
    task_id: str
    file: str  # path relative to the run dir
    step: int
    request_ts: str | None
    messages: list[dict[str, Any]]
    response_status: str | None
    response_content: str
    tool_calls: list[dict[str, Any]]
    request_index: int

    def tool_names(self) -> list[str]:
        names: list[str] = []
        for call in self.tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
        return names


def iter_worker_rounds(workers_dir: Path) -> Iterator[WorkerRound]:
    """Yield every worker decision round from the uuid-name AgentLogger files."""
    if not workers_dir.is_dir():
        return
    for agent_entry in sorted(workers_dir.iterdir()):
        if not agent_entry.is_dir():
            continue
        agent = agent_entry.name
        files = sorted(agent_entry.glob("*.ndjson"))
        files += sorted((agent_entry / agent).glob("*.ndjson"))
        for file_path in files:
            if file_path.name == "unknown.ndjson":
                continue
            yield from _rounds_from_file(file_path, agent=agent, workers_dir=workers_dir)


def _rounds_from_file(
    file_path: Path, *, agent: str, workers_dir: Path
) -> Iterator[WorkerRound]:
    relative = str(file_path.relative_to(workers_dir.parent))
    pending: dict[str, Any] | None = None
    index = 0
    for event in read_ndjson(file_path):
        kind = event.get("event")
        if kind == "llm_request":
            pending = event
            continue
        if kind != "llm_response" or pending is None:
            continue
        request = pending
        pending = None
        index += 1
        step = request.get("step_index")
        if step is None:
            step = event.get("step_index")
        yield WorkerRound(
            agent=agent,
            task_id=str(request.get("task_id") or file_path.stem),
            file=relative,
            step=int(step) if isinstance(step, int) else -1,
            request_ts=request.get("ts"),
            messages=[
                message
                for message in request.get("messages") or []
                if isinstance(message, dict)
            ],
            response_status=(
                str(event.get("status")) if event.get("status") else None
            ),
            response_content=str(event.get("content") or ""),
            tool_calls=[
                call
                for call in event.get("tool_calls") or []
                if isinstance(call, dict)
            ],
            request_index=index,
        )


def is_action_round(round_: WorkerRound) -> bool:
    """True when the round's decision includes an environment action tool."""
    names = round_.tool_names()
    if not names:
        return False
    for name in names:
        if name in NON_ACTION_TOOLS or "__" in name:
            continue
        return True
    return False


def extract_task_instruction(
    messages: Sequence[dict[str, Any]], *, cap: int = TASK_INSTRUCTION_CAP
) -> str | None:
    """Extract the dispatch instruction from the a2a-peer task envelope."""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                envelope = json.loads(stripped)
            except (ValueError, TypeError):
                envelope = None
            if isinstance(envelope, dict) and isinstance(envelope.get("body"), dict):
                body = envelope["body"]
                for key in ("content", "task", "instruction"):
                    text = body.get(key)
                    if isinstance(text, str) and text.strip():
                        return _clip(text.strip(), cap)
                continue
        if "## Environment State" in stripped[:200]:
            continue  # auto-injected state, not an instruction
        return _clip(stripped, cap)
    return None


def extract_env_sections(
    text: str, *, caps: dict[str, int] | None = None
) -> str:
    """Keep the fact-bearing Environment State sections, drop the noise ones.

    Sections are split on ``### <name>`` headings.  ``Spatial State`` /
    ``Embodied State`` / ``Task Execution State`` are kept (each capped);
    ``Relevant Recent Events`` (framework event spam) and
    ``Freshness / Conflicts / Evidence`` are dropped.  When fewer than two of
    the wanted sections can be found the whole block degrades to a head+tail
    clip so no information class is silently lost.
    """
    caps = caps or ENV_SECTION_CAPS
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            current = stripped[4:].strip()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    parts: list[str] = []
    for name in ("Spatial State", "Embodied State", "Task Execution State"):
        body = "\n".join(sections.get(name, [])).strip()
        if body:
            parts.append(f"### {name}\n{_clip(body, caps.get(name, 6000))}")
    if len(parts) >= 2:
        return "\n\n".join(parts)
    return _clip_head_tail(text.strip(), caps.get("fallback", 8000))


def build_observation_digest(
    messages: Sequence[dict[str, Any]],
    *,
    tool_obs_cap: int = TOOL_OBSERVATION_CAP,
    env_caps: dict[str, int] | None = None,
) -> str:
    """The latest observation: trailing tool results + injected env state."""
    last_assistant = -1
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            last_assistant = index
    tail = list(messages[last_assistant + 1 :])
    parts: list[str] = []
    tool_results = [m for m in tail if m.get("role") == "tool"]
    for position, message in enumerate(tool_results[-TOOL_OBSERVATIONS_KEPT:], start=1):
        content = message.get("content")
        if isinstance(content, (str, int, float)) and str(content).strip():
            parts.append(
                f"[tool result {position}]\n{_clip(str(content), tool_obs_cap)}"
            )
    user_messages = [m for m in tail if m.get("role") == "user"]
    if user_messages:
        content = user_messages[-1].get("content")
        if isinstance(content, str) and content.strip():
            parts.append(
                "[Environment State snapshot]\n"
                + extract_env_sections(content, caps=env_caps)
            )
    if not parts:
        return "(no prior observation in this round)"
    return "\n\n".join(parts)


def render_tool_calls(
    tool_calls: Sequence[dict[str, Any]], *, argument_cap: int = TOOL_ARGUMENT_CAP
) -> list[dict[str, Any]]:
    """Compact, audit-friendly rendering of a decision's tool calls."""
    rendered: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        try:
            args_text = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            args_text = str(arguments)
        rendered.append(
            {
                "tool": name,
                "arguments": (
                    args_text
                    if len(args_text) <= argument_cap
                    else args_text[:argument_cap] + TRUNCATION_MARKER
                ),
            }
        )
    return rendered


def render_decision(
    round_: WorkerRound, *, content_cap: int = RESPONSE_CONTENT_CAP
) -> str:
    """Render the agent's decision (content + tool calls) for the judge."""
    parts: list[str] = []
    content = (round_.response_content or "").strip()
    if content:
        parts.append("Agent message:\n" + _clip(content, content_cap))
    calls = render_tool_calls(round_.tool_calls)
    if calls:
        rendered = "\n".join(
            f"- {call['tool']}({call['arguments']})" for call in calls
        )
        parts.append("Tool call(s):\n" + rendered)
    return "\n\n".join(parts) if parts else "(no decision content)"
