"""P3 agentic system-health diagnosis loop (main plan §4 / §5 / D7, R6).

The loop is the second channel of the rolling reflection trigger (并存不替代,
§3.2): a bounded, synchronous, multi-round reviewer that consumes the
committed evidence of one scope through the four read-only query tools
(projection / temporal flow / supervision / control journal) and produces
validated :class:`DiagnosisCandidateV1` batches.

Execution model (main plan §5):
- synchronous ``while`` loop mirroring ``ReflectionModelPort``
  (reflection.py:250-256); no async loop body;
- each round is exactly one LLM function-call via
  ``ReflectionModelPort.complete_with_function_call`` with the diagnosis
  tools list; ``record_diagnosis`` sits at ``tools[0]`` so the port's
  tools[0] matching logic (reflection.py:336-346) surfaces completion calls
  unchanged — only the tool schema is swapped;
- the four read-only query tools are executed by the loop itself (evidence
  bootstrap + per-round refresh) because the port surfaces exactly one tool
  name per call (``tools[0]``); every tool result is fed back into the next
  round's prompt (轮间结果回喂);
- ``max_rounds`` (default 3) caps the rounds and ``diagnosis_sec`` (default
  90) caps the wall-clock budget; exceeding the budget yields a typed
  ``timeout`` result with zero writes — it never blocks the caller's exit
  path;
- output stays a function-call contract (``record_diagnosis``), never free
  text; ``validate_diagnosis_response`` is the deterministic fail-closed
  gate (missing function_call / structural violation → ``rejected`` with
  zero writes);
- accepted candidates are persisted to the run-local
  :class:`DiagnosisMemoryStore` and one canonical ``diagnosis.audit``
  temporal event is appended (DIAGNOSIS_AUDIT_EVENT_TYPE, independent
  top-level prefix; the reflection collector prefixes do not include
  ``diagnosis.``, so audit events never enter the reflection window).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from a2a.coordinator.memory.contracts import DiagnosisConfig
from a2a.coordinator.memory.diagnosis import (
    DIAGNOSIS_AUDIT_EVENT_TYPE,
    DiagnosisMemoryStore,
    DiagnosisStoreError,
    DiagnosisValidationResult,
    validate_diagnosis_response,
)
from a2a.coordinator.memory.reflection import ReflectionModelPort
from sar_orch.tools.coordinator.query_control_journal import QueryControlJournalTool
from sar_orch.tools.coordinator.query_projection import QueryProjectionTool
from sar_orch.tools.coordinator.query_supervision import QuerySupervisionTool
from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

__all__ = ["RECORD_DIAGNOSIS_TOOL", "DiagnosisLoop", "DiagnosisLoopResult"]

#: Completion tool contract — ``tools[0]`` for every round so the port's
#: matching logic (reflection.py:336-346, unchanged) surfaces its calls.
#: Fields mirror the LLM-side candidate contract (§3.2): target / finding /
#: suggestion / confidence / source_refs; diagnosis_key / kind /
#: policy_version are system-derived by the validator.
RECORD_DIAGNOSIS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_diagnosis",
        "description": (
            "Record the system-health diagnosis distilled from the evidence "
            "provided in this conversation. Every source_ref must be an "
            "exact [scope_id, event_id] pair present in the evidence. Never "
            "invent evidence; never mention simulator internals or source "
            "values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Review target: 'coordinator', 'system', or "
                        "'worker:<id>'."
                    ),
                },
                "finding": {
                    "type": "string",
                    "description": "Concise description of the observed issue.",
                },
                "suggestion": {
                    "type": "string",
                    "description": "Directional corrective suggestion.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence in the finding, 0.0 to 1.0.",
                },
                "source_refs": {
                    "type": "array",
                    "description": (
                        "Evidence pairs [scope_id, event_id] backing the "
                        "finding; each must be present in the provided "
                        "evidence."
                    ),
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
            },
            "required": ["target", "finding", "suggestion", "confidence", "source_refs"],
        },
    },
}

_SYSTEM_PROMPT = (
    "You are the system-health review subsystem of a multi-agent "
    "orchestration platform. Inspect the committed evidence of the current "
    "scope (projection, temporal flow, supervision count, control journal) "
    "and produce a concise diagnosis when you have enough evidence. Never "
    "invent evidence: every source_ref must be an exact [scope_id, event_id] "
    "pair observed in the provided evidence. Finish exclusively by calling "
    "record_diagnosis — never answer in plain text."
)

#: View filter (R6): diagnosis audit events never enter the reviewer input.
_VIEW_FILTER_PREFIX = "diagnosis."


@dataclass(frozen=True)
class DiagnosisLoopResult:
    """Typed outcome of one diagnosis-loop run.

    ``status``:
    - ``ok`` — validation accepted, candidates persisted, audit event
      attempted;
    - ``rejected`` — validator rejected the final response (zero writes);
    - ``timeout`` — wall-clock budget (``diagnosis_sec``) exhausted; the
      loop abandoned with zero writes (typed timeout semantics, never blocks
      the caller's exit);
    - ``rounds_exhausted`` — ``max_rounds`` reached without a completion
      call (zero writes).
    """

    status: str
    validation: DiagnosisValidationResult | None = None
    reason: str | None = None
    rounds: int = 0
    written: int = 0
    audit_event_id: str | None = None
    audit_error: str | None = None
    #: Observation-only (R4 补观测): per-round LLM call wall time in
    #: seconds (3-decimal), cumulative evidence-refresh wall time and the
    #: whole run() entry→terminal wall time.  Pure additive — never
    #: consulted by any control/validation/write decision; None only on
    #: early-exit branches where ``started`` was never set.
    round_latencies: list[float] | None = None
    evidence_sec: float | None = None
    duration_sec: float | None = None


def _await_sync(coro: Any) -> Any:
    """Run one async coroutine from synchronous code (reflection port
    pattern: fresh loop in this thread; off-thread when a loop is already
    running)."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    def _worker() -> None:
        try:
            result_queue.put(("ok", asyncio.run(coro)))
        except BaseException as exc:  # noqa: BLE001 - delivered to caller
            result_queue.put(("error", exc))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=30.0)
    if thread.is_alive():
        raise TimeoutError("query tool execution timed out")
    status, payload = result_queue.get()
    if status == "error":
        raise payload
    return payload


