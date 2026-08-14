"""Phase 0（P0）契约卡：agentic 审查者四件只读工具 schema 契约（主方案 §5 / B8 / R6）。

目标契约（四件只读查询工具，数据读取层已存在——探索 02 §3.2）：
- 投影查询：读 MemoryReadPort 投影（environment_state_provider.py:109-137），
  输入 domain ∈ {spatial, embodied}（可选 entity_id），输出
  entity_id → {entity_type, fields: {field_name: {value, env_step, ...}}}；
- temporal 流水查询：读 store.temporal_events（store.py:834-841），输入
  after_sequence，输出 sequence 升序事件列表；**对 diagnosis.audit 事件做
  视图过滤**（R6：诊断 audit 仅供评测/审计，不进审查者输入）；
- supervision 计数：读 store.supervision_event_count（store.py:952-961），
  输出计数；
- control journal 读：读 store.control_journal_entries（store.py:518-528），
  输入可选 dispatch_id 等，输出 journal 条目列表；
- 全部工具：只读（执行零写入）、schema 不暴露 barrier/oracle/truth 读取
  参数（B8 / H1 精神）。

当前代码事实（32bfe57）：
- sar_orch/tools/coordinator/ 下无 query_projection / query_temporal_flow /
  query_supervision / query_control_journal 模块 → ImportError（预期 RED）；
- MemoryStore / MemoryReadPort 读 API 已存在 → 种子数据用 GREEN API 直写。

P3 实现后转 GREEN。工具名 / 模块路径 / 构造签名（(store, scope_id)）待 P3
确认（探索 02 §3：缺的是「把读 API 包装成审查者可见工具」这一层）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from a2a.coordinator.memory.contracts import (
    ControlTransitionJournalEntry,
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore


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


@pytest.fixture
def scope_id(store, scope_factory):
    scope = scope_factory.resolve("ctx-1", 0)
    store.activate_scope(scope)  # append_temporal_event 有 FK → 必须先激活 scope
    return scope.scope_id


def _append_raw_event(store, scope_id, event_type, *, sequence, payload=None):
    """测试辅助：用现有 append_temporal_event（GREEN API）直写任意事件类型。"""
    return store.append_temporal_event(
        event_id=f"evt_{sequence}",
        scope_id=scope_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at="2026-08-13T00:00:00+00:00",
        ingested_at="2026-08-13T00:00:00+00:00",
        actor_id="Coordinator",
        logical_task_id=None,
        dispatch_id=None,
        worker_task_id=None,
        tool_call_id=None,
        success=None,
        error=None,
        payload=json.dumps(payload or {}),
        causation_id=None,
        correlation_id=None,
        idempotency_key=None,
    )


def _seed_projection(ingestor, scope_id, entity, value, env_step=8):
    """测试辅助：用现有 ingest_projection（GREEN API）写一条投影证据。"""
    result = ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id=f"evt_{entity}",
                sequence=0,
                env_step=env_step,
                actor_id=entity,
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id=entity,
                entity_type="agent",
                field_name="position",
                value=value,
            )
        ]
    )
    assert result.status == "ok"


def _seed_journal_entry(store, dispatch_id="dsp_1"):
    """测试辅助：用现有 record_journal_entry（GREEN API）写一条 journal。"""
    entry = ControlTransitionJournalEntry.build(
        context_id="ctx-1",
        runtime_epoch=0,
        dispatch_id=dispatch_id,
        control_revision=1,
        previous_state="DISPATCHING",
        state="RUNNING",
        source="dispatch",
        observed_at="2026-08-13T00:00:00+00:00",
        result={"action": "dispatch_task"},
    )
    store.record_journal_entry(entry)
    return entry


def _assert_no_forbidden_source_params(tool):
    """B8：只读工具 schema 不得暴露任何 barrier/oracle/truth 读取参数
    （H1 精神内扩展；禁读 barrier/oracle/truth，主方案 §5 / B8）。"""
    props = tool.parameters.get("properties", {})
    assert set(props) <= {"domain", "entity_id", "after_sequence", "dispatch_id"}
    blob = json.dumps(tool.parameters, ensure_ascii=False).lower()
    assert "oracle" not in blob
    assert "truth" not in blob
    assert "barrier" not in blob


# ---------------------------------------------------------------------------
# RED —— QueryProjectionTool（P3 新增，现不存在）
# ---------------------------------------------------------------------------


def test_query_projection_tool_import_and_schema():
    """§5：投影查询工具——name="query_projection"，输入 domain（枚举
    spatial/embodied，必填）+ 可选 entity_id；schema 无真值通道参数。

    当前 sar_orch/tools/coordinator/ 无 query_projection.py → ImportError
    （预期 RED）；工具名/模块路径待 P3 确认。
    """
    from sar_orch.tools.coordinator.query_projection import QueryProjectionTool

    tool = QueryProjectionTool(store=None, scope_id="scope")  # 构造签名待 P3 确认
    assert tool.name == "query_projection"
    assert tool.parameters["required"] == ["domain"]
    assert "entity_id" in tool.parameters["properties"]
    assert tool.parameters["properties"]["domain"]["enum"] == ["spatial", "embodied"]
    _assert_no_forbidden_source_params(tool)


def test_query_projection_tool_returns_spatial_snapshot_shape(store, scope_id, ingestor):
    """§5（探索 02 §3.2 / environment_state_provider.py:109-137）：投影查询
    输出 entity_id → {entity_type, fields: {field_name: 行}}。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_projection import QueryProjectionTool

    _seed_projection(ingestor, scope_id, "Alice", [3, 4, 0])
    tool = QueryProjectionTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute(domain="spatial"))
    assert result.success is True
    data = json.loads(result.content)
    assert "Alice" in data
    assert data["Alice"]["entity_type"] == "agent"
    assert "position" in data["Alice"]["fields"]
    row = data["Alice"]["fields"]["position"]
    assert {"value", "env_step", "provenance", "confidence", "outcome", "evidence_id", "sequence"} <= set(row)
    assert row["value"] == [3, 4, 0]


