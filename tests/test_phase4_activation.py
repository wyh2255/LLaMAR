"""Phase 4 activation / runtime tests: DAG gate, Team ACK, fan-out, aggregation.

RED tests  (failure paths):   node_not_found, node_not_ready, dependency_incomplete,
                              participant_busy, team_setup_failed, fan-out Nth failure,
                              canonical exact-once guard for late callback.
GREEN tests (success paths):  single-worker activation, multi-worker activation,
                              Team ACK → INSTALLED → fan-out → terminal aggregation,
                              SendMessageTool routing of activate_plan_node.
"""

from __future__ import annotations

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry
from a2a.coordinator.mission_graph import MissionGraph
from a2a.coordinator.mission_runtime import (
    MissionRuntime,
    MissionRuntimeManager,
    PhysicalState,
)
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.team_partition_service import (
    PartitionTransition,
    TeamPartitionRegistry,
    TeamPartitionService,
    TransitionStatus,
)

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def graph() -> MissionGraph:
    return MissionGraph()


@pytest.fixture
def manager() -> MissionRuntimeManager:
    return MissionRuntimeManager()


@pytest.fixture
def runtime(manager: MissionRuntimeManager) -> MissionRuntime:
    return manager.admit("ctx-activation")


@pytest.fixture
def registry() -> TeamPartitionRegistry:
    return TeamPartitionRegistry(coordinator_id="Coordinator")


@pytest.fixture
def team_service(registry: TeamPartitionRegistry) -> TeamPartitionService:
    svc = TeamPartitionService(registry=registry)
    svc.ensure_singletons(["Alice", "Bob", "Charlie"])
    return svc


@pytest.fixture
def store(manager: MissionRuntimeManager) -> TaskStore:
    s = TaskStore("test activation", router=None, context_id="ctx-test")
    s.attach_runtime(manager.admit("ctx-test"))
    return s


@pytest.fixture
def populated_graph(graph: MissionGraph) -> MissionGraph:
    graph.replace(
        [
            {
                "logical_id": "scout-north",
                "participant_ids": ["Alice"],
                "objective": "Scout north sector",
            },
            {
                "logical_id": "rescue-thomas",
                "participant_ids": ["Alice", "Bob"],
                "depends_on": ["scout-north"],
                "objective": "Rescue Thomas at north building",
                "assignments": {
                    "Alice": "Navigate to north building entrance",
                    "Bob": "Wait at extraction point",
                },
            },
            {
                "logical_id": "evacuate-civilians",
                "participant_ids": ["Charlie", "Alice"],
                "depends_on": ["rescue-thomas"],
                "objective": "Evacuate remaining civilians",
            },
        ]
    )
    return graph


def _build_prompt(objective: str, worker_id: str, assignments: dict[str, str]) -> str:
    assignment = assignments.get(worker_id, "")
    return f"{objective}\n\nAssignment for {worker_id}: {assignment}"


def _succeeding_adapter(
    worker_task_id_map: dict[str, str] | None = None,
) -> dict:
    """Return a dispatch adapter that always succeeds."""
    if worker_task_id_map is None:
        worker_task_id_map = {}

    async def adapter(
        worker_id: str,
        prompt: str,
        callback_url: str,
        dispatch_id: str,
        context_id: str,
    ) -> str:
        return worker_task_id_map.get(
            worker_id, f"wtid-{worker_id}-{dispatch_id[-8:]}"
        )

    return {"adapter": adapter}


# =========================================================================
# RED: DAG gate pre-checks
# =========================================================================


