"""Context management for router_agent.

Provides a three-tier memory model:
- Pinned state: structured key facts updated in real time
- Episodic memory: summarized past tool episodes
- Recent window: the last N raw assistant/tool messages kept unmodified
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .schema import Message


@dataclass
class ContextConfig:
    """Configuration for context management."""

    strategy: str = "hybrid"  # "none" | "summary" | "hybrid"
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

    def __init__(self, config: ContextConfig | None = None, token_limit: int = 80000):
        self.config = config or ContextConfig()
        self.token_limit = token_limit
        self.summary_trigger_tokens = int(token_limit * self.config.summary_trigger_ratio)

        # Pinned: structured state updated on every tool observation
        self.pinned: dict[str, Any] = {}
        # Episodic: list of summarized episodes
        self.episodic: list[_Episode] = []
        # Step counter for episode ordering
        self._episode_counter: int = 0

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
        if self.config.pinned_enabled:
            extracted = self._extract_pinned(tool_name, content, success)
            if extracted:
                self.pinned.update(extracted)

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
        if self.config.strategy == "none":
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
        """
        result: list[Message] = []
        result.append(Message(role="system", content=system_prompt))

        memory_text = self._render_memory_block()
        if memory_text:
            result.append(Message(role="user", content=memory_text))

        result.extend(messages[1:])  # skip original system prompt if present
        return result

    def _render_memory_block(self) -> str:
        """Render pinned state, recent episodes, and worker events into a memory block."""
        lines: list[str] = ["## Agent Context Memory"]

        if self.config.pinned_enabled and self.pinned:
            lines.append("### Current State")
            for key, value in self.pinned.items():
                lines.append(f"- {key}: {value}")

        if self.episodic:
            lines.append(f"### Recent Episodes (last {len(self.episodic)})")
            for ep in self.episodic[-10:]:
                lines.append(f"- step {ep.step}: {ep.summary}")

        # Inject worker event summary from EventStore
        try:
            from a2a.coordinator.event_store import event_store

            summary = event_store.get_summary()
            if summary:
                lines.append("")
                lines.append(summary)
        except ImportError:
            pass

        return "\n".join(lines)


class WorkerContextManager(ContextManager):
    """Worker-side context manager for SAR tasks.

    Pinned schema:
    - position: tuple(x, y, z)
    - inventory: list[str]
    - step: int
    - known_fires: list[dict]
    - known_persons: list[dict]
    - mission_status: str
    """

    def __init__(self, config: ContextConfig | None = None, token_limit: int = 80000):
        super().__init__(config, token_limit)
        self.pinned = {
            "position": None,
            "inventory": [],
            "step": 0,
            "known_fires": [],
            "known_persons": [],
            "mission_status": "in_progress",
        }

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


class CoordinatorContextManager(ContextManager):
    """Coordinator-side context manager for SAR orchestration.

    Pinned schema:
    - global_snapshot: dict
    - step_budget: dict {current_step, max_steps, remaining}
    - mission_finished: bool
    - dispatched_tasks: list[dict]
    - worker_results: list[dict]
    """

    def __init__(self, config: ContextConfig | None = None, token_limit: int = 80000):
        super().__init__(config, token_limit)
        self.pinned = {
            "global_snapshot": {},
            "step_budget": {"current_step": 0, "max_steps": 0, "remaining": 0},
            "mission_finished": False,
            "dispatched_tasks": [],
            "worker_results": [],
        }

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
