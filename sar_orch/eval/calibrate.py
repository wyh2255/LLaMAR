"""F4 Calibration：judge-first + 盲测（设计 `2026-08-09-judge-family-redesign-calibration.md` §7）。

职责：
- **确定性抽样**（seed=42）：cohort `sar_orch/results/20260804_234045_eval_diag_full`
  七 cell（与 P6 同源），observation 20 条（每 cell agent 均衡 + step 均匀，
  s1_a2 取 2）+ dispatch 8 条（每 cell ≥1 个有派发的 step，step 均匀）+ 盲测
  6 条（observation 4 + dispatch 2）；共 28 条。抽样只能消费真实 cohort 数据，
  数据不足以支撑计划 → `CalibrationSamplingError`（绝不静默编造 / 改总数）。
- **清单** `calibration_manifest.json`：sample id / family / cell / step / agent /
  claim_id 或 dispatch_ref / blind / seed / cell source digest / sample input digest。
- **驱动**：仅用 v2 家族 rubric，把 28 条样本 pre-write 成 ScoreJob 再驱动现有
  attempt-v2 workflow（控制面 claim/run/validate/persist/merge/ledger 全部复用）；
  模型配置必须显式注入（`ModelConfig` 或 `runner_factory_factory`），绝不静默
  构造真实 LLM；不改动默认 evaluator CLI（`cli.py` 的 adapter 不受本模块影响）。
- **核实清单**（per-sample JSON + Markdown）：evidence bundle 摘录 + 检索轨迹
  摘要 + judge 维度输出/abstain/evidence refs + 家族派发指令；盲测条目隐藏
  **全部** judge 输出与结论、只保留证据；人工标注后才可 reveal。
- **标注导入**：`accept|correct|unknown` + correct_label + notes；校验盲测
  完整性 / 顺序 / reveal 状态；不自动声称任何人工标签。
- **统计**：冻结门槛精确计算 —— 盲测一致 ≥5/6、非盲纠错 ≤5/22、abstain 诚实
  ≥50%、8 维度每维 ≥2 非 abstain；输出 JSON + Markdown，verdict 仅诊断、
  绝不进入 gate（本模块不 import gate/aggregate 的决策链）。

边界：不写进度文档；不改 v1 rubric / gate / aggregate；不读写 `cli.py` 全局。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import rubric_registry as rr
from sar_orch.eval import score_merge as sm
from sar_orch.eval import workflow as w
from sar_orch.eval.dataset import EpisodeDataset, load_episode

__all__ = [
    "ABSTAIN_HONESTY_MIN",
    "ANNOTATION_FILENAME",
    "BLIND_AGREEMENT_MIN",
    "BLIND_DENOM",
    "BLIND_DISPATCH",
    "BLIND_OBSERVATION",
    "BLIND_TOTAL",
    "CALIBRATION_SEED",
    "COHORT",
    "DEFAULT_COHORT_ROOT",
    "DISPATCH_TOTAL",
    "NON_BLIND_CORRECTION_MAX",
    "NON_BLIND_DENOM",
    "OBSERVATION_TOTAL",
    "P6_CELLS",
    "VERDICTS",
    "AnnotationError",
    "CalibrationConfigError",
    "CalibrationDriver",
    "CalibrationManifest",
    "CalibrationSample",
    "CalibrationSamplingError",
    "CalibrationStatistics",
    "CorrectLabels",
    "MetricResult",
    "ModelConfig",
    "build_chat_model",
    "build_review_packets",
    "compute_statistics",
    "import_annotations",
    "judge_conclusion",
    "load_manifest",
    "reveal_blind",
    "sample_cohort",
    "validate_annotations",
    "write_manifest",
    "write_statistics",
]

# ─────────────────────────────────────────────────────────────────────────────
# 常量（§7.2 / §7.4 冻结值）
# ─────────────────────────────────────────────────────────────────────────────

#: 设计 §7.2 的 cohort（与 P6 同源）。
COHORT = "20260804_234045_eval_diag_full"

#: 默认 cohort 根（相对本文件解析，与 CWD 无关）。
DEFAULT_COHORT_ROOT = (
    Path(__file__).resolve().parents[2] / "sar_orch" / "results" / COHORT
)

#: 七 cell → source 目录名（与 P6 `_cell_source_rel` 一致，冻结）。
P6_CELLS: dict[str, str] = {
    "s1_a2": "s1_s42_a2_r1",
    "s2_a2": "s2_s42_a2_r1",
    "s3_a2": "s3_s42_a2_r1",
    "s3_a4": "s3_s42_a4_r1",
    "s4_a2": "s4_s42_a2_r1",
    "s5_a2": "s5_s42_a2_r1",
    "s5_a4": "s5_s42_a4_r1",
}

#: 抽样总数（§7.2：共 28；observation 每 cell ~3、s1 取 2）。
OBSERVATION_TOTAL = 20
DISPATCH_TOTAL = 8
CALIBRATION_TOTAL = OBSERVATION_TOTAL + DISPATCH_TOTAL

#: 盲测 20% = 6（observation 4 + dispatch 2），seed 固定选取。
BLIND_OBSERVATION = 4
BLIND_DISPATCH = 2
BLIND_TOTAL = BLIND_OBSERVATION + BLIND_DISPATCH

#: 抽样随机种子（§7.2 固定 42）。
CALIBRATION_SEED = 42


#: 每 cell observation 目标条数（s1_a2 取 2，其余 3）。
def _observation_target(cell_id: str) -> int:
    return 2 if cell_id == "s1_a2" else 3


#: §7.4 冻结门槛。
BLIND_AGREEMENT_MIN = 5
BLIND_DENOM = 6
NON_BLIND_CORRECTION_MAX = 5
NON_BLIND_DENOM = CALIBRATION_TOTAL - BLIND_TOTAL  # 22
ABSTAIN_HONESTY_MIN = 0.5
DIMENSION_MIN_NON_ABSTAIN = 2

#: 8 个正交维度（dispatch-v2 4 + observation-v2 4，判定源取自 rubric 注册表）。
DISPATCH_DIMENSIONS = rr.load_rubric_spec(
    rr.DEFAULT_RUBRICS_DIR / "dispatch-v2.yaml"
).spec.dimensions
OBSERVATION_DIMENSIONS = rr.load_rubric_spec(
    rr.DEFAULT_RUBRICS_DIR / "observation-v2.yaml"
).spec.dimensions
ALL_DIMENSIONS = tuple(DISPATCH_DIMENSIONS + OBSERVATION_DIMENSIONS)

#: 人工标注 verdict 白名单。
VERDICTS = frozenset({"accept", "correct", "unknown"})

MANIFEST_FILENAME = "calibration_manifest.json"
SAMPLE_JOBS_FILENAME = "sample_jobs.json"
JUDGE_OUTPUTS_FILENAME = "judge_outputs.json"
ANNOTATION_FILENAME = "annotations.json"
REVEAL_DIR = "reveal"
PACKETS_DIR = "packets"
STATISTICS_JSON_FILENAME = "statistics.json"
STATISTICS_MD_FILENAME = "statistics.md"


# ─────────────────────────────────────────────────────────────────────────────
# 类型化错误
# ─────────────────────────────────────────────────────────────────────────────


class CalibrationError(ValueError):
    """calibration 通用错误。"""


class CalibrationSamplingError(CalibrationError):
    """cohort 数据不足以支撑冻结抽样计划（绝不编造 / 改总数）。"""


class CalibrationConfigError(CalibrationError):
    """驱动配置违例（缺模型 / 缺 runner / 非法 provider 等，不静默降级）。"""


class AnnotationError(CalibrationError):
    """人工标注导入 / 校验违例。"""


# ─────────────────────────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────────────────────────


class CalibrationSample(BaseModel):
    """一条 calibration 样本：识别身份 + 盲测标记 + 输入 digest。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str
    family: c.SampleTargetType
    cell: str
    step: int
    agent: str | None = None
    claim_id: str | None = None
    dispatch_ref: str | None = None
    blind: bool = False
    source_digest: str
    input_digest: str
    ordinal: int

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

    @field_validator("sample_id", "cell", "source_digest", "input_digest")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _family_identity(self) -> CalibrationSample:
        if self.family is c.SampleTargetType.OBSERVATION:
            if not self.agent or not self.claim_id:
                raise ValueError("observation sample requires agent and claim_id")
            if self.dispatch_ref is not None:
                raise ValueError("observation sample must not carry dispatch_ref")
        else:
            if not self.dispatch_ref:
                raise ValueError("dispatch sample requires dispatch_ref")
            if self.agent is not None or self.claim_id is not None:
                raise ValueError("dispatch sample must not carry agent/claim_id")
        return self


