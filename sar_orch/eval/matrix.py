"""P6 matrix typed contracts：P6SourceSpec / MatrixManifest / TraceProjection。

设计 §0.6-2/4/5、§3.2、§9 P6.1-2。本模块是契约与确定性计算层：

- `P6SourceSpec`：人工冻结的 selector；source_rel 只能是相对路径，拒绝绝对/`..`/
  glob/latest、缺失/重复/替代；七格 tuple 唯一。
- `MatrixManifest`：preparer 的 canonical 输出；**不内嵌自身 hash**
  （`matrix_manifest_sha256` 由 orchestration 写入独立 summary）。
- raw provenance 拆成 `raw_input_digest`（九类 executable inputs + logical
  `supervision_state={present,empty}`）与 `raw_trace_digest`（固定排序的
  source-owned NDJSON inventory，排除 evaluator-owned output）；
  `raw_provenance_digest` 是两者及 source-spec digest 的 canonical tuple hash。
- `TraceProjection`：只抽取严格白名单 framework error code 计数，绝不保存 NDJSON
  正文；绑定 staged semantic digest。
- `semantic_stage_cell()`：把 source 按 policy 物化为 0700 private work root 下的
  staged 目录（九类输入 + 空 `supervision/` + canonical `supervision_state.json`）。
  source 端非 allowlist 的 metadata key / CSV 列剥离并记录名称（排序、仅名称），
  写入 `MatrixCellManifest`（审计）；fail-closed 落在 staged 产物（defense_scan）。

本模块不读取 workflow / LLM / SAR 运行时。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import p6_staging_policy as policy

#: 九类 executable inputs（与 workflow `_SOURCE_FILE_NAMES` 的既有九项一致）。
EXECUTABLE_SOURCE_FILES = (
    "metadata.json",
    "trajectory.csv",
    "router_interactions.csv",
    "subtasks.csv",
    "agent_interactions.csv",
    "summary.csv",
    "token_usage.csv",
    "semantic_map.jsonl",
    "map_summary.jsonl",
)

#: P6 固定 staged 文件集 = 九类 staged inputs + supervision_state.json sentinel。
#: workflow `_SOURCE_FILE_NAMES` 会被修改为同一集合（无 sentinel 的 legacy 兼容）。
SUPERVISION_STATE_FILENAME = policy.SUPERVISION_STATE_FILENAME
STAGED_FILE_NAMES = EXECUTABLE_SOURCE_FILES + (SUPERVISION_STATE_FILENAME,)

#: source framework error taxonomy 白名单（设计 §0.6-5 / §9 P6.3）。
TARGET_SOURCE_ERROR_CODES = ("worker_busy", "task_not_routable_yet", "unknown_task_id")
SAFE_SOURCE_ERROR_CODES = ("no_worker", "participant_busy", "node_not_found")
SOURCE_ERROR_CODE_SET = frozenset(TARGET_SOURCE_ERROR_CODES + SAFE_SOURCE_ERROR_CODES)

#: harness error buckets（§9 P6.3）。任何非零都使 P6 失败。
HARNESS_ERROR_BUCKETS = (
    "manifest_validation",
    "source_provenance_mismatch",
    "semantic_staging",
    "workflow_execution",
    "artifact_verification",
    "unexpected_model_construction",
    "cleanup_failure",
)

#: grader skip taxonomy 代码（§0.6-7 / §9 P6.1）。
SUPERVISION_MISSING_OR_EMPTY = "supervision_missing_or_empty"
TOOL_EXECUTION_REPRESENTATIVE_ERROR = "tool_execution_representative_error"


def normalize_grader_skip_reason(reason: str) -> str:
    """把 `load_episode` grader_skips 的 reason 归一化为 taxonomy 代码。

    仅识别已知诊断代码；未知 reason 归入 `unclassified_grader_skip`（不静默丢弃）。
    """
    if "supervision/ missing or empty" in reason:
        return SUPERVISION_MISSING_OR_EMPTY
    if "representative row is a tool-execution error" in reason:
        return TOOL_EXECUTION_REPRESENTATIVE_ERROR
    return "unclassified_grader_skip"


def grader_skip_taxonomy_from_episode(episode: Any) -> dict[str, int]:
    """从 episode.grader_skips 生成 normalized taxonomy（确定性）。"""
    out: dict[str, int] = {}
    for skip in getattr(episode, "grader_skips", []) or []:
        reason = skip.get("reason", "") if isinstance(skip, dict) else str(skip)
        code = normalize_grader_skip_reason(reason)
        out[code] = out.get(code, 0) + 1
    return out


def empty_harness_error_counts() -> dict[str, int]:
    return {bucket: 0 for bucket in HARNESS_ERROR_BUCKETS}


# ─────────────────────────────────────────────────────────────────────────────
# P6SourceSpec：人工冻结 selector
# ─────────────────────────────────────────────────────────────────────────────


_SOURCE_REL_GLOB_RE = re.compile(r"[*?\[\]]")


def validate_source_rel(source_rel: str) -> str:
    """source_rel 必须是相对路径：拒绝绝对、`..`、`.`、glob/latest、空段/反斜杠。"""
    if not isinstance(source_rel, str) or not source_rel:
        raise ValueError("source_rel must be a non-empty string")
    if source_rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", source_rel):
        raise ValueError(f"source_rel must be relative: {source_rel!r}")
    if "\\" in source_rel:
        raise ValueError("source_rel must use '/' separators")
    if _SOURCE_REL_GLOB_RE.search(source_rel):
        raise ValueError(f"source_rel must not contain glob patterns: {source_rel!r}")
    segments = source_rel.split("/")
    for seg in segments:
        if not seg:
            raise ValueError(
                f"source_rel must be canonical (no empty segments): {source_rel!r}"
            )
        if seg in (".", ".."):
            raise ValueError(f"source_rel must not contain '.' or '..': {source_rel!r}")
        if seg.lower() in ("latest",):
            raise ValueError(f"source_rel must not select 'latest': {source_rel!r}")
    return source_rel


class P6CellSpec(BaseModel):
    """P6 单格：相对 source_rel + cell tuple + 预期 metadata/source/grader taxonomy。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: str
    source_rel: str
    scene: int
    agents: int
    seed: int
    repetition: str

    expected_metadata_fingerprint: dict[str, Any]
    expected_source_error_taxonomy: dict[str, int]
    expected_grader_skip_taxonomy: dict[str, int]

    @field_validator("source_rel")
    @classmethod
    def _source_rel(cls, value: str) -> str:
        return validate_source_rel(value)

    @field_validator("expected_source_error_taxonomy")
    @classmethod
    def _taxonomy_nonnegative(cls, value: dict[str, int]) -> dict[str, int]:
        if any(v < 0 for v in value.values()):
            raise ValueError("expected_source_error_taxonomy counts must be >= 0")
        return value

    @field_validator("expected_grader_skip_taxonomy")
    @classmethod
    def _skip_nonnegative(cls, value: dict[str, int]) -> dict[str, int]:
        if any(v < 0 for v in value.values()):
            raise ValueError("expected_grader_skip_taxonomy counts must be >= 0")
        return value

    @model_validator(mode="after")
    def _check_cell_identity(self) -> P6CellSpec:
        """cell_id 必须与 source_rel 内嵌身份一致（拒绝替代/substitution）。

        cell_id 形如 `s1_a2`；source_rel 最后一个段形如 `s1_s42_a2_r1`。
        不匹配即视为 selector 替代 → fail-closed。
        """
        m = re.match(r"^s(\d+)_a(\d+)$", self.cell_id)
        if not m:
            raise ValueError(f"cell_id must match s<scene>_a<agents>: {self.cell_id!r}")
        scene, agents = int(m.group(1)), int(m.group(2))
        last = self.source_rel.rsplit("/", 1)[-1]
        expected = f"s{scene}_s{self.seed}_a{agents}_{self.repetition}"
        if last != expected:
            raise ValueError(
                f"cell_id/source_rel mismatch (substitution rejected): "
                f"{self.cell_id!r} points at source {last!r}, expected {expected!r}"
            )
        return self

    @property
    def subject_tuple(self) -> tuple[int, int, int, str]:
        return (self.scene, self.agents, self.seed, self.repetition)


