"""F2 FamilyRoleRunner：judge 家族化设计的家族 DeepAgent + 维度 subagent runner。

（设计 `2026-08-09-judge-family-redesign-calibration.md` §2 / §4.2 / §5；
接口与 `roles.ScoreRoleRunner` 相同的 `(job, invocation_id, node_attempt) -> RunnerOutcome`。）

一次 job 调用 = 1 个家族 DeepAgent invocation + 每 rubric 维度 1 个独立
subagent invocation：

- **家族 agent**：工具集 = job evidence reader + 检索工具（家族配额 ≤4）+
  `dispatch_subagent`（封闭维度集合、反 halo、独立 subagent 隔离）。
- **维度 subagent**：独立 agent_id / thread / backend / 空 history + 独立
  `RetrievalSession`（配额 ≤6）；工具集**不含** `dispatch_subagent`（禁止嵌套
  subagent / general-purpose subagent / `task` / 写工具 / 跨家族）。
- **检索配额**：家族 ≤4 / 每维度 subagent ≤6 / 单 job 总计 ≤28，共享
  `_JobQuota`；每次调用全记录进对应 (sub)agent 的 `events.jsonl`。
- **派发指令全落盘** `family_dispatch.jsonl`（含被拒 attempt 与拒绝原因）；
  反 halo 确定性守卫拒绝携带 `[0,1]` 小数的指令（分数 = 家族初步结论）。
- **聚合只做格式合并**：以 subagent 落盘草稿为准构建家族 ScoreDraft；家族
  输出与 subagent 草稿任一维度分数/原因不一致 → typed FAILED；
  维度级 abstain 并集必须恰好覆盖 rubric 维度全集（缺一 → FAILED）。

与 `ScoreRoleRunner` 相同的 provider 兼容模式：`response_format=None` +
`_extract_structured_from_result` 文本 JSON 解析（复用 roles.py 既有 helper）。

审计落盘（设计 §5，store 根下 `agent_runs/<role>/<invocation_id>/`）：
`family_input.json` / `family_dispatch.jsonl` / `family_output.raw` /
`output.validated.json` / `usage.json` / `events.jsonl` +
`subagents/<dim>/{input.json, events.jsonl, output.raw, output.validated.json, usage.json}`。

`make_family_runner_factory` 必须显式注入 model（未注入 → `FamilyRoleRunnerError`，
绝不静默构造 LLM）。`run_dir` 注入 raw experiment run 目录时才绑定检索工具；
F3 接线 workflow 时由运行时提供。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval.agent import roles
from sar_orch.eval.agent.retrieval_tools import (
    QUOTA_DIMENSION_SUBAGENT,
    QUOTA_FAMILY_AGENT,
    QUOTA_JOB_TOTAL,
    RetrievalSession,
    make_tools,
)
from sar_orch.eval.agent.runner import (
    AgentRunner,
    RunnerError,
    RunnerFactory,
    RunnerOutcome,
    RunnerStatus,
)

__all__ = [
    "FAMILY_RUBRIC_IDS",
    "FamilyRoleRunner",
    "FamilyRoleRunnerError",
    "anti_halo_rejects",
    "is_family_rubric",
    "make_family_or_role_runner_factory",
    "make_family_runner_factory",
]

#: 家族 runner 承接的 rubric 版本（设计 §6：v2 家族源；v1 frozen 归 ScoreRoleRunner）。
#: 判定源与 workflow 共享（`rubric_registry.is_family_rubric`），避免两处漂移。
FAMILY_RUBRIC_IDS = rr.FAMILY_RUBRIC_IDS


def is_family_rubric(rubric: c.RubricSpec) -> bool:
    """v2 家族 rubric 判定（复用 `rubric_registry.is_family_rubric`）。

    v1 frozen rubric（dispatch-v1 / observation-v1）必须继续走 `ScoreRoleRunner`，
    二者以 rubric_id 判别（v1 字节不动，digest 锚点不受影响）。
    """
    return rr.is_family_rubric(rubric)


_FAMILY_PROMPT_FILES: dict[c.JudgeRole, str] = {
    c.JudgeRole.DISPATCH_SCORE_JUDGE: "dispatch_family_judge.md",
    c.JudgeRole.OBSERVATION_SCORE_JUDGE: "observation_family_judge.md",
}

PROMPTS_DIR = Path(__file__).parent / "prompts"
DIMENSIONS_PROMPTS_DIR = PROMPTS_DIR / "dimensions"

# 反 halo 确定性守卫：`[0,1]` 小数为分数 token（`1.0` / `0.0` / `0.75`）。
# 证据引用（`file:L12`、sha256、`evidence/job-scoped/<uuid>/<dim>.json`、step
# 数字）都不含小数，因此出现小数 = 家族初步结论外泄，指令必须被拒。
_SCORE_TOKEN_RE = re.compile(r"\b[0-1]\.\d+\b")

_EVIDENCE_REL_RE = re.compile(r"evidence/job-scoped/[0-9a-fA-F-]+/[A-Za-z0-9_]+\.json")
_REF_LINE_RE = re.compile(r"\b([A-Za-z0-9_.-]+\.(?:csv|ndjson|jsonl)):L\d+(?:-L\d+)?\b")

# 与 roles.py 相同的硬性红线（write/edit/execute/task + 默认 built-ins）。
FAMILY_EXCLUDED_TOOLS = roles.EXCLUDED_ROLE_TOOLS


class FamilyRoleRunnerError(ValueError):
    """家族 runner 构造 / 绑定违例（不静默降级）。"""


def _default_family_prompt_resolver(role: c.JudgeRole) -> str:
    name = _FAMILY_PROMPT_FILES.get(role)
    if name is None:
        raise FamilyRoleRunnerError(f"no family prompt file for role {role.value}")
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FamilyRoleRunnerError(f"cannot read family prompt {path}: {exc}") from exc


def _dimension_prompt(dimension: str) -> str:
    path = DIMENSIONS_PROMPTS_DIR / f"{dimension}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FamilyRoleRunnerError(
            f"cannot read dimension prompt {path}: {exc}"
        ) from exc


def anti_halo_rejects(instruction: str) -> str | None:
    """反 halo 确定性守卫：指令携带分数/结论 → 返回拒绝原因，否则 None。

    只允许"该维度判定指引 + 原始证据引用"（设计 §2.2 硬约束）。这是 prompt
    硬约束之上的确定性兜底（分数是家族初步结论的最强信号）。
    """
    if not isinstance(instruction, str) or not instruction.strip():
        return "instruction must be a non-empty string"
    if _SCORE_TOKEN_RE.search(instruction):
        return (
            "instruction embeds a score token (a family preliminary conclusion); "
            "dispatch instructions may only carry judging guidance and raw "
            "evidence references"
        )
    return None


def _extract_evidence_refs(text: str) -> list[str]:
    """从派发指令中抽取原始证据引用（bundle 路径 + `<file>:L<N>`）。"""
    refs = set(_EVIDENCE_REL_RE.findall(text))
    refs.update(_REF_LINE_RE.findall(text))
    return sorted(refs)


# ─────────────────────────────────────────────────────────────────────────────
# 检索配额：单 job 共享 `_JobQuota`（家族 ≤4 / 维度 ≤6 / 总 ≤28）
# ─────────────────────────────────────────────────────────────────────────────


class _JobQuota:
    """单 job 的共享检索配额账本：家族 + 全部 subagent 共用一个实例。"""

    def __init__(self, max_total: int = QUOTA_JOB_TOTAL) -> None:
        self.max_total = max_total
        self.used = 0
        self._lock = threading.Lock()

    def try_reserve(self) -> bool:
        with self._lock:
            if self.used >= self.max_total:
                return False
            self.used += 1
            return True


class _FamilyRetrievalSession(RetrievalSession):
    """绑定共享 job 配额的检索会话：先查 job 总额，再走会话自身配额。

    任一配额耗尽 → Error 文本（并照常记录该次调用，`returned_refs=[]`）。
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        quota: _JobQuota,
        max_calls: int,
        record_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(run_dir, max_calls=max_calls, record_sink=record_sink)
        self._quota = quota

    def _acquire(self, tool_name: str, params: dict[str, Any]) -> str | None:
        if self.calls >= self.max_calls:
            return super()._acquire(tool_name, params)
        if not self._quota.try_reserve():
            msg = (
                f"Error: job retrieval quota exhausted "
                f"(max_total={self._quota.max_total})"
            )
            self._record(
                tool_name,
                params,
                returned_refs=[],
                bytes=len(msg.encode("utf-8")),
                truncated=False,
            )
            return msg
        return super()._acquire(tool_name, params)


