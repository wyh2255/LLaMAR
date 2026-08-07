"""P2 确定性 score merge 矩阵（设计 §5.2 / §5.3 / §9 P2）。

不构造任何 DeepAgent / chat 模型；只验证纯合并逻辑。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval import score_merge as sm
from sar_orch.eval.rubric_registry import DEFAULT_DISPATCH_RUBRIC_ID


def _art(path="evidence/sample.json", sha=None, n=12) -> c.ArtifactRef:
    return c.ArtifactRef(
        path=path,
        sha256=sha if sha is not None else "a" * 64,
        bytes=n,
        media_type="application/json",
        producer="test",
    )


def _evidence_ref(dim: str) -> c.EvidenceRef:
    sha = c.sha256_hex(dim.encode("utf-8"))
    return c.EvidenceRef(
        ref=c.ArtifactRef(
            path=f"evidence/{dim}.json",
            sha256=sha,
            bytes=4,
            media_type="application/json",
            producer="validator",
        ),
        claim_type=c.ClaimType.SCORE,
        digest=sha,
    )


def _rubric(
    target_type=c.SampleTargetType.DISPATCH,
    digest=None,
    dimensions=("d1", "d2"),
    judge_role=None,
    weight=1.0,
    rubric_id=DEFAULT_DISPATCH_RUBRIC_ID,
) -> c.RubricSpec:
    return c.RubricSpec(
        rubric_id=rubric_id,
        version="1.0.0",
        digest=digest or ("3" * 64),
        target_type=target_type,
        input_selector="dispatch_samples",
        dimensions=list(dimensions),
        prompt_template_ref=_art("snapshots/prompts/dispatch.md"),
        judge_role=judge_role
        or (
            c.JudgeRole.DISPATCH_SCORE_JUDGE
            if target_type is c.SampleTargetType.DISPATCH
            else c.JudgeRole.OBSERVATION_SCORE_JUDGE
        ),
        merge_group="dispatch"
        if target_type is c.SampleTargetType.DISPATCH
        else "observation",
        weight=weight,
    )


def _job(
    rubric: c.RubricSpec,
    source="1" * 64,
    evidence="2" * 64,
    manifest="9" * 64,
    **overrides,
) -> c.ScoreJob:
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "job_digest": "0" * 64,
        "sample_id": uuid4(),
        "target_type": rubric.target_type,
        "source_digest": source,
        "evidence_digest": evidence,
        "rubric_id": rubric.rubric_id,
        "rubric_digest": rubric.digest,
        "role": rubric.judge_role,
        "prompt_digest": "4" * 64,
        "model_profile_digest": "5" * 64,
        "tool_schema_digest": "6" * 64,
        "input_bundle_ref": _art("input_bundle/1.json"),
        "retry_policy_digest": "8" * 64,
        "manifest_digest": manifest,
    }
    data.update(overrides)
    job = c.ScoreJob(**data)
    # 用与 rubric_registry.build_score_job 同一 canonical 定义重算 job_digest，
    # 使 merge 的逐 job digest 校验对正例放行。
    job_digest = c.sha256_hex(
        c.canonical_json(rr._score_job_payload(job)).encode("utf-8")
    )
    return job.model_copy(update={"job_digest": job_digest})


def _result(
    job: c.ScoreJob,
    status=c.ScoreJobStatus.SUCCEEDED,
    evidence_digest=None,
    rubric_digest=None,
    prompt_digest=None,
    dims=("d1", "d2"),
    validated=True,
) -> c.ScoreResult:
    binding = c.ScoreJobBinding(
        job_id=job.job_id,
        target_type=job.target_type,
        source_digest=job.source_digest,
        evidence_digest=evidence_digest or job.evidence_digest,
        rubric_id=job.rubric_id,
        rubric_digest=rubric_digest or job.rubric_digest,
        role=job.role,
        prompt_digest=prompt_digest or job.prompt_digest,
        model_profile_digest=job.model_profile_digest,
        tool_schema_digest=job.tool_schema_digest,
    )
    dim_refs = {d: _evidence_ref(d) for d in dims}
    return c.ScoreResult(
        binding=binding,
        status=status,
        dimension_evidence_refs=dim_refs,
        validated_output_ref=_art("output/validated.json") if validated else None,
        node_attempt=1,
    )


def _scores(job: c.ScoreJob, value=1.0, dims=("d1", "d2")):
    return {job.job_id: {d: float(value) for d in dims}}


def _group(
    target_type=c.SampleTargetType.DISPATCH,
    jobs=None,
    results=None,
    score_values=None,
    rubrics=None,
) -> sm.GroupMergeInput:
    jobs = tuple(jobs or ())
    return sm.GroupMergeInput(
        target_type=target_type,
        jobs=jobs,
        results=tuple(results or ()),
        score_values=score_values or {},
        rubrics=tuple(rubrics or ()),
    )


def _merge(
    groups,
    policy=None,
    manifest="9" * 64,
    violations=(),
):
    policy = policy or sm.MergePolicy()
    return sm.merge_score_groups(
        groups,
        policy=policy,
        merge_policy_digest=policy.digest(),
        manifest_digest=manifest,
        deterministic_violations=violations,
    )


def _single_group(
    rubrics=(),
    jobs=(),
    results=(),
    score_values=None,
    status=c.ScoreJobStatus.SUCCEEDED,
    dims=("d1", "d2"),
    value=1.0,
):
    rubric = rubrics[0] if rubrics else _rubric()
    jobs = list(jobs) or [_job(rubric)]
    results = list(results) or [_result(jobs[0], status=status, dims=dims)]
    score_values = score_values or {jobs[0].job_id: {d: float(value) for d in dims}}
    return sm.GroupMergeInput(
        target_type=rubric.target_type,
        jobs=tuple(jobs),
        results=tuple(results),
        score_values=score_values,
        rubrics=tuple(rubrics or [rubric]),
    )


class TestMergeSuccess:
    def test_single_job_ok(self):
        group = _single_group(value=0.8)
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        assert out.judge_execution_status is c.JudgeExecutionStatus.REQUESTED
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.OK
        assert gs.score == 0.8
        rs = gs.rubrics[0]
        assert rs.scored == 1 and rs.excluded == 0
        assert rs.dimension_scores == {"d1": 0.8, "d2": 0.8}
        assert rs.included_job_ids == [str(group.jobs[0].job_id)]

    def test_two_different_rubric_digests_same_target_merge(self):
        """§5.3：两个不同 rubric digest、同 source/evidence/target、binding 正确
        → weighted merge 成功；不同 eligible rubric digest 是正常 fan-out。"""
        rubric_a = _rubric(digest="a" * 64, weight=1.0)
        rubric_b = _rubric(digest="b" * 64, weight=1.0)
        job_a = _job(rubric_a)
        job_b = _job(rubric_b)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job_a, job_b),
            results=(_result(job_a), _result(job_b)),
            score_values={
                job_a.job_id: {"d1": 1.0, "d2": 1.0},
                job_b.job_id: {"d1": 0.5, "d2": 0.5},
            },
            rubrics=(rubric_a, rubric_b),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        gs = out.merged_scores["dispatch"]
        assert len(gs.rubrics) == 2
        assert {rs.rubric_digest for rs in gs.rubrics} == {
            "a" * 64,
            "b" * 64,
        }
        assert gs.score == pytest.approx(0.75)
        # disagreement 保留两个 rubric 的逐维度分值
        assert gs.disagreement["d1"] == pytest.approx([1.0, 0.5])

    def test_weighted_merge_uses_rubric_weights(self):
        rubric_a = _rubric(digest="a" * 64, weight=1.0)
        rubric_b = _rubric(digest="b" * 64, weight=3.0)
        job_a = _job(rubric_a)
        job_b = _job(rubric_b)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job_a, job_b),
            results=(
                _result(job_a, dims=("d1", "d2"), validated=True),
                _result(job_b, dims=("d1", "d2"), validated=True),
            ),
            score_values={
                job_a.job_id: {"d1": 1.0, "d2": 1.0},
                job_b.job_id: {"d1": 0.5, "d2": 0.5},
            },
            rubrics=(rubric_a, rubric_b),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        # (1.0*1 + 0.5*3) / 4
        assert gs.score == pytest.approx(0.625)

    def test_empty_group_is_ok_not_error(self):
        group = _group()
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        gs = out.merged_scores["dispatch"]
        assert gs.score is None
        assert gs.rubrics == []


class TestJobDigestValidation:
    """§5.2-1 / §3.5：逐 job 的 canonical job_digest 校验 —— stale/forged 一律 FAILED。"""

    def test_valid_job_digest_passes(self):
        # `_job()` 已按 canonical 定义重算 job_digest，正例必须放行。
        group = _single_group(value=0.7)
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        assert out.merged_scores["dispatch"].score == 0.7

    def test_stale_job_digest_after_mutation_fails(self):
        """job 的不可变字段在创建后被篡改、job_digest 保持旧值 → FAILED。"""
        group = _single_group()
        tampered = group.jobs[0].model_copy(update={"evidence_digest": "f" * 64})
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=(tampered,),
            results=group.results,
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert out.merged_scores is None
        assert any("job_digest" in v for v in out.violations)

    def test_forged_job_digest_fails(self):
        """直接伪造 job_digest（占位/伪造值）→ FAILED。"""
        job = _job(_rubric())
        forged = job.model_copy(update={"job_digest": "0" * 64})
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(forged,),
            results=(_result(job),),
            score_values={job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(_rubric(),),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert out.merged_scores is None
        assert any("job_digest" in v for v in out.violations)

    def test_digest_covers_immutable_fields_not_status(self):
        """job_digest 忽略 status：仅改 status 不得视为篡改。"""
        group = _single_group()
        job = group.jobs[0]
        claimed = job.model_copy(update={"status": c.ScoreJobStatus.CLAIMED})
        expected = c.sha256_hex(
            c.canonical_json(rr._score_job_payload(job)).encode("utf-8")
        )
        # status 不影响 canonical digest
        assert claimed.job_digest == expected
        assert rr._score_job_payload(claimed) == rr._score_job_payload(job)

    def test_merge_accepts_build_score_job_produced_job(self):
        """与 rubric_registry.build_score_job 的 canonical 定义单一来源一致：
        production builder 产出的 job 必须通过 merge 的 digest 校验。"""
        sample = c.Sample(
            sample_id=uuid4(),
            target_type=c.SampleTargetType.DISPATCH,
            step=1,
            agent=None,
            claim_call_ids=[],
            source_digest="1" * 64,
            evidence_digest="2" * 64,
            selection_reason="first_step",
            ordinal=0,
            selected_under_overflow=False,
        )
        rubric = _rubric()
        role = c.RoleConfig(
            role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
            agent_id="dispatch_score_judge",
            prompt_ref=_art("snapshots/prompts/dispatch.md", sha="3" * 64),
            prompt_digest="4" * 64,
            model_profile_ref=_art("snapshots/model_profiles/score.json", sha="5" * 64),
            model_profile_digest="6" * 64,
            tool_schema_ref=_art("snapshots/tool_schemas/score.json", sha="7" * 64),
            tool_schema_digest="8" * 64,
        )
        job = rr.build_score_job(
            sample,
            rubric,
            role,
            input_bundle_ref=_art("input_bundle/1.json"),
            manifest_digest="9" * 64,
            retry_policy_digest="8" * 64,
        )
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1", "d2")),),
            score_values={job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        assert out.merged_scores["dispatch"].score == 1.0


class TestMisbindingFails:
    def test_evidence_digest_mismatch_fails(self):
        group = _single_group()
        job = group.jobs[0]
        bad = _result(job, evidence_digest="f" * 64)
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=group.jobs,
            results=(bad,),
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert out.merged_scores is None
        assert any("binding mismatch" in v for v in out.violations)

    def test_rubric_digest_mismatch_fails(self):
        group = _single_group()
        job = group.jobs[0]
        bad = _result(job, rubric_digest="f" * 64)
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=group.jobs,
            results=(bad,),
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED

    def test_prompt_digest_mismatch_fails(self):
        group = _single_group()
        job = group.jobs[0]
        bad = _result(job, prompt_digest="f" * 64)
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=group.jobs,
            results=(bad,),
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED

    def test_result_for_unknown_job_fails(self):
        group = _single_group()
        stranger = _job(_rubric(digest="c" * 64))
        extra = _result(stranger)
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=group.jobs,
            results=(*group.results, extra),
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED

    def test_rubric_digest_not_supplied_fails(self):
        """job 声明的 rubric digest 不在 merge 输入中 → FAILED。"""
        job = _job(_rubric(digest="d" * 64))
        supplied = _rubric(digest="e" * 64)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job),),
            score_values=_scores(job),
            rubrics=(supplied,),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert any("not supplied" in v for v in out.violations)

    def test_duplicate_result_for_same_job_fails(self):
        group = _single_group()
        job = group.jobs[0]
        dup = _result(job)
        group = sm.GroupMergeInput(
            target_type=group.target_type,
            jobs=group.jobs,
            results=(*group.results, dup),
            score_values=group.score_values,
            rubrics=group.rubrics,
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert any("duplicate" in v for v in out.violations)


class TestCrossJobConsistency:
    def test_cross_job_source_mismatch_fails(self):
        rubric = _rubric()
        a, b = _job(rubric, source="1" * 64), _job(rubric, source="2" * 64)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(a, b),
            results=(_result(a), _result(b)),
            score_values={
                a.job_id: {"d1": 1.0, "d2": 1.0},
                b.job_id: {"d1": 1.0, "d2": 1.0},
            },
            rubrics=(rubric,),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED
        assert any("source_digest" in v for v in out.violations)

    def test_cross_job_evidence_mismatch_fails(self):
        rubric = _rubric()
        a, b = _job(rubric, evidence="2" * 64), _job(rubric, evidence="3" * 64)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(a, b),
            results=(_result(a), _result(b)),
            score_values={
                a.job_id: {"d1": 1.0, "d2": 1.0},
                b.job_id: {"d1": 1.0, "d2": 1.0},
            },
            rubrics=(rubric,),
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED

    def test_cross_job_manifest_mismatch_fails(self):
        rubric = _rubric()
        a, b = _job(rubric, manifest="9" * 64), _job(rubric, manifest="8" * 64)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(a, b),
            results=(_result(a), _result(b)),
            score_values={
                a.job_id: {"d1": 1.0, "d2": 1.0},
                b.job_id: {"d1": 1.0, "d2": 1.0},
            },
            rubrics=(rubric,),
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED

    def test_job_manifest_mismatch_with_merge_manifest_fails(self):
        group = _single_group()
        job = _job(_rubric(), manifest="8" * 64)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job),),
            score_values=_scores(job),
            rubrics=(_rubric(),),
        )
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.FAILED

    def test_job_target_type_mismatch_with_group_fails(self):
        obs_rubric = _rubric(
            target_type=c.SampleTargetType.OBSERVATION,
            dimensions=("hallucination_rate",),
            judge_role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
            rubric_id="observation-v1",
        )
        job = _job(obs_rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("hallucination_rate",)),),
            score_values={job.job_id: {"hallucination_rate": 1.0}},
            rubrics=(obs_rubric,),
        )
        assert _merge([group]).status is sm.MergedScoreStatus.FAILED


class TestUnknown:
    def test_unknown_never_scored_as_zero(self):
        """§5.3：Unknown 不等于 0。一个 succeeded(1.0) + 一个 unknown
        → group score 是 1.0，绝不是 0.5。"""
        rubric = _rubric()
        ok_job, unk_job = _job(rubric), _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(ok_job, unk_job),
            results=(
                _result(ok_job),
                _result(unk_job, status=c.ScoreJobStatus.UNKNOWN),
            ),
            score_values={ok_job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        out = _merge([group])
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.OK
        assert gs.score == 1.0
        rs = gs.rubrics[0]
        assert rs.excluded == 1
        assert rs.excluded_reasons[str(unk_job.job_id)] == "unknown_excluded"
        assert str(unk_job.job_id) not in rs.included_job_ids
        assert str(unk_job.job_id) not in rs.per_job_scores

    def test_unknown_partial_policy(self):
        rubric = _rubric()
        ok_job, unk_job = _job(rubric), _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(ok_job, unk_job),
            results=(
                _result(ok_job),
                _result(unk_job, status=c.ScoreJobStatus.UNKNOWN),
            ),
            score_values={ok_job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        out = _merge(
            [group],
            policy=sm.MergePolicy(unknown_policy=sm.UnknownPolicy.PARTIAL),
        )
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert any("unknown" in r for r in gs.reasons)

    def test_unknown_block_policy(self):
        rubric = _rubric()
        ok_job, unk_job = _job(rubric), _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(ok_job, unk_job),
            results=(
                _result(ok_job),
                _result(unk_job, status=c.ScoreJobStatus.UNKNOWN),
            ),
            score_values={ok_job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        out = _merge(
            [group],
            policy=sm.MergePolicy(unknown_policy=sm.UnknownPolicy.BLOCK),
        )
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.BLOCKED
        assert gs.score is None


class TestRequiredMissing:
    def test_missing_result_partial(self):
        rubric = _rubric()
        present, missing = _job(rubric), _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(present, missing),
            results=(_result(present),),
            score_values={present.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        out = _merge([group])
        gs = out.merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert any("missing" in r for r in gs.reasons)

    @pytest.mark.parametrize(
        "status",
        [
            c.ScoreJobStatus.FAILED,
            c.ScoreJobStatus.TIMED_OUT,
            c.ScoreJobStatus.CANCELLED,
            c.ScoreJobStatus.NOT_RUN_BUDGET,
        ],
    )
    def test_hard_failures_partial(self, status):
        rubric = _rubric()
        job = _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, status=status),),
            score_values={},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert gs.score is None

    def test_incomplete_scale_partial(self):
        """§5.2-3：只归一 scale 完整的结果；succeeded 但证据维度不全 → 不参与计分。"""
        rubric = _rubric()
        job = _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1",)),),
            score_values={job.job_id: {"d1": 1.0}},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert gs.score is None
        assert gs.rubrics[0].excluded_reasons[str(job.job_id)] == (
            "incomplete_evidence_scale"
        )

    def test_succeeded_without_score_values_partial(self):
        rubric = _rubric()
        job = _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1", "d2")),),
            score_values={},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert gs.rubrics[0].excluded_reasons[str(job.job_id)] == (
            "missing_score_values"
        )

    def test_incomplete_dimensions_in_score_values_partial(self):
        rubric = _rubric()
        job = _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1", "d2")),),
            score_values={job.job_id: {"d1": 1.0}},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert gs.rubrics[0].excluded_reasons[str(job.job_id)] == (
            "incomplete_dimensions"
        )

    def test_out_of_scale_value_partial(self):
        rubric = _rubric()
        job = _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1", "d2")),),
            score_values={job.job_id: {"d1": 1.0, "d2": 3.0}},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.PARTIAL
        assert gs.rubrics[0].excluded_reasons[str(job.job_id)] == ("out_of_scale_value")

    def test_late_ignored_is_excluded_without_partial(self):
        rubric = _rubric()
        ok_job, late_job = _job(rubric), _job(rubric)
        group = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(ok_job, late_job),
            results=(
                _result(ok_job),
                _result(late_job, status=c.ScoreJobStatus.LATE_IGNORED),
            ),
            score_values={ok_job.job_id: {"d1": 1.0, "d2": 1.0}},
            rubrics=(rubric,),
        )
        gs = _merge([group]).merged_scores["dispatch"]
        assert gs.status is sm.MergedScoreStatus.OK
        assert gs.rubrics[0].excluded_reasons[str(late_job.job_id)] == "late_ignored"


class TestVeto:
    def test_deterministic_violation_vetoes_high_scores(self):
        """§5.3：deterministic hard violation 不被高 LLM 分抵消 —— veto。"""
        group = _single_group(value=1.0)
        out = _merge(
            [group],
            violations=[
                sm.DeterministicViolation(
                    kind="hard_violation",
                    reason="carry performed without two coupled agents at deposit",
                )
            ],
        )
        assert out.status is sm.MergedScoreStatus.VETOED
        assert out.veto_reasons
        assert out.merged_scores["dispatch"].status is sm.MergedScoreStatus.VETOED
        assert out.merged_scores["dispatch"].score is None

    def test_no_veto_without_violations(self):
        group = _single_group(value=1.0)
        out = _merge([group])
        assert out.status is sm.MergedScoreStatus.OK
        assert out.veto_reasons == []

    def test_veto_reports_typed_kind(self):
        group = _single_group(value=1.0)
        out = _merge(
            [group],
            violations=[sm.DeterministicViolation(kind="path_escape", reason="x")],
        )
        assert out.violations == ["path_escape"]


class TestNoLlmProjection:
    def test_project_not_requested(self):
        """§5.3 / §1.2-12：no-LLM merge projection = not_requested + None scores。"""
        out = sm.project_not_requested(policy_digest="7" * 64)
        assert out.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED
        assert out.status is sm.MergedScoreStatus.NOT_REQUESTED
        assert out.merged_scores is None
        assert out.policy_digest == "7" * 64
        assert out.veto_reasons == []
        assert out.violations == []

    def test_projection_requires_no_jobs_or_results(self):
        # 不传任何 job/result/group 也能得到合法输出
        out = sm.project_not_requested(policy_digest="7" * 64)
        assert out.digest()
        assert out.merged_scores is None


class TestIndependentGroups:
    def test_dispatch_and_observation_groups_separate(self):
        disp = _single_group(
            rubrics=(_rubric(digest="a" * 64),),
            value=0.9,
        )
        obs_rubric = _rubric(
            target_type=c.SampleTargetType.OBSERVATION,
            dimensions=("hallucination_rate",),
            judge_role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
            rubric_id="observation-v1",
            digest="b" * 64,
        )
        obs_job = _job(obs_rubric)
        obs = _group(
            target_type=c.SampleTargetType.OBSERVATION,
            jobs=(obs_job,),
            results=(_result(obs_job, dims=("hallucination_rate",)),),
            score_values={obs_job.job_id: {"hallucination_rate": 0.5}},
            rubrics=(obs_rubric,),
        )
        out = _merge([disp, obs])
        assert set(out.merged_scores.keys()) == {"dispatch", "observation"}
        assert out.merged_scores["dispatch"].score == 0.9
        assert out.merged_scores["observation"].score == 0.5

    def test_no_fabricated_combined_total(self):
        disp = _single_group(value=0.9)
        obs_rubric = _rubric(
            target_type=c.SampleTargetType.OBSERVATION,
            dimensions=("hallucination_rate",),
            judge_role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
            rubric_id="observation-v1",
            digest="b" * 64,
        )
        obs_job = _job(obs_rubric)
        obs = _group(
            target_type=c.SampleTargetType.OBSERVATION,
            jobs=(obs_job,),
            results=(_result(obs_job, dims=("hallucination_rate",)),),
            score_values={obs_job.job_id: {"hallucination_rate": 0.5}},
            rubrics=(obs_rubric,),
        )
        out = _merge([disp, obs])
        assert "total" not in out.merged_scores
        assert not any("total" in g.reasons for g in out.merged_scores.values())


class TestStableOutput:
    def _build(self):
        rubric = _rubric(digest="a" * 64)
        job = _job(rubric)
        return _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(job,),
            results=(_result(job, dims=("d1", "d2")),),
            score_values={job.job_id: {"d1": 0.7, "d2": 0.3}},
            rubrics=(rubric,),
        )

    def test_same_inputs_same_output_and_digest(self):
        group = self._build()
        out1 = _merge([group])
        out2 = _merge([group])
        assert out1.model_dump() == out2.model_dump()
        assert out1.digest() == out2.digest()

    def test_job_order_does_not_affect_output(self):
        rubric = _rubric(digest="a" * 64)
        a, b = _job(rubric), _job(rubric)
        base = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(a, b),
            results=(_result(a), _result(b)),
            score_values={
                a.job_id: {"d1": 1.0, "d2": 1.0},
                b.job_id: {"d1": 0.0, "d2": 0.0},
            },
            rubrics=(rubric,),
        )
        rev = _group(
            target_type=c.SampleTargetType.DISPATCH,
            jobs=(b, a),
            results=(_result(b), _result(a)),
            score_values={
                a.job_id: {"d1": 1.0, "d2": 1.0},
                b.job_id: {"d1": 0.0, "d2": 0.0},
            },
            rubrics=(rubric,),
        )
        assert _merge([base]).model_dump() == _merge([rev]).model_dump()

    def test_output_digest_matches_canonical_json(self):
        group = self._build()
        out = _merge([group])
        assert out.digest() == c.sha256_hex(
            c.canonical_json(out.model_dump(mode="json")).encode("utf-8")
        )

    def test_no_timestamps_or_run_ids_in_output(self):
        group = self._build()
        dump = _merge([group]).model_dump()

        blob = c.canonical_json(dump)
        assert "created_at" not in blob
        assert "eval_run_id" not in blob
        assert "attempt_id" not in blob
        assert "2026-" not in blob


class TestMergeConfig:
    def test_merge_policy_digest_mismatch_raises(self):
        group = _single_group()
        policy = sm.MergePolicy()
        with pytest.raises(sm.MergeError, match="MergePolicy digest"):
            sm.merge_score_groups(
                [group],
                policy=policy,
                merge_policy_digest="0" * 64,
                manifest_digest="9" * 64,
            )

    def test_duplicate_target_group_raises(self):
        with pytest.raises(sm.MergeError, match="duplicate"):
            _merge([_single_group(), _single_group()])

    def test_default_policy_digest_is_stable(self):
        assert sm.MergePolicy().digest() == sm.MergePolicy().digest()
        assert (
            sm.MergePolicy(unknown_policy=sm.UnknownPolicy.EXCLUDED).digest()
            == sm.MergePolicy().digest()
        )
