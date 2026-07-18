import json

from Agent.router_agent.tools.base import Tool, ToolResult
from sar_orch.map import SemanticMapStore


class QuerySemanticMapTool(Tool):
    name = "query_semantic_map"
    description = "Query the coordinator-maintained semantic map. Does not read environment oracle state."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map: SemanticMapStore):
        self._semantic_map = semantic_map

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            content=json.dumps(
                self._semantic_map.snapshot(), ensure_ascii=False, default=str
            ),
        )