class TestDagGateRed:
    """Phase 4 §6.2: DAG gate must reject invalid preconditions."""

    @pytest.mark.asyncio
    async def test_node_not_found(
        self, runtime: MissionRuntime, graph: MissionGraph
    ):
        result = await runtime.activate_plan_node(
            "nonexistent", graph,
        )
        assert result["success"] is False
        assert result["error"] == "node_not_found"

    @pytest.mark.asyncio
    async def test_node_not_ready_blocked(
        self, runtime: MissionRuntime, graph: MissionGraph
    ):
        graph.replace(
            [
                {
                    "logical_id": "scout",
                    "participant_ids": ["Alice"],
                },
                {
                    "logical_id": "rescue",
                    "participant_ids": ["Alice"],
                    "depends_on": ["scout"],
                },
            ]
        )
        result = await runtime.activate_plan_node("rescue", graph)
        assert result["success"] is False
        assert result["error"] in ("dependency_incomplete", "node_not_ready")

    @pytest.mark.asyncio
    async def test_dependency_incomplete(
        self, runtime: MissionRuntime, graph: MissionGraph
    ):
        graph.replace(
            [
                {
                    "logical_id": "scout",
                    "participant_ids": ["Alice"],
                },
                {
                    "logical_id": "rescue",
                    "participant_ids": ["Alice"],
                    "depends_on": ["scout"],
                },
            ]
        )
        result = await runtime.activate_plan_node("rescue", graph)
        assert result["success"] is False
        assert result["error"] == "dependency_incomplete"

    @pytest.mark.asyncio
    async def test_participant_busy_at_gate(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
        registry: TeamPartitionRegistry,
    ):
        """Claim Alice in another transition so gate sees her as busy."""
        graph.replace(
            [
                {
                    "logical_id": "direct-task",
                    "participant_ids": ["Alice"],
                    "objective": "No-dependency task",
                },
            ]
        )

        # Claim Alice in another (non-graph) transition first
        transition = registry.prepare_activation(
            node_id="other-node",
            members=["Alice"],
            objective="Other",
            context_id="other-ctx",
        )
        registry.mark_installing(transition.transition_id)

        result = await runtime.activate_plan_node(
            "direct-task",
            graph,
            team_service=team_service,
        )
        # The gate should see Alice is busy via TeamPartition pre-check
        assert result["success"] is False
        assert result["error"] == "participant_busy"

    @pytest.mark.asyncio
    async def test_participant_busy_atomic_claim_after_gate(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
        registry: TeamPartitionRegistry,
    ):
        """Claim after gate passes: atomic claim re-checks busy under lock."""
        graph.replace(
            [
                {
                    "logical_id": "simple",
                    "participant_ids": ["Alice"],
                    "objective": "Simple task",
                },
            ]
        )

        adapt = _succeeding_adapter({"Alice": "wtid-simple"})
        runtime.set_dispatch_adapter(adapt["adapter"])

        # Activate simple node first — Alice is now claimed in MissionGraph
        result = await runtime.activate_plan_node("simple", graph)
        assert result["success"] is True

        # Now add a second independent node that also claims Alice
        graph.replace(
            [
                {
                    "logical_id": "simple",
                    "participant_ids": ["Alice"],
                    "objective": "Simple task",
                },
                {
                    "logical_id": "second",
                    "participant_ids": ["Alice"],
                    "objective": "Second task",
                },
            ]
        )
        # "simple" is still active (Alice claimed). "second" has no
        # dependencies so frontier says ready. Gate checks MissionGraph
        # claim → Alice is busy → participant_busy.
        result2 = await runtime.activate_plan_node("second", graph)
        assert result2["success"] is False
        assert result2["error"] == "participant_busy"

    @pytest.mark.asyncio
    async def test_activate_after_abort_rejected(
        self, runtime: MissionRuntime, graph: MissionGraph
    ):
        graph.replace(
            [
                {
                    "logical_id": "simple",
                    "participant_ids": ["Alice"],
                },
            ]
        )
        await runtime.abort("test_cleanup")
        result = await runtime.activate_plan_node("simple", graph)
        assert result["success"] is False
        assert result["error"] == "mission_runtime_aborted"


# =========================================================================
# RED: Team ACK failure
# =========================================================================


