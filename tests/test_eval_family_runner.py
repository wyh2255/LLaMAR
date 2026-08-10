"""F2 FamilyRoleRunner 测试（judge 家族化设计 §2 / §4.2 / §5）。

只以注入的 fake chat model 驱动**真实** DeepAgent 家族 runner（不接触真实
LLM/网络）。覆盖六项不变量（设计 §8.1 V-F2）：

- subagent 封闭：只有 rubric 声明的 4 个维度；未知维度 / 重复派发被拒；
  subagent 工具集不含 `dispatch_subagent` / task / 写工具；
- 派发指令落盘：`family_dispatch.jsonl` 逐字记录（含被拒 attempt）；
- 反 halo + 聚合不改分：分数外泄指令被拒；家族输出与 subagent 草稿不一致
  → typed FAILED；聚合只做格式合并；
- 独立隔离：家族 + 每维度 subagent 各自独立 agent_id / thread / backend /
  空 history；
- 维度级 abstain 并集覆盖：`dimensions` ∪ `dimension_unknown_reasons`
  恰好覆盖 rubric 维度全集（缺一 FAILED）；
- 检索配额：家族 ≤4 / 维度 subagent ≤6 / 单 job 总 ≤28（共享 `_JobQuota`）。

fake 模型按 system prompt 区分家族 vs subagent：
家族首次调用发 `dispatch_subagent` 工具调用，收到工具结果后发最终 ScoreDraft
JSON；subagent 直接发单维度 ScoreDraft JSON。
"""

from __future__ import annotations

import asyncio
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
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval.agent import family_runner as fr
from sar_orch.eval.agent.retrieval_tools import (
    QUOTA_DIMENSION_SUBAGENT,
    QUOTA_FAMILY_AGENT,
    QUOTA_JOB_TOTAL,
    make_tools,
)
from sar_orch.eval.agent.runner import RunnerError, RunnerStatus

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


def _v2_spec(target_type: c.SampleTargetType):
    name = (
        "dispatch-v2"
        if target_type is c.SampleTargetType.DISPATCH
        else "observation-v2"
    )
    return rr.load_rubric_spec(rr.DEFAULT_RUBRICS_DIR / f"{name}.yaml").spec


def _manifest(spec: c.RubricSpec) -> c.FrozenInputManifest:
    role = c.RoleConfig(
        role=spec.judge_role,
        agent_id=spec.judge_role.value,
        prompt_ref=_artref("snapshots/prompts/x.md", "b" * 64),
        prompt_digest="b" * 64,
        model_profile_ref=_artref("snapshots/model_profiles/x.json", "c" * 64),
        model_profile_digest="c" * 64,
        tool_schema_ref=_artref("snapshots/tool_schemas/x.json", "d" * 64),
        tool_schema_digest="d" * 64,
    )
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
            merge_policy_digest="f" * 64,
        ),
        roles=[role],
        rubrics=[spec],
        command=c.CommandSpec(
            argv_without_secrets=["eval"], cwd="/tmp", env_allowlist=["PATH"]
        ),
    ).freeze()
    return manifest


