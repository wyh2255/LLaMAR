"""Phase 0（P0）契约卡：DecisionEventV1 决策事件写入契约（主方案 §3.1 / D1-D2 / A3）。

目标契约：
- event_type 恰为五种 ``coordinator_decision.{assign_task, cancel_task,
  reply_to_help, activate_plan_node, update_plan}``；else 泛型 send_message
  不进 canonical（D1）；
- payload 字段：assign_task = content 全文 + who + correlation_id +
  worker_task_id；cancel_task / activate_plan_node = related_task_id；
  reply_to_help = related_task_id + response_preview = content[:200]（D2，
  与 logs 同口径）；update_plan = 声明式提交摘要（logical_id/participants/
  deps/objective，A3：前后对比在 tool_start 拿不到，只记提交摘要）；
- actor_id = "Coordinator"；env_step（tool_start 时刻 barrier._step_counter，
  探索 01 §7）进 payload JSON；
- 幂等复用 canonical JSON SHA-256 + idempotency_ledger claim（探索 01 §2）；
- 写入前显式套 RedactionPolicy 脱敏（R3 / 探索 01 §6：现有 _log_send_message
  写 logs 原文无脱敏，P1 补 canonical 写入时必须过 RedactionPolicy）；
- 零 DDL：temporal_event 无 event_type CHECK（store.py:138）、payload 为
  自由 TEXT JSON（store.py:134-155）。

当前代码事实（32bfe57）：
- contracts.py 无 ``DecisionEventV1`` → ImportError（预期 RED）；
- ingestor.py 无 ``coordinator_decision_idempotency_key`` → ImportError（预期 RED）；
- ``append_temporal_event`` / ``idempotency_ledger`` / ``RedactionPolicy`` /
  ``canonical_json_bytes`` 已存在 → GREEN 守护（P1 复用，不得破坏）。

P1 实现后转 GREEN。未来 API 命名（DTO 字段 / 方法签名）待 P1 确认。
"""

from __future__ import annotations

import json

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    MemoryContractError,
    canonical_json_bytes,
    digest_bytes,
)
from a2a.coordinator.memory.ingestor import MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


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


# ---------------------------------------------------------------------------
# GREEN 守护 —— P1 复用的既有通道机制（现在成立，P1 不得破坏）
# ---------------------------------------------------------------------------


def test_temporal_event_table_accepts_decision_event_type_zero_ddl(store, scope_id):
    """零 DDL（§3.1 / 探索 01 §2）：temporal_event.event_type 无 CHECK 约束
    （store.py:138），append_temporal_event 必须能原样落盘任意新类型。

    GREEN 守护：P1 只加新取值、不动表结构——本测试在 P1 前后都必须保持 GREEN。
    """
    _append_raw_event(store, scope_id, "coordinator_decision.assign_task", sequence=1)
    events = store.temporal_events(scope_id)
    assert [e["event_type"] for e in events] == ["coordinator_decision.assign_task"]
    assert json.loads(events[0]["payload"]) == {}


def test_idempotency_ledger_claim_mechanism_available(store, scope_id):
    """幂等机制（§3.1 / 探索 01 §2）：idempotency_ledger 已可承载决策事件的
    canonical JSON SHA-256 claim（store.py:746-760 写 / 963-972 读）。

    GREEN 守护：P1 的 coordinator_decision_idempotency_key 复用此机制。
    """
    key = digest_bytes(
        canonical_json_bytes(
            ["coordinator_decision", scope_id, "coordinator-dispatch-1", "task text"]
        )
    )
    store.insert_idempotency_ledger(
        scope_id=scope_id,
        idempotency_key=key,
        event_id="evt_1",
        payload_digest=key,
        receipt_sha256="r" * 64,
        committed_revision=1,
    )
    receipt = store.idempotency_receipt(scope_id, key)
    assert receipt is not None
    assert receipt["event_id"] == "evt_1"
    assert receipt["payload_digest"] == key


def test_canonical_json_sha256_primitives_available():
    """决策事件幂等键的原料（§3.1）：canonical JSON SHA-256 原语已存在。

    GREEN 守护：P1 幂等键 = digest_bytes(canonical_json_bytes(...))
    （contracts.py:308-324，探索 01 §2 同源）。
    """
    payload = {
        "event_type": "coordinator_decision.assign_task",
        "correlation_id": "coordinator-dispatch-1",
    }
    digest = digest_bytes(canonical_json_bytes(payload))
    assert len(digest) == 64
    assert digest == digest_bytes(canonical_json_bytes(payload))  # 确定性
    assert digest != digest_bytes(
        canonical_json_bytes({**payload, "correlation_id": "coordinator-dispatch-2"})
    )


