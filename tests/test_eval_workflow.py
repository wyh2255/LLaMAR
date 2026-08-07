"""P3 StateGraph 确定性控制面单测（设计 §2.2-2.3、§6、§9 P3）。

覆盖：canonical map reducer 冲突拒绝、no-LLM 零 runner 构造 + SUCCEEDED/
not_requested、确定性生命周期、Send fan-out、semaphore 并发上限、per-job
timeout/retry、merge 分组与多 rubric fan-out、invalid draft → FAILED、
cancel 停止新 claim + CANCELLED ledger 一次、late_ignored。

只用 fake runner（`sar_orch.eval.agent.runner`）；不构造任何 DeepAgent/chat 模型。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

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


def _rubric(target_type: c.SampleTargetType, digest=None) -> c.RubricSpec:
    dispatch = target_type is c.SampleTargetType.DISPATCH
    return c.RubricSpec(
        rubric_id="dispatch-v1" if dispatch else "observation-v1",
        version="1.0.0",
        digest=digest or (("3" * 64) if dispatch else ("4" * 64)),
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


def _manifest(
    llm_judge_required: bool = True,
    *,
    retry: int = 2,
    timeout_s: int = 60,
    concurrency: int = 2,
    max_score_jobs: int | None = None,
    rubrics=None,
) -> c.FrozenInputManifest:
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
            retry=retry,
            timeout_s=timeout_s,
            concurrency=concurrency,
            max_score_jobs=max_score_jobs,
            retention_days=30,
            allow_shared_model=True,
            merge_policy_digest=sm.MergePolicy().digest(),
        ),
        roles=[
            _role(c.JudgeRole.DISPATCH_SCORE_JUDGE),
            _role(c.JudgeRole.OBSERVATION_SCORE_JUDGE),
        ],
        rubrics=list(rubrics)
        if rubrics
        else [
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
    *,
    llm_judge_required: bool = True,
    runner_factory=None,
    episode=None,
    timeout_override=None,
    grader_fn=None,
    manifest=None,
) -> tuple[w.EvalRuntime, a.ArtifactStore]:
    manifest = manifest or _manifest(llm_judge_required=llm_judge_required)
    store = a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))
    rt = w.EvalRuntime(
        manifest,
        store,
        store.audit_journal(),
        runner_factory=runner_factory,
        episode=episode or _episode(),
        grader_fn=grader_fn or (lambda: ([], [])),
        timeout_s=timeout_override,
    )
    return rt, store


def _make_factory(manifest, configs=None, sleep=0.02):
    return r.make_fake_runner_factory(manifest, configs, sleep=sleep)


def _job_count(store: a.ArtifactStore) -> int:
    return len(w._list_jobs(store))


# ─────────────────────────────────────────────────────────────────────────────
# reducers（§2.2）
# ─────────────────────────────────────────────────────────────────────────────


class TestReducers:
    def test_merge_refs_merges_distinct_keys(self):
        a_ref = _artref("score_results/j1.json", "a" * 64)
        b_ref = _artref("score_results/j2.json", "b" * 64)
        merged = w.merge_refs({}, {"j1": a_ref})
        merged = w.merge_refs(merged, {"j2": b_ref})
        assert set(merged) == {"j1", "j2"}
        assert merged["j1"] is a_ref

    def test_merge_refs_rejects_conflict(self):
        first = _artref("score_results/j1.json", "a" * 64)
        second = _artref("score_results/j1.json", "b" * 64)
        merged = w.merge_refs({}, {"j1": first})
        with pytest.raises(c.ContractViolation, match="conflicting artifact ref"):
            w.merge_refs(merged, {"j1": second})

    def test_merge_refs_accepts_same_digest_idempotent(self):
        ref = _artref("score_results/j1.json", "a" * 64)
        merged = w.merge_refs({}, {"j1": ref})
        assert w.merge_refs(merged, {"j1": ref}) == merged

    def test_append_failures_sorts_and_keeps_existing(self):
        f1 = c.FailureRef(kind="a", reason="1")
        f2 = c.FailureRef(kind="b", reason="2")
        acc = w.append_failures([], f2)
        acc = w.append_failures(acc, f1)
        assert [f.kind for f in acc] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# no-LLM 零构造 + SUCCEEDED/not_requested（§1.2-12 / §2.3 / R3）
# ─────────────────────────────────────────────────────────────────────────────


class TestNoLLMZeroConstruction:
    def test_no_llm_zero_runner_and_succeeded_not_requested(self, tmp_path):
        factory, stats = _make_factory(_manifest(llm_judge_required=False), {})
        rt, store = _runtime(tmp_path, llm_judge_required=False, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert stats["constructed"] == 0
        assert not (store.root / "score_jobs").exists()
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        assert merged["judge_execution_status"] == "not_requested"
        assert merged["merged_scores"] is None

    def test_no_llm_never_invokes_a_present_runner_factory(self, tmp_path):
        called = {"n": 0}

        def never_factory():
            called["n"] += 1
            raise AssertionError("runner factory must not be called on no-LLM path")

        rt, _ = _runtime(
            tmp_path, llm_judge_required=False, runner_factory=never_factory
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert called["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 确定性生命周期（§2.1 / §6）
# ─────────────────────────────────────────────────────────────────────────────


class TestDeterministicLifecycle:
    def test_no_llm_phase_sequence_and_artifacts(self, tmp_path):
        rt, store = _runtime(tmp_path, llm_judge_required=False)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        for rel in (
            a.ArtifactStore.MANIFEST_REL,
            w.EVIDENCE_BUNDLE_REL,
            w.GRADER_RESULTS_REL,
            w.MERGED_REL,
            w.REPORT_REL,
            a.ArtifactStore.LEDGER_REL,
        ):
            assert store.exists(rel), rel
        kinds = [e["kind"] for e in rt.journal.read_all()]
        assert kinds.index("manifest_frozen") < kinds.index("evidence_materialized")
        assert kinds.index("evidence_materialized") < kinds.index("graders_completed")
        assert kinds.index("graders_completed") < kinds.index("judge_mode")
        assert kinds.index("judge_mode") < kinds.index("report_rendered")
        assert kinds.index("report_rendered") < kinds.index("finalized")
        assert "score_jobs_built" not in kinds

    def test_requested_phase_sequence(self, tmp_path):
        factory, _ = _make_factory(_manifest(), {})
        rt, _ = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        kinds = [e["kind"] for e in rt.journal.read_all()]
        assert "score_jobs_built" in kinds
        assert kinds.index("score_jobs_built") < kinds.index("score_job_claimed")
        assert kinds.index("scores_merged") < kinds.index("report_rendered")
        assert kinds.index("report_rendered") < kinds.index("finalized")


# ─────────────────────────────────────────────────────────────────────────────
# Send fan-out + reducer 集成（§6 / R4）
# ─────────────────────────────────────────────────────────────────────────────


class TestSendFanout:
    def test_all_jobs_fan_out_and_merge_ok(self, tmp_path):
        factory, stats = _make_factory(_manifest(), {})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        jobs = w._list_jobs(store)
        results = w._load_all_results(store)
        assert len(jobs) == len(results) == len(stats["calls"]) >= 4
        assert all(res.status is c.ScoreJobStatus.SUCCEEDED for res in results)
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        assert merged["status"] == "ok"
        assert merged["judge_execution_status"] == "requested"
        assert set(merged["merged_scores"]) == {"dispatch", "observation"}
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED

    def test_two_rubrics_same_target_merge_is_normal_fanout(self, tmp_path):
        rubric_a = _rubric(c.SampleTargetType.DISPATCH, digest="a" * 64)
        rubric_b = _rubric(c.SampleTargetType.DISPATCH, digest="b" * 64)
        obs = _rubric(c.SampleTargetType.OBSERVATION)
        manifest = _manifest(rubrics=[rubric_a, rubric_b, obs])
        factory, _ = _make_factory(manifest, {})
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        group = merged["merged_scores"]["dispatch"]
        assert len(group["rubrics"]) == 2
        assert {rs["rubric_digest"] for rs in group["rubrics"]} == {"a" * 64, "b" * 64}


# ─────────────────────────────────────────────────────────────────────────────
# semaphore 并发上限（§6 / R4）：model_requested audit 断言 in-flight ≤ limit
# ─────────────────────────────────────────────────────────────────────────────


class TestSemaphoreConcurrency:
    @staticmethod
    def _max_inflight(rt) -> int:
        inflight = 0
        mx = 0
        for e in rt.journal.read_all():
            if e["kind"] == "model_requested":
                inflight += 1
                mx = max(mx, inflight)
            elif e["kind"] == "model_finished":
                inflight -= 1
        return mx

    def test_inflight_never_exceeds_concurrency(self, tmp_path):
        manifest = _manifest(concurrency=2)
        factory, _ = _make_factory(manifest, {"__default__": "slow"}, sleep=0.08)
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert self._max_inflight(rt) == 2
        assert all(
            x.status is c.ScoreJobStatus.SUCCEEDED for x in w._load_all_results(store)
        )

    def test_inflight_with_concurrency_one(self, tmp_path):
        manifest = _manifest(concurrency=1)
        factory, _ = _make_factory(manifest, {"__default__": "slow"}, sleep=0.05)
        rt, _ = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        w.run_eval_workflow(rt)
        assert self._max_inflight(rt) == 1


# ─────────────────────────────────────────────────────────────────────────────
# per-job timeout / retry（§6 / R4）
# ─────────────────────────────────────────────────────────────────────────────


class TestTimeoutRetry:
    def test_timeout_once_then_succeed(self, tmp_path):
        factory, _ = _make_factory(_manifest(retry=2), {"__default__": "timeout_once"})
        rt, store = _runtime(tmp_path, runner_factory=factory, timeout_override=0.05)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        results = w._load_all_results(store)
        assert all(x.status is c.ScoreJobStatus.SUCCEEDED for x in results)
        assert all(x.retry_count == 1 for x in results)
        assert all(x.node_attempt == 2 for x in results)
        kinds = [e["kind"] for e in rt.journal.read_all()]
        assert "job_timeout" in kinds and "job_retry" in kinds

    def test_always_timeout_becomes_timed_out_partial(self, tmp_path):
        manifest = _manifest(retry=1)
        factory, _ = _make_factory(manifest, {"__default__": "timeout"})
        rt, store = _runtime(
            tmp_path, runner_factory=factory, timeout_override=0, manifest=manifest
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert all(x.status is c.ScoreJobStatus.TIMED_OUT for x in results)
        assert all(x.retry_count == 1 for x in results)
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.PARTIAL
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.PARTIAL

    def test_retryable_failure_then_success(self, tmp_path):
        factory, _ = _make_factory(_manifest(retry=2), {"__default__": "fail_then_ok"})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        results = w._load_all_results(store)
        assert all(x.status is c.ScoreJobStatus.SUCCEEDED for x in results)
        assert all(x.retry_count == 1 for x in results)

    def test_retryable_failure_exhausted_becomes_failed_partial(self, tmp_path):
        manifest = _manifest(retry=1)
        factory, _ = _make_factory(manifest, {"__default__": "fail"})
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert all(x.status is c.ScoreJobStatus.FAILED for x in results)
        assert all(x.retry_count == 1 for x in results)

    def test_invalid_draft_is_typed_failed_not_crashed(self, tmp_path):
        factory, _ = _make_factory(_manifest(retry=0), {"__default__": "invalid_draft"})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert all(x.status is c.ScoreJobStatus.FAILED for x in results)
        assert all(x.validation_errors for x in results)


# ─────────────────────────────────────────────────────────────────────────────
# unknown / budget / merge 分组
# ─────────────────────────────────────────────────────────────────────────────


class TestUnknownAndBudget:
    def test_unknown_is_not_zero_and_does_not_force_partial(self, tmp_path):
        factory, _ = _make_factory(_manifest(), {"__default__": "unknown"})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        group = merged["merged_scores"]["dispatch"]
        rubric = group["rubrics"][0]
        assert rubric["scored"] == 0
        assert rubric["excluded"] > 0
        assert "unknown_excluded" in rubric["excluded_reasons"].values()

    def test_max_score_jobs_budget_yields_budget_exhausted_partial(self, tmp_path):
        manifest = _manifest(max_score_jobs=2)
        factory, _ = _make_factory(manifest, {})
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        ledger = store.read_final_ledger()
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.BUDGET_EXHAUSTED
        jobs = w._list_jobs(store)
        assert any(j.status is c.ScoreJobStatus.NOT_RUN_BUDGET for j in jobs)
        results = w._load_all_results(store)
        assert len(results) == 2


# ─────────────────────────────────────────────────────────────────────────────
# cancel 与 late（§2.3 / R8）
# ─────────────────────────────────────────────────────────────────────────────


class TestCancel:
    def test_cancel_before_invoke_no_model_calls(self, tmp_path):
        factory, stats = _make_factory(_manifest(), {})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        rt.cancel.request()
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.CANCELLED
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.CANCELLED
        cancelled = [e for e in rt.journal.read_all() if e["kind"] == "cancelled"]
        assert len(cancelled) == 1
        assert stats["constructed"] == 0
        assert not any(e["kind"] == "model_requested" for e in rt.journal.read_all())

    def test_cancel_inflight_stops_new_claims_and_writes_ledger_once(self, tmp_path):
        manifest = _manifest(concurrency=2)
        factory, _ = _make_factory(manifest, {"__default__": "slow"}, sleep=0.3)
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)

        async def run_with_inflight_cancel():
            await rt.open_checkpointer()
            try:
                app = w.build_eval_workflow(rt)
                task = asyncio.create_task(
                    app.ainvoke(
                        w._initial_state(rt),
                        config={"configurable": {"thread_id": rt.thread_id}},
                    )
                )
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (
                        sum(
                            1
                            for e in rt.journal.read_all()
                            if e["kind"] == "model_requested"
                        )
                        >= 2
                    ):
                        break
                    await asyncio.sleep(0.01)
                rt.cancel.request()
                return await task
            finally:
                await rt.close_checkpointer()

        result = asyncio.run(run_with_inflight_cancel())
        assert result["terminal_status"] is c.WorkflowStatus.CANCELLED
        results = w._load_all_results(store)
        assert results and all(x.status is c.ScoreJobStatus.CANCELLED for x in results)
        cancelled = [e for e in rt.journal.read_all() if e["kind"] == "cancelled"]
        assert len(cancelled) == 1
        assert any(e["kind"] == "job_cancelled_inflight" for e in rt.journal.read_all())


class TestJobLifecycle:
    def test_successful_jobs_become_terminal_matching_result(self, tmp_path):
        """ScoreJob 从 CLAIMED 转到与 result 相同的终态（audit gap 2）。"""
        factory, _ = _make_factory(_manifest(), {})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        jobs = w._list_jobs(store)
        results = w._load_all_results(store)
        assert len(jobs) == len(results)
        by_id = {str(res.binding.job_id): res for res in results}
        for job in jobs:
            if job.status is c.ScoreJobStatus.NOT_RUN_BUDGET:
                continue
            assert c.is_job_terminal(job.status), job.status.value
            assert job.status is by_id[str(job.job_id)].status

    def test_timed_out_jobs_become_terminal_timed_out(self, tmp_path):
        manifest = _manifest(retry=0)
        factory, _ = _make_factory(manifest, {"__default__": "timeout"})
        rt, store = _runtime(
            tmp_path, runner_factory=factory, timeout_override=0, manifest=manifest
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        jobs = w._list_jobs(store)
        assert jobs
        assert all(j.status is c.ScoreJobStatus.TIMED_OUT for j in jobs)

    def test_cancelled_jobs_become_terminal_cancelled(self, tmp_path):
        factory, _ = _make_factory(_manifest(), {})
        rt, store = _runtime(tmp_path, runner_factory=factory)
        rt.cancel.request()
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.CANCELLED
        jobs = w._list_jobs(store)
        results = w._load_all_results(store)
        assert jobs and results
        assert all(j.status is c.ScoreJobStatus.CANCELLED for j in jobs)
        assert all(res.status is c.ScoreJobStatus.CANCELLED for res in results)


class TestLateIgnored:
    def test_response_after_terminal_job_is_late_ignored(self, tmp_path):
        manifest = _manifest()
        store = a.ArtifactStore(tmp_path / "late" / str(manifest.manifest.attempt_id))
        journal = store.audit_journal()
        pre = w.EvalRuntime(
            manifest,
            a.ArtifactStore(tmp_path / "pre"),
            a.AuditJournal(tmp_path / "pre" / "e.jsonl"),
            episode=_episode(),
        )
        job = w._build_jobs(pre)[0]
        store.write_canonical_json(
            f"{w.JOBS_DIR}/{job.job_id}.json",
            job.model_dump(mode="json"),
            producer="builder",
        )
        cancelled_job = job.model_copy(update={"status": c.ScoreJobStatus.CANCELLED})
        store.write_canonical_json(
            f"{w.JOBS_DIR}/{job.job_id}.json",
            cancelled_job.model_dump(mode="json"),
            producer="builder",
        )
        late = c.ScoreResult(
            binding=job.binding(), status=c.ScoreJobStatus.SUCCEEDED, node_attempt=1
        )
        w.persist_score_result(store, journal, cancelled_job, late)
        persisted = c.ScoreResult.model_validate(
            json.loads(store.read_bytes(f"{w.RESULTS_DIR}/{job.job_id}.json"))
        )
        assert persisted.status is c.ScoreJobStatus.LATE_IGNORED
        events = journal.read_all()
        assert any(e["kind"] == "late_ignored" for e in events)
        assert any(e["kind"] == "score_result_persisted" and e["late"] for e in events)
        # late_ignored 不得改写已终态的 job 文件（§2.3 / audit gap 2）。
        job_on_disk = w._load_score_job(store, str(job.job_id))
        assert job_on_disk is not None
        assert job_on_disk.status is c.ScoreJobStatus.CANCELLED

    def test_cancellation_ledger_written_exactly_once(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        store = a.ArtifactStore(tmp_path / "once" / str(manifest.manifest.attempt_id))
        journal = store.audit_journal()
        first = w.write_cancellation_ledger(store, manifest, journal)
        assert first is not None
        second = w.write_cancellation_ledger(store, manifest, journal)
        assert second is None
        assert len([e for e in journal.read_all() if e["kind"] == "cancelled"]) == 1
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.CANCELLED


# ─────────────────────────────────────────────────────────────────────────────
# P3-M4：WorkflowStatus 转换表由 runtime 强制执行（fresh/resume/cancel 合法迁移）
# ─────────────────────────────────────────────────────────────────────────────


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


class TestWorkflowTransitionGuard:
    def test_advance_phase_legal_chain(self, tmp_path):
        rt, _ = _runtime(tmp_path, manifest=_manifest(llm_judge_required=False))
        state = SimpleNamespace(phase=c.WorkflowStatus.CREATED)
        phase = w._advance_phase(
            state, rt, c.WorkflowStatus.INPUT_VALIDATED, c.WorkflowStatus.ADMITTED
        )
        assert phase is c.WorkflowStatus.ADMITTED

    def test_advance_phase_rejects_illegal_jump(self, tmp_path):
        rt, _ = _runtime(tmp_path, manifest=_manifest())
        state = SimpleNamespace(phase=c.WorkflowStatus.SCORES_JOINED)
        with pytest.raises(c.ContractViolation):
            w._advance_phase(state, rt, c.WorkflowStatus.RENDERED)

    def test_terminal_regression_fails_closed(self, tmp_path):
        rt, _ = _runtime(tmp_path, manifest=_manifest())
        state = SimpleNamespace(phase=c.WorkflowStatus.SUCCEEDED)
        with pytest.raises(c.ContractViolation):
            w._advance_phase(state, rt, c.WorkflowStatus.FAILED)
        state2 = SimpleNamespace(phase=c.WorkflowStatus.FAILED)
        with pytest.raises(c.ContractViolation):
            w._advance_phase(state2, rt, c.WorkflowStatus.SCORES_MERGED)

    def test_illegal_node_update_ends_failed_not_crash(self, tmp_path):
        """非法节点更新（从 terminal 出发的迁移）→ fail-closed FAILED，不崩溃。"""
        rt, _ = _runtime(tmp_path, manifest=_manifest(llm_judge_required=False))

        async def run():
            await rt.open_checkpointer()
            try:
                app = w.build_eval_workflow(rt)
                s0 = w._initial_state(rt)
                s0["phase"] = c.WorkflowStatus.SUCCEEDED
                return await app.ainvoke(
                    s0, config={"configurable": {"thread_id": "t"}}
                )
            finally:
                await rt.close_checkpointer()

        result = asyncio.run(run())
        assert result["terminal_status"] is c.WorkflowStatus.FAILED
        assert "illegal workflow transition" in result["error"]

    def test_fresh_no_llm_phase_chain_is_legal(self, tmp_path):
        rt, _ = _runtime(tmp_path, llm_judge_required=False)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        _assert_legal_chain(_phase_pairs(rt))

    def test_fresh_requested_phase_chain_is_legal(self, tmp_path):
        manifest = _manifest()
        factory, _ = _make_factory(manifest, {})
        rt, _ = _runtime(tmp_path, runner_factory=factory)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        _assert_legal_chain(_phase_pairs(rt))

    def test_cancel_phase_chain_is_legal(self, tmp_path):
        factory, _ = _make_factory(_manifest(), {})
        rt, _ = _runtime(tmp_path, runner_factory=factory)
        rt.cancel.request()
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.CANCELLED
        _assert_legal_chain(_phase_pairs(rt))


# ─────────────────────────────────────────────────────────────────────────────
# P3-M6：ScoreDraft evidence 与 dimensions 必须严格一一对应（数量/名称/路径/role）
# ─────────────────────────────────────────────────────────────────────────────


class TestEvidenceOneToOne:
    @staticmethod
    def _job(runtime: w.EvalRuntime) -> c.ScoreJob:
        return w._build_jobs(runtime)[0]

    @staticmethod
    def _job_evidence(job: c.ScoreJob, dim: str) -> c.EvidenceRef:
        sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
        ref = c.ArtifactRef(
            path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
            sha256=sha,
            bytes=len(sha),
            media_type="application/json",
            producer="test",
        )
        return c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)

    @staticmethod
    def _rubric_dims(manifest, job: c.ScoreJob) -> list[str]:
        for rubric in manifest.manifest.rubrics:
            if rubric.rubric_id == job.rubric_id and rubric.digest == job.rubric_digest:
                return rubric.dimensions
        raise AssertionError("rubric not found")

    def _build(
        self, rt: w.EvalRuntime, job: c.ScoreJob, dims, evidence
    ) -> c.ScoreResult:
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions=dims,
            evidence=evidence,
            model_used="test",
        )
        outcome = r.RunnerOutcome(
            status=r.RunnerStatus.SUCCEEDED,
            draft=draft,
            usage=c.UsageSnapshot(total_tokens=1),
            latency_ms=1,
        )
        return w.build_score_result(rt, job, outcome, 1, uuid4())

    def test_strict_one_to_one_succeeds(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = [d for d in self._rubric_dims(manifest, job)]
        evidence = [self._job_evidence(job, d) for d in dims]
        result = self._build(rt, job, {d: 1.0 for d in dims}, evidence)
        assert result.status is c.ScoreJobStatus.SUCCEEDED
        assert set(result.dimension_evidence_refs) == set(dims)

    def test_too_few_evidence_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        result = self._build(
            rt, job, {d: 1.0 for d in dims}, [self._job_evidence(job, dims[0])]
        )
        assert result.status is c.ScoreJobStatus.FAILED
        assert any(
            "evidence count 1 != dimension count" in e for e in result.validation_errors
        )

    def test_too_many_evidence_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        extra = self._job_evidence(job, dims[0])
        evidence = [self._job_evidence(job, d) for d in dims] + [extra]
        result = self._build(rt, job, {d: 1.0 for d in dims}, evidence)
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("evidence count" in e for e in result.validation_errors)

    def test_duplicate_evidence_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        same = self._job_evidence(job, dims[0])
        result = self._build(rt, job, {d: 1.0 for d in dims}, [same, same])
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("duplicate evidence ref" in e for e in result.validation_errors)

    def test_cross_job_ref_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        other = self._job(
            w.EvalRuntime(
                manifest,
                a.ArtifactStore(tmp_path / "other" / str(manifest.manifest.attempt_id)),
                a.AuditJournal(tmp_path / "other" / "e.jsonl"),
                episode=_episode(),
            )
        )
        dims = self._rubric_dims(manifest, job)
        evidence = [self._job_evidence(other, d) for d in dims]
        result = self._build(rt, job, {d: 1.0 for d in dims}, evidence)
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("not job-scoped" in e for e in result.validation_errors)

    def test_digest_mismatch_rejected_via_model_construct(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        sha = c.sha256_hex(f"{job.job_id}:{dims[0]}".encode())
        ref = c.ArtifactRef(
            path=f"evidence/job-scoped/{job.job_id}/{dims[0]}.json",
            sha256=sha,
            bytes=len(sha),
            media_type="application/json",
            producer="test",
        )
        # model_construct 绕验 Pydantic，专门打 validator 的防御层。
        bad = c.EvidenceRef.model_construct(
            ref=ref, claim_type=c.ClaimType.SCORE, digest="0" * 64
        )
        dims2 = self._rubric_dims(manifest, job)
        draft = c.ScoreDraft.model_construct(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={d: 1.0 for d in dims2},
            evidence=[bad, self._job_evidence(job, dims2[0])]
            if len(dims2) > 1
            else [bad],
            model_used="test",
        )
        outcome = r.RunnerOutcome(status=r.RunnerStatus.SUCCEEDED, draft=draft)
        result = w.build_score_result(rt, job, outcome, 1, uuid4())
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("evidence digest mismatch" in e for e in result.validation_errors)

    def test_wrong_claim_type_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        sha = c.sha256_hex(f"{job.job_id}:{dims[0]}".encode())
        ref = c.ArtifactRef(
            path=f"evidence/job-scoped/{job.job_id}/{dims[0]}.json",
            sha256=sha,
            bytes=len(sha),
            media_type="application/json",
            producer="test",
        )
        wrong = c.EvidenceRef(ref=ref, claim_type=c.ClaimType.OBSERVATION, digest=sha)
        evidence = [wrong]
        evidence += [self._job_evidence(job, d) for d in dims[1:]]
        result = self._build(rt, job, {d: 1.0 for d in dims}, evidence)
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("claim_type" in e for e in result.validation_errors)

    def test_dimension_name_mismatch_rejected(self, tmp_path):
        manifest = _manifest()
        rt, _ = _runtime(tmp_path, manifest=manifest)
        job = self._job(rt)
        dims = self._rubric_dims(manifest, job)
        wrong_dims = {
            ("not_a_dimension" if i == 0 else d): 1.0 for i, d in enumerate(dims)
        }
        evidence = [self._job_evidence(job, d) for d in dims]
        result = self._build(rt, job, wrong_dims, evidence)
        assert result.status is c.ScoreJobStatus.FAILED
        assert any("rubric dimensions" in e for e in result.validation_errors)

    def test_evidence_mismatch_runner_is_typed_failed(self, tmp_path):
        """图集成：fake runner 返回 dimensions/evidence 数量不一致的 draft →
        每个 job 都是 typed FAILED（validation_errors），不进成功 ScoreResult。"""
        manifest = _manifest(retry=0)
        factory, _ = _make_factory(manifest, {"__default__": "evidence_mismatch"})
        rt, store = _runtime(tmp_path, runner_factory=factory, manifest=manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.PARTIAL
        results = w._load_all_results(store)
        assert results
        assert all(x.status is c.ScoreJobStatus.FAILED for x in results)
        assert all(x.validation_errors for x in results)
