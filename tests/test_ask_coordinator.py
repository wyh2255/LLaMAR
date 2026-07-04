"""Tests for AskCoordinatorTool — Worker → Coordinator 帮助请求（纯信号版）。"""

import pytest
from a2a.worker.tools.ask_coordinator import AskCoordinatorTool
from a2a.worker.need_input import NeedInputError


class TestAskCoordinatorToolProperties:
    def test_name(self):
        tool = AskCoordinatorTool()
        assert tool.name == "ask_coordinator"

    def test_parameters_schema(self):
        tool = AskCoordinatorTool()
        schema = tool.parameters
        assert "question" in schema["properties"]
        assert "question" in schema["required"]


@pytest.mark.asyncio
async def test_execute_raises_need_input_error():
    tool = AskCoordinatorTool()
    with pytest.raises(NeedInputError) as exc_info:
        await tool.execute("Where is the target?")
    assert exc_info.value.question == "Where is the target?"


@pytest.mark.asyncio
async def test_execute_raises_for_any_question():
    tool = AskCoordinatorTool()
    with pytest.raises(NeedInputError) as exc_info:
        await tool.execute("What should I do next?")
    assert exc_info.value.question == "What should I do next?"
