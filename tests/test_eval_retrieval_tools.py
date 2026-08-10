"""P1 检索工具集合同测试（judge 家族化设计 §4.2）。

在 tmp_path 构造迷你合成 run 目录（CSV/NDJSON 与真实 run 同构），验证：

- 6 个工具的名称与基础检索正确性（step_range / agent / tool_name / 对象过滤）
- 两种 coordinator ndjson 形态（unnamed_task 顶层 content 与 <timestamp>
  ndjson 的 data.content（source=router））都被覆盖
- CSV 多行 quoted 字段下的逻辑行号（记录序号含表头）与引用头格式
- containment：绝对路径 / `..` / 反斜杠 / symlink 逃逸全部拒绝
- 单次返回 ≤4000 字符确定性截断 + 标记
- 配额（max_calls）与全记录（record_sink 五键契约）
- secret 掩码（artifacts.redact_text 复用）
- 文件缺失时的优雅降级

不构造任何 DeepAgent / chat 模型。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sar_orch.eval import contracts as c
from sar_orch.eval.agent.retrieval_tools import (
    MAX_RETURN_CHARS,
    QUOTA_DIMENSION_SUBAGENT,
    QUOTA_FAMILY_AGENT,
    QUOTA_JOB_TOTAL,
    RetrievalError,
    RetrievalSession,
    make_tools,
)

_REF_HEADER_RE = re.compile(
    r"^--- ref: ([^:]+):L(\d+)(?:-L(\d+))? sha256=([0-9a-f]{64}) ---$"
)

AGENT_CSV_HEADER = (
    "Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,"
    "Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_run(run: Path) -> None:
    """构造迷你合成 run 目录（与真实 run 文件同构）。"""
    _write(
        run / "router_interactions.csv",
        "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType\n"
        '"0","Explore the environment.","Alice","run1","c1","w1","assign_task"\n'
        '"0","Explore the environment.","Bob","run1","c2","w2","assign_task"\n'
        '"3","cancel_task(task_id=w1)","Worker","run1","","","cancel_task"\n'
        '"5","Investigate the fire.","Alice","run1","c3","w3","assign_task"\n',
    )
    _write(
        run / "events.ndjson",
        "\n".join(
            [
                json.dumps(
                    {
                        "agent": "Coordinator",
                        "event_type": "assign_task",
                        "payload": {
                            "who": "Alice",
                            "content": "Explore the environment.",
                            "message_type": "assign_task",
                            "related_task_id": "t1",
                        },
                        "step": 0,
                        "ts": 1,
                    }
                ),
                json.dumps(
                    {
                        "agent": "Coordinator",
                        "event_type": "assign_task",
                        "payload": {
                            "who": "Bob",
                            "content": "Explore the environment.",
                        },
                        "step": 0,
                        "ts": 2,
                    }
                ),
                json.dumps(
                    {
                        "agent": "Coordinator",
                        "event_type": "cancel_task",
                        "payload": {"related_task_id": "t1"},
                        "step": 3,
                        "ts": 3,
                    }
                ),
                json.dumps(
                    {
                        "agent": "Coordinator",
                        "event_type": "send_message",
                        "payload": {
                            "message_type": "activate_plan_node",
                            "related_task_id": "t2",
                        },
                        "step": 5,
                        "ts": 4,
                    }
                ),
                json.dumps(
                    {
                        "agent": "Alice",
                        "event_type": "report_observation",
                        "payload": {"content": "I see a fire"},
                        "step": 5,
                        "ts": 5,
                    }
                ),
            ]
        )
        + "\n",
    )
    # agent_interactions.csv：L2/L3/L4/L6 = Alice，L5 = Bob；
    # L3 含多行 quoted Observation（真实换行，验证记录序号而非物理行号）；
    # L4 含 secret（真实换行，验证 redact_text 掩码）；
    # L6 含超长 Observation。
    _write(
        run / "agent_interactions.csv",
        AGENT_CSV_HEADER
        + '\n"1","Alice","explore","{}","Explore()","Directly around me, I can see: Names: [CaldorFire]","in1","Alice narrative one","alice thought","run1","a1","tool_result","10",""\n'
        + '"2","Alice","get_supply","{\\"source_id\\": \\"ReservoirUtah\\"}","GetSupply(ReservoirUtah)","line1\nline2\nNames: [ReservoirUtah]","in2","","","run1","a2","tool_result","20",""\n'
        + '"5","Alice","report_observation","{}","ReportObservation()","Inventory: 3\napi_key: sk-abc12345secret\nNames: [CaldorFire]","in3","","","run1","a3","tool_result","30",""\n'
        + '"1","Bob","explore","{}","Explore()","Directly around me: Empty","in4","","","run1","b1","tool_result","10",""\n'
        + f'"9","Alice","explore","{{}}","Explore()","{"H" * 6000}","in5","","","run1","a4","tool_result","10",""\n',
    )
    _write(
        run / "semantic_map.jsonl",
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": 1,
                        "event_type": "observation_ingested",
                        "observation": {
                            "reporter": "Alice",
                            "step": 0,
                            "object_type": "fire",
                            "name": "CaldorFire",
                            "position": [4, 6, 0],
                            "confidence": 1.0,
                            "source_task_id": "t1",
                            "note": "chemical fire",
                        },
                        "object": {
                            "object_type": "fire",
                            "name": "CaldorFire",
                            "position": [4, 6, 0],
                            "attributes": {},
                            "status": "unknown",
                            "last_seen_step": 0,
                            "sources": [],
                            "confidence": 1.0,
                            "conflict": False,
                        },
                    }
                ),
                json.dumps(
                    {
                        "ts": 2,
                        "event_type": "observation_ingested",
                        "observation": {
                            "reporter": "Bob",
                            "step": 1,
                            "object_type": "fire",
                            "name": "GreatFire",
                            "position": [22, 18, 0],
                            "confidence": 0.9,
                            "source_task_id": "t2",
                            "note": "large fire",
                        },
                        "object": {
                            "object_type": "fire",
                            "name": "GreatFire",
                            "position": [22, 18, 0],
                            "attributes": {},
                            "status": "unknown",
                            "last_seen_step": 1,
                            "sources": [],
                            "confidence": 0.9,
                            "conflict": False,
                        },
                    }
                ),
                json.dumps(
                    {
                        "ts": 3,
                        "event_type": "observation_ingested",
                        "observation": {
                            "reporter": "Alice",
                            "step": 2,
                            "object_type": "person",
                            "name": "LostPersonTimmy",
                            "position": [8, 22, 0],
                            "confidence": 1.0,
                            "source_task_id": "",
                            "note": "",
                        },
                        "object": {
                            "object_type": "person",
                            "name": "LostPersonTimmy",
                            "position": [8, 22, 0],
                            "attributes": {},
                            "status": "unknown",
                            "last_seen_step": 2,
                            "sources": [],
                            "confidence": 1.0,
                            "conflict": False,
                        },
                    }
                ),
            ]
        )
        + "\n",
    )
    _write(
        run / "map_summary.jsonl",
        json.dumps(
            {
                "env_step": 1,
                "base_revision": 1,
                "map_revision": 2,
                "trigger_reasons": ["periodic"],
                "status": "success",
                "summary": "第1步：发现 CaldorFire 与 LostPersonTimmy。",
            }
        )
        + "\n"
        + json.dumps(
            {
                "env_step": 8,
                "base_revision": 2,
                "map_revision": 3,
                "trigger_reasons": ["fire_change"],
                "status": "success",
                "summary": "第8步：GreatFire 已扑灭。",
            }
        )
        + "\n",
    )
    _write(
        run / "coordinator" / "unnamed_task.ndjson",
        "\n".join(
            [
                json.dumps({"ts": "2026-08-09T12:00:00Z", "event": "llm_request", "messages": []}),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:00:01Z",
                        "event": "llm_response",
                        "content": "Dispatch Alice to explore the north sector.",
                        "tool_calls": [],
                        "finish_reason": "stop",
                        "usage": {},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:00:02Z",
                        "event": "tool_result",
                        "tool_name": "query_workers",
                        "success": True,
                        "result": "- Alice",
                    }
                ),
            ]
        )
        + "\n",
    )
    _write(
        run / "coordinator" / "20260809_120000.ndjson",
        "\n".join(
            [
                json.dumps({"event": "meta", "task_id": "x", "friendly_name": "20260809_120000"}),
                json.dumps(
                    {
                        "timestamp": "2026-08-09T12:00:00.000Z",
                        "event": "task_start",
                        "source": "executor",
                        "data": {"query": "Extinguish all fires"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-09T12:00:03.000Z",
                        "event": "llm_response",
                        "source": "router",
                        "data": {"content": "Let me plan the mission.", "tool_calls": []},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-09T12:00:04.000Z",
                        "event": "llm_response",
                        "source": "router",
                        "data": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "query_workers", "arguments": {}},
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
    )
    _write(
        run / "workers" / "Alice" / "Alice" / "abc.ndjson",
        "\n".join(
            [
                json.dumps({"ts": "2026-08-09T12:01:00Z", "event": "llm_request", "messages": []}),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:01:01Z",
                        "event": "llm_response",
                        "content": "I need to get Sand from ReservoirUtah first.",
                        "tool_calls": [],
                        "finish_reason": "stop",
                        "usage": {},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:01:02Z",
                        "event": "llm_response",
                        "content": "   ",
                        "tool_calls": [],
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:01:03Z",
                        "event": "llm_response",
                        "content": "",
                        "tool_calls": [
                            {"id": "c2", "type": "function", "function": {"name": "navigate_to"}}
                        ],
                    }
                ),
            ]
        )
        + "\n",
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    _build_run(run)
    return run


def _session(run_dir: Path, **kwargs) -> RetrievalSession:
    return RetrievalSession(run_dir, **kwargs)


def _tools(run_dir: Path, **kwargs) -> dict[str, object]:
    return {t.name: t for t in make_tools(_session(run_dir, **kwargs))}


# ---------------------------------------------------------------------------
# 工具集契约
# ---------------------------------------------------------------------------


class TestToolSet:
    def test_make_tools_returns_six_named_tools(self):
        tools = make_tools(_session(Path(".")))
        assert [t.name for t in tools] == [
            "read_dispatch_history",
            "read_worker_state",
            "read_agent_narrative",
            "read_coordinator_reasoning",
            "read_tool_trace",
            "query_semantic_map",
        ]

    def test_quota_constants_match_design(self):
        assert QUOTA_FAMILY_AGENT == 4
        assert QUOTA_DIMENSION_SUBAGENT == 6
        assert QUOTA_JOB_TOTAL == 28
        assert MAX_RETURN_CHARS == 4000

    def test_session_rejects_missing_run_dir(self, tmp_path):
        with pytest.raises(RetrievalError, match="not a directory"):
            _session(tmp_path / "nope")

    def test_session_rejects_bad_quota(self, run_dir):
        with pytest.raises(RetrievalError, match="max_calls"):
            _session(run_dir, max_calls=0)


# ---------------------------------------------------------------------------
# 各工具检索正确性
# ---------------------------------------------------------------------------


class TestReadDispatchHistory:
    def test_returns_csv_and_events_records(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_dispatch_history"].invoke({})
        assert "step=0 assigned_to=Alice event=assign_task" in out
        assert "step=5 assigned_to=Alice event=assign_task" in out
        # events.ndjson：assign_task / cancel_task / send_message 都算派发记录，
        # 非 Coordinator 事件不出现。
        assert "type=assign_task target=Bob" in out
        assert "type=cancel_task target=t1" in out
        assert "type=send_message target=t2" in out
        assert "report_observation" not in out

    def test_step_range_filter(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_dispatch_history"].invoke({"step_range": "0"})
        assert "step=0 assigned_to=Alice" in out
        assert "step=5 assigned_to=Alice" not in out

    def test_agent_filter(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_dispatch_history"].invoke({"agent": "Bob"})
        assert "assigned_to=Bob" in out
        assert "assigned_to=Alice" not in out
        assert "target=Bob" in out
        assert "target=Alice" not in out

    def test_ref_headers_carry_logical_lines_and_digest(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_dispatch_history"].invoke({})
        headers = [
            line for line in out.splitlines() if line.startswith("--- ref:")
        ]
        assert len(headers) == 2
        for header in headers:
            match = _REF_HEADER_RE.match(header)
            assert match, header
            rel = match.group(1)
            digest = match.group(4)
            assert digest == c.sha256_hex((run_dir / rel).read_bytes())
        assert "router_interactions.csv" in headers[0]
        assert "events.ndjson" in headers[1]

    def test_missing_files_degrade_gracefully(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        tools = _tools(empty)
        out = tools["read_dispatch_history"].invoke({})
        assert "no router_interactions.csv" in out
        assert "no events.ndjson" in out
        assert "Error" not in out

    def test_invalid_step_range_rejected(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_dispatch_history"].invoke({"step_range": "abc"})
        assert out.startswith("Error: invalid step_range")
        out2 = tools["read_dispatch_history"].invoke({"step_range": "5-1"})
        assert out2.startswith("Error: invalid step_range")


class TestReadWorkerState:
    def test_lists_actions_with_observation_previews(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke({"agent": "Alice"})
        # 引用头在前，正文在后。
        assert out.startswith("--- ref: agent_interactions.csv:")
        assert "agent=Alice actions in scope:" in out
        assert "explore x2" in out
        assert "get_supply x1" in out
        assert "L2: step=1 tool=explore action=Explore()" in out
        assert "observation: Directly around me" in out

    def test_multiline_csv_field_keeps_record_index(self, run_dir):
        # L3 的 Observation 含换行：逻辑行号必须是记录序号（含表头），
        # 不是物理行号；L4 的 secret 行证明多行字段被正确解析为同一记录。
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke({"agent": "Alice", "step_range": "1-2"})
        assert "--- ref: agent_interactions.csv:L2-L3" in out
        assert "L3: step=2 tool=get_supply" in out
        assert "line1 line2" in out

    def test_step_range_filters(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke({"agent": "Alice", "step_range": "9"})
        assert "L6: step=9" in out
        assert "L2: step=1" not in out

    def test_unknown_agent_reports_no_records(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke({"agent": "Carol"})
        assert "no agent_interactions records for agent 'Carol'" in out

    def test_missing_agent_param_rejected(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke({"agent": ""})
        assert out.startswith("Error:")


class TestReadAgentNarrative:
    def test_returns_worker_llm_narratives(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_agent_narrative"].invoke({"agent": "Alice"})
        assert "workers/Alice/Alice/abc.ndjson" in out
        assert "I need to get Sand from ReservoirUtah first." in out
        # 空 content / 纯空白 content 的 llm_response 不算旁白。
        assert "navigate_to" not in out

    def test_unknown_agent_graceful(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_agent_narrative"].invoke({"agent": "Carol"})
        assert "no narrative files for agent 'Carol'" in out

    def test_step_range_note(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_agent_narrative"].invoke(
            {"agent": "Alice", "step_range": "1-2"}
        )
        assert "step_range filter could not be applied" in out


class TestReadCoordinatorReasoning:
    def test_covers_both_ndjson_shapes(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_coordinator_reasoning"].invoke({})
        # unnamed_task.ndjson：顶层 content。
        assert "src=unnamed" in out
        assert "Dispatch Alice to explore the north sector." in out
        # <timestamp>.ndjson：data.content（source=router）。
        assert "src=router" in out
        assert "Let me plan the mission." in out
        # 只有 tool_calls 无 content 的轮次也要保留（决策理由 = 调用意图）。
        assert "[tool_calls: query_workers]" in out

    def test_missing_coordinator_dir_graceful(self, tmp_path):
        run = tmp_path / "run"
        run.mkdir()
        tools = _tools(run)
        out = tools["read_coordinator_reasoning"].invoke({})
        assert "no coordinator/*.ndjson" in out


class TestReadToolTrace:
    def test_raw_rows_include_llm_output_not_input_thinking(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_tool_trace"].invoke({"agent": "Alice"})
        assert "L2: step=1 agent=Alice tool=explore" in out
        assert "llm_output: Alice narrative one" in out
        assert "alice thought" not in out  # Thinking 列不出现
        assert "in1" not in out  # LLMInput 列不出现

    def test_tool_name_filter(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_tool_trace"].invoke({"tool_name": "get_supply"})
        assert "L3: step=2 agent=Alice tool=get_supply" in out
        assert "tool=explore" not in out

    def test_no_matches(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_tool_trace"].invoke({"tool_name": "no_such_tool"})
        assert out == "no matching agent_interactions rows"


class TestQuerySemanticMap:
    def test_name_substring_match_case_insensitive(self, run_dir):
        tools = _tools(run_dir)
        out = tools["query_semantic_map"].invoke({"object_name": "cald"})
        assert "name=CaldorFire" in out
        assert "name=GreatFire" not in out
        assert "reporter=Alice step=0 type=fire" in out
        assert "confidence=1.0 source_task_id=t1 note=chemical fire" in out

    def test_object_type_filter(self, run_dir):
        tools = _tools(run_dir)
        out = tools["query_semantic_map"].invoke({"object_type": "fire"})
        assert "name=CaldorFire" in out
        assert "name=GreatFire" in out
        assert "LostPersonTimmy" not in out

    def test_appends_latest_map_summary(self, run_dir):
        tools = _tools(run_dir)
        out = tools["query_semantic_map"].invoke({"object_name": "Timmy"})
        assert "name=LostPersonTimmy" in out
        assert "env_step=8 status=success | 第8步：GreatFire 已扑灭。" in out
        assert "第1步" not in out

    def test_no_match_reports_and_still_returns_summary(self, run_dir):
        tools = _tools(run_dir)
        out = tools["query_semantic_map"].invoke({"object_name": "nope"})
        assert "no semantic_map entries match" in out
        assert "env_step=8" in out


# ---------------------------------------------------------------------------
# 护栏：containment / 截断 / 配额 / 记录 / 脱敏
# ---------------------------------------------------------------------------


class TestContainment:
    def test_evil_paths_rejected(self, run_dir):
        session = _session(run_dir)
        for bad in (
            "/etc/passwd",
            "../escape",
            "a/../../b",
            "workers/..",
            "a//b",
            "a/./b",
            "a\\b",
            "~/.ssh/id_rsa",
            "C:/windows",
            "",
        ):
            with pytest.raises(RetrievalError):
                session.resolve_rel(bad)

    def test_symlink_file_escape_rejected(self, run_dir, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (run_dir / "evil_link.csv").symlink_to(outside)
        session = _session(run_dir)
        with pytest.raises(RetrievalError, match="symlink"):
            session.resolve_rel("evil_link.csv")

    def test_symlink_dir_escape_skipped_in_globs(self, run_dir, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "leak.ndjson").write_text(
            '{"event": "llm_response", "content": "leaked"}\n', encoding="utf-8"
        )
        # workers/Alice/Alice 目录换成指向外部的 symlink。
        alice_dir = run_dir / "workers" / "Alice" / "Alice"
        (alice_dir / "abc.ndjson").unlink()
        alice_dir.rmdir()
        alice_dir.symlink_to(outside_dir, target_is_directory=True)
        session = _session(run_dir)
        assert session._list_ndjson_files("workers/Alice/Alice") == []
        tools = make_tools(session)
        out = tools[2].invoke({"agent": "Alice"})
        assert "leaked" not in out
        assert "no narrative files" in out

    def test_missing_file_rejected(self, run_dir):
        session = _session(run_dir)
        with pytest.raises(RetrievalError, match="not found"):
            session.resolve_rel("no_such_file.csv")


class TestTruncation:
    @pytest.fixture
    def big_run(self, tmp_path: Path) -> Path:
        """多行大返回 fixture：20 行 × ~250 字符 observation → 正文 > 4000。"""
        run = tmp_path / "big_run"
        _build_run(run)
        rows = [
            (
                "Step,Agent,ToolName,ToolArgs,Action,Observation,LLMInput,LLMOutput,"
                "Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,ErrorType"
            )
        ]
        for i in range(20):
            obs = "O" * 250
            rows.append(
                f'"10","Alice","explore","{{}}","Explore()","{obs}","","","",'
                f'"run","a{i}","tool_result","10",""'
            )
        (run / "agent_interactions.csv").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )
        return run

    def test_return_capped_at_4000_chars_with_marker(self, big_run):
        tools = _tools(big_run)
        out = tools["read_worker_state"].invoke({"agent": "Alice"})
        assert len(out) <= MAX_RETURN_CHARS
        assert "chars omitted]" in out
        assert "O" * 100 in out  # 截断保留前缀内容

    def test_truncation_recorded(self, big_run):
        session = _session(big_run)
        tools = make_tools(session)
        tools[1].invoke({"agent": "Alice"})
        record = session.records[0]
        assert record["truncated"] is True
        assert record["bytes"] <= MAX_RETURN_CHARS
        assert record["returned_refs"] == ["agent_interactions.csv"]

    def test_small_returns_not_truncated(self, run_dir):
        session = _session(run_dir)
        tools = make_tools(session)
        out = tools[3].invoke({})  # coordinator reasoning
        assert "chars omitted]" not in out
        assert session.records[0]["truncated"] is False


class TestQuotaAndRecords:
    def test_quota_exhausted_returns_error(self, run_dir):
        session = _session(run_dir, max_calls=1)
        tools = make_tools(session)
        first = tools[0].invoke({})
        assert "Error" not in first
        second = tools[1].invoke({"agent": "Alice"})
        assert second.startswith("Error: retrieval quota exhausted")
        assert session.calls == 1
        assert len(session.records) == 2

    def test_record_sink_receives_full_contract(self, run_dir):
        sink: list[dict] = []
        session = _session(run_dir, record_sink=sink.append)
        tools = make_tools(session)
        tools[5].invoke({"object_name": "cald"})
        assert len(sink) == 1
        record = sink[0]
        assert set(record) == {"tool", "params", "returned_refs", "bytes", "truncated"}
        assert record["tool"] == "query_semantic_map"
        assert record["params"] == {"object_name": "cald", "object_type": ""}
        assert record["returned_refs"] == [
            "semantic_map.jsonl",
            "map_summary.jsonl",
        ]
        assert isinstance(record["bytes"], int)
        assert record["truncated"] is False

    def test_error_calls_are_recorded(self, run_dir):
        sink: list[dict] = []
        session = _session(run_dir, record_sink=sink.append)
        tools = make_tools(session)
        out = tools[1].invoke({"agent": "Alice", "step_range": "x"})
        assert out.startswith("Error:")
        assert len(sink) == 1
        assert sink[0]["tool"] == "read_worker_state"
        assert sink[0]["returned_refs"] == []

    def test_invalid_step_range_does_not_consume_quota_twice(self, run_dir):
        session = _session(run_dir, max_calls=3)
        tools = make_tools(session)
        tools[0].invoke({"step_range": "bad"})
        tools[0].invoke({})
        assert session.calls == 2  # 错误调用占一次配额，正常调用占一次


class TestSecretMasking:
    def test_secret_values_redacted(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_tool_trace"].invoke({"agent": "Alice"})
        assert "sk-abc12345secret" not in out
        assert "api_key: [REDACTED]" in out


class TestRefHeaderFormat:
    def test_single_record_span_is_single_line_ref(self, run_dir):
        tools = _tools(run_dir)
        out = tools["read_worker_state"].invoke(
            {"agent": "Alice", "step_range": "9"}
        )
        assert "--- ref: agent_interactions.csv:L6 sha256=" in out

    def test_digest_is_file_level_sha256(self, run_dir):
        session = _session(run_dir)
        tools = make_tools(session)
        out = tools[1].invoke({"agent": "Alice", "step_range": "1-2"})
        digest = c.sha256_hex((run_dir / "agent_interactions.csv").read_bytes())
        assert f"sha256={digest}" in out
        # 文件级 digest 缓存：同一文件两次读取 digest 相同。
        out2 = tools[1].invoke({"agent": "Alice", "step_range": "5"})
        assert f"sha256={digest}" in out2
