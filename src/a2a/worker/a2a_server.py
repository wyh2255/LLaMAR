"""Worker 上的 A2A HTTP Server。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import uvicorn
from starlette.applications import Starlette
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill

import a2a.server.request_handlers.request_handler as _a2a_handler

_a2a_handler.validate_proto_required_fields = lambda _msg: None


def create_worker_a2a_server(
    worker_id: str,
    host: str = "0.0.0.0",
    port: int = 8090,
    capabilities: list[str] | None = None,
    model: str = "claude-sonnet-4-5",
    prompts_dir: Path | None = None,
    tools_dir: Path | None = None,
    skills_dir: Path | None = None,
    max_steps: int = 50,
    temperature: float = 0.7,
    provider: str = "anthropic",
    api_base: str = "https://api.anthropic.com",
    api_key_env: str = "ANTHROPIC_API_KEY",
    extra_tools: list | None = None,
        system_prompt: str = "",
        step_callback: Callable | None = None,
        log_dir: Path | None = None,
        include_base_tools: bool = True,
) -> uvicorn.Server:
    """创建 Worker A2A HTTP Server。"""
    from a2a.worker.agent_adapter import AgentAdapter

    skills = [
        AgentSkill(id=cap, name=cap, description=f"Capability: {cap}", tags=[cap])
        for cap in (capabilities or [])
    ]
    skills.append(
        AgentSkill(
            id="backend",
            name="Backend",
            description="Execution backend: mini_agent",
            tags=["metadata", "backend", "mini_agent"],
        )
    )
    skills.append(
        AgentSkill(
            id="model",
            name="Model",
            description=f"LLM model: {model}",
            tags=["metadata", "model", model],
        )
    )

    agent_card = AgentCard(
        name=f"Mini-Agent Worker {worker_id}",
        description=f"Mini-Agent worker node {worker_id}",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"http://{host}:{port}/api/v1/jsonrpc/",
            )
        ],
    )

    # 每个 worker 有独立的日志子目录
    worker_log_dir = (log_dir / worker_id) if log_dir else None
    executor = AgentAdapter(
        model=model,
        prompts_dir=prompts_dir,
        tools_dir=tools_dir,
        skills_dir=skills_dir,
        log_dir=worker_log_dir,
        max_steps=max_steps,
        temperature=temperature,
        provider=provider,
        api_base=api_base,
        api_key_env=api_key_env,
        system_prompt=system_prompt,
        extra_tools=extra_tools,
        step_callback=step_callback,
        include_base_tools=include_base_tools,
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
    return uvicorn.Server(config)
