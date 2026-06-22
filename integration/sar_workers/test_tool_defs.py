"""tool_defs.py 单元测试。"""
import pytest
import asyncio
from integration.sar_workers.tool_defs import tool, ToolDef


def test_tool_creates_tool_def():
    @tool(name="test_tool", description="A test tool")
    async def my_tool(node, target_id: str) -> str:
        """Navigate to target.

        Args:
            target_id: The target ID
        """
        return f"navigated to {target_id}"

    assert isinstance(my_tool, ToolDef)
    assert my_tool.name == "test_tool"
    assert my_tool.description == "A test tool"


def test_tool_schema_generation():
    @tool(name="nav", description="Navigate")
    async def nav(node, target_id: str) -> str:
        """Nav tool.

        Args:
            target_id: Target object ID
        """
        return "ok"

    schema = nav.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nav"
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "target_id" in params["properties"]
    assert params["properties"]["target_id"]["type"] == "string"
    assert params["properties"]["target_id"]["description"] == "Target object ID"
    assert "target_id" in params["required"]


def test_tool_schema_non_string_types():
    @tool(name="typed", description="Typed tool")
    async def typed(node, count: int, flag: bool) -> str:
        """Typed tool.

        Args:
            count: Number of items
            flag: Enable flag
        """
        return "ok"

    schema = typed.to_schema()
    params = schema["function"]["parameters"]
    assert params["properties"]["count"]["type"] == "integer"
    assert params["properties"]["flag"]["type"] == "boolean"
    assert set(params["required"]) == {"count", "flag"}


def test_tool_no_params():
    @tool(name="explore", description="Explore")
    async def explore(node) -> str:
        """Explore area."""
        return "explored"

    schema = explore.to_schema()
    params = schema["function"]["parameters"]
    assert params["properties"] == {}


def test_tool_bind():
    @tool(name="t", description="t")
    async def t(node) -> str:
        return "ok"

    mock_node = object()
    bound = t.bind(mock_node)
    assert bound._node is mock_node
    assert bound.name == "t"
    # 原始 ToolDef 不变
    assert t._node is None


def test_tool_execute():
    @tool(name="t", description="t")
    async def t(node, x: str) -> str:
        return f"got {x}"

    class MockNode:
        pass

    bound = t.bind(MockNode())
    result = asyncio.run(bound.execute(x="hello"))
    assert result == "got hello"
