"""Phase 0（P0）契约卡：AgentCard registry projection（主方案 RED contract #3）。

目标契约（主方案 §3.2 / Phase 3）：
- capability：``AgentSkill(id=<capability>)`` → 排序去重 capability 列表；
- sensor_type：``AgentSkill(tags=[...\"sensor_type:<slug>\"...])`` → 排序去重 slug 列表；
- minimal registration（无有效 skill / AgentCard fetch 失败）→ 零 capability/sensor_type claim；
- scope bootstrap：runtime 创建时对 online、card-available agent 做能力快照；
- same-card duplicate：重复注册（重连）不摄入、无 revision storm；
- 新 worker 首次注册摄入契约；
- ``AgentInfo`` 增加 ``sensor_types`` / ``agent_card_digest`` / ``agent_card_available``。

当前代码事实（5413705）：
- ``a2a.coordinator.memory.registry_projection`` 模块不存在 → ImportError（预期 RED）；
- ``AgentInfo``（agent_registry.py:19-37）没有 sensor_types/agent_card_digest/
  agent_card_available 字段 → AttributeError（预期 RED）；
- 现有 ``AgentRegistry.register_from_agent_card``（agent_registry.py:110-162）已能提取
  capabilities，但不去重、不排序、不写 Memory → 部分守护测试 GREEN；
- ``projection_idempotency_key``（ingestor.py:73-95）+ ``ingest_projection`` 的
  duplicate 语义已存在 → same-card duplicate 的机制守护 GREEN。

Phase 3 实现后转 GREEN。
"""

from __future__ import annotations

import pytest

from a2a.coordinator.agent_registry import AgentInfo, AgentRegistry
from a2a.coordinator.memory.contracts import (
    FIELD_SOURCE_POLICY,
    ONLINE_PROVENANCE_ALLOWLIST,
    MemoryConfig,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

#: 与 create_worker_a2a_server（a2a_server.py:164-199）同构的 AgentCard JSON。
_CARD_WITH_SENSORS = {
    "name": "Alice",
    "description": "SAR worker",
    "skills": [
        {"id": "navigation", "name": "navigation", "tags": ["navigation"]},
        {"id": "rescue", "name": "rescue", "tags": ["rescue"]},
        {"id": "backend", "tags": ["metadata", "backend", "mini_agent"]},
        {"id": "model", "tags": ["metadata", "model", "deepseek-v4-flash"]},
        {
            "id": "thermal_camera",
            "tags": ["sensor_type:thermal", "sensor_type:thermal", "sensor_type:gps"],
        },
    ],
    "capabilities": {"streaming": True, "pushNotifications": True},
}

_CARD_MINIMAL = {
    "name": "Bob",
    "description": "no skills",
    "skills": [],
    "capabilities": {"streaming": False, "pushNotifications": False},
}

_CARD_DUPLICATE_CAPS = {
    "name": "Carol",
    "description": "duplicate caps",
    "skills": [
        {"id": "sar", "tags": ["sar"]},
        {"id": "sar", "tags": ["sar"]},
        {"id": "navigation", "tags": ["navigation"]},
        {"id": "backend", "tags": ["metadata", "backend", "mini_agent"]},
        {"id": "model", "tags": ["metadata", "model", "claude-opus-4-5"]},
    ],
    "capabilities": {"streaming": True, "pushNotifications": True},
}


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


def _capability_input(scope_id, *, event_id: str, entity_id: str, value) -> list:
    from a2a.coordinator.memory.contracts import NormalizedProjectionInputV1

    return [
        NormalizedProjectionInputV1(
            scope_id=scope_id,
            event_id=event_id,
            sequence=0,
            env_step=None,  # 静态 registry 元数据：env_step=None（主方案 §3.2）
            actor_id=entity_id,
            provenance="registry",
            domain="embodied",
            entity_id=entity_id,
            entity_type="agent",
            field_name="capability",
            value=value,
        )
    ]


# ---------------------------------------------------------------------------
# GREEN 守护 —— 现有注册/摄入机制（Phase 3 不得破坏）
# ---------------------------------------------------------------------------


def test_agent_registry_parses_capabilities_from_card():
    """现有 register_from_agent_card 能从 AgentCard skills 提取 capabilities
    （agent_registry.py:123-142）。

    GREEN 守护：Phase 3 在其上增加排序/去重/摄入，不能改变既有提取语义。
    """
    registry = AgentRegistry()
    agent = registry.register_from_agent_card("Alice", "http://alice:10001", _CARD_WITH_SENSORS)
    assert "navigation" in agent.capabilities
    assert "rescue" in agent.capabilities
    assert "backend" not in agent.capabilities
    assert "model" not in agent.capabilities


def test_registry_provenance_allowlisted():
    """``registry`` 必须在 ONLINE_PROVENANCE_ALLOWLIST 内（contracts.py:60）。

    GREEN 守护：registry 来源的投影 claim 天然过 allowlist 门。
    """
    assert "registry" in ONLINE_PROVENANCE_ALLOWLIST


def test_capability_and_sensor_type_field_policy_registry_only():
    """``capability``/``sensor_type`` 的 FIELD_SOURCE_POLICY 只允许 registry
    来源（contracts.py:176-177）。

    GREEN 守护：Phase 3 摄入时必须保持 provenance=registry。
    """
    assert FIELD_SOURCE_POLICY["capability"] == ("registry",)
    assert FIELD_SOURCE_POLICY["sensor_type"] == ("registry",)


def test_capability_claim_materializes_through_existing_reducer(ingestor, store, scope_factory):
    """现有 reducer/ingest_projection 已能接受 registry 来源的 capability claim
    （env_step=None，静态元数据）。

    GREEN 守护：证明“仲裁/摄入机制就绪，只缺 producer”；Phase 3 只补 producer。
    """
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        _capability_input(scope_id, event_id="evt_reg", entity_id="Alice", value=["navigation", "rescue"])
    )
    assert result.status == "ok"
    row = store.get_projection_field(scope_id, "embodied", "Alice", "capability")
    assert row is not None
    assert row["provenance"] == "registry"
    assert row["env_step"] is None


