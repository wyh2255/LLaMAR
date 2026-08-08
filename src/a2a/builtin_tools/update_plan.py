"""UpdatePlanTool — Agent 声明/修改编排计划 with MissionGraph validation."""

from __future__ import annotations

from typing import Any

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.mission_graph import MissionGraphError
from a2a.coordinator.task_store import TaskStore


def _format_participants(ids: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(str(x) for x in ids) + "]"


def _format_deps(deps: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(str(x) for x in deps) + "]"


def _format_change_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return _format_participants(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [f"{k}={v!r}" for k, v in sorted(value.items())]
        return "{" + ", ".join(parts) + "}"
    return repr(value) if isinstance(value, str) else str(value)


def _format_modified_entry(entry: dict[str, Any]) -> str:
    logical_id = entry["logical_id"]
    changes = entry.get("changes") or {}
    parts: list[str] = []
    for field in (
        "participants",
        "depends_on",
        "objective",
        "assignments",
        "status",
    ):
        if field not in changes:
            continue
        ch = changes[field]
        before = _format_change_value(ch.get("before"))
        after = _format_change_value(ch.get("after"))
        label = "participants" if field == "participants" else field
        if field == "objective":
            parts.append(f"objective: {before} → {after}")
        else:
            parts.append(f"{label}: {before} → {after}")
    detail = "; ".join(parts) if parts else "semantic change"
    return f"{logical_id} ({detail}; reset to planned)"


def _format_added_entry(entry: dict[str, Any]) -> str:
    logical_id = entry["logical_id"]
    participants = _format_participants(entry.get("participant_ids") or [])
    deps = _format_deps(entry.get("depends_on") or [])
    parts = [f"participants={participants}", f"deps={deps}"]
    objective = entry.get("objective")
    if objective:
        parts.append(f"objective={objective!r}")
    return f"{logical_id} ({', '.join(parts)})"


def _format_plan_feedback(view: dict[str, Any]) -> str:
    """Build LLM-readable multi-line summary from MissionGraph replace result."""
    revision = view.get("revision", 0)
    diff = view.get("diff") or {}
    added = list(diff.get("added") or [])
    removed = list(diff.get("removed") or [])
    modified = list(diff.get("modified") or [])
    preserved = list(diff.get("preserved") or [])
    frozen = list(diff.get("frozen") or [])
    ready = list(view.get("ready") or [])
    state_counts = dict(view.get("state_counts") or {})

    no_changes = not added and not removed and not modified
    lines: list[str] = [f"Plan updated (revision {revision}):"]

    if no_changes:
        lines.append("  No changes")
    else:
        if added:
            lines.append("  Added: " + ", ".join(_format_added_entry(e) for e in added))
        if removed:
            lines.append("  Removed: " + ", ".join(str(x) for x in removed))
        if modified:
            lines.append(
                "  Modified: " + ", ".join(_format_modified_entry(e) for e in modified)
            )
        if preserved:
            # Exclude frozen from preserved display if we show frozen separately,
            # but still list all preserved ids for clarity.
            lines.append("  Preserved: " + ", ".join(str(x) for x in preserved))
        if frozen:
            lines.append(
                "  Frozen (active, kept as-is): " + ", ".join(str(x) for x in frozen)
            )

    if ready:
        lines.append("  Ready now: " + ", ".join(str(x) for x in ready))
    else:
        lines.append("  Ready now: (none)")

    # Prefer a stable state order for readability.
    preferred = (
        "ready",
        "blocked",
        "active",
        "activating",
        "planned",
        "completed",
        "failed",
        "canceled",
    )
    state_parts: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in state_counts:
            state_parts.append(f"{key}={state_counts[key]}")
            seen.add(key)
    for key in sorted(state_counts):
        if key not in seen:
            state_parts.append(f"{key}={state_counts[key]}")
    if state_parts:
        lines.append("  State: " + ", ".join(state_parts))
    else:
        lines.append("  State: (empty)")

    return "\n".join(lines)


class UpdatePlanTool(Tool):
    """声明或修改编排计划。

    Agent 传入完整的计划节点列表（非增量）。
    System validates via MissionGraph: enriched entries (participant_ids, objective,
    assignments) are accepted; cycles, missing participants, undeclared deps rejected.
    Execution state is preserved across updates.
    """

    def __init__(self, store: TaskStore):
        self._store = store

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Declare or modify the orchestration plan. "
            "Pass the FULL plan list (not a delta). "
            "Each node has: task_id (unique), worker_id (optional), "
            "participant_ids (list of Worker IDs), objective, assignments, "
            "description, depends_on (list of task_ids), status ('pending'|'skipped'). "
            "Execution state is managed by the system and preserved across updates. "
            "Returns a diff summary."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Unique task identifier (kebab-case)",
                            },
                            "worker_id": {
                                "type": "string",
                                "description": "Target worker ID (optional, legacy)",
                            },
                            "participant_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Worker IDs (new enriched format)",
                            },
                            "objective": {
                                "type": "string",
                                "description": "What this logical node should achieve",
                            },
                            "assignments": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": "Per-worker assignment map",
                            },
                            "description": {
                                "type": "string",
                                "description": "Human-readable task description",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Task IDs this task depends on",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "skipped"],
                                "description": "Structural status (default: pending)",
                            },
                        },
                        "required": ["task_id"],
                    },
                    "description": "Full plan node list (replaces existing plan)",
                },
            },
            "required": ["plan"],
        }

    async def execute(self, plan: list[dict[str, Any]]) -> ToolResult:
        """Validate via MissionGraph, then update legacy PlanNode view."""
        was_empty = self._store.mission_node_count == 0
        try:
            view = self._store.replace_mission_graph(plan)
        except MissionGraphError as exc:
            return ToolResult(
                success=False,
                content=f"Invalid plan: {exc}",
                error="invalid_plan",
            )
        except Exception as exc:  # noqa: BLE001 - internal failure surfaces as a structured code
            return ToolResult(
                success=False,
                content=f"Plan update failed: {exc}",
                error="plan_update_failed",
            )

        self._store.update_plan(plan)
        content = _format_plan_feedback(view)
        if was_empty and view.get("nodes", 0) > 0:
            content += (
                "\n  Graph mode active: assign_task/dispatch_task now require "
                "the task to be declared in the plan. New tasks must be added "
                "via update_plan first."
            )
        return ToolResult(
            success=True,
            content=content,
            data={
                "diff": view.get("diff"),
                "snapshot": {
                    "revision": view.get("revision"),
                    "nodes": view.get("nodes"),
                    "state_counts": view.get("state_counts"),
                    "ready": view.get("ready"),
                    "node_ids": view.get("node_ids"),
                },
            },
        )
