"""AssignTaskTool - 将子任务分配给指定 Worker。"""

from __future__ import annotations

import httpx
from a2a.client import create_client, ClientConfig, Client
from a2a.types.a2a_pb2 import (
    SendMessageRequest,
    Message,
    Part,
    StreamResponse,
    Role,
)

from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError
from Agent.router_agent.tools.base import Tool, ToolResult


def _extract_text(events: list[StreamResponse]) -> str:
    """从 StreamResponse 列表中提取文本。"""
    texts = []
    for event in events:
        if event.HasField("status_update"):
            su = event.status_update
            if su.status and su.status.message:
                parts = su.status.message.parts
                texts.extend(p.text for p in parts if p.text)
        if event.HasField("artifact_update"):
            au = event.artifact_update
            if au.artifact and au.artifact.parts:
                texts.extend(p.text for p in au.artifact.parts if p.text)
        if event.HasField("task"):
            texts.append(f"Task:{event.task.id} state={event.task.status.state}")
    return "\n".join(texts) or "(empty result)"


class AssignTaskTool(Tool):
    """将子任务分配给指定 Worker 并收集结果。构造时注入 AgentRegistry + SDK Client 缓存。"""

    def __init__(
        self,
        registry: AgentRegistry,
        sdk_clients: dict[str, Client],
    ):
        self._registry = registry
        self._sdk_clients = sdk_clients
        self._httpx_client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "assign_task"

    @property
    def description(self) -> str:
        return (
            "Assign a subtask to a specific worker and wait for the result. "
            "Use this after query_workers to send work to an available worker. "
            "Returns the worker's response text."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The ID of the worker to assign the task to",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete, self-contained instruction for the worker",
                },
            },
            "required": ["agent_id", "prompt"],
        }

    async def execute(self, agent_id: str, prompt: str) -> ToolResult:
        """分配子任务并返回结果文本。"""
        try:
            agent_info = self._registry.get(agent_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{agent_id}' not found in registry.",
                error="worker_not_found",
            )

        client = self._sdk_clients.get(agent_id)
        if client is None:
            httpx_client = await self._get_or_create_httpx_client()
            config = ClientConfig(streaming=True, httpx_client=httpx_client)
            client = await create_client(agent_info.endpoint, config)
            self._sdk_clients[agent_id] = client

        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
            ),
        )

        events: list[StreamResponse] = []
        try:
            async for stream_response in client.send_message(request):
                events.append(stream_response)
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Error communicating with worker '{agent_id}': {e}",
                error="connection_failed",
            )

        return ToolResult(success=True, content=_extract_text(events))

    async def _get_or_create_httpx_client(self) -> httpx.AsyncClient:
        """获取或创建共享的 httpx 客户端。"""
        if self._httpx_client is None or self._httpx_client.is_closed:
            self._httpx_client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0)
            )
        return self._httpx_client
