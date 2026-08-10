"""P1 检索工具集：judge 家族化设计的第二层证据（设计 §4.2）。

家族 agent 与维度 subagent 在自认 bundle 证据不足时，经本模块的 6 个只读
工具查询 run 目录**原始文件**（不做任何物化/脱敏剥离，只做 secret pattern
掩码，C2）：

- `read_dispatch_history`：router_interactions.csv + events.ndjson 派发记录
- `read_worker_state`：agent_interactions.csv 动作序列 + 关键 observation 摘要
- `read_agent_narrative`：workers/<Agent>/<Agent>/*.ndjson 的 llm_response 旁白
- `read_coordinator_reasoning`：coordinator/*.ndjson 的 llm_response content
  （unnamed_task.ndjson 顶层 content 与 <timestamp>.ndjson 的
  `data.content`（source=router）都覆盖）
- `read_tool_trace`：agent_interactions.csv 原始行（含 LLMOutput，不含
  LLMInput/Thinking —— 后者是截断片段/全空，不作证据）
- `query_semantic_map`：semantic_map.jsonl 观测溯源 + map_summary.jsonl 最新条目

护栏（全部在工具内部 fail-closed）：

- **只读 + containment**：所有文件访问经 `RetrievalSession.resolve_rel`，
  拒绝绝对路径 / 反斜杠 / `..` / `.` / 空段 / `~` / 盘符前缀 / symlink 组件 /
  run_dir 逃逸；只读 run_dir 内**既有**文件。
- **配额**：`max_calls`（家族 ≤4 / 维度 subagent ≤6 / job 总计 ≤28，可配）；
  超配额调用返回 Error 字符串。
- **单次返回 ≤4000 字符**：确定性截断 + 标记。
- **全记录**：每次调用经 `record_sink` 回调落盘
  `{tool, params, returned_refs, bytes, truncated}`；未提供 sink 时累积在
  `session.records`。
- **引用头**：每个文件段带 `--- ref: <rel>:L<a>[-L<b>] sha256=<file_digest> ---`
  头（CSV 逻辑行号 = 记录序号含表头，与 `agent_interactions.csv:L<N>` 先例一致；
  ndjson/jsonl = 物理行号）；文件级 sha256 按会话缓存。
- **secret 掩码**：返回文本经 `artifacts.redact_text`（Thinking 块/行 + secret
  key=value 掩码），LLM 内容不再剥离（C2 放宽）。

本模块不构造任何 DeepAgent / chat 模型，不写任何文件。
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c

__all__ = [
    "MAX_RETURN_CHARS",
    "QUOTA_DIMENSION_SUBAGENT",
    "QUOTA_FAMILY_AGENT",
    "QUOTA_JOB_TOTAL",
    "RetrievalError",
    "RetrievalSession",
    "make_tools",
]

# §4.2 配额与截断参数（设计定值；会话构造时可覆盖）。
QUOTA_FAMILY_AGENT = 4
QUOTA_DIMENSION_SUBAGENT = 6
QUOTA_JOB_TOTAL = 28
MAX_RETURN_CHARS = 4000

_AGENT_INTERACTIONS_REL = "agent_interactions.csv"
_ROUTER_INTERACTIONS_REL = "router_interactions.csv"
_EVENTS_REL = "events.ndjson"
_SEMANTIC_MAP_REL = "semantic_map.jsonl"
_MAP_SUMMARY_REL = "map_summary.jsonl"
_COORDINATOR_DIR = "coordinator"
_WORKERS_DIR = "workers"

# 单字段展示上限（确定性）：observation / narrative / content 过长会吃掉
# 整次返回的 4K 预算，先做字段级截断再整体截断。
_FIELD_PREVIEW_CHARS = 300
_OBS_PREVIEW_CHARS = 200
_SUMMARY_CHARS = 400

_STEP_RANGE_RE = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# events.ndjson 中与派发相关的 Coordinator 事件类型。
_DISPATCH_EVENT_TYPES = frozenset({"assign_task", "send_message", "cancel_task"})

# 一条检索记录在 record_sink 中的键（§4.2 全记录契约）。
_RECORD_KEYS = ("tool", "params", "returned_refs", "bytes", "truncated")


class RetrievalError(ValueError):
    """检索工具护栏违例（containment / 非法参数 / 配额）。"""


def _validate_rel(rel: str) -> str:
    """run_dir 内规范相对路径：拒绝绝对路径 / `..` / `.` / 空段 / 反斜杠。"""
    if not isinstance(rel, str) or not rel:
        raise RetrievalError("path must be a non-empty string")
    if rel.startswith(("/", "\\")) or "\\" in rel:
        raise RetrievalError("absolute or backslash paths are not allowed")
    if rel.startswith("~"):
        raise RetrievalError("path must be relative (tilde rejected)")
    if _DRIVE_RE.match(rel):
        raise RetrievalError("path must be relative (drive prefix rejected)")
    segments = rel.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise RetrievalError("path must be canonical (no empty/./.. segments)")
    return rel


def _parse_step_range(step_range: str) -> tuple[int, int] | None:
    """解析 `""`（全部）/ `"5"`（单步）/ `"3-8"`（闭区间）；非法 → RetrievalError。"""
    if step_range is None:
        return None
    text = str(step_range).strip()
    if not text:
        return None
    match = _STEP_RANGE_RE.fullmatch(text)
    if match is None:
        raise RetrievalError(
            f"invalid step_range {step_range!r}; expected '', 'N' or 'A-B'"
        )
    lo = int(match.group(1))
    hi = int(match.group(2)) if match.group(2) is not None else lo
    if lo > hi:
        raise RetrievalError(f"invalid step_range {step_range!r}: start > end")
    return lo, hi


def _in_range(value: Any, bounds: tuple[int, int] | None) -> bool:
    if bounds is None:
        return True
    try:
        step = int(value)
    except (TypeError, ValueError):
        return False
    lo, hi = bounds
    return lo <= step <= hi


def _compact(text: str, limit: int) -> str:
    """把多行字段压成单行预览（换行→空格，超长截断）。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + f"...[{len(flat) - limit} chars omitted]"