class TestTeamAckRed:
    """Phase 4 §6.4: Team ACK failure must return team_setup_failed."""

    @pytest.mark.asyncio
    async def test_team_ack_failure_returns_team_setup_failed(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
    ):
        graph.replace(
            [
                {
                    "logical_id": "multi",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Multi-worker task",
                },
            ]
        )

        svc = team_service

        # Set the delivery adapter to always fail
        async def always_fail(tr: PartitionTransition) -> dict[str, bool]:
            return {w: False for w in tr.affected_workers}

        svc.set_delivery_adapter(always_fail)

        adapter_info = _succeeding_adapter()
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "multi",
            graph,
            team_service=svc,
        )
        assert result["success"] is False
        assert result["error"] == "team_setup_failed"

    @pytest.mark.asyncio
    async def test_single_worker_skips_team_ack(
        self, runtime: MissionRuntime, graph: MissionGraph
    ):
        """Single-participant nodes must skip the Team ACK saga entirely."""
        graph.replace(
            [
                {
                    "logical_id": "solo",
                    "participant_ids": ["Alice"],
                    "objective": "Solo task",
                },
            ]
        )
        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-solo-alice"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "solo",
            graph,
            team_service=None,
        )
        assert result["success"] is True
        assert result["node_id"] == "solo"

    @pytest.mark.asyncio
    async def test_multi_worker_without_delivery_adapter_skips_team_ack_and_succeeds(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
    ):
        """No delivery adapter configured (e.g. enable_peer_mail=False, the
        sar_orch/experiment.py default) must skip the Team ACK saga for
        multi-participant nodes too, not just single-participant ones.

        Without this bypass, the saga runs anyway, collects zero ACKs
        (nothing can deliver them), and drives to DEGRADED -- which
        retains its fence on Alice/Bob forever (see mark_degraded's
        "retains fences per design §7.1"), since nothing in any
        production path ever calls reconcile_after_recovery() outside of
        a coordinator restart. That is the exact failure observed in
        production logs: an 'explore-persons' node with participants
        [Alice, Bob] failed with team_setup_failed, and every activation
        of Alice or Bob individually failed with participant_busy from
        then on, no matter how many times the Coordinator replanned.
        """
        graph.replace(
            [
                {
                    "logical_id": "multi-no-adapter",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Multi-worker task, no delivery adapter wired",
                },
            ]
        )

        svc = team_service
        assert svc.has_delivery_adapter is False

        adapter_info = _succeeding_adapter({"Alice": "wtid-a", "Bob": "wtid-b"})
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "multi-no-adapter",
            graph,
            team_service=svc,
        )
        assert result["success"] is True
        assert result["node_id"] == "multi-no-adapter"

        # No fence was created, so both workers remain free.
        assert svc.registry.check_participants_free(["Alice", "Bob"]) == []

    @pytest.mark.asyncio
    async def test_repeated_multi_worker_activation_without_delivery_adapter(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
    ):
        """Reproduces the run4/run5 replanning pattern: activate a multi-
        worker node, complete it, then activate a second multi-worker node
        for the same participants. Must succeed both times -- proving no
        permanent participant_busy deadlock accumulates across repeated
        activations when no delivery adapter is configured."""
        from a2a.coordinator.task_store import TaskStore

        store = TaskStore("test repeat", router=None, context_id="ctx-repeat")
        store.attach_runtime(runtime)
        shared_graph = store._mission_graph

        shared_graph.replace(
            [
                {
                    "logical_id": "first",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "First joint task",
                },
            ]
        )

        svc = team_service
        adapter_info = _succeeding_adapter({"Alice": "wtid-a1", "Bob": "wtid-b1"})
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        first = await runtime.activate_plan_node(
            "first", shared_graph, team_service=svc
        )
        assert first["success"] is True

        alice_dispatch = next(
            d for d in runtime.dispatches.values() if d.worker_id == "Alice"
        )
        bob_dispatch = next(
            d for d in runtime.dispatches.values() if d.worker_id == "Bob"
        )
        runtime.apply_physical_status(
            alice_dispatch.dispatch_id, "COMPLETED", source="callback", result="done"
        )
        runtime.apply_physical_status(
            bob_dispatch.dispatch_id, "COMPLETED", source="callback", result="done"
        )

        shared_graph.replace(
            [
                {
                    "logical_id": "first",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "First joint task",
                    "status": "skipped",
                },
                {
                    "logical_id": "second",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Second joint task, same participants",
                },
            ]
        )

        adapter_info2 = _succeeding_adapter({"Alice": "wtid-a2", "Bob": "wtid-b2"})
        runtime.set_dispatch_adapter(adapter_info2["adapter"])

        second = await runtime.activate_plan_node(
            "second", shared_graph, team_service=svc
        )
        assert second["success"] is True
        assert second["node_id"] == "second"

    @pytest.mark.asyncio
    async def test_team_ack_timeout_compensation(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
    ):
        """Delivery adapter that returns nothing → all timeouts → COMPENSATING → DEGRADED."""
        graph.replace(
            [
                {
                    "logical_id": "multi-timeout",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Multi with timeout",
                },
            ]
        )

        svc = team_service

        async def empty_delivery(tr: PartitionTransition) -> dict[str, bool]:
            return {}

        svc.set_delivery_adapter(empty_delivery)

        adapter_info = _succeeding_adapter()
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "multi-timeout",
            graph,
            team_service=svc,
        )
        assert result["success"] is False
        assert result["error"] == "team_setup_failed"


