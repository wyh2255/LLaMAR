"""Context management for router_agent.

Provides a three-tier memory model:
- Pinned state: structured key facts updated in real time
- Episodic memory: summarized past tool episodes
- Recent window: the last N raw assistant/tool messages kept unmodified
"""

from __future__ import annotations

import copy
import json
import os
import re

import tiktoken
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .schema import Message
from .state_provider import RuntimeState, StateProvider


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
        state_provider: StateProvider | None = None,
    ):
        self.config = config or ContextConfig()
        self.token_limit = token_limit
        self.summary_trigger_tokens = int(
            token_limit * self.config.summary_trigger_ratio
        )
        self._log_dir = Path(log_dir) if log_dir else None

        # Pinned: structured state updated on every tool observation
        self.pinned: dict[str, Any] = {}
        self._pinned_state: BaseModel | None = None
        # Episodic: list of summarized episodes
        self.episodic: list[_Episode] = []
        # Step counter for episode ordering
        self._episode_counter: int = 0
        # Task snapshots: task_id -> (messages, pinned_data, loaded_skills) (for pause/resume)
        self._task_snapshots: dict[
            str, tuple[list[Message], dict | None, dict[str, str]]
        ] = {}

        # Runtime state provider: system-injected state refreshed before each
        # LLM request. ContextManager does not directly import SAR backends.
        self._state_provider: StateProvider | None = state_provider
        self._runtime_state: RuntimeState | None = None

        # Loaded skills: content loaded via get_skill tool, persisted across turns
        self._loaded_skills: dict[str, str] = {}

    def _snapshot_path(self, task_id: str) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / f"snapshot_{task_id}.json"

    def _snapshot_pinned_data(self) -> dict | None:
        if self._pinned_state is not None:
            return self._pinned_state.model_dump()
        if self.pinned:
            return dict(self.pinned)
        return None

    def _restore_pinned_data(self, data: dict | None) -> None:
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

    def on_skill_loaded(self, name: str, content: str) -> None:
        """Register a loaded skill for persistence across turns in the memory block."""
        self._loaded_skills[name] = content

    def save_snapshot(self, task_id: str, messages: list) -> None:
        """Save a full messages snapshot for later resume (memory + optional disk)."""
        pinned_data = self._snapshot_pinned_data()
        self._task_snapshots[task_id] = (
            copy.deepcopy(messages),
            pinned_data,
            dict(self._loaded_skills),
        )
        path = self._snapshot_path(task_id)
        if path is not None:
            try:
                os.makedirs(path.parent, exist_ok=True)
                payload = {
                    "pinned": pinned_data,
                    "loaded_skills": dict(self._loaded_skills),
                    "messages": [m.model_dump() for m in messages],
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except OSError:
                pass

    def load_snapshot(self, task_id: str) -> list | None:
        """Load and remove a snapshot. Checks memory first, then disk."""
        if task_id in self._task_snapshots:
            msgs, pinned_data, loaded_skills = self._task_snapshots.pop(task_id)
            self._restore_pinned_data(pinned_data)
            if loaded_skills:
                self._loaded_skills.update(loaded_skills)
            return msgs

        path = self._snapshot_path(task_id)
        if path is not None and path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                os.remove(path)
                if isinstance(payload, dict) and "messages" in payload:
                    self._restore_pinned_data(payload.get("pinned"))
                    loaded = payload.get("loaded_skills")
                    if isinstance(loaded, dict):
                        self._loaded_skills.update(loaded)
                    return [Message.model_validate(m) for m in payload["messages"]]
                return [Message.model_validate(m) for m in payload]
            except (OSError, json.JSONDecodeError):
                pass

        return None

    def refresh_runtime_state(
        self, context_id: str | None = None
    ) -> RuntimeState | None:
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

    def _project_runtime_state_to_pinned(self, state: RuntimeState) -> None:
        """Project runtime state payload into the typed pinned state."""
        if self._pinned_state is None:
            return
        payload = state.payload
        for key in (
            "step_budget",
            "semantic_summary",
            "team_status_summary",
            "global_snapshot",
            "task_status_view",
            "recent_changes",
            "mission_finished",
            "supervision",
        ):
            if key in payload and hasattr(self._pinned_state, key):
                setattr(self._pinned_state, key, payload[key])

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        return None

    def observe(
        self, tool_name: str, content: str, success: bool, state_mode: str | None = None
    ) -> None:
        """Process a tool result: update pinned state and append an episode."""
        if self.config.strategy == "raw":
            return

        if state_mode is not None and self._pinned_state is not None:
            if hasattr(self._pinned_state, "state_mode"):
                setattr(self._pinned_state, "state_mode", state_mode)

        if self.config.pinned_enabled:
            extracted = self._extract_pinned(tool_name, content, success)
            if extracted:
                self.pinned.update(extracted)
                if self._pinned_state is not None:
                    for k, v in extracted.items():
                        if hasattr(self._pinned_state, k):
                            setattr(self._pinned_state, k, v)

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
        max_items = self.config.episodic_max_items
        if len(self.episodic) > max_items:
            self.episodic = self.episodic[-max_items:]

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken if available, else char-based."""
        if not text:
            return 0
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception:
            return len(text) // 4

    def _estimate_messages_tokens(self, messages: list[Message]) -> int:
        """Estimate total tokens across all messages."""
        total = 0
        for msg in messages:
            content = msg.content
            if isinstance(content, str):
                total += self._estimate_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += self._estimate_tokens(block["text"])
        return total

    def _compress_phase1(self, messages: list[Message]) -> None:
        """Phase 1 token-based compression.

        When total estimated tokens exceed 50% of the token limit, replace
        long tool result contents in the middle zone (between protected head
        and protected tail) with a short placeholder. No messages are removed.
        """
        threshold_tokens = int(self.token_limit * 0.50)
        total_tokens = self._estimate_messages_tokens(messages)
        if total_tokens < threshold_tokens:
            return

        protect_last_n = 20
        tail_token_budget = int(threshold_tokens * 0.20)

        # --- Protected tail: count-based (at least last N) ---
        tail_start = max(0, len(messages) - protect_last_n)

        # --- Protected tail: token-based (work backwards from end) ---
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            content = msg.content if isinstance(msg.content, str) else ""
            tokens = self._estimate_tokens(content)
            accumulated += tokens
            if accumulated >= tail_token_budget:
                tail_start = min(tail_start, i)
                break

        # --- Protected head: first 3 messages ---
        head_end = min(3, len(messages))

        # --- Boundary alignment: don't split tool_call/tool pairs ---
        for i in range(tail_start, len(messages)):
            msg = messages[i]
            if msg.role == "tool" and msg.tool_call_id:
                for j in range(i - 1, head_end - 1, -1):
                    prev = messages[j]
                    if prev.role == "assistant" and prev.tool_calls:
                        for tc in prev.tool_calls:
                            if tc.id == msg.tool_call_id and j < tail_start:
                                tail_start = j
                        break

        # --- Truncate long tool results in the middle zone ---
        for i in range(head_end, tail_start):
            msg = messages[i]
            if msg.role == "tool" and isinstance(msg.content, str) and len(msg.content) > 200:
                messages[i].content = "[Old tool output cleared to save context space]"

    def prune_history(self, messages: list[Message]) -> None:
        """Prune raw message history using token-based compression.

        Phase 1: truncate long old tool results when total estimated tokens
        exceed 50% of the token limit. (Future phases may add LLM-based
        summarization of old assistant messages.)
        """
        if self.config.strategy in ("none", "raw"):
            return
        self._compress_phase1(messages)

    def assemble(self, system_prompt: str, messages: list[Message]) -> list[Message]:
        """Build the final message list to send to the LLM.

        Order: system prompt → raw recent messages → memory block (pinned + episodic).

        Memory block is placed AFTER conversation history so that the system prompt
        + growing message history form a stable prefix for DeepSeek auto-prefix caching.
        When strategy is "raw", no memory block is injected.
        """
        if self.config.strategy == "raw":
            return [Message(role="system", content=system_prompt), *messages[1:]]

        result: list[Message] = []
        result.append(Message(role="system", content=system_prompt))
        result.extend(messages[1:])

        memory_text = self._render_memory_block()
        if memory_text:
            result.append(Message(role="user", content=memory_text))

        return result

    def _render_environment_view(self) -> str:
        """Environment layer. Override in subclasses."""
        return ""

    def _render_current_state(self) -> str:
        """State layer. Override in subclasses."""
        if not self._pinned_state:
            if self.config.pinned_enabled and self.pinned:
                return "\n".join(
                    f"- {k}: {v}" for k, v in self.pinned.items() if v is not None
                )
            return ""
        return ""

    def _render_memory_block(self) -> str:
        """Render layered context memory block.

        Layout: environment → current state.
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

        # Loaded skills and action history are removed from Context Memory.
        # Skills are already in conversation history as tool results from get_skill.
        # Action history is redundant with the assistant+tool messages in history.

        return "\n".join(lines)


class CoordinatorPinnedState(BaseModel):
    """Coordinator-side typed pinned state schema."""

    version: int = Field(default=1, ge=1)
    global_snapshot: dict = Field(default_factory=dict)
    semantic_summary: dict = Field(default_factory=dict)
    team_status_summary: dict = Field(default_factory=dict)
    state_mode: str = "semantic"
    step_budget: dict = Field(
        default_factory=lambda: {"current_step": 0, "max_steps": 0, "remaining": 0}
    )
    mission_finished: bool = False
    dispatched_tasks: list[dict] = Field(default_factory=list)
    worker_results: list[dict] = Field(default_factory=list)
    task_status_view: list[dict] = Field(default_factory=list)
    recent_changes: list[str] = Field(default_factory=list)
    supervision: dict = Field(default_factory=dict)


class CoordinatorContextManager(ContextManager):
    """Coordinator-side context manager for SAR orchestration."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
        state_provider: StateProvider | None = None,
    ):
        super().__init__(config, token_limit, log_dir, state_provider)
        self._pinned_state = CoordinatorPinnedState()
        self._pinned_state.state_mode = self.config.state_mode
        self.pinned = self._pinned_state.model_dump()

    def _render_environment_view(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return ""
        parts = []
        if ps.state_mode == "semantic":
            summary = ps.semantic_summary
            if summary:
                dynamic = summary.get("known_dynamic_objects", {})
                priors = summary.get("known_priors", {})
                fires = dynamic.get("fires", [])
                persons = dynamic.get("persons", [])
                reservoirs = priors.get("reservoirs", [])
                deposits = priors.get("deposits", [])
                parts.append(f"Known fires: {len(fires)}")
                parts.append(f"Known persons: {len(persons)}")
                parts.append(f"Known reservoirs: {len(reservoirs)}")
                parts.append(f"Known deposits: {len(deposits)}")
                if summary.get("stale_entries"):
                    parts.append(f"Stale entries: {len(summary['stale_entries'])}")
                if summary.get("conflicts"):
                    parts.append(f"Conflicts: {len(summary['conflicts'])}")
            team = ps.team_status_summary
            if team:
                parts.append(f"Workers: {len(team.get('workers', []))}")
        elif ps.state_mode == "oracle":
            if ps.global_snapshot:
                parts.append(
                    f"Environment at step {ps.global_snapshot.get('step', '?')}"
                )
                parts.append(ps.global_snapshot.get("summary", ""))
        return "\n".join(parts)

    def _render_current_state(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return super()._render_current_state()
        lines = []
        budget = ps.step_budget
        lines.append(
            f"- Step: {budget.get('current_step', 0)} / {budget.get('max_steps', 0)} "
            f"(remaining: {budget.get('remaining', 0)})"
        )
        lines.append(f"- Mission finished: {ps.mission_finished}")
        if ps.dispatched_tasks:
            lines.append(f"- Dispatched: {len(ps.dispatched_tasks)} tasks")
            for t in ps.dispatched_tasks[-3:]:
                lines.append(f"  - {t.get('agent_id')}: {t.get('task_id')}")
        if ps.task_status_view:
            lines.append(f"- Task status: {len(ps.task_status_view)} tasks")
            for tv in ps.task_status_view[-3:]:
                state = tv.get("state", "UNKNOWN")
                worker = tv.get("worker_id", "unknown")
                disp = tv.get("dispatch_id", "unknown")
                lines.append(f"  - {disp} ({worker}): {state}")
        if ps.recent_changes:
            lines.append("- Recent changes:")
            for change in ps.recent_changes[-3:]:
                lines.append(f"  - {change}")
        if ps.supervision:
            unack = ps.supervision.get("unacknowledged_events", [])
            alerts = ps.supervision.get("alerts", [])
            if unack or alerts:
                lines.append("- Supervision alerts:")
                for ev in unack[-5:]:
                    lines.append(
                        f"  - [{ev.get('event_type')}] dispatch={ev.get('dispatch_id')}: "
                        f"{ev.get('event_id')}"
                    )
        return "\n".join(lines)

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        """Extract coordinator-level state from tool results."""
        if not success or not content:
            return None

        updates: dict[str, Any] = {}

        if tool_name == "query_semantic_map":
            data = json.loads(content)
            if isinstance(data, dict):
                updates["semantic_summary"] = data
                if "step_budget" in data:
                    updates["step_budget"] = data["step_budget"]

        if tool_name == "query_team_status":
            data = json.loads(content)
            if isinstance(data, dict):
                updates["team_status_summary"] = data

        if tool_name == "query_sar_state":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                snapshot = {
                    k: v
                    for k, v in data.items()
                    if k not in ("step", "max_steps", "finished")
                }
                updates["global_snapshot"] = snapshot
                updates["step_budget"] = {
                    "current_step": data.get("step", 0),
                    "max_steps": data.get("max_steps", 0),
                    "remaining": max(0, data.get("max_steps", 0) - data.get("step", 0)),
                }
                updates["mission_finished"] = bool(data.get("finished", False))

        if tool_name == "dispatch_task":
            m = re.search(
                r"Task ['\"](?P<tid>[^'\"]+)['\"] dispatched to ['\"](?P<wid>[^'\"]+)['\"]",
                content,
            )
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
            updates["mission_finished"] = True

        return updates if updates else None