def _append_jsonl(
    store: a.ArtifactStore,
    rel: str,
    record: dict[str, Any],
    lock: threading.Lock,
) -> None:
    """append-only JSONL 审计写入（family_dispatch.jsonl / events.jsonl）。"""
    path = store.contained_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = c.canonical_json(record) + "\n"
    with lock, open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


# ─────────────────────────────────────────────────────────────────────────────
# FamilyRoleRunner（AgentRunner protocol）
# ─────────────────────────────────────────────────────────────────────────────


class FamilyRoleRunner:
    """家族 DeepAgent runner：一次 job = 家族 agent + 每维度一个 subagent。

    - `run(job, ...)` 首次调用绑定 job（runner 每 job 构造一次）；再绑定不同
      job → `RunnerError`（绝不跨 job 复用 evidence view / 检索会话）。
    - 每次 invocation：全新家族 DeepAgent + 独立 thread/backend/空 history；
      家族 agent 通过 `dispatch_subagent` 按维度派发独立 subagent。
    - 聚合只做格式合并：以 subagent 落盘草稿为准；家族输出与 subagent 草稿
      不一致 / 并集未覆盖 rubric 维度全集 → typed FAILED，绝不返回可被当作
      成功 pollute 的 draft。
    - `invocations` / `subagent_invocations` 暴露可观测记录（agent_id /
      thread_id / backend / history_len），供 fake-runner 级别隔离断言。
    """

    def __init__(
        self,
        store: a.ArtifactStore,
        manifest: c.FrozenInputManifest,
        model: BaseChatModel,
        *,
        run_dir: str | Path | None = None,
        prompt_resolver: Callable[[c.JudgeRole], str] | None = None,
        now: Callable[[], str] | None = None,
        job_total_quota: int = QUOTA_JOB_TOTAL,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._model = model
        self._run_dir = Path(run_dir) if run_dir is not None else None
        if self._run_dir is not None and not self._run_dir.is_dir():
            raise FamilyRoleRunnerError(f"run_dir is not a directory: {run_dir}")
        self._prompt_resolver = prompt_resolver or _default_family_prompt_resolver
        self._now = now or (lambda: datetime.now(UTC).isoformat())
        self._bound_job: c.ScoreJob | None = None
        self._profile_key = roles._profile_key(model)
        self._register_family_profile()
        self._quota = _JobQuota(job_total_quota)
        self._jsonl_lock = threading.Lock()
        self.invocations: list[dict[str, Any]] = []
        self.subagent_invocations: list[dict[str, Any]] = []
        self.family_session: _FamilyRetrievalSession | None = None
        self.subagent_sessions: dict[str, _FamilyRetrievalSession] = {}
        self._dispatched_dims: set[str] = set()
        self.dispatch_records: list[dict[str, Any]] = []

    # ── 受限 harness：排除默认 built-ins + 禁用 general-purpose subagent ──
    def _register_family_profile(self) -> str:
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )

        register_harness_profile(
            self._profile_key,
            HarnessProfile(
                excluded_tools=roles.EXCLUDED_ROLE_TOOLS,
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
        return self._profile_key

    # ── 冻结 manifest 内 rubric 解析 ────────────────────────────────────────
    def _rubric(self, job: c.ScoreJob) -> c.RubricSpec:
        for r in self._manifest.manifest.rubrics:
            if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
                return r
        raise FamilyRoleRunnerError(
            f"rubric {job.rubric_id}/{job.rubric_digest[:8]} not in frozen manifest"
        )

    # ── 家族 / 维度 system prompt（复用 roles.py 的文本 JSON 契约） ──────────
    def _family_system_prompt(
        self, job: c.ScoreJob, rubric: c.RubricSpec, refs: dict[str, c.ArtifactRef]
    ) -> str:
        base = self._prompt_resolver(job.role)
        available = "\n".join(sorted(refs))
        score_claim_note = '"score"'
        return (
            f"{base}\n\n"
            f"## Job Contract (authoritative)\n"
            f"role: {job.role.value}\n"
            f"job_id: {job.job_id}\n"
            f"target_type: {job.target_type.value}\n"
            f"dimensions: {json.dumps(rubric.dimensions)}\n"
            f"available evidence files:\n{available}\n"
            f"You MUST call dispatch_subagent exactly once per rubric dimension "
            f"(one call per dimension, no more, no less). Each instruction must "
            f"contain only that dimension's judging guidance and raw evidence "
            f"references — never your own score or conclusion.\n"
            f"Your structured output must be a ScoreDraft.\n"
            f"Output format: emit exactly one JSON object matching the "
            f"ScoreDraft schema (no markdown fences, no prose).\n"
            f"{roles._draft_json_fields(c.ScoreDraft)}\n"
            f"{roles._draft_evidence_templates(refs, claim_type='score', claim_type_note=score_claim_note)}"
        )

    def _subagent_system_prompt(
        self,
        dimension: str,
        instruction: str,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
    ) -> str:
        base = _dimension_prompt(dimension)
        available = "\n".join(sorted(refs))
        score_claim_note = '"score"'
        return (
            f"{base}\n\n"
            f"## Family Dispatch Instruction (verbatim from family agent)\n"
            f"{instruction}\n\n"
            f"## Job Contract (authoritative)\n"
            f"role: {job.role.value}\n"
            f"job_id: {job.job_id}\n"
            f"dimensions: {json.dumps(rubric.dimensions)}\n"
            f"available evidence files:\n{available}\n"
            f"Emit a ScoreDraft for your dimension {dimension!r} only — exactly "
            f"one of `dimensions[{dimension!r}]` (scored) or "
            f"`dimension_unknown_reasons[{dimension!r}]` (abstain).\n"
            f"Output format: emit exactly one JSON object matching the "
            f"ScoreDraft schema (no markdown fences, no prose).\n"
            f"{roles._draft_json_fields(c.ScoreDraft)}\n"
            f"{roles._draft_evidence_templates(refs, claim_type='score', claim_type_note=score_claim_note)}"
        )

    # ── backend / agent 构造（每次 invocation 独立隔离） ──────────────────────
    @staticmethod
    def _filesystem_backend(store: a.ArtifactStore, rel: str):
        from deepagents import backends

        root = store.contained_path(f"{rel}/fs")
        return backends.FilesystemBackend(root_dir=root, virtual_mode=True)

    def _build_family_agent(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        tools: list[BaseTool],
        invocation_id: UUID,
    ):
        from deepagents import create_deep_agent

        agent_id = f"{job.role.value}-family-{invocation_id}"
        return create_deep_agent(
            model=self._model,
            tools=tools,
            system_prompt=self._family_system_prompt(job, rubric, refs),
            response_format=None,
            backend=self._filesystem_backend(
                self._store, f"agent_runs/{job.role.value}/{invocation_id}"
            ),
            name=agent_id,
        )

    def _build_subagent_agent(
        self,
        job: c.ScoreJob,
        dimension: str,
        system_prompt: str,
        tools: list[BaseTool],
        invocation_id: UUID,
        sub_rel: str,
    ):
        from deepagents import create_deep_agent

        agent_id = f"{job.role.value}-{dimension}-{invocation_id}"
        return create_deep_agent(
            model=self._model,
            tools=tools,
            system_prompt=system_prompt,
            response_format=None,
            backend=self._filesystem_backend(self._store, sub_rel),
            name=agent_id,
        )

    # ── 家族输入 / usage 落盘 ────────────────────────────────────────────────
    def _write_family_input(
        self,
        family_rel: str,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
    ) -> None:
        payload = {
            "job_id": str(job.job_id),
            "role": job.role.value,
            "target_type": job.target_type.value,
            "rubric_id": job.rubric_id,
            "dimensions": list(rubric.dimensions),
            "evidence_files": sorted(refs),
            "input_bundle_ref": {
                "path": job.input_bundle_ref.path,
                "sha256": job.input_bundle_ref.sha256,
            },
            "prompt_digest": c.sha256_hex(
                self._family_system_prompt(job, rubric, refs).encode("utf-8")
            ),
            "retrieval_quotas": {
                "family": QUOTA_FAMILY_AGENT,
                "subagent": QUOTA_DIMENSION_SUBAGENT,
                "job_total": self._quota.max_total,
            },
            "created_at": self._now(),
        }
        self._store.write_canonical_json(
            f"{family_rel}/family_input.json", payload, producer="family-runner"
        )

    @staticmethod
    def _write_usage(
        store: a.ArtifactStore, rel: str, usage: c.UsageSnapshot | None
    ) -> None:
        payload = None if usage is None else usage.model_dump(mode="json")
        store.write_canonical_json(rel, {"usage": payload}, producer="family-runner")

    # ── subagent 单维度草稿校验（fail-closed） ────────────────────────────────
    def _validate_subagent_draft(
        self, job: c.ScoreJob, dimension: str, draft: c.ScoreDraft
    ) -> list[str]:
        errors: list[str] = []
        if draft.role is not job.role:
            errors.append(
                f"subagent {dimension!r} draft role {draft.role.value} != "
                f"job role {job.role.value}"
            )
        union = set(draft.dimensions) | set(draft.dimension_unknown_reasons)
        if union != {dimension}:
            errors.append(
                f"subagent {dimension!r} draft must cover exactly dimension "
                f"{dimension!r} (got {sorted(union)})"
            )
        overlap = set(draft.dimensions) & set(draft.dimension_unknown_reasons)
        if overlap:
            errors.append(
                f"subagent {dimension!r} draft must not both score and abstain "
                f"on {sorted(overlap)}"
            )
        return errors

    async def _run_subagent(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        dimension: str,
        instruction: str,
        family_rel: str,
    ) -> tuple[c.ScoreDraft | None, c.UsageSnapshot | None, str | None]:
        """派发并运行一个维度 subagent；返回 (draft, usage, error)。"""
        store = self._store
        sub_rel = f"{family_rel}/subagents/{dimension}"
        sub_inv = uuid4()
        sub_thread = (
            f"eval-family-{job.role.value}-{str(job.job_id)[:8]}-{dimension}-{sub_inv}"
        )
        store.write_canonical_json(
            f"{sub_rel}/input.json",
            {"dimension": dimension, "instruction": instruction},
            producer="family-runner",
        )
        sub_events_rel = f"{sub_rel}/events.jsonl"
        store.write_atomic(sub_events_rel, b"")
        evidence_tool = roles.make_job_reader_tool(roles.JobEvidenceReader(store, job))
        sub_session: _FamilyRetrievalSession | None = None
        sub_tools: list[BaseTool] = [evidence_tool]
        if self._run_dir is not None:
            sub_session = _FamilyRetrievalSession(
                self._run_dir,
                quota=self._quota,
                max_calls=QUOTA_DIMENSION_SUBAGENT,
                record_sink=lambda rec: _append_jsonl(
                    store, sub_events_rel, rec, self._jsonl_lock
                ),
            )
            self.subagent_sessions[dimension] = sub_session
            sub_tools.extend(make_tools(sub_session))
        sub_prompt = self._subagent_system_prompt(
            dimension, instruction, job, rubric, refs
        )
        sub_agent = self._build_subagent_agent(
            job, dimension, sub_prompt, sub_tools, sub_inv, sub_rel
        )
        try:
            sub_result = await sub_agent.ainvoke(
                {"messages": []},
                config={"configurable": {"thread_id": sub_thread}},
            )
        except Exception as exc:  # noqa: BLE001 - 模型调用边界宽捕获
            return (
                None,
                None,
                (f"subagent invocation failed: {type(exc).__name__}: {exc}"),
            )
        sub_usage = roles._extract_usage(sub_result)
        self._write_usage(store, f"{sub_rel}/usage.json", sub_usage)
        sub_draft = roles._extract_structured_from_result(sub_result, c.ScoreDraft)
        if sub_draft is None:
            store.write_canonical_json(
                f"{sub_rel}/output.raw",
                {"status": "unparsed", "last_ai_text": roles._last_ai_text(sub_result)},
                producer="family-runner",
            )
            return None, sub_usage, "subagent returned no structured ScoreDraft"
        store.write_canonical_json(
            f"{sub_rel}/output.raw",
            {"status": "parsed", "draft": sub_draft.model_dump(mode="json")},
            producer="family-runner",
        )
        errors = self._validate_subagent_draft(job, dimension, sub_draft)
        if errors:
            return None, sub_usage, "; ".join(errors)
        store.write_canonical_json(
            f"{sub_rel}/output.validated.json",
            sub_draft.model_dump(mode="json"),
            producer="family-runner",
        )
        self.subagent_invocations.append(
            {
                "invocation_id": str(sub_inv),
                "job_id": str(job.job_id),
                "dimension": dimension,
                "agent_id": sub_agent.name,
                "thread_id": sub_thread,
                "backend": f"filesystem://{sub_rel}/fs",
                "history_len": 0,
            }
        )
        return sub_draft, sub_usage, None

    # ── dispatch_subagent 工具（封闭维度 + 反 halo + 落盘 + 独立 subagent） ──
    def _build_dispatch_tool(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        family_rel: str,
    ) -> BaseTool:
        dim_set = set(rubric.dimensions)
        seq_counter = [0]

        async def dispatch_subagent(dimension: str, instruction: str) -> str:
            seq_counter[0] += 1
            attempt: dict[str, Any] = {
                "seq": seq_counter[0],
                "dimension": dimension,
                "instruction": instruction,
                "ts": self._now(),
            }

            def _finalize(status: str, reason: str | None = None, **extra) -> None:
                attempt["status"] = status
                if reason is not None:
                    attempt["reason"] = reason
                attempt.update(extra)
                self.dispatch_records.append(dict(attempt))

            if dimension not in dim_set:
                _finalize(
                    "rejected_unknown_dimension",
                    f"unknown dimension; allowed: {sorted(dim_set)}",
                )
                return (
                    f"Error: unknown dimension {dimension!r}; allowed dimensions: "
                    f"{sorted(dim_set)}"
                )
            if dimension in self._dispatched_dims:
                _finalize(
                    "rejected_duplicate",
                    "exactly one subagent per dimension",
                )
                return (
                    f"Error: dimension {dimension!r} already dispatched "
                    "(exactly one subagent per dimension)"
                )
            halo = anti_halo_rejects(instruction)
            if halo is not None:
                _finalize("rejected_anti_halo", halo)
                return (
                    f"Error: dispatch instruction for {dimension!r} rejected "
                    f"(anti-halo): {halo}"
                )
            self._dispatched_dims.add(dimension)
            draft, usage, err = await self._run_subagent(
                job, rubric, refs, dimension, instruction, family_rel
            )
            if err is not None:
                _finalize("subagent_failed", err)
                return f"Error: subagent {dimension!r} failed: {err}"
            _finalize(
                "accepted",
                evidence_refs=_extract_evidence_refs(instruction),
                draft=draft.model_dump(mode="json"),
                usage=usage.model_dump(mode="json") if usage is not None else None,
            )
            return json.dumps(draft.model_dump(mode="json"))

        @tool("dispatch_subagent")
        async def _dispatch_tool(dimension: str, instruction: str) -> str:
            """Dispatch exactly one dimension subagent for this job.

            The subagent judges a single rubric dimension in isolation with its
            own evidence reader, retrieval quota, and empty history, then
            returns its single-dimension ScoreDraft JSON.

            Args:
                dimension: one of the rubric dimensions declared in the Job
                    Contract (exactly one call per dimension, no more).
                instruction: judging guidance + raw evidence references only.
                    MUST NOT contain your own score or conclusion (anti-halo).

            Returns:
                The subagent's single-dimension ScoreDraft JSON, or an Error
                string (unknown dimension, duplicate dispatch, anti-halo
                violation, or subagent failure).
            """
            return await dispatch_subagent(dimension, instruction)

        return _dispatch_tool

    def _flush_dispatch_log(self, family_rel: str, rubric: c.RubricSpec) -> None:
        """按 rubric 维度顺序落盘 `family_dispatch.jsonl`（每条一次，终态）。

        派发工具并发执行，`dispatch_records` 的完成顺序不确定；审计日志按
        rubric 维度声明顺序排序后逐行追加，保证 `family_dispatch.jsonl` 可回放、
        每条派发只出现一次（含被拒 attempt 与拒绝原因）。
        """
        dim_order = {d: i for i, d in enumerate(rubric.dimensions)}

        def _key(rec: dict[str, Any]) -> tuple[int, int, int]:
            idx = dim_order.get(rec["dimension"])
            if idx is not None:
                return (0, idx, rec.get("seq", 0))
            return (1, rec.get("seq", 0), 0)

        for rec in sorted(self.dispatch_records, key=_key):
            _append_jsonl(
                self._store,
                f"{family_rel}/family_dispatch.jsonl",
                rec,
                self._jsonl_lock,
            )

    # ── AgentRunner protocol ─────────────────────────────────────────────────
    async def run(
        self, job: c.ScoreJob, *, invocation_id: UUID, node_attempt: int
    ) -> RunnerOutcome:
        if self._bound_job is None:
            self._bound_job = job
        elif str(self._bound_job.job_id) != str(job.job_id):
            raise RunnerError(
                f"family role runner bound to job {self._bound_job.job_id} but "
                f"called for job {job.job_id}",
                retryable=False,
            )
        job = self._bound_job
        rubric = self._rubric(job)
        refs = roles.materialize_job_evidence(self._store, job, rubric.dimensions)
        family_rel = f"agent_runs/{job.role.value}/{invocation_id}"
        self._write_family_input(family_rel, job, rubric, refs)
        evidence_tool = roles.make_job_reader_tool(
            roles.JobEvidenceReader(self._store, job)
        )
        if self._run_dir is not None:
            self.family_session = _FamilyRetrievalSession(
                self._run_dir,
                quota=self._quota,
                max_calls=QUOTA_FAMILY_AGENT,
                record_sink=lambda rec: _append_jsonl(
                    self._store,
                    f"{family_rel}/events.jsonl",
                    rec,
                    self._jsonl_lock,
                ),
            )
        tools: list[BaseTool] = [evidence_tool]
        if self.family_session is not None:
            tools.extend(make_tools(self.family_session))
        tools.append(self._build_dispatch_tool(job, rubric, refs, family_rel))
        thread_id = (
            f"eval-family-{job.role.value}-{str(job.job_id)[:8]}-{invocation_id}"
        )
        agent = self._build_family_agent(job, rubric, refs, tools, invocation_id)
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
                "dimensions": list(rubric.dimensions),
                "retrieval_max_calls": (
                    QUOTA_FAMILY_AGENT if self.family_session is not None else None
                ),
                "shared_model_warning": (
                    "allow_shared_model=true: family and dimension subagents "
                    "share one model profile; subagent independence is "
                    "isolation-guaranteed, not model-distinct"
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
        except Exception as exc:  # noqa: BLE001 - 模型调用边界宽捕获
            self._flush_dispatch_log(family_rel, rubric)
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=(
                    f"family role runner invocation failed: {type(exc).__name__}: {exc}"
                ),
                model_requested=True,
            )
        self._flush_dispatch_log(family_rel, rubric)
        return self._to_outcome(job, rubric, refs, result, invocation_id, family_rel)

    def _to_outcome(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        result: dict[str, Any],
        invocation_id: UUID,
        family_rel: str,
    ) -> RunnerOutcome:
        store = self._store
        usage = roles._extract_usage(result)
        self._write_usage(store, f"{family_rel}/usage.json", usage)
        family_draft = roles._extract_structured_from_result(result, c.ScoreDraft)
        if family_draft is None:
            store.write_canonical_json(
                f"{family_rel}/family_output.raw",
                {"status": "unparsed", "last_ai_text": roles._last_ai_text(result)},
                producer="family-runner",
            )
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=("family role runner: model returned no structured ScoreDraft"),
                model_requested=True,
                usage=usage,
            )
        store.write_canonical_json(
            f"{family_rel}/family_output.raw",
            {"status": "parsed", "draft": family_draft.model_dump(mode="json")},
            producer="family-runner",
        )
        if family_draft.role is not job.role:
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=(
                    f"draft role {family_draft.role.value} != job role {job.role.value}"
                ),
                model_requested=True,
                usage=usage,
            )

        sub_drafts: dict[str, c.ScoreDraft] = {}
        for dim in rubric.dimensions:
            rel = f"{family_rel}/subagents/{dim}/output.validated.json"
            if not store.exists(rel):
                return RunnerOutcome(
                    status=RunnerStatus.FAILED,
                    error=f"missing subagent draft for dimension {dim!r}",
                    model_requested=True,
                    usage=usage,
                )
            try:
                data = json.loads(store.read_bytes(rel))
                sub_drafts[dim] = c.ScoreDraft.model_validate(data)
            except (ValueError, OSError) as exc:
                return RunnerOutcome(
                    status=RunnerStatus.FAILED,
                    error=f"invalid persisted subagent draft for {dim!r}: {exc}",
                    model_requested=True,
                    usage=usage,
                )

        merged_dims, merged_reasons, merged_evidence, errors = (
            self._merge_subagent_drafts(job, rubric, refs, sub_drafts)
        )
        if errors:
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error="family aggregation: " + "; ".join(errors),
                model_requested=True,
                usage=usage,
            )
        if (
            family_draft.dimensions != merged_dims
            or family_draft.dimension_unknown_reasons != merged_reasons
        ):
            return RunnerOutcome(
                status=RunnerStatus.FAILED,
                error=(
                    "family aggregation mismatch: family output differs from "
                    "subagent drafts (merge-only, never rewrite)"
                ),
                model_requested=True,
                usage=usage,
            )

        unknown_reason = "all dimensions abstained" if not merged_dims else None
        draft = c.ScoreDraft(
            role=job.role,
            invocation_id=invocation_id,
            dimensions=merged_dims,
            dimension_unknown_reasons=merged_reasons,
            evidence=merged_evidence,
            model_used=getattr(self._model, "_llm_type", None)
            or type(self._model).__name__,
            unknown_reason=unknown_reason,
        )
        store.write_canonical_json(
            f"{family_rel}/output.validated.json",
            draft.model_dump(mode="json"),
            producer="family-runner",
        )
        status = (
            RunnerStatus.UNKNOWN if not draft.dimensions else RunnerStatus.SUCCEEDED
        )
        return RunnerOutcome(
            status=status, draft=draft, model_requested=True, usage=usage
        )

    def _merge_subagent_drafts(
        self,
        job: c.ScoreJob,
        rubric: c.RubricSpec,
        refs: dict[str, c.ArtifactRef],
        sub_drafts: dict[str, c.ScoreDraft],
    ) -> tuple[dict[str, float], dict[str, str], list[c.EvidenceRef], list[str]]:
        """格式合并：只搬运 subagent 分数/原因，逐维度附本 job 证据 ref。

        维度级 abstain 并集必须恰好覆盖 rubric 维度全集（每维度要么有分、
        要么有显式原因，缺一 → errors fail-closed）。
        """
        dims: dict[str, float] = {}
        reasons: dict[str, str] = {}
        evidence: list[c.EvidenceRef] = []
        errors: list[str] = []
        for dim in rubric.dimensions:
            d = sub_drafts[dim]
            union = set(d.dimensions) | set(d.dimension_unknown_reasons)
            if union != {dim}:
                errors.append(
                    f"subagent {dim!r} draft does not cover exactly dimension {dim!r}"
                )
                continue
            if d.dimensions:
                dims[dim] = d.dimensions[dim]
                rel = f"evidence/job-scoped/{job.job_id}/{dim}.json"
                ref = refs.get(rel)
                if ref is None:
                    errors.append(f"missing materialized evidence for {dim!r}")
                    continue
                evidence.append(
                    c.EvidenceRef(
                        ref=ref, claim_type=c.ClaimType.SCORE, digest=ref.sha256
                    )
                )
            else:
                reasons[dim] = d.dimension_unknown_reasons[dim]
        return dims, reasons, evidence, errors


