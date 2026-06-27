"""AgentAdapter - 桥接 Mini-Agent 到 A2A EventQueue 的通用适配器。

遵循 executor.py 的直接事件推送模式，使用 Agent.run() 的 step_callback
钩子将每步输出实时推送为 A2A 事件。

每次 execute() 调用创建独立 Agent 实例，确保并发安全。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState, TaskStatus, Part, TaskStatusUpdateEvent
from a2a.helpers import new_text_message

logger = logging.getLogger(__name__)

from Agent.worker_agent.agent import Agent
from Agent.worker_agent.tools.bash_tool import BashTool
from Agent.worker_agent.tools.file_tools import ReadTool, WriteTool
from Agent.worker_agent.tools.skill_loader import SkillLoader
from Agent.worker_agent.tools.skill_tool import GetSkillTool
from Agent.worker_agent.llm import LLMClient
from Agent.worker_agent.schema import LLMProvider


class AgentAdapter(AgentExecutor):
    """
    桥接 Mini-Agent Agent.run() 到 A2A EventQueue 的通用适配器。

    每次 execute() 调用创建独立 Agent 实例，确保并发请求隔离。
    支持通过构造函数注入外部 prompt/tool/skill 路径。
    """

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

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """A2A AgentExecutor 接口实现。每次调用创建独立 Agent 实例。"""
        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id

        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work(message=new_text_message("Starting work"))

        query = context.get_user_input() or ""

        try:
            # ✅ 每次 execute() 创建新 Agent，不复用
            agent = self._build_agent()
            # ✅ 暂存引用，供 cancel() 使用
            self._current_agent = agent
            agent.add_user_message(query)

            async def _step_handler(type_: str, **data: Any) -> None:
                await self._on_step_event(type_, data, event_queue, task_id, context_id)
                # 如果设定了外部 step_callback，也调用它
                if self._step_callback is not None:
                    self._step_callback(type_=type_, **data)

            final_text = await agent.run(step_callback=_step_handler)

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
        """取消任务。发送取消信号给正在运行的 Agent。"""
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        # ✅ 使用暂存的 Agent 引用发送取消信号
        if hasattr(self, "_current_agent") and self._current_agent is not None:
            if self._current_agent.cancel_event is not None:
                try:
                    self._current_agent.cancel_event.set()
                except Exception as e:
                    logger.warning("Failed to cancel agent: %s", e)
            self._current_agent = None
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel()

    def _build_agent(self) -> Agent:
        """构建新 Agent 实例。每次调用返回新实例，不复用。"""
        provider_map = {
            "anthropic": LLMProvider.ANTHROPIC,
            "openai": LLMProvider.OPENAI,
        }
        provider = provider_map.get(self._provider, LLMProvider.ANTHROPIC)

        api_key = os.environ.get(self._api_key_env, "")
        llm_client = LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self._api_base,
            model=self._model,
        )

        # 加载 system prompt
        system_prompt = self._load_prompt() or ""
        # Merge with backward-compat system_prompt param
        if self._system_prompt:
            if system_prompt:
                system_prompt = self._system_prompt + "\n\n" + system_prompt
            else:
                system_prompt = self._system_prompt

        # 基础 tools（领域专用 agent 可禁用，如 SAR worker）
        tools: list = []
        if self._include_base_tools:
            tools.extend([ReadTool(), WriteTool()])
            workspace_dir = str(self._workspace_dir)
            if workspace_dir:
                tools.append(BashTool(workspace_dir=workspace_dir))

        # 自定义 tools
        custom_tools = self._load_custom_tools()
        tools.extend(custom_tools)

        # 向后兼容的 extra_tools
        tools.extend(self._extra_tools)

        # skills
        skills_dir = self._skills_dir or self._discover_skills_dir()
        if skills_dir:
            try:
                skill_loader = SkillLoader(skills_dir=str(skills_dir))
                skill_loader.discover_skills()
                metadata_prompt = skill_loader.get_skills_metadata_prompt()
                if metadata_prompt:
                    system_prompt = system_prompt + "\n\n" + metadata_prompt
                tools.append(GetSkillTool(skill_loader))
            except Exception as e:
                logger.warning("Failed to load skills from %s: %s", skills_dir, e)

        return Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            log_dir=self._log_dir,
        )

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

    async def _on_step_event(
        self,
        type_: str,
        data: dict[str, Any],
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
    ) -> None:
        """将 Mini-Agent 步进事件转换为 A2A 事件。"""
        if type_ == "llm_response":
            content = data.get("content")
            if content:
                display = content[:2000] + ("..." if len(content) > 2000 else "")
                text = f"[LLM] {display}"
                data_json = json.dumps(
                    {
                        "ev": "llm_response",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "content": content[:2000],
                    },
                    ensure_ascii=False,
                )
                text += f"\n[DATA]\n{data_json}"
                status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
                status.message.CopyFrom(new_text_message(text))
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=status,
                    )
                )
        elif type_ == "tool_start":
            tool_name = data.get("tool_name", "")
            tool_args = data.get("arguments", {})
            args_str = json.dumps(tool_args, ensure_ascii=False) if tool_args else "{}"
            args_preview = args_str[:100] + ("..." if len(args_str) > 100 else "")
            text = f"[Tool] {tool_name}: {args_preview}"
            data_json = json.dumps(
                {
                    "ev": "tool_start",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool_name": tool_name,
                    "arguments": args_str[:1000],
                },
                ensure_ascii=False,
            )
            text += f"\n[DATA]\n{data_json}"
            status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
            status.message.CopyFrom(new_text_message(text))
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=status,
                )
            )
        elif type_ == "tool_result":
            tool_name = data.get("tool_name", "")
            success = data.get("success", False)
            content = data.get("content", "")
            label = "[Result]" if success else "[Error]"
            truncated = content[:197] + "..." if len(content) > 200 else content
            text = f"{label} {tool_name}: {truncated}"
            data_json = json.dumps(
                {
                    "ev": "tool_result",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool_name": tool_name,
                    "success": success,
                    "content": (content or "")[:3000],
                },
                ensure_ascii=False,
            )
            text += f"\n[DATA]\n{data_json}"
            status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
            status.message.CopyFrom(new_text_message(text))
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=status,
                )
            )
        else:
            logger.debug("Unhandled step event type: %s", type_)
