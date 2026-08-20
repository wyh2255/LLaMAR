"""Teardown-safety tests for the candidate 3202abb crash.

Regression for ``TypeError: Object of type A2AClientError is not JSON
serializable`` at ``mission_runtime.py`` ``_persist`` during
``coordinator.stop()`` teardown.  Covers:

- exceptions are reduced to safe structured strings (code + message) before
  entering dispatch state / task events;
- ``MissionRuntimeManager._persist`` sanitizes a poisoned payload instead of
  raising, so shutdown always completes;
- ``ExperimentLogger.freeze_terminal()`` blocks post-terminal outcome rows
  from leaking into ``router_interactions.csv`` / ``agent_interactions.csv``.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from pathlib import Path

import pytest

from Agent.error_taxonomy import (
    FRAMEWORK_ERROR_CODES,
    NETWORK_ERROR,
    WORKER_UNREACHABLE,
    classify_error,
    exception_error_code,
    exception_to_safe_string,
)

# ---------------------------------------------------------------------------
# Taxonomy: exception -> allowlisted code + safe string
# ---------------------------------------------------------------------------


def test_exception_error_code_maps_network_layer_exceptions():
    from a2a.client.errors import A2AClientError

    assert exception_error_code(A2AClientError("boom")) == NETWORK_ERROR
    assert exception_error_code(ValueError("boom")) not in FRAMEWORK_ERROR_CODES


def test_exception_error_code_maps_worker_and_connection_failures():
    assert exception_error_code(ConnectionError("refused")) == "connection_failed"
    assert exception_error_code(ConnectionRefusedError("refused")) == "connection_failed"

    class _WorkerMissing(Exception):
        pass

    assert exception_error_code(_WorkerMissing("gone")) == WORKER_UNREACHABLE


def test_exception_to_safe_string_is_allowlisted_and_json_safe():
    from a2a.client.errors import A2AClientError

    text = exception_to_safe_string(A2AClientError("worker unreachable"))
    assert text.startswith(f"{NETWORK_ERROR}: A2AClientError: worker unreachable")
    assert classify_error(text) == NETWORK_ERROR
    # JSON round-trip is safe (no exception object is embedded).
    assert json.loads(json.dumps(text)) == text


def test_exception_error_codes_are_allowlisted():
    assert NETWORK_ERROR in FRAMEWORK_ERROR_CODES
    assert WORKER_UNREACHABLE in FRAMEWORK_ERROR_CODES
    assert classify_error(NETWORK_ERROR) == NETWORK_ERROR
    assert classify_error(WORKER_UNREACHABLE) == WORKER_UNREACHABLE


# ---------------------------------------------------------------------------
# Dispatch failing with A2AClientError persists cleanly (teardown completes)
# ---------------------------------------------------------------------------


class _BoomRouter:
    def __init__(self, exc):
        self._exc = exc

    async def send_task_async(
        self, agent_id, prompt, callback_url, task_id, context_id=None
    ):
        raise self._exc


def _make_store(router, state_path: Path):
    from a2a.coordinator.mission_runtime import MissionRuntimeManager
    from a2a.coordinator.task_store import TaskStore

    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-dispatch-error")
    store = TaskStore("request", router=router)
    store.max_tasks = 10
    store.attach_runtime(runtime)
    return manager, runtime, store


async def _wait_for(cond, timeout: float = 5.0) -> bool:
    """Spin the event loop until *cond* is truthy or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def _any_failed(runtime) -> bool:
    return any(d.state.value == "FAILED" for d in runtime.dispatches.values())


@pytest.mark.asyncio
async def test_dispatch_a2a_client_error_persists_cleanly(tmp_path):
    from a2a.client.errors import A2AClientError

    from a2a.builtin_tools.dispatch_task import DispatchTaskTool

    state_path = tmp_path / "coordinator-state.json"
    manager, runtime, store = _make_store(
        _BoomRouter(A2AClientError("worker endpoint unreachable")), state_path
    )

    tool = DispatchTaskTool(store, coordinator_host="localhost", coordinator_port=8080)
    result = await tool.execute(agent_id="Alice", prompt="go")
    assert result.success is True  # non-blocking dispatch returns immediately

    # Let the background dispatch task fail and persist the FAILED transition.
    assert await _wait_for(lambda: runtime.dispatches and _any_failed(runtime))

    dispatch = next(iter(runtime.dispatches.values()))
    assert dispatch.state.value == "FAILED"
    assert isinstance(dispatch.result, str)
    assert dispatch.result.startswith(NETWORK_ERROR)

    # The control-state payload must be JSON-serializable even with the failed
    # dispatch (no raw exception object leaks into state).
    parsed = json.loads(json.dumps(manager._state_payload(), ensure_ascii=False))
    assert parsed["dispatches"][0]["result"] == dispatch.result

    # Retrieve the failed future to avoid "exception never retrieved" noise.
    for future in list(runtime.futures.values()):
        if future.done() and not future.cancelled():
            future.exception()

    # Teardown (abort / shutdown) must complete without raising.
    await runtime.abort("shutdown")

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["active_context_id"] is None
    assert persisted["journal"]


