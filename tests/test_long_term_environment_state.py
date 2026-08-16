"""Phase 0（P0）契约卡：长期记忆环境状态 ACL 与预算（主方案 RED contract #6）。

目标契约（主方案 Phase 5 / D5）：
- worker 视图永远没有 ``long_term_memory``；只有 coordinator read mode 才有；
- 预算不足先丢长期段：丢弃顺序钉死
  Task > Spatial > Embodied > **Long-term** > Temporal > Freshness；
  长期段为固定上限摘要，低预算时可被完全裁剪并留 TRUNCATED，不可突破
  token limit；
- ``long_term_mode=shadow`` 时不渲染长期段（避免污染 shadow compare）；
- read 失败依旧触发现有 read-port rollback 语义（不得混合 legacy/canonical 内容）。

当前代码事实（5413705）：
- ``MemoryReadPort`` 无长期记忆方法（environment_state_provider.py:68-147）→
  AttributeError（预期 RED）；
- ``_SECTION_HEADINGS``（Agent/environment_state.py:123-129）无长期段 →
  AssertionError（预期 RED）；
- ``SECTION_PRIORITY``（environment_state_provider.py:42-47）无长期段 →
  AssertionError（预期 RED）；
- worker ACL 与 budget 机制已存在 → worker 无长期段 / read failure 守护 GREEN。

Phase 5 实现后转 GREEN。
"""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from Agent.environment_state import (
    EnvironmentStateQuery,
    Freshness,
)
from sar_orch.environment_state_provider import (
    SECTION_PRIORITY,
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


def _scope_id_of(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


class FakeRuntime:
    """Control-plane double（与 test_environment_state_acl.py 同构）。"""

    def __init__(self):
        self._dispatches = {}

    @property
    def dispatches(self):
        return self._dispatches

    def control_revision_of(self, dispatch_id):
        return 0


def _provider(store, scope_id, *, viewer_role="worker", viewer_id="Alice"):
    return EnvironmentStateProvider(
        MemoryReadPort(store, scope_id),
        ControlPlaneReadPort(FakeRuntime()),
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
    )


def _query(scope_id, *, viewer_role="worker", viewer_id="Alice", budget=100000):
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        token_budget=budget,
    )


# ---------------------------------------------------------------------------
# GREEN 守护 —— worker 视图无长期段 / read failure 语义（P5 不得破坏）
# ---------------------------------------------------------------------------


def test_worker_view_never_contains_long_term_memory(store, scope_factory):
    """worker viewer 的 fresh view sections 永远没有 ``long_term_memory``。

    GREEN 守护（现在成立；Phase 5 若把长期段泄漏给 worker 本测试转红）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="worker", viewer_id="Alice")
    view = provider.query_environment_state(_query(scope_id, viewer_role="worker", viewer_id="Alice"))
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" not in view.sections
    assert "long_term_memory" not in view.evidence


def test_worker_acl_self_embodied_only(ingestor, store, scope_factory):
    """worker 只看到自己的 embodied 段（现有 ACL）；长期段同样不得泄漏。

    GREEN 守护：P5 扩展 ACL 时 worker 分支必须保持“只看自己”。
    """
    from a2a.coordinator.memory.contracts import NormalizedProjectionInputV1

    scope_id = _scope_id_of(scope_factory)
    for entity, value in (("Alice", [3, 4, 0]), ("Bob", [9, 9, 0])):
        ingestor.ingest_projection(
            [
                NormalizedProjectionInputV1(
                    scope_id=scope_id,
                    event_id=f"evt_{entity}",
                    sequence=0,
                    env_step=8,
                    actor_id=entity,
                    provenance="worker_sensor_tool",
                    domain="embodied",
                    entity_id=entity,
                    entity_type="agent",
                    field_name="position",
                    value=value,
                )
            ]
        )
    provider = _provider(store, scope_id, viewer_role="worker", viewer_id="Alice")
    view = provider.query_environment_state(_query(scope_id, viewer_role="worker", viewer_id="Alice"))
    embodied = view.sections["embodied_state"]
    assert set(embodied.keys()) == {"Alice"}
    assert "long_term_memory" not in view.sections


def test_read_failure_never_mixes_long_term_with_legacy_canonical(store, scope_factory):
    """read failure 依旧产生 STALE/UNAVAILABLE，不混合 legacy/canonical 内容；
    失败视图同样不含长期段（P5 不得破坏 rollback 闩锁语义）。

    GREEN 守护。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="worker", viewer_id="Alice")
    # scope 不匹配 → UNAVAILABLE（principal 拒绝路径）
    bad = provider.query_environment_state(
        EnvironmentStateQuery(
            scope_id="other-scope",
            viewer_role="worker",
            viewer_id="Alice",
            token_budget=100000,
        )
    )
    assert bad.freshness is Freshness.UNAVAILABLE
    assert "long_term_memory" not in bad.sections


