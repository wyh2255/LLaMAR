"""P2 rubric registry / Sample / ScoreJob 合同测试（设计 §5.3 / §9 P2）。

不构造任何 DeepAgent / chat 模型；只验证纯确定性逻辑与精确源 digest。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr

# ---------------------------------------------------------------------------
# fakes（鸭子类型，与 legacy select_judge_steps 相同接口）
# ---------------------------------------------------------------------------


class _AI:
    def __init__(self, ok=True, agent="Alice", tool="report_observation"):
        self.succeeded = ok
        self.agent = agent
        self.tool_name = tool


class _Step:
    def __init__(self, interactions=None):
        self.interactions = list(interactions or [_AI(True)])


class _Ep:
    def __init__(self, n, fail_at=(), interactions_by_step=None):
        self.steps = {
            i: _Step(
                list(interactions_by_step.get(i, [_AI(True)]))
                if interactions_by_step is not None
                else ([_AI(False)] if i in fail_at else [_AI(True)])
            )
            for i in range(1, n + 1)
        }


S = "1" * 64
E = "2" * 64


def _yaml(text: str, **kwargs) -> rr.LoadedRubric:
    return rr.load_rubric_text(
        text,
        source_path="test.yaml",
        prompt_resolver=lambda name: f"# {name}\n".encode(),
        **kwargs,
    )


DISPATCH_YAML = """rubric_id: dispatch-v1
version: 1.0.0
target_type: dispatch
input_selector: dispatch_samples
dimensions: [full_coverage, role_match]
judge_role: dispatch_score_judge
merge_group: dispatch
weight: 1.0
calibration_status: uncalibrated
prompt_template: dispatch_judge.md
"""

OBSERVATION_YAML = """rubric_id: observation-v1
version: 1.0.0
target_type: observation
input_selector: observation_samples
dimensions: [hallucination_rate]
judge_role: observation_score_judge
merge_group: observation
weight: 1.0
prompt_template: observation_judge.md
"""


# ---------------------------------------------------------------------------
# 版本化 YAML 加载与精确源 digest
# ---------------------------------------------------------------------------


class TestRubricLoading:
    def test_loads_dispatch_and_materializes_spec(self):
        loaded = _yaml(DISPATCH_YAML)
        spec = loaded.spec
        assert spec.rubric_id == "dispatch-v1"
        assert spec.target_type is c.SampleTargetType.DISPATCH
        assert spec.judge_role is c.JudgeRole.DISPATCH_SCORE_JUDGE
        assert spec.dimensions == ["full_coverage", "role_match"]
        assert spec.weight == 1.0
        assert spec.calibration_status is c.CalibrationStatus.UNCALIBRATED

    def test_exact_source_digest_matches_raw_bytes(self):
        loaded = _yaml(DISPATCH_YAML)
        assert loaded.source_digest == c.sha256_hex(DISPATCH_YAML.encode("utf-8"))
        assert loaded.source_digest == loaded.spec.digest
        assert loaded.source_bytes == DISPATCH_YAML.encode("utf-8")

    def test_digest_is_deterministic_across_loads(self):
        assert _yaml(DISPATCH_YAML).source_digest == _yaml(DISPATCH_YAML).source_digest

    def test_digest_changes_when_source_changes(self):
        changed = DISPATCH_YAML.replace("weight: 1.0", "weight: 1.5")
        assert _yaml(changed).source_digest != _yaml(DISPATCH_YAML).source_digest

    def test_digest_changes_on_any_byte(self):
        assert (
            _yaml(DISPATCH_YAML + "  ").source_digest
            != _yaml(DISPATCH_YAML).source_digest
        )

    def test_load_from_file_bytes_matches_read_bytes(self, tmp_path):
        path = tmp_path / "dispatch-v1.yaml"
        path.write_text(DISPATCH_YAML, encoding="utf-8")
        loaded = rr.load_rubric_spec(path, prompt_resolver=lambda name: b"# dispatch\n")
        assert loaded.source_digest == c.sha256_hex(path.read_bytes())

    def test_invalid_yaml_rejected(self):
        with pytest.raises(rr.RubricError, match="parse failed"):
            _yaml("rubric_id: [unclosed")

    def test_root_must_be_mapping(self):
        with pytest.raises(rr.RubricError, match="mapping"):
            _yaml("- just\n- a\n- list\n")

    def test_unknown_field_rejected(self):
        with pytest.raises(rr.RubricError, match="extra"):
            _yaml(DISPATCH_YAML + "surprise_field: true\n")

    def test_missing_required_field_rejected(self):
        with pytest.raises(rr.RubricError):
            _yaml("rubric_id: dispatch-v1\nversion: 1.0.0\n")

    def test_empty_dimensions_rejected(self):
        with pytest.raises(rr.RubricError, match="dimensions"):
            _yaml(
                DISPATCH_YAML.replace(
                    "dimensions: [full_coverage, role_match]",
                    "dimensions: []",
                )
            )

    def test_target_role_mismatch_rejected(self):
        bad = DISPATCH_YAML.replace(
            "judge_role: dispatch_score_judge",
            "judge_role: observation_score_judge",
        )
        with pytest.raises(rr.RubricError, match="dispatch rubric must use"):
            _yaml(bad)

    def test_negative_weight_rejected(self):
        with pytest.raises(rr.RubricError, match="weight"):
            _yaml(DISPATCH_YAML.replace("weight: 1.0", "weight: -1"))

    def test_prompt_template_ref_materialized(self):
        loaded = _yaml(DISPATCH_YAML)
        ref = loaded.spec.prompt_template_ref
        assert ref.path == "snapshots/prompts/dispatch-v1.md"
        assert ref.sha256 == c.sha256_hex(b"# dispatch_judge.md\n")
        assert ref.bytes == len(b"# dispatch_judge.md\n")
        assert ref.producer == "rubric-registry"

    def test_prompt_snapshot_rel_override(self):
        loaded = _yaml(DISPATCH_YAML, prompt_snapshot_rel="snapshots/prompts/x.md")
        assert loaded.spec.prompt_template_ref.path == "snapshots/prompts/x.md"

    def test_missing_prompt_template_rejected(self):
        with pytest.raises(rr.RubricError, match="not found"):
            rr.load_rubric_text(
                DISPATCH_YAML,
                prompt_resolver=lambda name: (_ for _ in ()).throw(
                    FileNotFoundError(name)
                ),
            )

    def test_observation_defaults(self):
        loaded = _yaml(OBSERVATION_YAML)
        assert loaded.spec.rubric_id == "observation-v1"
        assert loaded.spec.target_type is c.SampleTargetType.OBSERVATION
        assert loaded.spec.judge_role is c.JudgeRole.OBSERVATION_SCORE_JUDGE
        assert loaded.spec.dimensions == ["hallucination_rate"]


class TestDefaultRegistry:
    def test_bundled_defaults_include_dispatch_and_observation(self):
        reg = rr.default_registry()
        ids = {r.spec.rubric_id for r in reg.items}
        # 断言不依赖 rubric 数量：v1 冻结源必须存在，v2 家族源也必须存在。
        assert {"dispatch-v1", "observation-v1"} <= ids
        assert {"dispatch-v2", "observation-v2"} <= ids
        assert len(ids) == 4

    def test_default_rubric_id_mapping(self):
        assert (
            rr.RubricRegistry.default_rubric_id(c.SampleTargetType.DISPATCH)
            == "dispatch-v1"
        )
        assert (
            rr.RubricRegistry.default_rubric_id(c.SampleTargetType.OBSERVATION)
            == "observation-v1"
        )

    def test_default_rubric_by_target(self):
        reg = rr.default_registry()
        assert (
            reg.default_rubric(c.SampleTargetType.DISPATCH).spec.target_type
            is c.SampleTargetType.DISPATCH
        )
        assert (
            reg.default_rubric(c.SampleTargetType.OBSERVATION).spec.target_type
            is c.SampleTargetType.OBSERVATION
        )

    def test_by_target_type_filters(self):
        reg = rr.default_registry()
        dispatch_ids = [
            r.spec.rubric_id for r in reg.by_target_type(c.SampleTargetType.DISPATCH)
        ]
        observation_ids = [
            r.spec.rubric_id
            for r in reg.by_target_type(c.SampleTargetType.OBSERVATION)
        ]
        # 每家族 v1 + v2 共存，且不跨 target 泄漏（断言不依赖具体数量）。
        assert dispatch_ids == ["dispatch-v1", "dispatch-v2"]
        assert observation_ids == ["observation-v1", "observation-v2"]

    def test_registry_digest_stable_across_construction_order(self):
        a = rr.default_registry()
        b = rr.RubricRegistry(list(reversed(a.items)))
        assert a.digest() == b.digest()

    def test_duplicate_rubric_id_rejected(self):
        with pytest.raises(rr.RubricError, match="duplicate"):
            rr.RubricRegistry([_yaml(DISPATCH_YAML), _yaml(DISPATCH_YAML)])

    def test_unknown_rubric_id_rejected(self):
        with pytest.raises(rr.RubricError, match="unknown rubric_id"):
            rr.default_registry().get("nope-v9")


# ---------------------------------------------------------------------------
# v2 家族 rubric（judge 家族化设计 §6：v1 frozen 保留，v2 走家族 runner）
# ---------------------------------------------------------------------------


class TestV2Rubrics:
    RUBRICS_DIR = rr.DEFAULT_RUBRICS_DIR

    def _load(self, name: str) -> rr.LoadedRubric:
        return rr.load_rubric_spec(self.RUBRICS_DIR / name)

    def test_dispatch_v2_loads_with_family_spec(self):
        loaded = self._load("dispatch-v2.yaml")
        spec = loaded.spec
        assert spec.rubric_id == "dispatch-v2"
        assert spec.target_type is c.SampleTargetType.DISPATCH
        assert spec.judge_role is c.JudgeRole.DISPATCH_SCORE_JUDGE
        assert spec.dimensions == [
            "dispatch_completeness",
            "dispatch_feasibility",
            "dispatch_novelty",
            "dispatch_efficiency",
        ]
        assert spec.merge_group == "dispatch"
        assert spec.calibration_status is c.CalibrationStatus.UNCALIBRATED
        assert spec.prompt_template_ref.path == "snapshots/prompts/dispatch-v2.md"
        assert spec.prompt_template_ref.producer == "rubric-registry"

    def test_observation_v2_loads_with_family_spec(self):
        loaded = self._load("observation-v2.yaml")
        spec = loaded.spec
        assert spec.rubric_id == "observation-v2"
        assert spec.target_type is c.SampleTargetType.OBSERVATION
        assert spec.judge_role is c.JudgeRole.OBSERVATION_SCORE_JUDGE
        assert spec.dimensions == [
            "existence_grounding",
            "type_fidelity",
            "position_fidelity",
            "attribute_fidelity",
        ]
        assert spec.merge_group == "observation"
        assert spec.calibration_status is c.CalibrationStatus.UNCALIBRATED
        assert spec.prompt_template_ref.path == "snapshots/prompts/observation-v2.md"

    def test_v2_digest_is_exact_source_digest_and_stable(self):
        a = self._load("dispatch-v2.yaml")
        b = self._load("dispatch-v2.yaml")
        assert a.source_digest == c.sha256_hex(
            (self.RUBRICS_DIR / "dispatch-v2.yaml").read_bytes()
        )
        assert a.source_digest == a.spec.digest
        assert a.source_digest == b.source_digest

    def test_v2_digest_differs_from_v1(self):
        v1 = self._load("dispatch-v1.yaml")
        v2 = self._load("dispatch-v2.yaml")
        assert v1.source_digest != v2.source_digest

    def test_v1_and_v2_coexist_in_default_registry(self):
        reg = rr.default_registry()
        assert reg.contains("dispatch-v1")
        assert reg.contains("dispatch-v2")
        assert reg.contains("observation-v1")
        assert reg.contains("observation-v2")
        # v1 字节不动（frozen）：registry 内 v1 digest 与仓库文件字节一致。
        for name in ("dispatch-v1.yaml", "observation-v1.yaml"):
            loaded = reg.get(name.removesuffix(".yaml"))
            assert loaded.source_digest == c.sha256_hex(
                (self.RUBRICS_DIR / name).read_bytes()
            )

    def test_family_prompt_template_files_exist(self):
        # registry 加载会读 prompt 文件计算 digest —— 家族 prompt 必须存在。
        for rubric_name, prompt_name in (
            ("dispatch-v2", "dispatch_family_judge.md"),
            ("observation-v2", "observation_family_judge.md"),
        ):
            loaded = rr.default_registry().get(rubric_name)
            # prompt_template_ref 是快照 rel；实际源文件必须存在于 prompts/。
            assert loaded.spec.prompt_template_ref.path == (
                f"snapshots/prompts/{rubric_name}.md"
            )
            assert (rr._AGENT_PROMPTS_DIR / prompt_name).is_file()


class TestV2RubricAssets:
    """家族 prompt 与 8 个维度 subagent prompt 的存在性与非空校验。"""

    PROMPTS_DIR = rr._AGENT_PROMPTS_DIR
    DIMENSIONS_DIR = rr._AGENT_PROMPTS_DIR / "dimensions"

    FAMILY_PROMPTS = frozenset(
        {
            "dispatch_family_judge.md",
            "observation_family_judge.md",
        }
    )
    DIMENSION_PROMPTS = frozenset(
        {
            "dispatch_completeness.md",
            "dispatch_feasibility.md",
            "dispatch_novelty.md",
            "dispatch_efficiency.md",
            "existence_grounding.md",
            "type_fidelity.md",
            "position_fidelity.md",
            "attribute_fidelity.md",
        }
    )

    def test_family_prompts_exist_and_nonempty(self):
        for name in self.FAMILY_PROMPTS:
            path = self.PROMPTS_DIR / name
            assert path.is_file(), name
            assert path.read_text(encoding="utf-8").strip(), name

    def test_dimension_prompts_exist_and_nonempty(self):
        assert self.DIMENSIONS_DIR.is_dir()
        found = {p.name for p in self.DIMENSIONS_DIR.glob("*.md")}
        assert self.DIMENSION_PROMPTS <= found
        for name in self.DIMENSION_PROMPTS:
            text = (self.DIMENSIONS_DIR / name).read_text(encoding="utf-8")
            assert text.strip(), name

    def test_dimension_names_match_v2_rubrics(self):
        # 维度 prompt 文件集合 = v2 rubric 的维度集合（防漂移）。
        reg = rr.default_registry()
        v2_dimensions = set()
        for rubric_id in ("dispatch-v2", "observation-v2"):
            v2_dimensions.update(reg.get(rubric_id).spec.dimensions)
        assert {name.removesuffix(".md") for name in self.DIMENSION_PROMPTS} == (
            v2_dimensions
        )


# ---------------------------------------------------------------------------
# dispatch Sample 选择：保留 legacy select_judge_steps 语义
# ---------------------------------------------------------------------------


def _dispatch(ep, target=20, source=S, evidence=E):
    return rr.build_dispatch_samples(
        ep, target=target, source_digest=source, evidence_digest=evidence
    )


class TestDispatchSamples:
    def test_steps_match_legacy_select_judge_steps(self):
        from sar_orch.eval.agent.eval_agent import select_judge_steps

        cases = [
            (_Ep(30, fail_at={7, 13, 22}), 20),
            (_Ep(30, fail_at=set(range(1, 26))), 5),
            (_Ep(10), 50),
            (_Ep(30), 10),
            (_Ep(1), 20),
            (_Ep(100, fail_at={2, 99}), 17),
            (_Ep(0), 20),
        ]
        for ep, target in cases:
            plan = _dispatch(ep, target)
            assert [s.step for s in plan.selected] == select_judge_steps(
                ep,
                target,  # type: ignore[arg-type]
            ), (target, plan)

    def test_selection_is_deterministic(self):
        ep = _Ep(30, fail_at={7, 13, 22})
        first = _dispatch(ep, 20)
        for _ in range(4):
            again = _dispatch(ep, 20)
            assert [s.step for s in again.selected] == [s.step for s in first.selected]
            assert again.digest() == first.digest()

    def test_every_failing_step_included(self):
        plan = _dispatch(_Ep(30, fail_at={7, 13, 22}), 20)
        assert {7, 13, 22} <= {s.step for s in plan.selected}

    def test_failing_steps_overflow_target(self):
        plan = _dispatch(_Ep(30, fail_at=set(range(1, 26))), 5)
        assert len(plan.selected) >= 25
        assert plan.overflow is True
        failed_samples = [
            s for s in plan.selected if s.selection_reason == "failed_interaction"
        ]
        assert any(s.selected_under_overflow for s in failed_samples)

    def test_endpoints_always_sampled(self):
        for n in (1, 2, 5, 30, 100):
            plan = _dispatch(_Ep(n), 20)
            steps = {s.step for s in plan.selected}
            assert 1 in steps and n in steps

    def test_target_respected_without_failures(self):
        for target in (1, 3, 5, 10, 20):
            plan = _dispatch(_Ep(30), target)
            assert len(plan.selected) <= max(target, 2)

    def test_target_above_episode_length_returns_all(self):
        plan = _dispatch(_Ep(10), 50)
        assert [s.step for s in plan.selected] == list(range(1, 11))

    def test_nonpositive_target_yields_nothing(self):
        for bad in (0, -1, -100):
            assert _dispatch(_Ep(10), bad).selected == ()

    def test_empty_episode_yields_nothing(self):
        assert _dispatch(_Ep(0), 20).selected == ()

    def test_steps_are_sorted(self):
        plan = _dispatch(_Ep(30, fail_at={22, 7, 13}), 15)
        steps = [s.step for s in plan.selected]
        assert steps == sorted(steps)

    def test_sample_identity_fields_complete(self):
        plan = _dispatch(_Ep(30, fail_at={7}), 20)
        assert plan.selected
        for sample in plan.selected:
            assert isinstance(sample.sample_id, UUID)
            assert sample.target_type is c.SampleTargetType.DISPATCH
            assert sample.agent is None
            assert sample.claim_call_ids == []
            assert sample.source_digest == S
            assert sample.evidence_digest == E
            assert sample.step >= 1
            assert sample.ordinal >= 0
            assert sample.selection_reason in {
                "failed_interaction",
                "first_step",
                "last_step",
                "fixed_stride",
            }
            assert isinstance(sample.selected_under_overflow, bool)

    def test_ordinals_are_sequential(self):
        plan = _dispatch(_Ep(30, fail_at={7, 13, 22}), 20)
        assert [s.ordinal for s in plan.selected] == list(range(len(plan.selected)))

    def test_first_step_reason_is_not_failed(self):
        plan = _dispatch(_Ep(30, fail_at={7}), 20)
        first = next(s for s in plan.selected if s.step == 1)
        assert first.selection_reason in {"first_step", "failed_interaction"}

    def test_plan_records_policy_digest_and_overflow(self):
        plan = _dispatch(_Ep(30, fail_at={7}), 20)
        assert plan.target_type is c.SampleTargetType.DISPATCH
        assert plan.target == 20
        assert plan.policy_digest == rr.selection_policy_digest(
            20, c.SampleTargetType.DISPATCH
        )
        assert plan.overflow is False

    def test_plan_policy_digest_is_stable(self):
        a = _dispatch(_Ep(30), 20)
        b = _dispatch(_Ep(30), 20)
        assert a.policy_digest == b.policy_digest
        assert a.policy_digest == rr.selection_policy_digest(
            20, c.SampleTargetType.DISPATCH
        )

    def test_plan_content_digest_stable_for_equivalent_selection(self):
        a = _dispatch(_Ep(30, fail_at={7}), 20)
        b = _dispatch(_Ep(30, fail_at={7}), 20)
        assert a.digest() == b.digest()
        # 不同选择 -> 不同内容 digest
        c2 = _dispatch(_Ep(30), 20)
        assert a.digest() != c2.digest()

    def test_invalid_source_digest_rejected(self):
        with pytest.raises(rr.RubricError, match="digest"):
            _dispatch(_Ep(10), source="zzz")


# ---------------------------------------------------------------------------
# observation Sample：identity = (agent, step, call)
# ---------------------------------------------------------------------------


def _obs(ep, target=20, source=S, evidence=E):
    return rr.build_observation_samples(
        ep, target=target, source_digest=source, evidence_digest=evidence
    )


def _obs_episode(spec: dict[int, list]) -> object:
    return _Ep(0) if not spec else _Ep(max(spec), interactions_by_step=spec)


class TestObservationSamples:
    def test_observation_sample_identity_is_agent_step_call(self):
        ep = _obs_episode(
            {
                1: [_AI(True, "Alice"), _AI(True, "Bob")],
                2: [_AI(True, "Alice")],
            }
        )
        plan = _obs(ep, target=2)
        assert [(s.step, s.agent) for s in plan.selected] == [
            (1, "Alice"),
            (1, "Bob"),
            (2, "Alice"),
        ]
        for s in plan.selected:
            assert s.target_type is c.SampleTargetType.OBSERVATION
            assert s.claim_call_ids
            assert s.claim_call_ids[0].startswith(f"{s.step}:{s.agent}:")

    def test_one_sample_per_report_observation_call(self):
        ep = _obs_episode(
            {1: [_AI(True, "Alice"), _AI(True, "Alice"), _AI(True, "Bob")]}
        )
        plan = _obs(ep, target=1)
        assert len(plan.selected) == 3

    def test_observation_step_selection_matches_legacy(self):
        from sar_orch.eval.agent.eval_agent import select_judge_steps

        spec = {
            1: [_AI(False, "Alice")],
            2: [_AI(True, "Bob")],
            5: [_AI(True, "Alice")],
            9: [_AI(True, "Bob")],
            12: [_AI(False, "Carol")],
        }
        ep = _obs_episode(spec)
        legacy_steps = set(
            select_judge_steps(ep, 5)  # type: ignore[arg-type]
        )
        sample_steps = {s.step for s in _obs(ep, target=5).selected}
        assert sample_steps <= legacy_steps

    def test_steps_without_reports_produce_no_samples(self):
        spec = {
            1: [_AI(True, "Alice", tool="navigate_to")],
            2: [_AI(True, "Alice")],
        }
        plan = _obs(_obs_episode(spec), target=2)
        assert [s.step for s in plan.selected] == [2]

    def test_observation_selection_deterministic(self):
        spec = {
            1: [_AI(False, "Alice")],
            3: [_AI(True, "Bob")],
            7: [_AI(True, "Carol")],
        }
        ep = _obs_episode(spec)
        first = _obs(ep, target=2)
        assert first.digest() == _obs(ep, target=2).digest()

    def test_observation_plan_target_is_steps_not_samples(self):
        spec = {
            1: [_AI(True, "A"), _AI(True, "B")],
            2: [_AI(True, "A"), _AI(True, "B")],
        }
        plan = _obs(_obs_episode(spec), target=2)
        # 2 steps 选中，但 sample 数 = 4 个 report_observation call
        assert len(plan.selected) == 4
        assert plan.target == 2

    def test_observation_selected_under_overflow_tracks_step_overflow(self):
        spec = {
            i: [_AI(False, "A")] for i in range(1, 11)
        }  # 10 个失败步，target=2 -> overflow
        plan = _obs(_obs_episode(spec), target=2)
        assert plan.overflow is True
        assert plan.step_overflow is True
        assert any(s.selected_under_overflow for s in plan.selected)


# ---------------------------------------------------------------------------
# ScoreJob 绑定
# ---------------------------------------------------------------------------


def _rubric(
    target_type=c.SampleTargetType.DISPATCH,
    judge_role=None,
    digest=None,
    dimensions=None,
    rubric_id="dispatch-v1",
):
    return c.RubricSpec(
        rubric_id=rubric_id,
        version="1.0.0",
        digest=digest or ("a" * 64),
        target_type=target_type,
        input_selector="dispatch_samples",
        dimensions=list(dimensions or ["d1", "d2"]),
        prompt_template_ref=c.ArtifactRef(
            path="snapshots/prompts/dispatch.md",
            sha256="b" * 64,
            bytes=8,
            media_type="text/markdown",
            producer="test",
        ),
        judge_role=judge_role or c.JudgeRole.DISPATCH_SCORE_JUDGE,
        merge_group="dispatch",
        weight=1.0,
    )


def _role(role=c.JudgeRole.DISPATCH_SCORE_JUDGE):
    return c.RoleConfig(
        role=role,
        agent_id=role.value,
        prompt_ref=c.ArtifactRef(
            path="snapshots/prompts/dispatch.md",
            sha256="0" * 64,
            bytes=8,
            media_type="text/markdown",
            producer="test",
        ),
        prompt_digest="b" * 64,
        model_profile_ref=c.ArtifactRef(
            path="snapshots/model_profiles/score.json",
            sha256="1" * 64,
            bytes=8,
            media_type="application/json",
            producer="test",
        ),
        model_profile_digest="c" * 64,
        tool_schema_ref=c.ArtifactRef(
            path="snapshots/tool_schemas/score.json",
            sha256="2" * 64,
            bytes=8,
            media_type="application/json",
            producer="test",
        ),
        tool_schema_digest="d" * 64,
    )


def _bundle_ref():
    return c.ArtifactRef(
        path="input_bundle/1.json",
        sha256="f" * 64,
        bytes=8,
        media_type="application/json",
        producer="freeze",
    )


class TestScoreJobBinding:
    def test_build_score_job_binds_all_fields(self):
        sample = _dispatch(_Ep(5), target=5).selected[0]
        rubric = _rubric()
        role = _role()
        job = rr.build_score_job(
            sample,
            rubric,
            role,
            input_bundle_ref=_bundle_ref(),
            manifest_digest="9" * 64,
            retry_policy_digest="5" * 64,
        )
        assert job.job_id == job.binding().job_id
        assert job.sample_id == sample.sample_id
        assert job.target_type is sample.target_type
        assert job.source_digest == sample.source_digest
        assert job.evidence_digest == sample.evidence_digest
        assert job.rubric_id == rubric.rubric_id
        assert job.rubric_digest == rubric.digest
        assert job.role is role.role
        assert job.prompt_digest == role.prompt_digest
        assert job.model_profile_digest == role.model_profile_digest
        assert job.tool_schema_digest == role.tool_schema_digest
        assert job.input_bundle_ref.path == "input_bundle/1.json"
        assert job.manifest_digest == "9" * 64
        assert job.retry_policy_digest == "5" * 64
        assert job.status is c.ScoreJobStatus.PENDING

    def test_job_digest_is_canonical_and_covers_binding(self):
        sample = _dispatch(_Ep(5), target=5).selected[0]
        job = rr.build_score_job(
            sample,
            _rubric(),
            _role(),
            input_bundle_ref=_bundle_ref(),
            manifest_digest="9" * 64,
            retry_policy_digest="5" * 64,
        )
        expected = c.sha256_hex(
            c.canonical_json(
                job.model_dump(mode="json", exclude={"status", "job_digest"})
            ).encode("utf-8")
        )
        assert job.job_digest == expected

    def test_reject_sample_rubric_target_type_mismatch(self):
        sample = _dispatch(_Ep(5), target=5).selected[0]
        obs_rubric = _rubric(
            target_type=c.SampleTargetType.OBSERVATION,
            judge_role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
            rubric_id="observation-v1",
        )
        with pytest.raises(rr.RubricError, match="target_type"):
            rr.build_score_job(
                sample,
                obs_rubric,
                _role(c.JudgeRole.OBSERVATION_SCORE_JUDGE),
                input_bundle_ref=_bundle_ref(),
                manifest_digest="9" * 64,
                retry_policy_digest="5" * 64,
            )

    def test_reject_rubric_judge_role_role_mismatch(self):
        sample = _dispatch(_Ep(5), target=5).selected[0]
        rubric = _rubric(judge_role=c.JudgeRole.DISPATCH_SCORE_JUDGE)
        wrong_role = _role(c.JudgeRole.OBSERVATION_SCORE_JUDGE)
        with pytest.raises(rr.RubricError, match="judge_role"):
            rr.build_score_job(
                sample,
                rubric,
                wrong_role,
                input_bundle_ref=_bundle_ref(),
                manifest_digest="9" * 64,
                retry_policy_digest="5" * 64,
            )

    def test_reject_non_score_role(self):
        sample = _dispatch(_Ep(5), target=5).selected[0]
        report_rubric = _rubric(judge_role=c.JudgeRole.REPORT_JUDGE)
        with pytest.raises(rr.RubricError, match="score judge role"):
            rr.build_score_job(
                sample,
                report_rubric,
                _role(c.JudgeRole.REPORT_JUDGE),
                input_bundle_ref=_bundle_ref(),
                manifest_digest="9" * 64,
                retry_policy_digest="5" * 64,
            )

    def test_build_score_jobs_batch_one_per_sample(self):
        plan = _dispatch(_Ep(8, fail_at={3}), target=5)
        jobs = rr.build_score_jobs(
            plan.selected,
            _rubric(),
            _role(),
            input_bundle_ref=_bundle_ref(),
            manifest_digest="9" * 64,
            retry_policy_digest="5" * 64,
        )
        assert len(jobs) == len(plan.selected)
        assert len({j.job_id for j in jobs}) == len(jobs)
        assert len({j.sample_id for j in jobs}) == len(plan.selected)

    def test_observation_job_uses_observation_role(self):
        spec = {1: [_AI(True, "Alice")], 2: [_AI(True, "Bob")]}
        plan = _obs(_obs_episode(spec), target=2)
        rubric = _rubric(
            target_type=c.SampleTargetType.OBSERVATION,
            judge_role=c.JudgeRole.OBSERVATION_SCORE_JUDGE,
            rubric_id="observation-v1",
            dimensions=["hallucination_rate"],
        )
        job = rr.build_score_job(
            plan.selected[0],
            rubric,
            _role(c.JudgeRole.OBSERVATION_SCORE_JUDGE),
            input_bundle_ref=_bundle_ref(),
            manifest_digest="9" * 64,
            retry_policy_digest="5" * 64,
        )
        assert job.role is c.JudgeRole.OBSERVATION_SCORE_JUDGE
        assert job.target_type is c.SampleTargetType.OBSERVATION
