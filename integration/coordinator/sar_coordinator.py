"""
SAR Coordinator — MARoS RouterAgent adapted for Search & Rescue task coordination.

SAR 协调器 — 将 MARoS 的 RouterAgent 适配为搜救（Search & Rescue）任务协调器。
负责接收救援任务描述、拆分为子任务分配给各机器人、监控完成进度并动态重分配。
"""
from __future__ import annotations

import asyncio
import json
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

from openharness_a2a.coordinator.router_agent import RouterAgent

# ── System prompt (template with {agents_text} for RouterAgent formatting) ──
# ── 系统提示词（模板中包含 {agents_text}，供 RouterAgent 格式化注入智能体信息）──

# SAR 协调器系统提示词：告诉 LLM（RouterAgent）如何扮演搜救任务协调员。
# 内容包括：任务拆解规则、可用机器人与能力、协调方式、任务优先级和环境知识。
# {agents_text} 占位符会在运行时被替换为当前注册的智能体列表。
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


class SARRouterAgent(RouterAgent):
    """
    SAR 专用 RouterAgent — 使用搜救系统提示词替代默认模板。
    RouterAgent with SAR-specific system prompt.

    Sets ``self._sar_prompt`` during construction; ``_build_system_prompt()``
    uses it instead of the default restaurant-food-court template.

    Defined at module level so it can be imported without triggering the
    CoordinatorServer import (which pulls in rclpy).
    """

    def __init__(self, sar_system_prompt: str, *args: Any, **kwargs: Any):
        self._sar_prompt = sar_system_prompt
        super().__init__(*args, **kwargs)

    def _build_system_prompt(self, user_request: str) -> str:
        """
        构建 SAR 系统提示词 — 注入注册中心中的智能体信息与历史记忆。
        SAR-specific system prompt with registry agent info.

        从 registry 获取所有已注册智能体的描述文本填充 {agents_text} 占位符，
        并从 memory_client 中检索最多 3 条相似历史用户请求作为上下文注入。
        """
        agents_text = ""
        if self._registry is not None:
            agents_text = self._registry.get_all_agents_prompt_text()

        base = self._sar_prompt.format(agents_text=agents_text)

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


# ── SARCoordinator ──────────────────────────────────────────────────────────


class SARCoordinator:
    """
    启动带有 SAR 特定工具和配置的 MARoS 协调器。
    Launch MARoS Coordinator with SAR-specific tools and configuration.

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
        barrier,                     # SARBarrier — 栅栏对象，用于与实验场景通信
        agent_names: list[str],      # ["Alice", "Bob", ...] — 所有智能体的名称列表
        worker_ports: dict[str, int],# {"Alice": 8191, "Bob": 8192, ...} — 每个智能体 worker 的端口映射
        port: int = 8080,            # 协调器 HTTP 服务端口（默认 8080）
        model: str = "claude-opus-4-5",  # 使用的 LLM 模型（仅做记录，实际由 CoordinatorServer 配置）
    ):
        self._barrier = barrier
        self._agent_names = list(agent_names)
        self._worker_ports = dict(worker_ports)
        self._port = port
        self._model = model
        # NOTE: model is stored for reference; CoordinatorServer's LLM is
        # configured via config_path, so self._model is not wired into
        # RouterAgent (which has no model parameter).
        # 注意：model 仅作记录保留；CoordinatorServer 的 LLM 通过 config_path 配置，
        # 因此 self._model 不会注入 RouterAgent（RouterAgent 没有 model 参数）。

        # ── 构建智能体描述文本，供系统提示词使用 ──
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

        # ── 构建 SAR 专用路由工具列表（替换默认的 QueryMapTool）──
        # Build SAR-specific router tools
        from integration.coordinator.sar_router_tools import QuerySARStateTool

        self._sar_tools = [QuerySARStateTool(barrier)]

        # ── 延迟初始化的服务器任务句柄 ──
        # Deferred server handle
        self._server_task: Optional[asyncio.Task] = None

    async def start(self):
        """
        启动带有 SAR 配置的 Coordinator 服务器。
        Start the Coordinator server with SAR configuration.

        WARNING: This method accesses CoordinatorServer internal (_-prefixed)
        attributes.  Tested against MARoS commit: 722c4de.  If CoordinatorServer
        internals change, this method may need updates.

        Lazily imports my_a2a's CoordinatorServer (which brings in rclpy).
        Creates a custom SARRouterAgent with the SAR system prompt and
        QuerySARStateTool, injects it into CoordinatorAgentExecutor, and
        starts the FastAPI + WebSocket server.
        """
        # ── Lazy imports (defer rclpy dependency) ────────────────────────
        # ── 懒加载导入（延迟 rclpy 依赖，避免在模块加载时引入 ROS 库）──
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
        # ── 构建 SAR RouterAgent ─────────────────────────────────────────
        # Access the CoordinatorServer internals to share state
        # (we create a minimal CoordinatorServer to get registry/queue/etc.)
        # 访问 CoordinatorServer 内部状态以共享数据
        # （创建最小化的 CoordinatorServer 来获取 registry/queue 等组件）
        self._coord_server = CoordinatorServer(
            host="0.0.0.0",
            port=self._port,
            a2a_port=self._port + 1,
        )

        # Build the SARRouterAgent and register all tools
        # 创建 SARRouterAgent 并注册所有工具
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
        # SAR 特定：用 QuerySARStateTool 替换默认的 QueryMapTool
        for tool in self._sar_tools:
            router.register_tool(tool)
        router.register_tool(SessionNoteTool())
        router.register_tool(BashTool())

        # ── Build the FastAPI app with SAR-configured executor ──────────
        # ── 构建带有 SAR 配置执行器的 FastAPI 应用 ─────────────────────
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
            # 启动阶段：启动清理任务、创建 A2A 服务器并作为后台任务运行
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
            # 关闭阶段：取消 A2A 服务器任务并停止清理任务
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
            """
            Worker WebSocket 路由 — 接受 worker 连接、接收实时消息。
            每个 worker 通过唯一 worker_id 建立长连接，协调器据此下发任务和接收状态更新。
            """
            await websocket.accept()
            # 在共享字典中注册该 worker 的 WebSocket 连接
            async with cs._worker_ws_lock:
                cs._worker_ws[worker_id] = websocket
            try:
                while True:
                    data = await websocket.receive_text()
                    await cs._handle_worker_message(worker_id, json.loads(data))
            except WebSocketDisconnect:
                # 断开连接时清理注册信息
                async with cs._worker_ws_lock:
                    cs._worker_ws.pop(worker_id, None)

        # ── Start uvicorn in background task ────────────────────────────
        # ── 以后台任务启动 uvicorn 服务器 ───────────────────────────────
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
        """
        优雅关闭协调器服务。
        Graceful shutdown.

        设置 uvicorn 退出标志、取消后台服务器任务，确保资源正确释放。
        """
        if hasattr(self, "_uvicorn_server") and self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("SAR Coordinator stopped")
