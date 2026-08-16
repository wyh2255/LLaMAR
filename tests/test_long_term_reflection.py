"""Phase 0（P0）契约卡：反思 source window + 原子快照（主方案 RED contract #5
+ 原子快照补充）。

目标契约：
- 反思输入只取已提交 canonical ``callback.*`` / ``evidence.projection`` /
  ``supervision.*`` / ``control.*`` 增量窗口；拒绝 legacy EventStore、CSV、
  truth artifact、barrier、raw LLM trace（D7）；
- ``ScopeEventSnapshotV1(scope_id, memory_revision, events, snapshot_digest)``
  在同一锁与同一 SQLite read 临界区内读取，不调用两个独立 read helper 拼接；
  snapshot 只读、不修改任何表；unknown scope 返回 typed 失败；close 后仍可读；
  读取异常返回 typed failure，长期 store 零内容写入（原子快照补充 §2）；
- ``reflection_run`` 幂等键绑定 project_id + source_scope_id +
  snapshot.memory_revision + snapshot.snapshot_digest + policy_version；同一
  snapshot 重试零重复持久化（§2.5）；
- 窗口游标 ``window_end_sequence`` 只在 completed 推进（§3.4.3）；
- fail-closed：伪造 source ref、truth term（FORBIDDEN_TRUTH_TERMS）、缺
  function-call、重复 memory_key 一律 rejected，零 long_term_memory 写入；
- 模型调用/网络/export 绝不在 SQLite BEGIN IMMEDIATE 内（§3.3）；
- 滚动反思异步执行（coalesce），terminal 收尾 join 带超时（long_term.config
  [timeout] reflection_sec，默认 60s），超时写 typed timeout 不阻塞 run 退出。

当前代码事实（5413705）：
- ``MemoryStore.scope_event_snapshot`` 不存在 → AttributeError（预期 RED）；
- ``a2a.coordinator.memory.reflection`` / ``sar_orch.long_term_reflection``
  不存在 → ImportError（预期 RED）；
- 现有 ingestor 已保证 control 事件 payload 只含 digest（无 raw LLM trace）、
  allowlist/truth scan 已就绪 → GREEN 守护。

Phase 1（reflection DTO/collector 骨架）与 Phase 4（snapshot + 触发）实现后转 GREEN。
"""

from __future__ import annotations

import json

import pytest

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    ONLINE_PROVENANCE_ALLOWLIST,
    ControlTransitionJournalEntry,
    MemoryConfig,
    scan_forbidden_truth_fields,
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


def _scope_id_of(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


def _seed_control_event(ingestor, store, scope_factory) -> str:
    """写入一条 control.* 事件（反思输入锚点），返回 scope_id。"""
    scope_id = _scope_id_of(scope_factory)
    entry = ControlTransitionJournalEntry.build(
        context_id="ctx-1",
        runtime_epoch=0,
        dispatch_id="dsp_1",
        control_revision=1,
        previous_state="DISPATCHING",
        state="RUNNING",
        source="dispatch",
        observed_at="2026-08-12T00:00:00+00:00",
        result={"action": "dispatch_task"},
    )
    result = ingestor.ingest_control_receipt(entry)
    assert result.status == "ok"
    return scope_id


# ---------------------------------------------------------------------------
# GREEN 守护 —— 反思输入的在线隔离基础（P4 必须复用）
# ---------------------------------------------------------------------------


def test_reflection_input_sources_are_allowlisted():
    """反思四类事件来源（callback.* / evidence.projection / supervision.* /
    control.*）全部来自 ONLINE_PROVENANCE_ALLOWLIST（主方案 §3.4.2）。

    GREEN 守护：P4 source collector 输入白名单 = allowlist。
    """
    assert {"control", "supervision"} <= set(ONLINE_PROVENANCE_ALLOWLIST)


def test_forbidden_truth_scan_detects_truth_terms():
    """现有 scan_forbidden_truth_fields 能检出 oracle/ground_truth 掩码值。

    GREEN 守护：P4 反思 validator 的 truth scan 复用此函数（contracts.py:94-115）。
    """
    assert scan_forbidden_truth_fields({"note": "matches ground_truth"})
    assert scan_forbidden_truth_fields({"oracle_score": 1.0})
    assert not scan_forbidden_truth_fields({"position": [1, 2, 0]})


def test_control_event_payload_contains_only_digests_not_raw_llm(
    ingestor, store, scope_factory
):
    """control.* canonical 事件 payload 只有 journal_sha256/result_digest，
    绝不含 raw LLM reasoning/CoT（ingestor.py:565-570）。

    GREEN 守护：这证明“决策原文不进 canonical”——P4 反思不能从 canonical
    读取 raw LLM trace，V1 只消费结构化 control 证据（§3.4.6）。
    """
    scope_id = _seed_control_event(ingestor, store, scope_factory)
    events = store.temporal_events(scope_id)
    control_events = [e for e in events if e["event_type"].startswith("control.")]
    assert control_events
    payload = json.loads(control_events[0]["payload"])
    assert set(payload.keys()) == {"journal_sha256", "result_digest"}
    raw = json.dumps(payload)
    assert "dispatch_task" not in raw  # result 原文不落 canonical


def test_legacy_event_store_is_not_canonical_source(ingestor, store, scope_factory):
    """legacy EventStore 与 canonical Temporal 是两套独立存储；canonical
    temporal_events 只返回已提交 canonical 事件（探索 03 §8.1）。

    GREEN 守护：P4 反思只读 canonical（store.temporal_events），不接 EventStore。
    """
    scope_id = _seed_control_event(ingestor, store, scope_factory)
    events = store.temporal_events(scope_id)
    assert events
    # canonical Temporal 只含四类事件前缀；不存在 oracle/truth/barrier 事件
    assert all(
        e["event_type"].startswith(("control.", "callback.", "evidence.", "supervision."))
        for e in events
    )


# ---------------------------------------------------------------------------
# RED —— ScopeEventSnapshotV1（Phase 4 在 MemoryStore 新增，现不存在）
# ---------------------------------------------------------------------------


def test_scope_event_snapshot_api_exists(store, scope_factory):
    """原子快照补充 §2：``scope_event_snapshot()`` 返回
    ScopeEventSnapshotV1(scope_id, memory_revision, events, snapshot_digest)。

    当前 MemoryStore 无此方法 → AttributeError（预期 RED）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    snapshot = store.scope_event_snapshot(scope_id)  # AttributeError
    assert snapshot.scope_id == scope_id
    assert isinstance(snapshot.memory_revision, int)
    assert isinstance(snapshot.events, tuple)
    assert len(snapshot.snapshot_digest) == 64


def test_snapshot_consistent_under_interleaved_writer(ingestor, store, scope_factory):
    """原子快照补充 §3.1：reader 取得 snapshot 前后交错一个 callback/projection
    writer；反思器收到的 revision/events/digest 必须来自一个真实的一致快照，
    而非混合状态（不得先 revision_of 再 temporal_events 拼接）。

    当前方法不存在 → AttributeError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import NormalizedProjectionInputV1

    scope_id = _seed_control_event(ingestor, store, scope_factory)
    before = store.scope_event_snapshot(scope_id)  # AttributeError（预期 RED）
    # 交错 writer：两次 snapshot 之间写入一条新的 projection 事件
    ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_interleaved",
                sequence=0,
                env_step=9,
                actor_id="alice",
                provenance="worker_telemetry",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="position",
                value=[1, 2, 0],
            )
        ]
    )
    after = store.scope_event_snapshot(scope_id)
    # 一致快照契约：before 快照冻结交错写入前的世界（后续写不得改变其内容，
    # 否则是活游标而非一致快照）；after 快照包含新事件且 revision/digest 随
    # 内容单调前进。任一快照都不是"revision 已含新事件但 events 未含"的混合态。
    before_ids = [e["event_id"] for e in before.events]
    before_seqs = [e["sequence"] for e in before.events]
    assert before_seqs == sorted(before_seqs)  # sequence 升序
    assert "evt_interleaved" not in before_ids
    # P0 冻结修正（父侧裁决 2026-08-12）：canonical temporal event_id 由
    # ingestor 系统生成（ingestor.py:766 ``new_event_id("evt")``），输入
    # evidence id 落在 ``causation_id=f"evidence:{event_id}"``（ingestor.py:797）
    # ——原断言 ``"evt_interleaved" in {event_id...}`` 与冻结 ingestor 契约矛盾
    # （P0 阶段该测试为 AttributeError RED，可满足性未验证）。修正为
    # causation_id 匹配，快照一致性语义不变。
    assert any(
        e.get("causation_id") == "evidence:evt_interleaved" for e in after.events
    )
    assert after.memory_revision >= before.memory_revision
    assert after.snapshot_digest != before.snapshot_digest  # 内容变化 → digest 变化


