"""Regression tests for graceful A2A/Uvicorn server shutdown."""

import asyncio

import pytest

from a2a.shared.server_lifecycle import (
    shutdown_a2a_active_tasks,
    shutdown_uvicorn_server,
)


class _GracefulServer:
    def __init__(self) -> None:
        self.should_exit = False
        self.exited_normally = False
        self.was_cancelled = False

    async def serve(self) -> None:
        try:
            while not self.should_exit:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        self.exited_normally = True


class _StubbornServer(_GracefulServer):
    async def serve(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise


class _FakeEventQueue:
    def __init__(self) -> None:
        self.close_calls: list[bool] = []

    async def close(self, *, immediate: bool) -> None:
        self.close_calls.append(immediate)


class _FakeActiveTask:
    def __init__(self) -> None:
        self._event_queue_agent = _FakeEventQueue()
        self._event_queue_subscribers = _FakeEventQueue()
        self._is_finished = asyncio.Event()
        self.producer_cancelled = False
        self._producer_task = asyncio.create_task(self._run_producer())
        self._consumer_task = asyncio.create_task(self._run_consumer())

    async def _run_producer(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.producer_cancelled = True
            raise
        finally:
            await self._event_queue_agent.close(immediate=False)
            await self._event_queue_subscribers.close(immediate=False)
            self._is_finished.set()

    async def _run_consumer(self) -> None:
        await self._is_finished.wait()


class _FakeActiveTaskRegistry:
    def __init__(self, active_task: _FakeActiveTask) -> None:
        self._active_tasks = {"task-1": active_task}
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()


class _FakeRequestHandler:
    def __init__(self, active_task: _FakeActiveTask) -> None:
        self._active_task_registry = _FakeActiveTaskRegistry(active_task)


@pytest.mark.asyncio
async def test_shutdown_uvicorn_server_waits_for_graceful_serve_exit():
    server = _GracefulServer()
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0)

    await shutdown_uvicorn_server(server, task, timeout=0.1)

    assert server.should_exit is True
    assert task.done()
    assert server.exited_normally is True
    assert server.was_cancelled is False


@pytest.mark.asyncio
async def test_shutdown_uvicorn_server_cancels_only_after_timeout():
    server = _StubbornServer()
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0)

    await shutdown_uvicorn_server(server, task, timeout=0.01)

    assert server.should_exit is True
    assert task.cancelled()
    assert server.was_cancelled is True


@pytest.mark.asyncio
async def test_shutdown_a2a_active_tasks_closes_owned_event_queues():
    active_task = _FakeActiveTask()
    handler = _FakeRequestHandler(active_task)
    await asyncio.sleep(0)

    await shutdown_a2a_active_tasks(handler, timeout=0.1)

    assert active_task.producer_cancelled is True
    assert active_task._producer_task.done()
    assert active_task._consumer_task.done()
    assert True in active_task._event_queue_agent.close_calls
    assert True in active_task._event_queue_subscribers.close_calls