def test_existing_budget_priority_order_task_spatial_embodied_temporal():
    """丢弃顺序 Task > Spatial > Embodied > Long-term > Temporal（P5 冻结）。

    GREEN 守护（P5 更新）：P5 把 Long-term 插入 Embodied 之后、Temporal
    之前（与 RED 契约 index(embodied) < index(long_term) <
    index(relevant_events) 一致）；本断言原为 P5 前的 [:4] 字面钉死，
    与 P5 契约互斥，随 Phase 5 更新为含 Long-term 的冻结前缀。
    Phase 4（P4，主方案 §3.3）再更新一次：System Health 插在 Embodied 与
    Long-term 之间（冻结顺序 Task > Spatial > Embodied > System Health >
    Long-term > Temporal），冻结前缀随之扩为 5 元。
    """
    assert SECTION_PRIORITY[:5] == (
        "task_execution_state",
        "spatial_state",
        "embodied_state",
        "system_health",
        "long_term_memory",
    )


# ---------------------------------------------------------------------------
# RED —— 长期段读侧 API（Phase 5，现不存在）
# ---------------------------------------------------------------------------


def test_memory_read_port_long_term_memory_method(store):
    """``MemoryReadPort`` 必须提供只读长期记忆访问（published-only，
    coordinator read mode 使用）。当前无此方法 → AttributeError（预期 RED）。
    """
    port = MemoryReadPort(store, scope_id="scope")
    entries = port.long_term_memory()  # AttributeError
    assert isinstance(entries, list)


def test_long_term_section_heading_registered():
    """渲染器必须注册 ``long_term_memory`` section 标题（P5 在
    _SECTION_HEADINGS 增加）。当前无 → AssertionError（预期 RED）。
    """
    from Agent.environment_state import _SECTION_HEADINGS

    assert "long_term_memory" in _SECTION_HEADINGS  # RED: 当前不存在


def test_budget_priority_includes_long_term_after_embodied_before_temporal():
    """预算丢弃顺序必须钉死：Task > Spatial > Embodied > Long-term > Temporal >
    Freshness（Phase 5 冻结）。当前 SECTION_PRIORITY 无长期段 →
    AssertionError（预期 RED）。
    """
    assert "long_term_memory" in SECTION_PRIORITY  # RED: 当前不存在
    assert SECTION_PRIORITY.index("embodied_state") < SECTION_PRIORITY.index("long_term_memory") < SECTION_PRIORITY.index("relevant_events")


def test_high_budget_view_keeps_long_term_section(store, scope_factory):
    """coordinator read mode、预算充足时，fresh view 必须包含 published-only
    的 ``long_term_memory`` 段。当前 provider 永不产生该段 → AssertionError
    （预期 RED）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="coordinator", viewer_id="system")
    view = provider.query_environment_state(_query(scope_id, viewer_role="coordinator", viewer_id="system", budget=100000))
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" in view.sections  # RED: 当前无长期段


def test_low_budget_drops_long_term_before_temporal(store, scope_factory):
    """预算不足时长期段先于 Temporal 被丢弃（丢弃顺序 Long-term > Temporal），
    且保留 TRUNCATED 标记、不突破 token limit。

    当前 _apply_budget 无法承载长期段（注入后直接丢失）→ AssertionError
    （预期 RED）。
    """
    from Agent.environment_state import TRUNCATED_KEY

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="coordinator", viewer_id="system")
    # 模拟 Phase 5 之后 provider 注入的长期段
    view = provider.query_environment_state(_query(scope_id, viewer_role="coordinator", viewer_id="system", budget=1))
    # 低预算：长期段被裁剪但必须显式标记（不能静默消失）
    assert view.sections.get(TRUNCATED_KEY) is True
    assert "long_term_memory" not in view.sections


def test_shadow_mode_never_renders_long_term_section(store, scope_factory):
    """``long_term_mode=shadow`` 时不渲染长期段、``read`` 才渲染（Phase 5 / D5）。

    当前渲染函数无 ``long_term_mode`` 参数（且无长期段标题）→ TypeError
    （预期 RED：未来 API 缺失；P5 增加 mode 旋钮后本测试转 GREEN）。
    """
    from sar_orch.environment_state_provider import render_environment_state_view

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="coordinator", viewer_id="system")
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system", budget=100000)
    )
    # Phase 5 契约（主方案 Phase 5 #5）：渲染函数接受 long_term_mode 旋钮
    # （off|shadow|read，D4/D5）；shadow 不渲染长期段（避免污染 shadow
    # compare）、read 才渲染。
    # TODO(P5): 长期段标题文本待与 Agent/environment_state._SECTION_HEADINGS
    # 风格确认（P0 拟定 "Long-term Memory"，参考文件冲突 #2 已知项）。
    shadow_rendered = render_environment_state_view(
        view, long_term_mode="shadow"
    )  # TypeError（预期 RED：未来 API 缺失）
    assert "Long-term Memory" not in shadow_rendered
    read_rendered = render_environment_state_view(view, long_term_mode="read")
    assert "Long-term Memory" in read_rendered  # RED: 当前无长期段标题