def test_same_capability_bundle_duplicate_no_revision_storm(ingestor, store, scope_factory):
    """同一能力证据束（内容决定 key）重复摄入 → typed duplicate，零新写入。

    GREEN 守护：``projection_idempotency_key``（ingestor.py:73-95）是 Phase 3
    same-card duplicate 依赖的机制。
    """
    scope_id = _scope_id_of(scope_factory)
    inputs = _capability_input(scope_id, event_id="evt_reg", entity_id="Alice", value=["navigation", "rescue"])
    assert ingestor.ingest_projection(inputs).status == "ok"
    revision_after_first = store.revision_of(scope_id)
    dup = ingestor.ingest_projection(inputs)
    assert dup.status == "duplicate"
    assert store.revision_of(scope_id) == revision_after_first
    assert len(store.temporal_events(scope_id)) == 1


def test_registry_claim_with_forbidden_truth_value_denied(ingestor, store, scope_factory):
    """registry 来源的 claim 若 value 内嵌真值词，仍被 H1-INV-1 门拒绝（零写）。

    GREEN 守护：registry 摄入不得绕过 truth boundary。
    """
    scope_id = _scope_id_of(scope_factory)
    inputs = _capability_input(scope_id, event_id="evt_reg_bad", entity_id="Alice", value=["oracle_mode"])
    result = ingestor.ingest_projection(inputs)
    assert result.status == "online_truth_forbidden"
    assert store.get_projection_field(scope_id, "embodied", "Alice", "capability") is None


# ---------------------------------------------------------------------------
# RED —— AgentInfo 扩展字段（Phase 3 修改 agent_registry.py）
# ---------------------------------------------------------------------------


def test_agent_info_has_sensor_types():
    """D2/D3：``AgentInfo`` 必须增加 ``sensor_types``（排序去重 slug 列表）。

    当前 dataclass（agent_registry.py:19-37）无此字段 → AttributeError（预期 RED）。
    """
    registry = AgentRegistry()
    agent = registry.register_from_agent_card("Alice", "http://alice:10001", _CARD_WITH_SENSORS)
    assert isinstance(agent.sensor_types, list)  # AttributeError: 字段不存在


def test_agent_info_has_agent_card_digest():
    """D2：``AgentInfo`` 必须增加 ``agent_card_digest``（卡片内容的确定性摘要，
    用于 same-card duplicate 判定）。

    当前无此字段 → AttributeError（预期 RED）。
    """
    registry = AgentRegistry()
    agent = registry.register_from_agent_card("Alice", "http://alice:10001", _CARD_WITH_SENSORS)
    assert isinstance(agent.agent_card_digest, str)  # AttributeError: 字段不存在


