"""Phase 1+4 reflection contracts: DTOs, deterministic validator, source window
collector, model port and the offline reflection runner.

P1 defines the pure validation surface only — no real model call, no source
window collection, no store wiring.  Phase 4 adds the bounded incremental
``ReflectionSourceCollector`` (consuming only ``ScopeEventSnapshotV1``, never
rebuilding a window from store helpers), ``advance_window_cursor``,
``run_reflection`` (claim → model → validate → publish, model call strictly
outside any SQLite transaction) and ``reflection_write_transaction``.

Fail-closed validator contract (main plan §3.3 / D6 / D8): a reflection
output is rejected (zero long-term content writes) when it lacks a
function call, carries a forged/unverifiable source ref, contains a
FORBIDDEN_TRUTH_TERMS term, or repeats a memory_key inside one batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    LONG_TERM_MEMORY_KINDS,
    LongTermMemoryCandidateV1,
    MemoryContractError,
    canonical_json_bytes,
    canonicalize_statement,
    derive_memory_key,
    digest_bytes,
)
from Agent.worker_agent.llm.llm_wrapper import LLMClient
from Agent.worker_agent.schema import LLMProvider, Message

logger = logging.getLogger(__name__)

__all__ = [
    "LongTermMemoryCandidateV1",
    "ReflectionModelPort",
    "ReflectionRunResult",
    "ReflectionSourceCollector",
    "ReflectionSourceError",
    "ReflectionValidationResult",
    "ReflectionWindow",
    "advance_window_cursor",
    "reflection_write_transaction",
    "run_reflection",
    "validate_reflection_response",
]

_CANDIDATE_FIELDS = ("memory_key", "kind", "statement", "confidence", "source_refs")


@dataclass(frozen=True)
class ReflectionValidationResult:
    """Deterministic validator outcome.

    The validator itself never writes to any store; ``long_term_memory_written``
    is therefore always 0 here — P4 wiring persists only accepted candidates
    and records ``reflection_run.status=rejected`` + audit on rejection.
    """

    status: str  # ok | rejected
    long_term_memory_written: int = 0
    reason: str | None = None
    candidates: tuple[LongTermMemoryCandidateV1, ...] = ()


def _window_ref_set(input_window: Any) -> set[tuple[str, str]]:
    """Extract (scope_id, event_id) pairs from a source window.

    Accepts the P4 ``ScopeEventSnapshotV1`` shape (``.events`` of dicts with
    ``scope_id``/``event_id``) or a plain iterable of (scope_id, event_id)
    pairs.  Anything else yields an empty set → any non-empty ref list is
    rejected (fail closed).
    """
    events = getattr(input_window, "events", None)
    if isinstance(events, (list, tuple, set, frozenset)):
        refs: set[tuple[str, str]] = set()
        for event in events:
            if (
                isinstance(event, dict)
                and isinstance(event.get("scope_id"), str)
                and isinstance(event.get("event_id"), str)
            ):
                refs.add((event["scope_id"], event["event_id"]))
        return refs
    if isinstance(input_window, (set, frozenset)):
        return {
            ref
            for ref in input_window
            if isinstance(ref, tuple) and len(ref) == 2
        }
    if isinstance(input_window, (list, tuple)):
        return {
            tuple(ref)
            for ref in input_window
            if isinstance(ref, (list, tuple)) and len(ref) == 2
        }
    return set()


def validate_reflection_response(
    response: Any, *, input_window: Any = None
) -> ReflectionValidationResult:
    """Deterministic fail-closed validation of a reflection model response.

    ``response`` must carry ``function_call`` (single candidate dict or a
    batch list).  Every candidate must have memory_key / kind / statement /
    confidence / source_refs; ``kind`` must be in
    :data:`LONG_TERM_MEMORY_KINDS`; the canonicalized statement must be free
    of :data:`FORBIDDEN_TRUTH_TERMS`; memory_keys must be unique inside the
    batch; and every source ref must be verifiable against ``input_window``
    (with no window provided, any non-empty ref list is rejected as
    unverifiable).  All rejection paths return
    ``long_term_memory_written == 0``.
    """

    def rejected(reason: str) -> ReflectionValidationResult:
        return ReflectionValidationResult(
            status="rejected", long_term_memory_written=0, reason=reason
        )

    if not isinstance(response, dict):
        return rejected("invalid_response_shape")
    if "function_call" not in response:
        return rejected("missing_function_call")
    raw = response["function_call"]
    if isinstance(raw, dict):
        raw_candidates = [raw]
    elif isinstance(raw, (list, tuple)):
        raw_candidates = list(raw)
    else:
        return rejected("invalid_function_call_shape")
    if not raw_candidates:
        return rejected("missing_function_call")

    window_refs = _window_ref_set(input_window) if input_window is not None else None
    seen_keys: set[str] = set()
    validated: list[LongTermMemoryCandidateV1] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            return rejected("invalid_candidate_shape")
        missing = [
            name for name in _CANDIDATE_FIELDS if name not in candidate
        ]
        if missing:
            return rejected(f"missing_candidate_field:{','.join(missing)}")
        memory_key = candidate["memory_key"]
        if not isinstance(memory_key, str) or not memory_key.strip():
            return rejected("invalid_memory_key")
        if memory_key in seen_keys:
            return rejected("duplicate_memory_key")
        kind = candidate["kind"]
        if kind not in LONG_TERM_MEMORY_KINDS:
            return rejected(f"invalid_kind:{kind}")
        statement = candidate["statement"]
        if not isinstance(statement, str) or not statement.strip():
            return rejected("empty_statement")
        canonical = canonicalize_statement(statement)
        if not canonical:
            return rejected("empty_statement")
        if any(term in canonical for term in FORBIDDEN_TRUTH_TERMS):
            return rejected("forbidden_truth_term")
        refs = candidate["source_refs"]
        if not isinstance(refs, (list, tuple)):
            return rejected("invalid_source_refs")
        normalized_refs: list[tuple[str, str]] = []
        for ref in refs:
            if not (
                isinstance(ref, (list, tuple))
                and len(ref) == 2
                and isinstance(ref[0], str)
                and isinstance(ref[1], str)
            ):
                return rejected("invalid_source_ref_shape")
            normalized_refs.append((ref[0], ref[1]))
        if normalized_refs:
            if window_refs is None:
                return rejected("unverifiable_source_ref")
            for ref in normalized_refs:
                if ref not in window_refs:
                    return rejected("forged_source_ref")
        seen_keys.add(memory_key)
        # F2: 非数值 confidence（如 "abc"）→ typed rejected，绝不抛裸 ValueError。
        confidence = candidate["confidence"]
        if not isinstance(confidence, (int, float)):
            return rejected("invalid_confidence")
        # F3: 构造后必须过 DTO fail-closed 校验（含 confidence ∈ [0,1]），
        # 越界/非法值 → typed rejected，与 validator 其余路径一致。
        try:
            validated_candidate = LongTermMemoryCandidateV1(
                schema_version=1,
                memory_key=memory_key,
                kind=kind,
                statement=canonical,
                confidence=float(confidence),
                source_refs=tuple(normalized_refs),
            ).validate()
        except MemoryContractError as exc:
            return rejected(exc.code)
        validated.append(validated_candidate)
    return ReflectionValidationResult(
        status="ok", long_term_memory_written=0, candidates=tuple(validated)
    )


class ReflectionModelPort:
    """Function-call reflection model port (D8) — real LLM adapter (P4→P5).

    P1 defined the adapter shape only; the real adapter (P4→P5) reuses the
    existing ``LLMClient`` + tools stack (``LLMClient.generate`` with the
    OpenAI tool-call path).  ``complete_with_function_call`` is synchronous
    and wraps the async client call in ``asyncio.run``; the strict
    function-call contract is enforced fail-closed: a plain-text / JSON
    fallback or a model/network error yields a response **without** a
    ``function_call`` key so :func:`validate_reflection_response` rejects it
    (zero long-term writes).  The 5/5 real-model smoke gate (main plan
    §5.3) must pass before ``long_term_mode`` may leave ``shadow``/``off``.
    """

    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_sec: float = 300.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.timeout_sec = timeout_sec
        #: Diagnostics: last fail-closed reason (model/network error), None on
        #: success.  Never contains the api key (client errors do not echo it).
        self.last_error: str | None = None

    def complete_with_function_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Invoke the model with strict function calling (real adapter).

        ``messages = [system, user]``; the tool payload of the first
        function tool call matching ``tools[0]`` becomes ``function_call``
        (unwrapping the reflection tool's ``memories`` array), and the
        OpenAI ``usage`` block is carried through for audit.  Every failure
        path (missing key/base, unknown provider, model/network/timeout
        exception, missing or mismatched tool call) returns a dict **without**
        ``function_call`` so the deterministic validator rejects it — the
        port never raises and never hangs (``asyncio.wait_for`` bounds the
        call; a running-event-loop call is offloaded to a daemon thread,
        see ``_invoke_off_thread``).

        Event-loop context (M1): the method stays synchronous.  When no
        event loop is running (smoke / worker threads) the call runs on a
        fresh loop in the current thread via ``asyncio.run``.  When called
        synchronously from inside a running event loop (production terminal
        reflection inside ``async run_experiment``), ``asyncio.run`` would
        raise ``RuntimeError`` and an inline await would freeze the loop —
        the call is instead executed on a daemon thread whose own fresh
        loop is torn down by ``asyncio.run``, and the caller waits at most
        ``timeout_sec + 5``.

        Resource lifecycle (m3): a fresh ``LLMClient`` is created per call.
        The wrapper holds an event-loop-bound async client (httpx pool) and
        exposes no ``close()``/``aclose()``; every call runs on a fresh loop
        (possibly in a different thread), so cross-call reuse would bind the
        pool to a closed/foreign loop — unsafe.  The per-call pool is
        released when the call returns (loop teardown + refcount/GC), which
        bounds socket usage to one in-flight call.
        """
        self.last_error = None
        try:
            if not self.api_key:
                return self._fail_closed("api_key not configured")
            if not self.api_base:
                return self._fail_closed("api_base not configured")

            def _invoke() -> Any:
                client = LLMClient(
                    api_key=self.api_key,
                    provider=LLMProvider(self.provider),
                    api_base=self.api_base,
                    model=self.model,
                )
                messages = [
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ]

                async def _call() -> Any:
                    return await client.generate(messages, tools)

                return asyncio.run(
                    asyncio.wait_for(_call(), timeout=self.timeout_sec)
                )

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (smoke / standalone thread): fresh loop in
                # this thread, exactly as before.
                response = _invoke()
            else:
                # Running loop (production terminal path): never asyncio.run
                # here (RuntimeError) and never block the loop inline —
                # offload to a daemon thread with a bounded join.
                response = self._invoke_off_thread(_invoke)
            usage = None
            if getattr(response, "usage", None) is not None:
                u = response.usage
                usage = {
                    "total_tokens": int(u.total_tokens),
                    "prompt_tokens": int(u.prompt_tokens),
                    "completion_tokens": int(u.completion_tokens),
                    "cache_hit_tokens": int(u.cache_hit_tokens),
                    "cache_miss_tokens": int(u.cache_miss_tokens),
                }
            tool_calls = getattr(response, "tool_calls", None) or ()
            expected_name = ""
            if tools and isinstance(tools[0], dict):
                expected_name = (
                    tools[0].get("function", {}).get("name", "")
                    if isinstance(tools[0].get("function"), dict)
                    else ""
                )
            for tool_call in tool_calls:
                name = getattr(getattr(tool_call, "function", None), "name", None)
                if name != expected_name:
                    continue
                arguments = getattr(
                    getattr(tool_call, "function", None), "arguments", None
                )
                if not isinstance(arguments, dict):
                    continue
                # The reflection tool wraps candidates under ``memories``;
                # anything else is passed through for the validator to
                # reject (fail closed).
                payload = arguments.get("memories", arguments)
                return {
                    "function_call": payload,
                    "finish_reason": "tool_calls",
                    "usage": usage,
                }
            # D8: no matching function call → never accepted.
            content = getattr(response, "content", None) or ""
            return {"content": content, "finish_reason": "stop", "usage": usage}
        except Exception as exc:  # noqa: BLE001 - fail closed, never hang/crash
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("reflection model call failed (fail closed): %s", self.last_error)
            return self._fail_closed(self.last_error)

    def _invoke_off_thread(self, invoke: Callable[[], Any]) -> Any:
        """Run ``invoke()`` (own fresh event loop) on a daemon thread.

        Only used when ``complete_with_function_call`` is called
        synchronously from inside a running event loop (production terminal
        reflection): ``asyncio.run`` in the loop thread would raise
        ``RuntimeError``, and awaiting inline would freeze the whole
        experiment loop for up to ``timeout_sec``.  The worker delivers the
        result or exception through a queue; the caller blocks at most
        ``timeout_sec + 5`` seconds (``wait_for`` normally ends the worker
        at ``timeout_sec``).  A worker still alive past the bound raises
        ``TimeoutError`` so the caller's catch-all fails closed while the
        daemon thread keeps draining in the background.
        """
        box: queue.Queue[Any] = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                box.put(invoke())
            except BaseException as exc:  # noqa: BLE001 - deliver to caller
                box.put(exc)

        thread = threading.Thread(
            target=_worker,
            name="reflection-model-call",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=self.timeout_sec + 5.0)
        if thread.is_alive():
            raise TimeoutError(
                f"reflection model call exceeded {self.timeout_sec}s bound "
                "(offload thread still running; fail closed)"
            )
        result = box.get()
        if isinstance(result, BaseException):
            raise result
        return result

    def _fail_closed(self, error: str) -> dict[str, Any]:
        """Response shape without ``function_call`` → validator rejects."""
        return {"content": "", "finish_reason": "stop", "error": error}

    @property
    def model_digest(self) -> str:
        """Deterministic digest of the provider/model pair (audit trail)."""
        return digest_bytes(
            canonical_json_bytes({"provider": self.provider, "model": self.model})
        )


