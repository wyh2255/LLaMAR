"""Phase 2 RED/GREEN: explicit update_plan + Coordinator Context projection.

Contracts:
1. UpdatePlanTool accepts participant_ids / objective / assignments (enriched format).
2. Invalid graph (cycle, missing participant) rejects deterministically.
3. DispatchTaskTool rejects undeclared task when MissionGraph populated.
4. Coordinator state provider projects structured Mission DAG + Physical Dispatch views.
5. ContextManager renders Mission DAG and Physical Dispatches as separate blocks.
"""

from __future__ import annotations

import asyncio
from typing import Any

from a2a.builtin_tools.dispatch_task import DispatchTaskTool
from a2a.builtin_tools.update_plan import UpdatePlanTool
from a2a.coordinator.mission_runtime import MissionRuntimeManager
from a2a.coordinator.task_store import TaskStore


# ── helpers ──────────────────────────────────────────────────────────


def _make_store(context_id: str = "ctx-p2") -> TaskStore:
    store = TaskStore("p2 request", router=None, context_id=context_id)
    runtime = MissionRuntimeManager().admit(context_id)
    store.attach_runtime(runtime)
    return store


def _enriched(
    task_id: str,
    *,
    participants: list[str] | None = None,
    worker_id: str | None = None,
    depends_on: list[str] | None = None,
    objective: str = "",
    assignments: dict[str, str] | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    """Enriched plan-entry dict (new Phase 2 format)."""
    entry: dict[str, Any] = {"task_id": task_id}
    if participants:
        entry["participant_ids"] = participants
    if worker_id:
        entry["worker_id"] = worker_id
    if depends_on:
        entry["depends_on"] = depends_on
    if objective:
        entry["objective"] = objective
    if assignments:
        entry["assignments"] = assignments
    if status != "pending":
        entry["status"] = status
    return entry


def _legacy(
    task_id: str,
    *,
    worker_id: str | None = None,
    depends_on: list[str] | None = None,
    description: str = "",
    status: str = "pending",
) -> dict[str, Any]:
    """Legacy plan-entry dict (pre-Phase 2 format)."""
    entry: dict[str, Any] = {"task_id": task_id}
    if worker_id:
        entry["worker_id"] = worker_id
    if depends_on:
        entry["depends_on"] = depends_on
    if description:
        entry["description"] = description
    if status != "pending":
        entry["status"] = status
    return entry


# ── TestUpdatePlanToolEnriched ───────────────────────────────────────


class TestUpdatePlanToolEnriched:
    """UpdatePlanTool accepts new enriched format and validates via MissionGraph."""

    def test_accepts_participant_ids_and_objective(self):
        store = _make_store()
        plan = [
            _enriched("scout", participants=["Alice"], objective="scout north"),
            _enriched(
                "rescue",
                participants=["Alice", "Bob"],
                depends_on=["scout"],
                objective="rescue victim",
                assignments={"Alice": "carry stretcher", "Bob": "navigate"},
            ),
        ]
        result = store.replace_mission_graph(plan)
        assert result["nodes"] == 2
        node = store.get_mission_node("scout")
        assert node is not None
        assert node.objective == "scout north"
        assert node.participant_ids == ["Alice"]

        node2 = store.get_mission_node("rescue")
        assert node2 is not None
        assert node2.depends_on == ["scout"]
        assert dict(node2.assignments) == {
            "Alice": "carry stretcher",
            "Bob": "navigate",
        }

    def test_accepts_legacy_format(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _legacy("stask", worker_id="Alice", description="simple"),
            {"task_id": "btask", "worker_id": "Bob"},
        ]
        result = asyncio.run(tool.execute(plan))
        assert result.success
        assert "plan updated" in result.content.lower()
        assert "revision" in result.content.lower()
        # Legacy entries without participant_ids still populate MissionGraph
        # via worker_id normalization.
        node = store.get_mission_node("stask")
        assert node is not None
        assert node.participant_ids == ["Alice"]
        assert node.objective == "simple"

    def test_rejects_cycle_deterministically(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("a", participants=["Alice"], depends_on=["b"]),
            _enriched("b", participants=["Bob"], depends_on=["a"]),
        ]
        result = asyncio.run(tool.execute(plan))
        assert not result.success
        assert "cycle" in result.error.lower()

    def test_rejects_missing_participant(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            {"task_id": "orphan", "description": "no participant"},
        ]
        result = asyncio.run(tool.execute(plan))
        assert not result.success
        assert "participant" in result.error.lower()

    def test_rejects_self_dependency(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("a", participants=["Alice"], depends_on=["a"]),
        ]
        result = asyncio.run(tool.execute(plan))
        assert not result.success
        assert "self" in result.error.lower()

    def test_rejects_unknown_dependency(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("a", participants=["Alice"], depends_on=["ghost"]),
        ]
        result = asyncio.run(tool.execute(plan))
        assert not result.success
        assert "unknown" in result.error.lower() or "dependency" in result.error.lower()

    def test_rejects_duplicate_ids(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("dup", participants=["Alice"]),
            _enriched("dup", participants=["Bob"]),
        ]
        result = asyncio.run(tool.execute(plan))
        assert not result.success
        assert "duplicate" in result.error.lower()

    def test_execute_still_produces_diff_legacy(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("scout", participants=["Alice"], objective="look"),
        ]
        result = asyncio.run(tool.execute(plan))
        assert result.success
        content = result.content.lower()
        assert "plan updated" in content
        assert "added" in content
        assert "scout" in content
        # PlanNode compatibility view also populated
        pnode = store.get_node("scout")
        assert pnode is not None
        assert pnode.state == "pending"

    def test_active_node_fields_preserved_on_replan(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("live", participants=["Alice"])])
        store._mission_graph.mark_activating("live")  # noqa: SLF001
        store._mission_graph.mark_active("live")  # noqa: SLF001
        store._mission_graph.attach_dispatch("live", "Alice", "dsp_live")

        tool = UpdatePlanTool(store)
        # Same plan — should succeed (unchanged active node fields).
        result = asyncio.run(tool.execute([_enriched("live", participants=["Alice"])]))
        assert result.success

        # Changed objective on active node -> rejected
        result2 = asyncio.run(
            tool.execute(
                [_enriched("live", participants=["Alice"], objective="changed")]
            )
        )
        assert not result2.success
        assert "objective" in result2.error.lower() or "cannot" in result2.error.lower()

    def test_backward_compat_preserves_execution_state(self):
        store = _make_store()
        # Set up with legacy path
        store.update_plan(
            [_legacy("t1", worker_id="Alice"), _legacy("t2", worker_id="Bob")]
        )
        store.set_state("t1", "done", result="all good")
        assert store.get_node("t1").state == "done"

        # Replan with same task_id
        tool = UpdatePlanTool(store)
        result = asyncio.run(
            tool.execute(
                [_legacy("t1", worker_id="Alice"), _legacy("t2", worker_id="Bob")]
            )
        )
        assert result.success
        # t1 still done
        assert store.get_node("t1").state == "done"


