"""Shared A2A stream response parser for deterministic terminal ACK.

Extracted from ``CoordinatorSenderService`` and ``WorkerPeerSenderService``
to avoid duplicating the fragile ``StreamResponse`` parsing logic.

Only ``task.status`` and ``status_update.status`` fields are inspected for
terminal state.  ``artifact_update`` and ``message`` are **not** treated as
implicit completion — the protocol requires an explicit terminal status.

Returns:
    ``{"success": True, "transport_task_id": "<id>"}`` on terminal success,
    ``{"success": False, "transport_task_id": "<id>", "error": "<reason>",
      "task_status": "<state>"}`` on terminal failure or no-terminal.
"""

from __future__ import annotations

from typing import Any


def _task_state_name(state: Any) -> str:
    from a2a.types.a2a_pb2 import TaskState

    if isinstance(state, int):
        for name, val in TaskState.items():
            if val == state:
                return name
        return f"UNKNOWN:{state}"
    return str(state)


def _is_terminal_success(state_name: str | None) -> bool:
    return state_name in (
        "TASK_STATE_COMPLETED",
        "COMPLETED",
        "SUCCEEDED",
    )


def _is_terminal_failure(state_name: str | None) -> bool:
    return state_name in (
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_CANCELED",
        "FAILED",
        "REJECTED",
        "CANCELED",
    )


def _status_message(status: Any) -> str | None:
    try:
        msg = status.status_message
        if msg:
            parts = getattr(msg, "parts", [])
            if parts:
                return parts[0].text
    except Exception:
        pass
    return None


async def parse_stream_for_terminal(
    stream,
    timeout: float,
) -> dict[str, Any]:
    """Iterate *stream* and return on the first terminal status event.

    Args:
        stream: async iterable yielding ``StreamResponse`` protos.
        timeout: ``asyncio.wait_for`` deadline in seconds.

    Returns:
        See module docstring.
    """
    import asyncio

    async def _iter():
        task_id: str | None = None
        async for sr in stream:
            if sr.HasField("task"):
                t = sr.task
                task_id = t.id
                if t.HasField("status"):
                    state = _task_state_name(t.status.state)
                    if _is_terminal_success(state):
                        return {"success": True, "transport_task_id": task_id}
                    if _is_terminal_failure(state):
                        err = _status_message(t.status) or state
                        return {
                            "success": False,
                            "transport_task_id": task_id,
                            "error": err,
                            "task_status": state,
                        }
            if sr.HasField("status_update"):
                su = sr.status_update
                su_task_id = su.task_id or task_id or "?"
                if su.HasField("status"):
                    state = _task_state_name(su.status.state)
                    if _is_terminal_success(state):
                        return {"success": True, "transport_task_id": su_task_id}
                    if _is_terminal_failure(state):
                        err = _status_message(su.status) or state
                        return {
                            "success": False,
                            "transport_task_id": su_task_id,
                            "error": err,
                            "task_status": state,
                        }

        if task_id:
            return {
                "success": False,
                "transport_task_id": task_id,
                "error": "No terminal status received",
            }
        return {"success": False, "error": "No task in response"}

    return await asyncio.wait_for(_iter(), timeout=timeout)
