"""Export a finished SAR run directory into a single run_detail.json for the results UI.

Deterministic parser (no LLM). Parsing logic mirrors sar_orch/eval/dataset.py.

Usage:
    python sar_orch/export_run_detail.py <run_dir> [-o out.json]
"""

import argparse
import ast
import csv
import json
import re
from pathlib import Path

_ACTION_RE = re.compile(r"^(\w+)\(([^)]*)\)$")
_INVENTORY_RE = re.compile(r"I am holding (\{.*?\})")
_POSITION_RE = re.compile(r"co-ordinates:\s*(\([^)]+\))")
_NAMES_RE = re.compile(r"Names:\s*(\[[^\]]*\])")

_ACTION_ALIASES = {"CarryPerson": "Carry"}

OBS_LIMIT = 4000
LLM_LIMIT = 20000


def parse_action(action_str):
    m = _ACTION_RE.match((action_str or "").strip())
    if not m:
        return (action_str or "").strip(), []
    args = [a.strip() for a in m.group(2).split(",") if a.strip()]
    name = _ACTION_ALIASES.get(m.group(1), m.group(1))
    return name, args


def parse_observation(text):
    inventory = position = None
    names = []
    m = _INVENTORY_RE.search(text or "")
    if m:
        try:
            inventory = json.loads(m.group(1).replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            pass
    m = _POSITION_RE.search(text or "")
    if m:
        try:
            parts = [int(x.strip()) for x in m.group(1).strip("()").split(",")]
            if len(parts) == 3:
                position = parts
        except (ValueError, TypeError):
            pass
    m = _NAMES_RE.search(text or "")
    if m:
        try:
            parsed = ast.literal_eval(m.group(1))
            if isinstance(parsed, list):
                names = [str(n) for n in parsed]
        except (ValueError, SyntaxError, TypeError):
            pass
    return inventory, position, names


def _csv_list(value):
    if not value or value == "[]":
        return []
    try:
        return json.loads(value.replace("'", '"'))
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, SyntaxError):
        return []


def _load_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _load_jsonl(path):
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


def _clip(s, limit):
    s = s or ""
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} chars]"


