import json

import pytest

from sar_orch.semantic_map import SemanticMapStore
from sar_orch.tools.coordinator.query_semantic_map import QuerySemanticMapTool
from sar_orch.tools.coordinator.query_team_status import QueryTeamStatusTool


@pytest.mark.asyncio
async def test_query_semantic_map_returns_store_snapshot():
    store = SemanticMapStore()
    store.ingest_observation(
        {"reporter": "Alice", "step": 1, "object_type": "fire", "name": "FireA", "position": [1, 1, 0], "attributes": {"status": "active"}}
    )
    tool = QuerySemanticMapTool(store)

    result = await tool.execute()
    payload = json.loads(result.content)

    assert result.success is True
    assert payload["known_dynamic_objects"]["fires"][0]["name"] == "FireA"


@pytest.mark.asyncio
async def test_query_team_status_includes_recent_observations():
    store = SemanticMapStore()
    store.ingest_observation(
        {"reporter": "Alice", "step": 1, "object_type": "fire", "name": "FireA", "position": [1, 1, 0], "attributes": {"status": "active"}}
    )
    tool = QueryTeamStatusTool(store)

    result = await tool.execute()
    payload = json.loads(result.content)

    assert result.success is True
    assert payload["recent_observations"][0]["name"] == "FireA"
