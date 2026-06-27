"""Coordinator CLI 入口。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from a2a.coordinator.server import create_server
from a2a.shared.env_loader import load_env_file
from a2a.shared.path_config import load_path_config

app = typer.Typer(help="OpenHarness A2A Coordinator")


@app.command()
def main(
    host: str = typer.Option("0.0.0.0", "--host", help="REST API host"),
    port: int = typer.Option(8080, "--port", help="REST API port"),
    a2a_port: int = typer.Option(8081, "--a2a-port", help="A2A server port"),
    config_path: str = typer.Option(
        None, "--config", help="Path to agents.yaml config"
    ),
    prompts_dir: str = typer.Option(
        None, "--prompts-dir", help="Path to Coordinator prompts directory"
    ),
    tools_dir: str = typer.Option(
        None, "--tools-dir", help="Path to custom tools directory"
    ),
    skills_dir: str = typer.Option(
        None, "--skills-dir", help="Path to skills directory"
    ),
    env_file: str = typer.Option(".env", "--env-file", help="Path to .env config file"),
    model: str = typer.Option(
        "claude-opus-4-5", "--model", help="Router Agent LLM model"
    ),
    max_steps: int = typer.Option(
        15, "--max-steps", help="Max Agent steps for routing"
    ),
    temperature: float = typer.Option(0.7, "--temperature", help="LLM temperature"),
    provider: str = typer.Option(
        "anthropic", "--provider", help="LLM provider (anthropic|openai)"
    ),
    api_base: str = typer.Option(
        "https://api.anthropic.com", "--api-base", help="LLM API base URL"
    ),
    api_key_env: str = typer.Option(
        "ANTHROPIC_API_KEY", "--api-key-env", help="Env var for API key"
    ),
    # --- Verifier 参数 ---
    verifier_model: str = typer.Option(
        None, "--verifier-model", help="Verifier Agent LLM model (defaults to --model)"
    ),
    verifier_max_steps: int = typer.Option(
        10, "--verifier-max-steps", help="Max Agent steps for verification"
    ),
    verifier_temperature: float = typer.Option(
        0.3, "--verifier-temperature", help="Verifier LLM temperature"
    ),
    no_verifier: bool = typer.Option(
        False, "--no-verifier", help="Disable the verification agent"
    ),
    # --- Orchestration 参数 ---
    orchestration_mode: str = typer.Option(
        "agentic", "--orchestration-mode", help="Orchestration mode (dag|agentic)"
    ),
    max_tasks_per_run: int = typer.Option(
        20, "--max-tasks-per-run", help="Max tasks per agentic orchestration run"
    ),
    orchestration_timeout: int = typer.Option(
        600, "--orchestration-timeout", help="Agentic orchestration timeout in seconds"
    ),
) -> None:
    """启动 Coordinator。"""
    logging.basicConfig(level=logging.INFO)

    # 加载 .env 文件，用其中的值作为 fallback 默认值
    env = load_env_file(env_file)

    # .env 中的 api_key 设置到环境变量，供下游读取
    if "api_key" in env:
        os.environ[api_key_env] = env["api_key"]

    # .env 值为 fallback，CLI 显式传入的参数优先
    effective_model = env.get("model", model)
    effective_provider = env.get("provider", provider)
    effective_api_base = env.get("api_base", api_base)

    # 加载 config.yaml 中的外部资源路径默认值
    # 优先级: CLI 显式 > config.yaml > None
    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    path_cfg = load_path_config(config_path, "coordinator")
    effective_prompts_dir = prompts_dir or (
        str(path_cfg.prompts_dir) if path_cfg.prompts_dir else None
    )
    effective_tools_dir = tools_dir or (
        str(path_cfg.tools_dir) if path_cfg.tools_dir else None
    )
    effective_skills_dir = skills_dir or (
        str(path_cfg.skills_dir) if path_cfg.skills_dir else None
    )
    effective_log_dir = str(path_cfg.log_dir) if path_cfg.log_dir else None

    server = create_server(
        host=host,
        port=port,
        a2a_port=a2a_port,
        config_path=config_path,
        prompts_dir=effective_prompts_dir,
        tools_dir=effective_tools_dir,
        skills_dir=effective_skills_dir,
        log_dir=effective_log_dir,
        router_model=effective_model,
        router_max_steps=max_steps,
        router_temperature=temperature,
        router_provider=effective_provider,
        router_api_base=effective_api_base,
        router_api_key_env=api_key_env,
        verifier_model=verifier_model,
        verifier_max_steps=verifier_max_steps,
        verifier_temperature=verifier_temperature,
        verifier_enabled=not no_verifier,
        orchestration_mode=orchestration_mode,
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
    )
    server.run()


if __name__ == "__main__":
    app()
