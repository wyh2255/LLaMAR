"""P0 合同单测：身份/定位符、terminal 状态机、no-LLM not_requested、manifest/ledger 分离、
ArtifactRef/path/source-manifest 负例、score-job 绑定、role policy、freeze 后无 final 字段。

含 §6.3/§9 P0 langgraph 持久化 checkpointer 子进程 probe
（compile→invoke→interrupt→新进程/新 graph/同 db/同 thread_id resume）。

不调用任何真实 LLM/provider；不触碰 workflow/artifact/CLI。
"""

import itertools
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sar_orch.eval import contracts as c

# ─────────────────────────────────────────────────────────────────────────────
# 构造辅助
# ─────────────────────────────────────────────────────────────────────────────

def _ts() -> datetime:
    return datetime(2026, 8, 6, 0, 42, 10, tzinfo=UTC)


def artref(path="evidence/sample_1.json", sha=None) -> c.ArtifactRef:
    return c.ArtifactRef(
        path=path,
        sha256=sha if sha is not None else "a" * 64,
        bytes=12,
        media_type="application/json",
        producer="freeze",
    )


def make_role(role=c.JudgeRole.DISPATCH_SCORE_JUDGE, model_digest=None) -> c.RoleConfig:
    return c.RoleConfig(
        role=role,
        agent_id=role.value,
        prompt_ref=artref("snapshots/prompts/dispatch.md"),
        prompt_digest="b" * 64,
        model_profile_ref=artref("snapshots/model_profiles/score.json"),
        model_profile_digest=model_digest or "c" * 64,
        tool_schema_ref=artref("snapshots/tool_schemas/score.json"),
        tool_schema_digest="d" * 64,
    )


def make_rubric(
    judge_role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
    rubric_id="dispatch-v1",
    digest=None,
) -> c.RubricSpec:
    return c.RubricSpec(
        rubric_id=rubric_id,
        version="1.0.0",
        digest=digest or "e" * 64,
        target_type=c.SampleTargetType.DISPATCH,
        input_selector="dispatch_samples",
        dimensions=["pass_rate", "hallucination_rate"],
        prompt_template_ref=artref("snapshots/rubrics/dispatch-v1.yaml"),
        judge_role=judge_role,
        merge_group="dispatch",
        weight=1.0,
    )


def make_manifest(
    roles=None,
    rubrics=None,
    llm_judge_required=False,
    created_at=None,
    eval_run_id=None,
    attempt_id=None,
) -> c.InputManifest:
    return c.InputManifest(
        eval_run_id=eval_run_id or uuid4(),
        attempt_id=attempt_id or uuid4(),
        created_at=created_at or _ts(),
        subject=c.SubjectRef(
            source_run_ref=artref("subject/run_ref.json"),
            source_input_manifest_ref=artref("evidence/source_run_manifest.json"),
            source_digest="f" * 64,
            scene=1,
            agents=2,
            seed=42,
            code_commit="339fc3b",
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
            merge_policy_digest="7" * 64,
        ),
        roles=roles or [make_role()],
        rubrics=rubrics or [make_rubric()],
        command=c.CommandSpec(
            argv_without_secrets=["eval", "--no-llm-judge"],
            cwd="/tmp",
            env_allowlist=["PATH"],
        ),
    )


def make_binding(**overrides) -> c.ScoreJobBinding:
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "target_type": c.SampleTargetType.DISPATCH,
        "source_digest": "1" * 64,
        "evidence_digest": "2" * 64,
        "rubric_id": "dispatch-v1",
        "rubric_digest": "3" * 64,
        "role": c.JudgeRole.DISPATCH_SCORE_JUDGE,
        "prompt_digest": "4" * 64,
        "model_profile_digest": "5" * 64,
        "tool_schema_digest": "6" * 64,
    }
    data.update(overrides)
    return c.ScoreJobBinding(**data)


def make_job(**overrides) -> c.ScoreJob:
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "job_digest": "7" * 64,
        "sample_id": uuid4(),
        "target_type": c.SampleTargetType.DISPATCH,
        "source_digest": "1" * 64,
        "evidence_digest": "2" * 64,
        "rubric_id": "dispatch-v1",
        "rubric_digest": "3" * 64,
        "role": c.JudgeRole.DISPATCH_SCORE_JUDGE,
        "prompt_digest": "4" * 64,
        "model_profile_digest": "5" * 64,
        "tool_schema_digest": "6" * 64,
        "input_bundle_ref": artref("input_bundle/1.json"),
        "retry_policy_digest": "8" * 64,
        "manifest_digest": "9" * 64,
    }
    data.update(overrides)
    return c.ScoreJob(**data)


def make_result(binding=None, status=c.ScoreJobStatus.SUCCEEDED, **overrides) -> c.ScoreResult:
    data: dict[str, Any] = {"binding": binding or make_binding(), "status": status, "node_attempt": 1}
    data.update(overrides)
    return c.ScoreResult(**data)


