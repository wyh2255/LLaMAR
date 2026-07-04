from __future__ import annotations

import asyncio
import json
import logging

from Agent.router_agent.context import ContextConfig

logger = logging.getLogger(__name__)


class SARCoordinator:
    """SAR Coordinator — wraps the A2A CoordinatorServer with SAR-specific tools and prompts."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        barrier=None,  # SARBarrier instance
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
        sandbox_policy=None,
    ):
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._barrier = barrier
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._orchestration_mode = orchestration_mode
        self._exp_logger = exp_logger
        self._sandbox_policy = sandbox_policy

        self._server = None

    async def start(self):
        """Start the coordinator server in a background thread."""
        from a2a.coordinator.server import create_server
        from sar_orch.tools.coordinator.query_sar_state import QuerySARStateTool
        import threading

        # Create the SAR state tool with the barrier instance (only available at runtime)
        sar_tool = QuerySARStateTool(barrier=self._barrier)

        # Router step_callback for logging subtask dispatches + coordinator token usage
        def _router_cb(event_type: str, **kw):
            if self._exp_logger is None:
                return
            step = getattr(self._barrier, "_step_counter", 0)
            if event_type == "llm_response":
                usage = kw.get("usage")
                if usage is not None:
                    self._exp_logger.log_token_usage(
                        step=step,
                        agent="Coordinator",
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif event_type == "tool_start":
                tool_name = kw.get("tool_name", "")
                args = kw.get("arguments", {})
                if tool_name == "dispatch_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=args.get("prompt", ""),
                        assigned_to=args.get("agent_id", ""),
                    )
                elif tool_name == "respond_worker":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=f"respond_worker(task_id={args.get('task_id', '')})",
                        assigned_to="Worker",
                    )
                elif tool_name == "query_sar_state":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="query_sar_state()",
                        assigned_to="Coordinator",
                    )
                elif tool_name == "query_task_events":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=f"query_task_events(task_ids={args.get('task_ids', [])})",
                        assigned_to="Coordinator",
                    )
                elif tool_name == "finish_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="finish_task()",
                        assigned_to="Coordinator",
                    )
            elif event_type == "tool_result":
                tool_name = kw.get("tool_name", "")
                if tool_name == "query_sar_state":
                    content = kw.get("content", "")
                    self._exp_logger.log_coordinator_state(
                        step=step,
                        state_summary=(content or "")[:2000],
                    )

        self._server = create_server(
            host=self._host,
            port=self._port,
            a2a_port=self._a2a_port,
            router_model=self._model,
            router_provider=self._provider,
            router_api_base=self._api_base,
            router_api_key_env=self._api_key_env,
            router_max_steps=200,
            router_temperature=0.7,
            prompts_dir=self._prompts_dir,
            # Do NOT pass tools_dir — the coord tool needs the barrier instance.
            # Instead, inject via extra_tools.
            extra_tools=[sar_tool],
            log_dir=self._log_dir,
            verifier_enabled=False,
            orchestration_mode=self._orchestration_mode,
            max_tasks_per_run=50,
            orchestration_timeout=1200,
            router_step_callback=_router_cb,
            context_config=ContextConfig(
                strategy="hybrid",
                recent_messages=12,
                pinned_enabled=True,
            ),
            token_limit=80000,
            require_explicit_completion=True,
            sandbox_policy=self._sandbox_policy,
        )
        # Inject barrier for real-time map visualization
        self._server.set_barrier(self._barrier)
        # Configure EventStore with coordinator log_dir for NDJSON persistence
        from a2a.coordinator.event_store import event_store

        if self._log_dir:
            event_store.set_log_dir(str(self._log_dir))
        logger.info(
            "SAR Coordinator starting on port %d (A2A port %d)",
            self._port,
            self._a2a_port,
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        await asyncio.sleep(2.0)

    async def submit_task(self, task_description: str) -> str:
        """Submit a task to the coordinator via the standard A2A SDK Client."""
        import httpx
        from a2a.client import create_client, ClientConfig
        from a2a.types.a2a_pb2 import (
            SendMessageRequest,
            Message,
            Part,
            Role,
        )
        from google.protobuf.json_format import MessageToDict

        endpoint = f"http://{self._host}:{self._a2a_port}/"
        config = ClientConfig(
            streaming=True,
            supported_protocol_bindings=[],
            httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(600.0)),
        )

        message = Message(
            role=Role.ROLE_USER,
            parts=[Part(text=task_description)],
        )
        request = SendMessageRequest(message=message)

        events = []
        try:
            client = await create_client(endpoint, config)
            async for stream_response in client.send_message(request):
                events.append(MessageToDict(stream_response))
            await client.close()
        except Exception as e:
            logger.error("Task submission failed: %s", e)
            return f"Error: {e}"

        return json.dumps(events, indent=2)

    def clear_sessions(self) -> None:
        """Clear the CoordinatorAgentExecutor session store."""
        if self._server is not None and hasattr(self._server, "executor"):
            try:
                self._server.executor.clear_sessions()
            except Exception as e:
                logger.warning("Failed to clear coordinator sessions: %s", e)

    async def stop(self):
        """Stop the coordinator."""
        if self._server is not None and hasattr(self._server, "shutdown"):
            await self._server.shutdown()
