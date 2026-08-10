"""Context management for worker_agent.

Provides a three-tier memory model:
- Pinned state: structured key facts updated in real time (position, inventory, ...)
- Episodic memory: summarized past tool episodes
- Recent window: the last N raw assistant/tool messages kept unmodified

Compression:
- Phase 1 (cheap, no LLM): truncate long old tool results when tokens > 50% of limit
- Phase 3 (LLM-based): replace middle zone with structured summary when tokens > 80% of limit
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from .schema import Message

if TYPE_CHECKING:
    from Agent.router_agent.state_provider import RuntimeState, StateProvider

#: Marker rendered in place of a persisted skill whose source file can no
#: longer be re-read with a matching digest.  Old skill content is never
#: copied into snapshots as domain data.
SKILL_RELOAD_REQUIRED = "SKILL_RELOAD_REQUIRED"

#: Rendered heading of the trailing role=user state block.
ENVIRONMENT_STATE_HEADING = "## Environment State"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_skill_root(rendered_content: str) -> Path | None:
    """Extract the ``**Skill Root Directory:** `...` `` path from rendered content."""
    m = re.search(r"\*\*Skill Root Directory:\*\*\s*`([^`]+)`", rendered_content)
    if not m:
        return None
    return Path(m.group(1))


@dataclass(frozen=True)
class LoadedSkillRef:
    """A loaded-skill reference persisted in a ContextSnapshotV2.

    Only the name, the canonical source path (relative to the configured skill
    root) and a content digest are stored.  Skill content is never serialized;
    on restore it is reloaded only from the configured root when the path and
    digest both match.
    """

    name: str
    source_relative_path: str = ""
    content_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_relative_path": self.source_relative_path,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoadedSkillRef":
        return cls(
            name=str(data.get("name", "")),
            source_relative_path=str(data.get("source_relative_path", "")),
            content_sha256=str(data.get("content_sha256", "")),
        )


@dataclass(frozen=True)
class ContextSessionCursor:
    """Temporal cursor for a ``(scope_id, viewer_id)`` session namespace.

    ContextSnapshotV2 persists exactly this minimal cursor — never pinned,
    RuntimeState payload, or any domain projection.  It is only advanced
    monotonically via ``ContextManager.next_cursor()``.  A snapshot that lacks a
    cursor resumes at sequence=0 for the current scope and never infers a
    cursor across scopes.
    """

    scope_id: str
    viewer_id: str
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "viewer_id": self.viewer_id,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextSessionCursor":
        return cls(
            scope_id=str(data.get("scope_id", "")),
            viewer_id=str(data.get("viewer_id", "")),
            sequence=int(data.get("sequence", 0) or 0),
        )


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
    #: Read-path feature flag.  Defaults to ``read_port`` since the H3
    #: retirement approval (2026-08-10); ``legacy`` is retained as the
    #: rollback target and stays available.
    memory_read_mode: str = "read_port"


@dataclass
class _Episode:
    """Single summarized episode."""

    step: int = 0
    tool_name: str = ""
    summary: str = ""


class ContextManager:
    """Base context manager with pinned + episodic + recent-window memory."""

    # Phase 1 cheap compression threshold (fraction of token_limit)
    PHASE1_THRESHOLD: float = 0.50

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
        state_provider: "StateProvider | None" = None,
        skills_dir: str | Path | None = None,
    ):
        self.config = config or ContextConfig()
        self.token_limit = token_limit
        self.summary_trigger_tokens = int(
            token_limit * self.config.summary_trigger_ratio
        )
        self._log_dir = Path(log_dir) if log_dir else None
        self._skills_dir = Path(skills_dir) if skills_dir else None

        # Pinned: structured state updated on every tool observation
        self.pinned: dict[str, Any] = {}
        self._pinned_state: BaseModel | None = (
            None  # typed version, replace `pinned` over time
        )
        # Episodic: list of summarized episodes
        self.episodic: list[_Episode] = []
        # Step counter for episode ordering
        self._episode_counter: int = 0
        # Task snapshots: task_id -> (messages, loaded_skill_refs, cursor) (for pause/resume)
        self._task_snapshots: dict[
            str, tuple[list[Message], list[LoadedSkillRef], ContextSessionCursor | None]
        ] = {}

        # Temporal cursor state keyed by (scope_id, viewer_id). Only advanced
        # monotonically via next_cursor(); snapshots persist ContextSessionCursor.
        self._cursor_sequences: dict[tuple[str, str], int] = {}

        # Runtime state provider: system-injected state refreshed before each
        # LLM request. ContextManager does not directly import SAR backends.
        self._state_provider: StateProvider | None = state_provider
        self._runtime_state: RuntimeState | None = None

        # Loaded skills: content loaded via get_skill tool, persisted across turns.
        # Snapshot persistence stores only LoadedSkillRef (ContextSnapshotV2).
        self._loaded_skills: dict[str, str] = {}
        self._loaded_skill_refs: dict[str, LoadedSkillRef] = {}

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

    def _build_skill_ref(self, name: str, content: str) -> LoadedSkillRef:
        """Build a ContextSnapshotV2 skill ref from rendered skill content."""
        root = _extract_skill_root(content)
        source_relative = ""
        if root is not None:
            source = root / "SKILL.md"
            source_relative = self._relative_skill_path(source)
        return LoadedSkillRef(
            name=name,
            source_relative_path=source_relative,
            content_sha256=_sha256_hex(content),
        )

    def _relative_skill_path(self, source: Path) -> str:
        """Canonical relative source path under the configured skill root.

        Absolute source paths are never stored.  When no skill root is configured
        or the loaded source resolves outside the root, an empty path is returned
        so the ref renders SKILL_RELOAD_REQUIRED on restore.
        """
        if self._skills_dir is None:
            return ""
        try:
            return str(source.resolve().relative_to(Path(self._skills_dir).resolve()))
        except ValueError:
            return ""

    def _reload_skill_content(self, ref: LoadedSkillRef) -> str | None:
        """Reload a skill from the configured root only when path + digest match.

        Rejects absolute source paths, ``..`` traversal, and any resolved
        candidate that escapes the configured skill root.  The reloaded content
        must re-hash to the stored digest, otherwise the caller renders
        SKILL_RELOAD_REQUIRED.
        """
        if self._skills_dir is None:
            return None
        rel = ref.source_relative_path
        if not rel:
            return None
        rel_path = Path(rel)
        if rel_path.is_absolute():
            return None
        if ".." in rel_path.parts:
            return None
        root = Path(self._skills_dir).resolve()
        candidate = (root / rel_path).resolve()
        if root not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        try:
            from .tools.skill_loader import SkillLoader

            loader = SkillLoader(skills_dir=str(self._skills_dir))
            skill = loader.load_skill(candidate)
        except Exception:
            return None
        if skill is None:
            return None
        rendered = skill.to_prompt()
        if _sha256_hex(rendered) != ref.content_sha256:
            return None
        return rendered

    def _restore_loaded_skills(self, skill_refs: list[LoadedSkillRef]) -> None:
        """Restore loaded skills from refs; unverifiable skills render the marker."""
        for ref in skill_refs:
            content = self._reload_skill_content(ref)
            if content is None:
                content = SKILL_RELOAD_REQUIRED
            self._loaded_skills[ref.name] = content
            self._loaded_skill_refs[ref.name] = ref

    def on_skill_loaded(self, name: str, content: str) -> None:
        """Register a loaded skill for persistence across turns in the memory block."""
        self._loaded_skills[name] = content
        self._loaded_skill_refs[name] = self._build_skill_ref(name, content)

    # ── Temporal cursor (ContextSession) ──────────────────────────────

    def get_cursor(self, scope_id: str, viewer_id: str) -> int:
        """Return the current temporal cursor sequence for a session namespace.

        Returns 0 for any namespace with no recorded cursor — a cursor is never
        inferred across scopes.
        """
        return self._cursor_sequences.get((scope_id, viewer_id), 0)

    def next_cursor(self, scope_id: str, viewer_id: str) -> int:
        """Monotonically advance the temporal cursor for a session namespace.

        The cursor is only ever advanced by this API; scope/epoch changes reset
        to a fresh namespace (sequence starts at 0 again) rather than reusing an
        old cursor.
        """
        key = (scope_id, viewer_id)
        sequence = self._cursor_sequences.get(key, 0) + 1
        self._cursor_sequences[key] = sequence
        return sequence

    def _restore_cursor(self, cursor: ContextSessionCursor | None) -> None:
        """Restore a persisted cursor, preserving monotonicity within its namespace."""
        if cursor is None:
            return
        key = (cursor.scope_id, cursor.viewer_id)
        self._cursor_sequences[key] = max(
            self._cursor_sequences.get(key, 0), cursor.sequence
        )

    def save_snapshot(
        self,
        task_id: str,
        messages: list,
        scope_id: str = "",
        viewer_id: str = "",
    ) -> None:
        """Save a full messages snapshot for later resume (memory + optional disk).

        ContextSnapshotV2: pinned / RuntimeState are never serialized; loaded
        skills persist only as name + canonical relative source path + sha256;
        the ContextSession temporal cursor for ``(scope_id, viewer_id)`` is
        persisted with the snapshot.
        """
        cursor = ContextSessionCursor(
            scope_id=scope_id,
            viewer_id=viewer_id,
            sequence=self.get_cursor(scope_id, viewer_id),
        )
        self._task_snapshots[task_id] = (
            copy.deepcopy(messages),
            list(self._loaded_skill_refs.values()),
            cursor,
        )
        path = self._snapshot_path(task_id)
        if path is not None:
            try:
                os.makedirs(path.parent, exist_ok=True)
                payload = {
                    "version": 2,
                    "cursor": cursor.to_dict(),
                    "loaded_skills": [
                        ref.to_dict() for ref in self._loaded_skill_refs.values()
                    ],
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
            msgs, skill_refs, cursor = self._task_snapshots.pop(task_id)
            self._restore_loaded_skills(skill_refs)
            self._restore_cursor(cursor)
            return msgs

        # Fall back to disk
        path = self._snapshot_path(task_id)
        if path is not None and path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                os.remove(path)
                if isinstance(payload, dict) and "messages" in payload:
                    refs = [
                        LoadedSkillRef.from_dict(r)
                        for r in payload.get("loaded_skills", [])
                        if isinstance(r, dict)
                    ]
                    self._restore_loaded_skills(refs)
                    cursor_raw = payload.get("cursor")
                    if isinstance(cursor_raw, dict):
                        self._restore_cursor(ContextSessionCursor.from_dict(cursor_raw))
                    return [Message.model_validate(m) for m in payload["messages"]]
                # backward compat: old format was a flat array
                return [Message.model_validate(m) for m in payload]
            except (OSError, json.JSONDecodeError):
                pass

        return None

    # ── Runtime State Injection ──────────────────────────────────────

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
            "current_task",
        ):
            if key in payload and hasattr(self._pinned_state, key):
                setattr(self._pinned_state, key, payload[key])

    # ── Token Estimation ─────────────────────────────────────────────

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken if available, else char-based."""
        if not text:
            return 0
        try:
            import tiktoken

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
        """Phase 1 token-based compression — cheap, no LLM call.

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
            "## 任务目标\n"
            "[My assigned role in the SAR mission, current task description]\n\n"
            "## 当前位置与状态\n"
            "[Current position (x,y,z), inventory (water load, carried items), step count]\n\n"
            "## 进度\n"
            "### 已完成\n"
            "[Positions explored, fires extinguished, persons found and reported, debris cleared]\n"
            "### 进行中\n"
            "[Current task being executed — navigate to, extinguish, search, report]\n"
            "### 阻塞项\n"
            "[No-path to target, tool failures, task cancelled, waiting for new task]\n\n"
            "## 关键发现\n"
            "[Important observations: fire locations with intensity, person locations, blocked paths, hazardous areas]\n\n"
            "## 下一步\n"
            "[Planned actions: which direction to go, what to do at destination]"
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

    # ── Prune History ────────────────────────────────────────────────

    def prune_history(self, messages: list[Message]) -> None:
        """Prune raw message history using token-based compression.

        Phase 1: truncate long old tool results when total estimated tokens
        exceed 50% of the token limit. Then apply count-based pruning to
        keep the message window bounded, preserving episodic accumulation.

        Phase 3 (LLM-based, triggered separately) handles the case when
        tokens exceed 80% of the limit.
        """
        if self.config.strategy in ("none", "raw"):
            return

        # Phase 1: cheap token-based truncation
        self._compress_phase1(messages)

        # Count-based prune: keep recent window bounded, summarize old messages
        recent = self.config.recent_messages
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

    # ── Assemble ─────────────────────────────────────────────────────

    def _build_stable_system_prompt(self, system_prompt: str) -> str:
        """Append the output contract to the stable system prompt.

        ``ContextConfig.output_schema`` is part of the stable system prompt
        construction, never the trailing role=user state block.  If the system
        prompt already carries the contract (e.g. folded in at Agent build
        time), it is left untouched.
        """
        schema_text = self._render_output_schema()
        if not schema_text:
            return system_prompt
        if "## Output / Response Contract" in system_prompt:
            return system_prompt
        return (
            f"{system_prompt.rstrip()}\n\n## Output / Response Contract\n{schema_text}"
        )

    @staticmethod
    def _has_unclosed_tool_call(messages: list[Message]) -> bool:
        """True when the most recent assistant turn still has an unanswered tool call.

        Mirrors the controller/NeedInput closure contract: an assistant tool_call
        that has no matching tool result must be closed (by the controller resume
        path) before the Environment State block may be appended.
        """
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.role == "assistant" and msg.tool_calls:
                answered = {
                    m.tool_call_id for m in messages[i + 1 :] if m.role == "tool"
                }
                return any(tc.id and tc.id not in answered for tc in msg.tool_calls)
            if msg.role != "tool":
                break
        return False

    def assemble(self, system_prompt: str, messages: list[Message]) -> list[Message]:
        """Build the final message list to send to the LLM.

        Order: stable system prompt (with output contract) → raw recent messages
        → Environment State block (role=user, pinned + episodic).

        The Environment State block is placed AFTER conversation history so that
        the system prompt + growing message history form a stable prefix for
        DeepSeek auto-prefix caching.  When strategy is "raw", no Environment State
        block is injected — messages pass through as-is.  If the most recent
        assistant turn still has an unclosed tool call, the Environment State append
        is rejected and the history passes through as-is (the controller/NeedInput
        resume path closes the protocol).
        """
        if self.config.strategy == "raw":
            return [
                Message(
                    role="system",
                    content=self._build_stable_system_prompt(system_prompt),
                ),
                *messages[1:],
            ]

        result: list[Message] = []
        result.append(
            Message(
                role="system", content=self._build_stable_system_prompt(system_prompt)
            )
        )
        result.extend(messages[1:])  # skip original system prompt if present

        memory_text = self._render_memory_block()
        if memory_text and not self._has_unclosed_tool_call(messages):
            result.append(Message(role="user", content=memory_text))

        return result

    # ── Read-port path (Phase 4) ─────────────────────────────────────

    def _render_read_port_block(self) -> str:
        """Render the Environment State block from the read-port provider.

        Used when ``memory_read_mode == "read_port"`` and the injected state
        provider exposes the generic ``query_environment_state`` protocol.  The
        renderer stays pure; ACL filtering happens inside the provider.  The
        temporal cursor is read from the ContextSession namespace keyed by
        ``(scope_id, viewer_id)`` and advanced monotonically to the view's
        ``next_cursor``.  On scope change the namespace is fresh (reset=0).

        On provider failure (UNAVAILABLE / STALE / exception) the read-port
        path triggers a read_port→legacy rollback latch (once) and returns an
        empty string so ``_render_memory_block`` falls through to the legacy
        pinned rendering — never mixing canonical and legacy truth in one view.
        """
        from Agent.environment_state import (
            NEXT_CURSOR_KEY,
            EnvironmentStateQuery,
            Freshness,
            render_environment_state_view,
        )

        provider = self._state_provider
        query_fn = getattr(provider, "query_environment_state", None)
        if query_fn is None:
            return ""
        scope_id = getattr(provider, "scope_id", "")
        viewer_id = getattr(provider, "viewer_id", "system")
        viewer_role = getattr(provider, "viewer_role", "coordinator")
        current_dispatch_id = getattr(provider, "current_dispatch_id", None)

        cursor = self.get_cursor(scope_id, viewer_id)
        query = EnvironmentStateQuery(
            scope_id=scope_id,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            current_dispatch_id=current_dispatch_id,
            temporal_cursor=cursor,
            token_budget=self._read_port_token_budget(),
        )
        try:
            view = query_fn(query)
        except Exception as exc:  # noqa: BLE001 - rollback, never break pre-LLM
            self._trigger_read_port_rollback(f"provider_error: {exc}")
            return ""
        if view.freshness is not Freshness.FRESH:
            # A pending-admission deferral is the startup dispatch-binding race:
            # the coordinator has not yet bound this worker's server-issued task
            # id, and the binding lands milliseconds later.  Never trip the
            # permanent read_port→legacy rollback for startup ordering — fall
            # back to legacy for THIS request only and let the next pre_llm
            # fetch retry the read-port path.  All genuine failures still latch.
            if str(view.reason or "") == "environment_state_pending_admission":
                return ""
            self._trigger_read_port_rollback(f"{view.freshness.value}: {view.reason}")
            return ""
        next_cursor = int(view.sections.get(NEXT_CURSOR_KEY, cursor) or 0)
        if next_cursor > cursor:
            self._cursor_sequences[(scope_id, viewer_id)] = next_cursor
        return render_environment_state_view(view)

    def _trigger_read_port_rollback(self, reason: str) -> None:
        """Latch the read_port→legacy rollback on the provider (once)."""
        provider = self._state_provider
        trigger = getattr(provider, "rollback_environment_state", None)
        if trigger is not None:
            trigger(reason)

    def _read_port_token_budget(self) -> int:
        """Token budget for the read-port state block (design §6)."""
        return max(0, self.token_limit - 1024)

    # ── Render: Environment View ─────────────────────────────────────

    def _render_environment_view(self) -> str:
        """Environment layer: fires, persons, etc. Override in subclasses."""
        return ""

    # ── Render: Current State ────────────────────────────────────────

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

    # ── Render: Output Schema ────────────────────────────────────────

    def _render_output_schema(self) -> str:
        """Output format instructions for the LLM.

        Renders the configured output_schema if set. Override in subclasses
        to provide tool-specific guidance.
        """
        if self.config.output_schema:
            return self.config.output_schema
        return ""

    def _render_task_plan(self) -> str:
        """Render worker's current task plan and progress."""
        ps = self._pinned_state
        if not isinstance(ps, WorkerPinnedState):
            return ""
        lines = []
        ct = ps.current_task
        if ct:
            desc = ct.get("description", ct.get("task_id", "?"))
            status = ct.get("status", "assigned")
            lines.append(f"- Current task: {desc}")
            lines.append(f"  Status: {status}")
            if "progress" in ct:
                lines.append(f"  Progress: {ct['progress']}%")
            if "result" in ct and ct["result"]:
                lines.append(f"  Result: {ct['result'][:200]}")
        lines.append(f"- Mission: {ps.mission_status}")
        if ps.position:
            lines.append(
                f"- Position: ({ps.position[0]}, {ps.position[1]}, {ps.position[2]})"
            )
            lines.append(f"- Step: {ps.step}")
        return "\n".join(lines) if lines else ""

    # ── Render: Memory Block ─────────────────────────────────────────

    def _render_memory_block(self) -> str:
        """Render the Environment State block (role=user state projection).

        When ``memory_read_mode == "read_port"`` and the injected state
        provider exposes ``query_environment_state``, the block is rendered
        from the canonical read-port view (ACL applied by the provider).  The
        legacy pinned render path is used otherwise — including after a
        read_port→legacy rollback latch has been tripped.
        """
        if self.config.memory_read_mode == "read_port":
            provider = self._state_provider
            rollout_active = getattr(provider, "rollout_active", None)
            if rollout_active is None or rollout_active():
                read_port_text = self._render_read_port_block()
                if read_port_text:
                    return read_port_text
        lines: list[str] = ["---", ENVIRONMENT_STATE_HEADING, "---"]

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

        return "\n".join(lines)


