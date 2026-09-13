"""Phase 0（P0）契约卡：LongTermMemory 契约面（主方案 RED contract #4）。

目标契约（主方案 §3.3 / D4 / D9 + 跨 Run 存储补充）：
- ``LongTermMemoryConfig`` 拒绝相对路径/URI；run-local root 自动派生
  ``<memory_root>/long_term/long_term.sqlite3``；``long_term_mode`` 默认 ``off``；
- ``LongTermMemoryCandidateV1``：schema_version / memory_key / kind / statement /
  confidence / source_refs；``kind`` 为受限枚举
  strategy|lesson|hazard|pattern|status；
- ``memory_key`` = sha256(project_id, kind, policy_version, canonicalized(statement))，
  ``content_digest`` = sha256(canonicalized(statement))，两者都基于 post-redaction
  文本派生；canonicalize 规则：lowercase → strip → 空白折叠 → 去首尾标点；
- ``policy_version`` 为代码常量（初始 1）；
- ``reflection_run`` 幂等键至少绑定 project_id + source_scope_id +
  snapshot.memory_revision + snapshot.snapshot_digest + policy_version；
- D9：``long_term.config`` 缺失项用默认值；unparseable/非法值 → typed 错误 +
  long_term_mode 强制 off + audit，不静默回退默认。

当前代码事实（5413705）：
- ``LongTermMemoryConfig`` / ``LongTermMemoryCandidateV1`` 尚不存在 →
  ImportError（预期 RED）；
- 现有 ``MemoryConfig.validate``（contracts.py:527-541）已拒绝相对路径/URI、
  且 ``db_path`` 派生已存在 → GREEN 守护（P1 的长期配置应复用同一校验语义）；
- ``FORBIDDEN_TRUTH_TERMS`` / ``ONLINE_PROVENANCE_ALLOWLIST`` 已存在 →
  GREEN 守护（P4 validator/collector 复用词表）。

Phase 1 实现后转 GREEN。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    ONLINE_PROVENANCE_ALLOWLIST,
    MemoryConfig,
    MemoryConfigError,
)

# ---------------------------------------------------------------------------
# GREEN 守护 —— 现有 MemoryConfig 校验语义（P1 长期配置必须复用）
# ---------------------------------------------------------------------------


def test_existing_memory_config_rejects_relative_root():
    """现有 MemoryConfig 拒绝相对路径（contracts.py:533-536）。

    GREEN 守护：LongTermMemoryConfig 必须继承同一 fail-closed 语义
    （拒绝相对路径/URI 是主方案 RED contract #4 的第一条）。
    """
    with pytest.raises(MemoryConfigError):
        MemoryConfig(experiment_id="run-1", memory_root=Path("relative/root")).validate()


def test_existing_memory_config_rejects_uri_root():
    """现有 MemoryConfig 拒绝 URI（``://``，contracts.py:537-540）。

    GREEN 守护：长期配置不得引入 s3:// 等远程 root。
    """
    with pytest.raises(MemoryConfigError):
        MemoryConfig(
            experiment_id="run-1", memory_root=Path("s3://bucket/root")
        ).validate()


def test_existing_memory_config_db_path_derivation(tmp_path):
    """现有 MemoryConfig 派生 canonical DB：``<memory_root>/memory/memory.sqlite3``
    （contracts.py:523-525）。

    GREEN 守护：长期库路径派生（``<memory_root>/long_term/long_term.sqlite3``）
    必须与 canonical 派生同构、同 root。
    """
    config = MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    assert config.db_path == tmp_path / "memory" / "memory.sqlite3"


def test_forbidden_truth_terms_available_for_reflection_validator():
    """FORBIDDEN_TRUTH_TERMS 词表存在且覆盖 oracle/ground_truth。

    GREEN 守护：P4 反思 validator 复用同一词表（contracts.py:72-91），
    不得另建词表。
    """
    assert "oracle" in FORBIDDEN_TRUTH_TERMS
    assert "ground_truth" in FORBIDDEN_TRUTH_TERMS
    assert "simulator" in FORBIDDEN_TRUTH_TERMS


def test_allowlist_covers_reflection_input_sources():
    """ONLINE_PROVENANCE_ALLOWLIST 覆盖反思输入四类事件来源
    （callback.* / evidence.projection / supervision.* / control.*）。

    GREEN 守护：P4 source collector 的输入白名单复用 allowlist（contracts.py:54-64）。
    """
    assert {"worker_observation", "worker_telemetry", "registry", "control", "supervision"} <= set(
        ONLINE_PROVENANCE_ALLOWLIST
    )


# ---------------------------------------------------------------------------
# RED —— LongTermMemoryConfig（Phase 1 新增，现不存在 → ImportError）
# ---------------------------------------------------------------------------


def test_long_term_memory_config_exported():
    """``LongTermMemoryConfig`` 必须从 ``a2a.coordinator.memory.contracts`` 导出。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(experiment_id="run-1", memory_root=Path("/tmp/root"))
    assert config.memory_root == Path("/tmp/root")


