"""P2 确定性 score merge（设计 §5.2 / §5.3 / §1.2-10,11,12 / §9 P2）。

纯确定性合并，无 LLM、无 I/O、无 workflow：

- 逐 result 验证自身 ScoreJob binding；错绑 source/evidence/rubric/prompt/model/tool
  或重复 result → 整个 merge 记为 FAILED（integrity veto，绝不静默放行）。
- 逐 job 验证 `job_digest`：必须与 `rubric_registry._score_job_payload` /
  `build_score_job` 定义的 canonical digest 完全一致（全部不可变绑定字段，排除
  status/job_digest）。外部构造/篡改导致的 stale/forged digest 也 → FAILED。
- 跨 job 要求相同 source/evidence/target identity 与 manifest digest；不同
  **eligible rubric digest** 是正常的 fan-out，不得误判为冲突。
- 只归一 `succeeded` 且 scale 完整（维度证据 + 归一化分值全覆盖）的结果；
  `unknown` 永不等于 0，按冻结 `unknown_policy`（excluded/partial/block）记录。
  requested 路径的 required 缺失 / failed / timed_out → typed PARTIAL，绝不伪造合成分。
- dispatch / observation 保持独立 group，不产生伪总分；输出 digest 对等价输入稳定。
- deterministic hard violation 永远是 veto，不被任何 LLM 高分抵消。
- `project_not_requested` 是 `--no-llm-judge` 的显式投影：`judge_execution_status=
  not_requested` 且 `merged_scores=None`，不要求任何 job/result（§1.2-12）。

禁止：本模块不得 import / 构造任何 DeepAgent 或 chat 模型。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from sar_orch.eval import contracts as c
from sar_orch.eval.rubric_registry import _score_job_payload

__all__ = [
    "DeterministicViolation",
    "GroupMergeInput",
    "GroupScore",
    "MergeError",
    "MergePolicy",
    "MergedScoreStatus",
    "RubricScore",
    "ScoreMergeOutput",
    "UnknownPolicy",
    "merge_score_groups",
    "project_not_requested",
]


class MergeError(ValueError):
    """merge 配置违例（policy digest 不匹配、重复 target group 等）。"""


class UnknownPolicy(str, Enum):
    """`unknown` 结果的处理策略（§5.2-3）：unknown 永远不等于 0。"""

    EXCLUDED = "excluded"  # unknown 记入 excluded，不计分、不触发 partial
    PARTIAL = "partial"  # unknown → typed PARTIAL
    BLOCK = "block"  # unknown → 该 group 被 block（不产出分）


class MergedScoreStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    VETOED = "vetoed"
    FAILED = "failed"
    NOT_REQUESTED = "not_requested"


class MergePolicy(BaseModel):
    """冻结的合并策略；`digest()` 是其 immutable identity。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = c.SUPPORTED_SCHEMA_VERSION
    unknown_policy: UnknownPolicy = UnknownPolicy.EXCLUDED
    veto_on_deterministic_violation: bool = True

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        if value != c.SUPPORTED_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {value!r}")
        return value

    def digest(self) -> str:
        return c.sha256_hex(
            c.canonical_json(self.model_dump(mode="json")).encode("utf-8")
        )


class DeterministicViolation(BaseModel):
    """确定性 hard violation（来自 deterministic graders 的红线）。

    target_type 为 None 表示影响全部 group（§1.2-11 的硬性 veto 语义）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    reason: str
    target_type: c.SampleTargetType | None = None

    @field_validator("kind", "reason")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


# ─────────────────────────────────────────────────────────────────────────────
# 输出模型（stable canonical）
# ─────────────────────────────────────────────────────────────────────────────


class RubricScore(BaseModel):
    """单个 rubric digest 的聚合结果：分数、权重、纳入/排除、input refs。"""

    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    rubric_digest: c.SHA256Hex
    target_type: c.SampleTargetType
    dimensions: list[str]
    weight: float
    scored: int
    excluded: int
    score: float | None = None
    dimension_scores: dict[str, float | None] = {}
    per_job_scores: dict[str, dict[str, float]] = {}
    included_job_ids: list[str] = []
    excluded_job_ids: list[str] = []
    excluded_reasons: dict[str, str] = {}
    input_refs: list[c.ArtifactRef] = []


class GroupScore(BaseModel):
    """一个 target type group 的合并结果；dispatch/observation 各自独立。"""

    model_config = ConfigDict(extra="forbid")

    target_type: c.SampleTargetType
    status: MergedScoreStatus
    score: float | None = None
    rubrics: list[RubricScore] = []
    reasons: list[str] = []
    disagreement: dict[str, list[float]] = {}
    input_refs: list[c.ArtifactRef] = []


class ScoreMergeOutput(BaseModel):
    """确定性 merge 的唯一输出；`digest()` 对等价输入稳定（§5.2-7）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = c.SUPPORTED_SCHEMA_VERSION
    status: MergedScoreStatus
    judge_execution_status: c.JudgeExecutionStatus
    merged_scores: dict[str, GroupScore] | None = None
    veto_reasons: list[str] = []
    violations: list[str] = []
    policy_digest: c.SHA256Hex

    def digest(self) -> str:
        return c.sha256_hex(
            c.canonical_json(self.model_dump(mode="json")).encode("utf-8")
        )