def test_snapshot_readonly_no_mutation(store, scope_factory):
    """原子快照补充 §2.2：snapshot 不修改 scope/revision/event/outbox/
    projection/security audit；不得使用 BEGIN IMMEDIATE。

    当前方法不存在 → AttributeError（预期 RED）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    before_revision = store.revision_of(scope_id)
    before_events = len(store.temporal_events(scope_id))
    store.scope_event_snapshot(scope_id)  # AttributeError
    assert store.revision_of(scope_id) == before_revision
    assert len(store.temporal_events(scope_id)) == before_events


def test_snapshot_unknown_scope_typed_failure(store):
    """原子快照补充 §2.3：unknown scope 返回 typed ``unknown_scope``（fail
    closed，不抛裸异常）。当前方法不存在 → AttributeError（预期 RED）。
    """
    result = store.scope_event_snapshot("no-such-scope")  # AttributeError
    assert result.status == "unknown_scope"


def test_snapshot_after_close_scope_still_readable(ingestor, store, scope_factory):
    """原子快照补充 §2.3：close scope 后 snapshot 仍可读（V1 不负责 close
    scope），且不改变 run-local memory_revision。

    当前方法不存在 → AttributeError（预期 RED）。
    """
    scope_id = _seed_control_event(ingestor, store, scope_factory)
    assert store.close_scope(scope_id)
    snapshot = store.scope_event_snapshot(scope_id)  # AttributeError
    assert snapshot.memory_revision == store.revision_of(scope_id)


def test_snapshot_read_error_typed_failure_and_zero_long_term_write(store, scope_factory):
    """原子快照补充 §2.3：读取异常返回 typed failure；长期 store 零内容写入
    （long_term_memory/support 均为零——该零写入由反思 hook 侧保证，见
    test_snapshot_error_zero_long_term_content_write；snapshot 结果本身不挂
    长期库状态字段）。

    当前方法不存在 → AttributeError（预期 RED）。
    """
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    result = store.scope_event_snapshot(scope_id, simulate_read_error=True)  # AttributeError
    assert result.status == "read_failed"


# ---------------------------------------------------------------------------
# RED —— ReflectionSourceCollector / ReflectionModelPort（Phase 1/4，现不存在）
# ---------------------------------------------------------------------------


def test_reflection_source_collector_import():
    """``ReflectionSourceCollector`` 只能接受 ScopeEventSnapshotV1，不得自己
    调用 revision_of()/temporal_events() 重建 source window（原子快照补充 §2.4）。

    当前模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    assert collector is not None


def test_collector_rejects_legacy_event_store_input():
    """D7：legacy EventStore 不得作为反思输入。当前不存在 → ImportError（预期 RED）；
    P1 实现后 accept_source 必须拒绝（抛 typed 错误或返回显式拒绝结果，no-op 即假绿）。
    """
    from a2a.coordinator.event_store import EventStore
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    with pytest.raises(Exception):  # noqa: B017 - 拒绝路径必须抛 typed 错误
        collector.accept_source(EventStore())


def test_collector_rejects_truth_manifest_input():
    """D7：truth artifact（truth manifest/trace）不得作为反思输入。
    当前不存在 → ImportError（预期 RED）；P1 实现后 accept_source 必须拒绝。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    with pytest.raises(Exception):  # noqa: B017 - 拒绝路径必须抛 typed 错误
        collector.accept_source({"truth_manifest": {"scope_id": "x"}})


def test_collector_rejects_barrier_input():
    """D7：Barrier/get_env_snapshot 不得作为反思输入（H1-INV-1）。
    当前不存在 → ImportError（预期 RED）；P1 实现后 accept_source 必须拒绝。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    with pytest.raises(Exception):  # noqa: B017 - 拒绝路径必须抛 typed 错误
        collector.accept_source({"type": "barrier", "env_snapshot": {}})


