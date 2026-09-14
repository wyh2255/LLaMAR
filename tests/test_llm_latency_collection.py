"""P1-③ LLMLatencyMs 采集接线测试（mock 时钟，不真调 LLM）。

链路：Agent 侧对 LLM 往返计时 → step_callback 事件的 ``llm_latency_ms``
→ SARWorker / SARCoordinator 回调 → ``token_usage.csv`` 的 ``LLMLatencyMs``。

覆盖：
1. 两份 Agent 拷贝（worker_agent / router_agent）正常路径：mock 时钟下事件
   携带的 ``llm_latency_ms`` 等于模拟的 LLM 往返耗时；
2. 异常路径（usage=None 的 status=error 标记行）同样携带真实耗时；
3. 消费者把 ``llm_latency_ms`` 落到 token_usage.csv（ok / error 两种行均验证），
   字段缺失时回退 0.0（旧行为，不破坏其他 emitter）；
4. MapAgent / MapSummarizer 的 token sink 同样携带 ``llm_latency_ms``。
"""

from __future__ import annotations

import csv
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PKGS = ["Agent.router_agent", "Agent.worker_agent"]


# ── helpers ────────────────────────────────────────────────────────────


class _FakeClock:
    """可控单调时钟：假 LLM 在 generate() 内推进它，模拟已知往返耗时。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _load(pkg: str):
    agent_mod = importlib.import_module(f"{pkg}.agent")
    schema = importlib.import_module(f"{pkg}.schema")
    return agent_mod, schema


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _usage(
    prompt: int = 10,
    completion: int = 5,
    total: int = 15,
    hit: int = 0,
    miss: int = 15,
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )


class _FakeBarrier:
    _step_counter = 7


# ── 1/2. Agent 事件源携带耗时 ──────────────────────────────────────────


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_llm_response_event_carries_mocked_latency(pkg, tmp_path, monkeypatch):
    """正常路径：llm_response 事件的 llm_latency_ms == 模拟往返耗时。"""
    agent_mod, schema = _load(pkg)
    clock = _FakeClock()
    monkeypatch.setattr(agent_mod, "perf_counter", clock)

    class _TimedLLM:
        async def generate(self, messages, tools=None):
            clock.advance(2.5)
            return schema.LLMResponse(
                content="done",
                finish_reason="stop",
                usage=schema.TokenUsage(
                    prompt_tokens=10, completion_tokens=5, total_tokens=15
                ),
            )

    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event_type, **kw):
        seen.append((event_type, kw))

    agent = agent_mod.Agent(
        llm_client=_TimedLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(step_callback=cb, task_id="t-latency")
    assert result.content == "done"

    responses = [kw for t, kw in seen if t == "llm_response"]
    assert responses, f"no llm_response event emitted: {[t for t, _ in seen]}"
    assert responses[0]["llm_latency_ms"] == pytest.approx(2500.0)
    assert responses[0]["usage"] is not None


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_llm_error_marker_carries_mocked_latency(pkg, tmp_path, monkeypatch):
    """异常路径：usage=None 的 error 标记事件同样如实携带耗时。"""
    agent_mod, _ = _load(pkg)
    clock = _FakeClock()
    monkeypatch.setattr(agent_mod, "perf_counter", clock)

    class _BoomLLM:
        async def generate(self, messages, tools=None):
            clock.advance(1.25)
            raise RuntimeError("provider boom")

    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event_type, **kw):
        seen.append((event_type, kw))

    agent = agent_mod.Agent(
        llm_client=_BoomLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(step_callback=cb, task_id="t-latency-error")
    assert result.success is False

    responses = [kw for t, kw in seen if t == "llm_response"]
    assert responses, f"no llm_response marker emitted: {[t for t, _ in seen]}"
    assert responses[0]["status"] == "error"
    assert responses[0]["usage"] is None
    assert responses[0]["llm_latency_ms"] == pytest.approx(1250.0)


# ── 3. 消费者落盘到 token_usage.csv ────────────────────────────────────


def test_worker_callback_writes_latency_to_token_usage_csv(tmp_path):
    """SARWorker: ok 行与 error 行的 LLMLatencyMs 都落到 CSV。"""
    from sar_orch.logger import ExperimentLogger
    from sar_orch.worker import SARWorker

    logger = ExperimentLogger(log_dir=str(tmp_path))
    worker = SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    worker._on_step_event(
        "llm_response",
        content="hi",
        tool_calls=[],
        usage=_usage(),
        llm_latency_ms=1234.5,
    )
    # 错误路径：usage=None 的零值标记行也带真实耗时
    worker._on_step_event(
        "llm_response",
        content="boom",
        tool_calls=[],
        usage=None,
        status="error",
        llm_latency_ms=250.25,
    )
    logger.close()

    rows = _read_csv(tmp_path / "token_usage.csv")
    assert len(rows) == 2
    assert rows[0]["Agent"] == "Alice"
    assert rows[0]["Step"] == "7"
    assert rows[0]["LLMLatencyMs"] == "1234.5"
    assert rows[0]["Status"] == "ok"
    assert rows[1]["LLMLatencyMs"] == "250.25"
    assert rows[1]["Status"] == "error"
    assert rows[1]["TotalTokens"] == "0"


def test_worker_callback_missing_latency_defaults_to_zero(tmp_path):
    """旧 emitter（事件无 llm_latency_ms）保持 0.0 历史行为。"""
    from sar_orch.logger import ExperimentLogger
    from sar_orch.worker import SARWorker

    logger = ExperimentLogger(log_dir=str(tmp_path))
    worker = SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    worker._on_step_event("llm_response", content="hi", tool_calls=[], usage=_usage())
    logger.close()

    rows = _read_csv(tmp_path / "token_usage.csv")
    assert rows[0]["LLMLatencyMs"] == "0.0"


def test_router_callback_writes_latency_to_token_usage_csv(tmp_path):
    """SARCoordinator router 回调：ok / error 行都落到 CSV。"""
    from sar_orch.coordinator import SARCoordinator
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    coord = SARCoordinator(
        barrier=_FakeBarrier(), exp_logger=logger, coordinator_secret=bytes(range(32))
    )
    coord._on_router_event(
        "llm_response",
        content="hi",
        tool_calls=[],
        usage=_usage(prompt=100, completion=20, total=120),
        llm_latency_ms=888.0,
    )
    coord._on_router_event(
        "llm_response",
        content="boom",
        tool_calls=[],
        usage=None,
        status="error",
        llm_latency_ms=99.5,
    )
    logger.close()

    rows = _read_csv(tmp_path / "token_usage.csv")
    assert len(rows) == 2
    assert rows[0]["Agent"] == "Coordinator"
    assert rows[0]["LLMLatencyMs"] == "888.0"
    assert rows[0]["Status"] == "ok"
    assert rows[1]["LLMLatencyMs"] == "99.5"
    assert rows[1]["Status"] == "error"


# ── 4. Map 侧 sink 覆盖 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_map_agent_sink_carries_latency(monkeypatch):
    """MapAgent（LangGraph 查询）的 token sink 携带 llm_latency_ms。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from langchain_core.messages import AIMessage

    import sar_orch.map_agent.server as server_mod

    clock = _FakeClock()
    monkeypatch.setattr(server_mod, "monotonic", clock)

    async def _ainvoke(*args, **kwargs):
        clock.advance(3.0)
        return {
            "messages": [
                AIMessage(
                    content="Mock answer",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                )
            ]
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=_ainvoke)

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    server_mod.set_llm_client(mock_llm)
    captured: dict[str, Any] = {}
    server_mod.set_token_sink(lambda **kw: captured.update(kw))
    try:
        with patch(
            "sar_orch.map_agent.server.build_map_agent_graph",
            return_value=mock_graph,
        ):
            result = await server_mod._query_natural(query="q")
    finally:
        server_mod.set_token_sink(None)
        server_mod.set_llm_client(None)

    assert result["answer"] == "Mock answer"
    assert captured["agent"] == "MapAgent"
    assert captured["prompt_tokens"] == 10
    assert captured["llm_latency_ms"] == pytest.approx(3000.0)