@dataclass(frozen=True)
class GroupMergeInput:
    """一个 target type group 的全部输入。

    `score_values` 是 validator 归一化后的逐 job 维度分值（job_id → {dim: [0,1]}）；
    `ScoreResult` 本身只携带维度 evidence refs（§5.1），数值由此映射提供。
    `rubrics` 覆盖该 group 出现的全部 (rubric_id, digest)。
    """

    target_type: c.SampleTargetType
    jobs: tuple[c.ScoreJob, ...]
    results: tuple[c.ScoreResult, ...]
    score_values: Mapping[UUID, Mapping[str, float]]
    rubrics: tuple[c.RubricSpec, ...]


# ─────────────────────────────────────────────────────────────────────────────
# 实现
# ─────────────────────────────────────────────────────────────────────────────

_STATLUS_RANK = {
    MergedScoreStatus.OK: 0,
    MergedScoreStatus.PARTIAL: 1,
    MergedScoreStatus.BLOCKED: 2,
    MergedScoreStatus.VETOED: 3,
    MergedScoreStatus.FAILED: 4,
    MergedScoreStatus.NOT_REQUESTED: -1,
}


def project_not_requested(*, policy_digest: str) -> ScoreMergeOutput:
    """`--no-llm-judge` 的显式投影：不创建 job、不运行 merge（§1.2-12）。

    `merged_scores=None`，`judge_execution_status=not_requested`；不要求任何
    job/result。`policy_digest` 是冻结 input manifest 里的 merge policy digest。
    """
    return ScoreMergeOutput(
        schema_version=c.SUPPORTED_SCHEMA_VERSION,
        status=MergedScoreStatus.NOT_REQUESTED,
        judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
        merged_scores=None,
        policy_digest=policy_digest,
    )


def _failed_output(policy_digest: str, reasons: list[str]) -> ScoreMergeOutput:
    return ScoreMergeOutput(
        schema_version=c.SUPPORTED_SCHEMA_VERSION,
        status=MergedScoreStatus.FAILED,
        judge_execution_status=c.JudgeExecutionStatus.REQUESTED,
        merged_scores=None,
        violations=reasons,
        policy_digest=policy_digest,
    )


def _sorted_refs(refs: Sequence[c.ArtifactRef]) -> list[c.ArtifactRef]:
    deduped: dict[tuple[str, str], c.ArtifactRef] = {}
    for ref in refs:
        deduped.setdefault((ref.path, ref.sha256), ref)
    return [deduped[key] for key in sorted(deduped, key=lambda k: (k[0], k[1]))]


def _round(value: float) -> float:
    return round(value, 6)


def _validate_job_digest(job: c.ScoreJob) -> None:
    """逐 job 完整性校验：`job_digest` 必须是 canonical 不可变绑定的 digest。

    与 `rubric_registry._score_job_payload` / `build_score_job` 使用同一来源
    （§5.2-1 / §3.5）：对除 `status` 与 `job_digest` 外的全部不可变字段做
    canonical SHA-256。外部构造或事后篡改（stale/forged job_digest）一律
    `MergeError`，由 `merge_score_groups` 转成 typed FAILED 输出，绝不进入计分。
    """
    expected = c.sha256_hex(c.canonical_json(_score_job_payload(job)).encode("utf-8"))
    if job.job_digest != expected:
        raise MergeError(
            f"job_digest mismatch for job {job.job_id}: expected {expected}, "
            f"got {job.job_digest}"
        )


