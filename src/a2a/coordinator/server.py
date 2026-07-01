"""Coordinator 主服务 - FastAPI + WebSocket，集成 A2A Server。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.responses import FileResponse
from pydantic import BaseModel

from a2a.coordinator.a2a_server import create_coordinator_a2a_server
from a2a.coordinator.agent_registry import AgentRegistry, AgentInfo, AgentStatus
from a2a.coordinator.router import RouterAgent
from a2a.coordinator.worker_registry import WorkerRegistry
from a2a.coordinator.task_logger import TaskLogger
from a2a.coordinator.task_queue import (
    TaskQueue,
    TaskNotFoundError,
    InvalidStatusTransitionError,
)
from a2a.coordinator.mesh_guide import MeshGuide, AgentNotFoundError
from a2a.coordinator.routes import health, workers
from a2a.shared.types import (
    DistributedTask,
    TaskStatus,
    WS_REGISTER,
    WS_HEARTBEAT,
    WS_TASK_PROGRESS,
    WS_CANCEL_TASK,
    WS_RELAY_A2A,
)


logger = logging.getLogger(__name__)


class CreateTaskRequest(BaseModel):
    task_id: str | None = None
    prompt: str
    task_type: str = "agent"


class CoordinatorServer:
    """Coordinator 主服务。"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        config_path: str | None = None,
        # --- RouterAgent 注入参数 ---
        prompts_dir: str | None = None,
        tools_dir: str | None = None,
        extra_tools: list | None = None,
        skills_dir: str | None = None,
        log_dir: str | None = None,
        router_model: str = "claude-opus-4-5",
        router_max_steps: int = 15,
        router_temperature: float = 0.7,
        router_provider: str = "anthropic",
        router_api_base: str = "https://api.anthropic.com",
        router_api_key_env: str = "ANTHROPIC_API_KEY",
        # --- VerifierAgent 注入参数 ---
        verifier_model: str | None = None,  # None = 使用 router_model
        verifier_max_steps: int = 10,
        verifier_temperature: float = 0.3,
        verifier_enabled: bool = True,  # False = 跳过验证
        # --- Orchestration 参数 ---
        orchestration_mode: str = "agentic",
        max_tasks_per_run: int = 20,
        orchestration_timeout: int = 600,
        router_step_callback=None,
    ) -> None:
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._registry = WorkerRegistry()
        self._agent_registry = AgentRegistry(
            static_config_path=Path(config_path) if config_path else None
        )
        self._task_logger = TaskLogger()
        self._task_queue = TaskQueue(on_cleanup=self._cleanup_task_logs)
        self._mesh_guide = MeshGuide(self._agent_registry)
        self._worker_ws: Dict[str, WebSocket] = {}
        self._worker_ws_lock = asyncio.Lock()

        # 构建 RouterAgent（注入所有路径和配置参数）
        self._router = RouterAgent(
            registry=self._agent_registry,
            prompts_dir=Path(prompts_dir) if prompts_dir else None,
            custom_tools_dir=Path(tools_dir) if tools_dir else None,
            extra_tools=extra_tools,
            skills_dir=Path(skills_dir) if skills_dir else None,
            model=router_model,
            max_steps=router_max_steps,
            temperature=router_temperature,
            provider=router_provider,
            api_base=router_api_base,
            api_key_env=router_api_key_env,
            log_dir=Path(log_dir) if log_dir else None,
        )

        # 构建 VerifierAgent（可选）
        self._verifier = None
        if verifier_enabled:
            api_key = os.environ.get(router_api_key_env, "")
            if not api_key:
                logger.warning(
                    "Verifier disabled: API key not set (env var '%s'). "
                    "Set the key or pass --no-verifier to suppress this warning.",
                    router_api_key_env,
                )
            else:
                from a2a.coordinator.verifier import VerifierAgent

                v_model = verifier_model or router_model
                self._verifier = VerifierAgent(
                    registry=self._agent_registry,
                    model=v_model,
                    max_steps=verifier_max_steps,
                    temperature=verifier_temperature,
                    provider=router_provider,
                    api_base=router_api_base,
                    api_key_env=router_api_key_env,
                    log_dir=Path(log_dir) if log_dir else None,
                )

        self._orchestration_mode = orchestration_mode
        self._max_tasks_per_run = max_tasks_per_run
        self._orchestration_timeout = orchestration_timeout
        self._router_step_callback = router_step_callback

        self._barrier = None  # SARBarrier (optional, for map visualization)

        self._app = self._build_app()
        self._server_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    def set_barrier(self, barrier) -> None:
        """注入 SARBarrier 引用，供 /map/state SSE 端点使用。"""
        self._barrier = barrier

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    @property
    def task_queue(self) -> TaskQueue:
        return self._task_queue

    @property
    def mesh_guide(self) -> MeshGuide:
        return self._mesh_guide

    def _cleanup_task_logs(self, removed_ids: list[str]) -> None:
        """TaskQueue on_cleanup 回调：清理已完成任务的日志文件。"""
        for tid in removed_ids:
            self._task_logger.cleanup_task(tid)

    def _build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # 启动时
            await self._start_cleanup_task()
            a2a_srv = create_coordinator_a2a_server(
                host=self._host,
                port=self._a2a_port,
                agent_registry=self._agent_registry,
                task_queue=self._task_queue,
                router=self._router,  # 注入 RouterAgent
                verifier=self._verifier,  # 注入 VerifierAgent（可为 None）
                task_logger=self._task_logger,  # 注入 TaskLogger
                orchestration_mode=self._orchestration_mode,
                max_tasks_per_run=self._max_tasks_per_run,
                orchestration_timeout=self._orchestration_timeout,
                router_step_callback=self._router_step_callback,
            )
            self._server_task = asyncio.create_task(a2a_srv.serve())
            yield
            # 关闭时
            if self._server_task:
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass
            # 关闭 RouterAgent SDK clients
            await self._router.close()
            await self._stop_cleanup_task()

        app = FastAPI(title="OpenHarness A2A Coordinator", lifespan=lifespan)

        # 注册 REST API routes
        health.register_routes(app, self)
        workers.register_routes(app, self)

        @app.post("/tasks")
        async def create_task(request: CreateTaskRequest):
            task_id = request.task_id or uuid.uuid4().hex
            if not request.prompt:
                raise HTTPException(status_code=400, detail="prompt required")
            task = DistributedTask(
                task_id=task_id,
                task_type=request.task_type,
                prompt=request.prompt,
            )
            self._task_queue.enqueue(task)
            return {"task_id": task_id, "status": "pending"}

        @app.get("/tasks/{task_id}")
        async def get_task(task_id: str):
            try:
                task = self._task_queue.get(task_id)
                return {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "assigned_worker": task.assigned_worker,
                    "result": task.result,
                    "error": task.error,
                }
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail="Task not found")

        # ---- UI 和 A2A 代理 ----

        UI_DIR = Path(__file__).parent / "ui"

        @app.get("/ui")
        async def serve_ui():
            """提供任务控制台 HTML 界面。"""
            ui_file = UI_DIR / "task_ui.html"
            if not ui_file.exists():
                raise HTTPException(status_code=404, detail="UI file not found")
            return FileResponse(ui_file)

        # ---- TaskLogger HTTP API ----

        @app.get("/logs")
        async def list_logs():
            task_ids = self._task_logger.list_task_ids()
            return {"logs": task_ids}

        @app.get("/logs/{task_id}")
        async def get_log(task_id: str):
            try:
                path = self._task_logger.get_log_path(task_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid task_id")

            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="log not found")

            return FileResponse(path, media_type="application/x-ndjson")

        @app.get("/logs/{task_id}/stream")
        async def stream_log(task_id: str, request: Request):
            try:
                path = self._task_logger.get_log_path(task_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid task_id")

            async def event_generator():
                # 1. 回放已有内容
                last_size = 0
                if os.path.exists(path):
                    with open(path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                                try:
                                    evt = json.loads(line)
                                    if evt.get("event") in (
                                        "done",
                                        "task_final",
                                        "task_error",
                                    ):
                                        yield "event: done\ndata: {}\n\n"
                                        return
                                except json.JSONDecodeError:
                                    pass
                        last_size = f.tell()

                # 2. 实时轮询新行
                while True:
                    if await request.is_disconnected():
                        break

                    await asyncio.sleep(0.1)

                    if not os.path.exists(path):
                        continue

                    current_size = os.path.getsize(path)
                    if current_size < last_size:
                        # 文件被删除重建，从头开始
                        last_size = 0
                    if current_size > last_size:
                        with open(path, "r") as f:
                            f.seek(last_size)
                            for line in f:
                                line = line.strip()
                                if line:
                                    yield f"data: {line}\n\n"

                                    try:
                                        evt = json.loads(line)
                                        if evt.get("event") in (
                                            "done",
                                            "task_final",
                                            "task_error",
                                        ):
                                            yield "event: done\ndata: {}\n\n"
                                            return
                                    except json.JSONDecodeError:
                                        pass
                        last_size = current_size

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/ui/debug")
        async def debug_ui():
            html_path = UI_DIR / "debug.html"
            if not html_path.exists():
                raise HTTPException(status_code=404, detail="debug UI not found")
            return FileResponse(html_path, media_type="text/html")

        @app.get("/ui/map")
        async def serve_map_ui():
            """提供 SAR 地图可视化界面。"""
            map_file = UI_DIR / "map.html"
            if not map_file.exists():
                raise HTTPException(status_code=404, detail="map UI not found")
            return FileResponse(map_file, media_type="text/html")

        @app.get("/map/state")
        async def map_state_stream(request: Request):
            """SSE 端点：实时推送 SAR 网格地图状态。"""
            async def event_generator():
                import time
                last_step = -1
                while True:
                    if await request.is_disconnected():
                        break
                    if self._barrier is None:
                        yield "event: waiting\ndata: {}\n\n"
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        snapshot = self._barrier.get_env_snapshot()
                        metrics = self._barrier.get_metrics()
                        current_step = metrics.get("steps", 0)
                        if current_step != last_step or last_step == -1:
                            last_step = current_step
                            # 序列化 position 为 tuple
                            def _serialize(obj):
                                if hasattr(obj, "get"):
                                    return obj.get()
                                return str(obj)
                            data = {
                                "step": current_step,
                                "finished": self._barrier.is_finished(),
                                "coverage": metrics.get("coverage", 0),
                                "transport_rate": metrics.get("transport_rate", 0),
                                "agents": len(snapshot.get("agents", [])),
                                "fires": len(snapshot.get("fires", [])),
                                "persons": len(snapshot.get("persons", [])),
                                "snapshot": json.loads(
                                    json.dumps(snapshot, default=_serialize)
                                ),
                            }
                            yield f"data: {json.dumps(data)}\n\n"
                    except Exception as e:
                        logger.warning(f"map/state error: {e}")
                    await asyncio.sleep(0.5)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post("/api/a2a/jsonrpc")
        async def proxy_a2a_jsonrpc(request: Request):
            """将 JSON-RPC 请求流式代理到 A2A Server (8081)，支持 SSE。"""
            body = await request.body()
            headers = {
                "content-type": request.headers.get("content-type", "application/json"),
                "a2a-version": "1.0",
            }
            a2a_url = f"http://{self._host}:{self._a2a_port}/api/v1/jsonrpc/"

            async def _stream():
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(600.0)
                    ) as client:
                        async with client.stream(
                            "POST", a2a_url, content=body, headers=headers
                        ) as resp:
                            if resp.status_code >= 400:
                                # 上游返回错误，包装为 JSON-RPC error 并终止流
                                err = json.dumps(
                                    {
                                        "jsonrpc": "2.0",
                                        "error": {
                                            "code": -32000,
                                            "message": f"A2A server returned HTTP {resp.status_code}",
                                        },
                                    }
                                )
                                yield f"data: {err}\n\n".encode()
                                return
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                except httpx.ConnectError:
                    err = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32000,
                                "message": "A2A server not reachable",
                            },
                        }
                    )
                    yield f"data: {err}\n\n".encode()

            return StreamingResponse(_stream(), media_type="text/event-stream")

        @app.websocket("/ws/worker/{worker_id}")
        async def worker_ws(websocket: WebSocket, worker_id: str):
            await websocket.accept()
            async with self._worker_ws_lock:
                self._worker_ws[worker_id] = websocket
            try:
                while True:
                    data = await websocket.receive_text()
                    await self._handle_worker_message(worker_id, json.loads(data))
            except WebSocketDisconnect:
                async with self._worker_ws_lock:
                    ws = self._worker_ws.pop(worker_id, None)
                try:
                    running_tasks = self._task_queue.list_running_by_worker(worker_id)
                    for task in running_tasks:
                        task.status = TaskStatus.PENDING
                        task.assigned_worker = None
                        task.updated_at = datetime.now(timezone.utc)
                        logger.info(
                            f"[Coordinator] Reset task {task.task_id} from disconnected worker {worker_id}"
                        )
                except Exception as e:
                    logger.error(f"Error resetting tasks for worker {worker_id}: {e}")
                finally:
                    # Best effort unregister - ensure cleanup even if requeue had issues
                    try:
                        self._registry.unregister(worker_id)
                        self._agent_registry.unregister_worker(worker_id)
                    except Exception as e:
                        logger.warning(f"Failed to unregister worker {worker_id}: {e}")

        return app

    async def _handle_worker_message(self, worker_id: str, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type")
        payload = msg.get("payload", {})

        if msg_type == WS_REGISTER:
            a2a_endpoint = payload["a2a_endpoint"]

            # 1. WorkerRegistry 记录基本信息（仅连通性）
            self._registry.register_from_ws(worker_id, a2a_endpoint)

            # 2. 通过 A2A 协议拉取 AgentCard（带重试，处理 A2A server 启动时序竞争）
            agent_card_url = f"{a2a_endpoint.rstrip('/')}/.well-known/agent-card.json"
            card_data = await self._fetch_agent_card(
                agent_card_url, retries=3, delay=1.0
            )

            # 3. 解析 AgentCard 并注册到 AgentRegistry
            if card_data:
                self._agent_registry.register_from_agent_card(
                    worker_id=worker_id,
                    endpoint=a2a_endpoint,
                    agent_card=card_data,
                )
            else:
                logger.warning(
                    f"Failed to fetch AgentCard for {worker_id}, using minimal registration"
                )
                self._agent_registry.register(
                    AgentInfo(
                        agent_id=worker_id,
                        description=f"Worker {worker_id} (AgentCard unavailable)",
                        endpoint=a2a_endpoint,
                        status=AgentStatus.ONLINE,
                    )
                )

        elif msg_type == WS_HEARTBEAT:
            self._registry.update_heartbeat(worker_id)
            self._agent_registry.update_heartbeat_from_worker(worker_id)

        elif msg_type == WS_TASK_PROGRESS:
            task_id = payload.get("task_id")
            event_data = payload.get("event_data", {})
            logger.info(
                f"[Coordinator] Task {task_id} progress from {worker_id}: {event_data}"
            )

        elif msg_type == WS_RELAY_A2A:
            # A2A 中转请求 - Worker 请求直连到其他 Worker
            to_agent_id = payload.get("to_agent_id")
            # 始终使用 worker_id（来自 WebSocket 连接），不接受 payload 中的 from_worker_id
            from_worker_id = worker_id
            if to_agent_id:
                try:
                    info = await self._mesh_guide.relay_via_coordinator(
                        from_worker_id, to_agent_id
                    )
                    async with self._worker_ws_lock:
                        target_ws = self._worker_ws.get(to_agent_id)
                    if target_ws:
                        await target_ws.send_json(
                            {
                                "type": "incoming_relay_request",
                                "payload": {
                                    "from_worker_id": from_worker_id,
                                    "relay_info": info,
                                },
                            }
                        )
                        await self._send_to_worker(
                            from_worker_id,
                            {
                                "type": "relay_initiated",
                                "payload": {
                                    "to_agent_id": to_agent_id,
                                    "relay_info": info,
                                },
                            },
                        )
                    else:
                        # 目标未连接，告知请求者
                        await self._send_to_worker(
                            from_worker_id,
                            {
                                "type": "relay_failed",
                                "payload": {
                                    "error": f"Agent {to_agent_id} is not connected"
                                },
                            },
                        )
                except AgentNotFoundError:
                    logger.warning(f"Agent {to_agent_id} not found for relay request")
                    await self._send_to_worker(
                        from_worker_id,
                        {
                            "type": "relay_failed",
                            "payload": {"error": f"Agent {to_agent_id} not found"},
                        },
                    )

        elif msg_type == WS_CANCEL_TASK:
            task_id = payload.get("task_id")
            if task_id:
                try:
                    task = self._task_queue.get(task_id)
                    assigned_worker = task.assigned_worker
                    self._task_queue.cancel(task_id)
                    logger.info(f"[Coordinator] Task {task_id} cancelled")

                    if assigned_worker and assigned_worker in self._worker_ws:
                        async with self._worker_ws_lock:
                            await self._send_to_worker(
                                assigned_worker,
                                {
                                    "type": WS_CANCEL_TASK,
                                    "payload": {"task_id": task_id},
                                },
                            )
                except TaskNotFoundError:
                    logger.warning(f"Cannot cancel task {task_id}: not found")
                except InvalidStatusTransitionError:
                    logger.warning(
                        f"Cannot cancel task {task_id}: invalid state transition"
                    )
                except Exception as e:
                    logger.error(f"Unexpected error cancelling task {task_id}: {e}")

    async def _fetch_agent_card(
        self, url: str, retries: int = 3, delay: float = 1.0
    ) -> dict | None:
        """从 Worker 拉取 AgentCard，带重试（处理 A2A server 启动时序竞争）。

        Args:
            url: AgentCard URL
            retries: 最大重试次数
            delay: 重试间隔（秒）

        Returns:
            AgentCard JSON dict，失败返回 None
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(retries):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    logger.info(f"Fetched AgentCard from {url}")
                    return resp.json()
                except Exception as e:
                    logger.warning(
                        f"AgentCard fetch attempt {attempt + 1}/{retries} "
                        f"for {url} failed: {e}"
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
        return None

    async def _send_to_worker(self, worker_id: str, msg: Dict[str, Any]) -> bool:
        async with self._worker_ws_lock:
            ws = self._worker_ws.get(worker_id)
        if ws is None:
            return False
        await ws.send_json(msg)
        return True

    async def _start_cleanup_task(self) -> None:
        """启动后台心跳清理任务。"""

        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    # Cancellation requested - exit cleanly
                    break
                try:
                    await self._registry._check_heartbeats()
                    logger.debug("[Coordinator] Heartbeat cleanup completed")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[Coordinator] Heartbeat cleanup error: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def _stop_cleanup_task(self) -> None:
        """停止后台清理任务。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    def run(self) -> None:
        config = uvicorn.Config(self._app, host=self._host, port=self._port)
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.run()

    async def shutdown(self) -> None:
        if hasattr(self, "_uvicorn_server") and self._uvicorn_server:
            self._uvicorn_server.should_exit = True


def create_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    a2a_port: int = 8081,
    config_path: str | None = None,
    prompts_dir: str | None = None,
    tools_dir: str | None = None,
    extra_tools: list | None = None,
    skills_dir: str | None = None,
    log_dir: str | None = None,
    router_model: str = "deepseek-v4-flash",
    router_max_steps: int = 15,
    router_temperature: float = 0.7,
    router_provider: str = "anthropic",
    router_api_base: str = "https://api.anthropic.com",
    router_api_key_env: str = "ANTHROPIC_API_KEY",
    verifier_model: str | None = None,
    verifier_max_steps: int = 10,
    verifier_temperature: float = 0.3,
    verifier_enabled: bool = True,
    orchestration_mode: str = "agentic",
    max_tasks_per_run: int = 20,
    orchestration_timeout: int = 600,
    router_step_callback=None,
) -> CoordinatorServer:
    return CoordinatorServer(
        host=host,
        port=port,
        a2a_port=a2a_port,
        config_path=config_path,
        prompts_dir=prompts_dir,
        tools_dir=tools_dir,
        extra_tools=extra_tools,
        skills_dir=skills_dir,
        log_dir=log_dir,
        router_model=router_model,
        router_max_steps=router_max_steps,
        router_temperature=router_temperature,
        router_provider=router_provider,
        router_api_base=router_api_base,
        router_api_key_env=router_api_key_env,
        verifier_model=verifier_model,
        verifier_max_steps=verifier_max_steps,
        verifier_temperature=verifier_temperature,
        verifier_enabled=verifier_enabled,
        orchestration_mode=orchestration_mode,
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
        router_step_callback=router_step_callback,
    )
