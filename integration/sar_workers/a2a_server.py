"""精简版 A2A Worker 服务器 — 替代 MARoS transport.py。

协议兼容 Coordinator（push_task.py + server.py + worker_registry.py）：
- JSON-RPC 2.0 task endpoint with SSE streaming response
- AgentCard at /.well-known/agent-card.json
- WebSocket registration + heartbeat + task result notification

不依赖 a2a_lib、openharness_a2a、mini_agent。

注意：不使用 `from __future__ import annotations`，因为它会把类型注解变成字符串，
导致 FastAPI 无法识别 `request: Request` 参数类型（返回 422）。
"""
import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


class A2AWorkerServer:
    """精简版 A2A Worker 服务器。

    Args:
        agent_name: Worker 名称（如 "Alice"）
        port: HTTP server 端口
        coordinator_url: Coordinator HTTP 地址（如 "http://localhost:8080"）
        react_agent: WorkerReActAgent 实例
        skills: Skill 定义列表
        model: LLM 模型名（写入 AgentCard）
    """

    def __init__(
        self,
        agent_name: str,
        port: int,
        coordinator_url: str,
        react_agent: Any,
        skills: list[Any] = None,
        model: str = "deepseek-v4-flash",
    ):
        self._agent_name = agent_name
        self._port = port
        self._coordinator_url = coordinator_url.rstrip("/")
        self._react_agent = react_agent
        self._skills = skills or []
        self._model = model
        self._a2a_endpoint = f"http://localhost:{port}/"

        self._running = False
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._uvicorn_server = None  # 保存引用用于 stop()

    # ── HTTP 路由 ──────────────────────────────────────────────────

    def _create_app(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse

        app = FastAPI(title=f"SAR Worker: {self._agent_name}")

        @app.get("/.well-known/agent-card.json")
        async def agent_card():
            # 使用 Skill.to_agent_skill_dict() 生成兼容格式（含 id/tags）
            skills_list = []
            for s in self._skills:
                if hasattr(s, "to_agent_skill_dict"):
                    skills_list.append(s.to_agent_skill_dict())
                else:
                    skills_list.append({
                        "id": getattr(s, "name", str(s)),
                        "name": getattr(s, "name", str(s)),
                        "description": getattr(s, "description", ""),
                        "tags": [getattr(s, "name", str(s))],
                    })
            # 追加 backend 和 model 特殊 skill（Coordinator 用于注册 agent 信息）
            skills_list.append({
                "id": "backend", "name": "backend",
                "description": "Backend", "tags": ["backend", "react_agent"],
            })
            skills_list.append({
                "id": "model", "name": "model",
                "description": "Model", "tags": ["model", self._model],
            })
            return {
                "version": "1.0",
                "name": self._agent_name,
                "description": f"SAR search and rescue robot: {self._agent_name}",
                "url": self._a2a_endpoint,
                "skills": skills_list,
                "interfaces": ["a2a-jsonrpc"],
            }

        @app.post("/api/v1/jsonrpc/")
        async def jsonrpc_handler(request: Request):
            body = await request.json()
            method = body.get("method", "")
            req_id = body.get("id", str(uuid.uuid4()))

            if method == "SendMessage":
                params = body.get("params", {})
                message = params.get("message", {})
                parts = message.get("parts", [])
                task_text = parts[0].get("text", "") if parts else ""
                task_id = message.get("messageId", req_id)

                logger.info(f"[{self._agent_name}] JSON-RPC SendMessage: {task_text[:80]}...")

                # 返回 SSE 流（Coordinator 通过 httpx.stream 读取）
                return StreamingResponse(
                    self._sse_stream(task_id, task_text),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            else:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    "id": req_id,
                }, status_code=400)

        return app

    async def _sse_stream(self, task_id: str, task_text: str):
        """SSE 流生成器 — 先发 working 状态，执行任务，再发 completed/failed。

        Coordinator 的 PushTaskTool 通过 httpx.stream("POST", url) 读取 SSE 流，
        解析 "data: {...}" 格式的事件，从 result.artifact.parts[].text 提取最终结果。
        """
        # 事件 1: 任务已接受（working 状态）
        working_event = {
            "jsonrpc": "2.0",
            "result": {"status": {"state": "working"}, "id": task_id},
        }
        yield f"data: {json.dumps(working_event)}\n\n"

        # 执行 ReAct 循环
        result_text = ""
        try:
            result_text = await self._react_agent.run(user_message=task_text)
            logger.info(f"[{self._agent_name}] Task {task_id} completed: {result_text[:80]}...")

            # 事件 2: 任务完成（带 artifact）
            complete_event = {
                "jsonrpc": "2.0",
                "result": {
                    "artifact": {"parts": [{"text": result_text}]},
                    "status": {"state": "completed"},
                    "id": task_id,
                },
            }
            yield f"data: {json.dumps(complete_event)}\n\n"
        except Exception as e:
            logger.error(f"[{self._agent_name}] Task {task_id} failed: {e}")
            error_event = {
                "jsonrpc": "2.0",
                "result": {
                    "artifact": {"parts": [{"text": f"Error: {e}"}]},
                    "status": {"state": "failed"},
                    "id": task_id,
                },
            }
            yield f"data: {json.dumps(error_event)}\n\n"

    # ── WebSocket 客户端 ───────────────────────────────────────────

    async def _ws_send(self, data: dict):
        if self._ws:
            try:
                await self._ws.send(json.dumps(data))
            except Exception as e:
                logger.warning(f"[{self._agent_name}] WS send failed: {e}")

    async def _ws_loop(self):
        import websockets

        ws_url = (
            f"ws://{self._coordinator_url.replace('http://', '').replace('https://', '')}"
            f"/ws/worker/{self._agent_name}"
        )

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "type": "register",
                        "payload": {
                            "worker_id": self._agent_name,
                            "a2a_endpoint": self._a2a_endpoint,
                        },
                    }))
                    logger.info(f"[{self._agent_name}] Registered with Coordinator at {ws_url}")

                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        async for message in ws:
                            data = json.loads(message)
                            logger.debug(f"[{self._agent_name}] WS recv: {data.get('type', 'unknown')}")
                    finally:
                        heartbeat_task.cancel()
                        self._ws = None

            except websockets.ConnectionClosed:
                logger.warning(f"[{self._agent_name}] WS disconnected, reconnecting in 2s...")
                self._ws = None
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"[{self._agent_name}] WS error: {e}, reconnecting in 5s...")
                self._ws = None
                await asyncio.sleep(5)

    async def _heartbeat_loop(self, ws):
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "payload": {"worker_id": self._agent_name},
                }))
            except Exception:
                break

    # ── 启动/停止 ──────────────────────────────────────────────────

    def start_in_thread(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"[{self._agent_name}] A2A server starting on port {self._port}")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"[{self._agent_name}] Server loop error: {e}")
        finally:
            loop.close()

    async def _serve(self):
        import uvicorn

        app = self._create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="warning")
        server = uvicorn.Server(config)
        self._uvicorn_server = server  # 保存引用用于 stop()

        http_task = asyncio.create_task(server.serve())
        ws_task = asyncio.create_task(self._ws_loop())

        try:
            await asyncio.gather(http_task, ws_task)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self._running = False
        # 通知 uvicorn 优雅退出（释放端口）
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"[{self._agent_name}] A2A server stopped")