# ── Phase 4: bounded incremental source window (atomic-snapshot supplement
#    §2.4 / main plan §3.4.2) ────────────────────────────────────────────────


class ReflectionSourceError(MemoryContractError):
    """Typed rejection of a non-canonical reflection source (D7)."""

    code = "invalid_reflection_source"


#: The only event families a reflection may consume (main plan §3.4.2).
_REFLECTION_EVENT_PREFIXES = ("control.", "callback.", "evidence.", "supervision.")


@dataclass(frozen=True)
class ReflectionWindow:
    """One bounded incremental source window.

    ``events`` are sequence-ascending canonical events strictly after the
    collector's ``window_end_sequence``; ``truncated`` records that the
    window exceeded ``max_events`` / ``max_chars`` and only the most recent
    events were kept; ``window_end_sequence`` is the sequence of the last
    kept event (0 for an empty window) — the cursor-advance target on a
    ``completed`` run (§3.4.3).
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    window_end_sequence: int = 0


class ReflectionSourceCollector:
    """Incremental bounded window builder over committed canonical events.

    The collector only ever consumes an accepted ``ScopeEventSnapshotV1``
    (or an iterable of its events); it never calls ``revision_of()`` /
    ``temporal_events()`` itself, so it cannot rebuild a source window from
    store helpers (atomic-snapshot supplement §2.4).  Legacy EventStore /
    truth manifest / barrier / raw LLM trace sources are rejected with a
    typed error (D7).
    """

    def __init__(
        self,
        window_end_sequence: int = 0,
        max_events: int = 200,
        max_chars: int = 8000,
    ) -> None:
        self._window_end_sequence = window_end_sequence
        self._max_events = max_events
        self._max_chars = max_chars
        self._snapshot: Any | None = None

    def accepts_event_type(self, event_type: Any) -> bool:
        """Only ``control.*`` / ``callback.*`` / ``evidence.*`` / ``supervision.*``."""
        return isinstance(event_type, str) and event_type.startswith(
            _REFLECTION_EVENT_PREFIXES
        )

    def accept_source(self, source: Any) -> ReflectionSourceCollector:
        """Accept one canonical source snapshot; reject everything else (D7)."""
        if (
            hasattr(source, "scope_id")
            and hasattr(source, "events")
            and hasattr(source, "snapshot_digest")
            and getattr(source, "status", "ok") == "ok"
        ):
            self._snapshot = source
            return self
        raise ReflectionSourceError(
            "invalid_reflection_source",
            "reflection source must be a committed ScopeEventSnapshotV1; "
            "legacy EventStore / truth artifact / barrier / raw LLM trace "
            "are never accepted",
        )

    def collect(self, events: Any) -> ReflectionWindow:
        """Build the incremental window: sequence > cursor, allowlisted types.

        Events without a ``sequence`` field are ignored (canonical events
        always carry one).  Out-of-range events are dropped, the remainder is
        ordered by sequence, and over-limit windows keep only the most recent
        events with ``truncated=True``.
        """
        selected: list[dict[str, Any]] = []
        for event in events or ():
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                continue
            if sequence <= self._window_end_sequence:
                continue
            event_type = event.get("event_type")
            # M7：event_type 缺失（None）fail closed——``accepts_event_type``
            # 已对非 str 返回 False，缺类型的事件一律不进窗口。
            if not self.accepts_event_type(event_type):
                continue
            selected.append(event)
        selected.sort(key=lambda event: int(event["sequence"]))
        truncated = False
        if self._max_events is not None and len(selected) > self._max_events:
            selected = selected[-self._max_events :]
            truncated = True
        if self._max_chars is not None:
            sizes = [len(canonical_json_bytes(event)) for event in selected]
            while selected and sum(sizes) > self._max_chars:
                selected.pop(0)
                sizes.pop(0)
                truncated = True
        end = int(selected[-1]["sequence"]) if selected else 0
        return ReflectionWindow(
            events=selected, truncated=truncated, window_end_sequence=end
        )


def advance_window_cursor(current: int, end: int, status: str) -> int:
    """§3.4.3: the window cursor advances only on a ``completed`` run.

    ``failed`` / ``rejected`` / ``timeout`` keep ``current`` so the next
    trigger re-covers the same interval (no silent event loss).
    """
    return end if status == "completed" else current


# ── Phase 4: offline reflection runner ──────────────────────────────────────


@dataclass(frozen=True)
class ReflectionRunResult:
    """Outcome of :func:`run_reflection` (typed; never a bare exception).

    ``status``: ``completed`` | ``duplicate`` | ``rejected`` | ``failed`` |
    ``retryable_lock_busy`` | ``skipped``.  ``long_term_memory_written`` counts
    published rows of this run (supersede updates included).
    """

    status: str
    run_id: str | None = None
    long_term_memory_written: int = 0
    reason: str | None = None


class _ReflectionWriteTx:
    """Handle yielded by :func:`reflection_write_transaction`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.lock_held = True

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, parameters)