def _build_run(run) -> None:
    """迷你合成 run 目录：仅够检索工具返回非错误内容。"""
    run.mkdir(parents=True, exist_ok=True)
    (run / "router_interactions.csv").write_text(
        "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType\n"
        '"0","Explore the environment.","Alice","r1","c1","w1","assign_task"\n',
        encoding="utf-8",
    )
    (run / "agent_interactions.csv").write_text(
        AGENT_CSV_HEADER
        + '\n"0","Alice","explore","{}","Explore()","Directly around me.","","","","r1","c2","tool_result","10",""\n',
        encoding="utf-8",
    )
    (run / "events.ndjson").write_text(
        json.dumps(
            {
                "agent": "Coordinator",
                "event_type": "assign_task",
                "payload": {"who": "Alice", "content": "Explore."},
                "step": 0,
                "ts": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _build_job(
    store: a.ArtifactStore, manifest: c.FrozenInputManifest, spec: c.RubricSpec
):
    bundle_ref = store.write_canonical_json(
        "evidence/evidence_bundle.json",
        {"payload": "job-scoped bundle", "family": spec.rubric_id},
        producer="test",
    )
    return c.ScoreJob(
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
        input_bundle_ref=bundle_ref,
        retry_policy_digest="6" * 64,
        manifest_digest="7" * 64,
    )


def _store(tmp_path, manifest: c.FrozenInputManifest) -> a.ArtifactStore:
    return a.ArtifactStore(tmp_path / "attempt" / str(manifest.manifest.attempt_id))


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 fake chat model：区分家族 / subagent，按脚本出牌（无真实 LLM）
# ─────────────────────────────────────────────────────────────────────────────


class FakeFamilyModel(BaseChatModel):
    """注入模型。家族首轮发 `dispatch_subagent`（每维度一次，可追加额外工具
    调用）；收到工具结果后发最终 ScoreDraft JSON。subagent 直接发单维度
    ScoreDraft JSON。`seen_tools` 记录每次模型调用实际见到的工具名。
    """

    model_config: ClassVar[dict[str, Any]] = {"extra": "allow"}

    def __init__(
        self,
        *,
        default_subagent: str = "score",
        subagent_behaviors: dict[str, str] | None = None,
        family_final: dict[str, Any] | None = None,
        family_extra_calls: list[tuple[str, dict[str, Any]]] | None = None,
        instruction_for: dict[str, str] | None = None,
        prose_final: bool = False,
    ):
        super().__init__()
        self._default_subagent = default_subagent
        self._behaviors = dict(subagent_behaviors or {})
        self._family_final = family_final
        self._family_extra_calls = list(family_extra_calls or [])
        self._instruction_for = dict(instruction_for or {})
        self._prose_final = prose_final
        self.seen_tools: list[list[str]] = []
        self.subagent_seen_tools: list[list[str]] = []
        self.family_calls = 0
        self.subagent_calls = 0

    @property
    def _llm_type(self) -> str:
        return "fake-family-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tools = kwargs.get("tools", [])
        tool_names = sorted(t.name for t in tools)
        self.seen_tools.append(tool_names)
        sysm = next((m for m in messages if getattr(m, "type", "") == "system"), None)
        raw = sysm.content if sysm is not None else ""
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
            )
        sys_text = raw if isinstance(raw, str) else str(raw)
        if "## Family Dispatch Instruction" in sys_text:
            self.subagent_seen_tools.append(tool_names)
            return self._subagent_generate(sys_text)
        return self._family_generate(sys_text, messages)

    # ── family ───────────────────────────────────────────────────────────────
    def _family_generate(self, sys_text: str, messages) -> ChatResult:
        self.family_calls += 1
        contract = sys_text.split("## Job Contract (authoritative)")[-1]
        role = self._parse_role(contract)
        dims = self._parse_dims(contract)
        has_tool = any(getattr(m, "type", "") == "tool" for m in messages)
        if self._prose_final:
            return self._chat("The dispatches were acceptable overall.")
        if not has_tool:
            calls = self._default_dispatch_calls(dims)
            return self._chat("", tool_calls=calls)
        return self._chat(json.dumps(self._family_final_args(role, dims)))

    def _default_dispatch_calls(self, dims: list[str]) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for dim in dims:
            instr = self._instruction_for.get(
                dim,
                f"Judge {dim} for this step. Use the evidence files listed in the Job Contract.",
            )
            calls.append(
                self._tool_call(
                    "dispatch_subagent",
                    {"dimension": dim, "instruction": instr},
                    f"fd_{len(calls) + 1}",
                )
            )
        for name, args in self._family_extra_calls:
            calls.append(self._tool_call(name, args, f"fx_{len(calls) + 1}"))
        return calls

    def _family_final_args(self, role: str, dims: list[str]) -> dict[str, Any]:
        if self._family_final is not None:
            args = dict(self._family_final)
            args.setdefault("role", role)
            args.setdefault("invocation_id", str(uuid4()))
            args.setdefault("dimensions", {})
            args.setdefault("dimension_unknown_reasons", {})
            args.setdefault("evidence", [])
            args.setdefault("model_used", "fake-family-model")
            return args
        args: dict[str, Any] = {
            "role": role,
            "invocation_id": str(uuid4()),
            "dimensions": {},
            "dimension_unknown_reasons": {},
            "evidence": [],
            "model_used": "fake-family-model",
        }
        for dim in dims:
            behavior = self._behaviors.get(dim, self._default_subagent)
            if behavior == "score":
                args["dimensions"][dim] = 0.9
            elif behavior == "score_half":
                args["dimensions"][dim] = 0.5
            elif behavior == "abstain":
                args["dimension_unknown_reasons"][dim] = "no evidence to judge"
            else:
                # wrong_dim / wrong_role / prose 等负例必须显式注入 family_final。
                args["dimensions"][dim] = 0.9
        return args

    # ── subagent ─────────────────────────────────────────────────────────────
    def _subagent_generate(self, sys_text: str) -> ChatResult:
        self.subagent_calls += 1
        contract = sys_text.split("## Job Contract (authoritative)")[-1]
        role = self._parse_role(contract)
        m = re.search(r"for your dimension '([A-Za-z0-9_]+)' only", sys_text)
        assert m, "subagent prompt must name its dimension"
        dimension = m.group(1)
        behavior = self._behaviors.get(dimension, self._default_subagent)
        args: dict[str, Any] = {
            "role": role,
            "invocation_id": str(uuid4()),
            "dimensions": {},
            "dimension_unknown_reasons": {},
            "evidence": [],
            "model_used": "fake-family-model",
        }
        if behavior == "score":
            args["dimensions"][dimension] = 0.9
        elif behavior == "score_half":
            args["dimensions"][dimension] = 0.5
        elif behavior == "abstain":
            args["dimension_unknown_reasons"][dimension] = "no evidence to judge"
        elif behavior == "wrong_dim":
            args["dimensions"]["some_other_dim"] = 0.5
        elif behavior == "wrong_role":
            args["role"] = (
                "observation_score_judge"
                if role == "dispatch_score_judge"
                else "dispatch_score_judge"
            )
            args["dimensions"][dimension] = 0.9
        elif behavior == "prose":
            return self._chat("This dimension looks fine to me.")
        else:
            raise AssertionError(f"unknown subagent behavior {behavior!r}")
        return self._chat(json.dumps(args))

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_role(contract: str) -> str:
        m = re.search(r"^role: (\S+)$", contract, re.MULTILINE)
        assert m, "contract must carry role"
        return m.group(1)

    @staticmethod
    def _parse_dims(contract: str) -> list[str]:
        m = re.search(r"dimensions: (\[.*\])", contract)
        assert m, "contract must carry dimensions"
        return json.loads(m.group(1))

    @staticmethod
    def _tool_call(name: str, args: dict[str, Any], cid: str) -> dict[str, Any]:
        return {"name": name, "args": args, "id": cid, "type": "tool_call"}

    @staticmethod
    def _chat(
        content: str, *, tool_calls: list[dict[str, Any]] | None = None
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=content, tool_calls=tool_calls or [])
                )
            ]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 运行 helper：construct store/job → FamilyRoleRunner.run()
