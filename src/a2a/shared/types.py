"""共享数据模型 - Coordinator 和 Worker 共同使用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class WorkerStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkerNode:
    """Worker 节点注册信息（仅连通性）。

    能力信息通过 A2A AgentCard 在 AgentRegistry 中维护。
    """

    worker_id: str
    a2a_endpoint: str  # "http://{host}:{port}/"
    status: WorkerStatus = WorkerStatus.OFFLINE
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)

    @property
    def endpoint(self) -> str:
        return self.a2a_endpoint


@dataclass
class DistributedTask:
    """分布式任务。"""

    task_id: str
    task_type: str
    prompt: str
    assigned_worker: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: Any | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    context_id: str | None = None


# === WebSocket 消息协议 ===


@dataclass
class WSMessage:
    """WebSocket 消息信封。"""

    type: str
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


WS_REGISTER = "register"
WS_HEARTBEAT = "heartbeat"
WS_TASK_PROGRESS = "task_progress"

WS_CANCEL_TASK = "cancel_task"
WS_SHUTDOWN = "shutdown"
WS_RELAY_A2A = "relay_a2a"


def build_ws_register_payload(
    worker_id: str,
    a2a_endpoint: str,
) -> dict[str, Any]:
    """构建 WS_REGISTER 消息 payload（仅连通性信息）。

    Worker 的能力信息通过 A2A AgentCard 获取。
    """
    return {
        "worker_id": worker_id,
        "a2a_endpoint": a2a_endpoint,
    }


def build_ws_heartbeat_payload(worker_id: str) -> dict[str, Any]:
    return {"worker_id": worker_id}


def build_direct_connect_info(
    relay: bool, target_endpoint: str, target_worker_id: str | None = None
) -> dict[str, Any]:
    return {
        "relay": relay,
        "target_endpoint": target_endpoint,
        "target_worker_id": target_worker_id,
    }