class P6SourceSpec(BaseModel):
    """人工冻结的 selector。preparer/verifier 都不得重选、替换或补格。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    cohort: str
    cells: list[P6CellSpec]

    @field_validator("cohort")
    @classmethod
    def _cohort(cls, value: str) -> str:
        if not value or value.startswith("/") or ".." in value.split("/"):
            raise ValueError(f"cohort must be a simple relative name: {value!r}")
        return value

    @model_validator(mode="after")
    def _check_cells(self) -> P6SourceSpec:
        if not self.cells:
            raise ValueError("P6SourceSpec must have at least one cell")
        source_rels = [cell.source_rel for cell in self.cells]
        if len(set(source_rels)) != len(source_rels):
            raise ValueError("duplicate source_rel in P6SourceSpec cells")
        tuples = [cell.subject_tuple for cell in self.cells]
        if len(set(tuples)) != len(tuples):
            raise ValueError("duplicate cell tuple (scene, agents, seed, repetition)")
        cell_ids = [cell.cell_id for cell in self.cells]
        if len(set(cell_ids)) != len(cell_ids):
            raise ValueError("duplicate cell_id in P6SourceSpec cells")
        if self.cohort == P6_COHORT and set(cell_ids) != set(P6_CELL_IDS):
            raise ValueError(
                f"P6SourceSpec for cohort {P6_COHORT!r} must contain exactly the "
                f"seven cells {list(P6_CELL_IDS)}; got {sorted(cell_ids)}"
            )
        return self

    def digest(self) -> str:
        return c.sha256_hex(self.canonical_bytes())

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    def canonical_json(self) -> str:
        return c.canonical_json(self.model_dump(mode="json"))


def load_source_spec(path: str | Path) -> P6SourceSpec:
    """从外部 JSON 文件加载 P6SourceSpec（人工冻结的 selector）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return P6SourceSpec.model_validate(raw)


