"""Tests for Map Agent LLM-driven query (Phase 3 / Phase B).

Covers:
- ``build_map_agent_graph`` — returns a compiled LangGraph graph.
- ``extract_usage`` — token-aggregation helper.
- Server error handling — LLM failure returns friendly dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph.state import CompiledStateGraph

from sar_orch.map_agent.llm_query import (
    MAP_AGENT_SYSTEM_PROMPT,
    build_map_agent_graph,
    extract_usage,
)
from sar_orch.map_agent.server import (
    _query_natural,
    set_llm_client,
    set_token_sink,
)


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_llm():
    """Return a mock LangChain chat model suitable for ``build_map_agent_graph``."""
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.invoke.return_value = MagicMock(content="Mock response")
    llm.ainvoke.return_value = MagicMock(content="Mock response")
    return llm


# ═══════════════════════════════════════════════════════════════════════
# build_map_agent_graph
# ═══════════════════════════════════════════════════════════════════════


class TestBuildMapAgentGraph:
    """Contract: ``build_map_agent_graph`` returns a ``CompiledStateGraph``."""

    def test_returns_compiled_graph(self, mock_llm):
        """Given a LangChain chat model, returns a compiled graph."""
        graph = build_map_agent_graph(mock_llm)
        assert isinstance(graph, CompiledStateGraph), (
            f"Expected CompiledStateGraph, got {type(graph)}"
        )

    def test_graph_has_tool_node(self, mock_llm):
        """The compiled graph should contain a tool node."""
        graph = build_map_agent_graph(mock_llm)
        # CompiledStateGraph has a .get_graph() method for introspection
        nodes = list(graph.nodes.keys())
        tool_nodes = [n for n in nodes if "tool" in n.lower() or "action" in n.lower()]
        assert len(tool_nodes) >= 1, f"No tool-related nodes found in {nodes}"

    def test_prompt_is_str(self):
        """System prompt must be a non-empty string."""
        assert isinstance(MAP_AGENT_SYSTEM_PROMPT, str)
        assert len(MAP_AGENT_SYSTEM_PROMPT) > 50


# ═══════════════════════════════════════════════════════════════════════
# extract_usage
# ═══════════════════════════════════════════════════════════════════════


class TestExtractUsage:
    """Contract: ``extract_usage`` aggregates token usage from AIMessages."""

    def test_empty_result(self):
        """Empty messages list returns all-zero counters."""
        usage = extract_usage({"messages": []})
        assert usage == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }

    def test_no_usage_metadata(self):
        """Messages without usage_metadata return all-zero counters."""
        from langchain_core.messages import AIMessage

        result = {
            "messages": [
                AIMessage(content="Hello"),
                AIMessage(content="World"),
            ]
        }
        usage = extract_usage(result)
        assert usage == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }

    def test_aggregates_single_message(self):
        """Single AIMessage with usage metadata is correctly extracted."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="Answer",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "input_token_details": {"cache_read": 20, "cache_creation": 10},
            },
        )
        result = {"messages": [msg]}
        usage = extract_usage(result)
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150
        assert usage["cache_hit_tokens"] == 20
        assert usage["cache_miss_tokens"] == 10

    def test_aggregates_multiple_messages(self):
        """Multiple AIMessages have their tokens summed."""
        from langchain_core.messages import AIMessage

        msg1 = AIMessage(
            content="Thought",
            usage_metadata={
                "input_tokens": 200,
                "output_tokens": 30,
                "total_tokens": 230,
                "input_token_details": {"cache_read": 0, "cache_creation": 200},
            },
        )
        msg2 = AIMessage(
            content="Final Answer",
            usage_metadata={
                "input_tokens": 230,
                "output_tokens": 20,
                "total_tokens": 250,
            },
        )
        result = {"messages": [msg1, msg2]}
        usage = extract_usage(result)
        assert usage["prompt_tokens"] == 430  # 200 + 230
        assert usage["completion_tokens"] == 50  # 30 + 20
        assert usage["total_tokens"] == 480  # 230 + 250
        assert usage["cache_hit_tokens"] == 0
        assert usage["cache_miss_tokens"] == 200

    def test_missing_input_details(self):
        """When input_token_details is missing, cache fields default to 0."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="No details",
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
            },
        )
        result = {"messages": [msg]}
        usage = extract_usage(result)
        assert usage["cache_hit_tokens"] == 0
        assert usage["cache_miss_tokens"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Server: error handling
# ═══════════════════════════════════════════════════════════════════════


class TestQueryNaturalErrorHandling:
    """Contract: ``_query_natural`` handles LLM failures gracefully."""

    @pytest.fixture(autouse=True)
    def _setup_server_globals(self):
        """Reset server globals before each test."""
        set_llm_client(None)
        set_token_sink(None)
        yield

    async def test_no_llm_client_returns_error(self):
        """When no LLM client is configured, the tool returns an error dict."""
        result = await _query_natural(query="test query")
        assert isinstance(result, dict)
        assert "answer" in result
        assert "error" in result

    async def test_llm_failure_returns_error(self):
        """When the LLM call raises, the tool returns an error dict."""
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        set_llm_client(mock_llm)

        result = await _query_natural(query="test")
        assert isinstance(result, dict)
        assert "answer" in result
        assert "error" in result

    async def test_token_sink_called_on_success(self):
        """Token sink should be called with agent='MapAgent' on success."""
        from langchain_core.messages import AIMessage

        # Build a mock graph that returns a valid result
        mock_graph = AsyncMock(spec=CompiledStateGraph)
        mock_graph.ainvoke.return_value = {
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

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        set_llm_client(mock_llm)

        sink = MagicMock()
        set_token_sink(sink)

        with patch(
            "sar_orch.map_agent.server.build_map_agent_graph",
            return_value=mock_graph,
        ):
            result = await _query_natural(query="test")
            assert result["answer"] == "Mock answer"
            sink.assert_called_once()
            call_kwargs = sink.call_args[1]
            assert call_kwargs.get("agent") == "MapAgent"
            assert call_kwargs.get("prompt_tokens") == 10
            assert call_kwargs.get("completion_tokens") == 5
            assert call_kwargs.get("total_tokens") == 15

    async def test_error_still_returns_dict(self):
        """Even on catastrophic failure, the tool returns a dict, never raises."""
        set_llm_client(None)
        result = await _query_natural(query="any")
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════
# MCP tool registration
# ═══════════════════════════════════════════════════════════════════════


class TestMCPToolRegistration:
    """Contract: the FastMCP server has the ``query_natural`` tool registered."""

    def test_query_natural_tool_registered(self):
        """The tool 'map_agent__query_natural' must be on the MCP server."""
        from sar_orch.map_agent.server import mcp

        # FastMCP exposes registered tools via _tool_manager
        if not hasattr(mcp, "_tool_manager"):
            pytest.skip("FastMCP version does not expose _tool_manager")

        get_tool = getattr(mcp._tool_manager, "get_tool", None)
        if get_tool is None:
            pytest.skip("ToolManager does not expose get_tool")

        tool = get_tool("map_agent__query_natural")
        assert tool is not None, (
            "Tool 'map_agent__query_natural' not registered. "
            f"Registered tools: {list(mcp._tool_manager._tools.keys())}"
        )
        assert tool.name == "map_agent__query_natural"