@pytest.mark.asyncio
async def test_dispatch_error_event_text_is_structured_and_json_safe(tmp_path):
    """The FAILED status_update text must carry the allowlisted code, never the
    raw exception repr."""
    from a2a.client.errors import A2AClientError

    from a2a.builtin_tools.dispatch_task import DispatchTaskTool
    from a2a.coordinator.event_store import event_store

    original_log_dir = event_store._log_dir
    state_path = tmp_path / "coordinator-state.json"
    _manager, runtime, store = _make_store(
        _BoomRouter(A2AClientError("worker unreachable")), state_path
    )
    event_store.set_log_dir(str(tmp_path / "events"))
    try:
        tool = DispatchTaskTool(
            store, coordinator_host="localhost", coordinator_port=8080
        )
        await tool.execute(agent_id="Alice", prompt="go")
        assert await _wait_for(lambda: runtime.dispatches and _any_failed(runtime))

        dispatch = next(iter(runtime.dispatches.values()))
        records = event_store._events.get(dispatch.dispatch_id, [])
        failed = [r for r in records if r.event_type == "status_update"]
        assert failed, "expected a status_update event"
        assert failed[-1].state == "FAILED"
        text = failed[-1].text or ""
        # Structured, JSON-safe string with an allowlisted code prefix — never
        # the raw exception object.
        assert text.startswith(NETWORK_ERROR)
        assert classify_error(text) == NETWORK_ERROR
        assert json.loads(json.dumps(text)) == text
        for future in list(runtime.futures.values()):
            if future.done() and not future.cancelled():
                future.exception()  # retrieve to avoid "never retrieved" noise
    finally:
        event_store._log_dir = original_log_dir
        event_store.clear()
        await runtime.abort("shutdown")


# ---------------------------------------------------------------------------
# _persist sanitizes a poisoned payload instead of raising
# ---------------------------------------------------------------------------


def test_persist_sanitizes_poisoned_payload(tmp_path, caplog):
    from a2a.coordinator.mission_runtime import MissionRuntimeManager

    caplog.set_level(logging.WARNING, logger="a2a.coordinator.mission_runtime")

    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-poisoned")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    # A raw exception object slips into dispatch.result (the bug that crashed
    # teardown).  _persist must sanitize instead of raising.
    runtime.apply_physical_status(
        dispatch.dispatch_id,
        "FAILED",
        source="dispatch_error",
        result=ValueError("boom"),
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "boom" in persisted["dispatches"][0]["result"]
    assert any("not JSON-serializable" in r.message for r in caplog.records)

    # Subsequent persist of a poisoned payload also survives.
    manager._persist()
    assert state_path.exists()


def test_persist_sanitizes_nested_non_serializable_leaves(tmp_path):
    from a2a.coordinator.mission_runtime import MissionRuntimeManager

    state_path = tmp_path / "coordinator-state.json"
    manager = MissionRuntimeManager(state_path=state_path)
    runtime = manager.admit("ctx-nested")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    # Nested structure carrying an enum + exception.
    runtime._dispatches[dispatch.dispatch_id].result = {
        "meta": {"exc": RuntimeError("deep-boom"), "state": dispatch.state}
    }
    manager._persist()

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "deep-boom" in persisted["dispatches"][0]["result"]["meta"]["exc"]
    assert persisted["dispatches"][0]["result"]["meta"]["state"] == "DISPATCHING"


# ---------------------------------------------------------------------------
# Terminal freeze blocks post-terminal outcome rows
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_terminal_freeze_blocks_post_terminal_router_rows(tmp_path):
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_router_interaction(
        step=1,
        subtask="pre-1",
        assigned_to="Alice",
        event_type="send_message_result",
        success=False,
        error_code="network_error",
    )
    logger.log_router_interaction(
        step=2, subtask="pre-2", assigned_to="Alice", event_type="send_message_result"
    )
    logger.freeze_terminal()

    # Post-terminal rows must be dropped.
    logger.log_router_interaction(
        step=3,
        subtask="post-1",
        assigned_to="Charlie",
        event_type="send_message_result",
        success=False,
        error_code="unclassified_tool_error",
    )
    logger.log_router_interaction(
        step=3,
        subtask="post-2",
        assigned_to="Bob",
        event_type="send_message_result",
        success=False,
        error_code="unclassified_tool_error",
    )
    logger.close()

    rows = _read_csv(tmp_path / "router_interactions.csv")
    assert len(rows) == 2
    assert rows[0]["Subtask"] == "pre-1"
    assert rows[1]["Subtask"] == "pre-2"


def test_terminal_freeze_blocks_post_terminal_agent_rows(tmp_path):
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_agent_interaction(
        step=1,
        agent="Alice",
        tool_name="navigate_to",
        tool_args="{}",
        event_type="tool_result",
        success=True,
    )
    logger.freeze_terminal()
    logger.log_agent_interaction(
        step=3,
        agent="Charlie",
        tool_name="send_message",
        tool_args="{}",
        event_type="tool_result",
        success=False,
        error_code="worker_busy",
    )
    logger.close()

    rows = _read_csv(tmp_path / "agent_interactions.csv")
    assert len(rows) == 1
    assert rows[0]["Agent"] == "Alice"


def test_terminal_freeze_is_idempotent_and_does_not_break_summary(tmp_path):
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.freeze_terminal()
    logger.freeze_terminal()
    logger.log_step(
        step_num=1,
        actions=[],
        successes=[],
        observations=[],
        coverage=0.5,
        transport_rate=0.5,
        finished=True,
    )
    logger.set_end_reason("success")
    logger.log_router_interaction(
        step=2, subtask="ignored", assigned_to="Alice", event_type="send_message_result"
    )
    logger.close()

    assert (tmp_path / "summary.csv").exists()
    rows = _read_csv(tmp_path / "router_interactions.csv")
    assert rows == []
