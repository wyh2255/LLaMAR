"""P4 受限 DeepAgent role runner 与 job-scoped evidence reader（设计 §4、§9 P4）。

- `EvidenceNotAuthorized`：跨 job / 绝对路径 / legacy workspace / 非 allowlist ref
  的 typed 拒绝（携带 `evidence_not_authorized`）。
- `JobEvidenceReader`：job-scoped evidence 工厂。只经 `ArtifactStore.read_verified(ref)`
  访问 ScoreJob allowlist（`evidence/job-scoped/<job_id>/` 命名空间 + job input
  bundle），**绝不复用** `sar_orch.eval.agent.tools` 的模块级 `_episode/_workspace_dir`。
- `ScoreRoleRunner`（`AgentRunner`）：每次 invocation 独立 agent_id / thread /
  backend / 空 history；通过 `HarnessProfile` 显式排除默认 built-ins
  （`write_file`、`edit_file`、`execute`、`task` 及其余默认工具），禁用
  general-purpose subagent；唯一工具是 job reader；输出用结构化 `ScoreDraft`。
  非法 evidence / 非法 draft 一律转为 typed FAILED/UNKNOWN outcome，绝不返回
  携带越权证据的成功 draft。
- `make_role_runner_factory(runtime, *, model, prompt_resolver)`：适配 workflow 的
  `runner_factory`。`model` 必须显式注入 —— 未提供则抛 `RoleRunnerError`，
  绝不静默构造 LLM。

禁止：本模块不得在未显式注入 model 时构造 DeepAgent；job evidence 只能经
`ArtifactStore.read_verified` 访问。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval.agent.runner import (
    AgentRunner,
    RunnerError,
    RunnerFactory,
    RunnerOutcome,
    RunnerStatus,
)

__all__ = [
    "EXCLUDED_ROLE_TOOLS",
    "AllowlistedEvidenceReader",
    "DraftRoleOutcome",
    "EvidenceNotAuthorized",
    "JobEvidenceReader",
    "RecommendationRoleRunner",
    "ReportRoleRunner",
    "RoleRunnerError",
    "ScoreRoleRunner",
    "make_frozen_ref_tool",
    "make_job_reader_tool",
    "make_recommendation_role_runner_factory",
    "make_report_evidence_tool",
    "make_report_role_runner_factory",
    "make_role_runner_factory",
    "materialize_job_evidence",
]

# 显式排除的默认 built-ins：4 个硬性红线（write_file/edit_file/execute/task）+
# 其余默认工具（read_file/ls/glob/grep/write_todos），保证实际 tool inventory
# 只有 job reader + 结构化输出（设计 §4：工具清单本身不是权限控制，但必须最窄）。
EXCLUDED_ROLE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "execute",
        "task",
        "read_file",
        "ls",
        "glob",
        "grep",
        "write_todos",
    }
)

_EVIDENCE_NAMESPACE_PREFIX = "evidence/job-scoped/"
_LEGACY_WORKSPACE_DIRS = ("eval_workspace",)


class RoleRunnerError(ValueError):
    """角色 runner 构造 / 绑定违例（不静默降级）。"""


class EvidenceNotAuthorized(a.ArtifactError):
    """job-scoped evidence 越权访问（跨 job / 绝对路径 / legacy workspace）。"""


# ─────────────────────────────────────────────────────────────────────────────
# Job-scoped evidence reader（§4）：只经 ArtifactStore.read_verified 访问 allowlist
# ─────────────────────────────────────────────────────────────────────────────


class JobEvidenceReader:
    """ScoreJob allowlist evidence 工厂。

    allowlist = 已物化的 `evidence/job-scoped/<job_id>/` 文件 + job 的
    `input_bundle_ref`（共享 evidence bundle）。其他 job 的 evidence、绝对路径、
    traversal、legacy `eval_workspace/` 一律 `EvidenceNotAuthorized`。
    """

    def __init__(self, store: a.ArtifactStore, job: c.ScoreJob) -> None:
        self._store = store
        self._job = job
        self._namespace = f"{_EVIDENCE_NAMESPACE_PREFIX}{job.job_id}/"

    @property
    def job_id(self) -> str:
        return str(self._job.job_id)

    @property
    def namespace(self) -> str:
        return self._namespace

    def authorized_refs(self) -> dict[str, c.ArtifactRef]:
        """job allowlist refs：命名空间内已落盘文件 + input bundle（path→ref）。"""
        refs: dict[str, c.ArtifactRef] = {}
        root = Path(self._store.root).joinpath(self._namespace)
        for p in sorted(root.glob("*.json")):
            rel = f"{self._namespace}{p.name}"
            data = self._store.read_bytes(rel)
            refs[rel] = c.ArtifactRef(
                path=rel,
                sha256=c.sha256_hex(data),
                bytes=len(data),
                media_type="application/json",
                producer="role-runner",
            )
        refs[self._job.input_bundle_ref.path] = self._job.input_bundle_ref
        return refs

    def resolve(self, ref: c.ArtifactRef) -> c.ArtifactRef:
        """ref 必须精确命中本 job allowlist（path + sha256）；否则拒绝。"""
        allow = self.authorized_refs()
        entry = allow.get(ref.path)
        if entry is None or entry.sha256 != ref.sha256:
            raise EvidenceNotAuthorized(
                f"evidence_not_authorized: ref {ref.path!r} is not in job "
                f"{self.job_id} allowlist"
            )
        return entry

    def read_verified(self, ref: c.ArtifactRef) -> bytes:
        return self._store.read_verified(self.resolve(ref))

    def read_by_path(self, path: str) -> bytes:
        allow = self.authorized_refs()
        ref = allow.get(path)
        if ref is None:
            raise EvidenceNotAuthorized(
                f"evidence_not_authorized: {path!r} is not in job {self.job_id} allowlist"
            )
        return self._store.read_verified(ref)


def _reject_tool_path(path: str) -> None:
    """job reader 工具入参前置检查：绝对路径 / traversal / legacy workspace。

    allowlist 的精确匹配由 `JobEvidenceReader.read_by_path` 负责（含跨 job 拒绝）；
    这里只挡明显非法的形态。
    """
    if not path or not isinstance(path, str):
        raise EvidenceNotAuthorized(
            "evidence_not_authorized: path must be a non-empty string"
        )
    if path.startswith(("/", "\\")) or "\\" in path:
        raise EvidenceNotAuthorized(
            "evidence_not_authorized: absolute or backslash paths are not allowed"
        )
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise EvidenceNotAuthorized(
            "evidence_not_authorized: path must be canonical (no empty/. /.. segments)"
        )
    if segments[0] in _LEGACY_WORKSPACE_DIRS:
        raise EvidenceNotAuthorized(
            "evidence_not_authorized: legacy eval_workspace is not job-scoped evidence"
        )


def make_job_reader_tool(reader: JobEvidenceReader) -> BaseTool:
    """job-scoped evidence reader 的 langchain tool（每个 job 一个绑定实例）。"""

    @tool
    def read_job_evidence(path: str) -> str:
        """Read job-scoped evidence for the current score job.

        Args:
            path: attempt-relative path of an authorized evidence file, e.g.
                `evidence/job-scoped/<job_id>/<dimension>.json`, or the job's
                input bundle.

        Returns:
            The evidence text, or an `evidence_not_authorized` error string.
        """
        try:
            _reject_tool_path(path)
            data = reader.read_by_path(path)
        except EvidenceNotAuthorized as exc:
            return str(exc)
        return data.decode("utf-8")

    return read_job_evidence


def materialize_job_evidence(
    store: a.ArtifactStore, job: c.ScoreJob, dimensions: list[str]
) -> dict[str, c.ArtifactRef]:
    """把 job 的 evidence bundle 物化为每维度 job-scoped evidence 文件（确定性）。

    只读 `job.input_bundle_ref`（evidence materializer 已落盘的共享 bundle），
    每个 rubric 维度一个文件，内容与维度一一对应；同名内容写入幂等（字节不变
    → digest 不变），供 ScoreDraft evidence 引用与 resume 重放。
    """
    bundle = json.loads(store.read_verified(job.input_bundle_ref).decode("utf-8"))
    refs: dict[str, c.ArtifactRef] = {}
    for dim in dimensions:
        rel = f"{_EVIDENCE_NAMESPACE_PREFIX}{job.job_id}/{dim}.json"
        content = {
            "job_id": str(job.job_id),
            "role": job.role.value,
            "target_type": job.target_type.value,
            "dimension": dim,
            "evidence": bundle,
        }
        refs[rel] = store.write_canonical_json(rel, content, producer="role-runner")
    return refs


# ─────────────────────────────────────────────────────────────────────────────
# 受限 DeepAgent role runner（AgentRunner protocol）
# ─────────────────────────────────────────────────────────────────────────────

PromptResolver = Callable[[c.JudgeRole], str]

_ROLE_PROMPT_FILES: dict[c.JudgeRole, str] = {
    c.JudgeRole.DISPATCH_SCORE_JUDGE: "dispatch_judge.md",
    c.JudgeRole.OBSERVATION_SCORE_JUDGE: "observation_judge.md",
    c.JudgeRole.REPORT_JUDGE: "report_judge.md",
    c.JudgeRole.RECOMMENDATION_JUDGE: "recommendation_judge.md",
}

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _default_prompt_resolver(role: c.JudgeRole) -> str:
    name = _ROLE_PROMPT_FILES.get(role)
    if name is None:
        raise RoleRunnerError(f"no default prompt file for role {role.value}")
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleRunnerError(f"cannot read role prompt {path}: {exc}") from exc


def _profile_key(model: BaseChatModel) -> str:
    """DeepAgents profile 查找 key：优先 provider；取不到时退化为类型名。"""
    from deepagents._models import get_model_provider

    provider = get_model_provider(model)
    if provider:
        return provider
    return type(model).__name__.lower()


class ScoreRoleRunner:
    """受限 score role runner：每次 invocation 独立 agent/thread/backend/空 history。

    - `run(job, ...)` 首次调用绑定 job（runner 每 job 构造一次）；再绑定不同
      job → `RunnerError`（绝不跨 job 复用 evidence view）。
    - 每次 invocation 物化 job-scoped evidence、构建全新受限 DeepAgent、
      `response_format=c.ScoreDraft`，以空 history + 独立 thread_id 调用。
    - 结构化输出解析后**再次**校验 role / dimensions / evidence allowlist；
      任一非法 → `RunnerOutcome(FAILED)`，绝不返回可被当作成功 pollute 的 draft。
    - `invocations` 暴露可观测记录（agent_id/thread_id/backend/history_len），
      供 fake-runner 级别的隔离断言。
    """

    def __init__(
        self,
        store: a.ArtifactStore,
        manifest: c.FrozenInputManifest,
        model: BaseChatModel,
        prompt_resolver: PromptResolver,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._model = model
        self._prompt_resolver = prompt_resolver
        self._bound_job: c.ScoreJob | None = None
        self._profile_key = _profile_key(model)
        self._register_role_profile()
        self.invocations: list[dict[str, Any]] = []

    # ── 受限 harness（§4）：排除默认 built-ins + 禁用 general-purpose subagent ──
    def _register_role_profile(self) -> str:
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )

        register_harness_profile(
            self._profile_key,
            HarnessProfile(
                excluded_tools=EXCLUDED_ROLE_TOOLS,
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
        return self._profile_key

    # ── 冻结 manifest 内 rubric 解析 ─────────────────────────────────────────
    def _rubric(self, job: c.ScoreJob) -> c.RubricSpec:
        for r in self._manifest.manifest.rubrics:
            if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
                return r
        raise RoleRunnerError(
            f"rubric {job.rubric_id}/{job.rubric_digest[:8]} not in frozen manifest"
        )

    def _system_prompt(
        self, job: c.ScoreJob, rubric: c.RubricSpec, refs: dict[str, c.ArtifactRef]
    ) -> str:
        base = self._prompt_resolver(job.role)
        available = "\n".join(sorted(refs))
        return (
            f"{base}\n\n"
            f"## Job Contract (authoritative)\n"
            f"role: {job.role.value}\n"
            f"job_id: {job.job_id}\n"
            f"target_type: {job.target_type.value}\n"
            f"dimensions: {json.dumps(rubric.dimensions)}\n"
            f"available evidence files:\n{available}\n"
            f"Your structured output must be a ScoreDraft. Cite each scored "
            f"dimension with exactly one evidence file from the list above; "
            f"never reference evidence from another job."
        )

    def _invocation_backend(self, job: c.ScoreJob, invocation_id: UUID):
        """每次 invocation 独立 backend root（filesystem 工具已排除，纯隔离兜底）。"""
        from deepagents import backends

        root = self._store.contained_path(
            f"agent_runs/{job.role.value}/{invocation_id}/fs"
        )
        return backends.FilesystemBackend(root_dir=root, virtual_mode=True)

    def _build_agent(
        self,
        job: c.ScoreJob,
        tool: BaseTool,
        refs: dict[str, c.ArtifactRef],
        invocation_id: UUID,
    ):
        from deepagents import create_deep_agent

        # 每次 invocation 独立 agent_id（含 invocation_id，便于 trace 区分）。
        agent_id = f"{job.role.value}-{str(job.job_id)[:8]}-{invocation_id}"
        rubric = self._rubric(job)
        return create_deep_agent(
            model=self._model,
            tools=[tool],
            system_prompt=self._system_prompt(job, rubric, refs),
            response_format=c.ScoreDraft,
            backend=self._invocation_backend(job, invocation_id),
            name=agent_id,
        )

    # ── evidence / draft 校验（runner 侧 fail-closed，workflow validator 兜底） ──
    def _resolve_evidence(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        response_evidence: list[c.EvidenceRef],
    ) -> tuple[list[c.EvidenceRef] | None, str | None]:
        """解析 draft evidence；非法 → (None, reason)。

        - 模型显式给出 evidence refs：每条必须精确命中本 job allowlist
          （path + sha256），否则 `evidence_not_authorized`；
        - 模型未给：按 rubric dimensions 顺序自动挂接本 job 已物化的每维度 ref
          （严格一一对应，绝不静默丢弃/伪造其它 job 的 evidence）。
        """
        reader = JobEvidenceReader(self._store, job)
        if response_evidence:
            allow = reader.authorized_refs()
            for ev in response_evidence:
                entry = allow.get(ev.ref.path)
                if entry is None or entry.sha256 != ev.digest:
                    return None, (
                        f"evidence ref {ev.ref.path!r} not authorized for job "
                        f"{job.job_id} (evidence_not_authorized)"
                    )
                if ev.claim_type is not c.ClaimType.SCORE:
                    return None, (
                        f"evidence ref {ev.ref.path!r} claim_type "
                        f"{ev.claim_type.value} is not SCORE"
                    )
            return list(response_evidence), None
        if not rubric.dimensions:
            return [], None
        evidence: list[c.EvidenceRef] = []
        for dim in rubric.dimensions:
            rel = f"{_EVIDENCE_NAMESPACE_PREFIX}{job.job_id}/{dim}.json"
            ref = refs.get(rel)
            if ref is None:
                return None, f"missing materialized evidence for dimension {dim!r}"
            evidence.append(
                c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=ref.sha256)
            )
        return evidence, None

    def _to_outcome(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        result: dict[str, Any],
        invocation_id: UUID,
    ) -> RunnerOutcome:
        sr = result.get("structured_response")
        if sr is None:
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error="role runner: model returned no structured ScoreDraft",
                model_requested=True,
            )
        if not isinstance(sr, c.ScoreDraft):
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=f"role runner: unexpected structured output type {type(sr).__name__}",
                model_requested=True,
            )
        if sr.role is not job.role:
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=f"draft role {sr.role.value} != job role {job.role.value}",
                model_requested=True,
            )
        if sr.dimensions and set(sr.dimensions) != set(rubric.dimensions):
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=(
                    f"draft dimensions {sorted(sr.dimensions)} != rubric "
                    f"dimensions {sorted(rubric.dimensions)}"
                ),
                model_requested=True,
            )
        # abstain（空 dimensions）不带 evidence；评分才按维度一一挂接本 job 证据。
        evidence: list[c.EvidenceRef] = []
        if sr.dimensions:
            resolved, err = self._resolve_evidence(job, rubric, refs, sr.evidence)
            if err is not None:
                return RunnerOutcome(
                    status=RunnerStatus.FAILED,
                    error=f"role runner: {err}",
                    model_requested=True,
                )
            evidence = resolved or []
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=invocation_id,
            dimensions=sr.dimensions,
            evidence=evidence or [],
            model_used=getattr(self._model, "_llm_type", None)
            or type(self._model).__name__,
            unknown_reason=sr.unknown_reason,
        )
        status = RunnerStatus.UNKNOWN if not sr.dimensions else RunnerStatus.SUCCEEDED
        return RunnerOutcome(status=status, draft=draft, model_requested=True)

    # ── AgentRunner protocol ─────────────────────────────────────────────────
    async def run(
        self, job: c.ScoreJob, *, invocation_id: UUID, node_attempt: int
    ) -> RunnerOutcome:
        if self._bound_job is None:
            self._bound_job = job
        elif str(self._bound_job.job_id) != str(job.job_id):
            raise RunnerError(
                f"role runner bound to job {self._bound_job.job_id} but called "
                f"for job {job.job_id}",
                retryable=False,
            )
        job = self._bound_job
        rubric = self._rubric(job)
        refs = materialize_job_evidence(self._store, job, rubric.dimensions)
        reader = JobEvidenceReader(self._store, job)
        tool = make_job_reader_tool(reader)
        thread_id = f"eval-role-{job.role.value}-{str(job.job_id)[:8]}-{invocation_id}"
        agent = self._build_agent(job, tool, refs, invocation_id)
        self.invocations.append(
            {
                "invocation_id": str(invocation_id),
                "job_id": str(job.job_id),
                "agent_id": agent.name,
                "thread_id": thread_id,
                "backend": f"filesystem://agent_runs/{job.role.value}/{invocation_id}/fs",
                "history_len": 0,
                "node_attempt": node_attempt,
                "profile_key": self._profile_key,
                "shared_model_warning": (
                    "allow_shared_model=true: role shares a model profile; "
                    "role distinctness is contextual, not guaranteed by profile equality"
                    if self._manifest.manifest.policy.allow_shared_model
                    else None
                ),
            }
        )
        try:
            result = await agent.ainvoke(
                {"messages": []},
                config={"configurable": {"thread_id": thread_id}},
            )
        except Exception as exc:  # noqa: BLE001 - 模型调用边界的宽捕获（CancelledError 不在此列）
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=f"role runner invocation failed: {type(exc).__name__}: {exc}",
                model_requested=True,
            )
        return self._to_outcome(job, rubric, refs, result, invocation_id)


class AllowlistedEvidenceReader:
    """角色级 allowlist evidence reader：只读显式注入的 allowlist refs。

    与 `JobEvidenceReader` 的差异：allowlist 不从 job 命名空间推导，而是由调用方
    显式注入 —— report_judge 注入 merged bundle + allowlisted evidence/audit
    summary；recommendation_judge 注入 frozen merged/report/failure refs。同样只经
    `ArtifactStore.read_verified` 访问；allowlist 之外的路径/digest →
    `EvidenceNotAuthorized`（typed `evidence_not_authorized`）。
    """

    def __init__(self, store: a.ArtifactStore, refs: dict[str, c.ArtifactRef]) -> None:
        self._store = store
        self._refs = dict(refs)

    def authorized_refs(self) -> dict[str, c.ArtifactRef]:
        return dict(self._refs)

    def resolve(self, ref: c.ArtifactRef) -> c.ArtifactRef:
        entry = self._refs.get(ref.path)
        if entry is None or entry.sha256 != ref.sha256:
            raise EvidenceNotAuthorized(
                f"evidence_not_authorized: ref {ref.path!r} is not in the "
                f"role allowlist"
            )
        return entry

    def read_verified(self, ref: c.ArtifactRef) -> bytes:
        return self._store.read_verified(self.resolve(ref))

    def read_by_path(self, path: str) -> bytes:
        ref = self._refs.get(path)
        if ref is None:
            raise EvidenceNotAuthorized(
                f"evidence_not_authorized: {path!r} is not in the role allowlist"
            )
        return self._store.read_verified(ref)


def make_report_evidence_tool(reader: AllowlistedEvidenceReader) -> BaseTool:
    """report_judge 的只读 allowlist reader 工具（merged + evidence/audit summary）。"""

    @tool
    def read_report_evidence(path: str) -> str:
        """Read allowlisted evidence / audit summary for the report_judge role.

        Args:
            path: attempt-relative path of an allowlisted artifact, e.g. the merged
                score bundle or an allowlisted evidence/audit summary file.

        Returns:
            The artifact text, or an `evidence_not_authorized` error string.
        """
        try:
            _reject_tool_path(path)
            data = reader.read_by_path(path)
        except EvidenceNotAuthorized as exc:
            return str(exc)
        return data.decode("utf-8")

    return read_report_evidence


def make_frozen_ref_tool(reader: AllowlistedEvidenceReader) -> BaseTool:
    """recommendation_judge 的只读 frozen-refs reader 工具（merged/report/failure）。"""

    @tool
    def read_frozen_ref(path: str) -> str:
        """Read a frozen merged/report/failure ref for the recommendation_judge role.

        Args:
            path: attempt-relative path of a frozen ref (merged bundle, final
                report, or failure refs) that the role is allowed to read.

        Returns:
            The artifact text, or an `evidence_not_authorized` error string.
        """
        try:
            _reject_tool_path(path)
            data = reader.read_by_path(path)
        except EvidenceNotAuthorized as exc:
            return str(exc)
        return data.decode("utf-8")

    return read_frozen_ref


@dataclass(frozen=True)
class DraftRoleOutcome:
    """report/recommendation 角色的一次调用结果。

    `draft` 是 `ReportNarrativeDraft` 或 `RecommendationDraft`（不同于
    `RunnerOutcome.draft` 的 ScoreDraft 类型）；仅当 status=SUCCEEDED/UNKNOWN
    时有效。非法 draft / 越权 evidence → `validation_errors` 或 `error` 的
    typed FAILED，绝不携带越权 draft。
    """

    status: RunnerStatus
    draft: c.ReportNarrativeDraft | c.RecommendationDraft | None = None
    error: str | None = None
    model_requested: bool = True
    validation_errors: list[str] = field(default_factory=list)


class _DraftRoleRunner:
    """report/recommendation 角色共享的受限 runner 基类（§4 deferred roles）。

    - 显式注入 model（未提供 → `RoleRunnerError`，绝不静默构造 LLM）；
    - 唯一工具是 role-scoped 只读 allowlist reader —— 没有任何 evidence 写工具，
      且 `EXCLUDED_ROLE_TOOLS` 排除默认 built-ins / 通用 subagent；
    - 每次 invocation 独立 agent_id / thread / backend / 空 history；
    - structured draft 的每个 evidence/failure ref 必须命中注入的 allowlist
      （path + digest），否则 typed FAILED。
    """

    role: c.JudgeRole
    draft_cls: type  # c.ReportNarrativeDraft | c.RecommendationDraft
    tool_name: str

    def __init__(
        self,
        store: a.ArtifactStore,
        manifest: c.FrozenInputManifest,
        model: BaseChatModel,
        prompt_resolver: PromptResolver,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._model = model
        self._prompt_resolver = prompt_resolver
        self._profile_key = _profile_key(model)
        self._register_role_profile()
        self.invocations: list[dict[str, Any]] = []

    def _register_role_profile(self) -> str:
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )

        register_harness_profile(
            self._profile_key,
            HarnessProfile(
                excluded_tools=EXCLUDED_ROLE_TOOLS,
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
        return self._profile_key

    def _system_prompt(self, allowlist: dict[str, c.ArtifactRef]) -> str:
        base = self._prompt_resolver(self.role)
        available = "\n".join(sorted(allowlist))
        return (
            f"{base}\n\n"
            f"## Role Contract (authoritative)\n"
            f"role: {self.role.value}\n"
            f"available allowlisted inputs:\n{available}\n"
            f"Your structured output must be a {self.draft_cls.__name__}. "
            f"Cite every factual claim with an allowlisted input from the list "
            f"above; never reference evidence outside the allowlist."
        )

    def _invocation_backend(self, invocation_id: UUID):
        from deepagents import backends

        root = self._store.contained_path(
            f"agent_runs/{self.role.value}/{invocation_id}/fs"
        )
        return backends.FilesystemBackend(root_dir=root, virtual_mode=True)

    def _build_agent(
        self,
        tool: BaseTool,
        allowlist: dict[str, c.ArtifactRef],
        invocation_id: UUID,
    ):
        from deepagents import create_deep_agent

        agent_id = f"{self.role.value}-{str(invocation_id)[:8]}-{invocation_id}"
        return create_deep_agent(
            model=self._model,
            tools=[tool],
            system_prompt=self._system_prompt(allowlist),
            response_format=self.draft_cls,
            backend=self._invocation_backend(invocation_id),
            name=agent_id,
        )

    def _make_tool(self, reader: AllowlistedEvidenceReader) -> BaseTool:
        raise NotImplementedError

    def _resolve_refs(
        self, allowlist: dict[str, c.ArtifactRef], refs: list[c.ArtifactRef]
    ) -> tuple[list[c.ArtifactRef] | None, str | None]:
        """每个 ref 必须精确命中 allowlist（path + digest），否则 typed 拒绝。"""
        reader = AllowlistedEvidenceReader(self._store, allowlist)
        resolved: list[c.ArtifactRef] = []
        for ref in refs:
            try:
                resolved.append(reader.resolve(ref))
            except EvidenceNotAuthorized as exc:
                return None, str(exc)
        return resolved, None

    async def run(
        self,
        *,
        invocation_id: UUID,
        node_attempt: int,
        allowlist: dict[str, c.ArtifactRef],
    ) -> DraftRoleOutcome:
        reader = AllowlistedEvidenceReader(self._store, allowlist)
        tool = self._make_tool(reader)
        thread_id = (
            f"eval-role-{self.role.value}-{str(invocation_id)[:8]}-{invocation_id}"
        )
        agent = self._build_agent(tool, allowlist, invocation_id)
        prompt = self._system_prompt(allowlist)
        self.invocations.append(
            {
                "invocation_id": str(invocation_id),
                "role": self.role.value,
                "agent_id": agent.name,
                "thread_id": thread_id,
                "backend": f"filesystem://agent_runs/{self.role.value}/{invocation_id}/fs",
                "history_len": 0,
                "node_attempt": node_attempt,
                "profile_key": self._profile_key,
                "prompt_digest": c.sha256_hex(prompt.encode("utf-8")),
                "shared_model_warning": (
                    "allow_shared_model=true: role shares a model profile; "
                    "role distinctness is contextual, not guaranteed by profile equality"
                    if self._manifest.manifest.policy.allow_shared_model
                    else None
                ),
            }
        )
        try:
            result = await agent.ainvoke(
                {"messages": []},
                config={"configurable": {"thread_id": thread_id}},
            )
        except Exception as exc:  # noqa: BLE001 - 模型调用边界的宽捕获
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error=f"role runner invocation failed: {type(exc).__name__}: {exc}",
                model_requested=True,
            )
        return self._to_outcome(allowlist, result, invocation_id)

    def _to_outcome(
        self,
        allowlist: dict[str, c.ArtifactRef],
        result: dict[str, Any],
        invocation_id: UUID,
    ) -> DraftRoleOutcome:
        raise NotImplementedError


class ReportRoleRunner(_DraftRoleRunner):
    """report_judge 受限 runner：唯一工具是 allowlisted evidence/audit summary
    reader；唯一输出 ReportNarrativeDraft。不做任何 score/merge/rubric/
    recommendation writer，无私有 role trace 访问。
    """

    role = c.JudgeRole.REPORT_JUDGE
    draft_cls = c.ReportNarrativeDraft
    tool_name = "read_report_evidence"

    def _make_tool(self, reader: AllowlistedEvidenceReader) -> BaseTool:
        return make_report_evidence_tool(reader)

    def _collect_refs(self, draft: c.ReportNarrativeDraft) -> list[c.ArtifactRef]:
        refs = [ev.ref for ev in draft.evidence]
        for para in draft.paragraphs:
            refs.extend(ev.ref for ev in para.evidence)
        return refs

    def _to_outcome(
        self,
        allowlist: dict[str, c.ArtifactRef],
        result: dict[str, Any],
        invocation_id: UUID,
    ) -> DraftRoleOutcome:
        sr = result.get("structured_response")
        if sr is None or not isinstance(sr, c.ReportNarrativeDraft):
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error="role runner: model returned no structured ReportNarrativeDraft",
                model_requested=True,
            )
        if sr.role is not c.JudgeRole.REPORT_JUDGE:
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error=f"draft role {sr.role.value} != report_judge",
                model_requested=True,
            )
        _resolved, err = self._resolve_refs(allowlist, self._collect_refs(sr))
        if err is not None:
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error=f"role runner: {err}",
                model_requested=True,
            )
        draft = c.ReportNarrativeDraft(
            role=self.role,
            invocation_id=invocation_id,
            narrative=sr.narrative,
            paragraphs=sr.paragraphs,
            evidence=sr.evidence,
            model_used=getattr(self._model, "_llm_type", None)
            or type(self._model).__name__,
            fallback_reason=sr.fallback_reason,
        )
        status = (
            RunnerStatus.UNKNOWN
            if draft.fallback_reason is not None
            else RunnerStatus.SUCCEEDED
        )
        return DraftRoleOutcome(status=status, draft=draft, model_requested=True)


class RecommendationRoleRunner(_DraftRoleRunner):
    """recommendation_judge 受限 runner：唯一工具是 frozen merged/report/failure
    refs reader（不给任何 evidence 写工具）；唯一输出 RecommendationDraft。
    建议必须是 artifact-only 文本，不修改 score/report，不自动改代码/prompt。
    """

    role = c.JudgeRole.RECOMMENDATION_JUDGE
    draft_cls = c.RecommendationDraft
    tool_name = "read_frozen_ref"

    def _make_tool(self, reader: AllowlistedEvidenceReader) -> BaseTool:
        return make_frozen_ref_tool(reader)

    def _collect_refs(self, draft: c.RecommendationDraft) -> list[c.ArtifactRef]:
        refs: list[c.ArtifactRef] = []
        for item in draft.recommendations:
            refs.extend(ev.ref for ev in item.evidence)
            for failure in item.failure_refs:
                if failure.ref is not None:
                    refs.append(failure.ref)
        return refs

    def _to_outcome(
        self,
        allowlist: dict[str, c.ArtifactRef],
        result: dict[str, Any],
        invocation_id: UUID,
    ) -> DraftRoleOutcome:
        sr = result.get("structured_response")
        if sr is None or not isinstance(sr, c.RecommendationDraft):
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error="role runner: model returned no structured RecommendationDraft",
                model_requested=True,
            )
        if sr.role is not c.JudgeRole.RECOMMENDATION_JUDGE:
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error=f"draft role {sr.role.value} != recommendation_judge",
                model_requested=True,
            )
        _resolved, err = self._resolve_refs(allowlist, self._collect_refs(sr))
        if err is not None:
            return DraftRoleOutcome(
                status=RunnerStatus.FAILED,
                error=f"role runner: {err}",
                model_requested=True,
            )
        draft = c.RecommendationDraft(
            role=self.role,
            invocation_id=invocation_id,
            recommendations=sr.recommendations,
            source=sr.source,
        )
        return DraftRoleOutcome(
            status=RunnerStatus.SUCCEEDED, draft=draft, model_requested=True
        )


def make_report_role_runner_factory(
    runtime: Any,
    *,
    model: BaseChatModel | None = None,
    prompt_resolver: PromptResolver | None = None,
) -> Callable[[], ReportRoleRunner]:
    """report_judge 受限 runner 工厂；model 未注入 → fail-closed。"""
    if model is None:
        raise RoleRunnerError(
            "report_judge role runner requires an explicit model; "
            "refusing to construct an implicit LLM"
        )
    resolver = prompt_resolver or _default_prompt_resolver

    def _factory() -> ReportRoleRunner:
        return ReportRoleRunner(runtime.store, runtime.manifest, model, resolver)

    return _factory


def make_recommendation_role_runner_factory(
    runtime: Any,
    *,
    model: BaseChatModel | None = None,
    prompt_resolver: PromptResolver | None = None,
) -> Callable[[], RecommendationRoleRunner]:
    """recommendation_judge 受限 runner 工厂；model 未注入 → fail-closed。"""
    if model is None:
        raise RoleRunnerError(
            "recommendation_judge role runner requires an explicit model; "
            "refusing to construct an implicit LLM"
        )
    resolver = prompt_resolver or _default_prompt_resolver

    def _factory() -> RecommendationRoleRunner:
        return RecommendationRoleRunner(
            runtime.store, runtime.manifest, model, resolver
        )

    return _factory


def make_role_runner_factory(
    runtime: Any,
    *,
    model: BaseChatModel | None = None,
    prompt_resolver: PromptResolver | None = None,
) -> RunnerFactory:
    """把受限 score role runner 适配成 workflow 的 `runner_factory`。

    `model` 必须显式注入 —— 未提供则抛 `RoleRunnerError`（绝不静默构造 LLM）。
    `runtime` 只需鸭子类型提供 `.store` 与 `.manifest`（避免 import workflow）。
    """
    if model is None:
        raise RoleRunnerError(
            "role runner requires an explicit model; refusing to construct an implicit LLM"
        )
    resolver = prompt_resolver or _default_prompt_resolver

    def _factory() -> AgentRunner:
        return ScoreRoleRunner(runtime.store, runtime.manifest, model, resolver)

    return _factory
