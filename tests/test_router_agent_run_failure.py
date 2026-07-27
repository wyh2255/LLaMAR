"""Tests for router Agent failure results."""

import pytest

from Agent.router_agent.agent import Agent
from Agent.router_agent.retry import RetryExhaustedError
from Agent.router_agent.schema import FunctionCall, LLMResponse, ToolCall


@pytest.mark.asyncio
async def test_router_agent_run_returns_failed_result_when_llm_engine_fails(tmp_path):
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
    assert "4 次重试" in result.content


@pytest.mark.asyncio
async def test_router_agent_run_returns_failed_result_after_max_steps(tmp_path):
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


@pytest.mark.asyncio
async def test_router_agent_run_returns_failed_result_after_explicit_completion_nudges(
    tmp_path,
):
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
