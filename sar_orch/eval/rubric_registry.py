"""P2 rubric 注册表、Sample 选择与 ScoreJob 绑定（设计 §5.1 / §5.3 / §9 P2）。

本模块是纯确定性逻辑，不含 I/O（除读取 rubric YAML 源与 prompt 模板源）、
workflow、LLM 构造与 artifact 写入：

- `load_rubric_text` / `load_rubric_spec`：加载并校验**版本化 YAML** rubric，
  `digest` 是对 YAML **原始字节**的 SHA-256（exact source digest）；为
  `RubricSpec.prompt_template_ref` 物化 prompt 模板 ArtifactRef。
- `RubricRegistry`：目录加载、按 id 查询、按 target_type 取默认 dispatch/observation。
- Sample 构建：`build_dispatch_samples` / `build_observation_samples` 完全保留
  legacy `select_judge_steps` 的确定性选择语义（失败步优先、端点必选、stride 填充、
  失败步可超过 target）；每个 Sample 携带 source/evidence digest、sample_id、
  selection_reason、ordinal 与 `selected_under_overflow`。
  observation Sample 的 identity 是 `(agent, step, call)`。
- `build_score_job`：把 (Sample, RubricSpec, RoleConfig, input bundle, manifest,
  retry policy) 绑定成不可变 `ScoreJob`；target type / judge role 不匹配即拒绝。

禁止：本模块不得 import / 构造任何 DeepAgent 或 chat 模型。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from sar_orch.eval import contracts as c

__all__ = [
    "DEFAULT_DISPATCH_RUBRIC_ID",
    "DEFAULT_JUDGE_SAMPLE_STEPS",
    "DEFAULT_OBSERVATION_RUBRIC_ID",
    "DEFAULT_RUBRICS_DIR",
    "LoadedRubric",
    "RubricError",
    "RubricRegistry",
    "RubricSourceSpec",
    "SamplePlan",
    "StepSelection",
    "build_dispatch_samples",
    "build_observation_samples",
    "build_sample_plan",
    "build_score_job",
    "build_score_jobs",
    "default_registry",
    "load_rubric_spec",
    "load_rubric_text",
    "selection_policy_digest",
]

# 与 CLI 的 `--judge-sample-steps` 默认一致（设计 §7.1 / AGENTS.md）。
DEFAULT_JUDGE_SAMPLE_STEPS = 20

DEFAULT_RUBRICS_DIR = Path(__file__).parent / "rubrics"
_AGENT_PROMPTS_DIR = Path(__file__).parent / "agent" / "prompts"

DEFAULT_DISPATCH_RUBRIC_ID = "dispatch-v1"
DEFAULT_OBSERVATION_RUBRIC_ID = "observation-v1"

_SELECTION_POLICY_NAME = "attempt-stream-v1/select_judge_steps"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class RubricError(ValueError):
    """rubric 加载 / 校验 / 绑定违例。"""


def _validate_sha(value: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise RubricError(f"expected 64 lowercase hex digest, got {value!r}")
    return value


def _media_type_for(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "md": "text/markdown",
        "txt": "text/plain",
        "yaml": "application/yaml",
        "json": "application/json",
    }.get(suffix, "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# YAML 源 schema（与 contracts.RubricSpec 一一对应，但 prompt_template 是源路径）
# ─────────────────────────────────────────────────────────────────────────────


class RubricSourceSpec(BaseModel):
    """rubric YAML 的受控源 schema；未知字段一律拒绝（extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    version: str
    target_type: c.SampleTargetType
    input_selector: str
    dimensions: list[str]
    judge_role: c.JudgeRole
    merge_group: str
    weight: float
    calibration_status: c.CalibrationStatus = c.CalibrationStatus.UNCALIBRATED
    prompt_template: str

    @field_validator(
        "rubric_id", "version", "input_selector", "merge_group", "prompt_template"
    )
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("dimensions")
    @classmethod
    def _non_empty_dimensions(cls, value: list[str]) -> list[str]:
        if not value or any(not d or not d.strip() for d in value):
            raise ValueError("dimensions must be a non-empty list of names")
        return value

    @field_validator("weight")
    @classmethod
    def _finite_weight(cls, value: float) -> float:
        if value < 0 or not math.isfinite(value):
            raise ValueError("weight must be a finite non-negative number")
        return value

    @model_validator(mode="after")
    def _target_role_consistent(self) -> RubricSourceSpec:
        if (
            self.target_type is c.SampleTargetType.DISPATCH
            and self.judge_role is not c.JudgeRole.DISPATCH_SCORE_JUDGE
        ):
            raise ValueError(
                f"dispatch rubric must use dispatch_score_judge, got {self.judge_role.value}"
            )
        if (
            self.target_type is c.SampleTargetType.OBSERVATION
            and self.judge_role is not c.JudgeRole.OBSERVATION_SCORE_JUDGE
        ):
            raise ValueError(
                f"observation rubric must use observation_score_judge, "
                f"got {self.judge_role.value}"
            )
        return self


