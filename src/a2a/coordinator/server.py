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
from a2a.coordinator.event_store import event_store
from a2a.coordinator.mesh_guide import MeshGuide, AgentNotFoundError
from a2a.coordinator.routes import health, workers
from a2a.coordinator.supervision_state_store import SupervisionStateStore
from a2a.coordinator.task_watchdog import TaskWatchdog, WatchdogConfig
from a2a.shared.server_lifecycle import shutdown_uvicorn_server
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

# Push notification artifact text cache
# Worker may push multiple times (artifact + status), need to aggregate
_push_artifact_cache: dict[str, list[str]] = {}


def _extract_worker_data_blocks(text: str) -> list[dict[str, Any]]:
    marker = "[DATA]"
    if marker not in text:
        return []
    blocks = []
    for chunk in text.split(marker)[1:]:
        raw = chunk.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_observation_from_status_text(text: str) -> dict[str, Any] | None:
    """Legacy: extract single observation from a report_observation tool result."""
    for block in _extract_worker_data_blocks(text):
        if (
            block.get("ev") != "tool_result"
            or block.get("tool_name") != "report_observation"
            or not block.get("success")
        ):
            continue
        content = block.get("content") or ""
        try:
            observation = json.loads(content)
        except json.JSONDecodeError:
            return None
        return observation if isinstance(observation, dict) else None
    return None


