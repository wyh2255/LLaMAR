"""Phase 0（P0）契约卡：DiagnosisCandidateV1 诊断输出契约（主方案 §3.2 / D3-D4 / R2）。

目标契约：
- 独立于 LongTermMemoryCandidateV1 的 validator ``validate_diagnosis_response``，
  复用 reflection.py:134-211 纯校验段模式（形状 / 去重 / truth 词 / ref
  可验证性 / confidence 全 rejected 路径，零领域写入）；
- 字段：diagnosis_key（系统派生 = SHA-256(canonical_json(scope+target+kind+
  statement))）、kind=diagnosis、target ∈ {coordinator, worker:<id>, system}、
  finding、suggestion、confidence（0-1）、source_refs（只允许指向四类在线
  证据的 event_id，**禁指向既有诊断** = 防回声室 R2）、policy_version；
- 短命生命周期（B3）：独立 store `<memory_root>/diagnosis/diagnosis.sqlite3`
  （D6，模板 = LongTermMemoryStore），不跨 run、不参与长期记忆 supersede 链；
- 诊断与滚动反思并存（A4）：本契约只覆盖诊断通道本身。

当前代码事实（32bfe57）：
- ``a2a.coordinator.memory.diagnosis`` 模块不存在 → ImportError（预期 RED）；
- contracts.py 无 ``DiagnosisConfig`` → ImportError（预期 RED）；
- FORBIDDEN_TRUTH_TERMS / scan_forbidden_truth_fields 已存在 → GREEN 守护
  （validator 必须复用同一词表，contracts.py:87-106）。

P2 实现后转 GREEN。API 路径 / 结果类型字段 / input_window 形状 / 字段归属
（candidate 提供 target/finding/suggestion/confidence/source_refs，系统派生
diagnosis_key + kind + policy_version）待 P2 确认（探索 02 §6：独立
validator/DTO 模板 = reflection.py:113-215）。
"""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import (
    FORBIDDEN_TRUTH_TERMS,
    scan_forbidden_truth_fields,
)


def _candidate(**overrides):
    """LLM 侧候选（candidate 字段归属待 P2 确认：validator 负责派生
    diagnosis_key/kind/policy_version）。"""
    candidate = {
        "target": "coordinator",
        "finding": "coordinator assigned overlapping fire tasks",
        "suggestion": "deduplicate tasks by fire location",
        "confidence": 0.6,
        "source_refs": [("scope-a", "evt_ctl")],
    }
    candidate.update(overrides)
    return candidate


def _online_window():
    """四类在线证据窗口（source_refs 唯一合法指向，§3.2）。"""
    return [
        {"scope_id": "scope-a", "event_id": "evt_ctl", "event_type": "control.dispatch.RUNNING", "sequence": 1},
        {"scope_id": "scope-a", "event_id": "evt_cb", "event_type": "callback.status_update", "sequence": 2},
        {"scope_id": "scope-a", "event_id": "evt_ev", "event_type": "evidence.projection", "sequence": 3},
        {"scope_id": "scope-a", "event_id": "evt_sup", "event_type": "supervision.WORKER_UNREACHABLE", "sequence": 4},
    ]


def _echo_window():
    """含既有诊断 audit 事件的窗口（防回声室测试用，R2/R6）。"""
    return _online_window() + [
        {"scope_id": "scope-a", "event_id": "evt_diag", "event_type": "diagnosis.audit", "sequence": 9}
    ]


# ---------------------------------------------------------------------------
# RED —— validate_diagnosis_response（P2 新增，现不存在）
# ---------------------------------------------------------------------------