#: 默认 7-cell source spec（§0.6-1）。逐 cell taxonomy 来自实际 cohort 实测。
P6_COHORT = "20260804_234045_eval_diag_full"
P6_CELL_IDS = ("s1_a2", "s2_a2", "s3_a2", "s3_a4", "s4_a2", "s5_a2", "s5_a4")


def _cell_source_rel(cell_id: str) -> str:
    scene, agents = cell_id.split("_")
    return f"{P6_COHORT}/s{scene[1:]}_s42_a{agents[1:]}_r1"


def default_source_spec() -> P6SourceSpec:
    """冻结的默认 7-cell P6SourceSpec。"""
    metadata_fp = {
        "prompt_version": "eval_diag_full",
        "code_commit": "4d6d5e7-dirty",
        "max_steps": 30,
        "seed": 42,
    }
    zero_tax = {code: 0 for code in TARGET_SOURCE_ERROR_CODES}
    zero_tax.update({code: 0 for code in SAFE_SOURCE_ERROR_CODES})
    zero_tax["unclassified_nonempty_error_count"] = 0

    # 逐 cell target/safe/unclassified 计数（实际 cohort 实测值；§0.6-5 冻结）。
    per_cell_source = {
        "s1_a2": {},
        "s2_a2": {"unknown_task_id": 1, "no_worker": 1, "participant_busy": 1},
        "s3_a2": {},
        "s3_a4": {},
        "s4_a2": {"no_worker": 3, "participant_busy": 1},
        "s5_a2": {
            "unknown_task_id": 1,
            "no_worker": 11,
            "participant_busy": 3,
            "unclassified_nonempty_error_count": 3,
        },
        "s5_a4": {
            "unknown_task_id": 1,
            "no_worker": 15,
            "node_not_found": 1,
            "unclassified_nonempty_error_count": 2,
        },
    }
    per_cell_skip = {
        "s1_a2": {"supervision_missing_or_empty": 1},
        "s2_a2": {"supervision_missing_or_empty": 1},
        "s3_a2": {"supervision_missing_or_empty": 1},
        "s3_a4": {"supervision_missing_or_empty": 1},
        "s4_a2": {"supervision_missing_or_empty": 1},
        "s5_a2": {
            "supervision_missing_or_empty": 1,
            "tool_execution_representative_error": 1,
        },
        "s5_a4": {"supervision_missing_or_empty": 1},
    }

    cells = []
    for cell_id in P6_CELL_IDS:
        scene, agents = cell_id.split("_")
        src = dict(zero_tax)
        src.update(per_cell_source.get(cell_id, {}))
        cells.append(
            P6CellSpec(
                cell_id=cell_id,
                source_rel=_cell_source_rel(cell_id),
                scene=int(scene[1:]),
                agents=int(agents[1:]),
                seed=42,
                repetition="r1",
                expected_metadata_fingerprint=dict(metadata_fp),
                expected_source_error_taxonomy=src,
                expected_grader_skip_taxonomy=dict(per_cell_skip[cell_id]),
            )
        )
    return P6SourceSpec(cohort=P6_COHORT, cells=cells)


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 digest 计算
# ─────────────────────────────────────────────────────────────────────────────


