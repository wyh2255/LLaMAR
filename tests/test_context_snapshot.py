"""Tests for ContextManager task snapshot storage."""

from Agent.worker_agent.context import ContextManager
from Agent.worker_agent.schema import Message


def test_save_and_load_snapshot():
    ctx = ContextManager()
    msgs = [
        Message(role="system", content="prompt"),
        Message(role="user", content="go"),
        Message(role="assistant", content="ok"),
    ]
    ctx.save_snapshot("task-1", msgs)
    loaded = ctx.load_snapshot("task-1")
    assert loaded is not None
    assert len(loaded) == 3
    assert loaded[0].role == "system"
    assert loaded[2].content == "ok"


def test_load_snapshot_returns_none_if_not_saved():
    ctx = ContextManager()
    assert ctx.load_snapshot("nonexistent") is None


def test_load_snapshot_pops_after_read():
    """load_snapshot 是一次性的——第二次返回 None。"""
    ctx = ContextManager()
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])
    first = ctx.load_snapshot("task-1")
    second = ctx.load_snapshot("task-1")
    assert first is not None
    assert second is None


def test_snapshots_isolated_by_task_id():
    ctx = ContextManager()
    ctx.save_snapshot("task-a", [Message(role="user", content="a")])
    ctx.save_snapshot("task-b", [Message(role="user", content="b")])
    loaded_a = ctx.load_snapshot("task-a")
    loaded_b = ctx.load_snapshot("task-b")
    assert loaded_a[0].content == "a"
    assert loaded_b[0].content == "b"


def test_save_snapshot_does_not_mutate_original():
    """保存的快照应该是副本，修改原始不影响快照。"""
    ctx = ContextManager()
    msgs = [Message(role="user", content="original")]
    ctx.save_snapshot("task-1", msgs)
    msgs.append(Message(role="assistant", content="appended"))
    loaded = ctx.load_snapshot("task-1")
    assert len(loaded) == 1
    assert loaded[0].content == "original"