class DiagnosisLoop:
    """Bounded synchronous agentic diagnosis loop (main plan §5 / D7)."""

    def __init__(
        self,
        *,
        model_port: ReflectionModelPort,
        store: Any,
        scope_id: str,
        diagnosis_store: DiagnosisMemoryStore,
        config: DiagnosisConfig,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._model_port = model_port
        self._store = store
        self._scope_id = scope_id
        self._diagnosis_store = diagnosis_store
        self._config = config
        self._now = now or time.monotonic
        self._query_tools: list[Any] = [
            QueryProjectionTool(store=store, scope_id=scope_id),
            QueryTemporalFlowTool(store=store, scope_id=scope_id),
            QuerySupervisionTool(store=store, scope_id=scope_id),
            QueryControlJournalTool(store=store, scope_id=scope_id),
        ]

    # ── tool schemas ─────────────────────────────────────────────────────

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-shaped tool list: ``record_diagnosis`` first (tools[0],
        surfaced by the port) + the four read-only query schemas."""
        return [
            RECORD_DIAGNOSIS_TOOL,
            *[tool.to_openai_schema() for tool in self._query_tools],
        ]

    # ── main loop ────────────────────────────────────────────────────────

    def run(self) -> DiagnosisLoopResult:
        """Run up to ``max_rounds`` review rounds within ``diagnosis_sec``."""
        if self._model_port is None or self._store is None:
            return DiagnosisLoopResult(status="rejected", reason="missing_dependencies")
        if self._diagnosis_store is not None:
            try:
                self._diagnosis_store.open()
            except DiagnosisStoreError as exc:
                return DiagnosisLoopResult(status="rejected", reason=exc.code)

        transcript: list[dict[str, Any]] = []
        evidence_sec = 0.0
        evidence_started = self._now()
        self._collect_evidence(transcript)
        evidence_sec += self._now() - evidence_started
        started = self._now()
        rounds = 0
        round_latencies: list[float] = []
        while rounds < self._config.max_rounds:
            if self._now() - started > self._config.diagnosis_sec:
                return DiagnosisLoopResult(
                    status="timeout",
                    rounds=rounds,
                    reason="diagnosis_sec_exceeded",
                    round_latencies=round_latencies,
                    evidence_sec=evidence_sec,
                    duration_sec=round(self._now() - started, 3),
                )
            round_started = self._now()
            response = self._complete_with_remaining_budget(
                started=started,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=self._render_transcript(transcript),
                tools=self.tool_schemas(),
            )
            round_latencies.append(round(self._now() - round_started, 3))
            rounds += 1
            if self._now() - started > self._config.diagnosis_sec:
                return DiagnosisLoopResult(
                    status="timeout",
                    rounds=rounds,
                    reason="diagnosis_sec_exceeded",
                    round_latencies=round_latencies,
                    evidence_sec=evidence_sec,
                    duration_sec=round(self._now() - started, 3),
                )
            if "function_call" not in response:
                # No completion call: the model requested more evidence (or
                # produced free text, which the validator contract rejects).
                # Refresh the read-only evidence and feed it into the next
                # round (轮间结果回喂).
                refresh_started = self._now()
                self._collect_evidence(transcript)
                evidence_sec += self._now() - refresh_started
                continue
            validation = validate_diagnosis_response(
                response, input_window=self._evidence_window()
            )
            if validation.status != "ok":
                return DiagnosisLoopResult(
                    status="rejected",
                    validation=validation,
                    rounds=rounds,
                    reason=validation.reason,
                    round_latencies=round_latencies,
                    evidence_sec=evidence_sec,
                    duration_sec=round(self._now() - started, 3),
                )
            written = self._diagnosis_store.save_diagnoses(
                self._scope_id, validation.candidates
            )
            audit_event_id, audit_error = self._write_audit(validation)
            return DiagnosisLoopResult(
                status="ok",
                validation=validation,
                rounds=rounds,
                written=written,
                audit_event_id=audit_event_id,
                audit_error=audit_error,
                round_latencies=round_latencies,
                evidence_sec=evidence_sec,
                duration_sec=round(self._now() - started, 3),
            )
        return DiagnosisLoopResult(
            status="rounds_exhausted",
            rounds=rounds,
            reason="max_rounds_exceeded",
            round_latencies=round_latencies,
            evidence_sec=evidence_sec,
            duration_sec=round(self._now() - started, 3),
        )

    def _complete_with_remaining_budget(
        self,
        *,
        started: float,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Call the dedicated diagnosis port within the global budget.

        The real ``ReflectionModelPort`` is synchronous and uses its mutable
        ``timeout_sec`` for the underlying async request. The diagnosis port
        is exclusive to this loop, so temporarily narrowing that adapter
        bound to the remaining global budget gives ``diagnosis_sec`` a real
        in-flight deadline. Test fakes without ``timeout_sec`` keep the old
        call shape. The original bound is restored for the next trigger.
        """
        port = self._model_port
        timeout = getattr(port, "timeout_sec", None)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            return port.complete_with_function_call(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
            )

        remaining = max(self._config.diagnosis_sec - (self._now() - started), 0.001)
        original_timeout = timeout
        port.timeout_sec = min(float(original_timeout), remaining)
        try:
            return port.complete_with_function_call(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
            )
        finally:
            port.timeout_sec = original_timeout

    # ── evidence collection (loop-executed read-only tools) ──────────────

    def _collect_evidence(self, transcript: list[dict[str, Any]]) -> None:
        for tool in self._query_tools:
            if tool.name == "query_projection":
                # domain is required by the projection tool; the reviewer
                # sees both committed domains.
                self._collect_one(tool, transcript, domain="spatial")
                self._collect_one(tool, transcript, domain="embodied")
            else:
                self._collect_one(tool, transcript)

    def _collect_one(
        self,
        tool: Any,
        transcript: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        result = _await_sync(tool.execute(**kwargs))
        if result.success:
            try:
                payload = json.loads(result.content)
            except (TypeError, ValueError):
                payload = result.content
            transcript.append({"tool": tool.name, "result": payload})
        else:
            transcript.append({"tool": tool.name, "error": result.error})

    def _render_transcript(self, transcript: list[dict[str, Any]]) -> str:
        return json.dumps(
            {"scope_id": self._scope_id, "evidence": transcript},
            ensure_ascii=False,
            default=str,
        )

    def _evidence_window(self) -> list[dict[str, Any]]:
        """The event view the reviewer actually saw: temporal events minus
        diagnosis.* audit events, plus projection evidence rows (their
        ``event_id`` values are legit source_ref targets)."""
        window: list[dict[str, Any]] = []
        for evt in self._store.temporal_events(self._scope_id):
            if str(evt.get("event_type", "")).startswith(_VIEW_FILTER_PREFIX):
                continue
            window.append(evt)
        for row in self._store.projection_fields(self._scope_id):
            window.append(
                {
                    "scope_id": self._scope_id,
                    "event_id": row["event_id"],
                    "event_type": "projection.field",
                }
            )
        return window

    # ── audit ────────────────────────────────────────────────────────────

    def _write_audit(
        self, validation: DiagnosisValidationResult
    ) -> tuple[str | None, str | None]:
        """Append one canonical ``diagnosis.audit`` temporal event.

        Best-effort by design (the audit trail is not the diagnosis store):
        a failure is captured in ``audit_error`` and never blocks the
        already-persisted diagnosis result.
        """
        try:
            event_id = uuid.uuid4().hex
            now_iso = datetime.now(timezone.utc).isoformat()
            payload = json.dumps(
                {
                    "count": len(validation.candidates),
                    "diagnosis_keys": [
                        candidate.diagnosis_key for candidate in validation.candidates
                    ],
                    "policy_version": (
                        validation.candidates[0].policy_version
                        if validation.candidates
                        else None
                    ),
                },
                ensure_ascii=False,
                default=str,
            )
            self._store.append_temporal_event(
                event_id=event_id,
                scope_id=self._scope_id,
                sequence=self._store.next_sequence(self._scope_id),
                event_type=DIAGNOSIS_AUDIT_EVENT_TYPE,
                occurred_at=now_iso,
                ingested_at=now_iso,
                actor_id="DiagnosisReviewer",
                logical_task_id=None,
                dispatch_id=None,
                worker_task_id=None,
                tool_call_id=None,
                success=True,
                error=None,
                payload=payload,
                causation_id=None,
                correlation_id=None,
                idempotency_key=None,
            )
            return event_id, None
        except Exception as exc:  # noqa: BLE001 - best-effort audit
            return None, f"{type(exc).__name__}: {exc}"