def _cap(text: str, limit: int) -> str:
    """单字段确定性截断（保留换行）。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[{len(text) - limit} chars omitted]"


# 一个文件段：(rel, 文件级 sha256, 首条逻辑行号, 末条逻辑行号)。
Section = tuple[str, str, int, int]


class RetrievalSession:
    """一次 judge invocation 的只读检索会话：run_dir 绑定 + 配额 + 调用记录。

    每个 (sub)agent invocation 构造独立实例（独立配额与记录）；`make_tools()`
    返回绑定本会话的 6 个 langchain BaseTool。所有文件读取只经
    `resolve_rel`（containment），只读不写。
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        max_calls: int = QUOTA_DIMENSION_SUBAGENT,
        record_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        root = Path(run_dir)
        if root.is_symlink():
            raise RetrievalError(f"run_dir must not be a symlink: {run_dir}")
        self.run_dir = root.resolve()
        if not self.run_dir.is_dir():
            raise RetrievalError(f"run_dir is not a directory: {self.run_dir}")
        if max_calls < 1:
            raise RetrievalError(f"max_calls must be >= 1, got {max_calls}")
        self.max_calls = max_calls
        self.record_sink = record_sink
        self.calls = 0
        self.records: list[dict[str, Any]] = []
        self._digest_cache: dict[str, str] = {}

    # ── containment ─────────────────────────────────────────────────────────
    def resolve_rel(self, rel: str) -> Path:
        """run_dir 受限路径解析：非法形态 / symlink 组件 / 逃逸 / 缺失一律拒绝。"""
        rel = _validate_rel(rel)
        lexical = self.run_dir.joinpath(rel)
        resolved = lexical.resolve()
        if resolved != lexical:
            raise RetrievalError(f"symlink components not allowed in path: {rel!r}")
        if not resolved.is_relative_to(self.run_dir):
            raise RetrievalError(f"path escapes run dir: {rel!r}")
        if not resolved.is_file():
            raise RetrievalError(f"file not found in run dir: {rel!r}")
        return resolved

    def _digest(self, rel: str, data: bytes) -> str:
        cached = self._digest_cache.get(rel)
        if cached is None:
            cached = c.sha256_hex(data)
            self._digest_cache[rel] = cached
        return cached

    # ── 数据读取（只读既有文件） ────────────────────────────────────────────
    def _read_text(self, rel: str) -> tuple[str, str]:
        """返回 (文本, 文件级 sha256)；文件必须已存在（resolve_rel 保证）。"""
        path = self.resolve_rel(rel)
        data = path.read_bytes()
        return data.decode("utf-8", errors="replace"), self._digest(rel, data)

    def _read_csv_records(self, rel: str) -> tuple[list[dict[str, str]], str]:
        """CSV 记录列表；多行 quoted 字段按 csv 语义合并（newline='' 保留 \\r\\n）。"""
        text, digest = self._read_text(rel)
        rows: list[dict[str, str]] = []
        reader = csv.DictReader(io.StringIO(text, newline=""))
        for row in reader:
            rows.append({k: (v or "") for k, v in row.items()})
        return rows, digest

    def _read_ndjson_records(
        self, rel: str
    ) -> tuple[list[tuple[int, dict[str, Any]]], str]:
        """返回 ([(物理行号, 事件)], digest)；坏行跳过。"""
        text, digest = self._read_text(rel)
        records: list[tuple[int, dict[str, Any]]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append((lineno, json.loads(stripped)))
            except ValueError:
                continue
        return records, digest

    def _list_ndjson_files(self, rel_dir: str) -> list[str]:
        """列出 rel_dir 下的既有 *.ndjson（逐个 containment 校验，拒绝逃逸）。"""
        rel_dir = _validate_rel(rel_dir)
        base = self.run_dir.joinpath(rel_dir)
        if not base.is_dir():
            return []
        out: list[str] = []
        for child in sorted(base.glob("*.ndjson")):
            rel = f"{rel_dir}/{child.name}"
            try:
                self.resolve_rel(rel)
            except RetrievalError:
                continue
            out.append(rel)
        return out

    # ── 统一出口：引用头 + 脱敏 + 截断 + 记录 ───────────────────────────────
    def _render(
        self,
        tool_name: str,
        params: dict[str, Any],
        sections: list[Section],
        body: str,
    ) -> str:
        """把文件段渲染成带引用头的文本：引用头 → 正文 → 脱敏 → 截断 → 记录。"""
        header_lines: list[str] = []
        for rel, digest, first_line, last_line in sections:
            span = f"L{first_line}" if first_line == last_line else (
                f"L{first_line}-L{last_line}"
            )
            header_lines.append(f"--- ref: {rel}:{span} sha256={digest} ---")
        if header_lines:
            body = "\n".join(header_lines) + "\n" + body

        redacted, _reasons = a.redact_text(body)
        final, truncated = self._truncate(redacted)
        self._record(
            tool_name,
            params,
            returned_refs=[s[0] for s in sections],
            bytes=len(final.encode("utf-8")),
            truncated=truncated,
        )
        return final

    def _render_error(self, tool_name: str, params: dict[str, Any], msg: str) -> str:
        """护栏违例返回（仍然记录该次调用，returned_refs 为空）。"""
        self._record(tool_name, params, returned_refs=[], bytes=len(msg.encode()), truncated=False)
        return msg

    def _record(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        returned_refs: list[str],
        bytes: int,
        truncated: bool,
    ) -> None:
        record = {
            "tool": tool_name,
            "params": params,
            "returned_refs": returned_refs,
            "bytes": bytes,
            "truncated": truncated,
        }
        self.records.append(record)
        if self.record_sink is not None:
            self.record_sink(record)

    @staticmethod
    def _truncate(text: str) -> tuple[str, bool]:
        """确定性整体截断：最终长度 ≤ MAX_RETURN_CHARS（含截断标记）。"""
        if len(text) <= MAX_RETURN_CHARS:
            return text, False
        cut = MAX_RETURN_CHARS - 64
        omitted = len(text) - cut
        marker = f"\n...[truncated: {omitted} chars omitted]"
        if len(marker) > 64:
            marker = marker[:64]
        return text[:cut] + marker, True

    # ── 配额与工具入口 ─────────────────────────────────────────────────────
    def _acquire(self, tool_name: str, params: dict[str, Any]) -> str | None:
        """占用一次配额；超限记录并返回错误文本。"""
        if self.calls >= self.max_calls:
            msg = f"Error: retrieval quota exhausted (max_calls={self.max_calls})"
            self._record(tool_name, params, returned_refs=[], bytes=len(msg.encode()), truncated=False)
            return msg
        self.calls += 1
        return None

    def _run(
        self,
        tool_name: str,
        params: dict[str, Any],
        builder: Callable[[], tuple[str, list[Section]]],
    ) -> str:
        """工具统一入口：配额 → 构建 → 渲染；护栏违例转 Error 文本。"""
        error = self._acquire(tool_name, params)
        if error is not None:
            return error
        try:
            body, sections = builder()
        except RetrievalError as exc:
            return self._render_error(tool_name, params, f"Error: {exc}")
        return self._render(tool_name, params, sections, body)

    # ── 各工具的数据组装（返回 (正文, 文件段)） ─────────────────────────────
    def _dispatch_history_sections(
        self, step_range: str = "", agent: str = ""
    ) -> tuple[str, list[Section]]:
        bounds = _parse_step_range(step_range)
        sections: list[Section] = []
        lines: list[str] = []

        if self.run_dir.joinpath(_ROUTER_INTERACTIONS_REL).is_file():
            rows, digest = self._read_csv_records(_ROUTER_INTERACTIONS_REL)
            matched: list[tuple[int, dict[str, str]]] = []
            for idx, row in enumerate(rows, start=2):  # L1 = 表头
                if not _in_range(row.get("Step"), bounds):
                    continue
                if agent and row.get("AssignedTo") != agent:
                    continue
                matched.append((idx, row))
            if matched:
                sections.append(
                    (_ROUTER_INTERACTIONS_REL, digest, matched[0][0], matched[-1][0])
                )
            for idx, row in matched:
                subtask = _compact(row.get("Subtask", ""), _FIELD_PREVIEW_CHARS)
                lines.append(
                    f"L{idx}: step={row.get('Step')} assigned_to={row.get('AssignedTo')} "
                    f"event={row.get('EventType')} | {subtask}"
                )
        else:
            lines.append(f"(no {_ROUTER_INTERACTIONS_REL} in run dir)")

        if self.run_dir.joinpath(_EVENTS_REL).is_file():
            records, digest = self._read_ndjson_records(_EVENTS_REL)
            matched: list[tuple[int, dict[str, Any]]] = []
            for lineno, ev in records:
                if ev.get("agent") != "Coordinator":
                    continue
                if ev.get("event_type") not in _DISPATCH_EVENT_TYPES:
                    continue
                if not _in_range(ev.get("step"), bounds):
                    continue
                payload = ev.get("payload") or {}
                if agent and payload.get("who") != agent:
                    continue
                matched.append((lineno, ev))
            if matched:
                sections.append((_EVENTS_REL, digest, matched[0][0], matched[-1][0]))
            for lineno, ev in matched:
                payload = ev.get("payload") or {}
                content = _compact(payload.get("content", ""), _FIELD_PREVIEW_CHARS)
                target = payload.get("who") or payload.get("related_task_id") or ""
                lines.append(
                    f"L{lineno}: step={ev.get('step')} type={ev.get('event_type')} "
                    f"target={target} | {content}"
                )
        else:
            lines.append(f"(no {_EVENTS_REL} in run dir)")

        if not lines:
            return "no dispatch history records found", sections
        return "\n".join(lines), sections

    def _worker_state_text(
        self, agent: str, step_range: str = ""
    ) -> tuple[str, list[Section]]:
        bounds = _parse_step_range(step_range)
        if not agent:
            raise RetrievalError("agent must be a non-empty string")
        if not self.run_dir.joinpath(_AGENT_INTERACTIONS_REL).is_file():
            return f"(no {_AGENT_INTERACTIONS_REL} in run dir)", []
        rows, digest = self._read_csv_records(_AGENT_INTERACTIONS_REL)
        matched: list[tuple[int, dict[str, str]]] = []
        for idx, row in enumerate(rows, start=2):
            if row.get("Agent") != agent:
                continue
            if not _in_range(row.get("Step"), bounds):
                continue
            matched.append((idx, row))
        if not matched:
            scope = f" in step range {step_range}" if bounds else ""
            return (
                f"no agent_interactions records for agent {agent!r}{scope}; "
                "task state cannot be derived from this file"
            ), []

        by_tool: dict[str, int] = {}
        lines: list[str] = []
        for idx, row in matched:
            tool = row.get("ToolName", "")
            by_tool[tool] = by_tool.get(tool, 0) + 1
            obs = _compact(row.get("Observation", ""), _OBS_PREVIEW_CHARS)
            lines.append(
                f"L{idx}: step={row.get('Step')} tool={tool} "
                f"action={row.get('Action')} args={row.get('ToolArgs')}"
            )
            if obs:
                lines.append(f"    observation: {obs}")
        summary = ", ".join(f"{t} x{n}" for t, n in sorted(by_tool.items()))
        return (
            f"agent={agent} actions in scope: {summary}\n" + "\n".join(lines)
        ), [(_AGENT_INTERACTIONS_REL, digest, matched[0][0], matched[-1][0])]

    def _agent_narrative_text(
        self, agent: str, step_range: str = ""
    ) -> tuple[str, list[Section]]:
        bounds = _parse_step_range(step_range)
        if not agent:
            raise RetrievalError("agent must be a non-empty string")
        rel_dir = _validate_rel(f"{_WORKERS_DIR}/{agent}/{agent}")
        files = self._list_ndjson_files(rel_dir)
        if not files:
            return f"no narrative files for agent {agent!r} under workers/", []

        sections: list[Section] = []
        lines: list[str] = []
        for rel in files:
            records, digest = self._read_ndjson_records(rel)
            matched: list[tuple[int, dict[str, Any]]] = []
            for lineno, ev in records:
                if ev.get("event") != "llm_response":
                    continue
                content = ev.get("content")
                if not content or not str(content).strip():
                    continue
                if "step" in ev and not _in_range(ev.get("step"), bounds):
                    continue
                matched.append((lineno, ev))
            if not matched:
                continue
            sections.append((rel, digest, matched[0][0], matched[-1][0]))
            for lineno, ev in matched:
                content = _cap(str(ev.get("content", "")), _FIELD_PREVIEW_CHARS)
                lines.append(
                    f"L{lineno}: ts={ev.get('ts')} task_id={ev.get('task_id')} "
                    f"| {content}"
                )
        if bounds is not None:
            lines.append(
                "(note: worker ndjson records carry no step field; step_range "
                "filter could not be applied, all narratives returned)"
            )
        if not lines:
            return f"no llm_response narratives for agent {agent!r}", sections
        return "\n".join(lines), sections

    def _coordinator_reasoning_text(
        self, step_range: str = ""
    ) -> tuple[str, list[Section]]:
        bounds = _parse_step_range(step_range)
        files = self._list_ndjson_files(_COORDINATOR_DIR)
        if not files:
            return "(no coordinator/*.ndjson files in run dir)", []

        sections: list[Section] = []
        lines: list[str] = []
        for rel in files:
            records, digest = self._read_ndjson_records(rel)
            matched: list[tuple[int, dict[str, Any]]] = []
            for lineno, ev in records:
                if ev.get("event") != "llm_response":
                    continue
                content, tool_calls = self._llm_response_payload(ev)
                if (not content or not str(content).strip()) and not tool_calls:
                    continue
                matched.append((lineno, ev))
            if not matched:
                continue
            sections.append((rel, digest, matched[0][0], matched[-1][0]))
            for lineno, ev in matched:
                content, tool_calls = self._llm_response_payload(ev)
                source = ev.get("source", "")
                text = _cap(str(content or ""), _FIELD_PREVIEW_CHARS)
                calls = ", ".join(
                    f"{tc.get('function', {}).get('name', '?')}"
                    for tc in tool_calls
                    if isinstance(tc, dict)
                )
                line = (
                    f"L{lineno}: ts={ev.get('ts') or ev.get('timestamp')} "
                    f"src={source or 'unnamed'} | {text}"
                )
                if calls:
                    line += f" [tool_calls: {calls}]"
                lines.append(line)
        if bounds is not None:
            lines.append(
                "(note: coordinator ndjson records carry no step field; "
                "step_range filter could not be applied, all reasoning returned)"
            )
        if not lines:
            return "no coordinator llm_response reasoning found", sections
        return "\n".join(lines), sections

    @staticmethod
    def _llm_response_payload(
        ev: dict[str, Any],
    ) -> tuple[Any, list[Any]]:
        """llm_response 的 (content, tool_calls)：router source 内容在 data 下。"""
        if ev.get("source") == "router":
            data = ev.get("data") or {}
            return data.get("content"), data.get("tool_calls") or []
        return ev.get("content"), ev.get("tool_calls") or []

    def _tool_trace_text(
        self, agent: str = "", step_range: str = "", tool_name: str = ""
    ) -> tuple[str, list[Section]]:
        bounds = _parse_step_range(step_range)
        if not self.run_dir.joinpath(_AGENT_INTERACTIONS_REL).is_file():
            return f"(no {_AGENT_INTERACTIONS_REL} in run dir)", []
        rows, digest = self._read_csv_records(_AGENT_INTERACTIONS_REL)
        matched: list[tuple[int, dict[str, str]]] = []
        for idx, row in enumerate(rows, start=2):
            if agent and row.get("Agent") != agent:
                continue
            if tool_name and row.get("ToolName") != tool_name:
                continue
            if not _in_range(row.get("Step"), bounds):
                continue
            matched.append((idx, row))
        if not matched:
            return "no matching agent_interactions rows", []
        lines: list[str] = []
        for idx, row in matched:
            lines.append(
                f"L{idx}: step={row.get('Step')} agent={row.get('Agent')} "
                f"tool={row.get('ToolName')} args={row.get('ToolArgs')} "
                f"action={row.get('Action')} event={row.get('EventType')} "
                f"error={row.get('ErrorType') or ''} "
                f"latency_ms={row.get('ToolLatencyMs') or ''}"
            )
            obs = row.get("Observation", "")
            if obs:
                lines.append(f"    observation: {_cap(obs, _FIELD_PREVIEW_CHARS)}")
            llm_out = row.get("LLMOutput", "")
            if llm_out:
                lines.append(f"    llm_output: {_cap(llm_out, _FIELD_PREVIEW_CHARS)}")
        return "\n".join(lines), [
            (_AGENT_INTERACTIONS_REL, digest, matched[0][0], matched[-1][0])
        ]

    def _semantic_map_text(
        self, object_name: str = "", object_type: str = ""
    ) -> tuple[str, list[Section]]:
        sections: list[Section] = []
        lines: list[str] = []

        if self.run_dir.joinpath(_SEMANTIC_MAP_REL).is_file():
            records, digest = self._read_ndjson_records(_SEMANTIC_MAP_REL)
            matched: list[tuple[int, dict[str, Any]]] = []
            needle = object_name.strip().lower() if object_name else ""
            for lineno, ev in records:
                obj = ev.get("object") or {}
                obs = ev.get("observation") or {}
                name = str(obj.get("name") or obs.get("name") or "")
                otype = str(obj.get("object_type") or obs.get("object_type") or "")
                if needle and needle not in name.lower():
                    continue
                if object_type and otype != object_type:
                    continue
                matched.append((lineno, ev))
            if matched:
                sections.append(
                    (_SEMANTIC_MAP_REL, digest, matched[0][0], matched[-1][0])
                )
            for lineno, ev in matched:
                obs = ev.get("observation") or {}
                obj = ev.get("object") or {}
                lines.append(
                    f"L{lineno}: ts={ev.get('ts')} reporter={obs.get('reporter')} "
                    f"step={obs.get('step')} "
                    f"type={obj.get('object_type') or obs.get('object_type')} "
                    f"name={obj.get('name') or obs.get('name')} "
                    f"position={obj.get('position') or obs.get('position')} "
                    f"confidence={obs.get('confidence')} "
                    f"source_task_id={obs.get('source_task_id') or ''} "
                    f"note={_compact(obs.get('note', ''), 80)}"
                )
            if not matched:
                lines.append(
                    "no semantic_map entries match the given filters "
                    f"(object_name={object_name!r}, object_type={object_type!r})"
                )
        else:
            lines.append(f"(no {_SEMANTIC_MAP_REL} in run dir)")

        if self.run_dir.joinpath(_MAP_SUMMARY_REL).is_file():
            records, digest = self._read_ndjson_records(_MAP_SUMMARY_REL)
            if records:
                lineno, latest = records[-1]
                sections.append((_MAP_SUMMARY_REL, digest, lineno, lineno))
                summary = _cap(str(latest.get("summary", "")), _SUMMARY_CHARS)
                lines.append(
                    f"L{lineno}: env_step={latest.get('env_step')} "
                    f"status={latest.get('status')} | {summary}"
                )
        else:
            lines.append(f"(no {_MAP_SUMMARY_REL} in run dir)")

        return "\n".join(lines), sections


def make_tools(session: RetrievalSession) -> list[BaseTool]:
    """把检索会话绑定成 6 个只读 langchain BaseTool（闭包捕获会话）。"""

    @tool
    def read_dispatch_history(step_range: str = "", agent: str = "") -> str:
        """Read the Coordinator's dispatch history from the raw run directory.

        Combines router_interactions.csv records and events.ndjson Coordinator
        dispatch events (assign_task / send_message / cancel_task).

        Args:
            step_range: optional step filter, '' (all), 'N' (single step) or
                'A-B' (inclusive range).
            agent: optional assigned-agent filter (exact match).

        Returns:
            Formatted dispatch records with per-file reference headers
            (`--- ref: <file>:L<a>-L<b> sha256=<digest> ---`).
        """
        return session._run(
            "read_dispatch_history",
            {"step_range": step_range, "agent": agent},
            lambda: session._dispatch_history_sections(step_range, agent),
        )

    @tool
    def read_worker_state(agent: str, step_range: str = "") -> str:
        """Read one worker's action sequence and key observation summaries.

        Derived from agent_interactions.csv: the agent's actions in scope with
        short observation previews plus an action histogram. This is a simple
        listing — the judge reads the raw rows; no state machine is built here.

        Args:
            agent: the worker agent name (exact match, e.g. 'Alice').
            step_range: optional step filter, '' (all), 'N', or 'A-B'.

        Returns:
            Action sequence lines with `file:L<logical-line>` references.
        """
        return session._run(
            "read_worker_state",
            {"agent": agent, "step_range": step_range},
            lambda: session._worker_state_text(agent, step_range),
        )

    @tool
    def read_agent_narrative(agent: str, step_range: str = "") -> str:
        """Read a worker agent's LLM narrative (llm_response content).

        Scans workers/<Agent>/<Agent>/*.ndjson for llm_response events with
        non-empty content. Worker logs are timestamp-based; step_range is
        validated but cannot filter records that carry no step field.

        Args:
            agent: the worker agent name (exact match).
            step_range: optional step filter, '' (all), 'N', or 'A-B'.

        Returns:
            Narrative lines with reference headers and file sha256 digests.
        """
        return session._run(
            "read_agent_narrative",
            {"agent": agent, "step_range": step_range},
            lambda: session._agent_narrative_text(agent, step_range),
        )

    @tool
    def read_coordinator_reasoning(step_range: str = "") -> str:
        """Read the Coordinator's decision reasoning (llm_response content).

        Covers both coordinator ndjson shapes: unnamed_task.ndjson (top-level
        content) and <timestamp>.ndjson (data.content, source=router).

        Args:
            step_range: optional step filter, '' (all), 'N', or 'A-B'.
                Coordinator logs carry no step field; the filter is validated
                but cannot be applied to records without one.

        Returns:
            Reasoning lines with reference headers and file sha256 digests.
        """
        return session._run(
            "read_coordinator_reasoning",
            {"step_range": step_range},
            lambda: session._coordinator_reasoning_text(step_range),
        )

    @tool
    def read_tool_trace(
        agent: str = "", step_range: str = "", tool_name: str = ""
    ) -> str:
        """Read raw agent_interactions rows (incl. LLMOutput).

        LLMInput and Thinking columns are excluded (truncated fragments /
        empty — not admissible evidence). Rows are CSV records; L<N> is the
        record index including the header.

        Args:
            agent: optional agent filter (exact match).
            step_range: optional step filter, '' (all), 'N', or 'A-B'.
            tool_name: optional tool filter (exact match on ToolName).

        Returns:
            Raw rows with per-field caps and reference headers.
        """
        return session._run(
            "read_tool_trace",
            {"agent": agent, "step_range": step_range, "tool_name": tool_name},
            lambda: session._tool_trace_text(agent, step_range, tool_name),
        )

    @tool
    def query_semantic_map(object_name: str = "", object_type: str = "") -> str:
        """Query semantic_map observation provenance + latest map summary.

        Args:
            object_name: optional case-insensitive substring match on the
                object name (e.g. 'CaldorFire' matches 'CaldorFire_Region_1').
            object_type: optional exact match on object type
                (fire / person / reservoir / deposit / agent).

        Returns:
            Matching semantic_map entries (confidence / source_task_id / note)
            and the latest map_summary entry, with reference headers.
        """
        return session._run(
            "query_semantic_map",
            {"object_name": object_name, "object_type": object_type},
            lambda: session._semantic_map_text(object_name, object_type),
        )

    return [
        read_dispatch_history,
        read_worker_state,
        read_agent_narrative,
        read_coordinator_reasoning,
        read_tool_trace,
        query_semantic_map,
    ]
