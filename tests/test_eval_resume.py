"""P3 resume/cancel/exit-mapping 单测（设计 §2.3、§3.3、§7.1、§9 P3）。

覆盖：terminal ledger 完整 refs → zero-work short-circuit；非 terminal →
只路由缺失阶段（kill 后 resume 只重跑缺失 job）；lease conflict → exit 2；
`map_workflow_exit` 映射（SUCCEEDED/PARTIAL→0、FAILED→1、CANCELLED→130、
attempt_busy/publish_conflict→2）；CLI 可注入 workflow adapter 接线；参考
adapter 在真实（合成）结果目录上的 no-LLM / requested-fake / attempt_busy 行为。

只用 fake runner；不构造任何 DeepAgent/chat 模型。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langgraph.types import Command

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


def _manifest(llm_judge_required: bool = True, **pol: Any) -> c.FrozenInputManifest:
    defaults: dict[str, Any] = {
        "llm_judge_required": llm_judge_required,
        "audit_level": c.AuditLevel.STANDARD,
        "retry": 2,
        "timeout_s": 60,
        "concurrency": 4,
        "retention_days": 30,
        "merge_policy_digest": sm.MergePolicy().digest(),
    }
    defaults.update(pol)
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
        policy=c.PolicySpec(**defaults),
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


def _episode() -> SimpleNamespace:
    steps: dict[int, SimpleNamespace] = {}
    for s in range(1, 6):
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
    tmp_path, manifest, *, runner_factory=None, episode=None
) -> tuple[w.EvalRuntime, a.ArtifactStore]:
    store = a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))
    rt = w.EvalRuntime(
        manifest,
        store,
        store.audit_journal(),
        runner_factory=runner_factory,
        episode=episode or _episode(),
        grader_fn=lambda: ([], []),
    )
    return rt, store


def _make_factory(manifest, configs=None):
    return r.make_fake_runner_factory(manifest, configs)


def _complete_result(runtime: w.EvalRuntime, job: c.ScoreJob) -> c.ScoreResult:
    """用 fake runner 生成一个完整（证据 + validated output）的 succeeded 结果。"""
    factory, _ = _make_factory(runtime.manifest, {str(job.job_id): "ok"})
    runner = factory()
    outcome = asyncio.run(runner.run(job, invocation_id=uuid4(), node_attempt=1))
    result = w.build_score_result(runtime, job, outcome, 1, uuid4())
    assert result.status is c.ScoreJobStatus.SUCCEEDED
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 完成 ledger short-circuit（§2.3 / R8）
# ─────────────────────────────────────────────────────────────────────────────


class TestShortCircuit:
    def test_completed_run_short_circuits_with_zero_work(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        first = w.run_eval_workflow(rt)
        assert first.status is c.WorkflowStatus.SUCCEEDED
        before = store.audit_journal().event_count()

        rt2, _ = _runtime(tmp_path, manifest)
        second = w.run_eval_workflow(rt2)
        assert second.status is c.WorkflowStatus.SUCCEEDED
        new_events = store.audit_journal().read_all()[before:]
        kinds = [e["kind"] for e in new_events]
        assert kinds == ["short_circuit"]
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED

    def test_requested_completed_run_short_circuits(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        factory, _ = _make_factory(manifest, {})
        rt, store = _runtime(tmp_path, manifest, runner_factory=factory)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED
        before = store.audit_journal().event_count()
        factory2, _ = _make_factory(manifest, {})
        rt2, _ = _runtime(tmp_path, manifest, runner_factory=factory2)
        assert w.run_eval_workflow(rt2).status is c.WorkflowStatus.SUCCEEDED
        kinds = [e["kind"] for e in store.audit_journal().read_all()[before:]]
        assert kinds == ["short_circuit"]

    def test_short_circuit_after_completed_run_skips_jobs(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        factory, _ = _make_factory(manifest, {})
        rt, _ = _runtime(tmp_path, manifest, runner_factory=factory)
        w.run_eval_workflow(rt)
        factory2, stats2 = _make_factory(manifest, {})
        rt2, _ = _runtime(tmp_path, manifest, runner_factory=factory2)
        w.run_eval_workflow(rt2)
        assert stats2["constructed"] == 0
        assert len(stats2["calls"]) == 0


# ─────────────────────────────────────────────────────────────────────────────
# kill/resume（§2.3 / R2）
# ─────────────────────────────────────────────────────────────────────────────


class TestKillResume:
    def test_resume_reruns_only_missing_jobs(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        factory, _ = _make_factory(manifest, {"__default__": "crash"})
        rt, store = _runtime(tmp_path, manifest, runner_factory=factory)
        with pytest.raises(RuntimeError, match="simulated kill"):
            w.run_eval_workflow(rt)

        jobs = w._list_jobs(store)
        assert jobs
        # 模拟 kill 前已完成的一个 job：写完整结果。
        done_job = jobs[0]
        result = _complete_result(rt, done_job)
        w.persist_score_result(store, store.audit_journal(), done_job, result)
        missing = {
            jid for jid in w._list_job_refs(store) if not w._has_result(store, jid)
        }
        assert str(done_job.job_id) not in missing

        before = store.audit_journal().event_count()
        factory2, _ = _make_factory(manifest, {})
        rt2, _ = _runtime(tmp_path, manifest, runner_factory=factory2)
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED

        new_events = store.audit_journal().read_all()[before:]
        resumed = {e["job_id"] for e in new_events if e["kind"] == "model_requested"}
        assert resumed == missing
        assert str(done_job.job_id) not in resumed
        results = w._load_all_results(store)
        assert len(results) == len(jobs)
        assert any(x.binding.job_id == done_job.job_id for x in results)

    def test_resume_from_missing_merge_skips_completed_jobs(self, tmp_path):
        """全部 job 已完成但 merged 缺失 → resume 只跑 merge/render/finalize。"""
        manifest = _manifest(llm_judge_required=True)
        factory, _ = _make_factory(manifest, {})
        rt, store = _runtime(tmp_path, manifest, runner_factory=factory)
        assert w.run_eval_workflow(rt).status is c.WorkflowStatus.SUCCEEDED

        # 模拟在 finalize 前被 kill：删除 merged/report/ledger（保留 jobs+results）。
        for rel in (w.MERGED_REL, w.REPORT_REL, a.ArtifactStore.LEDGER_REL):
            store.path(rel).unlink()

        before = store.audit_journal().event_count()
        factory2, stats2 = _make_factory(manifest, {})
        rt2, _ = _runtime(tmp_path, manifest, runner_factory=factory2)
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        new_events = store.audit_journal().read_all()[before:]
        kinds = [e["kind"] for e in new_events]
        assert "scores_merged" in kinds
        assert not any(e["kind"] == "model_requested" for e in new_events)
        assert stats2["constructed"] == 0
        assert "finalized" in kinds


# ─────────────────────────────────────────────────────────────────────────────
# lease conflict（§1.2-3 / R2）
# ─────────────────────────────────────────────────────────────────────────────


class TestLeaseConflict:
    def test_attempt_busy_maps_to_exit_two(self, tmp_path):
        results_dir = _make_results_dir(tmp_path)
        run_id = uuid4()
        series = results_dir / "eval_attempts" / str(run_id)
        lease = a.AttemptLease.acquire(series, owner_id="holder")
        try:
            args = SimpleNamespace(
                results_dir=str(results_dir),
                no_llm_judge=True,
                judge_sample_steps=20,
                eval_run_id=run_id,
                attempt_id=uuid4(),
            )
            assert w.run_from_results_dir(args) == 2
        finally:
            lease.release()
        args = SimpleNamespace(
            results_dir=str(results_dir),
            no_llm_judge=True,
            judge_sample_steps=20,
            eval_run_id=run_id,
            attempt_id=uuid4(),
        )
        assert w.run_from_results_dir(args) == 0


# ─────────────────────────────────────────────────────────────────────────────
# exit mapping（§7.1 / R8）
# ─────────────────────────────────────────────────────────────────────────────


class TestExitMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (c.WorkflowStatus.SUCCEEDED, 0),
            (c.WorkflowStatus.PARTIAL, 0),
            (c.WorkflowStatus.FAILED, 1),
            (c.WorkflowStatus.CANCELLED, 130),
        ],
    )
    def test_status_mapping(self, status, expected):
        assert w.map_workflow_exit(w.WorkflowOutcome(status=status)) == expected

    def test_attempt_busy_and_publish_conflict_map_to_two(self):
        assert (
            w.map_workflow_exit(
                w.WorkflowOutcome(status=c.WorkflowStatus.SUCCEEDED, attempt_busy=True)
            )
            == 2
        )
        assert (
            w.map_workflow_exit(
                w.WorkflowOutcome(
                    status=c.WorkflowStatus.SUCCEEDED, publish_conflict=True
                )
            )
            == 2
        )
        assert (
            w.map_workflow_exit(
                w.WorkflowOutcome(status=c.WorkflowStatus.CANCELLED, attempt_busy=True)
            )
            == 2
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI 可注入 workflow adapter（§7.1 / §9 P3 Modify cli.py）
# ─────────────────────────────────────────────────────────────────────────────


class TestCLIAdapterWiring:
    def test_injected_adapter_returns_code(self):
        from sar_orch.eval import cli

        code = cli.main(["--results-dir", "/x"], workflow_adapter=lambda args: 130)
        assert code == 130

    def test_module_level_adapter(self):
        from sar_orch.eval import cli

        cli.set_workflow_adapter(lambda args: 2)
        try:
            code = cli.main(["--results-dir", "/x"])
            assert code == 2
        finally:
            cli.set_workflow_adapter(None)

    def test_legacy_path_unchanged_when_no_adapter(self):
        from sar_orch.eval import cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["--results-dir", "/nonexistent-eval-dir"])
        assert exc.value.code == 1


# ─────────────────────────────────────────────────────────────────────────────
# 参考 adapter：真实（合成）结果目录
# ─────────────────────────────────────────────────────────────────────────────


def _make_results_dir(root: Path) -> Path:
    rd = root / "results"
    rd.mkdir(parents=True)
    (rd / "metadata.json").write_text(
        json.dumps(
            {
                "scene": 1,
                "agent_count": 2,
                "seed": 42,
                "model": "deepseek-v4-flash",
                "code_commit": "x",
                "git_dirty": False,
                "agent_names": ["Alice", "Bob"],
            }
        )
    )
    (rd / "trajectory.csv").write_text(
        "Step,Coverage,TransportRate,Finished\n"
        "1,0.5,0.0,false\n2,0.6,0.1,false\n3,0.7,0.2,false\n"
    )
    (rd / "router_interactions.csv").write_text(
        "Step,Subtask,AssignedTo,CorrelationID,WorkerTaskID,EventType\n"
    )
    (rd / "subtasks.csv").write_text(
        "Step,SubtaskID,Status,AssignedTo,Subtask,FailureClass\n"
    )
    (rd / "agent_interactions.csv").write_text(
        "Step,Agent,ToolName,ToolArgs,Action,Observation\n"
        '2,Alice,report_observation,"{}",report_observation(),report_observation ok\n'
    )
    (rd / "summary.csv").write_text("Scene,Agents,Seed\n1,2,42\n")
    (rd / "token_usage.csv").write_text("Step,Agent,TotalTokens\n")
    (rd / "semantic_map.jsonl").write_text("")
    (rd / "map_summary.jsonl").write_text("")
    return rd


class TestReferenceAdapter:
    def test_no_llm_adapter_succeeds_with_artifacts(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        args = SimpleNamespace(
            results_dir=str(rd), no_llm_judge=True, judge_sample_steps=20
        )
        assert w.run_from_results_dir(args) == 0
        attempts = list((rd / "eval_attempts").glob("*/attempts/*"))
        assert len(attempts) == 1
        store = a.ArtifactStore(attempts[0])
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED
        assert store.exists(w.REPORT_REL)

    def test_no_llm_adapter_short_circuits_on_rerun(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        run_id, attempt_id = uuid4(), uuid4()
        args = SimpleNamespace(
            results_dir=str(rd),
            no_llm_judge=True,
            judge_sample_steps=20,
            eval_run_id=run_id,
            attempt_id=attempt_id,
        )
        assert w.run_from_results_dir(args) == 0
        store = a.ArtifactStore(
            rd / "eval_attempts" / str(run_id) / "attempts" / str(attempt_id)
        )
        before = store.audit_journal().event_count()
        assert w.run_from_results_dir(args) == 0
        new_events = store.audit_journal().read_all()[before:]
        assert [e["kind"] for e in new_events] == ["short_circuit", "pointer_published"]

    def test_requested_without_runner_refused_no_llm_constructed(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        args = SimpleNamespace(
            results_dir=str(rd), no_llm_judge=False, judge_sample_steps=20
        )
        assert w.run_from_results_dir(args) == 2
        assert not (rd / "eval_attempts").exists()

    def test_requested_with_fake_runner_succeeds(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        args = SimpleNamespace(
            results_dir=str(rd), no_llm_judge=False, judge_sample_steps=20
        )
        code = w.run_from_results_dir(
            args,
            runner_factory_factory=lambda runtime: r.make_fake_runner_factory(
                runtime.manifest, {}
            )[0],
        )
        assert code == 0
        attempts = list((rd / "eval_attempts").glob("*/attempts/*"))
        store = a.ArtifactStore(attempts[-1])
        ledger = store.read_final_ledger()
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert (store.root / "score_jobs").exists()

    def test_missing_results_dir_returns_two(self, tmp_path):
        args = SimpleNamespace(
            results_dir=str(tmp_path / "missing"),
            no_llm_judge=True,
            judge_sample_steps=20,
        )
        assert w.run_from_results_dir(args) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 持久化 checkpointer（§6.3 / P0 probe）：SQLite + 同 thread_id 续跑
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistentCheckpointer:
    def test_sqlite_checkpointer_interrupt_and_resume_same_thread(self, tmp_path):
        """同一 SQLite DB + 同一 thread_id，全新 graph 实例可续跑（非重启）。

        interrupt 发生在 `freeze_input_manifest` 之后；resume 用全新编译的 graph
        + `Command(resume=...)`。freeze 不重跑（只有一次 manifest_frozen），
        续跑完成 materialize→…→finalize → SUCCEEDED。
        """
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        rt.thread_id = "probe-thread-1"
        config = {"configurable": {"thread_id": rt.thread_id}}

        async def run_interrupt_resume():
            await rt.open_checkpointer()
            try:
                rt.probe_interrupt = True
                app = w.build_eval_workflow(rt)
                interrupted = await app.ainvoke(w._initial_state(rt), config=config)
                assert "__interrupt__" in interrupted
                rt.probe_interrupt = False
                # 关闭连接：checkpoint 必须已持久化到磁盘，而非驻留在内存连接。
                await rt.close_checkpointer()
                await rt.open_checkpointer()
                app2 = w.build_eval_workflow(rt)  # 全新 graph 实例，同 DB/thread
                resumed = await app2.ainvoke(Command(resume="proceed"), config=config)
                return interrupted, resumed
            finally:
                await rt.close_checkpointer()

        interrupted, resumed = asyncio.run(run_interrupt_resume())
        assert interrupted["phase"] is c.WorkflowStatus.MANIFEST_FROZEN
        assert resumed["terminal_status"] is c.WorkflowStatus.SUCCEEDED
        events = store.audit_journal().read_all()
        assert len([e for e in events if e["kind"] == "manifest_frozen"]) == 1
        assert store.exists(w.EVIDENCE_BUNDLE_REL)

    def test_checkpoint_db_file_created_and_connection_closed(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert (store.root / "checkpoint.sqlite").exists()
        # 显式生命周期：run 结束后连接已关闭。
        assert rt._owned_saver is None
        assert rt._cp_cm is None

    def test_checkpointer_requires_open_before_build(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, _ = _runtime(tmp_path, manifest)
        with pytest.raises(RuntimeError, match="checkpointer is not open"):
            w.build_eval_workflow(rt)


# ─────────────────────────────────────────────────────────────────────────────
# 非终态 resume：必须从 immutable frozen manifest 重建（§2.2/§2.3）
# ─────────────────────────────────────────────────────────────────────────────


def _attempt_root(rd: Path, eval_run_id, attempt_id) -> Path:
    return rd / "eval_attempts" / str(eval_run_id) / "attempts" / str(attempt_id)


def _make_nonterminal_attempt(
    tmp_path: Path, *, llm_judge_required: bool, eval_run_id, attempt_id
) -> tuple[Path, SimpleNamespace]:
    """构造一个已写 frozen manifest/evidence/jobs 但**无 final ledger** 的
    非终态 attempt（模拟 run 在 finalize 前被 kill）。"""
    rd = _make_results_dir(tmp_path)
    args = SimpleNamespace(
        results_dir=str(rd),
        no_llm_judge=not llm_judge_required,
        judge_sample_steps=20,
        eval_run_id=eval_run_id,
        attempt_id=attempt_id,
    )
    if llm_judge_required:
        code = w.run_from_results_dir(
            args,
            runner_factory_factory=lambda runtime: r.make_fake_runner_factory(
                runtime.manifest, {}
            )[0],
        )
    else:
        code = w.run_from_results_dir(args)
    assert code == 0
    ledger = _attempt_root(rd, eval_run_id, attempt_id) / "audit" / "final_ledger.json"
    assert ledger.exists()
    ledger.unlink()
    return rd, args


def _frozen_manifest(rd: Path, eval_run_id, attempt_id) -> c.FrozenInputManifest:
    store = a.ArtifactStore(_attempt_root(rd, eval_run_id, attempt_id))
    return store.read_input_manifest()


def _result_count(rd: Path, eval_run_id, attempt_id) -> int:
    results = _attempt_root(rd, eval_run_id, attempt_id) / "score_results"
    return len(list(results.glob("*.json"))) if results.exists() else 0


class TestResumeFrozenManifest:
    def test_source_mutation_rejected_exit_2_zero_runner(self, tmp_path):
        """source 变更 → fail-closed exit 2：不调 runner、不写新 canonical artifact。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, _ = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=True,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        frozen_digest = _frozen_manifest(rd, eval_run_id, attempt_id).digest
        results_before = _result_count(rd, eval_run_id, attempt_id)

        traj = rd / "trajectory.csv"
        with traj.open("a") as fh:
            fh.write("4,0.8,0.3,true\n")

        stats = {"constructed": 0}

        def counting_factory(runtime):
            def _factory():
                stats["constructed"] += 1
                return r.make_fake_runner_factory(runtime.manifest, {})[0]()

            return _factory

        args = SimpleNamespace(
            results_dir=str(rd),
            no_llm_judge=False,
            judge_sample_steps=20,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        code = w.run_from_results_dir(args, runner_factory_factory=counting_factory)
        assert code == 2
        assert stats["constructed"] == 0
        assert not (
            _attempt_root(rd, eval_run_id, attempt_id) / "audit" / "final_ledger.json"
        ).exists()
        assert _frozen_manifest(rd, eval_run_id, attempt_id).digest == frozen_digest
        assert _result_count(rd, eval_run_id, attempt_id) == results_before

    def test_no_llm_policy_mismatch_rejected_exit_2(self, tmp_path):
        """frozen requested 但 live --no-llm-judge → exit 2，不调 runner。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, _ = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=True,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        frozen_digest = _frozen_manifest(rd, eval_run_id, attempt_id).digest
        results_before = _result_count(rd, eval_run_id, attempt_id)

        args = SimpleNamespace(
            results_dir=str(rd),
            no_llm_judge=True,  # conflicts with frozen llm_judge_required=True
            judge_sample_steps=20,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        code = w.run_from_results_dir(args)
        assert code == 2
        assert not (
            _attempt_root(rd, eval_run_id, attempt_id) / "audit" / "final_ledger.json"
        ).exists()
        assert _frozen_manifest(rd, eval_run_id, attempt_id).digest == frozen_digest
        assert _result_count(rd, eval_run_id, attempt_id) == results_before

    def test_judge_sample_steps_mismatch_rejected_exit_2(self, tmp_path):
        """evidence 已记录 sampling target，live judge_sample_steps 不一致 → exit 2。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, _ = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=False,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        frozen_digest = _frozen_manifest(rd, eval_run_id, attempt_id).digest
        args = SimpleNamespace(
            results_dir=str(rd),
            no_llm_judge=True,
            judge_sample_steps=30,  # frozen evidence records target=20
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        code = w.run_from_results_dir(args)
        assert code == 2
        assert not (
            _attempt_root(rd, eval_run_id, attempt_id) / "audit" / "final_ledger.json"
        ).exists()
        assert _frozen_manifest(rd, eval_run_id, attempt_id).digest == frozen_digest

    def test_resume_uses_exact_frozen_manifest_digest(self, tmp_path):
        """同参数非终态 resume：runtime 使用 frozen manifest 的精确 digest/created_at，
        即使 live 构造（时钟/source）会不同。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, args = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=False,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        frozen = _frozen_manifest(rd, eval_run_id, attempt_id)
        frozen_digest = frozen.digest
        frozen_created_at = frozen.manifest.created_at

        runtime = w.build_runtime_for_results_dir(
            rd,
            args,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
            attempt_root=_attempt_root(rd, eval_run_id, attempt_id),
            series_root=rd / "eval_attempts" / str(eval_run_id),
        )
        assert runtime.manifest.digest == frozen_digest
        assert runtime.manifest.manifest.created_at == frozen_created_at

        outcome = w.run_eval_workflow(runtime)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert _frozen_manifest(rd, eval_run_id, attempt_id).digest == frozen_digest
        assert (
            _attempt_root(rd, eval_run_id, attempt_id) / "audit" / "final_ledger.json"
        ).exists()


# ─────────────────────────────────────────────────────────────────────────────
# P3-M1：非终态 resume 必须从已验证磁盘 artifact 重建（judge/merge/failures/refs）
# ─────────────────────────────────────────────────────────────────────────────


def _attempt_store(rd: Path, eval_run_id, attempt_id) -> a.ArtifactStore:
    return a.ArtifactStore(_attempt_root(rd, eval_run_id, attempt_id))


class TestResumeReconstruct:
    def test_no_llm_after_graders_resume_stays_not_requested_zero_jobs(self, tmp_path):
        """(a) no-LLM 在 graders 后中断、缺 merge/report 时恢复，仍为
        SUCCEEDED/not_requested，零 score job、零 runner 构造。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, args = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=False,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        root = _attempt_root(rd, eval_run_id, attempt_id)
        # 模拟在 graders 后、render 前被 kill：删掉 merged/report（ledger 已删）。
        for rel in (w.MERGED_REL, w.REPORT_REL):
            (root / rel).unlink()

        stats = {"constructed": 0}

        def never(runtime):
            def _f():
                stats["constructed"] += 1
                raise AssertionError("runner must not be constructed on no-LLM resume")

            return _f

        code = w.run_from_results_dir(args, runner_factory_factory=never)
        assert code == 0
        store = _attempt_store(rd, eval_run_id, attempt_id)
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.terminal_status is c.WorkflowStatus.SUCCEEDED
        assert ledger.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED
        merged = json.loads(store.read_bytes(w.MERGED_REL))
        assert merged["judge_execution_status"] == "not_requested"
        assert not (store.root / "score_jobs").exists()
        assert stats["constructed"] == 0

    def test_failed_merge_persisted_resume_stays_failed_with_failure_refs(
        self, tmp_path
    ):
        """(b) merge 已持久化为 FAILED、render 前中断时恢复：仍为 FAILED，
        不重判 SUCCEEDED，failure refs 不丢失。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, args = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=True,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        root = _attempt_root(rd, eval_run_id, attempt_id)
        # 模拟 render 前被 kill：删 report/ledger，并把 merged 换成 FAILED bundle。
        (root / w.REPORT_REL).unlink()
        store = _attempt_store(rd, eval_run_id, attempt_id)
        failed_merged = {
            "schema_version": 1,
            "status": "failed",
            "judge_execution_status": "requested",
            "merged_scores": None,
            "veto_reasons": [],
            "violations": ["dispatch: simulated merge failure"],
            "policy_digest": sm.MergePolicy().digest(),
        }
        store.write_canonical_json(w.MERGED_REL, failed_merged, producer="test")

        code = w.run_from_results_dir(
            args,
            runner_factory_factory=lambda runtime: r.make_fake_runner_factory(
                runtime.manifest, {}
            )[0],
        )
        assert code == 1  # FAILED
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.terminal_status is c.WorkflowStatus.FAILED
        assert any(f.kind == "score_merge" for f in ledger.failure_refs)
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["terminal_status"] == "failed"
        assert report["merge_status"] == "failed"

    def test_ledger_missing_resume_keeps_report_and_merge_refs_and_real_digest(
        self, tmp_path
    ):
        """(c) 仅缺 ledger：恢复保留 report/merge refs 与真实 report digest，
        并可通过 publish 链校验。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, args = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=False,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        store = _attempt_store(rd, eval_run_id, attempt_id)
        report_ref = w._rehash_ref(store, w.REPORT_REL, "renderer")
        merged_ref = w._rehash_ref(store, w.MERGED_REL, "merge")
        assert report_ref is not None and merged_ref is not None
        frozen = store.read_input_manifest()

        code = w.run_from_results_dir(args)
        assert code == 0
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.report_digest == report_ref.sha256
        assert ledger.artifact_refs[w.REPORT_REL].sha256 == report_ref.sha256
        assert ledger.artifact_refs[w.MERGED_REL].sha256 == merged_ref.sha256

        series = rd / "eval_attempts" / str(eval_run_id)
        # P5：adapter 在 SUCCEEDED 后自动发布 selected pointer；指针 digest 链
        # 必须与恢复保留的 report/merge refs 完全一致（§3.5 / §1.2-2）。
        pointer = a.SelectedPointer(series).read()
        assert pointer is not None
        assert pointer.report_digest == ledger.report_digest
        assert pointer.final_ledger_digest == ledger.digest()
        assert pointer.input_manifest_digest == frozen.digest
        assert pointer.revision >= 1  # 首次 run 已发布 rev 0，resume 后 CAS bump

    def test_report_missing_resume_preserves_merged_ref_and_rehash(self, tmp_path):
        """(d) 仅缺 report：恢复重渲染 report 仍引用真实 merged bundle，
        ledger 保留 merged/report refs。"""
        eval_run_id, attempt_id = uuid4(), uuid4()
        rd, args = _make_nonterminal_attempt(
            tmp_path,
            llm_judge_required=False,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        store = _attempt_store(rd, eval_run_id, attempt_id)
        merged_ref = w._rehash_ref(store, w.MERGED_REL, "merge")
        (store.root / w.REPORT_REL).unlink()

        code = w.run_from_results_dir(args)
        assert code == 0
        ledger = store.read_final_ledger()
        assert ledger is not None
        assert ledger.artifact_refs[w.MERGED_REL].sha256 == merged_ref.sha256
        report = json.loads(store.read_bytes(w.REPORT_REL))
        assert report["merged_bundle"] == w.MERGED_REL
        assert store.exists(w.REPORT_REL)


# ─────────────────────────────────────────────────────────────────────────────
# P3-M2：AttemptLease fencing 必须接入 P3 写路径（stale lease → typed failure）
# ─────────────────────────────────────────────────────────────────────────────


class TestFencing:
    def test_stale_lease_zero_model_audit_job_result_ledger_writes(self, tmp_path):
        manifest = _manifest(llm_judge_required=True)
        series_root = tmp_path / "series" / str(manifest.manifest.eval_run_id)
        lease1 = a.AttemptLease.acquire(series_root, owner_id="a")
        lease1.release()
        lease2 = a.AttemptLease.acquire(series_root, owner_id="b")  # 使 lease1 过期
        try:
            stats = {"constructed": 0}

            def factory():
                stats["constructed"] += 1
                raise AssertionError("runner must not be constructed with stale lease")

            store = a.ArtifactStore(
                tmp_path / "attempt" / str(manifest.manifest.attempt_id)
            )
            rt = w.EvalRuntime(
                manifest,
                store,
                store.audit_journal(),
                lease=lease1,
                runner_factory=factory,
                episode=_episode(),
                grader_fn=lambda: ([], []),
            )
            outcome = w.run_eval_workflow(rt)
            assert outcome.status is c.WorkflowStatus.FAILED
            assert outcome.error and "fencing" in outcome.error
            assert stats["constructed"] == 0
            assert store.audit_journal().event_count() == 0
            assert not (store.root / "score_jobs").exists()
            assert not (store.root / "score_results").exists()
            assert not store.exists(a.ArtifactStore.MANIFEST_REL)
            assert not store.exists(a.ArtifactStore.LEDGER_REL)
        finally:
            lease2.release()

    def test_stale_lease_released_also_fenced(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        series_root = tmp_path / "series" / str(manifest.manifest.eval_run_id)
        lease = a.AttemptLease.acquire(series_root, owner_id="a")
        lease.release()  # 已释放
        store = a.ArtifactStore(
            tmp_path / "attempt" / str(manifest.manifest.attempt_id)
        )
        rt = w.EvalRuntime(
            manifest,
            store,
            store.audit_journal(),
            lease=lease,
            episode=_episode(),
            grader_fn=lambda: ([], []),
        )
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.FAILED
        assert outcome.error and "fencing" in outcome.error
        assert store.audit_journal().event_count() == 0

    def test_valid_lease_positive_path_passes(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        series_root = tmp_path / "series" / str(manifest.manifest.eval_run_id)
        lease = a.AttemptLease.acquire(series_root, owner_id="a")
        try:
            store = a.ArtifactStore(
                tmp_path / "attempt" / str(manifest.manifest.attempt_id)
            )
            rt = w.EvalRuntime(
                manifest,
                store,
                store.audit_journal(),
                lease=lease,
                episode=_episode(),
                grader_fn=lambda: ([], []),
            )
            outcome = w.run_eval_workflow(rt)
            assert outcome.status is c.WorkflowStatus.SUCCEEDED
            assert (
                store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED
            )
        finally:
            lease.release()


# ─────────────────────────────────────────────────────────────────────────────
# P3-M3：持久 thread id + checkpoint continuation（interrupt → 新 runtime 续跑）
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistentThreadAndCheckpointResume:
    def test_thread_id_is_persistent_per_attempt(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt1, _ = _runtime(tmp_path, manifest)
        rt2, _ = _runtime(tmp_path, manifest)
        assert rt1.thread_id == rt2.thread_id
        assert str(manifest.manifest.eval_run_id) in rt1.thread_id
        assert str(manifest.manifest.attempt_id) in rt1.thread_id

    def test_interrupt_then_new_runtime_auto_resumes_same_thread(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt1, store = _runtime(tmp_path, manifest)
        rt1.probe_interrupt = True

        async def first():
            await rt1.open_checkpointer()
            try:
                app = w.build_eval_workflow(rt1)
                return await app.ainvoke(
                    w._initial_state(rt1),
                    config={"configurable": {"thread_id": rt1.thread_id}},
                )
            finally:
                await rt1.close_checkpointer()

        interrupted = asyncio.run(first())
        assert "__interrupt__" in interrupted
        assert interrupted["phase"] is c.WorkflowStatus.MANIFEST_FROZEN

        # 新 runtime（同 attempt → 同 thread）：run_eval_workflow 自动
        # 探测 pending interrupt 并以 Command(resume) 续跑。
        rt2, _ = _runtime(tmp_path, manifest)
        outcome = w.run_eval_workflow(rt2)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        events = store.audit_journal().read_all()
        assert len([e for e in events if e["kind"] == "manifest_frozen"]) == 1
        assert store.exists(w.EVIDENCE_BUNDLE_REL)
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED

    def test_explicit_resume_command_seam(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt1, _ = _runtime(tmp_path, manifest)
        rt1.probe_interrupt = True

        async def first():
            await rt1.open_checkpointer()
            try:
                app = w.build_eval_workflow(rt1)
                return await app.ainvoke(
                    w._initial_state(rt1),
                    config={"configurable": {"thread_id": rt1.thread_id}},
                )
            finally:
                await rt1.close_checkpointer()

        interrupted = asyncio.run(first())
        assert "__interrupt__" in interrupted
        rt2, _ = _runtime(tmp_path, manifest)
        outcome = w.run_eval_workflow(rt2, resume=True)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED

    def test_real_adapter_interrupt_then_resume_same_attempt(self, tmp_path):
        """真实 adapter 路径（build_runtime_for_results_dir）：interrupt →
        新 lease/新 runtime → 同 attempt/thread checkpoint continuation。"""
        rd = _make_results_dir(tmp_path)
        eval_run_id, attempt_id = uuid4(), uuid4()
        args = SimpleNamespace(
            results_dir=str(rd),
            no_llm_judge=False,
            judge_sample_steps=20,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        series_root = rd / "eval_attempts" / str(eval_run_id)
        attempt_root = _attempt_root(rd, eval_run_id, attempt_id)

        lease = a.AttemptLease.acquire(series_root, owner_id="cli")
        try:
            runtime1 = w.build_runtime_for_results_dir(
                rd,
                args,
                eval_run_id=eval_run_id,
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                series_root=series_root,
                lease=lease,
            )
            runtime1.runner_factory = r.make_fake_runner_factory(runtime1.manifest, {})[
                0
            ]
            runtime1.probe_interrupt = True
            interrupted = asyncio.run(w.ainvoke_workflow(runtime1))
            assert "__interrupt__" in interrupted
        finally:
            lease.release()

        lease2 = a.AttemptLease.acquire(series_root, owner_id="cli")
        try:
            runtime2 = w.build_runtime_for_results_dir(
                rd,
                args,
                eval_run_id=eval_run_id,
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                series_root=series_root,
                lease=lease2,
            )
            runtime2.runner_factory = r.make_fake_runner_factory(runtime2.manifest, {})[
                0
            ]
            outcome = w.run_eval_workflow(runtime2)
            assert outcome.status is c.WorkflowStatus.SUCCEEDED
            store = _attempt_store(rd, eval_run_id, attempt_id)
            assert (
                store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED
            )
        finally:
            lease2.release()


# ─────────────────────────────────────────────────────────────────────────────
# P3-M5：checkpoint.sqlite 的 symlink / hardlink 逃逸必须在 open 前拒绝
# ─────────────────────────────────────────────────────────────────────────────


class TestCheckpointContainment:
    def test_checkpoint_symlink_escape_rejected_before_open(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        outside = tmp_path / "outside.sqlite"
        outside.write_bytes(b"precious")
        os.symlink(outside, store.root / "checkpoint.sqlite")
        with pytest.raises(a.ContainmentError):
            asyncio.run(rt.open_checkpointer())
        assert outside.read_bytes() == b"precious"
        # 未被 sqlite 替换成普通文件：仍是 symlink，且外部目标未被触碰。
        assert (store.root / "checkpoint.sqlite").is_symlink()

    def test_checkpoint_hardlink_escape_rejected_before_open(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        outside = tmp_path / "outside.sqlite"
        outside.write_bytes(b"precious")
        os.link(outside, store.root / "checkpoint.sqlite")
        with pytest.raises(a.ContainmentError):
            asyncio.run(rt.open_checkpointer())
        assert outside.read_bytes() == b"precious"

    def test_checkpoint_normal_path_recoverable(self, tmp_path):
        manifest = _manifest(llm_judge_required=False)
        rt, store = _runtime(tmp_path, manifest)
        outcome = w.run_eval_workflow(rt)
        assert outcome.status is c.WorkflowStatus.SUCCEEDED
        assert (store.root / "checkpoint.sqlite").exists()
