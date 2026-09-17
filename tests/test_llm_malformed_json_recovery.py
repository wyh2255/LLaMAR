"""F-jsonretry：LLM 响应/tool_call arguments 畸形 JSON → 有界重试 + 降级。

背景（pop-1 step-1 gate 根因）：provider 偶发返回截断/缺分隔符的 tool_call
arguments，``json.loads`` 抛 JSONDecodeError 沿 LLM 客户端冒泡，agent 循环把它
当普通异常走「框架错误」终态，单次坏响应直接崩掉整个 run。

覆盖两条修复链路，双拷贝各一套（router_agent / worker_agent，AGENTS.md 契约）：
1. 客户端（OpenAIClient.generate）：同请求有界重试（默认 2 次，参数可调），
   耗尽后抛类型化 MalformedLLMResponseError（attempts=已耗尝试数）；
2. Agent 循环：畸形响应轮次降级处理（注入一次纠错提示 + NDJSON 诊断记录：
   llm_response(status=degraded) 终止标记与 llm_parse_retry 事件，
   step_callback 补 status=degraded 的零 usage llm_response 标记），
   只有连续 max_parse_degradations 轮才回到框架错误终态。
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

PKGS = ["Agent.router_agent", "Agent.worker_agent"]

pytestmark = pytest.mark.unit


def _load(pkg: str):
    agent_mod = importlib.import_module(f"{pkg}.agent")
    llm_mod = importlib.import_module(f"{pkg}.llm")
    llm_openai = importlib.import_module(f"{pkg}.llm.openai_client")
    retry_mod = importlib.import_module(f"{pkg}.retry")
    schema_mod = importlib.import_module(f"{pkg}.schema")
    return agent_mod, llm_mod, llm_openai, retry_mod, schema_mod


def _make_openai_client(llm_openai, retry_mod, **kwargs):
    """Real OpenAIClient with a stubbed SDK transport (no network)."""
    client = llm_openai.OpenAIClient(
        api_key="test-key",
        api_base="http://localhost:1/v1",
        model="test-model",
        retry_config=retry_mod.RetryConfig(initial_delay=0.0, max_delay=0.0),
        **kwargs,
    )
    return client


def _stub_completion(arguments: str):
    message = SimpleNamespace(
        content="",
        reasoning_details=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="noop", arguments=arguments),
            )
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class _StubCompletions:
    """Feeds canned payloads (or exceptions) to AsyncOpenAI.chat.completions."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload


def _attach_stub(client, payloads) -> _StubCompletions:
    stub = _StubCompletions(payloads)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return stub


MALFORMED_ARGUMENTS = [
    pytest.param('{"a": ', id="truncated"),
    pytest.param('{"a": 1 "b": 2}', id="missing-delimiter"),
    pytest.param("", id="empty-arguments"),
    pytest.param("[1, 2]", id="json-but-not-object"),
]


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_degradation_streak_resets_between_runs(pkg, tmp_path):
    """同一实例两次 run：降级计数跨 run 重置（各自最多 limit 轮降级）。"""
    agent_mod, llm_mod, _, _, _ = _load(pkg)

    class _AlwaysMalformedLLM:
        async def generate(self, messages, tools=None):
            raise _malformed_error(llm_mod)

    agent = agent_mod.Agent(
        llm_client=_AlwaysMalformedLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
        max_parse_degradations=2,
    )

    first = await agent.run(task_id="t-run-1")
    first_events = _read_ndjson(agent)
    second = await agent.run(task_id="t-run-2")
    second_events = _read_ndjson(agent)

    assert first.success is False and second.success is False
    assert len([e for e in first_events if e.get("status") == "degraded"]) == 2
    assert len([e for e in second_events if e.get("status") == "degraded"]) == 2


@pytest.mark.parametrize("pkg", PKGS)
def test_wrapper_passes_retry_budget_to_openai_client(pkg):
    """LLMClient 门面透传 malformed_json_retries；未指定时保持客户端默认 2。"""
    llm_mod = importlib.import_module(f"{pkg}.llm")
    schema_mod = importlib.import_module(f"{pkg}.schema")

    tuned = llm_mod.LLMClient(
        api_key="test-key",
        provider=schema_mod.LLMProvider.OPENAI,
        api_base="http://localhost:1/v1",
        model="test-model",
        malformed_json_retries=5,
    )
    assert tuned._client.malformed_json_retries == 5

    default = llm_mod.LLMClient(
        api_key="test-key",
        provider=schema_mod.LLMProvider.OPENAI,
        api_base="http://localhost:1/v1",
        model="test-model",
    )
    assert default._client.malformed_json_retries == 2