def test_agent_info_has_agent_card_available():
    """D2：``AgentInfo`` 必须增加 ``agent_card_available``（可判定字段，
    区分“拉卡成功但无能力”与“拉卡失败”）。

    当前无此字段 → AttributeError（预期 RED）。
    """
    registry = AgentRegistry()
    agent = registry.register_from_agent_card("Alice", "http://alice:10001", _CARD_WITH_SENSORS)
    assert agent.agent_card_available is True  # AttributeError: 字段不存在


# ---------------------------------------------------------------------------
# RED —— 未来 registry_projection 模块（Phase 3 新增，现不存在）
# ---------------------------------------------------------------------------


def test_registry_projection_module_parses_capabilities():
    """``a2a.coordinator.memory.registry_projection`` 必须提供 AgentCard →
    NormalizedProjectionInputV1 的纯函数（Phase 3）。

    模块不存在 → ImportError（预期 RED）。契约：capability 为排序去重列表。
    """
    from a2a.coordinator.memory.registry_projection import (
        parse_agent_card_capabilities,
    )

    caps = parse_agent_card_capabilities(_CARD_DUPLICATE_CAPS)
    assert caps == ["navigation", "sar"]  # 排序去重


def test_sensor_type_tag_normalized_sorted_unique():
    """D3：``sensor_type:<slug>`` 保留格式解析为排序去重 slug 列表。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        parse_agent_card_sensor_types,
    )

    sensors = parse_agent_card_sensor_types(_CARD_WITH_SENSORS)
    assert sensors == ["gps", "thermal"]  # 排序去重（输入含重复 thermal）


def test_sensor_type_absent_when_no_valid_tag():
    """D3：无有效 ``sensor_type:<slug>`` tag 时不得写 sensor_type（不能把
    空值当事实）。模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        parse_agent_card_sensor_types,
    )

    sensors = parse_agent_card_sensor_types(_CARD_MINIMAL)
    assert sensors == []


def test_minimal_registration_zero_claims():
    """D2：minimal registration（无有效 skill / 拉卡失败兜底）→ 零
    capability/sensor_type claim（不能把“未知”持久化成“无能力”）。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        registry_projection_inputs,
    )

    inputs = registry_projection_inputs(
        agent_id="Bob",
        capabilities=[],
        sensor_types=[],
        card_available=False,
        scope_id="scope",
    )
    assert inputs == []


def test_scope_bootstrap_snapshot_on_runtime_created():
    """D2：runtime 创建时（activate scope 之后）对 online、card-available agent
    做能力快照 bootstrap（先 activate scope → 再 snapshot online cards）。
    Phase 3 在 server runtime-created hook 实现。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        build_scope_bootstrap_inputs,
    )

    inputs = build_scope_bootstrap_inputs(scope_id="scope", agents=[AgentInfo(agent_id="Alice", description="", endpoint="")])
    assert len(inputs) >= 1


def test_same_card_duplicate_no_reingest():
    """D2：same-card duplicate（断线重连重复注册、卡片 digest 未变）→ 不摄入、
    无 revision storm。Phase 3 依赖 card digest + 现有 idempotency。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        registry_projection_inputs,
    )

    first = registry_projection_inputs(
        agent_id="Alice",
        capabilities=["navigation"],
        sensor_types=["thermal"],
        card_available=True,
        scope_id="scope",
    )
    second = registry_projection_inputs(
        agent_id="Alice",
        capabilities=["navigation"],
        sensor_types=["thermal"],
        card_available=True,
        scope_id="scope",
    )
    # 同卡片 → 同一确定性 bundle（内容决定 key，ingestor 侧 duplicate 零写入）
    assert [inp.event_id for inp in first] == [inp.event_id for inp in second]


def test_new_worker_first_registration_ingests_capability():
    """D2：新 worker 首次注册成功拉卡后，对 active scope 摄入能力快照
    （provenance=registry、domain=embodied、env_step=None）。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        registry_projection_inputs,
    )

    inputs = registry_projection_inputs(
        agent_id="Alice",
        capabilities=["navigation", "rescue"],
        sensor_types=[],
        card_available=True,
        scope_id="scope",
    )
    for inp in inputs:
        assert inp.provenance == "registry"
        assert inp.domain == "embodied"
        assert inp.env_step is None
    assert {inp.field_name for inp in inputs} == {"capability"}