def _canonical_hash(data: dict[str, Any]) -> str:
    return c.sha256_hex(c.canonical_json(data).encode("utf-8"))


def raw_input_digest(source_dir: Path) -> str:
    """raw_input_digest：九类 executable inputs 的 raw bytes hash + logical
    `supervision_state={present,empty}`（§3.2）。"""
    files: dict[str, str] = {}
    for rel in EXECUTABLE_SOURCE_FILES:
        target = source_dir / rel
        if not target.exists():
            raise ValueError(f"missing executable input: {rel}")
        if not target.is_file():
            raise ValueError(f"executable input not a regular file: {rel}")
        files[rel] = c.sha256_hex(target.read_bytes())
    sup_dir = source_dir / "supervision"
    present = sup_dir.exists()
    empty = present and not any(sup_dir.iterdir())
    return raw_input_digest_from_files(files, {"present": present, "empty": empty})


def raw_input_digest_from_files(
    files: dict[str, str], supervision_state: dict[str, bool]
) -> str:
    return _canonical_hash({"files": files, "supervision_state": supervision_state})


def _source_owned_ndjson_inventory(source_dir: Path) -> list[str]:
    """固定、排序的 source-owned NDJSON inventory：排除 evaluator-owned output
    （eval_attempts / eval_workspace 保留目录与 root eval_report）。"""
    rels: list[str] = []
    source_dir = source_dir.resolve()
    for ndjson in sorted(source_dir.rglob("*.ndjson")):
        rel = str(ndjson.relative_to(source_dir))
        first = rel.split("/", 1)[0]
        if first in ("eval_attempts", "eval_workspace"):
            continue
        rels.append(rel)
    return rels


def raw_trace_digest(source_dir: Path) -> tuple[str, dict[str, str]]:
    """raw_trace_digest：source-owned NDJSON inventory 的 canonical hash。

    返回 (digest, {rel: sha256})，后者写入 manifest 供 verifier 无 source 重算。
    """
    inventory = _source_owned_ndjson_inventory(source_dir)
    files = {rel: c.sha256_hex((source_dir / rel).read_bytes()) for rel in inventory}
    return raw_trace_digest_from_files(files), files


def raw_trace_digest_from_files(files: dict[str, str]) -> str:
    return _canonical_hash({"files": files})


def raw_provenance_digest(
    source_spec_digest: str, raw_input_digest_: str, raw_trace_digest_: str
) -> str:
    """raw_provenance_digest = (source_spec_digest, raw_input, raw_trace) tuple hash。"""
    return _canonical_hash(
        {
            "source_spec_digest": source_spec_digest,
            "raw_input_digest": raw_input_digest_,
            "raw_trace_digest": raw_trace_digest_,
        }
    )


def supervision_state_digest() -> str:
    return c.sha256_hex(policy.supervision_state_bytes())


def staged_semantic_digest(staged_dir: Path) -> str:
    """staged_semantic_digest：九类 staged inputs + supervision_state.json 的
    `snapshot_source_files` digest，必须等于 workflow frozen subject.source_digest。"""
    staged_dir = Path(staged_dir)
    allowlist = [name for name in STAGED_FILE_NAMES if (staged_dir / name).exists()]
    snapshot = a.snapshot_source_files(staged_dir, allowlist)
    return snapshot.digest


# ─────────────────────────────────────────────────────────────────────────────
# TraceProjection：只抽错误计数，绝不保存 NDJSON 正文
# ─────────────────────────────────────────────────────────────────────────────