# ── Worker-Specific Implementations ──────────────────────────────────


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
    current_task: dict | None = Field(default=None)


class WorkerContextManager(ContextManager):
    """Worker-side context manager for SAR tasks."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
        state_provider: "StateProvider | None" = None,
        skills_dir: str | Path | None = None,
    ):
        super().__init__(config, token_limit, log_dir, state_provider, skills_dir)
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

    def _render_team_coordination(self) -> str:
        """Render team coordination block from runtime state.

        Shows teammate positions, inventory, tasks. Injected as a dedicated
        section in the memory block when team status data is available.
        """
        if self._runtime_state is None:
            return ""
        tc = self._runtime_state.get("team_coordination", {})
        teammates = tc.get("teammates", [])
        if not teammates:
            return ""

        lines = ["### Team Coordination"]
        for t in teammates:
            aid = t["agent_id"]
            pos = t.get("position")
            inv = t.get("inventory", [])
            task = t.get("current_task_id", "idle")
            state = t.get("task_state", "UNKNOWN")

            inv_str = self._format_inventory(inv)
            carrying = " [CARRYING PERSON]" if t.get("is_carrying_person") else ""
            pos_str = (
                f"({pos[0]}, {pos[1]}, {pos[2]})"
                if isinstance(pos, (list, tuple)) and len(pos) >= 3
                else str(pos)
            )
            lines.append(
                f"  - {aid}: at {pos_str} | task={task} ({state}) | {inv_str}{carrying}"
            )
        lines.append("---")
        return "\n".join(lines)

    @staticmethod
    def _format_inventory(inv: list) -> str:
        if not inv:
            return "empty"
        if "Person" in inv:
            return "carrying Person"
        counts = Counter(inv)
        return " + ".join(f"{item} x{n}" for item, n in counts.items())

    def _render_mailbox_reminder(self) -> str:
        """Render the system-generated mailbox reminder section.

        Injected as the final section of the memory block during assemble().
        Only appears when runtime_state has a non-empty mailbox_summary.
        Never exposes message body content, only summary metadata.
        """
        if self._runtime_state is None:
            return ""
        raw = self._runtime_state.get("mailbox_summary")
        if not isinstance(raw, dict):
            return ""
        count = raw.get("unread_count", 0)
        if not isinstance(count, int) or count <= 0:
            return ""
        senders = raw.get("unique_senders", [])
        total_senders = raw.get("total_unique_senders", len(senders))
        oldest = raw.get("oldest_unread_at", "")
        lines = ["### Mailbox"]
        msg = f"You have {count} unread message(s)"
        if senders:
            display = senders[:5]
            sender_str = ", ".join(display)
            msg += f" from {sender_str}"
            remaining = total_senders - len(display)
            if remaining > 0:
                msg += f" and {remaining} other(s)"
        if oldest:
            msg += f" (oldest from {oldest})"
        msg += "."
        lines.append(msg)
        lines.append("Use `read_mailbox` tool to read them.")
        return "\n".join(lines)

    def _render_memory_block(self) -> str:
        base = super()._render_memory_block()
        # Inject team coordination section (after current state, before mailbox)
        team_text = self._render_team_coordination()
        if team_text:
            if not base:
                base = team_text
            else:
                base = base + "\n" + team_text
        # Inject mailbox reminder section
        reminder = self._render_mailbox_reminder()
        if not reminder:
            return base
        if not base:
            return reminder
        return base + "\n" + reminder

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
