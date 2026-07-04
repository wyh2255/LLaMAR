"""LangCoordinatorServer — CoordinatorServer 子类，用 LangRouterAgent 替换 RouterAgent。

复用父类的 FastAPI/WS/JSON-RPC/UI/地图 SSE 全部传输层；仅覆盖 RouterAgent 构造。
关键：父类 _build_app 的 lifespan 在服务器启动时（非 __init__ 时）才读 self._router
建 executor，故在 __init__ 末尾替换 self._router 即可让 executor 拿到 LangRouterAgent。
create_server_lang() 与 a2a.coordinator.server.create_server 同参，便于 sar_orch 无缝切换。
"""

from __future__ import annotations

from pathlib import Path

from a2a.coordinator.server import CoordinatorServer

from .router_agent import LangRouterAgent


class LangCoordinatorServer(CoordinatorServer):
    """CoordinatorServer with LangGraph router internals (drop-in override)."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        config_path: str | None = None,
        prompts_dir: str | None = None,
        tools_dir: str | None = None,
        extra_tools: list | None = None,
        skills_dir: str | None = None,
        log_dir: str | None = None,
        router_model: str = "claude-opus-4-5",
        router_max_steps: int = 15,
        router_temperature: float = 0.7,
        router_provider: str = "anthropic",
        router_api_base: str = "https://api.anthropic.com",
        router_api_key_env: str = "ANTHROPIC_API_KEY",
        verifier_model: str | None = None,
        verifier_max_steps: int = 10,
        verifier_temperature: float = 0.3,
        verifier_enabled: bool = True,
        orchestration_mode: str = "agentic",
        max_tasks_per_run: int = 20,
        orchestration_timeout: int = 600,
        router_step_callback=None,
    ) -> None:
        # 先调父类 __init__：建传输层壳 + 一个 legacy RouterAgent（将被替换）
        super().__init__(
            host=host,
            port=port,
            a2a_port=a2a_port,
            config_path=config_path,
            prompts_dir=prompts_dir,
            tools_dir=tools_dir,
            extra_tools=extra_tools,
            skills_dir=skills_dir,
            log_dir=log_dir,
            router_model=router_model,
            router_max_steps=router_max_steps,
            router_temperature=router_temperature,
            router_provider=router_provider,
            router_api_base=router_api_base,
            router_api_key_env=router_api_key_env,
            verifier_model=verifier_model,
            verifier_max_steps=verifier_max_steps,
            verifier_temperature=verifier_temperature,
            verifier_enabled=verifier_enabled,
            orchestration_mode=orchestration_mode,
            max_tasks_per_run=max_tasks_per_run,
            orchestration_timeout=orchestration_timeout,
            router_step_callback=router_step_callback,
        )
        # 用 LangRouterAgent 替换 router（executor 在 lifespan 启动时读 self._router）
        self._router = LangRouterAgent(
            registry=self._agent_registry,
            prompts_dir=Path(prompts_dir) if prompts_dir else None,
            custom_tools_dir=Path(tools_dir) if tools_dir else None,
            extra_tools=extra_tools,
            skills_dir=Path(skills_dir) if skills_dir else None,
            model=router_model,
            max_steps=router_max_steps,
            temperature=router_temperature,
            provider=router_provider,
            api_base=router_api_base,
            api_key_env=router_api_key_env,
            log_dir=Path(log_dir) if log_dir else None,
        )


def create_server_lang(
    host: str = "0.0.0.0",
    port: int = 8080,
    a2a_port: int = 8081,
    config_path: str | None = None,
    prompts_dir: str | None = None,
    tools_dir: str | None = None,
    extra_tools: list | None = None,
    skills_dir: str | None = None,
    log_dir: str | None = None,
    router_model: str = "deepseek-v4-flash",
    router_max_steps: int = 15,
    router_temperature: float = 0.7,
    router_provider: str = "anthropic",
    router_api_base: str = "https://api.anthropic.com",
    router_api_key_env: str = "ANTHROPIC_API_KEY",
    verifier_model: str | None = None,
    verifier_max_steps: int = 10,
    verifier_temperature: float = 0.3,
    verifier_enabled: bool = True,
    orchestration_mode: str = "agentic",
    max_tasks_per_run: int = 20,
    orchestration_timeout: int = 600,
    router_step_callback=None,
) -> LangCoordinatorServer:
    """创建 LangGraph 内核的 CoordinatorServer（与 create_server 同参）。"""
    return LangCoordinatorServer(
        host=host,
        port=port,
        a2a_port=a2a_port,
        config_path=config_path,
        prompts_dir=prompts_dir,
        tools_dir=tools_dir,
        extra_tools=extra_tools,
        skills_dir=skills_dir,
        log_dir=log_dir,
        router_model=router_model,
        router_max_steps=router_max_steps,
        router_temperature=router_temperature,
        router_provider=router_provider,
        router_api_base=router_api_base,
        router_api_key_env=router_api_key_env,
        verifier_model=verifier_model,
        verifier_max_steps=verifier_max_steps,
        verifier_temperature=verifier_temperature,
        verifier_enabled=verifier_enabled,
        orchestration_mode=orchestration_mode,
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
        router_step_callback=router_step_callback,
    )
