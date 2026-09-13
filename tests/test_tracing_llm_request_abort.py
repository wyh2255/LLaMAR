"""F3 tracing 回归测试：llm_request 进主 trace + log_abort 终止标记 + step_index + 在飞 generate 可取消。

覆盖两份独立拷贝（router_agent / worker_agent，AGENTS.md 契约：同步改动）：
- cancel_event 在 LLM 在飞时置位 → generate 被取消，NDJSON 出现 status=cancelled 终止事件
- LLM 异常 → status=error 终止事件
- llm_request 事件经 step_callback 发出（coordinator sink 转发 TaskLogger 的主 trace 链路）
- llm_response / tool_result 事件携带 step_index
"""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PKGS = ["Agent.router_agent", "Agent.worker_agent"]


def _load(pkg: str):
    agent_mod = importlib.import_module(f"{pkg}.agent")
    schema = importlib.import_module(f"{pkg}.schema")
    return agent_mod, schema


def _read_ndjson(agent) -> list[dict]:
    """Read the AgentLogger NDJSON trace written by the last run."""
    path = agent.logger.get_log_file_path()
    assert path is not None and path.exists(), f"NDJSON trace missing: {path}"
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_inflight_cancel_cancels_generate_and_writes_cancelled_marker(
    pkg, tmp_path
):
    """cancel_event 在 LLM 在飞时置位：generate 被取消，trace 有 status=cancelled。"""
    agent_mod, schema = _load(pkg)

    class _SlowLLM:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False

        async def generate(self, messages, tools=None):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return schema.LLMResponse(content="done", finish_reason="stop")

    llm = _SlowLLM()
    agent = agent_mod.Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    cancel_event = asyncio.Event()

    async def _cancel_after_start():
        await llm.started.wait()
        cancel_event.set()

    run_task = asyncio.ensure_future(
        agent.run(cancel_event=cancel_event, task_id="t-cancel")
    )
    cancel_task = asyncio.ensure_future(_cancel_after_start())
    result = await run_task
    await cancel_task

    # generate 确实被取消（在飞请求终止，而不是等到返回）
    assert llm.cancelled, "in-flight generate was not cancelled"
    assert result.success is None
    assert "cancel" in result.content.lower()

    events = _read_ndjson(agent)
    markers = [e for e in events if e.get("status") == "cancelled"]
    assert markers, f"no cancelled marker in trace: {events}"
    assert markers[0]["event"] == "llm_response"  # 复用 llm_response schema
    assert markers[0]["step_index"] == 0
    # 被中断的请求没有正常 llm_response
    assert not [e for e in events if e.get("event") == "llm_response" and "status" not in e]


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_llm_exception_writes_error_marker(pkg, tmp_path):
    """LLM 异常路径：NDJSON 出现 status=error 终止事件（历史缺口回归）。"""
    agent_mod, _ = _load(pkg)

    class _FailingLLM:
        async def generate(self, messages, tools=None):
            raise RuntimeError("provider boom")

    agent = agent_mod.Agent(
        llm_client=_FailingLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )

    result = await agent.run(task_id="t-error")

    assert result.success is False
    events = _read_ndjson(agent)
    markers = [e for e in events if e.get("status") == "error"]
    assert markers, f"no error marker in trace: {events}"
    assert markers[0]["event"] == "llm_response"
    assert markers[0]["step_index"] == 0


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_llm_request_event_reaches_step_callback(pkg, tmp_path):
    """llm_request 事件经 step_callback 发出，携带 step_index/messages/tools。"""
    agent_mod, schema = _load(pkg)

    class _OkLLM:
        async def generate(self, messages, tools=None):
            return schema.LLMResponse(content="done", finish_reason="stop")

    seen = []

    async def cb(event_type, **kw):
        seen.append((event_type, kw))

    agent = agent_mod.Agent(
        llm_client=_OkLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    await agent.run(step_callback=cb, task_id="t-cb")

    reqs = [kw for t, kw in seen if t == "llm_request"]
    assert reqs, f"no llm_request event emitted: {[t for t, _ in seen]}"
    assert reqs[0]["step_index"] == 0
    assert reqs[0]["messages"]  # 至少含 system prompt
    assert reqs[0]["tools"] == []


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_llm_response_and_tool_result_carry_step_index(pkg, tmp_path):
    """正常路径：llm_response / tool_result 事件带 step_index 字段。"""
    agent_mod, schema = _load(pkg)

    class _ToolLLM:
        def __init__(self):
            self.call_count = 0

        async def generate(self, messages, tools=None):
            self.call_count += 1
            if self.call_count == 1:
                return schema.LLMResponse(
                    content="",
                    tool_calls=[
                        schema.ToolCall(
                            id="call-1",
                            type="function",
                            function=schema.FunctionCall(name="noop", arguments={}),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return schema.LLMResponse(content="done", finish_reason="stop")

    class _NoopTool:
        name = "noop"
        description = "noop tool"

        async def execute(self, **kwargs):
            return SimpleNamespace(success=True, content="ok", data=None, error="")

    agent = agent_mod.Agent(
        llm_client=_ToolLLM(),
        system_prompt="test",
        tools=[_NoopTool()],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(task_id="t-stepidx")
    assert result.content == "done"

    events = _read_ndjson(agent)
    responses = [e for e in events if e.get("event") == "llm_response"]
    tool_results = [e for e in events if e.get("event") == "tool_result"]
    assert responses, f"no llm_response events: {events}"
    assert all("step_index" in e for e in responses), responses
    assert responses[0]["step_index"] == 0
    assert tool_results, f"no tool_result events: {events}"
    assert all("step_index" in e for e in tool_results), tool_results
    assert tool_results[0]["step_index"] == 0


@pytest.mark.asyncio
async def test_coordinator_sink_forwards_llm_request_to_task_logger(tmp_path):
    """主 trace：A2ACoordinatorSink 将 llm_request 转发给 TaskLogger。"""
    from a2a.coordinator.sink import A2ACoordinatorSink
    from a2a.coordinator.task_logger import TaskLogger

    class _FakeQueue:
        def __init__(self):
            self.enqueue_event = AsyncMock()

    task_logger = TaskLogger(base_dir=str(tmp_path / "logs"))
    task_logger.init_task("task-f3", friendly_name="task-f3")
    sink = A2ACoordinatorSink(
        _FakeQueue(), "task-f3", "ctx-1", task_logger, SimpleNamespace(progress=0.5)
    )

    await sink.emit(
        "llm_request",
        messages=[SimpleNamespace(role="system", content="sys")],
        tools=[SimpleNamespace(name="update_plan")],
        step_index=3,
    )

    log_path = tmp_path / "logs" / "task-f3.ndjson"
    assert log_path.exists(), f"TaskLogger file missing: {log_path}"
    lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()]
    reqs = [e for e in lines if e.get("event") == "llm_request"]
    assert reqs, f"no llm_request in main trace: {lines}"
    data = reqs[-1]["data"]
    assert data["step_index"] == 3
    assert data["tools"] == ["update_plan"]
    assert data["message_count"] == 1
