"""Phase 0（P0）契约卡：LongTermMemoryStore（主方案 RED contract #4 + 跨 Run
存储补充 §3）。

目标契约：
- 独立 run-local 文件 ``<memory_root>/long_term/long_term.sqlite3``，复用
  ``MemoryConfig.memory_root`` 派生，不新增 root 参数；拒绝相对路径/URI；
- 独立 schema + 自带 migration runner（``schema_migrations(version,
  applied_at, migration_sha256)``；001_initial；新库先建 version table 再跑
  001；显式 BEGIN IMMEDIATE → statement-by-statement → version row → COMMIT；
  已应用版本真 no-op；未知未来版本 / digest 不匹配 / 半完成迁移 fail closed；
  失败必须 ROLLBACK，不能用 executescript 充当事务边界）；
- 跨 scope（run）读写隔离：两个 scope 的长期数据互不可见，所有查询必带
  project_id + scope_id；
- 同 source revision 重试零重复持久化（reflection_run exactly-once）；
- WAL / busy_timeout / 有限 BEGIN IMMEDIATE retry；锁耗尽返回 typed retryable
  状态，绝不部分提交；LLM/network/export 永远在 transaction 外；
- ``long_term_memory`` / ``long_term_support`` / ``long_term_audit`` 表语义；
- schema/version 错误不得修改 run-local source DB。

当前代码事实（5413705）：
- ``a2a.coordinator.memory.long_term`` 模块不存在 → ImportError（预期 RED）；
- 现有 MemoryStore 直接 ``PRAGMA user_version=3`` + executescript（无迁移
  runner，store.py:286-290）→ 守护测试钉住 canonical 侧不动。

Phase 1 实现后转 GREEN。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from a2a.coordinator.memory.store import MemoryStore


@pytest.fixture
def tmp_root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# GREEN 守护 —— canonical store 拓扑冻结（P1 不得污染 canonical）
# ---------------------------------------------------------------------------


def test_canonical_store_schema_has_no_long_term_tables(tmp_root):
    """canonical ``_SCHEMA`` 不得出现任何长期记忆表（跨 Run 补充 §2：
    不得把长期表加到 run-local MemoryStore._SCHEMA）。

    GREEN 守护：P1 即使新增长期库也不得改动此处。
    """
    from a2a.coordinator.memory.store import _SCHEMA

    for table in (
        "long_term_revision",
        "reflection_run",
        "long_term_memory",
        "long_term_support",
        "long_term_audit",
        "schema_migrations",
    ):
        assert table not in _SCHEMA


def test_canonical_store_user_version_stays_three(tmp_root):
    """canonical DB ``PRAGMA user_version`` 保持 3（store.py:289）。

    GREEN 守护：P1 长期库有自己的 migration runner，不得改写 canonical
    user_version。
    """
    db = tmp_root / "memory.sqlite3"
    store = MemoryStore(db)
    try:
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        finally:
            conn.close()
    finally:
        store.close()


def test_canonical_projection_domain_check_unchanged(tmp_root):
    """``projection_field.domain`` CHECK 保持 ``('spatial','embodied')``
    （store.py:171）——长期记忆不得扩展为第三种投影 domain。

    GREEN 守护：P1 长期库是独立表，不碰 domain CHECK。
    """
    from a2a.coordinator.memory.store import _SCHEMA

    assert "CHECK(domain IN ('spatial','embodied'))" in _SCHEMA


# ---------------------------------------------------------------------------
# RED —— LongTermMemoryStore 模块与 run-local 拓扑（Phase 1，现不存在）
# ---------------------------------------------------------------------------


def test_long_term_memory_store_module_import():
    """``LongTermMemoryStore`` 必须从 ``a2a.coordinator.memory.long_term``
    导出。当前模块不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(Path("/tmp/root"))
    assert store is not None


def test_store_derives_run_local_path_from_memory_root(tmp_root):
    """D4：store 打开 ``<memory_root>/long_term/long_term.sqlite3``（由
    MemoryConfig.memory_root 派生）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(experiment_id="run-1", memory_root=tmp_root)
    store = LongTermMemoryStore(config.long_term_db_path)
    assert store.db_path == tmp_root / "long_term" / "long_term.sqlite3"


def test_store_rejects_relative_path():
    """D4：store 拒绝相对路径（fail-closed）。当前不存在 → ImportError（预期 RED）；
    P1 实现后必须抛 typed 拒绝错误（no-op 打开即假绿）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(Path("relative/long_term.sqlite3"))
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        store.open()


