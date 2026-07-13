"""SupervisionStateStore — 持久化任务监督状态。

为 TaskWatchdog 提供独立、持久化的监督状态存储，与 EventStore 的普通事件流解耦。
保存每个任务的当前监督状态、时间戳、未确认 actionable event 和已确认事件 ID。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SupervisionState:
    """单个任务的监督状态。

    时间戳统一使用 monotonic time 用于运行期比较，同时保存 wall-clock
    timestamp 用于持久化和日志分析。
    """

    dispatch_id: str
    worker_id: str = ""
    worker_task_id: str = ""
    created_at: float = 0.0
    created_at_wall: float = 0.0
    last_contact_at: float = 0.0
    last_progress_at: float = 0.0
    last_state_change_at: float = 0.0
    supervision_state: str = "HEALTHY"
    active_alerts: dict[str, str] = field(default_factory=dict)
    unacknowledged_events: list[dict[str, Any]] = field(default_factory=list)
    acknowledged_event_ids: set[str] = field(default_factory=set)
    terminal: bool = False
    stale_threshold_seconds: float = 120.0
    unreachable_threshold_seconds: float = 120.0
    deadline_warning_seconds: float = 300.0
    hard_deadline_seconds: float = 600.0
    grace_period_seconds: float = 10.0
    last_progress_step: int = 0
    last_metrics: dict[str, Any] = field(default_factory=dict)
    event_counter: int = 0

    def next_event_id(self) -> str:
        self.event_counter += 1
        return f"{self.dispatch_id}::{self.event_counter}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "worker_id": self.worker_id,
            "worker_task_id": self.worker_task_id,
            "created_at": self.created_at,
            "created_at_wall": self.created_at_wall,
            "last_contact_at": self.last_contact_at,
            "last_progress_at": self.last_progress_at,
            "last_state_change_at": self.last_state_change_at,
            "supervision_state": self.supervision_state,
            "active_alerts": dict(self.active_alerts),
            "unacknowledged_events": list(self.unacknowledged_events),
            "acknowledged_event_ids": list(self.acknowledged_event_ids),
            "terminal": self.terminal,
            "stale_threshold_seconds": self.stale_threshold_seconds,
            "unreachable_threshold_seconds": self.unreachable_threshold_seconds,
            "deadline_warning_seconds": self.deadline_warning_seconds,
            "hard_deadline_seconds": self.hard_deadline_seconds,
            "grace_period_seconds": self.grace_period_seconds,
            "last_progress_step": self.last_progress_step,
            "last_metrics": dict(self.last_metrics),
            "event_counter": self.event_counter,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SupervisionState":
        state = cls(
            dispatch_id=data.get("dispatch_id", ""),
            worker_id=data.get("worker_id", ""),
            worker_task_id=data.get("worker_task_id", ""),
            created_at=data.get("created_at", 0.0),
            created_at_wall=data.get("created_at_wall", 0.0),
            last_contact_at=data.get("last_contact_at", 0.0),
            last_progress_at=data.get("last_progress_at", 0.0),
            last_state_change_at=data.get("last_state_change_at", 0.0),
            supervision_state=data.get("supervision_state", "HEALTHY"),
            active_alerts=dict(data.get("active_alerts", {})),
            unacknowledged_events=list(data.get("unacknowledged_events", [])),
            acknowledged_event_ids=set(data.get("acknowledged_event_ids", [])),
            terminal=data.get("terminal", False),
            stale_threshold_seconds=data.get("stale_threshold_seconds", 120.0),
            unreachable_threshold_seconds=data.get(
                "unreachable_threshold_seconds", 120.0
            ),
            deadline_warning_seconds=data.get("deadline_warning_seconds", 300.0),
            hard_deadline_seconds=data.get("hard_deadline_seconds", 600.0),
            grace_period_seconds=data.get("grace_period_seconds", 10.0),
            last_progress_step=data.get("last_progress_step", 0),
            last_metrics=dict(data.get("last_metrics", {})),
            event_counter=data.get("event_counter", 0),
        )
        return state


class SupervisionStateStore:
    """独立、持久化的任务监督状态存储。

    线程安全：所有操作通过 _lock 保护。支持 NDJSON 持久化。
    """

    def __init__(self, log_dir: str | None = None) -> None:
        self._states: dict[str, SupervisionState] = {}
        self._lock = threading.Lock()
        self._log_dir = log_dir

    def set_log_dir(self, log_dir: str | None) -> None:
        self._log_dir = log_dir

    def get_or_create(
        self,
        dispatch_id: str,
        *,
        worker_id: str = "",
        worker_task_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> SupervisionState:
        """获取或创建任务的监督状态。"""
        config = config or {}
        with self._lock:
            state = self._states.get(dispatch_id)
            if state is None:
                now = time.monotonic()
                state = SupervisionState(
                    dispatch_id=dispatch_id,
                    worker_id=worker_id,
                    worker_task_id=worker_task_id,
                    created_at=now,
                    created_at_wall=time.time(),
                    last_contact_at=now,
                    last_progress_at=now,
                    last_state_change_at=now,
                    stale_threshold_seconds=config.get(
                        "stale_threshold_seconds", 120.0
                    ),
                    unreachable_threshold_seconds=config.get(
                        "unreachable_threshold_seconds", 120.0
                    ),
                    deadline_warning_seconds=config.get(
                        "deadline_warning_seconds", 300.0
                    ),
                    hard_deadline_seconds=config.get("hard_deadline_seconds", 600.0),
                    grace_period_seconds=config.get("grace_period_seconds", 10.0),
                )
                self._states[dispatch_id] = state
            return state

    def get(self, dispatch_id: str) -> SupervisionState | None:
        with self._lock:
            return self._states.get(dispatch_id)

    def update(self, dispatch_id: str, state: SupervisionState) -> None:
        """更新状态并持久化。"""
        with self._lock:
            self._states[dispatch_id] = state
        self._persist(state)

    def _persist(self, state: SupervisionState) -> None:
        if self._log_dir is None:
            return
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            path = os.path.join(
                self._log_dir, f"supervision_{state.dispatch_id}.ndjson"
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "state": state.to_dict(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as e:
            logger.warning("SupervisionStateStore NDJSON write failed: %s", e)

    def list_states(self) -> list[SupervisionState]:
        with self._lock:
            return list(self._states.values())

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def acknowledge_event(self, dispatch_id: str, event_id: str) -> None:
        """Coordinator 确认已处理某个 actionable event。"""
        with self._lock:
            state = self._states.get(dispatch_id)
            if state is None:
                return
            state.acknowledged_event_ids.add(event_id)
            state.unacknowledged_events = [
                e for e in state.unacknowledged_events if e.get("event_id") != event_id
            ]
            state.active_alerts = {
                k: v for k, v in state.active_alerts.items() if v != event_id
            }

    def snapshot(self) -> dict[str, SupervisionState]:
        """返回当前所有监督状态的快照。"""
        with self._lock:
            return dict(self._states)
