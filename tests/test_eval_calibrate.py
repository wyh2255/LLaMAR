"""F4 calibrate.py 聚焦测试（judge 家族化设计 §7 / §8.1 V-F4）。

覆盖：
- 确定性分层抽样：seed=42 可复现；20 observation（s1=2、其余 3、agent 均衡 +
  step 均匀）+ 8 dispatch（每 cell ≥1、一个 cell 2）+ 盲测恰好 6（obs 4 + dsp 2）；
  数据不足 → `CalibrationSamplingError`（绝不编造 / 改总数）；
- `calibration_manifest.json`：counts / strata / blind / claim_id / dispatch_ref /
  source_digest / input_digest；
- 核实清单（packets）：evidence 摘录 + judge 维度输出 + 家族派发 + 检索审计；
  盲测隐藏全部 judge 输出与结论但保留证据；reveal 必须标注先行；
- 驱动：fake runner 驱动 attempt-v2 workflow（v2 rubric only），sample→job 映射、
  judge_outputs 落盘；无显式模型 / runner → typed config error（无隐式 LLM）；
- 标注校验：accept|correct|unknown / correct 必须带 label / 其余禁 label /
  未知样本 / 重复 / 盲测完整性与顺序 / reveal 状态；
- 冻结指标公式与失败用例：盲测 ≥5/6、纠错 ≤5/22、abstain 诚实 ≥50%
  （含 vacuous）、8 维度每维 ≥2 非 abstain；verdict 仅诊断。

只用合成 cohort + fake runner，零真实 LLM / 网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sar_orch.eval import calibrate as cal
from sar_orch.eval import contracts as c
from sar_orch.eval import workflow as w
from sar_orch.eval.agent.runner import RunnerOutcome, RunnerStatus

OBS_DIMS = cal.OBSERVATION_DIMENSIONS
DSP_DIMS = cal.DISPATCH_DIMENSIONS

AGENT_CSV_HEADER = (
    "Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,"
    "Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType"
)

_CELL_AGENTS = {
    "s1_a2": ["Alice", "Bob"],
    "s2_a2": ["Alice", "Bob"],
    "s3_a2": ["Alice", "Bob"],
    "s3_a4": ["Alice", "Bob", "Charlie", "David"],
    "s4_a2": ["Alice", "Bob"],
    "s5_a2": ["Alice", "Bob"],
    "s5_a4": ["Alice", "Bob", "Charlie", "David"],
}


# ─────────────────────────────────────────────────────────────────────────────
# 合成 cohort：7 cell，各 cell 数据满足冻结抽样计划
# ─────────────────────────────────────────────────────────────────────────────


def _write_cell(cohort: Path, cell_id: str) -> Path:
    run = cohort / cal.P6_CELLS[cell_id]
    run.mkdir(parents=True, exist_ok=True)
    scene = int(cell_id.split("_a")[0][1:])
    agents = _CELL_AGENTS[cell_id]
    (run / "metadata.json").write_text(
        json.dumps(
            {
                "scene": scene,
                "seed": 42,
                "agent_count": len(agents),
                "agent_names": agents,
                "max_steps": 30,
                "model": "fake",
                "provider": "fake",
                "code_commit": "x",
                "git_dirty": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    traj = [
        (
            "Step,Coverage,TransportRate,Actions,Successes,Finished,EndReason,"
            "TimeoutAgents,CompletedSubtasksDelta"
        )
    ]
    for step in range(1, 31):
        traj.append(
            f'"{step}","0.1","0.0","[\\"NoOp\\"]","[true]","false","","[]","[]"'
        )
    (run / "trajectory.csv").write_text("\n".join(traj) + "\n", encoding="utf-8")

    agent_lines = [AGENT_CSV_HEADER]
    call = 0
    for step in range(1, 31, 2):  # 每 cell 每 agent 在奇数 step 各有一条 claim
        for agent in agents:
            claim = (
                '{"object_type": "fire", "name": "FireA", '
                f'"position": [1, {step % 5}, 0], "step": {step}}}'
            )
            obs = (
                f"Directly around me, co-ordinates: (1,{step % 5},0). "
                f"I am holding {{}}. Names: [FireA]. confidence=1.0"
            )
            agent_lines.append(
                f'"{step}","{agent}","report_observation",'
                f'"{claim}","report_observation()",'
                f'"{obs}","","{{""note"": ""reported FireA step {step}""}}","",'
                f'"r1","c{call}","tool_result","12",""'
            )
            call += 1
    (run / "agent_interactions.csv").write_text(
        "\n".join(agent_lines) + "\n", encoding="utf-8"
    )

    # router：raw Step 0-based → episode dispatch step = raw+1。
    router = ["Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType"]
    for idx, step in enumerate([1, 5, 9, 13, 17, 21, 25]):
        for agent in agents:
            router.append(
                f'"{step - 1}","Investigate area at step {step}.","{agent}",'
                f'"r1","c{idx}","w{idx}","assign_task"'
            )
    (run / "router_interactions.csv").write_text(
        "\n".join(router) + "\n", encoding="utf-8"
    )

    (run / "subtasks.csv").write_text(
        "RunID,Step,SubtaskID,Status,AssignedTo,Subtask,CreatedAt,UpdatedAt,"
        "FailureClass,Details\n"
        '"r1","1","d1","assigned","Alice","Explore.","","","",""\n',
        encoding="utf-8",
    )
    (run / "summary.csv").write_text(
        'RunID,Coverage,TransportRate,Finished\n"r1","0.1","0.0","false"\n',
        encoding="utf-8",
    )
    (run / "token_usage.csv").write_text(
        "Step,Agent,PromptTokens,CompletionTokens,TotalTokens\n", encoding="utf-8"
    )
    (run / "semantic_map.jsonl").write_text(
        json.dumps(
            {
                "event_type": "observation_ingested",
                "observation": {
                    "reporter": agents[0],
                    "step": 1,
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [1, 1, 0],
                },
                "object": {"object_type": "fire", "name": "FireA"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "map_summary.jsonl").write_text(
        json.dumps(
            {
                "env_step": 1,
                "status": "success",
                "summary": "FireA reported.",
                "trigger_reasons": ["periodic"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "supervision").mkdir(exist_ok=True)
    return run


def _build_cohort(tmp_path: Path) -> Path:
    cohort = tmp_path / "cohort"
    for cell_id in cal.P6_CELLS:
        _write_cell(cohort, cell_id)
    return cohort


# ─────────────────────────────────────────────────────────────────────────────
# 1. 确定性抽样 + 清单 counts/strata
# ─────────────────────────────────────────────────────────────────────────────


class TestSampling:
    def test_deterministic_and_reproducible(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m1 = cal.sample_cohort(cohort)
        m2 = cal.sample_cohort(cohort)
        assert m1.digest() == m2.digest()
        assert [s.sample_id for s in m1.samples] == [s.sample_id for s in m2.samples]

    def test_totals_and_strata(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        assert len(m.samples) == 28
        obs = [s for s in m.samples if s.family is c.SampleTargetType.OBSERVATION]
        dsp = [s for s in m.samples if s.family is c.SampleTargetType.DISPATCH]
        assert len(obs) == 20 and len(dsp) == 8
        from collections import Counter

        per_cell_obs = Counter(s.cell for s in obs)
        assert per_cell_obs["s1_a2"] == 2
        for cell in ("s2_a2", "s3_a2", "s3_a4", "s4_a2", "s5_a2", "s5_a4"):
            assert per_cell_obs[cell] == 3
        # dispatch：每 cell ≥1，恰好一个 cell 2 条。
        per_cell_dsp = Counter(s.cell for s in dsp)
        assert all(v >= 1 for v in per_cell_dsp.values())
        assert sum(per_cell_dsp.values()) == 8
        assert sorted(v for v in per_cell_dsp.values()) == [1, 1, 1, 1, 1, 1, 2]

    def test_observation_agent_balanced_and_step_spread(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        obs = [s for s in m.samples if s.family is c.SampleTargetType.OBSERVATION]
        for cell in {s.cell for s in obs}:
            cell_samples = [s for s in obs if s.cell == cell]
            agents_used = {s.agent for s in cell_samples}
            n_agents = len(_CELL_AGENTS[cell])
            # 每 cell 至多一个 agent 缺席（4-agent cell target 3 → 1 agent 缺席）。
            assert n_agents - len(agents_used) <= 1
            # step 尽量互不重复（<= n_agents 的重复容忍）。
            steps = [s.step for s in cell_samples]
            assert len(set(steps)) >= len(cell_samples) - len(agents_used)

    def test_blind_subset_exactly_six_with_fixed_seed(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        blind = m.blind_sample_ids()
        assert len(blind) == 6
        obs_blind = [
            s
            for s in m.samples
            if s.blind and s.family is c.SampleTargetType.OBSERVATION
        ]
        dsp_blind = [
            s for s in m.samples if s.blind and s.family is c.SampleTargetType.DISPATCH
        ]
        assert len(obs_blind) == 4 and len(dsp_blind) == 2
        # 固定 seed=42 → 盲测子集确定（与设计 §7.2 一致，钉住当前抽签）。
        assert set(blind) == {
            "obs-0001",
            "obs-0004",
            "obs-0008",
            "obs-0009",
            "dsp-0002",
            "dsp-0004",
        }
        assert all(s.blind == (s.sample_id in blind) for s in m.samples)

    def test_sample_identity_contract(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        for s in m.samples:
            if s.family is c.SampleTargetType.OBSERVATION:
                assert s.claim_id == f"{s.step}:{s.agent}:0"
                assert s.dispatch_ref is None
                assert s.agent is not None
            else:
                assert s.dispatch_ref == f"step:{s.step}"
                assert s.agent is None and s.claim_id is None
            assert s.input_digest and s.source_digest

    def test_manifest_json_fields(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        out = tmp_path / "out"
        path = cal.write_manifest(m, out / cal.MANIFEST_FILENAME)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["seed"] == 42
        assert raw["totals"]["total"] == 28
        assert raw["totals"]["blind"] == 6
        first = raw["samples"][0]
        for key in (
            "sample_id",
            "family",
            "cell",
            "step",
            "agent",
            "claim_id",
            "dispatch_ref",
            "blind",
            "source_digest",
            "input_digest",
        ):
            assert key in first
        # manifest_digest 可复现。
        m2 = cal.sample_cohort(cohort)
        assert cal.write_manifest(m2, out / "m2.json") and True
        assert (
            json.loads((out / cal.MANIFEST_FILENAME).read_text("utf-8"))[
                "manifest_digest"
            ]
            == json.loads((out / "m2.json").read_text("utf-8"))["manifest_digest"]
        )

    def test_manifest_roundtrip_and_digest_check(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        m = cal.sample_cohort(cohort)
        out = tmp_path / "out"
        path = cal.write_manifest(m, out / cal.MANIFEST_FILENAME)
        loaded = cal.load_manifest(path)
        assert loaded.digest() == m.digest()
        assert [s.sample_id for s in loaded.samples] == [s.sample_id for s in m.samples]
        # 篡改 → digest 校验失败。
        raw = json.loads(path.read_text("utf-8"))
        raw["samples"][0]["step"] += 1
        tampered = out / "tampered.json"
        tampered.write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
        with pytest.raises(cal.CalibrationError, match="manifest_digest mismatch"):
            cal.load_manifest(tampered)

    def test_sampling_error_when_cell_missing(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        (cohort / cal.P6_CELLS["s5_a4"]).rename(tmp_path / "gone")
        with pytest.raises(cal.CalibrationSamplingError, match="missing"):
            cal.sample_cohort(cohort)

    def test_sampling_error_when_claims_insufficient(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        # 清空 s2_a2 的 claim → 目标 3 无法满足 → typed error，不编造。
        run = cohort / cal.P6_CELLS["s2_a2"]
        (run / "agent_interactions.csv").write_text(
            AGENT_CSV_HEADER + "\n", encoding="utf-8"
        )
        with pytest.raises(cal.CalibrationSamplingError, match="no report_observation"):
            cal.sample_cohort(cohort)

    def test_sampling_error_when_agent_quota_exceeded(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        # 只留 Alice 一条 claim → Bob 配额 1 无法满足 → typed error。
        run = cohort / cal.P6_CELLS["s2_a2"]
        (run / "agent_interactions.csv").write_text(
            AGENT_CSV_HEADER
            + '\n"1","Alice","report_observation","{""object_type"": ""fire"", '
            '"name"": ""FireA""}","report_observation()",'
            '"co-ordinates: (1,1,0).","","","","r1","c0","tool_result","12",""\n',
            encoding="utf-8",
        )
        with pytest.raises(cal.CalibrationSamplingError, match="not supported"):
            cal.sample_cohort(cohort)

    def test_sampling_error_when_cell_has_no_dispatch(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        run = cohort / cal.P6_CELLS["s4_a2"]
        (run / "router_interactions.csv").write_text(
            "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType\n",
            encoding="utf-8",
        )
        with pytest.raises(cal.CalibrationSamplingError, match="no dispatch steps"):
            cal.sample_cohort(cohort)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 核实清单：盲测隐藏 / reveal
# ─────────────────────────────────────────────────────────────────────────────


def _full_judge_outputs(manifest: cal.CalibrationManifest) -> dict[str, dict]:
    outputs: dict[str, dict] = {}
    for s in manifest.samples:
        dims = OBS_DIMS if s.family is c.SampleTargetType.OBSERVATION else DSP_DIMS
        draft = {
            "role": (
                "observation_score_judge"
                if s.family is c.SampleTargetType.OBSERVATION
                else "dispatch_score_judge"
            ),
            "dimensions": {d: 0.9 for d in dims},
            "dimension_unknown_reasons": {},
            "evidence": [
                {
                    "ref": {
                        "path": f"evidence/job-scoped/job1/{d}.json",
                        "sha256": "a" * 64,
                        "bytes": 12,
                        "media_type": "application/json",
                        "producer": "test",
                    },
                    "claim_type": "score",
                    "digest": "a" * 64,
                }
                for d in dims
            ],
            "model_used": "fake",
        }
        outputs[s.sample_id] = {
            "job_id": "job1",
            "job_status": "succeeded",
            "result_status": "succeeded",
            "evidence_excerpt": (
                {"claims": [{"claim": "fire FireA", "system_response": "ok"}]}
                if s.family is c.SampleTargetType.OBSERVATION
                else {
                    "dispatch_records": [
                        {
                            "Step": s.step,
                            "Subtask": "go",
                            "AssignedTo": "Alice",
                            "EventType": "assign_task",
                        }
                    ],
                    "step_budget": {
                        "step": s.step,
                        "max_steps": 30,
                        "remaining": 30 - s.step,
                    },
                }
            ),
            "draft": draft,
            "conclusion": cal.judge_conclusion(draft),
            "family_dispatch": [
                {
                    "dimension": d,
                    "instruction": f"judge {d} using the bundle",
                    "status": "accepted",
                }
                for d in dims
            ],
            "retrieval_audit": [
                {
                    "tool": "read_dispatch_history",
                    "params": {"agent": "Alice"},
                    "returned_refs": ["router_interactions.csv"],
                    "bytes": 100,
                    "truncated": False,
                }
            ],
        }
    return outputs


def _write_judge_outputs(output_dir: Path, outputs: dict[str, dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / cal.JUDGE_OUTPUTS_FILENAME).write_text(
        json.dumps({"schema_version": 1, "outputs": outputs}, ensure_ascii=False),
        "utf-8",
    )


class TestReviewPackets:
    def _setup(self, tmp_path) -> tuple[cal.CalibrationManifest, Path]:
        cohort = _build_cohort(tmp_path)
        manifest = cal.sample_cohort(cohort)
        out = tmp_path / "out"
        _write_judge_outputs(out, _full_judge_outputs(manifest))
        return manifest, out

    def test_packets_include_evidence_and_judge_output(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        packets = cal.build_review_packets(manifest, out)
        assert len(packets) == 28
        non_blind = next(p for p in packets if not p["blind"])
        assert non_blind["judge_output_hidden"] is False
        assert non_blind["judge"]["conclusion"]["state"] == "clean"
        assert non_blind["family_dispatch"]
        assert non_blind["retrieval_audit"]
        assert non_blind["evidence_excerpt"]
        assert (out / cal.PACKETS_DIR / f"{non_blind['sample_id']}.md").exists()
        md = (out / cal.PACKETS_DIR / f"{non_blind['sample_id']}.md").read_text("utf-8")
        assert "## Judge Output" in md
        assert "existence_grounding" in md or "dispatch_completeness" in md

    def test_blind_packet_hides_all_judge_output_keeps_evidence(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        packets = cal.build_review_packets(manifest, out)
        blind = next(p for p in packets if p["blind"])
        assert blind["judge_output_hidden"] is True
        assert blind["judge"] is None
        assert blind["family_dispatch"] == []
        assert blind["retrieval_audit"] == []
        # 证据保留。
        assert blind["evidence_excerpt"]
        md = (out / cal.PACKETS_DIR / f"{blind['sample_id']}.md").read_text("utf-8")
        assert "HIDDEN (blind)" in md
        assert "Judge Output — HIDDEN" in md
        assert "## Evidence" in md

    def test_reveal_requires_annotation_first(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        blind_id = manifest.blind_sample_ids()[0]
        # 未标注 → 拒绝 reveal（blind-first ordering）。
        with pytest.raises(cal.AnnotationError, match="annotated before reveal"):
            cal.reveal_blind(manifest, out, blind_id)

    def test_reveal_after_annotation(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        blind_id = manifest.blind_sample_ids()[0]
        cal.import_annotations(
            manifest,
            out,
            [
                {"sample_id": sid, "verdict": "accept"}
                for sid in manifest.blind_sample_ids()
            ],
        )
        packet = cal.reveal_blind(manifest, out, blind_id)
        assert packet["judge"]["conclusion"]["state"] == "clean"
        assert packet["annotation"]["verdict"] == "accept"
        assert (out / cal.REVEAL_DIR / f"{blind_id}.json").exists()
        assert (out / cal.REVEAL_DIR / f"{blind_id}.md").exists()

    def test_reveal_rejects_non_blind(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        non_blind = next(s.sample_id for s in manifest.samples if not s.blind)
        with pytest.raises(cal.AnnotationError, match="not a blind item"):
            cal.reveal_blind(manifest, out, non_blind)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 驱动边界：fake runner 驱动 attempt-v2 workflow
# ─────────────────────────────────────────────────────────────────────────────


class _CalFakeRunner:
    """v2 fake runner：全维度 0.9，携带 evidence refs（fake，不物化文件）。"""

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
        evidence = []
        for dim in rubric.dimensions:
            sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
            ref = c.ArtifactRef(
                path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
                sha256=sha,
                bytes=len(sha),
                media_type="application/json",
                producer="cal-fake",
            )
            evidence.append(
                c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)
            )
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=invocation_id,
            dimensions={d: 0.9 for d in rubric.dimensions},
            evidence=evidence,
            model_used="cal-fake-runner",
        )
        return RunnerOutcome(
            status=RunnerStatus.SUCCEEDED,
            draft=draft,
            usage=c.UsageSnapshot(
                prompt_tokens=10, completion_tokens=5, total_tokens=15
            ),
            latency_ms=3,
        )


class TestDrive:
    def _driver(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        manifest = cal.sample_cohort(cohort)
        out = tmp_path / "out"
        driver = cal.CalibrationDriver(
            cohort,
            out,
            manifest,
            runner_factory_factory=lambda rt: lambda: _CalFakeRunner(rt.manifest),
            grader_fn=lambda: ([], []),
        )
        return driver, manifest, out

    def test_driver_requires_explicit_model_or_runner(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        manifest = cal.sample_cohort(cohort)
        with pytest.raises(cal.CalibrationConfigError, match="explicit model_config"):
            cal.CalibrationDriver(cohort, tmp_path / "out", manifest)

    def test_driver_rejects_both_model_and_runner(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        manifest = cal.sample_cohort(cohort)
        with pytest.raises(cal.CalibrationConfigError, match="not both"):
            cal.CalibrationDriver(
                cohort,
                tmp_path / "out",
                manifest,
                model_config=cal.ModelConfig(model="x"),
                runner_factory_factory=lambda rt: lambda: None,
            )

    def test_model_builder_rejects_missing_model_or_key(self):
        with pytest.raises(cal.CalibrationConfigError, match="model"):
            cal.build_chat_model(cal.ModelConfig(model="", api_key="configured"))
        with pytest.raises(cal.CalibrationConfigError, match="api_key"):
            cal.build_chat_model(cal.ModelConfig(model="configured"))

    def test_fake_drive_succeeds_and_records_all_samples(self, tmp_path):
        driver, manifest, out = self._driver(tmp_path)
        state = driver.drive()
        assert set(state["cells"]) == set(cal.P6_CELLS)
        for info in state["cells"].values():
            assert info["outcome"] == "succeeded"
            assert (
                out / "attempts" / info["eval_run_id"] / "attempts" / info["attempt_id"]
            ).is_dir()
        # 每个样本都有 judge output（draft 已评分）。
        outputs = cal._load_outputs(out)
        assert set(outputs.keys()) == {s.sample_id for s in manifest.samples}
        for doc in outputs.values():
            assert doc["draft"]["dimensions"]
            assert doc["conclusion"]["state"] == "clean"
        # sample→job 映射落盘。
        jobs_file = json.loads((out / cal.SAMPLE_JOBS_FILENAME).read_text("utf-8"))
        assert jobs_file["seed"] == 42

    def test_drive_v2_rubrics_only(self, tmp_path):
        driver, _manifest, out = self._driver(tmp_path)
        driver.drive()
        jobs_file = json.loads((out / cal.SAMPLE_JOBS_FILENAME).read_text("utf-8"))
        for info in jobs_file["cells"].values():
            attempt_root = Path(info["attempt_root"])
            store = __import__(
                "sar_orch.eval.artifacts", fromlist=["ArtifactStore"]
            ).ArtifactStore(attempt_root)
            frozen = store.read_input_manifest()
            rubric_ids = {r.rubric_id for r in frozen.manifest.rubrics}
            assert rubric_ids <= {"dispatch-v2", "observation-v2"}

    def test_drive_workflow_artifacts_persisted(self, tmp_path):
        driver, _manifest, out = self._driver(tmp_path)
        driver.drive()
        jobs_file = json.loads((out / cal.SAMPLE_JOBS_FILENAME).read_text("utf-8"))
        info = next(iter(jobs_file["cells"].values()))
        store = __import__(
            "sar_orch.eval.artifacts", fromlist=["ArtifactStore"]
        ).ArtifactStore(Path(info["attempt_root"]))
        assert store.exists(w.MERGED_REL)
        assert store.exists("audit/final_ledger.json")
        assert store.exists("reports/eval_report.json")

    def test_drive_reuses_terminal_cells_without_new_runner_calls(self, tmp_path):
        driver, manifest, out = self._driver(tmp_path)
        driver.drive()

        def _unexpected_runner(_runtime):
            raise AssertionError("terminal calibration cell must not build a runner")

        resumed = cal.CalibrationDriver(
            driver.cohort_root,
            out,
            manifest,
            runner_factory_factory=_unexpected_runner,
            grader_fn=lambda: ([], []),
        )
        state = resumed.drive()
        assert all(info["outcome"] == "succeeded" for info in state["cells"].values())


# ─────────────────────────────────────────────────────────────────────────────
# 4. 标注校验
# ─────────────────────────────────────────────────────────────────────────────


class TestAnnotations:
    def _setup(self, tmp_path):
        cohort = _build_cohort(tmp_path)
        manifest = cal.sample_cohort(cohort)
        out = tmp_path / "out"
        _write_judge_outputs(out, _full_judge_outputs(manifest))
        return manifest, out

    def _blind_annotations(self, manifest, verdict="accept"):
        return [
            {"sample_id": sid, "verdict": verdict}
            for sid in manifest.blind_sample_ids()
        ]

    def test_valid_full_annotations(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        anns = self._blind_annotations(manifest)
        for s in manifest.samples:
            if not s.blind:
                anns.append({"sample_id": s.sample_id, "verdict": "accept"})
        cal.validate_annotations(manifest, anns)
        payload = cal.import_annotations(manifest, out, anns)
        assert len(payload["annotations"]) == 28

    def test_unknown_verdict_rejected(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        with pytest.raises(cal.AnnotationError, match="verdict"):
            cal.validate_annotations(
                manifest,
                [{"sample_id": manifest.samples[0].sample_id, "verdict": "agree"}],
            )

    def test_correct_requires_label(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        sid = next(s.sample_id for s in manifest.samples if not s.blind)
        with pytest.raises(cal.AnnotationError, match="correct_label"):
            cal.validate_annotations(
                manifest, [{"sample_id": sid, "verdict": "correct"}]
            )

    def test_non_correct_forbids_label(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        sid = next(s.sample_id for s in manifest.samples if not s.blind)
        with pytest.raises(cal.AnnotationError, match="must not carry"):
            cal.validate_annotations(
                manifest,
                [{"sample_id": sid, "verdict": "accept", "correct_label": "x"}],
            )

    def test_unknown_sample_id_rejected(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        with pytest.raises(cal.AnnotationError, match="unknown sample_id"):
            cal.validate_annotations(
                manifest, [{"sample_id": "nope", "verdict": "accept"}]
            )

    def test_duplicate_rejected(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        sid = manifest.samples[0].sample_id
        with pytest.raises(cal.AnnotationError, match="duplicate"):
            cal.validate_annotations(
                manifest,
                [
                    {"sample_id": sid, "verdict": "accept"},
                    {"sample_id": sid, "verdict": "unknown"},
                ],
            )

    def test_blind_incomplete_rejected(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        anns = self._blind_annotations(manifest)[:-1]
        with pytest.raises(cal.AnnotationError, match="blind samples must all"):
            cal.validate_annotations(manifest, anns)

    def test_non_blind_incomplete_rejected(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        anns = self._blind_annotations(manifest)
        with pytest.raises(cal.AnnotationError, match="all calibration samples"):
            cal.validate_annotations(manifest, anns, require_all=True)

    def test_blind_order_enforced(self, tmp_path):
        manifest, _ = self._setup(tmp_path)
        blind_ids = manifest.blind_sample_ids()
        anns = [
            {"sample_id": sid, "verdict": "accept"}
            for sid in [blind_ids[1], blind_ids[0], *blind_ids[2:]]
        ]
        with pytest.raises(cal.AnnotationError, match="blind order"):
            cal.validate_annotations(manifest, anns)

    def test_correct_label_becomes_ground_truth(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        sid = next(s.sample_id for s in manifest.samples if not s.blind)
        anns = self._blind_annotations(manifest)
        anns.append(
            {
                "sample_id": sid,
                "verdict": "correct",
                "correct_label": "fire name was invented",
            }
        )
        for s in manifest.samples:
            if not s.blind and s.sample_id != sid:
                anns.append({"sample_id": s.sample_id, "verdict": "accept"})
        cal.import_annotations(manifest, out, anns)
        loaded = cal._load_annotations(out)
        assert loaded[sid]["correct_label"] == "fire name was invented"

    def test_reveal_state_conflict_on_modified_annotation(self, tmp_path):
        manifest, out = self._setup(tmp_path)
        blind_id = manifest.blind_sample_ids()[0]
        first = [
            {"sample_id": sid, "verdict": "accept"}
            for sid in manifest.blind_sample_ids()
        ]
        cal.import_annotations(manifest, out, first)
        cal.reveal_blind(manifest, out, blind_id)
        # 修改 blind 标注（accept → unknown）再导入 → reveal 状态冲突。
        changed = [
            {"sample_id": sid, "verdict": "unknown" if sid == blind_id else "accept"}
            for sid in manifest.blind_sample_ids()
        ]
        with pytest.raises(cal.AnnotationError, match="already revealed"):
            cal.import_annotations(manifest, out, changed)


# ─────────────────────────────────────────────────────────────────────────────
# 5. 冻结统计公式与失败用例
# ─────────────────────────────────────────────────────────────────────────────


def _draft(sample, *, dims, flagged=()):
    return {
        "role": "x",
        "dimensions": {d: (0.1 if d in flagged else 0.9) for d in dims},
        "dimension_unknown_reasons": {},
        "evidence": [],
        "model_used": "test",
    }


def _stat_outputs(manifest, *, abstain_ids=(), flagged_ids=()):
    outputs = _full_judge_outputs(manifest)
    for sample_id in abstain_ids:
        s = manifest.sample(sample_id)
        dims = OBS_DIMS if s.family is c.SampleTargetType.OBSERVATION else DSP_DIMS
        draft = _abstain_draft(dims)
        outputs[sample_id]["draft"] = draft
        outputs[sample_id]["conclusion"] = cal.judge_conclusion(draft)
    for sample_id in flagged_ids:
        s = manifest.sample(sample_id)
        dims = OBS_DIMS if s.family is c.SampleTargetType.OBSERVATION else DSP_DIMS
        draft = _draft(s, dims=dims, flagged=tuple(dims[:2]))
        outputs[sample_id]["draft"] = draft
        outputs[sample_id]["conclusion"] = cal.judge_conclusion(draft)
    return outputs


def _abstain_draft(dims):
    return {
        "role": "x",
        "dimensions": {},
        "dimension_unknown_reasons": {d: "no evidence to judge" for d in dims},
        "evidence": [],
        "model_used": "test",
    }


def _all_accept_annotations(
    manifest, *, blind_verdicts=None, extra_correct=(), extra_unknown=()
):
    """默认全部 accept；盲测用 blind_verdicts（dict sid→verdict）覆盖。"""
    blind_verdicts = blind_verdicts or {}
    anns = []
    for s in manifest.samples:
        if s.blind:
            verdict = blind_verdicts.get(s.sample_id, "accept")
        elif s.sample_id in extra_correct:
            verdict = "correct"
        elif s.sample_id in extra_unknown:
            verdict = "unknown"
        else:
            verdict = "accept"
        item = {"sample_id": s.sample_id, "verdict": verdict}
        if verdict == "correct":
            item["correct_label"] = "correct ground truth"
        anns.append(item)
    return anns


class TestStatistics:
    def _manifest(self, tmp_path) -> cal.CalibrationManifest:
        return cal.sample_cohort(_build_cohort(tmp_path))

    def test_all_metrics_met(self, tmp_path):
        manifest = self._manifest(tmp_path)
        # 5/6 blind accept+clean；1 条 blind unknown → 不一致。
        blind_verdicts = {
            manifest.blind_sample_ids()[i]: ("unknown" if i == 5 else "accept")
            for i in range(6)
        }
        # 22 非盲中 5 条 correct → 恰好 ≤5/22。
        non_blind = [s.sample_id for s in manifest.samples if not s.blind]
        # 2 条 judge 全 abstain（非盲），人工确认 unknown。
        abstain_ids = non_blind[:2]
        outputs = _stat_outputs(manifest, abstain_ids=abstain_ids)
        extra_unknown = set(abstain_ids)
        anns = _all_accept_annotations(
            manifest,
            blind_verdicts=blind_verdicts,
            extra_correct=set(non_blind[2:7]),
            extra_unknown=extra_unknown,
        )
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.blind_agreement.numerator == 5
        assert stats.blind_agreement.met is True
        assert stats.correction_rate.numerator == 5
        assert stats.correction_rate.met is True
        assert stats.abstain_honesty.numerator == 2
        assert stats.abstain_honesty.ratio == 1.0
        assert stats.abstain_honesty.met is True
        assert all(v >= 2 for v in stats.dimension_coverage.values())
        assert stats.verdict_met is True
        # verdict 仅诊断。
        assert stats.to_dict()["verdict"]["scope"].startswith("diagnostic")

    def test_blind_agreement_below_threshold(self, tmp_path):
        manifest = self._manifest(tmp_path)
        blind_verdicts = {
            manifest.blind_sample_ids()[i]: ("unknown" if i in (4, 5) else "accept")
            for i in range(6)
        }
        outputs = _stat_outputs(manifest)
        anns = _all_accept_annotations(manifest, blind_verdicts=blind_verdicts)
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.blind_agreement.numerator == 4
        assert stats.blind_agreement.met is False
        assert stats.verdict_met is False

    def test_correction_rate_above_threshold(self, tmp_path):
        manifest = self._manifest(tmp_path)
        non_blind = [s.sample_id for s in manifest.samples if not s.blind]
        outputs = _stat_outputs(manifest)
        anns = _all_accept_annotations(manifest, extra_correct=set(non_blind[:6]))
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.correction_rate.numerator == 6
        assert stats.correction_rate.met is False
        assert stats.verdict_met is False

    def test_abstain_honesty_below_threshold(self, tmp_path):
        manifest = self._manifest(tmp_path)
        non_blind = [s.sample_id for s in manifest.samples if not s.blind]
        abstain_ids = non_blind[:3]
        outputs = _stat_outputs(manifest, abstain_ids=abstain_ids)
        # 3 条 abstain，只确认 1 条 unknown → 33% < 50%。
        anns = _all_accept_annotations(manifest, extra_unknown={abstain_ids[0]})
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.abstain_honesty.numerator == 1
        assert stats.abstain_honesty.denominator == 3
        assert stats.abstain_honesty.ratio is not None
        assert round(stats.abstain_honesty.ratio, 6) == round(1 / 3, 6)
        assert stats.abstain_honesty.met is False
        assert stats.verdict_met is False

    def test_abstain_honesty_vacuous_with_no_abstains(self, tmp_path):
        manifest = self._manifest(tmp_path)
        outputs = _stat_outputs(manifest)
        anns = _all_accept_annotations(manifest)
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.abstain_honesty.denominator == 0
        assert stats.abstain_honesty.ratio is None
        assert stats.abstain_honesty.met is True
        assert "vacuous" in stats.abstain_honesty.detail

    def test_dimension_coverage_fails(self, tmp_path):
        manifest = self._manifest(tmp_path)
        outputs = _full_judge_outputs(manifest)
        # 让 dispatch_feasibility 只在 1 条 dispatch 样本里非 abstain。
        dsp = [s for s in manifest.samples if s.family is c.SampleTargetType.DISPATCH]
        for i, s in enumerate(dsp):
            dims = list(DSP_DIMS)
            if i != 0:
                dims.remove("dispatch_feasibility")
            draft = _draft(s, dims=dims)
            outputs[s.sample_id]["draft"] = draft
            outputs[s.sample_id]["conclusion"] = cal.judge_conclusion(draft)
        anns = _all_accept_annotations(manifest)
        stats = cal.compute_statistics(manifest, outputs, anns)
        assert stats.dimension_coverage["dispatch_feasibility"] == 1
        assert stats.verdict_met is False

    def test_blind_flagged_correct_counts_as_agreement(self, tmp_path):
        manifest = self._manifest(tmp_path)
        blind_id = manifest.blind_sample_ids()[0]
        outputs = _stat_outputs(manifest, flagged_ids=[blind_id])
        blind_verdicts = {blind_id: "correct"}
        anns = _all_accept_annotations(manifest, blind_verdicts=blind_verdicts)
        stats = cal.compute_statistics(manifest, outputs, anns)
        # (human correct ∧ judge flagged) 计入一致；其余 5 条 accept∧clean → 6/6。
        assert stats.blind_agreement.numerator == 6
        assert stats.blind_agreement.met is True

    def test_statistics_write_outputs(self, tmp_path):
        manifest = self._manifest(tmp_path)
        outputs = _stat_outputs(manifest)
        anns = _all_accept_annotations(manifest)
        stats = cal.compute_statistics(manifest, outputs, anns)
        json_path, md_path = cal.write_statistics(stats, tmp_path / "stats")
        raw = json.loads(json_path.read_text("utf-8"))
        assert raw["blind_agreement"]["denominator"] == 6
        assert raw["correction_rate"]["denominator"] == 22
        md = md_path.read_text("utf-8")
        assert "diagnostic only" in md
        assert "Dimension coverage" in md
