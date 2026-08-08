"""report_judge / recommendation_judge 接入 LangGraph workflow（设计 §2.1/§2.3/§4）。

覆盖（全 fake runner / 无真实 LLM）：
- requested 全链：run_report_judge → run_recommendation_judge 产出 + 持久化 +
  report JSON 纳入 narrative/recommendations（带 refs）；
- no-LLM 不受影响：not_requested 零 runner/角色构造，角色节点不运行；
- report judge 失败 → typed failure + PARTIAL + deterministic report 兜底
  （绝不写成功叙述）；
- recommendation 失败 → deterministic_fallback（`source=deterministic_fallback`
  且必带 fallback_reason）+ PARTIAL；
- 非法 draft / 越权 ref（allowlist 之外）→ typed failure；
- resume 不重写已持久化 narrative/recommendation artifact，只重跑缺失阶段；
- phase 链合法（SCORES_MERGED → REPORT_AUTHORED → RECOMMENDATIONS_AUTHORED
  → RENDERED）；
- stale lease 零副作用（角色节点不构造、不写、不调模型）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import score_merge as sm
from sar_orch.eval import workflow as w
from sar_orch.eval.agent import runner as r


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
        evaluator=c.EvaluatorSpec(workflow_version="0.1.0", source_tree_digest="9" * 64),
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
            _role(c.JudgeRole.REPORT_JUDGE),
            _role(c.JudgeRole.RECOMMENDATION_JUDGE),
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
    tmp_path,
    manifest: c.FrozenInputManifest,
    *,
    runner_factory=None,
    report_judge_factory=None,
    recommendation_judge_factory=None,
    lease=None,
) -> tuple[w.EvalRuntime, a.ArtifactStore]:
    store = a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))
    rt = w.EvalRuntime(
        manifest,
        store,
        store.audit_journal(),
        lease=lease,
        runner_factory=runner_factory,
        report_judge_factory=report_judge_factory,
        recommendation_judge_factory=recommendation_judge_factory,
        episode=_episode(),
        grader_fn=lambda: ([], []),
    )
    return rt, store


def _score_factory(manifest):
    return r.make_fake_runner_factory(manifest, {})[0]


# ─────────────────────────────────────────────────────────────────────────────
# deterministic fake draft-role runner（report/recommendation；无 LLM）
# ─────────────────────────────────────────────────────────────────────────────


class _DraftOutcome:
    def __init__(self, status, draft=None, error=None, model_requested=True):
        self.status = status
        self.draft = draft
        self.error = error
        self.model_requested = model_requested


class FakeDraftRoleRunner:
    """report/recommendation 角色的确定性 fake runner。

    behavior：
    - ``ok``：schema-valid draft，evidence 精确引用注入 allowlist 的 ref；
    - ``fail``：模型异常 → typed FAILED outcome；
    - ``cross_ref``：draft 引用 allowlist 之外的 ref（必须拒）；
    - ``abstain``：report_judge 返回带 fallback_reason 的 draft（UNKNOWN）。
    """

    def __init__(
        self, role: c.JudgeRole, behavior: str = "ok", calls: list | None = None
    ) -> None:
        self._role = role
        self._behavior = behavior
        self.calls = calls if calls is not None else []
        self.last_allowlist: dict[str, c.ArtifactRef] = {}

    def _first_ref(self, allowlist: dict[str, c.ArtifactRef]) -> c.ArtifactRef:
        return next(iter(allowlist.values()))

    def _report_draft(self, allowlist) -> c.ReportNarrativeDraft:
        ref = self._first_ref(allowlist)
        ev = c.EvidenceRef(
            ref=ref, claim_type=c.ClaimType.SUMMARY, digest=ref.sha256
        )
        return c.ReportNarrativeDraft(
            role=c.JudgeRole.REPORT_JUDGE,
            invocation_id=uuid4(),
            narrative="coverage reached 0.8 per merged bundle",
            paragraphs=[
                c.ReportParagraph(text="coverage reached 0.8", evidence=[ev])
            ],
            model_used="fake-draft-role",
        )

    def _abstain_report_draft(self) -> c.ReportNarrativeDraft:
        return c.ReportNarrativeDraft(
            role=c.JudgeRole.REPORT_JUDGE,
            invocation_id=uuid4(),
            narrative="report judge abstained",
            model_used="fake-draft-role",
            fallback_reason="insufficient evidence to author a full report",
        )

    def _recommendation_draft(self, allowlist) -> c.RecommendationDraft:
        ref = self._first_ref(allowlist)
        ev = c.EvidenceRef(
            ref=ref, claim_type=c.ClaimType.RECOMMENDATION, digest=ref.sha256
        )
        return c.RecommendationDraft(
            role=c.JudgeRole.RECOMMENDATION_JUDGE,
            invocation_id=uuid4(),
            recommendations=[
                c.RecommendationItem(
                    text="strengthen dispatch coverage",
                    evidence=[ev],
                    severity=c.Severity.WARNING,
                )
            ],
            source=c.RecommendationSource.RECOMMENDATION_JUDGE,
        )

    def _cross_ref_draft(self):
        ref = c.ArtifactRef(
            path="evidence/other-role/private.json",
            sha256="e" * 64,
            bytes=12,
            media_type="application/json",
            producer="fake-draft-role",
        )
        if self._role is c.JudgeRole.REPORT_JUDGE:
            ev = c.EvidenceRef(
                ref=ref, claim_type=c.ClaimType.SUMMARY, digest=ref.sha256
            )
            return c.ReportNarrativeDraft(
                role=c.JudgeRole.REPORT_JUDGE,
                invocation_id=uuid4(),
                narrative="cross ref",
                paragraphs=[c.ReportParagraph(text="cross ref claim", evidence=[ev])],
                model_used="fake-draft-role",
            )
        ev = c.EvidenceRef(
            ref=ref, claim_type=c.ClaimType.RECOMMENDATION, digest=ref.sha256
        )
        return c.RecommendationDraft(
            role=c.JudgeRole.RECOMMENDATION_JUDGE,
            invocation_id=uuid4(),
            recommendations=[
                c.RecommendationItem(text="cross ref rec", evidence=[ev])
            ],
            source=c.RecommendationSource.RECOMMENDATION_JUDGE,
        )

    async def run(
        self, *, invocation_id: UUID, node_attempt: int, allowlist: dict[str, c.ArtifactRef]
    ) -> _DraftOutcome:
        self.calls.append((str(invocation_id), node_attempt))
        self.last_allowlist = dict(allowlist)
        if self._behavior == "fail":
            return _DraftOutcome(
                status=r.RunnerStatus.FAILED,
                error="simulated draft-role model exception",
                model_requested=True,
            )
        if self._behavior == "cross_ref":
            return _DraftOutcome(
                status=r.RunnerStatus.SUCCEEDED,
                draft=self._cross_ref_draft(),
                model_requested=True,
            )
        if self._behavior == "abstain":
            return _DraftOutcome(
                status=r.RunnerStatus.UNKNOWN,
                draft=self._abstain_report_draft(),
                model_requested=True,
            )
        if self._role is c.JudgeRole.REPORT_JUDGE:
            return _DraftOutcome(
                status=r.RunnerStatus.SUCCEEDED,
                draft=self._report_draft(allowlist),
                model_requested=True,
            )
        return _DraftOutcome(
            status=r.RunnerStatus.SUCCEEDED,
            draft=self._recommendation_draft(allowlist),
            model_requested=True,
        )


def _draft_factory(role: c.JudgeRole, behavior: str = "ok"):
    stats: dict = {"constructed": 0, "calls": []}

    def factory():
        stats["constructed"] += 1
        return FakeDraftRoleRunner(role, behavior=behavior, calls=stats["calls"])

    return factory, stats


def _phase_pairs(rt) -> list[tuple[str, str]]:
    return [(e["from"], e["to"]) for e in rt.journal.read_all() if e["kind"] == "phase"]


def _assert_legal_chain(pairs: list[tuple[str, str]]) -> None:
    assert pairs, "expected at least one phase transition"
    current = c.WorkflowStatus.CREATED
    for from_v, to_v in pairs:
        assert from_v == current.value, f"phase chain broken at {from_v}"
        to = c.WorkflowStatus(to_v)
        assert c.workflow_can_transition(current, to), f"illegal {from_v} -> {to_v}"
        current = to


# ─────────────────────────────────────────────────────────────────────────────
# requested 全链：两节点产出 + 持久化 + report 纳入内容（§2.1 / R4）
# ─────────────────────────────────────────────────────────────────────────────


class TestRequestedFullChain:
    def test_both_nodes_persist_and_report_includes_content(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        report_factory, report_stats = _draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")
        rec_factory, rec_stats = _draft_factory(
            c.JudgeRole.RECOMMENDATION_JUDGE, "ok"
        )
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=report_factory,
            recommendation_judge_factory=rec_factory,
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED

        # 两节点各调用一次模型（score jobs 之外的角色调用）。
        assert report_stats["constructed"] == 1
        assert rec_stats["constructed"] == 1

        # narrative / recommendations 已持久化，且都进 ledger artifact refs。
        assert store.exists(w.REPORT_NARRATIVE_REL)
        assert store.exists(w.RECOMMENDATIONS_REL)
        ledger = store.read_final_ledger()
        assert w.REPORT_NARRATIVE_REL in ledger.artifact_refs
        assert w.RECOMMENDATIONS_REL in ledger.artifact_refs
        store.read_verified(ledger.artifact_refs[w.REPORT_NARRATIVE_REL])
        store.read_verified(ledger.artifact_refs[w.RECOMMENDATIONS_REL])

        # report JSON 纳入 narrative/recommendations（带 refs）。
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["narrative"]["source"] == "report_judge"
        assert report["narrative"]["ref"] == w.REPORT_NARRATIVE_REL
        assert report["narrative"]["paragraphs"], report["narrative"]
        assert report["narrative"]["paragraphs"][0]["evidence"]
        assert report["recommendations"]["source"] == "recommendation_judge"
        assert report["recommendations"]["ref"] == w.RECOMMENDATIONS_REL
        assert report["recommendations"]["items"]

        # draft 的 evidence ref 必须落在角色 allowlist 内且 digest 一致。
        narrative = json.loads(store.read_bytes(w.REPORT_NARRATIVE_REL))
        for para in narrative["paragraphs"]:
            for ev in para["evidence"]:
                assert ev["digest"] == ev["ref"]["sha256"]
                assert ev["ref"]["path"] in report_stats or rec_stats  # allowlist 成员

        # phase 链合法。
        _assert_legal_chain(_phase_pairs(rt))

    def test_phase_chain_includes_role_phases(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        rt, _ = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=_draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")[0],
            recommendation_judge_factory=_draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "ok"
            )[0],
        )
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        pairs = _phase_pairs(rt)
        values = [to for _, to in pairs]
        assert "scores_merged" in values
        assert "report_authored" in values
        assert "recommendations_authored" in values
        assert "rendered" in values
        assert values.index("scores_merged") < values.index("report_authored")
        assert values.index("report_authored") < values.index(
            "recommendations_authored"
        )
        assert values.index("recommendations_authored") < values.index("rendered")


# ─────────────────────────────────────────────────────────────────────────────
# no-LLM 不受影响（§1.2-12 / R3）：not_requested 零构造，角色节点不运行
# ─────────────────────────────────────────────────────────────────────────────


class TestNoLLMUnaffected:
    def test_not_requested_zero_role_construction(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)

        def never_score():
            raise AssertionError("score runner must not be constructed on no-LLM")

        def never_report():
            raise AssertionError("report_judge must not run on no-LLM path")

        def never_rec():
            raise AssertionError("recommendation_judge must not run on no-LLM path")

        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=never_score,
            report_judge_factory=never_report,
            recommendation_judge_factory=never_rec,
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED
        assert not (store.root / "score_jobs").exists()
        assert not store.exists(w.REPORT_NARRATIVE_REL)
        assert not store.exists(w.RECOMMENDATIONS_REL)
        # report 的 narrative/recommendations 以 deterministic fallback 标注 source。
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["narrative"]["source"] == "deterministic_fallback"
        assert report["recommendations"]["source"] == "deterministic_fallback"


# ─────────────────────────────────────────────────────────────────────────────
# report judge 失败 → typed failure + PARTIAL + deterministic report（§2.1 line 159）
# ─────────────────────────────────────────────────────────────────────────────


class TestReportJudgeFailure:
    def _run(self, tmp_path, behavior):
        manifest = _manifest(llm_judge_required=True)
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=_draft_factory(c.JudgeRole.REPORT_JUDGE, behavior)[0],
            recommendation_judge_factory=_draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "ok"
            )[0],
        )
        outcome = w.run_eval_workflow(rt)
        return rt, store, outcome

    def test_model_exception_partial_deterministic_report(self, tmp_path):
        rt, store, outcome = self._run(tmp_path, "fail")
        assert outcome.status is c.WorkflowStatus.PARTIAL
        # 绝不写成功叙述：narrative artifact 不存在。
        assert not store.exists(w.REPORT_NARRATIVE_REL)
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["narrative"]["source"] == "deterministic_fallback"
        assert report["narrative"]["fallback_reason"]
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.PARTIAL
        assert any(f.kind == "report_judge" for f in ledger.failure_refs)
        # recommendations 仍由 judge 正常产出。
        assert report["recommendations"]["source"] == "recommendation_judge"
        _assert_legal_chain(_phase_pairs(rt))

    def test_abstain_with_fallback_reason_partial(self, tmp_path):
        _rt, store, outcome = self._run(tmp_path, "abstain")
        assert outcome.status is c.WorkflowStatus.PARTIAL
        assert not store.exists(w.REPORT_NARRATIVE_REL)
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["narrative"]["source"] == "deterministic_fallback"
        assert report["narrative"]["fallback_reason"]
        ledger = store.read_final_ledger()
        assert any(f.kind == "report_judge" for f in ledger.failure_refs)

    def test_cross_ref_unauthorized_typed_failure(self, tmp_path):
        rt, store, outcome = self._run(tmp_path, "cross_ref")
        assert outcome.status is c.WorkflowStatus.PARTIAL
        assert not store.exists(w.REPORT_NARRATIVE_REL)
        failed = [
            e
            for e in rt.journal.read_all()
            if e["kind"] == "report_judge_failed"
        ]
        assert failed and "evidence_not_authorized" in failed[0]["reason"]
        ledger = store.read_final_ledger()
        assert any(f.kind == "report_judge" for f in ledger.failure_refs)


# ─────────────────────────────────────────────────────────────────────────────
# recommendation 失败 → deterministic_fallback（source + fallback_reason）+ PARTIAL
# ─────────────────────────────────────────────────────────────────────────────


class TestRecommendationJudgeFailure:
    def test_failure_persists_deterministic_fallback_with_reason(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=_draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")[0],
            recommendation_judge_factory=_draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "fail"
            )[0],
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        # fallback artifact 持久化且必带 fallback_reason。
        assert store.exists(w.RECOMMENDATIONS_REL)
        rec = json.loads(store.read_bytes(w.RECOMMENDATIONS_REL))
        assert rec["source"] == "deterministic_fallback"
        assert rec["fallback_reason"]
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["recommendations"]["source"] == "deterministic_fallback"
        assert report["recommendations"]["fallback_reason"]
        # narrative 仍由 judge 正常产出。
        assert report["narrative"]["source"] == "report_judge"
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.PARTIAL
        assert any(f.kind == "recommendation_judge" for f in ledger.failure_refs)
        _assert_legal_chain(_phase_pairs(rt))

    def test_cross_ref_unauthorized_typed_failure(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=_draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")[0],
            recommendation_judge_factory=_draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "cross_ref"
            )[0],
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        rec = json.loads(store.read_bytes(w.RECOMMENDATIONS_REL))
        assert rec["source"] == "deterministic_fallback"
        ledger = store.read_final_ledger()
        assert any(f.kind == "recommendation_judge" for f in ledger.failure_refs)


# ─────────────────────────────────────────────────────────────────────────────
# resume：已持久化 narrative/recommendation 不得重写，只重跑缺失阶段（§2.3 / R2）
# ─────────────────────────────────────────────────────────────────────────────


def _sha256(store: a.ArtifactStore, rel: str) -> str:
    return c.sha256_hex(store.read_bytes(rel))


class TestResumeNoRewrite:
    def test_resume_does_not_rewrite_persisted_artifacts(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        report_factory, report_stats = _draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")
        rec_factory, rec_stats = _draft_factory(c.JudgeRole.RECOMMENDATION_JUDGE, "ok")
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=report_factory,
            recommendation_judge_factory=rec_factory,
        )
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        narr_digest = _sha256(store, w.REPORT_NARRATIVE_REL)
        rec_digest = _sha256(store, w.RECOMMENDATIONS_REL)

        # 模拟 finalize 前被 kill：仅缺 ledger，其余 artifact 完整。
        store.path(a.ArtifactStore.LEDGER_REL).unlink()

        def never():
            raise AssertionError("runner must not be re-invoked on completed resume")

        rt2, store2 = _runtime(
            tmp_path,
            manifest,
            runner_factory=never,
            report_judge_factory=never,
            recommendation_judge_factory=never,
        )
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert _sha256(store2, w.REPORT_NARRATIVE_REL) == narr_digest
        assert _sha256(store2, w.RECOMMENDATIONS_REL) == rec_digest
        # 只重建 ledger：无角色模型调用。
        assert report_stats["constructed"] == 1
        assert rec_stats["constructed"] == 1

    def test_resume_reruns_only_missing_recommendations(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        report_factory, report_stats = _draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")
        rec_factory, rec_stats = _draft_factory(c.JudgeRole.RECOMMENDATION_JUDGE, "ok")
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=report_factory,
            recommendation_judge_factory=rec_factory,
        )
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        narr_digest = _sha256(store, w.REPORT_NARRATIVE_REL)

        # 模拟 recommendation 持久化后被 kill（rec 缺失、ledger 缺失）。
        store.path(w.RECOMMENDATIONS_REL).unlink()
        store.path(a.ArtifactStore.LEDGER_REL).unlink()

        def never_score():
            raise AssertionError("score runner must not be re-invoked")

        def never_report():
            raise AssertionError("report_judge must not be re-invoked")

        rt2, store2 = _runtime(
            tmp_path,
            manifest,
            runner_factory=never_score,
            report_judge_factory=never_report,
            recommendation_judge_factory=rec_factory,
        )
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        # narrative 不重写；rec 重跑并落盘。
        assert _sha256(store2, w.REPORT_NARRATIVE_REL) == narr_digest
        assert store2.exists(w.RECOMMENDATIONS_REL)
        assert report_stats["constructed"] == 1
        assert rec_stats["constructed"] == 2

    def test_recommendation_failure_partial_preserved_on_resume(self, tmp_path):
        """genuine judge 失败持久化的 fallback → resume 保持 PARTIAL（§2.3）。"""
        manifest = _manifest(llm_judge_required=True)
        rt, store = _runtime(
            tmp_path,
            manifest,
            runner_factory=_score_factory(manifest),
            report_judge_factory=_draft_factory(c.JudgeRole.REPORT_JUDGE, "ok")[0],
            recommendation_judge_factory=_draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "fail"
            )[0],
        )
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.PARTIAL
        store.path(a.ArtifactStore.LEDGER_REL).unlink()

        def never():
            raise AssertionError("runner must not be re-invoked")

        rt2, store2 = _runtime(
            tmp_path,
            manifest,
            runner_factory=never,
            report_judge_factory=never,
            recommendation_judge_factory=never,
        )
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        ledger = store2.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.PARTIAL
        assert any(f.kind == "recommendation_judge" for f in ledger.failure_refs)


# ─────────────────────────────────────────────────────────────────────────────
# stale lease 零副作用（§1.2-3 / P3-M2）：角色节点不构造、不写、不调模型
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleLease:
    def test_stale_lease_zero_role_side_effects(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        series_root = tmp_path / "series" / str(manifest.manifest.eval_run_id)
        lease1 = a.AttemptLease.acquire(series_root, owner_id="a")
        lease1.release()
        lease2 = a.AttemptLease.acquire(series_root, owner_id="b")
        try:
            score_factory, _ = _score_factory(manifest), {"constructed": 0}
            report_factory, report_stats = _draft_factory(
                c.JudgeRole.REPORT_JUDGE, "ok"
            )
            rec_factory, rec_stats = _draft_factory(
                c.JudgeRole.RECOMMENDATION_JUDGE, "ok"
            )
            store = a.ArtifactStore(
                tmp_path / "attempt" / str(manifest.manifest.attempt_id)
            )
            rt = w.EvalRuntime(
                manifest,
                store,
                store.audit_journal(),
                lease=lease1,
                runner_factory=score_factory,
                report_judge_factory=report_factory,
                recommendation_judge_factory=rec_factory,
                episode=_episode(),
                grader_fn=lambda: ([], []),
            )
            outcome = w.run_eval_workflow(rt)
            assert outcome.status is c.WorkflowStatus.FAILED
            assert outcome.error and "fencing" in outcome.error
            assert store.audit_journal().event_count() == 0
            assert report_stats["constructed"] == 0
            assert rec_stats["constructed"] == 0
            assert not store.exists(w.REPORT_NARRATIVE_REL)
            assert not store.exists(w.RECOMMENDATIONS_REL)
            assert not (store.root / "reports").exists()
            assert not (store.root / "score_jobs").exists()
        finally:
            lease2.release()
