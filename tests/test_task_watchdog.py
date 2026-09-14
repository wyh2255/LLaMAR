"""Tests for TaskWatchdog and SupervisionStateStore (Phase 3)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from a2a.coordinator.event_store import EventStore
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.worker_registry import WorkerRegistry
from a2a.coordinator.task_watchdog import TaskWatchdog, WatchdogConfig
from a2a.coordinator.supervision_state_store import SupervisionStateStore


class MockWorkerRegistry(WorkerRegistry):
    """WorkerRegistry with deterministic contact times for tests."""

    def __init__(self):
        super().__init__()
        self._contact_overrides: dict[str, float] = {}

    def override_contact(self, worker_id: str, ts: float) -> None:
        self._last_contact_at[worker_id] = ts


class MockBarrier:
    def __init__(self, step=0, coverage=0.0, transport_rate=0.0, finished=False):
        self._step_counter = step
        self._coverage = coverage
        self._transport_rate = transport_rate
        self._finished = finished

    def is_finished(self):
        return self._finished

    def get_metrics(self):
        return {
            "coverage": self._coverage,
            "transport_rate": self._transport_rate,
            "steps": self._step_counter,
            "finished": self._finished,
        }


@pytest.fixture
def event_store():
    return EventStore()


@pytest.fixture
def supervision_store(event_store):
    return SupervisionStateStore()


@pytest.fixture
def registry():
    return MockWorkerRegistry()


@pytest.fixture
def task_store():
    store = TaskStore(
        original_request="test", router=MagicMock(), max_tasks=10, context_id="ctx-1"
    )
    store.update_plan(
        [
            {
                "task_id": "dispatch-1",
                "worker_id": "Alice",
                "description": "test task",
            }
        ]
    )
    store.register_worker_task_id("dispatch-1", "worker-task-1")
    return store


@pytest.fixture
def watchdog(registry, event_store, supervision_store, task_store):
    barrier = MockBarrier(step=1)
    config = WatchdogConfig(
        task_stale_seconds=2.0,
        worker_unreachable_seconds=2.0,
        deadline_warning_seconds=5.0,
        task_hard_deadline_seconds=10.0,
        grace_period_seconds=0.0,
        watchdog_tick_seconds=0.05,
    )
    wd = TaskWatchdog(
        worker_registry=registry,
        event_store=event_store,
        supervision_store=supervision_store,
        barrier=barrier,
        config=config,
    )
    wd.set_task_store(task_store)
    return wd


# ------------------------------------------------------------------
# SupervisionStateStore tests
# ------------------------------------------------------------------


def test_supervision_store_get_or_create():
    store = SupervisionStateStore()
    state = store.get_or_create("dispatch-1", worker_id="Alice")
    assert state.dispatch_id == "dispatch-1"
    assert state.worker_id == "Alice"
    assert state.supervision_state == "HEALTHY"
    assert state.created_at > 0

    # Second call returns same state
    state2 = store.get_or_create("dispatch-1", worker_id="Bob")
    assert state2 is state
    assert state2.worker_id == "Alice"  # not overwritten


def test_supervision_store_acknowledge_event():
    store = SupervisionStateStore()
    state = store.get_or_create("dispatch-1")
    state.unacknowledged_events.append({"event_id": "ev-1", "event_type": "TASK_STALE"})
    state.active_alerts["TASK_STALE"] = "ev-1"
    store.update("dispatch-1", state)

    store.acknowledge_event("dispatch-1", "ev-1")
    state = store.get("dispatch-1")
    assert state.unacknowledged_events == []
    assert "TASK_STALE" not in state.active_alerts
    assert "ev-1" in state.acknowledged_event_ids


# ------------------------------------------------------------------
# TaskWatchdog progress/contact tests
# ------------------------------------------------------------------


def test_record_contact_updates_last_contact(event_store, supervision_store):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    wd.record_contact("dispatch-1", worker_id="Alice", worker_task_id="wt-1")
    state = supervision_store.get("dispatch-1")
    assert state.last_contact_at > 0


def test_record_progress_updates_last_progress_and_recovers_stale(
    event_store, supervision_store
):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    # Put task in STALE
    state = supervision_store.get_or_create("dispatch-1", worker_id="Alice")
    state.supervision_state = "STALE"
    state.active_alerts["TASK_STALE"] = "ev-1"
    state.unacknowledged_events.append({"event_id": "ev-1", "event_type": "TASK_STALE"})
    supervision_store.update("dispatch-1", state)

    before = state.last_progress_at
    wd.record_progress(
        "dispatch-1",
        worker_id="Alice",
        worker_task_id="wt-1",
        source="observation",
        step=5,
    )
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert state.last_progress_at > before
    assert state.supervision_state == "HEALTHY"
    assert "TASK_STALE" not in state.active_alerts
    assert any(e["event_type"] == "TASK_RECOVERED" for e in state.unacknowledged_events)


def test_record_state_change_marks_terminal(event_store, supervision_store):
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
    )
    wd.record_state_change(
        "dispatch-1",
        worker_id="Alice",
        worker_task_id="wt-1",
        state_name="TASK_STATE_COMPLETED",
    )
    state = supervision_store.get("dispatch-1")
    assert state.terminal is True
    assert state.supervision_state == "TERMINAL"


# ------------------------------------------------------------------
# TaskWatchdog detection tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_stale_after_progress_timeout(watchdog, supervision_store):
    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"

    # Artificially age progress timestamp
    state.last_progress_at = time.monotonic() - 10.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    assert "TASK_STALE" in state.active_alerts


@pytest.mark.asyncio
async def test_detects_worker_unreachable(watchdog, registry, supervision_store):
    registry.register_from_ws("Alice", "http://localhost:9000")
    registry.override_contact("Alice", time.monotonic() - 10.0)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "WORKER_UNREACHABLE"
    assert "WORKER_UNREACHABLE" in state.active_alerts


@pytest.mark.asyncio
async def test_detects_deadline_exceeded(watchdog, supervision_store):
    cfg = watchdog._config
    state = supervision_store.get_or_create(
        "dispatch-1",
        worker_id="Alice",
        config={
            "stale_threshold_seconds": cfg.task_stale_seconds,
            "unreachable_threshold_seconds": cfg.worker_unreachable_seconds,
            "deadline_warning_seconds": cfg.deadline_warning_seconds,
            "hard_deadline_seconds": cfg.task_hard_deadline_seconds,
            "grace_period_seconds": cfg.grace_period_seconds,
        },
    )
    state.created_at = time.monotonic() - 20.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert state.supervision_state == "DEADLINE_EXCEEDED"
    assert "TASK_DEADLINE_EXCEEDED" in state.active_alerts


@pytest.mark.asyncio
async def test_terminal_task_skips_supervision(
    watchdog, event_store, supervision_store
):
    state = supervision_store.get_or_create("dispatch-1", worker_id="Alice")
    state.terminal = True
    state.last_progress_at = time.monotonic() - 100.0
    supervision_store.update("dispatch-1", state)

    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state is not None
    assert "TASK_STALE" not in state.active_alerts


# ------------------------------------------------------------------
# TaskWatchdog ticker tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_start_stop():
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
    )
    await wd.start()
    assert wd.health()["running"] is True
    await wd.stop()
    assert wd.health()["running"] is False


@pytest.mark.asyncio
async def test_watchdog_ticker_records_latency():
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        config=WatchdogConfig(watchdog_tick_seconds=0.05),
    )
    await wd.start()
    await asyncio.sleep(0.15)
    await wd.stop()
    health = wd.health()
    assert health["last_check_at"] > 0
    assert health["last_check_latency_ms"] >= 0


# ------------------------------------------------------------------
# WorkerRegistry contact tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_registry_update_contact_and_heartbeat():
    registry = WorkerRegistry()
    registry.register_from_ws("Alice", "http://localhost:9000")
    before_contact = registry.get_last_contact_at("Alice")
    before_hb = registry.get("Alice").last_heartbeat

    await asyncio.sleep(0.01)
    registry.update_heartbeat("Alice")
    after_contact = registry.get_last_contact_at("Alice")
    after_hb = registry.get("Alice").last_heartbeat

    assert after_contact > before_contact
    assert after_hb > before_hb

    registry.update_contact("Alice")
    assert registry.get_last_contact_at("Alice") > after_contact


# ------------------------------------------------------------------
# Domain delta progress tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervision_events_route_to_memory_sink_with_dispatch():
    """Phase 2: every supervision emit reaches the dispatch-bound canonical
    writer (SupervisionEventAdapter) with the resolved PhysicalDispatch."""
    from a2a.coordinator.mission_runtime import MissionRuntimeManager

    manager = MissionRuntimeManager()
    runtime = manager.admit("ctx-1")
    dispatch = runtime.create_dispatch("logical-1", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-task-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="acceptance")

    captured: list = []

    def sink(event, dispatch, runtime_epoch=None):
        captured.append((event["event_type"], dispatch.dispatch_id, runtime_epoch))

    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        supervision_event_sink=sink,
    )
    wd.set_runtime(runtime)
    wd.set_task_store(
        TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    )

    kinds = [
        "WORKER_UNREACHABLE",
        "TASK_STALE",
        "TASK_DEADLINE_EXCEEDED",
        "TASK_DEADLINE_WARNING",
        "TASK_RECOVERED",
    ]
    for i, kind in enumerate(kinds):
        wd._emit_to_memory(
            {
                "event_id": f"ev-{i}",
                "event_type": kind,
                "dispatch_id": dispatch.dispatch_id,
                "worker_id": "Alice",
            }
        )

    assert [(k, d, e) for k, d, e in captured] == [
        (kind, dispatch.dispatch_id, runtime.epoch) for kind in kinds
    ]


def test_supervision_emit_without_dispatch_keeps_only_local_diagnostic():
    """When no dispatch can be resolved, the canonical sink is never called."""
    captured: list = []

    def sink(event, dispatch, runtime_epoch=None):
        captured.append(event)

    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=EventStore(),
        supervision_store=SupervisionStateStore(),
        supervision_event_sink=sink,
    )
    wd._emit_to_memory({"event_id": "ev-1", "event_type": "TASK_STALE"})
    assert captured == []


@pytest.mark.asyncio
async def test_progress_refreshed_on_domain_delta():
    barrier = MockBarrier(step=2, coverage=0.1, transport_rate=0.0)
    store = SupervisionStateStore()
    event_store = EventStore()
    registry = MockWorkerRegistry()
    task_store = TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    task_store.update_plan([{"task_id": "dispatch-1", "worker_id": "Alice"}])

    wd = TaskWatchdog(
        worker_registry=registry,
        event_store=event_store,
        supervision_store=store,
        barrier=barrier,
        config=WatchdogConfig(grace_period_seconds=0.0),
    )
    wd.set_task_store(task_store)

    # First check establishes baseline metrics
    await wd._check_all()
    state = store.get("dispatch-1")
    first_progress = state.last_progress_at

    # Change domain metric
    barrier._coverage = 0.5
    await wd._check_all()
    state = store.get("dispatch-1")
    assert state.last_progress_at > first_progress
    assert state.last_progress_step == 2


@pytest.mark.asyncio
async def test_no_stale_when_task_created_mid_environment():
    """任务创建于环境中期（env step=4）时，首次 _check_all 不应误报 TASK_STALE。

    回归：SupervisionState.last_progress_step 默认 0，若首次 tick 不建立基线，
    steps_since_progress = 4 - 0 >= 3 会让任务在 grace 一过就被判 STALE。
    """
    barrier = MockBarrier(step=4, coverage=0.2, transport_rate=0.0)
    store = SupervisionStateStore()
    event_store = EventStore()
    registry = MockWorkerRegistry()
    task_store = TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    task_store.update_plan([{"task_id": "dispatch-1", "worker_id": "Alice"}])

    wd = TaskWatchdog(
        worker_registry=registry,
        event_store=event_store,
        supervision_store=store,
        barrier=barrier,
        config=WatchdogConfig(grace_period_seconds=0.0),
    )
    wd.set_task_store(task_store)

    # 首次检查：任务创建于 step=4 时只建立基线，不产生 TASK_STALE
    await wd._check_all()
    state = store.get("dispatch-1")
    assert state is not None
    assert state.last_progress_step == 4
    assert "TASK_STALE" not in state.active_alerts
    assert state.supervision_state == "HEALTHY"
    assert state.unacknowledged_events == []
    assert "TASK_STALE" not in event_store.get_summary(task_ids={"dispatch-1"})

    # 第二次检查：metrics 不变、step 推进到 5（steps_since=1 < 3）仍不应告警
    barrier._step_counter = 5
    await wd._check_all()
    state = store.get("dispatch-1")
    assert "TASK_STALE" not in state.active_alerts
    assert state.last_progress_step == 4  # metrics 未变化，基线不前进


# ------------------------------------------------------------------
# C2b: TASK_STALE 组合方式/阈值按环境校准（A100 首跑报告 §4-4）
# ------------------------------------------------------------------


def test_watchdog_config_defaults_unchanged():
    """C2b 红线：内核缺省阈值与组合方式逐字不变（SAR 行为零变化）。"""
    cfg = WatchdogConfig()
    assert cfg.task_stale_seconds == 120.0
    assert cfg.worker_unreachable_seconds == 120.0
    assert cfg.deadline_warning_seconds == 300.0
    assert cfg.task_hard_deadline_seconds == 600.0
    assert cfg.watchdog_tick_seconds == 5.0
    assert cfg.grace_period_seconds == 10.0
    assert cfg.no_progress_step_threshold == 3
    assert cfg.stale_requires_both is False


def _watchdog_with(config, barrier, event_store, supervision_store):
    """构造带单一 dispatch 计划的最小 watchdog（C2b 测试共用）。"""
    task_store = TaskStore(original_request="test", router=MagicMock(), max_tasks=10)
    task_store.update_plan([{"task_id": "dispatch-1", "worker_id": "Alice"}])
    wd = TaskWatchdog(
        worker_registry=MockWorkerRegistry(),
        event_store=event_store,
        supervision_store=supervision_store,
        barrier=barrier,
        config=config,
    )
    wd.set_task_store(task_store)
    return wd


def _age(state, *, created_at=None, progress_age=None):
    """回拨 state 的时间戳以模拟流逝（created_at / last_progress_at）。"""
    now = time.monotonic()
    if created_at is not None:
        state.created_at = now - created_at
    if progress_age is not None:
        state.last_progress_at = now - progress_age
    return state


@pytest.mark.asyncio
async def test_default_combo_fires_on_steps_alone(watchdog, supervision_store):
    """缺省组合（「或」）：仅步数条件满足即报 TASK_STALE —— SAR 现状行为保持。"""
    await watchdog._check_all()  # 首次 tick 建立基线（step=1）
    assert supervision_store.get("dispatch-1").supervision_state == "HEALTHY"

    # 只推进步数：progress_age 远小于 task_stale_seconds(=2.0)
    watchdog._barrier._step_counter = 5
    await watchdog._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    assert "TASK_STALE" in state.active_alerts


@pytest.mark.asyncio
async def test_both_combo_requires_both_conditions(event_store, supervision_store):
    """双条件组合：步数或时间任一单独超限都不报，同时超限才报。"""
    barrier = MockBarrier(step=0)
    wd = _watchdog_with(
        WatchdogConfig(
            task_stale_seconds=2.0,
            no_progress_step_threshold=3,
            stale_requires_both=True,
            grace_period_seconds=0.0,
        ),
        barrier,
        event_store,
        supervision_store,
    )

    await wd._check_all()  # 基线（step=0）
    assert supervision_store.get("dispatch-1").supervision_state == "HEALTHY"

    # ① 仅步数超限（>=3 步但时间未到）→ 不报
    barrier._step_counter = 5
    await wd._check_all()
    state = supervision_store.get("dispatch-1")
    assert "TASK_STALE" not in state.active_alerts

    # ② 仅时间超限（>2.0s 但步数不足）→ 不报
    barrier._step_counter = 1
    _age(state, progress_age=10.0)
    supervision_store.update("dispatch-1", state)
    await wd._check_all()
    state = supervision_store.get("dispatch-1")
    assert "TASK_STALE" not in state.active_alerts

    # ③ 双条件同时满足 → 报
    barrier._step_counter = 5
    await wd._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    assert "TASK_STALE" in state.active_alerts


@pytest.mark.asyncio
async def test_both_combo_true_stall_still_fires_and_recovers(
    event_store, supervision_store
):
    """反向用例（红线）：真停滞（无任何 progress 且双条件同时超限）仍报
    TASK_STALE，并在真实进展事件后自动恢复。"""
    barrier = MockBarrier(step=0)
    wd = _watchdog_with(
        WatchdogConfig(
            task_stale_seconds=2.0,
            no_progress_step_threshold=3,
            stale_requires_both=True,
            grace_period_seconds=0.0,
        ),
        barrier,
        event_store,
        supervision_store,
    )
    await wd._check_all()  # 基线（step=0）

    # 真停滞：整整 30 回合、100s 无任何 progress 刷新
    barrier._step_counter = 30
    state = supervision_store.get("dispatch-1")
    _age(state, progress_age=100.0)
    supervision_store.update("dispatch-1", state)
    await wd._check_all()

    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    event = next(
        e for e in state.unacknowledged_events if e["event_type"] == "TASK_STALE"
    )
    assert event["steps_since_progress"] == 30
    assert event["progress_age_seconds"] >= 100.0

    # 真实进展（观测上报）→ 自动恢复
    wd.record_progress(
        "dispatch-1", worker_id="Alice", source="observation_report", step=30
    )
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"
    assert "TASK_STALE" not in state.active_alerts
    assert any(e["event_type"] == "TASK_RECOVERED" for e in state.unacknowledged_events)


# ------------------------------------------------------------------
# C2b: AI2Thor 真机预设验收（报告 §4-4 时间线复现，预设来源=
# ai2thor_orch.assembly_hooks.build_watchdog_config）
# ------------------------------------------------------------------


def _ai2thor_watchdog(barrier, event_store, supervision_store):
    from ai2thor_orch.assembly_hooks import build_watchdog_config

    return _watchdog_with(
        build_watchdog_config(), barrier, event_store, supervision_store
    )


@pytest.mark.asyncio
async def test_ai2thor_preset_silent_on_a100_short_run_rhythm(
    event_store, supervision_store
):
    """A100 短跑时间线复现：旧缺省（「或」+3 步）在 since=6/age=10s 误报，
    真机预设（双条件+10 步+90s）同一时间线保持静默（8 回合短跑全程 0 告警）。"""
    barrier = MockBarrier(step=0)
    wd = _ai2thor_watchdog(barrier, event_store, supervision_store)
    await wd._check_all()  # 基线（step=0）

    # 越过 grace（预设未改 grace=10s）：created_at 提前 60s 不触发
    # WORKER_UNREACHABLE（缺省阈值 120s）
    state = supervision_store.get("dispatch-1")
    _age(state, created_at=60.0)
    supervision_store.update("dispatch-1", state)

    # 旧配置的实际触发点（报告 §4-4：progress_age=10.0s / steps_since=6）
    barrier._step_counter = 6
    state = supervision_store.get("dispatch-1")
    _age(state, progress_age=10.0)
    supervision_store.update("dispatch-1", state)
    await wd._check_all()
    state = supervision_store.get("dispatch-1")
    assert "TASK_STALE" not in state.active_alerts

    # 短跑终态：8 回合、约 21s 无进展（run 就此收官）→ 仍静默
    barrier._step_counter = 8
    _age(state, progress_age=21.0)
    supervision_store.update("dispatch-1", state)
    await wd._check_all()
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"
    assert state.unacknowledged_events == []

    # 对照组：旧缺省组合（「或」+3 步）在同一触发点确实误报 —— 证明修复有的放矢
    old_barrier = MockBarrier(step=0)
    wd_old = _watchdog_with(
        WatchdogConfig(grace_period_seconds=0.0),
        old_barrier,
        EventStore(),
        SupervisionStateStore(),
    )
    await wd_old._check_all()
    old_state = wd_old._supervision_store.get("dispatch-1")
    assert old_state is not None
    old_barrier._step_counter = 6
    _age(old_state, progress_age=10.0)
    wd_old._supervision_store.update("dispatch-1", old_state)
    await wd_old._check_all()
    final_old = wd_old._supervision_store.get("dispatch-1")
    assert final_old is not None
    assert final_old.supervision_state == "STALE"


@pytest.mark.asyncio
async def test_ai2thor_preset_silent_on_longest_normal_window(
    event_store, supervision_store
):
    """l3_full 实测最长正常无进展窗（≈24 回合 / 62s：progress 只在观测上报、
    artifact、域指标变化时刷新）在真机预设下不报 stale。"""
    barrier = MockBarrier(step=0)
    wd = _ai2thor_watchdog(barrier, event_store, supervision_store)
    await wd._check_all()  # 基线（step=0）

    state = supervision_store.get("dispatch-1")
    _age(state, created_at=60.0, progress_age=62.0)
    supervision_store.update("dispatch-1", state)
    barrier._step_counter = 24
    await wd._check_all()

    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"
    assert "TASK_STALE" not in state.active_alerts


@pytest.mark.asyncio
async def test_ai2thor_preset_true_stall_fires_and_recovers(
    event_store, supervision_store
):
    """反向用例：真停滞（无 progress、双条件同时超限）在真机预设下仍报
    TASK_STALE 并可恢复（预设口径：90s / 10 回合）。"""
    barrier = MockBarrier(step=0)
    wd = _ai2thor_watchdog(barrier, event_store, supervision_store)
    await wd._check_all()  # 基线（step=0）

    state = supervision_store.get("dispatch-1")
    _age(state, created_at=105.0, progress_age=100.0)
    supervision_store.update("dispatch-1", state)
    barrier._step_counter = 30
    await wd._check_all()

    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "STALE"
    event = next(
        e for e in state.unacknowledged_events if e["event_type"] == "TASK_STALE"
    )
    assert event["progress_age_seconds"] >= 100.0
    assert event["steps_since_progress"] == 30

    wd.record_progress(
        "dispatch-1", worker_id="Alice", source="observation_report", step=30
    )
    state = supervision_store.get("dispatch-1")
    assert state.supervision_state == "HEALTHY"
    assert "TASK_STALE" not in state.active_alerts
    assert any(e["event_type"] == "TASK_RECOVERED" for e in state.unacknowledged_events)
