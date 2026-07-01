from __future__ import annotations
import asyncio
import json
import logging
import os
from pathlib import Path
import threading

from a2a.shared.env_loader import load_env_file

logger = logging.getLogger(__name__)


class SARWorker:
    """SAR Worker — wraps an A2A Worker server with SAR-specific tools for one agent."""

    def __init__(
        self,
        worker_id: str,
        agent_name: str,
        agent_idx: int,
        barrier,  # SARBarrier instance
        a2a_host: str = "0.0.0.0",
        a2a_port: int = 8191,
        coordinator_url: str = "ws://localhost:8080",
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        exp_logger=None,  # ExperimentLogger for agent_interactions.csv
    ):
        self.worker_id = worker_id  # e.g., "Alice", "Bob" — matches agent name used by coordinator
        self.agent_name = agent_name
        self.agent_idx = agent_idx
        self._barrier = barrier
        self._a2a_host = a2a_host
        self._a2a_port = a2a_port
        self._coordinator_url = coordinator_url
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._exp_logger = exp_logger

        self._server = None
        self._client = None
        self._server_task = None
        self._stop_event = threading.Event()

        # Step callback correlation state
        self._call_seq: int = 0
        self._pending_tool: dict | None = None
        self._last_llm_output: str = ""

    def start(self):
        """Start the A2A server (non-blocking, runs in background)."""
        from sar_orch.tools.worker import SAR_WORKER_TOOLS
        from a2a.worker.a2a_server import create_worker_a2a_server
        from a2a.worker.coordinator_client import CoordinatorWebSocketClient

        # Load .env file and set the API key environment variable
        # (same pattern as a2a/worker/cli.py)
        env_path = Path(__file__).parent.parent / ".env"
        env = load_env_file(str(env_path))
        if "api_key" in env:
            os.environ[self._api_key_env] = env["api_key"]

        # Create tool instances bound to this agent's barrier
        tools = [
            tool_cls(barrier=self._barrier, agent_idx=self.agent_idx)
            for tool_cls in SAR_WORKER_TOOLS
        ]

        cap_list = ["sar", "navigation", "rescue", "firefighting"]

        # Build action string from tool name and arguments
        def _build_action(tool_name: str, args: dict) -> str:
            name_map = {
                "navigate_to": "NavigateTo", "move": "Move", "explore": "Explore",
                "carry_person": "CarryPerson", "drop_off_person": "DropOffPerson",
                "get_supply": "GetSupply", "store_supply": "StoreSupply",
                "use_supply": "UseSupply", "clear_inventory": "ClearInventory",
                "no_op": "NoOp",
            }
            sar_name = name_map.get(tool_name, tool_name)
            if not args:
                return f"{sar_name}()"
            arg_parts = ", ".join(str(v) for v in args.values())
            return f"{sar_name}({arg_parts})"

        # step_callback for logging agent interactions + token usage
        # NOTE: must be sync — AgentAdapter._step_handler does NOT await external callbacks
        def _step_callback(type_: str, **data):
            if type_ == "llm_response":
                self._last_llm_output = data.get("content", "")
                usage = data.get("usage")
                if usage is not None and self._exp_logger is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif type_ == "tool_start":
                self._pending_tool = {
                    "tool_name": data.get("tool_name", ""),
                    "arguments": data.get("arguments", {}),
                }
            elif type_ == "tool_result" and self._pending_tool is not None:
                tool_name = self._pending_tool["tool_name"]
                args = self._pending_tool["arguments"]
                exp = self._exp_logger
                if exp is not None:
                    exp.log_agent_interaction(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        tool_name=tool_name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        action=_build_action(tool_name, args),
                        observation=data.get("content", ""),
                        llm_output=self._last_llm_output,
                    )
                self._pending_tool = None

        self._server = create_worker_a2a_server(
            worker_id=self.worker_id,
            host=self._a2a_host,
            port=self._a2a_port,
            capabilities=cap_list,
            model=self._model,
            provider=self._provider,
            api_base=self._api_base,
            api_key_env=self._api_key_env,
            extra_tools=tools,
            prompts_dir=Path(self._prompts_dir) if self._prompts_dir else None,
            log_dir=Path(self._log_dir) if self._log_dir else None,
            max_steps=50,
            temperature=0.7,
            step_callback=_step_callback,
            include_base_tools=False,
        )

        a2a_endpoint = f"http://{self._a2a_host}:{self._a2a_port}/"
        self._client = CoordinatorWebSocketClient(
            coordinator_url=self._coordinator_url,
            worker_id=self.worker_id,
            a2a_endpoint=a2a_endpoint,
        )

        async def run():
            self._server_task = asyncio.create_task(self._server.serve())
            await self._client.connect()
            try:
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                await self._client.disconnect()
                self._server_task.cancel()

        # Run in a background thread since start() is called from sync context
        self._thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the worker — signals shutdown, disconnects client, and cancels the server."""
        if self._server is not None:
            self._server.should_exit = True
        self._stop_event.set()
        self._thread.join(timeout=10)
