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
from typing import Any, TYPE_CHECKING

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from a2a.helpers import new_text_message

from Agent.worker_agent.agent import Agent
from Agent.worker_agent.context import ContextConfig
from Agent.worker_agent.schema import SamplingParams
from Agent.controller import CallbackSink, SessionAPI, TeeSink
from Agent.worker_agent.build import (
    AgentBuildOptions,
    ControllerBuildOptions,
    build_agent,
    build_controller,
)
from a2a.worker.sink import A2AWorkerSink

if TYPE_CHECKING:
    from Agent.router_agent.state_provider import StateProvider

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
        seed: int | None = None,
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
        state_provider: "StateProvider | None" = None,
    ):
        self._model = model
        self._prompts_dir = prompts_dir
        self._tools_dir = tools_dir
        self._skills_dir = skills_dir
        self._max_steps = max_steps
        self._temperature = temperature
        self._seed = seed
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
        self._state_provider = state_provider
        self._task_cancel_events: dict[
            str, asyncio.Event
        ] = {}  # asyncio-single-threaded: no lock needed.

        self._sandbox_policy = sandbox_policy

        # 统一控制器：通过 build_controller 组装。
        # agent_factory 保留 role/引擎覆盖扩展点
        # （LangAgentAdapter 覆盖 _build_agent 即自动生效）。
        self._agent_opts = AgentBuildOptions(
            model=self._model,
            provider=self._provider,
            api_base=self._api_base,
            api_key=os.environ.get(self._api_key_env, ""),
            # self._temperature was stored here and then dropped at this exact
            # boundary -- AgentBuildOptions had no sampling field, so the value
            # died between the adapter and the LLM client (E-1).
            sampling=SamplingParams(
                temperature=self._temperature, seed=self._seed
            ),
            system_prompt=self._load_prompt() or "",
            tools=self._extra_tools,
            include_base_tools=self._include_base_tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            token_limit=self._token_limit,
            log_dir=self._log_dir,
            skills_dir=self._skills_dir,
            require_explicit_completion=self._require_explicit_completion,
            sandbox_policy=self._sandbox_policy,
        )
        self._controller: SessionAPI = build_controller(
            ControllerBuildOptions(
                agent=self._agent_opts,
                context_config=self._context_config,
                token_limit=self._token_limit,
                require_explicit_completion=self._require_explicit_completion,
                state_provider=self._state_provider,
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

        cancel_event = asyncio.Event()
        self._task_cancel_events[task_id] = cancel_event

        updater = TaskUpdater(event_queue, task_id, context_id)

        # 构造输出通道：A2A 传输 sink（+ 可选外部 step_callback）
        sink = A2AWorkerSink(event_queue, task_id, context_id)
        if self._step_callback is not None:

            def ext_cb(type_, **data):
                return self._step_callback(type_=type_, **data)

            sink = TeeSink([sink, CallbackSink(ext_cb)])

        try:
            # Pass task_id/context_id to Agent for NDJSON logging
            if hasattr(self, "_agent_opts") and self._agent_opts is not None:
                self._agent_opts.task_id = task_id
                self._agent_opts.context_id = context_id

            # Check for existing snapshot (resume after input-required)
            snapshot = self._controller.get_snapshot(context_id, task_id)

            if snapshot:
                logger.info(
                    "[RESUME] task=%s context=%s snapshot_length=%d",
                    task_id,
                    context_id,
                    len(snapshot),
                )
                await updater.start_work(
                    message=new_text_message("Resuming after help")
                )
                query = context.get_user_input() or ""
                result = await self._controller.submit(
                    context_id,
                    query,
                    sink,
                    cancel_event=cancel_event,
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
                    # 重绑切断了构造时对 _agent_opts.tools 的别名，需显式同步
                    if hasattr(self, "_agent_opts") and self._agent_opts is not None:
                        self._agent_opts.tools = self._extra_tools

                query = context.get_user_input() or ""
                result = await self._controller.submit(
                    context_id,
                    query,
                    sink,
                    cancel_event=cancel_event,
                    task_id=task_id,
                )

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
            # 区分框架失败（success=False 且非业务终止）与正常完成：
            # 框架失败映射为 A2A FAILED，业务终止维持 COMPLETED
            if result.success is False and not result.task_complete:
                await updater.failed(
                    message=new_text_message(final_text or "Agent run failed")
                )
            else:
                await updater.complete()
        except asyncio.CancelledError:
            await updater.cancel()
            raise
        except Exception as e:
            await updater.failed(message=new_text_message(str(e)))
            raise
        finally:
            self._task_cancel_events.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消任务。只取消指定 task_id 的事件，不伤害共享 context_id。"""
        task_id = context.task_id or ""
        context_id = context.context_id or ""

        ev = self._task_cancel_events.get(task_id)
        if ev is not None:
            ev.set()

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


# ── Phase 2: Envelope-aware adapter ────────────────────────────────────
# Overrides AgentAdapter.execute() to classify incoming requests through
# the EnvelopeIngress classifier.  Mail/control requests are handled
# locally without starting AgentController.  Task/legacy requests forward
# to the parent class.
#
# CancelTask security seam: the A2A SDK's CancelTaskRequest protobuf does
# not carry sender identity, and the framework's request handler passes
# only task_id to cancel().  There is no mechanism to authenticate the
# cancel request at the AgentAdapter.cancel() level without modifying the
# SDK.  We therefore document this as an unresolved integration seam and
# preserve existing cancel behaviour unchanged.


class _OverrideInputContext:
    """Wraps a RequestContext to override get_user_input().

    Used by EnvelopeAwareAdapter when a signed task envelope wraps the
    actual task content — the inner content is returned instead of the
    raw envelope JSON.
    """

    def __init__(self, inner: RequestContext, user_input: str) -> None:
        self._inner = inner
        self._user_input = user_input

    def get_user_input(self, delimiter: str = "\n") -> str:
        return self._user_input

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class EnvelopeAwareAdapter(AgentAdapter):
    """AgentAdapter subclass that classifies incoming requests through
    EnvelopeIngress before dispatching.

    Constructor parameters beyond those of AgentAdapter:

        ingress:   EnvelopeIngress classifier instance.
        mailbox:   WorkerMailboxStore for mail delivery.
        team_state: WorkerTeamState for control messages.

    All three DI components are required at construction.
    """

    def __init__(
        self,
        ingress: Any,
        mailbox: Any,
        team_state: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if ingress is None:
            raise TypeError("ingress is required")
        if mailbox is None:
            raise TypeError("mailbox is required")
        if team_state is None:
            raise TypeError("team_state is required")
        super().__init__(*args, **kwargs)
        self._ingress = ingress
        self._mailbox = mailbox
        self._team_state = team_state

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Classify then dispatch.

        - legacy / task envelope: forward to ``super().execute()``.
        - mail envelope: deliver to mailbox, return terminal completion.
        - team_update / team_revoke: update local team state, return ACK.
        - reject: return terminal failure with reason.
        """
        text = context.get_user_input() or ""
        result = self._ingress.classify(text)
        logger.debug(
            "EnvelopeAwareAdapter classify: action=%s reason=%s",
            result.action,
            result.reason,
        )

        if result.action in ("legacy",):
            return await super().execute(context, event_queue)

        # Route signed task BEFORE local task creation — super().execute()
        # will create its own Task/events.
        if result.action == "task":
            inner_content = result.body.get("content", "")
            wrapped_ctx = _OverrideInputContext(context, inner_content)
            return await super().execute(wrapped_ctx, event_queue)

        if result.action == "reject":
            return await self._reject(result, context, event_queue)

        # Remaining actions (mail, team_update, team_revoke) are local
        # control paths — create a Task for terminal event flow.
        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        if result.action == "mail":
            return await self._handle_mail(result, task, context, updater)

        if result.action == "team_update":
            return await self._handle_team_update(result, context, updater)

        if result.action == "team_revoke":
            return await self._handle_team_revoke(result, updater)

        await self._reject(result, context, event_queue)

    # ── cancel: no authentication possible today (see seam note above) ──

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Delegate to parent.  No sender authentication available at the
        A2A SDK level for CancelTaskRequest — see the integration seam
        note at the top of this class.
        """
        return await super().cancel(context, event_queue)

    # ── internal handlers ───────────────────────────────────────────────

    async def _handle_mail(
        self,
        result: Any,
        task: Any,
        context: RequestContext,
        updater: TaskUpdater,
    ) -> None:
        envelope = result.envelope
        content = result.body.get("content", "")
        subject = result.body.get("subject", "")
        if not isinstance(subject, str):
            subject = ""
        if not isinstance(content, str):
            content = ""

        record = self._mailbox.deliver(
            message_id=envelope.message_id,
            sender_id=envelope.sender_id,
            recipient_id=envelope.recipient_id,
            subject=subject,
            body=content,
            team_id=getattr(envelope, "team_id", None),
            team_epoch=getattr(envelope, "team_epoch", None),
            sent_at=envelope.sent_at.isoformat()
            if hasattr(envelope, "sent_at") and envelope.sent_at
            else "",
        )
        logger.info(
            "Mail delivered id=%s from=%s subj=%s",
            record.message_id,
            record.sender_id,
            subject,
        )
        await updater.complete(
            message=new_text_message(f"Mail delivered (id={record.message_id})")
        )

    async def _handle_team_update(
        self,
        result: Any,
        context: RequestContext,
        updater: TaskUpdater,
    ) -> None:
        body = result.body
        envelope = result.envelope
        # Envelope team_id and team_epoch are authoritative.  If body
        # duplicates them they must match.
        env_team_id = getattr(envelope, "team_id", None)
        env_epoch = getattr(envelope, "team_epoch", None)
        body_team_id = body.get("team_id")
        body_epoch = body.get("epoch")
        if body_team_id is not None and body_team_id != env_team_id:
            reason = f"team_id mismatch: envelope={env_team_id} body={body_team_id}"
            logger.warning("Team update rejected: %s", reason)
            await updater.failed(message=new_text_message(reason))
            return
        if body_epoch is not None and body_epoch != env_epoch:
            reason = f"epoch mismatch: envelope={env_epoch} body={body_epoch}"
            logger.warning("Team update rejected: %s", reason)
            await updater.failed(message=new_text_message(reason))
            return
        try:
            self._team_state.install(
                team_id=env_team_id or body.get("team_id", ""),
                epoch=env_epoch if env_epoch is not None else body.get("epoch", 0),
                members=body.get("members", []),
                endpoints=body.get("endpoints", {}),
                team_secret=body.get("team_secret", ""),
                coordinator_id=envelope.sender_id,
            )
            await updater.complete(message=new_text_message("Team installed"))
        except Exception as exc:
            logger.error("Team update failed")
            logger.debug("Team update failure detail", exc_info=True)
            await updater.failed(message=new_text_message(f"Team update failed: {exc}"))

    async def _handle_team_revoke(
        self,
        result: Any,
        updater: TaskUpdater,
    ) -> None:
        envelope = result.envelope
        try:
            self._team_state.revoke(
                team_id=envelope.team_id or "",
                epoch=envelope.team_epoch if envelope.team_epoch is not None else 0,
            )
            await updater.complete(message=new_text_message("Team revoked"))
        except Exception as exc:
            logger.warning("Team revoke rejected: %s", exc)
            await updater.failed(message=new_text_message(f"Team revoke failed: {exc}"))

    async def _reject(
        self,
        result: Any,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        reason = result.reason or "Rejected"
        logger.warning("Ingress reject: %s", reason)

        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.reject(message=new_text_message(reason))