class CalibrationManifest(BaseModel):
    """calibration 清单（§7.2 落盘 `calibration_manifest.json`）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    cohort: str
    seed: int
    created_at: datetime
    totals: dict[str, int]
    samples: list[CalibrationSample]

    @field_validator("cohort")
    @classmethod
    def _cohort_name(cls, value: str) -> str:
        if not value or value.startswith("/") or ".." in value.split("/"):
            raise ValueError(f"cohort must be a simple relative name: {value!r}")
        return value

    @field_validator("totals")
    @classmethod
    def _totals(cls, value: dict[str, int]) -> dict[str, int]:
        if any(v < 0 for v in value.values()):
            raise ValueError("totals must be non-negative")
        return value

    def digest(self) -> str:
        """清单内容 digest（含样本选择，不含 created_at）。"""
        return sha256_hex(
            canonical_json(
                {
                    "cohort": self.cohort,
                    "seed": self.seed,
                    "samples": [s.model_dump(mode="json") for s in self.samples],
                }
            ).encode("utf-8")
        )

    def blind_sample_ids(self) -> list[str]:
        return [s.sample_id for s in self.samples if s.blind]

    def sample(self, sample_id: str) -> CalibrationSample:
        for s in self.samples:
            if s.sample_id == sample_id:
                return s
        raise CalibrationError(f"unknown sample_id {sample_id!r} in manifest")


def canonical_json(data: Any) -> str:
    return c.canonical_json(data)


def sha256_hex(data: bytes) -> str:
    return c.sha256_hex(data)


@dataclass(frozen=True)
class ModelConfig:
    """显式模型配置（CLI/API 注入；绝不隐式构造 LLM）。"""

    model: str
    provider: str = "openai"
    api_base: str = "https://api.deepseek.com"
    api_key: str | None = None
    temperature: float = 0.0


def build_chat_model(config: ModelConfig):
    """按显式 ModelConfig 构造 BaseChatModel（openai 兼容 provider）。"""
    if not config.model.strip():
        raise CalibrationConfigError("model must be non-empty")
    if not config.api_key:
        raise CalibrationConfigError("api_key must be explicitly configured")
    if config.provider not in ("openai", "deepseek"):
        raise CalibrationConfigError(
            f"unsupported provider {config.provider!r}; supported: openai/deepseek"
        )
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 仅缺依赖时触发
        raise CalibrationConfigError(
            "langchain-openai is not installed; cannot build a real model"
        ) from exc
    return ChatOpenAI(
        model=config.model,
        base_url=config.api_base,
        api_key=config.api_key or "",
        temperature=config.temperature,
    )


def _load_project_env() -> dict[str, str]:
    """加载项目根 `.env` 的既有小写配置约定，不打印其中的值。"""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# ─────────────────────────────────────────────────────────────────────────────
# 确定性抽样（§7.2）
# ─────────────────────────────────────────────────────────────────────────────


def _redact_truncate(text: str, limit: int = 4000) -> str:
    if not text:
        return text
    text = a.redact_text(text)[0]
    if len(text) <= limit:
        return text
    return text[:limit] + " ... [truncated]"


def _observation_claims(episode: EpisodeDataset) -> list[dict[str, Any]]:
    """report_observation claims：确定性迭代顺序（step, agent, call_index）。

    与 workflow `_build_claims` 同一识别身份（`<step>:<agent>:<call_index>`）。
    """
    claims: list[dict[str, Any]] = []
    for step, sr in sorted((episode.steps or {}).items()):
        seen: dict[str, int] = {}
        for ai in getattr(sr, "interactions", None) or []:
            if ai.tool_name != "report_observation":
                continue
            claim = getattr(ai, "tool_args", "") or ""
            if not claim:
                continue
            ci = seen.get(ai.agent, 0)
            seen[ai.agent] = ci + 1
            claims.append(
                {
                    "step": int(step),
                    "agent": ai.agent,
                    "call_index": ci,
                    "claim_id": f"{step}:{ai.agent}:{ci}",
                    "claim": _redact_truncate(claim),
                    "system_response": _redact_truncate(
                        getattr(ai, "observation", "") or ""
                    ),
                }
            )
    return claims


def _spread_indices(length: int, count: int) -> list[int]:
    """在 [0, length) 上确定性均匀取 count 个下标（stride）。

    count==0 → []；count>=length → 全部；count==1 → 中点；否则 round 均布。
    """
    if count <= 0:
        return []
    if count >= length:
        return list(range(length))
    if count == 1:
        return [length // 2]
    return [round(i * (length - 1) / (count - 1)) for i in range(count)]


def _deduplicate_steps(
    by_agent: dict[str, list[dict[str, Any]]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """同一 cell 内已选 claim 尽量让 step 互不重复（保持 agent 配额）。

    确定性：按 (step, agent) 遍历；重复 step 时在同 agent 内取第一个未用 step
    的 claim 替换；无可用替换则保留（不允许为去重破坏 agent 配额）。
    """
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for cl in sorted(selected, key=lambda x: (x["step"], x["agent"])):
        if cl["step"] not in used:
            used.add(cl["step"])
            out.append(cl)
            continue
        agent = cl["agent"]
        replacement = next(
            (
                cand
                for cand in by_agent[agent]
                if cand["step"] not in used and cand["step"] != cl["step"]
            ),
            None,
        )
        if replacement is not None:
            used.add(replacement["step"])
            out.append(replacement)
        else:
            out.append(cl)
    return out


def _sample_observation_cell(
    cell_id: str, claims: list[dict[str, Any]], target: int
) -> list[dict[str, Any]]:
    """单 cell observation 抽样：agent 均衡配额 + 步内 stride + step 去重。

    数据不足（cell 无 claim / 某 agent 配额超过其 claim 数）→ typed error。
    """
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for cl in claims:
        by_agent.setdefault(cl["agent"], []).append(cl)
    agents = sorted(by_agent)
    if not agents:
        raise CalibrationSamplingError(
            f"cell {cell_id}: no report_observation claims in cohort data"
        )
    base, rem = divmod(target, len(agents))
    quotas = {agent: base + (1 if idx < rem else 0) for idx, agent in enumerate(agents)}
    for agent, quota in quotas.items():
        if quota > len(by_agent[agent]):
            raise CalibrationSamplingError(
                f"cell {cell_id}: observation target {target} not supported by "
                f"cohort data: agent {agent!r} has {len(by_agent[agent])} claims "
                f"but the agent-balanced quota is {quota}"
            )
    selected: list[dict[str, Any]] = []
    for agent in agents:
        pool = by_agent[agent]
        for idx in _spread_indices(len(pool), quotas[agent]):
            selected.append(pool[idx])
    return _deduplicate_steps(by_agent, selected)


def _dispatch_steps(episode: EpisodeDataset) -> list[int]:
    """该 cell 有派发记录的 step（去重排序）。"""
    return sorted({int(d.step) for d in (episode.dispatches or [])})


def _dispatch_records_at(episode: EpisodeDataset, step: int) -> list[dict[str, Any]]:
    """该 step 的派发记录（确定性排序；文本经脱敏 + 截断，与 workflow 同标准）。"""
    records = [
        {
            "Step": int(d.step),
            "Subtask": w._truncate_deterministic(w._redact_claim_text(d.subtask)),
            "AssignedTo": d.assigned_to,
            "EventType": d.event_type,
        }
        for d in sorted(
            (episode.dispatches or []), key=lambda d: (d.step, d.assigned_to)
        )
        if int(d.step) == int(step)
    ]
    return records


def _sample_dispatch(
    episodes: dict[str, EpisodeDataset],
) -> list[tuple[str, int]]:
    """dispatch 抽样：每 cell ≥1 个有派发的 step + 一个额外 cell，stride 均匀。

    有 cell 无派发 → typed error（无法满足"每 cell ≥1"）。
    """
    pools: dict[str, list[int]] = {}
    for cell_id, episode in episodes.items():
        steps = _dispatch_steps(episode)
        if not steps:
            raise CalibrationSamplingError(
                f"cell {cell_id}: no dispatch steps in cohort data (required "
                f"'each cell >= 1 dispatch step')"
            )
        pools[cell_id] = steps
    # 7 cell 各 ≥1 → 8 条；额外 1 条给派发 step 最多的 cell（并列取 cell_id 最大）。
    extra_cell = max(pools, key=lambda cid: (len(pools[cid]), cid))
    allocations = {cid: 1 for cid in pools}
    allocations[extra_cell] += 1
    out: list[tuple[str, int]] = []
    for cell_id in sorted(pools):
        steps = pools[cell_id]
        for idx in _spread_indices(len(steps), allocations[cell_id]):
            out.append((cell_id, steps[idx]))
    return out


def _cell_source_digest(run_dir: Path) -> str:
    """cell 源输入 digest：allowlist 文件 sha256 映射的 canonical hash。"""
    snapshot = a.snapshot_source_files(run_dir, w._source_allowlist(run_dir))
    payload = {"files": dict(sorted(snapshot.files.items()))}
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def _sample_input_digest(sample: dict[str, Any], episode: EpisodeDataset) -> str:
    """sample 输入 digest：核心里据的 canonical hash（不依赖运行时布局）。"""
    if sample["family"] is c.SampleTargetType.OBSERVATION:
        payload = {
            "family": "observation",
            "cell": sample["cell"],
            "step": sample["step"],
            "agent": sample["agent"],
            "claim_id": sample["claim_id"],
            "claim": sample["claim"],
            "system_response": sample["system_response"],
        }
    else:
        payload = {
            "family": "dispatch",
            "cell": sample["cell"],
            "step": sample["step"],
            "dispatch_records": _dispatch_records_at(episode, sample["step"]),
        }
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def _cell_run_dir(cohort_root: Path, cell_id: str) -> Path:
    return cohort_root / P6_CELLS[cell_id]


def _select_blind(
    obs_ids: Sequence[str], dispatch_ids: Sequence[str], *, seed: int
) -> frozenset[str]:
    """盲测选取：固定 seed，从各自排序 id 列表确定性抽签。"""
    rng = random.Random(seed)
    blind = set(rng.sample(sorted(obs_ids), BLIND_OBSERVATION))
    blind.update(rng.sample(sorted(dispatch_ids), BLIND_DISPATCH))
    return frozenset(blind)


def sample_cohort(
    cohort_root: str | Path = DEFAULT_COHORT_ROOT, *, seed: int = CALIBRATION_SEED
) -> CalibrationManifest:
    """§7.2 分层抽样：确定、可复现；数据不足 → `CalibrationSamplingError`。"""
    cohort_root = Path(cohort_root)
    episodes: dict[str, EpisodeDataset] = {}
    run_dirs: dict[str, Path] = {}
    for cell_id in sorted(P6_CELLS):
        run_dir = _cell_run_dir(cohort_root, cell_id)
        if not run_dir.is_dir():
            raise CalibrationSamplingError(
                f"cell {cell_id}: cohort run dir missing: {run_dir}"
            )
        episodes[cell_id] = load_episode(run_dir)
        run_dirs[cell_id] = run_dir

    obs_samples: list[dict[str, Any]] = []
    for cell_id in sorted(P6_CELLS):
        claims = _observation_claims(episodes[cell_id])
        target = _observation_target(cell_id)
        picked = _sample_observation_cell(cell_id, claims, target)
        if len(picked) != target:
            raise CalibrationSamplingError(
                f"cell {cell_id}: expected {target} observation samples, "
                f"got {len(picked)} (sampler under-produced)"
            )
        for cl in picked:
            sample = {
                "family": c.SampleTargetType.OBSERVATION,
                "cell": cell_id,
                "step": cl["step"],
                "agent": cl["agent"],
                "claim_id": cl["claim_id"],
                "dispatch_ref": None,
                "claim": cl["claim"],
                "system_response": cl["system_response"],
            }
            sample["input_digest"] = _sample_input_digest(sample, episodes[cell_id])
            obs_samples.append(sample)
    if len(obs_samples) != OBSERVATION_TOTAL:
        raise CalibrationSamplingError(
            f"observation sampling produced {len(obs_samples)} != {OBSERVATION_TOTAL}"
        )

    dispatch_samples: list[dict[str, Any]] = []
    for cell_id, step in _sample_dispatch(episodes):
        sample = {
            "family": c.SampleTargetType.DISPATCH,
            "cell": cell_id,
            "step": step,
            "agent": None,
            "claim_id": None,
            "dispatch_ref": f"step:{step}",
        }
        sample["input_digest"] = _sample_input_digest(sample, episodes[cell_id])
        dispatch_samples.append(sample)
    if len(dispatch_samples) != DISPATCH_TOTAL:
        raise CalibrationSamplingError(
            f"dispatch sampling produced {len(dispatch_samples)} != {DISPATCH_TOTAL}"
        )

    blind = _select_blind(
        [f"obs-{i + 1:04d}" for i in range(len(obs_samples))],
        [f"dsp-{i + 1:04d}" for i in range(len(dispatch_samples))],
        seed=seed,
    )
    if len(blind) != BLIND_TOTAL:
        raise CalibrationSamplingError(
            f"blind selection produced {len(blind)} != {BLIND_TOTAL}"
        )

    samples: list[CalibrationSample] = []
    obs_seen: dict[str, int] = {}
    for idx, raw in enumerate(obs_samples):
        cell = raw["cell"]
        n = obs_seen.get(cell, 0) + 1
        obs_seen[cell] = n
        sample_id = f"obs-{idx + 1:04d}"
        samples.append(
            CalibrationSample(
                sample_id=sample_id,
                family=raw["family"],
                cell=cell,
                step=raw["step"],
                agent=raw["agent"],
                claim_id=raw["claim_id"],
                blind=sample_id in blind,
                source_digest=_cell_source_digest(run_dirs[cell]),
                input_digest=raw["input_digest"],
                ordinal=idx,
            )
        )
    for idx, raw in enumerate(dispatch_samples):
        sample_id = f"dsp-{idx + 1:04d}"
        cell = raw["cell"]
        samples.append(
            CalibrationSample(
                sample_id=sample_id,
                family=raw["family"],
                cell=cell,
                step=raw["step"],
                dispatch_ref=raw["dispatch_ref"],
                blind=sample_id in blind,
                source_digest=_cell_source_digest(run_dirs[cell]),
                input_digest=raw["input_digest"],
                ordinal=OBSERVATION_TOTAL + idx,
            )
        )

    totals = {
        "observation": OBSERVATION_TOTAL,
        "dispatch": DISPATCH_TOTAL,
        "total": CALIBRATION_TOTAL,
        "blind": BLIND_TOTAL,
        "blind_observation": BLIND_OBSERVATION,
        "blind_dispatch": BLIND_DISPATCH,
    }
    manifest = CalibrationManifest(
        cohort=cohort_root.name,
        seed=seed,
        created_at=datetime.now(UTC),
        totals=totals,
        samples=samples,
    )
    # 固化 invariants：清单确实是 28 条 / 盲测恰好 6。
    if len(manifest.samples) != CALIBRATION_TOTAL:
        raise CalibrationSamplingError(
            f"manifest has {len(manifest.samples)} != {CALIBRATION_TOTAL} samples"
        )
    if len(manifest.blind_sample_ids()) != BLIND_TOTAL:
        raise CalibrationSamplingError(
            f"manifest blind count {len(manifest.blind_sample_ids())} != {BLIND_TOTAL}"
        )
    return manifest


def write_manifest(manifest: CalibrationManifest, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")
    payload["manifest_digest"] = manifest.digest()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return path


def load_manifest(path: str | Path) -> CalibrationManifest:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    digest = raw.pop("manifest_digest", None)
    manifest = CalibrationManifest.model_validate(raw)
    if digest is not None:
        expected = manifest.digest()
        if digest != expected:
            raise CalibrationError(
                f"manifest_digest mismatch: {digest[:12]}... != {expected[:12]}..."
            )
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# 驱动：pre-write ScoreJobs → 现有 attempt-v2 workflow（v2 rubric only）
# ─────────────────────────────────────────────────────────────────────────────

#: calibration 身份命名空间（确定性 attempt/job id 派生）。
_NS = NAMESPACE_URL


def _cal_uuid(*parts: str) -> UUID:
    return uuid5(_NS, ":".join(parts))


class CalibrationDriver:
    """按 cell 分片驱动 attempt-v2 workflow（v2 家族 rubric only）。

    - `model_config` 或 `runner_factory_factory` 至少一个必须显式注入；都不提供
      而 workflow 需要 score runner → `CalibrationConfigError`（绝不隐式 LLM）。
    - 每个 cell 一个 attempt：pre-write 该 cell 的 calibration ScoreJob 到 store，
      再 `run_eval_workflow`（`_build_score_jobs` 复用已落盘 job → 恰好 28 条被评）。
    - 产出 `sample_jobs.json`（cell → attempt 身份 + sample → job）+ `judge_outputs.json`
      （per-sample judge 输出 / bundle 摘录 / 检索审计 / 家族派发）。
    """

    def __init__(
        self,
        cohort_root: str | Path,
        output_dir: str | Path,
        manifest: CalibrationManifest,
        *,
        model_config: ModelConfig | None = None,
        runner_factory_factory: (
            Callable[[w.EvalRuntime], Callable[[], Any]] | None
        ) = None,
        timeout_s: float | None = None,
        grader_fn: Callable[[], tuple[list[Any], list[sm.DeterministicViolation]]]
        | None = None,
    ) -> None:
        if model_config is None and runner_factory_factory is None:
            raise CalibrationConfigError(
                "calibration drive requires an explicit model_config or an "
                "injected runner_factory_factory; refusing to construct an "
                "implicit real LLM"
            )
        if model_config is not None and runner_factory_factory is not None:
            raise CalibrationConfigError(
                "provide either model_config or runner_factory_factory, not both"
            )
        self.cohort_root = Path(cohort_root)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.model_config = model_config
        self.runner_factory_factory = runner_factory_factory
        self.timeout_s = timeout_s
        self._grader_fn = grader_fn

    # ── attempt 布局 ────────────────────────────────────────────────────────
    def _attempt_identity(self, cell_id: str) -> tuple[UUID, UUID]:
        seed = self.manifest.seed
        eval_run_id = _cal_uuid(f"calibration:{self.manifest.cohort}:{cell_id}:{seed}")
        attempt_id = _cal_uuid(
            f"calibration:{self.manifest.cohort}:{cell_id}:attempt:{seed}"
        )
        return eval_run_id, attempt_id

    def _attempt_root(self, cell_id: str) -> Path:
        eval_run_id, attempt_id = self._attempt_identity(cell_id)
        return (
            self.output_dir
            / "attempts"
            / str(eval_run_id)
            / "attempts"
            / str(attempt_id)
        )

    def _runner_factory(self, rt: w.EvalRuntime) -> Callable[[], Any]:
        if self.runner_factory_factory is not None:
            return self.runner_factory_factory(rt)
        from sar_orch.eval.agent.family_runner import (
            make_family_or_role_runner_factory,
        )

        assert self.model_config is not None  # __init__ 已保证二选一
        model = build_chat_model(self.model_config)
        return make_family_or_role_runner_factory(
            rt, model=model, run_dir=rt.source_run_dir
        )

    # ── cell manifest（v2 rubric only，仅含该 cell 有样本的家族）─────────────
    def _cell_manifest(
        self,
        cell_id: str,
        episode: EpisodeDataset,
        cell_root: Path,
        source_digest: str,
        eval_run_id: UUID,
        attempt_id: UUID,
    ) -> c.FrozenInputManifest:
        families = {s.family for s in self.manifest.samples if s.cell == cell_id}
        rubrics: list[c.RubricSpec] = []
        if c.SampleTargetType.DISPATCH in families:
            rubrics.append(
                rr.load_rubric_spec(rr.DEFAULT_RUBRICS_DIR / "dispatch-v2.yaml").spec
            )
        if c.SampleTargetType.OBSERVATION in families:
            rubrics.append(
                rr.load_rubric_spec(rr.DEFAULT_RUBRICS_DIR / "observation-v2.yaml").spec
            )
        if not rubrics:
            raise CalibrationConfigError(
                f"cell {cell_id}: no calibration samples for any v2 family"
            )
        metadata = episode.metadata or {}
        try:
            scene = int(metadata.get("scene", 1))
        except (TypeError, ValueError):
            scene = 1
        try:
            agents = int(metadata.get("agent_count") or metadata.get("agents") or 2)
        except (TypeError, ValueError):
            agents = 2
        try:
            seed = int(metadata.get("seed", 0))
        except (TypeError, ValueError):
            seed = 0
        subject_payload = {
            "source_run_dir": cell_root.name,
            "source_digest": source_digest,
            "scene": scene,
            "agents": agents,
            "seed": seed,
            "code_commit": metadata.get("code_commit", ""),
            "git_dirty": bool(metadata.get("git_dirty", False)),
        }
        subject_ref = w._canonical_json_ref(
            w.SUBJECT_REF_REL, subject_payload, "calibration"
        )
        source_manifest = c.SourceInputManifest(
            eval_run_id=eval_run_id,
            source_run_dir=cell_root.name,
            files={},
            excluded=[],
        )
        source_manifest_ref = w._canonical_json_ref(
            w.SOURCE_MANIFEST_REL,
            source_manifest.model_dump(mode="json"),
            "calibration",
        )
        manifest = c.InputManifest(
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
                code_commit=metadata.get("code_commit", ""),
                git_dirty=bool(metadata.get("git_dirty", False)),
            ),
            evaluator=c.EvaluatorSpec(
                workflow_version="0.1.0",
                source_tree_digest=sha256_hex(b"sar-eval-calibration"),
            ),
            policy=c.PolicySpec(
                llm_judge_required=True,
                audit_level=c.AuditLevel.STANDARD,
                retry=2,
                timeout_s=60,
                concurrency=2,
                max_score_jobs=None,
                retention_days=30,
                allow_shared_model=True,
                merge_policy_digest=sm.MergePolicy().digest(),
            ),
            roles=[w._role_config(r) for r in rubrics],
            rubrics=rubrics,
            command=c.CommandSpec(
                argv_without_secrets=["calibrate"],
                cwd=str(cell_root),
                env_allowlist=["PATH"],
            ),
        )
        return manifest.freeze()

    # ── per-sample judge output 收集 ─────────────────────────────────────────
    def _collect_judge_output(
        self, store: a.ArtifactStore, job: c.ScoreJob
    ) -> dict[str, Any]:
        job_id = str(job.job_id)
        job_payload = json.loads(store.read_bytes(f"{w.JOBS_DIR}/{job_id}.json"))
        out: dict[str, Any] = {
            "job_id": job_id,
            "job_status": job_payload.get("status", "pending"),
        }
        result_payload: dict[str, Any] | None = None
        result_rel = f"{w.RESULTS_DIR}/{job_id}.json"
        if store.exists(result_rel):
            result_payload = json.loads(store.read_bytes(result_rel))
        out["result_status"] = (
            result_payload.get("status") if result_payload is not None else None
        )
        if result_payload is not None:
            out["validation_errors"] = result_payload.get("validation_errors", [])
            out["usage"] = result_payload.get("usage")
        if store.exists(job.input_bundle_ref.path):
            bundle = json.loads(store.read_verified(job.input_bundle_ref))
            out["bundle_rel"] = job.input_bundle_ref.path
            out["bundle_digest"] = job.input_bundle_ref.sha256
            out["evidence_excerpt"] = bundle
        if result_payload is not None and result_payload.get("validated_output_ref"):
            ref = c.ArtifactRef.model_validate(result_payload["validated_output_ref"])
            draft = json.loads(store.read_verified(ref))
            out["draft"] = draft
            out["conclusion"] = judge_conclusion(draft)
            inv_rel = str(Path(ref.path).parent)
            out["family_dispatch"] = self._read_jsonl(
                store, f"{inv_rel}/family_dispatch.jsonl"
            )
            out["retrieval_audit"] = self._read_retrieval_audit(store, inv_rel)
            out["usage"] = result_payload.get("usage")
        return out

    @staticmethod
    def _read_jsonl(store: a.ArtifactStore, rel: str) -> list[dict[str, Any]]:
        if not store.exists(rel):
            return []
        out: list[dict[str, Any]] = []
        for line in (
            store.read_bytes(rel).decode("utf-8", errors="replace").splitlines()
        ):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _read_retrieval_audit(
        self, store: a.ArtifactStore, inv_rel: str
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for rec in self._read_jsonl(store, f"{inv_rel}/events.jsonl"):
            rec = dict(rec)
            rec["agent"] = "family"
            records.append(rec)
        for dim_rel in sorted(
            Path(store.root).joinpath(inv_rel, "subagents").glob("*")
        ):
            if not dim_rel.is_dir():
                continue
            dim = dim_rel.name
            for rec in self._read_jsonl(
                store, f"{inv_rel}/subagents/{dim}/events.jsonl"
            ):
                rec = dict(rec)
                rec["agent"] = f"subagent:{dim}"
                records.append(rec)
        return records

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def drive(self) -> dict[str, Any]:
        cells = sorted({s.cell for s in self.manifest.samples})
        cell_state: dict[str, Any] = {}
        outputs: dict[str, Any] = {}
        for cell_id in cells:
            cell_samples = [s for s in self.manifest.samples if s.cell == cell_id]
            cell_root = _cell_run_dir(self.cohort_root, cell_id)
            if not cell_root.is_dir():
                raise CalibrationConfigError(
                    f"cell {cell_id}: run dir missing: {cell_root}"
                )
            episode = load_episode(cell_root)
            source_digest = _cell_source_digest(cell_root)
            eval_run_id, attempt_id = self._attempt_identity(cell_id)
            store = a.ArtifactStore(self._attempt_root(cell_id))
            if store.exists(a.ArtifactStore.MANIFEST_REL):
                frozen = store.read_input_manifest()
                identity = frozen.manifest
                if (
                    identity.eval_run_id != eval_run_id
                    or identity.attempt_id != attempt_id
                    or identity.subject.source_digest != source_digest
                ):
                    raise CalibrationConfigError(
                        f"cell {cell_id}: persisted attempt manifest does not match "
                        "the frozen calibration identity/source"
                    )
            else:
                frozen = self._cell_manifest(
                    cell_id, episode, cell_root, source_digest, eval_run_id, attempt_id
                )
            rt = w.EvalRuntime(
                frozen,
                store,
                store.audit_journal(),
                episode=episode,
                grader_fn=(self._grader_fn or partial(w._default_grader_fn, episode)),
                judge_sample_steps=rr.DEFAULT_JUDGE_SAMPLE_STEPS,
                timeout_s=self.timeout_s,
                source_run_dir=cell_root,
            )
            role_by_role = {r.role: r for r in frozen.manifest.roles}
            rubric_by_family = {r.target_type: r for r in frozen.manifest.rubrics}
            retry_digest = w._retry_policy_digest(frozen.manifest.policy)
            sample_jobs: dict[str, str] = {}
            for ordinal, cs in enumerate(cell_samples):
                c_sample = c.Sample(
                    sample_id=_cal_uuid(
                        f"calibration:sample:{self.manifest.cohort}:{cs.sample_id}"
                    ),
                    target_type=cs.family,
                    step=cs.step,
                    agent=cs.agent,
                    claim_call_ids=[cs.claim_id] if cs.claim_id else [],
                    source_digest=source_digest,
                    evidence_digest=frozen.manifest.digest(),
                    selection_reason="calibration_stratified",
                    ordinal=ordinal,
                )
                rubric = rubric_by_family[cs.family]
                role = role_by_role[rubric.judge_role]
                job_id = _cal_uuid(
                    f"calibration:job:{self.manifest.cohort}:{cs.sample_id}"
                )
                job = w._load_score_job(store, str(job_id))
                if job is None:
                    bundle_ref = w._materialize_v2_job_bundle(rt, c_sample, job_id)
                    job = rr.build_score_job(
                        c_sample,
                        rubric,
                        role,
                        input_bundle_ref=bundle_ref,
                        manifest_digest=frozen.manifest.digest(),
                        retry_policy_digest=retry_digest,
                        job_id=job_id,
                    )
                    store.write_canonical_json(
                        f"{w.JOBS_DIR}/{job_id}.json",
                        job.model_dump(mode="json"),
                        producer="calibration",
                    )
                elif (
                    job.rubric_id != rubric.rubric_id
                    or job.manifest_digest != frozen.manifest.digest()
                ):
                    raise CalibrationConfigError(
                        f"cell {cell_id}: persisted score job {job_id} does not match "
                        "the frozen calibration manifest/rubric"
                    )
                sample_jobs[cs.sample_id] = str(job_id)
            ledger = store.read_final_ledger()
            if ledger is None:
                rt.runner_factory = self._runner_factory(rt)
                outcome = w.run_eval_workflow(rt)
                outcome_status = outcome.status.value
            elif c.is_workflow_terminal(ledger.terminal_status):
                outcome_status = ledger.terminal_status.value
            else:
                rt.runner_factory = self._runner_factory(rt)
                outcome = w.run_eval_workflow(rt)
                outcome_status = outcome.status.value
            cell_outputs: dict[str, Any] = {}
            for cs in cell_samples:
                job_id = UUID(sample_jobs[cs.sample_id])
                job = _load_job_for_output(store, job_id)
                cell_outputs[cs.sample_id] = self._collect_judge_output(store, job)
            cell_state[cell_id] = {
                "eval_run_id": str(eval_run_id),
                "attempt_id": str(attempt_id),
                "attempt_root": str(store.root),
                "outcome": outcome_status,
                "jobs": sample_jobs,
            }
            outputs.update(cell_outputs)
        state = {
            "schema_version": 1,
            "seed": self.manifest.seed,
            "cohort": self.manifest.cohort,
            "cells": cell_state,
        }
        (self.output_dir / SAMPLE_JOBS_FILENAME).write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", "utf-8"
        )
        (self.output_dir / JUDGE_OUTPUTS_FILENAME).write_text(
            json.dumps(
                {"schema_version": 1, "outputs": outputs}, indent=2, ensure_ascii=False
            )
            + "\n",
            "utf-8",
        )
        return state


def _load_job_for_output(store: a.ArtifactStore, job_id: UUID) -> c.ScoreJob:
    job = w._load_score_job(store, str(job_id))
    if job is None:
        raise CalibrationConfigError(f"score job {job_id} missing after drive")
    return job


# ─────────────────────────────────────────────────────────────────────────────
# judge 结论（clean / flagged / abstain / None）与核实清单
# ─────────────────────────────────────────────────────────────────────────────


def judge_conclusion(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 ScoreDraft 派生 judge 结论（诊断信号）。

    - 无 draft → None（该样本无结论）；
    - 全维度 abstain → `{"state": "abstain", ...}`；
    - 任一评分维度 < 0.5 → `flagged`；否则 `clean`。
    """
    if draft is None:
        return None
    dims = draft.get("dimensions", {}) or {}
    reasons = draft.get("dimension_unknown_reasons", {}) or {}
    if not dims:
        return {"state": "abstain", "scored": 0, "abstained": len(reasons)}
    values = [float(v) for v in dims.values()]
    flagged = sorted(d for d, v in dims.items() if float(v) < 0.5)
    return {
        "state": "flagged" if flagged else "clean",
        "scored": len(values),
        "abstained": len(reasons),
        "flagged": flagged,
        "min": min(values),
        "mean": round(sum(values) / len(values), 6),
    }