def _score_rubric(
    rspec: c.RubricSpec,
    rjobs: list[c.ScoreJob],
    results: Sequence[c.ScoreResult],
    score_values: Mapping[UUID, Mapping[str, float]],
    policy: MergePolicy,
) -> tuple[RubricScore, bool, bool, list[str]]:
    """返回 (RubricScore, partial, blocked, reasons)。"""
    required_dims = set(rspec.dimensions)
    rjob_ids = {j.job_id for j in rjobs}
    results_by_job = {
        r.binding.job_id: r for r in results if r.binding.job_id in rjob_ids
    }

    included: list[str] = []
    excluded: list[str] = []
    excluded_reasons: dict[str, str] = {}
    per_job_scores: dict[str, dict[str, float]] = {}
    reasons: list[str] = []
    partial = False
    blocked = False

    for job in rjobs:
        jid = job.job_id
        result = results_by_job.get(jid)
        if result is None:
            excluded.append(str(jid))
            excluded_reasons[str(jid)] = "missing_result"
            reasons.append(f"job {jid}: required result missing")
            partial = True
            continue

        status = result.status
        if status is c.ScoreJobStatus.SUCCEEDED:
            evidence_dims = set(result.dimension_evidence_refs.keys())
            if not required_dims <= evidence_dims:
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "incomplete_evidence_scale"
                reasons.append(
                    f"job {jid}: succeeded but dimension evidence incomplete "
                    f"(missing {sorted(required_dims - evidence_dims)})"
                )
                partial = True
                continue
            vals = score_values.get(jid)
            if vals is None:
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "missing_score_values"
                reasons.append(f"job {jid}: succeeded but no validated scores")
                partial = True
                continue
            missing = required_dims - set(vals.keys())
            if missing:
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "incomplete_dimensions"
                reasons.append(
                    f"job {jid}: succeeded but score values missing {sorted(missing)}"
                )
                partial = True
                continue
            try:
                normalized = {dim: float(vals[dim]) for dim in rspec.dimensions}
            except (TypeError, ValueError):
                normalized = {}
            if any(
                not math.isfinite(v) or not (0.0 <= v <= 1.0)
                for v in normalized.values()
            ):
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "out_of_scale_value"
                reasons.append(f"job {jid}: score value outside normalized [0,1]")
                partial = True
                continue
            included.append(str(jid))
            per_job_scores[str(jid)] = {
                dim: normalized[dim] for dim in rspec.dimensions
            }
        elif status is c.ScoreJobStatus.UNKNOWN:
            # unknown 永不等于 0：绝不进入分子/分母（§5.2-3）。
            if policy.unknown_policy is UnknownPolicy.EXCLUDED:
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "unknown_excluded"
            elif policy.unknown_policy is UnknownPolicy.PARTIAL:
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "unknown_partial"
                reasons.append(f"job {jid}: unknown under unknown_policy=partial")
                partial = True
            else:  # BLOCK
                excluded.append(str(jid))
                excluded_reasons[str(jid)] = "unknown_block"
                reasons.append(f"job {jid}: unknown under unknown_policy=block")
                blocked = True
        elif status is c.ScoreJobStatus.LATE_IGNORED:
            excluded.append(str(jid))
            excluded_reasons[str(jid)] = "late_ignored"
        else:
            # FAILED / TIMED_OUT / CANCELLED / NOT_RUN_BUDGET：required 缺失。
            excluded.append(str(jid))
            excluded_reasons[str(jid)] = status.value
            reasons.append(f"job {jid}: status={status.value}")
            partial = True

    included_sorted = sorted(included)
    dimension_scores: dict[str, float | None] = {}
    for dim in rspec.dimensions:
        vals = [per_job_scores[jid][dim] for jid in included_sorted]
        dimension_scores[dim] = _round(sum(vals) / len(vals)) if vals else None
    scored_dims = [v for v in dimension_scores.values() if v is not None]
    score = _round(sum(scored_dims) / len(scored_dims)) if scored_dims else None

    input_refs = _sorted_refs(
        [
            r.validated_output_ref
            for r in results
            if r.binding.job_id in rjob_ids
            and r.validated_output_ref is not None
            and r.status is c.ScoreJobStatus.SUCCEEDED
        ]
    )

    rubric_score = RubricScore(
        rubric_id=rspec.rubric_id,
        rubric_digest=rspec.digest,
        target_type=rspec.target_type,
        dimensions=list(rspec.dimensions),
        weight=rspec.weight,
        scored=len(included_sorted),
        excluded=len(excluded),
        score=score,
        dimension_scores=dimension_scores,
        per_job_scores={
            jid: {dim: per_job_scores[jid][dim] for dim in rspec.dimensions}
            for jid in included_sorted
        },
        included_job_ids=included_sorted,
        excluded_job_ids=sorted(excluded),
        excluded_reasons=excluded_reasons,
        input_refs=input_refs,
    )
    return rubric_score, partial, blocked, reasons