# ─────────────────────────────────────────────────────────────────────────────
# 身份命名空间与 locator
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentityNamespace:
    def test_locator_parse_and_roundtrip(self):
        run_id, attempt_id = uuid4(), uuid4()
        loc = c.AttemptLocator.parse(f"{run_id}/{attempt_id}")
        assert loc.eval_run_id == run_id
        assert loc.attempt_id == attempt_id
        assert str(loc) == f"{run_id}/{attempt_id}"

    @pytest.mark.parametrize(
        "bad",
        [
            str(uuid4()),
            "",
            "x",
            "x/y",
            "abc/def",
            f"{uuid4()}/{uuid4()}/extra",
            f"{uuid4()}/",
            f"/{uuid4()}",
            "not-a-uuid/not-a-uuid",
        ],
    )
    def test_locator_rejects_bad_shapes(self, bad):
        with pytest.raises(c.ContractViolation):
            c.AttemptLocator.parse(bad)

    def test_locator_rejects_non_string(self):
        with pytest.raises(c.ContractViolation):
            c.AttemptLocator.parse(12345)

    def test_attempt_identity_locator_binds_to_series(self):
        run_id, attempt_id = uuid4(), uuid4()
        identity = c.EvalAttemptIdentity(
            eval_run_id=run_id, attempt_id=attempt_id, created_at=_ts()
        )
        loc = identity.locator()
        assert loc.eval_run_id == run_id
        assert loc.attempt_id == attempt_id

    def test_series_and_attempt_namespace_are_separate(self):
        series = c.EvalSeriesIdentity(
            eval_run_id=uuid4(),
            source_run_dir="runs/20260719_141217_s2_s42_a4",
            source_digest="a" * 64,
            created_at=_ts(),
        )
        attempt = c.EvalAttemptIdentity(
            eval_run_id=series.eval_run_id,
            attempt_id=uuid4(),
            created_at=_ts(),
        )
        assert attempt.locator().eval_run_id == series.eval_run_id
        assert attempt.attempt_id != series.eval_run_id

    def test_identity_schema_version_controlled(self):
        with pytest.raises(ValidationError):
            c.EvalAttemptIdentity(
                schema_version=2,
                eval_run_id=uuid4(),
                attempt_id=uuid4(),
                created_at=_ts(),
            )


# ─────────────────────────────────────────────────────────────────────────────
# workflow terminal 状态机
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkflowTransitions:
    FORWARD: ClassVar[tuple[tuple[c.WorkflowStatus, c.WorkflowStatus], ...]] = (
        (c.WorkflowStatus.CREATED, c.WorkflowStatus.INPUT_VALIDATED),
        (c.WorkflowStatus.INPUT_VALIDATED, c.WorkflowStatus.ADMITTED),
        (c.WorkflowStatus.ADMITTED, c.WorkflowStatus.MANIFEST_FROZEN),
        (c.WorkflowStatus.MANIFEST_FROZEN, c.WorkflowStatus.EVIDENCE_MATERIALIZED),
        (c.WorkflowStatus.EVIDENCE_MATERIALIZED, c.WorkflowStatus.DETERMINISTIC_GRADED),
        (c.WorkflowStatus.DETERMINISTIC_GRADED, c.WorkflowStatus.SCORE_JOBS_READY),
        (c.WorkflowStatus.SCORE_JOBS_READY, c.WorkflowStatus.SCORING),
        (c.WorkflowStatus.SCORING, c.WorkflowStatus.SCORES_JOINED),
        (c.WorkflowStatus.SCORES_JOINED, c.WorkflowStatus.SCORES_MERGED),
        (c.WorkflowStatus.SCORES_MERGED, c.WorkflowStatus.REPORT_AUTHORED),
        (c.WorkflowStatus.REPORT_AUTHORED, c.WorkflowStatus.RECOMMENDATIONS_AUTHORED),
        (c.WorkflowStatus.RECOMMENDATIONS_AUTHORED, c.WorkflowStatus.RENDERED),
        (c.WorkflowStatus.RENDERED, c.WorkflowStatus.VERIFIED),
        (c.WorkflowStatus.VERIFIED, c.WorkflowStatus.SUCCEEDED),
    )

    def test_forward_chain_is_valid(self):
        for current, next_ in self.FORWARD:
            assert c.workflow_can_transition(current, next_)
            c.validate_workflow_transition(current, next_)

    @pytest.mark.parametrize(
        "terminal",
        [
            c.WorkflowStatus.SUCCEEDED,
            c.WorkflowStatus.PARTIAL,
            c.WorkflowStatus.FAILED,
            c.WorkflowStatus.CANCELLED,
        ],
    )
    def test_terminal_never_regresses(self, terminal):
        assert c.is_workflow_terminal(terminal)
        for other in c.WorkflowStatus:
            assert not c.workflow_can_transition(terminal, other)

    def test_cannot_skip_phases(self):
        assert not c.workflow_can_transition(
            c.WorkflowStatus.DETERMINISTIC_GRADED, c.WorkflowStatus.SCORING
        )
        assert not c.workflow_can_transition(
            c.WorkflowStatus.JUDGE_SKIPPED, c.WorkflowStatus.VERIFIED
        )
        assert not c.workflow_can_transition(
            c.WorkflowStatus.SCORES_JOINED, c.WorkflowStatus.REPORT_AUTHORED
        )

    def test_not_requested_branch_is_valid(self):
        chain = [
            c.WorkflowStatus.DETERMINISTIC_GRADED,
            c.WorkflowStatus.JUDGE_SKIPPED,
            c.WorkflowStatus.RENDERED,
            c.WorkflowStatus.VERIFIED,
            c.WorkflowStatus.SUCCEEDED,
        ]
        for current, next_ in itertools.pairwise(chain):
            assert c.workflow_can_transition(current, next_)

    def test_fail_closed_from_early_states(self):
        assert c.workflow_can_transition(c.WorkflowStatus.CREATED, c.WorkflowStatus.FAILED)
        assert c.workflow_can_transition(c.WorkflowStatus.ADMITTED, c.WorkflowStatus.CANCELLED)

    def test_validate_workflow_transition_raises(self):
        with pytest.raises(c.ContractViolation):
            c.validate_workflow_transition(
                c.WorkflowStatus.SUCCEEDED, c.WorkflowStatus.FAILED
            )