def make_family_runner_factory(
    runtime: Any,
    *,
    model: BaseChatModel | None = None,
    run_dir: str | Path | None = None,
    prompt_resolver: Callable[[c.JudgeRole], str] | None = None,
) -> RunnerFactory:
    """把家族 runner 适配成 workflow 的 `runner_factory`。

    `model` 必须显式注入 —— 未提供则抛 `FamilyRoleRunnerError`（绝不静默构造
    LLM）。`runtime` 只需鸭子类型提供 `.store` 与 `.manifest`。`run_dir` 是
    raw experiment run 目录；F3 接线 workflow 时提供（缺省为 None → 仅
    bundle 证据，检索工具不绑定）。
    """
    if model is None:
        raise FamilyRoleRunnerError(
            "family role runner requires an explicit model; refusing to "
            "construct an implicit LLM"
        )
    resolver = prompt_resolver or _default_family_prompt_resolver

    def _factory() -> AgentRunner:
        return FamilyRoleRunner(
            runtime.store,
            runtime.manifest,
            model,
            run_dir=run_dir,
            prompt_resolver=resolver,
        )

    return _factory


class _RoutingAgentRunner:
    """按 job 的 rubric 路由的 workflow runner（v2 家族 / v1 score role）。

    每个实例只服务一个 job（workflow 每次 `runner_factory()` 一个 runner）；真实
    底层 runner 在首次 `run(job, ...)` 时按 rubric 惰性构造并缓存，绝不跨 job
    复用。`run_dir` 传给 FamilyRoleRunner 用于检索工具 containment（v1
    ScoreRoleRunner 不需要）。
    """

    def __init__(
        self,
        runtime: Any,
        *,
        model: BaseChatModel,
        run_dir: str | Path | None = None,
        prompt_resolver: Callable[[c.JudgeRole], str],
        family_prompt_resolver: Callable[[c.JudgeRole], str],
    ) -> None:
        self._runtime = runtime
        self._model = model
        self._run_dir = run_dir
        self._prompt_resolver = prompt_resolver
        self._family_prompt_resolver = family_prompt_resolver
        self._delegate: AgentRunner | None = None

    def _rubric(self, job: c.ScoreJob) -> c.RubricSpec:
        for r in self._runtime.manifest.manifest.rubrics:
            if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
                return r
        raise FamilyRoleRunnerError(
            f"rubric {job.rubric_id}/{job.rubric_digest[:8]} not in frozen manifest"
        )

    def _build_delegate(self, job: c.ScoreJob) -> AgentRunner:
        rubric = self._rubric(job)
        if is_family_rubric(rubric):
            return FamilyRoleRunner(
                self._runtime.store,
                self._runtime.manifest,
                self._model,
                run_dir=self._run_dir,
                prompt_resolver=self._family_prompt_resolver,
            )
        return roles.ScoreRoleRunner(
            self._runtime.store,
            self._runtime.manifest,
            self._model,
            prompt_resolver=self._prompt_resolver,
        )

    async def run(
        self, job: c.ScoreJob, *, invocation_id: UUID, node_attempt: int
    ) -> RunnerOutcome:
        if self._delegate is None:
            self._delegate = self._build_delegate(job)
        return await self._delegate.run(
            job, invocation_id=invocation_id, node_attempt=node_attempt
        )