@dataclass(frozen=True)
class LoadedRubric:
    """加载后的 rubric：精确源字节 / 源 digest + 物化后的 `RubricSpec`。"""

    source_path: str
    source_digest: c.SHA256Hex
    source_bytes: bytes
    spec: c.RubricSpec


def _default_prompt_bytes(prompt_template: str) -> bytes:
    target = _AGENT_PROMPTS_DIR / prompt_template
    if not target.is_file():
        raise RubricError(f"prompt template {prompt_template!r} not found at {target}")
    return target.read_bytes()


def load_rubric_text(
    text: str,
    *,
    source_path: str = "inline.yaml",
    source_bytes: bytes | None = None,
    prompt_resolver: Callable[[str], bytes] | None = None,
    prompt_snapshot_rel: str | None = None,
) -> LoadedRubric:
    """从 YAML 文本加载并校验 rubric，返回不可变 `LoadedRubric`。

    `digest` 覆盖 `source_bytes`（默认 `text.encode("utf-8")`），即 exact source
    digest —— 对原始文件字节的 SHA-256，不是重排后的 re-serialize。
    `prompt_resolver` 解析 `prompt_template` 指向的源文件字节；缺省从
    `sar_orch/eval/agent/prompts/` 读取。
    """
    raw = source_bytes if source_bytes is not None else text.encode("utf-8")
    digest = c.sha256_hex(raw)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RubricError(f"rubric YAML parse failed for {source_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RubricError(
            f"rubric YAML root must be a mapping, got {type(data).__name__}"
        )
    try:
        src = RubricSourceSpec.model_validate(data)
    except ValueError as exc:
        raise RubricError(f"invalid rubric spec ({source_path}): {exc}") from exc

    if prompt_resolver is not None:
        try:
            prompt_bytes = prompt_resolver(src.prompt_template)
        except OSError as exc:
            raise RubricError(
                f"prompt template {src.prompt_template!r} not found or unreadable: "
                f"{exc}"
            ) from exc
    else:
        prompt_bytes = _default_prompt_bytes(src.prompt_template)

    prompt_ref = c.ArtifactRef(
        path=prompt_snapshot_rel or f"snapshots/prompts/{src.rubric_id}.md",
        sha256=c.sha256_hex(prompt_bytes),
        bytes=len(prompt_bytes),
        media_type=_media_type_for(src.prompt_template),
        producer="rubric-registry",
    )
    spec = c.RubricSpec(
        rubric_id=src.rubric_id,
        version=src.version,
        digest=digest,
        target_type=src.target_type,
        input_selector=src.input_selector,
        dimensions=list(src.dimensions),
        prompt_template_ref=prompt_ref,
        judge_role=src.judge_role,
        merge_group=src.merge_group,
        weight=src.weight,
        calibration_status=src.calibration_status,
    )
    return LoadedRubric(
        source_path=source_path,
        source_digest=digest,
        source_bytes=raw,
        spec=spec,
    )


def load_rubric_spec(
    path: str | Path,
    *,
    prompt_resolver: Callable[[str], bytes] | None = None,
    prompt_snapshot_rel: str | None = None,
) -> LoadedRubric:
    """从文件加载 rubric；digest 覆盖文件**原始字节**（exact source digest）。"""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RubricError(f"cannot read rubric spec {path}: {exc}") from exc
    text = raw.decode("utf-8")
    return load_rubric_text(
        text,
        source_path=str(path),
        source_bytes=raw,
        prompt_resolver=prompt_resolver,
        prompt_snapshot_rel=prompt_snapshot_rel,
    )


