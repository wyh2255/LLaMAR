"""deferred report_judge / recommendation_judge 角色：prompt assets、受限 runner、
Draft schema 校验（设计 §4、§9 deferred Create）。零真实 LLM、零网络。

覆盖：
- `report_judge.md` / `recommendation_judge.md` 存在且 `_default_prompt_resolver`
  可解析；prompt 文件 canonical digest 稳定（记录快照，改动即失败）；
- `ReportNarrativeDraft` / `RecommendationDraft` schema 补齐校验：
  - report factual claim 段落必须带 allowlisted evidence ref（无 ref → typed）；
  - 纯 redacted evidence 支撑 factual claim → typed redaction violation；
  - judge-authored recommendation 必须带 evidence/failure basis；
  - report evidence claim_type 必须 SUMMARY/OBSERVATION；recommendation evidence
    claim_type 必须 RECOMMENDATION；
- 受限 role runner（`ReportRoleRunner` / `RecommendationRoleRunner`，真实
  DeepAgent + 注入 fake chat model）：
  - 工具清单只含 role 对应只读 allowlist reader + 结构化 draft；默认 built-ins
    被 HarnessProfile 排除；recommendation_judge 无任何 evidence 写工具；
  - 每次 invocation 独立 agent_id / thread / backend / 空 history；
  - 跨 role 输入 / allowlist 之外的 ref → typed FAILED（evidence_not_authorized）；
  - 未注入 model → fail-closed（RoleRunnerError）。

只以注入的 fake chat model 驱动真实 DeepAgent role runner；不接触真实 LLM/网络，
不接入 workflow 节点（run_report_judge / run_recommendation_judge 接线留待后续）。
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
from pydantic import ValidationError

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval.agent import roles

#: prompt 文件 canonical digest 快照（改动 prompt 必须同步更新，否则失败）。
REPORT_JUDGE_PROMPT_SHA256 = (
    "baa4381fa6e8c08af06a72a0e12c4a3635b016d8701b300fb9e0e433bf5c182d"
)
RECOMMENDATION_JUDGE_PROMPT_SHA256 = (
    "28c3e25d64f8549637b883d37c850a5dcc9eaf07537c9cc5d9e9b5a2052e99e1"
)


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


def _manifest() -> c.FrozenInputManifest:
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
            llm_judge_required=True,
            audit_level=c.AuditLevel.STANDARD,
            retry=2,
            timeout_s=60,
            concurrency=2,
            retention_days=30,
            allow_shared_model=True,
            merge_policy_digest="0" * 64,
        ),
        roles=[
            _role(c.JudgeRole.REPORT_JUDGE),
            _role(c.JudgeRole.RECOMMENDATION_JUDGE),
        ],
        rubrics=[],
        command=c.CommandSpec(
            argv_without_secrets=["eval"], cwd="/tmp", env_allowlist=["PATH"]
        ),
    ).freeze()


def _store(tmp_path) -> a.ArtifactStore:
    return a.ArtifactStore(tmp_path / "attempt")


def _allowlist(store: a.ArtifactStore) -> dict[str, c.ArtifactRef]:
    """report_judge 的 allowlist：merged bundle + allowlisted evidence/audit summary。"""
    merged = store.write_canonical_json(
        "merged/score_bundle.json", {"status": "succeeded"}, producer="merge"
    )
    audit = store.write_canonical_json(
        "evidence/audit_summary.json", {"kind": "audit"}, producer="materializer"
    )
    evidence = store.write_canonical_json(
        "evidence/steps_overview.json", {"steps": 5}, producer="materializer"
    )
    return {merged.path: merged, audit.path: audit, evidence.path: evidence}


def _frozen_allowlist(store: a.ArtifactStore) -> dict[str, c.ArtifactRef]:
    """recommendation_judge 的 allowlist：frozen merged/report/failure refs。"""
    merged = store.write_canonical_json(
        "merged/score_bundle.json", {"status": "succeeded"}, producer="merge"
    )
    report = store.write_canonical_json(
        "reports/eval_report.json", {"report_family": "attempt-v2"}, producer="renderer"
    )
    failure = store.write_canonical_json(
        "evidence/deterministic_violations.json", [{"kind": "x"}], producer="grader"
    )
    return {merged.path: merged, report.path: report, failure.path: failure}


def _ev(
    ref: c.ArtifactRef, claim_type: str = "summary", redacted: bool = False
) -> dict:
    return {
        "ref": {
            "path": ref.path,
            "sha256": ref.sha256,
            "bytes": ref.bytes,
            "media_type": ref.media_type,
            "producer": ref.producer,
        },
        "claim_type": claim_type,
        "digest": ref.sha256,
        "redacted": redacted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 fake chat model：驱动真实 DeepAgent（记录 tool inventory / 按行为出牌）
# ─────────────────────────────────────────────────────────────────────────────


class FakeDraftModel(BaseChatModel):
    """真实 report/recommendation role runner 的注入模型。

    - `behavior="ok_report"` / `"ok_recommendation"`：产出 schema-valid draft，
      evidence 引用注入的 allowlist ref（path + digest 精确命中）；
    - `behavior="cross_ref"`：draft evidence 引用 allowlist 之外的路径（必须拒）；
    - `behavior="wrong_role"`：输出其它 role 的 draft（runner 必须拒）；
    - `behavior="bad_digest"`：ref 在 allowlist 内但 digest 与 allowlist 不符
      （必须拒）。
    `seen_tools` 记录每次模型调用实际见到的工具名（tool inventory 断言）。
    """

    model_config: ClassVar[dict[str, Any]] = {"extra": "allow"}

    def __init__(
        self, *, behavior: str = "ok_report", allowlist: dict[str, c.ArtifactRef]
    ):
        super().__init__()
        self._behavior = behavior
        self._allowlist = dict(allowlist)
        self.seen_tools: list[list[str]] = []
        self.calls = 0

    @property
    def _llm_type(self) -> str:
        return "fake-draft-model"

    def _prompt_contract(self, messages) -> str:
        sysm = next((m for m in messages if getattr(m, "type", "") == "system"), None)
        raw = sysm.content if sysm is not None else ""
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
            )
        return (raw if isinstance(raw, str) else str(raw)).split(
            "## Role Contract (authoritative)"
        )[-1]

    def _one_ref(self) -> c.ArtifactRef:
        return next(iter(self._allowlist.values()))

    def _cross_ref(self) -> c.ArtifactRef:
        sha = "e" * 64
        return c.ArtifactRef(
            path="evidence/other-role/private.json",
            sha256=sha,
            bytes=12,
            media_type="application/json",
            producer="fake-draft-model",
        )

    def _report_args(self, role: str) -> dict:
        if self._behavior == "cross_ref":
            ref = self._cross_ref()
        else:
            ref = self._one_ref()
        return {
            "role": role,
            "invocation_id": str(uuid4()),
            "narrative": "coverage reached 0.8 per evidence",
            "paragraphs": [
                {
                    "text": "coverage reached 0.8",
                    "evidence": [_ev(ref, "summary", False)],
                }
            ],
            "evidence": [],
            "model_used": "fake-draft-model",
            "fallback_reason": None,
        }

    def _recommendation_args(self, role: str) -> dict:
        if self._behavior == "cross_ref":
            ref = self._cross_ref()
        else:
            ref = self._one_ref()
        return {
            "role": role,
            "invocation_id": str(uuid4()),
            "recommendations": [
                {
                    "text": "strengthen dispatch coverage in report",
                    "evidence": [_ev(ref, "recommendation", False)],
                    "failure_refs": [],
                    "severity": "warning",
                }
            ],
            "source": "recommendation_judge",
            "fallback_reason": None,
        }

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        tools = kwargs.get("tools", [])
        self.seen_tools.append(sorted(t.name for t in tools))
        if any(getattr(m, "type", "") == "tool" for m in messages):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="done"))]
            )
        contract = self._prompt_contract(messages)
        role_m = re.search(r"^role: (\S+)$", contract, re.MULTILINE)
        role = role_m.group(1) if role_m else "report_judge"
        cross = self._behavior == "cross_tool"
        if (role == "report_judge") != cross:
            args = self._report_args(role)
        else:
            args = self._recommendation_args(role)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=json.dumps(args)))]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# prompt assets：可解析 + canonical digest 稳定
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptAssets:
    def test_default_prompt_resolver_reads_both_prompts(self):
        for role, expect_digest in (
            (c.JudgeRole.REPORT_JUDGE, REPORT_JUDGE_PROMPT_SHA256),
            (c.JudgeRole.RECOMMENDATION_JUDGE, RECOMMENDATION_JUDGE_PROMPT_SHA256),
        ):
            text = roles._default_prompt_resolver(role)
            assert c.sha256_hex(text.encode("utf-8")) == expect_digest

    def test_report_prompt_contract_keywords(self):
        text = roles._default_prompt_resolver(c.JudgeRole.REPORT_JUDGE)
        assert "ReportNarrativeDraft" in text
        assert "read_report_evidence" in text
        assert "factual claim" in text or "factual claim" in text.lower()
        assert "allowlist" in text
        # 明确禁止 canonical writer / 编造证据
        assert "不编造" in text or "绝不编造" in text
        assert "redaction" in text or "redacted" in text

    def test_recommendation_prompt_contract_keywords(self):
        text = roles._default_prompt_resolver(c.JudgeRole.RECOMMENDATION_JUDGE)
        assert "RecommendationDraft" in text
        assert "read_frozen_ref" in text
        assert "failure" in text
        assert "evidence" in text
        # 只读 frozen refs，无 evidence 写工具，建议是 artifact-only 文本
        assert "写" in text and "artifact-only" in text

    def test_prompt_file_canonical_digest_stable(self, tmp_path):
        """prompt 文件字节 digest 与记录的 canonical 快照一致（改动即失败）。"""
        from sar_orch.eval.agent.roles import PROMPTS_DIR

        for name, expect in (
            ("report_judge.md", REPORT_JUDGE_PROMPT_SHA256),
            ("recommendation_judge.md", RECOMMENDATION_JUDGE_PROMPT_SHA256),
        ):
            data = (PROMPTS_DIR / name).read_bytes()
            assert c.sha256_hex(data) == expect
            # resolver 返回内容就是文件原文（canonical source of truth）
            role = (
                c.JudgeRole.REPORT_JUDGE
                if name.startswith("report")
                else c.JudgeRole.RECOMMENDATION_JUDGE
            )
            assert roles._default_prompt_resolver(role) == data.decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Draft schema 补齐校验：typed validation failure（无 LLM）
# ─────────────────────────────────────────────────────────────────────────────


class TestDraftSchemaValidation:
    def test_report_paragraph_without_evidence_ref_typed_failure(self):
        with pytest.raises(ValidationError):
            c.ReportParagraph(text="coverage reached 0.8", evidence=[])

    def test_report_paragraph_redaction_violation_typed_failure(self):
        sha = "a" * 64
        ev = c.EvidenceRef(
            ref=_artref("evidence/audit_summary.json", sha),
            claim_type=c.ClaimType.SUMMARY,
            digest=sha,
            redacted=True,
        )
        with pytest.raises(ValidationError):
            c.ReportParagraph(text="claim", evidence=[ev])

    def test_report_draft_without_evidence_ref_fails(self):
        with pytest.raises(ValidationError):
            c.ReportNarrativeDraft(
                role=c.JudgeRole.REPORT_JUDGE,
                invocation_id=uuid4(),
                narrative="coverage reached 0.8",
                paragraphs=[c.ReportParagraph(text="claim", evidence=[])],
                model_used="deepseek-v4-flash",
            )

    def test_report_evidence_claim_type_restricted(self):
        sha = "a" * 64
        ev = c.EvidenceRef(
            ref=_artref("evidence/steps_overview.json", sha),
            claim_type=c.ClaimType.SCORE,
            digest=sha,
        )
        with pytest.raises(ValidationError):
            c.ReportNarrativeDraft(
                role=c.JudgeRole.REPORT_JUDGE,
                invocation_id=uuid4(),
                narrative="x",
                evidence=[ev],
                model_used="m",
            )

    def test_report_paragraph_with_valid_evidence_ok(self):
        sha = "a" * 64
        ev = c.EvidenceRef(
            ref=_artref("evidence/steps_overview.json", sha),
            claim_type=c.ClaimType.SUMMARY,
            digest=sha,
            redacted=False,
        )
        para = c.ReportParagraph(text="coverage reached 0.8", evidence=[ev])
        assert para.evidence[0].claim_type is c.ClaimType.SUMMARY

    def test_judge_recommendation_item_without_basis_fails(self):
        with pytest.raises(ValidationError):
            c.RecommendationDraft(
                role=c.JudgeRole.RECOMMENDATION_JUDGE,
                invocation_id=uuid4(),
                recommendations=[c.RecommendationItem(text="add a rubric")],
                source=c.RecommendationSource.RECOMMENDATION_JUDGE,
            )

    def test_recommendation_evidence_claim_type_restricted(self):
        sha = "a" * 64
        ev = c.EvidenceRef(
            ref=_artref("evidence/steps_overview.json", sha),
            claim_type=c.ClaimType.SUMMARY,
            digest=sha,
        )
        with pytest.raises(ValidationError):
            c.RecommendationItem(
                text="add a rubric", evidence=[ev], severity=c.Severity.WARNING
            )

    def test_judge_recommendation_with_failure_basis_ok(self):
        item = c.RecommendationItem(
            text="address failure taxonomy",
            failure_refs=[
                c.FailureRef(kind="taxonomy", reason="unknown", source="typed")
            ],
            severity=c.Severity.CRITICAL,
        )
        draft = c.RecommendationDraft(
            role=c.JudgeRole.RECOMMENDATION_JUDGE,
            invocation_id=uuid4(),
            recommendations=[item],
            source=c.RecommendationSource.RECOMMENDATION_JUDGE,
        )
        assert draft.recommendations[0].failure_refs[0].kind == "taxonomy"


# ─────────────────────────────────────────────────────────────────────────────
# 受限 role runner：tool inventory + 禁项缺席
# ─────────────────────────────────────────────────────────────────────────────


class TestRoleRunnerToolInventory:
    def _runner(self, cls, tmp_path, behavior, allowlist):
        store = _store(tmp_path)
        manifest = _manifest()
        model = FakeDraftModel(behavior=behavior, allowlist=allowlist)
        runner = cls(store, manifest, model, roles._default_prompt_resolver)
        return runner, model

    def test_report_runner_sees_only_report_tool(self, tmp_path):
        store = _store(tmp_path)
        allowlist = _allowlist(store)
        runner, model = self._runner(
            roles.ReportRoleRunner, tmp_path, "ok_report", allowlist
        )
        import asyncio

        asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert model.seen_tools, "model must have been called"
        for seen in model.seen_tools:
            assert set(seen) == {"read_report_evidence"}, seen

    def test_recommendation_runner_sees_only_frozen_tool(self, tmp_path):
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        runner, model = self._runner(
            roles.RecommendationRoleRunner, tmp_path, "ok_recommendation", allowlist
        )
        import asyncio

        asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert model.seen_tools, "model must have been called"
        for seen in model.seen_tools:
            assert set(seen) == {"read_frozen_ref"}, seen

    def test_forbidden_default_builtins_never_reachable(self, tmp_path):
        for cls, behavior in (
            (roles.ReportRoleRunner, "ok_report"),
            (roles.RecommendationRoleRunner, "ok_recommendation"),
        ):
            store = _store(tmp_path)
            allowlist = (
                _allowlist(store)
                if cls is roles.ReportRoleRunner
                else _frozen_allowlist(store)
            )
            runner, model = self._runner(cls, tmp_path, behavior, allowlist)
            import asyncio

            asyncio.run(
                runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
            )
            forbidden = {"write_file", "edit_file", "execute", "task"}
            for seen in model.seen_tools:
                assert not (forbidden & set(seen)), (cls.__name__, seen)

    def test_no_evidence_write_tool_for_recommendation(self, tmp_path):
        """recommendation_judge 只有只读 frozen-ref 工具，没有任何 evidence 写工具。"""
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        runner, model = self._runner(
            roles.RecommendationRoleRunner, tmp_path, "ok_recommendation", allowlist
        )
        import asyncio

        asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        for seen in model.seen_tools:
            assert not any("write" in name for name in seen), seen
            assert seen == ["read_frozen_ref"], seen

    def test_tool_output_carries_sha256_metadata(self, tmp_path):
        """工具返回体必须以 `--- ref: <path> sha256=<digest> ---` 开头。

        模型从工具输出里即可拿到精确 digest 照抄（真实 smoke 曾因模型
        编造 digest 而 entire FAILED + fallback）。
        """
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        path, ref = next(iter(allowlist.items()))
        reader = roles.AllowlistedEvidenceReader(store, allowlist)
        body = roles._tool_read_body(reader, path)
        assert body.startswith(f"--- ref: {path} sha256={ref.sha256} ---\n")

    def test_tool_output_digest_matches_allowlist_entry(self, tmp_path):
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        path, ref = next(iter(allowlist.items()))
        reader = roles.AllowlistedEvidenceReader(store, allowlist)
        body = roles._tool_read_body(reader, path)
        header = body.splitlines()[0]
        assert f"sha256={ref.sha256}" in header

    def test_resolve_digest_mismatch_reports_both_digests(self, tmp_path):
        """digest 不匹配的失败消息必须暴露 expected/got 前缀，便于诊断。"""
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        _path, ref = next(iter(allowlist.items()))
        reader = roles.AllowlistedEvidenceReader(store, allowlist)
        bogus = ref.model_copy(update={"sha256": "f" * 64})
        with pytest.raises(roles.EvidenceNotAuthorized, match="digest mismatch"):
            reader.resolve(bogus)
        with pytest.raises(roles.EvidenceNotAuthorized, match="path unknown"):
            reader.resolve(
                c.ArtifactRef(
                    path="not/on/allowlist.json",
                    sha256="e" * 64,
                    bytes=1,
                    media_type="application/json",
                    producer="test",
                )
            )

    def test_patch_draft_refs_completes_bytes_from_allowlist(self, tmp_path):
        """模型输出 bytes=0（null 容错后）的 ref → runner 权威补全为 allowlist entry。

        `read_verified` 硬校验 bytes，模型无法知道文件大小，必须由 runner 补全。
        """
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        path, entry = next(iter(allowlist.items()))
        placeholder_ref = c.ArtifactRef(
            path=path,
            sha256=entry.sha256,
            bytes=0,  # coerce 后的中间态默认值
            media_type="application/json",
            producer="unknown",
        )
        ev = c.EvidenceRef(
            ref=placeholder_ref,
            claim_type=c.ClaimType.RECOMMENDATION,
            digest=entry.sha256,
        )
        draft = c.RecommendationDraft(
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
        runner = roles.RecommendationRoleRunner(
            store,
            _manifest(),
            FakeDraftModel(behavior="ok_recommendation", allowlist=allowlist),
            roles._default_prompt_resolver,
        )
        patched = runner._patch_draft_refs(draft, allowlist)
        patched_ev = patched.recommendations[0].evidence[0]
        assert patched_ev.ref.bytes == entry.bytes
        assert patched_ev.ref.media_type == entry.media_type
        assert patched_ev.ref.producer == entry.producer
        assert patched_ev.ref.sha256 == entry.sha256

    def test_coerce_evidence_refs_tolerates_null_metadata(self):
        """模型输出 ref.bytes/media_type=null → 容错为可解析默认值（后续权威补全）。"""
        data = {
            "evidence": [
                {"ref": {"path": "evidence/job-scoped/j1/x.json",
                          "sha256": "a" * 64, "bytes": None, "media_type": None},
                 "claim_type": "score", "digest": "a" * 64}
            ],
            "paragraphs": [
                {"text": "claim", "evidence": [
                    {"ref": {"path": "p.json", "sha256": "b" * 64},
                     "claim_type": "summary", "digest": "b" * 64}
                ]}
            ],
            "recommendations": [
                {"text": "rec", "evidence": [], "failure_refs": [
                    {"kind": "k", "reason": "r", "source": "typed",
                     "ref": {"path": "f.json", "sha256": "c" * 64}}
                ], "severity": "warning"}
            ],
        }
        out = roles._coerce_evidence_refs(data)
        top = out["evidence"][0]["ref"]
        para = out["paragraphs"][0]["evidence"][0]["ref"]
        fail = out["recommendations"][0]["failure_refs"][0]["ref"]
        for ref in (top, para, fail):
            assert ref["bytes"] == 0
            assert ref["media_type"] == "application/json"
            assert ref["producer"] == "unknown"
        # 已存在的值不动
        assert top["path"] == "evidence/job-scoped/j1/x.json"

    def test_coerce_evidence_refs_guards_non_dict_elements(self):
        """B1：paragraphs/recommendations 含非 dict 元素 → 不崩溃（走 typed FAILED）。"""
        data = {
            "paragraphs": ["plain string paragraph", None, 42],
            "recommendations": ["rec string", None],
            "evidence": [],
        }
        out = roles._coerce_evidence_refs(data)
        assert out == data
        # 合法元素不受影响
        mixed = {
            "paragraphs": [
                {"text": "ok", "evidence": [
                    {"ref": {"path": "p.json", "sha256": "b" * 64},
                     "claim_type": "summary", "digest": "b" * 64}
                ]},
                "junk",
            ]
        }
        out2 = roles._coerce_evidence_refs(mixed)
        assert out2["paragraphs"][0]["evidence"][0]["ref"]["bytes"] == 0
        assert out2["paragraphs"][1] == "junk"

    def test_patch_draft_refs_report_path_completes_bytes(self, tmp_path):
        """_patch_draft_refs 的 report 路径：顶层 evidence + paragraphs[].evidence。"""
        store = _store(tmp_path)
        allowlist = _allowlist(store)
        path, entry = next(iter(allowlist.items()))
        placeholder = c.ArtifactRef(
            path=path,
            sha256=entry.sha256,
            bytes=0,
            media_type="application/json",
            producer="unknown",
        )
        top_ev = c.EvidenceRef(
            ref=placeholder, claim_type=c.ClaimType.SUMMARY, digest=entry.sha256
        )
        para_ev = c.EvidenceRef(
            ref=placeholder.model_copy(update={"producer": "model"}),
            claim_type=c.ClaimType.OBSERVATION,
            digest=entry.sha256,
        )
        draft = c.ReportNarrativeDraft(
            role=c.JudgeRole.REPORT_JUDGE,
            invocation_id=uuid4(),
            narrative="coverage reached 0.8",
            evidence=[top_ev],
            paragraphs=[c.ReportParagraph(text="claim", evidence=[para_ev])],
            model_used="fake",
        )
        runner = roles.ReportRoleRunner(
            store,
            _manifest(),
            FakeDraftModel(behavior="ok_report", allowlist=allowlist),
            roles._default_prompt_resolver,
        )
        patched = runner._patch_draft_refs(draft, allowlist)
        assert patched.evidence[0].ref.bytes == entry.bytes
        assert patched.evidence[0].ref.producer == entry.producer
        assert patched.paragraphs[0].evidence[0].ref.bytes == entry.bytes
        assert patched.paragraphs[0].evidence[0].ref.producer == entry.producer


# ─────────────────────────────────────────────────────────────────────────────
# 每次 invocation：独立 agent_id / thread / backend / 空 history
# ─────────────────────────────────────────────────────────────────────────────


class TestInvocationIsolation:
    def test_per_invocation_isolated(self, tmp_path):
        for cls, allow_builder in (
            (roles.ReportRoleRunner, _allowlist),
            (roles.RecommendationRoleRunner, _frozen_allowlist),
        ):
            store = _store(tmp_path)
            allowlist = allow_builder(store)
            runner = cls(
                store,
                _manifest(),
                FakeDraftModel(behavior="ok_report", allowlist=allowlist),
                roles._default_prompt_resolver,
            )
            import asyncio

            async def _two_calls(runner=runner, allowlist=allowlist):
                a = await runner.run(
                    invocation_id=uuid4(), node_attempt=1, allowlist=allowlist
                )
                b = await runner.run(
                    invocation_id=uuid4(), node_attempt=2, allowlist=allowlist
                )
                return a, b

            a, b = asyncio.run(_two_calls())
            assert a.status is roles.RunnerStatus.SUCCEEDED
            assert b.status is roles.RunnerStatus.SUCCEEDED
            assert a.draft is not None and b.draft is not None
            inv_a, inv_b = runner.invocations
            assert inv_a["thread_id"] != inv_b["thread_id"]
            assert inv_a["agent_id"] != inv_b["agent_id"]
            assert inv_a["backend"] != inv_b["backend"]
            assert inv_a["history_len"] == 0 and inv_b["history_len"] == 0
            assert inv_a["node_attempt"] == 1 and inv_b["node_attempt"] == 2
            assert str(a.draft.invocation_id) in inv_a["thread_id"]
            # prompt digest 进入身份
            assert inv_a["prompt_digest"] == inv_b["prompt_digest"]


# ─────────────────────────────────────────────────────────────────────────────
# 非法 draft / 越权 evidence → typed FAILED（不污染成功输出）
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidDraftNoPollution:
    def test_cross_role_ref_failed_report(self, tmp_path):
        store = _store(tmp_path)
        allowlist = _allowlist(store)
        runner = roles.ReportRoleRunner(
            store,
            _manifest(),
            FakeDraftModel(behavior="cross_ref", allowlist=allowlist),
            roles._default_prompt_resolver,
        )
        import asyncio

        out = asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert out.status is roles.RunnerStatus.FAILED
        assert "not authorized" in (out.error or "") or "allowlist" in (out.error or "")
        assert out.draft is None

    def test_cross_role_ref_failed_recommendation(self, tmp_path):
        store = _store(tmp_path)
        allowlist = _frozen_allowlist(store)
        runner = roles.RecommendationRoleRunner(
            store,
            _manifest(),
            FakeDraftModel(behavior="cross_ref", allowlist=allowlist),
            roles._default_prompt_resolver,
        )
        import asyncio

        out = asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert out.status is roles.RunnerStatus.FAILED
        assert out.draft is None

    def test_digest_mismatch_failed(self, tmp_path):
        """ref 在 allowlist 内但 digest 与 allowlist 不符 → typed FAILED。"""
        store = _store(tmp_path)
        allowlist = _allowlist(store)
        # 构造与 allowlist 同 path、不同 digest 的 ref
        first = next(iter(allowlist.values()))
        tampered = first.model_copy(update={"sha256": "d" * 64})
        runner = roles.ReportRoleRunner(
            store,
            _manifest(),
            FakeDraftModel(behavior="ok_report", allowlist={tampered.path: tampered}),
            roles._default_prompt_resolver,
        )
        # runner 的 allowlist 是真实 digest；模型引用的 tampered ref 与之不符
        import asyncio

        out = asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert out.status is roles.RunnerStatus.FAILED
        assert "evidence_not_authorized" in (out.error or "")
        assert out.draft is None

    def test_wrong_role_draft_failed(self, tmp_path):
        """模型输出其它角色的结构化 draft 类型 → typed FAILED。"""
        store = _store(tmp_path)
        allowlist = _allowlist(store)
        model = FakeDraftModel(behavior="cross_tool", allowlist=allowlist)
        runner = roles.ReportRoleRunner(
            store, _manifest(), model, roles._default_prompt_resolver
        )
        import asyncio

        out = asyncio.run(
            runner.run(invocation_id=uuid4(), node_attempt=1, allowlist=allowlist)
        )
        assert out.status is roles.RunnerStatus.FAILED
        assert out.draft is None
        assert "structured" in (out.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# factory 必须显式注入 model；绝不静默构造 LLM
# ─────────────────────────────────────────────────────────────────────────────


class TestFactoryRequiresModel:
    def test_report_factory_without_model_rejects(self, tmp_path):
        store = _store(tmp_path)
        runtime = SimpleNamespace(store=store, manifest=_manifest())
        with pytest.raises(roles.RoleRunnerError, match="explicit model"):
            roles.make_report_role_runner_factory(runtime)

    def test_recommendation_factory_without_model_rejects(self, tmp_path):
        store = _store(tmp_path)
        runtime = SimpleNamespace(store=store, manifest=_manifest())
        with pytest.raises(roles.RoleRunnerError, match="explicit model"):
            roles.make_recommendation_role_runner_factory(runtime)

    def test_factory_injects_model_ok(self, tmp_path):
        store = _store(tmp_path)
        runtime = SimpleNamespace(store=store, manifest=_manifest())
        allowlist = _allowlist(store)
        model = FakeDraftModel(behavior="ok_report", allowlist=allowlist)
        factory = roles.make_report_role_runner_factory(
            runtime, model=model, prompt_resolver=roles._default_prompt_resolver
        )
        runner = factory()
        assert isinstance(runner, roles.ReportRoleRunner)


# ─────────────────────────────────────────────────────────────────────────────
# _extract_usage：provider usage_metadata → UsageSnapshot 聚合（真实 runner 回传）
# ─────────────────────────────────────────────────────────────────────────────


def _usage_message(input_tokens=0, output_tokens=0, total_tokens=0, cache_read=0):
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_token_details": {"cache_read": cache_read, "cache_creation": 0},
        },
    )


class TestUsageExtraction:
    def test_aggregates_multiple_ai_messages(self):
        result = {
            "messages": [
                _usage_message(input_tokens=100, output_tokens=50, total_tokens=150),
                _usage_message(input_tokens=60, output_tokens=30, total_tokens=90),
            ]
        }
        usage = roles._extract_usage(result)
        assert usage is not None
        assert usage.prompt_tokens == 160
        assert usage.completion_tokens == 80
        assert usage.total_tokens == 240
        assert usage.cache_hit_tokens == 0

    def test_cache_miss_derived_from_input_minus_cache_read(self):
        result = {
            "messages": [
                _usage_message(
                    input_tokens=100, output_tokens=50, total_tokens=150, cache_read=40
                )
            ]
        }
        usage = roles._extract_usage(result)
        assert usage is not None
        assert usage.cache_hit_tokens == 40
        assert usage.cache_miss_tokens == 60

    def test_ignores_tool_and_human_messages(self):
        from langchain_core.messages import ToolMessage

        result = {
            "messages": [
                _usage_message(input_tokens=100, output_tokens=50, total_tokens=150),
                ToolMessage(content="tool result", tool_call_id="t1"),
            ]
        }
        usage = roles._extract_usage(result)
        assert usage is not None
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50

    def test_no_usage_reported_returns_none(self):
        result = {"messages": [AIMessage(content="no metadata")]}
        assert roles._extract_usage(result) is None

    def test_empty_messages_returns_none(self):
        assert roles._extract_usage({"messages": []}) is None

    def test_cache_hit_exceeding_input_returns_none_miss(self):
        """S4：cache_read > input（异常 provider 数据）→ cache_miss=None，不伪造负数。"""
        result = {
            "messages": [
                _usage_message(
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    cache_read=160,
                )
            ]
        }
        usage = roles._extract_usage(result)
        assert usage is not None
        assert usage.cache_hit_tokens == 160
        assert usage.cache_miss_tokens is None
