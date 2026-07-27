"""Focused regressions: production adapter wiring + terminal cancel idempotency."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus
from a2a.coordinator.mission_runtime import (
    MissionRuntimeManager,
    PhysicalState,
)
from a2a.coordinator.production_adapters import (
    build_delivery_adapter,
    build_dispatch_adapter,
    wire_production_adapters,
)
from a2a.coordinator.task_store import PlanNode, TaskStore
from a2a.coordinator.team_partition_service import (
    PartitionTransition,
    TeamAssignment,
    TeamPartitionService,
    TransitionStatus,
)


# ---------------------------------------------------------------------------
# Dispatch adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_adapter_calls_router_send_task_async():
    router = MagicMock()
    router.send_task_async = AsyncMock(return_value="wtid-alice-1")

    adapter = build_dispatch_adapter(
        router, coordinator_host="localhost", coordinator_port=8080
    )
    wtid = await adapter(
        "Alice",
        "explore north",
        "",
        "dsp_abc",
        "ctx-1",
    )
    assert wtid == "wtid-alice-1"
    router.send_task_async.assert_awaited_once()
    args = router.send_task_async.await_args
    assert args.args[0] == "Alice"
    assert args.args[1] == "explore north"
    assert "push-callback" in args.args[2]
    assert args.args[3] == "dsp_abc"
    assert args.kwargs.get("context_id") == "ctx-1"


@pytest.mark.asyncio
async def test_wire_production_adapters_installs_dispatch_on_manager():
    manager = MissionRuntimeManager()
    team = TeamPartitionService()
    router = MagicMock()
    router.send_task_async = AsyncMock(return_value="wtid-1")
    registry = MagicMock()

    result = wire_production_adapters(
        mission_runtime_manager=manager,
        team_partition_service=team,
        router=router,
        agent_registry=registry,
        worker_registry=None,
        coordinator_host="localhost",
        coordinator_port=8080,
        coordinator_secret=None,
    )
    assert result["dispatch"] is True
    assert result["delivery"] is False

    runtime = manager.admit("ctx-wire")
    assert runtime._dispatch_adapter is not None  # noqa: SLF001

    # Adapter is usable for fan-out acceptance.
    wtid = await runtime._dispatch_adapter(  # noqa: SLF001
        "Alice", "prompt", "http://cb", "dsp_1", "ctx-wire"
    )
    assert wtid == "wtid-1"


@pytest.mark.asyncio
async def test_wire_production_adapters_installs_delivery_when_secret_present():
    manager = MissionRuntimeManager()
    team = TeamPartitionService()
    router = MagicMock()
    registry = MagicMock()
    registry.get.return_value = AgentInfo(
        agent_id="Alice",
        description="A",
        endpoint="http://alice:8090",
        status=AgentStatus.ONLINE,
    )

    result = wire_production_adapters(
        mission_runtime_manager=manager,
        team_partition_service=team,
        router=router,
        agent_registry=registry,
        worker_registry=None,
        coordinator_host="localhost",
        coordinator_port=8080,
        coordinator_secret=b"x" * 32,
    )
    assert result["delivery"] is True
    assert team._delivery_adapter is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_delivery_adapter_sends_team_update_for_collaborative_after():
    sender = MagicMock()
    sender.send_team_update = AsyncMock(return_value={"success": True})
    sender.send_team_revoke = AsyncMock(return_value={"success": True})

    registry = MagicMock()
    registry.get.side_effect = lambda wid: AgentInfo(
        agent_id=wid,
        description=wid,
        endpoint=f"http://{wid.lower()}:8090",
        status=AgentStatus.ONLINE,
    )

    adapter = build_delivery_adapter(sender, registry)
    secret = b"s" * 32
    transition = PartitionTransition(
        transition_id="tr_1",
        context_id="ctx",
        source_node_id="node-1",
        before={
            "Alice": TeamAssignment(team_id="solo:Alice", epoch=1, member_ids=["Alice"]),
            "Bob": TeamAssignment(team_id="solo:Bob", epoch=1, member_ids=["Bob"]),
        },
        after={
            "Alice": TeamAssignment(
                team_id="team:node-1:r2",
                epoch=2,
                member_ids=["Alice", "Bob"],
                objective="Rescue",
            ),
            "Bob": TeamAssignment(
                team_id="team:node-1:r2",
                epoch=2,
                member_ids=["Alice", "Bob"],
                objective="Rescue",
            ),
        },
        affected_workers=["Alice", "Bob"],
        epoch=2,
        status=TransitionStatus.INSTALLING,
        team_secret=secret,
        team_id="team:node-1:r2",
    )

    outcomes = await adapter(transition)
    assert outcomes == {"Alice": True, "Bob": True}
    assert sender.send_team_update.await_count == 2
    sender.send_team_revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_adapter_revokes_before_team_on_release():
    sender = MagicMock()
    sender.send_team_update = AsyncMock(return_value={"success": True})
    sender.send_team_revoke = AsyncMock(return_value={"success": True})

    registry = MagicMock()
    registry.get.side_effect = lambda wid: AgentInfo(
        agent_id=wid,
        description=wid,
        endpoint=f"http://{wid.lower()}:8090",
        status=AgentStatus.ONLINE,
    )
    adapter = build_delivery_adapter(sender, registry)

    transition = PartitionTransition(
        transition_id="tr_rel",
        context_id="ctx",
        source_node_id="",
        before={
            "Alice": TeamAssignment(
                team_id="team:node-1:r2",
                epoch=2,
                member_ids=["Alice", "Bob"],
            ),
        },
        after={
            "Alice": TeamAssignment(
                team_id="solo:Alice",
                epoch=3,
                member_ids=["Alice"],
            ),
        },
        affected_workers=["Alice"],
        epoch=3,
        status=TransitionStatus.INSTALLING,
        team_id="solo:release",
    )

    outcomes = await adapter(transition)
    assert outcomes == {"Alice": True}
    sender.send_team_revoke.assert_awaited_once()
    kwargs = sender.send_team_revoke.await_args.kwargs
    assert kwargs["team_id"] == "team:node-1:r2"
    assert kwargs["epoch"] == 2


# ---------------------------------------------------------------------------
# Terminal cancel idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_already_terminal_is_idempotent_no_remote_call():
    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-cancel-idemp")
    store = TaskStore("req", router=None, context_id="ctx-cancel-idemp")
    store.attach_runtime(runtime)
    store.add_adhoc_node(task_id="explore-alice", worker_id="Alice")
    dispatch = store.create_physical_dispatch("explore-alice", "Alice")
    assert dispatch is not None
    store.apply_physical_status(
        dispatch.dispatch_id, PhysicalState.DISPATCHING, source="test"
    )
    store.register_worker_task_id(dispatch.dispatch_id, "wtid-1")
    store.apply_physical_status(
        dispatch.dispatch_id, PhysicalState.RUNNING, source="test"
    )
    store.apply_physical_status(
        dispatch.dispatch_id, PhysicalState.CANCELED, source="test"
    )
    assert store.get_dispatch(dispatch.dispatch_id).state.terminal

    registry = MagicMock()
    tool = CancelTaskTool(store, registry)

    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        new_callable=AsyncMock,
    ) as create_client:
        result = await tool.execute(task_id=dispatch.dispatch_id)

    assert result.success is True
    assert result.data is not None and result.data.get("idempotent") is True
    create_client.assert_not_awaited()
    registry.get.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_terminal_via_plan_node_compat_path():
    """Legacy path without runtime dispatch still works; terminal only when runtime."""
    store = MagicMock()
    store.resolve_dispatch_id.return_value = "dsp_done"
    store.get_node.return_value = PlanNode(task_id="dsp_done", worker_id="Alice")
    store._runtime = None  # noqa: SLF001
    store._dispatch_to_worker = {"dsp_done": "wtid-x"}  # noqa: SLF001
    store.get_dispatch = MagicMock(return_value=None)
    store.cancel_dispatch = MagicMock()

    registry = MagicMock()
    registry.get.return_value = AgentInfo(
        agent_id="Alice",
        description="A",
        endpoint="http://alice:8090",
        status=AgentStatus.ONLINE,
    )
    tool = CancelTaskTool(store, registry)

    mock_task = MagicMock()
    mock_task.status.state = 5
    mock_client = MagicMock()
    mock_client.cancel_task = AsyncMock(return_value=mock_task)
    mock_client.close = AsyncMock()

    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        AsyncMock(return_value=mock_client),
    ):
        result = await tool.execute(task_id="dsp_done")

    assert result.success is True
    mock_client.close.assert_awaited()


# ---------------------------------------------------------------------------
# EventQueueSource safe close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_event_queue_safely_marks_dispatcher_expected():
    from a2a.shared.server_lifecycle import close_event_queue_safely

    closed = []

    class FakeQueue:
        def __init__(self) -> None:
            self._dispatcher_task_expected_to_cancel = False
            self._dispatcher_task = None

        async def close(self, *, immediate: bool = False) -> None:
            self._dispatcher_task_expected_to_cancel = True
            closed.append(immediate)

    q = FakeQueue()
    await close_event_queue_safely(q, immediate=True)
    assert closed == [True]
    assert q._dispatcher_task_expected_to_cancel is True


@pytest.mark.asyncio
async def test_shutdown_closes_live_event_queue_source_dispatcher():
    import asyncio

    from a2a.shared.server_lifecycle import shutdown_a2a_active_tasks

    class FakeEQ:
        def __init__(self) -> None:
            self._is_closed = False
            self._dispatcher_task_expected_to_cancel = False
            self.close_calls: list[bool] = []
            self._dispatcher_task = asyncio.create_task(asyncio.Event().wait())

        async def close(self, *, immediate: bool = False) -> None:
            self._dispatcher_task_expected_to_cancel = True
            self.close_calls.append(immediate)
            self._is_closed = True
            if self._dispatcher_task and not self._dispatcher_task.done():
                self._dispatcher_task.cancel()
                try:
                    await self._dispatcher_task
                except asyncio.CancelledError:
                    pass

        def is_closed(self) -> bool:
            return self._is_closed

    class FakeActive:
        def __init__(self) -> None:
            self._event_queue_agent = FakeEQ()
            self._event_queue_subscribers = FakeEQ()
            self._is_finished = asyncio.Event()
            self._is_finished.set()  # finished path still drains live dispatcher
            self._producer_task = None
            self._consumer_task = None

    active = FakeActive()

    class Reg:
        def __init__(self) -> None:
            self._active_tasks = {"t1": active}
            self._lock = asyncio.Lock()

    class Handler:
        def __init__(self) -> None:
            self._active_task_registry = Reg()

    await shutdown_a2a_active_tasks(Handler(), timeout=0.5)
    assert active._event_queue_agent.close_calls == [True]
    assert active._event_queue_agent._dispatcher_task.done()
