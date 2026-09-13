"""Worker 上的 A2A HTTP Server。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

import a2a.server.request_handlers.request_handler as _a2a_handler
from a2a.server.request_handlers import (
    DefaultRequestHandlerV2,
    validate_request_params,
)
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.shared.server_lifecycle import shutdown_a2a_active_tasks
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.task import apply_history_length
from Agent.worker_agent.context import ContextConfig

_a2a_handler.validate_proto_required_fields = lambda _msg: None


def _assert_return_immediately_sdk_contract() -> None:
    """Fail fast if the pinned SDK no longer exposes the members the
    ``return_immediately`` fast-ack path relies on.

    ``_ReturnImmediatelyAwareRequestHandler`` deliberately reuses the private
    SDK method ``_setup_active_task`` so task/context-id generation, push-config
    registration, and ActiveTask startup stay exactly the SDK's.  If the SDK
    renames or removes that contract, this worker fails closed at startup
    instead of silently regressing to the deterministic dispatch deadlock.
    """
    setup = getattr(DefaultRequestHandlerV2, "_setup_active_task", None)
    if not callable(setup):
        raise TypeError(
            "a2a-sdk DefaultRequestHandlerV2 no longer exposes "
            "_setup_active_task(); cannot install the return_immediately "
            "fast-ack handler (dispatch deadlock regression guard)."
        )
    from a2a.server.agent_execution.active_task import ActiveTask

    if not callable(getattr(ActiveTask, "enqueue_request", None)):
        raise TypeError(
            "a2a-sdk ActiveTask no longer exposes enqueue_request(); "
            "cannot install the return_immediately fast-ack handler "
            "(dispatch deadlock regression guard)."
        )


class _ReturnImmediatelyAwareRequestHandler(DefaultRequestHandlerV2):
    """``DefaultRequestHandlerV2`` that breaks the deterministic
    ``return_immediately`` dispatch deadlock on the worker.

    SDK ``on_message_send`` with ``configuration.return_immediately=True``
    still subscribes to the ActiveTask until the first executor Task event
    before replying.  The SDK EventConsumer awaits the push-notification send
    BEFORE enqueueing that first event to subscribers, and the Coordinator can
    only admit the push after it has bound ``worker_task_id`` from this very
    HTTP response — a circular dependency that stalls every dispatch by the full
    push-retry budget (~6s+) and can exhaust the sender's 6-attempt backoff.

    This subclass, ONLY for ``return_immediately=True`` requests, invokes the
    SDK task setup exactly once (worker task/context id generation, push-config
    registration, ActiveTask start) and then replies immediately with an initial
    WORKING Task carrying the generated ids — without subscribing or waiting for
    executor events.  The request is then enqueued for background execution; the
    executor's push notifications are admitted because the coordinator's binding
    lands right after this response (the existing 0.2s sender retry remains a
    real backstop for the residual race).  All other (blocking/streaming)
    requests keep the SDK's exact behaviour.
    """

    @validate_request_params
    async def on_message_send(
        self,
        params: Any,
        context: Any,
    ) -> Any:
        if not params.configuration.return_immediately:
            return await super().on_message_send(params, context)

        # Private SDK method (guarded by _assert_return_immediately_sdk_contract):
        # generates the worker task/context ids, registers the push config, and
        # starts the ActiveTask producer/consumer exactly as the SDK does.
        active_task, request_context = await self._setup_active_task(params, context)

        # The initial "STARTED" ack: carries the generated worker task id and
        # context id so the coordinator can bind worker_task_id immediately.
        initial_task = Task(
            id=request_context.task_id,
            context_id=request_context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )

        # Start background execution WITHOUT subscribing: the SDK's subscribe()
        # path is what blocked the HTTP reply on executor events.
        await active_task.enqueue_request(request_context)

        return apply_history_length(initial_task, params.configuration)


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
    context_config: ContextConfig | None = None,
    token_limit: int = 80000,
    require_explicit_completion: bool = False,
    sandbox_policy=None,
    state_provider=None,
    # ── Phase 2: DI for envelope-aware adapter ──
    envelope_ingress: Any = None,
    mailbox_store: Any = None,
    team_state_store: Any = None,
    # ── Phase 2: signed push callback sender ──
    callback_signer: Any = None,
    # ── 任务生命周期回调：execute 进入/退出时回调（worker 空闲心跳用）──
    task_lifecycle_cb: Callable[[bool], None] | None = None,
    # ── Phase 3: declared sensor types → sensor_type:<slug> metadata tags ──
    # 默认无 sensors（SAR 虚拟仿真恒空，机制验证走 fixture）。
    sensors: list[str] | None = None,
) -> uvicorn.Server:
    """创建 Worker A2A HTTP Server。

    When *envelope_ingress*, *mailbox_store*, and *team_state_store* are
    provided, an ``EnvelopeAwareAdapter`` is used instead of the plain
    ``AgentAdapter``, enabling signed envelope classification (Phase 2).
    When *callback_signer* is provided the default ``BasePushNotificationSender``
    is replaced by ``SignedPushNotificationSender``; otherwise the AgentCard
    advertises ``push_notifications=false`` so a secure Coordinator never
    creates an unsigned push config.
    """
    from a2a.worker.agent_adapter import AgentAdapter, EnvelopeAwareAdapter

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
    if sensors:
        skills.append(
            AgentSkill(
                id="sensors",
                name="Sensors",
                description="Sensor metadata: " + ", ".join(sensors),
                tags=["metadata"] + [f"sensor_type:{s}" for s in sensors],
            )
        )

    agent_card = AgentCard(
        name=f"Mini-Agent Worker {worker_id}",
        description=f"Mini-Agent worker node {worker_id}",
        version="1.0.0",
        capabilities=AgentCapabilities(
            streaming=True, push_notifications=callback_signer is not None
        ),
        skills=skills,
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"http://localhost:{port}/api/v1/jsonrpc/",
            )
        ],
    )

    # 每个 worker 有独立的日志子目录
    worker_log_dir = (log_dir / worker_id) if log_dir else None

    # Phase 2: envelope-aware adapter.  ALL or NONE — any partial
    # combination is rejected at construction.
    di_provided = sum(
        1 for x in (envelope_ingress, mailbox_store, team_state_store) if x is not None
    )
    if di_provided not in (0, 3):
        raise TypeError(
            "envelope_ingress, mailbox_store, and team_state_store must all be "
            f"provided together; got {di_provided}/3"
        )
    if di_provided == 3:
        executor = EnvelopeAwareAdapter(
            ingress=envelope_ingress,
            mailbox=mailbox_store,
            team_state=team_state_store,
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
            context_config=context_config,
            token_limit=token_limit,
            require_explicit_completion=require_explicit_completion,
            sandbox_policy=sandbox_policy,
            state_provider=state_provider,
            task_lifecycle_cb=task_lifecycle_cb,
        )
    else:
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
            context_config=context_config,
            token_limit=token_limit,
            require_explicit_completion=require_explicit_completion,
            sandbox_policy=sandbox_policy,
            state_provider=state_provider,
            task_lifecycle_cb=task_lifecycle_cb,
        )

    push_config_store = InMemoryPushNotificationConfigStore()
    push_httpx_client = httpx.AsyncClient()
    if callback_signer is not None:
        from a2a.worker.callback_sender import SignedPushNotificationSender

        push_sender = SignedPushNotificationSender(
            httpx_client=push_httpx_client,
            config_store=push_config_store,
            signer=callback_signer,
        )
    else:
        push_sender = BasePushNotificationSender(
            httpx_client=push_httpx_client,
            config_store=push_config_store,
        )

    _assert_return_immediately_sdk_contract()

    request_handler = _ReturnImmediatelyAwareRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
        push_config_store=push_config_store,
        push_sender=push_sender,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await shutdown_a2a_active_tasks(request_handler)
            await push_httpx_client.aclose()

    routes: list[Route] = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/api/v1/jsonrpc/"))
    app = Starlette(routes=routes, lifespan=lifespan)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.executor = executor  # type: ignore[attr-defined]
    server.agent_card = agent_card  # type: ignore[attr-defined]
    return server