def test_collector_rejects_raw_llm_trace_input():
    """D7：raw LLM trace/reasoning 不得作为反思输入（V1 不读取 raw LLM
    reasoning，主方案 §3.4.6）。当前不存在 → ImportError（预期 RED）；
    P1 实现后 accept_source 必须拒绝。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    with pytest.raises(Exception):  # noqa: B017 - 拒绝路径必须抛 typed 错误
        collector.accept_source({"llm_trace": [{"role": "assistant", "content": "..."}]})


def test_collector_accepts_only_canonical_event_types():
    """事件过滤为 callback.* / evidence.projection / supervision.* /
    control.*（主方案 §3.4.2）；其余类型拒绝。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector()
    assert collector.accepts_event_type("callback.status_update")
    assert collector.accepts_event_type("evidence.projection")
    assert collector.accepts_event_type("supervision.worker_unreachable")
    assert collector.accepts_event_type("control.dispatch.RUNNING")
    assert not collector.accepts_event_type("oracle.snapshot")  # 拒绝


def test_collector_incremental_window_after_last_completed():
    """增量窗口：取上次 completed 反思的 window_end_sequence 之后的 sequence
    区间（首次为 run 开始）；窗口为空则跳过本次反思（§3.4.2）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector(window_end_sequence=10)
    # P0 冻结修正（父侧裁决 2026-08-12，同 209-217 先例）：M7 要求
    # event_type 缺失（None）fail closed——原 fixtures 只有 sequence 字段
    # （无 event_type），M7 语义下全部被拒 → 窗口恒空，无法测序列增量。
    # 补 allowlist 类型使 fixtures 成为 canonical 形状，断言（增量区间）
    # 不变。
    window = collector.collect(
        [
            {"sequence": s, "event_type": "callback.status_update"}
            for s in range(1, 21)
        ]
    )
    assert [e["sequence"] for e in window.events] == list(range(11, 21))


def test_collector_window_empty_skips_reflection():
    """窗口为空（上次 completed 的 window_end_sequence 已到最新）→ 跳过本次
    反思（§3.4.2）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector(window_end_sequence=20)
    window = collector.collect([{"sequence": s} for s in range(1, 21)])
    assert window.events == []


def test_collector_window_truncated_flag():
    """窗口上限（默认 ≤200 事件 / ≤8k 字符）：超出按 sequence 取最近并记
    truncated=true（D7）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        ReflectionSourceCollector,
    )

    collector = ReflectionSourceCollector(max_events=5, max_chars=8000)
    # P0 冻结修正（父侧裁决 2026-08-12，同 209-217 先例）：M7 event_type
    # fail closed 下原 sequence-only fixtures 全被拒 → 窗口恒空、truncated
    # 恒 False。补 allowlist 类型，截断断言不变。
    window = collector.collect(
        [
            {"sequence": s, "event_type": "callback.status_update"}
            for s in range(1, 101)
        ]
    )
    assert window.truncated is True
    assert len(window.events) <= 5


def test_window_cursor_advances_only_on_completed():
    """§3.4.3：window_end_sequence 只在 reflection_run 达到 completed 时推进；
    failed/rejected/timeout 不推进（下次触发重新覆盖该区间）。

    签名 ``advance_window_cursor(current, end, status)``：completed 推进到
    ``end``（本次窗口终点），其余状态保持 ``current`` 不变。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        advance_window_cursor,
    )

    assert advance_window_cursor(current=10, end=42, status="completed") == 42
    assert advance_window_cursor(current=10, end=42, status="failed") == 10
    assert advance_window_cursor(current=10, end=42, status="rejected") == 10
    assert advance_window_cursor(current=10, end=42, status="timeout") == 10


def test_reflection_model_port_protocol():
    """``ReflectionModelPort``：function-calling/严格 schema adapter（复用现有
    LLMClient + tools 栈，D8）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import ReflectionModelPort

    port = ReflectionModelPort(provider="openai", model="deepseek-v4-flash")
    assert port is not None


def test_reflection_requires_function_call_response():
    """缺 function-call 的模型响应一律 fail closed：零长期内容写入，不补造
    （主方案 2.1.6 / D6）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        validate_reflection_response,
    )

    response = {"content": "这里没有 function call", "finish_reason": "stop"}
    result = validate_reflection_response(response)
    assert result.status == "rejected"
    assert result.long_term_memory_written == 0


def test_forged_source_ref_rejected_fail_closed():
    """伪造 source ref（不在 input window 内 / scope-digest 不一致）→
    validator 拒绝，reflection_run.status=rejected，零 long_term_memory
    （主方案 §3.3）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        validate_reflection_response,
    )

    candidate = {
        "memory_key": "k1",
        "kind": "lesson",
        "statement": "coordinate at fires",
        "confidence": 0.9,
        "source_refs": [("scope-a", "evt-not-in-window")],  # 伪造 ref
    }
    result = validate_reflection_response({"function_call": candidate})
    assert result.status == "rejected"
    assert result.long_term_memory_written == 0


def test_truth_term_in_reflection_output_rejected():
    """反思产物含 FORBIDDEN_TRUTH_TERMS → fail closed（复用 contracts.py:72-91
    词表）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        validate_reflection_response,
    )

    candidate = {
        "memory_key": "k2",
        "kind": "status",
        "statement": f"simulator says {min(FORBIDDEN_TRUTH_TERMS)}",
        "confidence": 0.9,
        "source_refs": [("scope-a", "evt-1")],
    }
    result = validate_reflection_response({"function_call": candidate})
    assert result.status == "rejected"
    assert result.long_term_memory_written == 0


