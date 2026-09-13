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
    TRUNCATED_KEY,
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


# ---------------------------------------------------------------------------
# R3 修订 —— system_health 预算档配置化（diagnosis_budget_threshold）
# ---------------------------------------------------------------------------


class _FakeDiagnosis:
    """最小诊断读侧 payload（target/finding/suggestion/confidence，仿
    DiagnosisCandidateV1 投影；provider 的 _system_health_section 只读这四个
    属性）。"""

    def __init__(self, target, finding, suggestion, confidence):
        self.target = target
        self.finding = finding
        self.suggestion = suggestion
        self.confidence = confidence


class _FakeDiagnosisStore:
    """最小诊断 store double（diagnoses(scope_id) -> list，仿
    DiagnosisMemoryStore 读侧）。"""

    def __init__(self, diagnoses):
        self._diagnoses = list(diagnoses)

    def diagnoses(self, scope_id):
        return list(self._diagnoses)


def _provider_with_diagnoses(store, scope_id, diagnoses, **provider_kwargs):
    """coordinator/system 视图 + 已接线诊断 store（只接 MemoryReadPort 读面，
    仿 coordinator_state_provider.py 的 P4 组装——provider 本身不持有
    store，review Minor 1 移除死参数）。"""
    fake_store = _FakeDiagnosisStore(diagnoses)
    return EnvironmentStateProvider(
        MemoryReadPort(store, scope_id, diagnosis_store=fake_store),
        ControlPlaneReadPort(FakeRuntime()),
        scope_id=scope_id,
        viewer_role="coordinator",
        viewer_id="system",
        long_term_mode="read",
        **provider_kwargs,
    )


def test_system_health_kept_when_budget_threshold_lowered(store, scope_factory):
    """R3 修订: ``diagnosis_budget_threshold=2`` → 低预算也保留段——
    ``token_budget=2``（默认档 3 会裁掉 system_health）段仍在。

    注意 TRUNCATED 语义：read 模式的 coordinator 视图恒注入 long_term_memory
    段（P5，阈值 3），所以 budget=2 时该段被裁 → 视图 TRUNCATED=True（与
    test_long_term_read_port.py::test_budget_below_long_term_threshold_drops_
    section_and_truncates 一致）——这与 system_health 无关；隔离验证见下方
    budget=3（long-term 档位边界）：两段都保留 → TRUNCATED 不为 True。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [_FakeDiagnosis("coordinator", "assignments overlap", "deduplicate", 0.8)],
        diagnosis_budget_threshold=2,
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system", budget=2)
    )
    assert "system_health" in view.sections  # R3: 调小阈值 → budget=2 也保留段
    # budget=3（long-term 档位边界）：system_health 不再贡献 TRUNCATED。
    view3 = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system", budget=3)
    )
    assert "system_health" in view3.sections
    assert view3.sections.get(TRUNCATED_KEY) is not True


def test_system_health_dropped_when_budget_threshold_raised(store, scope_factory):
    """R3 修订: ``diagnosis_budget_threshold=4`` + ``token_budget=3`` →
    system_health 段被裁（默认档 3 会保留它）、TRUNCATED is True。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [_FakeDiagnosis("coordinator", "assignments overlap", "deduplicate", 0.8)],
        diagnosis_budget_threshold=4,
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system", budget=3)
    )
    assert view.sections.get(TRUNCATED_KEY) is True
    assert "system_health" not in view.sections


