"""Phase 0（P0）契约卡：System Health 注入段渲染契约（主方案 §3.3 / D5 / A2）。

目标契约：
- 新 section ``system_health``，渲染标题 ``### System Health``
  （_SECTION_HEADINGS，environment_state.py:123-133）；
- 预算档：SECTION_PRIORITY 插在 embodied 与 long_term 之间
  （Task > Spatial > Embodied > **System Health** > Long-term > Temporal）；
  threshold=3（与 long_term 同档，_SECTION_BUDGET_THRESHOLD）；
- 渲染一行一诊断：``target: finding → suggestion (confidence=N)``；
- 注入门控 = ``_is_system and long_term_mode == "read" and inject_enabled``
  （A2：``[diagnosis] inject_enabled`` 独立旋钮，默认 true，供消融实验
  单独关掉诊断注入）。

当前代码事实（32bfe57）：
- ``_SECTION_HEADINGS`` 无 ``system_health`` → AssertionError（预期 RED）；
- ``SECTION_PRIORITY`` 无 ``system_health`` → AssertionError（预期 RED）；
- ``_SECTION_BUDGET_THRESHOLD`` 无 ``system_health`` → AssertionError（预期 RED）；
- ``EnvironmentStateProvider`` ctor 无 ``diagnosis_inject_enabled`` 参数 /
  ``diagnosis_inject_enabled`` 属性 → TypeError / AttributeError（预期 RED）；
- ``MemoryReadPort`` 无 ``diagnoses()`` → AttributeError（预期 RED）；
- worker ACL / 既有预算档已存在 → worker 无诊断段守护 GREEN。

P4 实现后转 GREEN。诊断段 payload 形状（{target: {finding, suggestion,
confidence}}）与读侧方法名（diagnoses）待 P4 确认。
"""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from Agent.environment_state import (
    _SECTION_HEADINGS,
    EnvironmentStateQuery,
    EnvironmentStateView,
    Freshness,
    render_environment_state_view,
)
from sar_orch.environment_state_provider import (
    _SECTION_BUDGET_THRESHOLD,
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


class FakeRuntime:
    """Control-plane double（与 test_long_term_environment_state.py 同构）。"""

    def __init__(self):
        self._dispatches = {}

    @property
    def dispatches(self):
        return self._dispatches

    def control_revision_of(self, dispatch_id):
        return 0


def _provider(store, scope_id, *, viewer_role="worker", viewer_id="Alice", **kwargs):
    return EnvironmentStateProvider(
        MemoryReadPort(store, scope_id),
        ControlPlaneReadPort(FakeRuntime()),
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        **kwargs,
    )


def _query(scope_id, *, viewer_role="worker", viewer_id="Alice", budget=100000):
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        token_budget=budget,
    )


# ---------------------------------------------------------------------------
# GREEN 守护 —— 既有预算档顺序 / worker ACL（P4 不得破坏）
# ---------------------------------------------------------------------------


def test_existing_priority_order_preserved_after_system_health_insert():
    """GREEN 守护（§3.3 / D5）：P4 在 embodied 与 long_term 之间插入
    system_health 时，既有相对顺序不得破坏。

    本守护用 index 相对序断言（不用 SECTION_PRIORITY[:N] 字面冻结——
    P4 插档不会打破相对序，字面冻结会）。
    """
    assert SECTION_PRIORITY.index("task_execution_state") < SECTION_PRIORITY.index("spatial_state")
    assert SECTION_PRIORITY.index("spatial_state") < SECTION_PRIORITY.index("embodied_state")
    assert (
        SECTION_PRIORITY.index("embodied_state")
        < SECTION_PRIORITY.index("long_term_memory")
        < SECTION_PRIORITY.index("relevant_events")
    )
    assert _SECTION_BUDGET_THRESHOLD["long_term_memory"] == 3  # 长期段档位不得漂移


