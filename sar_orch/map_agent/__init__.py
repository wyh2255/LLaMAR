"""sar_orch.map_agent — Map Agent MCP server for worker agents.

Provides structured query tools (get_fire_info, get_person_info,
get_reservoir_info, get_task_context) that trim SemanticMapStore
snapshots into worker-friendly formats, plus a LangGraph-driven
free-text query tool (query_natural).
"""

from sar_orch.map_agent.tools import MapAgentTools
from sar_orch.map_agent.server import (
    mcp,
    mount_to_fastapi,
    set_llm_client,
    set_token_sink,
)
from sar_orch.map_agent.llm_query import (
    build_map_agent_graph,
    extract_usage,
)

__all__ = [
    "MapAgentTools",
    "build_map_agent_graph",
    "extract_usage",
    "mcp",
    "mount_to_fastapi",
    "set_llm_client",
    "set_token_sink",
]
