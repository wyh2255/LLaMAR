"""Agent 构建模块 —— Agent + Controller 的单点组装入口。

参考 DeepSeek-Reasonix 的 boot.Build() 模式：所有前端（A2A、CLI、实验）
都通过 build_agent() / build_controller() 构建实例，不再散落构造逻辑。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from Agent.controller import AgentController, SessionAPI

from .agent import Agent
from .context import ContextConfig, WorkerContextManager
from .hooks import AgentHooks
from .llm import LLMClient
from .schema import LLMProvider
from .tools.base import Tool
from .tools.bash_tool import BashTool
from .tools.file_tools import ReadTool, WriteTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class AgentBuildOptions:
    """构建 Agent 实例的全部参数。"""

    # LLM
    model: str
    provider: str  # "anthropic" | "openai"
    api_base: str
    api_key: str

    # Prompt
    system_prompt: str  # 最终 system prompt 文本

    # Tools
    tools: list[Tool]  # 领域工具（SAR NavigateTo 等）
    extra_tools: list[Tool] | None = None  # 运行时额外工具
    include_base_tools: bool = True  # 是否加入 ReadTool/WriteTool/BashTool

    # Agent 参数
    max_steps: int = 50
    workspace_dir: str = "./workspace"
    token_limit: int = 80000
    log_dir: str | Path | None = None

    # 上下文管理
    context_strategy: str = "hybrid"
    context_recent_messages: int = 12
    context_summary_trigger_ratio: float = 0.8
    context_pinned_enabled: bool = True
    hooks: AgentHooks | None = None

    # 完成语义
    require_explicit_completion: bool = False


@dataclass
class ControllerBuildOptions:
    """构建 AgentController 的全部参数。"""

    agent: AgentBuildOptions
    context_config: ContextConfig | None = None
    token_limit: int = 80000
    require_explicit_completion: bool = False


# ---------------------------------------------------------------------------
# 组装函数
# ---------------------------------------------------------------------------


def build_agent(opts: AgentBuildOptions) -> Agent:
    """创建 LLMClient → 组装 tools → 返回 Agent 实例。

    tools 组装顺序：base_tools（若 include_base_tools）+ opts.tools + opts.extra_tools。
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
    )

    tools: list[Tool] = []
    if opts.include_base_tools:
        tools.append(ReadTool())
        tools.append(WriteTool())
        tools.append(BashTool(workspace_dir=opts.workspace_dir))
    tools.extend(opts.tools)
    if opts.extra_tools:
        tools.extend(opts.extra_tools)

    return Agent(
        llm_client=llm_client,
        system_prompt=opts.system_prompt,
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


def build_controller(
    opts: ControllerBuildOptions,
    *,
    session_factory: Callable[[], Any] | None = None,
    agent_factory: Callable[..., Agent] | None = None,
) -> SessionAPI:
    """创建 AgentController。

    默认 agent_factory 用 build_agent，默认 session_factory 用 WorkerContextManager。
    调用方可覆盖任一个以实现定制。
    """
    if agent_factory is None:
        _opts_agent = opts.agent

        def _default_agent_factory(**kwargs: Any) -> Agent:
            return build_agent(_opts_agent)

        agent_factory = _default_agent_factory

    if session_factory is None:
        _ctx_config = opts.context_config
        _tok_limit = opts.token_limit

        def _default_session_factory() -> Any:
            return WorkerContextManager(_ctx_config, _tok_limit)

        session_factory = _default_session_factory

    return AgentController(
        agent_factory=agent_factory,
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
    module_prefix: str = "_a2a_worker_tool_",
) -> list[Tool]:
    """从 tools_dir/*.py 动态导入 Tool 实例。

    跳过以 _ 开头的文件。模块名使用 module_prefix + 文件名防止冲突。
    """
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
            raise ImportError(
                f"加载自定义工具失败 '{py_file.name}': {e}"
            ) from e
    return tools


def discover_skills_dir(scope: str = "worker") -> Path | None:
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


def build_worker_context_manager(
    config: ContextConfig | None = None,
    token_limit: int = 80000,
) -> WorkerContextManager:
    """快捷创建 WorkerContextManager。"""
    return WorkerContextManager(config, token_limit)