# ── 1. 客户端：同请求有界重试 ───────────────────────────────────────────


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_client_retries_same_request_then_succeeds(pkg):
    """首次畸形、第二次合法：同请求重试成功，parse_retries 如实上报。"""
    _, _, llm_openai, retry_mod, _ = _load(pkg)
    client = _make_openai_client(llm_openai, retry_mod)
    stub = _attach_stub(
        client, [_stub_completion('{"a": '), _stub_completion('{"a": 1}')]
    )

    response = await client.generate([], tools=None)

    assert stub.calls == 2
    assert response.parse_retries == 1
    assert response.tool_calls[0].function.arguments == {"a": 1}


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.parametrize("arguments", MALFORMED_ARGUMENTS)
@pytest.mark.asyncio
async def test_client_exhausts_budget_with_typed_error(pkg, arguments):
    """畸形 payload 重试预算耗尽：抛类型化错误（attempts=3 次尝试）。"""
    _, llm_mod, llm_openai, retry_mod, _ = _load(pkg)
    client = _make_openai_client(llm_openai, retry_mod)
    stub = _attach_stub(client, [_stub_completion(arguments)] * 3)

    with pytest.raises(llm_mod.MalformedLLMResponseError) as excinfo:
        await client.generate([], tools=None)

    assert stub.calls == 3  # 1 次原始 + 默认 2 次同请求重试
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.last_error, (json.JSONDecodeError, ValueError))


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_client_retry_budget_is_configurable(pkg):
    """malformed_json_retries=1 → 2 次尝试后即抛错（预算可调）。"""
    _, llm_mod, llm_openai, retry_mod, _ = _load(pkg)
    client = _make_openai_client(
        llm_openai, retry_mod, malformed_json_retries=1
    )
    stub = _attach_stub(client, [_stub_completion('{"a": ') ] * 2)

    with pytest.raises(llm_mod.MalformedLLMResponseError) as excinfo:
        await client.generate([], tools=None)

    assert stub.calls == 2
    assert excinfo.value.attempts == 2


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_client_zero_retries_single_attempt(pkg):
    """malformed_json_retries=0 → 不重试，一次尝试后抛错（旧行为开关）。"""
    _, llm_mod, llm_openai, retry_mod, _ = _load(pkg)
    client = _make_openai_client(
        llm_openai, retry_mod, malformed_json_retries=0
    )
    stub = _attach_stub(client, [_stub_completion("")])

    with pytest.raises(llm_mod.MalformedLLMResponseError):
        await client.generate([], tools=None)

    assert stub.calls == 1


# ── 2. Agent 循环：降级继续 / 连续失败才终止 ─────────────────────────────