class TestScoreJobTransitions:
    def test_pending_to_claimed_then_terminal(self):
        assert c.job_can_transition(c.ScoreJobStatus.PENDING, c.ScoreJobStatus.CLAIMED)
        assert c.job_can_transition(c.ScoreJobStatus.CLAIMED, c.ScoreJobStatus.SUCCEEDED)
        assert c.job_can_transition(c.ScoreJobStatus.CLAIMED, c.ScoreJobStatus.UNKNOWN)
        assert c.job_can_transition(c.ScoreJobStatus.PENDING, c.ScoreJobStatus.NOT_RUN_BUDGET)

    def test_job_requires_claim_before_terminal(self):
        assert not c.job_can_transition(c.ScoreJobStatus.PENDING, c.ScoreJobStatus.SUCCEEDED)
        assert not c.job_can_transition(c.ScoreJobStatus.PENDING, c.ScoreJobStatus.TIMED_OUT)
        assert not c.job_can_transition(c.ScoreJobStatus.CLAIMED, c.ScoreJobStatus.PENDING)
        assert not c.job_can_transition(c.ScoreJobStatus.CLAIMED, c.ScoreJobStatus.NOT_RUN_BUDGET)

    def test_terminal_job_never_regresses(self):
        for terminal in sorted(c.SCORE_JOB_TERMINAL_STATUSES, key=lambda s: s.value):
            assert c.is_job_terminal(terminal)
            for other in c.ScoreJobStatus:
                assert not c.job_can_transition(terminal, other)

    def test_validate_job_transition_raises(self):
        with pytest.raises(c.ContractViolation):
            c.validate_job_transition(c.ScoreJobStatus.PENDING, c.ScoreJobStatus.FAILED)

    def test_late_response_only_after_terminal(self):
        with pytest.raises(c.ContractViolation):
            c.validate_late_response(c.ScoreJobStatus.PENDING)
        c.validate_late_response(c.ScoreJobStatus.SUCCEEDED)
        c.validate_late_response(c.ScoreJobStatus.NOT_RUN_BUDGET)


# ─────────────────────────────────────────────────────────────────────────────
# no-LLM / judge 状态语义
# ─────────────────────────────────────────────────────────────────────────────

class TestJudgeExecutionStatus:
    def test_no_llm_not_requested_semantics(self):
        c.validate_judge_execution_status(
            c.JudgeExecutionStatus.NOT_REQUESTED, requested_path=False, job_count=0
        )

    def test_not_requested_must_never_create_jobs(self):
        with pytest.raises(c.ContractViolation):
            c.validate_judge_execution_status(
                c.JudgeExecutionStatus.NOT_REQUESTED, requested_path=False, job_count=1
            )

    def test_requested_path_cannot_be_not_requested(self):
        with pytest.raises(c.ContractViolation):
            c.validate_judge_execution_status(
                c.JudgeExecutionStatus.NOT_REQUESTED, requested_path=True
            )

    def test_budget_exhausted_requires_not_run_budget_job(self):
        with pytest.raises(c.ContractViolation):
            c.validate_judge_execution_status(
                c.JudgeExecutionStatus.BUDGET_EXHAUSTED,
                requested_path=True,
                not_run_budget_count=0,
            )
        c.validate_judge_execution_status(
            c.JudgeExecutionStatus.BUDGET_EXHAUSTED,
            requested_path=True,
            not_run_budget_count=1,
        )

    def test_partial_requires_requested_missing(self):
        c.validate_judge_execution_status(
            c.JudgeExecutionStatus.PARTIAL, requested_path=True, missing_required=True
        )
        with pytest.raises(c.ContractViolation):
            c.validate_judge_execution_status(
                c.JudgeExecutionStatus.PARTIAL, requested_path=False, missing_required=True
            )
        with pytest.raises(c.ContractViolation):
            c.validate_judge_execution_status(
                c.JudgeExecutionStatus.PARTIAL, requested_path=True, missing_required=False
            )

    def test_policy_derives_initial_status(self):
        assert (
            c.PolicySpec(
                llm_judge_required=False,
                audit_level=c.AuditLevel.STANDARD,
                retry=0,
                timeout_s=60,
                concurrency=1,
                retention_days=30,
                merge_policy_digest="a" * 64,
            ).judge_execution_status
            is c.JudgeExecutionStatus.NOT_REQUESTED
        )
        assert (
            c.PolicySpec(
                llm_judge_required=True,
                audit_level=c.AuditLevel.STANDARD,
                retry=0,
                timeout_s=60,
                concurrency=1,
                retention_days=30,
                merge_policy_digest="a" * 64,
            ).judge_execution_status
            is c.JudgeExecutionStatus.REQUESTED
        )

    def test_terminal_judge_consistency(self):
        c.validate_terminal_judge_consistency(
            terminal_status=c.WorkflowStatus.SUCCEEDED,
            judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
        )
        with pytest.raises(c.ContractViolation):
            c.validate_terminal_judge_consistency(
                terminal_status=c.WorkflowStatus.PARTIAL,
                judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
            )
        with pytest.raises(c.ContractViolation):
            c.validate_terminal_judge_consistency(
                terminal_status=c.WorkflowStatus.SUCCEEDED,
                judge_execution_status=c.JudgeExecutionStatus.BUDGET_EXHAUSTED,
            )
        with pytest.raises(c.ContractViolation):
            c.validate_terminal_judge_consistency(
                terminal_status=c.WorkflowStatus.CREATED,
                judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
            )


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactRef / path / source manifest 负例
# ─────────────────────────────────────────────────────────────────────────────