@pytest.mark.asyncio
async def test_map_summarizer_sink_carries_latency(tmp_path, monkeypatch):
    """MapSummarizer 的 token sink 携带摘要 LLM 调用的墙钟耗时。"""
    import sar_orch.map.summarizer as summ_mod

    clock = _FakeClock()
    monkeypatch.setattr(
        summ_mod, "time", SimpleNamespace(monotonic=clock, time=lambda: 0.0)
    )

    class _FakeResponse:
        def __init__(self, content: str, usage: Any) -> None:
            self.content = content
            self.usage = usage

    class _TimedLLM:
        async def generate(self, **kwargs):
            clock.advance(1.5)
            return _FakeResponse("Fire F1 intensity increased.", _usage(50, 30, 80))

    sink_calls: list[dict[str, Any]] = []
    summarizer = summ_mod.MapSummarizer(
        summary_path=tmp_path / "latency_sink.jsonl",
        token_usage_sink=lambda **kw: sink_calls.append(kw),
    )
    await summarizer.maybe_summarize(
        llm_client=_TimedLLM(),
        env_step=5,
        map_revision=1,
        snapshot={},
        map_delta={"change_count": 1, "fires": {"gained": [{"name": "F1"}]}},
    )

    assert len(sink_calls) == 1, f"expected 1 sink call, got {sink_calls}"
    assert sink_calls[0]["agent"] == "MapSummarizer"
    assert sink_calls[0]["prompt_tokens"] == 50
    assert sink_calls[0]["llm_latency_ms"] == pytest.approx(1500.0)
