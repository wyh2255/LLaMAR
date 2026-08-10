"""P0 评估合同：类型化身份、状态机、ArtifactRef、两阶段 manifest/ledger、score-job 绑定、role policy 与 drafts。

本模块只定义**纯数据合同**（Pydantic v2 模型 + 校验函数），
不包含任何 I/O、持久化、workflow、audit allocator、role runner、rubric registry 或 CLI。
后续 phase（P1+）在这些合同之上实现 artifact 服务、LangGraph 控制面与角色运行器。

设计依据：
.hermes/plans/2026-08-06_004210-eval-judge-transparent-langgraph-design.md
§1.2（核心不变量）、§2.2-2.3（State 与状态矩阵）、§3.2（input manifest / final ledger）、
§4（角色隔离与 Draft）、§5.1（Sample / ScoreJob 绑定）。
"""

import hashlib
import json
import math
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    computed_field,
    field_validator,
    model_validator,
)

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "ArtifactRef",
    "AttemptLocator",
    "AttemptRelativePath",
    "AuditLevel",
    "CalibrationStatus",
    "ClaimType",
    "CommandSpec",
    "ContractViolation",
    "EvalAttemptIdentity",
    "EvalSeriesIdentity",
    "EvaluatorSpec",
    "EvidenceRef",
    "FailureRef",
    "FinalLedger",
    "FrozenInputManifest",
    "HarnessProfile",
    "InputManifest",
    "JudgeExecutionStatus",
    "JudgeRole",
    "PolicySpec",
    "RecommendationDraft",
    "RecommendationItem",
    "RecommendationSource",
    "ReportNarrativeDraft",
    "ReportParagraph",
    "RoleConfig",
    "RoleHarnessPolicy",
    "RubricSpec",
    "SHA256Hex",
    "Sample",
    "SampleTargetType",
    "ScoreDraft",
    "ScoreJob",
    "ScoreJobBinding",
    "ScoreJobStatus",
    "ScoreResult",
    "SelectedAttempt",
    "Severity",
    "SourceInputManifest",
    "SubjectRef",
    "UsageSnapshot",
    "WorkflowStatus",
    "canonical_json",
    "check_job_result_binding",
    "is_job_terminal",
    "is_workflow_terminal",
    "job_can_transition",
    "sha256_hex",
    "validate_job_transition",
    "validate_judge_execution_status",
    "validate_late_response",
    "validate_terminal_judge_consistency",
    "validate_workflow_transition",
    "workflow_can_transition",
]


class ContractViolation(ValueError):
    """合同语义违例（locator / transition / binding / judge-status 等）。"""


# ─────────────────────────────────────────────────────────────────────────────
# 常量与基础类型
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_SCHEMA_VERSION = 1

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_RESERVED_SOURCE_DIRS = ("eval_attempts", "eval_workspace")
_REQUIRED_EXCLUDED_TOOLS = ("write_file", "edit_file", "execute", "task")
_FORBIDDEN_ROLE_TOOLS = frozenset(
    {
        "write_score",
        "write_merge",
        "write_ledger",
        "publish_pointer",
        "append_audit",
        "write_report",
        "write_recommendation",
    }
)
def canonical_json(data: Any) -> str:
    """稳定 canonical JSON：排序键、紧凑分隔符、保留非 ASCII 文本。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    """UTF-8 字节流的 SHA-256 小写十六进制。"""
    return hashlib.sha256(data).hexdigest()


def _validate_sha256_hex(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
        raise ValueError("expected 64 lowercase hex characters")
    return value


def _validate_attempt_relative_path(value: str) -> str:
    """attempt root 下的规范相对路径：拒绝绝对路径、`..`、`.`、空段与反斜杠。"""
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    if value.startswith(("/", "\\")):
        raise ValueError("path must be relative (no absolute path)")
    if "\\" in value:
        raise ValueError("path must use '/' separators (backslash rejected)")
    if value.startswith("~"):
        raise ValueError("path must be relative")
    if _WINDOWS_DRIVE_RE.match(value):
        raise ValueError("path must be relative (no drive prefix)")
    segments = value.split("/")
    if any(not segment for segment in segments):
        raise ValueError("path must be canonical (no empty/duplicate segments)")
    if any(segment == "." for segment in segments):
        raise ValueError("path must be canonical (no '.' segments)")
    if any(segment == ".." for segment in segments):
        raise ValueError("path must not traverse outside the attempt root ('..' rejected)")
    return value


def _validate_schema_version(value: int) -> int:
    if value != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {value!r}; supported: {SUPPORTED_SCHEMA_VERSION}"
        )
    return value


SHA256Hex = Annotated[str, AfterValidator(_validate_sha256_hex)]
AttemptRelativePath = Annotated[str, AfterValidator(_validate_attempt_relative_path)]


class VersionedContract(BaseModel):
    """带受控 schema_version 的合同基类。"""

    schema_version: int = SUPPORTED_SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return _validate_schema_version(value)


# ─────────────────────────────────────────────────────────────────────────────
# 身份命名空间与定位符
# ─────────────────────────────────────────────────────────────────────────────


class EvalSeriesIdentity(VersionedContract):
    """稳定 series 身份：一个 source run 一个 series，首次创建时写入 series.json。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: UUID
    source_run_dir: AttemptRelativePath
    source_digest: SHA256Hex
    created_at: datetime