def _score_group(
    group: GroupMergeInput,
    policy: MergePolicy,
    manifest_digest: str,
) -> GroupScore:
    jobs = group.jobs
    if not jobs:
        return GroupScore(
            target_type=group.target_type,
            status=MergedScoreStatus.OK,
            score=None,
            rubrics=[],
            reasons=["no score jobs in group"],
            disagreement={},
            input_refs=[],
        )

    # ── 逐 job 完整性：canonical job_digest 校验（§5.2-1 / §3.5） ───────────────
    for job in jobs:
        _validate_job_digest(job)

    # ── 跨 job source/evidence/target identity / policy 一致性（§5.2-2） ────────
    if any(j.target_type is not group.target_type for j in jobs):
        raise MergeError(
            f"job target_type differs from group target_type {group.target_type.value}"
        )
    if len({j.source_digest for j in jobs}) != 1:
        raise MergeError("cross-job source_digest mismatch within group")
    if len({j.evidence_digest for j in jobs}) != 1:
        raise MergeError("cross-job evidence_digest mismatch within group")
    if len({j.manifest_digest for j in jobs}) != 1:
        raise MergeError("cross-job manifest_digest mismatch within group")
    if jobs[0].manifest_digest != manifest_digest:
        raise MergeError("job manifest_digest does not match the merge manifest_digest")

    rubric_index: dict[tuple[str, str], c.RubricSpec] = {
        (r.rubric_id, r.digest): r for r in group.rubrics
    }
    for job in jobs:
        if (job.rubric_id, job.rubric_digest) not in rubric_index:
            raise MergeError(
                f"rubric {job.rubric_id}/{job.rubric_digest[:8]} is not supplied "
                f"for the {group.target_type.value} merge"
            )

    # ── 逐 result 绑定校验（§5.2-1 / §3.5 duplicate → fail） ───────────────────
    jobs_by_id = {j.job_id: j for j in jobs}
    result_counts: dict[UUID, int] = {}
    for result in group.results:
        job = jobs_by_id.get(result.binding.job_id)
        if job is None:
            raise MergeError(
                f"result references job {result.binding.job_id} not in group"
            )
        if result.binding.target_type is not group.target_type:
            raise MergeError(
                f"result target_type mismatch for job {result.binding.job_id}"
            )
        try:
            c.check_job_result_binding(job, result)
        except c.ContractViolation as exc:
            raise MergeError(
                f"job/result binding mismatch for job {job.job_id}: {exc}"
            ) from exc
        result_counts[result.binding.job_id] = (
            result_counts.get(result.binding.job_id, 0) + 1
        )
    duplicates = [str(jid) for jid, n in result_counts.items() if n > 1]
    if duplicates:
        raise MergeError(
            f"duplicate result for job(s): {', '.join(sorted(duplicates))}"
        )

    # ── 按 (rubric_id, digest) 分组；不同 eligible rubric digest 是正常 fan-out ──
    rubric_groups: dict[tuple[str, str], list[c.ScoreJob]] = {}
    for job in jobs:
        rubric_groups.setdefault((job.rubric_id, job.rubric_digest), []).append(job)

    rubric_scores: list[RubricScore] = []
    group_reasons: list[str] = []
    partial = False
    blocked = False
    for (rid, rdig), rjobs in rubric_groups.items():
        rspec = rubric_index[(rid, rdig)]
        rs, rs_partial, rs_blocked, rs_reasons = _score_rubric(
            rspec, rjobs, group.results, group.score_values, policy
        )
        rubric_scores.append(rs)
        group_reasons.extend(rs_reasons)
        partial = partial or rs_partial
        blocked = blocked or rs_blocked

    # rubric 排序保证输出对输入顺序稳定。
    rubric_scores.sort(key=lambda rs: (rs.rubric_id, rs.rubric_digest))

    scored = [rs for rs in rubric_scores if rs.score is not None]
    total_weight = sum(rs.weight for rs in scored)
    if scored and total_weight > 0:
        assert all(rs.score is not None for rs in scored)
        group_score = _round(
            sum(rs.score * rs.weight for rs in scored) / total_weight  # type: ignore[union-attr]
        )
    elif scored and total_weight == 0:
        group_score = None
        group_reasons.append("all rubric weights are zero; group score undefined")
    else:
        group_score = None

    if blocked:
        status = MergedScoreStatus.BLOCKED
        group_score = None
        group_reasons.append(
            "group blocked by unknown_policy=block; merged score suppressed"
        )
    elif partial:
        status = MergedScoreStatus.PARTIAL
    else:
        status = MergedScoreStatus.OK

    # disagreement：跨 rubric 的逐维度分值（多个 eligible digest 时可见）。
    all_dims: list[str] = []
    seen_dims: set[str] = set()
    for rs in rubric_scores:
        for dim in rs.dimensions:
            if dim not in seen_dims:
                all_dims.append(dim)
                seen_dims.add(dim)
    disagreement: dict[str, list[float]] = {}
    for dim in sorted(all_dims):
        vals: list[float] = []
        for rs in rubric_scores:
            dim_score = rs.dimension_scores.get(dim)
            if dim_score is not None:
                vals.append(dim_score)
        if vals:
            disagreement[dim] = vals

    input_refs = _sorted_refs([r for rs in rubric_scores for r in rs.input_refs])

    return GroupScore(
        target_type=group.target_type,
        status=status,
        score=group_score,
        rubrics=rubric_scores,
        reasons=group_reasons,
        disagreement=disagreement,
        input_refs=input_refs,
    )


