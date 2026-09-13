"""Phase 0（P0）契约卡：长期记忆只读质量 evaluator（主方案 RED contract #7）。

目标契约（主方案 Phase 4 / 探索 03 §7 可复用范式）：
- evaluator 只能以 ``mode=ro`` 打开长期库（``sqlite3.connect(f"file:{path}?mode=ro",
  uri=True)``），输出质量 artifact（``long_term_memory_quality.json``），
  绝不写 source/run/project DB；
- 指标（单 run 版，2026-08-12 冻结）：source traceability rate、
  forbidden-truth violation 恒 0、同 key 冲突率、supersede 链长度、
  反思延迟/token 统计；
- manifest 必须含 scope_id（对照现有 memory_projection_quality 先例）；
- 仅 terminal 后运行、truth 仅 post-hoc 比较（与在线反思的 allowlist 隔离
  不冲突）。

当前代码事实（5413705）：
- ``sar_orch.eval.long_term_memory_quality`` 不存在 → ImportError（预期 RED）；
- 现有 ``sar_orch.eval.memory_projection_quality._open_canonical_db`` 已用
  ``mode=ro``（memory_projection_quality.py:227）→ GREEN 守护（先例范式）。

Phase 4 实现后转 GREEN。
"""

from __future__ import annotations

import sqlite3

import pytest

# ---------------------------------------------------------------------------
# GREEN 守护 —— 现有只读评估范式（P4 长期 evaluator 必须照搬）
# ---------------------------------------------------------------------------


def test_existing_projection_evaluator_opens_db_readonly(tmp_path):
    """现有 memory_projection_quality 以 ``mode=ro`` 打开 canonical DB
    （memory_projection_quality.py:220-229）。

    GREEN 守护：P4 长期 evaluator 必须复用同一只读打开范式。
    """
    from sar_orch.eval.memory_projection_quality import _open_canonical_db

    db_dir = tmp_path / "coordinator" / "memory"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    ro = _open_canonical_db(tmp_path)
    try:
        # 只读连接写任何内容都必须失败
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE should_not_exist (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (1)")
    finally:
        ro.close()
    # 源库未被写入
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        check.close()


def test_existing_evaluator_never_writes_memory(tmp_path):
    """现有 evaluator 的 docstring 契约“never writes”（memory_projection_quality.py:9-17）：
    其 DB 打开路径全部 mode=ro。GREEN 守护：P4 长期 evaluator 同样 never writes。
    """
    from sar_orch.eval.memory_projection_quality import _open_canonical_db

    db_dir = tmp_path / "coordinator" / "memory"
    db_dir.mkdir(parents=True)
    sqlite3.connect(db_dir / "memory.sqlite3").close()
    conn = _open_canonical_db(tmp_path)
    try:
        # mode=ro 的写入尝试必须抛 OperationalError（上一测试已证），
        # 这里只验证连接是 sqlite3.Connection（只读 URI 已由 mode=ro 保证）
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RED —— sar_orch.eval.long_term_memory_quality（Phase 4 新增，现不存在）
# ---------------------------------------------------------------------------


def test_long_term_quality_evaluator_module_import():
    """``sar_orch.eval.long_term_memory_quality`` 必须提供只读长期记忆质量
    evaluator。当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        evaluate_long_term_memory_quality,
    )

    assert callable(evaluate_long_term_memory_quality)


def test_evaluator_opens_long_term_db_readonly(tmp_path):
    """契约 #7 核心：长期库只能以 ``mode=ro`` 打开（
    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)``）；写任何表失败。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        _open_long_term_db,
    )

    db_path = tmp_path / "long_term" / "long_term.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE long_term_memory (memory_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    ro = _open_long_term_db(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO long_term_memory VALUES ('x')")
    finally:
        ro.close()