# =========================================================================
# RED: Fan-out acceptance failure
# =========================================================================


class TestFanOutRed:
    """Phase 4 §6.5: Nth participant failure must cancel already-accepted."""

    @pytest.mark.asyncio
    async def test_nth_acceptance_failure_calls_cancel_adapter(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
    ):
        """Nth failure triggers cancel_dispatch_remote which calls cancel_adapter."""
        graph.replace(
            [
                {
                    "logical_id": "rescue",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Rescue operation",
                },
            ]
        )

        svc = team_service

        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        svc.set_delivery_adapter(all_ack)

        dispatch_call_count: int = 0
        cancel_call_log: list[tuple[str, str]] = []

        async def first_succeeds_then_fails(
            worker_id: str,
            prompt: str,
            callback_url: str,
            dispatch_id: str,
            context_id: str,
        ) -> str:
            nonlocal dispatch_call_count
            dispatch_call_count += 1
            if dispatch_call_count == 1:
                return f"wtid-alice-{dispatch_id[-8:]}"
            raise RuntimeError("Bob worker unreachable")

        runtime.set_dispatch_adapter(first_succeeds_then_fails)

        # Inject cancel adapter that records invocations
        async def recording_cancel(worker_id: str, worker_task_id: str) -> str:
            cancel_call_log.append((worker_id, worker_task_id))
            return "TASK_STATE_CANCELED"

        manager = runtime._manager
        manager.set_cancel_adapter(recording_cancel)

        result = await runtime.activate_plan_node(
            "rescue",
            graph,
            team_service=svc,
        )
        assert result["success"] is False
        assert result["error"] == "dispatch_acceptance_failed"

        # cancel_dispatch_remote must have been called for the accepted worker
        assert len(cancel_call_log) == 1
        assert cancel_call_log[0][0] == "Alice"

        # Alice's dispatch should be terminal (cancel adapter returned CANCELED)
        alice_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Alice"),
            None,
        )
        assert alice_dispatch is not None
        assert alice_dispatch.state.terminal

        # Logical node must be failed
        node_after = graph.get_node("rescue")
        assert node_after is not None
        assert node_after.state in ("failed",)

    @pytest.mark.asyncio
    async def test_no_dispatch_adapter_fails_node_no_leaked_prepared(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
    ):
        """Missing dispatch adapter must fail node and NOT leave PREPARED dispatches."""
        graph.replace(
            [
                {
                    "logical_id": "solo",
                    "participant_ids": ["Alice"],
                    "objective": "Solo without adapter",
                },
            ]
        )
        result = await runtime.activate_plan_node("solo", graph)
        assert result["success"] is False
        assert result["error"] == "dispatch_acceptance_failed"

        # Node must be failed (not stuck activating)
        node_after = graph.get_node("solo")
        assert node_after is not None
        assert node_after.state == "failed"

        # No PREPARED dispatches should remain
        for d in runtime.dispatches.values():
            assert d.state is not PhysicalState.PREPARED

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_late_duplicate_callback_ignored(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
    ):
        """Exact-once: late/duplicate callback must not double-aggregate."""
        graph.replace(
            [
                {
                    "logical_id": "solo",
                    "participant_ids": ["Alice"],
                    "objective": "Solo for exact-once test",
                },
            ]
        )
        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-solo-exact"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        # We already have a status_owner set up for exact-once.
        from a2a.coordinator.task_store import TaskStore
        store = TaskStore("test exact-once", router=None, context_id="ctx-test")
        store.attach_runtime(runtime)

        result = await runtime.activate_plan_node("solo", graph)
        assert result["success"] is True

        dispatch = next(iter(runtime.dispatches.values()))
        assert dispatch is not None

        diag_count_before = len(runtime.diagnostics)

        # First terminal via apply_physical_status
        runtime.apply_physical_status(
            dispatch.dispatch_id,
            "COMPLETED",
            source="callback",
            result="mission accomplished",
        )
        assert dispatch.state is PhysicalState.COMPLETED

        # Duplicate terminal should be ignored (exact-once guard in TaskStore)
        runtime.apply_physical_status(
            dispatch.dispatch_id,
            "FAILED",
            source="late_duplicate",
        )
        # Must still be COMPLETED (terminal state never regresses)
        assert dispatch.state is PhysicalState.COMPLETED

        # Late non-terminal should also be ignored (terminal guard)
        runtime.apply_physical_status(
            dispatch.dispatch_id,
            "RUNNING",
            source="very_late_callback",
        )
        assert dispatch.state is PhysicalState.COMPLETED

        diag_count_after = len(runtime.diagnostics)
        assert diag_count_after > diag_count_before


