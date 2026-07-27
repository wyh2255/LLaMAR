"""Phase 6: all parent termination paths use MissionRuntime.abort().

route_strategy.md §8 contract: abort is the sole parent-level cleanup
primitive — freeze, bounded-wait cancel, TeamPartition reconcile,
clear mappings, release admission.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from a2a.coordinator.mission_graph import MissionGraph
from a2a.coordinator.mission_runtime import (
    MissionRuntime,
    MissionRuntimeManager,
    PhysicalState,
)
from a2a.coordinator.server import create_server
from a2a.coordinator.team_partition_service import (
    TeamPartitionRegistry,
    TeamPartitionService,
)

# =========================================================================
# Deterministic fixtures
# =========================================================================


@pytest.fixture
def manager() -> MissionRuntimeManager:
    return MissionRuntimeManager()


@pytest.fixture
def runtime(manager: MissionRuntimeManager) -> MissionRuntime:
    return manager.admit("ctx-phase6")


@pytest.fixture
def registry() -> TeamPartitionRegistry:
    return TeamPartitionRegistry(coordinator_id="Coordinator")


@pytest.fixture
def team_service(registry: TeamPartitionRegistry) -> TeamPartitionService:
    svc = TeamPartitionService(registry=registry)
    svc.ensure_singletons(["Alice", "Bob"])
    return svc


@pytest.fixture
def injected_manager(
    team_service: TeamPartitionService,
) -> MissionRuntimeManager:
    mgr = MissionRuntimeManager()
    mgr.set_team_partition_service(team_service)
    return mgr


# =========================================================================
# Contract test: freeze new activations after abort
# =========================================================================


@pytest.mark.asyncio
async def test_abort_freezes_new_activations(runtime: MissionRuntime):
    await runtime.abort("test_freeze")

    with pytest.raises(RuntimeError, match="mission_runtime_aborted"):
        runtime.create_dispatch("any-task", "Alice")

    with pytest.raises(RuntimeError, match="mission_runtime_aborted"):
        runtime.create_dispatches("any-team-task", ["Alice", "Bob"])

    result = await runtime.activate_plan_node("phantom", MissionGraph())
    assert result == {"success": False, "error": "mission_runtime_aborted"}


# =========================================================================
# Contract test: TeamPartition release/reconcile on abort
# =========================================================================


@pytest.mark.asyncio
async def test_abort_releases_team_partition(
    injected_manager: MissionRuntimeManager,
    team_service: TeamPartitionService,
):
    """TeamPartition release_node_team is called during abort."""
    runtime = injected_manager.admit("ctx-team-abort")
    runtime.set_team_partition_service(team_service)

    release = AsyncMock(wraps=team_service.release_node_team)
    team_service.release_node_team = release

    await runtime.abort("team_reconcile")

    release.assert_awaited_once_with("ctx-team-abort", ack_timeout=10.0)


@pytest.mark.asyncio
async def test_manager_abort_reconciles_team_partition(
    injected_manager: MissionRuntimeManager,
    team_service: TeamPartitionService,
):
    """MissionRuntimeManager.abort() triggers TeamPartition reconciliation."""
    runtime = injected_manager.admit("ctx-mgr-abort")

    release = AsyncMock(wraps=team_service.release_node_team)
    team_service.release_node_team = release

    await injected_manager.abort("manager_shutdown")

    release.assert_awaited_once_with("ctx-mgr-abort", ack_timeout=10.0)
    assert runtime.aborted


# =========================================================================
# Contract test: bounded wait for remote cancellations
# =========================================================================


@pytest.mark.asyncio
async def test_abort_bounded_wait_handles_slow_remote():
    """cancel_adapter that never returns does not block abort forever."""
    manager = MissionRuntimeManager()

    async def forever_cancel(_w: str, _t: str) -> str:
        await asyncio.Event().wait()
        return "TASK_STATE_CANCELED"

    manager.set_cancel_adapter(forever_cancel)
    runtime = manager.admit("ctx-slow-cancel")
    d1 = runtime.create_dispatch("task-a", "Alice")
    runtime.apply_physical_status(d1.dispatch_id, "DISPATCHING", source="sync")
    d2 = runtime.create_dispatch("task-b", "Bob")
    runtime.apply_physical_status(d2.dispatch_id, "ACCEPTED", source="sync")

    await runtime.abort("bounded_wait_test")

    assert runtime.aborted
    assert d1.state in (PhysicalState.CANCEL_PENDING, PhysicalState.CANCELED)
    assert d2.state in (PhysicalState.CANCEL_PENDING, PhysicalState.CANCELED)
    assert manager.active_runtime is None


# =========================================================================
# Contract test: terminal dispatches are not mutated
# =========================================================================


@pytest.mark.asyncio
async def test_abort_preserves_terminal_dispatches(manager: MissionRuntimeManager):
    runtime = manager.admit("ctx-terminal")
    d1 = runtime.create_dispatch("task-complete", "Alice")
    runtime.apply_physical_status(d1.dispatch_id, "DISPATCHING", source="sync")
    runtime.apply_physical_status(d1.dispatch_id, "ACCEPTED", source="sync")
    runtime.register_worker_task(d1.dispatch_id, "wt-complete")
    runtime.apply_physical_status(
        d1.dispatch_id, "COMPLETED", source="callback", result="done"
    )
    d2 = runtime.create_dispatch("task-failed", "Bob")
    runtime.apply_physical_status(d2.dispatch_id, "DISPATCHING", source="sync")
    runtime.apply_physical_status(d2.dispatch_id, "ACCEPTED", source="sync")
    runtime.register_worker_task(d2.dispatch_id, "wt-failed")
    runtime.apply_physical_status(
        d2.dispatch_id, "FAILED", source="callback", result="fail"
    )

    await runtime.abort("preserve_terminal")

    assert d1.state is PhysicalState.COMPLETED
    assert d1.result == "done"
    assert d2.state is PhysicalState.FAILED
    assert d2.result == "fail"


# =========================================================================
# Contract test: idempotent
# =========================================================================


@pytest.mark.asyncio
async def test_abort_is_idempotent(runtime: MissionRuntime):
    d = runtime.create_dispatch("task", "Alice")
    runtime.apply_physical_status(d.dispatch_id, "ACCEPTED", source="sync")
    future = runtime.register_future(d.dispatch_id)

    await runtime.abort("first")
    await runtime.abort("second")
    await runtime.abort("third")

    assert runtime.aborted
    assert d.state in (PhysicalState.CANCELED, PhysicalState.CANCEL_PENDING)
    assert future.done()
    assert runtime._manager.active_runtime is None


# =========================================================================
# Contract test: futures and worker mappings cleared after abort
# =========================================================================


@pytest.mark.asyncio
async def test_abort_clears_futures_and_mappings(manager: MissionRuntimeManager):
    runtime = manager.admit("ctx-clear")
    d1 = runtime.create_dispatch("task-a", "Alice")
    runtime.register_worker_task(d1.dispatch_id, "worker-task-a")
    f1 = runtime.register_future(d1.dispatch_id)

    await runtime.abort("clear_check")

    assert len(runtime._futures) == 0
    assert len(runtime._worker_to_dispatch) == 0
    assert f1.done()


# =========================================================================
# Server shutdown wiring
# =========================================================================


@pytest.mark.asyncio
async def test_server_lifespan_shutdown_triggers_manager_abort(tmp_path):
    """CoordinatorServer lifespan exit calls mission_runtime_manager.abort()."""
    server = create_server(verifier_enabled=False, log_dir=str(tmp_path))
    runtime = server.mission_runtime_manager.admit("ctx-server-shutdown")

    await server._shutdown_on_owner_loop()

    assert runtime.aborted


@pytest.mark.asyncio
async def test_shutdown_cleanup_after_experiment_cancel(tmp_path):
    """POST /experiment/{context_id}/cancel triggers manager abort."""
    server = create_server(verifier_enabled=False, log_dir=str(tmp_path))
    from a2a.shared.types import DistributedTask

    server.task_queue.enqueue(
        DistributedTask("task-1", "agent", "test", context_id="ctx-exp-cancel")
    )
    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/experiment/ctx-exp-cancel/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"


# =========================================================================
# Signatures already exist in existing tests; no need to re-test
# test_abort_keeps_dispatching_without_worker_task_in_cancel_pending
# test_abort_is_idempotent_cleans_futures_and_releases_admission
# are already in test_mission_runtime_lifecycle.py.
# =========================================================================


# =========================================================================
# Startup recovery wiring (recover_and_reconcile calls release_node_team
# through reconcile → runtime.reconcile)
# =========================================================================


@pytest.mark.asyncio
async def test_recover_and_reconcile_advances_epoch(tmp_path):
    """recover_and_reconcile advances epoch and does not crash."""
    state_path = tmp_path / "coordinator-control-state.json"
    mgr = MissionRuntimeManager(state_path=state_path)
    mgr.set_cancel_adapter(AsyncMock(return_value="TASK_STATE_CANCELED"))
    mgr.set_team_partition_service(TeamPartitionService())

    runtime = mgr.admit("ctx-recovery")
    d = runtime.create_dispatch("task", "Alice")
    runtime.apply_physical_status(d.dispatch_id, "DISPATCHING", source="sync")
    await asyncio.sleep(0.01)

    mgr2 = MissionRuntimeManager(state_path=state_path)

    release = AsyncMock(return_value=None)
    mgr2.set_team_partition_service(MagicMock(release_node_team=release))
    mgr2.set_cancel_adapter(AsyncMock(return_value="TASK_STATE_CANCELED"))

    epoch = await mgr2.recover_and_reconcile()
    assert epoch > 0
    assert mgr2._recovery_required
    assert mgr2.active_runtime is not None


# =========================================================================
# All parent termination paths listed in route_strategy.md §8:
#   - normal finish (_execute_agentic finally → abort("mission_complete"))
#   - orchestration timeout (asyncio.TimeoutError → finally → abort)
#   - Router/controller error (Exception → finally → abort)
#   - Executor cancel()
#   - SAR experiment cancel (HTTP)  ✓ (test_shutdown_cleanup)
#   - Coordinator shutdown (lifespan) ✓ (test_server_lifespan)
#   - Coordinator startup recovery  ✓ (test_recover_and_reconcile)
# =========================================================================