def count_source_framework_errors(
    ndjson_paths: list[Path],
) -> tuple[dict[str, int], int]:
    """从 NDJSON 记录抽取白名单 framework error code 计数。

    只统计 `error` 字段（顶层或嵌套）的非空字符串；`SOURCE_ERROR_CODE_SET` 内的
    归入对应 code，其余计入 `unclassified_nonempty_error_count`。NDJSON 正文绝不落盘。
    """
    counts: dict[str, int] = {code: 0 for code in TARGET_SOURCE_ERROR_CODES}
    counts.update({code: 0 for code in SAFE_SOURCE_ERROR_CODES})
    unclassified = 0

    def walk(value: Any) -> None:
        nonlocal unclassified
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "error" and isinstance(v, str) and v.strip():
                    code = v.strip()
                    if code in SOURCE_ERROR_CODE_SET:
                        counts[code] = counts.get(code, 0) + 1
                    else:
                        unclassified += 1
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in ndjson_paths:
        for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            walk(record)
    return counts, unclassified


class TraceProjection(BaseModel):
    """脱敏 projection：kind + inventory + 白名单计数 + staged digest 绑定。

    只保存计数/文件清单，不保存 NDJSON 正文。`projection_digest` 是 payload 的
    canonical hash（构建时计算并持久化；verifier 重算以检测 manifest 篡改）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str  # "worker_trace" | "coordinator_dispatch"
    inventory: list[str]
    error_counts: dict[str, int]
    unclassified_nonempty_error_count: int
    staged_semantic_digest: str
    projection_digest: str = ""

    def projection_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "inventory": self.inventory,
            "error_counts": self.error_counts,
            "unclassified_nonempty_error_count": self.unclassified_nonempty_error_count,
            "staged_semantic_digest": self.staged_semantic_digest,
        }

    def digest(self) -> str:
        return _canonical_hash(self.projection_payload())


def build_trace_projections(
    source_dir: Path, staged_semantic_digest_: str
) -> list[TraceProjection]:
    """按 source-owned NDJSON inventory 生成 worker/coordinator 两个 projection。

    worker_trace = 落在 `workers/` 下；coordinator_dispatch = 其余（coordinator/
    root events 等）。每个 projection 只含白名单计数，绑定 staged digest。
    """
    inventory = _source_owned_ndjson_inventory(source_dir)
    worker_rels = [rel for rel in inventory if rel.split("/", 1)[0] == "workers"]
    coord_rels = [rel for rel in inventory if rel not in worker_rels]

    projections = []
    for kind, rels in (
        ("worker_trace", worker_rels),
        ("coordinator_dispatch", coord_rels),
    ):
        counts, unclassified = count_source_framework_errors(
            [source_dir / rel for rel in rels]
        )
        proj = TraceProjection(
            kind=kind,
            inventory=rels,
            error_counts=counts,
            unclassified_nonempty_error_count=unclassified,
            staged_semantic_digest=staged_semantic_digest_,
        )
        proj.projection_digest = proj.digest()
        projections.append(proj)
    return projections


def aggregate_source_framework_counts(
    projections: list[TraceProjection],
) -> dict[str, int]:
    """cell 级 source_framework_error_counts = 各 projection 计数 + unclassified。"""
    out: dict[str, int] = {}
    for proj in projections:
        for code, count in proj.error_counts.items():
            out[code] = out.get(code, 0) + count
        out["unclassified_nonempty_error_count"] = (
            out.get("unclassified_nonempty_error_count", 0)
            + proj.unclassified_nonempty_error_count
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# semantic staging：物理 materialize
# ─────────────────────────────────────────────────────────────────────────────


def _mkprivate(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def stage_cell_physical(
    spec: P6SourceSpec,
    cell: P6CellSpec,
    source_dir: Path,
    staged_dir: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """把一个 cell 的 source 按 policy 物化为 staged_dir（mode 0700，无
    symlink/hardlink）。只复制 policy 允许的结构化输入 + 空 supervision/ + sentinel。

    返回 `(stripped_metadata_keys, stripped_csv_columns)`：source 端被剥离并记录的
    非 allowlist metadata key 名 / CSV 列名（排序、仅名称），供写入 manifest 审计。
    staged 产物的 fail-closed 由 `assert_staged_metadata_keys` / `defense_scan`
    承担（staged metadata 非九字段、staged CSV 非 allowlist 列均违规）。
    """
    _mkprivate(staged_dir)
    stripped_metadata_keys: list[str] = []
    stripped_csv_columns: dict[str, list[str]] = {}
    for rel in EXECUTABLE_SOURCE_FILES:
        src_path = source_dir / rel
        if not src_path.exists():
            raise policy.SemanticStagingError(f"missing executable input: {rel}")
        if not src_path.is_file() or src_path.is_symlink():
            raise policy.SemanticStagingError(f"input not regular file: {rel}")
        content = src_path.read_bytes()
        if rel == "metadata.json":
            staged = json.loads(content.decode("utf-8"))
            out, stripped_metadata_keys = policy.stage_metadata(staged)
            policy.assert_staged_metadata_keys(out)
            content = json.dumps(out, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        else:
            content, stripped = policy.stage_structured_file(rel, content)
            if stripped:
                stripped_csv_columns[rel] = stripped
        target = staged_dir / rel
        target.write_bytes(content)
        os.chmod(target, 0o600)
    # 空 supervision/ + canonical supervision_state.json
    sup_dir = staged_dir / "supervision"
    _mkprivate(sup_dir)
    sentinel = staged_dir / SUPERVISION_STATE_FILENAME
    sentinel.write_bytes(policy.supervision_state_bytes())
    os.chmod(sentinel, 0o600)
    return stripped_metadata_keys, stripped_csv_columns


# ─────────────────────────────────────────────────────────────────────────────
# MatrixManifest：preparer 的 canonical 输出（不内嵌自身 hash）
# ─────────────────────────────────────────────────────────────────────────────


class MatrixCellManifest(BaseModel):
    """单格 manifest：cell 身份/tuple、raw→policy→staged 四组 digest、文件级 hash、
    staged path（相对 work root）、projections、expected taxonomies。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: str
    source_rel: str
    subject_tuple: dict[str, int | str]
    metadata_fingerprint: dict[str, Any]

    source_spec_digest: str
    raw_input_digest: str
    raw_trace_digest: str
    raw_provenance_digest: str
    redaction_policy_digest: str
    staged_semantic_digest: str
    supervision_state_digest: str

    raw_input_files: dict[str, str]
    raw_trace_files: dict[str, str]
    staged_files: dict[str, str]
    supervision_state: dict[str, bool]

    trajectory_regular: bool
    staged_path: str

    projections: list[TraceProjection]
    source_framework_error_counts: dict[str, int]
    expected_source_error_taxonomy: dict[str, int]
    expected_grader_skip_taxonomy: dict[str, int]

    #: 审计：staging 时从 source 剥离并记录的 metadata key 名 / CSV 列名
    #: （排序、仅名称，不保存值）。默认空以兼容旧 manifest（manifest 不内嵌
    #: 自身 hash、保持 canonical JSON）。
    stripped_metadata_keys: list[str] = Field(default_factory=list)
    stripped_csv_columns: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def scene(self) -> int:
        return int(self.subject_tuple["scene"])

    @property
    def agents(self) -> int:
        return int(self.subject_tuple["agents"])

    @property
    def seed(self) -> int:
        return int(self.subject_tuple["seed"])


class MatrixManifest(BaseModel):
    """canonical manifest。字段固定；**不内嵌自身 SHA-256**。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    manifest_version: str = "p6-matrix-manifest-v1"
    cohort: str
    work_root: str
    source_spec_digest: str
    redaction_policy_digest: str
    cells: list[MatrixCellManifest]

    def canonical_bytes(self) -> bytes:
        return c.canonical_json(self.model_dump(mode="json")).encode("utf-8")

    def canonical_json(self) -> str:
        return c.canonical_json(self.model_dump(mode="json"))

    def load(self, path: str | Path) -> MatrixManifest:
        return MatrixManifest.model_validate(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )


def prepare_source_spec_json(spec: P6SourceSpec, path: str | Path) -> None:
    Path(path).write_text(spec.canonical_json(), encoding="utf-8")
