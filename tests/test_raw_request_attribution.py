"""raw_request 事件归属回归测试（A100 首跑报告 §4-4 收编）。

A100 首跑 ``coordinator/unknown.ndjson`` 里落了一条 ``has_metadata=false`` 的
``raw_request``：executor 在任务物化之前用 fallback id ``"unknown"`` 记录，
事件脱离了本 run 的任务文件。收编后：``raw_request`` 在任务物化 +
``init_task`` 之后记录，与 ``task_start`` 落进同一个任务文件
（friendly_name 缺省时 = 时间戳文件），不再进 ``unknown.ndjson``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest

from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
from a2a.coordinator.agent_registry import AgentRegistry
from a2a.coordinator.task_logger import TaskLogger
from Agent.router_agent.schema import RunResult

_TASK_ID = "task-raw-request"
_CONTEXT_ID = "ctx-raw-request"


class _RecordingEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _run_executor(tmp_path: Path) -> Path:
    """跑一遍最小 executor 流程（submit 打桩），返回日志目录。"""
    task_logger = TaskLogger(base_dir=str(tmp_path / "logs"))
    executor = CoordinatorAgentExecutor(
        registry=AgentRegistry(), task_logger=task_logger
    )

    async def submit_stub(*args: Any, **kwargs: Any) -> RunResult:
        return RunResult(content="done", success=True)

    executor._controller.submit = submit_stub  # noqa: SLF001

    request = SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            parts=[Part(text="say hello")],
            task_id=_TASK_ID,
            context_id=_CONTEXT_ID,
        )
    )
    context = RequestContext(
        call_context=ServerCallContext(),
        request=request,
        task_id=_TASK_ID,
        context_id=_CONTEXT_ID,
    )

    await executor.execute(context, _RecordingEventQueue())
    return tmp_path / "logs"


@pytest.mark.asyncio
async def test_raw_request_lands_in_task_file_not_unknown(tmp_path: Path) -> None:
    base = await _run_executor(tmp_path)
    files = sorted(base.glob("*.ndjson"))
    assert files, "executor should have written at least one ndjson file"

    task_files = [f for f in files if f.name != "unknown.ndjson"]
    raw_request_files = [
        f
        for f in task_files
        if any(e.get("event") == "raw_request" for e in _read_events(f))
    ]
    assert len(raw_request_files) == 1, (
        f"raw_request must land in exactly one task file, got {[f.name for f in files]}"
    )

    event_types = [e["event"] for e in _read_events(raw_request_files[0])]
    assert "task_start" in event_types
    assert event_types.index("raw_request") < event_types.index("task_start")

    unknown = base / "unknown.ndjson"
    if unknown.exists():
        assert all(e.get("event") != "raw_request" for e in _read_events(unknown))


@pytest.mark.asyncio
async def test_raw_request_payload_keeps_request_snapshot(tmp_path: Path) -> None:
    """收编不改变事件载荷：query_preview / has_metadata 语义原样保留。"""
    base = await _run_executor(tmp_path)
    events = [
        e
        for f in base.glob("*.ndjson")
        for e in _read_events(f)
        if e.get("event") == "raw_request"
    ]
    assert len(events) == 1
    data = events[0]["data"]
    assert data["query_preview"] == "say hello"
    assert data["task_id"] == _TASK_ID
    assert data["context_id"] == _CONTEXT_ID
    # 该 dispatch 未携带 metadata —— 属输入事实（记录为 false 即正确行为）。
    assert data["has_metadata"] is False
