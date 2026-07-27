"""Tests for Agent.run() catching NeedInputError."""

import pytest
from unittest.mock import AsyncMock

from Agent.worker_agent.agent import Agent
from Agent.worker_agent.retry import RetryExhaustedError
from Agent.worker_agent.schema import ToolCall, FunctionCall, LLMResponse
from a2a.worker.need_input import NeedInputError


class _FakeTool:
    """A tool that raises NeedInputError."""

    def __init__(self):
        self._name = "ask_coordinator"

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "Ask coordinator"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        }

    async def execute(self, question: str):
        raise NeedInputError(question)

    def to_schema(self):
        return {}

    def to_openai_schema(self):
        return {}


@pytest.mark.asyncio
async def test_agent_run_returns_need_input_when_tool_raises():
    """Agent.run() should catch NeedInputError and return RunResult(need_input=True)."""
    tool_call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}),
    )

    call_count = 0

    async def fake_generate(messages, tools=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                content="",
                tool_calls=[tool_call],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="done",
            finish_reason="stop",
        )

    llm_client = AsyncMock()
    llm_client.generate = fake_generate

    agent = Agent(
        llm_client=llm_client,
        system_prompt="test",
        tools=[_FakeTool()],
        max_steps=5,
    )

    result = await agent.run()
    assert result.need_input is True
    assert result.content == "Where?"
    assert result.success is False


@pytest.mark.asyncio
async def test_agent_run_returns_failed_result_when_llm_engine_fails(tmp_path):
    class _FailingLLM:
        async def generate(self, messages, tools=None):
            raise RetryExhaustedError(RuntimeError("provider unavailable"), attempts=4)

    agent = Agent(
        llm_client=_FailingLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        max_steps=5,
    )

    result = await agent.run()

    assert result.success is False
    assert result.need_input is False
    assert "4 retries" in result.content


@pytest.mark.asyncio
async def test_agent_run_returns_failed_result_after_explicit_completion_nudges(tmp_path):
    class _PlainTextLLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(content="still working", finish_reason="stop")

    class _ContinueHooks:
        async def on_run_start(self, agent, user_message):
            return None

        async def on_run_end(self, agent, result):
            return None

        async def should_continue(self, agent, step):
            return True

        async def pre_llm(self, agent, messages):
            return messages

        async def post_llm(self, agent, response):
            return None

    agent = Agent(
        llm_client=_PlainTextLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        max_steps=10,
        hooks=_ContinueHooks(),
        require_explicit_completion=True,
    )

    result = await agent.run()

    assert result.success is False
    assert result.need_input is False



@pytest.mark.asyncio
async def test_agent_run_returns_failed_result_after_max_steps(tmp_path):
    tool_call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="never_finishes", arguments={}),
    )

    class _NeverFinishesLLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(
                content="",
                tool_calls=[tool_call],
                finish_reason="tool_calls",
            )

    agent = Agent(
        llm_client=_NeverFinishesLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        max_steps=1,
    )

    result = await agent.run()

    assert result.success is False
    assert result.need_input is False
