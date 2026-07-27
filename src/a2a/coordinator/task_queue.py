"""任务队列 - 内存任务队列，支持入队、认领，完成、失败。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from a2a.shared.types import DistributedTask, TaskStatus


class TaskNotFoundError(Exception):
    pass


class InvalidStatusTransitionError(ValueError):
    """无效的任务状态转换。"""

    pass


# 允许的状态转换映射
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
}


class TaskQueue:
    """内存任务队列（非线程安全，仅用于单进程 async 上下文）。"""

    def __init__(
        self,
        max_history: int = 1000,
        on_cleanup: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._tasks: dict[str, DistributedTask] = {}
        self._max_history = max_history
        self.on_cleanup = on_cleanup

    def _validate_transition(
        self, task: DistributedTask, new_status: TaskStatus
    ) -> None:
        """验证状态转换是否合法。"""
        valid_next = VALID_TRANSITIONS.get(task.status, set())
        if new_status not in valid_next:
            raise InvalidStatusTransitionError(
                f"invalid transition from {task.status.value} to {new_status.value}"
            )

    def enqueue(self, task: DistributedTask) -> None:
        self._tasks[task.task_id] = task

    def start(self, task_id: str) -> DistributedTask:
        """将 PENDING 任务标记为 RUNNING（Coordinator 开始处理）。"""
        task = self._get_task(task_id)
        self._validate_transition(task, TaskStatus.RUNNING)
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now(timezone.utc)
        return task

    def complete(self, task_id: str, result: Any) -> DistributedTask:
        task = self._get_task(task_id)
        self._validate_transition(task, TaskStatus.COMPLETED)
        task.status = TaskStatus.COMPLETED
        task.result = result
        task.updated_at = datetime.now(timezone.utc)
        self._cleanup_old_tasks()
        return task

    def fail(self, task_id: str, error: str) -> DistributedTask:
        task = self._get_task(task_id)
        self._validate_transition(task, TaskStatus.FAILED)
        task.status = TaskStatus.FAILED
        task.error = error
        task.updated_at = datetime.now(timezone.utc)
        self._cleanup_old_tasks()
        return task

    def cancel(self, task_id: str) -> DistributedTask:
        task = self._get_task(task_id)
        self._validate_transition(task, TaskStatus.CANCELLED)
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now(timezone.utc)
        self._cleanup_old_tasks()
        return task

    def get(self, task_id: str) -> DistributedTask:
        return self._get_task(task_id)

    def list_pending(self) -> list[DistributedTask]:
        return [t for t in self._tasks.values() if t.status == TaskStatus.PENDING]

    def list_by_worker(self, worker_id: str) -> list[DistributedTask]:
        return [t for t in self._tasks.values() if t.assigned_worker == worker_id]

    def list_by_context(self, context_id: str) -> list[DistributedTask]:
        """Return all coordinator tasks owned by one A2A context."""
        return [t for t in self._tasks.values() if t.context_id == context_id]

    def list_running_by_worker(self, worker_id: str) -> list[DistributedTask]:
        return [
            t
            for t in self._tasks.values()
            if t.assigned_worker == worker_id and t.status == TaskStatus.RUNNING
        ]

    def _get_task(self, task_id: str) -> DistributedTask:
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        return self._tasks[task_id]

    def _cleanup_old_tasks(self) -> None:
        if len(self._tasks) <= self._max_history:
            return
        terminal_states = {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        candidates = [
            (tid, t) for tid, t in self._tasks.items() if t.status in terminal_states
        ]
        if len(candidates) <= self._max_history // 2:
            return
        candidates.sort(key=lambda x: x[1].updated_at)
        to_remove = candidates[: len(candidates) - self._max_history // 2]
        removed_ids = []
        for tid, _ in to_remove:
            self._tasks.pop(tid, None)
            removed_ids.append(tid)
        if self.on_cleanup is not None and removed_ids:
            try:
                self.on_cleanup(removed_ids)
            except Exception:
                pass