def test_evaluator_outputs_quality_artifact(tmp_path):
    """输出 ``<results_dir>/long_term_memory_quality.json``（对照现有
    memory_projection_quality.json 先例）。当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        evaluate_long_term_memory_quality,
    )

    evaluate_long_term_memory_quality(results_dir=tmp_path, truth_manifest=None)
    assert (tmp_path / "long_term_memory_quality.json").exists()


def test_evaluator_never_writes_source_run_or_project_db(tmp_path):
    """契约 #7：evaluator 运行后，source/run/project DB 的 mtime 与内容不得
    变化（只读打开长期库 + 输出 artifact 到 results_dir）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        evaluate_long_term_memory_quality,
    )

    source_db = tmp_path / "coordinator" / "memory" / "memory.sqlite3"
    source_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(source_db)
    conn.execute("CREATE TABLE memory_scope (scope_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    before = source_db.stat().st_mtime_ns

    evaluate_long_term_memory_quality(results_dir=tmp_path, truth_manifest=None)
    assert source_db.stat().st_mtime_ns == before


def test_metrics_source_traceability_rate(tmp_path):
    """指标（单 run 版）：source traceability rate——每条 long_term_memory 的
    source_refs 可重放到 canonical 证据。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        source_traceability_rate,
    )

    rate = source_traceability_rate(
        long_term_db=tmp_path / "long_term" / "long_term.sqlite3",
        source_db=tmp_path / "memory" / "memory.sqlite3",
    )
    assert 0.0 <= rate <= 1.0


def test_metrics_forbidden_truth_violation_is_zero(tmp_path):
    """指标：forbidden-truth violation 恒 0（fail-closed 不变量，作为质量
    指标输出）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        forbidden_truth_violation_count,
    )

    assert forbidden_truth_violation_count(
        long_term_db=tmp_path / "long_term" / "long_term.sqlite3"
    ) == 0


def test_metrics_same_key_conflict_rate(tmp_path):
    """指标：同 key 冲突率（同 memory_key 不同 content_digest 的比例）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        same_key_conflict_rate,
    )

    rate = same_key_conflict_rate(long_term_db=tmp_path / "long_term" / "long_term.sqlite3")
    assert 0.0 <= rate <= 1.0


def test_metrics_supersede_chain_length(tmp_path):
    """指标：supersede 链长度（supersedes_memory_id 链平均深度）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        supersede_chain_length,
    )

    length = supersede_chain_length(long_term_db=tmp_path / "long_term" / "long_term.sqlite3")
    assert length >= 0


def test_metrics_reflection_latency_and_tokens(tmp_path):
    """指标：反思延迟/token 统计（reflection_run 的延迟与模型 token 用量）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        reflection_latency_stats,
    )

    stats = reflection_latency_stats(long_term_db=tmp_path / "long_term" / "long_term.sqlite3")
    assert "p50_sec" in stats
    assert "total_tokens" in stats


def test_evaluator_requires_scope_id_manifest(tmp_path):
    """manifest 必须含 scope_id，缺失 → typed 错误（对照现有
    memory_projection_quality.py:482-486 先例）。

    当前不存在 → ImportError（预期 RED）；P4 实现后缺 scope_id 必须抛 typed
    错误（静默接受即假绿）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        evaluate_long_term_memory_quality,
    )

    manifest = tmp_path / "truth_manifest.json"
    manifest.write_text('{"project": "x"}', encoding="utf-8")  # 缺 scope_id
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        evaluate_long_term_memory_quality(results_dir=tmp_path, truth_manifest=manifest)


