"""Tests for Agent.run() catching NeedInputError."""

import pytest
from unittest.mock import AsyncMock

from Agent.worker_agent.agent import Agent
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