def test_duplicate_memory_key_in_same_response_rejected():
    """同一响应内重复 memory_key → rejected（主方案 §3.3）。
    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        validate_reflection_response,
    )

    response = {
        "function_call": [
            {"memory_key": "k1", "kind": "lesson", "statement": "a", "confidence": 0.9, "source_refs": []},
            {"memory_key": "k1", "kind": "lesson", "statement": "b", "confidence": 0.9, "source_refs": []},
        ]
    }
    result = validate_reflection_response(response)
    assert result.status == "rejected"
    assert result.long_term_memory_written == 0


def test_same_snapshot_retry_single_persistence(tmp_path):
    """原子快照补充 §3.2：同一 snapshot 反思 hook（滚动或终结）重试两次，
    只产生一条 reflection_run 与一次 candidate/support persistence。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = {
        "scope_id": "run-a",
        "memory_revision": 7,
        "snapshot_digest": "d" * 64,
        # P0 冻结修正（父侧裁决 2026-08-12，同 209-217 先例）：契约 §3.4.2
        # 要求空窗口跳过本次反思（不 claim、零 reflection_run 行）——原构造
        # 的 dict 无 events 即空窗口，与同步路径空窗口跳过语义（M1）冲突。
        # 补一条 control.* 事件使窗口非空：第一次调用 claim→completed 并把
        # 窗口游标推进到 sequence=1；第二次调用显式 pin 同一窗口
        # （window_end_sequence=0，等价于游标未动的重试——completed 后游标
        # 已推进，相同调用再触发只会得到空窗口 skipped），命中 claim 幂等键
        # → duplicate，幂等断言保持不变。
        "events": [
            {
                "sequence": 1,
                "scope_id": "run-a",
                "event_id": "e1",
                "event_type": "control.dispatch.RUNNING",
            }
        ],
    }
    run_reflection(store, snapshot, policy_version=1)
    second = run_reflection(store, snapshot, policy_version=1, window_end_sequence=0)
    # 幂等形状与 store 层冻结一致（test_long_term_memory_store.py：
    # claim_reflection_run 重复返回 status == "duplicate"）
    assert second.status == "duplicate"
    assert len(store.list_reflection_runs(project_id="llamar", scope_id="run-a")) == 1


def test_snapshot_error_zero_long_term_content_write(tmp_path):
    """原子快照补充 §3.3：snapshot 读取期间人为抛错 → reflection_run 可记录
    失败诊断，但 long_term_memory/support 均为零。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    result = run_reflection(store, snapshot=None, policy_version=1)  # 快照失败
    assert result.status == "failed"
    assert store.list_memories(project_id="llamar", scope_id="any") == []


def test_no_llm_call_inside_db_transaction(tmp_path):
    """模型调用、网络、export 不得落在 SQLite BEGIN IMMEDIATE 内（主方案
    §3.3 最后一条）：``reflection_write_transaction`` 是短写事务，进入事务体
    即已持有锁（lock_held True），LLM/网络调用必须发生在进入事务之前。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.reflection import (
        reflection_write_transaction,
    )

    with reflection_write_transaction(store_path=str(tmp_path / "long_term.sqlite3")) as tx:
        assert tx.lock_held is True  # 事务体内锁已持有（短事务，LLM 调用不在其中）


def test_sar_long_term_reflection_module_import():
    """``sar_orch.long_term_reflection``（Phase 4）：SAR terminal wiring 与
    provider/model config adapter；``reflection_run`` 触发。当前不存在 →
    ImportError（预期 RED）。
    """
    from sar_orch.long_term_reflection import (
        reflection_run,
    )

    assert callable(reflection_run)


def test_terminal_reflection_joins_inflight_with_timeout():
    """§3.4.1：terminal 收尾先 drain 进行中的滚动反思（join 带超时，默认 60s）；
    超时写 typed timeout status 且不阻塞 run 退出。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.long_term_reflection import (
        drain_inflight_reflection,
    )

    result = drain_inflight_reflection(timeout_sec=60)
    assert result.status in ("joined", "timeout")


def test_rolling_reflection_async_coalesce():
    """§3.4.1：滚动反思异步执行；触发时已有进行中反思则 coalesce 跳过。
    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.long_term_reflection import (
        maybe_trigger_rolling_reflection,
    )

    assert maybe_trigger_rolling_reflection() in ("started", "coalesced_skip")


# ---------------------------------------------------------------------------
# P4 追加 —— 离线 model-port 路径 / 滚动异步触发 / terminal drain（文件末尾追加，
# P0 断言零改动）
# ---------------------------------------------------------------------------


class _FakeModelPort:
    """P4 离线验证用假模型 port：记录调用并返回预设 function-call 响应。

    绝不发起真实网络/LLM 调用；``calls`` 记录 (system_prompt, user_prompt,
    tools) 以便断言模型调用发生在任何 SQLite 事务之外。
    """

    def __init__(self, response):
        self._response = response
        self.calls = []

    def complete_with_function_call(self, *, system_prompt, user_prompt, tools):
        self.calls.append((system_prompt, user_prompt, tools))
        return self._response


def _snapshot_with_events(scope_id="run-a", revision=7, digest=None):
    from a2a.coordinator.memory.store import ScopeEventSnapshotV1

    return ScopeEventSnapshotV1(
        scope_id=scope_id,
        memory_revision=revision,
        events=(
            {
                "event_id": "e1",
                "scope_id": scope_id,
                "sequence": 1,
                "event_type": "control.dispatch.RUNNING",
            },
            {
                "event_id": "e2",
                "scope_id": scope_id,
                "sequence": 2,
                "event_type": "callback.status_update",
            },
        ),
        snapshot_digest=digest or ("d" * 64),
    )


def _valid_response():
    return {
        "function_call": [
            {
                "memory_key": "k1",
                "kind": "lesson",
                "statement": "coordinate at fires",
                "confidence": 0.9,
                "source_refs": [["run-a", "e1"]],
            },
            {
                "memory_key": "k2",
                "kind": "hazard",
                "statement": "avoid water at fires",
                "confidence": 0.8,
                "source_refs": [["run-a", "e2"]],
            },
        ]
    }


