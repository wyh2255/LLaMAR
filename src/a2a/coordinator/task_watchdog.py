"""TaskWatchdog — 任务监督组件。

在 Coordinator 所在 asyncio event loop 中作为单个 asyncio.Task 运行，
检测任务停滞、Worker 不可达和 deadline 超限。只生成去重后的 actionable event，
不自动取消或重派任务，也不周期调用 LLM。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from a2a.coordinator.supervision_state_store import SupervisionState, SupervisionStateStore

if TYPE_CHECKING:
    from a2a.coordinator.worker_registry import WorkerRegistry
    from a2a.coordinator.task_store import TaskStore
    from a2a.coordinator.event_store import EventStore

logger = logging.getLogger(__name__)


@dataclass
class WatchdogConfig:
    """TaskWatchdog 系统配置。所有阈值由系统给出，不由 LLM 创建。"""

    task_stale_seconds: float = 120.0
    worker_unreachable_seconds: float = 120.0
    deadline_warning_seconds: float = 300.0
    task_hard_deadline_seconds: float = 600.0
    watchdog_tick_seconds: float = 5.0
    grace_period_seconds: float = 10.0
    no_progress_step_threshold: int = 3


class TaskWatchdog:
    """任务监督 watchdog。

    约束：
    - 第一版固定在 Coordinator event loop 内运行，不跨线程访问 TaskStore/WorkerRegistry。
    - 只生成去重后的 actionable event，不自动取消或重派任务。
    - 不实现 WakeQueue；告警由 Coordinator 既有编排循环在下一次 pre_llm 时读取。
    - terminal task 不再产生 stale/deadline 事件。
    """

    def __init__(
        self,
        worker_registry: "WorkerRegistry",
        event_store: "EventStore",
        supervision_store: "SupervisionStateStore",
        barrier=None,  # Optional barrier for domain delta detection
        run_control=None,  # Optional EnvironmentRunControl for env step
        config: WatchdogConfig | None = None,
    ) -> None:
        self._registry = worker_registry
        self._event_store = event_store
        self._supervision_store = supervision_store
        self._barrier = barrier
        self._run_control = run_control
        self._config = config or WatchdogConfig()
        self._task_store: "TaskStore | None" = None
        self._task: asyncio.Task | None = None
        self._stopped = True
        self._last_check_at: float = 0.0
        self._last_check_latency_ms: float = 0.0
        self._runtime = None

    def set_task_store(self, task_store: "TaskStore | None") -> None:
        """Attach the per-request TaskStore once it is created."""
        self._task_store = task_store

    def set_runtime(self, runtime) -> None:
        """Attach the canonical context-bound lifecycle owner."""
        self._runtime = runtime

    async def start(self) -> None:
        """启动 watchdog ticker。"""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "TaskWatchdog started (tick=%.1fs)", self._config.watchdog_tick_seconds
        )

    async def stop(self) -> None:
        """停止 watchdog ticker。"""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("TaskWatchdog stopped")

    def health(self) -> dict[str, Any]:
        """返回 watchdog 健康状态与最近一次检查延迟。"""
        return {
            "running": not self._stopped and self._task is not None,
            "last_check_at": self._last_check_at,
            "last_check_latency_ms": self._last_check_latency_ms,
            "tick_seconds": self._config.watchdog_tick_seconds,
        }

    async def _tick_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._config.watchdog_tick_seconds)
            except asyncio.CancelledError:
                break
            try:
                started = time.monotonic()
                await self._check_all()
                self._last_check_latency_ms = (time.monotonic() - started) * 1000.0
                self._last_check_at = time.monotonic()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("TaskWatchdog check failed: %s", exc)

    async def _check_all(self) -> None:
        """遍历 TaskStore 计划中的任务，更新监督状态并生成告警。"""
        if self._task_store is None:
            return

        plan = self._task_store.get_plan()
        now = time.monotonic()
        env_step = (
            self._run_control.get_run_status().step
            if self._run_control is not None
            else (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
        )

        runtime = self._runtime or getattr(self._task_store, "_runtime", None)
        if runtime is not None:
            self._runtime = runtime
            work_items = [
                (dispatch.dispatch_id, dispatch.worker_id, dispatch.worker_task_id or "")
                for dispatch in runtime.dispatches.values()
            ]
        else:
            work_items = [
                (
                    node.task_id,
                    node.worker_id or "",
                    self._task_store._dispatch_to_worker.get(node.task_id, ""),
                )
                for node in plan
            ]

        for dispatch_id, worker_id, worker_task_id in work_items:

            state = self._supervision_store.get_or_create(
                dispatch_id,
                worker_id=worker_id,
                worker_task_id=worker_task_id,
                config={
                    "stale_threshold_seconds": self._config.task_stale_seconds,
                    "unreachable_threshold_seconds": self._config.worker_unreachable_seconds,
                    "deadline_warning_seconds": self._config.deadline_warning_seconds,
                    "hard_deadline_seconds": self._config.task_hard_deadline_seconds,
                    "grace_period_seconds": self._config.grace_period_seconds,
                },
            )
            # Recompute now after state creation to avoid created_at being
            # slightly ahead of now, which would make in_grace True.
            now = time.monotonic()

            if state.terminal:
                continue

            # Physical runtime state is canonical. EventStore is only a
            # diagnostic source for legacy stores without a runtime.
            if runtime is not None:
                dispatch = runtime.get_dispatch(dispatch_id)
                task_state_name = (
                    dispatch.state.value if dispatch is not None else "UNKNOWN"
                )
            else:
                task_state = self._event_store.get_task_state(
                    dispatch_id, worker_task_id or None
                )
                task_state_name = task_state.get("state", "UNKNOWN")
            if task_state_name in ("COMPLETED", "FAILED", "CANCELED"):
                state.terminal = True
                state.supervision_state = "TERMINAL"
                self._supervision_store.update(dispatch_id, state)
                continue

            # Record state change if applicable
            if task_state_name != "UNKNOWN" and task_state_name != "DISPATCHED":
                state.last_state_change_at = now

            elapsed = now - state.created_at
            in_grace = elapsed < state.grace_period_seconds

            # Check progress by domain delta (env metrics / step)
            self._refresh_progress_by_domain_delta(state, env_step, now)

            # Worker unreachable
            if worker_id and not in_grace:
                await self._check_worker_unreachable(state, worker_id, now)

            # Stale (no progress)
            if not in_grace:
                await self._check_stale(state, dispatch_id, env_step, now)

            # Deadline
            if not in_grace:
                await self._check_deadline(state, dispatch_id, now)

            self._supervision_store.update(dispatch_id, state)

    def _refresh_progress_by_domain_delta(
        self, state: "SupervisionState", env_step: int, now: float
    ) -> None:
        """根据环境指标变化刷新进展时间。

        如果 barrier metrics（coverage、transport_rate、finished）发生变化，
        视为有效领域进展。避免把 step 增长本身当作进展。
        """
        if self._barrier is None:
            return

        try:
            metrics = self._barrier.get_metrics()
            # Only detect domain-delta progress for SAR-format metrics
            # (contains 'coverage' key). AI2Thor's get_run_status().domain_metrics
            # does not have coverage, so it degrades gracefully.
            if "coverage" not in metrics:
                return
            current = {
                "coverage": metrics.get("coverage", 0.0),
                "transport_rate": metrics.get("transport_rate", 0.0),
                "finished": metrics.get("finished", False),
                "steps": env_step,
            }
        except Exception:
            return

        last = state.last_metrics
        if last:
            changed = (
                current.get("coverage") != last.get("coverage")
                or current.get("transport_rate") != last.get("transport_rate")
                or current.get("finished") != last.get("finished")
            )
            if changed:
                state.last_progress_at = now
                state.last_progress_step = env_step

        state.last_metrics = current

    async def _check_worker_unreachable(
        self, state: "SupervisionState", worker_id: str, now: float
    ) -> None:
        last_contact = self._registry.get_last_contact_at(worker_id)
        if last_contact is None:
            last_contact = state.created_at
        age = now - last_contact

        if age > state.unreachable_threshold_seconds:
            if "WORKER_UNREACHABLE" not in state.active_alerts:
                event_id = state.next_event_id()
                event: dict[str, Any] = {
                    "event_id": event_id,
                    "event_type": "WORKER_UNREACHABLE",
                    "dispatch_id": state.dispatch_id,
                    "worker_id": worker_id,
                    "ts": time.time(),
                    "age_seconds": age,
                }
                state.active_alerts["WORKER_UNREACHABLE"] = event_id
                state.unacknowledged_events.append(event)
                state.supervision_state = "WORKER_UNREACHABLE"
                self._event_store.append(
                    state.dispatch_id,
                    "supervision_event",
                    state="WORKER_UNREACHABLE",
                    text=str(event),
                )
                logger.warning(
                    "WORKER_UNREACHABLE: dispatch_id=%s worker_id=%s age=%.1fs",
                    state.dispatch_id,
                    worker_id,
                    age,
                )
        else:
            if "WORKER_UNREACHABLE" in state.active_alerts:
                event_id = state.active_alerts.pop("WORKER_UNREACHABLE")
                state.acknowledged_event_ids.add(event_id)
                state.supervision_state = "HEALTHY"
                self._emit_recovery(state, "WORKER_UNREACHABLE")
                logger.info(
                    "TASK_RECOVERED from WORKER_UNREACHABLE: dispatch_id=%s",
                    state.dispatch_id,
                )

    async def _check_stale(
        self,
        state: "SupervisionState",
        dispatch_id: str,
        env_step: int,
        now: float,
    ) -> None:
        progress_age = now - state.last_progress_at
        steps_since_progress = env_step - state.last_progress_step

        stale_by_time = progress_age > state.stale_threshold_seconds
        stale_by_steps = steps_since_progress >= self._config.no_progress_step_threshold

        if stale_by_time or stale_by_steps:
            if "TASK_STALE" not in state.active_alerts:
                event_id = state.next_event_id()
                event: dict[str, Any] = {
                    "event_id": event_id,
                    "event_type": "TASK_STALE",
                    "dispatch_id": dispatch_id,
                    "ts": time.time(),
                    "progress_age_seconds": progress_age,
                    "steps_since_progress": steps_since_progress,
                }
                state.active_alerts["TASK_STALE"] = event_id
                state.unacknowledged_events.append(event)
                state.supervision_state = "STALE"
                self._event_store.append(
                    dispatch_id,
                    "supervision_event",
                    state="TASK_STALE",
                    text=str(event),
                )
                logger.warning(
                    "TASK_STALE: dispatch_id=%s progress_age=%.1fs steps_since=%d",
                    dispatch_id,
                    progress_age,
                    steps_since_progress,
                )
        else:
            if "TASK_STALE" in state.active_alerts:
                event_id = state.active_alerts.pop("TASK_STALE")
                state.acknowledged_event_ids.add(event_id)
                state.supervision_state = "HEALTHY"
                self._emit_recovery(state, "TASK_STALE")
                logger.info(
                    "TASK_RECOVERED from TASK_STALE: dispatch_id=%s", dispatch_id
                )

    async def _check_deadline(
        self, state: "SupervisionState", dispatch_id: str, now: float
    ) -> None:
        elapsed = now - state.created_at

        if elapsed > state.hard_deadline_seconds:
            if "TASK_DEADLINE_EXCEEDED" not in state.active_alerts:
                event_id = state.next_event_id()
                event: dict[str, Any] = {
                    "event_id": event_id,
                    "event_type": "TASK_DEADLINE_EXCEEDED",
                    "dispatch_id": dispatch_id,
                    "ts": time.time(),
                    "elapsed_seconds": elapsed,
                }
                state.active_alerts["TASK_DEADLINE_EXCEEDED"] = event_id
                state.unacknowledged_events.append(event)
                state.supervision_state = "DEADLINE_EXCEEDED"
                self._event_store.append(
                    dispatch_id,
                    "supervision_event",
                    state="TASK_DEADLINE_EXCEEDED",
                    text=str(event),
                )
                logger.warning(
                    "TASK_DEADLINE_EXCEEDED: dispatch_id=%s elapsed=%.1fs",
                    dispatch_id,
                    elapsed,
                )
            if self._runtime is not None:
                await self._runtime.cancel_dispatch_remote(
                    dispatch_id, reason="watchdog_timeout"
                )
        elif elapsed > state.deadline_warning_seconds:
            if "TASK_DEADLINE_WARNING" not in state.active_alerts:
                event_id = state.next_event_id()
                event = {
                    "event_id": event_id,
                    "event_type": "TASK_DEADLINE_WARNING",
                    "dispatch_id": dispatch_id,
                    "ts": time.time(),
                    "elapsed_seconds": elapsed,
                }
                state.active_alerts["TASK_DEADLINE_WARNING"] = event_id
                state.unacknowledged_events.append(event)
                state.supervision_state = "DEADLINE_WARNING"
                self._event_store.append(
                    dispatch_id,
                    "supervision_event",
                    state="TASK_DEADLINE_WARNING",
                    text=str(event),
                )
                logger.warning(
                    "TASK_DEADLINE_WARNING: dispatch_id=%s elapsed=%.1fs",
                    dispatch_id,
                    elapsed,
                )

    def _emit_recovery(self, state: "SupervisionState", recovered_from: str) -> None:
        event_id = state.next_event_id()
        event: dict[str, Any] = {
            "event_id": event_id,
            "event_type": "TASK_RECOVERED",
            "dispatch_id": state.dispatch_id,
            "worker_id": state.worker_id,
            "ts": time.time(),
            "recovered_from": recovered_from,
        }
        state.unacknowledged_events.append(event)
        self._event_store.append(
            state.dispatch_id,
            "supervision_event",
            state="TASK_RECOVERED",
            text=str(event),
        )

    # ------------------------------------------------------------------
    # Public API for event-driven progress/contact recording
    # ------------------------------------------------------------------

    def record_contact(
        self, dispatch_id: str, worker_id: str = "", worker_task_id: str = ""
    ) -> None:
        """记录任务联系时间。

        由 WebSocket heartbeat 和 A2A push callback 调用。
        """
        state = self._supervision_store.get_or_create(
            dispatch_id, worker_id=worker_id, worker_task_id=worker_task_id
        )
        state.last_contact_at = time.monotonic()
        self._supervision_store.update(dispatch_id, state)

    def record_progress(
        self,
        dispatch_id: str,
        worker_id: str = "",
        worker_task_id: str = "",
        source: str = "",
        step: int = 0,
    ) -> None:
        """记录任务进展。

        由 A2A push callback 在 actionable 事件（状态变更、artifact、observation）
        到达时调用。
        """
        state = self._supervision_store.get_or_create(
            dispatch_id, worker_id=worker_id, worker_task_id=worker_task_id
        )
        now = time.monotonic()
        state.last_progress_at = now
        state.last_contact_at = now
        if step > 0:
            state.last_progress_step = step
        if state.supervision_state == "STALE":
            # Auto-recover from STALE on progress
            if "TASK_STALE" in state.active_alerts:
                event_id = state.active_alerts.pop("TASK_STALE")
                state.acknowledged_event_ids.add(event_id)
                state.supervision_state = "HEALTHY"
                self._emit_recovery(state, "TASK_STALE")
        self._supervision_store.update(dispatch_id, state)
        logger.debug(
            "Progress recorded for dispatch_id=%s source=%s step=%s",
            dispatch_id,
            source,
            step,
        )

    def record_state_change(
        self,
        dispatch_id: str,
        worker_id: str = "",
        worker_task_id: str = "",
        state_name: str = "",
    ) -> None:
        """记录任务状态变更时间。"""
        state = self._supervision_store.get_or_create(
            dispatch_id, worker_id=worker_id, worker_task_id=worker_task_id
        )
        now = time.monotonic()
        state.last_state_change_at = now
        state.last_contact_at = now
        terminal_names = {
            "COMPLETED",
            "FAILED",
            "CANCELED",
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
        }
        if state_name in terminal_names:
            state.terminal = True
            state.supervision_state = "TERMINAL"
        elif state_name == "INPUT_REQUIRED":
            state.last_progress_at = now
            state.last_progress_step = (
                getattr(self._barrier, "_step_counter", 0) if self._barrier else 0
            )
        self._supervision_store.update(dispatch_id, state)

    def record_worker_contact(self, worker_id: str) -> None:
        """在 WorkerRegistry 层记录 Worker 联系时间。

        与 record_contact 不同，此方法只更新 WorkerRegistry 的 last_contact_at，
        用于 WORKER_UNREACHABLE 判断。
        """
        self._registry.update_contact(worker_id)