class TestArtifactRef:
    def test_valid_ref(self):
        ref = artref()
        assert ref.path == "evidence/sample_1.json"
        assert ref.sha256 == "a" * 64
        assert ref.bytes == 12

    @pytest.mark.parametrize(
        "bad",
        [
            "/etc/passwd",
            "/abs/relative",
            "\\server\\share",
            "C:\\x",
            "c:/windows",
            "~/x",
            "..",
            "../escape",
            "a/../../b",
            "./a",
            "a/./b",
            "a//b",
            "a/",
            "",
            ".",
        ],
    )
    def test_unsafe_paths_rejected(self, bad):
        with pytest.raises(ValidationError):
            artref(path=bad)

    @pytest.mark.parametrize(
        "bad_digest",
        [
            "a" * 63,
            "A" * 64,
            "g" + "a" * 63,
            "a" * 65,
            "",
            "not-a-digest",
        ],
    )
    def test_invalid_digest_rejected(self, bad_digest):
        with pytest.raises(ValidationError):
            artref(sha=bad_digest)

    def test_negative_size_rejected(self):
        with pytest.raises(ValidationError):
            c.ArtifactRef(
                path="a.json",
                sha256="a" * 64,
                bytes=-1,
                media_type="application/json",
                producer="freeze",
            )

    def test_empty_producer_and_media_type_rejected(self):
        with pytest.raises(ValidationError):
            c.ArtifactRef(
                path="a.json", sha256="a" * 64, bytes=1, media_type="", producer="x"
            )

    def test_ref_never_holds_content(self):
        ref = artref()
        assert not hasattr(ref, "content")
        assert ref.bytes == 12


class TestSourceManifest:
    def test_valid_snapshot(self):
        source = c.SourceInputManifest(
            eval_run_id=uuid4(),
            source_run_dir="runs/20260719_141217_s2_s42_a4",
            files={
                "metadata.json": "a" * 64,
                "trajectory.csv": "b" * 64,
                "agent_interactions.csv": "c" * 64,
            },
        )
        assert len(source.files) == 3

    @pytest.mark.parametrize("bad_key", ["eval_attempts/x.json", "eval_workspace/y.json"])
    def test_reserved_output_dirs_never_snapshotted(self, bad_key):
        with pytest.raises(ValidationError):
            c.SourceInputManifest(
                eval_run_id=uuid4(),
                source_run_dir="runs/x",
                files={bad_key: "a" * 64},
            )

    def test_unsafe_source_key_rejected(self):
        with pytest.raises(ValidationError):
            c.SourceInputManifest(
                eval_run_id=uuid4(),
                source_run_dir="runs/x",
                files={"../outside.csv": "a" * 64},
            )

    def test_bad_source_file_digest_rejected(self):
        with pytest.raises(ValidationError):
            c.SourceInputManifest(
                eval_run_id=uuid4(),
                source_run_dir="runs/x",
                files={"trajectory.csv": "bogus"},
            )