def test_diagnosis_module_import():
    """§3.2：``DiagnosisCandidateV1`` / ``validate_diagnosis_response`` /
    ``DiagnosisValidationResult`` 必须可导入。

    当前 a2a.coordinator.memory.diagnosis 不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import (  # noqa: F401
        DiagnosisCandidateV1,
        DiagnosisValidationResult,
        validate_diagnosis_response,
    )


def test_requires_function_call_response():
    """§3.2（复用 reflection.py:136-137）：缺 function-call 的响应一律
    fail closed，零诊断写入。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"content": "这里没有 function call", "finish_reason": "stop"}
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_invalid_response_shape_rejected():
    """§3.2（复用 reflection.py:134-135）：非 dict 响应 → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response("not-a-dict")
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_empty_batch_rejected():
    """§3.2（复用 reflection.py:145-146）：空批 → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response({"function_call": []})
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_invalid_candidate_shape_rejected():
    """§3.2（复用 reflection.py:152-153）：候选非 dict → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response({"function_call": ["not-a-dict"]})
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_missing_candidate_field_rejected():
    """§3.2：缺 candidate 字段（target/finding/suggestion/confidence/
    source_refs）→ rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    candidate = _candidate()
    del candidate["suggestion"]
    result = validate_diagnosis_response(
        {"function_call": candidate}, input_window=_online_window()
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_empty_finding_rejected():
    """§3.2（复用 reflection.py:167-174）：finding 为空 → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(finding="   ")}, input_window=_online_window()
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_invalid_target_rejected():
    """§3.2：target ∈ {coordinator, worker:<id>, system}——oracle / 裸 worker
    （无 id）/ coordinator:<id> 格式一律 rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    for target in ("oracle", "worker", "worker:", "coordinator:1", "system:root", ""):
        result = validate_diagnosis_response(
            {"function_call": _candidate(target=target)}, input_window=_online_window()
        )
        assert result.status == "rejected", f"target={target!r} 必须拒绝"
        assert result.diagnosis_written == 0


def test_truth_term_in_finding_rejected():
    """§3.2（复用 contracts.py:87-106 词表）：finding 含 FORBIDDEN_TRUTH_TERMS
    → rejected（诊断不得引用 oracle/ground_truth，R1）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(finding=f"matches {min(FORBIDDEN_TRUTH_TERMS)}")},
        input_window=_online_window(),
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_truth_term_in_suggestion_rejected():
    """§3.2：truth 词扫描必须同时覆盖 suggestion（建议也不得引用真值通道）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(suggestion="verify via the oracle")},
        input_window=_online_window(),
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_forged_source_ref_rejected():
    """§3.2（复用 reflection.py:191-193）：source_ref 不在输入窗口内 →
    forged_source_ref，rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(source_refs=[("scope-a", "evt-not-in-window")])},
        input_window=_online_window(),
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_unverifiable_source_ref_without_window_rejected():
    """§3.2（复用 reflection.py:188-190）：无窗口时任何非空 source_refs 一律
    不可验证 → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response({"function_call": _candidate()})
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_echo_chamber_source_ref_rejected():
    """§3.2 / R2 防回声室：source_ref 指向既有诊断（diagnosis.audit 事件）
    → rejected——诊断只允许引用四类在线证据，禁引诊断。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(source_refs=[("scope-a", "evt_diag")])},
        input_window=_echo_window(),
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_four_online_evidence_source_refs_accepted():
    """§3.2：source_refs 指向四类在线证据（control./callback./evidence./
    supervision.）全部放行。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {
            "function_call": _candidate(
                source_refs=[("scope-a", "evt_ctl"), ("scope-a", "evt_cb"), ("scope-a", "evt_ev"), ("scope-a", "evt_sup")]
            )
        },
        input_window=_online_window(),
    )
    assert result.status == "ok"
    assert len(result.candidates) == 1


def test_invalid_confidence_rejected():
    """§3.2（复用 reflection.py:196-198）：非数值 confidence → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate(confidence="high")}, input_window=_online_window()
    )
    assert result.status == "rejected"
    assert result.diagnosis_written == 0


def test_out_of_range_confidence_rejected():
    """§3.2（复用 reflection.py:199-211 DTO fail-closed）：confidence 越界
    [0,1] → rejected。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    for confidence in (1.5, -0.1):
        result = validate_diagnosis_response(
            {"function_call": _candidate(confidence=confidence)},
            input_window=_online_window(),
        )
        assert result.status == "rejected", f"confidence={confidence} 必须拒绝"
        assert result.diagnosis_written == 0


