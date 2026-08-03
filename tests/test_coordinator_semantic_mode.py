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
        "step_budget": {"current_step": 0, "max_steps": 0, "remaining": 0},
        "known_dynamic_objects": {"fires": [], "persons": []},
        "known_priors": {"reservoirs": [], "deposits": []},
        "agents": [{"agent_id": "Alice", "state": "RUNNING"}],
        "recent_observations": [],
        "stale_entries": [],
        "conflicts": [],
    }

    ctx.observe("query_semantic_map", json.dumps(payload), True)
    messages = ctx.assemble("system", [Message(role="system", content="system")])

    assert "Workers: 1" in messages[-1].content


def test_semantic_mode_tool_names_exclude_query_sar_state():
    from sar_orch.tools.coordinator import QuerySemanticMapTool, QueryTeamStatusTool
    from sar_orch.map import SemanticMapStore

    store = SemanticMapStore()
    tools = [QuerySemanticMapTool(store), QueryTeamStatusTool(store)]
    names = {tool.name for tool in tools}

    assert "query_semantic_map" in names
    assert "query_team_status" in names
    assert "query_sar_state" not in names


def test_context_bounds_map_summary_rendering():
    """Context Memory must cap a malformed oversized map summary."""
    from Agent.router_agent.state_provider import RuntimeState

    summary = "A" * 150 + "B" * 50

    class Provider:
        def snapshot(self, context_id=None):
            return RuntimeState(
                version=1,
                env_step=3,
                payload={
                    "step_budget": {"current_step": 3, "max_steps": 50, "remaining": 47},
                    "semantic_summary": {},
                    "team_status_summary": {"workers": []},
                    "task_status_view": [],
                    "recent_changes": [],
                    "mission_finished": False,
                    "supervision": {},
                    "map_revision": 2,
                    "map_delta": {},
                    "map_summary": summary,
                    "map_summary_revision": 2,
                },
            )

    ctx = CoordinatorContextManager(state_provider=Provider())
    ctx.refresh_runtime_state()
    messages = ctx.assemble("system", [Message(role="system", content="system")])
    memory = messages[-1].content

    assert "### Map Summary (revision 2)" in memory
    assert summary[:150] in memory
    assert summary[150:] not in memory


def test_context_renders_map_diff_schema_in_priority_order_with_cap():
    """Phase 6 rendering consumes MapDiffCalculator's actual delta schema."""
    from Agent.router_agent.state_provider import RuntimeState

    class Provider:
        def snapshot(self, context_id=None):
            return RuntimeState(
                version=4,
                env_step=3,
                payload={
                    "step_budget": {"current_step": 3, "max_steps": 50, "remaining": 47},
                    "semantic_summary": {},
                    "team_status_summary": {"workers": []},
                    "task_status_view": [],
                    "recent_changes": [],
                    "mission_finished": False,
                    "supervision": {},
                    "map_revision": 4,
                    "map_delta": {
                        "change_count": 7,
                        "persons": {
                            "status_changed": [
                                {"name": "P1", "old": "trapped", "new": "rescued"}
                            ],
                            "gained": [{"name": "P2"}],
                        },
                        "fires": {
                            "intensity_changed": [
                                {"name": "F1", "old": "Low", "new": "High"}
                            ],
                            "status_changed": [
                                {"name": "F2", "old": "active", "new": "extinguished"}
                            ],
                            "gained": [{"name": "F3"}],
                        },
                        "conflicts_new": [{"name": "F4"}],
                        "conflicts_resolved": [],
                        "stale_new": [{"name": "P3"}],
                        "stale_resolved": [],
                    },
                    "map_summary": "",
                    "map_summary_revision": 0,
                },
            )

    ctx = CoordinatorContextManager(state_provider=Provider())
    ctx.refresh_runtime_state()
    messages = ctx.assemble("system", [Message(role="system", content="system")])
    memory = messages[-1].content

    assert "### Map Changes (revision 4)" in memory
    assert "P1 marked rescued" in memory
    assert "F1 intensity Low → High" in memory
    assert "F2 active → extinguished" in memory
    assert "F3 newly detected" in memory
    assert "P2 newly detected" in memory
    assert "... and 2 more changes" in memory
