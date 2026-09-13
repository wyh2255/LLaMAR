"""Phase 0（P0）契约卡：Embodied telemetry（主方案 RED contract #1/#2）。

目标契约（来自《长期记忆_反思机制+动态Agentcard接入_实施方案》§3.1 / Phase 2）：
- 已认证 callback 在同一 canonical bundle 内产生 callback Temporal + evidence
  Temporal + ``embodied.position/inventory`` 投影，``provenance=worker_telemetry``；
- 重复 callback 不新增行；
- 缺/坏 step 不产生 telemetry claim；
- D1：``worker_telemetry`` 必须位于 position/inventory 的 FIELD_SOURCE_POLICY 第一位；
- 没有真实 battery 来源 → 任何测试都不写 battery。

当前代码事实（5413705）：
- ``projection_inputs`` 同事务通道已存在（ingestor.py:335-438 / server.py:1616-1628），
  所以“callback + evidence + projection 同一 bundle”的机制守护测试是 GREEN；
- 但 ``FIELD_SOURCE_POLICY["position"/"inventory"]`` 尚无 ``worker_telemetry``
  （contracts.py:158-161）→ D1 契约测试 RED（AssertionError）；
- server 侧 telemetry 提取/归一化函数不存在（structured position/inventory 被丢弃，
  server.py:98-142）→ 提取器契约测试 RED（ImportError）。

Phase 2 实现后转 GREEN。
"""

from __future__ import annotations

from typing import Any

import pytest

from a2a.coordinator.memory.contracts import (
    ONLINE_PROVENANCE_ALLOWLIST,
    MemoryConfig,
    NormalizedProjectionInputV1,
    field_source_priority,
)
from a2a.coordinator.memory.ingestor import (
    AuthenticatedCallbackEnvelope,
    MemoryIngestor,
    MemoryScopeFactory,
)
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

#: 模拟 Phase 2 之后的已认证 callback ``[DATA]`` structured_data：
#: 与 barrier.py:404-408 / _barrier_helpers.py:44-48 / sink.py:112-113 同构。
_TELEMETRY_DATA_TEXT = (
    "[DATA] {\"ev\": \"tool_result\", \"success\": true, "
    "\"structured_data\": {\"observations\": [], \"step\": 8, "
    "\"position\": [3, 4, 0], \"inventory\": [\"Water\"]}}"
)

