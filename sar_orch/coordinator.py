from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from Agent.router_agent.context import ContextConfig

from langchain_openai import ChatOpenAI

from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from sar_orch.map import SemanticMapStore
from sar_orch.map_agent import set_llm_client, set_token_sink
from a2a.coordinator.supervision_state_store import SupervisionStateStore
from sar_orch.tools.coordinator import QuerySARStateTool

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
        supervision_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
        sandbox_policy=None,
        state_mode: str = "semantic",
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        max_steps: int = 50,
        map_summary_path: str | Path | None = None,
        # Phase 2: authenticated Temporal shadow write
        memory_read_mode: str = "legacy",
        run_id: str | None = None,
    ):
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._barrier = barrier
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._orchestration_mode = orchestration_mode
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._supervision_dir = supervision_dir
        self._exp_logger = exp_logger
        self._sandbox_policy = sandbox_policy
        self._state_mode = state_mode
        self._max_steps = max_steps
        self._map_summary_path = Path(map_summary_path) if map_summary_path else None
        self._enable_peer_mail = enable_peer_mail
        self._coordinator_secret = coordinator_secret
        self._memory_read_mode = memory_read_mode
        self._run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        if enable_peer_mail:
            if coordinator_secret is None or len(coordinator_secret) < 16:
                raise ValueError(
                    f"coordinator_secret must be >= 16 bytes when enable_peer_mail=True, "
                    f"got {len(coordinator_secret) if coordinator_secret else 0}"
                )
        # Phase 2: secure memory modes fail closed without a protected secret.
        if self._memory_read_mode in ("shadow", "read_port"):
            if (
                coordinator_secret is None
                or not isinstance(coordinator_secret, bytes)
                or len(coordinator_secret) < 16
            ):
                from a2a.coordinator.memory.callback_auth import (
                    MemoryAuthNotConfiguredError,
                )

                raise MemoryAuthNotConfiguredError(
                    "memory_auth_not_configured: secure memory mode requires a "
                    "protected coordinator callback secret (>= 16 bytes)"
                )

        self._dispatch_seq = 0
        self._tool_seq = 0
        self._pending_router_tool: dict[str, dict] = {}
        self._server = None
        self._thread = None
        self._semantic_map = None
        self._team_registry = None
        self._sender = None

    def _extract_prior_objects(self, obj_type: str) -> list[dict]:
        """Extract reservoirs/deposits from SAR barrier environment."""
        env = self._barrier.env
        if obj_type == "reservoirs":
            return [
                {
                    "name": f"Reservoir_{i}",
                    "position": [r[0], r[1], r[2]],
                    "resource_type": "Water",
                }
                for i, r in enumerate(getattr(env, "reservoirs", []))
            ]
        if obj_type == "deposits":
            return [
                {
                    "name": f"Deposit_{i}",
                    "position": [d[0], d[1], d[2]],
                    "inventory": {},
                }
                for i, d in enumerate(getattr(env, "deposits", []))
            ]
        return []

    def _initial_step_budget(self) -> dict[str, int]:
        return {
            "current_step": 0,
            "max_steps": self._max_steps,
            "remaining": self._max_steps,
        }

    def _log_send_message(self, step: int, args: dict) -> None:
        """Log the underlying semantic event for a send_message tool call."""
        if self._exp_logger is None:
            return
        message_type = args.get("message_type", "unknown")
        content = args.get("content", "")
        who = args.get("who", "")
        related_task_id = args.get("related_task_id", "")
        if message_type == "assign_task":
            self._dispatch_seq += 1
            correlation_id = f"coordinator-dispatch-{self._dispatch_seq}"
            worker_task_id = f"dispatch-{self._dispatch_seq}"
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=content,
                assigned_to=who,
                correlation_id=correlation_id,
                worker_task_id=worker_task_id,
                event_type="assign_task",
            )
            self._exp_logger.log_subtask(
                subtask_id=worker_task_id,
                status="assigned",
                step=step,
                assigned_to=who,
                subtask=content,
            )
            self._exp_logger.log_event(
                "assign_task",
                step=step,
                agent="Coordinator",
                correlation_id=correlation_id,
                payload=args,
            )
        elif message_type == "reply_to_help":
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=f"reply_to_help(task_id={related_task_id})",
                assigned_to="Worker",
                event_type="reply_to_help",
            )
            self._exp_logger.log_event(
                "reply_to_help",
                step=step,
                agent="Coordinator",
                payload={
                    "related_task_id": related_task_id,
                    "response_preview": content[:200],
                },
            )
        elif message_type == "cancel_task":
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=f"cancel_task(task_id={related_task_id})",
                assigned_to="Worker",
                event_type="cancel_task",
            )
            self._exp_logger.log_event(
                "cancel_task",
                step=step,
                agent="Coordinator",
                payload={"related_task_id": related_task_id},
            )
        else:
            self._exp_logger.log_event(
                "send_message",
                step=step,
                agent="Coordinator",
                payload=args,
            )

    async def start(self):
        """Start the coordinator server in a background thread."""
        from a2a.coordinator.server import create_server
        import threading

        # Build semantic map store from barrier environment priors
        semantic_map = SemanticMapStore()
        semantic_map.set_jsonl_path(
            Path(self._log_dir) / "semantic_map.jsonl" if self._log_dir else None
        )
        semantic_map.init_priors(
            reservoirs=self._extract_prior_objects("reservoirs"),
            deposits=self._extract_prior_objects("deposits"),
            agents=[
                {"agent_id": name}
                for name in getattr(self._barrier.env, "agent_names", [])
            ],
            rules={"Chemical": "Sand", "Non-chemical": "Water"},
            step_budget=self._initial_step_budget(),
            task_objective="Extinguish all fires and rescue all persons",
        )

        # Load ground-truth object names from the barrier environment's checker
        try:
            gt_names = list(getattr(self._barrier.env, "checker", None).coverage or [])
            if gt_names:
                semantic_map.set_ground_truth(gt_names)
        except Exception:
            pass

        self._semantic_map = semantic_map

        from a2a.coordinator.event_store import event_store

        supervision_state_store = SupervisionStateStore(
            log_dir=str(Path(self._supervision_dir))
            if self._supervision_dir
            else (str(Path(self._log_dir)) if self._log_dir else None)
        )

        # Phase 6: construct MapSummarizer in semantic mode when dependencies exist
        map_summarizer = None
        if self._state_mode == "semantic":
            if self._map_summary_path is not None and self._exp_logger is not None:
                from sar_orch.map.summarizer import MapSummarizer

                def _map_summary_token_sink(**kwargs):
                    step = (
                        getattr(self._barrier, "_step_counter", 0)
                        if self._barrier is not None
                        else 0
                    )
                    self._exp_logger.log_token_usage(
                        step=step,
                        agent=kwargs.get("agent", "MapSummarizer"),
                        prompt_tokens=kwargs.get("prompt_tokens", 0),
                        completion_tokens=kwargs.get("completion_tokens", 0),
                        total_tokens=kwargs.get("total_tokens", 0),
                        cache_hit_tokens=kwargs.get("cache_hit_tokens", 0),
                        cache_miss_tokens=kwargs.get("cache_miss_tokens", 0),
                    )
                    self._exp_logger.flush_summary()

                map_summarizer = MapSummarizer(
                    summary_path=self._map_summary_path,
                    token_usage_sink=_map_summary_token_sink,
                )
            else:
                logger.warning(
                    "MapSummarizer disabled: %s %s",
                    "no map_summary_path" if self._map_summary_path is None else "",
                    "no exp_logger" if self._exp_logger is None else "",
                )

        state_provider = SARCoordinatorStateProvider(
            barrier=self._barrier,
            semantic_map=semantic_map,
            event_store=event_store,
            state_mode=self._state_mode,
            supervision_state_store=supervision_state_store,
            map_summarizer=map_summarizer,
            log_dir=str(Path(self._log_dir)) if self._log_dir else None,
        )
        self._state_provider = state_provider
        self._supervision_state_store = supervision_state_store

        # Phase 2: authenticated Temporal shadow write — MemoryConfig derived
        # from explicit run_id / log root, never inferred from a callback.
        memory_ingestor = None
        memory_config = None
        if self._memory_read_mode in ("shadow", "read_port"):
            if not self._log_dir:
                from a2a.coordinator.memory.callback_auth import (
                    MemoryAuthNotConfiguredError,
                )

                raise MemoryAuthNotConfiguredError(
                    "memory_auth_not_configured: shadow/read_port requires log_dir"
                )
            from a2a.coordinator.memory.contracts import MemoryConfig
            from a2a.coordinator.memory.ingestor import (
                MemoryIngestor,
                MemoryScopeFactory,
            )
            from a2a.coordinator.memory.store import MemoryStore

            memory_config = MemoryConfig(
                experiment_id=self._run_id,
                memory_root=Path(self._log_dir),
            ).validate()
            memory_store = MemoryStore(memory_config.db_path)
            memory_ingestor = MemoryIngestor(
                memory_store,
                MemoryScopeFactory(memory_config),
            )
            self._memory_store = memory_store

        extra_tools: list = []
        if self._state_mode == "oracle":
            extra_tools.append(QuerySARStateTool(self._barrier))

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
                        cache_hit_tokens=usage.cache_hit_tokens,
                        cache_miss_tokens=usage.cache_miss_tokens,
                    )
            elif event_type == "tool_start":
                tool_name = kw.get("tool_name", "")
                args = kw.get("arguments", {})
                if tool_name == "send_message":
                    self._log_send_message(step, args)
                elif tool_name == "query_sar_state":
                    self._tool_seq += 1
                    corr_id = f"coord-tool-{self._tool_seq}"
                    self._pending_router_tool[tool_name] = {
                        "correlation_id": corr_id,
                        "step": step,
                    }
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="query_sar_state()",
                        assigned_to="Coordinator",
                        correlation_id=corr_id,
                        event_type="query_sar_state",
                    )
                elif tool_name == "query_task_events":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=f"query_task_events(task_ids={args.get('task_ids', [])})",
                        assigned_to="Coordinator",
                        event_type="query_task_events",
                    )
                elif tool_name == "finish_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="finish_task()",
                        assigned_to="Coordinator",
                        event_type="finish_task",
                    )
            elif event_type == "tool_result":
                tool_name = kw.get("tool_name", "")
                if tool_name == "query_sar_state":
                    content = kw.get("content", "")
                    pending = self._pending_router_tool.pop(tool_name, {})
                    self._exp_logger.log_coordinator_state(
                        step=step,
                        state_summary=(content or "")[:2000],
                        correlation_id=pending.get("correlation_id", ""),
                    )

        # SAR UI static files live alongside the orchestration code (sar_orch/ui/).
        _sar_ui_dir = Path(__file__).parent / "ui"

        self._server = create_server(
            host=self._host,
            port=self._port,
            a2a_port=self._a2a_port,
            router_model=self._model,
            router_provider=self._provider,
            router_api_base=self._api_base,
            router_api_key_env=self._api_key_env,
            router_max_steps=200,
            orchestration_mode=self._orchestration_mode,
            router_temperature=0.7,
            prompts_dir=self._prompts_dir,
            skills_dir=str(
                Path(self._prompts_dir).parent.parent / "skills" / "coordinator"
            )
            if self._prompts_dir
            else None,
            # Do NOT pass tools_dir — the coord tool needs the barrier instance.
            # Instead, inject via extra_tools.
            extra_tools=extra_tools,
            log_dir=self._log_dir,
            verifier_enabled=False,
            max_tasks_per_run=50,
            orchestration_timeout=1200,
            router_step_callback=_router_cb,
            context_config=ContextConfig(
                strategy="hybrid",
                recent_messages=12,
                pinned_enabled=True,
                state_mode=self._state_mode,
            ),
            token_limit=80000,
            require_explicit_completion=True,
            sandbox_policy=self._sandbox_policy,
            state_provider=state_provider,
            supervision_state_store=supervision_state_store,
            coordinator_secret=self._coordinator_secret,
            ui_dir=str(_sar_ui_dir),
            memory_read_mode=self._memory_read_mode,
            callback_secret=self._coordinator_secret,
            memory_config=memory_config,
            memory_ingestor=memory_ingestor,
        )

        # Attach agent registry (created inside server) to state provider
        self._state_provider._agent_registry = getattr(
            self._server, "_agent_registry", None
        )

        # Phase 3: inject LLM client + token sink into Map Agent (for llm_query)
        _map_agent_llm = ChatOpenAI(
            model=self._model,
            openai_api_key=os.environ.get(self._api_key_env, ""),
            openai_api_base=self._api_base,
            temperature=0.0,
        )
        set_llm_client(_map_agent_llm)

        def _map_agent_token_sink(**kwargs):
            step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
            if self._exp_logger is not None:
                self._exp_logger.log_token_usage(
                    step=step,
                    agent=kwargs.get("agent", "MapAgent"),
                    prompt_tokens=kwargs.get("prompt_tokens", 0),
                    completion_tokens=kwargs.get("completion_tokens", 0),
                    total_tokens=kwargs.get("total_tokens", 0),
                    cache_hit_tokens=kwargs.get("cache_hit_tokens", 0),
                    cache_miss_tokens=kwargs.get("cache_miss_tokens", 0),
                )
                self._exp_logger.flush_summary()

        set_token_sink(_map_agent_token_sink)

        # Phase 4: build peer-mail tools AFTER create_server so real registries exist
        if self._enable_peer_mail and self._coordinator_secret is not None:
            from a2a.coordinator.team_registry import CoordinatorTeamRegistry
            from a2a.coordinator.sender_service import CoordinatorSenderService
            from a2a.builtin_tools.configure_team import (
                ConfigureTeamTool,
                DisbandTeamTool,
                SyncTeamTool,
            )
            from a2a.builtin_tools.send_mail import SendMailTool

            self._team_registry = CoordinatorTeamRegistry()
            self._sender = CoordinatorSenderService(
                coordinator_secret=self._coordinator_secret,
            )

            # Resolver — lazily resolves registries from server when tools execute
            def _agent_reg():
                return getattr(self._server, "_agent_registry", None)

            def _worker_reg():
                return getattr(self._server, "_registry", None)

            extra_tools_post = [
                ConfigureTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                    agent_registry=_agent_reg(),
                    worker_registry=_worker_reg(),
                ),
                DisbandTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                ),
                SyncTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                ),
                SendMailTool(
                    sender=self._sender,
                    worker_registry=_worker_reg(),
                    agent_registry=_agent_reg(),
                ),
            ]
            # Extend the router's extra_tools list
            if hasattr(self._server, "_router") and self._server._router is not None:
                self._server._router._extra_tools.extend(extra_tools_post)
            else:
                extra_tools.extend(extra_tools_post)
        # Load mode-specific system prompt if not oracle
        if self._state_mode == "semantic" and self._prompts_dir:
            semantic_prompt_path = Path(self._prompts_dir) / "system.semantic.md"
            if semantic_prompt_path.exists():
                self._server._router._system_prompt = semantic_prompt_path.read_text(
                    encoding="utf-8"
                )

        # Inject barrier for real-time map visualization and semantic map for observation ingestion
        self._server.set_barrier(self._barrier)
        self._server.set_semantic_map(semantic_map)
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

        endpoint = f"http://localhost:{self._a2a_port}/"
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
        client = None
        try:
            client = await create_client(endpoint, config)
            async for stream_response in client.send_message(request):
                events.append(MessageToDict(stream_response))
        except Exception as e:
            logger.error("Task submission failed: %s", e)
            # Preserve the historical direct-call return shape for a failure
            # before an A2A client exists.  run_experiment treats this marker
            # as a framework error; once an A2A task exists, failures remain
            # exceptions/status-failed and never become normal final text.
            if client is None:
                return f"Error: {e}"
            raise RuntimeError(f"A2A task submission failed: {e}") from e
        finally:
            if client is not None:
                await client.close()

        for event in events:
            task = event.get("task", {})
            status_update = event.get("statusUpdate", {})
            status = task.get("status", {}) or status_update.get("status", {})
            state = status.get("state")
            if state in {
                "TASK_STATE_FAILED",
                "TASK_STATE_REJECTED",
                "TASK_STATE_CANCELED",
            }:
                message = (status.get("message", {}) or {}).get("parts", [])
                detail = " ".join(
                    part.get("text", "") for part in message if part.get("text")
                )
                raise RuntimeError(
                    f"Coordinator A2A task failed ({state}): {detail or 'no detail'}"
                )

        return json.dumps(events, indent=2)

    def clear_sessions(self) -> None:
        """Clear the CoordinatorAgentExecutor session store."""
        if self._server is not None and hasattr(self._server, "executor"):
            try:
                self._server.executor.clear_sessions()
            except Exception as e:
                logger.warning("Failed to clear coordinator sessions: %s", e)

    async def stop(self):
        """Stop the coordinator and wait for its Uvicorn thread to exit."""
        if self._server is not None and hasattr(self._server, "shutdown"):
            await self._server.shutdown()
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 11)
            if self._thread.is_alive():
                logger.warning("Coordinator server did not stop within 11 seconds")