def make_family_or_role_runner_factory(
    runtime: Any,
    *,
    model: BaseChatModel | None = None,
    run_dir: str | Path | None = None,
    prompt_resolver: Callable[[c.JudgeRole], str] | None = None,
    family_prompt_resolver: Callable[[c.JudgeRole], str] | None = None,
) -> RunnerFactory:
    """workflow 的 runner 路由工厂：v2 家族 rubric → FamilyRoleRunner，v1 → ScoreRoleRunner。

    `model` 必须显式注入 —— 未提供则抛 `FamilyRoleRunnerError`（绝不静默构造
    LLM）。`run_dir` 缺省回退到 `runtime.source_run_dir`（F3 接线由
    `build_runtime_for_results_dir` 注入的 raw source run 目录）；传入时优先。
    两种 prompt resolver 各自缺省到模块默认（家族 / score role 默认文件）。
    """
    if model is None:
        raise FamilyRoleRunnerError(
            "family/role runner requires an explicit model; refusing to "
            "construct an implicit LLM"
        )
    family_resolver = family_prompt_resolver or _default_family_prompt_resolver
    role_resolver = prompt_resolver or roles._default_prompt_resolver
    effective_run_dir = run_dir
    if effective_run_dir is None:
        effective_run_dir = getattr(runtime, "source_run_dir", None)

    def _factory() -> AgentRunner:
        return _RoutingAgentRunner(
            runtime,
            model=model,
            run_dir=effective_run_dir,
            prompt_resolver=role_resolver,
            family_prompt_resolver=family_resolver,
        )

    return _factory