class RubricRegistry:
    """版本化 rubric 注册表：按 id 查询、按 target_type 取默认、稳定注册表 digest。"""

    def __init__(self, loaded: Sequence[LoadedRubric]):
        by_id: dict[str, LoadedRubric] = {}
        for item in loaded:
            if item.spec.rubric_id in by_id:
                raise RubricError(
                    f"duplicate rubric_id {item.spec.rubric_id!r} in registry"
                )
            by_id[item.spec.rubric_id] = item
        self._by_id = by_id
        self._ordered: tuple[LoadedRubric, ...] = tuple(
            sorted(by_id.values(), key=lambda r: r.spec.rubric_id)
        )

    @classmethod
    def load_dir(cls, directory: str | Path) -> RubricRegistry:
        directory = Path(directory)
        files = sorted(list(directory.glob("*.yaml")) + list(directory.glob("*.yml")))
        loaded = [load_rubric_spec(f) for f in files]
        return cls(loaded)

    @property
    def items(self) -> tuple[LoadedRubric, ...]:
        return self._ordered

    def get(self, rubric_id: str) -> LoadedRubric:
        try:
            return self._by_id[rubric_id]
        except KeyError:
            raise RubricError(f"unknown rubric_id {rubric_id!r}") from None

    def contains(self, rubric_id: str) -> bool:
        return rubric_id in self._by_id

    def by_target_type(self, target_type: c.SampleTargetType) -> list[LoadedRubric]:
        return [r for r in self._ordered if r.spec.target_type is target_type]

    @staticmethod
    def default_rubric_id(target_type: c.SampleTargetType) -> str:
        if target_type is c.SampleTargetType.DISPATCH:
            return DEFAULT_DISPATCH_RUBRIC_ID
        return DEFAULT_OBSERVATION_RUBRIC_ID

    def default_rubric(self, target_type: c.SampleTargetType) -> LoadedRubric:
        return self.get(self.default_rubric_id(target_type))

    def digest(self) -> str:
        """注册表 digest：对 (rubric_id, source_digest) 的稳定 canonical。"""
        return c.sha256_hex(
            c.canonical_json(
                {
                    "rubrics": [
                        {"rubric_id": r.spec.rubric_id, "digest": r.source_digest}
                        for r in self._ordered
                    ]
                }
            ).encode("utf-8")
        )