# ─────────────────────────────────────────────────────────────────────────────
# input manifest / final ledger 分离与 freeze
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestLedgerSeparation:
    @pytest.mark.parametrize(
        "extra_field",
        [
            {"terminal_status": c.WorkflowStatus.SUCCEEDED},
            {"finalized_at": _ts()},
            {"report_digest": "a" * 64},
            {"pointer_revision": 3},
            {"failure_refs": []},
        ],
    )
    def test_manifest_rejects_terminal_and_finalization_fields(self, extra_field):
        payload = make_manifest().model_dump(mode="json")
        payload.update(extra_field)
        with pytest.raises(ValidationError):
            c.InputManifest.model_validate(payload)

    def test_manifest_is_structurally_immutable(self):
        manifest = make_manifest()
        with pytest.raises(ValidationError):
            manifest.created_at = _ts()

    def test_freeze_payload_has_no_final_fields(self):
        frozen = make_manifest().freeze()
        payload = frozen.payload()
        for forbidden in ("terminal_status", "finalized_at", "report_digest", "pointer_revision"):
            assert forbidden not in payload
        assert "terminal_status" not in frozen.model_dump(mode="json")

    def test_freeze_digest_is_stable_and_canonical(self):
        m1 = make_manifest()
        m2 = make_manifest(eval_run_id=m1.eval_run_id, attempt_id=m1.attempt_id)
        assert m1.freeze().digest == m2.freeze().digest
        frozen = m1.freeze()
        assert frozen.digest == frozen.digest
        assert len(frozen.digest) == 64
        assert int(frozen.digest, 16) >= 0

    def test_freeze_digest_changes_when_manifest_changes(self):
        m1 = make_manifest(llm_judge_required=False)
        m2 = make_manifest(llm_judge_required=True)
        assert m1.freeze().digest != m2.freeze().digest

    def test_frozen_digest_cannot_be_forged(self):
        with pytest.raises(ValidationError):
            c.FrozenInputManifest(manifest=make_manifest(), digest="f" * 64)

    def test_frozen_manifest_immutable(self):
        frozen = make_manifest().freeze()
        with pytest.raises(ValidationError):
            frozen.manifest = make_manifest()

    def test_ledger_requires_terminal_status(self):
        with pytest.raises(ValidationError):
            c.FinalLedger(
                input_manifest_digest="a" * 64,
                terminal_status=c.WorkflowStatus.ADMITTED,
                finalized_at=_ts(),
                artifact_refs={"reports/eval_report.json": artref()},
                report_digest="b" * 64,
                judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
            )

    def test_ledger_no_llm_success_is_consistent(self):
        ledger = c.FinalLedger(
            input_manifest_digest="a" * 64,
            terminal_status=c.WorkflowStatus.SUCCEEDED,
            finalized_at=_ts(),
            artifact_refs={"reports/eval_report.json": artref()},
            report_digest="b" * 64,
            judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
        )
        assert c.is_workflow_terminal(ledger.terminal_status)
        assert len(ledger.digest()) == 64

    def test_ledger_partial_not_requested_rejected(self):
        with pytest.raises(ValidationError):
            c.FinalLedger(
                input_manifest_digest="a" * 64,
                terminal_status=c.WorkflowStatus.PARTIAL,
                finalized_at=_ts(),
                artifact_refs={},
                report_digest="b" * 64,
                judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
            )

    def test_ledger_rejects_forged_digests(self):
        with pytest.raises(ValidationError):
            c.FinalLedger(
                input_manifest_digest="xyz",
                terminal_status=c.WorkflowStatus.SUCCEEDED,
                finalized_at=_ts(),
                artifact_refs={},
                report_digest="b" * 64,
                judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
            )

    def test_selected_attempt_pointer_contract(self):
        pointer = c.SelectedAttempt(
            eval_run_id=uuid4(),
            attempt_id=uuid4(),
            input_manifest_digest="a" * 64,
            final_ledger_digest="b" * 64,
            report_digest="c" * 64,
            revision=2,
        )
        assert pointer.revision == 2
        with pytest.raises(ValidationError):
            c.SelectedAttempt(
                eval_run_id=uuid4(),
                attempt_id=uuid4(),
                input_manifest_digest="a" * 64,
                final_ledger_digest="b" * 64,
                report_digest="c" * 64,
                revision=-1,
            )

    def test_manifest_rubric_role_must_be_configured(self):
        with pytest.raises(ValidationError):
            make_manifest(rubrics=[make_rubric(judge_role=c.JudgeRole.REPORT_JUDGE)])

    def test_canonical_json_is_sorted_and_compact(self):
        blob = c.canonical_json({"b": 2, "a": {"d": 4, "c": 3}})
        assert blob == '{"a":{"c":3,"d":4},"b":2}'
        assert c.sha256_hex(b"x") == c.sha256_hex(b"x")
        assert c.sha256_hex(b"x") != c.sha256_hex(b"y")