# ─────────────────────────────────────────────────────────────────────────────


def _run_once(
    tmp_path,
    model: FakeFamilyModel,
    *,
    target_type: c.SampleTargetType = c.SampleTargetType.DISPATCH,
    run_dir=None,
    **runner_kwargs,
):
    spec = _v2_spec(target_type)
    manifest = _manifest(spec)
    store = _store(tmp_path, manifest)
    job = _build_job(store, manifest, spec)
    runner = fr.FamilyRoleRunner(
        store, manifest, model, run_dir=run_dir, **runner_kwargs
    )
    outcome = asyncio.run(runner.run(job, invocation_id=uuid4(), node_attempt=1))
    return outcome, runner, store, job, spec


# ─────────────────────────────────────────────────────────────────────────────
# 正向：成功路径 + 聚合 + 审计落盘
# ─────────────────────────────────────────────────────────────────────────────


class TestSuccessPath:
    def test_success_dispatch_merge_and_artifacts(self, tmp_path):
        model = FakeFamilyModel()
        outcome, runner, store, job, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.SUCCEEDED
        draft = outcome.draft
        assert set(draft.dimensions) == set(DISPATCH_DIMS)
        assert draft.dimension_unknown_reasons == {}
        assert all(v == 0.9 for v in draft.dimensions.values())
        assert draft.role is job.role
        assert len(draft.evidence) == len(DISPATCH_DIMS)
        for ev in draft.evidence:
            assert ev.ref.path.startswith(f"evidence/job-scoped/{job.job_id}/")
            assert ev.claim_type is c.ClaimType.SCORE
            store.read_verified(ev.ref)

        family_rel = (
            f"agent_runs/{job.role.value}/{runner.invocations[0]['invocation_id']}"
        )
        # 审计落盘：family_input / family_dispatch / family_output.raw / output.validated
        family_input = json.loads(store.read_bytes(f"{family_rel}/family_input.json"))
        assert family_input["job_id"] == str(job.job_id)
        assert family_input["dimensions"] == DISPATCH_DIMS
        assert family_input["retrieval_quotas"] == {
            "family": QUOTA_FAMILY_AGENT,
            "subagent": QUOTA_DIMENSION_SUBAGENT,
            "job_total": QUOTA_JOB_TOTAL,
        }
        assert store.exists(f"{family_rel}/family_output.raw")
        assert store.exists(f"{family_rel}/output.validated.json")
        assert store.exists(f"{family_rel}/usage.json")
        persisted = json.loads(store.read_bytes(f"{family_rel}/output.validated.json"))
        assert c.ScoreDraft.model_validate(persisted) == draft

        # family_dispatch.jsonl：恰好 4 条 accepted，指令逐字落盘。
        dispatch_lines = [
            json.loads(l)
            for l in store.read_bytes(f"{family_rel}/family_dispatch.jsonl")
            .decode("utf-8")
            .splitlines()
        ]
        assert [d["dimension"] for d in dispatch_lines] == DISPATCH_DIMS
        assert all(d["status"] == "accepted" for d in dispatch_lines)
        assert dispatch_lines[0]["instruction"].startswith(
            "Judge dispatch_completeness"
        )

        # 每维度 subagent 审计目录。
        for dim in DISPATCH_DIMS:
            sub_rel = f"{family_rel}/subagents/{dim}"
            sub_input = json.loads(store.read_bytes(f"{sub_rel}/input.json"))
            assert sub_input["dimension"] == dim
            assert (
                sub_input["instruction"]
                == dispatch_lines[DISPATCH_DIMS.index(dim)]["instruction"]
            )
            sub_validated = json.loads(
                store.read_bytes(f"{sub_rel}/output.validated.json")
            )
            assert c.ScoreDraft.model_validate(sub_validated).dimensions == {dim: 0.9}
            assert store.exists(f"{sub_rel}/output.raw")
            assert store.exists(f"{sub_rel}/usage.json")
            assert store.exists(f"{sub_rel}/events.jsonl")

    def test_observation_family_success(self, tmp_path):
        model = FakeFamilyModel()
        outcome, _, store, _, _ = _run_once(
            tmp_path, model, target_type=c.SampleTargetType.OBSERVATION
        )
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert set(outcome.draft.dimensions) == set(OBSERVATION_DIMS)
        assert outcome.draft.dimension_unknown_reasons == {}
        for ev in outcome.draft.evidence:
            store.read_verified(ev.ref)


