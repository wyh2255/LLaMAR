"""Phase 1 RED/GREEN contracts: pure MissionGraph + runtime-backed dual-store facade."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from a2a.coordinator.mission_graph import (
    MissionGraph,
    MissionGraphError,
    MissionNodeRuntime,
    MissionNodeSpec,
)
from a2a.coordinator.mission_runtime import MissionRuntimeManager, PhysicalState
from a2a.coordinator.task_store import PlanNode, TaskStore


def _spec(
    logical_id: str,
    *,
    participants: list[str] | None = None,
    worker_id: str | None = None,
    depends_on: list[str] | None = None,
    assignments: dict[str, str] | None = None,
    objective: str = "",
    status: str = "pending",
) -> MissionNodeSpec:
    return MissionNodeSpec(
        logical_id=logical_id,
        participant_ids=list(participants or []),
        worker_id=worker_id,
        depends_on=list(depends_on or []),
        assignments=dict(assignments or {}),
        objective=objective,
        status=status,
    )


class TestMissionGraphValidation:
    def test_unique_ids_required(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="duplicate"):
            graph.replace(
                [
                    _spec("a", participants=["Alice"]),
                    _spec("a", participants=["Bob"]),
                ]
            )

    def test_participant_ids_must_be_nonempty_and_unique(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="participant"):
            graph.replace([_spec("a", participants=[])])
        with pytest.raises(MissionGraphError, match="participant"):
            graph.replace([_spec("a", participants=["Alice", "Alice"])])

    def test_worker_id_normalizes_to_participant_ids(self):
        graph = MissionGraph()
        graph.replace([_spec("a", worker_id="Alice")])
        node = graph.get_node("a")
        assert node is not None
        assert node.participant_ids == ["Alice"]

    def test_assignment_keys_must_name_declared_participants(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="assignment"):
            graph.replace(
                [
                    _spec(
                        "a",
                        participants=["Alice"],
                        assignments={"Bob": "do something"},
                    )
                ]
            )

    def test_self_dependency_fails(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="self"):
            graph.replace(
                [_spec("a", participants=["Alice"], depends_on=["a"])]
            )

    def test_missing_dependency_fails(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="unknown dependency|missing"):
            graph.replace(
                [_spec("a", participants=["Alice"], depends_on=["missing"])]
            )

    def test_cycle_fails_deterministically(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="cycle"):
            graph.replace(
                [
                    _spec("a", participants=["Alice"], depends_on=["b"]),
                    _spec("b", participants=["Bob"], depends_on=["a"]),
                ]
            )

    def test_replace_is_atomic_invalid_does_not_mutate(self):
        graph = MissionGraph()
        graph.replace([_spec("root", participants=["Alice"])])
        revision_before = graph.revision
        snapshot_before = graph.snapshot_view()

        with pytest.raises(MissionGraphError):
            graph.replace(
                [
                    _spec("root", participants=["Alice"]),
                    _spec("bad", participants=["Bob"], depends_on=["nope"]),
                ]
            )

        assert graph.revision == revision_before
        assert graph.snapshot_view() == snapshot_before
        assert graph.get_node("root") is not None
        assert graph.get_node("bad") is None


class TestMissionGraphDependencyFrontier:
    def test_node_ready_only_after_every_dependency_successful(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("dep", participants=["Alice"]),
                _spec("child", participants=["Bob"], depends_on=["dep"]),
            ]
        )
        assert graph.get_node("dep").state == "ready"
        assert graph.get_node("child").state == "blocked"
        assert [n.logical_id for n in graph.get_ready_nodes()] == ["dep"]

        graph.mark_activating("dep")
        graph.mark_active("dep")
        graph.attach_dispatch("dep", "Alice", "dsp_dep")
        graph.mark_dispatch_terminal("dep", "Alice", "COMPLETED", result="ok")

        assert graph.get_node("dep").state == "completed"
        assert graph.get_node("child").state == "ready"
        assert [n.logical_id for n in graph.get_ready_nodes()] == ["child"]

    def test_failed_dependency_does_not_unlock(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("dep", participants=["Alice"]),
                _spec("child", participants=["Bob"], depends_on=["dep"]),
            ]
        )
        graph.mark_activating("dep")
        graph.mark_active("dep")
        graph.attach_dispatch("dep", "Alice", "dsp_dep")
        graph.mark_dispatch_terminal("dep", "Alice", "FAILED", result="boom")

        assert graph.get_node("dep").state == "failed"
        assert graph.get_node("child").state == "blocked"
        assert graph.get_ready_nodes() == []

    def test_canceled_dependency_does_not_unlock(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("dep", participants=["Alice"]),
                _spec("child", participants=["Bob"], depends_on=["dep"]),
            ]
        )
        graph.mark_activating("dep")
        graph.mark_active("dep")
        graph.attach_dispatch("dep", "Alice", "dsp_dep")
        graph.mark_dispatch_terminal("dep", "Alice", "CANCELED", result="stop")

        assert graph.get_node("dep").state == "failed"
        assert graph.get_node("child").state == "blocked"

    def test_skipped_dependency_counts_as_successful(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("dep", participants=["Alice"], status="skipped"),
                _spec("child", participants=["Bob"], depends_on=["dep"]),
            ]
        )
        assert graph.get_node("dep").status == "skipped"
        assert graph.get_node("child").state == "ready"


class TestMissionGraphLifecycle:
    def test_lifecycle_planned_blocked_ready_activating_active_completed(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("a", participants=["Alice"]),
                _spec("b", participants=["Bob"], depends_on=["a"]),
            ]
        )
        assert graph.get_node("a").state == "ready"
        assert graph.get_node("b").state == "blocked"

        ok, reason = graph.can_activate("a")
        assert ok is True
        assert reason == ""

        graph.mark_activating("a")
        assert graph.get_node("a").state == "activating"
        graph.mark_active("a")
        assert graph.get_node("a").state == "active"
        graph.attach_dispatch("a", "Alice", "dsp_a")
        graph.mark_dispatch_terminal("a", "Alice", "COMPLETED", result="done")
        assert graph.get_node("a").state == "completed"
        assert graph.get_node("b").state == "ready"

    def test_active_to_failed_and_canceled(self):
        graph = MissionGraph()
        graph.replace([_spec("a", participants=["Alice"])])
        graph.mark_activating("a")
        graph.mark_active("a")
        graph.attach_dispatch("a", "Alice", "dsp_a")
        graph.mark_dispatch_terminal("a", "Alice", "FAILED", result="x")
        assert graph.get_node("a").state == "failed"

        graph.replace([_spec("b", participants=["Bob"])])
        graph.mark_activating("b")
        graph.mark_active("b")
        graph.mark_canceled("b", reason="parent_abort")
        assert graph.get_node("b").state == "canceled"
        assert graph.get_node("b").failure_reason == "parent_abort"

    def test_terminal_monotonicity(self):
        graph = MissionGraph()
        graph.replace([_spec("a", participants=["Alice"])])
        graph.mark_activating("a")
        graph.mark_active("a")
        graph.attach_dispatch("a", "Alice", "dsp_a")
        graph.mark_dispatch_terminal("a", "Alice", "COMPLETED", result="ok")
        graph.mark_dispatch_terminal("a", "Alice", "FAILED", result="late")
        assert graph.get_node("a").state == "completed"

        graph.mark_canceled("a", reason="ignored")
        assert graph.get_node("a").state == "completed"

    def test_can_activate_returns_stable_reasons(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("a", participants=["Alice"]),
                _spec("b", participants=["Bob"], depends_on=["a"]),
            ]
        )
        ok, reason = graph.can_activate("missing")
        assert ok is False
        assert reason == "node_not_found"

        ok, reason = graph.can_activate("b")
        assert ok is False
        assert reason in {"node_not_ready", "dependency_incomplete"}

        graph.mark_activating("a")
        ok, reason = graph.can_activate("a")
        assert ok is False
        assert reason == "node_not_ready"

    def test_replace_rejects_active_or_activating_removal_or_mutation(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec("keep", participants=["Alice"]),
                _spec("live", participants=["Bob", "Charlie"]),
            ]
        )
        graph.mark_activating("live")

        with pytest.raises(MissionGraphError, match="activating|active"):
            graph.replace([_spec("keep", participants=["Alice"])])

        with pytest.raises(MissionGraphError, match="participant|activ"):
            graph.replace(
                [
                    _spec("keep", participants=["Alice"]),
                    _spec("live", participants=["Bob"]),  # participant change
                ]
            )

        with pytest.raises(MissionGraphError, match="depend|activ"):
            graph.replace(
                [
                    _spec("keep", participants=["Alice"]),
                    _spec(
                        "live",
                        participants=["Bob", "Charlie"],
                        depends_on=["keep"],
                    ),
                ]
            )

        # Unchanged active/activating node is preserved.
        graph.replace(
            [
                _spec("keep", participants=["Alice"]),
                _spec("live", participants=["Bob", "Charlie"]),
            ]
        )
        assert graph.get_node("live").state == "activating"

    def test_replace_preserves_runtime_for_semantically_unchanged_nodes(self):
        graph = MissionGraph()
        graph.replace([_spec("a", participants=["Alice"], objective="scout")])
        graph.mark_activating("a")
        graph.mark_active("a")
        graph.attach_dispatch("a", "Alice", "dsp_a")
        # Exactly unchanged active/activating semantics preserve runtime state.
        graph.replace(
            [_spec("a", participants=["Alice"], objective="scout")]
        )
        node = graph.get_node("a")
        assert node.state == "active"
        assert node.dispatch_ids == {"Alice": "dsp_a"}
        assert node.objective == "scout"


class TestDualStoreFacade:
    def test_get_mission_node_returns_graph_runtime_not_plan_or_dispatch(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-graph")
        store = TaskStore("request", router=None, context_id="ctx-graph")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("rescue", participants=["Alice", "Bob"])])

        # Legacy PlanNode surface remains independent.
        store.update_plan(
            [
                {
                    "task_id": "rescue",
                    "worker_id": "Alice",
                    "description": "legacy",
                }
            ]
        )
        legacy = store.get_node("rescue")
        mission = store.get_mission_node("rescue")

        assert isinstance(legacy, PlanNode)
        assert isinstance(mission, MissionNodeRuntime)
        assert not isinstance(mission, PlanNode)
        assert mission.logical_id == "rescue"
        assert mission.participant_ids == ["Alice", "Bob"]
        assert store.get_mission_node("missing") is None

    def test_create_dispatches_preallocates_opaque_ids_and_lists_all(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-fanout")
        store = TaskStore("request", router=None, context_id="ctx-fanout")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        node = store.get_mission_node("rescue")
        assert node is not None
        # Activation gate path used by later phases; Phase 1 only needs allocation API.
        store._mission_graph.mark_activating("rescue")  # noqa: SLF001
        store._mission_graph.mark_active("rescue")  # noqa: SLF001

        dispatches = store.create_dispatches_for_activation(
            "rescue", ["Alice", "Bob"]
        )
        assert len(dispatches) == 2
        ids = {d.dispatch_id for d in dispatches}
        assert len(ids) == 2
        assert all(d.dispatch_id.startswith("dsp_") for d in dispatches)
        assert all(d.state is PhysicalState.PREPARED for d in dispatches)
        assert all(d.logical_node_id == "rescue" for d in dispatches)
        assert {d.worker_id for d in dispatches} == {"Alice", "Bob"}

        listed = store.list_dispatches_for_mission("rescue")
        assert {d.dispatch_id for d in listed} == ids

        mission = store.get_mission_node("rescue")
        assert set(mission.dispatch_ids.values()) == ids
        assert mission.dispatch_ids["Alice"] != mission.dispatch_ids["Bob"]

    def test_physical_lookup_never_accepts_logical_id(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-ns")
        store = TaskStore("request", router=None, context_id="ctx-ns")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("logical-only", participants=["Alice"])])
        store._mission_graph.mark_activating("logical-only")  # noqa: SLF001
        store._mission_graph.mark_active("logical-only")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation(
            "logical-only", ["Alice"]
        )
        assert store.get_dispatch("logical-only") is None
        assert store.get_dispatch(dispatches[0].dispatch_id) is dispatches[0]
        assert store.resolve_dispatch_id("logical-only") is None

    def test_compat_resolver_only_for_unique_single_member_mapping(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-compat")
        store = TaskStore("request", router=None, context_id="ctx-compat")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [
                _spec("solo", participants=["Alice"]),
                _spec("pair", participants=["Bob", "Charlie"]),
            ]
        )
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        store._mission_graph.mark_activating("pair")  # noqa: SLF001
        store._mission_graph.mark_active("pair")  # noqa: SLF001

        solo = store.create_dispatches_for_activation("solo", ["Alice"])
        pair = store.create_dispatches_for_activation("pair", ["Bob", "Charlie"])

        assert store.resolve_compat_dispatch_id("solo") == solo[0].dispatch_id
        assert store.resolve_compat_dispatch_id("pair") is None
        assert len(pair) == 2

    def test_create_dispatches_validates_graph_membership(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-validate")
        store = TaskStore("request", router=None, context_id="ctx-validate")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("known", participants=["Alice"])])

        with pytest.raises((MissionGraphError, ValueError, RuntimeError, KeyError)):
            store.create_dispatches_for_activation("unknown", ["Alice"])

        with pytest.raises((MissionGraphError, ValueError, RuntimeError, KeyError)):
            store.create_dispatches_for_activation("known", ["Eve"])

    def test_legacy_single_dispatch_create_physical_dispatch_still_works(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-legacy")
        store = TaskStore("request", router=None, context_id="ctx-legacy")
        store.attach_runtime(runtime)
        store.add_adhoc_node("adhoc", worker_id="Alice")
        dispatch = store.create_physical_dispatch("adhoc", "Alice")
        assert dispatch is not None
        assert dispatch.dispatch_id.startswith("dsp_")
        assert store.resolve_compat_dispatch_id("adhoc") == dispatch.dispatch_id
        assert store.list_dispatches_for_mission("adhoc") == [dispatch]


class TestExactOnceAggregation:
    def _drive_to_running(self, store, dispatch_id: str) -> None:
        store.apply_physical_status(dispatch_id, "DISPATCHING", source="test")
        store.apply_physical_status(dispatch_id, "ACCEPTED", source="test")
        store.apply_physical_status(dispatch_id, "RUNNING", source="test")

    def test_all_completed_marks_logical_completed_once(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-ok")
        store = TaskStore("request", router=None, context_id="ctx-agg-ok")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        store._mission_graph.mark_activating("rescue")  # noqa: SLF001
        store._mission_graph.mark_active("rescue")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation(
            "rescue", ["Alice", "Bob"]
        )
        by_worker = {d.worker_id: d for d in dispatches}

        self._drive_to_running(store, by_worker["Alice"].dispatch_id)
        self._drive_to_running(store, by_worker["Bob"].dispatch_id)

        store.apply_physical_status(
            by_worker["Alice"].dispatch_id,
            "COMPLETED",
            source="callback",
            result="a",
        )
        assert store.get_mission_node("rescue").state == "active"

        store.apply_physical_status(
            by_worker["Bob"].dispatch_id,
            "COMPLETED",
            source="callback",
            result="b",
        )
        assert store.get_mission_node("rescue").state == "completed"

        # Duplicate terminal must not re-aggregate or regress.
        store.apply_physical_status(
            by_worker["Bob"].dispatch_id,
            "COMPLETED",
            source="duplicate",
            result="b2",
        )
        store.apply_physical_status(
            by_worker["Bob"].dispatch_id,
            "FAILED",
            source="late",
            result="nope",
        )
        assert store.get_mission_node("rescue").state == "completed"

    def test_any_failed_or_canceled_marks_logical_failed(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-fail")
        store = TaskStore("request", router=None, context_id="ctx-agg-fail")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        store._mission_graph.mark_activating("rescue")  # noqa: SLF001
        store._mission_graph.mark_active("rescue")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation(
            "rescue", ["Alice", "Bob"]
        )
        by_worker = {d.worker_id: d for d in dispatches}
        self._drive_to_running(store, by_worker["Alice"].dispatch_id)
        self._drive_to_running(store, by_worker["Bob"].dispatch_id)

        store.apply_physical_status(
            by_worker["Alice"].dispatch_id,
            "FAILED",
            source="callback",
            result="boom",
        )
        assert store.get_mission_node("rescue").state == "failed"

    def test_input_required_keeps_logical_active(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-input")
        store = TaskStore("request", router=None, context_id="ctx-agg-input")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("help", participants=["Alice"])])
        store._mission_graph.mark_activating("help")  # noqa: SLF001
        store._mission_graph.mark_active("help")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("help", ["Alice"])
        self._drive_to_running(store, dispatches[0].dispatch_id)

        store.apply_physical_status(
            dispatches[0].dispatch_id,
            "INPUT_REQUIRED",
            source="callback",
        )
        assert store.get_mission_node("help").state == "active"
        assert dispatches[0].state is PhysicalState.INPUT_REQUIRED

    def test_first_terminal_only_invokes_graph_aggregation_once(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-once")
        store = TaskStore("request", router=None, context_id="ctx-agg-once")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("solo", ["Alice"])
        self._drive_to_running(store, dispatches[0].dispatch_id)

        calls: list[tuple] = []
        original = store._mission_graph.mark_dispatch_terminal  # noqa: SLF001

        def tracking(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        store._mission_graph.mark_dispatch_terminal = tracking  # noqa: SLF001

        store.apply_physical_status(
            dispatches[0].dispatch_id,
            "COMPLETED",
            source="callback",
            result="ok",
        )
        store.apply_physical_status(
            dispatches[0].dispatch_id,
            "COMPLETED",
            source="duplicate",
            result="ok2",
        )
        store.apply_physical_status(
            dispatches[0].dispatch_id,
            "FAILED",
            source="late",
            result="no",
        )

        assert len(calls) == 1
        assert store.get_mission_node("solo").state == "completed"


class TestFailClosedActivationContracts:
    """Reviewer-required fail-closed regressions for Phase-1 dual-store facade."""

    def _activate_pair(self, store: TaskStore, logical_id: str = "rescue") -> None:
        store._mission_graph.mark_activating(logical_id)  # noqa: SLF001
        store._mission_graph.mark_active(logical_id)  # noqa: SLF001

    def test_reject_duplicate_participant_fanout_before_allocation(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-dup-fanout")
        store = TaskStore("request", router=None, context_id="ctx-dup-fanout")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        self._activate_pair(store)

        with pytest.raises(MissionGraphError, match="unique|duplicate|exact"):
            store.create_dispatches_for_activation(
                "rescue", ["Alice", "Alice", "Bob"]
            )

        assert runtime.dispatch_count == 0
        assert store.list_dispatches_for_mission("rescue") == []
        assert store.get_mission_node("rescue").dispatch_ids == {}

    def test_reject_subset_participant_fanout_before_allocation(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-subset-fanout")
        store = TaskStore("request", router=None, context_id="ctx-subset-fanout")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        self._activate_pair(store)

        with pytest.raises(MissionGraphError, match="exact|participant|complete"):
            store.create_dispatches_for_activation("rescue", ["Alice"])

        assert runtime.dispatch_count == 0
        assert store.get_mission_node("rescue").dispatch_ids == {}

    def test_reject_extra_participant_fanout_before_allocation(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-extra-fanout")
        store = TaskStore("request", router=None, context_id="ctx-extra-fanout")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        self._activate_pair(store)

        with pytest.raises(MissionGraphError):
            store.create_dispatches_for_activation(
                "rescue", ["Alice", "Bob", "Eve"]
            )
        assert runtime.dispatch_count == 0

    def test_terminal_reject_leaves_no_prepared_or_mapping(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-terminal-rollback")
        store = TaskStore("request", router=None, context_id="ctx-terminal-rollback")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("solo", participants=["Alice"])])
        self._activate_pair(store, "solo")
        first = store.create_dispatches_for_activation("solo", ["Alice"])
        store.apply_physical_status(
            first[0].dispatch_id, "DISPATCHING", source="test"
        )
        store.apply_physical_status(
            first[0].dispatch_id, "ACCEPTED", source="test"
        )
        store.apply_physical_status(
            first[0].dispatch_id, "RUNNING", source="test"
        )
        store.apply_physical_status(
            first[0].dispatch_id,
            "COMPLETED",
            source="callback",
            result="done",
        )
        assert store.get_mission_node("solo").state == "completed"
        count_before = runtime.dispatch_count

        with pytest.raises(MissionGraphError, match="terminal"):
            store.create_dispatches_for_activation("solo", ["Alice"])

        assert runtime.dispatch_count == count_before
        assert store.list_dispatches_for_mission("solo") == first
        assert set(store.get_mission_node("solo").dispatch_ids.values()) == {
            first[0].dispatch_id
        }

    def test_reattach_with_existing_bindings_rejects_without_orphan_batch(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-reattach")
        store = TaskStore("request", router=None, context_id="ctx-reattach")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        self._activate_pair(store)
        first = store.create_dispatches_for_activation("rescue", ["Alice", "Bob"])
        first_ids = {d.dispatch_id for d in first}
        count_before = runtime.dispatch_count

        with pytest.raises(MissionGraphError, match="already|bound|dispatch"):
            store.create_dispatches_for_activation("rescue", ["Alice", "Bob"])

        assert runtime.dispatch_count == count_before
        listed = store.list_dispatches_for_mission("rescue")
        assert {d.dispatch_id for d in listed} == first_ids
        assert set(store.get_mission_node("rescue").dispatch_ids.values()) == first_ids

    def test_mid_attach_failure_rolls_back_runtime_mapping_and_graph(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-mid-attach")
        store = TaskStore("request", router=None, context_id="ctx-mid-attach")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        self._activate_pair(store)

        # Inject failure during graph attachment after runtime allocation.
        if hasattr(store._mission_graph, "attach_dispatches"):  # noqa: SLF001
            original_batch = store._mission_graph.attach_dispatches  # noqa: SLF001
            calls = {"n": 0}

            def flaky_batch(task_id, bindings):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("injected_attach_failure")
                return original_batch(task_id, bindings)

            store._mission_graph.attach_dispatches = flaky_batch  # noqa: SLF001
        else:
            original_one = store._mission_graph.attach_dispatch  # noqa: SLF001

            def flaky_one(task_id, worker_id, dispatch_id):
                if worker_id == "Bob":
                    raise RuntimeError("injected_attach_failure")
                return original_one(task_id, worker_id, dispatch_id)

            store._mission_graph.attach_dispatch = flaky_one  # noqa: SLF001

        with pytest.raises(RuntimeError, match="injected_attach_failure"):
            store.create_dispatches_for_activation("rescue", ["Alice", "Bob"])

        assert runtime.dispatch_count == 0
        assert store.list_dispatches_for_mission("rescue") == []
        assert store.get_mission_node("rescue").dispatch_ids == {}
        assert store._logical_to_dispatches.get("rescue") in (None, [])  # noqa: SLF001


class TestFailClosedAggregationContracts:
    def _drive_to_running(self, store: TaskStore, dispatch_id: str) -> None:
        store.apply_physical_status(dispatch_id, "DISPATCHING", source="test")
        store.apply_physical_status(dispatch_id, "ACCEPTED", source="test")
        store.apply_physical_status(dispatch_id, "RUNNING", source="test")

    def test_canceled_physical_aggregates_to_logical_failed(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-canceled")
        store = TaskStore("request", router=None, context_id="ctx-agg-canceled")
        store.attach_runtime(runtime)
        store.replace_mission_graph(
            [_spec("rescue", participants=["Alice", "Bob"])]
        )
        store._mission_graph.mark_activating("rescue")  # noqa: SLF001
        store._mission_graph.mark_active("rescue")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation(
            "rescue", ["Alice", "Bob"]
        )
        by_worker = {d.worker_id: d for d in dispatches}
        self._drive_to_running(store, by_worker["Alice"].dispatch_id)
        self._drive_to_running(store, by_worker["Bob"].dispatch_id)

        store.apply_physical_status(
            by_worker["Alice"].dispatch_id,
            "CANCELED",
            source="callback",
            result="stop",
        )
        assert store.get_mission_node("rescue").state == "failed"

    def test_legacy_create_physical_dispatch_not_attached_does_not_aggregate(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-legacy-no-agg")
        store = TaskStore("request", router=None, context_id="ctx-legacy-no-agg")
        store.attach_runtime(runtime)
        # Graph node exists but legacy physical path does not attach to it.
        store.replace_mission_graph([_spec("orphan", participants=["Alice"])])
        store.add_adhoc_node("orphan", worker_id="Alice")
        dispatch = store.create_physical_dispatch("orphan", "Alice")
        assert dispatch is not None

        calls: list[tuple] = []
        original = store._mission_graph.mark_dispatch_terminal  # noqa: SLF001

        def tracking(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        store._mission_graph.mark_dispatch_terminal = tracking  # noqa: SLF001
        self._drive_to_running(store, dispatch.dispatch_id)
        store.apply_physical_status(
            dispatch.dispatch_id,
            "COMPLETED",
            source="callback",
            result="legacy-ok",
        )
        assert calls == []
        # Graph node remains non-terminal (never attached / never aggregated).
        assert store.get_mission_node("orphan").state in {
            "ready",
            "planned",
            "activating",
            "active",
        }

    def test_concurrent_first_terminal_aggregates_exactly_once(self):
        manager = MissionRuntimeManager()
        runtime = manager.admit("ctx-agg-concurrent")
        store = TaskStore("request", router=None, context_id="ctx-agg-concurrent")
        store.attach_runtime(runtime)
        store.replace_mission_graph([_spec("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("solo", ["Alice"])
        self._drive_to_running(store, dispatches[0].dispatch_id)

        barrier = threading.Barrier(2)
        calls: list[tuple] = []
        lock = threading.Lock()
        original = store._mission_graph.mark_dispatch_terminal  # noqa: SLF001

        def tracking(*args, **kwargs):
            with lock:
                calls.append((args, kwargs))
            return original(*args, **kwargs)

        store._mission_graph.mark_dispatch_terminal = tracking  # noqa: SLF001

        def worker(source: str) -> None:
            barrier.wait()
            store.apply_physical_status(
                dispatches[0].dispatch_id,
                "COMPLETED",
                source=source,
                result="ok",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(worker, "callback-a")
            f2 = pool.submit(worker, "callback-b")
            f1.result(timeout=5)
            f2.result(timeout=5)

        assert len(calls) == 1
        assert store.get_mission_node("solo").state == "completed"


class TestMissionGraphContractHardening:
    def test_mission_node_spec_is_immutable(self):
        participants = ["Alice", "Bob"]
        depends = ["dep"]
        assignments = {"Alice": "scout"}
        spec = MissionNodeSpec(
            logical_id="n",
            participant_ids=participants,
            depends_on=depends,
            assignments=assignments,
            objective="go",
            status="pending",
        )
        participants.append("Eve")
        depends.append("other")
        assignments["Bob"] = "mutated"
        assert list(spec.participant_ids) == ["Alice", "Bob"]
        assert list(spec.depends_on) == ["dep"]
        assert dict(spec.assignments) == {"Alice": "scout"}
        with pytest.raises((AttributeError, TypeError)):
            spec.objective = "mutated"  # type: ignore[misc]
        with pytest.raises((TypeError, AttributeError)):
            spec.participant_ids[0] = "Zed"  # type: ignore[index]

    def test_invalid_status_rejected(self):
        graph = MissionGraph()
        with pytest.raises(MissionGraphError, match="status"):
            graph.replace(
                [_spec("a", participants=["Alice"], status="running")]
            )

    def test_query_methods_return_defensive_copies(self):
        graph = MissionGraph()
        graph.replace([_spec("a", participants=["Alice"])])
        node = graph.get_node("a")
        assert node is not None
        node.state = "completed"
        node.dispatch_ids["Alice"] = "hacked"
        node.participant_ids.append("Eve")
        fresh = graph.get_node("a")
        assert fresh is not None
        assert fresh.state == "ready"
        assert fresh.dispatch_ids == {}
        assert list(fresh.participant_ids) == ["Alice"]
        assert fresh is not node

        store = TaskStore("request", router=None, context_id="ctx-copy")
        store.replace_mission_graph([_spec("y", participants=["Bob"])])
        exposed = store.get_mission_node("y")
        assert exposed is not None
        exposed.state = "completed"
        assert store.get_mission_node("y").state == "ready"

    def test_active_replace_rejects_objective_assignments_status_changes(self):
        graph = MissionGraph()
        graph.replace(
            [
                _spec(
                    "live",
                    participants=["Alice"],
                    objective="scout",
                    assignments={"Alice": "go"},
                    status="pending",
                )
            ]
        )
        graph.mark_activating("live")
        graph.mark_active("live")
        graph.attach_dispatch("live", "Alice", "dsp_live")

        with pytest.raises(MissionGraphError, match="objective|semantic|activ"):
            graph.replace(
                [
                    _spec(
                        "live",
                        participants=["Alice"],
                        objective="scout-updated",
                        assignments={"Alice": "go"},
                    )
                ]
            )
        with pytest.raises(MissionGraphError, match="assignment|semantic|activ"):
            graph.replace(
                [
                    _spec(
                        "live",
                        participants=["Alice"],
                        objective="scout",
                        assignments={"Alice": "changed"},
                    )
                ]
            )
        with pytest.raises(MissionGraphError, match="status|semantic|activ|skipped"):
            graph.replace(
                [
                    _spec(
                        "live",
                        participants=["Alice"],
                        objective="scout",
                        assignments={"Alice": "go"},
                        status="skipped",
                    )
                ]
            )

        # Exactly unchanged preserves runtime.
        graph.replace(
            [
                _spec(
                    "live",
                    participants=["Alice"],
                    objective="scout",
                    assignments={"Alice": "go"},
                    status="pending",
                )
            ]
        )
        node = graph.get_node("live")
        assert node.state == "active"
        assert node.dispatch_ids == {"Alice": "dsp_live"}
