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

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from a2a.coordinator.memory.contracts import (
    POLICY_VERSION,
    LongTermConfigError,
    LongTermRuntimeConfig,
    load_long_term_config,
)
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
    "configure_long_term_runtime",
    "drain_inflight_reflection",
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
}

_inflight_lock = threading.Lock()
_inflight_thread: threading.Thread | None = None
_inflight: dict[str, Any] = {
    "active": False,
    "result": None,
}


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
        )
        _inflight["active"] = False
        _inflight["result"] = None


# ── D8 provider/model adapter skeleton (no real model call in P4) ───────────

def build_reflection_model_port(env: dict[str, str]) -> ReflectionModelPort | None:
    """Build the D8 reflection model port from ``.env`` values.

    Reads ``reflection_provider`` / ``reflection_model`` /
    ``reflection_api_key`` / ``reflection_api_base`` (falling back to the
    generic ``provider`` / ``model`` / ``api_key`` / ``api_base`` keys).
    Returns ``None`` when the provider, model or api key is missing — the
    caller then skips the reflection with a typed ``model_unconfigured``
    status instead of invoking anything.  P4 never performs a real model
    call.
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
    for the terminal drain.
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
        _set_inflight_result(
            {
                "status": result.status,
                "run_id": result.run_id,
                "long_term_memory_written": result.long_term_memory_written,
                "reason": result.reason,
            }
        )
    except Exception:
        logger.exception("rolling reflection failed")
        _set_inflight_result({"status": "failed", "reason": "rolling_worker_error"})


def _set_inflight_result(result: dict[str, Any]) -> None:
    with _inflight_lock:
        _inflight["result"] = result
        _inflight["active"] = False


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
