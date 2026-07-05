import json

from Agent.router_agent.context import ContextConfig, CoordinatorContextManager
from Agent.router_agent.schema import Message


def test_context_extracts_semantic_map_not_global_snapshot():
    ctx = CoordinatorContextManager()
    payload = {
        "known_dynamic_objects": {"fires": [{"name": "FireA"}], "persons": []},
        "known_priors": {"reservoirs": [{"name": "ReservoirYork"}], "deposits": []},
        "stale_entries": [],
        "conflicts": [],
    }

    ctx.observe("query_semantic_map", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    memory = messages[-1].content
    assert "Known fires: 1" in memory
    assert "Known reservoirs: 1" in memory
    assert "global_snapshot" not in memory


def test_oracle_mode_renders_environment_view():
    ctx = CoordinatorContextManager(
        config=ContextConfig(state_mode="oracle"),
    )
    payload = {
        "step": 5,
        "max_steps": 100,
        "finished": False,
        "agents": [],
        "fires": [],
        "persons": [],
        "summary": "5 cells burning, 0 persons found",
    }
    ctx.observe("query_sar_state", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    memory = messages[-1].content
    assert "Environment at step" in memory
    assert "5 cells burning" in memory


def test_semantic_mode_config_renders_semantic_view():
    ctx = CoordinatorContextManager(
        config=ContextConfig(state_mode="semantic"),
    )
    payload = {
        "known_dynamic_objects": {
            "fires": [{"name": "FireA"}],
            "persons": [{"name": "PersonB"}],
        },
        "known_priors": {
            "reservoirs": [{"name": "ReservoirYork"}],
            "deposits": [],
        },
        "stale_entries": [],
        "conflicts": [],
    }
    ctx.observe("query_semantic_map", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    memory = messages[-1].content
    assert "Known fires: 1" in memory
    assert "Known persons: 1" in memory
    assert "Environment at step" not in memory


def test_context_extracts_team_status_summary():
    ctx = CoordinatorContextManager()
    payload = {
        "workers": [{"agent_id": "Alice", "state": "RUNNING"}],
        "recent_observations": [],
    }

    ctx.observe("query_team_status", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    assert "Workers: 1" in messages[-1].content


def test_semantic_mode_tool_names_exclude_query_sar_state():
    from sar_orch.tools.coordinator import QuerySemanticMapTool, QueryTeamStatusTool
    from sar_orch.semantic_map import SemanticMapStore

    store = SemanticMapStore()
    tools = [QuerySemanticMapTool(store), QueryTeamStatusTool(store)]
    names = {tool.name for tool in tools}

    assert "query_semantic_map" in names
    assert "query_team_status" in names
    assert "query_sar_state" not in names
