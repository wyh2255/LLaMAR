"""Worker 上的 Coordinator WebSocket 客户端（仅心跳）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from a2a.shared.types import (
    build_ws_register_payload,
    build_ws_heartbeat_payload,
    WS_REGISTER,
    WS_HEARTBEAT,
)


logger = logging.getLogger(__name__)


class ConnectionError(Exception):
    """WebSocket 未连接时发送消息。"""
    pass


class CoordinatorWebSocketClient:
    """
    Worker 的 WebSocket 客户端，仅负责心跳。
    任务通过 A2A HTTP 接收，不走 WebSocket。
    """

    def __init__(
        self,
        coordinator_url: str,
        worker_id: str,
        a2a_endpoint: str,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._coordinator_url = coordinator_url
        self._worker_id = worker_id
        self._a2a_endpoint = a2a_endpoint
        self._ws: WebSocketClientProtocol | None = None
        self._receive_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._reconnect_attempts = 0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def connect(self) -> None:
        """连接到 Coordinator WebSocket 并发送注册。"""
        ws_url = f"{self._coordinator_url}/ws/worker/{self._worker_id}"
        self._ws = await websockets.connect(ws_url)
        self._running = True

        # 注册 Worker（仅连通性信息，能力信息通过 A2A AgentCard 获取）
        await self._send({
            "type": WS_REGISTER,
            "payload": build_ws_register_payload(
                worker_id=self._worker_id,
                a2a_endpoint=self._a2a_endpoint,
            ),
        })

        # 启动心跳循环
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self) -> None:
        """断开连接。"""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._receive_task:
            self._receive_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send_heartbeat(self) -> None:
        """发送心跳。"""
        if self._ws:
            await self._send({
                "type": WS_HEARTBEAT,
                "payload": build_ws_heartbeat_payload(self._worker_id),
            })

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError(f"WebSocket not connected (worker_id={self._worker_id})")
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise

    async def _heartbeat_loop(self) -> None:
        """定期发送心跳。"""
        while self._running:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            if self._running:
                try:
                    await self.send_heartbeat()
                except ConnectionError:
                    logger.warning("Cannot send heartbeat, not connected")
                    break

    async def _try_reconnect(self) -> bool:
        """尝试重新连接。"""
        for attempt in range(self._max_retries):
            self._reconnect_attempts += 1
            delay = min(self._retry_delay * (2 ** attempt), 60)  # Cap at 60s
            logger.info(f"Reconnecting to coordinator (attempt {attempt + 1}/{self._max_retries}) in {delay}s")
            await asyncio.sleep(delay)
            try:
                await self.connect()
                logger.info("Reconnected successfully")
                self._reconnect_attempts = 0  # Reset on success
                return True
            except Exception as e:
                logger.warning(f"Reconnect attempt {attempt + 1} failed: {e}")
        logger.error("Max reconnection attempts reached")
        return False