_LEGACY_OBSERVATION_TEXT = (
    "[DATA] {\"ev\": \"tool_result\", \"success\": true, "
    "\"structured_data\": {\"observations\": [{\"object_type\": \"fire\", "
    "\"name\": \"Fire_1\", \"step\": 8, \"position\": [1, 2, 0]}]}}"
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


def _envelope(scope_id: str, body: bytes) -> AuthenticatedCallbackEnvelope:
    import hashlib

    return AuthenticatedCallbackEnvelope.build(
        scope_id=scope_id,
        dispatch_id="dsp_1",
        worker_task_id="worker-1",
        actor_id="Alice",
        runtime_epoch=0,
        callback_kind="status_update",
        normalized_state="RUNNING",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def _telemetry_input(
    scope_id,
    *,
    event_id: str,
    field_name: str,
    value: Any,
    env_step: int | None = 8,
) -> NormalizedProjectionInputV1:
    """Phase 2 producer 形状：agent 自身状态 → domain=embodied,
    provenance=worker_telemetry（主方案 §3.1：身份来自 auth_dispatch.worker_id）。"""
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id="Alice",
        provenance="worker_telemetry",
        domain="embodied",
        entity_id="Alice",
        entity_type="agent",
        field_name=field_name,
        value=value,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# RED #1a —— 同一 canonical bundle：callback Temporal + evidence Temporal +
# embodied.position/inventory 投影（机制守护，Phase 2 不得破坏）
# ---------------------------------------------------------------------------


def test_callback_bundle_with_telemetry_projection_is_atomic(
    ingestor, store, scope_factory
):
    """已认证 callback 携带 telemetry projection inputs 时，callback Temporal +
    evidence Temporal + embodied position/inventory 必须落在同一 canonical
    transaction（现有 ``ingest_callback(projection_inputs=...)`` 通道已实现）。

    GREEN 守护：Phase 2 只能在现有通道内合并 telemetry，不能新开旁路事务。
    """
    scope_id = _scope_id_of(scope_factory)
    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_WORKING"}}}'
    envelope = _envelope(scope_id, body)
    result = ingestor.ingest_callback(
        envelope,
        {"statusUpdate": {"taskId": "worker-1"}},
        projection_inputs=[
            _telemetry_input(scope_id, event_id="evt_tel", field_name="position", value=[3, 4, 0]),
            _telemetry_input(scope_id, event_id="evt_tel", field_name="inventory", value=["Water"]),
        ],
    )
    assert result.status == "ok"

    events = store.temporal_events(scope_id)
    event_types = [e["event_type"] for e in events]
    assert "callback.status_update" in event_types  # callback Temporal
    assert "evidence.projection" in event_types  # evidence Temporal
    assert len([t for t in event_types if t == "evidence.projection"]) == 1

    position = store.get_projection_field(scope_id, "embodied", "Alice", "position")
    inventory = store.get_projection_field(scope_id, "embodied", "Alice", "inventory")
    assert position is not None
    assert position["provenance"] == "worker_telemetry"
    assert inventory is not None
    assert inventory["provenance"] == "worker_telemetry"
    assert store.revision_of(scope_id) == result.committed_revision


def test_duplicate_telemetry_callback_no_new_rows(ingestor, store, scope_factory):
    """同一 canonical bundle（同 envelope + 同 body）重试必须零新增：
    不新增 Temporal / projection / revision / outbox 行（现有 idempotency 机制）。

    GREEN 守护：Phase 2 telemetry 合并进 callback 后必须保持此去重语义。
    """
    scope_id = _scope_id_of(scope_factory)
    body = b'{"statusUpdate":{"taskId":"worker-1","status":{"state":"TASK_STATE_WORKING"}}}'
    inputs = [
        _telemetry_input(scope_id, event_id="evt_tel_pos", field_name="position", value=[3, 4, 0]),
    ]
    first = ingestor.ingest_callback(_envelope(scope_id, body), {"ok": 1}, projection_inputs=inputs)
    assert first.status == "ok"
    before_events = len(store.temporal_events(scope_id))
    before_revision = store.revision_of(scope_id)

    retry = ingestor.ingest_callback(_envelope(scope_id, body), {"ok": 1}, projection_inputs=inputs)
    assert retry.status == "duplicate"
    assert len(store.temporal_events(scope_id)) == before_events
    assert store.revision_of(scope_id) == before_revision
    assert len(store.outbox_entries(scope_id)) == 2  # 只有首次的 callback+evidence 两条


def test_telemetry_duplicate_bundle_via_ingest_projection(ingestor, store, scope_factory):
    """独立投影入口的 telemetry bundle 重试同样 zero-write（projection_idempotency_key）。

    GREEN 守护：P2 复用 ``projection_idempotency_key`` 语义（ingestor.py:73-95）。
    """
    scope_id = _scope_id_of(scope_factory)
    inputs = [
        _telemetry_input(scope_id, event_id="evt_tel_pos", field_name="position", value=[3, 4, 0]),
        _telemetry_input(scope_id, event_id="evt_tel_inv", field_name="inventory", value=["Water"]),
    ]
    assert ingestor.ingest_projection(inputs).status == "ok"
    fields_before = len(store.projection_fields(scope_id))
    dup = ingestor.ingest_projection(inputs)
    assert dup.status == "duplicate"
    assert len(store.projection_fields(scope_id)) == fields_before


# ---------------------------------------------------------------------------
# RED #2a —— 时序栅栏：newer-step telemetry 不被旧/无 step 回退（守护，现有语义）
# ---------------------------------------------------------------------------


def test_newer_step_telemetry_not_regressed_by_older_step(ingestor, store, scope_factory):
    """step 8 telemetry 落库后，step 7 的 telemetry 不得覆盖（现有 reducer
    env_step fence，projections.py:104-113）。

    GREEN 守护：D1 实施后此栅栏必须原样保留。
    """
    scope_id = _scope_id_of(scope_factory)
    newer = ingestor.ingest_projection(
        [_telemetry_input(scope_id, event_id="evt_t8", field_name="position", value=[5, 6, 0], env_step=8)]
    )
    assert newer.status == "ok"
    older = ingestor.ingest_projection(
        [_telemetry_input(scope_id, event_id="evt_t7", field_name="position", value=[1, 1, 0], env_step=7)]
    )
    assert older.status == "ok"
    row = store.get_projection_field(scope_id, "embodied", "Alice", "position")
    assert row["value"] == [5, 6, 0]
    assert row["env_step"] == 8


def test_no_step_telemetry_cannot_overwrite_existing_step(ingestor, store, scope_factory):
    """已有带 step 的字段时，无 step 的 telemetry claim 不得覆盖（现有 fence）。

    GREEN 守护：D1 语义要求 telemetry 必须带 env_step，无 step 不产生覆盖。
    """
    scope_id = _scope_id_of(scope_factory)
    assert ingestor.ingest_projection(
        [_telemetry_input(scope_id, event_id="evt_t8", field_name="position", value=[5, 6, 0], env_step=8)]
    ).status == "ok"
    assert ingestor.ingest_projection(
        [_telemetry_input(scope_id, event_id="evt_nostep", field_name="position", value=[9, 9, 0], env_step=None)]
    ).status == "ok"
    row = store.get_projection_field(scope_id, "embodied", "Alice", "position")
    assert row["env_step"] == 8
    assert row["value"] == [5, 6, 0]


def test_worker_telemetry_provenance_allowlisted():
    """``worker_telemetry`` 必须在 ONLINE_PROVENANCE_ALLOWLIST 内（现状已满足）。

    GREEN 守护：D1 的权威性声明依赖 allowlist 成员身份（contracts.py:57）。
    """
    assert "worker_telemetry" in ONLINE_PROVENANCE_ALLOWLIST


# ---------------------------------------------------------------------------
# RED #2b —— D1 source policy：worker_telemetry 必须排 position/inventory 第一
# ---------------------------------------------------------------------------


def test_worker_telemetry_first_in_position_field_source_policy():
    """D1：``position`` 的 FIELD_SOURCE_POLICY 中 ``worker_telemetry`` 必须是
    最高权威（priority 0）。

    Phase 2 修复（contracts.py 调整 policy）；当前 policy 为
    (worker_sensor_tool, worker_observation, peer_report)，worker_telemetry
    不在其中 → field_source_priority 返回 3 → AssertionError（预期 RED）。
    """
    assert field_source_priority("position", "worker_telemetry") == 0


def test_worker_telemetry_first_in_inventory_field_source_policy():
    """D1：``inventory`` 的 FIELD_SOURCE_POLICY 中 ``worker_telemetry`` 必须是
    最高权威（priority 0）。

    当前 inventory policy 与 position 相同，worker_telemetry 不在其中 →
    priority 3 → AssertionError（预期 RED）。
    """
    assert field_source_priority("inventory", "worker_telemetry") == 0


def test_same_step_telemetry_outranks_observation_for_position(
    ingestor, store, scope_factory
):
    """场景级 D1 契约：同 env_step 时 worker_telemetry 的 position claim 必须
    胜过 worker_observation（自报是 agent 自身位置的最高权威）。

    当前：worker_observation priority 1 < worker_telemetry priority 3，
    telemetry 被 superseded → 投影 provenance 保持 worker_observation →
    AssertionError（预期 RED）。
    """
    scope_id = _scope_id_of(scope_factory)
    obs = NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id="evt_obs",
        sequence=0,
        env_step=8,
        actor_id="Alice",
        provenance="worker_observation",
        domain="embodied",
        entity_id="Alice",
        entity_type="agent",
        field_name="position",
        value=[3, 4, 0],
    )
    tel = _telemetry_input(scope_id, event_id="evt_tel", field_name="position", value=[5, 6, 0], env_step=8)
    assert ingestor.ingest_projection([obs]).status == "ok"
    assert ingestor.ingest_projection([tel]).status == "ok"

    row = store.get_projection_field(scope_id, "embodied", "Alice", "position")
    assert row["provenance"] == "worker_telemetry"  # D1 契约值


# ---------------------------------------------------------------------------
# RED #1b —— 未来 server telemetry 提取/归一化（Phase 2 新增，现不存在）
# ---------------------------------------------------------------------------


def test_extract_worker_telemetry_with_provenance_exists():
    """Phase 2 必须在 coordinator server 新增 telemetry 提取器
    ``_extract_worker_telemetry_with_provenance()``：从已认证 callback 的
    structured_data 提取 position/inventory（主方案 §3.1）。

    当前不存在 → ImportError（预期 RED）。P2 实现后，本测试断言提取结果
    携带 worker_telemetry provenance 且 entity 身份来自 auth dispatch。
    """
    from a2a.coordinator.server import (
        _extract_worker_telemetry_with_provenance,
    )

    pairs = _extract_worker_telemetry_with_provenance(_TELEMETRY_DATA_TEXT)
    assert pairs  # structured_data 携带 position/inventory 时必须提取到
    for _, provenance in pairs:
        assert provenance == "worker_telemetry"


def test_telemetry_missing_step_produces_no_claim():
    """缺 ``step`` 的 telemetry payload 不得产生任何 claim（主方案 §3.1：
    step 缺失 → 对应 field 不产生 claim；callback 自身 Temporal 审计保持）。

    当前提取器不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.server import (
        _normalize_telemetry_projection_inputs,
    )

    inputs = _normalize_telemetry_projection_inputs(
        [({"position": [1, 2, 0], "inventory": ["Water"]}, "worker_telemetry")],
        scope_id="scope",
        actor_id="Alice",
    )
    assert inputs == []


def test_telemetry_bad_step_produces_no_claim():
    """坏 step（非整数）或非三维 position 一律不产生 claim（fail-closed，
    绝不写入 0/None/推断值）。当前提取器不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.server import (
        _normalize_telemetry_projection_inputs,
    )

    bad = [
        ({"step": "eight", "position": [1, 2, 0], "inventory": []}, "worker_telemetry"),
        ({"step": 8, "position": [1, 2], "inventory": ["Water"]}, "worker_telemetry"),
        ({"step": 8, "position": [1, 2, 0], "inventory": "unparseable::{{{"}, "worker_telemetry"),
    ]
    inputs = _normalize_telemetry_projection_inputs(bad, scope_id="scope", actor_id="Alice")
    assert inputs == []


def test_telemetry_never_produces_battery_claim():
    """没有真实 battery 生产者（全仓库无 battery 数据源）→ telemetry 提取
    永不产生 battery claim，禁止写 0/None/推断值（主方案 §3.1 / 明确不做）。

    当前提取器不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.server import (
        _extract_worker_telemetry_with_provenance,
    )

    pairs = _extract_worker_telemetry_with_provenance(_TELEMETRY_DATA_TEXT)
    assert all(pair[0].get("battery") is None for pair in pairs)


def test_telemetry_identity_from_authenticated_dispatch_not_payload():
    """身份不相信 payload：entity_id/actor_id 必须来自已认证
    auth_dispatch.worker_id（经 ``_normalize_telemetry_projection_inputs`` 的
    ``actor_id`` 入参绑定），而不是 structured_data 里的 reporter/name
    （主方案 §3.1）。当前提取器不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.server import (
        _extract_worker_telemetry_with_provenance,
        _normalize_telemetry_projection_inputs,
    )

    spoofed = _TELEMETRY_DATA_TEXT.replace(
        '"step": 8', '"step": 8, "reporter": "Mallory"'
    )
    pairs = _extract_worker_telemetry_with_provenance(spoofed)
    # 提取器输出不携带 payload 自报身份：reporter/name 非白名单字段必须被剥离
    for pair in pairs:
        assert "reporter" not in pair[0]
        assert "name" not in pair[0]
    # 归一化函数以 auth dispatch 的 actor_id 绑定实体身份（reporter 无从泄漏）
    inputs = _normalize_telemetry_projection_inputs(
        pairs, scope_id="scope", actor_id="Alice"
    )
    for inp in inputs:
        assert inp.actor_id == "Alice"
        assert inp.entity_id == "Alice"


def test_telemetry_evidence_id_distinct_from_observation_evidence():
    """telemetry evidence id 必须与同 callback 的环境观测 evidence id 不冲突
    （确定性摘要，主方案 §3.1：telemetry evidence id 为 callback/body/worker/step
    的确定性摘要，不能与环境 observation evidence id 冲突）。

    当前提取器不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.server import (
        _extract_worker_telemetry_with_provenance,
    )

    telemetry_pairs = _extract_worker_telemetry_with_provenance(_TELEMETRY_DATA_TEXT)
    observation_pairs = _extract_worker_telemetry_with_provenance(_LEGACY_OBSERVATION_TEXT)
    # 读取实际证据 event_id 字段（不能依赖对象身份：id(pair) 恒不同，断言空真）
    telemetry_ids = {pair[0].get("event_id") for pair in telemetry_pairs}
    observation_ids = {pair[0].get("event_id") for pair in observation_pairs}
    # event_id 必须真实存在（非 None/非缺键），否则"不冲突"断言无意义
    assert all(isinstance(i, str) and i for i in telemetry_ids | observation_ids)
    assert telemetry_ids.isdisjoint(observation_ids)


# ---------------------------------------------------------------------------
# Phase 2（P2）增补 —— 提取→归一化→落库全链路（只追加，不改 P0 断言）
# ---------------------------------------------------------------------------


def test_telemetry_extract_normalize_ingest_full_chain(ingestor, store, scope_factory):
    """P2 全链路：合法 structured_data（step=8 + position + inventory）经
    提取器→归一化器→ingest_projection 后，embodied.position/inventory 以
    worker_telemetry provenance 落库，identity 绑定 auth dispatch 入参。"""
    from a2a.coordinator.server import (
        _extract_worker_telemetry_with_provenance,
        _normalize_telemetry_projection_inputs,
    )

    scope_id = _scope_id_of(scope_factory)
    pairs = _extract_worker_telemetry_with_provenance(_TELEMETRY_DATA_TEXT)
    assert pairs
    inputs = _normalize_telemetry_projection_inputs(
        pairs, scope_id=scope_id, actor_id="Alice"
    )
    field_names = {inp.field_name for inp in inputs}
    assert field_names == {"position", "inventory"}
    for inp in inputs:
        assert inp.provenance == "worker_telemetry"
        assert inp.domain == "embodied"
        assert inp.entity_id == "Alice"
        assert inp.actor_id == "Alice"
        assert inp.env_step == 8
        assert inp.event_id.startswith("tel:")

    assert ingestor.ingest_projection(inputs).status == "ok"
    pos = store.get_projection_field(scope_id, "embodied", "Alice", "position")
    inv = store.get_projection_field(scope_id, "embodied", "Alice", "inventory")
    assert pos is not None and pos["provenance"] == "worker_telemetry"
    assert pos["value"] == [3, 4, 0]
    assert inv is not None and inv["provenance"] == "worker_telemetry"
    assert inv["value"] == ["Water"]


def test_telemetry_invalid_inventory_fails_closed_empty_still_legal():
    """P2 review 修复（M1，fail-closed 漏洞）：inventory 为 None/int/bool/空串
    时整束 telemetry 必须零 claim，不得被 ``normalize_inventory`` 折叠成空库存
    claim 落库（否则经 reducer 覆写真实库存）；合法空库存 ``[]``/``{}`` 仍产生
    inventory claim（value=[]），agent 确实可能无库存。"""
    from a2a.coordinator.server import _normalize_telemetry_projection_inputs

    def claims_of(payload):
        inputs = _normalize_telemetry_projection_inputs(
            [(payload, "worker_telemetry")], scope_id="scope", actor_id="Alice"
        )
        return {inp.field_name: inp.value for inp in inputs}

    # 类型错误/缺失型 inventory：整束零 claim（fail-closed）
    for bad_inventory in (None, "", 42, True):
        claims = claims_of({"step": 8, "inventory": bad_inventory})
        assert claims == {}, (
            f"inventory={bad_inventory!r} 必须整束零 claim，实际 {claims}"
        )

    # 合法空库存：仍产生 inventory claim（value=[]）
    for empty_inventory in ([], {}):
        claims = claims_of({"step": 8, "inventory": empty_inventory})
        assert claims == {"inventory": []}, (
            f"合法空库存 {empty_inventory!r} 必须产生 inventory claim，实际 {claims}"
        )