def test_worker_view_never_contains_system_health(store, scope_factory):
    """GREEN 守护（§3.3 / ACL 三道门，探索 03 §3）：worker 视图永远没有
    system_health 段（P4 若把诊断段泄漏给 worker 本测试转红）。

    现在成立（段尚不存在）；P4 注入门控 = _is_system and read and
    inject_enabled，worker 天然不满足 _is_system。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, viewer_role="worker", viewer_id="Alice")
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    assert view.freshness is Freshness.FRESH
    assert "system_health" not in view.sections
    assert "system_health" not in view.evidence


# ---------------------------------------------------------------------------
# RED —— System Health 段注册 / 预算档 / 渲染（Phase 4，现不存在）
# ---------------------------------------------------------------------------


def test_system_health_section_heading_registered():
    """§3.3：渲染器必须注册 ``system_health`` section，标题 ``### System Health``
    （P4 在 _SECTION_HEADINGS 增加，environment_state.py:123-133）。

    当前无 → AssertionError（预期 RED）。
    """
    assert "system_health" in _SECTION_HEADINGS  # RED: 当前不存在
    assert _SECTION_HEADINGS["system_health"] == "### System Health"


def test_system_health_in_priority_between_embodied_and_long_term():
    """§3.3 / D5：预算丢弃顺序钉死 Task > Spatial > Embodied > **System
    Health** > Long-term > Temporal——system_health 插在 embodied 与
    long_term 之间。

    当前 SECTION_PRIORITY 无 system_health → AssertionError（预期 RED）。
    """
    assert "system_health" in SECTION_PRIORITY  # RED: 当前不存在
    assert (
        SECTION_PRIORITY.index("embodied_state")
        < SECTION_PRIORITY.index("system_health")
        < SECTION_PRIORITY.index("long_term_memory")
    )


def test_system_health_budget_threshold_same_tier_as_long_term():
    """§3.3 / D5：system_health 与 long_term 同档（threshold=3）——固定上限
    摘要段，低预算可整体裁剪并留 TRUNCATED。

    当前 _SECTION_BUDGET_THRESHOLD 无 system_health → AssertionError
    （预期 RED）。
    """
    assert _SECTION_BUDGET_THRESHOLD.get("system_health") == 3  # RED: 当前不存在


def test_render_emits_system_health_heading_and_one_line_per_diagnosis():
    """§3.3：渲染一行一诊断 ``target: finding → suggestion (confidence=N)``，
    段标题 ``### System Health``。

    当前 _SECTION_HEADINGS 未注册 → AssertionError（预期 RED）。
    诊断段 payload 形状（{target: {finding, suggestion, confidence}}）待
    P4 确认（探索 03 §1.4：仿 _format_long_term_memory 专用 formatter）。
    """
    view = EnvironmentStateView(
        Freshness.FRESH,
        sections={
            "system_health": {
                "coordinator": {
                    "finding": "assignments overlap at fire",
                    "suggestion": "deduplicate by location",
                    "confidence": 0.6,
                },
                "worker:bob": {
                    "finding": "stale at reservoir",
                    "suggestion": "refresh telemetry",
                    "confidence": 0.8,
                },
            },
        },
    )
    rendered = render_environment_state_view(view, long_term_mode="read")
    assert "### System Health" in rendered  # RED: 当前无该标题
    assert "coordinator: assignments overlap at fire → deduplicate by location (confidence=0.6)" in rendered
    assert "worker:bob: stale at reservoir → refresh telemetry (confidence=0.8)" in rendered


# ---------------------------------------------------------------------------
# RED —— 注入门控 / 读侧 API（Phase 4，现不存在）
# ---------------------------------------------------------------------------


def test_provider_diagnosis_inject_enabled_knob_default_true(store, scope_factory):
    """§3.3 / A2：注入门控 = _is_system and long_term_mode == "read" and
    inject_enabled——``[diagnosis] inject_enabled`` 独立旋钮默认 true
    （消融实验才显式关，config 可配；显式 False 由
    test_system_health_diagnosis_validator.py 的 DiagnosisConfig 契约覆盖）。

    当前 provider 无 ``diagnosis_inject_enabled`` 属性 → AttributeError
    （预期 RED）；属性名待 P4 确认。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(
        store,
        scope_id,
        viewer_role="coordinator",
        viewer_id="system",
        long_term_mode="read",
    )
    assert provider.diagnosis_inject_enabled is True  # AttributeError（预期 RED）


def test_memory_read_port_diagnoses_method(store):
    """§3.2/§3.3：诊断读侧方法（仿 long_term_memory()，
    environment_state_provider.py:171-184）——published 诊断只读；
    store 未接线时返回 []（永不 raise）。

    当前 MemoryReadPort 无该方法 → AttributeError（预期 RED）；
    方法名待 P4 确认。
    """
    port = MemoryReadPort(store, scope_id="scope")
    assert port.diagnoses() == []  # AttributeError（预期 RED）
