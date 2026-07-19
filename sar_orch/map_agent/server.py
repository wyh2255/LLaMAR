"""Map Agent FastMCP server — mounts an MCP endpoint to the Coordinator's FastAPI app.

Usage::

    from sar_orch.map_agent import mount_to_fastapi

    mount_to_fastapi(app, semantic_map_store)
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from sar_orch.map import SemanticMapStore
from sar_orch.map_agent.llm_query import build_map_agent_graph, extract_usage
from sar_orch.map_agent.tools import MapAgentTools, configure_tools

logger = logging.getLogger(__name__)

# Key: streamable_http_path="/" makes mount("/mcp/map") produce a clean URL.
mcp = FastMCP("map_agent", streamable_http_path="/")

# The tools singleton — populated by mount_to_fastapi()
_tools: MapAgentTools | None = None

# Phase 3 (Phase B): LLM-driven query support — populated by set_llm_client / set_token_sink
_llm_client: Any = None
_token_sink: Callable[..., None] | None = None


def _ensure_tools() -> MapAgentTools:
    if _tools is None:
        raise RuntimeError(
            "MapAgentTools not configured. Call mount_to_fastapi() first."
        )
    return _tools


def _ensure_llm():
    """Return the LLM client or raise a clear error."""
    if _llm_client is None:
        raise RuntimeError(
            "MapAgent LLM client not configured. Call set_llm_client() first."
        )
    return _llm_client


# ── Public setters (called from coordinator.py) ──────────────────────


def set_llm_client(llm: Any) -> None:
    """Inject the LangChain chat model used by ``map_agent__query_natural``.

    Args:
        llm: A LangChain ``BaseChatModel`` instance (e.g. ``ChatOpenAI``).
    """
    global _llm_client
    _llm_client = llm


def set_token_sink(sink: Callable[..., None] | None) -> None:
    """Inject a token-usage callback for the LLM-driven query tool.

    The sink receives the same keyword arguments as
    ``ExperimentLogger.log_token_usage`` (agent, prompt_tokens, …).

    Args:
        sink: A callable ``(**kwargs) -> None``, or ``None`` to clear.
    """
    global _token_sink
    _token_sink = sink


# ── MCP tool implementations ────────────────────────────────────────


def _get_fire_info(fire_name: str | None = None) -> dict[str, Any]:
    return _ensure_tools().get_fire_info(fire_name)


def _get_person_info(person_name: str | None = None) -> dict[str, Any]:
    return _ensure_tools().get_person_info(person_name)


def _get_reservoir_info(supply_type: str | None = None) -> dict[str, Any]:
    return _ensure_tools().get_reservoir_info(supply_type)


def _get_task_context(task_description: str | None = None) -> dict[str, Any]:
    return _ensure_tools().get_task_context(task_description)


# Phase 3: LLM-driven natural-language map query
async def _query_natural(
    query: str,
    worker_task_context: str = "",
    worker_position: list[int] | None = None,
) -> dict[str, Any]:
    """Answer free-form map queries via LangGraph ReAct agent."""
    sink = _token_sink
    try:
        llm = _ensure_llm()
        graph = build_map_agent_graph(llm)
        result = await graph.ainvoke(
            {
                "messages": [
                    (
                        "user",
                        f"Query: {query}\n"
                        f"Task context: {worker_task_context}\n"
                        f"Position: {worker_position}",
                    )
                ]
            }
        )
        # Token tracking: agent="MapAgent"
        if sink is not None:
            try:
                sink(agent="MapAgent", **extract_usage(result))
            except Exception:
                logger.warning("MapAgent token sink failed", exc_info=True)
        answer = result["messages"][-1].content if result.get("messages") else ""
        return {"answer": answer}
    except Exception as exc:
        logger.error("MapAgent LLM query failed: %s", exc, exc_info=True)
        return {"answer": "", "error": str(exc)}


# Register tools with explicit MCP names (mcp_loader doesn't auto-prefix).
mcp.tool(name="map_agent__get_fire_info")(_get_fire_info)
mcp.tool(name="map_agent__get_person_info")(_get_person_info)
mcp.tool(name="map_agent__get_reservoir_info")(_get_reservoir_info)
mcp.tool(name="map_agent__get_task_context")(_get_task_context)
mcp.tool(name="map_agent__query_natural")(_query_natural)


def mount_to_fastapi(app: FastAPI, semantic_map: SemanticMapStore) -> None:
    """Mount the Map Agent MCP server to an existing FastAPI app.

    The MCP endpoint is served at ``/mcp/map``.

    Args:
        app: The coordinator's FastAPI application.
        semantic_map: The global SemanticMapStore instance.
    """
    global _tools
    _tools = MapAgentTools(semantic_map)
    configure_tools(semantic_map)  # Enable module-level convenience functions
    app.mount("/mcp/map", mcp.streamable_http_app())