def test_evaluator_post_terminal_only(tmp_path):
    """仅 terminal 后运行（对照现有 evaluator 边界：memory_projection_quality
    仅 terminal 后运行、truth 仅 post-hoc 比较）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.eval.long_term_memory_quality import (
        terminal_required,
    )

    assert terminal_required() is True


# ---------------------------------------------------------------------------
# P4 追加 —— 种子库上的真实指标 / artifact（文件末尾追加，P0 断言零改动）
# ---------------------------------------------------------------------------


def _seed_long_term_and_source(tmp_path):
    """构造一对 (long_term_db, source_db)：long-term 2 条 published memory +
    可重放证据链；source 库含对应 canonical temporal 事件。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from a2a.coordinator.memory.store import MemoryStore

    long_term_dir = tmp_path / "coordinator" / "long_term"
    long_term_dir.mkdir(parents=True, exist_ok=True)
    lt = LongTermMemoryStore(long_term_dir / "long_term.sqlite3")
    lt.open()

    source_dir = tmp_path / "coordinator" / "memory"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = MemoryStore(source_dir / "memory.sqlite3")
    from a2a.coordinator.memory.contracts import MemoryScopeV1

    scope = MemoryScopeV1(
        project_id="llamar", experiment_id="run-1", context_id="ctx-1", runtime_epoch=0
    )
    source.activate_scope(scope)
    scope_id = scope.scope_id
    source.append_temporal_event(
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
    source.append_temporal_event(
        event_id="evt-2",
        scope_id=scope_id,
        sequence=2,
        event_type="callback.status_update",
        occurred_at="2026-08-12T00:00:01+00:00",
        ingested_at="2026-08-12T00:00:01+00:00",
        actor_id="alice",
        logical_task_id=None,
        dispatch_id="dsp_1",
        worker_task_id="w-1",
        tool_call_id=None,
        success=True,
        error=None,
        payload='{"status": "completed"}',
        causation_id=None,
        correlation_id=None,
        idempotency_key=None,
    )
    source.close()

    lt.publish_memory(
        project_id="llamar",
        scope_id=scope_id,
        memory_key="k1",
        statement="coordinate at fires",
        source_refs=[(scope_id, "evt-1")],
        kind="lesson",
    )
    lt.publish_memory(
        project_id="llamar",
        scope_id=scope_id,
        memory_key="k2",
        statement="avoid water at fires",
        source_refs=[(scope_id, "evt-2")],
        kind="hazard",
    )
    lt.close()
    return (
        long_term_dir / "long_term.sqlite3",
        source_dir / "memory.sqlite3",
        scope_id,
    )


def test_metrics_over_seeded_db(tmp_path):
    """种子库：traceability=1.0、forbidden=0、conflict=0、supersede=0、
    latency stats 含 p50/total_tokens。"""
    from sar_orch.eval.long_term_memory_quality import (
        forbidden_truth_violation_count,
        reflection_latency_stats,
        same_key_conflict_rate,
        source_traceability_rate,
        supersede_chain_length,
    )

    long_term_db, source_db, _ = _seed_long_term_and_source(tmp_path)
    assert source_traceability_rate(long_term_db, source_db) == 1.0
    assert forbidden_truth_violation_count(long_term_db) == 0
    assert same_key_conflict_rate(long_term_db) == 0.0
    assert supersede_chain_length(long_term_db) == 0.0
    stats = reflection_latency_stats(long_term_db)
    assert "p50_sec" in stats and "total_tokens" in stats


def test_traceability_drops_for_unknown_event(tmp_path):
    """证据链指向不存在的 canonical 事件 → traceability < 1.0（可重放性被
    量化，而非静默 1.0）。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from sar_orch.eval.long_term_memory_quality import source_traceability_rate

    long_term_db, source_db, _ = _seed_long_term_and_source(tmp_path)
    lt = LongTermMemoryStore(long_term_db)
    lt.open()
    lt.publish_memory(
        project_id="llamar",
        scope_id="run-x",
        memory_key="k3",
        statement="orphan evidence",
        source_refs=[("run-x", "evt-nonexistent")],
        kind="lesson",
    )
    lt.close()
    rate = source_traceability_rate(long_term_db, source_db)
    assert 0.0 < rate < 1.0


def test_evaluate_writes_artifact_with_scope_id_manifest(tmp_path):
    """manifest 含 scope_id → artifact 写入 results_dir 且携带 scope_id。"""
    import json

    from sar_orch.eval.long_term_memory_quality import (
        evaluate_long_term_memory_quality,
    )

    _, _, scope_id = _seed_long_term_and_source(tmp_path)
    manifest = tmp_path / "truth_manifest.json"
    manifest.write_text(
        json.dumps({"project": "x", "scope_id": scope_id}), encoding="utf-8"
    )
    artifact = evaluate_long_term_memory_quality(
        results_dir=tmp_path, truth_manifest=manifest
    )
    assert artifact["scope_id"] == scope_id
    artifact_path = tmp_path / "long_term_memory_quality.json"
    assert artifact_path.exists()
    loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert loaded["metrics"]["source_traceability_rate"] == 1.0
    assert loaded["metrics"]["forbidden_truth_violation_count"] == 0


# ---------------------------------------------------------------------------
# P4 review 修复追加（文件末尾追加，P0 断言零改动）
# ---------------------------------------------------------------------------


def test_same_key_conflict_rate_excludes_superseded(tmp_path):
    """M5：同 key 的合法 supersede 链（旧行 status='superseded'）不计入冲突
    ——只统计 status='published' 的行 → 冲突率为 0。"""
    from a2a.coordinator.memory.long_term import LongTermMemoryStore
    from sar_orch.eval.long_term_memory_quality import same_key_conflict_rate

    db = tmp_path / "long_term" / "long_term.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    lt = LongTermMemoryStore(db)
    lt.open()
    lt.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="first version",
        source_refs=[("run-a", "e1")],
        kind="lesson",
    )
    second = lt.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="second version",
        source_refs=[("run-a", "e2")],
        kind="lesson",
    )
    lt.close()
    assert second.status == "superseded"
    # 修复前：同 key 两行两个 digest → 冲突率 1.0；修复后只看 published → 0.0
    assert same_key_conflict_rate(db) == 0.0


def test_reflection_latency_excludes_offline_no_model_runs(tmp_path):
    """M9：completed 但无 reflection_usage audit 的 run（offline no-model，
    reason=no_model_port_offline 不落库）不计入延迟统计——只统计有 usage
    记录（digest_prefix 关联）的 completed run。"""
    import sqlite3

    from sar_orch.eval.long_term_memory_quality import reflection_latency_stats

    db = tmp_path / "long_term" / "long_term.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE reflection_run (
            run_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            source_memory_revision INTEGER NOT NULL,
            snapshot_digest TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            window_end_sequence INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE long_term_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            digest_prefix TEXT,
            at TEXT NOT NULL
        );
        """
    )
    # model-backed completed run：usage audit 经 digest_prefix 关联
    conn.execute(
        "INSERT INTO reflection_run (run_id, project_id, scope_id, "
        "idempotency_key, source_memory_revision, snapshot_digest, "
        "policy_version, status, window_end_sequence, truncated, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r1",
            "llamar",
            "run-a",
            "ik1",
            1,
            "a" * 64,
            1,
            "completed",
            1,
            0,
            "2026-08-12T00:00:00+00:00",
            "2026-08-12T00:00:05+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO long_term_audit (kind, reason, digest_prefix, at) "
        "VALUES (?,?,?,?)",
        ("reflection_usage", "tokens=42", "a" * 8, "2026-08-12T00:00:05+00:00"),
    )
    # offline no-model completed run：completed 但无 usage audit → 排除
    conn.execute(
        "INSERT INTO reflection_run (run_id, project_id, scope_id, "
        "idempotency_key, source_memory_revision, snapshot_digest, "
        "policy_version, status, window_end_sequence, truncated, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r2",
            "llamar",
            "run-a",
            "ik2",
            2,
            "b" * 64,
            1,
            "completed",
            2,
            0,
            "2026-08-12T00:00:00+00:00",
            "2026-08-12T00:00:09+00:00",
        ),
    )
    conn.commit()
    conn.close()

    stats = reflection_latency_stats(db)
    # 只统计有 usage 的 run：p50=5.0s、tokens=42、runs=1（offline run 排除）
    assert stats == {"p50_sec": 5.0, "total_tokens": 42, "runs": 1}