# ── TestUpdatePlanToolDiffFeedback ───────────────────────────────────


class TestUpdatePlanToolDiffFeedback:
    """P0+P1: replace() returns authoritative diff; UpdatePlanTool surfaces it in content."""

    def test_added_nodes_in_content(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("setup", participants=["Alice"], objective="prep"),
            _enriched(
                "rescue-east",
                participants=["Alice", "Bob"],
                objective="rescue east sector",
            ),
        ]
        result = asyncio.run(tool.execute(plan))
        assert result.success
        content = result.content
        assert "Added:" in content
        assert "rescue-east" in content
        assert "Alice" in content
        assert "Bob" in content
        assert "revision" in content.lower()

    def test_removed_nodes_in_content(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        asyncio.run(
            tool.execute(
                [
                    _enriched("setup", participants=["Alice"]),
                    _enriched("scout-north", participants=["Bob"]),
                ]
            )
        )
        result = asyncio.run(
            tool.execute([_enriched("setup", participants=["Alice"])])
        )
        assert result.success
        assert "Removed:" in result.content
        assert "scout-north" in result.content

    def test_modified_objective_shows_reset(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        asyncio.run(
            tool.execute(
                [
                    _enriched(
                        "transport",
                        participants=["Alice"],
                        objective="搜索北部",
                    )
                ]
            )
        )
        result = asyncio.run(
            tool.execute(
                [
                    _enriched(
                        "transport",
                        participants=["Alice"],
                        objective="搜索南部",
                    )
                ]
            )
        )
        assert result.success
        content = result.content
        assert "Modified:" in content
        assert "transport" in content
        assert "搜索北部" in content
        assert "搜索南部" in content
        assert "reset" in content.lower()
        # Structured data for observability
        assert result.data is not None
        diff = result.data["diff"]
        assert "transport" in diff["reset"]
        assert any(m["logical_id"] == "transport" for m in diff["modified"])

    def test_preserved_unchanged_nodes(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        base = [
            _enriched("setup", participants=["Alice"], objective="prep"),
            _enriched(
                "staging",
                participants=["Bob"],
                depends_on=["setup"],
                objective="stage gear",
            ),
        ]
        asyncio.run(tool.execute(base))
        # Add a new node; setup/staging unchanged
        result = asyncio.run(
            tool.execute(
                base
                + [
                    _enriched(
                        "rescue-east",
                        participants=["Alice"],
                        depends_on=["staging"],
                        objective="go east",
                    )
                ]
            )
        )
        assert result.success
        content = result.content
        assert "Preserved:" in content
        assert "setup" in content
        assert "staging" in content
        assert "Added:" in content
        assert "rescue-east" in content

    def test_frozen_active_node_in_content(self):
        store = _make_store()
        store.replace_mission_graph(
            [_enriched("search-fire", participants=["Alice"], objective="fight fire")]
        )
        store._mission_graph.mark_activating("search-fire")  # noqa: SLF001
        store._mission_graph.mark_active("search-fire")  # noqa: SLF001

        tool = UpdatePlanTool(store)
        result = asyncio.run(
            tool.execute(
                [
                    _enriched(
                        "search-fire", participants=["Alice"], objective="fight fire"
                    ),
                    _enriched(
                        "rescue-east",
                        participants=["Bob"],
                        depends_on=["search-fire"],
                        objective="rescue after fire",
                    ),
                ]
            )
        )
        assert result.success
        content = result.content
        assert "Frozen" in content
        assert "search-fire" in content
        assert "active" in content.lower()
        assert result.data is not None
        assert "search-fire" in result.data["diff"]["frozen"]
        assert "search-fire" in result.data["diff"]["preserved"]

    def test_revision_number_in_content(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        r1 = asyncio.run(
            tool.execute([_enriched("a", participants=["Alice"], objective="one")])
        )
        assert r1.success
        assert "revision 1" in r1.content.lower()
        r2 = asyncio.run(
            tool.execute(
                [
                    _enriched("a", participants=["Alice"], objective="one"),
                    _enriched("b", participants=["Bob"], objective="two"),
                ]
            )
        )
        assert r2.success
        assert "revision 2" in r2.content.lower()

    def test_ready_now_list_correct(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        result = asyncio.run(
            tool.execute(
                [
                    _enriched("setup", participants=["Alice"], objective="prep"),
                    _enriched(
                        "blocked-child",
                        participants=["Bob"],
                        depends_on=["setup"],
                        objective="wait",
                    ),
                    _enriched("rescue-east", participants=["Charlie"], objective="go"),
                ]
            )
        )
        assert result.success
        content = result.content
        assert "Ready now:" in content
        # setup and rescue-east are ready; blocked-child is blocked
        ready_line = [
            line for line in content.splitlines() if line.strip().startswith("Ready now:")
        ][0]
        assert "setup" in ready_line
        assert "rescue-east" in ready_line
        assert "blocked-child" not in ready_line
        assert "ready=" in content.lower() or "State:" in content

    def test_fail_remove_active_does_not_update_legacy(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("live", participants=["Alice"], objective="work"),
            _enriched("other", participants=["Bob"], objective="idle"),
        ]
        assert asyncio.run(tool.execute(plan)).success
        store._mission_graph.mark_activating("live")  # noqa: SLF001
        store._mission_graph.mark_active("live")  # noqa: SLF001

        # Capture legacy plan before failed replace
        legacy_before = [n.task_id for n in store._plan]  # noqa: SLF001
        result = asyncio.run(
            tool.execute([_enriched("other", participants=["Bob"], objective="idle")])
        )
        assert not result.success
        assert result.error is not None
        err = result.error.lower()
        assert "cannot" in err or "active" in err or "activating" in err
        # Legacy plan must not have been updated on MissionGraphError
        legacy_after = [n.task_id for n in store._plan]  # noqa: SLF001
        assert legacy_after == legacy_before
        assert store.get_mission_node("live") is not None
        assert store.get_mission_node("live").state == "active"

    def test_identical_replan_shows_no_changes(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        plan = [
            _enriched("setup", participants=["Alice"], objective="prep"),
            _enriched(
                "staging",
                participants=["Bob"],
                depends_on=["setup"],
                objective="stage",
            ),
        ]
        assert asyncio.run(tool.execute(plan)).success
        result = asyncio.run(tool.execute(plan))
        assert result.success
        content = result.content
        assert "No changes" in content
        assert "revision" in content.lower()
        assert result.data is not None
        diff = result.data["diff"]
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["modified"] == []
        assert set(diff["preserved"]) == {"setup", "staging"}

    def test_first_plan_signals_graph_mode_active(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        result = asyncio.run(
            tool.execute([_enriched("setup", participants=["Alice"], objective="prep")])
        )
        assert result.success
        assert "Graph mode active" in result.content

    def test_subsequent_plan_does_not_repeat_graph_mode_signal(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        assert asyncio.run(
            tool.execute([_enriched("setup", participants=["Alice"], objective="prep")])
        ).success
        result = asyncio.run(
            tool.execute(
                [
                    _enriched("setup", participants=["Alice"], objective="prep"),
                    _enriched("rescue", participants=["Bob"], objective="save"),
                ]
            )
        )
        assert result.success
        assert "Graph mode active" not in result.content

    def test_empty_plan_does_not_signal_graph_mode(self):
        store = _make_store()
        tool = UpdatePlanTool(store)
        result = asyncio.run(tool.execute([]))
        assert result.success
        assert "Graph mode active" not in result.content


# ── TestDispatchTaskChecksMissionGraph ───────────────────────────────


class TestDispatchTaskChecksMissionGraph:
    """DispatchTaskTool rejects undeclared tasks when MissionGraph is populated."""

    class RouterMock:
        async def send_task_async(
            self, agent_id, prompt, callback_url, task_id, context_id=None
        ):
            return f"wt-{task_id}"

    def _tool(self, store: TaskStore) -> DispatchTaskTool:
        store._router = self.RouterMock()
        return DispatchTaskTool(
            store, coordinator_host="localhost", coordinator_port=8080
        )

    def test_rejects_undeclared_task_when_graph_populated(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("declared", participants=["Alice"])])
        tool = self._tool(store)
        result = asyncio.run(
            tool.execute(agent_id="Alice", prompt="do something", task_id="undeclared")
        )
        assert not result.success
        assert "undeclared" in (result.error or "").lower()

    def test_allows_legacy_ad_hoc_when_graph_empty(self):
        store = _make_store()
        tool = self._tool(store)
        result = asyncio.run(
            tool.execute(agent_id="Alice", prompt="do something", task_id="adhoc-1")
        )
        assert result.success

    def test_requires_activation_for_declared_node(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("declared", participants=["Alice"])])
        tool = self._tool(store)
        result = asyncio.run(
            tool.execute(agent_id="Alice", prompt="do something", task_id="declared")
        )
        assert not result.success
        assert result.error == "graph_activation_required"
        assert "activate_plan_node" in result.content

    def test_rejects_worker_not_in_participants(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("declared", participants=["Alice"])])
        tool = self._tool(store)
        result = asyncio.run(
            tool.execute(agent_id="Bob", prompt="do something", task_id="declared")
        )
        assert not result.success
        assert "participant" in (result.error or "").lower()


# ── TestCoordinatorDAGProjection ─────────────────────────────────────


class TestCoordinatorMissionDAGProjection:
    """SARCoordinatorStateProvider projects structured Mission DAG + Physical Dispatch views."""

    def _make_provider(self, store: TaskStore):
        """Create minimal provider backed by the given store."""
        from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

        provider = SARCoordinatorStateProvider(state_mode="semantic")
        provider.set_task_store(store)
        provider.set_runtime(store._runtime)  # noqa: SLF001
        return provider

    def test_mission_dag_view_empty_without_graph(self):
        store = _make_store()
        provider = self._make_provider(store)
        dag = provider._build_mission_dag_view()  # noqa: SLF001
        assert dag == []

    def test_mission_dag_view_reflects_graph_state(self):
        store = _make_store()
        store.replace_mission_graph(
            [
                _enriched("scout", participants=["Alice"]),
                _enriched(
                    "rescue",
                    participants=["Alice", "Bob"],
                    depends_on=["scout"],
                    objective="rescue victim",
                ),
            ]
        )
        provider = self._make_provider(store)
        dag = provider._build_mission_dag_view()  # noqa: SLF001
        assert len(dag) == 2
        ids = {entry["logical_id"] for entry in dag}
        assert ids == {"scout", "rescue"}
        by_id = {entry["logical_id"]: entry for entry in dag}
        assert by_id["scout"]["state"] in ("ready", "planned")
        assert by_id["rescue"]["state"] in ("blocked", "planned")
        assert by_id["rescue"]["depends_on"] == ["scout"]
        assert by_id["rescue"]["objective"] == "rescue victim"
        assert by_id["rescue"]["participant_ids"] == ["Alice", "Bob"]

    def test_physical_dispatches_view(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("solo", ["Alice"])

        provider = self._make_provider(store)
        phys = provider._build_physical_dispatches_view()  # noqa: SLF001
        assert len(phys) == 1
        entry = phys[0]
        assert entry["dispatch_id"] == dispatches[0].dispatch_id
        assert entry["worker_id"] == "Alice"
        assert entry["logical_node_id"] == "solo"
        assert entry["state"] == "PREPARED"

    def test_physical_dispatches_view_empty_without_runtime(self):
        store = TaskStore("no-runtime", router=None, context_id="ctx-p2-no-rt")
        provider = self._make_provider(store)
        phys = provider._build_physical_dispatches_view()  # noqa: SLF001
        assert phys == []


# ── TestContextManagerDAGAndDispatchRendering ────────────────────────


class TestContextManagerMissionDAGRendering:
    """CoordinatorContextManager renders Mission DAG and Physical Dispatches as blocks."""

    def _build_runtime_state_payload(self, store: TaskStore) -> dict[str, Any]:
        from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

        provider = SARCoordinatorStateProvider(state_mode="semantic")
        provider.set_task_store(store)
        provider.set_runtime(store._runtime)  # noqa: SLF001
        return {
            "step_budget": {"current_step": 1, "max_steps": 50, "remaining": 49},
            "mission_finished": False,
            "task_status_view": provider._build_task_status_view(),  # noqa: SLF001
            "mission_dag_view": provider._build_mission_dag_view(),  # noqa: SLF001
            "physical_dispatches_view": provider._build_physical_dispatches_view(),  # noqa: SLF001
            "recent_changes": [],
            "supervision": {"alerts": [], "unacknowledged_events": []},
            "state_mode": "semantic",
            "semantic_summary": {},
            "team_status_summary": {"workers": []},
        }

    def _build_context(self, store: TaskStore):
        from Agent.router_agent.context import (
            CoordinatorContextManager,
            ContextConfig,
        )
        from Agent.router_agent.state_provider import RuntimeState
        import time

        payload = self._build_runtime_state_payload(store)

        class FixedStateProvider:
            def snapshot(self, context_id=None):
                return RuntimeState(
                    version=1,
                    env_step=1,
                    observed_at=time.monotonic(),
                    payload=payload,
                )

        config = ContextConfig()
        config.state_mode = "semantic"
        ctx = CoordinatorContextManager(config=config, token_limit=80000)
        ctx._state_provider = FixedStateProvider()  # noqa: SLF001
        ctx.refresh_runtime_state()
        return ctx

    def test_render_current_state_includes_mission_dag(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "Mission DAG" in rendered
        assert "scout" in rendered
        assert "Alice" in rendered

    def test_render_current_state_includes_physical_dispatches(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("solo", ["Alice"])
        store.apply_physical_status(
            dispatches[0].dispatch_id, "DISPATCHING", source="test"
        )

        ctx = self._build_context(store)
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "Physical Dispatches" in rendered
        assert dispatches[0].dispatch_id in rendered
        assert "DISPATCHING" in rendered

    def test_memory_block_separates_dag_from_current_state(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        block = ctx._render_memory_block()  # noqa: SLF001
        assert "### Mission DAG" in block
        assert "### Current State" in block
        dag_pos = block.index("### Mission DAG")
        state_pos = block.index("### Current State")
        assert dag_pos != state_pos

    def test_dag_renders_empty_when_no_graph(self):
        store = _make_store()
        ctx = self._build_context(store)
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "Mission DAG" not in rendered

    def test_physical_dispatches_empty_when_no_dispatches(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "Physical Dispatches" not in rendered

    def test_state_unchanged_detection_works_with_dag_version(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001
        rendered2 = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" in rendered2

    # ── Digest regression: field mutations must break state-unchanged ──

    def test_dispatch_state_mutation_breaks_state_unchanged(self):
        """Changing a dispatch state must change the digest."""
        store = _make_store()
        store.replace_mission_graph([_enriched("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        store.create_dispatches_for_activation("solo", ["Alice"])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        # Mutate a dispatch state via the pinned state directly
        ctx._pinned_state.physical_dispatches_view[0]["state"] = "RUNNING"  # noqa: SLF001
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dispatch_result_preview_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        store.create_dispatches_for_activation("solo", ["Alice"])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.physical_dispatches_view[0]["result_preview"] = (
            "rescued victim"  # noqa: SLF001
        )
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dispatch_artifact_preview_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("solo", participants=["Alice"])])
        store._mission_graph.mark_activating("solo")  # noqa: SLF001
        store._mission_graph.mark_active("solo")  # noqa: SLF001
        store.create_dispatches_for_activation("solo", ["Alice"])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.physical_dispatches_view[0]["artifact_preview"] = (
            "photo evidence"  # noqa: SLF001
        )
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dag_participants_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.mission_dag_view[0]["participant_ids"] = ["Alice", "Bob"]  # noqa: SLF001
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dag_objective_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph(
            [_enriched("scout", participants=["Alice"], objective="original")]
        )
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.mission_dag_view[0]["objective"] = "changed objective"  # noqa: SLF001
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dag_failure_reason_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        store._mission_graph.mark_activating("scout")  # noqa: SLF001
        store._mission_graph.mark_active("scout")  # noqa: SLF001
        dispatches = store.create_dispatches_for_activation("scout", ["Alice"])
        store.apply_physical_status(dispatches[0].dispatch_id, "FAILED", source="test")
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.mission_dag_view[0]["failure_reason"] = "new error"  # noqa: SLF001
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered

    def test_dag_state_mutation_breaks_state_unchanged(self):
        store = _make_store()
        store.replace_mission_graph([_enriched("scout", participants=["Alice"])])
        ctx = self._build_context(store)
        ctx._render_current_state()  # noqa: SLF001

        ctx._pinned_state.mission_dag_view[0]["state"] = "active"  # noqa: SLF001
        rendered = ctx._render_current_state()  # noqa: SLF001
        assert "State unchanged" not in rendered