def test_long_term_memory_config_rejects_relative_root(tmp_path):
    """D4/D9：长期配置拒绝相对路径（与 MemoryConfig 同构 fail-closed）。

    当前不存在 → ImportError（预期 RED）；P1 实现后必须抛 typed 拒绝错误。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(experiment_id="run-1", memory_root=Path("relative/root"))
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        config.validate()


def test_long_term_memory_config_rejects_uri_root():
    """D4/D9：长期配置拒绝 URI root（``file://`` / ``s3://`` 等）。

    当前不存在 → ImportError（预期 RED）；P1 实现后必须抛 typed 拒绝错误。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(
        experiment_id="run-1", memory_root=Path("file:///tmp/root")
    )
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        config.validate()


def test_long_term_memory_config_run_local_root_derivation(tmp_path):
    """D4：run-local 长期库路径自动派生为
    ``<memory_root>/long_term/long_term.sqlite3``（不新增独立 root 参数）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    assert config.long_term_db_path == tmp_path / "long_term" / "long_term.sqlite3"


def test_long_term_memory_config_default_mode_off():
    """D5：``long_term_mode`` 默认 ``off``（off|shadow|read）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryConfig

    config = LongTermMemoryConfig(experiment_id="run-1", memory_root=Path("/tmp/root"))
    assert config.long_term_mode == "off"


