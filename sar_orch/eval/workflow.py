"""P3 LangGraph 确定性控制面：StateGraph 生命周期、Send fan-out、resume/cancel。

设计：`.hermes/plans/2026-08-06_004210-eval-judge-transparent-langgraph-design.md`
§2.1-2.3、§6、§9 P3。

- `build_eval_workflow(runtime)` 工厂构建 `CompiledStateGraph`；不持有模块级
  agent/workspace。节点闭包绑定 `EvalRuntime`。
- `admit_or_resume` 先验 manifest/ledger：terminal 且 refs/digests 完整 →
  zero-work short-circuit；非 terminal → 按缺失 artifact 只路由缺失阶段
  （每个节点幂等）。
- 动态 fan-out 用 LangGraph `Send` map-reduce；`score_results` 用 canonical map
  reducer（同 key 只接受相同 digest，冲突拒绝）。
- `run_score_judge` 是 async node：`asyncio.Semaphore(policy.concurrency)` 限制
  并发，`asyncio.wait_for` 实现 per-job timeout；timeout/retryable error 转
  typed job failure；retry 换 node_attempt 但不变 job identity，retry_count 写
  result。
- cancel：停止新 claim、记录 in-flight job、写 CANCELLED ledger 恰好一次；
  terminal job 后到达的 response 只记为 `late_ignored`。
- `--no-llm-judge` 零 runner 构造：`runner_factory` 永不调用，workflow 终态
  `SUCCEEDED` 且 `judge_execution_status=not_requested`。
- requested 路径在 `deterministic_score_merge` 后经 `run_report_judge` →
  `run_recommendation_judge` → `render_reports`（§2.1）；两个角色节点用
  `ReportRoleRunner` / `RecommendationRoleRunner`（经 `report_judge_factory` /
  `recommendation_judge_factory` 注入，workflow 不 import 角色/模型）。
  - `run_report_judge`：allowlist = merged bundle + 已物化 evidence/audit
    summary refs；ReportNarrativeDraft 经 contracts + refs 命中校验后持久化
    `report/report_narrative.json`；失败 → typed failure + PARTIAL +
    deterministic report 兜底（绝不写成功叙述）。
  - `run_recommendation_judge`：allowlist = frozen merged/report/failure refs；
    RecommendationDraft 校验+持久化 `recommendations/recommendations.json`；
    失败 → deterministic fallback（`source=deterministic_fallback` + 必带
    fallback_reason）。
  - 未注入角色 runner 时节点只写 deterministic fallback（不记 typed failure，
    保留既有仅注入 score runner 的 requested 单测为 SUCCEEDED）。
- `map_workflow_exit`：SUCCEEDED/PARTIAL→0，FAILED→1，attempt_busy/
  publish_conflict→2，CANCELLED→130（§7.1）。
- `run_from_results_dir` 是 CLI 可注入 workflow adapter 的参考实现。

禁止：本模块不得 import / 构造 DeepAgent 或任何 chat 模型；graph 测试只用
fake runner（`sar_orch.eval.agent.runner`）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import signal
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, ConfigDict

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval import score_merge as sm
from sar_orch.eval.agent.runner import (
    AgentRunner,
    RunnerError,
    RunnerOutcome,
    RunnerStatus,
)
from sar_orch.eval.artifacts import snapshot_source_files
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.report import (
    ATTEMPT_REPORT_FAMILY,
    FAILURE_DIAGNOSTIC_BUCKETS,
    project_episode_from_grader_results,
)

__all__ = [
    "EXIT_ATTEMPT_BUSY",
    "EXIT_CANCELLED",
    "EXIT_FAILED",
    "EXIT_OK",
    "EvalRuntime",
    "EvalWorkflowState",
    "ResumeConfigError",
    "WorkflowOutcome",
    "append_failures",
    "build_eval_workflow",
    "build_runtime_for_results_dir",
    "build_score_result",
    "claim_score_job",
    "install_cancel_handler",
    "map_workflow_exit",
    "merge_refs",
    "persist_score_result",
    "publish_selected_attempt",
    "run_eval_workflow",
    "run_from_results_dir",
    "write_cancellation_ledger",
]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ATTEMPT_BUSY = 2
EXIT_CANCELLED = 130


class ResumeConfigError(ValueError):
    """resume 时 CLI/policy/locator/source 与冻结 manifest 不一致（§2.2/§2.3）。

    fail-closed：不得调用 runner、不得修改 canonical artifacts；
    CLI 映射 exit 2（§7.1）。
    """


# ─────────────────────────────────────────────────────────────────────────────
# artifact 路径（attempt root 相对）
# ─────────────────────────────────────────────────────────────────────────────

SUBJECT_REF_REL = "subject/run_ref.json"
SOURCE_MANIFEST_REL = "evidence/source_run_manifest.json"
EVIDENCE_BUNDLE_REL = "evidence/evidence_bundle.json"
GRADER_RESULTS_REL = "evidence/grader_results.json"
VIOLATIONS_REL = "evidence/deterministic_violations.json"
JOBS_DIR = "score_jobs"
RESULTS_DIR = "score_results"
MERGED_REL = "merged/score_bundle.json"
REPORT_NARRATIVE_REL = "report/report_narrative.json"
RECOMMENDATIONS_REL = "recommendations/recommendations.json"
REPORT_REL = "reports/eval_report.json"
LEDGER_REL = a.ArtifactStore.LEDGER_REL

#: report/recommendation 角色失败（typed failure → PARTIAL + deterministic
#: fallback，§2.1 line 159）。这些是**非 hard** 的 requested 路径缺失，不否决
#: workflow（区别于 score_merge / artifact integrity 的 hard veto）。
_JUDGE_ROLE_FALLBACK_FAILURE_KINDS = frozenset({"report_judge", "recommendation_judge"})


def _canonical_json_ref(
    rel: str, payload: dict[str, Any], producer: str
) -> c.ArtifactRef:
    """write_canonical_json（含 `\\n` 尾）产出的确定性 ArtifactRef。"""
    data = (c.canonical_json(payload) + "\n").encode("utf-8")
    return c.ArtifactRef(
        path=rel,
        sha256=c.sha256_hex(data),
        bytes=len(data),
        media_type="application/json",
        producer=producer,
    )


def _manifest_ref(manifest: c.FrozenInputManifest) -> c.ArtifactRef:
    """input_manifest.json 的确定性 ref（write_input_manifest 无 `\\n` 尾）。"""
    data = c.canonical_json(manifest.payload()).encode("utf-8")
    return c.ArtifactRef(
        path=a.ArtifactStore.MANIFEST_REL,
        sha256=c.sha256_hex(data),
        bytes=len(data),
        media_type="application/json",
        producer="freeze",
    )


# ─────────────────────────────────────────────────────────────────────────────
# reducers（§2.2）：canonical map reducer + append 排序
# ─────────────────────────────────────────────────────────────────────────────


def merge_refs(
    current: dict[str, c.ArtifactRef] | None,
    update: dict[str, c.ArtifactRef] | None,
) -> dict[str, c.ArtifactRef]:
    """canonical map reducer：key=job_id；同 key 只接受相同 digest，冲突拒绝。"""
    current = dict(current or {})
    for key, ref in (update or {}).items():
        prev = current.get(key)
        if prev is not None and prev.sha256 != ref.sha256:
            raise c.ContractViolation(
                f"conflicting artifact ref for {key}: {prev.sha256} != {ref.sha256}"
            )
        current[key] = ref
    return current


def append_failures(
    current: list[c.FailureRef] | None,
    update: c.FailureRef | Sequence[c.FailureRef] | None,
) -> list[c.FailureRef]:
    """append+canonical sort reducer：已有 failure 不可删除。"""
    current = list(current or [])
    if isinstance(update, (list, tuple)):
        current.extend(update)
    elif isinstance(update, c.FailureRef):
        current.append(update)
    current.sort(key=lambda f: (f.kind, f.reason))
    return current


# ─────────────────────────────────────────────────────────────────────────────
# 状态 schema
# ─────────────────────────────────────────────────────────────────────────────


class EvalWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str = ""
    attempt_id: str = ""
    series_root: str = ""
    attempt_root: str = ""
    phase: c.WorkflowStatus = c.WorkflowStatus.CREATED
    judge_execution_status: c.JudgeExecutionStatus | None = None
    merge_status: sm.MergedScoreStatus | None = None
    score_jobs: Annotated[dict[str, c.ArtifactRef], merge_refs] = {}
    score_results: Annotated[dict[str, c.ArtifactRef], merge_refs] = {}
    failures: Annotated[list[c.FailureRef], append_failures] = []
    input_manifest_ref: c.ArtifactRef | None = None
    merged_ref: c.ArtifactRef | None = None
    narrative_ref: c.ArtifactRef | None = None
    recommendation_ref: c.ArtifactRef | None = None
    report_ref: c.ArtifactRef | None = None
    final_ledger_ref: c.ArtifactRef | None = None
    short_circuit: bool = False
    terminal_status: c.WorkflowStatus | None = None
    error: str | None = None
    resume_from: str | None = None
    job_id: str | None = None


class WorkflowOutcome(BaseModel):
    status: c.WorkflowStatus
    attempt_busy: bool = False
    publish_conflict: bool = False
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# RuntimeContext：节点闭包只读；不写 State
# ─────────────────────────────────────────────────────────────────────────────


class CancelFlag:
    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()


class _NonFsyncAuditJournal:
    """append 默认 fsync=False 的审计代理。

    逐事件 fsync 会阻塞事件循环（让 Send 分支退化成串行）并拖慢整个流程；
    audit 是断尾可恢复日志（§3.3-3），非末尾 commit 才是成功证据。原子 artifact
    写入仍各自 fsync；只有事件行允许批量落盘。
    """

    def __init__(self, journal: Any) -> None:
        self._journal = journal

    def append(self, event: dict[str, Any], *, fsync: bool = False) -> int:
        return self._journal.append(event, fsync=fsync)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._journal, name)


class _FencedAuditJournal:
    """在每次 append 前执行 fencing 检查的审计代理（§1.2-3 / P3-M2）。

    没有 current fencing token 的进程不得 append audit：所有 attempt 内审计写入
    必须在发生任何外部写入前被 `FencingError` 拦截（fail-closed）。
    """

    def __init__(self, journal: Any, fence: Callable[[], None]) -> None:
        self._journal = journal
        self._fence = fence

    def append(self, event: dict[str, Any], *, fsync: bool = False) -> int:
        self._fence()
        return self._journal.append(event, fsync=fsync)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._journal, name)


def _fencing_failure_state(exc: BaseException) -> dict[str, Any]:
    """stale/released lease 下的 typed fencing failure：零写入、无 ledger。"""
    return {
        "phase": c.WorkflowStatus.FAILED,
        "terminal_status": c.WorkflowStatus.FAILED,
        "error": f"fencing failure: {exc}",
        "failures": [c.FailureRef(kind="fencing", reason=str(exc), source="typed")],
    }


def _transition_failure_state(exc: BaseException) -> dict[str, Any]:
    """非法 phase 迁移 / terminal regression 的 fail-closed 状态（P3-M4）。

    不写任何 canonical artifact；run 以 FAILED 终止（exit 1）。
    """
    return {
        "phase": c.WorkflowStatus.FAILED,
        "terminal_status": c.WorkflowStatus.FAILED,
        "error": f"illegal workflow transition: {exc}",
        "failures": [
            c.FailureRef(kind="workflow_transition", reason=str(exc), source="typed")
        ],
    }


class EvalRuntime:
    """节点依赖 + 会话级并发/取消状态。

    resume 必须从 immutable input manifest 重建（profile/tool schema/policy/
    store/journal/runner 都在这里重建）；CLI 参数若与 manifest digest 不一致
    必须拒绝（§2.2）。`_run_context` 保存当前 event loop 绑定的 semaphore，
    避免跨 `asyncio.run` 复用造成 "bound to a different event loop"。
    """

    def __init__(
        self,
        manifest: c.FrozenInputManifest,
        store: a.ArtifactStore,
        journal: Any,
        *,
        lease: a.AttemptLease | None = None,
        runner_factory: Callable[[], AgentRunner] | None = None,
        report_judge_factory: Callable[[], Any] | None = None,
        recommendation_judge_factory: Callable[[], Any] | None = None,
        source_manifest: c.SourceInputManifest | None = None,
        episode: Any = None,
        grader_fn: (
            Callable[[], tuple[list[Any], list[sm.DeterministicViolation]]] | None
        ) = None,
        timeout_s: float | None = None,
        now: Callable[[], datetime] | None = None,
        judge_sample_steps: int = rr.DEFAULT_JUDGE_SAMPLE_STEPS,
        checkpointer: Any = None,
        checkpoint_db: str | Path | None = None,
        thread_id: str | None = None,
        probe_interrupt: bool = False,
    ) -> None:
        self.manifest = manifest
        self.store = store
        journal = _NonFsyncAuditJournal(journal)
        self.journal = journal
        self.lease = lease
        if lease is not None:
            self.journal = _FencedAuditJournal(journal, lambda: self._fence())
        self.runner_factory = runner_factory
        self.report_judge_factory = report_judge_factory
        self.recommendation_judge_factory = recommendation_judge_factory
        self.source_manifest = source_manifest or _default_source_manifest(manifest)
        self.episode = episode
        self.grader_fn = grader_fn
        self.timeout_s = timeout_s
        self._now = now
        self.judge_sample_steps = judge_sample_steps
        self.cancel = CancelFlag()
        self._run_context: dict[str, Any] = {}
        self.series_root = store.root.parent.parent
        # ── 持久化 checkpointer（§6.3）：SQLite continuation cache，非成功证据。──
        self._checkpointer = checkpointer
        self._checkpoint_db = (
            str(checkpoint_db)
            if checkpoint_db is not None
            else str(store.contained_path("checkpoint.sqlite"))
        )
        # 每个 attempt 一条**持久且跨进程稳定**的 thread：由 attempt identity 派生，
        # 保证 CLI/进程重启（同一 attempt）落到同一 thread，checkpoint continuation
        # 才能跨 runtime 生效。显式 thread_id 仅测试/注入场景覆盖。
        if thread_id is None:
            thread_id = (
                f"eval-{manifest.manifest.eval_run_id}-{manifest.manifest.attempt_id}"
            )
        self.thread_id = thread_id
        self.probe_interrupt = probe_interrupt
        self._owned_saver: Any = None
        self._cp_cm: Any = None

    def _fence(self) -> None:
        """受保护写操作前的 fencing 检查（§1.2-3 / P3-M2）。

        持有 lease 时必须 `ensure_current()`：token 过期/被接管/已释放 →
        `FencingError`，调用方 fail-closed。未注入 lease（内部/测试 runtime）
        时不做检查，保留 P3 无 lease 直跑路径。
        """
        if self.lease is not None:
            self.lease.ensure_current()

    def _checkpoint_contained(self) -> str:
        """checkpoint 路径必须在 attempt root 内且无 symlink/hardlink 逃逸（P3-M5）。

        在 `from_conn_string` 打开 sqlite 之前执行；失败时不得在 root 外创建文件。
        """
        root = Path(self.store.root)
        if self._checkpointer is not None:
            return self._checkpoint_db
        p = Path(self._checkpoint_db)
        try:
            rel = str(p.absolute().relative_to(root))
        except ValueError:
            raise a.ContainmentError(
                f"checkpoint path escapes attempt root: {self._checkpoint_db!r}"
            ) from None
        return str(self.store.contained_path(rel))

    @property
    def concurrency(self) -> int:
        return self.manifest.manifest.policy.concurrency

    def now(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(UTC)

    def semaphore(self) -> asyncio.Semaphore:
        sem = self._run_context.get("semaphore")
        if sem is None:
            sem = asyncio.Semaphore(self.concurrency)
            self._run_context["semaphore"] = sem
        return sem

    # ── checkpointer 生命周期（显式 open/close，连接不泄漏） ──────────────────
    async def open_checkpointer(self) -> Any:
        """打开（或返回已注入的）持久化 checkpointer；`AsyncSqliteSaver` 按需连接。"""
        if self._checkpointer is not None:
            return self._checkpointer
        if self._owned_saver is None:
            self._checkpoint_contained()
            self._cp_cm = AsyncSqliteSaver.from_conn_string(self._checkpoint_db)
            self._owned_saver = await self._cp_cm.__aenter__()
        return self._owned_saver

    async def close_checkpointer(self) -> None:
        """关闭本 runtime 拥有的 checkpointer 连接；已注入的 saver 由注入方负责。"""
        if self._owned_saver is not None and self._cp_cm is not None:
            await self._cp_cm.__aexit__(None, None, None)
            self._owned_saver = None
            self._cp_cm = None

    def get_checkpointer(self) -> Any:
        """编译用 checkpointer；未打开则明确报错（生命周期由 open/close 掌控）。"""
        if self._checkpointer is not None:
            return self._checkpointer
        if self._owned_saver is None:
            raise RuntimeError(
                "checkpointer is not open; call await runtime.open_checkpointer() "
                "before build_eval_workflow()/ainvoke_workflow()"
            )
        return self._owned_saver


def _default_source_manifest(manifest: c.FrozenInputManifest) -> c.SourceInputManifest:
    subject = manifest.manifest.subject
    return c.SourceInputManifest(
        eval_run_id=manifest.manifest.eval_run_id,
        source_run_dir=subject.source_run_ref.path,
        files={},
        excluded=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# store 辅助（下游一律以磁盘为准，保证 resume 可见已持久化结果）
# ─────────────────────────────────────────────────────────────────────────────


def _load_score_job(store: a.ArtifactStore, job_id: str) -> c.ScoreJob | None:
    rel = f"{JOBS_DIR}/{job_id}.json"
    if not store.exists(rel):
        return None
    return c.ScoreJob.model_validate(json.loads(store.read_bytes(rel).decode("utf-8")))


def _list_jobs(store: a.ArtifactStore) -> list[c.ScoreJob]:
    jobs: list[c.ScoreJob] = []
    for p in sorted(Path(store.root).joinpath(JOBS_DIR).glob("*.json")):
        job = _load_score_job(store, p.stem)
        if job is not None:
            jobs.append(job)
    return jobs


def _list_job_refs(store: a.ArtifactStore) -> dict[str, c.ArtifactRef]:
    refs: dict[str, c.ArtifactRef] = {}
    for p in sorted(Path(store.root).joinpath(JOBS_DIR).glob("*.json")):
        rel = f"{JOBS_DIR}/{p.name}"
        data = store.read_bytes(rel)
        refs[p.stem] = c.ArtifactRef(
            path=rel,
            sha256=c.sha256_hex(data),
            bytes=len(data),
            media_type="application/json",
            producer="builder",
        )
    return refs


def _list_result_refs(store: a.ArtifactStore) -> dict[str, c.ArtifactRef]:
    refs: dict[str, c.ArtifactRef] = {}
    for p in sorted(Path(store.root).joinpath(RESULTS_DIR).glob("*.json")):
        rel = f"{RESULTS_DIR}/{p.name}"
        data = store.read_bytes(rel)
        refs[p.stem] = c.ArtifactRef(
            path=rel,
            sha256=c.sha256_hex(data),
            bytes=len(data),
            media_type="application/json",
            producer="validator",
        )
    return refs


def _load_all_results(store: a.ArtifactStore) -> list[c.ScoreResult]:
    results: list[c.ScoreResult] = []
    for p in sorted(Path(store.root).joinpath(RESULTS_DIR).glob("*.json")):
        ref = _list_result_refs(store)[p.stem]
        data = store.read_verified(ref)
        results.append(c.ScoreResult.model_validate(json.loads(data.decode("utf-8"))))
    return results


def _load_violations(store: a.ArtifactStore) -> list[sm.DeterministicViolation]:
    if not store.exists(VIOLATIONS_REL):
        return []
    data = json.loads(store.read_bytes(VIOLATIONS_REL).decode("utf-8"))
    return [sm.DeterministicViolation.model_validate(item) for item in data]


def _ledger_artifact_refs(
    store: a.ArtifactStore, state: EvalWorkflowState
) -> dict[str, c.ArtifactRef]:
    refs: dict[str, c.ArtifactRef] = {}
    if state.report_ref is not None:
        refs[REPORT_REL] = state.report_ref
    if state.merged_ref is not None:
        refs[MERGED_REL] = state.merged_ref
    if state.narrative_ref is not None:
        refs[REPORT_NARRATIVE_REL] = state.narrative_ref
    if state.recommendation_ref is not None:
        refs[RECOMMENDATIONS_REL] = state.recommendation_ref
    refs.update(_list_result_refs(store))
    return refs


def _ledger_ref(ledger: c.FinalLedger) -> c.ArtifactRef:
    data = c.canonical_json(ledger.model_dump(mode="json")).encode("utf-8")
    return c.ArtifactRef(
        path=LEDGER_REL,
        sha256=c.sha256_hex(data),
        bytes=len(data),
        media_type="application/json",
        producer="finalizer",
    )


# ─────────────────────────────────────────────────────────────────────────────
# resume 计算与 routing
# ─────────────────────────────────────────────────────────────────────────────

_RESUME_ROUTE = {
    "freeze": "freeze_input_manifest",
    "materialize": "materialize_evidence",
    "graders": "run_deterministic_graders",
    "jobs": "build_score_jobs",
    "run_jobs": "build_score_jobs",
    "merge": "deterministic_score_merge",
    "report_judge": "run_report_judge",
    "recommendation_judge": "run_recommendation_judge",
    "render": "render_reports",
    "finalize": "verify_and_finalize",
}


def _has_result(store: a.ArtifactStore, job_id: str) -> bool:
    return store.exists(f"{RESULTS_DIR}/{job_id}.json")


def _advance_phase(
    state: EvalWorkflowState, runtime: EvalRuntime, *targets: c.WorkflowStatus
) -> c.WorkflowStatus:
    """runtime 强制执行 §2.3 状态机：每个 phase 更新必须沿合法迁移前进（P3-M4）。

    中间态（INPUT_VALIDATED/SCORING/REPORT_AUTHORED/RECOMMENDATIONS_AUTHORED/
    VERIFIED）以合法路径步进校验；current==target 视为幂等（resume 重入）。
    非法跳步 / terminal regression → `ContractViolation`，由节点 wrapper
    fail-closed 转为 FAILED。
    """
    current = state.phase
    for target in targets:
        if current is target:
            continue
        c.validate_workflow_transition(current, target)
        runtime.journal.append(
            {"kind": "phase", "from": current.value, "to": target.value}
        )
        current = target
    return current


def _rehash_ref(
    store: a.ArtifactStore, rel: str, producer: str
) -> c.ArtifactRef | None:
    """按磁盘当前字节 rehash 出 ArtifactRef（非终态 resume 重建 refs 用）。"""
    if not store.exists(rel):
        return None
    data = store.read_bytes(rel)
    return c.ArtifactRef(
        path=rel,
        sha256=c.sha256_hex(data),
        bytes=len(data),
        media_type="application/json",
        producer=producer,
    )


def _reconstructed_phase(
    store: a.ArtifactStore, judge_required: bool, resume_from: str
) -> c.WorkflowStatus:
    """根据 resume 路由推断“进入下一节点前的 phase”（P3-M4）。

    不是“最远 artifact”而是 resume 节点的前置 phase：artifact 缺失跳步 resume
    时，phase 必须落在该节点目标的合法前驱上，否则过渡守卫会拒绝。
    """
    if not store.exists(a.ArtifactStore.MANIFEST_REL):
        return c.WorkflowStatus.CREATED
    if resume_from == "materialize":
        return c.WorkflowStatus.MANIFEST_FROZEN
    if resume_from == "graders":
        return c.WorkflowStatus.EVIDENCE_MATERIALIZED
    if resume_from in ("jobs", "run_jobs"):
        return (
            c.WorkflowStatus.DETERMINISTIC_GRADED
            if not _list_job_refs(store)
            else c.WorkflowStatus.SCORE_JOBS_READY
        )
    if resume_from == "merge":
        return (
            c.WorkflowStatus.DETERMINISTIC_GRADED
            if not judge_required
            else c.WorkflowStatus.SCORES_JOINED
        )
    if resume_from == "report_judge":
        return c.WorkflowStatus.SCORES_MERGED
    if resume_from == "recommendation_judge":
        return c.WorkflowStatus.REPORT_AUTHORED
    if resume_from == "render":
        return (
            c.WorkflowStatus.JUDGE_SKIPPED
            if not judge_required
            else c.WorkflowStatus.SCORES_MERGED
        )
    if resume_from == "finalize":
        return c.WorkflowStatus.RENDERED
    return c.WorkflowStatus.CREATED


def _reconstruct_resume_state(
    store: a.ArtifactStore,
    manifest: c.FrozenInputManifest,
    resume_from: str,
) -> dict[str, Any]:
    """P3-M1：非终态 resume 必须从**已验证的磁盘 artifact** 重建 judge status、
    merge status、failure refs、merge/report refs 与 phase。

    绝不把 `not_requested` 错写为 `requested`，绝不让已持久化为 FAILED 的 merge
    被重判为 SUCCEEDED，绝不丢失已有 report/merge refs 与真实 digest。任何 artifact
    不可解析/不可验证 → 抛 VerificationError（fail-closed）。
    """
    judge_required = manifest.manifest.policy.llm_judge_required
    fields: dict[str, Any] = {}
    # 注意：score_jobs / score_results 是 reducer 字段，由节点（`_build_score_jobs` /
    # `persist_score_result`）负责填充；重建这里**不**写 reducer 字段，避免与
    # checkpointer 回放缓存（PENDING 期 refs）冲突（P3-M3）。
    if judge_required:
        jobs = _list_jobs(store)
        results = _load_all_results(store)
        fields["judge_execution_status"] = _compute_judge_status(jobs, results)
    else:
        fields["judge_execution_status"] = c.JudgeExecutionStatus.NOT_REQUESTED
    merged_ref = _rehash_ref(store, MERGED_REL, "merge")
    if merged_ref is not None:
        fields["merged_ref"] = merged_ref
        try:
            merged = json.loads(store.read_bytes(MERGED_REL))
        except ValueError as exc:
            raise a.VerificationError(
                f"merged score bundle is corrupt on resume: {exc}"
            ) from exc
        status = merged.get("status")
        if status:
            fields["merge_status"] = sm.MergedScoreStatus(status)
        if status == sm.MergedScoreStatus.FAILED.value:
            violations = list(merged.get("violations") or [])
            fields["failures"] = [
                c.FailureRef(
                    kind="score_merge",
                    reason="; ".join(violations[:3]) or status,
                    source="typed",
                )
            ]
    report_ref = _rehash_ref(store, REPORT_REL, "renderer")
    if report_ref is not None:
        fields["report_ref"] = report_ref
    narrative_ref = _rehash_ref(store, REPORT_NARRATIVE_REL, "report_judge")
    if narrative_ref is not None:
        fields["narrative_ref"] = narrative_ref
    recommendation_ref = _rehash_ref(store, RECOMMENDATIONS_REL, "recommendation_judge")
    role_failures: list[c.FailureRef] = []
    if recommendation_ref is not None:
        fields["recommendation_ref"] = recommendation_ref
        try:
            rec = json.loads(store.read_bytes(RECOMMENDATIONS_REL))
        except ValueError as exc:
            raise a.VerificationError(
                f"recommendations artifact is corrupt on resume: {exc}"
            ) from exc
        if rec.get("source") == c.RecommendationSource.DETERMINISTIC_FALLBACK.value:
            role_failures.append(
                c.FailureRef(
                    kind="recommendation_judge",
                    reason=rec.get("fallback_reason")
                    or "deterministic recommendation fallback",
                    source="typed",
                )
            )
    if role_failures:
        fields["failures"] = list(fields.get("failures") or []) + role_failures
    fields["phase"] = _reconstructed_phase(store, judge_required, resume_from)
    return fields


def _compute_resume(store: a.ArtifactStore, judge_required: bool) -> str:
    """按缺失 artifact 只路由缺失阶段（§2.1 / §6）。"""
    if not store.exists(a.ArtifactStore.MANIFEST_REL):
        return "freeze"
    if not store.exists(EVIDENCE_BUNDLE_REL):
        return "materialize"
    if not store.exists(GRADER_RESULTS_REL):
        return "graders"
    if judge_required:
        job_refs = _list_job_refs(store)
        if not job_refs:
            return "jobs"
        if any(not _has_result(store, job_id) for job_id in job_refs):
            return "run_jobs"
    if not store.exists(MERGED_REL):
        return "merge"
    if judge_required:
        if not store.exists(REPORT_NARRATIVE_REL):
            return "report_judge"
        if not store.exists(RECOMMENDATIONS_REL):
            return "recommendation_judge"
    if not store.exists(REPORT_REL):
        return "render"
    return "finalize"


def _admit_or_resume(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    store, journal = runtime.store, runtime.journal
    attempt = f"{runtime.manifest.manifest.eval_run_id}/{runtime.manifest.manifest.attempt_id}"
    try:
        ledger = store.read_final_ledger()
    except a.VerificationError as exc:
        journal.append({"kind": "admit_failed", "reason": f"corrupt ledger: {exc}"})
        return {
            "phase": c.WorkflowStatus.FAILED,
            "terminal_status": c.WorkflowStatus.FAILED,
            "error": str(exc),
        }
    if ledger is not None and c.is_workflow_terminal(ledger.terminal_status):
        try:
            for ref in ledger.artifact_refs.values():
                store.read_verified(ref)
        except (a.VerificationError, a.ContainmentError) as exc:
            journal.append(
                {"kind": "short_circuit_failed", "reason": f"artifact integrity: {exc}"}
            )
            return {
                "short_circuit": True,
                "phase": c.WorkflowStatus.FAILED,
                "terminal_status": c.WorkflowStatus.FAILED,
                "error": str(exc),
            }
        journal.append(
            {"kind": "short_circuit", "terminal_status": ledger.terminal_status.value}
        )
        return {
            "short_circuit": True,
            "terminal_status": ledger.terminal_status,
            "phase": ledger.terminal_status,
            "judge_execution_status": ledger.judge_execution_status,
        }
    resume = _compute_resume(store, runtime.manifest.manifest.policy.llm_judge_required)
    journal.append({"kind": "admit", "attempt": attempt, "resume_from": resume})
    if store.exists(a.ArtifactStore.MANIFEST_REL):
        try:
            fields = _reconstruct_resume_state(store, runtime.manifest, resume)
        except (a.VerificationError, a.ContainmentError) as exc:
            journal.append(
                {"kind": "admit_failed", "reason": f"resume reconstruction: {exc}"}
            )
            return {
                "phase": c.WorkflowStatus.FAILED,
                "terminal_status": c.WorkflowStatus.FAILED,
                "error": str(exc),
            }
        fields["resume_from"] = resume
        return fields
    return {
        "resume_from": resume,
        "phase": _advance_phase(
            state, runtime, c.WorkflowStatus.INPUT_VALIDATED, c.WorkflowStatus.ADMITTED
        ),
    }


def _route_resume(state: EvalWorkflowState, runtime: EvalRuntime) -> str:
    if state.short_circuit:
        return "end"
    return _RESUME_ROUTE.get(state.resume_from or "freeze", "freeze_input_manifest")


# ─────────────────────────────────────────────────────────────────────────────
# 确定性节点
# ─────────────────────────────────────────────────────────────────────────────


def _subject_payload(manifest: c.FrozenInputManifest) -> dict[str, Any]:
    s = manifest.manifest.subject
    return {
        "source_run_dir": s.source_run_ref.path,
        "source_digest": s.source_digest,
        "scene": s.scene,
        "agents": s.agents,
        "seed": s.seed,
        "code_commit": s.code_commit,
        "git_dirty": s.git_dirty,
    }


def _freeze_input_manifest(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    if not store.exists(a.ArtifactStore.MANIFEST_REL):
        store.write_canonical_json(
            SUBJECT_REF_REL, _subject_payload(runtime.manifest), producer="freeze"
        )
        store.write_canonical_json(
            SOURCE_MANIFEST_REL,
            runtime.source_manifest.model_dump(mode="json"),
            producer="freeze",
        )
        store.write_input_manifest(runtime.manifest)
        journal.append({"kind": "manifest_frozen", "digest": runtime.manifest.digest})
    return {
        "input_manifest_ref": _manifest_ref(runtime.manifest),
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.MANIFEST_FROZEN),
    }


def _build_evidence_bundle(runtime: EvalRuntime) -> dict[str, Any]:
    manifest = runtime.manifest.manifest
    steps: dict[str, Any] = {}
    failure_steps: list[int] = []
    episode = runtime.episode
    if episode is not None and getattr(episode, "steps", None):
        for step, sr in sorted(episode.steps.items()):
            interactions = getattr(sr, "interactions", None) or []
            failed = any(ai.succeeded is False for ai in interactions)
            steps[str(step)] = {"interactions": len(interactions), "failed": failed}
            if failed:
                failure_steps.append(step)
    return {
        "source_digest": manifest.subject.source_digest,
        "steps_overview": steps,
        "failure_steps": failure_steps,
        "sampling_plan": {
            "policy": "attempt-stream-v1/select_judge_steps",
            "target": runtime.judge_sample_steps,
        },
    }


def _evidence_bundle_ref(runtime: EvalRuntime) -> c.ArtifactRef:
    return _canonical_json_ref(
        EVIDENCE_BUNDLE_REL, _build_evidence_bundle(runtime), "materializer"
    )


def _evidence_digest(runtime: EvalRuntime) -> str:
    return c.sha256_hex(
        c.canonical_json(_build_evidence_bundle(runtime)).encode("utf-8")
    )


def _materialize_evidence(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    if not store.exists(EVIDENCE_BUNDLE_REL):
        store.write_canonical_json(
            EVIDENCE_BUNDLE_REL,
            _build_evidence_bundle(runtime),
            producer="materializer",
        )
        journal.append({"kind": "evidence_materialized"})
    return {
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.EVIDENCE_MATERIALIZED)
    }


def _grade_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "__dict__"):
        return {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
    return {"grader": str(result)}


def _run_deterministic_graders(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    if not store.exists(GRADER_RESULTS_REL):
        if runtime.grader_fn is None:
            raise a.ArtifactError(
                "runtime.grader_fn is None but graders node requires it"
            )
        results, violations = runtime.grader_fn()
        store.write_canonical_json(
            GRADER_RESULTS_REL,
            {"results": [_grade_result_payload(r) for r in results]},
            producer="grader",
        )
        store.write_canonical_json(
            VIOLATIONS_REL,
            [v.model_dump(mode="json") for v in violations],
            producer="grader",
        )
        journal.append(
            {
                "kind": "graders_completed",
                "count": len(results),
                "violations": len(violations),
            }
        )
    return {
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.DETERMINISTIC_GRADED)
    }


def _route_judge_mode(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    required = runtime.manifest.manifest.policy.llm_judge_required
    status = (
        c.JudgeExecutionStatus.REQUESTED
        if required
        else c.JudgeExecutionStatus.NOT_REQUESTED
    )
    runtime.journal.append({"kind": "judge_mode", "status": status.value})
    return {
        "judge_execution_status": status,
        "phase": _advance_phase(
            state,
            runtime,
            (
                c.WorkflowStatus.SCORE_JOBS_READY
                if required
                else c.WorkflowStatus.JUDGE_SKIPPED
            ),
        ),
    }


def _route_judge_mode_path(state: EvalWorkflowState, runtime: EvalRuntime) -> str:
    if state.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED:
        return "not_requested"
    return "requested"


def _retry_policy_digest(policy: c.PolicySpec) -> str:
    return c.sha256_hex(
        c.canonical_json({"retry": policy.retry, "timeout_s": policy.timeout_s}).encode(
            "utf-8"
        )
    )


def _build_jobs(runtime: EvalRuntime) -> list[c.ScoreJob]:
    """(Sample, RubricSpec, RoleConfig) → ScoreJob；max_score_jobs 溢出记 NOT_RUN_BUDGET。"""
    manifest = runtime.manifest.manifest
    if runtime.episode is None:
        raise a.ArtifactError("runtime.episode is None but score jobs require samples")
    role_by_role = {r.role: r for r in manifest.roles}
    source_digest = manifest.subject.source_digest
    evidence_digest = _evidence_digest(runtime)
    input_bundle_ref = _evidence_bundle_ref(runtime)
    retry_digest = _retry_policy_digest(manifest.policy)
    jobs: list[c.ScoreJob] = []
    for target_type in (c.SampleTargetType.DISPATCH, c.SampleTargetType.OBSERVATION):
        plan = rr.build_sample_plan(
            target_type,
            runtime.episode,
            target=runtime.judge_sample_steps,
            source_digest=source_digest,
            evidence_digest=evidence_digest,
        )
        for rubric in manifest.rubrics:
            if rubric.target_type is not target_type:
                continue
            role = role_by_role[rubric.judge_role]
            jobs.extend(
                rr.build_score_jobs(
                    plan.selected,
                    rubric,
                    role,
                    input_bundle_ref=input_bundle_ref,
                    manifest_digest=manifest.digest(),
                    retry_policy_digest=retry_digest,
                )
            )
    max_jobs = manifest.policy.max_score_jobs
    if max_jobs is not None and len(jobs) > max_jobs:
        ordered = sorted(jobs, key=lambda j: (j.target_type.value, str(j.sample_id)))
        budget = list(ordered[:max_jobs])
        overflow = [
            j.model_copy(update={"status": c.ScoreJobStatus.NOT_RUN_BUDGET})
            for j in ordered[max_jobs:]
        ]
        jobs = budget + overflow
    return jobs


def _build_score_jobs(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    existing = _list_job_refs(store)
    if existing:
        # checkpointer 回放时 state 已带 build 期 refs，直接复用（避免 reducer
        # 冲突）；全新 thread 的 artifact resume 则从磁盘 rehash（P3-M3）。
        refs = state.score_jobs if state.score_jobs else existing
        journal.append({"kind": "score_jobs_reused", "count": len(refs)})
        return {
            "score_jobs": refs,
            "phase": _advance_phase(state, runtime, c.WorkflowStatus.SCORE_JOBS_READY),
        }
    jobs = _build_jobs(runtime)
    refs: dict[str, c.ArtifactRef] = {}
    for job in jobs:
        ref = store.write_canonical_json(
            f"{JOBS_DIR}/{job.job_id}.json",
            job.model_dump(mode="json"),
            producer="builder",
        )
        refs[str(job.job_id)] = ref
    journal.append({"kind": "score_jobs_built", "count": len(jobs)})
    return {
        "score_jobs": refs,
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.SCORE_JOBS_READY),
    }


def _route_jobs(state: EvalWorkflowState, runtime: EvalRuntime):
    store = runtime.store
    pending = [
        job_id
        for job_id in state.score_jobs
        if (job := _load_score_job(store, job_id)) is not None
        and not c.is_job_terminal(job.status)
        and not _has_result(store, job_id)
    ]
    if not pending:
        return "skip"
    return [Send("run_score_judge", {"job_id": job_id}) for job_id in sorted(pending)]


# ─────────────────────────────────────────────────────────────────────────────
# score job 生命周期：claim → run（semaphore/timeout/retry）→ validate → persist
# ─────────────────────────────────────────────────────────────────────────────


def claim_score_job(
    store: a.ArtifactStore,
    journal: Any,
    job: c.ScoreJob,
    *,
    lease: a.AttemptLease | None = None,
) -> c.ScoreJob | None:
    """PENDING→CLAIMED（fencing token 下二次检查）；非 pending → None（跳过）。"""
    if lease is not None:
        lease.ensure_current()
    job_id = str(job.job_id)
    current = _load_score_job(store, job_id)
    if current is None:
        raise a.ArtifactError(f"score job {job_id} missing on claim")
    if c.is_job_terminal(current.status):
        return None
    claimed = current.model_copy(update={"status": c.ScoreJobStatus.CLAIMED})
    store.write_canonical_json(
        f"{JOBS_DIR}/{job_id}.json", claimed.model_dump(mode="json"), producer="builder"
    )
    journal.append({"kind": "score_job_claimed", "job_id": job_id})
    return claimed


async def _run_attempt(
    runtime: EvalRuntime,
    runner: AgentRunner,
    job: c.ScoreJob,
    invocation_id,
    node_attempt: int,
    timeout: float,
) -> RunnerOutcome:
    """在 semaphore 内执行一次模型调用，model_requested/finished 落 audit。"""
    sem = runtime.semaphore()
    journal = runtime.journal
    job_id = str(job.job_id)
    async with sem:
        runtime._fence()  # 无 current fencing token 不得调用模型（P3-M2）
        journal.append(
            {"kind": "model_requested", "job_id": job_id, "node_attempt": node_attempt}
        )
        try:
            outcome = await asyncio.wait_for(
                runner.run(job, invocation_id=invocation_id, node_attempt=node_attempt),
                timeout=timeout,
            )
        except BaseException:
            journal.append(
                {
                    "kind": "model_finished",
                    "job_id": job_id,
                    "node_attempt": node_attempt,
                }
            )
            raise
        journal.append(
            {"kind": "model_finished", "job_id": job_id, "node_attempt": node_attempt}
        )
        return outcome


async def _run_job_with_retry(
    runtime: EvalRuntime, job: c.ScoreJob, invocation_id
) -> tuple[RunnerOutcome, int]:
    """timeout/retryable error → typed 状态；retry 换 node_attempt，job identity 不变。"""
    manifest = runtime.manifest.manifest
    max_attempts = 1 + manifest.policy.retry
    timeout = (
        runtime.timeout_s
        if runtime.timeout_s is not None
        else float(manifest.policy.timeout_s)
    )
    if runtime.runner_factory is None:
        raise a.ArtifactError(
            "runner_factory is None but a score job requires a runner"
        )
    runner = runtime.runner_factory()
    journal = runtime.journal
    job_id = str(job.job_id)
    last_attempt = 0
    for node_attempt in range(1, max_attempts + 1):
        last_attempt = node_attempt
        if runtime.cancel.requested():
            return (
                RunnerOutcome(
                    status=RunnerStatus.CANCELLED,
                    error="cancelled before model call",
                    model_requested=False,
                ),
                node_attempt,
            )
        try:
            outcome = await _run_attempt(
                runtime, runner, job, invocation_id, node_attempt, timeout
            )
            return outcome, node_attempt
        except TimeoutError:
            journal.append(
                {"kind": "job_timeout", "job_id": job_id, "node_attempt": node_attempt}
            )
            if node_attempt < max_attempts:
                journal.append(
                    {
                        "kind": "job_retry",
                        "job_id": job_id,
                        "node_attempt": node_attempt,
                    }
                )
                continue
            return (
                RunnerOutcome(
                    status=RunnerStatus.TIMED_OUT,
                    error=f"timeout after {node_attempt} attempt(s)",
                ),
                node_attempt,
            )
        except RunnerError as exc:
            journal.append(
                {
                    "kind": "job_runner_error",
                    "job_id": job_id,
                    "node_attempt": node_attempt,
                    "retryable": exc.retryable,
                }
            )
            if exc.retryable and node_attempt < max_attempts:
                journal.append(
                    {
                        "kind": "job_retry",
                        "job_id": job_id,
                        "node_attempt": node_attempt,
                    }
                )
                continue
            return (
                RunnerOutcome(status=RunnerStatus.FAILED, error=str(exc)),
                node_attempt,
            )
        except asyncio.CancelledError:
            raise
    return RunnerOutcome(
        status=RunnerStatus.FAILED, error="exhausted attempts"
    ), last_attempt


def _agent_run_path(job: c.ScoreJob, invocation_id, name: str) -> str:
    return f"agent_runs/{job.role.value}/{invocation_id}/{name}.json"


def _rubric_for(
    manifest: c.FrozenInputManifest, job: c.ScoreJob
) -> c.RubricSpec | None:
    for r in manifest.manifest.rubrics:
        if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
            return r
    return None


def _validate_draft(
    job: c.ScoreJob,
    draft: c.ScoreDraft | None,
    *,
    rubric: c.RubricSpec | None = None,
) -> list[str]:
    """P3-M6：每个 scored dimension 必须有**唯一、授权且 digest-verified** 的
    evidence ref；数量/名称/路径/role 任一不符 → typed validation failure。

    - evidence 数量必须与 dimensions 一一对应（多/少均拒）；
    - dimension 名称必须与冻结 rubric 完全一致（多/少/改名均拒）；
    - 同一 evidence ref 不得重复使用；
    - 每条 evidence 的 claim_type 必须是 SCORE；
    - evidence ref path 必须落在 `evidence/job-scoped/<job_id>/` 命名空间内
      （跨 job ref / 不存在的 job 证据 → 拒绝）；
    - `EvidenceRef.digest` 必须与其 `ref.sha256` 一致（Pydantic 已挡，validator
      再做防御性 double-check，`model_construct` 绕验场景兜底）。
    """
    if draft is None:
        return ["runner returned without a draft"]
    if draft.role is not job.role:
        return [f"draft role {draft.role.value} != job role {job.role.value}"]
    if not draft.dimensions and draft.unknown_reason is None:
        return ["draft must carry dimensions or an explicit unknown_reason"]
    if not draft.dimensions:
        return []
    dim_names = list(draft.dimensions)
    errors: list[str] = []
    if rubric is not None and set(draft.dimensions) != set(rubric.dimensions):
        errors.append(
            f"draft dimensions {sorted(draft.dimensions)} != rubric dimensions "
            f"{sorted(rubric.dimensions)}"
        )
    if len(draft.evidence) != len(dim_names):
        errors.append(
            f"evidence count {len(draft.evidence)} != dimension count {len(dim_names)}"
        )
    seen: set[tuple[str, str]] = set()
    for ev in draft.evidence:
        key = (ev.ref.path, ev.ref.sha256)
        if key in seen:
            errors.append(f"duplicate evidence ref {ev.ref.path}")
        seen.add(key)
        if ev.digest != ev.ref.sha256:
            errors.append(
                f"evidence digest mismatch for {ev.ref.path}: "
                f"{ev.digest[:8]}... != {ev.ref.sha256[:8]}..."
            )
        if ev.claim_type is not c.ClaimType.SCORE:
            errors.append(
                f"evidence claim_type {ev.claim_type.value} is not SCORE "
                f"for {ev.ref.path}"
            )
        namespace = f"evidence/job-scoped/{job.job_id}/"
        if not ev.ref.path.startswith(namespace):
            errors.append(
                f"evidence ref {ev.ref.path} is not job-scoped "
                f"(expected under {namespace})"
            )
    return errors


def build_score_result(
    runtime: EvalRuntime,
    job: c.ScoreJob,
    outcome: RunnerOutcome,
    node_attempt: int,
    invocation_id,
) -> c.ScoreResult:
    """validator：draft → 校验 → 写 raw/validated output → ScoreResult（唯一 writer）。"""
    store = runtime.store
    base: dict[str, Any] = {
        "binding": job.binding(),
        "retry_count": node_attempt - 1,
        "node_attempt": node_attempt,
        "latency_ms": outcome.latency_ms,
        "usage": outcome.usage,
    }
    if outcome.status is RunnerStatus.CANCELLED:
        return c.ScoreResult(status=c.ScoreJobStatus.CANCELLED, **base)
    if outcome.status is RunnerStatus.TIMED_OUT:
        return c.ScoreResult(status=c.ScoreJobStatus.TIMED_OUT, **base)
    if outcome.status is RunnerStatus.FAILED:
        return c.ScoreResult(
            status=c.ScoreJobStatus.FAILED,
            validation_errors=[outcome.error or "runner failed"],
            **base,
        )
    errors = _validate_draft(
        job, outcome.draft, rubric=_rubric_for(runtime.manifest, job)
    )
    if errors:
        return c.ScoreResult(
            status=c.ScoreJobStatus.FAILED, validation_errors=errors, **base
        )
    if outcome.draft is None:
        return c.ScoreResult(
            status=c.ScoreJobStatus.FAILED,
            validation_errors=["runner returned without a draft"],
            **base,
        )
    draft = outcome.draft
    raw_ref = store.write_canonical_json(
        _agent_run_path(job, invocation_id, "output.raw"),
        {"status": outcome.status.value, "draft": draft.model_dump(mode="json")},
        producer="runner",
    )
    validated_ref = store.write_canonical_json(
        _agent_run_path(job, invocation_id, "output.validated"),
        draft.model_dump(mode="json"),
        producer="validator",
    )
    if outcome.status is RunnerStatus.UNKNOWN:
        return c.ScoreResult(
            status=c.ScoreJobStatus.UNKNOWN,
            raw_output_ref=raw_ref,
            validated_output_ref=validated_ref,
            **base,
        )
    # P3-M6：`_validate_draft` 已保证 evidence 与 dimensions 一一对应且无重复，
    # 此处是严格双射（绝不静默丢弃任一维证据）。
    dim_refs = dict(zip(draft.dimensions.keys(), draft.evidence))
    return c.ScoreResult(
        status=c.ScoreJobStatus.SUCCEEDED,
        dimension_evidence_refs=dim_refs,
        raw_output_ref=raw_ref,
        validated_output_ref=validated_ref,
        **base,
    )


def persist_score_result(
    store: a.ArtifactStore,
    journal: Any,
    job: c.ScoreJob,
    result: c.ScoreResult,
    *,
    lease: a.AttemptLease | None = None,
) -> c.ArtifactRef:
    """持久化 ScoreResult，并把 ScoreJob 从 CLAIMED 转到结果的终态（§2.3）。

    - 正常终态（SUCCEEDED/UNKNOWN/FAILED/TIMED_OUT/CANCELLED）：job 文件同步
      转为与 result 相同的终态；transition 由合同状态机校验。
    - terminal job 后到达的 response 只记为 `late_ignored`，且**不得**改写
      已终态的 job 文件。
    """
    if lease is not None:
        lease.ensure_current()
    job_id = str(job.job_id)
    current = _load_score_job(store, job_id)
    late = False
    if (
        current is not None
        and c.is_job_terminal(current.status)
        and result.status is not c.ScoreJobStatus.LATE_IGNORED
    ):
        result = result.model_copy(
            update={
                "status": c.ScoreJobStatus.LATE_IGNORED,
                "validation_errors": [
                    *result.validation_errors,
                    "response arrived after terminal job status",
                ],
            }
        )
        late = True
    if result.status is not c.ScoreJobStatus.LATE_IGNORED:
        base_job = current if current is not None else job
        c.validate_job_transition(base_job.status, result.status)
        terminal_job = base_job.model_copy(update={"status": result.status})
        store.write_canonical_json(
            f"{JOBS_DIR}/{job_id}.json",
            terminal_job.model_dump(mode="json"),
            producer="builder",
        )
    ref = store.write_canonical_json(
        f"{RESULTS_DIR}/{job_id}.json",
        result.model_dump(mode="json"),
        producer="validator",
    )
    journal.append(
        {
            "kind": "score_result_persisted",
            "job_id": job_id,
            "status": result.status.value,
            "late": late,
        }
    )
    if late:
        journal.append({"kind": "late_ignored", "job_id": job_id})
    return ref


async def _run_score_judge(
    state: dict[str, Any], runtime: EvalRuntime
) -> dict[str, Any]:
    """Send 分支节点：claim → run（semaphore/timeout/retry）→ validate → persist。"""
    runtime._fence()  # 无 current fencing token 不得 claim/写 result（P3-M2）
    store, journal = runtime.store, runtime.journal
    job_id = str(state["job_id"])
    job = _load_score_job(store, job_id)
    if job is None:
        raise a.ArtifactError(f"score job {job_id} missing on run")
    if c.is_job_terminal(job.status):
        return {"score_results": {}}
    if runtime.cancel.requested():
        result = c.ScoreResult(
            binding=job.binding(), status=c.ScoreJobStatus.CANCELLED, node_attempt=0
        )
        ref = persist_score_result(store, journal, job, result, lease=runtime.lease)
        return {"score_results": {job_id: ref}}
    claimed = claim_score_job(store, journal, job, lease=runtime.lease)
    if claimed is None:
        return {"score_results": {}}
    invocation_id = uuid4()
    outcome, node_attempt = await _run_job_with_retry(runtime, claimed, invocation_id)
    if runtime.cancel.requested() and outcome.status is RunnerStatus.SUCCEEDED:
        journal.append(
            {
                "kind": "job_cancelled_inflight",
                "job_id": job_id,
                "node_attempt": node_attempt,
            }
        )
        result = c.ScoreResult(
            binding=claimed.binding(),
            status=c.ScoreJobStatus.CANCELLED,
            retry_count=node_attempt - 1,
            node_attempt=node_attempt,
        )
    else:
        result = build_score_result(
            runtime, claimed, outcome, node_attempt, invocation_id
        )
    ref = persist_score_result(store, journal, claimed, result, lease=runtime.lease)
    return {"score_results": {job_id: ref}}


# ─────────────────────────────────────────────────────────────────────────────
# join / merge / render / finalize
# ─────────────────────────────────────────────────────────────────────────────


def _compute_judge_status(
    jobs: Sequence[c.ScoreJob], results: Sequence[c.ScoreResult]
) -> c.JudgeExecutionStatus:
    result_by_job = {str(r.binding.job_id): r for r in results}
    statuses = [r.status for r in results]
    missing = [j.job_id for j in jobs if str(j.job_id) not in result_by_job]
    if any(j.status is c.ScoreJobStatus.NOT_RUN_BUDGET for j in jobs):
        return c.JudgeExecutionStatus.BUDGET_EXHAUSTED
    if missing or any(
        s
        in {
            c.ScoreJobStatus.FAILED,
            c.ScoreJobStatus.TIMED_OUT,
            c.ScoreJobStatus.CANCELLED,
        }
        for s in statuses
    ):
        return c.JudgeExecutionStatus.PARTIAL
    return c.JudgeExecutionStatus.REQUESTED


def _join_score_jobs(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    store, journal = runtime.store, runtime.journal
    judge = _compute_judge_status(_list_jobs(store), _load_all_results(store))
    journal.append({"kind": "scores_joined", "judge_execution_status": judge.value})
    return {
        "judge_execution_status": judge,
        "phase": _advance_phase(
            state, runtime, c.WorkflowStatus.SCORING, c.WorkflowStatus.SCORES_JOINED
        ),
    }


def _build_merge_groups(
    runtime: EvalRuntime,
    jobs: Sequence[c.ScoreJob],
    results: Sequence[c.ScoreResult],
) -> list[sm.GroupMergeInput]:
    manifest = runtime.manifest.manifest
    grouped: dict[c.SampleTargetType, tuple[list[c.ScoreJob], list[c.ScoreResult]]] = {
        c.SampleTargetType.DISPATCH: ([], []),
        c.SampleTargetType.OBSERVATION: ([], []),
    }
    for job in jobs:
        grouped[job.target_type][0].append(job)
    for result in results:
        grouped[result.binding.target_type][1].append(result)
    groups: list[sm.GroupMergeInput] = []
    for target_type, (group_jobs, group_results) in grouped.items():
        rubrics = [r for r in manifest.rubrics if r.target_type is target_type]
        score_values: dict[Any, dict[str, float]] = {}
        for result in group_results:
            if (
                result.status is c.ScoreJobStatus.SUCCEEDED
                and result.validated_output_ref is not None
            ):
                data = json.loads(
                    runtime.store.read_verified(result.validated_output_ref).decode(
                        "utf-8"
                    )
                )
                score_values[result.binding.job_id] = data.get("dimensions", {})
        groups.append(
            sm.GroupMergeInput(
                target_type=target_type,
                jobs=tuple(group_jobs),
                results=tuple(group_results),
                score_values=score_values,
                rubrics=tuple(rubrics),
            )
        )
    return groups


def _deterministic_score_merge(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    manifest = runtime.manifest.manifest
    if state.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED:
        output = sm.project_not_requested(
            policy_digest=manifest.policy.merge_policy_digest
        )
        ref = store.write_canonical_json(
            MERGED_REL, output.model_dump(mode="json"), producer="merge"
        )
        return {
            "merged_ref": ref,
            "merge_status": output.status,
            "judge_execution_status": c.JudgeExecutionStatus.NOT_REQUESTED,
            "phase": _advance_phase(state, runtime, c.WorkflowStatus.JUDGE_SKIPPED),
        }
    jobs = _list_jobs(store)
    results = _load_all_results(store)
    judge = state.judge_execution_status or _compute_judge_status(jobs, results)
    groups = _build_merge_groups(runtime, jobs, results)
    violations = _load_violations(store)
    try:
        output = sm.merge_score_groups(
            groups,
            policy=sm.MergePolicy(),
            merge_policy_digest=manifest.policy.merge_policy_digest,
            manifest_digest=manifest.digest(),
            deterministic_violations=violations,
        )
    except sm.MergeError as exc:
        journal.append({"kind": "scores_merge_failed", "reason": str(exc)})
        return {
            "failures": [
                c.FailureRef(kind="score_merge", reason=str(exc), source="typed")
            ],
            "merge_status": sm.MergedScoreStatus.FAILED,
            "judge_execution_status": judge,
            "phase": _advance_phase(state, runtime, c.WorkflowStatus.SCORES_MERGED),
        }
    ref = store.write_canonical_json(
        MERGED_REL, output.model_dump(mode="json"), producer="merge"
    )
    journal.append({"kind": "scores_merged", "merge_status": output.status.value})
    updates: dict[str, Any] = {
        "merged_ref": ref,
        "merge_status": output.status,
        "judge_execution_status": judge,
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.SCORES_MERGED),
    }
    if output.status is sm.MergedScoreStatus.FAILED:
        updates["failures"] = [
            c.FailureRef(
                kind="score_merge",
                reason="; ".join(output.violations[:3]),
                source="typed",
            )
        ]
    return updates


# ─────────────────────────────────────────────────────────────────────────────
# report_judge / recommendation_judge（§2.1、§4、§9 deferred roles）
# ─────────────────────────────────────────────────────────────────────────────


def _report_judge_allowlist(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, c.ArtifactRef]:
    """report_judge allowlist：merged bundle + 已物化的 evidence/audit summary refs。

    只收 attempt 内已落盘 artifact 的真实 ref（path + digest），供
    `AllowlistedEvidenceReader` 精确命中；缺文件则跳过，不注入悬空 ref。
    """
    store = runtime.store
    refs: dict[str, c.ArtifactRef] = {}
    merged = state.merged_ref or _rehash_ref(store, MERGED_REL, "merge")
    if merged is not None:
        refs[merged.path] = merged
    for rel in (
        SOURCE_MANIFEST_REL,
        EVIDENCE_BUNDLE_REL,
        GRADER_RESULTS_REL,
        VIOLATIONS_REL,
    ):
        ref = _rehash_ref(store, rel, "evidence")
        if ref is not None:
            refs[ref.path] = ref
    return refs


def _recommendation_judge_allowlist(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, c.ArtifactRef]:
    """recommendation_judge allowlist：frozen merged/report(narrative)/failure refs。"""
    store = runtime.store
    refs: dict[str, c.ArtifactRef] = {}
    merged = state.merged_ref or _rehash_ref(store, MERGED_REL, "merge")
    if merged is not None:
        refs[merged.path] = merged
    narrative = state.narrative_ref or _rehash_ref(
        store, REPORT_NARRATIVE_REL, "report_judge"
    )
    if narrative is not None:
        refs[narrative.path] = narrative
    violations = _rehash_ref(store, VIOLATIONS_REL, "grader")
    if violations is not None:
        refs[violations.path] = violations
    return refs


def _report_draft_refs(draft: Any) -> list[c.ArtifactRef]:
    refs = [ev.ref for ev in draft.evidence]
    for para in draft.paragraphs:
        refs.extend(ev.ref for ev in para.evidence)
    return refs


def _recommendation_draft_refs(draft: Any) -> list[c.ArtifactRef]:
    refs: list[c.ArtifactRef] = []
    for item in draft.recommendations:
        refs.extend(ev.ref for ev in item.evidence)
        for failure in item.failure_refs:
            if failure.ref is not None:
                refs.append(failure.ref)
    return refs


def _validate_allowlist_refs(
    refs: Sequence[c.ArtifactRef], allowlist: dict[str, c.ArtifactRef]
) -> list[str]:
    """draft 引用的每个 ref 必须精确命中角色 allowlist（path + sha256）。"""
    errors: list[str] = []
    for ref in refs:
        entry = allowlist.get(ref.path)
        if entry is None or entry.sha256 != ref.sha256:
            errors.append(
                f"ref {ref.path!r} not in role allowlist (evidence_not_authorized)"
            )
    return errors


async def _run_draft_role(
    runtime: EvalRuntime,
    runner: Any,
    allowlist: dict[str, c.ArtifactRef],
    invocation_id,
    node_attempt: int,
    *,
    role: str,
) -> Any:
    """单次角色模型调用：fencing + invoked/finished audit + timeout（§3.4）。

    与 score job 的 `_run_attempt` 同级：无 current fencing token 不得调用模型。
    事件用 role 专属 kind（`{role}_invoked` / `{role}_finished`），不混入 score
    job 的 `model_requested`（该事件按 job_id 路由，report/recommendation 无 job）。
    """
    runtime._fence()
    journal = runtime.journal
    timeout = (
        runtime.timeout_s
        if runtime.timeout_s is not None
        else float(runtime.manifest.manifest.policy.timeout_s)
    )
    journal.append({"kind": f"{role}_invoked", "node_attempt": node_attempt})
    try:
        outcome = await asyncio.wait_for(
            runner.run(
                invocation_id=invocation_id,
                node_attempt=node_attempt,
                allowlist=allowlist,
            ),
            timeout=timeout,
        )
    except BaseException:
        journal.append({"kind": f"{role}_finished", "node_attempt": node_attempt})
        raise
    journal.append({"kind": f"{role}_finished", "node_attempt": node_attempt})
    return outcome


def _report_judge_failure(
    state: EvalWorkflowState, runtime: EvalRuntime, reason: str
) -> dict[str, Any]:
    """report_judge typed failure → PARTIAL + deterministic report 兜底（设计 line 159）。

    不持久化 narrative artifact —— 绝不写成功叙述；renderer 用 deterministic
    fallback narrative block（`source=deterministic_fallback`）。
    """
    runtime.journal.append({"kind": "report_judge_failed", "reason": reason})
    return {
        "failures": [c.FailureRef(kind="report_judge", reason=reason, source="typed")],
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.REPORT_AUTHORED),
    }


def _deterministic_recommendations_fallback(
    state: EvalWorkflowState, reason: str
) -> c.RecommendationDraft:
    """deterministic fallback：建议项从 failure refs 生成（带 refs），必带原因。"""
    items: list[c.RecommendationItem] = []
    for failure in state.failures:
        severity = (
            c.Severity.CRITICAL
            if failure.kind in _JUDGE_ROLE_FALLBACK_FAILURE_KINDS
            else c.Severity.WARNING
        )
        items.append(
            c.RecommendationItem(
                text=f"{failure.kind}: {failure.reason}",
                failure_refs=[failure],
                severity=severity,
            )
        )
    if not items:
        items.append(
            c.RecommendationItem(
                text="The recommendation judge produced no recommendations; "
                "review the deterministic scores and failure diagnostics.",
                severity=c.Severity.INFO,
            )
        )
    return c.RecommendationDraft(
        role=c.JudgeRole.RECOMMENDATION_JUDGE,
        invocation_id=uuid4(),
        recommendations=items,
        source=c.RecommendationSource.DETERMINISTIC_FALLBACK,
        fallback_reason=reason,
    )


def _recommendation_failure(
    state: EvalWorkflowState, runtime: EvalRuntime, reason: str
) -> dict[str, Any]:
    """recommendation judge typed failure → deterministic fallback 持久化 + PARTIAL。

    fallback artifact 的 `source=deterministic_fallback` + `fallback_reason` 是
    resume 重建 typed failure 的事实源（§2.3/§3.1）；因此本路径只在**真实 judge
    失败**时持久化 —— runner 未注入只跳过（不落盘、不 PARTIAL），避免 resume
    把未配置场景误判为失败。
    """
    store, journal = runtime.store, runtime.journal
    draft = _deterministic_recommendations_fallback(state, reason)
    ref = store.write_canonical_json(
        RECOMMENDATIONS_REL,
        draft.model_dump(mode="json"),
        producer="recommendation_judge",
    )
    journal.append(
        {
            "kind": "recommendations_fallback_persisted",
            "digest": ref.sha256,
            "source": c.RecommendationSource.DETERMINISTIC_FALLBACK.value,
        }
    )
    journal.append({"kind": "recommendation_judge_failed", "reason": reason})
    return {
        "recommendation_ref": ref,
        "failures": [
            c.FailureRef(kind="recommendation_judge", reason=reason, source="typed")
        ],
        "phase": _advance_phase(
            state, runtime, c.WorkflowStatus.RECOMMENDATIONS_AUTHORED
        ),
    }


async def _run_report_judge(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    """requested 路径节点：SCORES_MERGED → REPORT_AUTHORED（§2.1）。

    - allowlist = merged bundle + 已物化 evidence/audit summary refs；
    - `ReportNarrativeDraft` 经 contracts（Pydantic）与 refs 命中校验后持久化
      `report/report_narrative.json`（canonical JSON，进 audit/ledger refs）；
    - 失败（模型异常 / 非法 draft / 越权 ref / judge abstain）→ typed failure
      + PARTIAL + deterministic report 兜底，绝不写成功叙述；
    - runner 未注入 → 只跳过（deterministic fallback，无 PARTIAL）；
    - resume：已持久化 narrative 不重写。
    """
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    existing = _rehash_ref(store, REPORT_NARRATIVE_REL, "report_judge")
    if existing is not None:
        journal.append({"kind": "report_narrative_reused", "digest": existing.sha256})
        return {
            "narrative_ref": existing,
            "phase": _advance_phase(state, runtime, c.WorkflowStatus.REPORT_AUTHORED),
        }
    if runtime.cancel.requested():
        return {}
    if runtime.report_judge_factory is None:
        journal.append(
            {
                "kind": "report_judge_skipped",
                "reason": "report_judge runner not configured",
            }
        )
        return {
            "phase": _advance_phase(state, runtime, c.WorkflowStatus.REPORT_AUTHORED)
        }
    allowlist = _report_judge_allowlist(state, runtime)
    if not allowlist:
        return _report_judge_failure(state, runtime, "report_judge allowlist is empty")
    runner = runtime.report_judge_factory()
    invocation_id = uuid4()
    try:
        outcome = await _run_draft_role(
            runtime, runner, allowlist, invocation_id, 1, role="report_judge"
        )
    except (TimeoutError, RunnerError) as exc:
        return _report_judge_failure(
            state, runtime, f"report_judge runner failed: {exc}"
        )
    except asyncio.CancelledError:
        raise
    if outcome.status is not RunnerStatus.SUCCEEDED:
        reason = outcome.error or "report_judge returned non-success outcome"
        if getattr(getattr(outcome, "draft", None), "fallback_reason", None):
            reason = outcome.draft.fallback_reason
        return _report_judge_failure(state, runtime, reason)
    draft = outcome.draft
    if draft is None:
        return _report_judge_failure(state, runtime, "report_judge returned no draft")
    if getattr(draft, "fallback_reason", None) is not None:
        return _report_judge_failure(
            state, runtime, draft.fallback_reason or "report_judge authored a fallback"
        )
    errors = _validate_allowlist_refs(_report_draft_refs(draft), allowlist)
    if errors:
        return _report_judge_failure(state, runtime, "; ".join(errors))
    ref = store.write_canonical_json(
        REPORT_NARRATIVE_REL, draft.model_dump(mode="json"), producer="report_judge"
    )
    journal.append(
        {
            "kind": "report_narrative_persisted",
            "digest": ref.sha256,
            "source": "report_judge",
        }
    )
    return {
        "narrative_ref": ref,
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.REPORT_AUTHORED),
    }


async def _run_recommendation_judge(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    """requested 路径节点：REPORT_AUTHORED → RECOMMENDATIONS_AUTHORED（§2.1）。

    - allowlist = frozen merged/report(narrative)/failure refs；
    - `RecommendationDraft` 校验 + 持久化 `recommendations/recommendations.json`；
    - judge 失败 → deterministic fallback（`source=deterministic_fallback` 且必带
      fallback_reason）+ typed failure → PARTIAL；
    - runner 未注入 → 只跳过（不落盘、不 PARTIAL，保留既有单 score requested
      测试的 SUCCEEDED 语义）；
    - resume：已持久化 recommendations 不重写。
    """
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    existing = _rehash_ref(store, RECOMMENDATIONS_REL, "recommendation_judge")
    if existing is not None:
        journal.append({"kind": "recommendations_reused", "digest": existing.sha256})
        return {
            "recommendation_ref": existing,
            "phase": _advance_phase(
                state, runtime, c.WorkflowStatus.RECOMMENDATIONS_AUTHORED
            ),
        }
    if runtime.cancel.requested():
        return {}
    if runtime.recommendation_judge_factory is None:
        journal.append(
            {
                "kind": "recommendation_judge_skipped",
                "reason": "recommendation_judge runner not configured",
            }
        )
        return {
            "phase": _advance_phase(
                state, runtime, c.WorkflowStatus.RECOMMENDATIONS_AUTHORED
            )
        }
    allowlist = _recommendation_judge_allowlist(state, runtime)
    if not allowlist:
        return _recommendation_failure(
            state, runtime, "recommendation_judge allowlist is empty"
        )
    runner = runtime.recommendation_judge_factory()
    invocation_id = uuid4()
    try:
        outcome = await _run_draft_role(
            runtime, runner, allowlist, invocation_id, 1, role="recommendation_judge"
        )
    except (TimeoutError, RunnerError) as exc:
        return _recommendation_failure(
            state, runtime, f"recommendation_judge runner failed: {exc}"
        )
    except asyncio.CancelledError:
        raise
    if outcome.status is not RunnerStatus.SUCCEEDED:
        return _recommendation_failure(
            state,
            runtime,
            outcome.error or "recommendation_judge returned non-success outcome",
        )
    draft = outcome.draft
    if draft is None:
        return _recommendation_failure(
            state, runtime, "recommendation_judge returned no draft"
        )
    errors = _validate_allowlist_refs(_recommendation_draft_refs(draft), allowlist)
    if errors:
        return _recommendation_failure(state, runtime, "; ".join(errors))
    ref = store.write_canonical_json(
        RECOMMENDATIONS_REL,
        draft.model_dump(mode="json"),
        producer="recommendation_judge",
    )
    journal.append(
        {
            "kind": "recommendations_persisted",
            "digest": ref.sha256,
            "source": draft.source.value,
        }
    )
    return {
        "recommendation_ref": ref,
        "phase": _advance_phase(
            state, runtime, c.WorkflowStatus.RECOMMENDATIONS_AUTHORED
        ),
    }


def _route_after_merge(state: EvalWorkflowState, runtime: EvalRuntime) -> str:
    """deterministic_score_merge 后分流：not_requested → render；requested → report_judge。"""
    if state.judge_execution_status is c.JudgeExecutionStatus.NOT_REQUESTED:
        return "render"
    return "report_judge"


def _render_narrative_block(
    store: a.ArtifactStore,
    ref: c.ArtifactRef | None,
    state: EvalWorkflowState,
) -> dict[str, Any]:
    """report JSON/MD 的 narrative 块：带 refs；fallback 时明确标注 source。"""
    if ref is not None:
        payload = json.loads(store.read_verified(ref).decode("utf-8"))
        return {
            "source": "report_judge",
            "ref": ref.path,
            "digest": ref.sha256,
            "narrative": payload.get("narrative"),
            "paragraphs": payload.get("paragraphs", []),
            "evidence": payload.get("evidence", []),
            "fallback_reason": payload.get("fallback_reason"),
        }
    reason = next(
        (
            f.reason
            for f in state.failures
            if f.kind in _JUDGE_ROLE_FALLBACK_FAILURE_KINDS and f.reason
        ),
        "report narrative not authored (deterministic fallback)",
    )
    return {
        "source": "deterministic_fallback",
        "ref": None,
        "digest": None,
        "narrative": (
            "The LLM report narrative is unavailable; this is the deterministic "
            f"report fallback. fallback_reason: {reason}"
        ),
        "paragraphs": [],
        "evidence": [],
        "fallback_reason": reason,
    }


def _render_recommendations_block(
    store: a.ArtifactStore, ref: c.ArtifactRef | None
) -> dict[str, Any]:
    """report JSON/MD 的 recommendations 块：带 refs；fallback 时明确标注 source。"""
    if ref is None:
        return {
            "source": "deterministic_fallback",
            "ref": None,
            "digest": None,
            "items": [],
            "fallback_reason": "recommendations not authored (deterministic fallback)",
        }
    payload = json.loads(store.read_verified(ref).decode("utf-8"))
    return {
        "source": payload.get("source", "recommendation_judge"),
        "ref": ref.path,
        "digest": ref.sha256,
        "items": payload.get("recommendations", []),
        "fallback_reason": payload.get("fallback_reason"),
    }


def _advance_phase_chain(
    state: EvalWorkflowState, runtime: EvalRuntime, *path: c.WorkflowStatus
) -> c.WorkflowStatus:
    """沿线性 phase 链前进：跳过 current 已到达的项，只校验 current 之后的合法迁移。

    与 `_advance_phase` 的差异：targets 是链上的有序目标；current 位于链中段时
    （例如 render 节点在 report/recommendation 节点推进之后到达），不再尝试
    从 current 回退到链首，而是只推进 current 之后的 phase。current 不在链中
    （合法前驱）时走完整链。
    """
    current = state.phase
    try:
        first = next(i for i, t in enumerate(path) if t is current)
        remaining = path[first:]
    except StopIteration:
        remaining = path
    return _advance_phase(state, runtime, *remaining)


def _project_terminal(
    state: EvalWorkflowState,
    runtime: EvalRuntime,
    *,
    judge: c.JudgeExecutionStatus,
    merge_status: sm.MergedScoreStatus | None,
) -> c.WorkflowStatus:
    if runtime.cancel.requested():
        return c.WorkflowStatus.CANCELLED
    # hard failures（score_merge / artifact / 其它 deterministic veto）永远否决；
    # report/recommendation 角色失败只降为 PARTIAL（§2.1 line 159）。
    hard = [
        f for f in state.failures if f.kind not in _JUDGE_ROLE_FALLBACK_FAILURE_KINDS
    ]
    if hard:
        return c.WorkflowStatus.FAILED
    if judge is c.JudgeExecutionStatus.NOT_REQUESTED:
        return c.WorkflowStatus.SUCCEEDED
    if merge_status is sm.MergedScoreStatus.FAILED:
        return c.WorkflowStatus.FAILED
    if judge in (
        c.JudgeExecutionStatus.PARTIAL,
        c.JudgeExecutionStatus.BUDGET_EXHAUSTED,
    ):
        return c.WorkflowStatus.PARTIAL
    if merge_status is sm.MergedScoreStatus.PARTIAL:
        return c.WorkflowStatus.PARTIAL
    if state.failures:
        return c.WorkflowStatus.PARTIAL
    return c.WorkflowStatus.SUCCEEDED


def _resolve_judge_status(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> c.JudgeExecutionStatus:
    if state.judge_execution_status is not None:
        return state.judge_execution_status
    if not runtime.manifest.manifest.policy.llm_judge_required:
        return c.JudgeExecutionStatus.NOT_REQUESTED
    return _compute_judge_status(
        _list_jobs(runtime.store), _load_all_results(runtime.store)
    )


def _load_grader_results(store: a.ArtifactStore) -> list[dict]:
    if not store.exists(GRADER_RESULTS_REL):
        return []
    return json.loads(store.read_bytes(GRADER_RESULTS_REL)).get("results", [])


def _render_reports(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    manifest = runtime.manifest.manifest
    subject = manifest.subject
    judge = _resolve_judge_status(state, runtime)
    merged_ref = state.merged_ref
    merge_status = state.merge_status
    if merged_ref is None and judge is c.JudgeExecutionStatus.NOT_REQUESTED:
        output = sm.project_not_requested(
            policy_digest=manifest.policy.merge_policy_digest
        )
        merged_ref = store.write_canonical_json(
            MERGED_REL, output.model_dump(mode="json"), producer="merge"
        )
        merge_status = output.status
        journal.append(
            {
                "kind": "scores_merged",
                "merge_status": output.status.value,
                "projection": "not_requested",
            }
        )
    terminal = _project_terminal(state, runtime, judge=judge, merge_status=merge_status)
    narrative_ref = state.narrative_ref or _rehash_ref(
        store, REPORT_NARRATIVE_REL, "report_judge"
    )
    recommendation_ref = state.recommendation_ref or _rehash_ref(
        store, RECOMMENDATIONS_REL, "recommendation_judge"
    )
    # P5 attempt-family report（§7.2 / §1.2-14）：保留
    # `metadata.eval_semantics_version="attempt-stream-v1"`，并带 report_family /
    # eval_attempt / judge_execution_status。metadata+episode 与 legacy `merge_results`
    # 对齐，使 attempt aggregate 能直接复用 group_by_key/aggregate_group。
    report = {
        "report_family": ATTEMPT_REPORT_FAMILY,
        "eval_semantics_version": manifest.evaluator.evaluator_semantics_version,
        "workflow_version": manifest.evaluator.workflow_version,
        "eval_attempt": f"{manifest.eval_run_id}/{manifest.attempt_id}",
        "attempt_locator": f"{manifest.eval_run_id}/{manifest.attempt_id}",
        "input_manifest_digest": manifest.digest(),
        "run_dir": subject.source_run_ref.path,
        "judge_execution_status": judge.value,
        "merge_status": merge_status.value if merge_status is not None else None,
        "merged_bundle": merged_ref.path if merged_ref is not None else None,
        "terminal_status": terminal.value,
        "score_jobs": sorted(state.score_jobs.keys()),
        "metadata": {
            "scene": subject.scene,
            "agents": subject.agents,
            "seed": subject.seed,
            "eval_semantics_version": manifest.evaluator.evaluator_semantics_version,
            "report_family": ATTEMPT_REPORT_FAMILY,
            "code_commit": subject.code_commit,
            "git_dirty": subject.git_dirty,
        },
        "episode": project_episode_from_grader_results(_load_grader_results(store)),
        "failure_taxonomy": {},
        "failure_diagnostics": {bucket: 0 for bucket in FAILURE_DIAGNOSTIC_BUCKETS},
        "constraint_violations": [],
        "trajectory_checks": [],
        "llm_judge": {},
        "narrative": _render_narrative_block(store, narrative_ref, state),
        "recommendations": _render_recommendations_block(store, recommendation_ref),
        "grader_skips": [],
    }
    report_ref = store.write_canonical_json(REPORT_REL, report, producer="renderer")
    journal.append({"kind": "report_rendered", "report_digest": report_ref.sha256})
    if judge is c.JudgeExecutionStatus.NOT_REQUESTED:
        phase = _advance_phase(state, runtime, c.WorkflowStatus.RENDERED)
    else:
        phase = _advance_phase_chain(
            state,
            runtime,
            c.WorkflowStatus.REPORT_AUTHORED,
            c.WorkflowStatus.RECOMMENDATIONS_AUTHORED,
            c.WorkflowStatus.RENDERED,
        )
    return {
        "merged_ref": merged_ref,
        "merge_status": merge_status,
        "report_ref": report_ref,
        "judge_execution_status": judge,
        "phase": phase,
    }


def write_cancellation_ledger(
    store: a.ArtifactStore,
    manifest: c.FrozenInputManifest,
    journal: Any,
    *,
    report_ref: c.ArtifactRef | None = None,
    now: Callable[[], datetime] | None = None,
    lease: a.AttemptLease | None = None,
) -> c.ArtifactRef | None:
    """CANCELLED ledger 唯一 owner；已存在 ledger → 不再写（once 语义）。"""
    if lease is not None:
        lease.ensure_current()
    if store.exists(a.ArtifactStore.LEDGER_REL):
        return None
    ledger = c.FinalLedger(
        input_manifest_digest=manifest.digest,
        terminal_status=c.WorkflowStatus.CANCELLED,
        finalized_at=now() if now is not None else datetime.now(UTC),
        artifact_refs={"reports/eval_report.json": report_ref}
        if report_ref is not None
        else {},
        report_digest=(
            report_ref.sha256
            if report_ref is not None
            else c.sha256_hex(b"cancelled:no-report")
        ),
        judge_execution_status=manifest.manifest.policy.judge_execution_status,
        failure_refs=[
            c.FailureRef(
                kind="cancelled", reason="cancellation requested", source="signal"
            )
        ],
    )
    ref = store.write_final_ledger(ledger)
    journal.append({"kind": "cancelled", "terminal_status": "cancelled"})
    return ref


def _verify_and_finalize(
    state: EvalWorkflowState, runtime: EvalRuntime
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    if store.exists(a.ArtifactStore.LEDGER_REL):
        ledger = store.read_final_ledger()
        if ledger is None:
            raise a.VerificationError("final ledger present but unreadable")
        return {
            "final_ledger_ref": _ledger_ref(ledger),
            "terminal_status": ledger.terminal_status,
            "phase": ledger.terminal_status,
            "judge_execution_status": ledger.judge_execution_status,
        }
    refs = []
    if state.report_ref is not None:
        refs.append(state.report_ref)
    if state.merged_ref is not None:
        refs.append(state.merged_ref)
    if state.narrative_ref is not None:
        refs.append(state.narrative_ref)
    if state.recommendation_ref is not None:
        refs.append(state.recommendation_ref)
    refs.extend(_list_result_refs(store).values())
    try:
        for ref in refs:
            store.read_verified(ref)
        store.read_input_manifest()
    except (a.VerificationError, a.ContainmentError) as exc:
        journal.append({"kind": "verification_failed", "reason": str(exc)})
        return {
            "failures": [
                c.FailureRef(
                    kind="artifact_verification", reason=str(exc), source="typed"
                )
            ],
            "terminal_status": c.WorkflowStatus.FAILED,
            "phase": _advance_phase(
                state, runtime, c.WorkflowStatus.VERIFIED, c.WorkflowStatus.FAILED
            ),
            "error": str(exc),
        }
    judge = _resolve_judge_status(state, runtime)
    terminal = _project_terminal(
        state, runtime, judge=judge, merge_status=state.merge_status
    )
    if runtime.cancel.requested() or terminal is c.WorkflowStatus.CANCELLED:
        return _finalize_cancelled(state, runtime, judge)
    ledger = c.FinalLedger(
        input_manifest_digest=runtime.manifest.digest,
        terminal_status=terminal,
        finalized_at=runtime.now(),
        artifact_refs=_ledger_artifact_refs(store, state),
        report_digest=(
            state.report_ref.sha256
            if state.report_ref is not None
            else c.sha256_hex(b"no-report")
        ),
        judge_execution_status=judge,
        failure_refs=state.failures,
    )
    ledger_ref = store.write_final_ledger(ledger)
    journal.append(
        {
            "kind": "finalized",
            "terminal_status": terminal.value,
            "judge_execution_status": judge.value,
        }
    )
    return {
        "final_ledger_ref": ledger_ref,
        "terminal_status": terminal,
        "phase": _advance_phase(state, runtime, c.WorkflowStatus.VERIFIED, terminal),
        "judge_execution_status": judge,
    }


def _finalize_cancelled(
    state: EvalWorkflowState, runtime: EvalRuntime, judge: c.JudgeExecutionStatus
) -> dict[str, Any]:
    runtime._fence()
    store, journal = runtime.store, runtime.journal
    if store.exists(a.ArtifactStore.LEDGER_REL):
        ledger = store.read_final_ledger()
        if ledger is None:
            raise a.VerificationError("final ledger present but unreadable")
        return {
            "final_ledger_ref": _ledger_ref(ledger),
            "terminal_status": c.WorkflowStatus.CANCELLED,
            "phase": c.WorkflowStatus.CANCELLED,
            "judge_execution_status": judge,
        }
    ledger = c.FinalLedger(
        input_manifest_digest=runtime.manifest.digest,
        terminal_status=c.WorkflowStatus.CANCELLED,
        finalized_at=runtime.now(),
        artifact_refs=_ledger_artifact_refs(store, state),
        report_digest=(
            state.report_ref.sha256
            if state.report_ref is not None
            else c.sha256_hex(b"cancelled:no-report")
        ),
        judge_execution_status=judge,
        failure_refs=[
            *state.failures,
            c.FailureRef(
                kind="cancelled", reason="cancellation requested", source="signal"
            ),
        ],
    )
    ledger_ref = store.write_final_ledger(ledger)
    journal.append({"kind": "cancelled", "terminal_status": "cancelled"})
    return {
        "final_ledger_ref": ledger_ref,
        "terminal_status": c.WorkflowStatus.CANCELLED,
        "phase": _advance_phase(
            state, runtime, c.WorkflowStatus.VERIFIED, c.WorkflowStatus.CANCELLED
        ),
        "judge_execution_status": judge,
    }


# ─────────────────────────────────────────────────────────────────────────────
# StateGraph factory（§2.1 / §6）
# ─────────────────────────────────────────────────────────────────────────────


def _checkpoint_probe(state: EvalWorkflowState, runtime: EvalRuntime) -> dict[str, Any]:
    """可注入的检查点探针：`probe_interrupt=True` 时在此中断（测试续跑用）。"""
    if runtime.probe_interrupt:
        interrupt("checkpoint-probe")
    return {}


def build_eval_workflow(
    runtime: EvalRuntime, *, checkpointer: Any = None
) -> CompiledStateGraph:
    """确定性 spine + Send fan-out + resume/cancel 的编译图。

    始终以持久化 checkpointer 编译（§6.3）：默认使用 runtime 的 SQLite
    `AsyncSqliteSaver`（`checkpoint.sqlite`，连接生命周期由
    `open_checkpointer`/`close_checkpointer` 掌控）；`checkpointer` 可显式注入
    其它 saver（测试临时 DB 等）。checkpointer 只是 continuation cache，
    manifest/artifacts/ledger 才是成功证据。
    """

    def _node(fn):
        if inspect.iscoroutinefunction(fn):

            async def wrapped_async(state):
                try:
                    return await fn(state, runtime)
                except a.FencingError as exc:
                    return _fencing_failure_state(exc)
                except c.ContractViolation as exc:
                    return _transition_failure_state(exc)

            wrapped_async.__name__ = getattr(fn, "__name__", "node")
            return wrapped_async

        def wrapped(state):
            try:
                return fn(state, runtime)
            except a.FencingError as exc:
                return _fencing_failure_state(exc)
            except c.ContractViolation as exc:
                return _transition_failure_state(exc)

        wrapped.__name__ = getattr(fn, "__name__", "node")
        return wrapped

    cp = checkpointer if checkpointer is not None else runtime.get_checkpointer()

    g = StateGraph(EvalWorkflowState)
    g.add_node("admit_or_resume", _node(_admit_or_resume))
    g.add_node("freeze_input_manifest", _node(_freeze_input_manifest))
    g.add_node("checkpoint_probe", _node(_checkpoint_probe))
    g.add_node("materialize_evidence", _node(_materialize_evidence))
    g.add_node("run_deterministic_graders", _node(_run_deterministic_graders))
    g.add_node("route_judge_mode", _node(_route_judge_mode))
    g.add_node("build_score_jobs", _node(_build_score_jobs))
    g.add_node("run_score_judge", _node(_run_score_judge))
    g.add_node("join_score_jobs", _node(_join_score_jobs))
    g.add_node("deterministic_score_merge", _node(_deterministic_score_merge))
    g.add_node("run_report_judge", _node(_run_report_judge))
    g.add_node("run_recommendation_judge", _node(_run_recommendation_judge))
    g.add_node("render_reports", _node(_render_reports))
    g.add_node("verify_and_finalize", _node(_verify_and_finalize))

    g.add_edge(START, "admit_or_resume")
    g.add_conditional_edges(
        "admit_or_resume",
        _node(_route_resume),
        {
            "end": END,
            "freeze_input_manifest": "freeze_input_manifest",
            "materialize_evidence": "materialize_evidence",
            "run_deterministic_graders": "run_deterministic_graders",
            "build_score_jobs": "build_score_jobs",
            "deterministic_score_merge": "deterministic_score_merge",
            "run_report_judge": "run_report_judge",
            "run_recommendation_judge": "run_recommendation_judge",
            "render_reports": "render_reports",
            "verify_and_finalize": "verify_and_finalize",
        },
    )
    g.add_edge("freeze_input_manifest", "checkpoint_probe")
    g.add_edge("checkpoint_probe", "materialize_evidence")
    g.add_edge("materialize_evidence", "run_deterministic_graders")
    g.add_edge("run_deterministic_graders", "route_judge_mode")
    g.add_conditional_edges(
        "route_judge_mode",
        _node(_route_judge_mode_path),
        {"not_requested": "render_reports", "requested": "build_score_jobs"},
    )
    g.add_conditional_edges(
        "build_score_jobs",
        _node(_route_jobs),
        {"run_score_judge": "run_score_judge", "skip": "join_score_jobs"},
    )
    g.add_edge("run_score_judge", "join_score_jobs")
    g.add_edge("join_score_jobs", "deterministic_score_merge")
    g.add_conditional_edges(
        "deterministic_score_merge",
        _node(_route_after_merge),
        {"render": "render_reports", "report_judge": "run_report_judge"},
    )
    g.add_edge("run_report_judge", "run_recommendation_judge")
    g.add_edge("run_recommendation_judge", "render_reports")
    g.add_edge("render_reports", "verify_and_finalize")
    g.add_edge("verify_and_finalize", END)
    return g.compile(checkpointer=cp)


# ─────────────────────────────────────────────────────────────────────────────
# 执行入口 + 退出码映射
# ─────────────────────────────────────────────────────────────────────────────


def _initial_state(runtime: EvalRuntime) -> dict[str, Any]:
    return {
        "eval_run_id": str(runtime.manifest.manifest.eval_run_id),
        "attempt_id": str(runtime.manifest.manifest.attempt_id),
        "series_root": str(runtime.series_root),
        "attempt_root": str(runtime.store.root),
    }


def _ensure_thread_id(
    runtime: EvalRuntime, config: dict[str, Any] | None
) -> dict[str, Any]:
    """显式 thread_id 优先；否则用 runtime 的稳定 thread（续跑由调用方指定）。"""
    config = dict(config or {})
    configurable = dict(config.get("configurable") or {})
    configurable.setdefault("thread_id", runtime.thread_id)
    config["configurable"] = configurable
    return config


def _has_pending_interrupt(tup: Any) -> bool:
    """检查某 thread 的 checkpoint 是否存在待续跑的 `__interrupt__`。"""
    if tup is None:
        return False
    for write in getattr(tup, "pending_writes", None) or []:
        if len(write) >= 2 and write[1] == "__interrupt__":
            return True
    return False


async def ainvoke_workflow(
    runtime: EvalRuntime,
    *,
    config: dict[str, Any] | None = None,
    resume: str | bool | None = None,
) -> dict[str, Any]:
    """执行（或续跑）workflow。

    P3-M3：thread 由 attempt identity 派生，跨 runtime/进程稳定。`resume` 提供
    `Command(resume=...)` 接缝；为 `None` 时自动探测同 thread 是否存在待续跑的
    interrupt，有则 `Command(resume="proceed")` 继续 —— 中断 → 新 runtime/进程 →
    同 attempt/thread 的真实 checkpoint continuation。
    """
    runtime._run_context = {}
    config = _ensure_thread_id(runtime, config)
    await runtime.open_checkpointer()
    try:
        app = build_eval_workflow(runtime)
        if resume is None:
            tup = await runtime.get_checkpointer().aget_tuple(config)
            if _has_pending_interrupt(tup):
                resume = "proceed"
        if resume is not None:
            payload = "proceed" if resume is True else resume
            return await app.ainvoke(Command(resume=payload), config=config)
        return await app.ainvoke(_initial_state(runtime), config=config)
    finally:
        await runtime.close_checkpointer()


def _outcome_from_state(result: dict[str, Any]) -> WorkflowOutcome:
    status = result.get("terminal_status")
    if status is None:
        status = result.get("phase", c.WorkflowStatus.CREATED)
    if isinstance(status, str):
        status = c.WorkflowStatus(status)
    return WorkflowOutcome(status=status, error=result.get("error"))


def run_eval_workflow(
    runtime: EvalRuntime,
    *,
    config: dict[str, Any] | None = None,
    resume: str | bool | None = None,
) -> WorkflowOutcome:
    result = asyncio.run(ainvoke_workflow(runtime, config=config, resume=resume))
    return _outcome_from_state(result)


def map_workflow_exit(outcome: WorkflowOutcome) -> int:
    """§7.1：0=SUCCEEDED/可诊断 PARTIAL；1=FAILED；2=attempt_busy/publish_conflict；130=CANCELLED。"""
    if outcome.attempt_busy or outcome.publish_conflict:
        return EXIT_ATTEMPT_BUSY
    if outcome.status is c.WorkflowStatus.CANCELLED:
        return EXIT_CANCELLED
    if outcome.status is c.WorkflowStatus.FAILED:
        return EXIT_FAILED
    if outcome.status in (c.WorkflowStatus.SUCCEEDED, c.WorkflowStatus.PARTIAL):
        return EXIT_OK
    return EXIT_ATTEMPT_BUSY


def publish_selected_attempt(
    store: a.ArtifactStore,
    series_root: Path,
    lease: a.AttemptLease,
    *,
    expected_revision: int | None = None,
    journal: Any = None,
) -> int | None:
    """P5 发布 selected pointer（§3.3-4 / §3.5 / §7.1）。

    - 仅 terminal=SUCCEEDED 且 manifest/ledger/report digest 链一致的 attempt 可发布；
      CANCELLED/PARTIAL/FAILED 或缺失 ledger 一律不发布（返回 None）。
    - `expected_revision=None` 时自动按当前 pointer revision 做 CAS bump
      （短回路重跑幂等：恰好一个 winner）；显式 expected_revision 用于并发注入。
    - 返回 exit 码贡献：None=已发布/无需发布；EXIT_ATTEMPT_BUSY(2)=pointer CAS
      冲突；EXIT_FAILED(1)=发布前 digest 校验失败（§1.2-3 / §3.5 fail-closed）。
    """
    try:
        ledger = store.read_final_ledger()
    except a.VerificationError:
        return EXIT_FAILED
    if ledger is None or ledger.terminal_status is not c.WorkflowStatus.SUCCEEDED:
        return None
    try:
        manifest = store.read_input_manifest()
    except a.VerificationError:
        return EXIT_FAILED
    pointer = a.SelectedPointer(series_root)
    current = pointer.read()
    if expected_revision is None:
        expected_revision = None if current is None else current.revision
        new_revision = 0 if current is None else current.revision + 1
    else:
        new_revision = expected_revision + 1
    selected = c.SelectedAttempt(
        eval_run_id=manifest.manifest.eval_run_id,
        attempt_id=manifest.manifest.attempt_id,
        input_manifest_digest=manifest.digest,
        final_ledger_digest=ledger.digest(),
        report_digest=ledger.report_digest,
        revision=new_revision,
    )
    try:
        pointer.publish(store, selected, expected_revision, lease=lease)
    except a.PublishConflict:
        return EXIT_ATTEMPT_BUSY
    except a.VerificationError:
        return EXIT_FAILED
    if journal is not None:
        journal.append({"kind": "pointer_published", "revision": new_revision})
    return None


def _export_attempt_report(store: a.ArtifactStore, output: str | Path) -> None:
    """`--output` 只导出已 final 的 attempt report（§7.1 / §3.1），不改 attempt
    root / ledger / pointer；报告缺失时静默跳过。"""
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if store.exists(REPORT_REL):
        out.write_bytes(store.read_bytes(REPORT_REL))


def install_cancel_handler(runtime: EvalRuntime) -> None:
    """CLI signal handler 是 CANCELLED 的唯一外部 owner（§2.3）。"""
    if threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum: int, frame: Any) -> None:
        runtime.cancel.request()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ─────────────────────────────────────────────────────────────────────────────
# CLI 可注入 workflow adapter 参考实现（§7.1）
# ─────────────────────────────────────────────────────────────────────────────


def _default_grader_fn(
    episode: Any,
) -> tuple[list[Any], list[sm.DeterministicViolation]]:
    from sar_orch.eval.graders import run_all_graders

    results = run_all_graders(episode)
    violations = [
        sm.DeterministicViolation(kind=r.grader, reason=str(r.detail))
        for r in results
        if r.passed is False
    ]
    return results, violations


_SOURCE_FILE_NAMES = (
    "metadata.json",
    "trajectory.csv",
    "router_interactions.csv",
    "subtasks.csv",
    "agent_interactions.csv",
    "summary.csv",
    "token_usage.csv",
    "semantic_map.jsonl",
    "map_summary.jsonl",
    "supervision_state.json",
)


def _source_allowlist(results_dir: Path) -> list[str]:
    return [name for name in _SOURCE_FILE_NAMES if (results_dir / name).exists()]


def _role_config(rubric: c.RubricSpec) -> c.RoleConfig:
    prompt_ref = rubric.prompt_template_ref
    profile_sha = c.sha256_hex(f"profile:{rubric.judge_role.value}".encode())
    schema_sha = c.sha256_hex(f"schema:{rubric.judge_role.value}".encode())
    return c.RoleConfig(
        role=rubric.judge_role,
        agent_id=rubric.judge_role.value,
        prompt_ref=prompt_ref,
        prompt_digest=prompt_ref.sha256,
        model_profile_ref=c.ArtifactRef(
            path=f"snapshots/model_profiles/{rubric.judge_role.value}.json",
            sha256=profile_sha,
            bytes=len(profile_sha),
            media_type="application/json",
            producer="freeze",
        ),
        model_profile_digest=profile_sha,
        tool_schema_ref=c.ArtifactRef(
            path=f"snapshots/tool_schemas/{rubric.judge_role.value}.json",
            sha256=schema_sha,
            bytes=len(schema_sha),
            media_type="application/json",
            producer="freeze",
        ),
        tool_schema_digest=schema_sha,
    )


def _build_manifest(
    results_dir: Path,
    episode: Any,
    source_manifest: c.SourceInputManifest,
    *,
    args: Any,
    eval_run_id,
    attempt_id,
    llm_judge_required: bool,
    source_digest: str,
    scene: int,
    agents: int,
    seed: int,
    code_commit: str,
    git_dirty: bool,
) -> c.InputManifest:
    registry = rr.default_registry()
    dispatch = registry.default_rubric(c.SampleTargetType.DISPATCH).spec
    observation = registry.default_rubric(c.SampleTargetType.OBSERVATION).spec
    subject_payload = {
        "source_run_dir": f"{results_dir.name}",
        "source_digest": source_digest,
        "scene": scene,
        "agents": agents,
        "seed": seed,
        "code_commit": code_commit,
        "git_dirty": git_dirty,
    }
    subject_ref = _canonical_json_ref(SUBJECT_REF_REL, subject_payload, "freeze")
    source_manifest_ref = _canonical_json_ref(
        SOURCE_MANIFEST_REL, source_manifest.model_dump(mode="json"), "freeze"
    )
    return c.InputManifest(
        eval_run_id=eval_run_id,
        attempt_id=attempt_id,
        created_at=datetime.now(UTC),
        subject=c.SubjectRef(
            source_run_ref=subject_ref,
            source_input_manifest_ref=source_manifest_ref,
            source_digest=source_digest,
            scene=scene,
            agents=agents,
            seed=seed,
            code_commit=code_commit,
            git_dirty=git_dirty,
        ),
        evaluator=c.EvaluatorSpec(
            workflow_version="0.1.0",
            source_tree_digest=c.sha256_hex(b"sar-eval-workflow-p3"),
        ),
        policy=c.PolicySpec(
            llm_judge_required=llm_judge_required,
            audit_level=c.AuditLevel.STANDARD,
            retry=2,
            timeout_s=60,
            concurrency=2,
            max_score_jobs=None,
            retention_days=30,
            allow_shared_model=True,
            merge_policy_digest=sm.MergePolicy().digest(),
        ),
        roles=[_role_config(dispatch), _role_config(observation)],
        rubrics=[dispatch, observation],
        command=c.CommandSpec(
            argv_without_secrets=_command_argv(args),
            cwd=str(results_dir),
            env_allowlist=["PATH"],
        ),
    )


def _command_argv(args: Any) -> list[str]:
    """CLI 调用留痕（§7.1 deprecated alias）：`--agent-model`/`--judge-model` 是
    兼容别名，传入时写入 manifest command（无密钥），供审计回放。"""
    argv = ["eval"]
    for name, val in (
        ("--agent-model", getattr(args, "agent_model", None)),
        ("--judge-model", getattr(args, "judge_model", None)),
    ):
        if val:
            argv += [name, str(val)]
    return argv


def _resolved_judge_sample_steps(args: Any) -> int:
    return int(getattr(args, "judge_sample_steps", rr.DEFAULT_JUDGE_SAMPLE_STEPS))


def _resolved_timeout_s(args: Any) -> float | None:
    """单次 role/score invocation 的超时秒数；未提供 → None（用 manifest policy）。

    真实 LLM smoke 可经 `--timeout` 覆盖默认 60s —— deepseek 系 provider 在
    并发/长 evidence 下单次调用可能超过 60s（score 步进已适配，report/
    recommendation 长叙述尤甚）。None 保持既有行为（policy.timeout_s=60）。
    """
    raw = getattr(args, "timeout_s", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid --timeout value: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"--timeout must be positive, got {value}")
    return value


def _validate_resume_inputs(
    frozen: c.FrozenInputManifest,
    args: Any,
    store: a.ArtifactStore,
    snapshot: a.SourceSnapshot,
    *,
    eval_run_id,
    attempt_id,
) -> None:
    """resume 时校验 caller 输入与冻结 manifest 的一致性（§2.2/§2.3 fail-closed）。

    任何 mismatch 抛 `ResumeConfigError`：不得调用 runner、不得修改 canonical
    artifacts。校验项（narrow，至少覆盖审计要求）：
    - locator：frozen eval_run_id/attempt_id 必须等于 caller 解析出的 identity；
    - `--no-llm-judge` 映射：live `llm_judge_required` 必须等于 frozen policy；
    - `judge_sample_steps`：若 evidence bundle 已记录 `sampling_plan.target`，
      必须与 live 值一致（未记录则跳过）；
    - source digest：live snapshot digest 必须等于 frozen subject.source_digest。
    """
    manifest = frozen.manifest
    if str(manifest.eval_run_id) != str(eval_run_id) or str(manifest.attempt_id) != str(
        attempt_id
    ):
        raise ResumeConfigError(
            f"resume locator mismatch: frozen {manifest.eval_run_id}/{manifest.attempt_id} "
            f"!= caller {eval_run_id}/{attempt_id}"
        )
    requested = not bool(getattr(args, "no_llm_judge", False))
    if requested is not manifest.policy.llm_judge_required:
        raise ResumeConfigError(
            f"resume policy mismatch: --no-llm-judge={not requested} conflicts with "
            f"frozen policy llm_judge_required={manifest.policy.llm_judge_required}"
        )
    judge_steps = _resolved_judge_sample_steps(args)
    if store.exists(EVIDENCE_BUNDLE_REL):
        bundle = json.loads(store.read_bytes(EVIDENCE_BUNDLE_REL))
        target = bundle.get("sampling_plan", {}).get("target")
        if target is not None and int(target) != judge_steps:
            raise ResumeConfigError(
                f"resume policy mismatch: judge_sample_steps={judge_steps} conflicts "
                f"with frozen sampling target={target}"
            )
    if snapshot.digest != manifest.subject.source_digest:
        raise ResumeConfigError(
            "resume source mismatch: live source digest "
            f"{snapshot.digest[:16]}... != frozen {manifest.subject.source_digest[:16]}..."
        )


def build_runtime_for_results_dir(
    results_dir: Path | str,
    args: Any,
    *,
    eval_run_id,
    attempt_id,
    attempt_root: Path | str,
    series_root: Path | str,
    lease: a.AttemptLease | None = None,
    runner_factory: Callable[[], AgentRunner] | None = None,
    report_judge_factory: Callable[[], Any] | None = None,
    recommendation_judge_factory: Callable[[], Any] | None = None,
) -> EvalRuntime:
    """从真实结果目录构造 runtime：source 快照 → manifest → store/journal → runtime。

    resume 引用必须是 `<eval_run_id>/<attempt_id>`（§1.2-1）；本实现按给定
    identity 解析 attempt root，绝不猜测裸 attempt_id。

    §2.2/§2.3：**非终态 resume 必须从 immutable `input_manifest.json` 重建
    runtime**，绝不用 live 生成的 manifest（新的 created_at / live 参数）覆盖。
    先安全探测已有 manifest，再校验 caller 输入（policy/judge_sample_steps/
    source digest/locator）；mismatch → `ResumeConfigError`（exit 2），不碰
    canonical artifacts。
    """
    results_dir = Path(results_dir)
    store = a.ArtifactStore(Path(attempt_root))
    journal = store.audit_journal()
    snapshot = snapshot_source_files(results_dir, _source_allowlist(results_dir))
    if store.exists(a.ArtifactStore.MANIFEST_REL):
        # ── resume：从 immutable frozen manifest 重建 runtime ────────────────
        try:
            frozen = store.read_input_manifest()
        except a.VerificationError as exc:
            raise ResumeConfigError(f"corrupt frozen input_manifest: {exc}") from exc
        _validate_resume_inputs(
            frozen,
            args,
            store,
            snapshot,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
        )
        episode = load_episode(results_dir)
        source_manifest = c.SourceInputManifest(
            eval_run_id=frozen.manifest.eval_run_id,
            source_run_dir=results_dir.name,
            files=snapshot.files,
            excluded=list(snapshot.excluded),
        )
        return EvalRuntime(
            manifest=frozen,
            store=store,
            journal=journal,
            lease=lease,
            runner_factory=runner_factory,
            report_judge_factory=report_judge_factory,
            recommendation_judge_factory=recommendation_judge_factory,
            source_manifest=source_manifest,
            episode=episode,
            grader_fn=partial(_default_grader_fn, episode),
            judge_sample_steps=_resolved_judge_sample_steps(args),
            timeout_s=_resolved_timeout_s(args),
        )
    # ── 全新 attempt：source 快照 → 构建 manifest → runtime ───────────────────
    episode = load_episode(results_dir)
    source_manifest = c.SourceInputManifest(
        eval_run_id=eval_run_id,
        source_run_dir=results_dir.name,
        files=snapshot.files,
        excluded=list(snapshot.excluded),
    )
    metadata = episode.metadata or {}
    try:
        scene = int(metadata.get("scene", 1))
        seed = int(metadata.get("seed", 0))
    except (TypeError, ValueError):
        scene, seed = 1, 0
    agents = metadata.get("agent_count") or metadata.get("agents") or 2
    try:
        agents = int(agents)
    except (TypeError, ValueError):
        agents = 2
    manifest = _build_manifest(
        results_dir,
        episode,
        source_manifest,
        args=args,
        eval_run_id=eval_run_id,
        attempt_id=attempt_id,
        llm_judge_required=not bool(getattr(args, "no_llm_judge", False)),
        source_digest=snapshot.digest,
        scene=scene,
        agents=agents,
        seed=seed,
        code_commit=metadata.get("code_commit", ""),
        git_dirty=bool(metadata.get("git_dirty", False)),
    )
    return EvalRuntime(
        manifest=manifest.freeze(),
        store=store,
        journal=journal,
        lease=lease,
        runner_factory=runner_factory,
        report_judge_factory=report_judge_factory,
        recommendation_judge_factory=recommendation_judge_factory,
        source_manifest=source_manifest,
        episode=episode,
        grader_fn=partial(_default_grader_fn, episode),
        judge_sample_steps=_resolved_judge_sample_steps(args),
        timeout_s=_resolved_timeout_s(args),
    )


def _resolve_attempt_paths(
    results_dir: Path, eval_run_id=None, attempt_id=None, attempt_root=None
) -> tuple[Path, Path, Any, Any]:
    from uuid import uuid4 as _uuid4

    eval_run_id = eval_run_id or _uuid4()
    attempt_id = attempt_id or _uuid4()
    if attempt_root is not None:
        # §3.1：`--attempt-root` 是受控 attempt root；series 由其父目录推断。
        # 合法性与 symlink 拒斥由 ArtifactStore 在 mkdir/open/lease 前验证。
        attempt_root = Path(attempt_root)
        return attempt_root.parent.parent, attempt_root, eval_run_id, attempt_id
    series_root = results_dir / "eval_attempts" / str(eval_run_id)
    attempt_root = series_root / "attempts" / str(attempt_id)
    return series_root, attempt_root, eval_run_id, attempt_id


def run_from_results_dir(
    args: Any,
    *,
    runner_factory: Callable[[], AgentRunner] | None = None,
    runner_factory_factory: Callable[[EvalRuntime], Callable[[], AgentRunner]]
    | None = None,
    report_judge_factory_factory: Callable[[EvalRuntime], Callable[[], Any]]
    | None = None,
    recommendation_judge_factory_factory: Callable[[EvalRuntime], Callable[[], Any]]
    | None = None,
) -> int:
    """CLI workflow adapter 参考实现：no-LLM 零构造；attempt_busy→2；CANCELLED→130。

    `runner_factory_factory` 在 runtime 构建完成后被调用（用于需要 manifest
    才能构造 fake runner 的注入场景）；真实 runner 属 P4，未注入时 requested
    路径返回 2（不静默构造任何 LLM）。

    P5（§7.1 / §3.5）：resume 引用解析 `<eval_run_id>/<attempt_id>`（裸 attempt_id
    拒绝）；SUCCEEDED 后发布 selected pointer（publish_conflict→2）；CANCELLED
    绝不发布；`--output` 只导出已 final report。
    """
    results_dir = Path(args.results_dir)
    if not results_dir.exists() or not results_dir.is_dir():
        return EXIT_ATTEMPT_BUSY
    requested = not bool(getattr(args, "no_llm_judge", False))
    if requested and runner_factory is None and runner_factory_factory is None:
        return EXIT_ATTEMPT_BUSY
    eval_run_id = getattr(args, "eval_run_id", None)
    attempt_id = getattr(args, "attempt_id", None)
    resume_locator = getattr(args, "resume_attempt", None)
    if resume_locator:
        try:
            loc = c.AttemptLocator.parse(resume_locator)
        except c.ContractViolation:
            # 非法 locator（非 `<uuid>/<uuid>` / 裸 attempt_id）→ exit 2。
            return EXIT_ATTEMPT_BUSY
        eval_run_id, attempt_id = loc.eval_run_id, loc.attempt_id
    series_root, attempt_root, eval_run_id, attempt_id = _resolve_attempt_paths(
        results_dir,
        eval_run_id,
        attempt_id,
        attempt_root=getattr(args, "attempt_root", None),
    )
    try:
        lease = a.AttemptLease.acquire(series_root, owner_id="eval-cli")
    except a.AttemptBusy:
        return EXIT_ATTEMPT_BUSY
    try:
        runtime = build_runtime_for_results_dir(
            results_dir,
            args,
            eval_run_id=eval_run_id,
            attempt_id=attempt_id,
            attempt_root=attempt_root,
            series_root=series_root,
            lease=lease,
            runner_factory=runner_factory,
        )
        if runner_factory is None and runner_factory_factory is not None:
            runtime.runner_factory = runner_factory_factory(runtime)
        if report_judge_factory_factory is not None:
            runtime.report_judge_factory = report_judge_factory_factory(runtime)
        if recommendation_judge_factory_factory is not None:
            runtime.recommendation_judge_factory = recommendation_judge_factory_factory(
                runtime
            )
        install_cancel_handler(runtime)
        outcome = run_eval_workflow(runtime)
        if outcome.status is c.WorkflowStatus.SUCCEEDED:
            code = publish_selected_attempt(
                runtime.store, series_root, lease, journal=runtime.journal
            )
            if code is not None:
                return code
        output = getattr(args, "output", None)
        if output and outcome.status in (
            c.WorkflowStatus.SUCCEEDED,
            c.WorkflowStatus.PARTIAL,
        ):
            _export_attempt_report(runtime.store, output)
        return map_workflow_exit(outcome)
    except ResumeConfigError:
        # resume 输入与冻结 manifest 不一致：fail-closed，exit 2（§7.1）。
        return EXIT_ATTEMPT_BUSY
    finally:
        lease.release()