def _read_ndjson(agent) -> list[dict]:
    path = agent.logger.get_log_file_path()
    assert path is not None and path.exists(), f"NDJSON trace missing: {path}"
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _malformed_error(llm_mod, attempts: int = 3):
    return llm_mod.MalformedLLMResponseError(
        "LLM response payload is not valid JSON after "
        f"{attempts} attempt(s): Expecting ',' delimiter",
        attempts=attempts,
        last_error=json.JSONDecodeError("Expecting ',' delimiter", '{"a": ', 6),
    )


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_degrades_round_and_continues_run(pkg, tmp_path):
    """单轮畸形 JSON：不终止 run，注入纠错提示后下一轮正常收敛。"""
    agent_mod, llm_mod, _, _, schema_mod = _load(pkg)

    class _MalformedOnceLLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise _malformed_error(llm_mod)
            return schema_mod.LLMResponse(content="done", finish_reason="stop")

    llm = _MalformedOnceLLM()
    seen: list[tuple[str, dict]] = []

    async def cb(event_type, **kw):
        seen.append((event_type, kw))

    agent = agent_mod.Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(step_callback=cb, task_id="t-degrade")

    # run 正常收敛（单次坏响应不再崩 run）
    assert result.content == "done"
    assert result.success is None
    assert llm.calls == 2

    events = _read_ndjson(agent)
    degraded = [e for e in events if e.get("status") == "degraded"]
    assert len(degraded) == 1, f"degrade marker missing: {events}"
    assert degraded[0]["event"] == "llm_response"  # 复用 llm_response schema
    assert degraded[0]["malformed_attempts"] == 3
    assert degraded[0]["degradation_streak"] == 1
    assert degraded[0]["degradation_limit"] == 3
    assert degraded[0]["step_index"] == 0
    # step_callback 收到同款零 usage 标记（token_usage.csv Status 列接线）
    cb_markers = [
        kw for t, kw in seen if t == "llm_response" and kw.get("status") == "degraded"
    ]
    assert cb_markers and cb_markers[0]["usage"] is None
    assert "llm_latency_ms" in cb_markers[0]

    # 纠错提示进入了下一轮 llm_request（模型确实被告知要重发合法 JSON）
    requests = [kw for t, kw in seen if t == "llm_request"]
    assert len(requests) == 2
    second_messages = requests[1]["messages"]
    assert any(
        isinstance(m.content, str) and "framework-notice" in m.content
        for m in second_messages
    ), "corrective nudge never reached the model"


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_aborts_only_after_consecutive_degraded_rounds(pkg, tmp_path):
    """连续 3 轮畸形 JSON：才回到框架错误终态（默认上限 3）。"""
    agent_mod, llm_mod, _, _, schema_mod = _load(pkg)

    class _AlwaysMalformedLLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            raise _malformed_error(llm_mod)

    llm = _AlwaysMalformedLLM()
    agent = agent_mod.Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=50,
    )
    result = await agent.run(task_id="t-abort")

    assert result.success is False
    assert llm.calls == 3  # 有界：达到降级上限即终止，不无限重试
    assert "3" in result.content

    events = _read_ndjson(agent)
    degraded = [e for e in events if e.get("status") == "degraded"]
    assert len(degraded) == 3
    assert [e["degradation_streak"] for e in degraded] == [1, 2, 3]
    error_markers = [e for e in events if e.get("status") == "error"]
    assert error_markers, f"no terminal error marker: {events}"


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_degradation_streak_resets_after_clean_round(pkg, tmp_path):
    """降级 → 正常 → 降级 → 正常：计数重置，不触发终止。"""
    agent_mod, llm_mod, _, _, schema_mod = _load(pkg)

    class _AlternatingLLM:
        """call 1/3 malformed; call 2 a tool call (loop continues); call 4 done."""

        def __init__(self):
            self.calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls in (1, 3):
                raise _malformed_error(llm_mod)
            if self.calls == 2:
                return schema_mod.LLMResponse(
                    content="",
                    tool_calls=[
                        schema_mod.ToolCall(
                            id="call-1",
                            type="function",
                            function=schema_mod.FunctionCall(name="noop", arguments={}),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return schema_mod.LLMResponse(content="done", finish_reason="stop")

    class _NoopTool:
        name = "noop"
        description = "noop tool"

        async def execute(self, **kwargs):
            return SimpleNamespace(success=True, content="ok", data=None, error="")

    llm = _AlternatingLLM()
    agent = agent_mod.Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[_NoopTool()],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(task_id="t-streak-reset")

    assert result.content == "done"
    assert llm.calls == 4  # 降级后继续跑，第二次降级同样只降级不终止

    events = _read_ndjson(agent)
    degraded = [e for e in events if e.get("status") == "degraded"]
    assert [e["degradation_streak"] for e in degraded] == [1, 1]
    assert not [e for e in events if e.get("status") == "error"]


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_max_parse_degradations_configurable(pkg, tmp_path):
    """max_parse_degradations=1：单轮降级失败即终止（参数可调）。"""
    agent_mod, llm_mod, _, _, schema_mod = _load(pkg)

    class _AlwaysMalformedLLM:
        async def generate(self, messages, tools=None):
            raise _malformed_error(llm_mod)

    agent = agent_mod.Agent(
        llm_client=_AlwaysMalformedLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
        max_parse_degradations=1,
    )
    result = await agent.run(task_id="t-degrade-limit")

    assert result.success is False
    events = _read_ndjson(agent)
    degraded = [e for e in events if e.get("status") == "degraded"]
    assert len(degraded) == 1


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_raw_json_decode_error_also_degrades(pkg, tmp_path):
    """非类型化客户端抛裸 JSONDecodeError：同样走降级而不是崩 run。"""
    agent_mod, _, _, _, schema_mod = _load(pkg)

    class _RawDecodeErrorLLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise json.JSONDecodeError("Expecting ',' delimiter", '{"a": ', 6)
            return schema_mod.LLMResponse(content="done", finish_reason="stop")

    llm = _RawDecodeErrorLLM()
    agent = agent_mod.Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    result = await agent.run(task_id="t-raw-decode")

    assert result.content == "done"
    events = _read_ndjson(agent)
    assert [e for e in events if e.get("status") == "degraded"]


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_agent_logs_successful_parse_retry(pkg, tmp_path):
    """client 侧重试成功后：NDJSON 留 llm_parse_retry 事件（run 统计可数）。"""
    agent_mod, llm_mod, _, _, schema_mod = _load(pkg)

    class _RetriedLLM:
        async def generate(self, messages, tools=None):
            return schema_mod.LLMResponse(
                content="done", finish_reason="stop", parse_retries=2
            )

    agent = agent_mod.Agent(
        llm_client=_RetriedLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        log_dir=str(tmp_path),
        max_steps=5,
    )
    await agent.run(task_id="t-parse-retry")

    events = _read_ndjson(agent)
    retries = [e for e in events if e.get("event") == "llm_parse_retry"]
    assert len(retries) == 1, f"parse retry event missing: {events}"
    assert retries[0]["parse_retries"] == 2
    assert retries[0]["step_index"] == 0
    assert not [e for e in events if e.get("status") == "degraded"]