@contextlib.contextmanager
def reflection_write_transaction(
    store_path: str,
) -> Iterator[_ReflectionWriteTx]:
    """Short ``BEGIN IMMEDIATE`` write transaction for reflection products.

    Entering the context already holds the write lock (``tx.lock_held is
    True``).  LLM / network / export calls must happen **before** entering —
    never inside (main plan §3.3 last rule).  The transaction commits on a
    clean exit and rolls back on any exception; the connection is always
    closed.
    """
    conn = sqlite3.connect(
        str(store_path), isolation_level=None, check_same_thread=False
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        conn.close()
        raise MemoryContractError(
            "lock_busy_retryable",
            f"reflection write transaction could not start: {exc}",
        ) from exc
    tx = _ReflectionWriteTx(conn)
    try:
        yield tx
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")
    finally:
        conn.close()


_REFLECTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_long_term_memories",
        "description": (
            "Record validated long-term memories distilled from the provided "
            "source window. Every source_ref must be a [scope_id, event_id] "
            "pair present in the window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "memory_key": {
                                "type": "string",
                                "description": (
                                    "internal batch label; the stored key is "
                                    "system-derived and may differ"
                                ),
                            },
                            "kind": {
                                "type": "string",
                                "enum": list(LONG_TERM_MEMORY_KINDS),
                            },
                            "statement": {"type": "string"},
                            "confidence": {"type": "number"},
                            "source_refs": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                            },
                        },
                        "required": [
                            "memory_key",
                            "kind",
                            "statement",
                            "confidence",
                            "source_refs",
                        ],
                    },
                }
            },
            "required": ["memories"],
        },
    },
}