def test_query_projection_tool_readonly(store, scope_id, ingestor):
    """§5：只读声明——执行后零写入（revision / temporal 计数不变）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_projection import QueryProjectionTool

    _seed_projection(ingestor, scope_id, "Alice", [3, 4, 0])
    before_revision = store.revision_of(scope_id)
    before_events = store.temporal_event_count(scope_id)
    tool = QueryProjectionTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute(domain="spatial"))
    assert result.success is True
    assert store.revision_of(scope_id) == before_revision
    assert store.temporal_event_count(scope_id) == before_events


# ---------------------------------------------------------------------------
# RED —— QueryTemporalFlowTool（P3 新增，现不存在）
# ---------------------------------------------------------------------------


def test_query_temporal_flow_tool_import_and_schema():
    """§5：temporal 流水查询工具——name="query_temporal_flow"，输入可选
    after_sequence（整数）；schema 无真值通道参数。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

    tool = QueryTemporalFlowTool(store=None, scope_id="scope")
    assert tool.name == "query_temporal_flow"
    assert tool.parameters["required"] == []
    assert "after_sequence" in tool.parameters["properties"]
    _assert_no_forbidden_source_params(tool)


def test_query_temporal_flow_tool_returns_sequence_ascending_events(store, scope_id):
    """§5（store.py:834-841）：输出 sequence 升序事件列表，事件携带
    event_id/sequence/event_type/actor_id/payload。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

    _append_raw_event(store, scope_id, "control.dispatch.RUNNING", sequence=1)
    _append_raw_event(store, scope_id, "callback.status_update", sequence=2)
    tool = QueryTemporalFlowTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    assert result.success is True
    events = json.loads(result.content)
    assert [e["sequence"] for e in events] == [1, 2]
    for evt in events:
        assert {"event_id", "sequence", "event_type", "actor_id", "payload"} <= set(evt)


def test_query_temporal_flow_tool_after_sequence_filter(store, scope_id):
    """§5：after_sequence 增量过滤——只返回 sequence 更大的事件。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

    _append_raw_event(store, scope_id, "control.dispatch.RUNNING", sequence=1)
    _append_raw_event(store, scope_id, "callback.status_update", sequence=2)
    tool = QueryTemporalFlowTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute(after_sequence=1))
    events = json.loads(result.content)
    assert [e["sequence"] for e in events] == [2]