class EvalAttemptIdentity(VersionedContract):
    """series 内唯一 attempt 身份；只通过 <eval_run_id>/<attempt_id> 定位，裸 attempt_id 不猜测。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: UUID
    attempt_id: UUID
    created_at: datetime

    def locator(self) -> "AttemptLocator":
        return AttemptLocator(eval_run_id=self.eval_run_id, attempt_id=self.attempt_id)


class AttemptLocator(BaseModel):
    """`<eval_run_id>/<attempt_id>` 恢复引用；裸 attempt_id 必须拒绝，不得猜测匹配。"""

    model_config = ConfigDict(frozen=True)

    eval_run_id: UUID
    attempt_id: UUID

    @classmethod
    def parse(cls, value: str) -> "AttemptLocator":
        if not isinstance(value, str):
            raise ContractViolation("attempt locator must be a string")
        parts = value.split("/")
        if len(parts) != 2:
            raise ContractViolation(
                f"attempt locator must be '<eval_run_id>/<attempt_id>', "
                f"got {len(parts)} part(s) from {value!r}"
            )
        eval_run_id, attempt_id = parts
        if not eval_run_id or not attempt_id:
            raise ContractViolation(f"attempt locator parts must be non-empty: {value!r}")
        try:
            return cls(eval_run_id=UUID(eval_run_id), attempt_id=UUID(attempt_id))
        except ValueError as exc:
            raise ContractViolation(
                f"attempt locator must contain two UUIDs, got {value!r}"
            ) from exc

    def __str__(self) -> str:
        return f"{self.eval_run_id}/{self.attempt_id}"


# ─────────────────────────────────────────────────────────────────────────────
# 类型化状态机
# ─────────────────────────────────────────────────────────────────────────────


class WorkflowStatus(str, Enum):
    CREATED = "created"
    INPUT_VALIDATED = "input_validated"
    ADMITTED = "admitted"
    MANIFEST_FROZEN = "manifest_frozen"
    EVIDENCE_MATERIALIZED = "evidence_materialized"
    DETERMINISTIC_GRADED = "deterministic_graded"
    JUDGE_SKIPPED = "judge_skipped"
    SCORE_JOBS_READY = "score_jobs_ready"
    SCORING = "scoring"
    SCORES_JOINED = "scores_joined"
    SCORES_MERGED = "scores_merged"
    REPORT_AUTHORED = "report_authored"
    RECOMMENDATIONS_AUTHORED = "recommendations_authored"
    RENDERED = "rendered"
    VERIFIED = "verified"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


WORKFLOW_TERMINAL_STATUSES = frozenset(
    {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }
)


def is_workflow_terminal(status: WorkflowStatus) -> bool:
    return status in WORKFLOW_TERMINAL_STATUSES


_WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.CREATED: frozenset(
        {WorkflowStatus.INPUT_VALIDATED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.INPUT_VALIDATED: frozenset(
        {WorkflowStatus.ADMITTED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.ADMITTED: frozenset(
        {WorkflowStatus.MANIFEST_FROZEN, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.MANIFEST_FROZEN: frozenset(
        {WorkflowStatus.EVIDENCE_MATERIALIZED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.EVIDENCE_MATERIALIZED: frozenset(
        {WorkflowStatus.DETERMINISTIC_GRADED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.DETERMINISTIC_GRADED: frozenset(
        {
            WorkflowStatus.JUDGE_SKIPPED,
            WorkflowStatus.SCORE_JOBS_READY,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.JUDGE_SKIPPED: frozenset(
        {WorkflowStatus.RENDERED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.SCORE_JOBS_READY: frozenset(
        {WorkflowStatus.SCORING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.SCORING: frozenset(
        {WorkflowStatus.SCORES_JOINED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.SCORES_JOINED: frozenset(
        {WorkflowStatus.SCORES_MERGED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.SCORES_MERGED: frozenset(
        {WorkflowStatus.REPORT_AUTHORED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.REPORT_AUTHORED: frozenset(
        {WorkflowStatus.RECOMMENDATIONS_AUTHORED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.RECOMMENDATIONS_AUTHORED: frozenset(
        {WorkflowStatus.RENDERED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.RENDERED: frozenset(
        {WorkflowStatus.VERIFIED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.VERIFIED: frozenset(
        {
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.PARTIAL,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.SUCCEEDED: frozenset(),
    WorkflowStatus.PARTIAL: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}


def workflow_can_transition(current: WorkflowStatus, next_: WorkflowStatus) -> bool:
    return next_ in _WORKFLOW_TRANSITIONS.get(current, frozenset())


def validate_workflow_transition(current: WorkflowStatus, next_: WorkflowStatus) -> None:
    """terminal 不回退；阶段必须按 §2.3 矩阵前进。"""
    if not workflow_can_transition(current, next_):
        raise ContractViolation(
            f"invalid workflow transition {current.value} -> {next_.value}"
        )


class ScoreJobStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    NOT_RUN_BUDGET = "not_run_budget"
    LATE_IGNORED = "late_ignored"


SCORE_JOB_TERMINAL_STATUSES = frozenset(
    {
        ScoreJobStatus.SUCCEEDED,
        ScoreJobStatus.UNKNOWN,
        ScoreJobStatus.FAILED,
        ScoreJobStatus.TIMED_OUT,
        ScoreJobStatus.CANCELLED,
        ScoreJobStatus.NOT_RUN_BUDGET,
    }
)

SCORE_RESULT_TERMINAL_STATUSES = SCORE_JOB_TERMINAL_STATUSES | frozenset(
    {ScoreJobStatus.LATE_IGNORED}
)


def is_job_terminal(status: ScoreJobStatus) -> bool:
    return status in SCORE_JOB_TERMINAL_STATUSES


_JOB_TRANSITIONS: dict[ScoreJobStatus, frozenset[ScoreJobStatus]] = {
    ScoreJobStatus.PENDING: frozenset(
        {ScoreJobStatus.CLAIMED, ScoreJobStatus.CANCELLED, ScoreJobStatus.NOT_RUN_BUDGET}
    ),
    ScoreJobStatus.CLAIMED: frozenset(
        {
            ScoreJobStatus.SUCCEEDED,
            ScoreJobStatus.UNKNOWN,
            ScoreJobStatus.FAILED,
            ScoreJobStatus.TIMED_OUT,
            ScoreJobStatus.CANCELLED,
        }
    ),
    ScoreJobStatus.SUCCEEDED: frozenset(),
    ScoreJobStatus.UNKNOWN: frozenset(),
    ScoreJobStatus.FAILED: frozenset(),
    ScoreJobStatus.TIMED_OUT: frozenset(),
    ScoreJobStatus.CANCELLED: frozenset(),
    ScoreJobStatus.NOT_RUN_BUDGET: frozenset(),
    ScoreJobStatus.LATE_IGNORED: frozenset(),
}


def job_can_transition(current: ScoreJobStatus, next_: ScoreJobStatus) -> bool:
    return next_ in _JOB_TRANSITIONS.get(current, frozenset())


def validate_job_transition(current: ScoreJobStatus, next_: ScoreJobStatus) -> None:
    if not job_can_transition(current, next_):
        raise ContractViolation(
            f"invalid score job transition {current.value} -> {next_.value}"
        )


def validate_late_response(job_status: ScoreJobStatus) -> None:
    """terminal 后到达的 response 只允许记为 late_ignored。"""
    if not is_job_terminal(job_status):
        raise ContractViolation(
            f"late response is only valid after a terminal job status, got {job_status.value}"
        )


class JudgeExecutionStatus(str, Enum):
    REQUESTED = "requested"
    NOT_REQUESTED = "not_requested"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"


def validate_judge_execution_status(
    status: JudgeExecutionStatus,
    *,
    requested_path: bool,
    job_count: int = 0,
    not_run_budget_count: int = 0,
    missing_required: bool = False,
) -> None:
    """§1.2-12/13 与 §2.3：judge 状态语义。

    - not_requested 永不创建 score job（job_count 必须为 0）；
    - requested LLM path 不得是 not_requested；
    - budget_exhausted 当且仅当存在 not_run_budget job；
    - PARTIAL 只出现在 requested path 且缺 required 项时。
    """
    if not requested_path and status is not JudgeExecutionStatus.NOT_REQUESTED:
        raise ContractViolation(
            f"no-LLM path requires judge_execution_status=not_requested, got {status.value}"
        )
    if requested_path and status is JudgeExecutionStatus.NOT_REQUESTED:
        raise ContractViolation(
            "requested LLM path cannot have judge_execution_status=not_requested"
        )
    if status is JudgeExecutionStatus.NOT_REQUESTED and job_count:
        raise ContractViolation(
            f"not_requested must never create score jobs, got {job_count}"
        )
    if (
        status is JudgeExecutionStatus.BUDGET_EXHAUSTED
        and not_run_budget_count < 1
    ):
        raise ContractViolation(
            "budget_exhausted requires at least one not_run_budget score job"
        )
    if status is JudgeExecutionStatus.PARTIAL and (
        not requested_path or not missing_required
    ):
        raise ContractViolation(
            "partial requires a requested LLM path with missing required items"
        )


def validate_terminal_judge_consistency(
    *,
    terminal_status: WorkflowStatus,
    judge_execution_status: JudgeExecutionStatus,
) -> None:
    """terminal workflow 状态与 judge 状态的一致性（§1.2-12 / §2.3）。"""
    if not is_workflow_terminal(terminal_status):
        raise ContractViolation(f"{terminal_status.value} is not a terminal workflow status")
    if (
        terminal_status is WorkflowStatus.PARTIAL
        and judge_execution_status is JudgeExecutionStatus.NOT_REQUESTED
    ):
        raise ContractViolation("not_requested must never cause PARTIAL")
    if (
        terminal_status is WorkflowStatus.SUCCEEDED
        and judge_execution_status is JudgeExecutionStatus.BUDGET_EXHAUSTED
    ):
        raise ContractViolation("budget_exhausted cannot end in SUCCEEDED")


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactRef 与 source manifest
# ─────────────────────────────────────────────────────────────────────────────


class ArtifactRef(BaseModel):
    """根目录受限 artifact 引用：attempt root 下规范相对路径 + SHA-256。

    不保存内容本身；State / manifest 只持有引用与 digest。
    """

    model_config = ConfigDict(extra="forbid")

    path: AttemptRelativePath
    sha256: SHA256Hex
    bytes: int
    media_type: str
    producer: str

    @field_validator("bytes")
    @classmethod
    def _non_negative_size(cls, value: int) -> int:
        if value < 0:
            raise ValueError("artifact size must be non-negative")
        return value

    @field_validator("media_type", "producer")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class SourceInputManifest(VersionedContract):
    """source run 输入白名单快照：load_episode() 实际消费文件 → SHA-256。

    排除 eval_attempts/ 与 eval_workspace/（红线字段只能作为 redaction reason）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: UUID
    source_run_dir: AttemptRelativePath
    files: dict[AttemptRelativePath, SHA256Hex]
    excluded: list[str] = []

    @field_validator("files")
    @classmethod
    def _no_reserved_output_dirs(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            first_segment = path.split("/", 1)[0]
            if first_segment in _RESERVED_SOURCE_DIRS:
                raise ValueError(
                    f"source manifest must not snapshot reserved dir {first_segment!r}"
                )
        return value


# ─────────────────────────────────────────────────────────────────────────────
# input manifest 与 final ledger 分离
# ─────────────────────────────────────────────────────────────────────────────


class AuditLevel(str, Enum):
    STANDARD = "standard"
    REDACTED = "redacted"


class SubjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_ref: ArtifactRef
    source_input_manifest_ref: ArtifactRef
    source_digest: SHA256Hex
    scene: int
    agents: int
    seed: int
    code_commit: str
    git_dirty: bool


class EvaluatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: str
    evaluator_semantics_version: str = "attempt-stream-v1"
    source_tree_digest: SHA256Hex


class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_judge_required: bool
    audit_level: AuditLevel
    retry: int
    timeout_s: int
    concurrency: int
    max_score_jobs: int | None = None
    retention_days: int
    allow_shared_model: bool = True
    merge_policy_digest: SHA256Hex

    @property
    def judge_execution_status(self) -> JudgeExecutionStatus:
        return (
            JudgeExecutionStatus.REQUESTED
            if self.llm_judge_required
            else JudgeExecutionStatus.NOT_REQUESTED
        )

    @field_validator("retry", "timeout_s", "retention_days")
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be non-negative")
        return value

    @field_validator("concurrency")
    @classmethod
    def _positive_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("concurrency must be >= 1")
        return value

    @field_validator("max_score_jobs")
    @classmethod
    def _unbounded_or_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_score_jobs must be >= 1 or None (unbounded)")
        return value


class CommandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv_without_secrets: list[str]
    cwd: str
    env_allowlist: list[str]


class RoleConfig(BaseModel):
    """单个角色的冻结配置：prompt / model profile / tool schema 各自的 ref 与 digest。"""

    model_config = ConfigDict(extra="forbid")

    role: "JudgeRole"
    agent_id: str
    prompt_ref: ArtifactRef
    prompt_digest: SHA256Hex
    model_profile_ref: ArtifactRef
    model_profile_digest: SHA256Hex
    tool_schema_ref: ArtifactRef
    tool_schema_digest: SHA256Hex

    @field_validator("agent_id")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("agent_id must be non-empty")
        return value


class CalibrationStatus(str, Enum):
    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"


class SampleTargetType(str, Enum):
    DISPATCH = "dispatch"
    OBSERVATION = "observation"


class RubricSpec(BaseModel):
    """版本化 rubric 的冻结绑定；judge_role 必须已配置在 manifest roles 中。"""

    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    version: str
    digest: SHA256Hex
    target_type: SampleTargetType
    input_selector: str
    dimensions: list[str]
    prompt_template_ref: ArtifactRef
    judge_role: "JudgeRole"
    merge_group: str
    weight: float
    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED

    @field_validator("rubric_id", "version", "merge_group", "input_selector")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("dimensions")
    @classmethod
    def _non_empty_dimensions(cls, value: list[str]) -> list[str]:
        if not value or any(not d or not d.strip() for d in value):
            raise ValueError("dimensions must be a non-empty list of names")
        return value

    @field_validator("weight")
    @classmethod
    def _non_negative_weight(cls, value: float) -> float:
        if value < 0 or not math.isfinite(value):
            raise ValueError("weight must be a finite non-negative number")
        return value


class InputManifest(VersionedContract):
    """冻结输入清单：只写一次；freeze 后字节与 SHA-256 永不改变。

    显式拒绝 terminal / finalization 字段（extra=forbid）：terminal_status、
    finalized_at、report_digest、pointer_revision 等只属于 final_ledger.json。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: UUID
    attempt_id: UUID
    created_at: datetime
    subject: SubjectRef
    evaluator: EvaluatorSpec
    policy: PolicySpec
    roles: list[RoleConfig]
    rubrics: list[RubricSpec]
    command: CommandSpec

    @model_validator(mode="after")
    def _rubric_role_must_be_configured(self) -> "InputManifest":
        configured = {role.role for role in self.roles}
        for rubric in self.rubrics:
            if rubric.judge_role not in configured:
                raise ValueError(
                    f"rubric {rubric.rubric_id} judge_role "
                    f"{rubric.judge_role.value} is not a configured manifest role"
                )
        return self

    def digest(self) -> str:
        """当前字节的 canonical SHA-256（freeze 前后的引用锚点）。"""
        return sha256_hex(canonical_json(self.model_dump(mode="json")).encode("utf-8"))

    def freeze(self) -> "FrozenInputManifest":
        return FrozenInputManifest(manifest=self)


class FrozenInputManifest(BaseModel):
    """freeze 后的不可变 input manifest 快照（含规范 digest，不可伪造）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: InputManifest

    @computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        return self.manifest.digest()

    def payload(self) -> dict[str, Any]:
        """供 artifact writer 持久化的 JSON 安全 payload。"""
        return self.manifest.model_dump(mode="json")


class FailureRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    reason: str
    ref: ArtifactRef | None = None
    source: str = "typed"

    @field_validator("kind", "reason")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be non-empty")
        return value


class FinalLedger(BaseModel):
    """terminal status 的唯一 owner；反向引用 input manifest digest。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_manifest_digest: SHA256Hex
    terminal_status: WorkflowStatus
    finalized_at: datetime
    artifact_refs: dict[str, ArtifactRef]
    report_digest: SHA256Hex
    judge_execution_status: JudgeExecutionStatus
    pointer_revision: int | None = None
    failure_refs: list[FailureRef] = []

    @field_validator("terminal_status")
    @classmethod
    def _must_be_terminal(cls, value: WorkflowStatus) -> WorkflowStatus:
        if not is_workflow_terminal(value):
            raise ValueError(f"{value.value} is not a terminal workflow status")
        return value

    @field_validator("pointer_revision")
    @classmethod
    def _non_negative_revision(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("pointer_revision must be >= 0")
        return value

    @model_validator(mode="after")
    def _judge_consistency(self) -> "FinalLedger":
        validate_terminal_judge_consistency(
            terminal_status=self.terminal_status,
            judge_execution_status=self.judge_execution_status,
        )
        return self

    def digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")).encode("utf-8"))


class SelectedAttempt(BaseModel):
    """唯一 publish pointer（CAS 更新）；aggregate/gate 默认只消费它。"""

    model_config = ConfigDict(extra="forbid")

    eval_run_id: UUID
    attempt_id: UUID
    input_manifest_digest: SHA256Hex
    final_ledger_digest: SHA256Hex
    report_digest: SHA256Hex
    revision: int

    @field_validator("revision")
    @classmethod
    def _non_negative_revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("pointer revision must be >= 0")
        return value


# ─────────────────────────────────────────────────────────────────────────────
# 角色隔离与 harness policy
# ─────────────────────────────────────────────────────────────────────────────


class JudgeRole(str, Enum):
    DISPATCH_SCORE_JUDGE = "dispatch_score_judge"
    OBSERVATION_SCORE_JUDGE = "observation_score_judge"
    REPORT_JUDGE = "report_judge"
    RECOMMENDATION_JUDGE = "recommendation_judge"


_SCORE_ROLES = frozenset(
    {JudgeRole.DISPATCH_SCORE_JUDGE, JudgeRole.OBSERVATION_SCORE_JUDGE}
)


class HarnessProfile(BaseModel):
    """受审计的角色 harness：显式排除默认 built-ins，禁用通用 subagent，空 history。

    工具清单本身不是权限控制；真正的隔离由 P4 的 job-scoped evidence reader
    + ArtifactStore.read_verified(ref) 工厂实现。
    """

    model_config = ConfigDict(extra="forbid")

    role: JudgeRole
    agent_id: str
    backend: str
    allowlist_tools: list[str] = []
    excluded_default_tools: list[str] = list(_REQUIRED_EXCLUDED_TOOLS)
    allow_subagent: bool = False
    empty_history: bool = True
    artifact_view: Literal["attempt-root"] = "attempt-root"

    @field_validator("excluded_default_tools")
    @classmethod
    def _must_exclude_default_builtins(cls, value: list[str]) -> list[str]:
        missing = set(_REQUIRED_EXCLUDED_TOOLS) - set(value)
        if missing:
            raise ValueError(f"must exclude default built-ins: {sorted(missing)}")
        return value

    @field_validator("allowlist_tools")
    @classmethod
    def _no_canonical_writers(cls, value: list[str]) -> list[str]:
        banned = _FORBIDDEN_ROLE_TOOLS & set(value)
        if banned:
            raise ValueError(f"role tools must not include canonical writers: {sorted(banned)}")
        return value

    @model_validator(mode="after")
    def _no_generic_subagent_no_history(self) -> "HarnessProfile":
        if self.allow_subagent:
            raise ValueError("general-purpose subagent must be disabled for roles")
        if not self.empty_history:
            raise ValueError("role invocations must start with empty history")
        return self


class RoleHarnessPolicy(BaseModel):
    """§4 / §0.5：默认 allow_shared_model=true（每 role 带 warning），
    只有 require_distinct_role_models 才拒绝相同 model profile。
    """

    model_config = ConfigDict(extra="forbid")

    allow_shared_model: bool = True
    require_distinct_role_models: bool = False

    @model_validator(mode="after")
    def _no_contradiction(self) -> "RoleHarnessPolicy":
        if self.require_distinct_role_models and self.allow_shared_model:
            raise ValueError(
                "require_distinct_role_models contradicts allow_shared_model=true"
            )
        return self

    def shared_model_warning(self, role: JudgeRole) -> str | None:
        if not self.allow_shared_model:
            return None
        return (
            f"allow_shared_model=true: role {role.value} shares a model profile; "
            "role distinctness is contextual, not guaranteed by profile equality"
        )

    def check_roles(self, roles: list[RoleConfig]) -> list[str]:
        """返回 role/policy 违例列表（空 = 通过）。"""
        violations: list[str] = []
        if not self.require_distinct_role_models:
            return violations
        seen: dict[str, str] = {}
        for role in roles:
            if role.model_profile_digest in seen:
                violations.append(
                    f"require_distinct_role_models violated: {seen[role.model_profile_digest]} "
                    f"and {role.role.value} share model_profile_digest "
                    f"{role.model_profile_digest}"
                )
            seen[role.model_profile_digest] = role.role.value
        return violations


# ─────────────────────────────────────────────────────────────────────────────
# 版本化 Draft 与 evidence 引用
# ─────────────────────────────────────────────────────────────────────────────


class ClaimType(str, Enum):
    OBSERVATION = "observation"
    SCORE = "score"
    SUMMARY = "summary"
    RECOMMENDATION = "recommendation"


class EvidenceRef(BaseModel):
    """allowlisted evidence 引用：digest 必须与 ArtifactRef.sha256 一致。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ArtifactRef
    claim_type: ClaimType
    digest: SHA256Hex
    redacted: bool = False

    @model_validator(mode="after")
    def _digest_matches_ref(self) -> "EvidenceRef":
        if self.digest != self.ref.sha256:
            raise ValueError(
                f"evidence digest {self.digest} does not match artifact sha256 {self.ref.sha256}"
            )
        return self


class ScoreDraft(VersionedContract):
    """版本化评分草稿：仅由 score role 返回；validator 是唯一 ScoreResult writer。

    unknown/abstain 必须带 unknown_reason，不得用空对象或 0 分代替。

    维度级 abstain（judge 家族化设计 §2.3）：`dimensions` 可只含 rubric 维度
    子集；每个缺席维度必须在 `dimension_unknown_reasons` 中有显式非空原因。
    `dimensions` 与 `dimension_unknown_reasons` 的键**不得重叠**（同一维度
    不能既有分又有原因）。「并集恰好覆盖 rubric 维度全集」的校验依赖 rubric
    （contracts 看不到），由 runner / workflow validator 层负责。
    整体 abstain 语义保留：空 `dimensions` + `unknown_reason`。
    """

    model_config = ConfigDict(extra="forbid")

    role: JudgeRole
    invocation_id: UUID
    dimensions: dict[str, float] = {}
    dimension_unknown_reasons: dict[str, str] = {}
    evidence: list[EvidenceRef] = []
    model_used: str
    unknown_reason: str | None = None

    @field_validator("role")
    @classmethod
    def _score_role_only(cls, value: JudgeRole) -> JudgeRole:
        if value not in _SCORE_ROLES:
            raise ValueError(f"score draft must be authored by a score role, got {value.value}")
        return value

    @field_validator("model_used")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("model_used must be non-empty")
        return value

    @field_validator("dimensions")
    @classmethod
    def _normalized_scores(cls, value: dict[str, float]) -> dict[str, float]:
        for dimension, score in value.items():
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError(
                    f"dimension {dimension!r} score {score} outside normalized [0, 1]"
                )
        return value

    @field_validator("dimension_unknown_reasons")
    @classmethod
    def _non_empty_unknown_reasons(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        for dimension, reason in value.items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    f"dimension_unknown_reasons[{dimension!r}] must be a non-empty string"
                )
        return value

    @model_validator(mode="after")
    def _dimensions_or_unknown(self) -> "ScoreDraft":
        if not self.dimensions and not self.dimension_unknown_reasons and self.unknown_reason is None:
            raise ValueError(
                "score draft must carry dimensions or an explicit unknown_reason"
            )
        overlap = set(self.dimensions) & set(self.dimension_unknown_reasons)
        if overlap:
            raise ValueError(
                "dimensions and dimension_unknown_reasons must not overlap: "
                f"{sorted(overlap)}"
            )
        return self


class ReportParagraph(BaseModel):
    """报告叙述中的一条 factual claim 段落：文本 + 支撑该 claim 的 evidence refs。

    §4：factual claim 必须带 allowlisted evidence ref；全部引用 redacted 证据
    （无法人工核实原文）是 redaction 违规。
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    evidence: list[EvidenceRef] = []

    @field_validator("text")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("paragraph text must be non-empty")
        return value

    @model_validator(mode="after")
    def _claim_must_have_evidence(self) -> "ReportParagraph":
        if not self.evidence:
            raise ValueError(
                "factual claim paragraph must carry at least one allowlisted evidence ref"
            )
        if not any(not ev.redacted for ev in self.evidence):
            raise ValueError(
                "redaction violation: factual claim cannot rely solely on redacted evidence"
            )
        return self


class ReportNarrativeDraft(VersionedContract):
    """版本化报告叙述草稿：仅由 report_judge 产出；fallback 必须带原因。

    `paragraphs` 是结构化 factual claims（每条必须带 allowlisted evidence ref，
    且至少一条非 redacted 证据）；`narrative` 保留自由叙述文本用于人读。判定为
    fallback 时（`fallback_reason` 非空）不要求 paragraphs。
    """

    model_config = ConfigDict(extra="forbid")

    role: JudgeRole
    invocation_id: UUID
    narrative: str
    paragraphs: list[ReportParagraph] = []
    evidence: list[EvidenceRef] = []
    model_used: str
    fallback_reason: str | None = None

    @field_validator("role")
    @classmethod
    def _report_role_only(cls, value: JudgeRole) -> JudgeRole:
        if value is not JudgeRole.REPORT_JUDGE:
            raise ValueError(
                f"report narrative must be authored by report_judge, got {value.value}"
            )
        return value

    @field_validator("narrative")
    @classmethod
    def _non_empty_narrative(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("narrative must be non-empty")
        return value

    @field_validator("evidence")
    @classmethod
    def _report_evidence_claim_types(
        cls, value: list[EvidenceRef]
    ) -> list[EvidenceRef]:
        allowed = frozenset({ClaimType.SUMMARY, ClaimType.OBSERVATION})
        for ev in value:
            if ev.claim_type not in allowed:
                raise ValueError(
                    f"report narrative evidence claim_type {ev.claim_type.value} "
                    f"is not SUMMARY/OBSERVATION"
                )
        return value


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RecommendationSource(str, Enum):
    RECOMMENDATION_JUDGE = "recommendation_judge"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    evidence: list[EvidenceRef] = []
    failure_refs: list[FailureRef] = []
    severity: Severity = Severity.INFO

    @field_validator("text")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("recommendation text must be non-empty")
        return value

    @field_validator("evidence")
    @classmethod
    def _recommendation_evidence_claim_type(
        cls, value: list[EvidenceRef]
    ) -> list[EvidenceRef]:
        for ev in value:
            if ev.claim_type is not ClaimType.RECOMMENDATION:
                raise ValueError(
                    f"recommendation evidence claim_type {ev.claim_type.value} "
                    f"is not RECOMMENDATION"
                )
        return value


class RecommendationDraft(VersionedContract):
    """版本化建议草稿：judge 产出或 deterministic_fallback；fallback 必带原因。

    judge 产出的每条建议必须携带可人工核实的依据（至少一条 evidence ref 或
    failure ref）；依赖纯 redacted 证据的 basis 是 redaction 违规。
    """

    model_config = ConfigDict(extra="forbid")

    role: JudgeRole
    invocation_id: UUID
    recommendations: list[RecommendationItem] = []
    source: RecommendationSource
    fallback_reason: str | None = None

    @field_validator("role")
    @classmethod
    def _recommendation_role_only(cls, value: JudgeRole) -> JudgeRole:
        if value is not JudgeRole.RECOMMENDATION_JUDGE:
            raise ValueError(
                f"recommendation draft targets recommendation_judge, got {value.value}"
            )
        return value

    @model_validator(mode="after")
    def _fallback_reason(self) -> "RecommendationDraft":
        if (
            self.source is RecommendationSource.DETERMINISTIC_FALLBACK
            and not self.fallback_reason
        ):
            raise ValueError("deterministic_fallback must carry fallback_reason")
        if (
            self.source is RecommendationSource.RECOMMENDATION_JUDGE
            and self.fallback_reason
        ):
            raise ValueError("judge-authored recommendations must not carry fallback_reason")
        return self

    @model_validator(mode="after")
    def _judge_items_must_have_basis(self) -> "RecommendationDraft":
        if self.source is not RecommendationSource.RECOMMENDATION_JUDGE:
            return self
        for item in self.recommendations:
            if not item.evidence and not item.failure_refs:
                raise ValueError(
                    "judge-authored recommendation item must carry an evidence "
                    "or failure basis for human verification"
                )
            if item.evidence and not any(not ev.redacted for ev in item.evidence):
                raise ValueError(
                    "redaction violation: recommendation basis cannot rely solely "
                    "on redacted evidence"
                )
        return self


class UsageSnapshot(BaseModel):
    """模型调用用量。provider 未报告 → None（unknown），禁止伪造 0。"""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    latency_ms: int | None = None
    cost: float | None = None

    @field_validator(
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "latency_ms",
    )
    @classmethod
    def _non_negative_int(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("must be non-negative")
        return value

    @field_validator("cost")
    @classmethod
    def _non_negative_cost(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("cost must be non-negative")
        return value


# ─────────────────────────────────────────────────────────────────────────────
# Sample / ScoreJob / ScoreResult 绑定
# ─────────────────────────────────────────────────────────────────────────────


class Sample(VersionedContract):
    """不是裸 step：dispatch sample 是一个 selected step；observation 是 (agent, step, call)。"""

    model_config = ConfigDict(extra="forbid")

    sample_id: UUID
    target_type: SampleTargetType
    step: int
    agent: str | None = None
    claim_call_ids: list[str] = []
    source_digest: SHA256Hex
    evidence_digest: SHA256Hex
    selection_reason: str
    ordinal: int
    selected_under_overflow: bool = False

    @field_validator("step")
    @classmethod
    def _step_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("sample step must be >= 1")
        return value

    @field_validator("ordinal")
    @classmethod
    def _ordinal_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ordinal must be >= 0")
        return value


class ScoreJobBinding(BaseModel):
    """ScoreJob 的不可变绑定子集；result 必须与 job 完全一致（§5.2）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    target_type: SampleTargetType
    source_digest: SHA256Hex
    evidence_digest: SHA256Hex
    rubric_id: str
    rubric_digest: SHA256Hex
    role: JudgeRole
    prompt_digest: SHA256Hex
    model_profile_digest: SHA256Hex
    tool_schema_digest: SHA256Hex

    def binding_matches(self, other: "ScoreJobBinding") -> list[str]:
        mismatches: list[str] = []
        for field_name in type(self).model_fields:
            left = getattr(self, field_name)
            right = getattr(other, field_name)
            if left != right:
                mismatches.append(f"{field_name}: {left} != {right}")
        return mismatches


class ScoreJob(BaseModel):
    """(Sample, RubricSpec, role profile, prompt/tool schema) 的不可变绑定。"""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_digest: SHA256Hex
    sample_id: UUID
    target_type: SampleTargetType
    source_digest: SHA256Hex
    evidence_digest: SHA256Hex
    rubric_id: str
    rubric_digest: SHA256Hex
    role: JudgeRole
    prompt_digest: SHA256Hex
    model_profile_digest: SHA256Hex
    tool_schema_digest: SHA256Hex
    input_bundle_ref: ArtifactRef
    retry_policy_digest: SHA256Hex
    manifest_digest: SHA256Hex
    status: ScoreJobStatus = ScoreJobStatus.PENDING

    @field_validator("rubric_id")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("rubric_id must be non-empty")
        return value

    def binding(self) -> ScoreJobBinding:
        return ScoreJobBinding(
            job_id=self.job_id,
            target_type=self.target_type,
            source_digest=self.source_digest,
            evidence_digest=self.evidence_digest,
            rubric_id=self.rubric_id,
            rubric_digest=self.rubric_digest,
            role=self.role,
            prompt_digest=self.prompt_digest,
            model_profile_digest=self.model_profile_digest,
            tool_schema_digest=self.tool_schema_digest,
        )


class ScoreResult(BaseModel):
    """一次 job 的验证后结果：状态 terminal；binding 必须与 job 完全一致。"""

    model_config = ConfigDict(extra="forbid")

    binding: ScoreJobBinding
    status: ScoreJobStatus
    dimension_evidence_refs: dict[str, EvidenceRef] = {}
    raw_output_ref: ArtifactRef | None = None
    validated_output_ref: ArtifactRef | None = None
    usage: UsageSnapshot | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    node_attempt: int = 0
    validation_errors: list[str] = []

    @field_validator("status")
    @classmethod
    def _result_terminal_only(cls, value: ScoreJobStatus) -> ScoreJobStatus:
        if value not in SCORE_RESULT_TERMINAL_STATUSES:
            raise ValueError(
                f"{value.value} is not a valid terminal score-result status"
            )
        return value

    @field_validator("retry_count", "node_attempt", "latency_ms")
    @classmethod
    def _non_negative_int(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("must be non-negative")
        return value

    def verify_binding(self, job: ScoreJob) -> list[str]:
        return self.binding.binding_matches(job.binding())


def check_job_result_binding(job: ScoreJob, result: ScoreResult) -> None:
    """逐 job 绑定校验（§5.2-1）；错绑 source/evidence/rubric/prompt/model/tool → 违例。"""
    mismatches = result.verify_binding(job)
    if mismatches:
        raise ContractViolation(
            "score job/result binding mismatch: " + "; ".join(mismatches)
        )