def test_ok_candidate_fields_kind_and_diagnosis_key():
    """§3.2：ok 路径候选必须携带系统派生字段——kind=diagnosis、
    diagnosis_key（64-hex SHA-256）、policy_version，且 target/finding/
    suggestion/confidence/source_refs 原样保留。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    result = validate_diagnosis_response(
        {"function_call": _candidate()}, input_window=_online_window()
    )
    assert result.status == "ok"
    assert result.diagnosis_written == 0
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.kind == "diagnosis"
    assert candidate.target == "coordinator"
    assert candidate.finding == "coordinator assigned overlapping fire tasks"
    assert candidate.suggestion == "deduplicate tasks by fire location"
    assert candidate.confidence == 0.6
    assert candidate.source_refs == (("scope-a", "evt_ctl"),)
    assert isinstance(candidate.policy_version, str) and candidate.policy_version
    assert len(candidate.diagnosis_key) == 64  # SHA-256(canonical_json(scope+target+kind+statement))


def test_diagnosis_key_deterministic_and_input_sensitive():
    """§3.2：diagnosis_key = SHA-256(canonical_json(scope+target+kind+
    statement))——确定性；finding/target 变化 → key 变化（同输入两次调用
    必须同 key）。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    first = validate_diagnosis_response(
        {"function_call": _candidate()}, input_window=_online_window()
    )
    second = validate_diagnosis_response(
        {"function_call": _candidate()}, input_window=_online_window()
    )
    assert first.status == "ok" and second.status == "ok"
    assert first.candidates[0].diagnosis_key == second.candidates[0].diagnosis_key

    finding_changed = validate_diagnosis_response(
        {"function_call": _candidate(finding="different finding text")},
        input_window=_online_window(),
    )
    target_changed = validate_diagnosis_response(
        {"function_call": _candidate(target="system")},
        input_window=_online_window(),
    )
    assert finding_changed.candidates[0].diagnosis_key != first.candidates[0].diagnosis_key
    assert target_changed.candidates[0].diagnosis_key != first.candidates[0].diagnosis_key


def test_valid_targets_accepted():
    """§3.2：target ∈ {coordinator, worker:<id>, system} 三形态全部放行。

    当前不存在 → ImportError（预期 RED）。
    """
    from a2a.coordinator.memory.diagnosis import validate_diagnosis_response

    for target in ("coordinator", "system", "worker:bob"):
        result = validate_diagnosis_response(
            {"function_call": _candidate(target=target)}, input_window=_online_window()
        )
        assert result.status == "ok", f"target={target!r} 必须放行"
        assert result.candidates[0].target == target


# ---------------------------------------------------------------------------
# RED —— DiagnosisConfig / DiagnosisMemoryStore（P2 新增，现不存在）
# ---------------------------------------------------------------------------


def test_diagnosis_config_defaults(tmp_path):
    """§3.2 / D4 / D6 / D7 / A2：``[diagnosis]`` 配置段默认值——inject_enabled
    默认 true（独立旋钮）、min_confidence=0.6（注入阈值）、max_rounds=3 /
    diagnosis_sec=90（agentic 轮次预算）；db 路径 = <memory_root>/diagnosis/
    diagnosis.sqlite3（独立文件，模板 = LongTermMemoryStore）。

    当前 contracts.py 无 DiagnosisConfig → ImportError（预期 RED）；
    API 形状待 P2 确认。
    """
    from a2a.coordinator.memory.contracts import DiagnosisConfig

    cfg = DiagnosisConfig(experiment_id="run-1", memory_root=tmp_path)
    assert cfg.inject_enabled is True
    assert cfg.min_confidence == 0.6
    assert cfg.max_rounds == 3
    assert cfg.diagnosis_sec == 90
    assert cfg.diagnosis_db_path == tmp_path / "diagnosis" / "diagnosis.sqlite3"
    # A2：独立旋钮可显式关（消融实验开/关诊断对比，不切换 long_term_mode）
    cfg_off = DiagnosisConfig(experiment_id="run-1", memory_root=tmp_path, inject_enabled=False)
    assert cfg_off.inject_enabled is False


def test_diagnosis_store_path_and_short_lived_lifecycle(tmp_path):
    """§3.2 / B3 / D6：诊断独立 store（短命生命周期）——新 run / 新 scope
    零诊断残留（不跨 run、不参与长期记忆 supersede 链）。

    当前 a2a.coordinator.memory.diagnosis 不存在 → ImportError（预期 RED）；
    API 形状待 P2 确认。
    """
    from a2a.coordinator.memory.contracts import DiagnosisConfig
    from a2a.coordinator.memory.diagnosis import DiagnosisMemoryStore

    cfg = DiagnosisConfig(experiment_id="run-1", memory_root=tmp_path)
    store = DiagnosisMemoryStore(cfg.diagnosis_db_path)
    store.open()
    assert cfg.diagnosis_db_path.exists()
    assert store.diagnoses("run-a-scope") == []  # 新 run 零诊断，不跨 run


# ---------------------------------------------------------------------------
# GREEN 守护 —— validator 必须复用既有 truth 词表
# ---------------------------------------------------------------------------


