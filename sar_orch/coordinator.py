from __future__ import annotations

import asyncio
import json
import logging

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
            if event_type == "llm_response" and self._exp_logger is not None:
                usage = kw.get("usage")
                if usage is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent="Coordinator",
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif (
                event_type == "tool_start"
                and kw.get("tool_name") == "dispatch_task"
                and self._exp_logger is not None
            ):
                args = kw.get("arguments", {})
                self._exp_logger.log_router_interaction(
                    step=getattr(self._barrier, "_step_counter", 0),
                    subtask=args.get("prompt", ""),
                    assigned_to=args.get("agent_id", ""),
                )

        self._server = create_server(
            host=self._host,
            port=self._port,
            a2a_port=self._a2a_port,
            router_model=self._model,
            router_provider=self._provider,
            router_api_base=self._api_base,
            router_api_key_env=self._api_key_env,
            router_max_steps=20,
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
        )
        # Inject barrier for real-time map visualization
        self._server.set_barrier(self._barrier)
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
        """Submit a task to the coordinator via the internal A2A endpoint."""
        import httpx

        a2a_url = f"http://{self._host}:{self._a2a_port}/"
        # Wait for server to be ready
        await asyncio.sleep(1.0)

        async with httpx.AsyncClient() as client:
            # First get agent card
            try:
                resp = await client.get(
                    f"{a2a_url}.well-known/agent-card.json", timeout=10.0
                )
                logger.info("Coordinator agent card: %s", resp.status_code)
            except Exception as e:
                logger.warning("Could not fetch agent card: %s", e)

            # Submit task via A2A SendMessage
            payload = {
                "jsonrpc": "2.0",
                "id": "task-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "role": 1,
                        "parts": [{"text": task_description}],
                    }
                },
            }
            headers = {
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
            }
            try:
                resp = await client.post(
                    f"{a2a_url}api/v1/jsonrpc/",
                    json=payload,
                    headers=headers,
                    timeout=600.0,
                )
                result = resp.json()
                logger.info("Task submission result status: %s", resp.status_code)
                return json.dumps(result, indent=2)
            except Exception as e:
                logger.error("Task submission failed: %s", e)
                return f"Error: {e}"

    async def stop(self):
        """Stop the coordinator."""
        if self._server is not None and hasattr(self._server, "shutdown"):
            await self._server.shutdown()
