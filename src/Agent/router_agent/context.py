"""Context management for router_agent.

Provides a three-tier memory model:
- Pinned state: structured key facts updated in real time
- Recent window: the last N raw assistant/tool messages kept unmodified

Compression:
- Phase 1 (cheap, no LLM): truncate long old tool results when tokens > 50% of limit
- Phase 3 (LLM-based): replace middle zone with structured summary when tokens > 80% of limit
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
    summary_trigger_ratio: float = (
        0.8  # 80% of token_limit triggers Phase 3 LLM compression
    )
    pinned_enabled: bool = True
    episodic_max_items: int = 20
    state_mode: str = "semantic"
    output_schema: str = ""  # Expected output format description for the LLM


class ContextManager:
    """Base context manager with pinned + recent-window memory."""

    # Phase 1 cheap compression threshold (fraction of token_limit)
    PHASE1_THRESHOLD: float = 0.50

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

        # Phase 3 compression: previous summary for iterative re-compression
        self._previous_summary: str | None = None
        self._load_compression_summary()

    def _snapshot_path(self, task_id: str) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / f"snapshot_{task_id}.json"

    def _compression_summary_path(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / "compression_summary.json"

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

    # ── Runtime State Injection ──────────────────────────────────────

    async def prepare_runtime_state(self, llm_client: Any) -> None:
        """Invoke optional async LLM preparation on the state provider.

        Only providers that implement ``AsyncStatePreparer`` will have
        ``prepare_for_llm()`` called.  Plain ``StateProvider``-only providers
        are no-ops.  Errors from a failing preparer are silently caught so
        the main LLM loop is never blocked.
        """
        if self._state_provider is None:
            return
        from .state_provider import AsyncStatePreparer

        if isinstance(self._state_provider, AsyncStatePreparer):
            try:
                await self._state_provider.prepare_for_llm(llm_client)
            except Exception:
                pass

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
            "map_revision",
            "map_delta",
            "map_summary",
            "map_summary_revision",
            "mission_dag_view",
            "physical_dispatches_view",
        ):
            if key in payload and hasattr(self._pinned_state, key):
                setattr(self._pinned_state, key, payload[key])

        # user_commands are drained per round: when the payload has none,
        # clear the pinned copy so stale commands never linger.
        if hasattr(self._pinned_state, "user_commands"):
            self._pinned_state.user_commands = payload.get("user_commands", [])

    # ── Token Estimation ─────────────────────────────────────────────

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

    # ── Phase 1: Cheap Tool Result Truncation (no LLM) ───────────────

    def _compress_phase1(self, messages: list[Message]) -> None:
        """Phase 1 token-based compression.

        When total estimated tokens exceed 50% of the token limit, replace
        long tool result contents in the middle zone (between protected head
        and protected tail) with a short placeholder. No messages are removed.
        """
        threshold_tokens = int(self.token_limit * self.PHASE1_THRESHOLD)
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
            if (
                msg.role == "tool"
                and isinstance(msg.content, str)
                and len(msg.content) > 200
            ):
                messages[i].content = "[Old tool output cleared to save context space]"

    # ── Phase 2: Determine Compression Boundaries ────────────────────

    def _determine_compression_boundaries(
        self, messages: list[Message]
    ) -> tuple[int, int]:
        """Determine (head_end, tail_start) indices for Phase 3 compression.

        Protected head: first 3 messages.
        Protected tail: last 15 messages + token-budget extension.
        Middle zone: everything between head and tail.
        Boundary alignment preserves tool_call/tool_result pairs.
        """
        # --- Protected head: first 3 messages ---
        head_end = min(3, len(messages))

        # --- Protected tail: count-based ---
        protect_last_n = 15
        tail_start = max(0, len(messages) - protect_last_n)

        # --- Protected tail: token-budget extension ---
        phase3_threshold = self.summary_trigger_tokens
        tail_token_budget = int(phase3_threshold * 0.20)

        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            content = msg.content if isinstance(msg.content, str) else ""
            tokens = self._estimate_tokens(content)
            accumulated += tokens
            if accumulated >= tail_token_budget:
                tail_start = min(tail_start, i)
                break

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

        return (head_end, tail_start)

    # ── Phase 3: LLM-Based Structured Summary ────────────────────────

    async def compress_with_llm(
        self,
        messages: list[Message],
        llm_client: Any,
    ) -> bool:
        """Run full Phase 1-4 LLM compression pipeline.

        Returns True if compression was applied, False otherwise.

        Hermes-style compression algorithm:
        1. Phase 1: truncate long old tool results (cheap cleanup)
        2. Phase 2: determine head/middle/tail boundaries
        3. Phase 3: call LLM to generate structured summary of middle zone
        4. Phase 4: replace middle zone with summary, clean tool pairs
        """
        total_tokens = self._estimate_messages_tokens(messages)
        if total_tokens < self.summary_trigger_tokens:
            return False

        # Phase 1: cheap tool result truncation first
        self._compress_phase1(messages)

        # Re-check threshold: Phase 1 may have reduced tokens below the
        # 80% trigger, making the expensive LLM call unnecessary.
        total_tokens = self._estimate_messages_tokens(messages)
        if total_tokens < self.summary_trigger_tokens:
            return False

        # Phase 2: determine boundaries
        head_end, tail_start = self._determine_compression_boundaries(messages)
        if head_end >= tail_start:
            return False

        # Extract middle zone for summarization
        middle_messages = messages[head_end:tail_start]
        if not middle_messages:
            return False

        # Phase 3: generate structured summary via LLM
        summary_text = await self._generate_compression_summary(
            middle_messages, llm_client
        )
        if not summary_text:
            return False

        # Phase 4: assemble compressed messages
        self._compress_phase4_assemble(messages, head_end, tail_start, summary_text)
        return True

    async def _generate_compression_summary(
        self,
        middle_messages: list[Message],
        llm_client: Any,
    ) -> str | None:
        """Generate a structured summary of the middle conversation zone.

        Uses a structured template following the Hermes Phase 3 pattern.
        Includes iterative re-compression support via _previous_summary.
        """
        # Build text representation of middle zone
        middle_text = ""
        for msg in middle_messages:
            role_label = msg.role.upper()
            if msg.tool_calls:
                tool_names = ", ".join(tc.function.name for tc in msg.tool_calls)
                content_preview = f"[Tool calls: {tool_names}]"
            elif isinstance(msg.content, str):
                preview = msg.content[:500]
                content_preview = preview.replace("\n", " ").strip()
            else:
                content_preview = "[complex content]"
            middle_text += f"[{role_label}] {content_preview}\n\n"

        # Estimate summary budget: content_tokens × 0.20, min 2000, max 12000
        middle_tokens = self._estimate_tokens(middle_text)
        summary_budget = max(2000, min(int(middle_tokens * 0.20), 12000))

        # Build instruction based on whether we have a previous summary
        if self._previous_summary:
            instruction = (
                "Update the existing conversation summary below with new information "
                "from the recent turns. Keep the same structured format. "
                "Preserve all completed work and add new progress. "
                "Remove or update information that is no longer relevant."
            )
            previous_block = f"\n## Existing Summary\n{self._previous_summary}\n"
        else:
            instruction = (
                "Create a concise, structured summary of the conversation so far."
            )
            previous_block = ""

        system_prompt = (
            "You are a SAR mission conversation compression assistant. "
            f"Keep the summary under approximately {summary_budget} tokens. "
            "Use this structured template:\n\n"
            "## 任务状态\n"
            "[Scene, step/max_steps, number of agents]\n\n"
            "## 环境指标\n"
            "[Coverage %, transport rate %, fires extinguished/total, persons rescued/total, active fires remaining]\n\n"
            "## 智能体状态\n"
            "[Each agent: current position, inventory (water/load), current task, status (active/done/timeout)]\n\n"
            "## 进度\n"
            "### 已完成\n"
            "[Tasks dispatched and completed, fires extinguished, persons found]\n"
            "### 进行中\n"
            "[Active tasks and which agent is executing them]\n"
            "### 阻塞项\n"
            "[Stuck agents, timeout agents, failed tasks, no-path situations]\n\n"
            "## 关键决策\n"
            "[Task allocation decisions, priority shifts, cancel/reassign decisions]\n\n"
            "## 权重提示\n"
            "[Remaining step budget vs remaining tasks — whether to rush or be thorough]"
        )

        user_prompt = (
            f"{instruction}\n"
            f"{previous_block}\n"
            f"## Recent Conversation to Summarize\n"
            f"{middle_text}\n\n"
            "Generate the structured summary now."
        )

        # Create Message objects for the LLM call
        summary_messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

        try:
            response = await llm_client.generate(messages=summary_messages)
            if response and response.content:
                self._previous_summary = response.content
                self._save_compression_summary()
                return response.content
        except Exception as e:
            print(f"⚠️ Phase 3 LLM compression failed: {e}")

        return None

    # ── Phase 4: Assemble Compressed Messages ────────────────────────

    def _compress_phase4_assemble(
        self,
        messages: list[Message],
        head_end: int,
        tail_start: int,
        summary_text: str,
    ) -> None:
        """Phase 4: Replace mid zone with structured summary and clean tool pairs.

        The summary is placed as an assistant message. Orphaned tool_call and
        tool_result pairs are cleaned up.
        """
        if head_end >= tail_start:
            return

        # Create summary message
        summary_msg = Message(
            role="assistant",
            content=(
                "[CONTEXT COMPACTION] Earlier turns have been compacted "
                "into a structured summary.\n\n"
                f"{summary_text}"
            ),
        )

        # Replace middle zone with summary message
        messages[head_end:tail_start] = [summary_msg]

        # Clean orphaned tool pairs
        self._sanitize_tool_pairs(messages)

    def _sanitize_tool_pairs(self, messages: list[Message]) -> None:
        """Remove orphaned tool results whose tool_call no longer exists.

        After Phase 4 replaces the middle zone with a summary, any tool
        result that referenced a tool_call now in the removed zone is
        cleaned up.
        """
        # Build set of active tool call IDs
        active_ids: set[str] = set()
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.id:
                        active_ids.add(tc.id)

        # Remove orphaned tool results in-place
        i = 0
        while i < len(messages):
            msg = messages[i]
            if (
                msg.role == "tool"
                and msg.tool_call_id
                and msg.tool_call_id not in active_ids
            ):
                messages.pop(i)
                continue
            i += 1

    # ── Summary Persistence ───────────────────────────────────────────

    def _save_compression_summary(self) -> None:
        """Persist the current compression summary to disk."""
        path = self._compression_summary_path()
        if path is None or not self._previous_summary:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"summary": self._previous_summary}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            pass

    def _load_compression_summary(self) -> None:
        """Load a previously persisted compression summary from disk."""
        path = self._compression_summary_path()
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                summary = data.get("summary", "")
                if summary:
                    self._previous_summary = summary
            except (OSError, json.JSONDecodeError):
                pass

    # ── Observe ──────────────────────────────────────────────────────

    def _extract_pinned(
        self, tool_name: str, content: str, success: bool
    ) -> dict[str, Any] | None:
        return None

    def observe(
        self, tool_name: str, content: str, success: bool, state_mode: str | None = None
    ) -> None:
        """Process a tool result: update pinned state."""
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

    # ── Prune History ────────────────────────────────────────────────

    def prune_history(self, messages: list[Message]) -> None:
        """Prune raw message history using token-based compression.

        Phase 1: truncate long old tool results when total estimated tokens
        exceed 50% of the token limit. (Future phases may add LLM-based
        summarization of old assistant messages.)

        Phase 3 (LLM-based, triggered separately via compress_with_llm)
        handles the case when tokens exceed 80% of the limit.
        """
        if self.config.strategy in ("none", "raw"):
            return
        self._compress_phase1(messages)

    # ── Assemble ─────────────────────────────────────────────────────

    def assemble(self, system_prompt: str, messages: list[Message]) -> list[Message]:
        """Build the final message list to send to the LLM.

        Order: system prompt → raw recent messages → memory block (pinned).

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

    # ── Render: Environment View ─────────────────────────────────────

    def _render_environment_view(self) -> str:
        """Environment layer. Override in subclasses."""
        return ""

    # ── Render: Current State ────────────────────────────────────────

    def _render_current_state(self) -> str:
        """State layer. Override in subclasses."""
        if not self._pinned_state:
            if self.config.pinned_enabled and self.pinned:
                return "\n".join(
                    f"- {k}: {v}" for k, v in self.pinned.items() if v is not None
                )
            return ""
        return ""

    # ── Render: Output Schema ────────────────────────────────────────

    def _render_output_schema(self) -> str:
        """Output format instructions for the LLM.

        Renders the configured output_schema if set. Override in subclasses
        to provide coordinator-specific guidance.
        """
        if self.config.output_schema:
            return self.config.output_schema
        return ""

    def _render_task_plan(self) -> str:
        """Task plan and progress. Override in subclasses."""
        return ""

    # ── Render: Memory Block ─────────────────────────────────────────

    def _render_memory_block(self) -> str:
        """Render layered context memory block.

        Layout: environment → current state → output schema.
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

        # Task plan & progress
        plan_text = self._render_task_plan()
        if plan_text:
            lines.append("### Task Plan & Progress")
            lines.append(plan_text)
            lines.append("---")

        # Output schema: instruct LLM on expected response format
        schema_text = self._render_output_schema()
        if schema_text:
            lines.append("### Output Format")
            lines.append(schema_text)
            lines.append("---")

        return "\n".join(lines)


# ── Coordinator-Specific Implementations ─────────────────────────────


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
    # Phase 6 — map continuity fields
    map_revision: int = 0
    map_delta: dict = Field(default_factory=dict)
    map_summary: str = Field(default_factory=str)
    map_summary_revision: int = 0
    # Phase 2 — Mission DAG + Physical Dispatches projection
    mission_dag_view: list[dict] = Field(default_factory=list)
    physical_dispatches_view: list[dict] = Field(default_factory=list)
    # Console UI — user commands injected mid-run (drained per round)
    user_commands: list[dict] = Field(default_factory=list)


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
        # Track state version to detect unchanged state across LLM rounds
        self._last_state_version: tuple[int, ...] | None = None

    def _render_environment_view(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return ""
        parts = []

        def _pos_str(pos):
            if pos and len(pos) >= 2:
                return f"({pos[0]},{pos[1]},{pos[2] if len(pos) > 2 else 0})"
            return ""

        def _region_str(region):
            # `attributes.regions` has no enforced schema: it's LLM-authored
            # (via report_observation's free-form `attributes` dict) or, for
            # map-agent-tool results, a list of {"name", "position"} dicts
            # (see sar_orch/map_agent/tools.py:_build_fire_result) -- never
            # guaranteed to be a list of strings.
            if isinstance(region, str):
                return region
            if isinstance(region, dict):
                return str(region.get("name") or region)
            return str(region)

        def _format_fire(f):
            name = f.get("name", "?")
            attrs = f.get("attributes", {}) or {}
            fire_type = attrs.get("type", "")
            intensity = attrs.get("intensity", "")
            pos = f.get("position")
            regions = attrs.get("regions", [])
            details = []
            if fire_type:
                details.append(fire_type)
            if intensity:
                details.append(f"{intensity} intensity")
            if pos:
                details.append(f"at {_pos_str(pos)}")
            if regions:
                if not isinstance(regions, list):
                    regions = [regions]
                details.append(f"[{', '.join(_region_str(r) for r in regions)}]")
            return f"  - {name}: {', '.join(details)}"

        def _format_person(p):
            name = p.get("name", "?")
            pos = p.get("position")
            status = p.get("status", "")
            details = []
            if status and status not in ("unknown",):
                details.append(status)
            if pos:
                details.append(f"at {_pos_str(pos)}")
            suffix = " ".join(details)
            return f"  - {name}: {suffix}" if suffix else f"  - {name}"

        def _format_reservoir(r):
            name = r.get("name", "?")
            attrs = r.get("attributes", {}) or {}
            supply_type = attrs.get("supply_type", "")
            pos = r.get("position")
            details = []
            if supply_type:
                details.append(supply_type)
            if pos:
                details.append(f"at {_pos_str(pos)}")
            suffix = " ".join(details)
            return f"  - {name}: {suffix}" if suffix else f"  - {name}"

        def _format_deposit(d):
            name = d.get("name", "?")
            pos = d.get("position")
            if pos:
                return f"  - {name} at {_pos_str(pos)}"
            return f"  - {name}"

        def _format_agent(a):
            name = a.get("agent_id", "?")
            pos = a.get("last_position")
            inv = a.get("inventory", {})
            task_id = a.get("current_task_id", "")
            task_state = a.get("task_state", "")
            detail_parts = []
            if pos:
                detail_parts.append(f"at {_pos_str(pos)}")
            if inv:
                inv_str = " | ".join(f"{k}:{v}" for k, v in inv.items())
                detail_parts.append(inv_str)
            if task_id:
                detail_parts.append(f"task: {task_id} ({task_state})")
            return f"  - {name}: {' | '.join(detail_parts)}"

        if ps.state_mode == "semantic":
            summary = ps.semantic_summary
            if summary:
                dynamic = summary.get("known_dynamic_objects", {})
                priors = summary.get("known_priors", {})
                fires = dynamic.get("fires", [])
                persons = dynamic.get("persons", [])
                reservoirs = priors.get("reservoirs", [])
                deposits = priors.get("deposits", [])
                workers_list = ps.team_status_summary.get("workers", [])

                if fires:
                    parts.append(f"Known fires: {len(fires)}")
                    for f in fires:
                        parts.append(_format_fire(f))
                if persons:
                    parts.append(f"Known persons: {len(persons)}")
                    for p in persons:
                        parts.append(_format_person(p))
                if reservoirs:
                    parts.append(f"Known reservoirs: {len(reservoirs)}")
                    for r in reservoirs:
                        parts.append(_format_reservoir(r))
                if deposits:
                    parts.append(f"Known deposits: {len(deposits)}")
                    for d in deposits:
                        parts.append(_format_deposit(d))
                if workers_list:
                    parts.append(f"Workers: {len(workers_list)}")
                    for a in workers_list:
                        parts.append(_format_agent(a))

                if summary.get("stale_entries"):
                    parts.append(f"Stale entries: {len(summary['stale_entries'])}")
                if summary.get("conflicts"):
                    parts.append(f"Conflicts: {len(summary['conflicts'])}")

            # Phase 6: render map delta (nonempty, semantic mode only)
            md = ps.map_delta
            if md and md.get("change_count", 0) > 0:
                map_rev = ps.map_revision
                parts.append(f"### Map Changes (revision {map_rev})")
                change_lines = []
                # Priority 1: person terminal
                persons_delta = md.get("persons", {})
                for entry in persons_delta.get("status_changed", []):
                    name = entry.get("name", "?")
                    status = entry.get("new")
                    if str(status).lower() in {"rescued", "extinguished", "complete"}:
                        change_lines.append(f"  - {name} marked {status}")
                # Priority 2: fire intensity/status
                fires_delta = md.get("fires", {})
                for entry in fires_delta.get("intensity_changed", []):
                    name = entry.get("name", "?")
                    old_i = entry.get("old", "")
                    new_i = entry.get("new", "")
                    if old_i and new_i:
                        change_lines.append(f"  - {name} intensity {old_i} → {new_i}")
                    elif new_i:
                        change_lines.append(f"  - {name} intensity now {new_i}")
                for entry in fires_delta.get("status_changed", []):
                    name = entry.get("name", "?")
                    new_s = entry.get("new", "")
                    old_s = entry.get("old", "")
                    if old_s and new_s:
                        change_lines.append(f"  - {name} {old_s} → {new_s}")
                    elif new_s:
                        change_lines.append(f"  - {name} now {new_s}")
                # Priority 3: gained entries
                for entry in fires_delta.get("gained", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - {name} newly detected")
                for entry in persons_delta.get("gained", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - {name} newly detected")
                # Priority 4: conflict/stale
                for entry in md.get("conflicts_new", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - Conflict: {name}")
                for entry in md.get("conflicts_resolved", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - Conflict resolved: {name}")
                for entry in md.get("stale_new", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - Stale: {name}")
                for entry in md.get("stale_resolved", []):
                    name = entry.get("name", "?")
                    change_lines.append(f"  - Stale resolved: {name}")
                # Cap at 5 entries, then show overflow count
                total = len(change_lines)
                for line in change_lines[:5]:
                    parts.append(line)
                if total > 5:
                    parts.append(f"  ... and {total - 5} more changes")
        return "\n".join(parts)

    def _render_current_state(self) -> str:
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return super()._render_current_state()
        lines = []
        budget = ps.step_budget
        current_step = budget.get("current_step", 0)
        lines.append(
            f"- Step: {current_step} / {budget.get('max_steps', 0)} "
            f"(remaining: {budget.get('remaining', 0)})"
        )
        lines.append(f"- Mission finished: {ps.mission_finished}")
        state_digest = [current_step, ps.map_revision, ps.map_summary_revision]

        # Console UI: user commands injected mid-run (highest priority)
        if ps.user_commands:
            lines.append("### User Commands")
            lines.append(
                "  Direct instructions from the human operator. Treat them as "
                "high-priority input and incorporate them into your planning."
            )
            for cmd in ps.user_commands:
                text = cmd.get("text", "") if isinstance(cmd, dict) else str(cmd)
                queued_at = cmd.get("queued_at", "") if isinstance(cmd, dict) else ""
                lines.append(f"  - [{queued_at}] {text}")
            state_digest.append(tuple(str(c) for c in ps.user_commands))

        # Phase 2: render Mission DAG (structured logical graph view)
        dag = ps.mission_dag_view
        if dag:
            lines.append("### Mission DAG")
            lines.append(f"  Nodes: {len(dag)}")
            dag_digest: list[tuple] = []
            for entry in dag:
                dag_digest.append(
                    (
                        entry["logical_id"],
                        entry["state"],
                        tuple(entry.get("participant_ids", [])),
                        tuple(entry.get("depends_on", [])),
                        entry.get("objective", ""),
                        entry.get("failure_reason", ""),
                    )
                )
            state_digest.append(tuple(dag_digest))
            for entry in dag:
                lid = entry["logical_id"]
                st = entry["state"]
                participants = ", ".join(entry["participant_ids"])
                deps = (
                    ", ".join(entry["depends_on"]) if entry["depends_on"] else "(none)"
                )
                obj = entry.get("objective", "")
                fail = entry.get("failure_reason", "")
                parts = [
                    f"  - {lid} [{st}] participants=[{participants}] deps=[{deps}]"
                ]
                if obj:
                    parts.append(f"    objective: {obj}")
                if fail:
                    parts.append(f"    failure: {fail}")
                lines.extend(parts)

        # Phase 2: render Physical Dispatches (structured physical view)
        phys = ps.physical_dispatches_view
        if phys:
            lines.append("### Physical Dispatches")
            lines.append("  (result/artifact below are truncated previews; call query_task_results for full results)")
            lines.append(f"  Records: {len(phys)}")
            phys_digest: list[tuple] = []
            for entry in phys:
                phys_digest.append(
                    (
                        entry["dispatch_id"],
                        entry["logical_node_id"],
                        entry["worker_id"],
                        entry.get("worker_task_id", ""),
                        entry["state"],
                        entry.get("result_preview", ""),
                        entry.get("artifact_preview", ""),
                    )
                )
            state_digest.append(tuple(phys_digest))
            for entry in phys:
                did = entry["dispatch_id"]
                wid = entry["worker_id"]
                st = entry["state"]
                wtid = entry.get("worker_task_id", "")
                result_preview = entry.get("result_preview", "")
                artifact_preview = entry.get("artifact_preview", "")
                parts = [f"  - {did} -> {wid} [{st}] worker_task={wtid}"]
                if result_preview:
                    parts.append(f"    result: {result_preview}")
                if artifact_preview:
                    parts.append(f"    artifact: {artifact_preview}")
                lines.extend(parts)

        # Phase 6: render map summary (nonempty, semantic mode only)
        if ps.state_mode == "semantic" and ps.map_summary:
            lines.append(f"### Map Summary (revision {ps.map_summary_revision})")
            lines.append(ps.map_summary[:150])
        if ps.dispatched_tasks:
            lines.append(f"- Dispatched: {len(ps.dispatched_tasks)} tasks")
            state_digest.append(len(ps.dispatched_tasks))
            for t in ps.dispatched_tasks[-3:]:
                lines.append(f"  - {t.get('agent_id')}: {t.get('task_id')}")
        if ps.recent_changes:
            lines.append("- Recent changes:")
            state_digest.append(len(ps.recent_changes))
            for change in ps.recent_changes[-3:]:
                lines.append(f"  - {change}")
        if ps.supervision:
            unack = ps.supervision.get("unacknowledged_events", [])
            alerts = ps.supervision.get("alerts", [])
            if unack or alerts:
                lines.append("- Supervision alerts:")
                state_digest.append(len(unack) + len(alerts))
                for ev in unack[-5:]:
                    lines.append(
                        f"  - [{ev.get('event_type')}] dispatch={ev.get('dispatch_id')}: "
                        f"{ev.get('event_id')}"
                    )
        # State-unchanged marker: only when ALL dimensions match
        digest = tuple(state_digest)
        if digest == self._last_state_version and self._last_state_version is not None:
            lines.append("- State unchanged since last round")
        self._last_state_version = digest
        return "\n".join(lines)

    def _render_output_schema(self) -> str:
        """Coordinator-specific output format instructions."""
        if self.config.output_schema:
            return self.config.output_schema
        return (
            "Respond with ONE tool call per turn. "
            "Use send_message(message_type='assign_task', ...) to dispatch, "
            "send_message(message_type='cancel_task', ...) to cancel, "
            "send_message(message_type='reply_to_help', ...) to respond, "
            "query_task_events(...) to check status (use timeout>0 to wait), "
            "or update_plan(...) to declare the mission plan."
        )

    def _render_task_plan(self) -> str:
        """Render task plan and progress from task_status_view."""
        ps = self._pinned_state
        if not isinstance(ps, CoordinatorPinnedState):
            return ""
        views = ps.task_status_view
        if not views:
            return ""
        planned = []
        active = []
        completed = []
        failed = []
        for v in views:
            state = v.get("state", "UNKNOWN")
            if state in ("pending",):
                planned.append(v)
            elif state in ("assigned", "running", "INPUT_REQUIRED"):
                active.append(v)
            elif state in ("completed", "success"):
                completed.append(v)
            elif state in ("failed", "cancelled", "error"):
                failed.append(v)
            else:
                active.append(v)
        lines = [f"Total tasks: {len(views)}"]
        if planned:
            lines.append(f"- Planned: {len(planned)}")
            for v in planned:
                lines.append(f"  ⏳ {v['dispatch_id']} → {v['worker_id']}")
        if active:
            lines.append(f"- Active: {len(active)}")
            for v in active:
                icon = "🆘" if v.get("help_request") else "▶️"
                label = f"{icon} {v['dispatch_id']} ({v['worker_id']}): {v['state']}"
                lines.append(f"  {label}")
                if v.get("help_request"):
                    lines.append(f"    ⚠️ {v['help_request'][:120]}")
        if completed:
            lines.append(f"- Completed: {len(completed)}")
            for v in completed:
                lines.append(f"  ✅ {v['dispatch_id']} ({v['worker_id']})")
        if failed:
            lines.append(f"- Failed: {len(failed)}")
            for v in failed:
                lines.append(f"  ❌ {v['dispatch_id']}: {v['state']}")
        # Supervision alerts for active tasks
        sup = ps.supervision
        if sup:
            alerts = sup.get("alerts", [])
            unack = sup.get("unacknowledged_events", [])
            if alerts or unack:
                lines.append("- Supervision:")
                for a in alerts[:3]:
                    did = a.get("dispatch_id", "?")
                    aws = a.get("active_alerts", {})
                    lines.append(
                        f"  ⚠️ {did}: {dict(aws) if isinstance(aws, dict) else aws}"
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
                # Legacy tool responses include agents in the semantic map,
                # while runtime rendering reads workers exclusively from the
                # team-status view.  Preserve the old tool-only UI without
                # treating stale map positions/inventory as live data.
                agents = data.get("agents", [])
                if isinstance(agents, list):
                    updates["team_status_summary"] = {
                        "workers": [
                            {
                                "agent_id": agent.get("agent_id", "unknown"),
                                "task_state": agent.get("state", "UNKNOWN"),
                            }
                            for agent in agents
                            if isinstance(agent, dict)
                        ]
                    }

        if tool_name == "query_team_status":
            data = json.loads(content)
            if isinstance(data, dict):
                updates["team_status_summary"] = data

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
