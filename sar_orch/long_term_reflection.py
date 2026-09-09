"""SAR wiring module for the long-term memory feature (Phase 4 offline).

Phase 1 exposed the D9 configuration surface (``load_long_term_config``).
Phase 4 adds the run wiring:

- ``reflection_run``         — synchronous trigger unit over one snapshot;
- ``maybe_trigger_rolling_reflection`` — async rolling trigger with coalesce
  (a running reflection skips a new trigger) and a min-interval throttle;
- ``drain_inflight_reflection`` — terminal drain: join the in-flight rolling
  thread with a timeout (``[timeout] reflection_sec``, default 60s); on
  timeout the run is NOT blocked — the drain reports ``timeout`` and the
  caller records a typed timeout status;
- ``configure_long_term_runtime`` — injects the run-local
  ``LongTermMemoryStore`` + snapshot provider + optional model port;
- ``configure_diagnosis_runtime`` — P4 second channel: injects the
  run-local diagnosis store + canonical store + config so the rolling
  worker also runs the bounded agentic diagnosis loop (same trigger,
  shared coalesce slot, result recorded independently — 并存不替代);
- ``build_reflection_model_port`` — D8 provider/model config adapter read
  from ``.env`` (``reflection_provider`` / ``reflection_model`` /
  ``reflection_api_key`` / ``reflection_api_base``).  Phase 4 never invokes
  a real model: a missing/empty key configuration yields ``None`` and the
  trigger records a skip (``model_unconfigured``) instead of failing the run.

All reflection input comes from ``scope_event_snapshot`` (atomic snapshot
supplement §2); this module never reads EventStore / truth / barrier / raw
LLM traces.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import (
    POLICY_VERSION,
    LongTermConfigError,
    LongTermRuntimeConfig,
    load_diagnosis_config,
    load_long_term_config,
)
from a2a.coordinator.memory.long_term import _redact_truth, _utc_now
from a2a.coordinator.memory.reflection import (
    ReflectionModelPort,
    ReflectionRunResult,
)
from a2a.coordinator.memory.reflection import (
    run_reflection as _run_reflection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DrainResult",
    "LongTermConfigError",
    "LongTermRuntimeConfig",
    "build_reflection_model_port",
    "configure_diagnosis_runtime",
    "configure_long_term_runtime",
    "drain_inflight_reflection",
    "load_diagnosis_config",
    "load_long_term_config",
    "maybe_trigger_rolling_reflection",
    "reflection_run",
]


@dataclass(frozen=True)
class DrainResult:
    """Outcome of :func:`drain_inflight_reflection` (terminal join)."""

    status: str  # joined | timeout
    result: dict[str, Any] | None = None

# ── module-level run runtime (one run at a time) ────────────────────────────

_runtime: dict[str, Any] = {
    "store": None,          # LongTermMemoryStore | None
    "snapshot_provider": None,  # () -> ScopeEventSnapshotV1 | None
    "project_id": "llamar",
    "policy_version": POLICY_VERSION,
    "model_port": None,     # ReflectionModelPort | None (P4: never real calls)
    "config": None,         # LongTermRuntimeConfig | None
    "last_triggered_at": 0.0,
    # Phase 4 (P4): second-channel diagnosis runtime (main plan §3.2 / §4).
    # The diagnosis port is deliberately separate from the rolling reflection
    # port so its bounded timeout cannot change the reflection channel.
    "canonical_store": None,    # MemoryStore | None (diagnosis query tools)
    "diagnosis_store": None,    # DiagnosisMemoryStore | None
    "diagnosis_config": None,   # DiagnosisConfig | None
    "diagnosis_model_port": None,  # ReflectionModelPort | None
}

_inflight_lock = threading.Lock()
_inflight_thread: threading.Thread | None = None
_inflight: dict[str, Any] = {
    "active": False,
    "result": None,
}

#: Append lock for the best-effort rolling intermediate-state rows (W2,
#: trajectory-audit Gap H4).  The rolling worker is single-flight by
#: coalesce, but the diagnosis loop inside it appends to the same file, so
#: both writers serialize through their own module locks.
_transcript_lock = threading.Lock()


def configure_long_term_runtime(
    *,
    store: Any,
    snapshot_provider: Callable[[], Any] | None = None,
    project_id: str = "llamar",
    policy_version: int = POLICY_VERSION,
    model_port: ReflectionModelPort | None = None,
    config: LongTermRuntimeConfig | None = None,
) -> None:
    """Inject the run-local long-term runtime (called when mode != off)."""
    with _inflight_lock:
        _runtime["store"] = store
        _runtime["snapshot_provider"] = snapshot_provider
        _runtime["project_id"] = project_id
        _runtime["policy_version"] = policy_version
        _runtime["model_port"] = model_port
        _runtime["config"] = config or LongTermRuntimeConfig()
        _runtime["last_triggered_at"] = 0.0


def configure_diagnosis_runtime(
    *,
    canonical_store: Any,
    diagnosis_store: Any,
    diagnosis_config: Any,
    model_port: ReflectionModelPort | None = None,
) -> None:
    """Inject the run-local diagnosis channel (P4, main plan §3.2 / §4).

    Called alongside :func:`configure_long_term_runtime` when the
    diagnosis store is available.  ``model_port`` is a dedicated diagnosis
    port; it must not reuse ``_runtime["model_port"]`` because the diagnosis
    budget is intentionally shorter than the reflection adapter timeout.
    The
    diagnosis loop runs inside the rolling worker — the same trigger
    point, the same coalesce slot (并存不替代) — and its typed result is
    recorded independently of the reflection result: neither blocks the
    other, and an unconfigured channel is a typed skip (fail-closed, D8).
    """
    with _inflight_lock:
        _runtime["canonical_store"] = canonical_store
        _runtime["diagnosis_store"] = diagnosis_store
        _runtime["diagnosis_config"] = diagnosis_config
        _runtime["diagnosis_model_port"] = model_port


def _reset_runtime() -> None:
    """Test/teardown helper: clear the module-level runtime."""
    with _inflight_lock:
        _runtime.update(
            store=None,
            snapshot_provider=None,
            project_id="llamar",
            policy_version=POLICY_VERSION,
            model_port=None,
            config=None,
            last_triggered_at=0.0,
            canonical_store=None,
            diagnosis_store=None,
            diagnosis_config=None,
            diagnosis_model_port=None,
        )
        _inflight["active"] = False
        _inflight["result"] = None


# ── D8 provider/model adapter skeleton (no real model call in P4) ───────────

def build_reflection_model_port(
    env: dict[str, str], *, timeout_sec: float = 300.0
) -> ReflectionModelPort | None:
    """Build the D8 reflection model port from ``.env`` values.

    Reads ``reflection_provider`` / ``reflection_model`` /
    ``reflection_api_key`` / ``reflection_api_base`` (falling back to the
    generic ``provider`` / ``model`` / ``api_key`` / ``api_base`` keys).
    ``timeout_sec`` is an adapter-local bound. Callers that need a tighter
    diagnosis budget build a separate port instead of mutating the rolling
    reflection port. Returns ``None`` when the provider, model or api key is
    missing — the caller then skips the reflection with a typed
    ``model_unconfigured`` status instead of invoking anything. P4 never
    performs a real model call.
    """
    provider = env.get("reflection_provider") or env.get("provider")
    model = env.get("reflection_model") or env.get("model")
    api_key = env.get("reflection_api_key") or env.get("api_key")
    api_base = env.get("reflection_api_base") or env.get("api_base")
    if not provider or not model or not api_key:
        return None
    return ReflectionModelPort(
        provider=provider,
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout_sec=timeout_sec,
    )


# ── trigger unit ────────────────────────────────────────────────────────────

def reflection_run(
    *,
    store: Any,
    snapshot: Any,
    policy_version: int = POLICY_VERSION,
    project_id: str = "llamar",
    model_port: ReflectionModelPort | None = None,
    window_end_sequence: int | None = None,
) -> ReflectionRunResult:
    """Synchronous reflection trigger unit over one committed snapshot.

    Delegates to :func:`a2a.coordinator.memory.reflection.run_reflection`;
    with no model port the run is claimed and completed with zero content
    (offline no-model path).  Never performs a real model call.
    """
    return _run_reflection(
        store,
        snapshot,
        policy_version,
        project_id=project_id,
        model_port=model_port,
        window_end_sequence=window_end_sequence,
    )


# ── rolling trigger (async + coalesce) ──────────────────────────────────────

def _rolling_worker() -> None:
    """Background rolling reflection: snapshot → incremental window → run.

    An empty window skips the reflection entirely (main plan §3.4.2) with no
    claim and no cursor movement.  With no model port the run is skipped as
    ``model_unconfigured`` (never blocks the experiment, never invokes a
    model).  The result is recorded on the module-level ``_inflight`` slot
    for the terminal drain; W2 additionally appends the typed intermediate
    outcomes (rejected / timeout / rounds_exhausted) to
    ``<memory_root>/diagnosis/transcripts.ndjson`` so they survive
    subsequent triggers (trajectory-audit Gap H4).
    """
    with _inflight_lock:
        store = _runtime.get("store")
        snapshot_provider = _runtime.get("snapshot_provider")
        project_id = _runtime.get("project_id", "llamar")
        policy_version = _runtime.get("policy_version", POLICY_VERSION)
        model_port = _runtime.get("model_port")
        config = _runtime.get("config") or LongTermRuntimeConfig()
    try:
        if store is None or snapshot_provider is None:
            _set_inflight_result({"status": "skipped_no_runtime"})
            return
        snapshot = snapshot_provider()
        if snapshot is None:
            _set_inflight_result({"status": "skipped_no_snapshot"})
            return
        if getattr(snapshot, "status", "ok") != "ok":
            _set_inflight_result({"status": "skipped_snapshot_not_ok"})
            return
        if model_port is None:
            _set_inflight_result({"status": "skipped_model_unconfigured"})
            return
        from a2a.coordinator.memory.reflection import ReflectionSourceCollector

        scope_id = snapshot.scope_id
        cursor = store.window_end_sequence(project_id=project_id, scope_id=scope_id)
        collector = ReflectionSourceCollector(
            window_end_sequence=cursor,
            max_events=config.max_events,
            max_chars=config.max_chars,
        )
        window = collector.collect(list(snapshot.events))
        if not window.events:
            _set_inflight_result({"status": "skipped_window_empty"})
            return
        result = _run_reflection(
            store,
            snapshot,
            policy_version,
            project_id=project_id,
            model_port=model_port,
            max_events=config.max_events,
            max_chars=config.max_chars,
            window_end_sequence=cursor,
        )
    except Exception:
        logger.exception("rolling reflection failed")
        _set_inflight_result({"status": "failed", "reason": "rolling_worker_error"})
        return
    # M-4 (review): the reflection result is recorded IMMEDIATELY on
    # completion — the diagnosis channel below runs independently
    # afterwards and can never block, delay, or overwrite it.  A terminal
    # drain that times out while the diagnosis channel is still running
    # loses at most the diagnosis (result["diagnosis"] absent → typed
    # None), never the reflection result.
    reflection_result = {
        "status": result.status,
        "run_id": result.run_id,
        "long_term_memory_written": result.long_term_memory_written,
        "reason": result.reason,
    }
    _record_inflight_result(reflection_result)
    # Phase 4 (P4): second channel — agentic diagnosis loop on the same
    # committed snapshot (并存不替代, main plan §3.2).  Same trigger,
    # shared coalesce slot, but the diagnosis result is attached as an
    # extra field on the recorded reflection result: it never replaces
    # the reflection status and a fail-closed skip never raises (D8).
    # M-3 (review): every diagnosis-side failure is contained inside
    # _run_diagnosis_channel; the defensive try below is a second barrier
    # so a future drift can never wipe the already-recorded result.
    try:
        diagnosis = _run_diagnosis_channel(snapshot)
    except Exception:  # fail-closed diagnosis channel (logged below)
        logger.exception("diagnosis channel raised outside its fail-closed boundary")
        diagnosis = {"status": "failed", "reason": "diagnosis_loop_error"}
    final_result = dict(reflection_result)
    final_result["diagnosis"] = diagnosis
    _set_inflight_result(final_result)
    # W2 (trajectory-audit Gap H4): rolling 中间态（rejected / timeout /
    # rounds_exhausted）不再只存 _inflight 内存覆盖——每次触发的 typed
    # 中间结果同步追加到 <memory_root>/diagnosis/transcripts.ndjson
    # （kind=rolling_state），中间轮次状态不再被后续触发覆盖丢失。
    # best-effort（D8）：写失败只记日志，绝不阻塞 rolling worker。
    if reflection_result["status"] == "rejected":
        _append_rolling_state(
            scope_id=snapshot.scope_id,
            channel="reflection",
            status="rejected",
            reason=reflection_result.get("reason"),
            run_id=reflection_result.get("run_id"),
        )
    if diagnosis.get("status") in _ROLLING_STATE_STATUSES:
        rounds = diagnosis.get("rounds")
        _append_rolling_state(
            scope_id=snapshot.scope_id,
            channel="diagnosis",
            status=diagnosis.get("status", ""),
            rounds=rounds if isinstance(rounds, int) else None,
            reason=diagnosis.get("reason"),
        )


def _run_diagnosis_channel(snapshot: Any) -> dict[str, Any]:
    """P4: run the bounded agentic diagnosis loop on the snapshot scope.

    Fail-closed by design (D8): missing canonical store / diagnosis store /
    config / model port yields a typed skip — the channel never blocks the
    rolling worker, the experiment poll loop, or run exit.  The loop's own
    typed outcomes (ok / rejected / timeout / rounds_exhausted) are
    returned verbatim for the terminal drain to surface.  M-3 (review):
    the import + construction live inside the internal try, so ANY
    diagnosis-side exception (broken import, constructor error, loop
    failure) is contained here as a typed ``diagnosis_loop_error`` and
    never bubbles up to overwrite the recorded reflection result.
    """
    with _inflight_lock:
        canonical_store = _runtime.get("canonical_store")
        diagnosis_store = _runtime.get("diagnosis_store")
        diagnosis_config = _runtime.get("diagnosis_config")
        model_port = _runtime.get("diagnosis_model_port")
    if canonical_store is None or diagnosis_store is None or diagnosis_config is None:
        return {"status": "skipped_no_runtime"}
    if model_port is None:
        return {"status": "skipped_model_unconfigured"}
    try:
        # M-3 (review): the import AND the DiagnosisLoop construction live
        # INSIDE the fail-closed try — a missing module, a broken import or
        # a constructor error is a diagnosis-channel failure (typed
        # diagnosis_loop_error), never an exception bubbling into
        # _rolling_worker's outer handler where it would overwrite the
        # already-recorded reflection result.
        from sar_orch.diagnosis_loop import DiagnosisLoop

        loop = DiagnosisLoop(
            model_port=model_port,
            store=canonical_store,
            scope_id=snapshot.scope_id,
            diagnosis_store=diagnosis_store,
            config=diagnosis_config,
        )
        result = loop.run()
        return {
            "status": result.status,
            "rounds": result.rounds,
            "written": result.written,
            "reason": result.reason,
            "audit_event_id": result.audit_event_id,
            "audit_error": result.audit_error,
            # R4 补观测: pure additive observation fields, propagated
            # verbatim from DiagnosisLoopResult (None on early-exit paths).
            "round_latencies": result.round_latencies,
            "evidence_sec": result.evidence_sec,
            "duration_sec": result.duration_sec,
        }
    except Exception:  # fail-closed diagnosis channel
        logger.exception("diagnosis loop failed")
        return {"status": "failed", "reason": "diagnosis_loop_error"}


def _record_inflight_result(result: dict[str, Any]) -> None:
    """Record the worker result WITHOUT clearing the in-flight flag.

    M-4 (review): the rolling worker persists the reflection result as soon
    as it completes so the terminal drain can already read it; the in-flight
    flag stays set until the whole worker (reflection + diagnosis channel)
    finishes, preserving the coalesce single-flight slot.
    """
    with _inflight_lock:
        _inflight["result"] = result


def _set_inflight_result(result: dict[str, Any]) -> None:
    with _inflight_lock:
        _inflight["result"] = result
        _inflight["active"] = False


# ── rolling intermediate-state transcript (W2, trajectory-audit Gap H4) ─────

_ROLLING_STATE_STATUSES = frozenset({"rejected", "timeout", "rounds_exhausted"})


def _transcripts_path() -> Path | None:
    """Derive ``<memory_root>/diagnosis/transcripts.ndjson`` from the
    run-local stores (either channel anchors the same memory_root).

    - diagnosis store:  ``<memory_root>/diagnosis/diagnosis.sqlite3``
    - long-term store:  ``<memory_root>/long_term/long_term.sqlite3``
    Both collapse to ``<memory_root>/diagnosis/transcripts.ndjson``, the
    same file the diagnosis loop appends per-round rows to.
    """
    for candidate in (_runtime.get("diagnosis_store"), _runtime.get("store")):
        db_path = getattr(candidate, "db_path", None)
        if db_path is not None:
            return Path(db_path).parent.parent / "diagnosis" / "transcripts.ndjson"
    return None


def _append_rolling_state(
    *,
    scope_id: str,
    channel: str,
    status: str,
    rounds: int | None = None,
    reason: str | None = None,
    run_id: str | None = None,
) -> None:
    """Append one rolling intermediate-state row to the transcripts file.

    W2: the rolling worker's typed intermediate outcomes (rejected /
    timeout / rounds_exhausted) used to live only in ``_inflight`` and
    were overwritten by the next trigger — only the terminal drain's last
    snapshot reached ``run_metrics.json``.  Each such outcome is now
    appended durably (``kind=rolling_state``) so the full intermediate
    sequence survives.  Strictly best-effort (D8): a missing anchor store
    or any write error is logged and never affects the worker.
    """
    try:
        path = _transcripts_path()
        if path is None:
            return
        row = {
            "kind": "rolling_state",
            "ts": _utc_now(),
            "scope_id": scope_id,
            "channel": channel,
            "status": status,
            "rounds": rounds,
            "reason": reason,
            "run_id": run_id,
        }
        text = _redact_truth(json.dumps(row, ensure_ascii=False, default=str))
        with _transcript_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:  # best-effort tracing, never blocks
        logger.exception("rolling transcript write failed (channel %s)", channel)


def maybe_trigger_rolling_reflection() -> str:
    """Fire the rolling reflection trigger (async thread + coalesce).

    Returns ``started`` when a worker thread was launched, or
    ``coalesced_skip`` when a reflection is already in flight (or the
    min-interval throttle has not elapsed).  The worker consumes only
    committed snapshots via the configured snapshot provider.
    """
    global _inflight_thread
    with _inflight_lock:
        config = _runtime.get("config") or LongTermRuntimeConfig()
        now = time.monotonic()
        if _inflight["active"]:
            return "coalesced_skip"
        if now - _runtime.get("last_triggered_at", 0.0) < config.min_interval_sec:
            return "coalesced_skip"
        _runtime["last_triggered_at"] = now
        _inflight["active"] = True
        _inflight["result"] = None
        thread = threading.Thread(
            target=_rolling_worker, name="long-term-rolling-reflection", daemon=True
        )
        _inflight_thread = thread
        thread.start()
        return "started"


# ── terminal drain ──────────────────────────────────────────────────────────

def drain_inflight_reflection(timeout_sec: float = 60) -> DrainResult:
    """Join the in-flight rolling reflection with a timeout (terminal).

    Returns ``status == "joined"`` when no thread was in flight or it
    finished within ``timeout_sec``, or ``status == "timeout"`` when the
    join timed out — the caller then records a typed timeout status and the
    run exits without waiting (main plan §3.4.1).
    """
    with _inflight_lock:
        thread = _inflight_thread
    if thread is None or not thread.is_alive():
        return DrainResult(status="joined", result=_inflight_result())
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        return DrainResult(status="timeout", result=_inflight_result())
    return DrainResult(status="joined", result=_inflight_result())


def _inflight_result() -> dict[str, Any] | None:
    with _inflight_lock:
        return _inflight.get("result")