def test_long_term_config_unparseable_forces_off_with_typed_error(tmp_path):
    """D9：``long_term.config`` unparseable → typed 错误（+ long_term_mode
    强制 off + audit，由调用方在捕获 typed 错误后保证），不静默回退默认。
    解析器位于未来 sar_orch.long_term_reflection。

    当前不存在 → ImportError（预期 RED）；P1 实现后损坏文件必须抛 typed 错误
    （no-op 返回即假绿）。
    """
    from sar_orch.long_term_reflection import load_long_term_config

    broken = tmp_path / "long_term.config"
    broken.write_text("[window\nmax_events = nope\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 错误的具体类未冻结
        load_long_term_config(broken)


def test_long_term_config_invalid_value_forces_off(tmp_path):
    """D9：值非法（如 max_events 非整数）→ typed 错误（+ mode 强制 off），
    不静默回退默认。

    当前不存在 → ImportError（预期 RED）；P1 实现后非法值必须抛 typed 错误。
    """
    from sar_orch.long_term_reflection import load_long_term_config

    bad = tmp_path / "long_term.config"
    bad.write_text("[window]\nmax_events = abc\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 错误的具体类未冻结
        load_long_term_config(bad)


def test_long_term_config_defaults_when_missing(tmp_path):
    """D9：缺失项用默认值（window max_events=200 / max_chars=8000 /
    trigger every_env_step=5 / min_interval_sec=30 / timeout reflection_sec=60）；
    文件缺失（非损坏）时返回 config 且 ``long_term_mode`` 保持默认 ``off``
    （D9 的“mode 强制 off”在返回路径上的独立断言）。

    当前不存在 → ImportError（预期 RED）。
    """
    from sar_orch.long_term_reflection import load_long_term_config

    config = load_long_term_config(tmp_path / "no_such_long_term.config")
    assert config.max_events == 200
    assert config.max_chars == 8000
    assert config.every_env_step == 5
    assert config.min_interval_sec == 30
    assert config.reflection_sec == 60
    assert config.long_term_mode == "off"  # 缺文件/损坏时绝不放行 read


# ---------------------------------------------------------------------------
# RED —— LongTermMemoryCandidateV1 与确定性 key 派生（Phase 1，现不存在）
# ---------------------------------------------------------------------------


def test_long_term_memory_candidate_v1_exported():
    """``LongTermMemoryCandidateV1`` 至少含：schema_version / memory_key /
    kind / statement / confidence / source_refs（主方案 §3.3）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryCandidateV1

    candidate = LongTermMemoryCandidateV1(
        schema_version=1,
        memory_key="key",
        kind="lesson",
        statement="agents should coordinate at fires",
        confidence=0.9,
        source_refs=[("scope-a", "evt-1")],
    )
    assert candidate.kind == "lesson"


def test_candidate_kind_rejects_unknown_enum_value():
    """kind 为受限枚举 strategy|lesson|hazard|pattern|status；枚举外值必须被
    validator 拒绝（fail-closed，零写入）。

    当前不存在 → ImportError（预期 RED）；P1 实现后枚举外值必须抛 typed
    拒绝错误。
    """
    from a2a.coordinator.memory.contracts import LongTermMemoryCandidateV1

    with pytest.raises(Exception):  # noqa: B017 - 未来 typed 拒绝异常的具体类未冻结
        LongTermMemoryCandidateV1(
            schema_version=1,
            memory_key="key",
            kind="gossip",  # 枚举外
            statement="s",
            confidence=0.9,
            source_refs=[],
        ).validate()


def test_memory_key_deterministic_derivation():
    """``memory_key`` = sha256(project_id, kind, policy_version,
    canonicalized(statement))；系统确定性派生，模型不可见、不可选。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import (
        derive_memory_key,
    )

    key_a = derive_memory_key(project_id="llamar", kind="lesson", policy_version=1, statement="Agents  coordinate.")
    key_b = derive_memory_key(project_id="llamar", kind="lesson", policy_version=1, statement="agents coordinate")
    assert key_a == key_b  # canonicalize 后一致 → 同一 key


def test_content_digest_based_on_post_redaction_text():
    """``content_digest = sha256(canonicalized(statement))``，基于最终存储的
    post-redaction 文本（redaction 先于 key/digest 计算，审计可复算）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import (
        derive_content_digest,
    )

    digest = derive_content_digest("  Fire at (1,2,0).  ")
    assert isinstance(digest, str) and len(digest) == 64


def test_canonicalize_statement_rules():
    """canonicalized(statement) 规则定死：lowercase → strip → 空白折叠 →
    去首尾标点（主方案 §3.3）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import (
        canonicalize_statement,
    )

    assert canonicalize_statement("  Fire   At (1,2,0).  ") == "fire at (1,2,0)"


def test_policy_version_constant_is_one():
    """``policy_version`` 为代码常量，初始 1；随 canonicalize/redaction 策略
    变更人工递增（模型不可见）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import POLICY_VERSION

    assert POLICY_VERSION == 1


def test_reflection_run_idempotency_key_binding():
    """``reflection_run`` 幂等键至少绑定 project_id + source_scope_id +
    snapshot.memory_revision + snapshot.snapshot_digest + policy_version
    （原子快照补充 §2.5）；同一 snapshot 重试零重复持久化。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.contracts import (
        reflection_run_idempotency_key,
    )

    key = reflection_run_idempotency_key(
        project_id="llamar",
        source_scope_id="scope-a",
        memory_revision=12,
        snapshot_digest="d" * 64,
        policy_version=1,
    )
    assert isinstance(key, str) and len(key) == 64


# ---------------------------------------------------------------------------
# F1 防回归 —— load_long_term_config 键映射（review Major）
# ---------------------------------------------------------------------------


def test_load_long_term_config_repo_root_quality_enabled_mapping():
    """F1 回归：仓库根 ``long_term.config`` 真实内容必须可解析。

    review 证实旧实现把 ``[quality] enabled`` 存成 ``values["enabled"]``，
    ``LongTermRuntimeConfig(**values)`` 抛未 typed TypeError（D9"typed 错误 →
    强制 off+audit"契约失效）。ini key 与运行时字段名已解耦：
    config 文件 key 仍是 ``enabled``，字段是 ``quality_enabled``。
    """
    from a2a.coordinator.memory.contracts import (
        LongTermRuntimeConfig,
        load_long_term_config,
    )

    repo_root = Path(__file__).resolve().parent.parent
    config_file = repo_root / "long_term.config"
    assert config_file.exists(), "仓库根 long_term.config 必须存在（F1 回归依赖）"
    cfg = load_long_term_config(config_file)
    assert isinstance(cfg, LongTermRuntimeConfig)
    assert cfg.quality_enabled is True
    assert cfg.max_events == 200
    assert cfg.max_chars == 8000
    assert cfg.task_complete is True
    assert cfg.supervision_event is True
    assert cfg.every_env_step == 5
    assert cfg.min_interval_sec == 30
    assert cfg.reflection_sec == 60
    assert cfg.long_term_mode == "off"


def test_load_long_term_config_quality_section_absent_uses_default():
    """[quality] 段缺失 → quality_enabled 落到默认 True（D9 缺失项用默认值），
    且绝不因字段名映射产生 TypeError。"""
    from a2a.coordinator.memory.contracts import load_long_term_config

    config_file = Path(__file__).resolve().parent / "no_such_long_term_config.ini"
    config_file.write_text(
        "[window]\nmax_events = 99\n", encoding="utf-8"
    )
    try:
        cfg = load_long_term_config(config_file)
        assert cfg.quality_enabled is True
        assert cfg.max_events == 99
    finally:
        config_file.unlink(missing_ok=True)

