"""Tests for AgentAdapter cancel-by-task-id behavior."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from a2a.worker.agent_adapter import AgentAdapter
from Agent.worker_agent.schema import RunResult


class _FakeUpdater:
    def __init__(self):
        self.start_work = AsyncMock()
        self.requires_input = AsyncMock()
        self.add_artifact = AsyncMock()
        self.complete = AsyncMock()
        self.failed = AsyncMock()
        self.cancel = AsyncMock()


class _FakeContext:
    def __init__(self, task_id="task-1", context_id="ctx-shared", user_input="go"):
        self._task_id = task_id
        self._context_id = context_id
        self._task = MagicMock()
        self._task.id = task_id
        self._task.context_id = context_id
        self._input = user_input

    @property
    def task_id(self):
        return self._task_id

    @property
    def context_id(self):
        return self._context_id

    @property
    def current_task(self):
        return self._task

    def get_user_input(self):
        return self._input


class _FakeEventQueue:
    def __init__(self):
        self.enqueue_event = AsyncMock()


@pytest.mark.asyncio
async def test_execute_registers_cancel_event_per_task():
    """Each execute() call registers a cancel_event keyed by task_id."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    assert "task-1" not in adapter._task_cancel_events  # cleaned up in finally
    # Verify submit was called with a cancel_event
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("cancel_event") is not None


@pytest.mark.asyncio
async def test_execute_non_snapshot_path_registers_cancel_event():
    """非 snapshot 路径也注册 cancel_event."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller._get_session = None  # force non-snapshot path
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-3", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    assert "task-3" not in adapter._task_cancel_events  # cleaned up in finally
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("cancel_event") is not None


@pytest.mark.asyncio
async def test_cancel_sets_event_for_specific_task_only():
    """cancel() should set only the target task's event and call updater.cancel()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.cancel = MagicMock()  # should NOT be called
    adapter._task_cancel_events = {
        "task-1": asyncio.Event(),
        "task-2": asyncio.Event(),
    }

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        await adapter.cancel(fake_ctx, fake_queue)

    assert adapter._task_cancel_events["task-1"].is_set()
    assert not adapter._task_cancel_events["task-2"].is_set()
    fake_updater.cancel.assert_called_once()
    adapter._controller.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_for_completed_task_does_not_raise():
    """cancel() on a finished task (no cancel_event) should not error."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._task_cancel_events = {}
    adapter._controller = MagicMock()

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-gone", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        await adapter.cancel(fake_ctx, fake_queue)

    fake_updater.cancel.assert_called_once()
    adapter._controller.cancel.assert_not_called()