# =========================================================================
# GREEN: Single-worker activation
# =========================================================================


class TestGreenSingleWorker:
    """Happy path for a single-participant activation."""

    @pytest.mark.asyncio
    async def test_single_worker_happy_path(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
    ):
        graph.replace(
            [
                {
                    "logical_id": "scout-north",
                    "participant_ids": ["Alice"],
                    "objective": "Scout north sector for fire damage",
                },
            ]
        )

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-scout-001"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node("scout-north", graph)

        assert result["success"] is True
        assert result["node_id"] == "scout-north"

        node = graph.get_node("scout-north")
        assert node is not None
        assert node.state in ("active", "activating")

        dispatch = next(iter(runtime.dispatches.values()))
        assert dispatch is not None
        assert dispatch.worker_id == "Alice"
        assert dispatch.worker_task_id == "wtid-scout-001"
        assert dispatch.state is not PhysicalState.PREPARED
        assert dispatch.state is not PhysicalState.DISPATCHING

    @pytest.mark.asyncio
    async def test_single_worker_then_terminal_aggregation(
        self,
        runtime: MissionRuntime,
        manager: MissionRuntimeManager,
    ):
        """Activate → receive terminal → node completed via exact-once."""
        from a2a.coordinator.task_store import TaskStore
        store = TaskStore("test term", router=None, context_id="ctx-term")
        store.attach_runtime(runtime)
        # Use the store's mission_graph so aggregation can find dispatch bindings.
        graph = store._mission_graph

        graph.replace(
            [
                {
                    "logical_id": "scout",
                    "participant_ids": ["Alice"],
                    "objective": "Scout east sector",
                },
            ]
        )

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-scout-term"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node("scout", graph)
        assert result["success"] is True

        node_before = graph.get_node("scout")
        assert node_before is not None
        assert node_before.state in ("active",)

        dispatch = next(iter(runtime.dispatches.values()))
        assert dispatch is not None

        runtime.apply_physical_status(
            dispatch.dispatch_id,
            "COMPLETED",
            source="callback",
            result="Sector clear, no fire damage",
        )

        node_after = graph.get_node("scout")
        assert node_after is not None
        assert node_after.state == "completed"
        assert "Sector clear" in str(
            node_after.results.get("Alice", "")
        ) or "Sector clear" in str(dispatch.result)


# =========================================================================
# GREEN: Multi-worker activation
# =========================================================================