def test_redaction_policy_redacts_secret_from_dispatch_text():
    """脱敏（§3.1 / R3 / 探索 01 §6）：RedactionPolicy(secret=...) 必须能过滤
    dispatch 文本中的精确 secret——P1 写入 canonical 前必须显式套用。

    GREEN 守护：机制已存在（redaction.py:20-49）；普通决策文本（坐标/名字）
    不得被误伤（探索 01 §6 误伤评估）。
    """
    policy = RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1")
    text = "assign Alice to investigate fire at (3,2) SUPERSECRET_VALUE_9f2c1"
    sanitized = policy.sanitize_event(text)
    assert "SUPERSECRET_VALUE_9f2c1" not in sanitized
    assert "fire at (3,2)" in sanitized  # 坐标/普通指令不被误伤


# ---------------------------------------------------------------------------
# RED —— DecisionEventV1（P1 在 contracts.py 新增，现不存在）
# ---------------------------------------------------------------------------


def test_decision_event_v1_import_and_five_event_kinds():
    """§3.1 / D1：DecisionEventV1 必须接受恰好五种 event_type。

    当前 contracts.py 无 DecisionEventV1 → ImportError（预期 RED）。
    API 路径待 P1 确认（DTO 挂 contracts.py，与 MemoryScopeV1 同层）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    assert DecisionEventV1.EVENT_TYPES == frozenset(
        {
            "coordinator_decision.assign_task",
            "coordinator_decision.cancel_task",
            "coordinator_decision.reply_to_help",
            "coordinator_decision.activate_plan_node",
            "coordinator_decision.update_plan",
        }
    )
    minimal = {
        "coordinator_decision.assign_task": {
            "content": "t",
            "who": "w",
            "correlation_id": "c",
            "worker_task_id": "d",
        },
        "coordinator_decision.cancel_task": {"related_task_id": "t"},
        "coordinator_decision.reply_to_help": {"content": "r", "related_task_id": "t"},
        "coordinator_decision.activate_plan_node": {"related_task_id": "n"},
        "coordinator_decision.update_plan": {
            "plan_nodes": [
                {"logical_id": "n1", "participants": [], "deps": [], "objective": "o"}
            ]
        },
    }
    for event_type, fields in minimal.items():
        evt = DecisionEventV1(
            event_type=event_type, actor_id="Coordinator", env_step=3, **fields
        ).validate()
        assert evt.event_type == event_type


def test_assign_task_payload_full_content_who_correlation_worker_task():
    """§3.1 / D2：assign_task payload = content 全文 + who + correlation_id +
    worker_task_id + env_step（content 不截断——与 reply_to_help 的 [:200]
    口径区分）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    content = "investigate fire at (3,2) and report casualties — " + "x" * 300
    evt = DecisionEventV1(
        event_type="coordinator_decision.assign_task",
        actor_id="Coordinator",
        env_step=7,
        content=content,
        who="Alice",
        correlation_id="coordinator-dispatch-1",
        worker_task_id="dispatch-1",
    ).validate()
    assert evt.canonical_payload() == {
        "content": content,
        "who": "Alice",
        "correlation_id": "coordinator-dispatch-1",
        "worker_task_id": "dispatch-1",
        "env_step": 7,
    }


def test_cancel_task_payload_related_task_id():
    """§3.1：cancel_task payload = related_task_id + env_step。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    evt = DecisionEventV1(
        event_type="coordinator_decision.cancel_task",
        actor_id="Coordinator",
        env_step=7,
        related_task_id="t-9",
    ).validate()
    assert evt.canonical_payload() == {"related_task_id": "t-9", "env_step": 7}


def test_reply_to_help_payload_preview_is_content_prefix_200():
    """§3.1 / D2：reply_to_help payload = related_task_id + response_preview，
    且 response_preview = content[:200]（与 logs 同口径）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    content = "the fire is at (3,2) and spreading — " + "y" * 500
    evt = DecisionEventV1(
        event_type="coordinator_decision.reply_to_help",
        actor_id="Coordinator",
        env_step=7,
        content=content,
        related_task_id="t-9",
    ).validate()
    payload = evt.canonical_payload()
    assert payload == {
        "related_task_id": "t-9",
        "response_preview": content[:200],
        "env_step": 7,
    }
    assert len(payload["response_preview"]) == 200  # 截断口径钉死