def _load_outputs(output_dir: Path) -> dict[str, Any]:
    path = output_dir / JUDGE_OUTPUTS_FILENAME
    if not path.exists():
        raise CalibrationConfigError(f"missing {path}; run drive() first")
    return json.loads(path.read_text(encoding="utf-8"))["outputs"]


def build_review_packets(
    manifest: CalibrationManifest, output_dir: str | Path
) -> list[dict[str, Any]]:
    """per-sample 核实清单（JSON + Markdown）。盲测隐藏全部 judge 输出与结论。"""
    output_dir = Path(output_dir)
    outputs = _load_outputs(output_dir)
    packets_dir = output_dir / PACKETS_DIR
    packets_dir.mkdir(parents=True, exist_ok=True)
    packets: list[dict[str, Any]] = []
    for sample in manifest.samples:
        packet = _build_review_packet(manifest, sample, outputs.get(sample.sample_id))
        packets_dir.joinpath(f"{sample.sample_id}.json").write_text(
            json.dumps(packet, indent=2, ensure_ascii=False) + "\n", "utf-8"
        )
        packets_dir.joinpath(f"{sample.sample_id}.md").write_text(
            _render_packet_markdown(packet), "utf-8"
        )
        packets.append(packet)
    return packets


def _build_review_packet(
    manifest: CalibrationManifest,
    sample: CalibrationSample,
    judge_output: dict[str, Any] | None,
) -> dict[str, Any]:
    """单条核实清单。盲测样本 `judge_output_hidden=true`，judge/family_dispatch/
    retrieval_audit 全部隐藏，只保留 evidence_excerpt 与样本元数据。"""
    packet: dict[str, Any] = {
        "schema_version": 1,
        "sample_id": sample.sample_id,
        "family": sample.family.value,
        "cell": sample.cell,
        "step": sample.step,
        "agent": sample.agent,
        "claim_id": sample.claim_id,
        "dispatch_ref": sample.dispatch_ref,
        "blind": sample.blind,
        "seed": manifest.seed,
        "cohort": manifest.cohort,
        "source_digest": sample.source_digest,
        "input_digest": sample.input_digest,
        "judge_output_hidden": sample.blind,
        "evidence_excerpt": ((judge_output or {}).get("evidence_excerpt") or {}),
        "judge": None,
        "family_dispatch": [],
        "retrieval_audit": [],
        "human": {
            "verdict": None,
            "correct_label": None,
            "notes": None,
            "revealed": False,
        },
    }
    if judge_output is None:
        packet["drive_error"] = "no judge output recorded for this sample"
    elif not sample.blind:
        packet["judge"] = {
            "job_id": judge_output.get("job_id"),
            "job_status": judge_output.get("job_status"),
            "result_status": judge_output.get("result_status"),
            "validation_errors": judge_output.get("validation_errors", []),
            "usage": judge_output.get("usage"),
            "bundle_rel": judge_output.get("bundle_rel"),
            "bundle_digest": judge_output.get("bundle_digest"),
            "draft": judge_output.get("draft"),
            "conclusion": judge_output.get("conclusion"),
        }
        packet["family_dispatch"] = judge_output.get("family_dispatch", [])
        packet["retrieval_audit"] = judge_output.get("retrieval_audit", [])
    return packet