# ─────────────────────────────────────────────────────────────────────────────
# score-job binding
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreJobBinding:
    def test_matching_job_and_result_bind(self):
        job = make_job()
        result = make_result(binding=job.binding())
        assert result.verify_binding(job) == []
        c.check_job_result_binding(job, result)

    @pytest.mark.parametrize(
        "override_field",
        [
            "source_digest",
            "evidence_digest",
            "rubric_digest",
            "prompt_digest",
            "model_profile_digest",
            "tool_schema_digest",
        ],
    )
    def test_digest_binding_mismatch_fails(self, override_field):
        job = make_job()
        mismatched = job.binding().model_dump()
        mismatched[override_field] = "0" * 64
        result = make_result(binding=c.ScoreJobBinding(**mismatched))
        assert result.verify_binding(job)
        with pytest.raises(c.ContractViolation):
            c.check_job_result_binding(job, result)

    def test_rubric_id_and_role_binding_mismatch_fails(self):
        job = make_job()
        result = make_result(
            binding=make_binding(
                job_id=job.job_id,
                rubric_id="observation-v1",
                role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
                source_digest="1" * 64,
                evidence_digest="2" * 64,
                rubric_digest="3" * 64,
                prompt_digest="4" * 64,
                model_profile_digest="5" * 64,
                tool_schema_digest="6" * 64,
            )
        )
        assert result.verify_binding(job)

    def test_job_id_mismatch_fails(self):
        job = make_job()
        result = make_result(binding=make_binding())
        assert result.verify_binding(job)

    def test_binding_roundtrip(self):
        job = make_job()
        binding = job.binding()
        assert binding.job_id == job.job_id
        assert binding.target_type == job.target_type
        assert binding.rubric_digest == job.rubric_digest

    def test_result_status_must_be_terminal(self):
        with pytest.raises(ValidationError):
            make_result(status=c.ScoreJobStatus.PENDING)
        late = make_result(status=c.ScoreJobStatus.LATE_IGNORED)
        assert late.status is c.ScoreJobStatus.LATE_IGNORED

    def test_job_binding_rejects_false_identity(self):
        with pytest.raises(ValidationError):
            make_job(job_digest="bogus")


# ─────────────────────────────────────────────────────────────────────────────
# role / harness policy
# ─────────────────────────────────────────────────────────────────────────────

class TestRoleHarnessPolicy:
    def test_profile_must_exclude_default_builtins(self):
        with pytest.raises(ValidationError):
            c.HarnessProfile(
                role=c.JudgeRole.REPORT_JUDGE,
                agent_id="report",
                backend="openai",
                excluded_default_tools=["write_file", "edit_file"],
            )

    def test_profile_forbids_generic_subagent(self):
        with pytest.raises(ValidationError):
            c.HarnessProfile(
                role=c.JudgeRole.REPORT_JUDGE,
                agent_id="report",
                backend="openai",
                allow_subagent=True,
            )

    def test_profile_requires_empty_history(self):
        with pytest.raises(ValidationError):
            c.HarnessProfile(
                role=c.JudgeRole.REPORT_JUDGE,
                agent_id="report",
                backend="openai",
                empty_history=False,
            )

    def test_profile_forbids_canonical_writers(self):
        with pytest.raises(ValidationError):
            c.HarnessProfile(
                role=c.JudgeRole.REPORT_JUDGE,
                agent_id="report",
                backend="openai",
                allowlist_tools=["write_score", "evidence_reader"],
            )

    def test_default_policy_allows_shared_model_with_warning(self):
        policy = c.RoleHarnessPolicy()
        assert policy.allow_shared_model is True
        assert policy.require_distinct_role_models is False
        warning = policy.shared_model_warning(c.JudgeRole.REPORT_JUDGE)
        assert warning and "allow_shared_model=true" in warning

    def test_require_distinct_contradicts_allow_shared(self):
        with pytest.raises(ValidationError):
            c.RoleHarnessPolicy(allow_shared_model=True, require_distinct_role_models=True)

    def test_distinct_models_enforced_when_required(self):
        strict = c.RoleHarnessPolicy(allow_shared_model=False, require_distinct_role_models=True)
        roles = [make_role(model_digest="c" * 64), make_role(c.JudgeRole.OBSERVATION_SCORE_JUDGE, model_digest="c" * 64)]
        assert strict.check_roles(roles)
        distinct = [make_role(model_digest="c" * 64), make_role(c.JudgeRole.OBSERVATION_SCORE_JUDGE, model_digest="d" * 64)]
        assert strict.check_roles(distinct) == []

    def test_shared_model_default_does_not_flag(self):
        shared = c.RoleHarnessPolicy()
        roles = [make_role(model_digest="c" * 64), make_role(c.JudgeRole.OBSERVATION_SCORE_JUDGE, model_digest="c" * 64)]
        assert shared.check_roles(roles) == []


# ─────────────────────────────────────────────────────────────────────────────
# 版本化 Draft 与 evidence refs
# ─────────────────────────────────────────────────────────────────────────────

