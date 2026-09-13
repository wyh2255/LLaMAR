"""env-contract G8: SAR assembly wires the barrier as EnvironmentRunControl.

Two layers of evidence:

1. Assembly level — ``SARCoordinator.start()`` calls
   ``server.set_run_control(barrier)`` next to ``set_barrier`` (the wire itself),
   and ``SARBarrier`` satisfies the ``EnvironmentRunControl`` protocol.
2. Endpoint level — the real ``POST /experiment/{context_id}/cancel`` route
   (TestClient) consumes run control in three states: run_control preferred,
   barrier-only fallback, and neither set.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from a2a.coordinator.run_control import EnvironmentRunControl
from a2a.coordinator.server import CoordinatorServer
from a2a.shared.types import DistributedTask
from sar_orch.barrier import SARBarrier


def _server_with_task(context_id: str) -> CoordinatorServer:
    server = CoordinatorServer(
        host="127.0.0.1", port=0, a2a_port=0, memory_read_mode="legacy"
    )
    server._task_queue.enqueue(
        DistributedTask(
            task_id=f"task-{context_id}",
            task_type="agent",
            prompt="request",
            context_id=context_id,
        )
    )
    return server


# ── 1. Assembly-level wire ──────────────────────────────────────────────────


def test_sar_barrier_satisfies_environment_run_control():
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)
    assert isinstance(barrier, EnvironmentRunControl)


def test_sar_coordinator_start_wires_run_control(monkeypatch):
    """start() → create_server（内核）→ set_barrier / set_run_control 同时接线。"""
    import a2a.coordinator.server as kernel_server_module
    from sar_orch.coordinator import SARCoordinator

    barrier = SARBarrier(num_agents=1, scene=1, seed=42)
    fake_server = MagicMock()
    monkeypatch.setattr(
        kernel_server_module, "create_server", lambda **kwargs: fake_server
    )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    coord = SARCoordinator(
        barrier=barrier,
        coordinator_secret=bytes(range(32)),
        memory_read_mode="legacy",
    )
    asyncio.run(coord.start())

    fake_server.set_barrier.assert_called_once_with(barrier)
    fake_server.set_run_control.assert_called_once_with(barrier)


# ── 2. Endpoint three-state probe ───────────────────────────────────────────


def test_cancel_endpoint_uses_run_control_when_set():
    """State A: run_control + barrier set → request_stop 走 run_control（记录原因）。"""
    server = _server_with_task("ctx-runcontrol")
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)
    server.set_barrier(barrier)
    server.set_run_control(barrier)

    resp = TestClient(server._app).post("/experiment/ctx-runcontrol/cancel")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["barrier_stopped"] is True
    assert payload["cancelled_tasks"] == ["task-ctx-runcontrol"]
    # run_control 路径证据：request_stop 记录的 stop_reason；fallback 的
    # 直接 stop() 不会设置该字段。
    assert barrier._stopped is True
    assert barrier._stop_reason == "cancel:ctx-runcontrol"


def test_cancel_endpoint_falls_back_to_barrier_without_run_control():
    """State B: barrier only → 回退到 barrier.stop()（无 stop_reason）。"""
    server = _server_with_task("ctx-fallback")
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)
    server.set_barrier(barrier)

    resp = TestClient(server._app).post("/experiment/ctx-fallback/cancel")

    assert resp.status_code == 200
    assert resp.json()["barrier_stopped"] is True
    assert barrier._stopped is True
    assert barrier._stop_reason == ""


def test_cancel_endpoint_reports_false_when_nothing_wired():
    """State C: 两者皆未接线 → barrier_stopped=False，端点仍正常取消任务。"""
    server = _server_with_task("ctx-none")

    resp = TestClient(server._app).post("/experiment/ctx-none/cancel")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["barrier_stopped"] is False
    assert payload["cancelled_tasks"] == ["task-ctx-none"]
