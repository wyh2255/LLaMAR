"""react_agent.py 单元测试 — 使用 mock LLM client + pytest-asyncio。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from integration.sar_workers.react_agent import WorkerReActAgent
from integration.sar_workers.tool_defs import tool
from integration.coordinator.llm_shim import Message, LLMResponse, ToolCall, FunctionCall


def make_mock_llm(responses):
    client = AsyncMock()
    client.generate = AsyncMock(side_effect=responses)
    return client


@pytest.fixture
def simple_tool():
    @tool(name="echo", description="Echo input")
    async def echo(node, text: str) -> str:
        return f"echoed: {text}"
    return echo


@pytest.mark.asyncio
async def test_react_no_tool_calls():
    llm = make_mock_llm([LLMResponse(content="Done!")])
    agent = WorkerReActAgent(llm_client=llm, tools=[], system_prompt="test")
    result = await agent.run("hi")
    assert result == "Done!"
    assert llm.generate.call_count == 1


@pytest.mark.asyncio
async def test_react_with_tool_calls(simple_tool):
    llm = make_mock_llm([
        LLMResponse(tool_calls=[ToolCall(
            id="tc1", function=FunctionCall(name="echo", arguments={"text": "hello"})
        )]),
        LLMResponse(content="Echo done"),
    ])
    agent = WorkerReActAgent(llm_client=llm, tools=[simple_tool], system_prompt="test")
    result = await agent.run()
    assert result == "Echo done"
    assert llm.generate.call_count == 2
    tool_msgs = [m for m in agent._messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "echoed: hello"


@pytest.mark.asyncio
async def test_react_unknown_tool():
    llm = make_mock_llm([
        LLMResponse(tool_calls=[ToolCall(
            id="tc1", function=FunctionCall(name="nonexistent", arguments={})
        )]),
        LLMResponse(content="Fixed"),
    ])
    agent = WorkerReActAgent(llm_client=llm, tools=[], system_prompt="test")
    result = await agent.run()
    tool_msgs = [m for m in agent._messages if m.role == "tool"]
    assert "Error" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_react_max_steps():
    infinite_calls = [
        LLMResponse(tool_calls=[ToolCall(
            id=f"tc{i}", function=FunctionCall(name="echo", arguments={"text": "x"})
        )]) for i in range(5)
    ]
    llm = make_mock_llm(infinite_calls)
    @tool(name="echo", description="Echo")
    async def echo(node, text: str) -> str:
        return "ok"
    agent = WorkerReActAgent(llm_client=llm, tools=[echo], system_prompt="test", max_steps=3)
    result = await agent.run()
    assert "Max steps" in result


@pytest.mark.asyncio
async def test_react_tool_execution_error():
    """工具抛异常时 agent 记录错误并继续。"""
    @tool(name="fail", description="Always fails")
    async def fail_tool(node) -> str:
        raise RuntimeError("tool exploded")

    llm = make_mock_llm([
        LLMResponse(tool_calls=[ToolCall(
            id="tc1", function=FunctionCall(name="fail", arguments={})
        )]),
        LLMResponse(content="Recovered from error"),
    ])
    agent = WorkerReActAgent(llm_client=llm, tools=[fail_tool], system_prompt="test")
    result = await agent.run("trigger failure")
    assert result == "Recovered from error"
    tool_msgs = [m for m in agent._messages if m.role == "tool"]
    assert "Error executing fail" in tool_msgs[0].content
    assert "tool exploded" in tool_msgs[0].content