# ─────────────────────────────────────────────────────────────────────────────
# 维度级 abstain 并集覆盖（§2.3）
# ─────────────────────────────────────────────────────────────────────────────


class TestDimensionAbstain:
    def test_partial_abstain_union_covers_rubric(self, tmp_path):
        model = FakeFamilyModel(subagent_behaviors={"dispatch_completeness": "abstain"})
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.SUCCEEDED
        draft = outcome.draft
        assert set(draft.dimensions) == set(DISPATCH_DIMS) - {"dispatch_completeness"}
        assert draft.dimension_unknown_reasons == {
            "dispatch_completeness": "no evidence to judge"
        }
        union = set(draft.dimensions) | set(draft.dimension_unknown_reasons)
        assert union == set(DISPATCH_DIMS)
        # 评分维度 evidence 与维度一一对应。
        assert len(draft.evidence) == len(draft.dimensions)
        assert draft.unknown_reason is None

    def test_mixed_scores_and_reasons_preserved_verbatim(self, tmp_path):
        model = FakeFamilyModel(
            subagent_behaviors={
                "dispatch_completeness": "score_half",
                "dispatch_feasibility": "abstain",
            }
        )
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert outcome.draft.dimensions["dispatch_completeness"] == 0.5
        assert outcome.draft.dimensions["dispatch_novelty"] == 0.9
        assert outcome.draft.dimension_unknown_reasons == {
            "dispatch_feasibility": "no evidence to judge"
        }

    def test_all_abstain_is_unknown_not_success(self, tmp_path):
        model = FakeFamilyModel(default_subagent="abstain")
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.UNKNOWN
        assert outcome.draft.dimensions == {}
        assert set(outcome.draft.dimension_unknown_reasons) == set(DISPATCH_DIMS)
        assert outcome.draft.unknown_reason == "all dimensions abstained"
        assert outcome.draft.evidence == []

    def test_missing_subagent_draft_fails_closed(self, tmp_path):
        # 家族 agent 没有派发某维度（subagent 从不存在）→ 并集缺失 → FAILED。
        model = FakeFamilyModel(
            family_extra_calls=[],
            instruction_for={"dispatch_completeness": "rejected 0.0 score"},
        )
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert "missing subagent draft" in outcome.error


