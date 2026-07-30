"""Tests for AgentAdapter.execute() pause/resume dispatch."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from a2a.worker.agent_adapter import AgentAdapter
from Agent.worker_agent.schema import RunResult, Message


class _FakeUpdater:
    def __init__(self):
        self.start_work = AsyncMock()
        self.requires_input = AsyncMock()
        self.add_artifact = AsyncMock()
        self.complete = AsyncMock()
        self.failed = AsyncMock()
        self.cancel = AsyncMock()


class _FakeContext:
    def __init__(self, task_id="task-1", context_id="ctx-1", user_input="go"):
        self._task = MagicMock()
        self._task.id = task_id
        self._task.context_id = context_id
        self._input = user_input

    @property
    def current_task(self):
        return self._task

    def get_user_input(self):
        return self._input


class _FakeEventQueue:
    def __init__(self):
        self.enqueue_event = AsyncMock()


@pytest.mark.asyncio
async def test_execute_calls_requires_input_on_need_input():
    """When submit returns need_input=True, execute() should call updater.requires_input()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="Where?", success=False, need_input=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}
    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext()
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    fake_updater.requires_input.assert_called_once()
    fake_updater.complete.assert_not_called()


@pytest.mark.asyncio
async def test_execute_resume_loads_snapshot():
    """When snapshot exists, execute() should pass initial_messages to submit()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()

    # Need a real ContextManager with a snapshot
    from Agent.worker_agent.context import ContextManager

    ctx_manager = ContextManager()
    ctx_manager.save_snapshot("task-1", [Message(role="system", content="sys")])
    adapter._controller.get_snapshot = MagicMock(
        side_effect=lambda _cid, tid: ctx_manager.load_snapshot(tid)
    )

    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}
    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", user_input="Go to sector 7")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    # Verify submit was called with initial_messages
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("initial_messages") is not None
    # Verify snapshot was consumed
    assert ctx_manager.load_snapshot("task-1") is None