def test_system_health_default_threshold_keeps_legacy_behavior(store, scope_factory):
    """R3 修订守护: 不传 ``diagnosis_budget_threshold``（默认 3）+
    ``token_budget=2`` → system_health 被裁 + TRUNCATED（与 P0 默认行为一致，
    配置面默认值不得改变既有行为）。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [_FakeDiagnosis("coordinator", "assignments overlap", "deduplicate", 0.8)],
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system", budget=2)
    )
    assert view.sections.get(TRUNCATED_KEY) is True
    assert "system_health" not in view.sections


# ---------------------------------------------------------------------------
# 2026-08-16 review 修复 —— M-1 同 target 最高置信度 / M-2 读失败降级 /
# Minor 3 threshold 校验 / Minor 5 默认值派生
# ---------------------------------------------------------------------------


def test_same_target_diagnoses_keep_highest_confidence_low_first(store, scope_factory):
    """M-1: 同 target 多条诊断只保留最高置信度那条——低置信度先写、高置信度
    后写 → 保留高（旧行为后写覆盖，静默坍缩）。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [
            _FakeDiagnosis("coordinator", "low finding", "low suggestion", 0.6),
            _FakeDiagnosis("coordinator", "high finding", "high suggestion", 0.9),
        ],
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    section = view.sections["system_health"]
    assert set(section) == {"coordinator"}  # 同 target 坍缩为一条
    assert section["coordinator"] == {
        "finding": "high finding",
        "suggestion": "high suggestion",
        "confidence": 0.9,
    }


def test_same_target_diagnoses_keep_highest_confidence_high_first(store, scope_factory):
    """M-1: 高置信度先写、低置信度后写 → 低置信度不得覆盖保留的高置信度条目。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [
            _FakeDiagnosis("coordinator", "high finding", "high suggestion", 0.9),
            _FakeDiagnosis("coordinator", "low finding", "low suggestion", 0.6),
        ],
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    section = view.sections["system_health"]
    assert set(section) == {"coordinator"}
    assert section["coordinator"] == {
        "finding": "high finding",
        "suggestion": "high suggestion",
        "confidence": 0.9,
    }


def test_same_target_rendering_only_shows_kept_diagnosis(store, scope_factory):
    """M-1: 渲染只出现保留的那条（最高置信度），被覆盖的低置信度行不出现。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider_with_diagnoses(
        store,
        scope_id,
        [
            _FakeDiagnosis("coordinator", "assignments overlap", "deduplicate", 0.6),
            _FakeDiagnosis("coordinator", "fire coverage gap", "add water post", 0.95),
        ],
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    rendered = render_environment_state_view(view, long_term_mode="read")
    assert "### System Health" in rendered
    assert "coordinator: fire coverage gap → add water post (confidence=0.95)" in rendered
    assert "assignments overlap" not in rendered
    assert rendered.count("coordinator:") == 1


def test_diagnosis_store_read_failure_never_stales_view(store, scope_factory):
    """M-2: 已接线诊断 store 运行中读取 raise → diagnoses() 返回 []（与未
    接线同语义），视图 FRESH、无 system_health 段、无 TRUNCATED 副作用
    （budget 足够时）——诊断通道故障绝不拖垮视图。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id

    class _RaisingDiagnosisStore(_FakeDiagnosisStore):
        def diagnoses(self, scope_id):
            raise RuntimeError("diagnosis store read failure")

    provider = EnvironmentStateProvider(
        MemoryReadPort(store, scope_id, diagnosis_store=_RaisingDiagnosisStore([])),
        ControlPlaneReadPort(FakeRuntime()),
        scope_id=scope_id,
        viewer_role="coordinator",
        viewer_id="system",
        long_term_mode="read",
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="coordinator", viewer_id="system")
    )
    assert view.freshness is Freshness.FRESH
    assert "system_health" not in view.sections
    assert view.sections.get(TRUNCATED_KEY) is not True
    # 读面本身也遵守「永不 raise」契约
    assert provider._memory_read_port.diagnoses() == []


def test_diagnosis_budget_threshold_invalid_values_rejected(store, scope_factory):
    """Minor 3: provider 层 diagnosis_budget_threshold 防御校验——非 int /
    bool / <= 0 一律 ValueError（与 config 层 fail-closed 一致，不静默降级）。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    for bad in ("3", 0, -1, True):
        with pytest.raises(ValueError):
            _provider(store, scope_id, diagnosis_budget_threshold=bad)


def test_diagnosis_budget_threshold_default_derived_from_module_constant(
    store, scope_factory
):
    """Minor 5: 默认档派生自 _SECTION_BUDGET_THRESHOLD["system_health"]——
    单一事实源，双源硬编码分叉已消除（P0 守护仍断言常量 == 3）。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id)
    assert (
        provider._diagnosis_budget_threshold
        == _SECTION_BUDGET_THRESHOLD["system_health"]
    )
