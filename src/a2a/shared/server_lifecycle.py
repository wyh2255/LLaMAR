"""Shared graceful shutdown helpers for A2A and Uvicorn servers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any


logger = logging.getLogger(__name__)


async def shutdown_uvicorn_server(
    server: Any,
    server_task: asyncio.Task[Any] | None,
    *,
    timeout: float = 10.0,
) -> None:
    """Request Uvicorn shutdown and wait before using cancellation as a fallback."""
    if server is not None:
        server.should_exit = True
    if server_task is None:
        return

    try:
        await asyncio.wait_for(asyncio.shield(server_task), timeout=timeout)
    except asyncio.TimeoutError:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def close_event_queue_safely(
    event_queue: Any,
    *,
    immediate: bool = True,
    timeout: float = 2.0,
) -> None:
    """Close an EventQueue / EventQueueSource without racing its dispatcher.

    SDK ``EventQueueSource.close(immediate=True)`` marks the dispatcher cancel
    as expected before cancelling it.  Calling ``Task.cancel()`` on the
    dispatcher without that flag logs
    ``was cancelled without calling EventQueue.close() first`` and can leave
    pending tasks destroyed during interpreter shutdown.
    """
    if event_queue is None or not hasattr(event_queue, "close"):
        return
    # Prefer the public close path so EventQueueSource sets
    # ``_dispatcher_task_expected_to_cancel`` before cancelling.
    close_result = event_queue.close(immediate=immediate)
    if hasattr(close_result, "__await__"):
        try:
            await asyncio.wait_for(close_result, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs closing event queue; forcing dispatcher cancel",
                timeout,
            )
            dispatcher = getattr(event_queue, "_dispatcher_task", None)
            if isinstance(dispatcher, asyncio.Task) and not dispatcher.done():
                # Mark expected so CancelledError path does not warn.
                if hasattr(event_queue, "_dispatcher_task_expected_to_cancel"):
                    event_queue._dispatcher_task_expected_to_cancel = True  # noqa: SLF001
                dispatcher.cancel()
                try:
                    await dispatcher
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            logger.debug("Event queue close raised", exc_info=True)


async def shutdown_a2a_active_tasks(
    request_handler: Any,
    *,
    timeout: float = 10.0,
) -> None:
    """Stop SDK ActiveTasks without taking ownership of normal queue closing.

    ``ActiveTask`` producer/consumer finally blocks own normal EventQueue
    closure.  This helper only cancels and awaits their lifecycle tasks.  If a
    task remains unfinished after the bounded wait, it uses a serial immediate
    close as a last-resort escape hatch; completed tasks are never closed a
    second time here.

    EventQueueSource dispatchers are always closed via
    :func:`close_event_queue_safely` so cancellation is marked expected.
    """
    registry = getattr(request_handler, "_active_task_registry", None)
    if registry is None:
        return

    active_tasks_by_id = getattr(registry, "_active_tasks", None)
    if not isinstance(active_tasks_by_id, dict):
        return

    lock = getattr(registry, "_lock", None)
    if lock is not None:
        async with lock:
            active_tasks = list(active_tasks_by_id.values())
    else:
        active_tasks = list(active_tasks_by_id.values())

    lifecycle_tasks: list[asyncio.Task[Any]] = []
    for active_task in active_tasks:
        producer_task = getattr(active_task, "_producer_task", None)
        consumer_task = getattr(active_task, "_consumer_task", None)
        if isinstance(producer_task, asyncio.Task):
            if not producer_task.done():
                producer_task.cancel()
            lifecycle_tasks.append(producer_task)
        if isinstance(consumer_task, asyncio.Task):
            lifecycle_tasks.append(consumer_task)

    async def _await_lifecycle() -> None:
        results = await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result

    if lifecycle_tasks:
        try:
            await asyncio.wait_for(_await_lifecycle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs while shutting down %d A2A active task(s)",
                timeout,
                len(active_tasks),
            )
            for task in lifecycle_tasks:
                if not task.done():
                    task.cancel()
            await _await_lifecycle()

    # Close unfinished ActiveTask queues safely (marks EventQueueSource
    # dispatcher cancel as expected).  Finished tasks keep ownership of their
    # own normal close path — never double-close them here.
    for active_task in active_tasks:
        finished = getattr(active_task, "_is_finished", None)
        if finished is not None and finished.is_set():
            # Still drain any leftover EventQueueSource that producer finally
            # failed to close (defensive; no-op if already closed).
            for attribute in ("_event_queue_agent", "_event_queue_subscribers"):
                event_queue = getattr(active_task, attribute, None)
                if event_queue is None:
                    continue
                is_closed = getattr(event_queue, "is_closed", None)
                if callable(is_closed) and is_closed():
                    continue
                if getattr(event_queue, "_is_closed", False):
                    continue
                # Only force-close EventQueueSource-like objects that still have
                # a live dispatcher; never re-close a drained legacy queue.
                dispatcher = getattr(event_queue, "_dispatcher_task", None)
                if isinstance(dispatcher, asyncio.Task) and not dispatcher.done():
                    await close_event_queue_safely(event_queue, immediate=True)
            continue
        unfinished = any(
            isinstance(getattr(active_task, attribute, None), asyncio.Task)
            and not getattr(active_task, attribute).done()
            for attribute in ("_producer_task", "_consumer_task")
        )
        if not unfinished:
            # Lifecycle done but queue may still host a dispatcher task.
            for attribute in ("_event_queue_agent", "_event_queue_subscribers"):
                event_queue = getattr(active_task, attribute, None)
                dispatcher = getattr(event_queue, "_dispatcher_task", None) if event_queue else None
                if isinstance(dispatcher, asyncio.Task) and not dispatcher.done():
                    await close_event_queue_safely(event_queue, immediate=True)
            continue
        for attribute in ("_event_queue_agent", "_event_queue_subscribers"):
            event_queue = getattr(active_task, attribute, None)
            await close_event_queue_safely(event_queue, immediate=True)