class TestDrafts:
    def _score_draft(self, **overrides):
        data: dict[str, Any] = {
            "role": c.JudgeRole.DISPATCH_SCORE_JUDGE,
            "invocation_id": uuid4(),
            "dimensions": {"pass_rate": 0.8},
            "model_used": "deepseek-v4-flash",
        }
        data.update(overrides)
        return c.ScoreDraft(**data)

    def test_valid_score_draft(self):
        draft = self._score_draft()
        assert draft.schema_version == 1

    def test_score_draft_role_restricted(self):
        with pytest.raises(ValidationError):
            self._score_draft(role=c.JudgeRole.REPORT_JUDGE)

    @pytest.mark.parametrize("bad_score", [1.5, -0.1, math.nan, math.inf])
    def test_score_draft_rejects_out_of_scale(self, bad_score):
        with pytest.raises(ValidationError):
            self._score_draft(dimensions={"pass_rate": bad_score})

    def test_score_draft_requires_dimensions_or_unknown(self):
        with pytest.raises(ValidationError):
            self._score_draft(dimensions={}, unknown_reason=None)
        abstain = self._score_draft(dimensions={}, unknown_reason="evidence unavailable")
        assert abstain.unknown_reason == "evidence unavailable"

    def test_score_draft_versioned(self):
        with pytest.raises(ValidationError):
            self._score_draft(schema_version=2)

    def test_evidence_ref_digest_must_match_artifact(self):
        ref = artref(sha="a" * 64)
        with pytest.raises(ValidationError):
            c.EvidenceRef(ref=ref, claim_type=c.ClaimType.OBSERVATION, digest="b" * 64)
        evidence = c.EvidenceRef(ref=ref, claim_type=c.ClaimType.OBSERVATION, digest="a" * 64)
        assert evidence.digest == ref.sha256

    def test_report_narrative_draft(self):
        draft = c.ReportNarrativeDraft(
            role=c.JudgeRole.REPORT_JUDGE,
            invocation_id=uuid4(),
            narrative="coverage reached 0.8",
            model_used="deepseek-v4-flash",
        )
        assert draft.narrative
        with pytest.raises(ValidationError):
            c.ReportNarrativeDraft(
                role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
                invocation_id=uuid4(),
                narrative="x",
                model_used="m",
            )
        with pytest.raises(ValidationError):
            c.ReportNarrativeDraft(
                role=c.JudgeRole.REPORT_JUDGE,
                invocation_id=uuid4(),
                narrative="   ",
                model_used="m",
            )

    def test_recommendation_fallback_requires_reason(self):
        with pytest.raises(ValidationError):
            c.RecommendationDraft(
                role=c.JudgeRole.RECOMMENDATION_JUDGE,
                invocation_id=uuid4(),
                source=c.RecommendationSource.DETERMINISTIC_FALLBACK,
            )
        ok = c.RecommendationDraft(
            role=c.JudgeRole.RECOMMENDATION_JUDGE,
            invocation_id=uuid4(),
            source=c.RecommendationSource.DETERMINISTIC_FALLBACK,
            fallback_reason="rule template",
        )
        assert ok.fallback_reason == "rule template"

    def test_judge_recommendations_cannot_carry_fallback_reason(self):
        with pytest.raises(ValidationError):
            c.RecommendationDraft(
                role=c.JudgeRole.RECOMMENDATION_JUDGE,
                invocation_id=uuid4(),
                recommendations=[c.RecommendationItem(text="add rubric")],
                source=c.RecommendationSource.RECOMMENDATION_JUDGE,
                fallback_reason="nope",
            )

    def test_recommendation_item_requires_text(self):
        with pytest.raises(ValidationError):
            c.RecommendationItem(text="  ")

    def test_usage_unknown_is_none_not_zero(self):
        usage = c.UsageSnapshot()
        assert usage.total_tokens is None
        with pytest.raises(ValidationError):
            c.UsageSnapshot(total_tokens=-1, cost=-0.1)

    def test_sample_step_and_ordinal(self):
        sample = c.Sample(
            sample_id=uuid4(),
            target_type=c.SampleTargetType.DISPATCH,
            step=5,
            source_digest="a" * 64,
            evidence_digest="b" * 64,
            selection_reason="failure step priority",
            ordinal=0,
        )
        assert sample.step == 5
        with pytest.raises(ValidationError):
            c.Sample(
                sample_id=uuid4(),
                target_type=c.SampleTargetType.DISPATCH,
                step=0,
                source_digest="a" * 64,
                evidence_digest="b" * 64,
                selection_reason="x",
                ordinal=-1,
            )


# ─────────────────────────────────────────────────────────────────────────────
# ScoreDraft 维度级 abstain（judge 家族化设计 §2.3）
# ─────────────────────────────────────────────────────────────────────────────


