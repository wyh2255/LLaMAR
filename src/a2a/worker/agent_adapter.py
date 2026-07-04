"""AgentAdapter - 桥接 Mini-Agent 到 A2A EventQueue 的通用适配器。

遵循 executor.py 的直接事件推送模式，使用 Agent.run() 的 step_callback
钩子将每步输出实时推送为 A2A 事件。

每次 execute() 调用创建独立 Agent 实例，确保并发安全。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from a2a.helpers import new_text_message

from Agent.worker_agent.agent import Agent
from Agent.worker_agent.context import ContextConfig
from Agent.controller import CallbackSink, SessionAPI, TeeSink
from Agent.worker_agent.build import (
    AgentBuildOptions,
    ControllerBuildOptions,
    build_agent,
    build_controller,
)
from a2a.worker.sink import A2AWorkerSink

logger = logging.getLogger(__name__)


class AgentAdapter(AgentExecutor):
    """
    桥接 Mini-Agent Agent.run() 到 A2A EventQueue 的通用适配器。

    每次 execute() 调用创建独立 Agent 实例，确保并发请求隔离。
    支持通过构造函数注入外部 prompt/tool/skill 路径。
    通过 context_id 维持跨子任务的会话记忆。
    """

    _log_dir: Path | None = None

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        prompts_dir: Path | None = None,
        tools_dir: Path | None = None,
        skills_dir: Path | None = None,
        max_steps: int = 50,
        temperature: float = 0.7,
        workspace_dir: str = "./workspace",
        provider: str = "anthropic",
        api_base: str = "https://api.anthropic.com",
        api_key_env: str = "ANTHROPIC_API_KEY",
        system_prompt: str = "",
        extra_tools: list | None = None,
        step_callback: Any | None = None,
        log_dir: Path | None = None,
        include_base_tools: bool = True,
        context_config: ContextConfig | None = None,
        token_limit: int = 80000,
        sandbox_policy=None,
        require_explicit_completion: bool = False,
    ):
        self._model = model
        self._prompts_dir = prompts_dir
        self._tools_dir = tools_dir
        self._skills_dir = skills_dir
        self._max_steps = max_steps
        self._temperature = temperature
        self._workspace_dir = Path(workspace_dir)
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        # Backward-compat params (will be superseded by dir-based injection in later tasks)
        self._system_prompt = system_prompt
        self._extra_tools = extra_tools or []
        self._step_callback = step_callback
        self._log_dir = log_dir
        self._include_base_tools = include_base_tools
        self._context_config = context_config
        self._token_limit = token_limit
        self._require_explicit_completion = require_explicit_completion

        self._sandbox_policy = sandbox_policy

        # 统一控制器：通过 build_controller 组装。
        # agent_factory 保留 role/引擎覆盖扩展点
        # （LangAgentAdapter 覆盖 _build_agent 即自动生效）。
        self._agent_opts = AgentBuildOptions(
            model=self._model,
            provider=self._provider,
            api_base=self._api_base,
            api_key=os.environ.get(self._api_key_env, ""),
            system_prompt=self._load_prompt() or "",
            tools=self._extra_tools,
            include_base_tools=self._include_base_tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            token_limit=self._token_limit,
            log_dir=self._log_dir,
            require_explicit_completion=self._require_explicit_completion,
            sandbox_policy=self._sandbox_policy,
        )
        self._controller: SessionAPI = build_controller(
            ControllerBuildOptions(
                agent=self._agent_opts,
                context_config=self._context_config,
                token_limit=self._token_limit,
                require_explicit_completion=self._require_explicit_completion,
            ),
            agent_factory=lambda **kw: self._build_agent(),
        )

    def clear_sessions(self) -> None:
        """清空所有会话存储（实验结束时调用）。"""
        self._controller.clear_sessions()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """A2A AgentExecutor 接口实现。经统一控制器驱动一次 Agent 运行。"""
        query_preview = (context.get_user_input() or "")[:200]
        logger.info(
            "[ENTRY] task=%s context=%s query=%s",
            getattr(context, "task_id", "?"),
            getattr(context, "context_id", "?"),
            query_preview,
        )
        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id

        # Pass task_id/context_id to Agent for NDJSON logging
        if hasattr(self, "_agent_opts") and self._agent_opts is not None:
            self._agent_opts.task_id = task_id
            self._agent_opts.context_id = context_id

        updater = TaskUpdater(event_queue, task_id, context_id)

        # 构造输出通道：A2A 传输 sink（+ 可选外部 step_callback）
        sink = A2AWorkerSink(event_queue, task_id, context_id)
        if self._step_callback is not None:

            def ext_cb(type_, **data):
                return self._step_callback(type_=type_, **data)

            sink = TeeSink([sink, CallbackSink(ext_cb)])

        # Check for existing snapshot (resume after input-required)
        ctx = (
            self._controller._get_session(context_id)
            if hasattr(self._controller, "_get_session")
            else None
        )
        snapshot = ctx.load_snapshot(task_id) if ctx is not None else None

        if snapshot:
            logger.info(
                "[RESUME] task=%s context=%s snapshot_length=%d",
                task_id,
                context_id,
                len(snapshot),
            )
            await updater.start_work(message=new_text_message("Resuming after help"))
            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id,
                query,
                sink,
                task_id=task_id,
                initial_messages=snapshot,
            )
        else:
            await updater.start_work(message=new_text_message("Starting work"))

            # Inject AskCoordinatorTool
            from a2a.worker.tools.ask_coordinator import AskCoordinatorTool

            ask_tool = AskCoordinatorTool()
            if not any(t.name == "ask_coordinator" for t in self._extra_tools):
                self._extra_tools = [ask_tool] + self._extra_tools

            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id,
                query,
                sink,
                task_id=task_id,
            )

        try:
            if result.need_input:
                logger.info(
                    "[PAUSE] task=%s context=%s question=%s",
                    task_id,
                    context_id,
                    result.content[:200],
                )
                await updater.requires_input(message=new_text_message(result.content))
                return

            final_text = result.content
            if final_text:
                await updater.add_artifact(
                    parts=[Part(text=final_text)],
                    name="result",
                )
            await updater.complete()
        except asyncio.CancelledError:
            await updater.cancel()
            raise
        except Exception as e:
            await updater.failed(message=new_text_message(str(e)))
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消任务。经控制器发送取消信号给正在运行的 Agent。"""
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        self._controller.cancel()
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel()

    def _build_agent(self) -> Agent:
        """构建新 Agent 实例。委托给 build_agent。"""
        return build_agent(self._agent_opts)

    def _load_prompt(self) -> str | None:
        """从 prompts_dir 加载 system.md。"""
        if self._prompts_dir is None:
            return None
        prompt_file = self._prompts_dir / "system.md"
        if not prompt_file.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {prompt_file}. "
                f"Either create the file or omit --prompts-dir."
            )
        return prompt_file.read_text(encoding="utf-8")

    def _load_custom_tools(self) -> list:
        """从 tools_dir 加载自定义 Tool。"""
        if self._tools_dir is None or not self._tools_dir.is_dir():
            return []
        tools = []
        for py_file in sorted(self._tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"_a2a_worker_tool_{py_file.stem}", str(py_file)
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "tool"):
                    tools.append(module.tool)
                    logger.info("Loaded custom worker tool: %s", py_file.name)
            except Exception as e:
                raise ImportError(
                    f"Failed to load custom tool '{py_file.name}': {e}"
                ) from e
        return tools

    def _discover_skills_dir(self) -> Path | None:
        """尝试从约定路径发现 skills 目录。"""
        for candidate in [
            Path.cwd() / "skills" / "worker",
            Path(__file__).parent.parent / "skills" / "worker",
        ]:
            if candidate.is_dir():
                return candidate
        return None
