"""SAR Coordinator — MARoS RouterAgent adapted for Search & Rescue task coordination."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

# ── Import path setup ───────────────────────────────────────────────────────
_llamar_root = Path(__file__).resolve().parent.parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_my_a2a = Path("/home/wyh/daily_work/MARoS/my_a2a/src")
if str(_maros_my_a2a) not in sys.path:
    sys.path.insert(0, str(_maros_my_a2a))

logger = logging.getLogger(__name__)

# ── System prompt (template with {agents_text} for RouterAgent formatting) ──

SAR_COORDINATOR_SYSTEM_PROMPT = """You are a Search & Rescue task coordinator. Your job is to:

1. Receive rescue mission descriptions from the user
2. Break them into subtasks assigned to specific rescue robots
3. Monitor subtask completion and dynamically reassign as the situation changes

## Available Robots and Their Capabilities
{agents_text}

## How to Coordinate
- Use `query_sar_state` to see the current environment: fires, persons, resources, agent positions
- Use `push_task` to assign a subtask to a specific robot
- Use `wait_for_result` to wait until a robot completes its subtask
- Use `cancel_task` if a robot's task is no longer relevant
- Use `record_note` to record important coordination decisions

## Task Priority
1. **Life safety first**: Rescue trapped persons before fighting fires
2. **Containment**: Prevent fires from spreading (extinguish medium+ intensity fires)
3. **Efficiency**: Have some robots collect supplies while others fight fires