class TestScoreDraftDimensionUnknownReasons:
    def _draft(self, **overrides):
        data: dict[str, Any] = {
            "role": c.JudgeRole.DISPATCH_SCORE_JUDGE,
            "invocation_id": uuid4(),
            "dimensions": {"pass_rate": 0.8},
            "model_used": "deepseek-v4-flash",
        }
        data.update(overrides)
        return c.ScoreDraft(**data)

    def test_valid_partial_dimensions_with_reasons(self):
        draft = self._draft(
            dimensions={"dispatch_completeness": 0.9},
            dimension_unknown_reasons={
                "dispatch_feasibility": "no dispatch to judge this step"
            },
        )
        assert draft.dimension_unknown_reasons == {
            "dispatch_feasibility": "no dispatch to judge this step"
        }
        assert draft.dimensions == {"dispatch_completeness": 0.9}

    def test_overlap_between_dimensions_and_reasons_rejected(self):
        with pytest.raises(ValidationError, match="must not overlap"):
            self._draft(
                dimensions={"dispatch_completeness": 0.9},
                dimension_unknown_reasons={
                    "dispatch_completeness": "contradictory reason"
                },
            )

    def test_empty_reason_value_rejected(self):
        for bad in ("", "   ", "\n\t"):
            with pytest.raises(ValidationError, match="non-empty"):
                self._draft(
                    dimensions={},
                    dimension_unknown_reasons={"dim_a": bad},
                    unknown_reason="overall abstain",
                )

    def test_legacy_construction_without_reasons_still_valid(self):
        # 向后兼容：默认 {}，既有构造不破坏。
        draft = self._draft(dimensions={"pass_rate": 0.8})
        assert draft.dimension_unknown_reasons == {}
        abstain = self._draft(dimensions={}, unknown_reason="evidence unavailable")
        assert abstain.dimension_unknown_reasons == {}
        assert abstain.unknown_reason == "evidence unavailable"

    def test_overall_abstain_with_per_dimension_reasons(self):
        # 整体 abstain 语义保留：全部维度缺席 + 各维度原因（可无 unknown_reason）。
        draft = self._draft(
            dimensions={},
            dimension_unknown_reasons={
                "dispatch_completeness": "no worker state available",
                "dispatch_feasibility": "no dispatch to judge",
                "dispatch_novelty": "no dispatch history",
                "dispatch_efficiency": "no budget information",
            },
        )
        assert draft.dimensions == {}
        assert len(draft.dimension_unknown_reasons) == 4

    def test_fully_empty_draft_still_rejected(self):
        # 无分数、无维度原因、无整体 abstain 原因 → 拒绝（与旧语义一致）。
        with pytest.raises(ValidationError, match="dimensions or an explicit"):
            self._draft(dimensions={}, unknown_reason=None)

    def test_json_round_trip_preserves_reasons(self):
        draft = self._draft(
            dimensions={"pass_rate": 0.5},
            dimension_unknown_reasons={"other_dim": "missing evidence"},
        )
        payload = draft.model_dump(mode="json")
        assert payload["dimension_unknown_reasons"] == {"other_dim": "missing evidence"}
        restored = c.ScoreDraft.model_validate(payload)
        assert restored == draft
        assert restored.dimension_unknown_reasons == draft.dimension_unknown_reasons

    def test_reason_score_scale_still_enforced(self):
        # dimensions 分数 [0,1] 既有校验保持。
        with pytest.raises(ValidationError, match="normalized"):
            self._draft(dimensions={"pass_rate": 1.5},
                        dimension_unknown_reasons={"x": "y"})


# ─────────────────────────────────────────────────────────────────────────────
# P0 langgraph 持久化 checkpointer 子进程 probe（§6.3 / §9 P0）
# ─────────────────────────────────────────────────────────────────────────────

class TestLangGraphPersistentCheckpointProbe:
    """compile→invoke→interrupt→进程退出→新进程/新 graph/同 db/同 thread_id→resume。

    使用 langgraph-checkpoint-sqlite（SqliteSaver）做真实持久化，无模型调用。
    """

    WORKER = """\
import json
import sys
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def ask_node(state):
    answer = interrupt("awaiting judge classification")
    return {"result": answer}


def build_app(checkpointer):
    graph = StateGraph(dict)
    graph.add_node("ask", ask_node)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=checkpointer)


def main() -> int:
    mode, db, thread_id = sys.argv[1], sys.argv[2], sys.argv[3]
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(db) as cp:
        app = build_app(cp)
        if mode == "interrupt":
            result = app.invoke({"input": "episode-1"}, config=config)
            interrupted = "__interrupt__" in result
            print("PHASE_A_OUT", json.dumps(result, default=str), "interrupted=" + str(interrupted), flush=True)
            return 0 if interrupted else 3
        if mode == "resume":
            result = app.invoke(Command(resume="critical"), config=config)
            print("PHASE_B_OUT", json.dumps(result, default=str), flush=True)
            return 0
    print("unknown mode", mode, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
"""

    def _spawn(self, tmp_path, *args):
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        return subprocess.run(
            [sys.executable, str(tmp_path / "probe_worker.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
            check=False,
        )

    def test_sqlite_resume_across_processes(self, tmp_path):
        pytest.importorskip("langgraph.checkpoint.sqlite")
        (tmp_path / "probe_worker.py").write_text(self.WORKER, encoding="utf-8")
        db = str(tmp_path / "state.sqlite")
        thread_id = "probe-thread-1"

        phase_a = self._spawn(tmp_path, "interrupt", db, thread_id)
        assert phase_a.returncode == 0, phase_a.stderr
        assert "interrupted=True" in phase_a.stdout
        assert (tmp_path / "state.sqlite").exists()

        phase_b = self._spawn(tmp_path, "resume", db, thread_id)
        assert phase_b.returncode == 0, phase_b.stderr
        payload = json.loads(phase_b.stdout.split("PHASE_B_OUT ", 1)[1])
        assert payload["result"] == "critical"
        assert phase_b.stdout != phase_a.stdout