def test_run_reflection_model_port_publishes_validated_candidates(tmp_path):
    """离线 model-port 路径：验证通过的候选同 run 内直接 published（系统派生
    memory_key、证据链绑定、reflection_run completed、窗口游标推进）。"""
    from a2a.coordinator.memory.contracts import derive_memory_key
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    port = _FakeModelPort(_valid_response())

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "completed"
    assert result.long_term_memory_written == 2
    assert len(port.calls) == 1  # 模型调用一次，且发生在事务之外
    memories = store.list_memories(project_id="llamar", scope_id="run-a")
    assert len(memories) == 2
    assert {m["status"] for m in memories} == {"published"}
    # 存储 key 是系统派生，绝不采用模型提供的 k1/k2
    expected_keys = {
        derive_memory_key(
            project_id="llamar",
            kind=m["kind"],
            policy_version=1,
            statement=m["statement"],
        )
        for m in memories
    }
    assert expected_keys == {m["memory_key"] for m in memories}
    assert "k1" not in {m["memory_key"] for m in memories}
    # 证据链可重放
    lesson = next(m for m in memories if m["kind"] == "lesson")
    refs = store.list_support_refs(
        project_id="llamar", scope_id="run-a", memory_key=lesson["memory_key"]
    )
    assert ("run-a", "e1") in refs
    # 窗口游标推进到窗口终点（max sequence=2）
    assert (
        store.window_end_sequence(project_id="llamar", scope_id="run-a") == 2
    )
    runs = store.list_reflection_runs(project_id="llamar", scope_id="run-a")
    assert len(runs) == 1 and runs[0]["status"] == "completed"


def test_run_reflection_same_snapshot_with_model_port_is_duplicate(tmp_path):
    """带 model-port 的同一 snapshot 重试仍 exactly-once：第二次 duplicate，
    零新增 reflection_run / memory。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    port = _FakeModelPort(_valid_response())

    first = run_reflection(store, snapshot, policy_version=1, model_port=port)
    # M1 语义（父侧裁决同 506-527）：completed 后窗口游标推进，相同调用再
    # 触发只会得到空窗口 skipped；显式 pin 同一窗口（window_end_sequence=0）
    # 等价于游标未动的重试，命中 claim 幂等键 → duplicate。断言不变。
    second = run_reflection(
        store, snapshot, policy_version=1, model_port=port, window_end_sequence=0
    )

    assert first.status == "completed"
    assert second.status == "duplicate"
    assert len(store.list_reflection_runs(project_id="llamar", scope_id="run-a")) == 1
    assert len(store.list_memories(project_id="llamar", scope_id="run-a")) == 2


def test_run_reflection_rejected_response_zero_content_write(tmp_path):
    """缺 function-call 的模型响应 → reflection_run=rejected + audit，零
    long_term_memory 写入（fail closed）。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    port = _FakeModelPort({"content": "no function call here"})

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "rejected"
    assert result.long_term_memory_written == 0
    assert store.list_memories(project_id="llamar", scope_id="run-a") == []
    runs = store.list_reflection_runs(project_id="llamar", scope_id="run-a")
    assert len(runs) == 1 and runs[0]["status"] == "rejected"
    audits = store.list_audit_entries()
    assert any(a["kind"] == "reflection_rejected" for a in audits)


def test_run_reflection_same_key_same_digest_zero_write_across_snapshots(tmp_path):
    """跨 snapshot 的相同语句（同 key 同 digest）→ publish duplicate，零新增行
    （§3.4.5 幂等）。第二个 snapshot 带新事件使增量窗口非空。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection
    from a2a.coordinator.memory.store import ScopeEventSnapshotV1

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    response_a = {
        "function_call": [
            {
                "memory_key": "k1",
                "kind": "lesson",
                "statement": "coordinate at fires",
                "confidence": 0.9,
                "source_refs": [["run-a", "e1"]],
            }
        ]
    }
    snapshot_a = _snapshot_with_events(revision=7, digest="a" * 64)
    snapshot_b = ScopeEventSnapshotV1(
        scope_id="run-a",
        memory_revision=8,
        events=snapshot_a.events
        + (
            {
                "event_id": "e3",
                "scope_id": "run-a",
                "sequence": 3,
                "event_type": "supervision.worker_unreachable",
            },
        ),
        snapshot_digest="b" * 64,
    )
    response_b = {
        "function_call": [
            {
                "memory_key": "k1",
                "kind": "lesson",
                "statement": "coordinate at fires",
                "confidence": 0.9,
                "source_refs": [["run-a", "e3"]],
            }
        ]
    }

    first = run_reflection(store, snapshot_a, 1, model_port=_FakeModelPort(response_a))
    second = run_reflection(store, snapshot_b, 1, model_port=_FakeModelPort(response_b))

    assert first.status == "completed" and first.long_term_memory_written == 1
    assert second.status == "completed" and second.long_term_memory_written == 0
    assert len(store.list_memories(project_id="llamar", scope_id="run-a")) == 1


def test_run_reflection_usage_audit_records_tokens(tmp_path):
    """模型响应携带 usage.total_tokens → reflection_usage audit（quality
    evaluator 的 token 统计通道）。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    response = _valid_response()
    response["usage"] = {"total_tokens": 1234}
    port = _FakeModelPort(response)

    result = run_reflection(store, _snapshot_with_events(), 1, model_port=port)

    assert result.status == "completed"
    audits = store.list_audit_entries()
    assert any(a["kind"] == "reflection_usage" and "tokens=1234" in a["reason"] for a in audits)


def test_collector_accepts_snapshot_and_builds_incremental_window():
    """ReflectionSourceCollector.accept_source 只接受 ScopeEventSnapshotV1；
    collect 结果 window_end_sequence = 窗口终点。"""
    from a2a.coordinator.memory.reflection import ReflectionSourceCollector
    from a2a.coordinator.memory.store import ScopeEventSnapshotV1

    snapshot = ScopeEventSnapshotV1(
        scope_id="run-a",
        memory_revision=3,
        events=(
            {"event_id": "e1", "scope_id": "run-a", "sequence": 1, "event_type": "control.dispatch.RUNNING"},
            {"event_id": "e2", "scope_id": "run-a", "sequence": 2, "event_type": "callback.status_update"},
        ),
        snapshot_digest="d" * 64,
    )
    collector = ReflectionSourceCollector(window_end_sequence=1)
    collector.accept_source(snapshot)
    window = collector.collect(snapshot.events)
    assert [e["sequence"] for e in window.events] == [2]
    assert window.window_end_sequence == 2
    assert window.truncated is False