def test_store_schema_migrations_table_exists(tmp_root):
    """跨 Run 补充 §3.1：单一 schema version authority
    ``schema_migrations(version, applied_at, migration_sha256)``。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    tables = store.list_tables()
    assert "schema_migrations" in tables


def test_fresh_root_applies_001_initial_only(tmp_root):
    """跨 Run 补充 §3.2：新库先建 version table 再运行 001_initial；二次打开
    无新 migration（no-op）。当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    first = LongTermMemoryStore(db_path)
    first.open()
    assert first.applied_migrations() == [1]
    first.close()

    second = LongTermMemoryStore(db_path)
    second.open()
    assert second.applied_migrations() == [1]  # 已应用版本真 no-op


def test_unknown_future_version_fail_closed(tmp_root):
    """跨 Run 补充 §3.4：未知未来版本 → fail closed，禁止打开后继续读写。

    当前不存在 → ImportError（预期 RED）；P1 实现后未知版本打开必须抛 typed
    拒绝错误（静默打开即假绿）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    store = LongTermMemoryStore(db_path)
    store.open()
    store.force_schema_version(999)  # 模拟未来版本
    reopened = LongTermMemoryStore(db_path)
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        reopened.open()  # 必须拒绝并抛 typed 错误


def test_migration_digest_mismatch_fail_closed(tmp_root):
    """跨 Run 补充 §3.4：migration digest 不匹配 → fail closed。

    当前不存在 → ImportError（预期 RED）；P1 实现后 digest 不匹配打开必须抛
    typed 拒绝错误（静默打开即假绿）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    store = LongTermMemoryStore(db_path)
    store.open()
    store.corrupt_migration_digest(1)
    reopened = LongTermMemoryStore(db_path)
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        reopened.open()  # digest 不匹配 → 拒绝


def test_migration_midway_error_rolls_back(tmp_root):
    """跨 Run 补充 §3.3：migration 中途抛错 → 显式 ROLLBACK，无 partial write
    （不允许 executescript 充当事务边界）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    store = LongTermMemoryStore(db_path)
    store.open()
    store.inject_failing_migration(2)  # 002 中途抛错
    reopened = LongTermMemoryStore(db_path)
    reopened.open()
    assert reopened.applied_migrations() == [1]  # 002 未应用，回滚干净


def test_simulated_old_version_upgrade_preserves_data(tmp_root):
    """跨 Run 补充 §3：仅应用旧 migration 的临时 DB 升级后，旧数据/support
    refs 保留，新增默认/状态语义正确。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    store = LongTermMemoryStore(db_path)
    store.open()
    store.seed_legacy_row_v1("scope-a", "legacy-memory")
    store.upgrade_to_latest()
    reopened = LongTermMemoryStore(db_path)
    reopened.open()
    assert reopened.fetch_memory("scope-a", "legacy-memory") is not None


# ---------------------------------------------------------------------------
# RED —— scope 隔离 / 幂等 / 并发（Phase 1）
# ---------------------------------------------------------------------------


def test_scope_isolation_between_runs(tmp_root):
    """D4/跨 Run 补充：跨 scope（run）读写隔离——两个 scope 的长期数据互不可见，
    所有查询必带 project_id + scope_id。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    store.publish_memory(project_id="llamar", scope_id="run-a", memory_key="k1", statement="s1")
    assert store.list_memories(project_id="llamar", scope_id="run-b") == []
    assert store.list_memories(project_id="llamar", scope_id="run-a") != []


def test_same_source_revision_retry_zero_duplicate_persistence(tmp_root):
    """原子快照补充 §2.5：同一 snapshot（同 source revision + digest +
    policy_version）重试两次 → 只一条 reflection_run 与一次 candidate/support
    持久化。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    claim_args = {
        "project_id": "llamar",
        "scope_id": "run-a",
        "source_memory_revision": 7,
        "snapshot_digest": "d" * 64,
        "policy_version": 1,
        "window_end_sequence": 42,
    }
    store.claim_reflection_run(**claim_args)
    dup = store.claim_reflection_run(**claim_args)
    assert dup.status == "duplicate"
    runs = store.list_reflection_runs(project_id="llamar", scope_id="run-a")
    assert len(runs) == 1


