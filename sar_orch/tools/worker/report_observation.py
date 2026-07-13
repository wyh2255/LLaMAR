import json
from collections.abc import Callable

from Agent.worker_agent.tools.base import Tool, ToolResult


class ReportObservationTool(Tool):
    name = "report_observation"
    description = (
        "Non-blocking report of local SAR observations to the coordinator semantic map."
    )
    parameters = {
        "type": "object",
        "properties": {
            "object_type": {
                "type": "string",
                "description": "fire|person|reservoir|deposit|agent|status|unknown",
            },
            "name": {"type": "string", "description": "Optional object name"},
            "position": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 3,
                "maxItems": 3,
            },
            "attributes": {"type": "object"},
            "confidence": {"type": "number", "default": 1.0},
            "note": {"type": "string"},
        },
        "required": ["object_type"],
    }

    def __init__(
        self,
        agent_name: str = "unknown",
        task_id: str = "",
        get_step: Callable[[], int] | None = None,
    ):
        self._agent_name = agent_name
        self._task_id = task_id
        self._get_step = get_step or (lambda: 0)

    async def execute(self, **kwargs) -> ToolResult:
        payload = {
            "reporter": self._agent_name,
            "step": int(self._get_step()),
            "object_type": kwargs.get("object_type", "unknown"),
            "name": kwargs.get("name"),
            "position": kwargs.get("position"),
            "attributes": kwargs.get("attributes") or {},
            "confidence": float(kwargs.get("confidence", 1.0)),
            "source_task_id": self._task_id,
            "note": kwargs.get("note", ""),
        }
        return ToolResult(success=True, content=json.dumps(payload, ensure_ascii=False))