_SYSTEM_PROMPT = (
    "You are the long-term memory reflection subsystem. Distill durable, "
    "post-redaction statements (kind in "
    + ", ".join(LONG_TERM_MEMORY_KINDS)
    + ") from the committed source window. Never invent evidence: every "
    "source_ref must be an exact [scope_id, event_id] pair from the window. "
    "Never mention simulator truth, ground truth or oracle values."
)


def _build_user_prompt(snapshot: Any, window: ReflectionWindow) -> str:
    payload = {
        "scope_id": getattr(snapshot, "scope_id", None),
        "memory_revision": getattr(snapshot, "memory_revision", 0),
        "window_truncated": window.truncated,
        "events": window.events,
    }
    return canonical_json_bytes(payload).decode("utf-8")


def _snapshot_field(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def run_reflection(
    store: Any,
    snapshot: Any,
    policy_version: int,
    *,
    project_id: str = "llamar",
    model_port: ReflectionModelPort | None = None,
    max_events: int = 200,
    max_chars: int = 8000,
    window_end_sequence: int | None = None,
) -> ReflectionRunResult:
    """Run one reflection over a committed source snapshot (offline P4 path).

    - ``snapshot is None`` (snapshot acquisition failed) → ``failed`` with
      zero long-term content writes (redacted audit diagnosis only);
    - empty source window (collector cursor already at the latest committed
      event) → ``skipped`` with reason ``empty_window``: no claim, zero
      ``reflection_run`` rows, zero writes (§3.4.2, aligned with the rolling
      worker);
    - exactly-once claim: the idempotency key binds project + scope +
      ``memory_revision`` + ``snapshot_digest`` + ``policy_version``, so a
      retried snapshot returns ``duplicate`` with zero new rows;
    - the model call (when a port is injected) happens **outside** any SQLite
      transaction; validated candidates are published in per-candidate short
      transactions with system-derived memory_keys and explicit supersede;
    - with no model port the run is claimed and completed with zero content
      (offline no-model path — P4 never invokes a real model).
    """
    if snapshot is None:
        try:
            store.record_audit(
                "reflection_failed",
                "snapshot unavailable; zero long-term content written",
            )
        except Exception:
            logger.exception("failed to record snapshot-failure audit")
        return ReflectionRunResult(status="failed", reason="snapshot_unavailable")

    scope_id = _snapshot_field(snapshot, "scope_id")
    memory_revision = _snapshot_field(snapshot, "memory_revision", 0)
    snapshot_digest = _snapshot_field(snapshot, "snapshot_digest", "")
    if not isinstance(scope_id, str) or not scope_id:
        return ReflectionRunResult(status="failed", reason="invalid_snapshot")
    if not isinstance(memory_revision, int):
        memory_revision = int(memory_revision or 0)
    if not isinstance(snapshot_digest, str):
        snapshot_digest = str(snapshot_digest)

    if window_end_sequence is None:
        try:
            cursor = store.window_end_sequence(
                project_id=project_id, scope_id=scope_id
            )
        except Exception:
            logger.exception("window cursor read failed; starting at 0")
            cursor = 0
    else:
        cursor = window_end_sequence

    collector = ReflectionSourceCollector(
        window_end_sequence=cursor,
        max_events=max_events,
        max_chars=max_chars,
    )
    events = _snapshot_field(snapshot, "events", ())
    window = collector.collect(list(events) if events else [])
    window_end = window.window_end_sequence

    if not window.events:
        # §3.4.2（M1）：窗口为空 → 跳过本次反思，与滚动路径
        # ``_rolling_worker`` 的 ``skipped_window_empty`` 对齐：不 claim、零
        # reflection_run 行、零写入、游标不推进。
        return ReflectionRunResult(
            status="skipped", reason="empty_window", long_term_memory_written=0
        )

    claim = store.claim_reflection_run(
        project_id=project_id,
        scope_id=scope_id,
        source_memory_revision=memory_revision,
        snapshot_digest=snapshot_digest,
        policy_version=policy_version,
        window_end_sequence=window_end,
    )
    if claim.status == "duplicate":
        return ReflectionRunResult(
            status="duplicate", run_id=claim.run_id, reason="duplicate_snapshot"
        )
    if claim.status == "retryable_lock_busy":
        return ReflectionRunResult(status="retryable_lock_busy")
    run_id = claim.run_id
    if run_id is None:  # pragma: no cover - claim ok always assigns a run_id
        return ReflectionRunResult(status="failed", reason="claim_missing_run_id")

    # M6：written 提升到 try 外初始化——catch-all failed 分支返回已部分发布
    # 的真实计数，而非恒 0（per-candidate 短事务已提交的行不算丢失）。
    written = 0
    try:
        if model_port is None:
            store.mark_reflection_run(run_id, "completed")
            return ReflectionRunResult(
                status="completed",
                run_id=run_id,
                long_term_memory_written=0,
                reason="no_model_port_offline",
            )
        # ── model call strictly outside any SQLite transaction ──
        response = model_port.complete_with_function_call(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(snapshot, window),
            tools=[_REFLECTION_TOOL],
        )
        validation = validate_reflection_response(response, input_window=window)
        if validation.status == "rejected":
            store.mark_reflection_run(run_id, "rejected")
            store.record_audit(
                "reflection_rejected",
                validation.reason or "rejected",
                digest_prefix=snapshot_digest[:8],
            )
            return ReflectionRunResult(
                status="rejected",
                run_id=run_id,
                long_term_memory_written=0,
                reason=validation.reason,
            )
        for candidate in validation.candidates:
            memory_key = derive_memory_key(
                project_id=project_id,
                kind=candidate.kind,
                policy_version=policy_version,
                statement=candidate.statement,
            )
            result = store.publish_memory(
                project_id=project_id,
                scope_id=scope_id,
                memory_key=memory_key,
                statement=candidate.statement,
                source_refs=list(candidate.source_refs),
                kind=candidate.kind,
                confidence=candidate.confidence,
                policy_version=policy_version,
            )
            if result.status in ("ok", "superseded"):
                written += 1
            elif result.status == "retryable_lock_busy":
                store.mark_reflection_run(run_id, "failed")
                store.record_audit(
                    "reflection_failed",
                    "publish lock busy; published rows are per-candidate atomic",
                    digest_prefix=snapshot_digest[:8],
                )
                return ReflectionRunResult(
                    status="retryable_lock_busy",
                    run_id=run_id,
                    long_term_memory_written=written,
                )
        store.mark_reflection_run(run_id, "completed")
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            store.record_audit(
                "reflection_usage",
                f"tokens={usage['total_tokens']}",
                digest_prefix=snapshot_digest[:8],
            )
        return ReflectionRunResult(
            status="completed", run_id=run_id, long_term_memory_written=written
        )
    except Exception as exc:  # noqa: BLE001 - typed failed status, never crash the run
        try:
            store.mark_reflection_run(run_id, "failed")
            store.record_audit(
                "reflection_failed",
                f"reflection failed: {exc}",
                digest_prefix=snapshot_digest[:8],
            )
        except Exception:
            logger.exception("failed to record reflection failure")
        return ReflectionRunResult(
            status="failed",
            run_id=run_id,
            long_term_memory_written=written,
            reason=str(exc),
        )
