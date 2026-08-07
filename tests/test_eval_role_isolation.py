"""P4 受限 DeepAgent role runner 与 job evidence 隔离（设计 §4、§9 P4）。

覆盖：
- 实际 DeepAgent role runner 的 tool inventory 只含 job reader + 结构化输出
  `ScoreDraft`；write_file/edit_file/execute/task（及其余默认 built-ins）被
  HarnessProfile 排除；
- job-scoped evidence reader 拒绝跨 job ref / 绝对路径 / traversal / legacy
  eval_workspace（typed `evidence_not_authorized`），且只经
  `ArtifactStore.read_verified` 访问；
- 每次 invocation 独立 agent_id / thread / backend / 空 history；
- 非法 evidence / 非法 draft → runner FAILED → workflow PARTIAL，绝不产生
  SUCCEEDED score result 污染；
- `make_role_runner_factory` 必须显式注入 model，绝不静默构造 LLM。

只以注入的 fake chat model 驱动**真实** DeepAgent role runner（不接触真实
LLM/网络）；P3 `FakeRunner` 行为与 workflow 契约不变。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import score_merge as sm
from sar_orch.eval import workflow as w
from sar_orch.eval.agent import roles, subagents
from sar_orch.eval.agent.runner import RunnerError


def _ts() -> datetime:
    return datetime(2026, 8, 6, 0, 42, 10, tzinfo=UTC)


def _artref(path="evidence/x.json", sha="a" * 64) -> c.ArtifactRef:
    return c.ArtifactRef(
        path=path, sha256=sha, bytes=12, media_type="application/json", producer="test"
    )


def _role(role: c.JudgeRole) -> c.RoleConfig:
    return c.RoleConfig(
        role=role,
        agent_id=role.value,
        prompt_ref=_artref("snapshots/prompts/x.md", "b" * 64),
        prompt_digest="b" * 64,
        model_profile_ref=_artref("snapshots/model_profiles/x.json", "c" * 64),
        model_profile_digest="c" * 64,
        tool_schema_ref=_artref("snapshots/tool_schemas/x.json", "d" * 64),
        tool_schema_digest="d" * 64,
    )


def _rubric(target_type: c.SampleTargetType) -> c.RubricSpec:
    dispatch = target_type is c.SampleTargetType.DISPATCH
    return c.RubricSpec(
        rubric_id="dispatch-v1" if dispatch else "observation-v1",
        version="1.0.0",
        digest=("3" * 64) if dispatch else ("4" * 64),
        target_type=target_type,
        input_selector="dispatch_samples" if dispatch else "observation_samples",
        dimensions=["pass_rate", "hallucination_rate"]
        if dispatch
        else ["obs1", "obs2"],
        prompt_template_ref=_artref("snapshots/prompts/x.md", "b" * 64),
        judge_role=(
            c.JudgeRole.DISPATCH_SCORE_JUDGE
            if dispatch
            else c.JudgeRole.OBSERVATION_SCORE_JUDGE
        ),
        merge_group="dispatch" if dispatch else "observation",
        weight=1.0,
    )


def _manifest(llm_judge_required: bool = True) -> c.FrozenInputManifest:
    return c.InputManifest(
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
            llm_judge_required=llm_judge_required,
            audit_level=c.AuditLevel.STANDARD,
            retry=2,
            timeout_s=60,
            concurrency=2,
            retention_days=30,
            allow_shared_model=True,
            merge_policy_digest=sm.MergePolicy().digest(),
        ),
        roles=[
            _role(c.JudgeRole.DISPATCH_SCORE_JUDGE),
            _role(c.JudgeRole.OBSERVATION_SCORE_JUDGE),
        ],
        rubrics=[
            _rubric(c.SampleTargetType.DISPATCH),
            _rubric(c.SampleTargetType.OBSERVATION),
        ],
        command=c.CommandSpec(
            argv_without_secrets=["eval"], cwd="/tmp", env_allowlist=["PATH"]
        ),
    ).freeze()


def _episode(n_steps: int = 5) -> SimpleNamespace:
    steps: dict[int, SimpleNamespace] = {}
    for s in range(1, n_steps + 1):
        interactions = []
        if s == 3:
            interactions.append(
                SimpleNamespace(
                    tool_name="report_observation", agent="Alice", succeeded=True
                )
            )
            interactions.append(
                SimpleNamespace(tool_name="Move", agent="Bob", succeeded=False)
            )
        steps[s] = SimpleNamespace(interactions=interactions)
    return SimpleNamespace(steps=steps)


def _runtime(
    tmp_path, manifest=None, *, runner_factory=None
) -> tuple[w.EvalRuntime, a.ArtifactStore]:
    manifest = manifest or _manifest()
    store = a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))
    rt = w.EvalRuntime(
        manifest,
        store,
        store.audit_journal(),
        runner_factory=runner_factory,
        episode=_episode(),
        grader_fn=lambda: ([], []),
    )
    return rt, store


def _materialize_bundle(rt: w.EvalRuntime, store: a.ArtifactStore) -> None:
    """把 evidence bundle 落盘（与 `_evidence_bundle_ref` 字节一致），
    job `input_bundle_ref` 才能被 `read_verified`。"""
    store.write_canonical_json(
        w.EVIDENCE_BUNDLE_REL,
        w._build_evidence_bundle(rt),
        producer="materializer",
    )


def _rubric_for(manifest, job: c.ScoreJob) -> c.RubricSpec:
    for r in manifest.manifest.rubrics:
        if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
            return r
    raise AssertionError("rubric not found")


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 fake chat model：驱动真实 DeepAgent（记录 tool inventory / 按行为出牌）
# ─────────────────────────────────────────────────────────────────────────────


class FakeRoleModel(BaseChatModel):
    """真实 role runner 的注入模型。不解析私有 CoT，只按行为产出结构化输出。

    - `behavior="ok"`：按 Job Contract 的 role/dimensions 给出 1.0 分；
    - `behavior="wrong_role"`：输出其它 role 的 draft（runner 必须拒）；
    - `behavior="bad_evidence"`：draft evidence 引用**其它 job** 的证据（必须拒）；
    - `behavior="abstain"`：空 dimensions + unknown_reason（→ UNKNOWN）。
    `seen_tools` 记录每次模型调用实际见到的工具名（tool inventory 断言）。
    """

    model_config: ClassVar[dict[str, Any]] = {"extra": "allow"}

    def __init__(self, *, behavior: str = "ok"):
        super().__init__()
        self._behavior = behavior
        self.seen_tools: list[list[str]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-role-model"

    def _prompt_contract(self, messages) -> str:
        sysm = next((m for m in messages if getattr(m, "type", "") == "system"), None)
        raw = sysm.content if sysm is not None else ""
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
            )
        return (raw if isinstance(raw, str) else str(raw)).split(
            "## Job Contract (authoritative)"
        )[-1]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tools = kwargs.get("tools", [])
        self.seen_tools.append(sorted(t.name for t in tools))
        if any(getattr(m, "type", "") == "tool" for m in messages):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="done"))]
            )
        contract = self._prompt_contract(messages)
        role_m = re.search(r"^role: (\S+)$", contract, re.MULTILINE)
        dims_m = re.search(r"dimensions: (\[.*\])", contract)
        role = role_m.group(1) if role_m else "dispatch_score_judge"
        dims = json.loads(dims_m.group(1)) if dims_m else []
        behavior = self._behavior
        if behavior == "ok":
            args = {
                "role": role,
                "invocation_id": str(uuid4()),
                "dimensions": {d: 1.0 for d in dims},
                "evidence": [],
                "model_used": "fake-role-model",
            }
        elif behavior == "wrong_role":
            other = (
                "observation_score_judge"
                if role == "dispatch_score_judge"
                else "dispatch_score_judge"
            )
            args = {
                "role": other,
                "invocation_id": str(uuid4()),
                "dimensions": {d: 1.0 for d in dims},
                "evidence": [],
                "model_used": "fake-role-model",
            }
        elif behavior == "bad_evidence":
            sha = "a" * 64
            ev = {
                "ref": {
                    "path": "evidence/job-scoped/00000000-0000-0000-0000-000000000000/full_coverage.json",
                    "sha256": sha,
                    "bytes": 64,
                    "media_type": "application/json",
                    "producer": "fake-role-model",
                },
                "claim_type": "score",
                "digest": sha,
                "redacted": False,
            }
            args = {
                "role": role,
                "invocation_id": str(uuid4()),
                "dimensions": {d: 1.0 for d in dims},
                "evidence": [ev],
                "model_used": "fake-role-model",
            }
        elif behavior == "abstain":
            args = {
                "role": role,
                "invocation_id": str(uuid4()),
                "dimensions": {},
                "evidence": [],
                "model_used": "fake-role-model",
                "unknown_reason": "fake abstain: evidence insufficient",
            }
        else:
            raise AssertionError(f"unknown behavior {behavior!r}")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "ScoreDraft", "args": args, "id": "call_out"}
                        ],
                    )
                )
            ]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)


def _wire(
    rt: w.EvalRuntime,
    model: FakeRoleModel,
    *,
    prompt_resolver=None,
    collect_runners=None,
):
    factory = roles.make_role_runner_factory(
        rt, model=model, prompt_resolver=prompt_resolver
    )

    def _factory():
        runner = factory()
        if collect_runners is not None:
            collect_runners.append(runner)
        return runner

    rt.runner_factory = _factory
    return rt


# ─────────────────────────────────────────────────────────────────────────────
# 实际 tool inventory：仅 job reader + 结构化输出；默认 built-ins 被排除
# ─────────────────────────────────────────────────────────────────────────────


class TestRoleToolInventory:
    def test_actual_role_runner_sees_only_job_reader_and_score_draft(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="ok")
        rt, _ = _runtime(tmp_path, manifest)
        _wire(rt, model)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert model.seen_tools, "model must have been called"
        for seen in model.seen_tools:
            assert set(seen) == {"ScoreDraft", "read_job_evidence"}, seen

    def test_forbidden_default_builtins_never_reachable(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="ok")
        rt, _ = _runtime(tmp_path, manifest)
        _wire(rt, model)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        forbidden = {"write_file", "edit_file", "execute", "task"}
        for seen in model.seen_tools:
            assert not (forbidden & set(seen)), seen

    def test_harness_profile_excludes_builtins_explicitly(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest)
        model = FakeRoleModel(behavior="ok")
        profile_key = roles._profile_key(model)
        # profile 注册本身就必须覆盖硬性红线的 4 个内置工具。
        assert {
            "write_file",
            "edit_file",
            "execute",
            "task",
        } <= roles.EXCLUDED_ROLE_TOOLS
        runner = roles.ScoreRoleRunner(rt.store, rt.manifest, model, lambda r: "judge")
        assert runner._profile_key == profile_key


# ─────────────────────────────────────────────────────────────────────────────
# JobEvidenceReader：跨 job / 绝对路径 / traversal / legacy workspace 拒绝
# ─────────────────────────────────────────────────────────────────────────────


class TestJobEvidenceReader:
    def _two_jobs(self, tmp_path):
        manifest = _manifest()
        rt, store = _runtime(tmp_path, manifest)
        _materialize_bundle(rt, store)
        jobs = w._build_jobs(rt)
        assert len(jobs) >= 2
        job_a, job_b = jobs[0], jobs[1]
        assert str(job_a.job_id) != str(job_b.job_id)
        return store, job_a, job_b

    def test_reads_authorized_job_scoped_evidence(self, tmp_path):
        store, job, _ = self._two_jobs(tmp_path)
        rubric = _rubric_for(_manifest(), job)
        refs = roles.materialize_job_evidence(store, job, rubric.dimensions)
        reader = roles.JobEvidenceReader(store, job)
        for dim in rubric.dimensions:
            ref = refs[f"evidence/job-scoped/{job.job_id}/{dim}.json"]
            data = reader.read_verified(ref)
            payload = json.loads(data.decode("utf-8"))
            assert payload["job_id"] == str(job.job_id)
            assert payload["dimension"] == dim
        # 共享 input bundle 也属于 allowlist（judge 需要读它作上下文）。
        assert reader.read_verified(job.input_bundle_ref)

    def test_cross_job_ref_rejected(self, tmp_path):
        store, job_a, job_b = self._two_jobs(tmp_path)
        rubric_b = _rubric_for(_manifest(), job_b)
        roles.materialize_job_evidence(store, job_b, rubric_b.dimensions)
        reader_a = roles.JobEvidenceReader(store, job_a)
        rel_b = f"evidence/job-scoped/{job_b.job_id}/{rubric_b.dimensions[0]}.json"
        ref_b = c.ArtifactRef(
            path=rel_b,
            sha256="a" * 64,
            bytes=64,
            media_type="application/json",
            producer="test",
        )
        with pytest.raises(
            roles.EvidenceNotAuthorized, match="evidence_not_authorized"
        ):
            reader_a.read_verified(ref_b)
        with pytest.raises(roles.EvidenceNotAuthorized):
            reader_a.read_by_path(rel_b)

    def test_job_b_cannot_read_job_a_evidence(self, tmp_path):
        store, job_a, job_b = self._two_jobs(tmp_path)
        rubric_a = _rubric_for(_manifest(), job_a)
        roles.materialize_job_evidence(store, job_a, rubric_a.dimensions)
        reader_b = roles.JobEvidenceReader(store, job_b)
        rel_a = f"evidence/job-scoped/{job_a.job_id}/{rubric_a.dimensions[0]}.json"
        with pytest.raises(roles.EvidenceNotAuthorized):
            reader_b.read_by_path(rel_a)

    def test_tool_rejects_absolute_traversal_and_legacy_paths(self, tmp_path):
        store, job, _ = self._two_jobs(tmp_path)
        rubric = _rubric_for(_manifest(), job)
        roles.materialize_job_evidence(store, job, rubric.dimensions)
        reader = roles.JobEvidenceReader(store, job)
        tool = roles.make_job_reader_tool(reader)
        for bad in (
            "/etc/passwd",
            "..",
            "evidence/../input_manifest.json",
            "eval_workspace/x.json",
            f"evidence/job-scoped/{uuid4()}/x.json",
        ):
            out = tool.invoke({"path": bad})
            assert "evidence_not_authorized" in out, (bad, out)
        ok = tool.invoke(
            {"path": f"evidence/job-scoped/{job.job_id}/{rubric.dimensions[0]}.json"}
        )
        assert "evidence_not_authorized" not in ok
        assert json.loads(ok)["job_id"] == str(job.job_id)


# ─────────────────────────────────────────────────────────────────────────────
# 每次 invocation：独立 agent_id / thread / backend / 空 history
# ─────────────────────────────────────────────────────────────────────────────


class TestInvocationIsolation:
    def test_per_invocation_backend_context_history_isolated(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="ok")
        rt, store = _runtime(tmp_path, manifest)
        runners: list[roles.ScoreRoleRunner] = []
        _wire(rt, model, collect_runners=runners)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        assert runners, "expected one runner per score job"
        assert len(runners) == len(w._list_jobs(store))
        all_invoc = [inv for rnr in runners for inv in rnr.invocations]
        assert len(all_invoc) == len(w._list_jobs(store))
        # thread / agent_id / backend 逐 invocation 互不相同。
        assert len({i["thread_id"] for i in all_invoc}) == len(all_invoc)
        assert len({i["agent_id"] for i in all_invoc}) == len(all_invoc)
        assert len({i["backend"] for i in all_invoc}) == len(all_invoc)
        # 每个 invocation 以空 history 开始（history_len == 0）。
        assert all(i["history_len"] == 0 for i in all_invoc)
        # thread 锚定 invocation_id —— 同 job 不同 invocation 也不会复用。
        for inv in all_invoc:
            assert str(inv["invocation_id"]) in inv["thread_id"]

    def test_runner_binds_job_and_rejects_cross_job_reuse(self, tmp_path):
        manifest = _manifest()
        rt, store = _runtime(tmp_path, manifest)
        _materialize_bundle(rt, store)
        jobs = w._build_jobs(rt)
        runner = roles.ScoreRoleRunner(
            rt.store, rt.manifest, FakeRoleModel(behavior="ok"), lambda r: "judge"
        )
        with pytest.raises(RunnerError, match="bound to job"):
            import asyncio

            async def _reuse():
                await runner.run(jobs[0], invocation_id=uuid4(), node_attempt=1)
                await runner.run(jobs[1], invocation_id=uuid4(), node_attempt=1)

            asyncio.run(_reuse())


# ─────────────────────────────────────────────────────────────────────────────
# 非法 evidence / 非法 draft → typed FAILED → workflow PARTIAL（不污染 score）
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidDraftNoPollution:
    def test_wrong_role_draft_failed_partial_no_success(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="wrong_role")
        rt, store = _runtime(tmp_path, manifest)
        _wire(rt, model)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert results
        assert all(r.status is c.ScoreJobStatus.FAILED for r in results)
        assert all(r.validation_errors for r in results)
        assert all(any("draft role" in e for e in r.validation_errors) for r in results)

    def test_cross_job_evidence_failed_partial_no_success(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="bad_evidence")
        rt, store = _runtime(tmp_path, manifest)
        _wire(rt, model)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert results
        assert all(r.status is c.ScoreJobStatus.FAILED for r in results)
        assert all(r.validation_errors for r in results)
        assert all(
            any("not authorized" in e for e in r.validation_errors) for r in results
        )
        # 绝无成功 ScoreResult 携带非法证据。
        assert not any(r.dimension_evidence_refs for r in results)

    def test_abstain_is_typed_unknown_not_success_or_failure(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="abstain")
        rt, store = _runtime(tmp_path, manifest)
        _wire(rt, model)
        outcome = w.run_eval_workflow(rt)
        # Unknown ≠ 0、不伪造成功；requested path 无 required 缺失 → SUCCEEDED。
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        results = w._load_all_results(store)
        assert all(r.status is c.ScoreJobStatus.UNKNOWN for r in results)
        assert all(r.validated_output_ref is not None for r in results)
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        group = merged["merged_scores"]["dispatch"]
        assert group["rubrics"][0]["scored"] == 0
        assert "unknown_excluded" in group["rubrics"][0]["excluded_reasons"].values()


# ─────────────────────────────────────────────────────────────────────────────
# 正向集成：真实 role runner 端到端成功 + 每维度证据 ref 可 verified read
# ─────────────────────────────────────────────────────────────────────────────


class TestValidRoleRun:
    def test_valid_role_run_succeeds_and_evidence_refs_verify(self, tmp_path):
        manifest = _manifest()
        model = FakeRoleModel(behavior="ok")
        rt, store = _runtime(tmp_path, manifest)
        _wire(rt, model)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        results = w._load_all_results(store)
        assert results
        assert all(r.status is c.ScoreJobStatus.SUCCEEDED for r in results)
        for res in results:
            for ev in res.dimension_evidence_refs.values():
                assert ev.ref.path.startswith(
                    f"evidence/job-scoped/{res.binding.job_id}/"
                ), ev.ref.path
                assert ev.claim_type is c.ClaimType.SCORE
                store.read_verified(ev.ref)
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.REQUESTED

    def test_default_prompt_resolver_reads_role_prompts(self, tmp_path):
        for role in (
            c.JudgeRole.DISPATCH_SCORE_JUDGE,
            c.JudgeRole.OBSERVATION_SCORE_JUDGE,
        ):
            text = roles._default_prompt_resolver(role)
            assert "read_job_evidence" in text
            assert "ScoreDraft" in text


# ─────────────────────────────────────────────────────────────────────────────
# P4 score-role prompts and legacy CLI subagent prompts must not share a contract.
# Legacy `collect_judge_results()` consumes `save_judge_verdict()` JSON, whereas
# ScoreRoleRunner consumes job-scoped evidence and structured ScoreDraft output.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("factory", "score_role", "mentions_verdict_writer"),
    [
        (subagents.make_dispatch_judge, c.JudgeRole.DISPATCH_SCORE_JUDGE, True),
        (subagents.make_observation_judge, c.JudgeRole.OBSERVATION_SCORE_JUDGE, False),
    ],
)
def test_legacy_subagent_prompt_is_isolated_from_score_role_contract(
    factory, score_role, mentions_verdict_writer
):
    legacy_prompt = factory()["system_prompt"]
    score_prompt = roles._default_prompt_resolver(score_role)

    assert "STRICT JSON SCHEMA" in legacy_prompt
    assert "read_job_evidence" not in legacy_prompt
    assert "ScoreDraft" not in legacy_prompt
    assert "read_job_evidence" in score_prompt
    assert "ScoreDraft" in score_prompt
    if mentions_verdict_writer:
        assert "save_judge_verdict" in legacy_prompt


@pytest.mark.parametrize(
    "factory",
    [subagents.make_dispatch_judge, subagents.make_observation_judge],
)
def test_legacy_subagent_exposes_verdict_writer(factory):
    tool_names = {tool.name for tool in factory()["tools"]}
    assert "save_judge_verdict" in tool_names


# ─────────────────────────────────────────────────────────────────────────────
# factory 必须显式注入 model；绝不静默构造 LLM
# ─────────────────────────────────────────────────────────────────────────────


class TestFactoryRequiresModel:
    def test_factory_without_model_rejects(self, tmp_path):
        rt, _ = _runtime(tmp_path, _manifest())
        with pytest.raises(roles.RoleRunnerError, match="explicit model"):
            roles.make_role_runner_factory(rt)
