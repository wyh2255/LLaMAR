import httpx

from Agent.worker_agent.tools.base import Tool, ToolResult


class QuerySharedMemoryTool(Tool):
    name = "query_shared_memory"
    description = (
        "Query coordinator semantic map shared memory. Does not consume SAR env steps."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, semantic_map_url: str):
        self._semantic_map_url = semantic_map_url.rstrip("/")

    async def execute(self, **kwargs) -> ToolResult:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self._semantic_map_url}/semantic-map")
            response.raise_for_status()
        return ToolResult(success=True, content=response.text)