# ─────────────────────────────────────────────────────────────────────────────
# 封闭 subagent 类型 + 反 halo + 聚合不改分（fail-closed）
# ─────────────────────────────────────────────────────────────────────────────


class TestClosedSubagentsAndAntiHalo:
    def test_unknown_dimension_rejected_closed_set(self, tmp_path):
        model = FakeFamilyModel(
            family_extra_calls=[
                (
                    "dispatch_subagent",
                    {"dimension": "no_such_dim", "instruction": "judge"},
                )
            ]
        )
        outcome, runner, store, job, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert set(outcome.draft.dimensions) == set(DISPATCH_DIMS)
        # 5 条记录：4 accepted + 1 rejected_unknown_dimension。
        records = runner.dispatch_records
        assert len(records) == 5
        rejected = [r for r in records if r["status"] == "rejected_unknown_dimension"]
        assert len(rejected) == 1
        assert rejected[0]["dimension"] == "no_such_dim"
        assert "allowed" in rejected[0]["reason"]
        # 未知维度绝不落盘 subagent 目录。
        family_rel = (
            f"agent_runs/{job.role.value}/{runner.invocations[0]['invocation_id']}"
        )
        assert not store.exists(f"{family_rel}/subagents/no_such_dim")

    def test_duplicate_dispatch_rejected(self, tmp_path):
        model = FakeFamilyModel(
            family_extra_calls=[
                (
                    "dispatch_subagent",
                    {"dimension": "dispatch_completeness", "instruction": "again"},
                )
            ]
        )
        outcome, runner, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.SUCCEEDED
        rejected = [
            r for r in runner.dispatch_records if r["status"] == "rejected_duplicate"
        ]
        assert len(rejected) == 1
        assert rejected[0]["dimension"] == "dispatch_completeness"

    def test_anti_halo_score_smuggling_rejected_and_fails(self, tmp_path):
        # 派发指令携带分数（家族初步结论）→ 该维度拒绝 → 并集缺失 → FAILED。
        model = FakeFamilyModel(
            instruction_for={
                "dispatch_completeness": "The coordinator clearly scored 0.0 here — verify."
            }
        )
        outcome, runner, store, job, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        rejected = [
            r for r in runner.dispatch_records if r["status"] == "rejected_anti_halo"
        ]
        assert len(rejected) == 1
        assert rejected[0]["dimension"] == "dispatch_completeness"
        assert "score token" in rejected[0]["reason"]
        family_rel = (
            f"agent_runs/{job.role.value}/{runner.invocations[0]['invocation_id']}"
        )
        assert not store.exists(
            f"{family_rel}/subagents/dispatch_completeness/output.validated.json"
        )

    def test_aggregation_mismatch_fails(self, tmp_path):
        # 家族 agent 改写 subagent 分数 → 聚合不一致 → FAILED。
        diverging = {
            "dimensions": {
                "dispatch_completeness": 0.1,
                "dispatch_feasibility": 0.1,
                "dispatch_novelty": 0.1,
                "dispatch_efficiency": 0.1,
            },
            "dimension_unknown_reasons": {},
        }
        model = FakeFamilyModel(family_final=diverging)
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert "aggregation mismatch" in outcome.error

    def test_aggregation_mismatch_on_reason_fails(self, tmp_path):
        model = FakeFamilyModel(
            subagent_behaviors={"dispatch_completeness": "abstain"},
            family_final={
                "dimensions": {
                    "dispatch_feasibility": 0.9,
                    "dispatch_novelty": 0.9,
                    "dispatch_efficiency": 0.9,
                },
                "dimension_unknown_reasons": {
                    "dispatch_completeness": "REWORDED reason"
                },
            },
        )
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert "aggregation mismatch" in outcome.error

    def test_family_wrong_role_fails(self, tmp_path):
        model = FakeFamilyModel(
            family_final={
                "role": "observation_score_judge",
                "dimensions": {d: 0.9 for d in DISPATCH_DIMS},
                "dimension_unknown_reasons": {},
            }
        )
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert "role" in outcome.error

    def test_family_no_structured_output_fails(self, tmp_path):
        model = FakeFamilyModel(prose_final=True)
        outcome, _, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert "no structured ScoreDraft" in outcome.error

    def test_subagent_wrong_dimension_fails(self, tmp_path):
        model = FakeFamilyModel(
            subagent_behaviors={"dispatch_completeness": "wrong_dim"},
            family_final={
                "dimensions": {d: 0.9 for d in DISPATCH_DIMS},
                "dimension_unknown_reasons": {},
            },
        )
        outcome, runner, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert any(
            r["status"] == "subagent_failed" and "exactly dimension" in r["reason"]
            for r in runner.dispatch_records
        )

    def test_subagent_wrong_role_fails(self, tmp_path):
        model = FakeFamilyModel(
            subagent_behaviors={"dispatch_feasibility": "wrong_role"},
            family_final={
                "dimensions": {d: 0.9 for d in DISPATCH_DIMS},
                "dimension_unknown_reasons": {},
            },
        )
        outcome, runner, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert any(
            r["status"] == "subagent_failed" and "role" in r["reason"]
            for r in runner.dispatch_records
        )

    def test_subagent_prose_fails(self, tmp_path):
        model = FakeFamilyModel(
            subagent_behaviors={"dispatch_efficiency": "prose"},
            family_final={
                "dimensions": {d: 0.9 for d in DISPATCH_DIMS},
                "dimension_unknown_reasons": {},
            },
        )
        outcome, runner, _, _, _ = _run_once(tmp_path, model)
        assert outcome.status is RunnerStatus.FAILED
        assert any(
            r["status"] == "subagent_failed"
            and "no structured ScoreDraft" in r["reason"]
            for r in runner.dispatch_records
        )