def default_registry() -> RubricRegistry:
    """加载仓库内置的 dispatch-v1 / observation-v1 冻结源。"""
    return RubricRegistry.load_dir(DEFAULT_RUBRICS_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Sample 选择：保留 legacy select_judge_steps 的确定性语义
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepSelection:
    """单个被选 step 及其理由 / overflow 标记。"""

    step: int
    reason: str
    under_overflow: bool


def _select_steps_with_reasons(episode: Any, target: int) -> list[StepSelection]:
    """逐字保留 `sar_orch/eval/agent/eval_agent.py:select_judge_steps` 的选择规则。

    优先级：1) 含失败交互的步（信号所在，优先全保留）2) 首末两步 3) 剩余步的
    固定 stride 均匀覆盖。失败步可超过 target（overflow）。episode 是鸭子类型：
    只需要 `steps`（step → 含 `interactions` 的 StepRecord，interaction 有
    `succeeded`）—— 与 legacy 完全一致，保证复用同一确定性语义。
    """
    all_steps = sorted(episode.steps.keys())
    if not all_steps or target <= 0:
        return []

    failed = {
        s
        for s in all_steps
        if any(ai.succeeded is False for ai in episode.steps[s].interactions)
    }
    chosen = set(failed) | {all_steps[0], all_steps[-1]}

    remaining = [s for s in all_steps if s not in chosen]
    room = target - len(chosen)
    if room > 0 and remaining:
        if room >= len(remaining):
            chosen.update(remaining)
        else:
            last = len(remaining) - 1
            chosen.update(
                remaining[round(i * last / (room - 1))] if room > 1 else remaining[0]
                for i in range(room)
            )

    ordered = sorted(chosen)
    overflow_set = set(ordered[target:]) if len(ordered) > target else set()
    first, last = all_steps[0], all_steps[-1]

    out: list[StepSelection] = []
    for s in ordered:
        if s in failed:
            reason = "failed_interaction"
        elif s == first:
            reason = "first_step"
        elif s == last:
            reason = "last_step"
        else:
            reason = "fixed_stride"
        out.append(
            StepSelection(step=s, reason=reason, under_overflow=s in overflow_set)
        )
    return out


def selection_policy_digest(target: int, target_type: c.SampleTargetType) -> str:
    """采样策略的稳定 digest：等价输入（同 target/type）必同 digest。"""
    return c.sha256_hex(
        c.canonical_json(
            {
                "selection_policy": _SELECTION_POLICY_NAME,
                "target": target,
                "target_type": target_type.value,
            }
        ).encode("utf-8")
    )


@dataclass(frozen=True)
class SamplePlan:
    """采样计划：target / selected / overflow / policy digest（§5.1）。"""

    target_type: c.SampleTargetType
    target: int
    selected: tuple[c.Sample, ...]
    overflow: bool
    policy_digest: c.SHA256Hex
    # 记录选择出的 step 是否发生了 overflow（observation 派生自 step 选择）。
    step_overflow: bool = False

    def digest(self) -> str:
        """内容 digest：覆盖选择结果（不含随机 sample_id），等价计划必同 digest。"""
        payload = {
            "target_type": self.target_type.value,
            "target": self.target,
            "overflow": self.overflow,
            "policy_digest": self.policy_digest,
            "samples": [
                {
                    "target_type": s.target_type.value,
                    "step": s.step,
                    "agent": s.agent,
                    "claim_call_ids": s.claim_call_ids,
                    "source_digest": s.source_digest,
                    "evidence_digest": s.evidence_digest,
                    "selection_reason": s.selection_reason,
                    "ordinal": s.ordinal,
                    "selected_under_overflow": s.selected_under_overflow,
                }
                for s in self.selected
            ],
        }
        return c.sha256_hex(c.canonical_json(payload).encode("utf-8"))


def _make_sample(
    *,
    target_type: c.SampleTargetType,
    step: int,
    agent: str | None,
    claim_call_ids: list[str],
    source_digest: str,
    evidence_digest: str,
    selection_reason: str,
    ordinal: int,
    under_overflow: bool,
) -> c.Sample:
    return c.Sample(
        sample_id=uuid4(),
        target_type=target_type,
        step=step,
        agent=agent,
        claim_call_ids=list(claim_call_ids),
        source_digest=source_digest,
        evidence_digest=evidence_digest,
        selection_reason=selection_reason,
        ordinal=ordinal,
        selected_under_overflow=under_overflow,
    )


def build_dispatch_samples(
    episode: Any,
    *,
    target: int = DEFAULT_JUDGE_SAMPLE_STEPS,
    source_digest: str,
    evidence_digest: str,
) -> SamplePlan:
    """为 dispatch 构建 Sample：每个被选 step 一个 sample（§5.1）。"""
    _validate_sha(source_digest)
    _validate_sha(evidence_digest)
    selections = _select_steps_with_reasons(episode, target)
    samples: list[c.Sample] = []
    for idx, sel in enumerate(selections):
        samples.append(
            _make_sample(
                target_type=c.SampleTargetType.DISPATCH,
                step=sel.step,
                agent=None,
                claim_call_ids=[],
                source_digest=source_digest,
                evidence_digest=evidence_digest,
                selection_reason=sel.reason,
                ordinal=idx,
                under_overflow=sel.under_overflow,
            )
        )
    return SamplePlan(
        target_type=c.SampleTargetType.DISPATCH,
        target=target,
        selected=tuple(samples),
        overflow=len(selections) > target,
        policy_digest=selection_policy_digest(target, c.SampleTargetType.DISPATCH),
        step_overflow=len(selections) > target,
    )


def build_observation_samples(
    episode: Any,
    *,
    target: int = DEFAULT_JUDGE_SAMPLE_STEPS,
    source_digest: str,
    evidence_digest: str,
) -> SamplePlan:
    """为 observation 构建 Sample：identity = `(agent, step, report_observation call)`。

    step 选择与 dispatch 共用同一确定性规则；每个被选 step 内，每个 agent 的每条
    `report_observation` 调用各成一个 sample，`claim_call_ids` 携带调用身份。
    """
    _validate_sha(source_digest)
    _validate_sha(evidence_digest)
    selections = _select_steps_with_reasons(episode, target)
    samples: list[c.Sample] = []
    ordinal = 0
    for sel in selections:
        sr = episode.steps[sel.step]
        call_seen: dict[str, int] = {}
        for ai in sr.interactions:
            if ai.tool_name != "report_observation":
                continue
            key = ai.agent
            call_index = call_seen.get(key, 0)
            call_seen[key] = call_index + 1
            call_id = f"{sel.step}:{ai.agent}:{call_index}"
            samples.append(
                _make_sample(
                    target_type=c.SampleTargetType.OBSERVATION,
                    step=sel.step,
                    agent=ai.agent,
                    claim_call_ids=[call_id],
                    source_digest=source_digest,
                    evidence_digest=evidence_digest,
                    selection_reason=f"{sel.reason};observation_call",
                    ordinal=ordinal,
                    under_overflow=sel.under_overflow,
                )
            )
            ordinal += 1
    return SamplePlan(
        target_type=c.SampleTargetType.OBSERVATION,
        target=target,
        selected=tuple(samples),
        overflow=len(selections) > target,
        policy_digest=selection_policy_digest(target, c.SampleTargetType.OBSERVATION),
        step_overflow=len(selections) > target,
    )


def build_sample_plan(
    target_type: c.SampleTargetType,
    episode: Any,
    *,
    target: int = DEFAULT_JUDGE_SAMPLE_STEPS,
    source_digest: str,
    evidence_digest: str,
) -> SamplePlan:
    if target_type is c.SampleTargetType.DISPATCH:
        return build_dispatch_samples(
            episode,
            target=target,
            source_digest=source_digest,
            evidence_digest=evidence_digest,
        )
    if target_type is c.SampleTargetType.OBSERVATION:
        return build_observation_samples(
            episode,
            target=target,
            source_digest=source_digest,
            evidence_digest=evidence_digest,
        )
    raise RubricError(f"unsupported target_type {target_type.value!r}")


# ─────────────────────────────────────────────────────────────────────────────
# ScoreJob 绑定（§5.1 / §5.2-1）
# ─────────────────────────────────────────────────────────────────────────────


_SCORE_ROLES = frozenset(
    {c.JudgeRole.DISPATCH_SCORE_JUDGE, c.JudgeRole.OBSERVATION_SCORE_JUDGE}
)


def _score_job_payload(job: c.ScoreJob) -> dict:
    payload = job.model_dump(mode="json", exclude={"status", "job_digest"})
    return payload


def build_score_job(
    sample: c.Sample,
    rubric: c.RubricSpec,
    role: c.RoleConfig,
    *,
    input_bundle_ref: c.ArtifactRef,
    manifest_digest: str,
    retry_policy_digest: str,
) -> c.ScoreJob:
    """把 (Sample, RubricSpec, RoleConfig, input bundle, manifest, retry policy)
    绑定成不可变 ScoreJob；target type / judge role 不匹配即拒绝。

    `job_digest` 是对不可变绑定字段（不含 status/job_digest）的 canonical SHA-256，
    是 job 自身的完整性锚点。
    """
    if sample.target_type is not rubric.target_type:
        raise RubricError(
            f"sample target_type {sample.target_type.value} != "
            f"rubric target_type {rubric.target_type.value}"
        )
    if rubric.judge_role is not role.role:
        raise RubricError(
            f"rubric judge_role {rubric.judge_role.value} != "
            f"role config role {role.role.value}"
        )
    if role.role not in _SCORE_ROLES:
        raise RubricError(
            f"score job role must be a score judge role, got {role.role.value}"
        )

    job = c.ScoreJob(
        job_id=uuid4(),
        job_digest="0" * 64,
        sample_id=sample.sample_id,
        target_type=sample.target_type,
        source_digest=sample.source_digest,
        evidence_digest=sample.evidence_digest,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.digest,
        role=role.role,
        prompt_digest=role.prompt_digest,
        model_profile_digest=role.model_profile_digest,
        tool_schema_digest=role.tool_schema_digest,
        input_bundle_ref=input_bundle_ref,
        retry_policy_digest=retry_policy_digest,
        manifest_digest=manifest_digest,
    )
    job_digest = c.sha256_hex(c.canonical_json(_score_job_payload(job)).encode("utf-8"))
    return job.model_copy(update={"job_digest": job_digest})


def build_score_jobs(
    samples: Sequence[c.Sample],
    rubric: c.RubricSpec,
    role: c.RoleConfig,
    *,
    input_bundle_ref: c.ArtifactRef,
    manifest_digest: str,
    retry_policy_digest: str,
) -> list[c.ScoreJob]:
    """批量构建：每个 sample 一个 ScoreJob。"""
    return [
        build_score_job(
            sample,
            rubric,
            role,
            input_bundle_ref=input_bundle_ref,
            manifest_digest=manifest_digest,
            retry_policy_digest=retry_policy_digest,
        )
        for sample in samples
    ]