def test_failed_card_fetch_not_persisted_as_no_capability():
    """D2：AgentCard fetch 失败 → minimal registration 只保留 transport
    可用性，不投影能力（``agent_card_available`` gate）。

    模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.registry_projection import (
        registry_projection_inputs,
    )

    inputs = registry_projection_inputs(
        agent_id="Bob",
        capabilities=[],
        sensor_types=[],
        card_available=False,  # 拉卡失败：未知 ≠ 无能力
        scope_id="scope",
    )
    assert inputs == []


# ---------------------------------------------------------------------------
# Phase 3 增补 —— 实现层验证（P0 契约转 GREEN 后追加；不改动任何既有断言）
# ---------------------------------------------------------------------------


def test_register_from_agent_card_fills_sensor_types_and_digest():
    """registry 层：register_from_agent_card 必须填充 sensor_types（排序去重）、
    agent_card_digest（确定性摘要）与 agent_card_available=True。"""
    registry = AgentRegistry()
    agent = registry.register_from_agent_card(
        "Alice", "http://alice:10001", _CARD_WITH_SENSORS
    )
    assert agent.sensor_types == ["gps", "thermal"]  # 排序去重（输入含重复 thermal）
    assert isinstance(agent.agent_card_digest, str) and len(agent.agent_card_digest) == 64
    assert agent.agent_card_available is True


def test_register_from_agent_card_capabilities_sorted_unique():
    """registry 层：capabilities 必须排序去重（与 parse_agent_card_capabilities
    一致；P0 GREEN 守护只断言子集关系，此处钉死顺序）。"""
    registry = AgentRegistry()
    agent = registry.register_from_agent_card(
        "Carol", "http://carol:10001", _CARD_DUPLICATE_CAPS
    )
    assert agent.capabilities == ["navigation", "sar"]


def test_agent_card_digest_deterministic_across_registrations():
    """same card → same digest（断线重连 same-card duplicate 判定的基础）。"""
    registry = AgentRegistry()
    first = registry.register_from_agent_card(
        "Alice", "http://alice:10001", _CARD_WITH_SENSORS
    )
    second = registry.register_from_agent_card(
        "Alice", "http://alice:10001", _CARD_WITH_SENSORS
    )
    assert first.agent_card_digest == second.agent_card_digest


def test_bootstrap_inputs_exclude_card_unavailable_agents():
    """D2：bootstrap 只覆盖 card-available agent；拉卡失败（card_available
    False）的 agent 零 claim。"""
    from a2a.coordinator.memory.registry_projection import (
        build_scope_bootstrap_inputs,
    )

    inputs = build_scope_bootstrap_inputs(
        scope_id="scope",
        agents=[
            AgentInfo(agent_id="Alice", description="", endpoint=""),
            AgentInfo(
                agent_id="Bob",
                description="",
                endpoint="",
                agent_card_available=False,
            ),
        ],
    )
    assert {inp.entity_id for inp in inputs} == {"Alice"}


def test_runtime_created_hook_bootstraps_online_agents(tmp_path, monkeypatch):
    """D2：runtime 创建 hook（activate scope 之后）对 online、card-available
    agent 做能力快照 bootstrap；结果落在 canonical Memory 的 embodied 段。"""

    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.server import CoordinatorServer

    memory_store = MemoryStore(tmp_path / "hook.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(
        memory_store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.configure_memory(
        ingestor=ingestor,
        config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        secret=b"SUPERSECRET_VALUE_9f2c1",
    )
    # runtime 创建前注册：Alice online + card available。
    server._agent_registry.register_from_agent_card(
        "Alice", "http://alice:10001", _CARD_WITH_SENSORS
    )
    server._agent_registry.register(
        AgentInfo(
            agent_id="Bob",
            description="no card",
            endpoint="http://bob:10002",
            agent_card_available=False,
        )
    )

    server._mission_runtime_manager.admit("ctx-1")  # 触发 runtime-created hook

    scope_id = ingestor.scope_id_for("ctx-1", 0)
    row = memory_store.get_projection_field(scope_id, "embodied", "Alice", "capability")
    assert row is not None
    assert row["provenance"] == "registry"
    assert row["env_step"] is None
    assert row["value"] == ["navigation", "rescue", "thermal_camera"]
    sensor_row = memory_store.get_projection_field(
        scope_id, "embodied", "Alice", "sensor_type"
    )
    assert sensor_row is not None
    assert sensor_row["value"] == ["gps", "thermal"]
    # 拉卡失败的 Bob 零 claim。
    assert (
        memory_store.get_projection_field(scope_id, "embodied", "Bob", "capability")
        is None
    )
    # hook 异常不破坏 runtime 创建（此处用真实 ingest，bootstrap 成功后
    # attach_receipt_sink 仍然执行——runtime 已创建即证明）。
    assert server._mission_runtime_manager.active_runtime is not None


def test_ws_register_first_registration_ingests_capability_snapshot(
    tmp_path, monkeypatch
):
    """D2：新 worker 首次注册成功拉卡后，对 active scope 摄入能力快照。"""

    import asyncio

    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.server import CoordinatorServer
    from a2a.shared.types import WS_REGISTER

    memory_store = MemoryStore(tmp_path / "ws.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(
        memory_store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.configure_memory(
        ingestor=ingestor,
        config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        secret=b"SUPERSECRET_VALUE_9f2c1",
    )
    # 先有 active runtime（hook 激活 scope ctx-1/0）。
    server._mission_runtime_manager.admit("ctx-1")

    async def _fake_fetch(url, retries=3, delay=1.0):
        return _CARD_WITH_SENSORS

    monkeypatch.setattr(server, "_fetch_agent_card", _fake_fetch)
    asyncio.run(
        server._handle_worker_message(
            "Alice",
            {"type": WS_REGISTER, "payload": {"a2a_endpoint": "http://alice:10001"}},
        )
    )

    scope_id = ingestor.scope_id_for("ctx-1", 0)
    row = memory_store.get_projection_field(scope_id, "embodied", "Alice", "capability")
    assert row is not None
    assert row["provenance"] == "registry"
    assert row["env_step"] is None
    assert row["value"] == ["navigation", "rescue", "thermal_camera"]
    assert (
        memory_store.get_projection_field(scope_id, "embodied", "Alice", "sensor_type")
        is not None
    )


def test_ws_register_duplicate_registration_zero_ingest(tmp_path, monkeypatch):
    """D2：重复注册（断线重连，same card）→ 零摄入、无 revision storm。"""

    import asyncio

    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.server import CoordinatorServer
    from a2a.shared.types import WS_REGISTER

    memory_store = MemoryStore(tmp_path / "ws-dup.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(
        memory_store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.configure_memory(
        ingestor=ingestor,
        config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        secret=b"SUPERSECRET_VALUE_9f2c1",
    )
    server._mission_runtime_manager.admit("ctx-1")

    async def _fake_fetch(url, retries=3, delay=1.0):
        return _CARD_WITH_SENSORS

    monkeypatch.setattr(server, "_fetch_agent_card", _fake_fetch)
    msg = {"type": WS_REGISTER, "payload": {"a2a_endpoint": "http://alice:10001"}}
    asyncio.run(server._handle_worker_message("Alice", msg))
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    revision_after_first = memory_store.revision_of(scope_id)
    # capability + sensor_type 两个 claim（两个 evidence event_id）→ 2 个 temporal 事件。
    assert len(memory_store.temporal_events(scope_id)) == 2

    # 断线重连：重复注册（same card）→ 零摄入。
    asyncio.run(server._handle_worker_message("Alice", msg))
    assert memory_store.revision_of(scope_id) == revision_after_first
    assert len(memory_store.temporal_events(scope_id)) == 2


def test_ws_register_failed_card_fetch_zero_claims(tmp_path, monkeypatch):
    """D2：AgentCard fetch 失败 → minimal registration 只保留 transport
    可用性，不投影能力（agent_card_available=False gate）。"""

    import asyncio

    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.server import CoordinatorServer
    from a2a.shared.types import WS_REGISTER

    memory_store = MemoryStore(tmp_path / "ws-min.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(
        memory_store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.configure_memory(
        ingestor=ingestor,
        config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        secret=b"SUPERSECRET_VALUE_9f2c1",
    )
    server._mission_runtime_manager.admit("ctx-1")

    async def _fake_fetch(url, retries=3, delay=1.0):
        return None

    monkeypatch.setattr(server, "_fetch_agent_card", _fake_fetch)
    asyncio.run(
        server._handle_worker_message(
            "Bob",
            {"type": WS_REGISTER, "payload": {"a2a_endpoint": "http://bob:10002"}},
        )
    )

    info = server._agent_registry.get("Bob")
    assert info.agent_card_available is False
    scope_id = ingestor.scope_id_for("ctx-1", 0)
    assert (
        memory_store.get_projection_field(scope_id, "embodied", "Bob", "capability")
        is None
    )
    assert len(memory_store.temporal_events(scope_id)) == 0


# ---------------------------------------------------------------------------
# P3 review 修复增补 —— M1/M2 静态配置 agent 场景（追加段落，不改动既有断言）
# ---------------------------------------------------------------------------


def test_static_config_agent_not_projected_until_card_authenticated(tmp_path):
    """M1（P3 review）：静态 YAML 配置的 capabilities 未经 AgentCard 认证，
    不得被 bootstrap 投影为 registry 事实——``_load_static_config`` 构造的
    AgentInfo 必须 ``agent_card_available=False``，经
    ``build_scope_bootstrap_inputs`` 产生零 claim（未知 ≠ 无能力）。"""

    import yaml

    from a2a.coordinator.memory.registry_projection import (
        build_scope_bootstrap_inputs,
    )

    cfg = tmp_path / "agents.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "id": "StaticAlice",
                        "description": "static config agent",
                        "endpoint": "http://static-alice:10001",
                        "capabilities": ["navigation", "sar"],
                    }
                ]
            }
        )
    )
    registry = AgentRegistry(static_config_path=cfg)
    agent = registry.get("StaticAlice")
    assert agent.agent_card_available is False  # 未认证：未知 ≠ 无能力
    assert agent.capabilities == ["navigation", "sar"]  # 配置仍保留（仅不投影）
    inputs = build_scope_bootstrap_inputs(scope_id="scope", agents=[agent])
    assert inputs == []  # M1 契约：静态配置能力零 claim


def test_static_agent_first_ws_register_transitions_to_card_auth(tmp_path):
    """M2（P3 review）纯函数部分：静态配置预置的 agent（``contains()`` 为 True
    但 ``agent_card_digest is None``）首次 WS 注册拉卡后，
    ``register_from_agent_card`` 覆盖为 card-available（available=True、
    digest 非 None），``registry_projection_inputs`` 产生 capability claim——
    即 server 端 ``first_registration = prev is None or
    prev.agent_card_digest is None`` 判定在 digest None 时应为 True
    （静态 agent 首次注册也能摄入，动态重连 digest 非 None → False 零摄入）。"""

    import yaml

    from a2a.coordinator.memory.registry_projection import (
        registry_projection_inputs,
    )

    cfg = tmp_path / "agents.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "id": "StaticBob",
                        "description": "static config agent",
                        "endpoint": "http://static-bob:10002",
                        "capabilities": ["sar"],
                    }
                ]
            }
        )
    )
    registry = AgentRegistry(static_config_path=cfg)
    # 注册前状态：registry 中已存在（静态预置）但从未拉卡成功。
    assert registry.contains("StaticBob") is True
    assert registry.get("StaticBob").agent_card_digest is None
    # server WS 分支同款首次注册判定逻辑（M2）：
    # prev is None or prev.agent_card_digest is None → True。
    prev = registry.get("StaticBob")
    assert (prev is None or prev.agent_card_digest is None) is True

    # 首次成功 WS 注册拉卡：覆盖为 card-available，状态转换完成。
    agent = registry.register_from_agent_card(
        "StaticBob", "http://static-bob:10002", _CARD_DUPLICATE_CAPS
    )
    assert agent.agent_card_available is True
    assert agent.agent_card_digest is not None
    assert agent.capabilities == ["navigation", "sar"]
    inputs = registry_projection_inputs(
        agent_id=agent.agent_id,
        capabilities=agent.capabilities,
        sensor_types=agent.sensor_types,
        card_available=agent.agent_card_available,
        scope_id="scope",
    )
    assert [inp.field_name for inp in inputs] == ["capability"]
    assert inputs[0].value == ["navigation", "sar"]

    # 重连（digest 已非 None）：首次注册判定为 False → 零摄入路径（D2）。
    assert (agent.agent_card_digest is None) is False