@pytest.fixture
def lt_runtime_module(tmp_path):
    """模块级滚动运行时：fake long-term store + 可控 snapshot provider。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from sar_orch.long_term_reflection import _reset_runtime

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    _reset_runtime()
    yield store
    _reset_runtime()


def test_rolling_async_coalesce_blocks_reentry(lt_runtime_module):
    """进行中的滚动反思 → 新触发 coalesce 跳过（min_interval=0 排除节流干扰）。"""
    import threading

    from a2a.coordinator.memory.contracts import LongTermRuntimeConfig
    from sar_orch.long_term_reflection import (
        configure_long_term_runtime,
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    gate = threading.Event()

    def slow_snapshot():
        gate.wait(timeout=10)
        return _snapshot_with_events()

    configure_long_term_runtime(
        store=lt_runtime_module,
        snapshot_provider=slow_snapshot,
        project_id="llamar",
        model_port=_FakeModelPort(_valid_response()),
        config=LongTermRuntimeConfig(min_interval_sec=0),
    )
    assert maybe_trigger_rolling_reflection() == "started"
    # worker 仍在进行（被 gate 阻塞）→ 第二次触发必须 coalesce 跳过
    assert maybe_trigger_rolling_reflection() == "coalesced_skip"
    gate.set()
    drain = drain_inflight_reflection(timeout_sec=10)
    assert drain.status == "joined"


def test_terminal_drain_timeout_does_not_block(lt_runtime_module):
    """drain join 超时返回 timeout 且不阻塞（线程仍在运行，之后可再次 join）。"""
    import threading

    from a2a.coordinator.memory.contracts import LongTermRuntimeConfig
    from sar_orch.long_term_reflection import (
        configure_long_term_runtime,
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    gate = threading.Event()

    def blocked_snapshot():
        gate.wait(timeout=30)
        return _snapshot_with_events()

    configure_long_term_runtime(
        store=lt_runtime_module,
        snapshot_provider=blocked_snapshot,
        project_id="llamar",
        model_port=_FakeModelPort(_valid_response()),
        config=LongTermRuntimeConfig(min_interval_sec=0),
    )
    assert maybe_trigger_rolling_reflection() == "started"
    timed_out = drain_inflight_reflection(timeout_sec=0.05)
    assert timed_out.status == "timeout"  # 不阻塞退出
    gate.set()
    joined = drain_inflight_reflection(timeout_sec=10)
    assert joined.status == "joined"


def test_rolling_worker_skips_empty_window(lt_runtime_module):
    """窗口为空（游标已到最新）→ 滚动 worker 跳过，不 claim、不写内容。"""
    from a2a.coordinator.memory.contracts import LongTermRuntimeConfig
    from sar_orch.long_term_reflection import (
        configure_long_term_runtime,
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    # 快照 events 全部 <= 游标 10 → 窗口为空（先 claim 并 completed 推进游标）
    snapshot = _snapshot_with_events(revision=7)
    configure_long_term_runtime(
        store=lt_runtime_module,
        snapshot_provider=lambda: snapshot,
        project_id="llamar",
        model_port=_FakeModelPort(_valid_response()),
        config=LongTermRuntimeConfig(min_interval_sec=0),
    )
    claim = lt_runtime_module.claim_reflection_run(
        project_id="llamar",
        scope_id="run-a",
        source_memory_revision=6,
        snapshot_digest="c" * 64,
        policy_version=1,
        window_end_sequence=10,
    )
    lt_runtime_module.mark_reflection_run(claim.run_id, "completed")
    assert maybe_trigger_rolling_reflection() == "started"
    drain = drain_inflight_reflection(timeout_sec=10)
    assert drain.status == "joined"
    assert drain.result["status"] == "skipped_window_empty"
    assert len(lt_runtime_module.list_reflection_runs(project_id="llamar", scope_id="run-a")) == 1


# ---------------------------------------------------------------------------
# P4 review 修复追加（文件末尾追加，P0 断言零改动）
# ---------------------------------------------------------------------------


def test_run_reflection_sync_empty_window_skips_without_claim(tmp_path):
    """M1：同步路径窗口为空 → 跳过（不 claim、零 reflection_run 行、零
    写入、游标不推进），与滚动路径 skipped_window_empty 对齐（§3.4.2）。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = {
        "scope_id": "run-a",
        "memory_revision": 7,
        "snapshot_digest": "d" * 64,
        # 无 events → 空窗口
    }
    result = run_reflection(store, snapshot, policy_version=1)
    assert result.status == "skipped"
    assert result.reason == "empty_window"
    assert result.long_term_memory_written == 0
    assert store.list_reflection_runs(project_id="llamar", scope_id="run-a") == []
    assert store.list_memories(project_id="llamar", scope_id="run-a") == []
    # 再次触发同样跳过，仍零行（无 claim、无游标副作用）
    again = run_reflection(store, snapshot, policy_version=1)
    assert again.status == "skipped"
    assert store.list_reflection_runs(project_id="llamar", scope_id="run-a") == []


def test_read_mode_without_api_key_raises_typed_error(tmp_path):
    """D8（M2）：read 模式缺 reflection_api_key → 显式 typed 拒绝，不静默
    降级；shadow 模式保持 skipped_model_unconfigured。"""
    from a2a.coordinator.memory.contracts import (
        LongTermRuntimeConfig,
        MemoryContractError,
    )
    from a2a.coordinator.memory.store import ScopeEventSnapshotV1
    from sar_orch.experiment import _invoke_run_terminal_long_term_reflection

    class _FakeCoordinator:
        long_term_store = object()  # 非 None，通过 no_store 早退检查

        @staticmethod
        def long_term_snapshot():
            return ScopeEventSnapshotV1(scope_id="a" * 64, status="ok")

    kwargs = {
        "coordinator": _FakeCoordinator(),
        "exp_dir": tmp_path,
        "long_term_mode": "read",
        "lt_config": LongTermRuntimeConfig(),
        "truth_manifest": None,
        "env": {},  # 无 reflection_api_key → build_reflection_model_port 返回 None
    }
    with pytest.raises(MemoryContractError) as exc_info:
        _invoke_run_terminal_long_term_reflection(**kwargs)
    assert "read mode requires reflection_api_key" in str(exc_info.value)

    # shadow 模式不受影响：缺 key 仍按原语义跳过
    shadow = _invoke_run_terminal_long_term_reflection(
        **{**kwargs, "long_term_mode": "shadow"}
    )
    assert shadow["status"] == "skipped_model_unconfigured"


