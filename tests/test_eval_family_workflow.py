"""F3 workflow wiring for family v2 jobs（judge 家族化设计 §4.1 / §6 / §8.1 V-F3）。

覆盖：
- v1 回归：v1 rubric 不物化 v2 bundle、共享 evidence bundle 绑定不变、v1 完整
  dimensions 校验与整体 abstain 语义保持、v1 draft 不允许维度级 abstain；
- v2 dispatch bundle 物化：per-sample `dispatch_bundle.json`（Step/Subtask/
  AssignedTo/EventType 派发记录 + 派生 team state + map 摘要 + real step/max_steps）
  的 canonical 落盘与 digest 绑定进 `ScoreJob.input_bundle_ref`；
- v2 observation bundle：per-sample claim + `llm_response.content` 预览
  （≤500 字符、redact_text 掩码、绝不带 llm_input/thinking）；
- validator 维度级 abstain：`dimensions` ∪ `dimension_unknown_reasons` 恰好覆盖
  rubric 维度全集（extra/missing/overlap 拒绝）；retrieval refs digest 自洽 +
  物化 artifact verified-read 复核；
- merge 逐维度 unknown：v2 partial 缺席维度按 frozen unknown_policy 处理，
  绝不把缺席分当 0 合成；
- runner 路由：v2 rubric → FamilyRoleRunner，v1 rubric → ScoreRoleRunner，
  run_dir 透传检索 containment；
- v2 fake 端到端（fake runner，零真实 LLM）。

只用 fake runner / fake chat model；不构造任何真实 LLM。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval import score_merge as sm
from sar_orch.eval import workflow as w
from sar_orch.eval.agent import family_runner as fr
from sar_orch.eval.agent import roles
from sar_orch.eval.agent.runner import RunnerOutcome, RunnerStatus
from sar_orch.eval.dataset import load_episode

DISPATCH_DIMS = [
    "dispatch_completeness",
    "dispatch_feasibility",
    "dispatch_novelty",
    "dispatch_efficiency",
]
OBSERVATION_DIMS = [
    "existence_grounding",
    "type_fidelity",
    "position_fidelity",
    "attribute_fidelity",
]

AGENT_CSV_HEADER = (
    "Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,"
    "Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType"
)


def _ts() -> datetime:
    return datetime(2026, 8, 9, 0, 42, 10, tzinfo=UTC)


def _artref(path="evidence/x.json", sha="a" * 64) -> c.ArtifactRef:
    return c.ArtifactRef(
        path=path, sha256=sha, bytes=12, media_type="application/json", producer="test"
    )


def _v2_spec(target_type: c.SampleTargetType) -> c.RubricSpec:
    name = (
        "dispatch-v2"
        if target_type is c.SampleTargetType.DISPATCH
        else "observation-v2"
    )
    return rr.load_rubric_spec(rr.DEFAULT_RUBRICS_DIR / f"{name}.yaml").spec


def _v1_spec(target_type: c.SampleTargetType) -> c.RubricSpec:
    name = (
        "dispatch-v1"
        if target_type is c.SampleTargetType.DISPATCH
        else "observation-v1"
    )
    return rr.load_rubric_spec(rr.DEFAULT_RUBRICS_DIR / f"{name}.yaml").spec


def _role(spec: c.RubricSpec) -> c.RoleConfig:
    return c.RoleConfig(
        role=spec.judge_role,
        agent_id=spec.judge_role.value,
        prompt_ref=_artref("snapshots/prompts/x.md", "b" * 64),
        prompt_digest="b" * 64,
        model_profile_ref=_artref("snapshots/model_profiles/x.json", "c" * 64),
        model_profile_digest="c" * 64,
        tool_schema_ref=_artref("snapshots/tool_schemas/x.json", "d" * 64),
        tool_schema_digest="d" * 64,
    )


def _manifest(specs: list[c.RubricSpec]) -> c.FrozenInputManifest:
    roles_cfg = []
    seen: set[c.JudgeRole] = set()
    for spec in specs:
        if spec.judge_role not in seen:
            seen.add(spec.judge_role)
            roles_cfg.append(_role(spec))
    manifest = c.InputManifest(
        eval_run_id=uuid4(),
        attempt_id=uuid4(),
        created_at=_ts(),
        subject=c.SubjectRef(
            source_run_ref=_artref("subject/run_ref.json", "f" * 64),
            source_input_manifest_ref=_artref("evidence/source.json", "e" * 64),
            source_digest="1" * 64,
            scene=1,
            agents=2,
            seed=42,
            code_commit="x",
            git_dirty=False,
        ),
        evaluator=c.EvaluatorSpec(
            workflow_version="0.1.0", source_tree_digest="9" * 64
        ),
        policy=c.PolicySpec(
            llm_judge_required=True,
            audit_level=c.AuditLevel.STANDARD,
            retry=2,
            timeout_s=60,
            concurrency=2,
            retention_days=30,
            allow_shared_model=True,
            merge_policy_digest=sm.MergePolicy().digest(),
        ),
        roles=roles_cfg,
        rubrics=specs,
        command=c.CommandSpec(
            argv_without_secrets=["eval"], cwd="/tmp", env_allowlist=["PATH"]
        ),
    ).freeze()
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# 迷你合成 run 目录（与真实 run 文件同构，供 load_episode / dispatch bundle 物化）
# ─────────────────────────────────────────────────────────────────────────────


def _mini_run(run: Path) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    (run / "metadata.json").write_text(
        json.dumps(
            {
                "scene": 1,
                "seed": 42,
                "agent_count": 2,
                "agent_names": ["Alice", "Bob"],
                "max_steps": 10,
                "model": "fake",
                "provider": "fake",
                "code_commit": "x",
                "git_dirty": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "trajectory.csv").write_text(
        "Step,Coverage,TransportRate,Actions,Successes,Finished,EndReason,"
        "TimeoutAgents,CompletedSubtasksDelta\n"
        '"1","0.1","0.0","[\\"Move\\"]","[true]","false","","[]","[]"\n'
        '"2","0.2","0.0","[\\"Move\\",\\"report_observation\\"]","[true,true]",'
        '"false","","[]","[]"\n'
        '"3","0.3","0.0","[\\"Move\\"]","[true]","false","","[]","[]"\n',
        encoding="utf-8",
    )
    (run / "router_interactions.csv").write_text(
        "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType\n"
        '"0","Explore the environment.","Alice","r1","c1","w1","assign_task"\n'
        '"1","Investigate FireA.","Bob","r1","c2","w2","assign_task"\n',
        encoding="utf-8",
    )
    (run / "subtasks.csv").write_text(
        "RunID,Step,SubtaskID,Status,AssignedTo,Subtask,CreatedAt,UpdatedAt,"
        "FailureClass,Details\n"
        '"r1","0","dispatch-1","assigned","Alice","Explore the environment.","","","",""\n'
        '"r1","1","dispatch-2","completed","Bob","Investigate FireA.","","","",""\n',
        encoding="utf-8",
    )
    (run / "agent_interactions.csv").write_text(
        AGENT_CSV_HEADER
        + '\n"1","Alice","Move","{}","Move()","I tried to Move and was successful. '
        'co-ordinates: (1,2,0). I am holding {\'Water\': 1}. Names: [FireA].","","","",'
        '"r1","c3","tool_result","10",""\n'
        '"2","Alice","report_observation",'
        '"{""object_type"": ""fire"", ""name"": ""FireA"", '
        '""position"": [1, 2, 0]}","report_observation()",'
        "\"Directly around me, co-ordinates: (1,2,0). I am holding {'Water': 1}. "
        'Names: [FireA].","","{""api_key"": ""SECRET123"", ""note"": ""reported FireA""}","","'
        '"r1","c4","tool_result","12",""\n'
        '"2","Bob","Move","{}","Move()","I tried to Move and was not successful. '
        'co-ordinates: (3,4,0).","","","","r1","c5","tool_result","9",""\n',
        encoding="utf-8",
    )
    (run / "summary.csv").write_text(
        'RunID,Coverage,TransportRate,Finished\n"r1","0.3","0.0","false"\n',
        encoding="utf-8",
    )
    (run / "token_usage.csv").write_text(
        "Step,Agent,PromptTokens,CompletionTokens,TotalTokens\n",
        encoding="utf-8",
    )
    (run / "semantic_map.jsonl").write_text(
        json.dumps(
            {
                "ts": 1,
                "event_type": "observation_ingested",
                "observation": {
                    "reporter": "Alice",
                    "step": 2,
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [1, 2, 0],
                    "confidence": 1.0,
                    "source_task_id": "",
                    "note": "",
                },
                "object": {
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [1, 2, 0],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "map_summary.jsonl").write_text(
        json.dumps(
            {
                "env_step": 2,
                "status": "success",
                "summary": "FireA reported by Alice.",
                "trigger_reasons": ["periodic"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "supervision").mkdir()
    return run


def _runtime(tmp_path, manifest: c.FrozenInputManifest, run_dir: Path) -> w.EvalRuntime:
    store = a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))
    return w.EvalRuntime(
        manifest,
        store,
        store.audit_journal(),
        episode=load_episode(run_dir),
        grader_fn=lambda: ([], []),
        source_run_dir=run_dir,
    )


def _find_job(store: a.ArtifactStore, jobs, rubric_id: str, step: int | None = None):
    for job in jobs:
        if job.rubric_id != rubric_id:
            continue
        if step is not None and job.sample_id is None:
            continue
        return job
    return None


# ─────────────────────────────────────────────────────────────────────────────
# v1 回归：不物化 v2 bundle、共享 evidence bundle 绑定、v1 校验语义保持
# ─────────────────────────────────────────────────────────────────────────────


class TestV1Regression:
    def test_v1_jobs_keep_shared_bundle_and_no_v2_files(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v1_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        jobs = w._build_jobs(rt)
        assert jobs, "expected v1 dispatch jobs"
        shared = w._evidence_bundle_ref(rt)
        for job in jobs:
            assert job.rubric_id == "dispatch-v1"
            assert job.input_bundle_ref.path == shared.path
            assert job.input_bundle_ref.sha256 == shared.sha256
        # v1 job 不物化任何 v2 bundle。
        v2_rel = f"evidence/job-scoped/{jobs[0].job_id}/dispatch_bundle.json"
        assert not rt.store.exists(v2_rel)
        # v1 不绑定 dispatch-v2 rubric。
        assert not any(j.rubric_id == "dispatch-v2" for j in jobs)

    def test_v1_scored_draft_still_validates(self):
        spec = _v1_spec(c.SampleTargetType.DISPATCH)
        job = c.ScoreJob(
            job_id=uuid4(),
            job_digest="0" * 64,
            sample_id=uuid4(),
            target_type=spec.target_type,
            source_digest="1" * 64,
            evidence_digest="2" * 64,
            rubric_id=spec.rubric_id,
            rubric_digest=spec.digest,
            role=spec.judge_role,
            prompt_digest="3" * 64,
            model_profile_digest="4" * 64,
            tool_schema_digest="5" * 64,
            input_bundle_ref=_artref("evidence/evidence_bundle.json"),
            retry_policy_digest="6" * 64,
            manifest_digest="7" * 64,
        )
        evidence = []
        for dim in spec.dimensions:
            sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
            ref = c.ArtifactRef(
                path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
                sha256=sha,
                bytes=len(sha),
                media_type="application/json",
                producer="test",
            )
            evidence.append(
                c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)
            )
        scored = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={dim: 0.8 for dim in spec.dimensions},
            evidence=evidence,
            model_used="test",
        )
        assert w._validate_draft(job, scored, rubric=spec) == []
        # v1 整体 abstain（空 dimensions + unknown_reason）语义保持。
        abstain = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={},
            model_used="test",
            unknown_reason="evidence unavailable",
        )
        assert w._validate_draft(job, abstain, rubric=spec) == []

    def test_v1_draft_with_dimension_unknown_reasons_rejected(self):
        spec = _v1_spec(c.SampleTargetType.DISPATCH)
        job = c.ScoreJob(
            job_id=uuid4(),
            job_digest="0" * 64,
            sample_id=uuid4(),
            target_type=spec.target_type,
            source_digest="1" * 64,
            evidence_digest="2" * 64,
            rubric_id=spec.rubric_id,
            rubric_digest=spec.digest,
            role=spec.judge_role,
            prompt_digest="3" * 64,
            model_profile_digest="4" * 64,
            tool_schema_digest="5" * 64,
            input_bundle_ref=_artref("evidence/evidence_bundle.json"),
            retry_policy_digest="6" * 64,
            manifest_digest="7" * 64,
        )
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={"full_coverage": 0.8},
            dimension_unknown_reasons={"role_match": "no dispatch to judge"},
            model_used="test",
        )
        errors = w._validate_draft(job, draft, rubric=spec)
        assert any(
            "v1 draft must not carry dimension_unknown_reasons" in e for e in errors
        )

    def test_v1_incomplete_dimensions_still_rejected(self):
        spec = _v1_spec(c.SampleTargetType.DISPATCH)
        job = c.ScoreJob(
            job_id=uuid4(),
            job_digest="0" * 64,
            sample_id=uuid4(),
            target_type=spec.target_type,
            source_digest="1" * 64,
            evidence_digest="2" * 64,
            rubric_id=spec.rubric_id,
            rubric_digest=spec.digest,
            role=spec.judge_role,
            prompt_digest="3" * 64,
            model_profile_digest="4" * 64,
            tool_schema_digest="5" * 64,
            input_bundle_ref=_artref("evidence/evidence_bundle.json"),
            retry_policy_digest="6" * 64,
            manifest_digest="7" * 64,
        )
        partial = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={"full_coverage": 0.8},
            model_used="test",
        )
        errors = w._validate_draft(job, partial, rubric=spec)
        assert any("!= rubric dimensions" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# v2 dispatch bundle 物化 + digest 绑定
# ─────────────────────────────────────────────────────────────────────────────


class TestDispatchBundle:
    def test_v2_dispatch_jobs_materialize_and_bind_bundle(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        jobs = w._build_jobs(rt)
        v2_jobs = [j for j in jobs if j.rubric_id == "dispatch-v2"]
        assert v2_jobs, "expected v2 dispatch jobs"

        for job in v2_jobs:
            rel = job.input_bundle_ref.path
            assert rel == f"evidence/job-scoped/{job.job_id}/dispatch_bundle.json"
            # digest 绑定：input_bundle_ref 与落盘字节一致。
            assert rt.store.exists(rel)
            assert job.input_bundle_ref.sha256 == c.sha256_hex(rt.store.read_bytes(rel))
            bundle = json.loads(rt.store.read_bytes(rel))
            assert bundle["bundle"] == "dispatch-v2"
            assert (
                bundle["sample"]["step"] == job.sample_id or "step" in bundle["sample"]
            )

    def test_dispatch_bundle_content_fields(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        jobs = w._build_jobs(rt)
        job = next(j for j in jobs if j.rubric_id == "dispatch-v2")
        bundle = json.loads(rt.store.read_bytes(job.input_bundle_ref.path))

        # 派发记录字段名（Step/Subtask/AssignedTo/EventType）。
        assert bundle["dispatch_records"], "expected dispatch records"
        for rec in bundle["dispatch_records"]:
            assert {"Step", "Subtask", "AssignedTo", "EventType"} <= set(rec.keys())
        assert bundle["dispatch_records"][0]["AssignedTo"] in ("Alice", "Bob")

        # 派生 team state：position/inventory/active_task。
        by_agent = {s["agent"]: s for s in bundle["team_state"]}
        assert set(by_agent) == {"Alice", "Bob"}
        assert by_agent["Alice"]["position"] == [1, 2, 0]
        assert by_agent["Alice"]["inventory"] == {"Water": 1}
        # Bob 的 step2 失败 Move 后无库存快照；Alice 有活动任务（assigned）。
        assert by_agent["Alice"]["active_task"] is not None
        assert by_agent["Alice"]["active_task"]["status"] == "assigned"
        assert by_agent["Bob"]["active_task"] is None

        # map 摘要 + real step/max_steps。
        assert bundle["map_summary"]["status"] == "success"
        budget = bundle["step_budget"]
        assert budget["max_steps"] == 10
        assert budget["step"] >= 1
        assert budget["remaining"] == max(0, 10 - budget["step"])

    def test_dispatch_bundle_has_no_timestamps(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        job = next(j for j in w._build_jobs(rt) if j.rubric_id == "dispatch-v2")
        bundle = json.loads(rt.store.read_bytes(job.input_bundle_ref.path))
        assert isinstance(bundle["sample"]["step"], int)
        # canonical JSON：不携带随机/时间戳字段，digest 只由 episode 输入决定。
        assert "timestamp" not in json.dumps(bundle)
        assert "created_at" not in json.dumps(bundle)


# ─────────────────────────────────────────────────────────────────────────────
# v2 observation bundle：llm_response.content 预览注入 + 脱敏
# ─────────────────────────────────────────────────────────────────────────────


class TestObservationBundle:
    def _obs_sample(self, rt: w.EvalRuntime):
        plan = rr.build_sample_plan(
            c.SampleTargetType.OBSERVATION,
            rt.episode,
            target=3,
            source_digest=rt.manifest.manifest.subject.source_digest,
            evidence_digest=w._evidence_digest(rt),
        )
        for sample in plan.selected:
            if sample.agent == "Alice" and sample.step == 2:
                return sample
        raise AssertionError("expected Alice step-2 observation sample")

    def test_observation_bundle_injects_redacted_llm_response_preview(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.OBSERVATION)])
        rt = _runtime(tmp_path, manifest, run_dir)
        sample = self._obs_sample(rt)
        bundle = w._build_observation_bundle(rt, sample)
        assert bundle["bundle"] == "observation-v2"
        assert len(bundle["claims"]) == 1
        claim = bundle["claims"][0]
        assert claim["step"] == 2
        assert claim["agent"] == "Alice"
        assert "fire" in claim["claim"]
        preview = claim["llm_response_preview"]
        assert preview is not None
        assert len(preview) <= w._LLM_RESPONSE_PREVIEW_CHARS
        # redact_text 掩码：secret 被掩掉、明文绝不入 bundle。
        assert "SECRET123" not in preview
        assert "[REDACTED]" in preview
        # 绝不携带 llm_input / thinking 内容（semantics 说明文字提及字段名除外）。
        assert set(claim.keys()) == {
            "step",
            "agent",
            "tool",
            "claim",
            "system_response",
            "llm_response_preview",
        }

    def test_observation_bundle_bound_to_job(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.OBSERVATION)])
        rt = _runtime(tmp_path, manifest, run_dir)
        jobs = w._build_jobs(rt)
        v2_jobs = [j for j in jobs if j.rubric_id == "observation-v2"]
        assert v2_jobs
        for job in v2_jobs:
            rel = job.input_bundle_ref.path
            assert rel == f"evidence/job-scoped/{job.job_id}/observation_bundle.json"
            assert rt.store.exists(rel)
            assert job.input_bundle_ref.sha256 == c.sha256_hex(rt.store.read_bytes(rel))

    def test_preview_truncation_to_500(self):
        text = "x" * 900
        assert len(w._truncate_deterministic(text, w._LLM_RESPONSE_PREVIEW_CHARS)) <= (
            w._LLM_RESPONSE_PREVIEW_CHARS + len(w._CLAIM_TRUNCATED_MARK)
        )


# ─────────────────────────────────────────────────────────────────────────────
# workflow validator：v2 维度级 abstain 并集覆盖
# ─────────────────────────────────────────────────────────────────────────────


def _v2_job(rt: w.EvalRuntime, spec: c.RubricSpec) -> c.ScoreJob:
    for job in w._build_jobs(rt):
        if job.rubric_id == spec.rubric_id:
            return job
    raise AssertionError(f"no {spec.rubric_id} job")


def _partial_draft(job: c.ScoreJob, spec: c.RubricSpec, scored, abstained):
    evidence = []
    for dim in scored:
        sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
        ref = c.ArtifactRef(
            path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
            sha256=sha,
            bytes=len(sha),
            media_type="application/json",
            producer="test",
        )
        evidence.append(
            c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)
        )
    return c.ScoreDraft(
        role=job.role,
        invocation_id=uuid4(),
        dimensions={dim: 0.9 for dim in scored},
        dimension_unknown_reasons={dim: "no evidence to judge" for dim in abstained},
        evidence=evidence,
        model_used="test",
    )


class TestV2Validator:
    def test_partial_abstain_union_covers_rubric_passes(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        rt = _runtime(tmp_path, _manifest([spec]), run_dir)
        job = _v2_job(rt, spec)
        draft = _partial_draft(
            job, spec, scored=DISPATCH_DIMS[:2], abstained=DISPATCH_DIMS[2:]
        )
        assert w._validate_draft(job, draft, rubric=spec, store=rt.store) == []

    def test_missing_dimension_rejected(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        rt = _runtime(tmp_path, _manifest([spec]), run_dir)
        job = _v2_job(rt, spec)
        draft = _partial_draft(
            job, spec, scored=DISPATCH_DIMS[:2], abstained=DISPATCH_DIMS[2:3]
        )
        errors = w._validate_draft(job, draft, rubric=spec, store=rt.store)
        assert any("dimension union" in e for e in errors)

    def test_extra_dimension_rejected(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        rt = _runtime(tmp_path, _manifest([spec]), run_dir)
        job = _v2_job(rt, spec)
        draft = _partial_draft(
            job,
            spec,
            scored=DISPATCH_DIMS[:2] + ["bogus_dim"],
            abstained=DISPATCH_DIMS[2:],
        )
        errors = w._validate_draft(job, draft, rubric=spec, store=rt.store)
        assert any("dimension union" in e for e in errors)

    def test_overlap_rejected_even_if_pydantic_bypassed(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        rt = _runtime(tmp_path, _manifest([spec]), run_dir)
        job = _v2_job(rt, spec)
        # 用 model_construct 绕过 pydantic 的重叠拒绝，验证 validator 防御性 double-check。
        draft = c.ScoreDraft.model_construct(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={"dispatch_completeness": 0.9},
            dimension_unknown_reasons={"dispatch_completeness": "contradictory"},
            evidence=[],
            model_used="test",
        )
        errors = w._validate_draft(job, draft, rubric=spec, store=rt.store)
        assert any("both scores and abstains" in e for e in errors)

    def test_all_abstain_draft_valid_with_union(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        rt = _runtime(tmp_path, _manifest([spec]), run_dir)
        job = _v2_job(rt, spec)
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={},
            dimension_unknown_reasons={
                dim: "no evidence to judge" for dim in DISPATCH_DIMS
            },
            model_used="test",
            unknown_reason="all dimensions abstained",
        )
        assert w._validate_draft(job, draft, rubric=spec, store=rt.store) == []


# ─────────────────────────────────────────────────────────────────────────────
# merge：v2 partial 逐维度 unknown 处理（绝不按 0 合成）
# ─────────────────────────────────────────────────────────────────────────────


class TestV2MergePartial:
    def _group(self, policy=None, dims=DISPATCH_DIMS):
        spec = c.RubricSpec(
            rubric_id="dispatch-v2",
            version="2.0.0",
            digest="3" * 64,
            target_type=c.SampleTargetType.DISPATCH,
            input_selector="dispatch_samples",
            dimensions=list(dims),
            prompt_template_ref=_artref("snapshots/prompts/x.md", "b" * 64),
            judge_role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
            merge_group="dispatch",
            weight=1.0,
        )
        jobs, results = [], []
        score_values = {}
        unknown_values = {}
        for idx in range(2):
            job = c.ScoreJob(
                job_id=uuid4(),
                job_digest="0" * 64,
                sample_id=uuid4(),
                target_type=spec.target_type,
                source_digest="1" * 64,
                evidence_digest="2" * 64,
                rubric_id=spec.rubric_id,
                rubric_digest=spec.digest,
                role=spec.judge_role,
                prompt_digest="4" * 64,
                model_profile_digest="5" * 64,
                tool_schema_digest="6" * 64,
                input_bundle_ref=_artref(f"input_bundle/{idx}.json"),
                retry_policy_digest="8" * 64,
                manifest_digest="9" * 64,
            )
            job = job.model_copy(
                update={
                    "job_digest": c.sha256_hex(
                        c.canonical_json(rr._score_job_payload(job)).encode("utf-8")
                    )
                }
            )
            scored = dims[:2]
            abstained = dims[2:]
            jobs.append(job)
            binding = c.ScoreJobBinding(
                job_id=job.job_id,
                target_type=job.target_type,
                source_digest=job.source_digest,
                evidence_digest=job.evidence_digest,
                rubric_id=job.rubric_id,
                rubric_digest=job.rubric_digest,
                role=job.role,
                prompt_digest=job.prompt_digest,
                model_profile_digest=job.model_profile_digest,
                tool_schema_digest=job.tool_schema_digest,
            )
            results.append(
                c.ScoreResult(
                    binding=binding,
                    status=c.ScoreJobStatus.SUCCEEDED,
                    dimension_evidence_refs={
                        d: c.EvidenceRef(
                            ref=_artref(f"evidence/job-scoped/{job.job_id}/{d}.json"),
                            claim_type=c.ClaimType.SCORE,
                            digest=_artref().sha256,
                        )
                        for d in scored
                    },
                    validated_output_ref=_artref("output/validated.json"),
                    node_attempt=1,
                )
            )
            score_values[job.job_id] = {d: 0.9 for d in scored}
            unknown_values[job.job_id] = {d: "no evidence" for d in abstained}
        return sm.GroupMergeInput(
            target_type=spec.target_type,
            jobs=tuple(jobs),
            results=tuple(results),
            score_values=score_values,
            rubrics=(spec,),
            dimension_unknown_reasons=unknown_values,
        )

    def _merge(self, policy=None):
        policy = policy or sm.MergePolicy()
        return sm.merge_score_groups(
            [self._group(policy=policy)],
            policy=policy,
            merge_policy_digest=policy.digest(),
            manifest_digest="9" * 64,
        )

    def test_excluded_policy_scores_present_dims_only(self):
        out = self._merge()
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.OK
        rs = gs.rubrics[0]
        # 缺席维度不被合成 0：dimension_scores 缺席为 None，rubric 分 = present 均值。
        assert rs.dimension_scores[DISPATCH_DIMS[0]] == 0.9
        assert rs.dimension_scores[DISPATCH_DIMS[1]] == 0.9
        assert rs.dimension_scores[DISPATCH_DIMS[2]] is None
        assert rs.dimension_scores[DISPATCH_DIMS[3]] is None
        assert rs.score == 0.9
        assert rs.scored == 2

    def test_excluded_policy_records_per_dimension_keys(self):
        group = self._group()
        rs = (
            sm.merge_score_groups(
                [group],
                policy=sm.MergePolicy(),
                merge_policy_digest=sm.MergePolicy().digest(),
                manifest_digest="9" * 64,
            )
            .merged_scores["dispatch"]
            .rubrics[0]
        )
        assert any(
            v == "unknown_dimension_excluded" for v in rs.excluded_reasons.values()
        )

    def test_partial_policy_triggers_partial(self):
        out = self._merge(
            policy=sm.MergePolicy(unknown_policy=sm.UnknownPolicy.PARTIAL)
        )
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert any("unknown_policy=partial" in r for r in gs.reasons)

    def test_block_policy_blocks_group(self):
        out = self._merge(policy=sm.MergePolicy(unknown_policy=sm.UnknownPolicy.BLOCK))
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.BLOCKED
        assert gs.score is None


# ─────────────────────────────────────────────────────────────────────────────
# runner 路由：v2 → FamilyRoleRunner，v1 → ScoreRoleRunner
# ─────────────────────────────────────────────────────────────────────────────


class _FakeModel(BaseChatModel):
    model_config: ClassVar[dict[str, Any]] = {"extra": "allow"}

    @property
    def _llm_type(self) -> str:
        return "fake-family-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("never invoked in routing tests")


def _routing_factory(rt: w.EvalRuntime, run_dir: Path):
    return fr.make_family_or_role_runner_factory(
        rt, model=_FakeModel(), run_dir=run_dir
    )


class TestRunnerRouting:
    def test_is_family_rubric_separates_v1_and_v2(self):
        assert fr.is_family_rubric(_v2_spec(c.SampleTargetType.DISPATCH)) is True
        assert fr.is_family_rubric(_v2_spec(c.SampleTargetType.OBSERVATION)) is True
        assert fr.is_family_rubric(_v1_spec(c.SampleTargetType.DISPATCH)) is False
        assert fr.is_family_rubric(_v1_spec(c.SampleTargetType.OBSERVATION)) is False

    def test_factory_requires_model(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        with pytest.raises(fr.FamilyRoleRunnerError, match="explicit model"):
            fr.make_family_or_role_runner_factory(rt)

    def test_v2_job_routes_to_family_runner(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        job = _v2_job(rt, _v2_spec(c.SampleTargetType.DISPATCH))
        runner = _routing_factory(rt, run_dir)()
        delegate = runner._build_delegate(job)
        assert isinstance(delegate, fr.FamilyRoleRunner)
        assert delegate._run_dir == Path(run_dir).resolve()

    def test_v1_job_routes_to_score_role_runner(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v1_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        job = _v2_job(rt, _v1_spec(c.SampleTargetType.DISPATCH))
        runner = _routing_factory(rt, run_dir)()
        delegate = runner._build_delegate(job)
        assert isinstance(delegate, roles.ScoreRoleRunner)

    def test_run_dir_falls_back_to_runtime_source_run_dir(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest([_v2_spec(c.SampleTargetType.DISPATCH)])
        rt = _runtime(tmp_path, manifest, run_dir)
        factory = fr.make_family_or_role_runner_factory(rt, model=_FakeModel())
        runner = factory()
        assert runner._run_dir == Path(run_dir).resolve()


# ─────────────────────────────────────────────────────────────────────────────
# v2 fake 端到端：partial abstain 经完整 workflow（fake runner，零真实 LLM）
# ─────────────────────────────────────────────────────────────────────────────


class V2FakeRunner:
    """v2 partial fake runner：前 2 维度 0.9、其余维度 abstain（带原因）。"""

    def __init__(self, manifest: c.FrozenInputManifest):
        self._manifest = manifest
        self.calls: list[tuple[str, str, int]] = []

    def _rubric(self, job: c.ScoreJob) -> c.RubricSpec:
        for r in self._manifest.manifest.rubrics:
            if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
                return r
        raise AssertionError(f"rubric {job.rubric_id} not in manifest")

    async def run(self, job, *, invocation_id, node_attempt):
        self.calls.append((str(job.job_id), str(invocation_id), node_attempt))
        rubric = self._rubric(job)
        scored = rubric.dimensions[:2]
        abstained = rubric.dimensions[2:]
        evidence = []
        for dim in scored:
            sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
            ref = c.ArtifactRef(
                path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
                sha256=sha,
                bytes=len(sha),
                media_type="application/json",
                producer="fake-v2",
            )
            evidence.append(
                c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)
            )
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=invocation_id,
            dimensions={dim: 0.9 for dim in scored},
            dimension_unknown_reasons={
                dim: "no evidence to judge" for dim in abstained
            },
            evidence=evidence,
            model_used="fake-v2-runner",
        )
        return RunnerOutcome(
            status=RunnerStatus.SUCCEEDED,
            draft=draft,
            usage=c.UsageSnapshot(
                prompt_tokens=10, completion_tokens=5, total_tokens=15
            ),
            latency_ms=3,
        )


class TestV2WorkflowEndToEnd:
    def test_partial_abstain_workflow_succeeds_and_merges(self, tmp_path):
        run_dir = _mini_run(tmp_path / "run")
        manifest = _manifest(
            [
                _v2_spec(c.SampleTargetType.DISPATCH),
                _v2_spec(c.SampleTargetType.OBSERVATION),
            ]
        )
        rt = _runtime(tmp_path, manifest, run_dir)
        rt.judge_sample_steps = 3
        rt.runner_factory = lambda: V2FakeRunner(manifest)

        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED

        store = rt.store
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        dispatch = merged["merged_scores"]["dispatch"]
        observation = merged["merged_scores"]["observation"]
        # EXCLUDED 默认 policy：partial 缺席维度不触发 partial，group OK。
        assert dispatch["status"] == "ok"
        assert observation["status"] == "ok"
        dispatch_rs = dispatch["rubrics"][0]
        assert dispatch_rs["score"] == 0.9
        assert dispatch_rs["dimension_scores"][DISPATCH_DIMS[0]] == 0.9
        assert dispatch_rs["dimension_scores"][DISPATCH_DIMS[2]] is None
        # 缺席维度以 per-dimension excluded 记录，绝不按 0 合成。
        assert any(
            v == "unknown_dimension_excluded"
            for v in dispatch_rs["excluded_reasons"].values()
        )

        # v2 job 落盘的 validated output 保留维度级 abstain 原因。
        for job in w._list_jobs(store):
            if job.rubric_id != "dispatch-v2":
                continue
            result = next(
                r for r in w._load_all_results(store) if r.binding.job_id == job.job_id
            )
            assert result.status is c.ScoreJobStatus.SUCCEEDED
            payload = json.loads(store.read_verified(result.validated_output_ref))
            assert len(payload["dimensions"]) == 2
            assert len(payload["dimension_unknown_reasons"]) == 2
