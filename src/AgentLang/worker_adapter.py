"""LangAgentAdapter — AgentAdapter 子类，仅覆盖 _build_agent() 返回 LangGraph ReActAgent。

复用父类 execute/cancel（现经统一 AgentController + A2AWorkerSink 驱动）与全部
worker HTTP/WS 传输逻辑；仅把 LLMClient→build_llm(reasoning_split=True)、
Agent→ReActAgent。_build_agent 是控制器的 agent_factory，覆盖它即换引擎；
ReActAgent 无 attach_context，由控制器 hasattr 守卫跳过，run() 返回 str 经控制器
归一化为 RunResult。worker 变体的 reasoning_split 经 build_llm extra_body 传递，
对齐 worker_agent/llm/openai_client.py。
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill
from a2a.worker.agent_adapter import AgentAdapter
from Agent.worker_agent.tools.bash_tool import BashTool
from Agent.worker_agent.tools.file_tools import ReadTool, WriteTool
from Agent.worker_agent.tools.skill_loader import SkillLoader
from Agent.worker_agent.tools.skill_tool import GetSkillTool

from .core_agent import ReActAgent
from .llm import build_llm


class LangAgentAdapter(AgentAdapter):
    """AgentAdapter with LangGraph ReActAgent internals (drop-in override)."""

    def _build_agent(self) -> ReActAgent:
        """构建 LangGraph ReActAgent 实例（替代 AgentAdapter._build_agent）。"""
        api_key = os.environ.get(self._api_key_env, "")
        llm = build_llm(
            provider=self._provider,
            api_base=self._api_base,
            model=self._model,
            api_key=api_key,
            temperature=self._temperature,
            reasoning_split=True,  # worker 变体：分离 thinking
        )

        # system prompt（复用父类加载 + 向后兼容合并）
        system_prompt = self._load_prompt() or ""
        if self._system_prompt:
            system_prompt = (
                self._system_prompt + "\n\n" + system_prompt
                if system_prompt
                else self._system_prompt
            )

        # 基础 tools（领域 agent 可禁用，如 SAR worker）+ 自定义 + extra
        tools: list = []
        if self._include_base_tools:
            tools.extend([ReadTool(), WriteTool()])
            workspace_dir = str(self._workspace_dir)
            if workspace_dir:
                tools.append(BashTool(workspace_dir=workspace_dir))
        tools.extend(self._load_custom_tools())
        tools.extend(self._extra_tools)

        # skills（复用父类发现逻辑）
        skills_dir = self._skills_dir or self._discover_skills_dir()
        if skills_dir:
            try:
                skill_loader = SkillLoader(skills_dir=str(skills_dir))
                skill_loader.discover_skills()
                metadata_prompt = skill_loader.get_skills_metadata_prompt()
                if metadata_prompt:
                    system_prompt = system_prompt + "\n\n" + metadata_prompt
                tools.append(GetSkillTool(skill_loader))
            except Exception:
                pass

        return ReActAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            log_dir=self._log_dir,
        )


def create_worker_a2a_server_lang(
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
    step_callback=None,
    log_dir: Path | None = None,
    include_base_tools: bool = True,
) -> uvicorn.Server:
    """创建 Worker A2A HTTP Server（LangGraph 内核版）。与 create_worker_a2a_server 同参。"""
    skills = [
        AgentSkill(id=cap, name=cap, description=f"Capability: {cap}", tags=[cap])
        for cap in (capabilities or [])
    ]
    skills.append(
        AgentSkill(
            id="backend",
            name="Backend",
            description="Execution backend: langgraph",
            tags=["metadata", "backend", "langgraph"],
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
        name=f"LangGraph Worker {worker_id}",
        description=f"LangGraph worker node {worker_id}",
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

    worker_log_dir = (log_dir / worker_id) if log_dir else None
    executor = LangAgentAdapter(
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