def export_run(run_dir):
    run_dir = Path(run_dir)
    meta = {}
    mp = run_dir / "metadata.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    metrics = {}
    rp = run_dir / "run_metrics.json"
    if rp.exists():
        metrics = json.loads(rp.read_text(encoding="utf-8"))

    traj_rows = _load_csv(run_dir / "trajectory.csv")
    agent_rows = _load_csv(run_dir / "agent_interactions.csv")
    router_rows = _load_csv(run_dir / "router_interactions.csv")
    subtask_rows = _load_csv(run_dir / "subtasks.csv")
    token_rows = _load_csv(run_dir / "token_usage.csv")
    events = _load_jsonl(run_dir / "events.ndjson")

    interactions_by_step = {}
    for row in agent_rows:
        step = int(row.get("Step", 0))
        act_name, act_args = parse_action(row.get("Action", ""))
        obs = row.get("Observation", "")
        inv, pos, names = parse_observation(obs)
        interactions_by_step.setdefault(step, []).append({
            "agent": row.get("Agent", ""),
            "tool": row.get("ToolName", ""),
            "tool_args": row.get("ToolArgs", ""),
            "action": row.get("Action", ""),
            "action_name": act_name,
            "action_args": act_args,
            "event_type": row.get("EventType", ""),
            "error_type": row.get("ErrorType", ""),
            "latency_ms": row.get("ToolLatencyMs", ""),
            "inventory": inv,
            "position": pos,
            "visible_names": names,
            "observation": _clip(obs, OBS_LIMIT),
            "llm_input": _clip(row.get("LLMInput", ""), LLM_LIMIT),
            "llm_output": _clip(row.get("LLMOutput", ""), LLM_LIMIT),
            "thinking": _clip(row.get("Thinking", ""), LLM_LIMIT),
        })

    dispatches_by_step = {}
    for row in router_rows:
        step = int(row.get("Step", 0)) + 1
        dispatches_by_step.setdefault(step, []).append({
            "subtask": row.get("Subtask", ""),
            "assigned_to": row.get("AssignedTo", ""),
            "event_type": row.get("EventType", ""),
            "correlation_id": row.get("CorrelationID", ""),
        })

    subtasks_by_step = {}
    for row in subtask_rows:
        step = int(row.get("Step", 0)) + 1
        subtasks_by_step.setdefault(step, []).append({
            "subtask_id": row.get("SubtaskID", ""),
            "status": row.get("Status", ""),
            "assigned_to": row.get("AssignedTo", ""),
            "subtask": row.get("Subtask", ""),
            "failure_class": row.get("FailureClass", ""),
        })

    tokens_by_step = {}
    for row in token_rows:
        step = int(row.get("Step", 0))
        rec = tokens_by_step.setdefault(step, {})
        agent = row.get("Agent", "?")
        rec[agent] = rec.get(agent, 0) + int(row.get("TotalTokens") or 0)

    events_by_step = {}
    for e in events:
        step = e.get("step", 0)
        p = e.get("payload", {}) or {}
        events_by_step.setdefault(step, []).append({
            "t": e.get("timestamp"),
            "agent": e.get("agent", ""),
            "type": e.get("event_type", ""),
            "who": p.get("who", ""),
            "message_type": p.get("message_type", ""),
            "related_task_id": p.get("related_task_id", ""),
            "content": _clip(p.get("content", ""), 800),
        })

    steps = []
    for row in traj_rows:
        step = int(row.get("Step", 0))
        steps.append({
            "step": step,
            "actions": _csv_list(row.get("Actions", "[]")),
            "successes": [bool(s) for s in _csv_list(row.get("Successes", "[]"))],
            "timeout_agents": _csv_list(row.get("TimeoutAgents", "[]")),
            "coverage": float(row.get("Coverage", 0)),
            "transport_rate": float(row.get("TransportRate", 0)),
            "map_recall": float(row.get("MapRecall") or 0),
            "finished": row.get("Finished", "").strip().lower() == "true",
            "end_reason": row.get("EndReason", ""),
            "completed_delta": _csv_list(row.get("CompletedSubtasksDelta", "[]")),
            "interactions": interactions_by_step.get(step, []),
            "dispatches": dispatches_by_step.get(step, []),
            "subtasks": subtasks_by_step.get(step, []),
            "tokens": tokens_by_step.get(step, {}),
            "events": events_by_step.get(step, []),
        })
    steps.sort(key=lambda s: s["step"])

    coord_trace = []
    coord_dir = run_dir / "coordinator"
    if coord_dir.exists():
        candidates = [
            p for p in coord_dir.glob("*.ndjson")
            if not p.name.startswith(("events_", "supervision_"))
            and p.name not in ("unknown.ndjson", "unnamed_task.ndjson")
        ]
        if candidates:
            main = max(candidates, key=lambda p: p.stat().st_size)
            for rec in _load_jsonl(main):
                ev = rec.get("event", "")
                if ev not in ("llm_response", "tool_start", "tool_result", "task_status"):
                    continue
                data = rec.get("data", {}) or {}
                entry = {"t": rec.get("timestamp", ""), "event": ev}
                if ev == "llm_response":
                    entry["content"] = _clip(data.get("content", ""), 6000)
                    entry["tool_calls"] = data.get("tool_calls", [])
                elif ev == "tool_start":
                    entry["tool"] = data.get("tool_name", "")
                    entry["args"] = _clip(json.dumps(data.get("args", data.get("tool_args", "")), ensure_ascii=False, default=str), 600)
                elif ev == "tool_result":
                    entry["tool"] = data.get("tool_name", "")
                    entry["success"] = data.get("success")
                    entry["result"] = _clip(str(data.get("result", "")), 600)
                elif ev == "task_status":
                    entry["status"] = data.get("status", "")
                    entry["task_id"] = data.get("task_id", "")
                coord_trace.append(entry)

    agent_names = meta.get("agent_names") or sorted({
        i["agent"] for s in steps for i in s["interactions"]
    } - {"Coordinator", "MapAgent"})

    return {
        "dir": run_dir.name,
        "meta": meta,
        "metrics": metrics,
        "agent_names": agent_names,
        "steps": steps,
        "coordinator_trace": coord_trace,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    data = export_run(args.run_dir)
    out = args.out or str(Path(args.run_dir) / "run_detail.json")
    Path(out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} ({Path(out).stat().st_size // 1024} KB), steps={len(data['steps'])}, coord_events={len(data['coordinator_trace'])}")


if __name__ == "__main__":
    main()