class TestGreenMultiWorker:
    """Happy path for multi-participant activation with Team ACK."""

    @pytest.mark.asyncio
    async def test_multi_worker_happy_path_writes_team_id_epoch(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
        registry: TeamPartitionRegistry,
    ):
        graph.replace(
            [
                {
                    "logical_id": "rescue-thomas",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Rescue Thomas at north building",
                    "assignments": {
                        "Alice": "Navigate to north building entrance",
                        "Bob": "Wait at extraction point",
                    },
                },
            ]
        )

        svc = team_service

        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        svc.set_delivery_adapter(all_ack)

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-alice-001", "Bob": "wtid-bob-001"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "rescue-thomas",
            graph,
            team_service=svc,
            prompt_factory=_build_prompt,
        )

        assert result["success"] is True
        assert result["node_id"] == "rescue-thomas"
        assert len(result["dispatches"]) == 2

        node = graph.get_node("rescue-thomas")
        assert node is not None
        assert node.state == "active"

        # Team ID and epoch must be written into the MissionGraph node
        assert node.team_id is not None
        assert node.team_id.startswith("team:rescue-thomas")
        assert node.team_epoch is not None
        assert node.team_epoch > 0
        assert result["team_id"] == node.team_id
        assert result["team_epoch"] == node.team_epoch

        alice_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Alice"),
            None,
        )
        bob_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Bob"),
            None,
        )
        assert alice_dispatch is not None
        assert bob_dispatch is not None
        assert alice_dispatch.worker_task_id == "wtid-alice-001"
        assert bob_dispatch.worker_task_id == "wtid-bob-001"

    @pytest.mark.asyncio
    async def test_multi_worker_team_ack_installed(
        self,
        runtime: MissionRuntime,
        graph: MissionGraph,
        team_service: TeamPartitionService,
        registry: TeamPartitionRegistry,
    ):
        """Verify that the Team ACK saga reaches INSTALLED before fan-out."""
        graph.replace(
            [
                {
                    "logical_id": "rescue-thomas",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Rescue Thomas",
                },
            ]
        )

        svc = team_service
        ack_count: int = 0

        async def track_acks(tr: PartitionTransition) -> dict[str, bool]:
            nonlocal ack_count
            ack_count += len(tr.affected_workers)
            return {w: True for w in tr.affected_workers}

        svc.set_delivery_adapter(track_acks)

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-a", "Bob": "wtid-b"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "rescue-thomas",
            graph,
            team_service=svc,
        )
        assert result["success"] is True

        # Find the collaborative transition created during activation
        transitions = svc._registry.get_active_transitions()
        collab_transitions = [
            t for t in transitions
            if t.team_id and t.team_id.startswith("team:")
        ]
        assert len(collab_transitions) == 1
        assert collab_transitions[0].status == TransitionStatus.INSTALLED

        # Verify Alice and Bob are in the collaborative team
        for wid in ["Alice", "Bob"]:
            a = svc.get_assignment(wid)
            assert a is not None
            assert not a.is_singleton
            assert a.team_id == collab_transitions[0].team_id

    @pytest.mark.asyncio
    async def test_multi_worker_terminal_aggregation_all_completed(
        self,
        runtime: MissionRuntime,
        team_service: TeamPartitionService,
    ):
        """Both workers complete → logical node completed (exact-once)."""
        from a2a.coordinator.task_store import TaskStore
        store = TaskStore("test multi term", router=None, context_id="ctx-multi")
        store.attach_runtime(runtime)
        # Use store's graph so aggregation finds dispatch bindings.
        graph = store._mission_graph

        graph.replace(
            [
                {
                    "logical_id": "rescue",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Rescue operation",
                },
            ]
        )

        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        svc = team_service
        svc.set_delivery_adapter(all_ack)

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-a", "Bob": "wtid-b"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "rescue",
            graph,
            team_service=svc,
        )
        assert result["success"] is True

        node = graph.get_node("rescue")
        assert node is not None
        assert node.state == "active"

        alice_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Alice"),
        )
        bob_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Bob"),
        )

        runtime.apply_physical_status(
            alice_dispatch.dispatch_id, "COMPLETED", source="callback", result="Done A"
        )
        runtime.apply_physical_status(
            bob_dispatch.dispatch_id, "COMPLETED", source="callback", result="Done B"
        )

        node_after = graph.get_node("rescue")
        assert node_after is not None
        assert node_after.state == "completed"

    @pytest.mark.asyncio
    async def test_multi_worker_one_fails_node_fails(
        self,
        runtime: MissionRuntime,
        team_service: TeamPartitionService,
    ):
        """One worker FAILED → logical node failed, other becomes CANCEL_PENDING."""
        from a2a.coordinator.task_store import TaskStore
        store = TaskStore("test multi fail", router=None, context_id="ctx-multi-fail")
        store.attach_runtime(runtime)
        graph = store._mission_graph

        graph.replace(
            [
                {
                    "logical_id": "rescue",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Rescue operation",
                },
            ]
        )

        async def all_ack(tr: PartitionTransition) -> dict[str, bool]:
            return {w: True for w in tr.affected_workers}

        svc = team_service
        svc.set_delivery_adapter(all_ack)

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-a", "Bob": "wtid-b"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        result = await runtime.activate_plan_node(
            "rescue",
            graph,
            team_service=svc,
        )
        assert result["success"] is True

        alice_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Alice"),
        )
        bob_dispatch = next(
            (d for d in runtime.dispatches.values() if d.worker_id == "Bob"),
        )

        runtime.apply_physical_status(
            alice_dispatch.dispatch_id, "FAILED", source="callback", result="Robot stuck"
        )
        runtime.apply_physical_status(
            bob_dispatch.dispatch_id, "COMPLETED", source="callback", result="Done"
        )

        node_after = graph.get_node("rescue")
        assert node_after is not None
        assert node_after.state == "failed"


# =========================================================================
# GREEN: SendMessageTool routing
# =========================================================================