# ─────────────────────────────────────────────────────────────────────────────
# 独立隔离：家族 + 每维度 subagent 独立 agent_id / thread / backend / 空 history
# ─────────────────────────────────────────────────────────────────────────────


class TestIsolation:
    def test_family_and_subagents_isolated(self, tmp_path, run_dir):
        model = FakeFamilyModel()
        outcome, runner, _, _, _ = _run_once(tmp_path, model, run_dir=run_dir)
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert len(runner.invocations) == 1
        fam = runner.invocations[0]
        assert fam["history_len"] == 0
        assert str(fam["invocation_id"]) in fam["thread_id"]
        assert str(fam["invocation_id"]) in fam["agent_id"]

        subs = runner.subagent_invocations
        assert len(subs) == len(DISPATCH_DIMS)
        assert all(s["history_len"] == 0 for s in subs)
        assert all(str(s["invocation_id"]) in s["thread_id"] for s in subs)
        assert all(str(s["invocation_id"]) in s["agent_id"] for s in subs)
        assert {s["dimension"] for s in subs} == set(DISPATCH_DIMS)

        all_agent_ids = [fam["agent_id"]] + [s["agent_id"] for s in subs]
        all_threads = [fam["thread_id"]] + [s["thread_id"] for s in subs]
        all_backends = [fam["backend"]] + [s["backend"] for s in subs]
        assert len(set(all_agent_ids)) == len(all_agent_ids)
        assert len(set(all_threads)) == len(all_threads)
        assert len(set(all_backends)) == len(all_backends)

    def test_subagent_tool_inventory_closed(self, tmp_path, run_dir):
        model = FakeFamilyModel()
        outcome, _, _, _, _ = _run_once(tmp_path, model, run_dir=run_dir)
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert model.subagent_seen_tools
        forbidden = {"task", "write_file", "edit_file", "execute", "dispatch_subagent"}
        # 每个维度 subagent 的工具集都不含 general-purpose / 写 / task /
        # 嵌套派发工具。
        for seen in model.subagent_seen_tools:
            assert not (forbidden & set(seen)), forbidden & set(seen)
        # 家族 agent 的工具集含 dispatch_subagent（子集检测到家族工具集）。
        seen_by_prompt = set()
        for seen in model.seen_tools:
            seen_by_prompt.update(seen)
        assert "dispatch_subagent" in seen_by_prompt

    def test_cross_job_reuse_rejected(self, tmp_path):
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        manifest = _manifest(spec)
        store = _store(tmp_path, manifest)
        job_a = _build_job(store, manifest, spec)
        job_b = _build_job(store, manifest, spec)
        runner = fr.FamilyRoleRunner(store, manifest, FakeFamilyModel())
        with pytest.raises(RunnerError, match="bound to job"):

            async def _reuse():
                await runner.run(job_a, invocation_id=uuid4(), node_attempt=1)
                await runner.run(job_b, invocation_id=uuid4(), node_attempt=1)

            asyncio.run(_reuse())