def test_lock_exhaustion_returns_typed_retryable_no_partial_write(tmp_root):
    """主方案 §3.3：锁耗尽返回 typed retryable 状态，绝不部分提交。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    store.hold_write_lock()  # 模拟另一 writer 持有 BEGIN IMMEDIATE
    result = store.publish_memory(
        project_id="llamar", scope_id="run-a", memory_key="k1", statement="s1"
    )
    assert result.status == "retryable_lock_busy"
    assert store.list_memories(project_id="llamar", scope_id="run-a") == []


def test_two_store_instances_same_db_concurrent_reflection(tmp_root):
    """跨 Run 补充 §3.6：两个独立 store instance 指向同一 tmp DB，并发写
    相同 reflection id → 重试后恰有一条 canonical run/support，绝无
    duplicate/partial row。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    writer_a = LongTermMemoryStore(db_path)
    writer_b = LongTermMemoryStore(db_path)
    writer_a.open()
    writer_b.open()
    claim_args = {
        "project_id": "llamar",
        "scope_id": "run-a",
        "source_memory_revision": 7,
        "snapshot_digest": "d" * 64,
        "policy_version": 1,
        "window_end_sequence": 42,
    }
    result_a = writer_a.claim_reflection_run(**claim_args)
    result_b = writer_b.claim_reflection_run(**claim_args)
    assert {result_a.status, result_b.status} <= {"ok", "duplicate"}
    runs = writer_a.list_reflection_runs(project_id="llamar", scope_id="run-a")
    assert len(runs) == 1


def test_reopen_two_instances_read_same_scope(tmp_root):
    """跨 Run 补充 §3：reopen——两个独立 store instance 可读取同一 scope 数据，
    scope filter 仍生效。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    db_path = tmp_root / "long_term" / "long_term.sqlite3"
    first = LongTermMemoryStore(db_path)
    first.open()
    first.publish_memory(project_id="llamar", scope_id="run-a", memory_key="k1", statement="s1")
    first.close()

    second = LongTermMemoryStore(db_path)
    second.open()
    assert second.list_memories(project_id="llamar", scope_id="run-a") != []
    assert second.list_memories(project_id="llamar", scope_id="run-b") == []


def test_reflection_run_status_state_machine(tmp_root):
    """reflection_run 状态 pending|completed|rejected|failed|timeout；
    window_end_sequence 只在 completed 时推进（主方案 §3.4.3）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    run_id = store.claim_reflection_run(
        project_id="llamar",
        scope_id="run-a",
        source_memory_revision=7,
        snapshot_digest="d" * 64,
        policy_version=1,
        window_end_sequence=10,
    ).run_id
    store.mark_reflection_run(run_id, "rejected")  # 不推进游标
    assert store.window_end_sequence(project_id="llamar", scope_id="run-a") == 0
    store.mark_reflection_run(run_id, "completed")
    assert store.window_end_sequence(project_id="llamar", scope_id="run-a") == 10


def test_long_term_support_binds_source_scope_and_event(tmp_root):
    """long_term_support (memory_id, source_scope_id, source_event_id)：可重放
    证据链，每条 ref 绑定 source revision/event digest（主方案 §3.3）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    store.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="s1",
        source_refs=[("scope-a", "evt-1"), ("scope-a", "evt-2")],
    )
    refs = store.list_support_refs(project_id="llamar", scope_id="run-a", memory_key="k1")
    assert ("scope-a", "evt-1") in refs
    assert ("scope-a", "evt-2") in refs


def test_long_term_audit_append_only_redacted(tmp_root):
    """long_term_audit append-only：只记 redacted failure reason/digest，不写
    raw prompt、CoT、secret、truth（主方案 §3.3）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    store.record_audit(kind="reflection_rejected", reason="forbidden_truth_term", digest_prefix="ab12")
    entries = store.list_audit_entries()
    assert len(entries) == 1
    assert "ground_truth" not in entries[0]["reason"]  # redacted


