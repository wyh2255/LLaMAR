"""Phase 4 — concrete EnvironmentStateProvider composition root.

The concrete provider (``sar_orch.environment_state_provider``) combines a
``MemoryReadPort`` (Spatial / Temporal / Embodied canonical reads) with a
``ControlPlaneReadPort`` (MissionRuntime / TaskStore task views).  These tests
assert:

- task state comes from the control plane and Temporal delete/reorder cannot
  change it;
- cursor monotonicity and cross-epoch reset;
- budget section priority (Task > Spatial > Embodied > Temporal > Freshness);
- FRESH / STALE / UNAVAILABLE freshness semantics;
- NeedInput resume tool-call closure is preserved on the read-port assemble
  path;
- the provider never imports SARBarrier / oracle truth.
"""

from __future__ import annotations

from typing import Any

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from a2a.coordinator.mission_runtime import PhysicalDispatch, PhysicalState
from Agent.environment_state import (
    EnvironmentStateQuery,
    Freshness,
)
from Agent.worker_agent.context import ContextConfig as WorkerConfig
from Agent.worker_agent.context import WorkerContextManager
from Agent.worker_agent.schema import FunctionCall, ToolCall
from Agent.worker_agent.schema import Message as WorkerMessage
from sar_orch.environment_state_provider import (
    ControlPlaneReadPort,
    EnvironmentStateProvider,
    MemoryReadPort,
    render_environment_state_view,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _input(
    scope_id,
    *,
    event_id,
    domain,
    entity_id,
    entity_type,
    field_name,
    value: Any,
    env_step: int | None,
    actor_id: str,
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id=actor_id,
        provenance="worker_sensor_tool",
        domain=domain,
        entity_id=entity_id,
        entity_type=entity_type,
        field_name=field_name,
        value=value,
    )


class FakeRuntime:
    """Control-plane double with dispatch state + control_revision."""

    def __init__(self, dispatches: list[PhysicalDispatch]):
        self._dispatches = {d.dispatch_id: d for d in dispatches}
        self._control_revisions = {d.dispatch_id: 1 for d in dispatches}

    @property
    def dispatches(self) -> dict[str, PhysicalDispatch]:
        return self._dispatches

    def get_dispatch(self, dispatch_id: str) -> PhysicalDispatch | None:
        return self._dispatches.get(dispatch_id)

    def control_revision_of(self, dispatch_id: str) -> int:
        return self._control_revisions.get(dispatch_id, 0)


def _seed_memory(ingestor, scope_factory) -> str:
    """Seed spatial + embodied canonical projections; return scope_id."""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    inputs = [
        _input(
            scope_id,
            event_id="evt_pos",
            domain="embodied",
            entity_id="Alice",
            entity_type="agent",
            field_name="position",
            value=[3, 4, 0],
            env_step=8,
            actor_id="alice",
        ),
        _input(
            scope_id,
            event_id="evt_inv",
            domain="embodied",
            entity_id="Alice",
            entity_type="agent",
            field_name="inventory",
            value=["Water"],
            env_step=8,
            actor_id="alice",
        ),
        _input(
            scope_id,
            event_id="evt_fire",
            domain="spatial",
            entity_id="FireA",
            entity_type="fire",
            field_name="intensity",
            value="High",
            env_step=8,
            actor_id="alice",
        ),
    ]
    assert ingestor.ingest_projection(inputs).status == "ok"
    return scope_id


def _provider(
    store, runtime, scope_id, *, viewer_role="coordinator", viewer_id="system"
):
    memory_port = MemoryReadPort(store, scope_id)
    control_port = ControlPlaneReadPort(runtime)
    return EnvironmentStateProvider(
        memory_port,
        control_port,
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
    )


def _query(scope_id, *, viewer_role="coordinator", viewer_id="system", **kw):
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        temporal_cursor=kw.get("temporal_cursor", 0),
        token_budget=kw.get("token_budget", 1000),
        current_dispatch_id=kw.get("current_dispatch_id"),
    )


