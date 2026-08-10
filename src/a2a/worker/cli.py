"""Worker CLI 入口。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from Agent.sandbox import SandboxPolicy
from a2a.worker.coordinator_client import CoordinatorWebSocketClient
from a2a.worker.a2a_server import create_worker_a2a_server
from a2a.shared.env_loader import load_env_file
from a2a.shared.path_config import load_path_config


app = typer.Typer(help="OpenHarness A2A Worker")


@app.command()
def main(
    worker_id: str = typer.Option(..., "--worker-id", help="Worker ID"),
    coordinator_url: str = typer.Option(
        ..., "--coordinator-url", help="Coordinator WebSocket URL"
    ),
    a2a_host: str = typer.Option("0.0.0.0", "--a2a-host", help="A2A server host"),
    a2a_port: int = typer.Option(8090, "--a2a-port", help="A2A server port"),
    capabilities: str = typer.Option(
        "", "--capabilities", help="Comma-separated capabilities"
    ),
    env_file: str = typer.Option(".env", "--env-file", help="Path to .env config file"),
    model: str = typer.Option("claude-sonnet-4-5", "--model", help="LLM model name"),
    max_steps: int = typer.Option(50, "--max-steps", help="Max Agent steps"),
    temperature: float = typer.Option(0.7, "--temperature", help="LLM temperature"),
    prompts_dir: str = typer.Option(
        None, "--prompts-dir", help="Path to prompts directory"
    ),
    tools_dir: str = typer.Option(
        None, "--tools-dir", help="Path to custom tools directory"
    ),
    skills_dir: str = typer.Option(
        None, "--skills-dir", help="Path to skills directory"
    ),
    provider: str = typer.Option(
        "anthropic", "--provider", help="LLM provider (anthropic|openai)"
    ),
    api_base: str = typer.Option(
        "https://api.anthropic.com", "--api-base", help="LLM API base URL"
    ),
    api_key_env: str = typer.Option(
        "ANTHROPIC_API_KEY", "--api-key-env", help="Env var for API key"
    ),
    sandbox_profile: str = typer.Option(
        "off", "--sandbox-profile", help="Sandbox profile: off|workspace"
    ),
    memory_read_mode: str = typer.Option(
        "read_port",
        "--memory-read-mode",
        help="Memory mode: legacy|shadow|read_port (default read_port since "
        "H3 retirement; legacy retained as rollback target)",
    ),
) -> None:
    """启动 Worker。"""
    # 加载 .env 文件，用其中的值作为 fallback 默认值
    env = load_env_file(env_file)

    # .env 中的 api_key 设置到环境变量，供下游读取
    if "api_key" in env:
        os.environ[api_key_env] = env["api_key"]

    # Phase 2: resolve callback secret from protected local config only.
    callback_signer = None
    if memory_read_mode in ("shadow", "read_port"):
        raw = env.get("coordinator_secret") or os.environ.get("A2A_COORDINATOR_SECRET")
        if not raw:
            from a2a.coordinator.memory.callback_auth import (
                MemoryAuthNotConfiguredError,
            )

            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: secure memory mode requires a "
                "protected coordinator secret (coordinator_secret in .env or "
                "A2A_COORDINATOR_SECRET)"
            )
        from a2a.worker.callback_sender import CallbackSigner

        secret = raw.encode("utf-8") if isinstance(raw, str) else raw
        callback_signer = CallbackSigner(worker_id, secret)

    # .env 值为 fallback，CLI 显式传入的参数优先
    effective_model = env.get("model", model)
    effective_provider = env.get("provider", provider)
    effective_api_base = env.get("api_base", api_base)

    # 加载 config.yaml 中的外部资源路径默认值
    # 优先级: CLI 显式 > config.yaml > None
    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    path_cfg = load_path_config(config_path, "worker")
    effective_prompts_dir = prompts_dir or (
        str(path_cfg.prompts_dir) if path_cfg.prompts_dir else None
    )
    effective_tools_dir = tools_dir or (
        str(path_cfg.tools_dir) if path_cfg.tools_dir else None
    )
    effective_skills_dir = skills_dir or (
        str(path_cfg.skills_dir) if path_cfg.skills_dir else None
    )
    effective_log_dir = Path(path_cfg.log_dir) if path_cfg.log_dir else None

    # 沙箱策略
    project_root = Path(__file__).parent.parent.parent.parent.resolve()
    if sandbox_profile == "off":
        sandbox_policy = SandboxPolicy.off()
    elif sandbox_profile == "workspace":
        sandbox_policy = SandboxPolicy.workspace(
            project_root=project_root, workspace_dir="./workspace"
        )
    else:
        raise typer.BadParameter(
            f"Invalid sandbox profile '{sandbox_profile}'. Must be 'off' or 'workspace'."
        )

    cap_list = [c.strip() for c in capabilities.split(",") if c.strip()]
    a2a_endpoint = f"http://{a2a_host}:{a2a_port}/"

    client = CoordinatorWebSocketClient(
        coordinator_url=coordinator_url,
        worker_id=worker_id,
        a2a_endpoint=a2a_endpoint,
    )

    a2a_server = create_worker_a2a_server(
        worker_id=worker_id,
        host=a2a_host,
        port=a2a_port,
        capabilities=cap_list,
        model=effective_model,
        prompts_dir=Path(effective_prompts_dir) if effective_prompts_dir else None,
        tools_dir=Path(effective_tools_dir) if effective_tools_dir else None,
        skills_dir=Path(effective_skills_dir) if effective_skills_dir else None,
        log_dir=effective_log_dir,
        max_steps=max_steps,
        temperature=temperature,
        provider=effective_provider,
        api_base=effective_api_base,
        api_key_env=api_key_env,
        sandbox_policy=sandbox_policy,
        callback_signer=callback_signer,
    )

    async def run():
        server_task = asyncio.create_task(a2a_server.serve())
        await client.connect()
        try:
            await asyncio.Future()
        finally:
            await client.disconnect()
            server_task.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    app()