def test_snapshot_wellformed_unregistered_scope_unknown(tmp_path):
    """M3：64-hex 格式合法但从未激活（memory_scope 无记录）的 scope →
    typed unknown_scope（fail closed，不再静默返回 ok 空快照）；已激活但
    无事件的 scope 仍返回 ok 空快照。"""
    from a2a.coordinator.memory.contracts import MemoryConfig, MemoryScopeV1
    from a2a.coordinator.memory.ingestor import MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory.sqlite3")
    factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    unregistered = factory.resolve("ctx-1", 0).scope_id
    result = store.scope_event_snapshot(unregistered)
    assert result.status == "unknown_scope"
    assert result.events == ()
    assert result.memory_revision == 0
    assert len(result.snapshot_digest) == 64  # 空载荷 digest，API 形状稳定

    scope = MemoryScopeV1(
        project_id="llamar", experiment_id="run-1", context_id="ctx-2", runtime_epoch=0
    )
    store.activate_scope(scope)
    activated = store.scope_event_snapshot(scope.scope_id)
    assert activated.status == "ok"
    assert activated.events == ()
    assert activated.memory_revision == 0


def test_collector_rejects_missing_event_type():
    """M7：event_type 缺失（None）fail closed——缺类型事件不进窗口，只有
    allowlist 类型事件被选中。"""
    from a2a.coordinator.memory.reflection import ReflectionSourceCollector

    collector = ReflectionSourceCollector()
    window = collector.collect(
        [
            {"sequence": 1, "event_type": "control.dispatch.RUNNING"},
            {"sequence": 2},  # 无 event_type 键
            {"sequence": 3, "event_type": None},  # 显式 None
        ]
    )
    assert [e["sequence"] for e in window.events] == [1]


# ── reflection trace（模型输入/输出日志，prompt 调优）───────────────────────


