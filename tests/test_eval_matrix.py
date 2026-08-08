"""P6 offline evaluator-harness matrix 单测（设计 §0.6、§3.2、§9 P6.1-6）。

测试全部使用 synthetic result roots（tmp_path 下构造），**绝不读取或改写真实
历史 results**。覆盖：七格/selector、路径逃逸、symlink/hardlink、metadata/
field/redaction/secret/NDJSON、supervision missing/nonempty、digest drift、
taxonomy mismatch、subject mismatch、verifier no discovery、harness errors
blocking、cleanup/summary、stable double run/resume、FakeRunner no network。

projection 语义：source 端非 allowlist 的 metadata key / CSV 列**剥离并记录名称**
（stage_metadata / stage_csv_file 返回 stripped 名单，写入 manifest 审计），不再
抛错；fail-closed 落在 staged 产物（staged metadata 非九字段、staged CSV 非
allowlist 列、staged root 额外文件/目录/symlink/hardlink、secret/Thinking/raw
NDJSON 一律违规，由 assert_staged_metadata_keys / _assert_csv_header_clean /
defense_scan 承担）。

不运行真实 LLM / calibration / 网络。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import matrix as mx
from sar_orch.eval import p6_staging_policy as p
from sar_orch.eval import prepare_matrix as pm
from sar_orch.eval import run_matrix as rm
from sar_orch.eval import verify_matrix as vm
from sar_orch.eval import workflow as w
from sar_orch.eval.agent import runner as r

# ─────────────────────────────────────────────────────────────────────────────
# synthetic source 构造（不碰真实 results）
# ─────────────────────────────────────────────────────────────────────────────

TRAJ_HEADER = (
    "Step,Actions,Successes,Observations,Coverage,TransportRate,Finished,"
    "MapRecall,Freshness,TimeoutAgents,RunID,MaxSteps,RemainingSteps,"
    "WallTimeSinceStart,StepDurationMs,ErrorTypes,CompletedSubtasksDelta,EndReason"
)
ROUTER_HEADER = "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType"
SUBTASKS_HEADER = "RunID,Step,SubtaskID,Status,AssignedTo,Subtask,CreatedAt,UpdatedAt,FailureClass,Details"
AGENT_HEADER = (
    "Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,Thinking,"
    "RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType"
)
TOKEN_HEADER = (
    "Step,Agent,PromptTokens,CompletionTokens,TotalTokens,CacheHitTokens,"
    "CacheMissTokens,RunID,LLMLatencyMs,Model,PromptVersion"
)
SUMMARY_HEADER = (
    "ExperimentName,LogDir,TotalSteps,FinalCoverage,FinalTransportRate,Finished,"
    "TotalAgentInteractions,TotalRouterInteractions,AlicePromptTokens,"
    "AliceCompletionTokens,AliceTotalTokens,AliceCacheHitTokens,AliceCacheMissTokens,"
    "BobPromptTokens,BobCompletionTokens,BobTotalTokens,BobCacheHitTokens,"
    "BobCacheMissTokens,CoordinatorPromptTokens,CoordinatorCompletionTokens,"
    "CoordinatorTotalTokens,CoordinatorCacheHitTokens,CoordinatorCacheMissTokens,"
    "MapAgentPromptTokens,MapAgentCompletionTokens,MapAgentTotalTokens,"
    "MapAgentCacheHitTokens,MapAgentCacheMissTokens,MapSummarizerPromptTokens,"
    "MapSummarizerCompletionTokens,MapSummarizerTotalTokens,MapSummarizerCacheHitTokens,"
    "MapSummarizerCacheMissTokens"
)

SEMANTIC_MAP_RECORD = {
    "ts": 1785859214.0,
    "event_type": "observation_ingested",
    "observation": {
        "reporter": "Bob",
        "step": 0,
        "object_type": "fire",
        "name": "CaldorFire",
        "position": [4, 4, 0],
        "attributes": {"average_intensity": "Low", "fire_type": "Chemical"},
        "confidence": 1.0,
        "source_task_id": "",
        "note": "",
    },
    "object": {
        "object_type": "fire",
        "name": "CaldorFire",
        "position": [4, 4, 0],
        "attributes": {
            "average_intensity": "Low",
            "fire_type": "Chemical",
            "observed_cells": [{"name": "CaldorFire", "position": [4, 4, 0]}],
        },
        "status": "unknown",
        "last_seen_step": 0,
        "last_seen_ts": 1785859214.0,
        "sources": [
            {"reporter": "Bob", "task_id": "", "step": 0, "confidence": 1.0, "note": ""}
        ],
        "confidence": 1.0,
        "conflict": False,
    },
}

MAP_SUMMARY_RECORD = {
    "env_step": 1,
    "base_revision": 1,
    "map_revision": 10,
    "trigger_reasons": ["fire_change", "periodic"],
    "status": "success",
    "summary": "第1步：新增两处低强度火源。",
    "token_usage": {
        "prompt_tokens": 449,
        "completion_tokens": 237,
        "total_tokens": 686,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 449,
    },
    "timestamp": 1785859221.0,
}


def make_source_cell(
    source_dir: Path,
    *,
    cell_id: str,
    scene: int,
    agents: int,
    error_codes: dict[str, int],
    unclassified: int = 0,
    phantom: bool = False,
    secret_observation: str | None = None,
) -> None:
    """构造 synthetic source cell：9 类 executable inputs + 空 supervision/ +
    coordinator/workers NDJSON inventory。"""
    source_dir.mkdir(parents=True, exist_ok=True)
    names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:agents]

    metadata = {
        "run_id": f"run-{cell_id}",
        "scene": scene,
        "seed": 42,
        "agent_count": agents,
        "agent_names": names,
        "prompt_version": "eval_diag_full",
        "code_commit": "4d6d5e7-dirty",
        "git_dirty": True,
        "max_steps": 30,
    }
    (source_dir / "metadata.json").write_text(json.dumps(metadata))

    traj_rows = [
        '1,"[\'Explore()\', \'Explore()\']","[True, True]","[\'ok\', \'ok\']",0.1,0.0,false,0.0,0.0,"[]",run1,30,29,0.0,1,0,"[]",""',
        '2,"[\'Move()\']","[True]","[\'ok\']",0.2,0.1,false,0.0,0.0,"[]",run1,30,28,0.0,1,0,"[]",""',
    ]
    (source_dir / "trajectory.csv").write_text(
        TRAJ_HEADER + "\n" + "\n".join(traj_rows) + "\n"
    )

    router_rows = ["1,Explore the area,Alice,run1,c1,w1,dispatch"]
    (source_dir / "router_interactions.csv").write_text(
        ROUTER_HEADER + "\n" + "\n".join(router_rows) + "\n"
    )

    subtask_rows = ['run1,1,t1,completed,Alice,Explore,A,B,none,""']
    (source_dir / "subtasks.csv").write_text(
        SUBTASKS_HEADER + "\n" + "\n".join(subtask_rows) + "\n"
    )

    agent_rows = []
    if phantom:
        # phantom_first_row：代表行是 tool-execution error + 同 (step, agent) 后续行。
        agent_rows = [
            '1,Bob,Move,"{}",Move(Down),Error: Tool execution failed: AssertionError,LLM1,LLM2,think,run1,c2,step,5,""',
            '1,Bob,Move,"{}",Move(Down),I tried to Move and was successful.,LLM1,LLM2,think,run1,c2,step,5,""',
        ]
    else:
        obs = secret_observation or "I tried to Move and was successful."
        agent_rows = [
            f'1,Alice,Move,"{{}}",Move(Down),{obs},LLM1,LLM2,think,run1,c1,step,5,""'
        ]
    (source_dir / "agent_interactions.csv").write_text(
        AGENT_HEADER + "\n" + "\n".join(agent_rows) + "\n"
    )

    (source_dir / "summary.csv").write_text(
        SUMMARY_HEADER
        + "\n"
        + "exp,log,2,0.2,0.1,false,1,1,10,5,15,0,0,10,5,15,0,0,20,10,30,0,0,0,0,0,0,0,0,0,0,0,0\n"
    )
    (source_dir / "token_usage.csv").write_text(
        TOKEN_HEADER
        + "\n"
        + "1,Alice,10,5,15,0,0,run1,100,deepseek-v4-flash,eval_diag_full\n"
    )
    (source_dir / "semantic_map.jsonl").write_text(
        json.dumps(SEMANTIC_MAP_RECORD) + "\n"
    )
    (source_dir / "map_summary.jsonl").write_text(json.dumps(MAP_SUMMARY_RECORD) + "\n")
    (source_dir / "supervision").mkdir()

    # NDJSON inventory（coordinator + workers）
    coord_dir = source_dir / "coordinator"
    coord_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for code, count in error_codes.items():
        for _ in range(count):
            lines.append(
                json.dumps(
                    {
                        "ts": 1785859214.0,
                        "event": "tool_result",
                        "tool_name": "send_message",
                        "success": False,
                        "result": "",
                        "error": code,
                    }
                )
            )
    for _ in range(unclassified):
        lines.append(
            json.dumps(
                {
                    "ts": 1785859214.0,
                    "event": "tool_result",
                    "tool_name": "send_message",
                    "success": False,
                    "result": "",
                    "error": "orchestration timeout after 1200s",
                }
            )
        )
    (coord_dir / "router.ndjson").write_text("\n".join(lines) + ("\n" if lines else ""))
    workers_dir = source_dir / "workers"
    for name in names[:1]:
        wdir = workers_dir / name / name
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "task.ndjson").write_text(
            json.dumps(
                {
                    "ts": "2026-08-04T16:00:00+00:00",
                    "event": "tool_result",
                    "tool_name": "navigate_to",
                    "success": True,
                    "result": "ok",
                    "error": "",
                }
            )
            + "\n"
        )


#: synthetic spec 各格期望 source taxonomy（与 default_source_spec 冻结值一致）。
SYNTH_SOURCE_TAX = {
    "s1_a2": {},
    "s2_a2": {"unknown_task_id": 1, "no_worker": 1, "participant_busy": 1},
    "s3_a2": {},
    "s3_a4": {},
    "s4_a2": {"no_worker": 3, "participant_busy": 1},
    "s5_a2": {
        "unknown_task_id": 1,
        "no_worker": 11,
        "participant_busy": 3,
        "unclassified_nonempty_error_count": 3,
    },
    "s5_a4": {
        "unknown_task_id": 1,
        "no_worker": 15,
        "node_not_found": 1,
        "unclassified_nonempty_error_count": 2,
    },
}

SYNTH_GRADER_SKIP = {
    "s1_a2": {"supervision_missing_or_empty": 1},
    "s2_a2": {"supervision_missing_or_empty": 1},
    "s3_a2": {"supervision_missing_or_empty": 1},
    "s3_a4": {"supervision_missing_or_empty": 1},
    "s4_a2": {"supervision_missing_or_empty": 1},
    "s5_a2": {
        "supervision_missing_or_empty": 1,
        "tool_execution_representative_error": 1,
    },
    "s5_a4": {"supervision_missing_or_empty": 1},
}

#: 各格 NDJSON 注入的 error codes（应精确命中冻结 taxonomy）。
SYNTH_NDJSON = {
    "s1_a2": ({"unknown_task_id": 0}, 0),
    "s2_a2": ({"unknown_task_id": 1, "no_worker": 1, "participant_busy": 1}, 0),
    "s3_a2": ({}, 0),
    "s3_a4": ({}, 0),
    "s4_a2": ({"no_worker": 3, "participant_busy": 1}, 0),
    "s5_a2": ({"unknown_task_id": 1, "no_worker": 11, "participant_busy": 3}, 3),
    "s5_a4": ({"unknown_task_id": 1, "no_worker": 15, "node_not_found": 1}, 2),
}


def make_synthetic_cohort(results_root: Path) -> mx.P6SourceSpec:
    """在 tmp results-root 下构造 7-cell cohort，并返回对应 synthetic spec。"""
    cohort_dir = results_root / mx.P6_COHORT
    cells = []
    for cell_id in mx.P6_CELL_IDS:
        scene, agents = cell_id.split("_")
        scene, agents = int(scene[1:]), int(agents[1:])
        source_rel = mx._cell_source_rel(cell_id)
        source_dir = results_root / source_rel
        codes, unclass = SYNTH_NDJSON[cell_id]
        make_source_cell(
            source_dir,
            cell_id=cell_id,
            scene=scene,
            agents=agents,
            error_codes=codes,
            unclassified=unclass,
            phantom=(cell_id == "s5_a2"),
        )
        zero = {c: 0 for c in mx.TARGET_SOURCE_ERROR_CODES}
        zero.update({c: 0 for c in mx.SAFE_SOURCE_ERROR_CODES})
        zero["unclassified_nonempty_error_count"] = 0
        tax = dict(zero)
        tax.update(SYNTH_SOURCE_TAX[cell_id])
        cells.append(
            mx.P6CellSpec(
                cell_id=cell_id,
                source_rel=source_rel,
                scene=scene,
                agents=agents,
                seed=42,
                repetition="r1",
                expected_metadata_fingerprint={
                    "prompt_version": "eval_diag_full",
                    "code_commit": "4d6d5e7-dirty",
                    "max_steps": 30,
                    "seed": 42,
                },
                expected_source_error_taxonomy=tax,
                expected_grader_skip_taxonomy=dict(SYNTH_GRADER_SKIP[cell_id]),
            )
        )
    spec = mx.P6SourceSpec(cohort=mx.P6_COHORT, cells=cells)
    _ = cohort_dir  # cohort dir implied by cell dirs
    return spec


def write_spec_json(spec: mx.P6SourceSpec, path: Path) -> None:
    path.write_text(spec.canonical_json(), encoding="utf-8")


def make_valid_staged_dir(staged: Path) -> None:
    """构造 policy 合法的 minimal staged dir：固定 10 文件 + 空 supervision/。

    agent_interactions 用 staged 规范 header（无 LLM 三列）；metadata 为空对象。
    """
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "metadata.json").write_text("{}")
    (staged / "trajectory.csv").write_text(TRAJ_HEADER + "\n")
    (staged / "router_interactions.csv").write_text(ROUTER_HEADER + "\n")
    (staged / "subtasks.csv").write_text(SUBTASKS_HEADER + "\n")
    (staged / "agent_interactions.csv").write_text(
        ",".join(p.AGENT_INTERACTIONS_HEADERS) + "\n"
    )
    (staged / "summary.csv").write_text(SUMMARY_HEADER + "\n")
    (staged / "token_usage.csv").write_text(TOKEN_HEADER + "\n")
    (staged / "semantic_map.jsonl").write_text("")
    (staged / "map_summary.jsonl").write_text("")
    (staged / "supervision").mkdir()
    (staged / mx.SUPERVISION_STATE_FILENAME).write_bytes(p.supervision_state_bytes())


# ─────────────────────────────────────────────────────────────────────────────
# P6SourceSpec / selector
# ─────────────────────────────────────────────────────────────────────────────


class TestP6SourceSpec:
    def test_default_spec_has_seven_cells_and_unique_tuples(self):
        spec = mx.default_source_spec()
        assert [c.cell_id for c in spec.cells] == list(mx.P6_CELL_IDS)
        tuples = [c.subject_tuple for c in spec.cells]
        assert len(set(tuples)) == 7
        assert all(c.repetition == "r1" for c in spec.cells)
        assert all(c.seed == 42 for c in spec.cells)
        fp = spec.cells[0].expected_metadata_fingerprint
        assert fp["prompt_version"] == "eval_diag_full"
        assert fp["code_commit"] == "4d6d5e7-dirty"
        assert fp["max_steps"] == 30

    def test_rejects_absolute_source_rel(self):
        with pytest.raises(ValueError, match="relative"):
            mx.validate_source_rel("/abs/path/s1_s42_a2_r1")

    def test_rejects_dotdot_traversal(self):
        with pytest.raises(ValueError, match=r"\.\."):
            mx.validate_source_rel("cohort/../s1_s42_a2_r1")

    def test_rejects_glob_and_latest(self):
        with pytest.raises(ValueError, match="glob"):
            mx.validate_source_rel("cohort/s1_s42_a2_r*")
        with pytest.raises(ValueError, match="latest"):
            mx.validate_source_rel("cohort/latest")

    def test_rejects_duplicate_source_rel(self):
        base = mx.P6CellSpec(
            cell_id="s1_a2",
            source_rel="cohort/s1_s42_a2_r1",
            scene=1,
            agents=2,
            seed=42,
            repetition="r1",
            expected_metadata_fingerprint={},
            expected_source_error_taxonomy={},
            expected_grader_skip_taxonomy={},
        )
        dup = mx.P6CellSpec(
            cell_id="s1_a2",
            source_rel="cohort/s1_s42_a2_r1",
            scene=1,
            agents=2,
            seed=42,
            repetition="r1",
            expected_metadata_fingerprint={},
            expected_source_error_taxonomy={},
            expected_grader_skip_taxonomy={},
        )
        with pytest.raises(ValueError, match="duplicate source_rel"):
            mx.P6SourceSpec(cohort="c", cells=[base, dup])

    def test_rejects_duplicate_cell_tuple(self):
        # 同一 tuple 的两格（同一 cell identity）→ 重复，fail-closed。
        c1 = mx.P6CellSpec(
            cell_id="s1_a2",
            source_rel="cohort/s1_s42_a2_r1",
            scene=1,
            agents=2,
            seed=42,
            repetition="r1",
            expected_metadata_fingerprint={},
            expected_source_error_taxonomy={},
            expected_grader_skip_taxonomy={},
        )
        c2 = mx.P6CellSpec(
            cell_id="s1_a2",
            source_rel="cohort/s1_s42_a2_r1",
            scene=1,
            agents=2,
            seed=42,
            repetition="r1",
            expected_metadata_fingerprint={},
            expected_source_error_taxonomy={},
            expected_grader_skip_taxonomy={},
        )
        with pytest.raises(ValueError, match="duplicate"):
            mx.P6SourceSpec(cohort="c", cells=[c1, c2])

    def test_rejects_missing_cell(self, tmp_path):
        spec = make_synthetic_cohort(tmp_path / "results")
        # 缺一格的 spec 应在 selector 层被拒绝（缺格）。
        with pytest.raises(ValueError, match="must contain exactly the seven cells"):
            mx.P6SourceSpec(cohort=spec.cohort, cells=spec.cells[:-1])

    def test_spec_digest_is_stable(self):
        s1 = mx.default_source_spec()
        s2 = mx.default_source_spec()
        assert s1.digest() == s2.digest()


# ─────────────────────────────────────────────────────────────────────────────
# staging policy：metadata/field/redaction/secret/NDJSON
# ─────────────────────────────────────────────────────────────────────────────


class TestStagingPolicy:
    def test_metadata_keeps_only_allowed_fields(self):
        staged, stripped = p.stage_metadata(
            {
                "run_id": "x",
                "scene": 1,
                "seed": 42,
                "agent_count": 2,
                "agent_names": ["Alice"],
                "prompt_version": "eval_diag_full",
                "code_commit": "4d6d5e7-dirty",
                "git_dirty": True,
                "max_steps": 30,
            }
        )
        assert set(staged) == p.METADATA_ALLOWED_FIELDS
        assert stripped == []

    def test_metadata_denied_field_stripped_and_recorded(self):
        # 显式 denied key（api_base 等）→ source 端剥离并记录 key 名，不抛错。
        staged, stripped = p.stage_metadata({"scene": 1, "api_base": "x"})
        assert "api_base" not in staged
        assert stripped == ["api_base"]
        staged, stripped = p.stage_metadata(
            {
                "run_id": "x",
                "scene": 1,
                "seed": 42,
                "agent_count": 2,
                "agent_names": [],
                "prompt_version": "x",
                "code_commit": "y",
                "git_dirty": False,
                "max_steps": 30,
                "provider": "openai",
                "model": "deepseek-v4-flash",
            }
        )
        assert "provider" not in staged and "model" not in staged
        assert stripped == ["model", "provider"]
        assert set(staged) == p.METADATA_ALLOWED_FIELDS

    def test_metadata_unknown_field_stripped_and_recorded(self):
        # 未知 key（非 denied、非 allowlist）→ 同样剥离+记录，不抛错。
        staged, stripped = p.stage_metadata({"scene": 1, "temperature": 0.7})
        assert "temperature" not in staged
        assert stripped == ["temperature"]
        assert set(staged) == {"scene"}

    def test_stage_structured_file_metadata_strips_and_records(self):
        content = json.dumps({"scene": 1, "api_base": "x"}).encode("utf-8")
        staged, stripped = p.stage_structured_file("metadata.json", content)
        assert stripped == ["api_base"]
        data = json.loads(staged)
        assert set(data) == {"scene"}

    def test_staged_metadata_rejects_unknown_key(self):
        with pytest.raises(p.SemanticStagingError, match="unknown metadata key"):
            p.assert_staged_metadata_keys({"scene": 1, "api_base": "x"})

    def test_agent_interactions_drops_llm_columns(self):
        content = (
            b"Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType\n"
            b'1,Alice,Move,"{}",Move(Down),obs,i,o,t,run,c,e,5,""\n'
        )
        staged, stripped = p.stage_csv_file("agent_interactions.csv", content)
        header = staged.decode().splitlines()[0]
        assert "LLMInput" not in header
        assert "LLMOutput" not in header
        assert "Thinking" not in header
        assert "Step" in header and "Observation" in header
        assert stripped == ["LLMInput", "LLMOutput", "Thinking"]

    def test_redact_text_removes_thinking_and_secret(self):
        text, reasons = p.redact_text(
            "prefix <thinking>hidden chain</thinking> api_key=sk-topsecret suffix"
        )
        assert "<thinking>" not in text
        assert "sk-topsecret" not in text
        assert "[Thinking redacted]" in text or "[REDACTED]" in text
        assert reasons

    def test_redact_text_removes_test_sentinels(self):
        text, reasons = p.redact_text(
            f"a {p.TEST_SECRET_SENTINEL} b {p.TEST_THINKING_SENTINEL} c"
        )
        assert p.TEST_SECRET_SENTINEL not in text
        assert p.TEST_THINKING_SENTINEL not in text
        assert reasons

    def test_metadata_redaction_of_observation_string(self):
        staged, stripped = p.stage_metadata(
            {
                "run_id": "a",
                "scene": 1,
                "seed": 42,
                "agent_count": 2,
                "agent_names": [],
                "prompt_version": "x",
                "code_commit": "y",
                "git_dirty": False,
                "max_steps": 30,
            }
        )
        assert set(staged) == p.METADATA_ALLOWED_FIELDS
        assert stripped == []

    def test_policy_digest_stable_and_independent(self):
        d1 = p.policy_digest()
        d2 = p.policy_digest()
        assert d1 == d2
        assert len(d1) == 64

    def test_semantic_map_unknown_nested_key_fails_closed(self):
        record = dict(SEMANTIC_MAP_RECORD)
        record["object"] = dict(record["object"])
        record["object"]["unknown_field"] = 1
        with pytest.raises(p.SemanticStagingError, match="unknown semantic_map key"):
            p._stage_semantic_map_jsonl([record])

    def test_map_summary_unknown_token_key_fails_closed(self):
        record = dict(MAP_SUMMARY_RECORD)
        record["token_usage"] = dict(record["token_usage"])
        record["token_usage"]["extra"] = 1
        with pytest.raises(p.SemanticStagingError, match="unknown token_usage key"):
            p._stage_map_summary_jsonl([record])

    def test_defense_scan_rejects_raw_ndjson(self, tmp_path):
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "router.ndjson").write_text('{"error": "no_worker"}\n')
        violations = p.defense_scan(staged)
        assert any("raw NDJSON" in v for v in violations)

    def test_defense_scan_rejects_llm_header(self, tmp_path):
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "agent_interactions.csv").write_text(
            "Step,Agent,LLMInput,LLMOutput,Thinking\n1,Alice,x,y,z\n"
        )
        violations = p.defense_scan(staged)
        assert any("LLM" in v for v in violations)

    def test_defense_scan_rejects_unknown_csv_column(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        (staged / "trajectory.csv").write_text(
            "Step,Actions,Successes,Observations,Coverage,ExtraCol\n"
        )
        violations = p.defense_scan(staged)
        assert any("policy-disallowed column" in v for v in violations)

    def test_csv_unknown_column_stripped_and_recorded(self):
        content = (
            b"Step,Actions,Successes,Observations,Coverage,ExtraCol\n1,[],[],x,0.1,y\n"
        )
        staged, stripped = p.stage_csv_file("trajectory.csv", content)
        header = staged.decode().splitlines()[0]
        assert "ExtraCol" not in header
        assert stripped == ["ExtraCol"]
        assert header.startswith(
            "Step,Actions,Successes,Observations,Coverage,TransportRate"
        )

    def test_summary_unknown_column_stripped_and_recorded(self):
        content = b"ExperimentName,LogDir,TotalSteps,ExtraCol\nexp,log,2,zzz\n"
        staged, stripped = p.stage_csv_file("summary.csv", content)
        assert "ExtraCol" not in staged.decode().splitlines()[0]
        assert stripped == ["ExtraCol"]

    def test_agent_interactions_unknown_column_stripped_and_recorded(self):
        # LLM 三列与未知列走同一剥离+记录机制；staged 只含 allowlist 列。
        content = (
            b"Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,"
            b"Thinking,ExtraCol,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType\n"
            b'1,Alice,Move,"{}",Move(Down),obs,i,o,t,z,run,c,e,5,""\n'
        )
        staged, stripped = p.stage_csv_file("agent_interactions.csv", content)
        header = staged.decode().splitlines()[0]
        for col in ("LLMInput", "LLMOutput", "Thinking", "ExtraCol"):
            assert col not in header
        assert stripped == ["ExtraCol", "LLMInput", "LLMOutput", "Thinking"]

    def test_summary_allowed_token_columns_pass(self):
        content = (
            b"ExperimentName,LogDir,TotalSteps,FinalCoverage,FinalTransportRate,"
            b"Finished,TotalAgentInteractions,TotalRouterInteractions,"
            b"AlicePromptTokens,AliceCompletionTokens,AliceTotalTokens\n"
            b"exp,log,2,0.2,0.1,false,1,1,10,5,15\n"
        )
        staged, stripped = p.stage_csv_file("summary.csv", content)
        header = staged.decode().splitlines()[0]
        assert "AliceTotalTokens" in header
        assert "ExperimentName" in header
        assert stripped == []

    def test_defense_scan_passes_valid_staged_dir(self, tmp_path):
        staged = tmp_path / "valid_staged"
        make_valid_staged_dir(staged)
        assert p.defense_scan(staged) == []

    def test_defense_scan_rejects_unexpected_file(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        (staged / "extra.txt").write_text("leak")
        violations = p.defense_scan(staged)
        assert any("unexpected file" in v for v in violations)

    def test_defense_scan_rejects_unexpected_directory(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        (staged / "extra_dir").mkdir()
        violations = p.defense_scan(staged)
        assert any("unexpected directory" in v for v in violations)

    def test_defense_scan_rejects_unexpected_symlink(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        os.remove(staged / "summary.csv")
        os.symlink(tmp_path / "outside.csv", staged / "summary.csv")
        violations = p.defense_scan(staged)
        assert any("not a plain regular file" in v for v in violations)

    def test_defense_scan_rejects_hardlinked_allowed_file(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        twin = tmp_path / "twin_sentinel.json"
        twin.write_bytes(p.supervision_state_bytes())
        os.remove(staged / mx.SUPERVISION_STATE_FILENAME)
        os.link(twin, staged / mx.SUPERVISION_STATE_FILENAME)
        violations = p.defense_scan(staged)
        assert any("hardlink" in v for v in violations)

    def test_defense_scan_rejects_nonempty_supervision(self, tmp_path):
        staged = tmp_path / "staged"
        make_valid_staged_dir(staged)
        (staged / "supervision" / "x.json").write_text("{}")
        violations = p.defense_scan(staged)
        assert any("must be empty" in v for v in violations)


# ─────────────────────────────────────────────────────────────────────────────
# matrix digest 分离 / staging 物理 materialize
# ─────────────────────────────────────────────────────────────────────────────


class TestMatrixDigests:
    def test_raw_input_digest_changes_with_source(self, tmp_path):
        src = tmp_path / "src"
        make_source_cell(src, cell_id="s1_a2", scene=1, agents=2, error_codes={})
        d1 = mx.raw_input_digest(src)
        (src / "trajectory.csv").write_text(TRAJ_HEADER + "\n")
        d2 = mx.raw_input_digest(src)
        assert d1 != d2
        assert len(d1) == 64

    def test_raw_trace_digest_covers_inventory(self, tmp_path):
        src = tmp_path / "src"
        make_source_cell(
            src, cell_id="s1_a2", scene=1, agents=2, error_codes={"no_worker": 2}
        )
        d1, _files1 = mx.raw_trace_digest(src)
        assert len(d1) == 64
        (src / "coordinator" / "router.ndjson").write_text('{"error": "no_worker"}\n')
        d2, _ = mx.raw_trace_digest(src)
        assert d1 != d2

    def test_raw_provenance_is_tuple_hash(self):
        rd = mx.raw_provenance_digest("a" * 64, "b" * 64, "c" * 64)
        assert len(rd) == 64
        assert rd != "b" * 64

    def test_staged_semantic_digest_matches_workflow_snapshot(self, tmp_path):
        staged = tmp_path / "staged"
        make_source_cell(staged, cell_id="s1_a2", scene=1, agents=2, error_codes={})
        # 添加 sentinel（preparer 也会物化）。
        (staged / mx.SUPERVISION_STATE_FILENAME).write_bytes(
            p.supervision_state_bytes()
        )
        expected = mx.staged_semantic_digest(staged)
        # workflow 的 snapshot 覆盖同样的 10 文件。
        allowlist = w._source_allowlist(staged)
        assert mx.SUPERVISION_STATE_FILENAME in allowlist
        actual = a.snapshot_source_files(staged, allowlist).digest
        assert actual == expected

    def test_supervision_state_digest(self):
        assert len(mx.supervision_state_digest()) == 64


# ─────────────────────────────────────────────────────────────────────────────
# preparer
# ─────────────────────────────────────────────────────────────────────────────


def prepare_synthetic(
    tmp_path, *, keep_workdir=False
) -> tuple[Path, Path, mx.P6SourceSpec]:
    results_root = tmp_path / "results"
    spec = make_synthetic_cohort(results_root)
    work_root = tmp_path / "work"
    manifest = pm.prepare(spec, results_root=results_root, work_root=work_root)
    return manifest, results_root, spec


class TestPrepareMatrix:
    def test_prepare_materializes_seven_cells(self, tmp_path):
        manifest, _results, _spec = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        assert len(data["cells"]) == 7
        assert data["manifest_version"] == "p6-matrix-manifest-v1"
        # manifest 不内嵌自身 hash
        assert "matrix_manifest_sha256" not in data

    def test_staged_dirs_mode_0700_and_no_symlink(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        work = Path(manifest).parent
        staged = work / "staged"
        for cell_dir in staged.iterdir():
            assert cell_dir.is_dir()
            assert cell_dir.stat().st_mode & 0o777 == 0o700
            for f in cell_dir.rglob("*"):
                if f.is_file():
                    assert not f.is_symlink(), f
                    assert f.stat().st_nlink == 1, f

    def test_prepare_creates_empty_supervision_and_sentinel(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        staged = Path(manifest).parent / "staged" / "s1_a2"
        assert (staged / "supervision").is_dir()
        assert not any((staged / "supervision").iterdir())
        sentinel = staged / mx.SUPERVISION_STATE_FILENAME
        assert sentinel.exists()
        assert sentinel.read_bytes() == p.supervision_state_bytes()

    def test_prepare_rejects_missing_supervision(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        # 删掉一个 cell 的 supervision/ → preflight failure
        target = results_root / spec.cells[0].source_rel
        shutil.rmtree(target / "supervision")
        with pytest.raises(pm.PrepareError, match="supervision"):
            pm.prepare(spec, results_root=results_root, work_root=tmp_path / "w1")

    def test_prepare_rejects_nonempty_supervision(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        target = results_root / spec.cells[0].source_rel
        (target / "supervision" / "x.json").write_text("{}")
        with pytest.raises(pm.PrepareError, match="supervision"):
            pm.prepare(spec, results_root=results_root, work_root=tmp_path / "w2")

    def test_prepare_rejects_metadata_fingerprint_mismatch(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        target = results_root / spec.cells[0].source_rel
        meta = json.loads((target / "metadata.json").read_text())
        meta["prompt_version"] = "WRONG"
        (target / "metadata.json").write_text(json.dumps(meta))
        with pytest.raises(pm.PrepareError, match="fingerprint"):
            pm.prepare(spec, results_root=results_root, work_root=tmp_path / "w3")

    def test_prepare_rejects_substitution_source_rel(self, tmp_path):
        # 替代：cell_id 与 source_rel 内嵌身份不一致 → selector 层拒绝（fail-closed）。
        with pytest.raises(ValueError, match="substitution rejected"):
            mx.P6CellSpec(
                cell_id="s9_a9",
                source_rel="20260804_234045_eval_diag_full/s5_s42_a2_r1",
                scene=9,
                agents=9,
                seed=42,
                repetition="r1",
                expected_metadata_fingerprint={},
                expected_source_error_taxonomy={},
                expected_grader_skip_taxonomy={},
            )

    def test_prepare_rejects_path_escape(self, tmp_path):
        # `..` 在 P6CellSpec source_rel 校验层即被拒绝（fail-closed）。
        with pytest.raises(ValueError, match=r"\.\."):
            mx.P6CellSpec(
                cell_id="s1_a2",
                source_rel="../../escape/s1_s42_a2_r1",
                scene=1,
                agents=2,
                seed=42,
                repetition="r1",
                expected_metadata_fingerprint={},
                expected_source_error_taxonomy={},
                expected_grader_skip_taxonomy={},
            )

    def test_prepare_does_not_mutate_source(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        before = {
            cell.source_rel: (
                results_root / cell.source_rel / "metadata.json"
            ).read_bytes()
            for cell in spec.cells
        }
        pm.prepare(spec, results_root=results_root, work_root=tmp_path / "w6")
        for cell in spec.cells:
            assert (
                results_root / cell.source_rel / "metadata.json"
            ).read_bytes() == before[cell.source_rel]

    def test_manifest_records_stripped_csv_columns(self, tmp_path):
        """synthetic source 的 agent_interactions 含 LLM 三列 → 剥离并记录列名。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        assert len(data["cells"]) == 7
        for cell in data["cells"]:
            assert cell["stripped_metadata_keys"] == []
            assert cell["stripped_csv_columns"] == {
                "agent_interactions.csv": ["LLMInput", "LLMOutput", "Thinking"]
            }
        # 保持 manifest 不内嵌自身 hash、canonical JSON 可复算。
        assert "matrix_manifest_sha256" not in data

    def test_prepare_strips_and_records_extra_metadata_keys(self, tmp_path):
        """source metadata 含非 allowlist key（26-key 形状）→ prepare 不抛错，
        manifest 记录 key 名（排序、仅名称），staged metadata 仍只含九字段。"""
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        target = results_root / spec.cells[0].source_rel
        meta = json.loads((target / "metadata.json").read_text())
        for key in (
            "api_base",
            "provider",
            "model",
            "coordinator_prompts",
            "worker_prompts",
            "task_objective",
            "success_criteria",
            "env_name",
            "scenario_id",
            "temperature",
            "llm_seed",
            "wall_clock_timeout",
            "unknown_extra",
        ):
            meta[key] = f"value-{key}"
        (target / "metadata.json").write_text(json.dumps(meta))

        manifest = pm.prepare(spec, results_root=results_root, work_root=tmp_path / "w")
        data = json.loads(Path(manifest).read_text())
        cell = next(c for c in data["cells"] if c["cell_id"] == "s1_a2")
        assert cell["stripped_metadata_keys"] == sorted(
            {
                "api_base",
                "provider",
                "model",
                "coordinator_prompts",
                "worker_prompts",
                "task_objective",
                "success_criteria",
                "env_name",
                "scenario_id",
                "temperature",
                "llm_seed",
                "wall_clock_timeout",
                "unknown_extra",
            }
        )
        # staged metadata 仍只含九字段。
        staged_meta = json.loads(
            (tmp_path / "w" / "staged" / "s1_a2" / "metadata.json").read_text()
        )
        assert set(staged_meta) == p.METADATA_ALLOWED_FIELDS
        # 其余 cell 不受影响（九字段、无剥离）。
        other = next(c for c in data["cells"] if c["cell_id"] == "s2_a2")
        assert other["stripped_metadata_keys"] == []