def _extract_auto_observations(text: str) -> list[dict[str, Any]]:
    """Extract auto-reported observations from structured_data in tool result blocks.

    Handles both:
    - New format: any tool_result with structured_data.observations list
    - Legacy format: report_observation tool_result with JSON content
    """
    results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for block in _extract_worker_data_blocks(text):
        if block.get("ev") != "tool_result" or not block.get("success"):
            continue

        # New format: structured_data with observations list
        structured = block.get("structured_data")
        if isinstance(structured, dict):
            obs_list = structured.get("observations", [])
            if isinstance(obs_list, list):
                for obs in obs_list:
                    if isinstance(obs, dict):
                        dedup_key = f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
                        if dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            results.append(obs)

        # Legacy format: report_observation with JSON content
        if block.get("tool_name") == "report_observation":
            content = block.get("content") or ""
            try:
                obs = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obs, dict):
                dedup_key = (
                    f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
                )
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    results.append(obs)

    return results


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
        max_tasks_per_run: int = 20,
        orchestration_timeout: int = 600,
        orchestration_mode: str = "agentic",
        router_step_callback=None,
        context_config=None,
        token_limit: int = 80000,
        sandbox_policy=None,
        require_explicit_completion: bool = False,
        state_provider=None,
        supervision_state_store=None,
        task_watchdog=None,
        watchdog_config: WatchdogConfig | None = None,
        # Phase 4: signed task dispatch
        coordinator_secret: bytes | None = None,
        coordinator_id: str = "Coordinator",
        # UI static files directory. When None, UI endpoints return 404.
        ui_dir: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._sandbox_policy = sandbox_policy
        self._ui_dir = Path(ui_dir) if ui_dir else None
        self._registry = WorkerRegistry()
        self._agent_registry = AgentRegistry(
            static_config_path=Path(config_path) if config_path else None
        )
        self._task_logger = TaskLogger(base_dir=str(log_dir) if log_dir else "logs")
        self._task_queue = TaskQueue(on_cleanup=self._cleanup_task_logs)
        self._mesh_guide = MeshGuide(self._agent_registry)
        self._worker_ws: Dict[str, WebSocket] = {}
        self._worker_ws_lock = asyncio.Lock()

        # 构建 RouterAgent（注入所有路径和配置参数）
        self._router = RouterAgent(
            registry=self._agent_registry,
            sandbox_policy=self._sandbox_policy,
            prompts_dir=Path(prompts_dir) if prompts_dir else None,
            custom_tools_dir=Path(tools_dir) if tools_dir else None,
            extra_tools=extra_tools,
            skills_dir=Path(skills_dir) if skills_dir else None,
            coordinator_secret=coordinator_secret,
            coordinator_id=coordinator_id,
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
                    sandbox_policy=self._sandbox_policy,
                    model=v_model,
                    max_steps=verifier_max_steps,
                    temperature=verifier_temperature,
                    provider=router_provider,
                    api_base=router_api_base,
                    api_key_env=router_api_key_env,
                    log_dir=Path(log_dir) if log_dir else None,
                )

        self._max_tasks_per_run = max_tasks_per_run
        self._orchestration_timeout = orchestration_timeout
        self._orchestration_mode = orchestration_mode
        self._router_step_callback = router_step_callback
        self._context_config = context_config
        self._token_limit = token_limit
        self._require_explicit_completion = require_explicit_completion
        self._state_provider = state_provider
        self._log_dir = log_dir

        # Supervision state store and task watchdog (Phase 3)
        self._supervision_state_store = (
            supervision_state_store or SupervisionStateStore(log_dir=log_dir)
        )
        self._task_watchdog = task_watchdog or TaskWatchdog(
            worker_registry=self._registry,
            event_store=event_store,
            supervision_store=self._supervision_state_store,
            barrier=None,
            config=watchdog_config,
        )

        self._barrier = None  # SARBarrier (optional, for map visualization)
        self._semantic_map = (
            None  # SemanticMapStore (optional, for observation ingestion)
        )

        self._observed_step_keys: set[str] = set()
        self._app = self._build_app()
        self._a2a_server = None
        self._server_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    def set_barrier(self, barrier) -> None:
        """注入 SARBarrier 引用，供 /map/state SSE 端点使用。"""
        self._barrier = barrier

    def set_semantic_map(self, semantic_map) -> None:
        """注入 SemanticMapStore 引用，供 observation ingest 使用。"""
        self._semantic_map = semantic_map

    def _is_step_observation_known(self, obs: dict) -> bool:
        key = f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
        if key in self._observed_step_keys:
            return True
        self._observed_step_keys.add(key)
        return False

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
            # Inject barrier into watchdog for domain delta detection
            self._task_watchdog._barrier = self._barrier
            self._task_watchdog._supervision_store.set_log_dir(self._log_dir)
            await self._task_watchdog.start()
            a2a_srv = create_coordinator_a2a_server(
                host=self._host,
                port=self._a2a_port,
                agent_registry=self._agent_registry,
                task_queue=self._task_queue,
                router=self._router,  # 注入 RouterAgent
                verifier=self._verifier,  # 注入 VerifierAgent（可为 None）
                task_logger=self._task_logger,  # 注入 TaskLogger
                max_tasks_per_run=self._max_tasks_per_run,
                orchestration_timeout=self._orchestration_timeout,
                orchestration_mode=self._orchestration_mode,
                router_step_callback=self._router_step_callback,
                context_config=self._context_config,
                token_limit=self._token_limit,
                require_explicit_completion=self._require_explicit_completion,
                sandbox_policy=self._sandbox_policy,
                coordinator_host="localhost",
                coordinator_port=self._port,
                state_provider=self._state_provider,
                task_watchdog=self._task_watchdog,
            )
            self._a2a_server = a2a_srv
            self._server_task = asyncio.create_task(a2a_srv.serve())
            yield
            # 关闭时
            await self._task_watchdog.stop()
            try:
                await shutdown_uvicorn_server(self._a2a_server, self._server_task)
            finally:
                self._server_task = None
                self._a2a_server = None
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

        @app.post("/tasks/{task_id}/cancel")
        async def cancel_task(task_id: str):
            """Cancel a running task. Also cancels the worker-side A2A task if assigned."""
            try:
                task = self._task_queue.get(task_id)
                assigned_worker = task.assigned_worker
                self._task_queue.cancel(task_id)
                # Notify worker via WebSocket
                if assigned_worker and assigned_worker in self._worker_ws:
                    async with self._worker_ws_lock:
                        await self._send_to_worker(
                            assigned_worker,
                            {
                                "type": WS_CANCEL_TASK,
                                "payload": {"task_id": task_id},
                            },
                        )
                return {"status": "cancelled", "task_id": task_id}
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail="Task not found")
            except InvalidStatusTransitionError:
                raise HTTPException(status_code=400, detail="Cannot cancel task in current state")
            except Exception as e:
                logger.error(f"Error cancelling task {task_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        async def _do_cancel_experiment(context_id: str) -> dict:
            """Shared logic: cancel all tasks for a context_id, notify workers, stop barrier."""
            tasks = self._task_queue.list_by_context(context_id)
            if not tasks:
                raise HTTPException(
                    status_code=404,
                    detail=f"No tasks found for context_id: {context_id}",
                )

            cancelled_ids = []
            notified_workers: set[str] = set()

            for task in tasks:
                if task.status in (
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                ):
                    try:
                        self._task_queue.cancel(task.task_id)
                        cancelled_ids.append(task.task_id)
                        if task.assigned_worker:
                            notified_workers.add(task.assigned_worker)
                    except (TaskNotFoundError, InvalidStatusTransitionError):
                        pass

            # Notify all affected workers via WebSocket
            async with self._worker_ws_lock:
                for worker_id in notified_workers:
                    if worker_id in self._worker_ws:
                        await self._send_to_worker(
                            worker_id,
                            {
                                "type": WS_CANCEL_TASK,
                                "payload": {
                                    "task_id": "all",
                                    "context_id": context_id,
                                },
                            },
                        )

            # If SARBarrier is attached, stop the environment and wake all workers
            barrier_stopped = False
            if self._barrier is not None:
                try:
                    self._barrier.stop()
                    barrier_stopped = True
                except Exception as e:
                    logger.error("Error stopping barrier: %s", e)

            logger.info(
                "Experiment %s cancelled: %d tasks, %d workers notified, barrier=%s",
                context_id,
                len(cancelled_ids),
                len(notified_workers),
                barrier_stopped,
            )
            return {
                "status": "cancelled",
                "context_id": context_id,
                "cancelled_tasks": cancelled_ids,
                "workers_notified": list(notified_workers),
                "barrier_stopped": barrier_stopped,
            }

        @app.post("/experiment/cancel-by-task/{task_id}")
        async def cancel_experiment_by_task(task_id: str):
            """Cancel the entire experiment that a task belongs to.

            Looks up the task by task_id, finds its context_id, and cancels
            all tasks sharing that context_id. Also stops the SARBarrier if attached.
            """
            try:
                task = self._task_queue.get(task_id)
                cid = task.context_id
                if not cid:
                    # Fallback: use task_id itself as context marker
                    return await _do_cancel_experiment(task_id)
                # Forward to the context_id-based handler
                return await _do_cancel_experiment(cid)
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        @app.post("/experiment/{context_id}/cancel")
        async def cancel_experiment(context_id: str):
            """Cancel all tasks for a given context_id (one complete experiment).

            Finds all tasks in the TaskQueue with the matching context_id,
            cancels them, notifies all affected workers via WebSocket,
            and if a SARBarrier is attached, calls barrier.stop() to shut
            down the environment and wake all waiting worker threads.
            """
            return await _do_cancel_experiment(context_id)

        # ---- UI 和 A2A 代理 ----

        UI_DIR = self._ui_dir

        def _serve_ui_file(filename: str, media_type: str = "text/html"):
            if UI_DIR is None:
                raise HTTPException(status_code=404, detail="UI not configured")
            ui_file = UI_DIR / filename
            if not ui_file.exists():
                raise HTTPException(status_code=404, detail=f"UI file not found: {filename}")
            return FileResponse(ui_file, media_type=media_type)

        @app.get("/ui")
        async def serve_ui():
            """提供任务控制台 HTML 界面。"""
            return _serve_ui_file("task_ui.html")

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
            return _serve_ui_file("debug.html")

        @app.get("/ui/map")
        async def serve_map_ui():
            """提供 SAR 地图可视化界面。"""
            return _serve_ui_file("map.html")

        @app.get("/dashboard")
        async def serve_dashboard():
            """提供独立仪表盘前端。"""
            return _serve_ui_file("dashboard/index.html")

        @app.post("/a2a/push-callback")
        async def handle_push_notification(request: Request):
            from google.protobuf.json_format import ParseDict
            from a2a.types.a2a_pb2 import StreamResponse, TaskState
            from a2a.coordinator.task_store import resolve_global_future

            def _try_resolve_worker_id(task_id: str) -> str:
                """从 TaskStore 映射解析 worker_id；失败返回空字符串。"""
                if (
                    self._task_watchdog is None
                    or self._task_watchdog._task_store is None
                ):
                    return ""
                store = self._task_watchdog._task_store
                dispatch_id = store._worker_to_dispatch.get(task_id, "")
                if dispatch_id:
                    node = store.get_node(dispatch_id)
                    return node.worker_id if node and node.worker_id else ""
                return ""

            body = await request.json()
            sr = StreamResponse()
            ParseDict(body, sr)

            task_id = None
            is_terminal = False

            if sr.HasField("task"):
                t = sr.task
                task_id = t.id
                state_name = (
                    TaskState.Name(t.status.state) if t.status.state else "UNKNOWN"
                )
                is_terminal = t.status.state in (
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                )
                if task_id:
                    event_store.append(task_id, "status_update", state=state_name)
                    worker_id = _try_resolve_worker_id(task_id)
                    if worker_id:
                        self._task_watchdog.record_worker_contact(worker_id)
                    dispatch_id = (
                        self._task_watchdog._task_store._worker_to_dispatch.get(
                            task_id, task_id
                        )
                        if self._task_watchdog and self._task_watchdog._task_store
                        else task_id
                    )
                    self._task_watchdog.record_state_change(
                        dispatch_id=dispatch_id,
                        worker_id=worker_id,
                        worker_task_id=task_id,
                        state_name=state_name,
                    )
                    if is_terminal:
                        self._task_watchdog.record_progress(
                            dispatch_id=dispatch_id,
                            worker_id=worker_id,
                            worker_task_id=task_id,
                            source="status_terminal",
                            step=self._barrier._step_counter if self._barrier else 0,
                        )
            elif sr.HasField("artifact_update"):
                au = sr.artifact_update
                task_id = au.task_id
                if au.HasField("artifact"):
                    texts = [p.text for p in au.artifact.parts if p.text]
                    if texts and task_id:
                        combined = " ".join(texts)
                        _push_artifact_cache.setdefault(task_id, []).extend(texts)
                        event_store.append(task_id, "artifact_update", text=combined)
                        worker_id = _try_resolve_worker_id(task_id)
                        if worker_id:
                            self._task_watchdog.record_worker_contact(worker_id)
                        dispatch_id = (
                            self._task_watchdog._task_store._worker_to_dispatch.get(
                                task_id, task_id
                            )
                            if self._task_watchdog and self._task_watchdog._task_store
                            else task_id
                        )
                        self._task_watchdog.record_progress(
                            dispatch_id=dispatch_id,
                            worker_id=worker_id,
                            worker_task_id=task_id,
                            source="artifact_update",
                            step=self._barrier._step_counter if self._barrier else 0,
                        )
            elif sr.HasField("status_update"):
                su = sr.status_update
                task_id = su.task_id
                if su.HasField("status"):
                    state_name = (
                        TaskState.Name(su.status.state)
                        if su.status.state
                        else "UNKNOWN"
                    )
                    is_terminal = su.status.state in (
                        TaskState.TASK_STATE_COMPLETED,
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_CANCELED,
                    )
                    if task_id:
                        event_store.append(task_id, "status_update", state=state_name)
                        worker_id = _try_resolve_worker_id(task_id)
                        if worker_id:
                            self._task_watchdog.record_worker_contact(worker_id)
                        dispatch_id = (
                            self._task_watchdog._task_store._worker_to_dispatch.get(
                                task_id, task_id
                            )
                            if self._task_watchdog and self._task_watchdog._task_store
                            else task_id
                        )
                        if state_name in (
                            "COMPLETED",
                            "FAILED",
                            "CANCELED",
                            "INPUT_REQUIRED",
                            "TASK_STATE_COMPLETED",
                            "TASK_STATE_FAILED",
                            "TASK_STATE_CANCELED",
                            "TASK_STATE_INPUT_REQUIRED",
                        ):
                            self._task_watchdog.record_state_change(
                                dispatch_id=dispatch_id,
                                worker_id=worker_id,
                                worker_task_id=task_id,
                                state_name=state_name,
                            )
                            if is_terminal:
                                self._task_watchdog.record_progress(
                                    dispatch_id=dispatch_id,
                                    worker_id=worker_id,
                                    worker_task_id=task_id,
                                    source="status_terminal",
                                    step=self._barrier._step_counter
                                    if self._barrier
                                    else 0,
                                )
                        if su.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                            question = ""
                            if su.status.HasField("message"):
                                question = " ".join(
                                    p.text for p in su.status.message.parts if p.text
                                )
                            event_store.append(task_id, "help_request", text=question)
                            self._task_watchdog.record_progress(
                                dispatch_id=dispatch_id,
                                worker_id=worker_id,
                                worker_task_id=task_id,
                                source="input_required",
                                step=self._barrier._step_counter
                                if self._barrier
                                else 0,
                            )

                        if su.status.HasField("message"):
                            status_text = " ".join(
                                p.text for p in su.status.message.parts if p.text
                            )
                            observations = _extract_auto_observations(status_text)
                            if observations:
                                for obs in observations:
                                    if self._is_step_observation_known(obs):
                                        continue
                                    event_store.append(
                                        task_id,
                                        "observation_report",
                                        text=status_text[:500],
                                        observation=obs,
                                    )
                                    if self._semantic_map is not None:
                                        self._semantic_map.ingest_observation(obs)
                                self._task_watchdog.record_progress(
                                    dispatch_id=dispatch_id,
                                    worker_id=worker_id,
                                    worker_task_id=task_id,
                                    source="observation_report",
                                    step=self._barrier._step_counter
                                    if self._barrier
                                    else 0,
                                )

            if task_id and is_terminal:

                async def _resolve_with_delay():
                    await asyncio.sleep(0.1)
                    parts = _push_artifact_cache.pop(task_id, [])
                    text = " ".join(parts) if parts else "(no artifact text)"
                    resolve_global_future(task_id, text)

                asyncio.create_task(_resolve_with_delay())

            return {"status": "ok"}

        @app.get("/semantic-map")
        async def semantic_map():
            if self._semantic_map is None:
                return {
                    "status": "unavailable",
                    "known_dynamic_objects": {"fires": [], "persons": []},
                }
            return self._semantic_map.snapshot()

        @app.get("/map/state")
        async def map_state_stream(request: Request):
            """SSE 端点：实时推送 SAR 网格地图状态。"""

            async def event_generator():
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

        @app.get("/dashboard/stream")
        async def dashboard_stream(request: Request):
            """SSE 端点：统一仪表盘数据流，推送所有可视化所需数据。"""

            def _serialize(obj):
                if hasattr(obj, "get"):
                    return obj.get()
                if isinstance(obj, (list, tuple)):
                    return list(obj)
                return str(obj)

            async def event_generator():
                last_step = -1
                while True:
                    if await request.is_disconnected():
                        break
                    if self._barrier is None:
                        yield "event: waiting\ndata: {}\n\n"
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        metrics = self._barrier.get_metrics()
                        current_step = metrics.get("steps", 0)
                        finished = self._barrier.is_finished()
                        coverage = metrics.get("coverage", 0)
                        transport_rate = metrics.get("transport_rate", 0)

                        if current_step == last_step and last_step != -1:
                            await asyncio.sleep(0.5)
                            continue
                        last_step = current_step

                        # Collect all data sources
                        env_snapshot = self._barrier.get_env_snapshot()
                        trajectory = self._barrier.get_trajectory_history()
                        obs_stream = self._barrier.get_observation_stream(limit=40)
                        last_log = self._barrier.get_last_step_log()

                        sem_map = (
                            self._semantic_map.snapshot()
                            if self._semantic_map is not None
                            else {}
                        )

                        # Token usage from barrier accumulator (fed by coordinator callback)
                        router_tokens = {}
                        if hasattr(self, "_barrier") and self._barrier is not None:
                            acc = getattr(self._barrier, "_token_accumulator", None)
                            if acc is not None:
                                router_tokens = {
                                    "prompt_tokens": acc.get("prompt_tokens", 0),
                                    "completion_tokens": acc.get(
                                        "completion_tokens", 0
                                    ),
                                    "total_tokens": acc.get("total_tokens", 0),
                                    "cumulative_prompt": acc.get(
                                        "cumulative_prompt", 0
                                    ),
                                    "cumulative_completion": acc.get(
                                        "cumulative_completion", 0
                                    ),
                                    "cumulative_total": acc.get("cumulative_total", 0),
                                }

                        data = {
                            "step": current_step,
                            "finished": finished,
                            "coverage": round(coverage, 4),
                            "transport_rate": round(transport_rate, 4),
                            "env_snapshot": env_snapshot,
                            "semantic_map": sem_map,
                            "trajectory_history": trajectory,
                            "observation_stream": obs_stream,
                            "last_step_log": last_log,
                            "router_tokens": router_tokens,
                        }

                        yield f"data: {json.dumps(data, default=_serialize)}\n\n"
                    except Exception as e:
                        logger.warning("dashboard/stream error: %s", e)
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
            a2a_url = f"http://127.0.0.1:{self._a2a_port}/api/v1/jsonrpc/"

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
                    self._worker_ws.pop(worker_id, None)
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
            if self._task_watchdog is not None:
                self._task_watchdog.record_worker_contact(worker_id)

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
    max_tasks_per_run: int = 20,
    orchestration_timeout: int = 600,
    orchestration_mode: str = "agentic",
    router_step_callback=None,
    context_config=None,
    token_limit: int = 80000,
    sandbox_policy=None,
    require_explicit_completion: bool = False,
    state_provider=None,
    supervision_state_store=None,
    task_watchdog=None,
    watchdog_config: WatchdogConfig | None = None,
    # Phase 4: signed task dispatch
    coordinator_secret: bytes | None = None,
    coordinator_id: str = "Coordinator",
    ui_dir: str | None = None,
) -> CoordinatorServer:
    return CoordinatorServer(
        sandbox_policy=sandbox_policy,
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
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
        orchestration_mode=orchestration_mode,
        router_step_callback=router_step_callback,
        context_config=context_config,
        token_limit=token_limit,
        require_explicit_completion=require_explicit_completion,
        state_provider=state_provider,
        supervision_state_store=supervision_state_store,
        task_watchdog=task_watchdog,
        watchdog_config=watchdog_config,
        coordinator_secret=coordinator_secret,
        coordinator_id=coordinator_id,
        ui_dir=ui_dir,
    )