def test_query_temporal_flow_tool_filters_diagnosis_audit_events(store, scope_id):
    """§3.2 / R6 视图过滤：时间线只读工具对诊断 audit 事件（diagnosis.audit）
    做视图过滤——诊断 audit 仅供评测/审计，永不进入审查者输入。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

    _append_raw_event(store, scope_id, "control.dispatch.RUNNING", sequence=1)
    _append_raw_event(store, scope_id, "diagnosis.audit", sequence=2)
    tool = QueryTemporalFlowTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    events = json.loads(result.content)
    assert all(not e["event_type"].startswith("diagnosis.") for e in events)
    assert [e["event_type"] for e in events] == ["control.dispatch.RUNNING"]


def test_query_temporal_flow_tool_readonly(store, scope_id):
    """§5：只读声明——执行后零写入。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool

    _append_raw_event(store, scope_id, "control.dispatch.RUNNING", sequence=1)
    before_events = store.temporal_event_count(scope_id)
    tool = QueryTemporalFlowTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    assert result.success is True
    assert store.temporal_event_count(scope_id) == before_events


# ---------------------------------------------------------------------------
# RED —— QuerySupervisionTool（P3 新增，现不存在）
# ---------------------------------------------------------------------------


def test_query_supervision_tool_import_and_schema():
    """§5：supervision 计数工具——name="query_supervision"，无输入参数；
    schema 无真值通道参数。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_supervision import QuerySupervisionTool

    tool = QuerySupervisionTool(store=None, scope_id="scope")
    assert tool.name == "query_supervision"
    assert tool.parameters["required"] == []
    _assert_no_forbidden_source_params(tool)


def test_query_supervision_tool_counts_supervision_events(store, scope_id):
    """§5（store.py:952-961）：计数 = 已提交 supervision.* canonical 事件数
    （非 supervision 事件不计入）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_supervision import QuerySupervisionTool

    _append_raw_event(store, scope_id, "supervision.WORKER_UNREACHABLE", sequence=1)
    _append_raw_event(store, scope_id, "callback.status_update", sequence=2)
    tool = QuerySupervisionTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    assert result.success is True
    data = json.loads(result.content)
    assert data["count"] == 1
    assert data["count"] == store.supervision_event_count(scope_id)


def test_query_supervision_tool_readonly(store, scope_id):
    """§5：只读声明——执行后零写入。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_supervision import QuerySupervisionTool

    _append_raw_event(store, scope_id, "supervision.WORKER_UNREACHABLE", sequence=1)
    before_events = store.temporal_event_count(scope_id)
    tool = QuerySupervisionTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    assert result.success is True
    assert store.temporal_event_count(scope_id) == before_events


# ---------------------------------------------------------------------------
# RED —— QueryControlJournalTool（P3 新增，现不存在）
# ---------------------------------------------------------------------------


def test_query_control_journal_tool_import_and_schema():
    """§5：control journal 读工具——name="query_control_journal"，输入可选
    dispatch_id（context_id/runtime_epoch/dispatch_id 过滤，store.py:518-528）；
    schema 无真值通道参数。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_control_journal import QueryControlJournalTool

    tool = QueryControlJournalTool(store=None, scope_id="scope")
    assert tool.name == "query_control_journal"
    assert tool.parameters["required"] == []
    assert "dispatch_id" in tool.parameters["properties"]
    _assert_no_forbidden_source_params(tool)


def test_query_control_journal_tool_returns_journal_entries(store, scope_id):
    """§5（store.py:518-528）：输出 journal 条目列表，每条携带
    dispatch_id/state/source/control_revision/journal_sha256。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_control_journal import QueryControlJournalTool

    _seed_journal_entry(store, dispatch_id="dsp_1")
    tool = QueryControlJournalTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute(dispatch_id="dsp_1"))
    assert result.success is True
    entries = json.loads(result.content)
    assert len(entries) == 1
    assert {"dispatch_id", "state", "source", "control_revision", "journal_sha256"} <= set(entries[0])
    assert entries[0]["dispatch_id"] == "dsp_1"
    assert entries[0]["state"] == "RUNNING"


def test_query_control_journal_tool_readonly(store, scope_id):
    """§5：只读声明——执行后 journal 行数不变（零写入）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.tools.coordinator.query_control_journal import QueryControlJournalTool

    _seed_journal_entry(store, dispatch_id="dsp_1")
    before = len(store.control_journal_entries())
    tool = QueryControlJournalTool(store=store, scope_id=scope_id)
    result = asyncio.run(tool.execute())
    assert result.success is True
    assert len(store.control_journal_entries()) == before