# ─────────────────────────────────────────────────────────────────────────────
# 检索配额：家族 ≤4 / 维度 ≤6 / 单 job 总 ≤28（共享 _JobQuota）
# ─────────────────────────────────────────────────────────────────────────────


class TestRetrievalQuota:
    def test_session_wiring_uses_design_quotas(self, tmp_path, run_dir):
        model = FakeFamilyModel()
        outcome, runner, _, _, _ = _run_once(tmp_path, model, run_dir=run_dir)
        assert outcome.status is RunnerStatus.SUCCEEDED
        assert runner.family_session is not None
        assert runner.family_session.max_calls == QUOTA_FAMILY_AGENT
        assert set(runner.subagent_sessions) == set(DISPATCH_DIMS)
        for session in runner.subagent_sessions.values():
            assert session.max_calls == QUOTA_DIMENSION_SUBAGENT
            # 家族与 subagent 共享同一 job 总配额账本。
            assert session._quota is runner.family_session._quota
        assert runner.family_session._quota.max_total == QUOTA_JOB_TOTAL

    def test_job_total_quota_exhausted_across_agents(self, tmp_path, run_dir):
        # 家族 agent 首轮先做 3 次检索；job_total_quota=2 → 第 3 次被拒。
        model = FakeFamilyModel(
            family_extra_calls=[
                ("read_dispatch_history", {}),
                ("read_worker_state", {"agent": "Alice"}),
                ("read_coordinator_reasoning", {}),
            ]
        )
        outcome, runner, _, _, _ = _run_once(
            tmp_path, model, run_dir=run_dir, job_total_quota=2
        )
        assert outcome.status is RunnerStatus.SUCCEEDED
        records = runner.family_session.records
        exhausted = [r for r in records if r["tool"] == "read_coordinator_reasoning"]
        assert len(exhausted) == 1
        assert exhausted[0]["returned_refs"] == []
        assert exhausted[0]["truncated"] is False
        # job 总配额只被占 2 次（第 3 次调用被拦，不占配额）。
        assert runner.family_session._quota.used == 2

    def test_family_session_shared_quota_unit(self, tmp_path, run_dir):
        from sar_orch.eval.agent.family_runner import _FamilyRetrievalSession, _JobQuota

        quota = _JobQuota(max_total=1)
        session = _FamilyRetrievalSession(
            run_dir, quota=quota, max_calls=QUOTA_FAMILY_AGENT
        )
        tools = {t.name: t for t in make_tools(session)}
        assert "Error" not in tools["read_dispatch_history"].invoke({})
        out = tools["read_worker_state"].invoke({"agent": "Alice"})
        assert out.startswith("Error: job retrieval quota exhausted")
        assert session.calls == 1
        assert quota.used == 1
        assert len(session.records) == 2
        assert session.records[1]["returned_refs"] == []

    def test_job_quota_reservation_is_atomic(self):
        from sar_orch.eval.agent.family_runner import _JobQuota

        quota = _JobQuota(max_total=1)
        assert quota.try_reserve() is True
        assert quota.try_reserve() is False
        assert quota.used == 1

    def test_subagent_session_quota_is_separate_from_job_total(self, tmp_path, run_dir):
        from sar_orch.eval.agent.family_runner import _FamilyRetrievalSession, _JobQuota

        quota = _JobQuota(max_total=100)
        session = _FamilyRetrievalSession(run_dir, quota=quota, max_calls=1)
        tools = {t.name: t for t in make_tools(session)}
        assert "Error" not in tools["read_dispatch_history"].invoke({})
        out = tools["read_worker_state"].invoke({"agent": "Alice"})
        # 会话自身配额先耗尽（max_calls=1），不消耗 job 总额。
        assert out.startswith("Error: retrieval quota exhausted")
        assert quota.used == 1
        assert session.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# 反 halo 守卫单元 + factory 契约
