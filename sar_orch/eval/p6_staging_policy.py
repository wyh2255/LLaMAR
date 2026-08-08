"""P6 versioned semantic-staging policy（设计 §0.6-4/6、§3.2、§9 P6.2）。

`P6SemanticStagingPolicyV1` 是唯一 canonical policy owner。projection 语义：source
只用于 digest 与**最小安全 projection**——staging 在输入端剥离非 allowlist 字段并
**记录名称**（只记 key/列名、不记值），不再 fail-closed；fail-closed 落在 staged
产物（`assert_staged_metadata_keys` / `_assert_csv_header_clean` / `defense_scan`）：

- metadata.json 仅允许 §0.6-6 的九字段。source 的非 allowlist key（含显式 denied
  的 api_base / provider / model / coordinator_prompts / worker_prompts /
  task_objective / success_criteria 及未知 key）剥离并记录 key 名；staged 输出
  仍只含九字段。
- 结构化文件 allowlist：每类 CSV/JSONL 只保留 policy 明列字段。source CSV 的非
  allowlist 列（含 `agent_interactions.csv` 的 LLMInput/LLMOutput/Thinking 与
  任何未知列）剥离并记录列名。JSONL 未知嵌套字段仍 fail-closed（无剥离语义）。
- 自由文本/JSONL string leaf 做确定性 redaction（secret / CoT sentinel / provider
  header），写入前消除；`defense_scan` 在写入后对 staged bytes 重验同一规则。
- 空 `supervision/` + canonical `supervision_state.json={"state":"present_empty"}`。
- policy 独立 canonical digest（`redaction_policy_digest`）。

本模块不读取 SAR 结果、不构造 LLM、不触碰 workflow。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from sar_orch.eval import contracts as c

POLICY_VERSION = "P6SemanticStagingPolicyV1"

SUPERVISION_STATE_FILENAME = "supervision_state.json"
SUPERVISION_STATE_CONTENT = {"state": "present_empty"}

#: 测试用 secret/CoT sentinel：redactor 必须消除，defense scan 必须拒绝残留。
TEST_SECRET_SENTINEL = "__P6_SECRET_SENTINEL__"
TEST_THINKING_SENTINEL = "__P6_THINKING_SENTINEL__"

#: staged root 固定允许的文件集合（九类 staged 输入 + supervision sentinel）。
#: defense_scan 只接受这些 regular file 与空 `supervision/` 目录；其余一律拒绝。
STAGED_ROOT_FILES = (
    "metadata.json",
    "trajectory.csv",
    "router_interactions.csv",
    "subtasks.csv",
    "agent_interactions.csv",
    "summary.csv",
    "token_usage.csv",
    "semantic_map.jsonl",
    "map_summary.jsonl",
    SUPERVISION_STATE_FILENAME,
)

#: metadata.json 唯一允许的九字段（§0.6-6）。
METADATA_ALLOWED_FIELDS = frozenset(
    {
        "run_id",
        "scene",
        "seed",
        "agent_count",
        "agent_names",
        "prompt_version",
        "code_commit",
        "git_dirty",
        "max_steps",
    }
)

#: metadata.json 显式拒绝字段（不得出现在 staged metadata）。
METADATA_DENIED_FIELDS = frozenset(
    {
        "api_base",
        "provider",
        "model",
        "coordinator_prompts",
        "worker_prompts",
        "task_objective",
        "success_criteria",
    }
)

#: CSV 列 allowlist（staged header 的规范顺序）。
TRAJECTORY_HEADERS = (
    "Step",
    "Actions",
    "Successes",
    "Observations",
    "Coverage",
    "TransportRate",
    "Finished",
    "MapRecall",
    "Freshness",
    "TimeoutAgents",
    "RunID",
    "MaxSteps",
    "RemainingSteps",
    "WallTimeSinceStart",
    "StepDurationMs",
    "ErrorTypes",
    "CompletedSubtasksDelta",
    "EndReason",
)
ROUTER_HEADERS = (
    "Step",
    "Subtask",
    "AssignedTo",
    "RunID",
    "CorrelationID",
    "WorkerTaskID",
    "EventType",
)
SUBTASKS_HEADERS = (
    "RunID",
    "Step",
    "SubtaskID",
    "Status",
    "AssignedTo",
    "Subtask",
    "CreatedAt",
    "UpdatedAt",
    "FailureClass",
    "Details",
)
AGENT_INTERACTIONS_HEADERS = (
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "RunID",
    "CorrelationID",
    "EventType",
    "ToolLatencyMs",
    "ErrorType",
)
TOKEN_USAGE_HEADERS = (
    "Step",
    "Agent",
    "PromptTokens",
    "CompletionTokens",
    "TotalTokens",
    "CacheHitTokens",
    "CacheMissTokens",
    "RunID",
    "LLMLatencyMs",
    "Model",
    "PromptVersion",
)

#: summary.csv：保留 final metrics / interaction totals / token 数值列；
#: ExperimentName/LogDir 写固定空值。
SUMMARY_FIXED_COLUMNS = frozenset(
    {
        "ExperimentName",
        "LogDir",
        "TotalSteps",
        "FinalCoverage",
        "FinalTransportRate",
        "Finished",
        "TotalAgentInteractions",
        "TotalRouterInteractions",
    }
)
_TOKEN_COLUMN_RE = re.compile(
    r"(PromptTokens|CompletionTokens|TotalTokens|CacheHitTokens|CacheMissTokens)$"
)

#: 每文件需要 redaction 的 CSV 列（string leaf）。
CSV_REDACT_COLUMNS: dict[str, frozenset[str]] = {
    "trajectory.csv": frozenset(
        {"Actions", "Observations", "ErrorTypes", "CompletedSubtasksDelta"}
    ),
    "router_interactions.csv": frozenset({"Subtask"}),
    "subtasks.csv": frozenset({"Subtask", "Details"}),
    "agent_interactions.csv": frozenset({"ToolArgs", "Action", "Observation"}),
    "summary.csv": frozenset(),
    "token_usage.csv": frozenset(),
}

#: 每文件允许出现但必须被删除的 source 列（policy 明示 drop）。source header
#: 出现任何既不在 allowlist 也不在此集合的列 → fail-closed（不做 extrasaction=ignore）。
CSV_DROP_COLUMNS: dict[str, frozenset[str]] = {
    "agent_interactions.csv": frozenset({"LLMInput", "LLMOutput", "Thinking"}),
}

#: CSV 列 allowlist（规范顺序）。summary 由 SUMMARY_FIXED_COLUMNS + token 列构成。
CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "trajectory.csv": TRAJECTORY_HEADERS,
    "router_interactions.csv": ROUTER_HEADERS,
    "subtasks.csv": SUBTASKS_HEADERS,
    "agent_interactions.csv": AGENT_INTERACTIONS_HEADERS,
    "token_usage.csv": TOKEN_USAGE_HEADERS,
}

#: semantic_map.jsonl 记录结构 allowlist。
SEMANTIC_MAP_TOP_KEYS = frozenset(
    {"ts", "event_type", "object", "object_type", "observation"}
)
OBJECT_KEYS = frozenset(
    {
        "object_type",
        "name",
        "position",
        "attributes",
        "status",
        "last_seen_step",
        "last_seen_ts",
        "sources",
        "confidence",
        "conflict",
    }
)
OBSERVATION_KEYS = frozenset(
    {
        "reporter",
        "step",
        "object_type",
        "name",
        "position",
        "attributes",
        "confidence",
        "source_task_id",
        "note",
    }
)
SOURCE_RECORD_KEYS = frozenset({"reporter", "task_id", "step", "confidence", "note"})

#: map_summary.jsonl 记录结构 allowlist。
MAP_SUMMARY_TOP_KEYS = frozenset(
    {
        "base_revision",
        "env_step",
        "map_revision",
        "status",
        "summary",
        "timestamp",
        "token_usage",
        "trigger_reasons",
    }
)
MAP_SUMMARY_TOKEN_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
    }
)

#: attributes 扩展的 key 必须通过 secret/header denylist。
_SECRET_HEADER_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|token|password|passwd|authorization|"
    r"bearer|cookie|credential|x-api|proxy-authorization)"
)

_THINKING_TAG_RE = re.compile(r"(?is)<thinking>.*?</thinking>")
_THINKING_LINE_RE = re.compile(r"(?im)^\s*\*?Thinking\*?:.*$")
_JSON_SECRET_RE = re.compile(
    r'("(?:api[_-]?key|apikey|secret|token|password|passwd|authorization|'
    r"bearer|cookie|credential)\"\s*:\s*)\"[^\"]*\""
)
_SECRET_LINE_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|token|password|passwd|authorization|"
    r"bearer|cookie|credential)\s*[=:]\s*\S+"
)
_RAW_NDJSON_MARKERS = (
    '"event": "llm_request"',
    '"tool_calls"',
    '"messages": [',
    '"llm_response"',
)


class SemanticStagingError(ValueError):
    """typed `semantic_staging` 失败：无法安全转换/未知字段/防御扫描违规。"""


def redact_text(text: str) -> tuple[str, list[str]]:
    """确定性 redaction：消除 Thinking 块/行、secret key=value、secret JSON value 与
    policy 测试 sentinel。返回 (redacted, reasons)；`SemanticStagingError` 不在此抛出。
    """
    reasons: list[str] = []
    if _THINKING_TAG_RE.search(text):
        text = _THINKING_TAG_RE.sub("[Thinking redacted]", text)
        reasons.append("thinking_block")
    if _THINKING_LINE_RE.search(text):
        text = _THINKING_LINE_RE.sub("[Thinking redacted]", text)
        reasons.append("thinking_line")
    if TEST_THINKING_SENTINEL in text:
        text = text.replace(TEST_THINKING_SENTINEL, "[Thinking redacted]")
        reasons.append("thinking_sentinel")
    if _JSON_SECRET_RE.search(text):
        text = _JSON_SECRET_RE.sub(r'\1"[REDACTED]"', text)
        reasons.append("secret_value")
    if _SECRET_LINE_RE.search(text):
        text = _SECRET_LINE_RE.sub(lambda m: m.group(1) + "=[REDACTED]", text)
        reasons.append("secret_value")
    if TEST_SECRET_SENTINEL in text:
        text = text.replace(TEST_SECRET_SENTINEL, "[REDACTED]")
        reasons.append("secret_sentinel")
    return text, reasons


def _is_allowable_metadata_key(key: str) -> bool:
    return key in METADATA_ALLOWED_FIELDS


def stage_metadata(source_metadata: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """metadata.json staging：只保留九字段；source 的非 allowlist key（含
    METADATA_DENIED_FIELDS 与未知 key）**剥离并记录 key 名**（不记值），不再抛错。

    返回 `(staged, stripped_key_names)`：staged 输出仍只含九字段；stripped 为
    排序后的 key 名列表。staged fail-closed 由 `assert_staged_metadata_keys`
    （stage_structured_file / defense_scan）落在 staged 产物上。允许字段值若
    是敏感字符串，同样经 redactor。
    """
    stripped = sorted(set(source_metadata) - METADATA_ALLOWED_FIELDS)
    out: dict[str, Any] = {}
    for key in sorted(METADATA_ALLOWED_FIELDS):
        if key not in source_metadata:
            continue
        value = source_metadata[key]
        if isinstance(value, str):
            value, _reasons = redact_text(value)
        out[key] = value
    return out, stripped


def assert_staged_metadata_keys(staged: dict[str, Any]) -> None:
    """staged metadata 防御：任何非九字段 key（含 denied/未知）→ fail-closed。"""
    unknown = set(staged) - METADATA_ALLOWED_FIELDS
    if unknown:
        raise SemanticStagingError(
            f"unknown metadata key(s) in staged metadata: {sorted(unknown)}"
        )


def _redact_json_value(value: Any, *, reason_sink: list[str]) -> Any:
    """递归 redact JSON value 的 string leaf；结构/number/bool/None 原样保留。

    不在此处做 key 校验（由各记录 allowlist 处理）。
    """
    if isinstance(value, str):
        red, reasons = redact_text(value)
        reason_sink.extend(reasons)
        return red
    if isinstance(value, list):
        return [_redact_json_value(x, reason_sink=reason_sink) for x in value]
    if isinstance(value, dict):
        return {
            k: _redact_json_value(v, reason_sink=reason_sink) for k, v in value.items()
        }
    return value


def _check_attributes_extension(key: str) -> None:
    """attributes 的 key 必须通过 secret/header denylist；命中 → fail-closed。"""
    if _SECRET_HEADER_KEY_RE.search(key):
        raise SemanticStagingError(
            f"attributes key matches secret/header denylist: {key!r}"
        )


def _redact_semantic_object(
    record: dict[str, Any], *, allowed: frozenset[str], reason_sink: list[str]
) -> dict[str, Any]:
    """object / observation 记录：只允许固定结构 keys；`attributes` 是唯一递归
    extension（key 过 denylist、string leaf 递归 redaction）；sources 固定 shape。"""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key not in allowed:
            raise SemanticStagingError(
                f"unknown semantic_map key {key!r} (allowlist={sorted(allowed)})"
            )
        if key == "attributes":
            _check_attributes_extension(key)
            attrs: dict[str, Any] = {}
            for ak, av in value.items():
                _check_attributes_extension(ak)
                attrs[ak] = _redact_json_value(av, reason_sink=reason_sink)
            out[key] = attrs
        elif key == "sources" and isinstance(value, list):
            staged_sources = []
            for src in value:
                if not isinstance(src, dict):
                    raise SemanticStagingError("source record must be a dict")
                unknown = set(src) - SOURCE_RECORD_KEYS
                if unknown:
                    raise SemanticStagingError(
                        f"unknown source record key(s): {sorted(unknown)}"
                    )
                staged_sources.append(_redact_json_value(src, reason_sink=reason_sink))
            out[key] = staged_sources
        else:
            out[key] = _redact_json_value(value, reason_sink=reason_sink)
    return out


def _stage_semantic_map_jsonl(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reason_sink: list[str] = []
    out: list[dict[str, Any]] = []
    for record in lines:
        if not isinstance(record, dict):
            raise SemanticStagingError("semantic_map record must be a JSON object")
        unknown = set(record) - SEMANTIC_MAP_TOP_KEYS
        if unknown:
            raise SemanticStagingError(
                f"unknown semantic_map top-level key(s): {sorted(unknown)}"
            )
        staged: dict[str, Any] = {}
        for key, value in record.items():
            if key in ("object", "observation"):
                if not isinstance(value, dict):
                    raise SemanticStagingError(f"semantic_map {key} must be a dict")
                allowed = OBJECT_KEYS if key == "object" else OBSERVATION_KEYS
                staged[key] = _redact_semantic_object(
                    value, allowed=allowed, reason_sink=reason_sink
                )
            else:
                staged[key] = _redact_json_value(value, reason_sink=reason_sink)
        out.append(staged)
    return out


def _stage_map_summary_jsonl(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reason_sink: list[str] = []
    out: list[dict[str, Any]] = []
    for record in lines:
        if not isinstance(record, dict):
            raise SemanticStagingError("map_summary record must be a JSON object")
        unknown = set(record) - MAP_SUMMARY_TOP_KEYS
        if unknown:
            raise SemanticStagingError(
                f"unknown map_summary top-level key(s): {sorted(unknown)}"
            )
        staged: dict[str, Any] = {}
        for key, value in record.items():
            if key == "token_usage":
                if not isinstance(value, dict):
                    raise SemanticStagingError("token_usage must be a dict")
                t_unknown = set(value) - MAP_SUMMARY_TOKEN_KEYS
                if t_unknown:
                    raise SemanticStagingError(
                        f"unknown token_usage key(s): {sorted(t_unknown)}"
                    )
                staged[key] = _redact_json_value(value, reason_sink=reason_sink)
            else:
                staged[key] = _redact_json_value(value, reason_sink=reason_sink)
        out.append(staged)
    return out


def stage_jsonl_file(filename: str, content: bytes) -> bytes:
    """JSONL staging：逐 record 按 allowlist 保留结构、string leaf redaction、
    未知字段 fail-closed，然后 canonical reserialize。"""
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(
        content.decode("utf-8", errors="replace").splitlines(), 1
    ):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SemanticStagingError(
                f"{filename}:L{line_no} unparseable JSON line ({exc})"
            ) from exc
        rows.append(record)
    if filename == "semantic_map.jsonl":
        staged_rows = _stage_semantic_map_jsonl(rows)
    elif filename == "map_summary.jsonl":
        staged_rows = _stage_map_summary_jsonl(rows)
    else:
        raise SemanticStagingError(f"no JSONL policy for {filename}")
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in staged_rows)
    return body.encode("utf-8") if body else b""


def _staged_summary_columns(header) -> list[str]:
    out: list[str] = []
    for col in header:
        if col in SUMMARY_FIXED_COLUMNS or _TOKEN_COLUMN_RE.search(col):
            out.append(col)
    # 固定顺序：ExperimentName/LogDir 若存在则前置；其余保持 source 出现顺序。
    order = []
    for col in ("ExperimentName", "LogDir"):
        if col in header and col in out:
            order.append(col)
    order.extend(col for col in out if col not in ("ExperimentName", "LogDir"))
    return order


def _stripped_csv_columns(filename: str, header) -> list[str]:
    """source CSV header 的非 allowlist 列名（排序）。

    含 agent_interactions 的 LLMInput/LLMOutput/Thinking 与任何未知列：一律剥离并
    记录名称（不记值），不抛错。staged 输出只保留 allowlist 列。
    """
    policy_columns = CSV_COLUMNS.get(filename)
    if policy_columns is None:
        raise SemanticStagingError(f"no CSV policy for {filename}")
    allowed = set(policy_columns)
    return sorted(col for col in header if col not in allowed)


def _assert_csv_header_clean(filename: str, header) -> None:
    """staged CSV header 防御：任何非 allowlist 列（含 LLM 三列/未知列）→ fail-closed。

    source 端已剥离额外列；staged 端不允许任何残留。defense_scan 调用。
    """
    policy_columns = CSV_COLUMNS.get(filename)
    if policy_columns is None:
        raise SemanticStagingError(f"no CSV policy for {filename}")
    allowed = set(policy_columns)
    unknown = [col for col in header if col not in allowed]
    if unknown:
        raise SemanticStagingError(
            f"{filename}: staged header contains policy-disallowed column(s): "
            f"{sorted(unknown)}"
        )


def _stripped_summary_columns(header) -> list[str]:
    """summary.csv source header 的非 allowlist 列名（排序）。"""
    return sorted(
        col
        for col in header
        if col not in SUMMARY_FIXED_COLUMNS and not _TOKEN_COLUMN_RE.search(col)
    )


def _assert_summary_header_clean(header) -> None:
    """summary.csv staged header 只允许 SUMMARY_FIXED_COLUMNS + 规范 token 列。"""
    unknown = [
        col
        for col in header
        if col not in SUMMARY_FIXED_COLUMNS and not _TOKEN_COLUMN_RE.search(col)
    ]
    if unknown:
        raise SemanticStagingError(
            f"summary.csv: staged header contains policy-disallowed column(s): "
            f"{sorted(unknown)}"
        )


def stage_csv_file(filename: str, content: bytes) -> tuple[bytes, list[str]]:
    """CSV staging：只保留 allowlist 列（规范顺序），redact 指定 string 列。

    source header 的非 allowlist 列（含 agent_interactions 的 LLM 三列与任何未知
    列）剥离并记录列名（排序、仅名称），不再抛错。返回 `(staged_bytes,
    stripped_column_names)`。staged fail-closed 由 `_assert_csv_header_clean`
    / `_assert_summary_header_clean` 落在 defense_scan。
    """
    if filename == "summary.csv":
        return _stage_summary_csv(content)
    columns = CSV_COLUMNS.get(filename)
    if columns is None:
        raise SemanticStagingError(f"no CSV policy for {filename}")
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise SemanticStagingError(f"{filename}: empty header")
    stripped = _stripped_csv_columns(filename, reader.fieldnames)
    redact_set = CSV_REDACT_COLUMNS.get(filename, frozenset())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in reader:
        out: dict[str, str] = {}
        for col in columns:
            value = str(row.get(col, ""))
            if col in redact_set:
                value, _reasons = redact_text(value)
            out[col] = value
        writer.writerow(out)
    return buf.getvalue().encode("utf-8"), stripped


def _stage_summary_csv(content: bytes) -> tuple[bytes, list[str]]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise SemanticStagingError("summary.csv: empty header")
    stripped = _stripped_summary_columns(reader.fieldnames)
    columns = _staged_summary_columns(reader.fieldnames)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in reader:
        out: dict[str, str] = {}
        for col in columns:
            value = str(row.get(col, ""))
            if col in ("ExperimentName", "LogDir"):
                out[col] = ""
            else:
                out[col] = value
        writer.writerow(out)
    return buf.getvalue().encode("utf-8"), stripped


def stage_structured_file(filename: str, content: bytes) -> tuple[bytes, list[str]]:
    """按 policy 把 source 文件转换为 staged 语义输入 bytes。

    返回 `(staged_bytes, stripped_names)`：metadata → 被剥离的非 allowlist key 名
    （排序、仅名称）；CSV → 被剥离的非 allowlist 列名；JSONL 未知嵌套字段仍
    fail-closed（无剥离语义，返回空列表）。
    """
    if filename == "metadata.json":
        try:
            metadata = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SemanticStagingError(f"metadata.json unparseable ({exc})") from exc
        if not isinstance(metadata, dict):
            raise SemanticStagingError("metadata.json must be a JSON object")
        staged, stripped = stage_metadata(metadata)
        assert_staged_metadata_keys(staged)
        return (
            json.dumps(staged, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            stripped,
        )
    if filename in CSV_COLUMNS or filename == "summary.csv":
        return stage_csv_file(filename, content)
    if filename in ("semantic_map.jsonl", "map_summary.jsonl"):
        return stage_jsonl_file(filename, content), []
    raise SemanticStagingError(f"no staging policy for {filename}")


def canonical_policy_dict() -> dict[str, Any]:
    """policy 的 canonical 表示（policy digest 的事实源）。"""
    return {
        "policy_version": POLICY_VERSION,
        "metadata_allowed_fields": sorted(METADATA_ALLOWED_FIELDS),
        "metadata_denied_fields": sorted(METADATA_DENIED_FIELDS),
        "supervision_state": SUPERVISION_STATE_CONTENT,
        "csv_columns": {k: list(v) for k, v in CSV_COLUMNS.items()},
        "csv_drop_columns": {k: sorted(v) for k, v in CSV_DROP_COLUMNS.items()},
        "summary_fixed_columns": sorted(SUMMARY_FIXED_COLUMNS),
        "csv_redact_columns": {k: sorted(v) for k, v in CSV_REDACT_COLUMNS.items()},
        "semantic_map_top_keys": sorted(SEMANTIC_MAP_TOP_KEYS),
        "object_keys": sorted(OBJECT_KEYS),
        "observation_keys": sorted(OBSERVATION_KEYS),
        "source_record_keys": sorted(SOURCE_RECORD_KEYS),
        "map_summary_top_keys": sorted(MAP_SUMMARY_TOP_KEYS),
        "map_summary_token_keys": sorted(MAP_SUMMARY_TOKEN_KEYS),
    }


def canonical_policy_bytes() -> bytes:
    return c.canonical_json(canonical_policy_dict()).encode("utf-8")


def policy_digest() -> str:
    """redaction_policy_digest：policy canonical bytes 的 SHA-256。"""
    return c.sha256_hex(canonical_policy_bytes())


def supervision_state_bytes() -> bytes:
    return c.canonical_json(SUPERVISION_STATE_CONTENT).encode("utf-8")


def _assert_no_raw_ndjson(content: bytes) -> None:
    text = content.decode("utf-8", errors="replace")
    for marker in _RAW_NDJSON_MARKERS:
        if marker in text:
            raise SemanticStagingError(f"raw NDJSON marker {marker!r} in staged bytes")


def _assert_no_llm_columns_in_agent_header(header) -> None:
    forbidden = {"LLMInput", "LLMOutput", "Thinking"}
    hit = forbidden.intersection(header)
    if hit:
        raise SemanticStagingError(
            f"agent_interactions staged header must drop LLM columns, got {sorted(hit)}"
        )


def _assert_staged_csv_header_clean(filename: str, content: bytes) -> None:
    """staged CSV header 防御：解析 header 并对非 allowlist 列 fail-closed。"""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8", errors="replace")))
    if reader.fieldnames is None:
        raise SemanticStagingError(f"{filename}: empty header")
    if filename == "summary.csv":
        _assert_summary_header_clean(reader.fieldnames)
    else:
        _assert_csv_header_clean(filename, reader.fieldnames)


def defense_scan(staged_dir: Path) -> list[str]:
    """对 staged 目录重跑防御扫描；返回违规列表（空 = pass）。

    这里是 fail-closed 防线（source 端只剥离+记录，不在此检查）：raw NDJSON、
    staged metadata 非九字段、staged CSV 非 allowlist 列（含 LLMInput/LLMOutput/
    Thinking）、secret/Thinking sentinel/pattern、未知结构化字段、staged root
    额外文件/目录/symlink/hardlink 一律违规。
    """
    violations: list[str] = []
    staged_dir = Path(staged_dir)

    if not staged_dir.exists():
        return ["staged dir missing"]

    # staged root 内容闭环：只允许固定 10 个 regular file + 空 `supervision/` 目录。
    # 任何 unexpected regular file/directory/symlink/hardlink → fail-closed（防止
    # staged semantic digest / 验证忽略额外内容）。
    allowed_root_files = set(STAGED_ROOT_FILES)
    for entry in sorted(staged_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name == "supervision":
            if entry.is_symlink() or not entry.is_dir():
                violations.append("staged supervision/ not a plain directory")
            elif any(entry.iterdir()):
                violations.append("staged supervision/ must be empty")
            continue
        if name in allowed_root_files:
            if entry.is_symlink() or not entry.is_file():
                violations.append(f"staged {name} not a plain regular file")
            elif entry.stat().st_nlink > 1:
                violations.append(f"staged {name} is a hardlink")
            continue
        if entry.is_symlink():
            violations.append(f"unexpected symlink in staged dir: {name}")
        elif entry.is_dir():
            violations.append(f"unexpected directory in staged dir: {name}")
        elif entry.is_file():
            violations.append(f"unexpected file in staged dir: {name}")
            if entry.stat().st_nlink > 1:
                violations.append(f"unexpected file {name} is a hardlink")
        else:
            violations.append(f"unexpected entry in staged dir: {name}")

    for ndjson in sorted(staged_dir.rglob("*.ndjson")):
        violations.append(f"raw NDJSON file present in staged dir: {ndjson.name}")

    metadata_path = staged_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_bytes().decode("utf-8"))
            assert_staged_metadata_keys(metadata)
        except (json.JSONDecodeError, SemanticStagingError) as exc:
            violations.append(f"metadata.json defense: {exc}")

    agent_path = staged_dir / "agent_interactions.csv"
    if agent_path.exists():
        try:
            reader = csv.DictReader(
                io.StringIO(agent_path.read_bytes().decode("utf-8", errors="replace"))
            )
            _assert_no_llm_columns_in_agent_header(reader.fieldnames or [])
        except (csv.Error, SemanticStagingError) as exc:
            violations.append(f"agent_interactions.csv defense: {exc}")

    for name in (
        "metadata.json",
        *CSV_COLUMNS,
        "summary.csv",
        "semantic_map.jsonl",
        "map_summary.jsonl",
    ):
        path = staged_dir / name
        if not path.exists():
            continue
        content = path.read_bytes()
        try:
            _assert_no_raw_ndjson(content)
            if name == "metadata.json":
                continue
            # 重跑同一 redactor：staged 内容不应再产生任何 redaction reason。
            if name.endswith(".jsonl"):
                staged_rows = stage_jsonl_file(name, content)
                for row in staged_rows:
                    reasons: list[str] = []
                    _redact_json_value(row, reason_sink=reasons)
                    if reasons:
                        raise SemanticStagingError(
                            f"staged {name} still contains redactable content: "
                            f"{sorted(set(reasons))}"
                        )
            else:
                # staged CSV 防御：header 不允许任何非 allowlist 列（source 端已剥离）。
                _assert_staged_csv_header_clean(name, content)
                stage_csv_file(name, content)
                text = content.decode("utf-8", errors="replace")
                _red, reasons = redact_text(text)
                if reasons:
                    raise SemanticStagingError(
                        f"staged {name} still contains redactable content: "
                        f"{sorted(set(reasons))}"
                    )
        except SemanticStagingError as exc:
            violations.append(f"{name} defense: {exc}")

    sup_path = staged_dir / SUPERVISION_STATE_FILENAME
    if not sup_path.exists() or sup_path.read_bytes() != supervision_state_bytes():
        violations.append(
            f"staged {SUPERVISION_STATE_FILENAME} missing or non-canonical"
        )
    return violations
