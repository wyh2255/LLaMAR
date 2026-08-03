"""Router Agent 构建模块 —— Agent + Controller 的单点组装入口。

参考 DeepSeek-Reasonix 的 boot.Build() 模式：与 worker_agent/build.py 对称，
面向 Coordinator/Router 场景。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from Agent.controller import AgentController, SessionAPI
from Agent.sandbox import (
    SandboxPolicy,
    validate_custom_tools_dir,
    wrap_tools_with_sandbox,
)

from .agent import Agent
from .context import ContextConfig, CoordinatorContextManager
from .hooks import AgentHooks
from .llm import LLMClient
from .schema import LLMProvider, SamplingParams
from .tools.base import Tool


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class RouterBuildOptions:
    """构建 Router Agent 实例的全部参数。

    与当前 RouterAgent._build_agent (router.py) 的差异：
    - 首次传递 context_strategy / context_recent_messages 等上下文参数
      （当前代码省略了它们，build_router_agent 将正确传递 —— 行为改善）
    - tools 三源合并：builtin_tools + custom_tools + extra_tools
    """

    # LLM
    model: str
    provider: str  # "anthropic" | "openai"
    api_base: str
    api_key: str

    # Prompt
    system_prompt: str  # 最终 system prompt（内置默认或从文件加载）

    # Tools —— 三源合并
    builtin_tools: list[Tool] | None = None  # 内置（默认 QueryWorkersTool）
    custom_tools: list[Tool] | None = None  # 持久化工具
    extra_tools: list[Tool] | None = None  # 构造时注入的工具

    # 采样参数（temperature / top_p / seed）。None = 不注入，由 provider 取默认。
    # 与 worker 侧 AgentBuildOptions.sampling 对称。
    sampling: SamplingParams | None = None

    # Agent 参数
    max_steps: int = 15
    workspace_dir: str = "./workspace/coordinator"
    token_limit: int = 80000
    log_dir: str | Path | None = None

    # Skills
    skills_dir: str | Path | None = None

    # 上下文管理（当前 RouterAgent._build_agent 未传递这些参数，
    # build_router_agent 将首次正确传递它们）
    context_strategy: str = "hybrid"
    context_recent_messages: int = 12
    context_summary_trigger_ratio: float = 0.8
    context_pinned_enabled: bool = True
    hooks: AgentHooks | None = None

    require_explicit_completion: bool = False

    # 沙箱策略
    sandbox_policy: SandboxPolicy | None = None


@dataclass
class RouterControllerBuildOptions:
    """构建 Router Controller 的全部参数。"""

    agent: RouterBuildOptions
    context_config: ContextConfig | None = None
    token_limit: int = 80000
    require_explicit_completion: bool = False
    state_provider: Any | None = None


# ---------------------------------------------------------------------------
# 组装函数
# ---------------------------------------------------------------------------


def _tool_descriptions_text(tools: list[Tool]) -> str:
    """Generate a text block describing available tools."""
    if not tools:
        return ""
    lines = ["## Available Tools"]
    for t in tools:
        desc = t.description.replace("\n", " ").strip()
        lines.append(f"- **{t.name}**: {desc}")
        if t.parameters and "properties" in t.parameters:
            params = t.parameters["properties"]
            required = set(t.parameters.get("required", []))
            for pname, pinfo in params.items():
                req = " (required)" if pname in required else ""
                pdesc = pinfo.get("description", "").replace("\n", " ")
                lines.append(f"  - `{pname}`{req}: {pdesc}")
    return "\n".join(lines)


def build_router_agent(opts: RouterBuildOptions) -> Agent:
    """创建 LLMClient → 三源合并 tools → 返回 Agent。

    tools 合并顺序：builtin_tools + custom_tools + extra_tools。
    """
    provider_map = {
        "anthropic": LLMProvider.ANTHROPIC,
        "openai": LLMProvider.OPENAI,
    }
    provider = provider_map.get(opts.provider, LLMProvider.ANTHROPIC)

    llm_client = LLMClient(
        api_key=opts.api_key,
        provider=provider,
        api_base=opts.api_base,
        model=opts.model,
        sampling=opts.sampling,
    )

    tools: list[Tool] = []
    if opts.builtin_tools:
        tools.extend(opts.builtin_tools)
    if opts.custom_tools:
        tools.extend(opts.custom_tools)
    if opts.extra_tools:
        tools.extend(opts.extra_tools)

    # Skills: discover and register GetSkillTool before sandbox wrapping
    skills_text = ""
    if opts.skills_dir:
        skills_path = (
            Path(opts.skills_dir)
            if isinstance(opts.skills_dir, (str, Path))
            else opts.skills_dir
        )
        if skills_path.is_dir():
            try:
                from .tools.skill_loader import SkillLoader
                from .tools.skill_tool import GetSkillTool

                skill_loader = SkillLoader(skills_dir=str(skills_path))
                skill_loader.discover_skills()
                skills_text = skill_loader.get_skills_metadata_prompt()
                tools.append(GetSkillTool(skill_loader))
                logger.info(
                    "Skills loaded from %s: %d skills",
                    skills_path,
                    len(skill_loader.list_skills()),
                )
            except Exception as e:
                logger.warning("Failed to load skills from %s: %s", skills_path, e)

    tools = wrap_tools_with_sandbox(tools, opts.sandbox_policy)

    # Build a comprehensive system prompt with tool descriptions + skills
    tool_text = _tool_descriptions_text(tools)
    base_prompt = opts.system_prompt
    if tool_text and tool_text not in base_prompt:
        base_prompt = base_prompt.rstrip() + "\n\n" + tool_text
    if skills_text and skills_text not in base_prompt:
        base_prompt = base_prompt.rstrip() + "\n\n" + skills_text

    return Agent(
        llm_client=llm_client,
        system_prompt=base_prompt,
        tools=tools,
        max_steps=opts.max_steps,
        workspace_dir=opts.workspace_dir,
        token_limit=opts.token_limit,
        log_dir=opts.log_dir,
        context_strategy=opts.context_strategy,
        context_recent_messages=opts.context_recent_messages,
        context_summary_trigger_ratio=opts.context_summary_trigger_ratio,
        context_pinned_enabled=opts.context_pinned_enabled,
        hooks=opts.hooks,
        require_explicit_completion=opts.require_explicit_completion,
    )


def _merge_runtime_kwargs(
    base: RouterBuildOptions,
    extra_tools: list | None,
    system_prompt_override: str | None,
) -> RouterBuildOptions:
    """合并运行时 kwargs 到 base opts。

    AgentController.submit() 通过 factory_kwargs 传递 extra_tools 和
    system_prompt_override，此函数将它们合并到 base opts 的副本中。
    """
    merged = replace(base)
    if extra_tools:
        merged.extra_tools = (
            list(merged.extra_tools) if merged.extra_tools else []
        ) + list(extra_tools)
    if system_prompt_override is not None:
        merged.system_prompt = system_prompt_override
    return merged


def build_router_controller(
    opts: RouterControllerBuildOptions,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> SessionAPI:
    """创建 Router 的 AgentController。

    agent_factory 自动处理 AgentController.submit() 传入的运行时
    extra_tools 和 system_prompt_override（与当前 CoordinatorAgentExecutor
    的行为一致）。
    """
    base_agent_opts = opts.agent

    def _factory(**kwargs) -> Agent:
        merged = _merge_runtime_kwargs(
            base_agent_opts,
            extra_tools=kwargs.get("extra_tools"),
            system_prompt_override=kwargs.get("system_prompt_override"),
        )
        return build_router_agent(merged)

    if session_factory is None:
        _ctx_config = opts.context_config
        _tok_limit = opts.token_limit
        _log_dir = base_agent_opts.log_dir if base_agent_opts else None
        _state_provider = opts.state_provider

        def _default_session_factory() -> Any:
            return CoordinatorContextManager(
                _ctx_config, _tok_limit, _log_dir, state_provider=_state_provider
            )

        session_factory = _default_session_factory

    return AgentController(
        agent_factory=_factory,
        session_factory=session_factory,
        require_explicit_completion=opts.require_explicit_completion,
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def load_system_prompt(
    prompts_dir: Path | None,
    *,
    override: str | None = None,
    append: str | None = None,
    default: str = "",
) -> str:
    """从 prompts_dir/system.md 加载 prompt。

    优先级：override > file > default。
    """
    if override is not None:
        result = override
    elif prompts_dir is not None:
        prompt_file = prompts_dir / "system.md"
        if prompt_file.exists():
            result = prompt_file.read_text(encoding="utf-8")
        else:
            result = default
    else:
        result = default

    if append:
        result = result + "\n\n" + append if result else append
    return result


def load_custom_tools(
    tools_dir: Path | None,
    *,
    module_prefix: str = "_a2a_coordinator_tool_",
    sandbox_policy: SandboxPolicy | None = None,
) -> list[Tool]:
    """从 tools_dir/*.py 动态导入 Tool 实例。

    跳过以 _ 开头的文件。模块名使用 module_prefix + 文件名防止冲突。
    """
    tools_dir = validate_custom_tools_dir(tools_dir, sandbox_policy)
    if tools_dir is None or not tools_dir.is_dir():
        return []

    tools: list[Tool] = []
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"{module_prefix}{py_file.stem}", str(py_file)
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "tool"):
                tools.append(module.tool)
                logger.info("加载自定义工具: %s", py_file.name)
        except Exception as e:
            raise ImportError(f"加载自定义工具失败 '{py_file.name}': {e}") from e
    return tools


def discover_skills_dir(scope: str = "coordinator") -> Path | None:
    """从约定路径发现 skills 目录。

    搜索顺序：cwd/skills/{scope}/ → 包目录下的 skills/{scope}/。
    """
    for candidate in [
        Path.cwd() / "skills" / scope,
        Path(__file__).parent.parent.parent / "skills" / scope,
    ]:
        if candidate.is_dir():
            return candidate
    return None


def build_coordinator_context_manager(
    config: ContextConfig | None = None,
    token_limit: int = 80000,
) -> CoordinatorContextManager:
    """快捷创建 CoordinatorContextManager。"""
    return CoordinatorContextManager(config, token_limit)
