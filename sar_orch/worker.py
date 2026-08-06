from __future__ import annotations
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from a2a.shared.env_loader import load_env_file
from a2a.shared.server_lifecycle import shutdown_uvicorn_server
from Agent.worker_agent.tools.mcp_loader import cleanup_mcp_connections

from Agent.worker_agent.context import ContextConfig

logger = logging.getLogger(__name__)

MIN_COORDINATOR_SECRET_LENGTH = 16


class ConfigurationError(Exception):
    """Raised when SARWorker configuration is invalid."""


class SARWorker:
    """SAR Worker -- wraps an A2A Worker server with SAR-specific tools for one agent."""

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
        sandbox_policy=None,  # SandboxPolicy for workspace sandboxing
        # Phase 2/3: peer mail
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        # Phase 2: secure callback signing mode
        memory_read_mode: str = "legacy",
    ):
        self.worker_id = worker_id
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
        self._sandbox_policy = sandbox_policy
        self._enable_peer_mail = enable_peer_mail
        self._coordinator_secret = coordinator_secret
        self._memory_read_mode = memory_read_mode

        # Validate immediately: log_dir always, secret only if explicitly supplied
        if self._enable_peer_mail:
            self._validate_mail_config()
        if self._memory_read_mode in ("shadow", "read_port"):
            if (
                coordinator_secret is None
                or not isinstance(coordinator_secret, bytes)
                or len(coordinator_secret) < MIN_COORDINATOR_SECRET_LENGTH
            ):
                from a2a.coordinator.memory.callback_auth import (
                    MemoryAuthNotConfiguredError,
                )

                raise MemoryAuthNotConfiguredError(
                    "memory_auth_not_configured: secure memory mode requires a "
                    "protected coordinator callback secret (>= 16 bytes)"
                )

        self._server = None
        self._client = None
        self._server_task = None
        self._stop_event = threading.Event()

        self._call_seq: int = 0
        self._pending_tool: dict | None = None
        self._last_llm_output: str = ""
        self._last_llm_input: str = ""

        # Phase 2/3: stores for envelope-aware adapter (created in start())
        self._mailbox_store = None
        self._team_state_store = None
        self._ingress = None

        # Phase 5: Peer mail sender service (created in start())
        self._peer_sender = None
        # MCP connections are owned by this worker's private asyncio.run loop.
        self._mcp_registry = []

    def _validate_mail_config(self) -> None:
        """Fail-closed validation when enable_peer_mail=True.

        Validates immediately at construction:
        - log_dir must be set
        - coordinator_secret, if explicitly provided, must be bytes >= 16

        It is valid for coordinator_secret to be None at construction if
        start() will resolve it from .env or A2A_COORDINATOR_SECRET.
        """
        if self._log_dir is None:
            raise ConfigurationError(
                "enable_peer_mail=True requires log_dir for persistent "
                "mailbox and team state storage"
            )
        if self._coordinator_secret is not None:
            if not isinstance(self._coordinator_secret, bytes):
                raise ConfigurationError(
                    "coordinator_secret must be bytes, "
                    f"got {type(self._coordinator_secret).__name__}"
                )
            if len(self._coordinator_secret) < MIN_COORDINATOR_SECRET_LENGTH:
                raise ConfigurationError(
                    f"coordinator_secret must be at least "
                    f"{MIN_COORDINATOR_SECRET_LENGTH} bytes, "
                    f"got {len(self._coordinator_secret)}"
                )

    def _init_peer_mail_stores(self, *, secret: bytes) -> None:
        """Create mailbox, team_state, and ingress stores.

        Called from start() after configuration is fully resolved.
        May be called in tests with explicit parameters to inspect
        created stores.
        """
        from a2a.worker.mailbox_store import WorkerMailboxStore
        from a2a.worker.team_state import WorkerTeamState
        from a2a.worker.ingress import EnvelopeIngress

        agent_log_dir = Path(self._log_dir)
        mailbox_path = agent_log_dir / "mailbox.ndjson"
        team_state_path = agent_log_dir / "team_state.json"

        self._mailbox_store = WorkerMailboxStore(
            path=mailbox_path,
            local_worker_id=self.agent_name,
        )
        self._team_state_store = WorkerTeamState(
            path=team_state_path,
            local_worker_id=self.agent_name,
            coordinator_id="Coordinator",
        )
        self._ingress = EnvelopeIngress(
            coordinator_secret=secret,
            coordinator_id="Coordinator",
            local_worker_id=self.agent_name,
            team_state=self._team_state_store,
            allow_legacy_tasks=False,
        )

        # Phase 5: Peer sender service
        from a2a.worker.peer_sender import WorkerPeerSenderService

        self._peer_sender = WorkerPeerSenderService(
            team_state=self._team_state_store,
            local_worker_id=self.agent_name,
        )

        logger.info(
            "Peer mail enabled for %s (mailbox=%s, team=%s, sender=%s)",
            self.agent_name,
            mailbox_path,
            team_state_path,
            self.agent_name,
        )

    async def _assemble_tools_async(self, http_url: str, mailbox_store=None) -> list:
        """Assemble all worker tools including MCP-loaded Map Agent tools."""
        from sar_orch.tools.worker import SAR_WORKER_TOOLS
        from sar_orch.worker_mcp_config import write_worker_mcp_config
        from Agent.worker_agent.tools.mcp_loader import load_mcp_tools_async

        tools = []
        for tool_cls in SAR_WORKER_TOOLS:
            if tool_cls.__name__ == "ReportObservationTool":
                tools.append(
                    tool_cls(
                        agent_name=self.agent_name,
                        task_id=getattr(self, "_current_a2a_task_id", ""),
                        get_step=lambda: getattr(self._barrier, "_step_counter", 0),
                    )
                )
            elif tool_cls.__name__ in ("FinishTaskTool", "AskCoordinatorTool"):
                tools.append(tool_cls())
            elif tool_cls.__name__ == "ReadMailboxTool":
                if mailbox_store is not None:
                    tools.append(tool_cls(mailbox=mailbox_store))
                else:
                    logger.debug("ReadMailboxTool not created - peer mail disabled")
            elif tool_cls.__name__ == "A2ASendMailTool":
                if self._peer_sender is not None:
                    tools.append(tool_cls(sender=self._peer_sender))
                else:
                    logger.info("A2ASendMailTool not created -- peer mail disabled")
            else:
                tools.append(tool_cls(barrier=self._barrier, agent_idx=self.agent_idx))

        # Load Map Agent MCP tools
        mcp_log_dir = self._log_dir or str(Path.cwd() / "logs")
        mcp_config_path = write_worker_mcp_config(
            log_dir=mcp_log_dir,
            agent_name=self.agent_name,
            coordinator_http_url=http_url,
        )
        try:
            mcp_tools = await load_mcp_tools_async(
                str(mcp_config_path), connection_registry=self._mcp_registry
            )
            tools.extend(mcp_tools)
            logger.info(
                "Loaded %d MCP tools from map_agent: %s",
                len(mcp_tools),
                [t.name for t in mcp_tools],
            )
        except Exception:
            logger.warning(
                "Failed to load MCP tools from map_agent "
                "(will continue without MCP tools)",
                exc_info=True,
            )

        return tools

    async def _shutdown_run_resources(self) -> None:
        """Drain worker resources in dependency order on the owning loop."""
        try:
            if self._client is not None:
                await self._client.disconnect()
        finally:
            try:
                if self._peer_sender is not None:
                    try:
                        await self._peer_sender.close()
                    except Exception:
                        logger.debug("Error closing peer sender", exc_info=True)
            finally:
                try:
                    # The worker A2A lifespan drains ActiveTasks before this
                    # await returns, while its loop and the map server remain live.
                    await shutdown_uvicorn_server(self._server, self._server_task)
                finally:
                    # This must run on the same loop/task that loaded MCP.
                    await cleanup_mcp_connections(self._mcp_registry)

    def start(self):
        """Start the A2A server (non-blocking, runs in background).

        Raises ConfigurationError on invalid config, synchronously before
        any server or thread creation.
        """
        from sar_orch.worker_state_provider import SARWorkerStateProvider
        from a2a.worker.a2a_server import create_worker_a2a_server
        from a2a.worker.coordinator_client import CoordinatorWebSocketClient

        env_path = Path(__file__).parent.parent / ".env"
        env = load_env_file(str(env_path))

        # Resolve coordinator secret before any side effects
        coord_secret: bytes | None = self._coordinator_secret
        if self._enable_peer_mail:
            if coord_secret is None:
                raw = env.get("coordinator_secret") or os.environ.get(
                    "A2A_COORDINATOR_SECRET"
                )
                if raw:
                    coord_secret = raw.encode("utf-8") if isinstance(raw, str) else raw
            if coord_secret is None:
                raise ConfigurationError(
                    "enable_peer_mail=True but no coordinator_secret found "
                    "(provide via constructor arg, .env coordinator_secret, "
                    "or A2A_COORDINATOR_SECRET env var)"
                )
            if not isinstance(coord_secret, bytes):
                raise ConfigurationError(
                    "coordinator_secret must be bytes, "
                    f"got {type(coord_secret).__name__}"
                )
            if len(coord_secret) < MIN_COORDINATOR_SECRET_LENGTH:
                raise ConfigurationError(
                    f"coordinator_secret must be at least "
                    f"{MIN_COORDINATOR_SECRET_LENGTH} bytes "
                    f"(got {len(coord_secret)})"
                )

        if "api_key" in env:
            os.environ[self._api_key_env] = env["api_key"]

        # Phase 2: build the callback signer from protected local config only.
        # The secret is never sent through prompts, A2A messages, context or logs.
        callback_signer = None
        if coord_secret is not None:
            from a2a.worker.callback_sender import CallbackSigner

            callback_signer = CallbackSigner(self.agent_name, coord_secret)

        # Create Phase 2/3 stores
        mailbox_store = None
        team_state_store = None
        ingress = None

        if self._enable_peer_mail and coord_secret is not None:
            self._init_peer_mail_stores(secret=coord_secret)
            mailbox_store = self._mailbox_store
            team_state_store = self._team_state_store
            ingress = self._ingress

        # Derive coordinator_id for state provider
        coordinator_id_for_summary = "Coordinator"
        if self._team_state_store is not None:
            ts = self._team_state_store.current()
            if ts is not None:
                coordinator_id_for_summary = ts.coordinator_id

        # Create worker state provider for automatic context injection
        http_url = re.sub(r"^ws://", "http://", self._coordinator_url.rstrip("/"))
        state_provider = SARWorkerStateProvider(
            barrier=self._barrier,
            agent_idx=self.agent_idx,
            semantic_map_url=http_url,
            mailbox=mailbox_store,
            team_state=team_state_store,
            coordinator_id=coordinator_id_for_summary,
        )
        # Phase 4: inject agent name for team status fetching
        state_provider._agent_name = self.agent_name

        # Set up observation publisher
        from sar_orch.map import WorkerReportPublisher
        from sar_orch.tools.worker._barrier_helpers import set_publisher

        _publisher_inst = WorkerReportPublisher(
            agent_name=self.agent_name,
            step_provider=lambda: getattr(self._barrier, "_step_counter", 0),
        )
        set_publisher(_publisher_inst)

        cap_list = ["sar", "navigation", "rescue", "firefighting"]

        def _build_action(tool_name: str, args: dict) -> str:
            name_map = {
                "navigate_to": "NavigateTo",
                "move": "Move",
                "explore": "Explore",
                "carry_person": "CarryPerson",
                "drop_off_person": "DropOffPerson",
                "get_supply": "GetSupply",
                "store_supply": "StoreSupply",
                "use_supply": "UseSupply",
                "clear_inventory": "ClearInventory",
                "no_op": "NoOp",
            }
            sar_name = name_map.get(tool_name, tool_name)
            if not args:
                return f"{sar_name}()"
            arg_parts = ", ".join(str(v) for v in args.values())
            return f"{sar_name}({arg_parts})"

        def _step_callback(type_: str, **data):
            if type_ == "llm_response":
                self._last_llm_output = data.get("content", "")
                msgs = data.get("input_messages")
                if msgs:
                    lines = []
                    for m in msgs[-6:]:
                        role = getattr(m, "role", "?")
                        c = getattr(m, "content", "")
                        c_str = c[:200] if isinstance(c, str) else str(c)[:200]
                        lines.append(f"{role}: {c_str}")
                    self._last_llm_input = "\n".join(lines)
                else:
                    self._last_llm_input = ""
                usage = data.get("usage")
                if usage is not None and self._exp_logger is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                        cache_hit_tokens=usage.cache_hit_tokens,
                        cache_miss_tokens=usage.cache_miss_tokens,
                    )
            elif type_ == "tool_start":
                self._call_seq += 1
                self._pending_tool = {
                    "tool_name": data.get("tool_name", ""),
                    "arguments": data.get("arguments", {}),
                    "started_at": time.monotonic(),
                    "correlation_id": f"{self.agent_name}-tool-{self._call_seq}",
                }
            elif type_ == "tool_result" and self._pending_tool is not None:
                tool_name = self._pending_tool["tool_name"]
                args = self._pending_tool["arguments"]
                exp = self._exp_logger
                if exp is not None:
                    tool_latency_ms = (
                        time.monotonic() - self._pending_tool["started_at"]
                    ) * 1000.0
                    exp.log_agent_interaction(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        tool_name=tool_name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        action=_build_action(tool_name, args),
                        observation=data.get("content", ""),
                        llm_input=self._last_llm_input,
                        llm_output=self._last_llm_output,
                        correlation_id=self._pending_tool["correlation_id"],
                        event_type="tool_result",
                        tool_latency_ms=tool_latency_ms,
                    )
                self._pending_tool = None

        async def run():
            try:
                # Tool assembly (async — includes MCP tool loading from Map Agent)
                tools = await self._assemble_tools_async(
                    http_url=http_url,
                    mailbox_store=mailbox_store,
                )

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
                    skills_dir=Path(self._prompts_dir).parent.parent
                    / "skills"
                    / "worker"
                    if self._prompts_dir
                    else None,
                    log_dir=Path(self._log_dir) if self._log_dir else None,
                    max_steps=100,
                    temperature=0.7,
                    step_callback=_step_callback,
                    include_base_tools=False,
                    context_config=ContextConfig(
                        strategy="hybrid",
                        recent_messages=12,
                        pinned_enabled=True,
                        state_mode="semantic",
                    ),
                    token_limit=80000,
                    require_explicit_completion=True,
                    sandbox_policy=self._sandbox_policy,
                    state_provider=state_provider,
                    envelope_ingress=ingress,
                    mailbox_store=mailbox_store,
                    team_state_store=team_state_store,
                    callback_signer=callback_signer,
                )

                a2a_endpoint = f"http://localhost:{self._a2a_port}/"
                self._client = CoordinatorWebSocketClient(
                    coordinator_url=self._coordinator_url,
                    worker_id=self.worker_id,
                    a2a_endpoint=a2a_endpoint,
                )

                self._server_task = asyncio.create_task(self._server.serve())
                await self._client.connect()
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                await self._shutdown_run_resources()

        self._thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        self._thread.start()

    def clear_sessions(self) -> None:
        """Clear the AgentAdapter session store for this worker."""
        if self._server is not None and hasattr(self._server, "executor"):
            try:
                self._server.executor.clear_sessions()
            except Exception as e:
                logger.warning("Failed to clear worker sessions: %s", e)

    def stop(self):
        """Stop the worker (closes sender inside the run() finally block)."""
        if self._server is not None:
            self._server.should_exit = True
        self._stop_event.set()
        self._thread.join(timeout=11)
        if self._thread.is_alive():
            logger.warning("Worker %s did not stop within 11 seconds", self.worker_id)
