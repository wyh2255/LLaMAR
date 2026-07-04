"""TaskStore — agentic 编排模式的单次请求生命周期状态管理。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from a2a.coordinator.router import RouterAgent


@dataclass
class PlanNode:
    """DAG 计划中的一个任务节点。

    Attributes:
        task_id: 计划内唯一标识，如 "fetch-stock"。
        worker_id: 目标 Worker ID，None 表示未指定。
        description: 人类可读描述。
        depends_on: 依赖的 task_id 列表（来自前序节点）。
        status: 结构状态，"pending" 或 "skipped"。由 Agent 通过 update_plan 修改。
        state: 执行状态，"pending"|"running"|"done"|"failed"|"verified"。由系统自动回写。
        result: 任务结果摘要，由系统自动回填。
        retry_count: 重试次数。
    """

    task_id: str
    worker_id: str | None = None
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    state: str = "pending"
    result: str | None = None
    retry_count: int = 0


_global_future_registry: dict[str, asyncio.Future] = {}

# Mapping from worker-assigned task_id -> coordinator dispatch task_id
# Push callback arrives with worker task_id; we need to find the dispatch future
_worker_to_dispatch_map: dict[str, str] = {}


def resolve_global_future(task_id: str, result: Any) -> None:
    """由 push callback handler 调用，解析 dispatch_task 创建的 Future。

    先尝试直接查找 task_id；如果未找到，再通过 worker_to_dispatch 映射查找。
    """
    future = _global_future_registry.pop(task_id, None)
    if future is not None and not future.done():
        future.set_result(result)
        return

    # Try reverse mapping: push callback may carry worker's task_id
    dispatch_id = _worker_to_dispatch_map.pop(task_id, None)
    if dispatch_id is not None:
        future = _global_future_registry.pop(dispatch_id, None)
        if future is not None and not future.done():
            future.set_result(result)


class TaskStore:
    """Agentic 编排模式的单次请求生命周期状态管理。

    管理：持久化计划（plan）、异步任务 future、完整结果、派发计数。
    所有方法本 phase 暂为 NotImplementedError，后续 phase 实现。
    """

    def __init__(
        self,
        original_request: str,
        router: "RouterAgent",
        verifier: Any | None = None,
        max_tasks: int = 20,
        context_id: str | None = None,
    ) -> None:
        self.original_request = original_request
        self._router = router
        self._verifier = verifier
        self.max_tasks = max_tasks
        self.context_id = context_id
        self._plan: list[PlanNode] = []
        self._futures: dict[str, Any] = {}
        self._results: dict[str, str] = {}
        self._dispatched_count: int = 0
        self._worker_to_dispatch: dict[str, str] = {}
        self._dispatch_to_worker: dict[str, str] = {}

    def register_worker_task_id(
        self, dispatch_task_id: str, worker_task_id: str
    ) -> None:
        """Record mapping between worker-assigned task_id and dispatch task_id.

        Push notification arrives with the worker's task_id; this mapping
        lets resolve_global_future and respond_worker find the correct
        dispatch future / node.
        """
        self._worker_to_dispatch[worker_task_id] = dispatch_task_id
        self._dispatch_to_worker[dispatch_task_id] = worker_task_id
        _worker_to_dispatch_map[worker_task_id] = dispatch_task_id

    def resolve_dispatch_id(self, task_id: str) -> str | None:
        """Return the dispatch task_id for a worker task_id, or vice versa."""
        if self.get_node(task_id) is not None:
            return task_id
        return self._worker_to_dispatch.get(task_id)

    def register_future(self, task_id: str) -> asyncio.Future:
        """注册 Future，同时存入本地 _futures 和全局注册表。"""
        future = asyncio.get_running_loop().create_future()
        self._futures[task_id] = future
        _global_future_registry[task_id] = future
        return future

    @property
    def results(self) -> dict[str, str]:
        """已完成任务的完整结果（task_id → result_text）。"""
        return self._results

    @property
    def dispatched_count(self) -> int:
        return self._dispatched_count

    def get_plan(self) -> list[PlanNode]:
        """返回当前计划节点列表的浅拷贝。"""
        return list(self._plan)

    def get_node(self, task_id: str) -> PlanNode | None:
        """按 task_id 查找节点。

        Args:
            task_id: 目标 task_id。

        Returns:
            匹配的 PlanNode，未找到返回 None。
        """
        for node in self._plan:
            if node.task_id == task_id:
                return node
        return None

    def update_plan(self, plan: list[dict[str, Any]]) -> dict[str, Any]:
        """替换整个计划，返回 diff（added/removed/modified）。

        已执行节点的 state/result/retry_count 会被保留。
        """

        def _to_node(d: dict[str, Any]) -> PlanNode:
            return PlanNode(
                task_id=d["task_id"],
                worker_id=d.get("worker_id"),
                description=d.get("description", ""),
                depends_on=d.get("depends_on", []),
                status=d.get("status", "pending"),
            )

        new_nodes = [_to_node(d) for d in plan]
        old_ids = {n.task_id for n in self._plan}
        new_ids = {n.task_id for n in new_nodes}

        added = [n.task_id for n in new_nodes if n.task_id not in old_ids]
        removed = [n.task_id for n in self._plan if n.task_id not in new_ids]

        old_by_id = {n.task_id: n for n in self._plan}
        modified: list[str] = []
        for nn in new_nodes:
            on = old_by_id.get(nn.task_id)
            if on is None:
                continue
            if (
                nn.worker_id != on.worker_id
                or nn.description != on.description
                or nn.depends_on != on.depends_on
                or nn.status != on.status
            ):
                modified.append(nn.task_id)

        merged: list[PlanNode] = []
        for nn in new_nodes:
            on = old_by_id.get(nn.task_id)
            if on is not None:
                nn.state = on.state
                nn.result = on.result
                nn.retry_count = on.retry_count
            merged.append(nn)

        self._plan = merged
        return {
            "added": added,
            "removed": removed,
            "modified": modified,
            "nodes": len(self._plan),
            "edges": sum(len(n.depends_on) for n in self._plan),
            "pending": len([n for n in self._plan if n.state == "pending"]),
        }

    def add_adhoc_node(
        self,
        task_id: str,
        worker_id: str | None = None,
        description: str = "",
    ) -> PlanNode:
        """为未声明的 task_id 添加临时节点（advisory 模式）。

        幂等操作：如果同 task_id 的节点已存在，直接返回已有节点。

        Args:
            task_id: 节点唯一标识。
            worker_id: 目标 Worker ID。
            description: 人类可读描述。

        Returns:
            新增或已存在的 PlanNode。
        """
        existing = self.get_node(task_id)
        if existing is not None:
            return existing
        node = PlanNode(
            task_id=task_id,
            worker_id=worker_id,
            description=description,
        )
        self._plan.append(node)
        return node

    def set_state(
        self,
        task_id: str,
        state: str,
        result: str | None = None,
    ) -> None:
        """系统自动回写节点执行状态。

        如果 node 不存在则静默忽略（ad-hoc 节点可能尚未加入）。

        Args:
            task_id: 目标 task_id。
            state: 新状态（running/done/failed/verified）。
            result: 可选的结果文本。
        """
        node = self.get_node(task_id)
        if node is None:
            return
        node.state = state
        if result is not None:
            node.result = result

    @property
    def progress(self) -> dict[str, int]:
        """返回进度统计 {"done": N, "total": M}。"""
        total = len(self._plan)
        done = sum(1 for n in self._plan if n.state in ("done", "failed", "verified"))
        return {"done": done, "total": total}

    def mark_finished(self, success: bool) -> None:
        """标记整个编排请求为已完成。"""
        self._mission_success = success

    @property
    def mission_success(self) -> bool | None:
        """返回整体任务成功状态。"""
        return getattr(self, "_mission_success", None)