def merge_score_groups(
    groups: Sequence[GroupMergeInput],
    *,
    policy: MergePolicy,
    merge_policy_digest: str,
    manifest_digest: str,
    deterministic_violations: Sequence[DeterministicViolation] = (),
) -> ScoreMergeOutput:
    """纯确定性合并。绑定/一致性违例返回 FAILED 输出；配置违例抛 `MergeError`。"""
    if merge_policy_digest != policy.digest():
        raise MergeError("merge_policy_digest does not match the MergePolicy digest")
    groups = tuple(groups)
    seen_types: set[c.SampleTargetType] = set()
    for g in groups:
        if g.target_type in seen_types:
            raise MergeError(f"duplicate group target_type {g.target_type.value}")
        seen_types.add(g.target_type)

    violations = [f"{v.kind}: {v.reason}" for v in deterministic_violations]
    veto_active = policy.veto_on_deterministic_violation and bool(
        deterministic_violations
    )

    processed: list[GroupScore] = []
    for g in groups:
        try:
            gs = _score_group(g, policy, manifest_digest)
        except MergeError as exc:
            return _failed_output(
                merge_policy_digest, [f"{g.target_type.value}: {exc}"]
            )
        if veto_active:
            gs = gs.model_copy(
                update={
                    "status": MergedScoreStatus.VETOED,
                    "score": None,
                    "reasons": [
                        *gs.reasons,
                        f"deterministic veto: {violations[0]}",
                    ],
                }
            )
        processed.append(gs)

    if veto_active:
        return ScoreMergeOutput(
            schema_version=c.SUPPORTED_SCHEMA_VERSION,
            status=MergedScoreStatus.VETOED,
            judge_execution_status=c.JudgeExecutionStatus.REQUESTED,
            merged_scores={g.target_type.value: g for g in processed},
            veto_reasons=violations,
            violations=[v.kind for v in deterministic_violations],
            policy_digest=merge_policy_digest,
        )

    worst = max((g.status for g in processed), key=lambda s: _STATLUS_RANK[s])
    return ScoreMergeOutput(
        schema_version=c.SUPPORTED_SCHEMA_VERSION,
        status=worst,
        judge_execution_status=c.JudgeExecutionStatus.REQUESTED,
        merged_scores={g.target_type.value: g for g in processed},
        policy_digest=merge_policy_digest,
    )