def _md_evidence(packet: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    excerpt = packet.get("evidence_excerpt") or {}
    family = packet.get("family")
    if family == "observation":
        claims = excerpt.get("claims") or []
        lines.append("**Evidence bundle (observation-v2):**")
        if not claims:
            lines.append("  *(empty claims)*")
        for claim in claims:
            lines.append(f"- claim: `{claim.get('claim', '')}`")
            lines.append(f"  system_response: `{claim.get('system_response', '')}`")
            if claim.get("llm_response_preview"):
                lines.append(
                    f"  llm_response_preview: `{claim.get('llm_response_preview', '')}`"
                )
    else:
        lines.append("**Evidence bundle (dispatch-v2):**")
        budget = excerpt.get("step_budget") or {}
        if budget:
            lines.append(
                f"- step_budget: step={budget.get('step')} "
                f"max={budget.get('max_steps')} remaining={budget.get('remaining')}"
            )
        records = excerpt.get("dispatch_records") or []
        lines.append(f"- dispatch_records ({len(records)}):")
        for rec in records:
            lines.append(
                f"  - step {rec.get('Step')} → {rec.get('AssignedTo')} "
                f"[{rec.get('EventType')}]: {rec.get('Subtask', '')}"
            )
        team = excerpt.get("team_state") or []
        if team:
            lines.append("- team_state:")
            for agent in team:
                lines.append(
                    f"  - {agent.get('agent')}: pos={agent.get('position')} "
                    f"inv={agent.get('inventory')} active={agent.get('active_task')}"
                )
        if excerpt.get("map_summary"):
            lines.append(f"- map_summary: {excerpt.get('map_summary')}")
    return lines


def _render_packet_markdown(packet: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Review Packet — {packet['sample_id']}")
    lines.append("")
    lines.append(
        f"- family: **{packet['family']}** | cell: {packet['cell']} | "
        f"step: {packet['step']}"
    )
    if packet.get("agent"):
        lines.append(f"- agent: {packet['agent']}")
    if packet.get("claim_id"):
        lines.append(f"- claim_id: {packet['claim_id']}")
    if packet.get("dispatch_ref"):
        lines.append(f"- dispatch_ref: {packet['dispatch_ref']}")
    lines.append(
        f"- blind: {'**YES (judge output hidden)**' if packet.get('blind') else 'no'}"
    )
    lines.append(f"- input_digest: `{packet['input_digest']}`")
    lines.append("")
    lines.append("## Evidence")
    lines.extend(_md_evidence(packet))
    lines.append("")
    if packet.get("judge_output_hidden"):
        lines.append(
            "## Judge Output — HIDDEN (blind)\n\n"
            "This sample is a blind-control item: the judge's dimension scores, "
            "abstain reasons, evidence refs and conclusion are withheld. Judge "
            "from the evidence above first, then annotate `accept` / `correct` "
            "(`unknown` if genuinely undecidable). The judge output becomes "
            "visible only via the reveal step after your annotation is recorded."
        )
    else:
        judge = packet.get("judge") or {}
        lines.append("## Judge Output")
        lines.append(
            f"- job_status: {judge.get('job_status')} | "
            f"result_status: {judge.get('result_status')}"
        )
        if judge.get("validation_errors"):
            lines.append(f"- validation_errors: {judge.get('validation_errors')}")
        draft = judge.get("draft") or {}
        dims = draft.get("dimensions") or {}
        reasons = draft.get("dimension_unknown_reasons") or {}
        if dims:
            lines.append("- dimension scores:")
            for dim, score in sorted(dims.items()):
                lines.append(f"  - {dim}: {score}")
        if reasons:
            lines.append("- dimension abstain reasons:")
            for dim, reason in sorted(reasons.items()):
                lines.append(f"  - {dim}: {reason}")
        evs = draft.get("evidence") or []
        if evs:
            lines.append("- evidence refs (path + digest):")
            for ev in evs:
                ref = ev.get("ref") or {}
                lines.append(f"  - {ref.get('path')} sha256={ev.get('digest')}")
        if judge.get("conclusion"):
            lines.append(f"- conclusion: {judge.get('conclusion')}")
        lines.append("")
        dispatch = packet.get("family_dispatch") or []
        if dispatch:
            lines.append("## Family Dispatch Instructions")
            for rec in dispatch:
                if rec.get("status") != "accepted":
                    continue
                lines.append(f"### {rec.get('dimension')}")
                lines.append(f"```\n{rec.get('instruction', '')}\n```")
            lines.append("")
        audit = packet.get("retrieval_audit") or []
        if audit:
            lines.append("## Retrieval Audit Summary")
            for rec in audit:
                lines.append(
                    f"- [{rec.get('agent')}] {rec.get('tool')} "
                    f"params={rec.get('params')} refs={rec.get('returned_refs')} "
                    f"bytes={rec.get('bytes')} truncated={rec.get('truncated')}"
                )
            lines.append("")
    lines.append("## Human Feedback")
    lines.append(
        "- verdict: `accept` | `correct` | `unknown`\n"
        "- correct_label (required iff `correct`; becomes ground truth)\n"
        "- notes (optional)"
    )
    return "\n".join(lines) + "\n"


def reveal_blind(
    manifest: CalibrationManifest,
    output_dir: str | Path,
    sample_id: str,
) -> dict[str, Any]:
    """盲测 reveal：人工标注**先于** reveal 才可揭晓 judge 输出（§7.3）。

    校验 reveal 状态：非盲测条目 / 未标注 / 标注缺失 verdict → 拒绝。
    """
    output_dir = Path(output_dir)
    sample = manifest.sample(sample_id)
    if not sample.blind:
        raise AnnotationError(
            f"sample {sample_id} is not a blind item; reveal is only for blind "
            f"control samples"
        )
    annotations = _load_annotations(output_dir)
    annotation = annotations.get(sample_id)
    if annotation is None or annotation.get("verdict") is None:
        raise AnnotationError(
            f"blind sample {sample_id} must be annotated before reveal "
            f"(blind-first ordering: annotation precedes reveal)"
        )
    outputs = _load_outputs(output_dir)
    judge_output = outputs.get(sample_id)
    if judge_output is None:
        raise CalibrationConfigError(
            f"no judge output recorded for blind sample {sample_id}"
        )
    reveal_dir = output_dir / REVEAL_DIR
    reveal_dir.mkdir(parents=True, exist_ok=True)
    packet = {
        "schema_version": 1,
        "sample_id": sample_id,
        "family": sample.family.value,
        "cell": sample.cell,
        "step": sample.step,
        "blind": True,
        "revealed_at": datetime.now(UTC).isoformat(),
        "annotation": annotation,
        "judge": {
            "job_id": judge_output.get("job_id"),
            "job_status": judge_output.get("job_status"),
            "result_status": judge_output.get("result_status"),
            "draft": judge_output.get("draft"),
            "conclusion": judge_output.get("conclusion"),
        },
        "family_dispatch": judge_output.get("family_dispatch", []),
        "retrieval_audit": judge_output.get("retrieval_audit", []),
    }
    reveal_dir.joinpath(f"{sample_id}.json").write_text(
        json.dumps(packet, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    reveal_dir.joinpath(f"{sample_id}.md").write_text(
        _render_reveal_markdown(packet), "utf-8"
    )
    return packet


def _render_reveal_markdown(packet: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Revealed Judge Output — {packet['sample_id']}")
    lines.append("")
    lines.append(
        f"- family: {packet['family']} | cell: {packet['cell']} | step: {packet['step']}"
    )
    lines.append(f"- revealed_at: {packet.get('revealed_at')}")
    annotation = packet.get("annotation") or {}
    lines.append(
        f"- your annotation: **{annotation.get('verdict')}**"
        + (
            f" label={annotation.get('correct_label')}"
            if annotation.get("correct_label")
            else ""
        )
        + (f" notes={annotation.get('notes')}" if annotation.get("notes") else "")
    )
    lines.append("")
    judge = packet.get("judge") or {}
    draft = judge.get("draft") or {}
    dims = draft.get("dimensions") or {}
    reasons = draft.get("dimension_unknown_reasons") or {}
    if dims:
        lines.append("## Dimension scores")
        for dim, score in sorted(dims.items()):
            lines.append(f"- {dim}: {score}")
    if reasons:
        lines.append("## Dimension abstain reasons")
        for dim, reason in sorted(reasons.items()):
            lines.append(f"- {dim}: {reason}")
    if judge.get("conclusion"):
        lines.append(f"\n## Judge conclusion\n\n{judge.get('conclusion')}")
    dispatch = packet.get("family_dispatch") or []
    if dispatch:
        lines.append("\n## Family Dispatch Instructions")
        for rec in dispatch:
            if rec.get("status") != "accepted":
                continue
            lines.append(
                f"\n### {rec.get('dimension')}\n```\n{rec.get('instruction', '')}\n```"
            )
    audit = packet.get("retrieval_audit") or []
    if audit:
        lines.append("\n## Retrieval Audit Summary")
        for rec in audit:
            lines.append(
                f"- [{rec.get('agent')}] {rec.get('tool')} params={rec.get('params')} "
                f"refs={rec.get('returned_refs')} bytes={rec.get('bytes')} "
                f"truncated={rec.get('truncated')}"
            )
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# 人工标注导入（§7.3）
# ─────────────────────────────────────────────────────────────────────────────

#: 人工正确标签（correct 时给出，成为 ground truth）。
CorrectLabels = dict[str, str]


def _load_annotations(output_dir: Path) -> dict[str, Any]:
    path = output_dir / ANNOTATION_FILENAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("annotations", {})
    if isinstance(raw, list):
        return {ann["sample_id"]: ann for ann in raw}
    return dict(raw)


def validate_annotations(
    manifest: CalibrationManifest,
    annotations: list[dict[str, Any]],
    *,
    require_blind_complete: bool = True,
    require_all: bool = False,
) -> list[dict[str, Any]]:
    """校验人工标注：verdict 白名单 / correct 必须带 label / 其余禁 label /
    样本存在 / 无重复 / 盲测完整性 / 盲测顺序与 reveal 状态。

    盲测顺序：标注文件中的盲测条目相对顺序必须与清单盲测顺序一致
    （人工先独立判断、后揭晓的顺序约束）。reveal 状态：任何已 reveal 的盲测
    条目其标注仍必须存在且一致（校验由 import 侧持有 reveal 清单完成）。
    """
    known = {s.sample_id for s in manifest.samples}
    blind_order = manifest.blind_sample_ids()
    blind_index = {sid: i for i, sid in enumerate(blind_order)}
    seen: set[str] = set()
    blind_seen: list[str] = []
    for ann in annotations:
        sample_id = ann.get("sample_id")
        if sample_id not in known:
            raise AnnotationError(
                f"annotation references unknown sample_id {sample_id!r}"
            )
        if sample_id in seen:
            raise AnnotationError(f"duplicate annotation for sample {sample_id!r}")
        seen.add(sample_id)
        verdict = ann.get("verdict")
        if verdict not in VERDICTS:
            raise AnnotationError(
                f"sample {sample_id}: verdict {verdict!r} not in {sorted(VERDICTS)}"
            )
        correct_label = ann.get("correct_label")
        notes = ann.get("notes")
        if verdict == "correct":
            if not correct_label or not str(correct_label).strip():
                raise AnnotationError(
                    f"sample {sample_id}: verdict 'correct' requires a non-empty "
                    f"correct_label (the ground truth)"
                )
        else:
            if correct_label is not None:
                raise AnnotationError(
                    f"sample {sample_id}: verdict {verdict!r} must not carry a "
                    f"correct_label (no ground truth is claimed)"
                )
        if notes is not None and not isinstance(notes, str):
            raise AnnotationError(f"sample {sample_id}: notes must be a string or null")
        sample = manifest.sample(sample_id)
        if sample.blind:
            blind_seen.append(sample_id)
    if require_all:
        missing = [sample.sample_id for sample in manifest.samples if sample.sample_id not in seen]
        if missing:
            raise AnnotationError(
                f"all calibration samples must be annotated before statistics: missing "
                f"{missing}"
            )
    elif require_blind_complete:
        missing = [sid for sid in blind_order if sid not in seen]
        if missing:
            raise AnnotationError(
                f"blind samples must all be annotated before statistics: missing "
                f"{missing}"
            )
    if len(set(blind_seen)) != len(blind_seen):
        raise AnnotationError("duplicate blind annotation in file")
    file_order = [blind_index[sid] for sid in blind_seen]
    if file_order != sorted(file_order):
        raise AnnotationError(
            "blind samples must be annotated in manifest blind order "
            "(independent judgment before reveal)"
        )
    return annotations


def import_annotations(
    manifest: CalibrationManifest,
    output_dir: str | Path,
    annotations: list[dict[str, Any]],
    *,
    require_blind_complete: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """导入并持久化人工标注；与既有 reveal 状态一致性校验（force 覆盖）。"""
    output_dir = Path(output_dir)
    validated = validate_annotations(
        manifest, annotations, require_blind_complete=require_blind_complete
    )
    reveal_dir = output_dir / REVEAL_DIR
    if not force:
        for ann in validated:
            sample_id = ann["sample_id"]
            reveal_file = reveal_dir / f"{sample_id}.json"
            if reveal_file.exists():
                revealed = json.loads(reveal_file.read_text(encoding="utf-8"))
                if (revealed.get("annotation") or {}).get("verdict") != ann.get(
                    "verdict"
                ):
                    raise AnnotationError(
                        f"sample {sample_id} was already revealed with a different "
                        f"annotation; pass force=True to overwrite"
                    )
    payload = {
        "schema_version": 1,
        "annotations": {ann["sample_id"]: ann for ann in validated},
    }
    (output_dir / ANNOTATION_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# 冻结统计与诊断 verdict（§7.4）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricResult:
    """一条冻结指标的透明结果。ratio/denominator 可空（分母为 0）。"""

    name: str
    numerator: int
    denominator: int | None
    ratio: float | None
    threshold: float | None
    met: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": self.ratio,
            "threshold": self.threshold,
            "met": self.met,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CalibrationStatistics:
    totals: dict[str, int]
    blind_agreement: MetricResult
    correction_rate: MetricResult
    abstain_honesty: MetricResult
    dimension_coverage: dict[str, int]
    verdict_met: bool
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "blind_agreement": self.blind_agreement.to_dict(),
            "correction_rate": self.correction_rate.to_dict(),
            "abstain_honesty": self.abstain_honesty.to_dict(),
            "dimension_coverage": dict(self.dimension_coverage),
            "verdict": {
                "met": self.verdict_met,
                "scope": "diagnostic only; judge output never alters any gate",
            },
            "diagnostics": self.diagnostics,
        }


def _human_verdict_map(annotations: list[dict[str, Any]]) -> dict[str, str]:
    return {ann["sample_id"]: ann["verdict"] for ann in annotations}


def compute_statistics(
    manifest: CalibrationManifest,
    judge_outputs: dict[str, Any],
    annotations: list[dict[str, Any]],
) -> CalibrationStatistics:
    """§7.4 冻结门槛的精确计算。

    - blind agreement：6 条盲测中 (human accept ∧ judge clean) ∨ (human correct
      ∧ judge flagged) 的条数 / 6（human unknown / judge abstain / 无结论均计
      不一致——不猜）。
    - correction rate：22 条非盲中 human==correct 条数 / 22（≤5/22）。
    - abstain honesty：judge 全 abstain 的样本中 human==unknown 的比例（≥50%）；
      无 abstain → vacuous satisfied（但明确报告）。
    - dimension coverage：8 维度每维 ≥2 条非 abstain。
    verdict 仅诊断（`diagnostic only`），绝不进 gate / aggregate。
    """
    validate_annotations(manifest, annotations, require_all=True)
    verdict_map = _human_verdict_map(annotations)

    def _judge(doc: dict[str, Any]) -> dict[str, Any] | None:
        return doc.get("draft")

    def _is_abstained(doc: dict[str, Any]) -> bool:
        draft = _judge(doc)
        if draft is None:
            return False
        return not (draft.get("dimensions") or {})

    blind_agreements = 0
    abstain_total = 0
    abstain_confirmed = 0
    corrections = 0
    dim_scored: dict[str, int] = {d: 0 for d in ALL_DIMENSIONS}
    for sample in manifest.samples:
        doc = judge_outputs.get(sample.sample_id) or {}
        human = verdict_map.get(sample.sample_id)
        conclusion = doc.get("conclusion")
        draft = _judge(doc)
        # dimension coverage：该样本所属家族维度中非 abstain 的计数。
        family_dims = (
            OBSERVATION_DIMENSIONS
            if sample.family is c.SampleTargetType.OBSERVATION
            else DISPATCH_DIMENSIONS
        )
        for dim in family_dims:
            dims = (draft or {}).get("dimensions") or {}
            if dim in dims:
                dim_scored[dim] += 1
        if sample.blind:
            if (
                human == "accept"
                and conclusion is not None
                and conclusion["state"] == "clean"
            ) or (
                human == "correct"
                and conclusion is not None
                and conclusion["state"] == "flagged"
            ):
                blind_agreements += 1
        else:
            if human == "correct":
                corrections += 1
        if _is_abstained(doc):
            abstain_total += 1
            if human == "unknown":
                abstain_confirmed += 1

    blind_agreement = MetricResult(
        name="blind_agreement",
        numerator=blind_agreements,
        denominator=BLIND_DENOM,
        ratio=round(blind_agreements / BLIND_DENOM, 6),
        threshold=BLIND_AGREEMENT_MIN / BLIND_DENOM,
        met=blind_agreements >= BLIND_AGREEMENT_MIN,
        detail=(
            f"blind independent agreement: judge conclusion matches the human's "
            f"independent judgment on {blind_agreements}/{BLIND_DENOM} blind samples"
        ),
    )
    correction = MetricResult(
        name="correction_rate",
        numerator=corrections,
        denominator=NON_BLIND_DENOM,
        ratio=round(corrections / NON_BLIND_DENOM, 6),
        threshold=NON_BLIND_CORRECTION_MAX / NON_BLIND_DENOM,
        met=corrections <= NON_BLIND_CORRECTION_MAX,
        detail=(
            f"judge-first correction: human marked {corrections}/{NON_BLIND_DENOM} "
            f"non-blind samples 'correct'"
        ),
    )
    if abstain_total:
        honesty_ratio = round(abstain_confirmed / abstain_total, 6)
        honesty_met = honesty_ratio >= ABSTAIN_HONESTY_MIN
        honesty_detail = (
            f"judge abstained on {abstain_total} samples; human confirmed "
            f"'cannot judge' on {abstain_confirmed} of them"
        )
    else:
        honesty_ratio = None
        honesty_met = True
        honesty_detail = (
            "judge abstained on 0 samples; abstain-honesty requirement is "
            "vacuously satisfied (explicitly reported, not assumed)"
        )
    honesty = MetricResult(
        name="abstain_honesty",
        numerator=abstain_confirmed,
        denominator=abstain_total,
        ratio=honesty_ratio,
        threshold=ABSTAIN_HONESTY_MIN,
        met=honesty_met,
        detail=honesty_detail,
    )
    coverage_met = all(v >= DIMENSION_MIN_NON_ABSTAIN for v in dim_scored.values())
    verdict_met = bool(
        blind_agreement.met and correction.met and honesty.met and coverage_met
    )
    return CalibrationStatistics(
        totals={
            "observation": OBSERVATION_TOTAL,
            "dispatch": DISPATCH_TOTAL,
            "total": CALIBRATION_TOTAL,
            "blind": BLIND_TOTAL,
            "blind_observation": BLIND_OBSERVATION,
            "blind_dispatch": BLIND_DISPATCH,
        },
        blind_agreement=blind_agreement,
        correction_rate=correction,
        abstain_honesty=honesty,
        dimension_coverage=dim_scored,
        verdict_met=verdict_met,
        diagnostics={
            "abstain_vacuous": abstain_total == 0,
            "per_sample": {
                s.sample_id: {
                    "family": s.family.value,
                    "blind": s.blind,
                    "human_verdict": verdict_map.get(s.sample_id),
                    "judge_conclusion": (judge_outputs.get(s.sample_id) or {}).get(
                        "conclusion"
                    ),
                }
                for s in manifest.samples
            },
        },
    )


def write_statistics(
    stats: CalibrationStatistics, output_dir: str | Path
) -> tuple[Path, Path]:
    """透明 JSON + Markdown 统计；verdict 保持诊断，不触碰任何 gate。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / STATISTICS_JSON_FILENAME
    md_path = output_dir / STATISTICS_MD_FILENAME
    json_path.write_text(
        json.dumps(stats.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    md_lines: list[str] = [
        "# Calibration Statistics",
        "",
        (
            f"- total samples: {stats.totals['total']} "
            f"(observation {stats.totals['observation']} / "
            f"dispatch {stats.totals['dispatch']})"
        ),
        (
            f"- blind: {stats.totals['blind']} "
            f"(observation {stats.totals['blind_observation']} / "
            f"dispatch {stats.totals['blind_dispatch']})"
        ),
        "",
        "## Metrics",
        "",
        "| metric | value | threshold | met |",
        "|---|---|---|---|",
    ]

    def _row(m: MetricResult) -> str:
        ratio = "—" if m.ratio is None else f"{m.ratio:.3f}"
        thr = "—" if m.threshold is None else f"{m.threshold:.3f}"
        return f"| {m.name} | {ratio} ({m.numerator}/{m.denominator}) | {thr} | {'YES' if m.met else 'NO'} |"

    md_lines.append(_row(stats.blind_agreement))
    md_lines.append(_row(stats.correction_rate))
    md_lines.append(_row(stats.abstain_honesty))
    md_lines.append("")
    md_lines.append("### Dimension coverage (non-abstain samples per dimension)")
    md_lines.append("")
    md_lines.append("| dimension | non-abstain samples | >= 2 |")
    md_lines.append("|---|---|---|")
    for dim, count in stats.dimension_coverage.items():
        md_lines.append(
            f"| {dim} | {count} | {'YES' if count >= DIMENSION_MIN_NON_ABSTAIN else 'NO'} |"
        )
    md_lines.append("")
    md_lines.append("## Verdict")
    md_lines.append("")
    md_lines.append(
        f"- all frozen thresholds met: **{'YES' if stats.verdict_met else 'NO'}**"
    )
    md_lines.append(
        "- **diagnostic only**: this verdict never alters any gate. Judge output "
        "stays a diagnostic signal until fresh review and explicit user approval."
    )
    md_lines.append("")
    for m in (stats.blind_agreement, stats.correction_rate, stats.abstain_honesty):
        md_lines.append(f"- **{m.name}**: {m.detail}")
    md_path.write_text("\n".join(md_lines) + "\n", "utf-8")
    return json_path, md_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI（`python -m sar_orch.eval.calibrate`；不影响默认 evaluator CLI）
# ─────────────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description="SAR Eval judge-first calibration (sampling / drive / packets / stats)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser(
        "sample", help="stratified sample → calibration_manifest.json"
    )
    p_sample.add_argument("--cohort", type=str, default=str(DEFAULT_COHORT_ROOT))
    p_sample.add_argument("--output", type=str, required=True)
    p_sample.add_argument("--seed", type=int, default=CALIBRATION_SEED)

    p_drive = sub.add_parser("drive", help="run attempt-v2 workflow (v2 rubrics only)")
    p_drive.add_argument("--manifest", type=str, required=True)
    p_drive.add_argument("--output", type=str, required=True)
    p_drive.add_argument("--model", type=str, default=None)
    p_drive.add_argument("--provider", type=str, default=None)
    p_drive.add_argument("--api-base", type=str, default=None)
    p_drive.add_argument("--api-key", type=str, default=None)
    p_drive.add_argument("--timeout", type=float, default=None)

    p_packets = sub.add_parser("packets", help="build per-sample review packets")
    p_packets.add_argument("--manifest", type=str, required=True)
    p_packets.add_argument("--output", type=str, required=True)

    p_reveal = sub.add_parser("reveal", help="reveal one blind sample's judge output")
    p_reveal.add_argument("--manifest", type=str, required=True)
    p_reveal.add_argument("--output", type=str, required=True)
    p_reveal.add_argument("--sample", type=str, required=True)

    p_annotate = sub.add_parser(
        "annotate", help="validate and save human annotations without computing statistics"
    )
    p_annotate.add_argument("--manifest", type=str, required=True)
    p_annotate.add_argument("--output", type=str, required=True)
    p_annotate.add_argument("--annotations", type=str, required=True)

    p_stats = sub.add_parser("stats", help="import annotations + compute statistics")
    p_stats.add_argument("--manifest", type=str, required=True)
    p_stats.add_argument("--output", type=str, required=True)
    p_stats.add_argument("--annotations", type=str, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            manifest = sample_cohort(args.cohort, seed=args.seed)
            path = write_manifest(manifest, Path(args.output) / MANIFEST_FILENAME)
            print(
                f"wrote {path} ({len(manifest.samples)} samples, "
                f"{len(manifest.blind_sample_ids())} blind)"
            )
            return 0
        if args.command == "drive":
            manifest = load_manifest(args.manifest)
            env_vars = _load_project_env()
            model_config = ModelConfig(
                model=args.model or env_vars.get("model", ""),
                provider=args.provider or env_vars.get("provider", "openai"),
                api_base=args.api_base
                or env_vars.get("api_base", "https://api.deepseek.com"),
                api_key=args.api_key or env_vars.get("api_key"),
            )
            driver = CalibrationDriver(
                DEFAULT_COHORT_ROOT,
                args.output,
                manifest,
                model_config=model_config,
                timeout_s=args.timeout,
            )
            state = driver.drive()
            print(f"drove {len(state['cells'])} cells → {args.output}")
            return 0
        if args.command == "packets":
            manifest = load_manifest(args.manifest)
            packets = build_review_packets(manifest, args.output)
            print(f"wrote {len(packets)} review packets → {args.output}/packets")
            return 0
        if args.command == "reveal":
            manifest = load_manifest(args.manifest)
            reveal_blind(manifest, args.output, args.sample)
            print(f"revealed {args.sample} → {args.output}/{REVEAL_DIR}")
            return 0
        if args.command == "annotate":
            manifest = load_manifest(args.manifest)
            raw = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                inner = raw.get("annotations", raw)
                annotations = list(inner.values()) if isinstance(inner, dict) else list(inner)
            else:
                annotations = list(raw)
            import_annotations(manifest, args.output, annotations)
            print(f"saved {len(annotations)} human annotations → {args.output}")
            return 0
        if args.command == "stats":
            manifest = load_manifest(args.manifest)
            annotations = []
            if args.annotations:
                raw = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    inner = raw.get("annotations", raw)
                    annotations = (
                        list(inner.values()) if isinstance(inner, dict) else list(inner)
                    )
                else:
                    annotations = list(raw)
                import_annotations(manifest, args.output, annotations)
            else:
                annotations = list(_load_annotations(Path(args.output)).values())
            outputs = _load_outputs(Path(args.output))
            stats = compute_statistics(manifest, outputs, annotations)
            write_statistics(stats, args.output)
            print(
                f"wrote {STATISTICS_JSON_FILENAME} / {STATISTICS_MD_FILENAME} "
                f"(verdict_met={stats.verdict_met})"
            )
            return 0
    except CalibrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(_main())