def test_activate_plan_node_payload_related_task_id():
    """§3.1 / D1：activate_plan_node（DAG 节点激活）payload = related_task_id +
    env_step；participants/objective 由 MissionGraph 声明读取，事件只记激活行为。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    evt = DecisionEventV1(
        event_type="coordinator_decision.activate_plan_node",
        actor_id="Coordinator",
        env_step=7,
        related_task_id="node-3",
    ).validate()
    assert evt.canonical_payload() == {"related_task_id": "node-3", "env_step": 7}


def test_update_plan_payload_declarative_commit_summary():
    """§3.1 / A3：update_plan 记声明式提交摘要（提交的 plan 节点列表：
    logical_id/participants/deps/objective），不做前后对比（diff 是
    MissionGraph.replace 执行结果，tool_start 拿不到，update_plan.py:67-90）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    nodes = [
        {"logical_id": "n1", "participants": ["Alice"], "deps": [], "objective": "reach fire"},
        {"logical_id": "n2", "participants": ["Bob"], "deps": ["n1"], "objective": "extinguish"},
    ]
    evt = DecisionEventV1(
        event_type="coordinator_decision.update_plan",
        actor_id="Coordinator",
        env_step=7,
        plan_nodes=nodes,
    ).validate()
    assert evt.canonical_payload() == {"plan_nodes": nodes, "env_step": 7}
    for node in evt.canonical_payload()["plan_nodes"]:
        assert {"logical_id", "participants", "deps", "objective"} <= set(node)


def test_actor_id_forced_to_coordinator():
    """§3.1：actor_id 固定 "Coordinator"——非 Coordinator 的 actor 一律拒绝。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.cancel_task",
            actor_id="Worker",
            env_step=7,
            related_task_id="t-9",
        ).validate()


def test_env_step_must_be_int():
    """§3.1 / 探索 01 §7：env_step = tool_start 时刻 barrier._step_counter
    （int），非 int 一律拒绝；env_step 落在 payload JSON（表无此列）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.cancel_task",
            actor_id="Coordinator",
            env_step="7",
            related_task_id="t-9",
        ).validate()


def test_generic_send_message_never_enters_canonical():
    """§3.1 / D1：else 泛型 send_message 不进 canonical——DTO 必须拒绝任何
    非五种 event_type（含 coordinator_decision.send_message 及裸 send_message）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.send_message",
            actor_id="Coordinator",
            env_step=7,
        ).validate()
    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="send_message",
            actor_id="Coordinator",
            env_step=7,
        ).validate()


def test_assign_task_requires_dispatch_fields():
    """§3.1：assign_task 必须携带 content/who/correlation_id/worker_task_id
    （缺一即 fail closed）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.assign_task",
            actor_id="Coordinator",
            env_step=7,
        ).validate()


def test_cancel_reply_activate_require_related_task_id():
    """§3.1：cancel_task / reply_to_help / activate_plan_node 必须携带
    related_task_id；reply_to_help 还必须有 content（response_preview 原料）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import DecisionEventV1

    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.cancel_task",
            actor_id="Coordinator",
            env_step=7,
        ).validate()
    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.reply_to_help",
            actor_id="Coordinator",
            env_step=7,
            content="x",  # 缺 related_task_id
        ).validate()
    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.reply_to_help",
            actor_id="Coordinator",
            env_step=7,
            related_task_id="t-9",  # 缺 content
        ).validate()
    with pytest.raises(MemoryContractError):
        DecisionEventV1(
            event_type="coordinator_decision.activate_plan_node",
            actor_id="Coordinator",
            env_step=7,
        ).validate()


def test_coordinator_decision_idempotency_key_contract():
    """§3.1：幂等键复用 canonical JSON SHA-256 + idempotency_ledger claim
    （探索 01 §8.3 建议 ingestor 新增 coordinator_decision_idempotency_key，
    仿 ingestor.py:43-95）。

    行为契约：64-hex、确定性、对 scope 与 content 敏感。
    当前 ingestor.py 无此函数 → ImportError（预期 RED）；签名待 P1 确认。
    """
    from a2a.coordinator.memory.ingestor import coordinator_decision_idempotency_key

    key = coordinator_decision_idempotency_key(
        scope_id="s1",
        event_type="coordinator_decision.assign_task",
        correlation_id="coordinator-dispatch-1",
        worker_task_id="dispatch-1",
        content="investigate fire at (3,2)",
    )
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
    assert key == coordinator_decision_idempotency_key(
        scope_id="s1",
        event_type="coordinator_decision.assign_task",
        correlation_id="coordinator-dispatch-1",
        worker_task_id="dispatch-1",
        content="investigate fire at (3,2)",
    )  # 确定性
    assert key != coordinator_decision_idempotency_key(
        scope_id="s1",
        event_type="coordinator_decision.assign_task",
        correlation_id="coordinator-dispatch-1",
        worker_task_id="dispatch-1",
        content="different content",
    )  # content 敏感
    assert key != coordinator_decision_idempotency_key(
        scope_id="s2",
        event_type="coordinator_decision.assign_task",
        correlation_id="coordinator-dispatch-1",
        worker_task_id="dispatch-1",
        content="investigate fire at (3,2)",
    )  # scope 敏感