## Environment Knowledge
- Fires spread when intensity reaches MEDIUM or higher
- Chemical fires can ONLY be extinguished with sand
- Non-chemical fires can be extinguished with water OR sand
- Persons need at least 2 robots to carry simultaneously
- Each robot can carry up to 3 supply items
- Reservoirs provide UNLIMITED water or sand
"""


# ── SARRouterAgent ──────────────────────────────────────────────────────────


class SARRouterAgent:
    """RouterAgent subclass with SAR-specific system prompt.

    Overrides _build_system_prompt() to use SAR_COORDINATOR_SYSTEM_PROMPT
    instead of the default restaurant-food-court prompt.  All other ReAct
    loop behaviour (tool registration, tool execution, token management)
    is inherited unchanged.

    Defined at module level so it can be imported without triggering the
    CoordinatorServer import (which pulls in rclpy).
    """

    def __new__(cls, sar_system_prompt: str, *args: Any, **kwargs: Any):
        """Lazy subclass: only imports RouterAgent when an instance is created."""
        from openharness_a2a.coordinator.router_agent import (
            RouterAgent,
            ROUTER_SYSTEM_PROMPT,
        )

        # Build a dynamic subclass so the override can close over sar_system_prompt
        original_build = RouterAgent._build_system_prompt

        class _SARRouterAgent(RouterAgent):
            def _build_system_prompt(self, user_request: str) -> str:
                """SAR-specific system prompt with registry agent info."""
                agents_text = ""
                if self._registry is not None:
                    agents_text = self._registry.get_all_agents_prompt_text()

                base = sar_system_prompt.format(agents_text=agents_text)

                # Passive memory injection (same logic as parent)
                if user_request and self._memory_client is not None:
                    try:
                        past = self._memory_client.search_events_sync(
                            query=user_request, top_k=3, event_type="user_request"
                        )
                        if past:
                            base += "\n\n## Past similar user requests:\n"
                            for i, ev in enumerate(past, 1):
                                payload = ev.get("payload", {})
                                base += (
                                    f"{i}. User: {payload.get('user_request', '')[:200]}\n"
                                    f"   Routed to: {payload.get('subtasks', '')}\n"
                                    f"   Reasoning: {payload.get('reasoning', '')[:200]}\n\n"
                                )
                    except Exception as e:
                        logger.warning(f"Memory injection failed: {e}")

                return base

        return _SARRouterAgent(*args, **kwargs)


# ── SARCoordinator ──────────────────────────────────────────────────────────


class SARCoordinator:
    """Launch MARoS Coordinator with SAR-specific tools and configuration.

    Wraps my_a2a's CoordinatorServer (lazy import to avoid rclpy at module
    level).  Injects:
      - SAR_COORDINATOR_SYSTEM_PROMPT as the RouterAgent system prompt
      - QuerySARStateTool replacing the default QueryMapTool

    Usage:
        coordinator = SARCoordinator(
            barrier=sar_barrier,
            agent_names=["Alice", "Bob"],
            worker_ports={"Alice": 8191, "Bob": 8192},
        )
        await coordinator.start()
        ...
        await coordinator.stop()
    """

    def __init__(
        self,
        barrier,                     # SARBarrier
        agent_names: list[str],      # ["Alice", "Bob", ...]
        worker_ports: dict[str, int],# {"Alice": 8191, "Bob": 8192, ...}
        port: int = 8080,
        model: str = "claude-opus-4-5",
    ):
        self._barrier = barrier
        self._agent_names = list(agent_names)
        self._worker_ports = dict(worker_ports)
        self._port = port
        self._model = model

        # Build agent descriptions for the system prompt
        agent_descs = []
        for name in self._agent_names:
            wp = self._worker_ports.get(name, 8190)
            agent_descs.append(
                f"- **{name}**: SAR rescue robot. A2A endpoint at "
                f"http://localhost:{wp}/. Can navigate, collect supplies, "
                f"extinguish fires, carry persons, explore."
            )
        self._system_prompt = SAR_COORDINATOR_SYSTEM_PROMPT.format(
            agents_text="\n".join(agent_descs),
        )

        # Build SAR-specific router tools
        from integration.coordinator.sar_router_tools import QuerySARStateTool

        self._sar_tools = [QuerySARStateTool(barrier)]

        # Deferred server handle
        self._server: Optional[Any] = None
        self._server_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the Coordinator server with SAR configuration.

        Lazily imports my_a2a's CoordinatorServer (which brings in rclpy).
        Creates a custom SARRouterAgent with the SAR system prompt and
        QuerySARStateTool, injects it into CoordinatorAgentExecutor, and
        starts the FastAPI + WebSocket server.
        """
        # ── Lazy imports (defer rclpy dependency) ────────────────────────
        import uvicorn

        from openharness_a2a.coordinator.server import CoordinatorServer
        from openharness_a2a.coordinator.agent_executor import (
            CoordinatorAgentExecutor,
        )
        from openharness_a2a.coordinator.router_tools import (
            BashTool,
            CancelTaskTool,
            PushTaskTool,
            QueryMemoryTool,
            SessionNoteTool,
            WaitForResultTool,
        )

        # ── Build SAR RouterAgent ────────────────────────────────────────
        # Access the CoordinatorServer internals to share state
        # (we create a minimal CoordinatorServer to get registry/queue/etc.)
        self._coord_server = CoordinatorServer(
            host="0.0.0.0",
            port=self._port,
            a2a_port=self._port + 1,
        )

        # Build the SARRouterAgent and register all tools
        pending_tasks: dict = {}
        router = SARRouterAgent(
            sar_system_prompt=self._system_prompt,
            llm_client=self._coord_server._llm_client,
            registry=self._coord_server._agent_registry,
            memory_client=self._coord_server._memory_client,
        )
        router.register_tool(
            PushTaskTool(
                registry=self._coord_server._agent_registry,
                pending_tasks=pending_tasks,
            )
        )
        router.register_tool(WaitForResultTool(pending_tasks=pending_tasks))
        router.register_tool(
            CancelTaskTool(
                task_queue=self._coord_server._task_queue,
                pending_tasks=pending_tasks,
            )
        )
        router.register_tool(
            QueryMemoryTool(memory_client=self._coord_server._memory_client)
        )
        # SAR-specific: replace QueryMapTool with QuerySARStateTool
        for tool in self._sar_tools:
            router.register_tool(tool)
        router.register_tool(SessionNoteTool())
        router.register_tool(BashTool())

        # ── Build the FastAPI app with SAR-configured executor ──────────
        from contextlib import asynccontextmanager

        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from openharness_a2a.coordinator.a2a_server import (
            create_coordinator_a2a_server,
        )
        from openharness_a2a.coordinator.routes import health, workers

        cs = self._coord_server  # shorthand

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup
            await cs._start_cleanup_task()
            a2a_srv = create_coordinator_a2a_server(
                host="0.0.0.0",
                port=cs._a2a_port,
                agent_registry=cs._agent_registry,
                task_queue=cs._task_queue,
                executor=CoordinatorAgentExecutor(
                    registry=cs._agent_registry,
                    task_queue=cs._task_queue,
                    memory_client=cs._memory_client,
                    task_logger=cs._task_logger,
                    llm_client=cs._llm_client,
                    router=router,  # <-- SAR-specific router injected here
                ),
            )
            cs._server_task = asyncio.create_task(a2a_srv.serve())
            yield
            # Shutdown
            if cs._server_task:
                cs._server_task.cancel()
                try:
                    await cs._server_task
                except asyncio.CancelledError:
                    pass
            await cs._stop_cleanup_task()

        app = FastAPI(title="SAR Coordinator", lifespan=lifespan)
        health.register_routes(app, cs)
        workers.register_routes(app, cs)

        @app.websocket("/ws/worker/{worker_id}")
        async def worker_ws(websocket: WebSocket, worker_id: str):
            await websocket.accept()
            async with cs._worker_ws_lock:
                cs._worker_ws[worker_id] = websocket
            try:
                while True:
                    import json

                    data = await websocket.receive_text()
                    await cs._handle_worker_message(worker_id, json.loads(data))
            except WebSocketDisconnect:
                async with cs._worker_ws_lock:
                    cs._worker_ws.pop(worker_id, None)

        # ── Start uvicorn in background task ────────────────────────────
        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="info")
        self._uvicorn_server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._uvicorn_server.serve())

        logger.info(
            "SAR Coordinator starting on port %d with agents: %s",
            self._port,
            ", ".join(self._agent_names),
        )

        # Give uvicorn a moment to bind the port
        await asyncio.sleep(0.5)

    async def stop(self):
        """Graceful shutdown."""
        if hasattr(self, "_uvicorn_server") and self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("SAR Coordinator stopped")
