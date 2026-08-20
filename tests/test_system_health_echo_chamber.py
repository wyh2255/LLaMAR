"""Phase 0（P0）契约卡：防回声室与权威性守护（主方案 §3.2 / R2 / R6 / B6 / B9）。

目标契约（三重防护 + 结构性隔离，2026-08-13 审查强化）：
- ① 诊断 source_ref 禁指向既有诊断（validator 拒绝，见
  test_system_health_diagnosis_validator.py::test_echo_chamber_source_ref_rejected）；
- ② 诊断 audit 事件用独立顶层前缀 ``diagnosis.audit``——不在
  ``_REFLECTION_EVENT_PREFIXES`` 四前缀内，反思收集器天然不收（R6：
  诊断永不进反思输入窗口）；
- ③ 时间线只读工具对诊断 audit 事件视图过滤（见
  test_system_health_read_tools.py::test_query_temporal_flow_tool_filters_diagnosis_audit_events）；
- B6 结构性隔离：诊断不进投影事实域——FIELD_SOURCE_POLICY / allowlist 均
  不扩展；
- B9 / §3.1（2026-08-13 修订）：allowlist 不动——coordinator_decision 与
  diagnosis 均不进 ONLINE_PROVENANCE_ALLOWLIST（决策事件与 diagnosis.audit
  是协调层自身行为记录，provenance gate 只作用于 ingest_projection 的
  投影输入，store.py:695 的 append_temporal_event 无 provenance 参数）。

本文件全部为 GREEN 守护（现在成立；P1/P2 若破坏任一锁即转红），唯一 RED
为未来模块的诊断 audit 事件类型常量。注意：守护断言用「语义」而非冻结
集合字面——P1 会给收集器新增 ``coordinator_decision.`` 前缀，本文件不
冻结 ``_REFLECTION_EVENT_PREFIXES`` 的完整元组。
"""

from __future__ import annotations

from a2a.coordinator.memory.contracts import (
    FIELD_SOURCE_POLICY,
    ONLINE_PROVENANCE_ALLOWLIST,
)
from a2a.coordinator.memory.reflection import (
    _REFLECTION_EVENT_PREFIXES,
    ReflectionSourceCollector,
)


def test_reflection_prefixes_keep_four_families_and_never_diagnosis():
    """R6（reflection.py:431）：反思收集器四前缀（control./callback./
    evidence./supervision.）必须始终在场；``diagnosis.`` 永不进入（独立顶层
    前缀防回声室）。

    GREEN 守护：语义断言（四前缀子集 + diagnosis 不在其中），不冻结完整
    元组字面——P1 新增 ``coordinator_decision.`` 前缀不得破坏本测试。
    """
    assert {"control.", "callback.", "evidence.", "supervision."} <= set(
        _REFLECTION_EVENT_PREFIXES
    )
    assert "diagnosis." not in _REFLECTION_EVENT_PREFIXES


def test_collector_never_accepts_diagnosis_audit_event_type():
    """R6：``ReflectionSourceCollector.accepts_event_type`` 对
    ``diagnosis.audit``（及裸 ``diagnosis``）恒返回 False——诊断 audit 事件
    永不进反思输入窗口（reflection.py:473-477 按前缀过滤）。

    GREEN 守护：现在成立；P1/P2 不得让诊断事件混入收集器。
    """
    collector = ReflectionSourceCollector()
    assert collector.accepts_event_type("diagnosis.audit") is False
    assert collector.accepts_event_type("diagnosis") is False


def test_collector_accepts_four_online_evidence_families():
    """反思收集器对四类在线证据家族保持放行（main plan §3.4.2）。

    GREEN 守护：现在成立；P1 扩展前缀时四家族不得被挤掉。
    """
    collector = ReflectionSourceCollector()
    assert collector.accepts_event_type("control.dispatch.RUNNING")
    assert collector.accepts_event_type("callback.status_update")
    assert collector.accepts_event_type("evidence.projection")
    assert collector.accepts_event_type("supervision.WORKER_UNREACHABLE")


def test_allowlist_never_extended_for_decision_or_diagnosis():
    """B9 / §3.1（2026-08-13 审查修订）：allowlist 不动——coordinator_decision
    与 diagnosis 均不进 ONLINE_PROVENANCE_ALLOWLIST（探索 01 §3：白名单
    7 成员冻结；provenance gate 只作用于投影输入，ingestor.py:619-624）。

    GREEN 守护：P1/P2 若给白名单加成员（过度设计，已撤销）本测试转红。
    """
    assert "coordinator_decision" not in ONLINE_PROVENANCE_ALLOWLIST
    assert "diagnosis" not in ONLINE_PROVENANCE_ALLOWLIST


def test_diagnosis_never_enters_projection_field_source_policy():
    """B6 结构性隔离：诊断不进投影事实域——FIELD_SOURCE_POLICY 任何字段族的
    来源元组都不得出现 diagnosis（contracts.py:173-209；探索 03 §4：
    诊断走独立 store，投影 reducer 与 FIELD_SOURCE_POLICY 完全无关）。

    GREEN 守护：现在成立；后续 Phase 若把诊断接进投影域本测试转红。
    """
    for field, sources in FIELD_SOURCE_POLICY.items():
        assert "diagnosis" not in sources, f"{field}: {sources}"
    assert "diagnosis" not in ONLINE_PROVENANCE_ALLOWLIST


def test_diagnosis_audit_event_type_constant():
    """D9（2026-08-13 审查修订）：诊断 audit 事件类型 = 独立顶层前缀
    ``diagnosis.audit``（原 evidence.diagnosis_audit 命名撤销——evidence.*
    会被收集器回收形成回声）。

    当前 a2a.coordinator.memory.diagnosis 不存在 → ImportError（预期 RED）；
    API 路径待 P2 确认。
    """
    from a2a.coordinator.memory.diagnosis import DIAGNOSIS_AUDIT_EVENT_TYPE

    assert DIAGNOSIS_AUDIT_EVENT_TYPE == "diagnosis.audit"
    assert not DIAGNOSIS_AUDIT_EVENT_TYPE.startswith(("evidence.", "control.", "callback.", "supervision."))
