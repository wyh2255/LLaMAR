import json

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.map import SemanticMapStore


class QueryTeamStatusTool(Tool):
    name = "query_team_status"
    description = "Query team-level worker status and recent observation summaries. Does not read raw environment ground-truth state."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map: SemanticMapStore):
        self._semantic_map = semantic_map

    async def execute(self, **kwargs) -> ToolResult:
        snapshot = self._semantic_map.snapshot()
        payload = {
            "workers": snapshot.get("agents", []),
            "pending_requests": [],
            "recent_observations": snapshot.get("recent_observations", []),
            "stale_entries": snapshot.get("stale_entries", []),
            "conflicts": snapshot.get("conflicts", []),
        }
        return ToolResult(
            success=True, content=json.dumps(payload, ensure_ascii=False, default=str)
        )
