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


async def shutdown_a2a_active_tasks(
    request_handler: Any,
    *,
    timeout: float = 10.0,
) -> None:
    """Stop SDK ActiveTasks before their event loop and EventQueues disappear.

    a2a-sdk currently exposes no public registry shutdown API. Its ActiveTask
    producer is documented as safe to cancel and closes both EventQueueSource
    instances in ``finally``. The explicit immediate closes below are idempotent
    fallbacks for partially started or timed-out tasks.
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

    if lifecycle_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*lifecycle_tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs while shutting down %d A2A active task(s)",
                timeout,
                len(active_tasks),
            )
            for task in lifecycle_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)

    queue_closes = []
    for active_task in active_tasks:
        for attribute in ("_event_queue_agent", "_event_queue_subscribers"):
            event_queue = getattr(active_task, attribute, None)
            if event_queue is not None and hasattr(event_queue, "close"):
                queue_closes.append(event_queue.close(immediate=True))
    if queue_closes:
        await asyncio.gather(*queue_closes, return_exceptions=True)
