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
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from .schema import Message

if TYPE_CHECKING:
    from Agent.router_agent.state_provider import RuntimeState, StateProvider


@dataclass
class ContextConfig:
    """Configuration for context management."""

    strategy: str = "hybrid"  # "none" | "summary" | "hybrid" | "raw"
    recent_messages: int = 12
    summary_trigger_ratio: float = 0.8
    pinned_enabled: bool = True
    episodic_max_items: int = 20
    state_mode: str = "semantic"


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
        state_provider: "StateProvider | None" = None,
    ):
        self.config = config or ContextConfig()
        self.token_limit = token_limit
        self.summary_trigger_tokens = int(
            token_limit * self.config.summary_trigger_ratio
        )
        self._log_dir = Path(log_dir) if log_dir else None

        # Pinned: structured state updated on every tool observation
        self.pinned: dict[str, Any] = {}
        self._pinned_state: BaseModel | None = (
            None  # typed version, replace `pinned` over time
        )
        # Episodic: list of summarized episodes
        self.episodic: list[_Episode] = []
        # Step counter for episode ordering
        self._episode_counter: int = 0
        # Task snapshots: task_id -> (messages, pinned_data) (for pause/resume)
        self._task_snapshots: dict[str, tuple[list[Message], dict | None]] = {}

        # Runtime state provider: system-injected state refreshed before each
        # LLM request. ContextManager does not directly import SAR backends.
        self._state_provider: StateProvider | None = state_provider
        self._runtime_state: RuntimeState | None = None

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

    def refresh_runtime_state(
        self, context_id: str | None = None
    ) -> "RuntimeState | None":
        """Refresh system-injected runtime state from the attached StateProvider.

        If no StateProvider is attached, returns None and leaves pinned state
        unchanged. The refreshed state is also projected into the pinned state
        so that _render_* methods can render it uniformly.
        """
        if self._state_provider is None:
            return None
        state = self._state_provider.snapshot(context_id)
        self._runtime_state = state
        self._project_runtime_state_to_pinned(state)
        return state

    def _project_runtime_state_to_pinned(self, state: "RuntimeState") -> None:
        """Project runtime state payload into the typed pinned state."""
        if self._pinned_state is None:
            return
        payload = state.payload
        for key in (
            "position",
            "inventory",
            "step",
            "known_fires",
            "known_persons",
            "mission_status",
        ):
            if key in payload and hasattr(self._pinned_state, key):
                setattr(self._pinned_state, key, payload[key])

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

        # Fix: remove orphaned tool messages whose assistant was pruned
        remaining_exec = [i for i in exec_indices if i not in to_remove]
        found_assistant = False
        for i in remaining_exec:
            msg = messages[i]
            if msg.role == "assistant":
                found_assistant = True
            elif msg.role == "tool" and not found_assistant:
                to_remove.add(i)

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

        Order: system prompt → raw recent messages → memory block (pinned + episodic).

        Memory block is placed AFTER conversation history so that the system prompt
        + growing message history form a stable prefix for DeepSeek auto-prefix caching.
        When strategy is "raw", no memory block is injected — messages pass through as-is.
        """
        if self.config.strategy == "raw":
            return [Message(role="system", content=system_prompt), *messages[1:]]

        result: list[Message] = []
        result.append(Message(role="system", content=system_prompt))
        result.extend(messages[1:])  # skip original system prompt if present

        memory_text = self._render_memory_block()
        if memory_text:
            result.append(Message(role="user", content=memory_text))

        return result

    def _render_environment_view(self) -> str:
        """Environment layer: fires, persons, etc. Override in subclasses."""
        return ""

    def _render_current_state(self) -> str:
        """State layer: position, inventory, step. Override in subclasses."""
        if not self._pinned_state:
            # fallback to old dict-style pinned
            if self.config.pinned_enabled and self.pinned:
                return "\n".join(
                    f"- {k}: {v}" for k, v in self.pinned.items() if v is not None
                )
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
    state_mode: str = "semantic"


class WorkerContextManager(ContextManager):
    """Worker-side context manager for SAR tasks."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
        state_provider: "StateProvider | None" = None,
    ):
        super().__init__(config, token_limit, log_dir, state_provider)
        self._pinned_state = WorkerPinnedState()
        self._pinned_state.state_mode = self.config.state_mode
        self.pinned = self._pinned_state.model_dump()

    def _render_environment_view(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, WorkerPinnedState):
            return ""
        parts = []
        if ps.known_fires:
            parts.append(f"Known fires: {len(ps.known_fires)}")
            for f in ps.known_fires[:5]:
                desc = f.get("description", str(f))[:200]
                parts.append(f"  - {desc}")
        if ps.known_persons:
            parts.append(f"Known persons: {len(ps.known_persons)}")
            for p in ps.known_persons[:3]:
                desc = p.get("description", str(p))[:200]
                parts.append(f"  - {desc}")
        return "\n".join(parts)

    def _render_current_state(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, WorkerPinnedState):
            return super()._render_current_state()
        lines = []
        age_note = ""
        if self._runtime_state is not None and self._runtime_state.observed_at > 0:
            age_ms = self._runtime_state.get("age_ms", 0.0)
            if age_ms > 0:
                age_note = f" source barrier, age {age_ms:.0f}ms"
        if ps.position:
            lines.append(
                f"- Position: ({ps.position[0]}, {ps.position[1]}, {ps.position[2]}){age_note}"
            )
        if ps.inventory:
            lines.append(f"- Inventory: {ps.inventory}{age_note}")
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
                    x.strip().strip("'\"")
                    for x in inv_match.group(1).strip("[]").split(",")
                    if x.strip()
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
