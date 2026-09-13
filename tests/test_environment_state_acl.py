"""Phase 4 — EnvironmentStateProvider worker ACL.

The provider applies ACL inside the provider (never in the renderer): a worker
viewer sees only its own Embodied fields (position / inventory), a safe team
summary for teammates, its own dispatch task views, and authorized temporal
delta.  A coordinator / system viewer sees the full projection.
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
from Agent.environment_state import EnvironmentStateQuery, Freshness
from sar_orch.environment_state_provider import (
    ControlPlaneReadPort,
    EnvironmentStateProvider,
    MemoryReadPort,
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
    field_name,
    value: Any,
    env_step=8,
    actor_id="alice",
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
        entity_type="agent" if domain == "embodied" else "fire",
        field_name=field_name,
        value=value,
    )


class FakeRuntime:
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


def _seed(ingestor, scope_factory) -> tuple[str, FakeRuntime]:
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_a_pos",
                domain="embodied",
                entity_id="Alice",
                field_name="position",
                value=[3, 4, 0],
                actor_id="alice",
            ),
            _input(
                scope_id,
                event_id="evt_a_inv",
                domain="embodied",
                entity_id="Alice",
                field_name="inventory",
                value=["Water"],
                actor_id="alice",
            ),
            _input(
                scope_id,
                event_id="evt_b_pos",
                domain="embodied",
                entity_id="Bob",
                field_name="position",
                value=[9, 9, 0],
                actor_id="bob",
            ),
            _input(
                scope_id,
                event_id="evt_b_inv",
                domain="embodied",
                entity_id="Bob",
                field_name="inventory",
                value=["Sand"],
                actor_id="bob",
            ),
            _input(
                scope_id,
                event_id="evt_fire",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                actor_id="alice",
            ),
        ]
    )
    runtime = FakeRuntime(
        [
            PhysicalDispatch(
                dispatch_id="d_alice",
                context_id="ctx-1",
                logical_node_id="n1",
                worker_id="Alice",
                worker_task_id="wt-a",
                state=PhysicalState.RUNNING,
            ),
            PhysicalDispatch(
                dispatch_id="d_bob",
                context_id="ctx-1",
                logical_node_id="n2",
                worker_id="Bob",
                worker_task_id="wt-b",
                state=PhysicalState.COMPLETED,
            ),
        ]
    )
    return scope_id, runtime


def _provider(store, runtime, scope_id, *, viewer_role, viewer_id):
    return EnvironmentStateProvider(
        MemoryReadPort(store, scope_id),
        ControlPlaneReadPort(runtime),
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
    )


def _query(scope_id, *, viewer_role, viewer_id):
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        temporal_cursor=0,
        token_budget=1000,
    )


def test_worker_cannot_read_other_worker_inventory_or_position(
    ingestor, store, scope_factory
):
    scope_id, runtime = _seed(ingestor, scope_factory)
    provider = _provider(
        store, runtime, scope_id, viewer_role="worker", viewer_id="Alice"
    )

    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    assert view.freshness == Freshness.FRESH

    embodied = view.sections["embodied_state"]
    # Alice sees her own full Embodied state.
    assert "Alice" in embodied
    assert embodied["Alice"]["fields"]["position"]["value"] == [3, 4, 0]
    assert embodied["Alice"]["fields"]["inventory"]["value"] == ["Water"]
    # Alice does NOT see Bob's inventory/position.
    assert "Bob" not in embodied


def test_worker_cannot_see_other_workers_dispatch_task_state(
    ingestor, store, scope_factory
):
    scope_id, runtime = _seed(ingestor, scope_factory)
    provider = _provider(
        store, runtime, scope_id, viewer_role="worker", viewer_id="Alice"
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    tasks = view.sections["task_execution_state"]
    dispatch_ids = [t["dispatch_id"] for t in tasks]
    assert "d_alice" in dispatch_ids
    assert "d_bob" not in dispatch_ids


def test_coordinator_system_principal_sees_all(ingestor, store, scope_factory):
    scope_id, runtime = _seed(ingestor, scope_factory)
    provider = _provider(
        store, runtime, scope_id, viewer_role="coordinator", viewer_id="system"
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    embodied = view.sections["embodied_state"]
    assert "Alice" in embodied and "Bob" in embodied
    tasks = [t["dispatch_id"] for t in view.sections["task_execution_state"]]
    assert "d_alice" in tasks and "d_bob" in tasks


def test_viewer_cannot_read_another_scope(ingestor, store, scope_factory):
    scope_id, _ = _seed(ingestor, scope_factory)
    provider = _provider(
        store, None, scope_id, viewer_role="coordinator", viewer_id="system"
    )
    view = provider.query_environment_state(
        EnvironmentStateQuery(
            scope_id="some-other-scope",
            viewer_role="coordinator",
            viewer_id="system",
        )
    )
    # Cross-scope query is not silently served with the provider's own scope.
    assert view.freshness == Freshness.UNAVAILABLE


def test_renderer_does_not_perform_acl_filtering(ingestor, store, scope_factory):
    """The provider returns an already-filtered view; renderer stays pure.

    The ACL decision (which Embodied nodes are visible) must happen inside the
    provider.  The pure renderer only formats whatever sections it receives.
    """
    from sar_orch.environment_state_provider import render_environment_state_view

    scope_id, runtime = _seed(ingestor, scope_factory)
    worker_provider = _provider(
        store, runtime, scope_id, viewer_role="worker", viewer_id="Alice"
    )
    worker_view = worker_provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    rendered = render_environment_state_view(worker_view)
    # Renderer output must not leak Bob's Embodied facts.
    assert "9, 9, 0" not in rendered
    assert "Sand" not in rendered
    assert "Alice" in rendered or "[3, 4, 0]" in rendered


# ---------------------------------------------------------------------------
# Phase 0（P0）增补 —— 长期记忆 ACL（主方案 RED contract #6 / D5）
# 只追加；不改动既有测试与 fixture。
# ---------------------------------------------------------------------------


def test_worker_view_never_exposes_long_term_memory(ingestor, store, scope_factory):
    """worker 视图永远没有 ``long_term_memory``；worker 只看到自己的 embodied
    段（现有 ACL）。

    GREEN 守护（现在成立；Phase 5 若泄漏给 worker 本测试转红）。
    """
    scope_id, _ = _seed(ingestor, scope_factory)
    provider = _provider(store, None, scope_id, viewer_role="worker", viewer_id="Alice")
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    assert view.freshness == Freshness.FRESH
    assert "long_term_memory" not in view.sections
    assert set(view.sections["embodied_state"].keys()) == {"Alice"}


def test_coordinator_read_mode_view_must_include_long_term_memory(
    ingestor, store, scope_factory
):
    """coordinator（system principal）read mode 的 fresh view 必须包含
    published-only 的 ``long_term_memory`` 段；当前永不产生 → AssertionError
    （预期 RED，Phase 5 实现）。

    不能静默泄漏：worker 分支保持无长期段（上一测试），coordinator 才有。
    """
    scope_id, runtime = _seed(ingestor, scope_factory)
    provider = _provider(
        store, runtime, scope_id, viewer_role="coordinator", viewer_id="system"
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    assert view.freshness == Freshness.FRESH
    assert "long_term_memory" in view.sections  # RED: 当前无长期段


def test_long_term_budget_drop_never_breaks_rollback_latch(
    ingestor, store, scope_factory
):
    """预算裁剪长期段不得静默泄漏或造成 rollback 失效：预算耗尽视图仍保持
    FRESH + TRUNCATED 标记，freshness 元数据完整（read failure 才会触发
    回滚闩锁）。

    GREEN 守护：只冻结 budget=0 的既有语义（``token_budget <= 0`` 标
    TRUNCATED，environment_state_provider.py:331）。budget=1 的行为不冻结
    —— P5 加入长期段后低预算（budget=1）也可能裁剪并标 TRUNCATED
    （见 test_long_term_environment_state.py 的 RED 契约）。
    """
    from Agent.environment_state import TRUNCATED_KEY

    scope_id, _ = _seed(ingestor, scope_factory)
    provider = _provider(
        store, None, scope_id, viewer_role="coordinator", viewer_id="system"
    )
    view = provider.query_environment_state(
        EnvironmentStateQuery(
            scope_id=scope_id,
            viewer_role="coordinator",
            viewer_id="system",
            token_budget=0,
        )
    )
    assert view.freshness == Freshness.FRESH
    # budget=0 → 预算耗尽必须显式标 TRUNCATED（当前 token_budget <= 0 语义；
    # P5 扩展后低预算裁剪同样必须显式标记，不能静默消失）
    assert view.sections.get(TRUNCATED_KEY) is True
    assert "long_term_memory" not in view.sections
    assert view.sections["freshness"]["memory_revision"] is not None
