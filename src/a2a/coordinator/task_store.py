"""TaskStore — agentic 编排模式的单次请求生命周期状态管理。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, TYPE_CHECKING

from a2a.coordinator.mission_graph import (
    MissionGraph,
    MissionGraphError,
    MissionNodeRuntime,
    MissionNodeSpec,
)
from a2a.coordinator.mission_runtime import MissionRuntime, normalize_physical_state

if TYPE_CHECKING:
    from a2a.coordinator.router import RouterAgent

logger = logging.getLogger(__name__)

DeferredActivationCallback = Callable[[str, str, str | None], Awaitable[Any]]


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


def _legacy_state_for_raw(raw_state: Any) -> str | None:
    normalized = normalize_physical_state(raw_state)
    if normalized is not None:
        return {
            "ACCEPTED": "running",
            "RUNNING": "running",
            "INPUT_REQUIRED": "running",
            "COMPLETED": "done",
            "FAILED": "failed",
            "CANCELED": "canceled",
        }.get(normalized.value)
    return {
        "SUBMITTED": "running",
        "WORKING": "running",
        "INPUT_REQUIRED": "running",
        "COMPLETED": "done",
        "FAILED": "failed",
        "REJECTED": "failed",
        "AUTH_REQUIRED": "failed",
        "CANCELED": "canceled",
        "CANCELLED": "canceled",
        "TASK_STATE_SUBMITTED": "running",
        "TASK_STATE_WORKING": "running",
        "TASK_STATE_INPUT_REQUIRED": "running",
        "TASK_STATE_COMPLETED": "done",
        "TASK_STATE_FAILED": "failed",
        "TASK_STATE_REJECTED": "failed",
        "TASK_STATE_AUTH_REQUIRED": "failed",
        "TASK_STATE_CANCELED": "canceled",
        "TASK_STATE_CANCELLED": "canceled",
    }.get(raw_state)


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
        self._logical_to_dispatch: dict[str, str] = {}
        # One-to-many logical -> physical dispatch ids (graph-managed activations).
        self._logical_to_dispatches: dict[str, list[str]] = {}
        # Physical dispatches that already contributed a first terminal aggregation.
        self._aggregated_terminal_dispatches: set[str] = set()
        self._runtime: MissionRuntime | None = None
        self._mission_graph: MissionGraph = MissionGraph()
        # Context-owned last-write-wins deferred assignment slot (one per worker).
        self._deferred_assignments: dict[str, dict[str, Any]] = {}
        self._deferred_activation_callback: DeferredActivationCallback | None = None
        self._deferred_activation_in_flight: set[str] = set()
        self._closed: bool = False
        # Critical section for dual-store mapping + exact-once aggregation.
        self._lock = RLock()

    def attach_runtime(self, runtime: MissionRuntime | None) -> None:
        """Attach the context-bound Phase 0 runtime owner."""
        if self._runtime is not None and self._runtime is not runtime:
            self._runtime.bind_status_owner(None)
        self._runtime = runtime
        if runtime is not None:
            runtime.bind_status_owner(self)

    def set_deferred_activation_callback(
        self, callback: DeferredActivationCallback | None
    ) -> None:
        """Inject the coroutine that dispatches a deferred assignment payload."""
        self._deferred_activation_callback = callback

    def enqueue_deferred_assignment(
        self,
        worker_id: str,
        content: str,
        related_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Store or coalesce a deferred assignment for ``worker_id`` (LWW, one slot)."""
        if self._closed:
            raise RuntimeError("task_store_closed")
        previous = self._deferred_assignments.get(worker_id)
        payload = {
            "worker_id": worker_id,
            "content": content,
            "related_task_id": related_task_id,
        }
        self._deferred_assignments[worker_id] = payload
        return {
            "coalesced": previous is not None,
            "replaced_deferred": previous is not None,
            "previous_content": previous.get("content") if previous else None,
        }

    def get_deferred_assignment(self, worker_id: str) -> dict[str, Any] | None:
        """Return the current deferred assignment payload, if any."""
        return self._deferred_assignments.get(worker_id)

    def clear_deferred_assignments(self) -> None:
        """Drop all deferred slots and cancel in-flight activation tracking."""
        self._deferred_assignments.clear()
        self._deferred_activation_in_flight.clear()

    def close(self) -> None:
        """Deactivate deferred work so late terminals cannot dispatch after teardown."""
        self._closed = True
        self.clear_deferred_assignments()
        self._deferred_activation_callback = None

    async def retry_deferred_activation(self, worker_id: str) -> bool:
        """Retry activating a retained deferred assignment (e.g. after transient fail)."""
        if self._closed:
            return False
        if worker_id in self._deferred_activation_in_flight:
            return False
        if not self.get_deferred_assignment(worker_id):
            return False
        if self.get_active_tasks_by_worker(worker_id):
            return False
        await self._activate_deferred_assignment(worker_id)
        return worker_id not in self._deferred_assignments

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
        if self._runtime is None:
            _worker_to_dispatch_map[worker_task_id] = dispatch_task_id
        else:
            self._runtime.register_worker_task(dispatch_task_id, worker_task_id)

    def create_physical_dispatch(self, logical_id: str, worker_id: str):
        """Allocate and register a physical dispatch before network I/O."""
        if self._runtime is None:
            return None
        dispatch = self._runtime.create_dispatch(logical_id, worker_id)
        self._logical_to_dispatch[logical_id] = dispatch.dispatch_id
        self._logical_to_dispatches.setdefault(logical_id, [])
        if dispatch.dispatch_id not in self._logical_to_dispatches[logical_id]:
            self._logical_to_dispatches[logical_id].append(dispatch.dispatch_id)
        return dispatch

    def resolve_dispatch_id(self, task_id: str) -> str | None:
        """Resolve only an opaque physical ID or known Worker task ID."""
        if self._runtime is not None:
            if self._runtime.get_dispatch(task_id) is not None:
                return task_id
            dispatch = self._runtime.resolve_worker_task(task_id)
            return dispatch.dispatch_id if dispatch is not None else None
        if task_id in self._dispatch_to_worker:
            return task_id
        return self._worker_to_dispatch.get(task_id)

    def resolve_historical_dispatch_id(self, task_id: str) -> str | None:
        """Resolve a physical dispatch that was cleaned up but did exist.

        The active runtime retains every allocated dispatch id, so a
        known-but-no-longer-active id (rolled back while PREPARED, or a worker
        task id of a removed dispatch) resolves for idempotent control actions
        instead of surfacing as a genuine unknown id.  An id that was never
        allocated stays None.
        """
        if self._runtime is None:
            return None
        dispatch = self._runtime.resolve_historical_dispatch(task_id)
        return dispatch.dispatch_id if dispatch is not None else None

    def resolve_compat_dispatch_id(self, logical_id: str) -> str | None:
        """Bounded presentation compatibility, separate from physical lookup.

        Returns a physical ID only when the logical node maps to exactly one
        dispatch; multi-participant mappings never pick an arbitrary member.
        """
        multi = self._logical_to_dispatches.get(logical_id) or []
        if len(multi) == 1:
            dispatch_id = multi[0]
            if self.get_dispatch(dispatch_id) is not None:
                return dispatch_id
            return None
        if len(multi) > 1:
            return None
        # Legacy single-dispatch path used by create_physical_dispatch.
        dispatch_id = self._logical_to_dispatch.get(logical_id)
        if dispatch_id and self.get_dispatch(dispatch_id) is not None:
            return dispatch_id
        return None

    def get_node_for_dispatch(self, dispatch_id: str) -> PlanNode | None:
        """Project a physical dispatch onto its logical PlanNode."""
        dispatch = self.get_dispatch(dispatch_id)
        return self.get_node(dispatch.logical_node_id) if dispatch is not None else None

    def replace_mission_graph(
        self, specs: Sequence[MissionNodeSpec | dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace the logical MissionGraph after full validation."""
        return self._mission_graph.replace(specs)

    def set_mission_graph_history_sink(self, sink: Any) -> None:
        """Attach a replace-history observer to the MissionGraph."""
        self._mission_graph.set_history_sink(sink)

    @property
    def mission_node_count(self) -> int:
        """Number of nodes declared in the MissionGraph."""
        return (
            len(self._mission_graph._nodes)
            if hasattr(self._mission_graph, "_nodes")
            else 0
        )  # noqa: SLF001

    @property
    def mission_node_ids(self) -> list[str]:
        """Sorted list of logical IDs declared in the MissionGraph."""
        return (
            sorted(self._mission_graph._nodes)
            if hasattr(self._mission_graph, "_nodes")
            else []
        )  # noqa: SLF001

    def get_mission_node(self, logical_id: str) -> MissionNodeRuntime | None:
        """Logical namespace lookup: MissionGraph runtime only (not PlanNode)."""
        return self._mission_graph.get_node(logical_id)

    def get_dispatch(self, dispatch_id: str):
        """Physical namespace lookup delegated to the active runtime."""
        return self._runtime.get_dispatch(dispatch_id) if self._runtime else None

    def get_historical_dispatch(self, dispatch_id: str):
        """Look up a dispatch that may have been cleaned up already.

        The active runtime retains every allocated physical id, so a
        rolled-back/removed dispatch (never terminal, still PREPARED) or a
        terminal dispatch whose record survives can be inspected for idempotent
        control actions.  Returns None for ids that never existed.
        """
        if self._runtime is None:
            return None
        return self._runtime.resolve_historical_dispatch(dispatch_id)

    def get_worker_task_id(self, dispatch_id: str) -> str | None:
        """Read the worker task ID from the physical dispatch authority."""
        if self._runtime is not None:
            dispatch = self._runtime.get_dispatch(dispatch_id)
            return dispatch.worker_task_id if dispatch is not None else None
        return self._dispatch_to_worker.get(dispatch_id)

    def list_dispatches_for_mission(self, logical_id: str) -> list[Any]:
        """List physical dispatches belonging to one logical node."""
        if self._runtime is None:
            return []
        mapped = self._logical_to_dispatches.get(logical_id)
        if mapped is not None:
            return [
                dispatch
                for dispatch_id in mapped
                if (dispatch := self._runtime.get_dispatch(dispatch_id)) is not None
            ]
        return [
            dispatch
            for dispatch in self._runtime.dispatches.values()
            if dispatch.logical_node_id == logical_id
        ]

    def classify_logical_node_dispatches(
        self, logical_id: str
    ) -> dict[str, Any] | None:
        """Classify a MissionGraph logical node's physical dispatches.

        Powers idempotent cancel/reply of an activated logical node whose
        dispatches are no longer live: a logical node that HAS physical
        dispatch bindings (all terminal or cleaned up) is a real orchestration
        object, never an unknown id.  Returns None when ``logical_id`` is not a
        declared graph node with any physical dispatch binding, otherwise:
          {"kind": "active", "dispatch_id": <id>} — at least one dispatch is
            still live (non-terminal); the caller must take the normal remote
            path for it, never an idempotent success while worker work is live.
          {"kind": "terminal", "state": <name>, "dispatch_id": <id>,
           "count": <n>} — every dispatch either reached a terminal state or
            was cleaned up (rolled back while PREPARED); cancel/reply is an
            idempotent success.
        """
        if self._runtime is None:
            return None
        node = self._mission_graph.get_node(logical_id)
        if node is None:
            return None

        dispatch_ids: list[str] = []
        for dispatch_id in self._logical_to_dispatches.get(logical_id) or []:
            if dispatch_id not in dispatch_ids:
                dispatch_ids.append(dispatch_id)
        for dispatch_id in (node.dispatch_ids or {}).values():
            if dispatch_id not in dispatch_ids:
                dispatch_ids.append(dispatch_id)
        legacy = self._logical_to_dispatch.get(logical_id)
        if legacy and legacy not in dispatch_ids:
            dispatch_ids.append(legacy)
        # Restored runtimes may hold the binding only on the physical records;
        # scan live + retained-history records as the authoritative fallback.
        for dispatch in list(self._runtime.dispatches.values()) + list(
            getattr(self._runtime, "_dispatch_history", {}).values()
        ):
            if (
                dispatch.logical_node_id == logical_id
                and dispatch.dispatch_id not in dispatch_ids
            ):
                dispatch_ids.append(dispatch.dispatch_id)
        if not dispatch_ids:
            return None

        active: list[str] = []
        terminal_states: list[str] = []
        for dispatch_id in dispatch_ids:
            dispatch = self.get_historical_dispatch(dispatch_id)
            if dispatch is None:
                continue
            live = self._runtime.get_dispatch(dispatch_id) is not None
            state = getattr(dispatch.state, "value", str(dispatch.state or ""))
            if live and not dispatch.state.terminal:
                active.append(dispatch_id)
            else:
                terminal_states.append(state)
        if active:
            return {"kind": "active", "dispatch_id": active[0]}
        if terminal_states:
            return {
                "kind": "terminal",
                "state": terminal_states[0],
                "dispatch_id": dispatch_ids[0],
                "count": len(dispatch_ids),
            }
        return None

    def create_dispatches_for_activation(
        self, logical_id: str, participant_ids: list[str]
    ) -> list[Any]:
        """Allocate opaque physical dispatches for one activation.

        Validates MissionGraph membership and exact unique fan-out, preallocates
        all PREPARED records before any network I/O, and attaches graph bindings
        atomically. Failures after allocation roll back still-PREPARED runtime
        records and leave TaskStore/graph mappings unchanged.
        """
        if self._runtime is None:
            raise RuntimeError("mission_runtime_not_attached")

        with self._lock:
            node = self._mission_graph.get_node(logical_id)
            if node is None:
                raise MissionGraphError(f"node_not_found: {logical_id}")
            if node.state in {"completed", "failed", "canceled"}:
                raise MissionGraphError(f"node {logical_id} is terminal")
            if node.dispatch_ids:
                raise MissionGraphError(
                    f"node {logical_id} already has dispatch bindings"
                )
            if self._logical_to_dispatches.get(logical_id):
                raise MissionGraphError(
                    f"node {logical_id} already has dispatch bindings"
                )
            if not participant_ids:
                raise MissionGraphError("participant_ids must be non-empty")
            if len(participant_ids) != len(set(participant_ids)):
                raise MissionGraphError(
                    f"participant_ids must be unique for exact fan-out of {logical_id}"
                )
            declared = list(node.participant_ids)
            if set(participant_ids) != set(declared):
                raise MissionGraphError(
                    f"participant_ids must exactly equal declared participants "
                    f"of {logical_id}"
                )

            # Preallocate opaque IDs; mapping/graph attach only after full success.
            dispatches = self._runtime.create_dispatches(
                logical_id, list(participant_ids)
            )
            allocated_ids = [dispatch.dispatch_id for dispatch in dispatches]
            try:
                bindings = {
                    dispatch.worker_id: dispatch.dispatch_id for dispatch in dispatches
                }
                self._mission_graph.attach_dispatches(logical_id, bindings)
                self._logical_to_dispatches[logical_id] = list(allocated_ids)
                # Presentation helper remains single-valued only for unambiguous cases.
                if len(dispatches) == 1:
                    self._logical_to_dispatch[logical_id] = dispatches[0].dispatch_id
                else:
                    self._logical_to_dispatch.pop(logical_id, None)
            except Exception:
                # All-or-none: runtime-owned rollback of this still-PREPARED batch.
                self._runtime.rollback_prepared_dispatches(allocated_ids)
                self._logical_to_dispatches.pop(logical_id, None)
                if len(allocated_ids) == 1:
                    # Only clear single-valued helper if it pointed at this batch.
                    if self._logical_to_dispatch.get(logical_id) in allocated_ids:
                        self._logical_to_dispatch.pop(logical_id, None)
                raise
            return dispatches

    def apply_physical_status(
        self,
        dispatch_id: str,
        raw_state: Any,
        source: str,
        observed_at: Any | None = None,
        result: Any | None = None,
    ):
        """Canonical physical status entry point for sync/callback/cancel."""
        if self._runtime is None:
            return None
        # Hold TaskStore lock across state-before / physical apply / first-terminal
        # aggregation bookkeeping so concurrent callback/sync cannot double-aggregate.
        with self._lock:
            state_before = None
            existing = self._runtime.get_dispatch(dispatch_id)
            if existing is not None:
                state_before = existing.state
            dispatch = self._runtime._apply_physical_status(  # noqa: SLF001
                dispatch_id,
                raw_state,
                source=source,
                observed_at=observed_at,
                result=result,
            )
            if dispatch is not None:
                first_terminal = (
                    dispatch.state.terminal
                    and (state_before is None or not state_before.terminal)
                    and dispatch_id not in self._aggregated_terminal_dispatches
                )
                if first_terminal:
                    self._aggregate_graph_terminal(dispatch, result=result)
                self._project_physical_status(dispatch)
                # Runtime abort is terminal teardown, not normal worker availability.
                # Never revive queued work while abort() is resolving remote tasks.
                if dispatch.state.terminal and not source.startswith("abort"):
                    self._schedule_deferred_activation(dispatch.worker_id)
            return dispatch

    def _aggregate_graph_terminal(
        self, dispatch: Any, *, result: Any | None = None
    ) -> None:
        """Invoke MissionGraph aggregation exactly once per physical terminal."""
        # Caller must hold self._lock (apply_physical_status critical section).
        if dispatch.dispatch_id in self._aggregated_terminal_dispatches:
            return
        node = self._mission_graph.get_node(dispatch.logical_node_id)
        if node is None:
            return
        # Only aggregate when this physical dispatch is attached to the graph node.
        if dispatch.dispatch_id not in node.dispatch_ids.values():
            return
        self._aggregated_terminal_dispatches.add(dispatch.dispatch_id)
        terminal_value = (
            dispatch.state.value
            if hasattr(dispatch.state, "value")
            else str(dispatch.state)
        )
        evidence = result if result is not None else dispatch.result
        if evidence is None:
            evidence = dispatch.artifact
        self._mission_graph.mark_dispatch_terminal(
            dispatch.logical_node_id,
            dispatch.worker_id,
            terminal_value,
            result=evidence,
        )

    def _project_physical_status(self, dispatch: Any) -> None:
        """Derive the single legacy PlanNode view from physical dispatches."""
        node = self.get_node(dispatch.logical_node_id)
        if node is None or self._runtime is None:
            return

        siblings = [
            item
            for item in self._runtime.dispatches.values()
            if item.logical_node_id == dispatch.logical_node_id
        ]
        if any(item.state.value == "FAILED" for item in siblings):
            compatibility_state = "failed"
        elif siblings and all(item.state.terminal for item in siblings):
            compatibility_state = (
                "canceled"
                if any(item.state.value == "CANCELED" for item in siblings)
                else "done"
            )
        elif any(item.state.value != "PREPARED" for item in siblings):
            compatibility_state = "running"
        else:
            compatibility_state = "pending"

        node.state = compatibility_state
        terminal_dispatches = [item for item in siblings if item.state.terminal]
        latest = max(
            terminal_dispatches or siblings,
            key=lambda item: (item.finalization_seq, item.created_at),
        )
        for item in terminal_dispatches:
            evidence = item.result if item.result is not None else item.artifact
            if evidence is not None:
                self._results[item.dispatch_id] = str(evidence)

        latest_evidence = (
            latest.result if latest.result is not None else latest.artifact
        )
        if latest_evidence is not None and terminal_dispatches:
            node.result = str(latest_evidence)
            self._results[node.task_id] = node.result

    def _schedule_deferred_activation(self, worker_id: str) -> None:
        """Schedule deferred activation on the current running event loop only."""
        if self._closed:
            return
        if worker_id in self._deferred_activation_in_flight:
            return
        if not self.get_deferred_assignment(worker_id):
            return
        if self.get_active_tasks_by_worker(worker_id):
            return
        if self._deferred_activation_callback is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "deferred activation for worker=%s deferred: no running event loop",
                worker_id,
            )
            return

        self._deferred_activation_in_flight.add(worker_id)

        async def _runner() -> None:
            try:
                await self._activate_deferred_assignment(worker_id)
            finally:
                self._deferred_activation_in_flight.discard(worker_id)

        loop.create_task(_runner())

    async def _activate_deferred_assignment(self, worker_id: str) -> None:
        """Activate the current LWW deferred payload via the injected callback."""
        if self._closed:
            return
        item = self.get_deferred_assignment(worker_id)
        if item is None:
            return
        if self.get_active_tasks_by_worker(worker_id):
            return
        callback = self._deferred_activation_callback
        if callback is None:
            return

        content = item["content"]
        related_task_id = item.get("related_task_id")
        try:
            result = await callback(worker_id, content, related_task_id)
        except Exception:
            logger.exception(
                "deferred activation failed for worker=%s; retaining deferred slot",
                worker_id,
            )
            return

        success = True
        if result is not None and hasattr(result, "success"):
            success = bool(result.success)
        if not success:
            logger.warning(
                "deferred activation returned failure for worker=%s; retaining slot",
                worker_id,
            )
            return

        current = self.get_deferred_assignment(worker_id)
        if (
            current is not None
            and current.get("content") == content
            and current.get("related_task_id") == related_task_id
        ):
            self._deferred_assignments.pop(worker_id, None)
        # If a newer LWW payload arrived during activation and worker is idle, reschedule.
        if (
            not self._closed
            and self.get_deferred_assignment(worker_id) is not None
            and not self.get_active_tasks_by_worker(worker_id)
        ):
            self._schedule_deferred_activation(worker_id)

    def register_future(self, task_id: str) -> asyncio.Future:
        """Register a Future in the physical runtime or legacy local registry."""
        if self._runtime is not None:
            if self._runtime.get_dispatch(task_id) is None:
                raise KeyError(task_id)
            future = self._runtime.register_future(task_id)
            self._futures[task_id] = future
            return future
        future = asyncio.get_running_loop().create_future()
        self._futures[task_id] = future
        _global_future_registry[task_id] = future

        def _consume_legacy_exception(done: asyncio.Future) -> None:
            if not done.cancelled():
                done.exception()

        future.add_done_callback(_consume_legacy_exception)
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

    def get_active_tasks_by_worker(self, worker_id: str) -> list[str]:
        """返回指定 worker 的活跃任务 ID 列表（running/pending/dispatched）。"""
        if self._runtime is not None:
            return [
                dispatch.dispatch_id
                for dispatch in self._runtime.dispatches.values()
                if dispatch.worker_id == worker_id and not dispatch.state.terminal
            ]
        active = []
        for node in self._plan:
            if node.worker_id == worker_id and node.state in (
                "running",
                "pending",
                "dispatched",
            ):
                active.append(node.task_id)
        return active

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

        # NEW: 自动标记 removed 任务为 canceled 并保留在 plan 中
        for task_id in removed:
            node = old_by_id.get(task_id)
            if node is not None and node.state not in (
                "done",
                "failed",
                "verified",
                "canceled",
            ):
                node.state = "canceled"
                # 从 _results 中移除（避免干扰结果查询）
                self._results.pop(task_id, None)
            # 保留在 plan 中，标记为 canceled 而非直接移除
            if node is not None and task_id not in new_ids:
                merged.append(node)

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
            self._results[task_id] = result

    def cancel_dispatch(self, dispatch_id: str, *, reason: str = "cancel") -> bool:
        """Cancel one physical dispatch through the runtime fence."""
        return (
            self._runtime.cancel_dispatch(dispatch_id, reason=reason)
            if self._runtime is not None
            else False
        )

    def sync_task_states(self, worker_states: dict[str, str]) -> list[str]:
        """同步 worker 端任务状态到 coordinator plan。

        遍历 plan 中所有节点，将 worker 端实际状态回写到 plan node，
        解决 push notification 未送达导致的状态不一致问题。

        Args:
            worker_states: {task_id: state} 从 worker 端查询到的实际状态

        Returns:
            状态发生变化的 task_id 列表
        """
        changed: list[str] = []
        terminal_states = {"done", "failed", "verified", "canceled"}

        if self._runtime is not None:
            for dispatch in self._runtime.dispatches.values():
                lookup_ids = [dispatch.dispatch_id]
                if dispatch.worker_task_id:
                    lookup_ids.insert(0, dispatch.worker_task_id)
                raw_state = next(
                    (
                        worker_states[identifier]
                        for identifier in lookup_ids
                        if identifier in worker_states
                    ),
                    None,
                )
                if raw_state is None:
                    continue
                actual_state = _legacy_state_for_raw(raw_state)
                if actual_state is None:
                    continue
                physical_before = dispatch.state
                before = (
                    self.get_node(dispatch.logical_node_id).state
                    if self.get_node(dispatch.logical_node_id) is not None
                    else None
                )
                self.apply_physical_status(
                    dispatch.dispatch_id, raw_state, source="sync"
                )
                node = self.get_node(dispatch.logical_node_id)
                if node is None:
                    continue
                if physical_before != dispatch.state or before != node.state:
                    changed.append(node.task_id)
            return changed

        for node in self._plan:
            if node.task_id in worker_states:
                raw_state = worker_states[node.task_id]
                actual_state = _legacy_state_for_raw(raw_state)
                if actual_state is None:
                    continue
                if node.state != actual_state:
                    if node.state in terminal_states:
                        continue
                    node.state = actual_state
                    changed.append(node.task_id)
        return changed

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
