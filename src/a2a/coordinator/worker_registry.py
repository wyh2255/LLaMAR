"""Worker 注册表 - 仅管理 WebSocket 连接的 Worker 节点和心跳。
Agent 注册和路由决策由 AgentRegistry 统一负责。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from a2a.shared.types import WorkerNode, WorkerStatus


# 60 秒无心跳判定 offline，120 秒清除
HEARTBEAT_TIMEOUT_SECONDS = 60
CLEANUP_TIMEOUT_SECONDS = 120


class WorkerNotFoundError(Exception):
    pass


class WorkerRegistry:
    """内存中的 Worker 注册表（线程安全）。"""

    def __init__(self) -> None:
        self._workers: dict[str, WorkerNode] = {}
        self._lock = asyncio.Lock()

    def register(self, worker: WorkerNode) -> None:
        """注册或更新 Worker。"""
        worker.status = WorkerStatus.ONLINE
        worker.last_heartbeat = datetime.utcnow()
        self._workers[worker.worker_id] = worker

    def register_from_ws(self, worker_id: str, a2a_endpoint: str) -> WorkerNode:
        """从 WS 注册（仅连通性信息）。

        能力信息通过 A2A AgentCard 在 AgentRegistry 中维护。
        """
        worker = WorkerNode(
            worker_id=worker_id,
            a2a_endpoint=a2a_endpoint,
            status=WorkerStatus.ONLINE,
        )
        self._workers[worker_id] = worker
        return worker

    def unregister(self, worker_id: str) -> None:
        """注销 Worker。"""
        self._workers.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerNode:
        """根据 ID 获取 Worker。"""
        if worker_id not in self._workers:
            raise WorkerNotFoundError(worker_id)
        return self._workers[worker_id]

    def get_by_endpoint(self, endpoint: str) -> WorkerNode | None:
        """根据 A2A endpoint 查找 Worker。"""
        for worker in self._workers.values():
            if worker.a2a_endpoint == endpoint:
                return worker
        return None

    async def list_online(self) -> list[WorkerNode]:
        """列出所有在线 Worker。"""
        async with self._lock:
            self._check_heartbeats_unlocked()
            return [
                w for w in self._workers.values() if w.status == WorkerStatus.ONLINE
            ]

    async def select_worker(
        self, capability: str | None = None, agent_registry=None
    ) -> WorkerNode | None:
        """选择合适的 Worker（优先空闲的，可选按 capability 过滤）。

        Args:
            capability: 可选的能力过滤
            agent_registry: AgentRegistry 实例，用于查询 Agent 能力
        """
        async with self._lock:
            self._check_heartbeats_unlocked()
            online = [
                w for w in self._workers.values() if w.status == WorkerStatus.ONLINE
            ]
            if not online:
                return None
            if capability and agent_registry:
                for w in online:
                    try:
                        agent = agent_registry.get(w.worker_id)
                        if capability in agent.capabilities:
                            return w
                    except Exception:
                        continue
                return online[0]
            return online[0]

    def update_heartbeat(self, worker_id: str) -> None:
        """更新 Worker 心跳时间。"""
        if worker_id in self._workers:
            self._workers[worker_id].last_heartbeat = datetime.utcnow()
            self._workers[worker_id].status = WorkerStatus.ONLINE

    def mark_busy(self, worker_id: str) -> None:
        """标记 Worker 为忙碌。"""
        if worker_id in self._workers:
            self._workers[worker_id].status = WorkerStatus.BUSY

    def mark_idle(self, worker_id: str) -> None:
        """标记 Worker 为空闲。"""
        if worker_id in self._workers:
            self._workers[worker_id].status = WorkerStatus.ONLINE

    def get_worker_endpoint(self, worker_id: str) -> str | None:
        """获取 Worker 的 A2A endpoint。"""
        try:
            return self.get(worker_id).a2a_endpoint
        except WorkerNotFoundError:
            return None

    def _check_heartbeats_unlocked(self) -> None:
        """检查心跳，超时标记为 offline 或移除（必须在持锁时调用）。"""
        now = datetime.utcnow()
        to_remove: list[str] = []
        to_mark_offline: list[str] = []

        for worker in self._workers.values():
            if worker.status == WorkerStatus.OFFLINE:
                continue
            elapsed = (now - worker.last_heartbeat).total_seconds()
            if elapsed > CLEANUP_TIMEOUT_SECONDS:
                to_remove.append(worker.worker_id)
            elif elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                to_mark_offline.append(worker.worker_id)

        for worker_id in to_remove:
            self._workers.pop(worker_id, None)
        for worker_id in to_mark_offline:
            self._workers[worker_id].status = WorkerStatus.OFFLINE

    async def _check_heartbeats(self) -> None:
        """异步版本，供 cleanup task 调用。"""
        async with self._lock:
            self._check_heartbeats_unlocked()
