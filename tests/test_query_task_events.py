"""Tests for QueryTaskEventsTool — Coordinator 查询 Worker 任务状态。"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from a2a.builtin_tools.query_task_events import QueryTaskEventsTool
from a2a.coordinator.event_store import event_store
from a2a.coordinator.task_store import TaskStore


@pytest.fixture(autouse=True)
def clear_event_store():
    """每个测试前清空全局 EventStore 单例。"""
    event_store.clear()


@pytest.fixture
def store():
    """提供双向映射的 TaskStore mock。"""
    mock_store = MagicMock(spec=TaskStore)
    mock_store._worker_to_dispatch = {}
    mock_store._dispatch_to_worker = {}
    return mock_store


@pytest.fixture
def tool(store):
    return QueryTaskEventsTool(store)


@pytest.mark.asyncio
async def test_query_one_resolves_dispatch_to_worker(store, tool):
    """dispatch_id 应通过 _dispatch_to_worker 解析为 worker_id。"""
    store._dispatch_to_worker["dispatch-1"] = "worker-uuid-1"
    event_store.append("worker-uuid-1", "help_request", text="Need help")

    result = await tool.execute(["dispatch-1"], timeout=0)

    assert result.success is True
    states = json.loads(result.content)
    assert len(states) == 1
    assert states[0]["task_id"] == "dispatch-1"
    assert states[0]["state"] == "INPUT_REQUIRED"
    assert states[0]["text"] == "Need help"


@pytest.mark.asyncio
async def test_query_one_no_mapping_returns_dispatched(store, tool):
    """未注册 worker 映射时，只能看到 dispatch_id 下的 task_created 事件。"""
    event_store.append("dispatch-1", "task_created", state="DISPATCHED")

    result = await tool.execute(["dispatch-1"], timeout=0)

    assert result.success is True
    states = json.loads(result.content)
    assert states[0]["state"] == "DISPATCHED"


@pytest.mark.asyncio
async def test_query_one_worker_completed(store, tool):
    """worker_id 下的 status_update(COMPLETED) 能被正确合并。"""
    store._dispatch_to_worker["dispatch-1"] = "worker-uuid-1"
    event_store.append("worker-uuid-1", "status_update", state="TASK_STATE_COMPLETED")

    result = await tool.execute(["dispatch-1"], timeout=0)

    states = json.loads(result.content)
    assert states[0]["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_query_one_worker_failed(store, tool):
    """worker_id 下的 status_update(FAILED) 能被正确合并。"""
    store._dispatch_to_worker["dispatch-1"] = "worker-uuid-1"
    event_store.append("worker-uuid-1", "status_update", state="FAILED", text="boom")

    result = await tool.execute(["dispatch-1"], timeout=0)

    states = json.loads(result.content)
    assert states[0]["state"] == "FAILED"
    assert states[0]["text"] == "boom"


@pytest.mark.asyncio
async def test_query_one_uses_dispatch_id_not_worker_id(store, tool):
    """修复前的 bug：用 _worker_to_dispatch 查 dispatch_id 会返回 None。

    此测试确保使用正确的 _dispatch_to_worker 映射。
    """
    store._dispatch_to_worker["dispatch-1"] = "worker-uuid-1"
    # 故意在 _worker_to_dispatch 里也放一个 dispatch_id -> worker 的反向条目
    # 以模拟旧代码如果拿错字典也不会误用
    store._worker_to_dispatch["dispatch-1"] = "wrong-worker"
    event_store.append("worker-uuid-1", "help_request", text="Help")

    result = await tool.execute(["dispatch-1"], timeout=0)

    states = json.loads(result.content)
    assert states[0]["state"] == "INPUT_REQUIRED"
    assert states[0]["text"] == "Help"


@pytest.mark.asyncio
async def test_timeout_clamped_to_five_seconds(store, tool, monkeypatch):
    """timeout=20 应被钳制到 5s：不可 actionable 时盲等不超过 5s+1 个粒度。

    用假时钟 + 假 sleep 推进时间，避免测试真实等待。
    """
    event_store.append("dispatch-1", "task_created", state="DISPATCHED")

    fake_now = 0.0

    class FakeLoop:
        def time(self):
            return fake_now

    async def fake_sleep(seconds):
        nonlocal fake_now
        fake_now += seconds

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: FakeLoop())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await tool.execute(["dispatch-1"], timeout=20)

    states = json.loads(result.content)
    assert states[0]["state"] == "DISPATCHED"
    # 未钳制时会等到 20s；钳制后 5s 即返回
    assert fake_now <= 5.5