def test_forbidden_truth_terms_wordlist_shared():
    """GREEN 守护：诊断 validator 的 truth 词表必须复用 contracts.py:87-106
    （与 validate_reflection_response 同一词表，探索 02 §6.1）。

    现在成立；P2 若另起词表本测试转红。
    """
    assert "oracle" in FORBIDDEN_TRUTH_TERMS
    assert scan_forbidden_truth_fields({"finding": "matches ground_truth"})
    assert not scan_forbidden_truth_fields({"finding": "coordinator assigned tasks"})


# ---------------------------------------------------------------------------
# R3 修订 —— section_budget_threshold 配置化（DiagnosisConfig.validate /
# load_diagnosis_config）
# ---------------------------------------------------------------------------


def test_section_budget_threshold_default_three(tmp_path):
    """R3 修订: ``section_budget_threshold`` 默认 3（与 long_term_memory 同档，
    配置面默认值不得改变既有行为）。"""
    from a2a.coordinator.memory.contracts import DiagnosisConfig

    cfg = DiagnosisConfig(experiment_id="run-1", memory_root=tmp_path)
    assert cfg.section_budget_threshold == 3


def test_section_budget_threshold_invalid_values_rejected(tmp_path):
    """R3 修订: ``section_budget_threshold`` 必须是 int（排除 bool）且 >= 1；
    0 / -1 / "3" → MemoryConfigError（与 invalid_max_rounds 同风格）。"""
    from a2a.coordinator.memory.contracts import (
        DiagnosisConfig,
        MemoryConfigError,
    )

    for bad in (0, -1, "3"):
        cfg = DiagnosisConfig(
            experiment_id="run-1",
            memory_root=tmp_path,
            section_budget_threshold=bad,
        )
        with pytest.raises(MemoryConfigError) as excinfo:
            cfg.validate()
        assert excinfo.value.code == "invalid_section_budget_threshold"


def test_load_diagnosis_config_section_budget_threshold_parsed(tmp_path):
    """R3 修订: ini ``section_budget_threshold = 4`` → 解析为 4（其余键走
    默认值）。"""
    from a2a.coordinator.memory.contracts import load_diagnosis_config

    config = tmp_path / "long_term.config"
    config.write_text(
        "[diagnosis]\nsection_budget_threshold = 4\n", encoding="utf-8"
    )
    parsed = load_diagnosis_config(config)
    assert parsed.section_budget_threshold == 4
    assert parsed.max_rounds == 3  # 缺失键仍走默认值


def test_load_diagnosis_config_section_budget_threshold_zero_rejected(tmp_path):
    """R3 修订: ini ``section_budget_threshold = 0`` → DiagnosisConfigError
    （>= 1 校验，fail-closed，不静默回退默认）。"""
    from a2a.coordinator.memory.contracts import (
        DiagnosisConfigError,
        load_diagnosis_config,
    )

    config = tmp_path / "long_term.config"
    config.write_text(
        "[diagnosis]\nsection_budget_threshold = 0\n", encoding="utf-8"
    )
    with pytest.raises(DiagnosisConfigError):
        load_diagnosis_config(config)


def test_load_diagnosis_config_section_budget_threshold_non_int_rejected(tmp_path):
    """R3 修订: ini ``section_budget_threshold = abc`` → DiagnosisConfigError
    （非整数，fail-closed）。"""
    from a2a.coordinator.memory.contracts import (
        DiagnosisConfigError,
        load_diagnosis_config,
    )

    config = tmp_path / "long_term.config"
    config.write_text(
        "[diagnosis]\nsection_budget_threshold = abc\n", encoding="utf-8"
    )
    with pytest.raises(DiagnosisConfigError):
        load_diagnosis_config(config)



def test_min_confidence_bool_rejected(tmp_path):
    """2026-08-16 review Minor 4: ``min_confidence=True/False`` 必须被拒绝
    （bool 是 int 子类，旧校验会放行；与 max_rounds /
    section_budget_threshold 的 bool 排除风格一致）。"""
    from a2a.coordinator.memory.contracts import (
        DiagnosisConfig,
        MemoryConfigError,
    )

    for bad in (True, False):
        cfg = DiagnosisConfig(
            experiment_id="run-1",
            memory_root=tmp_path,
            min_confidence=bad,
        )
        with pytest.raises(MemoryConfigError) as excinfo:
            cfg.validate()
        assert excinfo.value.code == "invalid_min_confidence"
