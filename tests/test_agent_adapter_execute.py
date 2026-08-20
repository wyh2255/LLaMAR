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
        self.reject = AsyncMock()


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


# ---------------------------------------------------------------------------
# 问题 2 回归：任务生命周期回调（worker 空闲心跳依赖）
# ---------------------------------------------------------------------------


def _make_adapter_with_lifecycle(calls: list, submit_result=None, submit_error=None):
    """构造一个带 task_lifecycle_cb 的 AgentAdapter（绕过 __init__，与既有
    测试风格一致）。"""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    if submit_error is not None:
        adapter._controller.submit = AsyncMock(side_effect=submit_error)
    else:
        adapter._controller.submit = AsyncMock(
            return_value=submit_result or RunResult(content="done", success=True)
        )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}
    adapter._task_lifecycle_cb = calls.append
    return adapter


@pytest.mark.asyncio
async def test_execute_fires_task_lifecycle_callback_true_then_false():
    """task_lifecycle_cb 在 execute 进入时收到 True，退出时收到 False。"""
    calls: list[bool] = []
    adapter = _make_adapter_with_lifecycle(calls)
    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext()
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    assert calls == [True, False]


@pytest.mark.asyncio
async def test_execute_fires_task_lifecycle_callback_false_on_exception():
    """异常路径下 finally 仍回调 False，任务活跃标志不会卡死为 True。"""
    calls: list[bool] = []
    adapter = _make_adapter_with_lifecycle(
        calls, submit_error=RuntimeError("llm boom")
    )
    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext()
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    with pytest.raises(RuntimeError, match="llm boom"):
                        await adapter.execute(fake_ctx, fake_queue)

    assert calls == [True, False]


@pytest.mark.asyncio
async def test_envelope_adapter_execute_fires_task_lifecycle_callback_on_local_path():
    """EnvelopeAwareAdapter 的本地控制路径（reject）同样维护 True→False，
    空闲心跳不会在 mail/team_update 处理期间并发提交 NoOp。"""
    from a2a.worker.agent_adapter import EnvelopeAwareAdapter

    class _FakeClassifyResult:
        action = "reject"
        reason = "unsigned envelope"

    calls: list[bool] = []
    adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
    adapter._ingress = MagicMock()
    adapter._ingress.classify.return_value = _FakeClassifyResult()
    adapter._mailbox = MagicMock()
    adapter._team_state = MagicMock()
    adapter._task_lifecycle_cb = calls.append
    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext()
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
            await adapter.execute(fake_ctx, fake_queue)

    assert calls == [True, False]
    fake_updater.reject.assert_called_once()