class TestSendMessageToolActivation:
    """SendMessageTool message_type='activate_plan_node' routing."""

    @pytest.mark.asyncio
    async def test_activate_plan_node_missing_related_task_id(
        self, store: TaskStore
    ):
        registry = AgentRegistry()
        tool = SendMessageTool(store, registry)

        result = await tool.execute(message_type="activate_plan_node")
        assert result.success is False
        assert result.error == "missing_related_task_id"

    @pytest.mark.asyncio
    async def test_activate_plan_node_via_tool(
        self, store: TaskStore, manager: MissionRuntimeManager
    ):
        runtime = manager.active_runtime
        assert runtime is not None

        graph = store._mission_graph
        graph.replace(
            [
                {
                    "logical_id": "scout",
                    "participant_ids": ["Alice"],
                    "objective": "Scout north sector",
                },
            ]
        )

        adapter_info = _succeeding_adapter(
            {"Alice": "wtid-scout-tool"}
        )
        runtime.set_dispatch_adapter(adapter_info["adapter"])

        registry = AgentRegistry()
        registry.register(
            AgentInfo(
                agent_id="Alice",
                description="Test worker",
                endpoint="http://alice:8090",
            )
        )
        tool = SendMessageTool(
            store, registry,
            coordinator_host="localhost",
            coordinator_port=8080,
        )

        result = await tool.execute(
            message_type="activate_plan_node",
            related_task_id="scout",
        )
        assert result.success is True
        assert result.error is None
        assert "scout" in result.content

    @pytest.mark.asyncio
    async def test_activate_plan_node_runtime_unavailable(
        self, store: TaskStore
    ):
        store._runtime = None
        registry = AgentRegistry()
        tool = SendMessageTool(store, registry)

        result = await tool.execute(
            message_type="activate_plan_node",
            related_task_id="scout",
        )
        assert result.success is False
        assert result.error == "runtime_unavailable"

    @pytest.mark.asyncio
    async def test_multi_worker_via_tool_invokes_delivery_before_dispatch(
        self, store: TaskStore, manager: MissionRuntimeManager
    ):
        """Prove SendMessageTool multi-worker activation invokes delivery adapter
        before dispatch adapter — Team ACK saga runs before fan-out dispatch."""
        runtime = manager.active_runtime
        assert runtime is not None

        # Wire TeamPartitionService through the manager (production pattern)
        from a2a.coordinator.team_partition_service import TeamPartitionService
        svc = TeamPartitionService()
        svc.ensure_singletons(["Alice", "Bob"])
        manager.set_team_partition_service(svc)

        graph = store._mission_graph
        graph.replace(
            [
                {
                    "logical_id": "multi-rescue",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Multi-worker rescue",
                },
            ]
        )

        delivery_call_count: int = 0
        dispatch_call_count: int = 0
        order_log: list[str] = []

        async def tracking_delivery(tr):
            nonlocal delivery_call_count
            delivery_call_count += 1
            order_log.append("delivery")
            return {w: True for w in tr.affected_workers}

        svc.set_delivery_adapter(tracking_delivery)

        async def tracking_dispatch(
            worker_id: str, prompt: str, callback_url: str,
            dispatch_id: str, context_id: str,
        ) -> str:
            nonlocal dispatch_call_count
            dispatch_call_count += 1
            order_log.append(f"dispatch:{worker_id}")
            return f"wtid-{worker_id}"

        runtime.set_dispatch_adapter(tracking_dispatch)

        registry = AgentRegistry()
        registry.register(
            AgentInfo(
                agent_id="Alice",
                description="Alice",
                endpoint="http://alice:8090",
            )
        )
        registry.register(
            AgentInfo(
                agent_id="Bob",
                description="Bob",
                endpoint="http://bob:8090",
            )
        )
        tool = SendMessageTool(
            store, registry,
            coordinator_host="localhost",
            coordinator_port=8080,
        )

        result = await tool.execute(
            message_type="activate_plan_node",
            related_task_id="multi-rescue",
        )
        assert result.success is True

        # delivery must have been called before dispatch for each worker
        assert delivery_call_count >= 1
        assert dispatch_call_count == 2
        assert order_log[0] == "delivery"
        assert "dispatch:Alice" in order_log
        assert "dispatch:Bob" in order_log

        # team_id/epoch must be in the result
        assert result.data["team_id"] is not None
        assert result.data["team_epoch"] is not None