# ─────────────────────────────────────────────────────────────────────────────
# verifier：静态 + 离线 workflow + taxonomy + subject + no discovery
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyMatrix:
    def test_verify_full_success_zero_harness_errors(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        report = vm.verify(manifest)
        assert report["total_harness_error_counts"] == mx.empty_harness_error_counts()
        assert len(report["cells"]) == 7
        for cell in report["cells"]:
            assert cell["grader_skip_taxonomy"] == cell["expected_grader_skip_taxonomy"]
            assert (
                cell["source_framework_error_counts"]
                == cell["expected_source_error_taxonomy"]
            )

    def test_verify_report_carries_stripped_audit_fields(self, tmp_path):
        """verify report 每 cell 透传 manifest 中的剥离审计字段（名称、排序）。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        report = vm.verify(manifest)
        for cell in report["cells"]:
            assert cell["stripped_metadata_keys"] == []
            assert cell["stripped_csv_columns"] == {
                "agent_interactions.csv": ["LLMInput", "LLMOutput", "Thinking"]
            }

    def test_verify_two_runs_stable_semantic_fingerprint(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        report = vm.verify(manifest)
        for cell in report["cells"]:
            assert len(cell["attempts"]) == 2
            assert cell["attempts"][0]["judge_status"] == "not_requested"
            assert cell["attempts"][0]["terminal_status"] == "succeeded"
            assert cell["attempts"][0]["runner_constructed"] == 0
            assert cell["attempts"][1]["runner_constructed"] == 0
            # subject tuple 等于 spec（不接受 workflow default）。
            assert int(cell["subject_tuple"]["scene"]) > 0
            assert cell["subject_tuple"]["seed"] == 42

    def test_verify_zero_runner_construction_never_factory(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        constructed = {"n": 0}

        def factory(runtime):
            def _f():
                constructed["n"] += 1
                raise AssertionError("no-LLM must not construct runners")

            return _f

        report = vm.verify(manifest, runner_factory_factory=factory)
        assert constructed["n"] == 0
        assert report["total_harness_error_counts"] == mx.empty_harness_error_counts()

    def test_verify_s3a4_resume(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        report = vm.verify(manifest, resume_cells={"s3_a4"})
        s3a4 = next(c for c in report["cells"] if c["cell_id"] == "s3_a4")
        assert s3a4["resume"] is not None
        assert s3a4["resume"]["status"] == "SUCCEEDED"
        # 已验证 artifact 在 resume 后不被重写（只补缺失节点）。
        assert s3a4["resume"]["artifacts_unchanged_across_resume"] is True
        assert report["total_harness_error_counts"] == mx.empty_harness_error_counts()

    def test_verify_no_source_discovery(self, tmp_path):
        """verifier 直接以 manifest 调用，不扫 results；source 存在与否无关。"""
        manifest, results_root, _spec = prepare_synthetic(tmp_path)
        # 删掉整个 source cohort，verifier 仍应只消费 staged 输入而成功。
        shutil.rmtree(results_root)
        report = vm.verify(manifest)
        assert report["total_harness_error_counts"] == mx.empty_harness_error_counts()

    def test_verify_source_taxonomy_mismatch_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"][0]["source_framework_error_counts"]["unknown_task_id"] = 99
        tampered = Path(manifest).with_name("tampered_manifest.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["source_provenance_mismatch"] > 0

    def test_verify_grader_skip_taxonomy_mismatch_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"][0]["expected_grader_skip_taxonomy"] = {"other": 1}
        tampered = Path(manifest).with_name("tampered_manifest2.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["source_provenance_mismatch"] > 0

    def test_verify_staged_digest_drift_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        work = Path(manifest).parent
        staged = work / "staged" / "s1_a2"
        (staged / "trajectory.csv").write_text(TRAJ_HEADER + "\n")
        report = vm.verify(manifest)
        assert report["total_harness_error_counts"]["semantic_staging"] > 0

    def test_verify_raw_input_digest_drift_fails(self, tmp_path):
        """manifest 内嵌 raw-input 文件 hash 被篡改 → raw_input_digest 重算失配。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        cell = data["cells"][0]
        cell["raw_input_files"]["trajectory.csv"] = "0" * 64
        tampered = Path(manifest).with_name("tampered_raw.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["source_provenance_mismatch"] > 0

    def test_verify_raw_trace_digest_drift_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        cell = data["cells"][0]
        cell["raw_trace_files"]["coordinator/router.ndjson"] = "1" * 64
        tampered = Path(manifest).with_name("tampered_trace.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["source_provenance_mismatch"] > 0

    def test_verify_projection_digest_drift_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        cell = data["cells"][0]
        cell["projections"][0]["error_counts"]["no_worker"] = 999
        tampered = Path(manifest).with_name("tampered_proj.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["semantic_staging"] > 0

    def test_verify_policy_digest_drift_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"][0]["redaction_policy_digest"] = "2" * 64
        tampered = Path(manifest).with_name("tampered_policy.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["source_provenance_mismatch"] > 0

    def test_verify_subject_default_mismatch_fails(self, tmp_path):
        """staged metadata 被改 → workflow subject 与 spec 不一致 → fail。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        work = Path(manifest).parent
        staged = work / "staged" / "s1_a2"
        meta = json.loads((staged / "metadata.json").read_text())
        meta["scene"] = 99
        (staged / "metadata.json").write_text(json.dumps(meta))
        report = vm.verify(manifest)
        # workflow 会读到 scene=99；subject tuple mismatch → artifact_verification。
        assert report["total_harness_error_counts"]["artifact_verification"] > 0

    def test_verify_staged_secret_leak_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        work = Path(manifest).parent
        staged = work / "staged" / "s1_a2"
        (staged / "metadata.json").write_text(
            json.dumps({"scene": 1, "api_base": "https://secret.example"})
        )
        report = vm.verify(manifest)
        assert report["total_harness_error_counts"]["semantic_staging"] > 0

    def test_verify_staged_symlink_fails(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        work = Path(manifest).parent
        staged = work / "staged" / "s1_a2"
        os.remove(staged / "summary.csv")
        os.symlink(tmp_path / "outside.csv", staged / "summary.csv")
        report = vm.verify(manifest)
        assert report["total_harness_error_counts"]["semantic_staging"] > 0

    def test_verify_manifest_sha_check(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        wrong_sha = "0" * 64
        report = vm.verify(manifest, recorded_manifest_sha=wrong_sha)
        assert report["total_harness_error_counts"]["manifest_validation"] > 0

    def test_verify_manifest_sha_correct_passes(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        sha = __import__("sar_orch.eval.contracts", fromlist=["sha256_hex"]).sha256_hex(
            Path(manifest).read_bytes()
        )
        report = vm.verify(manifest, recorded_manifest_sha=sha)
        assert report["total_harness_error_counts"] == mx.empty_harness_error_counts()

    def test_verify_missing_cell_counts_harness_error(self, tmp_path):
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"] = data["cells"][:-1]
        tampered = Path(manifest).with_name("tampered_missing_cell.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        _m, static = vm.verify_manifest_static(tampered)
        assert static["manifest_validation"] > 0

    def test_verify_missing_cell_full_report_counts_harness(self, tmp_path):
        """缺格通过完整 verify 路径 → total_harness_error_counts 计入 manifest_validation。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"] = data["cells"][:-1]
        tampered = Path(manifest).with_name("tampered_missing_cell_full.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        report = vm.verify(tampered)
        assert report["total_harness_error_counts"]["manifest_validation"] > 0

    def test_verify_duplicate_repetition_counts_harness_error(self, tmp_path):
        """cell tuple uniqueness 必须包含 repetition：同 (scene,agents,seed,repetition)
        重复 → manifest_validation harness error。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cells"][0]["subject_tuple"] = dict(data["cells"][6]["subject_tuple"])
        tampered = Path(manifest).with_name("tampered_dup_repetition.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        _m, static = vm.verify_manifest_static(tampered)
        assert static["manifest_validation"] > 0

    def test_verify_same_tuple_diff_repetition_allowed(self, tmp_path):
        """同 (scene,agents,seed) 不同 repetition 不算重复 tuple（uniqueness 含 repetition）。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        data = json.loads(Path(manifest).read_text())
        data["cohort"] = "custom_cohort"
        cells = data["cells"]
        cells[1]["subject_tuple"] = dict(cells[0]["subject_tuple"])
        cells[1]["subject_tuple"]["repetition"] = "r2"
        tampered = Path(manifest).with_name("custom_cohort.json")
        tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        _m, static = vm.verify_manifest_static(tampered)
        assert static["manifest_validation"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# run_matrix：cleanup / summary
# ─────────────────────────────────────────────────────────────────────────────


class TestRunMatrix:
    def test_run_matrix_default_cleanup_and_summary(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        summary_out = tmp_path / "summary.json"
        work_root = tmp_path / "tmp_work"
        code, summary = rm.run(
            spec,
            results_root=results_root,
            summary_out=summary_out,
            keep_workdir=False,
            work_root=work_root,
        )
        assert code == 0
        assert summary is not None
        # summary 在工作 root 之外 atomic 写出。
        assert summary_out.exists()
        data = json.loads(summary_out.read_text())
        assert data["matrix_manifest_sha256"]
        assert len(data["cells"]) == 7
        assert data["source_status_pre_post_equal"] is True
        # 默认 cleanup：work root 与 manifest 已删除。
        assert not work_root.exists()

    def test_run_matrix_keep_workdir_retains(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        summary_out = tmp_path / "summary.json"
        kept_work = tmp_path / "kept_work"
        code, summary = rm.run(
            spec,
            results_root=results_root,
            summary_out=summary_out,
            keep_workdir=True,
            work_root=kept_work,
        )
        assert code == 0
        assert summary is not None
        assert kept_work.exists()
        assert (kept_work / "MATRIX_MANIFEST.json").exists()

    def test_run_matrix_summary_excludes_staged_path_and_raw(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        summary_out = tmp_path / "summary.json"
        code, _ = rm.run(spec, results_root=results_root, summary_out=summary_out)
        assert code == 0
        text = summary_out.read_text()
        assert "staged/s" not in text
        assert p.TEST_SECRET_SENTINEL not in text
        data = json.loads(text)
        cell0 = data["cells"][0]
        # §0.6-7：每 cell 保留 raw/staged/projection/evidence/manifest/ledger/report digest。
        assert cell0["raw_input_digest"]
        assert cell0["raw_trace_digest"]
        assert cell0["raw_provenance_digest"]
        assert cell0["staged_semantic_digest"]
        assert cell0["supervision_state_digest"]
        assert len(cell0["projection_digests"]) == 2
        attempt = cell0["attempts"][0]
        assert attempt["input_manifest_digest"]
        assert attempt["final_ledger_digest"]
        assert attempt["report_digest"]
        assert attempt["evidence_digest"]
        assert attempt["deterministic_grader_digest"]

    def test_run_matrix_summary_carries_stripped_audit_fields(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        summary_out = tmp_path / "summary.json"
        code, _ = rm.run(spec, results_root=results_root, summary_out=summary_out)
        assert code == 0
        data = json.loads(summary_out.read_text())
        cell0 = data["cells"][0]
        assert cell0["stripped_metadata_keys"] == []
        assert cell0["stripped_csv_columns"] == {
            "agent_interactions.csv": ["LLMInput", "LLMOutput", "Thinking"]
        }

    def test_run_matrix_missing_results_root_returns_one(self, tmp_path):
        spec = mx.default_source_spec()
        code, _ = rm.run(
            spec, results_root=tmp_path / "nope", summary_out=tmp_path / "s.json"
        )
        assert code == 1

    def test_run_matrix_cleanup_on_prepare_failure(self, tmp_path):
        results_root = tmp_path / "results"
        spec = make_synthetic_cohort(results_root)
        target = results_root / spec.cells[0].source_rel
        shutil.rmtree(target / "supervision")
        summary_out = tmp_path / "summary.json"
        work_root = tmp_path / "tmp_work2"
        code, _ = rm.run(
            spec,
            results_root=results_root,
            summary_out=summary_out,
            work_root=work_root,
        )
        assert code == 1
        assert not work_root.exists()


# ─────────────────────────────────────────────────────────────────────────────
# FakeRunner requested-path probe（单独，不与 no-LLM 混用；无网络）
# ─────────────────────────────────────────────────────────────────────────────


class TestFakeRunnerProbe:
    def test_requested_probe_with_fake_runner_no_network(self, tmp_path):
        """requested-path 用 FakeRunner（离线）运行一次；绝不与 no-LLM 合并。"""
        manifest, _r, _s = prepare_synthetic(tmp_path)
        staged_dir = Path(manifest).parent / "staged" / "s1_a2"
        work_root = Path(manifest).parent

        def fake_factory(runtime):
            factory, _stats = r.make_fake_runner_factory(runtime.manifest, {})
            return factory

        args = vm._attempt_args(
            staged_dir, work_root, uuid4(), uuid4(), no_llm_judge=False
        )
        code = w.run_from_results_dir(args, runner_factory_factory=fake_factory)
        assert code == 0
        store = a.ArtifactStore(args.attempt_root)
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