# ─────────────────────────────────────────────────────────────────────────────


class TestAntiHaloUnit:
    def test_score_tokens_rejected(self):
        for text in (
            "The coordinator clearly scored 0.0 here.",
            "expect this dimension to be 1.0",
            "probably 0.75 on the evidence",
        ):
            assert fr.anti_halo_rejects(text) is not None, text

    def test_guidance_and_evidence_refs_accepted(self):
        for text in (
            (
                "Judge whether idle agents covered all known targets. Check "
                "router_interactions.csv:L2 and events.ndjson:L3."
            ),
            (
                "Use evidence/job-scoped/9e4e2df8-0000-0000-0000-000000000000/"
                "dispatch_completeness.json as your primary source."
            ),
            "Check steps 3-8 in the map summary for coverage.",
        ):
            assert fr.anti_halo_rejects(text) is None, text

    def test_empty_instruction_rejected(self):
        assert fr.anti_halo_rejects("") is not None
        assert fr.anti_halo_rejects("   \n") is not None


class TestFactoryContract:
    def test_factory_requires_model(self, tmp_path):
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        manifest = _manifest(spec)
        store = _store(tmp_path, manifest)
        runtime = SimpleNamespace(store=store, manifest=manifest)
        with pytest.raises(fr.FamilyRoleRunnerError, match="explicit model"):
            fr.make_family_runner_factory(runtime)

    def test_factory_builds_runner_and_rejects_missing_run_dir(self, tmp_path):
        spec = _v2_spec(c.SampleTargetType.DISPATCH)
        manifest = _manifest(spec)
        store = _store(tmp_path, manifest)
        runtime = SimpleNamespace(store=store, manifest=manifest)
        factory = fr.make_family_runner_factory(runtime, model=FakeFamilyModel())
        runner = factory()
        assert isinstance(runner, fr.FamilyRoleRunner)
        with pytest.raises(fr.FamilyRoleRunnerError, match="not a directory"):
            fr.FamilyRoleRunner(
                store, manifest, FakeFamilyModel(), run_dir=tmp_path / "nope"
            )


@pytest.fixture
def run_dir(tmp_path):
    run = tmp_path / "run"
    _build_run(run)
    return run