def test_schema_error_does_not_touch_source_db(tmp_root):
    """跨 Run 补充 §3.5：长期库 schema/version 错误不得修改 run-local source
    DB；store 拒绝打开时 long_term_memory/support 零部分写。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    source_db = tmp_root / "memory" / "memory.sqlite3"
    source = MemoryStore(source_db)
    source.close()
    before_mtime = source_db.stat().st_mtime

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    store.force_schema_version(999)
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3").open()
    assert source_db.stat().st_mtime == before_mtime
    assert source_db.exists()


# ---------------------------------------------------------------------------
# review 修复防回归 —— F4 store 边界 / F5 IntegrityError→duplicate / F6 终态
# ---------------------------------------------------------------------------


def test_publish_memory_rejects_out_of_range_confidence_and_empty_key(tmp_root):
    """F4：publish_memory store 边界校验 —— confidence 越界 / memory_key 空
    一律 typed MemoryContractError，绝不 status=ok 落库。"""
    from a2a.coordinator.memory.contracts import MemoryContractError
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    with pytest.raises(MemoryContractError) as exc_info:
        store.publish_memory(
            project_id="llamar",
            scope_id="run-a",
            memory_key="k1",
            statement="s1",
            confidence=5.0,
        )
    assert exc_info.value.code == "invalid_confidence"
    with pytest.raises(MemoryContractError) as exc_info:
        store.publish_memory(
            project_id="llamar",
            scope_id="run-a",
            memory_key="k1",
            statement="s1",
            confidence=-0.5,
        )
    assert exc_info.value.code == "invalid_confidence"
    with pytest.raises(MemoryContractError) as exc_info:
        store.publish_memory(
            project_id="llamar",
            scope_id="run-a",
            memory_key="",
            statement="s1",
        )
    assert exc_info.value.code == "invalid_memory_key"
    assert store.list_memories(project_id="llamar", scope_id="run-a") == []


def test_claim_reflection_run_integrity_error_maps_to_duplicate(tmp_root):
    """F5：并发窗口内 INSERT 撞 UNIQUE(idempotency_key) 抛 IntegrityError 时，
    claim 返回 typed duplicate（与 publish 幂等语义一致），绝不裸抛。

    模拟：先直插同 idempotency_key 的行，再让 SELECT 假装未见到（race 窗口），
    使 INSERT 必撞约束。
    """
    import uuid as _uuid

    from a2a.coordinator.memory.contracts import reflection_run_idempotency_key
    from a2a.coordinator.memory.long_term import LongTermMemoryStore

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    claim_args = {
        "project_id": "llamar",
        "scope_id": "run-a",
        "source_memory_revision": 7,
        "snapshot_digest": "d" * 64,
        "policy_version": 1,
        "window_end_sequence": 42,
    }
    idempotency_key = reflection_run_idempotency_key(
        project_id="llamar",
        source_scope_id="run-a",
        memory_revision=7,
        snapshot_digest="d" * 64,
        policy_version=1,
    )
    now = "2026-08-12T00:00:00+00:00"
    store._conn.execute(
        "INSERT INTO reflection_run (run_id, project_id, scope_id, "
        "idempotency_key, source_memory_revision, snapshot_digest, "
        "policy_version, status, window_end_sequence, truncated, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _uuid.uuid4().hex,
            "llamar",
            "run-a",
            idempotency_key,
            7,
            "d" * 64,
            1,
            "pending",
            42,
            0,
            now,
            now,
        ),
    )
    store._conn.commit()

    real_execute = store._conn.execute

    class _EmptyResult:
        """SELECT 结果：无行（race 窗口内 claim 看不到已存在的行）。"""

        def fetchone(self):
            return None

    class _ConnProxy:
        """委托真实连接，但模拟 race 窗口：claim 的 SELECT 假装看不到已存在的
        行（sqlite3.Connection.execute 是 C 方法，无法 monkeypatch setattr）。"""

        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, parameters=()):
            if sql.strip().startswith(
                "SELECT run_id, status FROM reflection_run"
            ):
                return _EmptyResult()
            return real_execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._real, name)

    proxy = _ConnProxy(store._conn)
    store._conn = proxy
    try:
        result = store.claim_reflection_run(**claim_args)
    finally:
        store._conn = proxy._real
    assert result.status == "duplicate"
    runs = store.list_reflection_runs(project_id="llamar", scope_id="run-a")
    assert len(runs) == 1  # 零新增行


def test_mark_reflection_run_cannot_leave_completed(tmp_root):
    """F6：completed 是终态 —— 离开 completed 抛 typed LongTermStoreError
    （窗口游标 MAX(window_end_sequence) WHERE status='completed' 不得回退）；
    completed → completed 幂等允许；rejected → completed 仍合法（P0 冻结语义）。"""
    from a2a.coordinator.memory.long_term import (
        LongTermMemoryStore,
        LongTermStoreError,
    )

    store = LongTermMemoryStore(tmp_root / "long_term" / "long_term.sqlite3")
    store.open()
    run_id = store.claim_reflection_run(
        project_id="llamar",
        scope_id="run-a",
        source_memory_revision=7,
        snapshot_digest="d" * 64,
        policy_version=1,
        window_end_sequence=10,
    ).run_id
    store.mark_reflection_run(run_id, "rejected")  # 冻结语义：rejected 合法
    store.mark_reflection_run(run_id, "completed")
    assert store.window_end_sequence(project_id="llamar", scope_id="run-a") == 10
    with pytest.raises(LongTermStoreError) as exc_info:
        store.mark_reflection_run(run_id, "pending")  # 离开 completed → 拒绝
    assert exc_info.value.code == "invalid_run_status_transition"
    assert store.window_end_sequence(project_id="llamar", scope_id="run-a") == 10
    store.mark_reflection_run(run_id, "completed")  # completed → completed 幂等
    assert store.window_end_sequence(project_id="llamar", scope_id="run-a") == 10



# ---------------------------------------------------------------------------
# P4 追加 —— scope_event_snapshot 确定性与事务卫生（文件末尾追加，P0 断言零改动）
# ---------------------------------------------------------------------------


def test_snapshot_digest_deterministic_and_consistent(tmp_root):
    """同一提交状态 → digest 完全一致；snapshot.revision == revision_of；
    events 按 sequence 升序。"""
    from a2a.coordinator.memory.contracts import MemoryScopeV1
    from a2a.coordinator.memory.store import MemoryStore

    store = MemoryStore(tmp_root / "memory.sqlite3")
    scope = MemoryScopeV1(
        project_id="llamar", experiment_id="run-1", context_id="ctx-1", runtime_epoch=0
    )
    store.activate_scope(scope)
    scope_id = scope.scope_id
    store.append_temporal_event(
        event_id="evt-1",
        scope_id=scope_id,
        sequence=1,
        event_type="control.dispatch.RUNNING",
        occurred_at="2026-08-12T00:00:00+00:00",
        ingested_at="2026-08-12T00:00:00+00:00",
        actor_id="system",
        logical_task_id=None,
        dispatch_id="dsp_1",
        worker_task_id=None,
        tool_call_id=None,
        success=None,
        error=None,
        payload=None,
        causation_id=None,
        correlation_id=None,
        idempotency_key=None,
    )
    first = store.scope_event_snapshot(scope_id)
    second = store.scope_event_snapshot(scope_id)
    assert first.snapshot_digest == second.snapshot_digest
    assert len(first.snapshot_digest) == 64
    assert first.memory_revision == store.revision_of(scope_id)
    seqs = [e["sequence"] for e in first.events]
    assert seqs == sorted(seqs)


def test_snapshot_read_failure_does_not_poison_later_writes(tmp_root):
    """read_failed / unknown_scope 路径不残留开放读事务：后续写照常工作。"""
    from a2a.coordinator.memory.contracts import MemoryScopeV1
    from a2a.coordinator.memory.store import MemoryStore

    store = MemoryStore(tmp_root / "memory.sqlite3")
    scope = MemoryScopeV1(
        project_id="llamar", experiment_id="run-1", context_id="ctx-1", runtime_epoch=0
    )
    store.activate_scope(scope)
    scope_id = scope.scope_id

    failed = store.scope_event_snapshot(scope_id, simulate_read_error=True)
    assert failed.status == "read_failed"
    unknown = store.scope_event_snapshot("no-such-scope")
    assert unknown.status == "unknown_scope"

    # 若读事务泄漏，这里的 BEGIN IMMEDIATE 会报 cannot start a transaction
    store.append_temporal_event(
        event_id="evt-after-failure",
        scope_id=scope_id,
        sequence=1,
        event_type="callback.status_update",
        occurred_at="2026-08-12T00:00:00+00:00",
        ingested_at="2026-08-12T00:00:00+00:00",
        actor_id="alice",
        logical_task_id=None,
        dispatch_id=None,
        worker_task_id=None,
        tool_call_id=None,
        success=True,
        error=None,
        payload=None,
        causation_id=None,
        correlation_id=None,
        idempotency_key=None,
    )
    snap = store.scope_event_snapshot(scope_id)
    assert snap.status == "ok"
    assert any(e["event_id"] == "evt-after-failure" for e in snap.events)


def test_snapshot_readonly_leaves_outbox_and_audit_untouched(tmp_root):
    """snapshot 零副作用扩展到 outbox / security audit（不只 revision/events）。"""
    from a2a.coordinator.memory.contracts import MemoryScopeV1
    from a2a.coordinator.memory.store import MemoryStore

    store = MemoryStore(tmp_root / "memory.sqlite3")
    scope = MemoryScopeV1(
        project_id="llamar", experiment_id="run-1", context_id="ctx-1", runtime_epoch=0
    )
    store.activate_scope(scope)
    scope_id = scope.scope_id
    store.write_outbox(
        outbox_id="out-1",
        scope_id=scope_id,
        event_id="evt-1",
        export_kind="control",
        payload_sha256="p" * 64,
    )
    store.record_security_audit(
        "test", reason="seed audit", digest_prefix="ab12"
    )
    before_outbox = store.outbox_entries(scope_id)
    before_audit = store.security_audit_entries()

    store.scope_event_snapshot(scope_id)

    assert store.outbox_entries(scope_id) == before_outbox
    assert store.security_audit_entries() == before_audit
