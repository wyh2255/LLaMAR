"""Coordinator 上的 A2A HTTP Server（外部客户端通过 A2A 协议接入 Coordinator）。"""

from __future__ import annotations

from typing import Any

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette

from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
from a2a.coordinator.task_logger import TaskLogger
from Agent.router_agent.context import ContextConfig

# monkey-patch: 跳过 a2a-sdk 的 proto 字段校验，避免 protobuf upb 后端兼容性问题
#   error: 'google._upb._message.FieldDescriptor' object has no attribute 'label'
#   ref:  docs/a2a_server_library_analysis.md §4.1
import a2a.server.request_handlers.request_handler as _a2a_handler

_a2a_handler.validate_proto_required_fields = lambda _msg: None


def create_coordinator_a2a_server(
    host: str = "0.0.0.0",
    port: int = 8081,
    capabilities: list[str] | None = None,
    agent_registry: AgentRegistry | None = None,
    task_queue: TaskQueue | None = None,
    router: RouterAgent | None = None,
    verifier: Any | None = None,
    task_logger: TaskLogger | None = None,
    orchestration_mode: str = "agentic",
    max_tasks_per_run: int = 20,
    orchestration_timeout: int = 600,
    router_step_callback=None,
    context_config: ContextConfig | None = None,
    token_limit: int = 80000,
    require_explicit_completion: bool = False,
    coordinator_host: str = "localhost",
    coordinator_port: int = 8080,
) -> uvicorn.Server:
    """创建 Coordinator A2A HTTP Server。

    Args:
        verifier: 可选的 VerifierAgent 实例。None 时跳过验证环节。
        task_logger: 可选的 TaskLogger 实例。None 时不记录日志。
    """
    from a2a.coordinator.agent_registry import AgentRegistry
    from a2a.coordinator.task_queue import TaskQueue

    skills = [
        AgentSkill(id=cap, name=cap, description=f"Capability: {cap}", tags=[cap])
        for cap in (capabilities or [])
    ]

    supported_interfaces = [
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{host}:{port}/api/v1/jsonrpc/",
        )
    ]

    agent_card = AgentCard(
        name="OpenHarness Coordinator",
        description="OpenHarness multi-agent coordination hub",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
        supported_interfaces=supported_interfaces,
    )

    executor = CoordinatorAgentExecutor(
        registry=agent_registry or AgentRegistry(),
        router=router,
        task_queue=task_queue or TaskQueue(),
        verifier=verifier,
        task_logger=task_logger,
        orchestration_mode=orchestration_mode,
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
        router_step_callback=router_step_callback,
        context_config=context_config,
        token_limit=token_limit,
        require_explicit_completion=require_explicit_completion,
        coordinator_host=coordinator_host,
        coordinator_port=coordinator_port,
    )
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/api/v1/jsonrpc/"))
    app = Starlette(routes=routes)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.executor = executor  # type: ignore[attr-defined]
    return server