def test_reflection_trace_written_on_completed(tmp_path):
    """completed 路径：trace 记录输入输出四件套 + validation 结果，落
    ``<coordinator>/reflection_trace.ndjson``（store.db_path 上一级）。"""
    import json as _json

    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    port = _FakeModelPort(_valid_response())

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "completed"
    trace_file = tmp_path / "coordinator" / "reflection_trace.ndjson"
    assert trace_file.exists(), "trace 文件应落在 coordinator 目录（db_path 上一级）"
    rows = [_json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == result.run_id
    assert row["validation_status"] == "ok"
    assert row["validation_reason"] is None
    assert row["snapshot_digest_prefix"] == ("d" * 64)[:8]
    assert row["system_prompt"].startswith("You are the long-term memory")
    assert "control.dispatch.RUNNING" in row["user_prompt"]
    assert "record_long_term_memories" in row["tool_schema"]
    assert "coordinate at fires" in row["raw_response"]
    assert "usage" not in row  # 本响应无 usage 块，保持原样序列化


def test_reflection_trace_written_on_rejected(tmp_path):
    """rejected 路径：trace 记录 validation_status=rejected + 原因（调优最需要）。"""
    import json as _json

    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    port = _FakeModelPort({})  # 无 function_call

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "rejected"
    trace_file = tmp_path / "coordinator" / "reflection_trace.ndjson"
    rows = [_json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["validation_status"] == "rejected"
    assert rows[0]["validation_reason"] == "missing_function_call"
    assert rows[0]["raw_response"] == "{}"


def test_reflection_trace_redacts_truth_terms(tmp_path):
    """trace 写入前脱敏：模型输出中的 forbidden truth terms → [REDACTED]。"""
    import json as _json

    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()
    bad = _valid_response()
    bad["function_call"][0]["statement"] = "ground truth says fire at A"
    port = _FakeModelPort(bad)

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "rejected"  # truth term 使验证拒绝（零写入）
    trace_file = tmp_path / "coordinator" / "reflection_trace.ndjson"
    rows = [_json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert "ground truth" not in rows[0]["raw_response"]
    assert "REDACTED" in rows[0]["raw_response"]
    assert rows[0]["validation_reason"] == "forbidden_truth_term"


def test_reflection_trace_write_failure_never_fails_run(tmp_path):
    """容错：store 无 db_path（或写失败）时 trace 静默跳过，反思仍正常完成。"""
    from a2a.coordinator.memory.reflection import run_reflection

    class _NoPathStore:
        def window_end_sequence(self, *, project_id, scope_id):
            return 0

        def claim_reflection_run(self, **kwargs):
            from a2a.coordinator.memory.long_term import ReflectionRunClaimResult

            return ReflectionRunClaimResult(
                status="ok", run_id="trace-test-run", run_status="pending"
            )

        def mark_reflection_run(self, run_id, status):
            pass

        def record_audit(self, kind, reason, digest_prefix=None):
            pass

        def publish_memory(self, **kwargs):
            return type("R", (), {"status": "ok"})()

    snapshot = _snapshot_with_events()
    port = _FakeModelPort(_valid_response())

    result = run_reflection(_NoPathStore(), snapshot, policy_version=1, model_port=port)

    assert result.status == "completed"
    assert result.long_term_memory_written == 2


def test_run_reflection_backfills_source_revision_on_support_rows(tmp_path):
    """G2-3 回归：run_reflection 真实 publish 后，long_term_support 行的
    source_revision 回填为 snapshot 的 memory_revision（同 scope 同快照，
    所有 source refs 共享该 revision）；event_digest 保持 None（可选列，
    canonical 无 per-event digest）。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.reflection import run_reflection

    store = LongTermMemoryStore(tmp_path / "coordinator" / "long_term" / "long_term.sqlite3")
    store.open()
    snapshot = _snapshot_with_events()  # memory_revision=7
    port = _FakeModelPort(_valid_response())

    result = run_reflection(store, snapshot, policy_version=1, model_port=port)

    assert result.status == "completed"
    assert result.long_term_memory_written == 2
    memories = store.list_memories(project_id="llamar", scope_id="run-a")
    assert len(memories) == 2
    for m in memories:
        rows = store.list_support_rows(
            project_id="llamar", scope_id="run-a", memory_key=m["memory_key"]
        )
        assert rows, f"memory {m['memory_key']} has no support rows"
        for row in rows:
            assert row["source_revision"] == snapshot.memory_revision
            assert row["event_digest"] is None


# ---------------------------------------------------------------------------
# 2026-08-16 review 修复 —— M-3 诊断通道异常不外溢 / M-4 反思结果先行记录
# ---------------------------------------------------------------------------


def _configure_rolling_with_diagnosis(lt_runtime_module, tmp_path, model_port):
    """反思运行时 + 诊断第二通道运行时同时接线（仿 experiment.py 组装顺序：
    先 configure_long_term_runtime，再 configure_diagnosis_runtime）。"""
    from a2a.coordinator.memory.contracts import (
        DiagnosisConfig,
        LongTermRuntimeConfig,
    )
    from sar_orch.long_term_reflection import (
        configure_diagnosis_runtime,
        configure_long_term_runtime,
    )

    configure_long_term_runtime(
        store=lt_runtime_module,
        snapshot_provider=lambda: _snapshot_with_events(),
        project_id="llamar",
        model_port=model_port,
        config=LongTermRuntimeConfig(min_interval_sec=0),
    )
    configure_diagnosis_runtime(
        canonical_store=object(),
        diagnosis_store=object(),
        diagnosis_config=DiagnosisConfig(experiment_id="run-1", memory_root=tmp_path),
    )


def test_diagnosis_channel_constructor_error_preserves_reflection_result(
    lt_runtime_module, tmp_path, monkeypatch
):
    """M-3: 诊断通道构造抛异常（monkeypatch DiagnosisLoop 构造即炸）→ 异常被
    _run_diagnosis_channel 内部 try 吞掉（typed diagnosis_loop_error），最终
    inflight result 保留反思结果——status 绝不是 failed / rolling_worker_error。"""
    from sar_orch.long_term_reflection import (
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    class _BoomLoop:
        def __init__(self, **kwargs):
            raise RuntimeError("diagnosis loop constructor exploded")

    monkeypatch.setattr("sar_orch.diagnosis_loop.DiagnosisLoop", _BoomLoop)
    _configure_rolling_with_diagnosis(
        lt_runtime_module, tmp_path, _FakeModelPort(_valid_response())
    )
    assert maybe_trigger_rolling_reflection() == "started"
    drain = drain_inflight_reflection(timeout_sec=10)
    assert drain.status == "joined"
    result = drain.result
    assert result is not None
    assert result.get("status") == "completed"  # 反思结果未被抹成 failed
    assert result.get("reason") != "rolling_worker_error"
    assert result.get("run_id")
    # 诊断侧失败以 typed 附加字段出现，绝不替换反思 status
    assert result.get("diagnosis") == {
        "status": "failed",
        "reason": "diagnosis_loop_error",
    }


def test_diagnosis_channel_unexpected_raise_keeps_reflection_result(
    lt_runtime_module, tmp_path, monkeypatch
):
    """M-3+M-4 防御边界: 即使诊断通道在自身 fail-closed 边界之外抛异常
    （monkeypatch _run_diagnosis_channel 直接炸），反思结果也已在先记录——
    最终 result 保留反思 status，诊断降级为 typed 失败字段。"""
    from sar_orch.long_term_reflection import (
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    def _exploding_channel(snapshot):
        raise RuntimeError("unexpected channel failure")

    monkeypatch.setattr(
        "sar_orch.long_term_reflection._run_diagnosis_channel", _exploding_channel
    )
    _configure_rolling_with_diagnosis(
        lt_runtime_module, tmp_path, _FakeModelPort(_valid_response())
    )
    assert maybe_trigger_rolling_reflection() == "started"
    drain = drain_inflight_reflection(timeout_sec=10)
    assert drain.status == "joined"
    result = drain.result
    assert result is not None
    assert result.get("status") == "completed"
    assert result.get("diagnosis") == {
        "status": "failed",
        "reason": "diagnosis_loop_error",
    }


def test_drain_timeout_while_diagnosis_running_keeps_reflection_result(
    lt_runtime_module, tmp_path, monkeypatch
):
    """M-4: 反思完成即记录结果、诊断独立进行——诊断通道阻塞时 drain 超时只丢
    诊断不丢反思：timeout drain 的 result 已含反思结果（无 diagnosis 键，
    调用方 typed None 兜底）；放行后 joined drain 的 result 反思 status 不变、
    诊断字段附着。"""
    import threading

    from sar_orch.long_term_reflection import (
        drain_inflight_reflection,
        maybe_trigger_rolling_reflection,
    )

    gate = threading.Event()
    entered = threading.Event()

    def blocked_channel(snapshot):
        entered.set()
        gate.wait(timeout=30)
        return {"status": "ok", "rounds": 1}

    monkeypatch.setattr(
        "sar_orch.long_term_reflection._run_diagnosis_channel", blocked_channel
    )
    _configure_rolling_with_diagnosis(
        lt_runtime_module, tmp_path, _FakeModelPort(_valid_response())
    )
    assert maybe_trigger_rolling_reflection() == "started"
    # 诊断通道已进入阻塞 → 反思必然已完成并先行记录
    assert entered.wait(timeout=10)
    timed_out = drain_inflight_reflection(timeout_sec=0.05)
    assert timed_out.status == "timeout"
    assert timed_out.result is not None
    assert timed_out.result.get("status") == "completed"  # 反思结果不丢
    assert "diagnosis" not in timed_out.result  # 超时最多丢诊断
    gate.set()
    joined = drain_inflight_reflection(timeout_sec=10)
    assert joined.status == "joined"
    assert joined.result.get("status") == "completed"  # 反思 status 未被覆盖
    assert joined.result.get("diagnosis") == {"status": "ok", "rounds": 1}
