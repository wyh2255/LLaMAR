"""Map Agent LLM query — LangGraph ReAct agent for free-form map queries.

Phase 3 (Phase B).  Uses ``langgraph.prebuilt.create_react_agent`` to
orchestrate a "understand free-text → call structured tools → trim result"
ReAct loop driven by a LangChain chat model.

Usage::

    from sar_orch.map_agent.llm_query import build_map_agent_graph

    graph = build_map_agent_graph(llm)
    result = await graph.ainvoke({
        "messages": [("user", "What is the nearest reservoir to CaldorFire?")]
    })
    answer = result["messages"][-1].content
"""

from __future__ import annotations

from typing import Any

from a2a.utils.prompt_loader import load_repo_prompt
from langgraph.prebuilt import create_react_agent

from sar_orch.map_agent.tools import (
    get_fire_info,
    get_person_info,
    get_reservoir_info,
    get_task_context,
)

#: Map Agent ReAct system prompt — out-of-line (workflow §B1/B2): loaded
#: fail-closed from the repo.  File bytes must stay byte-identical to the
#: former inline string (§B3 sha invariant, pinned in
#: ``tests/test_prompt_externalization_map.py``).
MAP_AGENT_SYSTEM_PROMPT = load_repo_prompt("sar_orch/prompts/map_agent/system.md")


def build_map_agent_graph(llm: Any):
    """Build the LangGraph ReAct agent for natural-language map queries.

    Args:
        llm: A LangChain ``BaseChatModel`` instance (e.g. ``ChatOpenAI``).

    Returns:
        A compiled ``CompiledStateGraph`` ready for ``ainvoke()``.
    """
    return create_react_agent(
        llm,
        tools=[get_fire_info, get_person_info, get_reservoir_info, get_task_context],
        prompt=MAP_AGENT_SYSTEM_PROMPT,
    )


def extract_usage(result: dict) -> dict[str, int]:
    """Aggregate token usage from all AIMessages in a LangGraph result.

    Args:
        result: The dict returned by ``graph.ainvoke()``, containing key
            ``"messages"``.

    Returns:
        A flat dict with keys ``prompt_tokens``, ``completion_tokens``,
        ``total_tokens``, ``cache_hit_tokens``, ``cache_miss_tokens``.
        All default to 0 when no usage metadata is present.
    """
    messages = result.get("messages", [])
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cache_hit_tokens = 0
    cache_miss_tokens = 0

    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if usage is None:
            continue
        prompt_tokens += usage.get("input_tokens", 0)
        completion_tokens += usage.get("output_tokens", 0)
        total_tokens += usage.get("total_tokens", 0)

        input_details = usage.get("input_token_details") or {}
        cache_hit_tokens += input_details.get("cache_read", 0)
        cache_miss_tokens += input_details.get("cache_creation", 0)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
    }