# ---------------------------------------------------------------------------
# Concrete provider combines MemoryReadPort + ControlPlaneReadPort
# ---------------------------------------------------------------------------


def test_provider_combines_memory_and_control_ports(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    dispatch = PhysicalDispatch(
        dispatch_id="d1",
        context_id="ctx-1",
        logical_node_id="n1",
        worker_id="Alice",
        worker_task_id="wt-1",
        state=PhysicalState.RUNNING,
    )
    runtime = FakeRuntime([dispatch])
    provider = _provider(store, runtime, scope_id)

    view = provider.query_environment_state(_query(scope_id))

    assert view.freshness == Freshness.FRESH
    sections = view.sections
    # Memory port section populated.
    assert sections["spatial_state"]["FireA"]["fields"]["intensity"]["value"] == "High"
    assert sections["embodied_state"]["Alice"]["fields"]["position"]["value"] == [
        3,
        4,
        0,
    ]
    # Control plane port section populated.
    task = sections["task_execution_state"][0]
    assert task["dispatch_id"] == "d1"
    assert task["state"] == "RUNNING"
    assert task["control_revision"] == 1


# ---------------------------------------------------------------------------
# Task state must come from the control plane, not Temporal events
# ---------------------------------------------------------------------------


def test_task_state_comes_from_control_plane_not_temporal(
    ingestor, store, scope_factory
):
    scope_id = _seed_memory(ingestor, scope_factory)
    dispatch = PhysicalDispatch(
        dispatch_id="d1",
        context_id="ctx-1",
        logical_node_id="n1",
        worker_id="Alice",
        worker_task_id="wt-1",
        state=PhysicalState.RUNNING,
    )
    runtime = FakeRuntime([dispatch])
    provider = _provider(store, runtime, scope_id)

    view = provider.query_environment_state(_query(scope_id))
    assert view.sections["task_execution_state"][0]["state"] == "RUNNING"

    # Deleting / reordering Temporal events cannot change the task view.
    with store._lock:
        store._conn.execute("DELETE FROM temporal_event WHERE scope_id=?", (scope_id,))
        store._conn.commit()
    view2 = provider.query_environment_state(_query(scope_id))
    assert view2.freshness == Freshness.FRESH
    assert view2.sections["task_execution_state"][0]["state"] == "RUNNING"
    assert view2.sections["task_execution_state"][0]["control_revision"] == 1


# ---------------------------------------------------------------------------
# Cursor monotonicity + cross-epoch reset
# ---------------------------------------------------------------------------


def test_cursor_is_monotonic_and_filters_temporal_delta(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    extra = _input(
        scope_id,
        event_id="evt_late",
        domain="spatial",
        entity_id="FireB",
        entity_type="fire",
        field_name="intensity",
        value="Low",
        env_step=9,
        actor_id="alice",
    )
    assert ingestor.ingest_projection([extra]).status == "ok"
    provider = _provider(store, None, scope_id)

    v1 = provider.query_environment_state(_query(scope_id))
    first_cursor = v1.sections["next_cursor"]
    assert first_cursor >= 0

    v2 = provider.query_environment_state(
        _query(scope_id, temporal_cursor=first_cursor)
    )
    assert v2.sections["next_cursor"] >= first_cursor
    assert v2.sections["relevant_events"] != v1.sections["relevant_events"]

    v3 = provider.query_environment_state(
        _query(scope_id, temporal_cursor=v2.sections["next_cursor"])
    )
    assert v3.sections["next_cursor"] >= v2.sections["next_cursor"]


def test_cross_epoch_reset_uses_fresh_cursor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    sid_old = ing.activate_scope("ctx-1", 0)
    sid_new = ing.activate_scope("ctx-1", 1)
    assert sid_old != sid_new

    old_provider = _provider(store, None, sid_old)
    new_provider = _provider(store, None, sid_new)
    old_provider.query_environment_state(_query(sid_old))
    new_view = new_provider.query_environment_state(_query(sid_new))
    assert new_view.sections["relevant_events"] == []
    assert new_view.sections["next_cursor"] == 0


# ---------------------------------------------------------------------------
# Budget section priority
# ---------------------------------------------------------------------------


def test_budget_section_priority_truncates_low_priority_first(
    ingestor, store, scope_factory
):
    scope_id = _seed_memory(ingestor, scope_factory)
    dispatch = PhysicalDispatch(
        dispatch_id="d1",
        context_id="ctx-1",
        logical_node_id="n1",
        worker_id="Alice",
        worker_task_id="wt-1",
        state=PhysicalState.RUNNING,
    )
    runtime = FakeRuntime([dispatch])
    provider = _provider(store, runtime, scope_id)

    view = provider.query_environment_state(_query(scope_id, token_budget=1))
    assert view.sections["task_execution_state"] != []
    assert view.sections["spatial_state"] != {}
    assert view.sections["relevant_events"] == []
    assert view.sections["embodied_state"] == {}

    view0 = provider.query_environment_state(_query(scope_id, token_budget=0))
    assert view0.sections["task_execution_state"] == []
    assert view0.sections["spatial_state"] == {}
    assert view0.sections["embodied_state"] == {}
    assert view0.sections["relevant_events"] == []
    assert "scope_id" in view0.sections["freshness"]
    assert view0.sections["truncated"] is True


# ---------------------------------------------------------------------------
# Freshness: FRESH / STALE / UNAVAILABLE
# ---------------------------------------------------------------------------


def test_freshness_fresh_when_memory_read_ok(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    view = provider.query_environment_state(_query(scope_id))
    assert view.freshness == Freshness.FRESH
    assert view.reason == ""
    assert view.sections["freshness"]["memory_revision"] >= 1


def test_freshness_stale_keeps_prior_view_on_read_error(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    first = provider.query_environment_state(_query(scope_id))
    assert first.freshness == Freshness.FRESH

    def _boom(*a, **k):
        raise RuntimeError("memory unavailable")

    provider._memory_read_port.temporal_events = _boom
    second = provider.query_environment_state(_query(scope_id))
    assert second.freshness == Freshness.STALE
    assert "memory unavailable" in second.reason
    assert second.source_revision is not None


def test_freshness_unavailable_when_no_safe_projection(store, scope_factory):
    scope = scope_factory.resolve("ctx-1", 0)
    store.activate_scope(scope)
    provider = _provider(store, None, scope.scope_id)

    def _boom(*a, **k):
        raise RuntimeError("no memory")

    provider._memory_read_port.temporal_events = _boom
    view = provider.query_environment_state(_query(scope.scope_id))
    assert view.freshness == Freshness.UNAVAILABLE


# ---------------------------------------------------------------------------
# Provider never uses SARBarrier / oracle truth (H1-INV-1)
# ---------------------------------------------------------------------------


def test_provider_reads_only_memory_and_control_ports():
    import inspect

    from sar_orch import environment_state_provider as mod

    src = inspect.getsource(mod)
    assert "get_env_snapshot" not in src
    assert "ground_truth" not in src
    assert "coverage_truth" not in src


# ---------------------------------------------------------------------------
# NeedInput resume tool-call closure on the read-port assemble path
# ---------------------------------------------------------------------------


def test_read_port_assemble_rejects_unclosed_tool_call(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    ctx = WorkerContextManager(
        config=WorkerConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=provider,
    )
    pending = ToolCall(
        id="pending-1",
        type="function",
        function=FunctionCall(name="navigate_to", arguments={}),
    )
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="go"),
        WorkerMessage(role="assistant", content="", tool_calls=[pending]),
    ]

    assembled = ctx.assemble("system", messages)
    assert assembled[-1].role == "assistant"
    assert all(
        m.role != "user" or "## Environment State" not in (m.content or "")
        for m in assembled
    )


def test_read_port_assemble_appends_view_after_closure(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    ctx = WorkerContextManager(
        config=WorkerConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=provider,
    )
    pending = ToolCall(
        id="closed-1",
        type="function",
        function=FunctionCall(name="navigate_to", arguments={}),
    )
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="go"),
        WorkerMessage(role="assistant", content="", tool_calls=[pending]),
        WorkerMessage(
            role="tool", content="result", tool_call_id=pending.id, name="navigate_to"
        ),
    ]
    assembled = ctx.assemble("system", messages)
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content


# ---------------------------------------------------------------------------
# Pure renderer helper
# ---------------------------------------------------------------------------


def test_render_environment_state_view_renders_sections(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    runtime = FakeRuntime(
        [
            PhysicalDispatch(
                dispatch_id="d1",
                context_id="ctx-1",
                logical_node_id="n1",
                worker_id="Alice",
                worker_task_id="wt-1",
                state=PhysicalState.RUNNING,
            )
        ]
    )
    provider = _provider(store, runtime, scope_id)
    view = provider.query_environment_state(_query(scope_id))
    text = render_environment_state_view(view)
    assert "## Environment State" in text
    assert "Spatial State" in text
    assert "Embodied State" in text
    assert "Relevant Recent Events" in text
    assert "Task Execution State" in text
    assert "Freshness" in text


def test_render_unavailable_view_renders_freshness(ingestor, store, scope_factory):
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    provider._memory_read_port.temporal_events = _boom
    view = provider.query_environment_state(_query(scope_id))
    text = render_environment_state_view(view)
    assert "UNAVAILABLE" in text or "unavailable" in text.lower()


# ---------------------------------------------------------------------------
# Shadow compare: legacy/new view diff with allowlist (Phase 4 contract)
# ---------------------------------------------------------------------------


def test_shadow_compare_allowlist_diffs_are_allowed(ingestor, store, scope_factory):
    from sar_orch.environment_state_provider import compare_legacy_vs_read_port

    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    view = provider.query_environment_state(_query(scope_id))
    # Parallel legacy projection: same domain structure, but metadata
    # (revision / cursor / evidence) differs — those are allowlisted.
    import copy

    legacy = copy.deepcopy(view.sections)
    legacy["freshness"]["memory_revision"] = 999
    legacy["freshness"]["view_revision"] = 999
    legacy["freshness"]["as_of_sequence"] = 0
    legacy["next_cursor"] = 0
    result = compare_legacy_vs_read_port(legacy, view)
    assert result.clean


def test_shadow_compare_reports_non_allowlist_field_diff(
    ingestor, store, scope_factory
):
    from sar_orch.environment_state_provider import compare_legacy_vs_read_port

    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    view = provider.query_environment_state(_query(scope_id))
    # Legacy disagrees on an actual domain field (intensity value).
    legacy = {
        "spatial_state": {"FireA": {"fields": {"intensity": {"value": "Low"}}}},
    }
    result = compare_legacy_vs_read_port(legacy, view)
    assert not result.clean
    assert any("intensity" in d.path for d in result.non_allowlist_diffs)


def test_rollout_audit_writer_records_non_allowlist_diffs(
    ingestor, store, scope_factory, tmp_path
):
    from sar_orch.environment_state_provider import (
        RolloutAuditWriter,
        compare_legacy_vs_read_port,
    )

    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    view = provider.query_environment_state(_query(scope_id))
    import copy

    legacy = copy.deepcopy(view.sections)
    legacy["spatial_state"]["FireA"]["fields"]["intensity"]["value"] = "Low"

    result = compare_legacy_vs_read_port(legacy, view)
    assert not result.clean

    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    writer = RolloutAuditWriter(audit_path)
    written = writer.record_diff(scope_id=scope_id, result=result)
    assert written == len(result.non_allowlist_diffs)
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert any("intensity" in line for line in lines)
    # Canonical DB is never deleted by a rollback audit.
    assert store.temporal_event_count(scope_id) >= 1


# ---------------------------------------------------------------------------
# Phase 0（P0）增补 —— 长期记忆段 ACL/预算契约（主方案 RED contract #6）
# 只追加；不改动既有测试与 fixture。
# ---------------------------------------------------------------------------


def test_worker_view_never_contains_long_term_memory_section(store, scope_factory):
    """worker viewer 的 fresh view 永远没有 ``long_term_memory`` section。

    GREEN 守护（现在成立；Phase 5 若泄漏给 worker 本测试转红）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, None, scope_id, viewer_role="worker", viewer_id="Alice")
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" not in view.sections


def test_coordinator_view_missing_long_term_section_before_phase5(
    ingestor, store, scope_factory
):
    """coordinator（system）fresh view 在 Phase 5 后必须包含 published-only 的
    ``long_term_memory`` 段；当前永不产生 → AssertionError（预期 RED）。

    Phase 5 实现后转 GREEN。
    """
    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id, viewer_role="coordinator", viewer_id="system")
    view = provider.query_environment_state(_query(scope_id, viewer_role="coordinator", viewer_id="system"))
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" in view.sections  # RED: 当前无长期段


def test_budget_priority_order_includes_long_term_after_embodied():
    """预算丢弃顺序必须钉死：Task > Spatial > Embodied > Long-term > Temporal >
    Freshness（Phase 5 冻结）。

    当前 SECTION_PRIORITY 无 long_term_memory → AssertionError（预期 RED）。
    """
    from sar_orch.environment_state_provider import SECTION_PRIORITY

    assert "long_term_memory" in SECTION_PRIORITY  # RED: 当前不存在
    assert SECTION_PRIORITY.index("embodied_state") < SECTION_PRIORITY.index("long_term_memory") < SECTION_PRIORITY.index("relevant_events")


def test_budget_zero_keeps_freshness_and_truncation_marker(
    ingestor, store, scope_factory
):
    """token_budget=0 时只保留 scope/freshness + TRUNCATED 标记；长期段（未来）
    同样被完全裁剪且显式标记（不能静默泄漏或造成 rollback 失效）。

    GREEN 守护：现有 TRUNCATED 语义（environment_state_provider.py:328-338）
    在 P5 扩展后必须保留。
    """
    from Agent.environment_state import TRUNCATED_KEY

    scope_id = _seed_memory(ingestor, scope_factory)
    provider = _provider(store, None, scope_id)
    view = provider.query_environment_state(
        EnvironmentStateQuery(scope_id=scope_id, token_budget=0)
    )
    assert view.sections[TRUNCATED_KEY] is True
    assert view.sections["spatial_state"] == {}
    assert view.sections["embodied_state"] == {}
    assert view.sections["relevant_events"] == []
    assert "long_term_memory" not in view.sections
    assert view.sections["freshness"]["memory_revision"] >= 0


def test_long_term_section_heading_registered_for_renderer():
    """渲染器必须注册 ``long_term_memory`` section 标题（P5 在
    _SECTION_HEADINGS 增加，environment_state.py:123-129）。

    当前无 → AssertionError（预期 RED）。
    """
    from Agent.environment_state import _SECTION_HEADINGS

    assert "long_term_memory" in _SECTION_HEADINGS  # RED: 当前不存在


def test_render_long_term_section_after_phase5(ingestor, store, scope_factory):
    """Phase 5 后 render_environment_state_view 必须能渲染长期段（published-only
    摘要）；当前渲染器不认识该段 → 断言渲染文本包含长期段标题失败
    （AssertionError，预期 RED）。

    注：本测试在当前代码上以 AssertionError 失败（段标题不存在），
    不依赖未来模块 import。
    """
    from Agent.environment_state import (
        EnvironmentStateView,
        Freshness,
        render_environment_state_view,
    )

    view = EnvironmentStateView(
        Freshness.FRESH,
        source_revision=1,
        sections={
            "spatial_state": {},
            "embodied_state": {},
            "relevant_events": [],
            "task_execution_state": [],
            "long_term_memory": {
                "k1": {
                    "kind": "lesson",
                    "statement": "coordinate at fires",
                    "confidence": 0.9,
                }
            },
            "freshness": {"scope_id": "scope", "memory_revision": 1, "view_revision": 1, "conflicts": []},
        },
    )
    rendered = render_environment_state_view(view)
    assert "### Long-term Memory" in rendered  # RED: 当前无此标题
