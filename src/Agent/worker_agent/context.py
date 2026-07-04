"""Context management for worker_agent.

Provides a three-tier memory model:
- Pinned state: structured key facts updated in real time (position, inventory, ...)
- Episodic memory: summarized past tool episodes
- Recent window: the last N raw assistant/tool messages kept unmodified
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .schema import Message


@dataclass
class ContextConfig:
    """Configuration for context management."""

    strategy: str = "hybrid"  # "none" | "summary" | "hybrid" | "raw"
    recent_messages: int = 12
    summary_trigger_ratio: float = 0.8
    pinned_enabled: bool = True
    episodic_max_items: int = 20


@dataclass
class _Episode:
    """Single summarized episode."""

    step: int = 0
    tool_name: str = ""
    summary: str = ""


class ContextManager:
    """Base context manager with pinned + episodic + recent-window memory."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
    ):
        self.config = config or ContextConfig()
        self.token_limit = token_limit
        self.summary_trigger_tokens = int(token_limit * self.config.summary_trigger_ratio)
        self._log_dir = Path(log_dir) if log_dir else None

        # Pinned: structured state updated on every tool observation
        self.pinned: dict[str, Any] = {}
        self._pinned_state: BaseModel | None = None  # typed version, replace `pinned` over time
        # Episodic: list of summarized episodes
        self.episodic: list[_Episode] = []
        # Step counter for episode ordering
        self._episode_counter: int = 0
        # Task snapshots: task_id -> (messages, pinned_data) (for pause/resume)
        self._task_snapshots: dict[str, tuple[list[Message], dict | None]] = {}

    def _snapshot_path(self, task_id: str) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / f"snapshot_{task_id}.json"

    def _snapshot_pinned_data(self) -> dict | None:
        """Serialize pinned state for snapshot."""
        if self._pinned_state is not None:
            return self._pinned_state.model_dump()
        if self.pinned:
            return dict(self.pinned)
        return None

    def _restore_pinned_data(self, data: dict | None) -> None:
        """Restore pinned state from snapshot data."""
        if data is None:
            return
        if self._pinned_state is not None:
            try:
                self._pinned_state = self._pinned_state.__class__.model_validate(data)
                self.pinned = self._pinned_state.model_dump()
                return
            except Exception:
                self._pinned_state = None  # fall back to dict
        self.pinned.update(data)

    def save_snapshot(self, task_id: str, messages: list) -> None:
        """Save a full messages snapshot for later resume (memory + optional disk)."""
        pinned_data = self._snapshot_pinned_data()
        self._task_snapshots[task_id] = (copy.deepcopy(messages), pinned_data)
        path = self._snapshot_path(task_id)
        if path is not None:
            try:
                os.makedirs(path.parent, exist_ok=True)
                payload = {
                    "pinned": pinned_data,
                    "messages": [m.model_dump() for m in messages],
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except OSError:
                pass

    def load_snapshot(self, task_id: str) -> list | None:
        """Load and remove a snapshot. Checks memory first, then disk."""
        # Check memory first
        if task_id in self._task_snapshots:
            msgs, pinned_data = self._task_snapshots.pop(task_id)
            self._restore_pinned_data(pinned_data)
            return msgs

        # Fall back to disk
        path = self._snapshot_path(task_id)
        if path is not None and path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                os.remove(path)
                if isinstance(payload, dict) and "messages" in payload:
                    self._restore_pinned_data(payload.get("pinned"))
                    return [Message.model_validate(m) for m in payload["messages"]]
                # backward compat: old format was a flat array
                return [Message.model_validate(m) for m in payload]
            except (OSError, json.JSONDecodeError):
                pass

        return None

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        """Subclass override: extract structured state from a tool result.

        Returns a partial dict of pinned fields to update, or None if nothing
        should be updated.
        """
        return None

    def observe(self, tool_name: str, content: str, success: bool) -> None:
        """Process a tool result: update pinned state and append an episode."""
        if self.config.strategy == "raw":
            return

        if self.config.pinned_enabled:
            extracted = self._extract_pinned(tool_name, content, success)
            if extracted:
                self.pinned.update(extracted)
                if self._pinned_state is not None:
                    for k, v in extracted.items():
                        if hasattr(self._pinned_state, k):
                            setattr(self._pinned_state, k, v)

        # Build a concise episode summary
        status = "success" if success else "failure"
        truncated = content[:500] + "..." if len(content) > 500 else content
        self._episode_counter += 1
        self.episodic.append(
            _Episode(
                step=self._episode_counter,
                tool_name=tool_name,
                summary=f"{tool_name} → {status}: {truncated}",
            )
        )
        self._prune_episodic()

    def _prune_episodic(self) -> None:
        """Keep episodic memory bounded."""
        max_items = self.config.episodic_max_items
        if len(self.episodic) > max_items:
            self.episodic = self.episodic[-max_items:]

    def prune_history(self, messages: list[Message]) -> None:
        """Prune raw message history to keep it bounded.

        Removes assistant/tool messages older than the recent window while
        preserving all user and system messages.
        """
        if self.config.strategy in ("none", "raw"):
            return

        recent = self.config.recent_messages
        # Identify indices of assistant/tool messages
        exec_indices = [
            i
            for i, msg in enumerate(messages)
            if msg.role in ("assistant", "tool") and i > 0
        ]
        if len(exec_indices) <= recent:
            return

        to_remove = set(exec_indices[:-recent])
        # Summarize removed episodes before discarding (lightweight)
        for i in sorted(to_remove):
            msg = messages[i]
            if msg.role == "tool":
                # Already observed via post_tool; skip double-counting
                continue
            if msg.role == "assistant" and msg.content:
                self._episode_counter += 1
                self.episodic.append(
                    _Episode(
                        step=self._episode_counter,
                        tool_name="assistant",
                        summary=f"assistant: {str(msg.content)[:200]}",
                    )
                )

        messages[:] = [msg for i, msg in enumerate(messages) if i not in to_remove]
        self._prune_episodic()

    def assemble(self, system_prompt: str, messages: list[Message]) -> list[Message]:
        """Build the final message list to send to the LLM.

        Order: system prompt → memory block (pinned + episodic) → raw recent messages.

        When strategy is "raw", no memory block is injected — messages pass through as-is.
        """
        if self.config.strategy == "raw":
            return [Message(role="system", content=system_prompt), *messages[1:]]

        result: list[Message] = []
        result.append(Message(role="system", content=system_prompt))

        memory_text = self._render_memory_block()
        if memory_text:
            result.append(Message(role="user", content=memory_text))

        result.extend(messages[1:])  # skip original system prompt if present
        return result

    def _render_environment_view(self) -> str:
        """Environment layer: fires, persons, etc. Override in subclasses."""
        return ""

    def _render_current_state(self) -> str:
        """State layer: position, inventory, step. Override in subclasses."""
        if not self._pinned_state:
            # fallback to old dict-style pinned
            if self.config.pinned_enabled and self.pinned:
                return "\n".join(f"- {k}: {v}" for k, v in self.pinned.items() if v is not None)
            return ""
        return ""

    def _render_memory_block(self) -> str:
        """Render layered context memory block.

        Layout: environment → current state → action history.
        """
        lines: list[str] = ["---", "## Context Memory", "---"]

        env_text = self._render_environment_view()
        if env_text:
            lines.append("### Environment")
            lines.append(env_text)
            lines.append("---")

        state_text = self._render_current_state()
        if state_text:
            lines.append("### Current State")
            lines.append(state_text)
            lines.append("---")

        if self.episodic:
            lines.append(f"### Action History (last {len(self.episodic)} steps)")
            for ep in self.episodic[-10:]:
                lines.append(f"- step {ep.step}: {ep.summary}")
            lines.append("---")

        return "\n".join(lines)


class WorkerPinnedState(BaseModel):
    """Worker-side typed pinned state schema."""
    version: int = Field(default=1, ge=1)
    position: tuple[int, int, int] | None = None
    inventory: list[str] = Field(default_factory=list)
    step: int = 0
    known_fires: list[dict] = Field(default_factory=list)
    known_persons: list[dict] = Field(default_factory=list)
    mission_status: str = "in_progress"


class WorkerContextManager(ContextManager):
    """Worker-side context manager for SAR tasks."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
    ):
        super().__init__(config, token_limit, log_dir)
        self._pinned_state = WorkerPinnedState()
        self.pinned = self._pinned_state.model_dump()

    def _render_environment_view(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, WorkerPinnedState):
            return ""
        parts = []
        if ps.known_fires:
            parts.append(f"Known fires: {len(ps.known_fires)}")
            for f in ps.known_fires[:5]:
                parts.append(f"  - {f}")
        if ps.known_persons:
            parts.append(f"Known persons: {len(ps.known_persons)}")
            for p in ps.known_persons[:3]:
                parts.append(f"  - {p}")
        return "\n".join(parts)

    def _render_current_state(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, WorkerPinnedState):
            return super()._render_current_state()
        lines = []
        if ps.position:
            lines.append(f"- Position: ({ps.position[0]}, {ps.position[1]}, {ps.position[2]})")
        if ps.inventory:
            lines.append(f"- Inventory: {ps.inventory}")
        lines.append(f"- Step: {ps.step}")
        lines.append(f"- Mission: {ps.mission_status}")
        return "\n".join(lines)

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        """Extract SAR worker state from tool results using regex and JSON parsing."""
        if not success or not content:
            return None

        updates: dict[str, Any] = {}

        # Position extraction: common across navigation/state tools
        pos_match = re.search(
            r"[Pp]osition[:\s]*\(?\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*([0-9]+)\s*\)?",
            content,
        )
        if pos_match:
            updates["position"] = (
                int(pos_match.group(1)),
                int(pos_match.group(2)),
                int(pos_match.group(3)),
            )

        # Inventory extraction: "Inventory: ['Water', 'Sand']" or "Inventory: []"
        inv_match = re.search(r"Inventory:\s*(\[[^\]]*\])", content)
        if inv_match:
            try:
                updates["inventory"] = json.loads(inv_match.group(1).replace("'", '"'))
            except json.JSONDecodeError:
                updates["inventory"] = [
                    x.strip().strip("'\"") for x in inv_match.group(1).strip("[]").split(",") if x.strip()
                ]

        # Step extraction
        step_match = re.search(r"[Ss]tep[:\s]+([0-9]+)", content)
        if step_match:
            updates["step"] = int(step_match.group(1))

        # Try JSON parsing for tools that return structured data
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if "position" in data:
                updates["position"] = data["position"]
            if "inventory" in data:
                updates["inventory"] = data["inventory"]
            if "step" in data:
                updates["step"] = data["step"]
            if "fires" in data:
                updates["known_fires"] = data["fires"]
            if "persons" in data:
                updates["known_persons"] = data["persons"]

        # no_op hints at mission completion
        if tool_name == "no_op":
            if "[MISSION COMPLETE]" in content:
                updates["mission_status"] = "complete"
            elif "Mission in progress" in content:
                updates["mission_status"] = "in_progress"

        return updates if updates else None


class CoordinatorPinnedState(BaseModel):
    """Coordinator-side typed pinned state schema."""
    version: int = Field(default=1, ge=1)
    global_snapshot: dict = Field(default_factory=dict)
    step_budget: dict = Field(default_factory=lambda: {"current_step": 0, "max_steps": 0, "remaining": 0})
    mission_finished: bool = False
    dispatched_tasks: list[dict] = Field(default_factory=list)
    worker_results: list[dict] = Field(default_factory=list)


class CoordinatorContextManager(ContextManager):
    """Coordinator-side context manager for SAR orchestration."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
    ):
        super().__init__(config, token_limit, log_dir)
        self._pinned_state = CoordinatorPinnedState()
        self.pinned = self._pinned_state.model_dump()

    def _render_environment_view(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return ""
        parts = []
        snap = ps.global_snapshot
        if snap:
            agents = snap.get("agents", [])
            parts.append(f"Active workers: {len(agents)}")
            fires = snap.get("fires", [])
            parts.append(f"Total fires: {len(fires)}")
            persons = snap.get("persons", [])
            rescued = sum(1 for p in persons if p.get("rescued"))
            parts.append(f"Persons: {len(persons)} total, {rescued} rescued")
        return "\n".join(parts)

    def _render_current_state(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return super()._render_current_state()
        lines = []
        budget = ps.step_budget
        lines.append(f"- Step: {budget.get('current_step', 0)} / {budget.get('max_steps', 0)} "
                      f"(remaining: {budget.get('remaining', 0)})")
        lines.append(f"- Mission finished: {ps.mission_finished}")
        if ps.dispatched_tasks:
            lines.append(f"- Dispatched: {len(ps.dispatched_tasks)} tasks")
            for t in ps.dispatched_tasks[-3:]:
                lines.append(f"  - {t.get('agent_id')}: {t.get('task_id')}")
        return "\n".join(lines)

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        """Extract coordinator-level state from tool results."""
        if not success or not content:
            return None

        updates: dict[str, Any] = {}

        if tool_name == "query_sar_state":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                snapshot = {k: v for k, v in data.items() if k not in ("step", "max_steps", "finished")}
                updates["global_snapshot"] = snapshot
                updates["step_budget"] = {
                    "current_step": data.get("step", 0),
                    "max_steps": data.get("max_steps", 0),
                    "remaining": max(0, data.get("max_steps", 0) - data.get("step", 0)),
                }
                updates["mission_finished"] = bool(data.get("finished", False))

        if tool_name == "dispatch_task":
            # Content like "Task 'X' dispatched to 'Y'. ..."
            m = re.search(r"Task ['\"](?P<tid>[^'\"]+)['\"] dispatched to ['\"](?P<wid>[^'\"]+)['\"]", content)
            if m:
                updates.setdefault("dispatched_tasks", []).append(
                    {"task_id": m.group("tid"), "agent_id": m.group("wid")}
                )

        if tool_name == "collect_results":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                updates.setdefault("worker_results", []).extend(data)

        if tool_name == "finish_task":
            # Coordinator finish_task content is a mission summary
            updates["mission_finished"] = True

        return updates if updates else None
