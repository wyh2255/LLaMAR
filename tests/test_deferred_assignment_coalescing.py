"""Phase0 deferred last-write-wins assignment coalescing (no cancel-thrash)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry, AgentStatus
from a2a.coordinator.mission_runtime import MissionRuntimeManager, PhysicalState
from a2a.coordinator.task_store import TaskStore


class TrackingRouter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def send_task_async(
        self,
        agent_id: str,
        prompt: str,
        callback_url: str,
        task_id: str,
        *,
        context_id: str | None = None,
    ) -> str:
        self.calls.append(task_id)
        self.prompts.append(prompt)
        return f"worker-task-{len(self.calls)}"


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="worker",
            endpoint="http://alice",
            status=AgentStatus.ONLINE,
        )
    )
    return registry


def _runtime_store(context_id: str, router: TrackingRouter | None = None):
    manager = MissionRuntimeManager()
    runtime = manager.admit(context_id)
    store = TaskStore("request", router=router or TrackingRouter(), context_id=context_id)
    store.attach_runtime(runtime)
    return manager, runtime, store


@pytest.mark.asyncio
async def test_a_runtime_second_assign_defers_without_cancel_or_busy():
    """A: active worker + second assign -> success queued; no cancel; no 2nd physical."""
    cancel_calls: list[tuple[str, str]] = []

    async def fake_cancel(worker_id: str, worker_task_id: str):
        cancel_calls.append((worker_id, worker_task_id))
        return "TASK_STATE_CANCELED"

    manager = MissionRuntimeManager(cancel_adapter=fake_cancel)
    runtime = manager.admit("ctx-defer-a")
    router = TrackingRouter()
    store = TaskStore("request", router=router, context_id="ctx-defer-a")
    store.attach_runtime(runtime)
    facade = SendMessageTool(store, _registry())

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first mission"
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert first.success is True
    assert first.data is not None
    assert first.data.get("queued") is False
    old_id = first.data["dispatch_id"]
    old = runtime.get_dispatch(old_id)
    assert old is not None
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")
    assert old.state is PhysicalState.RUNNING

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="deferred mission"
    )

    assert second.success is True
    assert second.error is None
    assert second.error != "worker_busy"
    assert second.error != "replacement_cancel_pending"
    assert second.data is not None
    assert second.data.get("queued") is True
    assert second.data.get("deferred") is True
    assert second.data.get("worker_id") == "Alice"
    assert "dispatch_id" not in second.data or second.data.get("dispatch_id") is None
    assert cancel_calls == []
    assert len(runtime.dispatches) == 1
    assert store.get_active_tasks_by_worker("Alice") == [old_id]
    assert len(router.calls) == 1
    assert old.state is PhysicalState.RUNNING
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_b_terminal_activates_latest_coalesced_deferred_once():
    """B: terminal activates only latest LWW deferred; one new physical; no double activation."""
    router = TrackingRouter()
    _manager, runtime, store = _runtime_store("ctx-defer-b", router)
    facade = SendMessageTool(store, _registry())
    activations: list[str] = []

    # Install hook via first idle assign path, then wrap for counting.
    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    original_cb = store._deferred_activation_callback  # noqa: SLF001
    assert original_cb is not None

    async def counting_cb(worker_id: str, content: str, related_task_id: str | None):
        activations.append(content)
        return await original_cb(worker_id, content, related_task_id)

    store.set_deferred_activation_callback(counting_cb)

    old_id = first.data["dispatch_id"]
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")

    r1 = await facade.execute(
        message_type="assign_task", who="Alice", content="stale deferred"
    )
    r2 = await facade.execute(
        message_type="assign_task", who="Alice", content="latest deferred"
    )
    assert r1.success and r1.data.get("queued") is True
    assert r2.success and r2.data.get("queued") is True
    assert r2.data.get("coalesced") is True or r2.data.get("replaced_deferred") is True
    assert len(runtime.dispatches) == 1
    assert len(router.calls) == 1

    store.apply_physical_status(old_id, "TASK_STATE_COMPLETED", source="test")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert activations == ["latest deferred"]
    assert len(router.calls) == 2
    assert router.prompts[-1] == "latest deferred"
    assert len(runtime.dispatches) == 2
    new_ids = [d for d in runtime.dispatches if d != old_id]
    assert len(new_ids) == 1
    assert store.get_active_tasks_by_worker("Alice") == new_ids
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_c_terminal_after_store_close_does_not_activate_deferred():
    """C: terminal status after store/runtime close cannot activate deferred work."""
    router = TrackingRouter()
    _manager, runtime, store = _runtime_store("ctx-defer-c", router)
    facade = SendMessageTool(store, _registry())

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    await asyncio.sleep(0)
    old_id = first.data["dispatch_id"]
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="should not run"
    )
    assert second.data.get("queued") is True

    store.close()
    store.apply_physical_status(old_id, "TASK_STATE_COMPLETED", source="test")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(router.calls) == 1
    assert len(runtime.dispatches) == 1
    assert store.get_deferred_assignment("Alice") is None
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_c_runtime_abort_terminal_does_not_activate_deferred():
    """Abort transitions are teardown, never an availability signal for queued work."""

    async def fake_cancel(worker_id: str, worker_task_id: str):
        return "TASK_STATE_CANCELED"

    manager = MissionRuntimeManager(cancel_adapter=fake_cancel)
    runtime = manager.admit("ctx-defer-abort")
    router = TrackingRouter()
    store = TaskStore(
        "request", router=cast(Any, router), context_id="ctx-defer-abort"
    )
    store.attach_runtime(runtime)
    facade = SendMessageTool(store, _registry())

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    assert first.data is not None
    old_id = first.data["dispatch_id"]
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")
    queued = await facade.execute(
        message_type="assign_task", who="Alice", content="must not revive"
    )
    assert queued.data is not None
    assert queued.data.get("queued") is True

    await runtime.abort("mission_complete")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(router.calls) == 1
    assert store.get_deferred_assignment("Alice") is not None
    store.close()


@pytest.mark.asyncio
async def test_d_activation_failure_retains_deferred_for_retry():
    """D: transient dispatch failure retains deferred slot; no silent drop."""
    router = TrackingRouter()
    _manager, runtime, store = _runtime_store("ctx-defer-d", router)
    facade = SendMessageTool(store, _registry())
    fail_once = {"n": 0}

    async def flaky_cb(worker_id: str, content: str, related_task_id: str | None):
        fail_once["n"] += 1
        if fail_once["n"] == 1:
            raise RuntimeError("transient dispatch failure")
        return await facade._dispatch_tool.execute(  # noqa: SLF001
            agent_id=worker_id,
            prompt=content,
            task_id=related_task_id,
        )

    store.set_deferred_activation_callback(flaky_cb)

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    await asyncio.sleep(0)
    old_id = first.data["dispatch_id"]
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")

    await facade.execute(
        message_type="assign_task", who="Alice", content="retry me"
    )
    assert store.get_deferred_assignment("Alice") is not None

    store.apply_physical_status(old_id, "TASK_STATE_COMPLETED", source="test")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    pending = store.get_deferred_assignment("Alice")
    assert pending is not None
    assert pending["content"] == "retry me"
    assert len(router.calls) == 1
    assert fail_once["n"] == 1

    # Explicit retriable activation after transient failure.
    await store.retry_deferred_activation("Alice")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert fail_once["n"] == 2
    assert len(router.calls) == 2
    assert router.prompts[-1] == "retry me"
    assert store.get_deferred_assignment("Alice") is None
    await runtime.abort("test_cleanup")


@pytest.mark.asyncio
async def test_e_legacy_store_without_runtime_still_rejects_busy_worker():
    """E: legacy/no-runtime path retains worker_busy guard."""
    store = TaskStore("request", router=None)
    registry = _registry()
    store.add_adhoc_node("alice-task-1", worker_id="Alice", description="Active")
    store.set_state("alice-task-1", "running")
    facade = SendMessageTool(store, registry)

    result = await facade.execute(
        message_type="assign_task", who="Alice", content="new task"
    )

    assert result.success is False
    assert result.error == "worker_busy"
    assert "alice-task-1" in result.content


@pytest.mark.asyncio
async def test_no_assign_task_replacement_cancel_path_on_runtime():
    """Runtime path must not call cancel with assign_task_replacement."""
    cancel_reasons: list[str] = []

    async def fake_cancel(worker_id: str, worker_task_id: str):
        cancel_reasons.append("adapter")
        return "TASK_STATE_CANCELED"

    manager = MissionRuntimeManager(cancel_adapter=fake_cancel)
    runtime = manager.admit("ctx-no-replace")
    # Patch cancel_dispatch_remote to record reason
    original = runtime.cancel_dispatch_remote

    async def tracking_cancel(dispatch_id: str, *, reason: str = "cancel") -> Any:
        cancel_reasons.append(reason)
        return await original(dispatch_id, reason=reason)

    runtime.cancel_dispatch_remote = tracking_cancel  # type: ignore[method-assign]
    router = TrackingRouter()
    store = TaskStore("request", router=router, context_id="ctx-no-replace")
    store.attach_runtime(runtime)
    facade = SendMessageTool(store, _registry())

    first = await facade.execute(
        message_type="assign_task", who="Alice", content="first"
    )
    await asyncio.sleep(0)
    old_id = first.data["dispatch_id"]
    store.apply_physical_status(old_id, "TASK_STATE_SUBMITTED", source="test")
    store.apply_physical_status(old_id, "TASK_STATE_WORKING", source="test")

    second = await facade.execute(
        message_type="assign_task", who="Alice", content="next"
    )
    assert second.success is True
    assert second.data.get("queued") is True
    assert "assign_task_replacement" not in cancel_reasons
    await runtime.abort("test_cleanup")
