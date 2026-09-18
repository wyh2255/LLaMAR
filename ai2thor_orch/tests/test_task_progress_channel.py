"""P0b task-progress 通道集成单测（TaskMetricsTracker → provider → context 渲染）。

覆盖：

- tracker 进度真源的读点：``missing_subtasks`` / ``item_progress``（动作证据口径，
  非 verifier 仿真真值）；
- coordinator provider payload 的 ``task_progress``（含 item_progress；tracker
  缺失 → 两个键都不放）；
- ``### Task Progress`` 段三形态：全部完成（22/22 常驻，不得因完成而省略）/
  部分完成 / barrier 无 contract（整段省略），以及段位置（``Objects of interest``
  之后、``### Sightings`` 之前）与 worker 侧零注入。
"""

from __future__ import annotations

import pytest

from Agent.router_agent.state_provider import RuntimeState
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.contracts.task import TaskContract, load_task
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.state.context import AI2ThorCoordinatorContextManager
from ai2thor_orch.state.coordinator_state_provider import (
    AI2ThorCoordinatorStateProvider,
)
from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider
from ai2thor_orch.tests.fakes import FakeController
from ai2thor_orch.tests.test_barrier import (
    _full_transport_sequence,
    _InventoryTrackingController,
)

pytestmark = pytest.mark.unit

_FRIDGE_ALIAS = "Fridge_1"
_BREAD_MISSING = (
    "NavigateTo(Bread), PickupObject(Bread), "
    "NavigateTo(Fridge, Bread), PutObject(Fridge, Bread)"
)


def _contract() -> TaskContract:
    return load_task("3_transport_groceries", "FloorPlan1")


def _barrier(
    *,
    contract: TaskContract | None = None,
    controller=None,
    max_steps: int = 16,
) -> AI2ThorBarrier:
    if controller is None:
        controller = _InventoryTrackingController() if contract else FakeController()
    return AI2ThorBarrier(
        num_agents=1,
        executor=ControllerExecutor(controller),
        max_steps=max_steps,
        step_timeout=5.0,
        contract=contract,
    )


def _render(barrier: AI2ThorBarrier) -> str:
    """barrier → provider → context 全链路渲染（与 session 装配同形状）。"""
    ctx = AI2ThorCoordinatorContextManager(
        state_provider=AI2ThorCoordinatorStateProvider(barrier)
    )
    ctx.refresh_runtime_state()
    return ctx._render_environment_view()


class _StubStateProvider:
    """固定 payload 的 provider 替身（用于精确构造渲染输入）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        return RuntimeState(version=1, env_step=0, payload=self._payload)


def _render_payload(payload: dict) -> str:
    ctx = AI2ThorCoordinatorContextManager(state_provider=_StubStateProvider(payload))
    ctx.refresh_runtime_state()
    return ctx._render_environment_view()


def _progress_block(rendered: str) -> list[str]:
    lines = rendered.splitlines()
    return lines[lines.index("### Task Progress") :]


@pytest.mark.asyncio
async def test_partial_progress_renders_per_grocery_missing_subtasks():
    """部分完成：总进度 + 每物品一行，缺项明细按 contract 原序列出。"""
    contract = _contract()
    barrier = _barrier(contract=contract)

    await barrier.submit_action(0, f"OpenObject({_FRIDGE_ALIAS})")
    rendered = _render(barrier)

    block = _progress_block(rendered)
    assert block[0] == "### Task Progress"
    assert block[1] == "Completed: 1/22 subtasks"
    assert f"- Bread: 0/4 — missing: {_BREAD_MISSING}" in block
    assert "- Fridge door: open done, close missing" in block

    # 分组顺序 = contract.coverage_objects 原序（容器渲染为「X door」行）
    labels = [line.split(":", 1)[0][2:] for line in block[2:]]
    expected = [
        f"{name} door" if name == "Fridge" else name
        for name in contract.coverage_objects
    ]
    assert labels == expected


@pytest.mark.asyncio
async def test_complete_progress_is_still_rendered_every_round():
    """全部完成：段仍在（22/22 常驻锚点），每物品渲染成 N/N done。"""
    contract = _contract()
    barrier = _barrier(contract=contract)

    for action in _full_transport_sequence():
        result = await barrier.submit_action(0, action)
        assert result.success is True

    rendered = _render(barrier)

    assert "### Task Progress" in rendered
    assert "Completed: 22/22 subtasks" in rendered
    for grocery in contract.coverage_objects:
        if grocery != "Fridge":
            assert f"- {grocery}: 4/4 done" in rendered
    assert "- Fridge door: open done, close done" in rendered
    assert "missing" not in "\n".join(_progress_block(rendered))


@pytest.mark.asyncio
async def test_barrier_without_contract_omits_section_entirely():
    """无 contract：tracker 缺失 → 段整段省略，worker 侧也不注入。"""
    barrier = _barrier(contract=None)

    await barrier.submit_action(0, "MoveAhead")
    rendered = _render(barrier)

    assert "### Task Progress" not in rendered
    assert rendered.splitlines()[-1].startswith("Objects of interest:")

    worker_payload = AI2ThorWorkerStateProvider(barrier, 0).snapshot().payload
    assert "task_progress" not in worker_payload
    assert "item_progress" not in worker_payload


def test_section_sits_between_objects_of_interest_and_sightings():
    """段位置契约：Objects of interest 之后、### Sightings 之前，空行隔离。"""
    rendered = _render_payload(
        {
            "scene": "FloorPlan1",
            "step_budget": {"current_step": 3, "max_steps": 10},
            "visible_objects": ["Bread_1"],
            "task_progress": {
                "completed_count": 1,
                "total_count": 22,
                "missing_subtasks": ["PickupObject(Bread)"],
            },
            "item_progress": [
                {
                    "item": "Bread",
                    "total": 2,
                    "completed": 1,
                    "subtasks": ["NavigateTo(Bread)", "PickupObject(Bread)"],
                    "missing": ["PickupObject(Bread)"],
                    "door": False,
                }
            ],
            "sightings": [
                {"step": 2, "agent": "Alice", "alias": "Bread_1", "x": 1.0, "z": 2.0}
            ],
        }
    )

    lines = rendered.splitlines()
    progress_idx = lines.index("### Task Progress")
    assert lines.index("Objects of interest: Bread_1") < progress_idx
    assert progress_idx < lines.index("### Sightings")
    assert lines[progress_idx - 1] == ""
    assert lines[progress_idx + 1] == "Completed: 1/22 subtasks"
    assert lines[progress_idx + 2] == ("- Bread: 1/2 — missing: PickupObject(Bread)")


def test_payload_without_item_progress_degrades_to_missing_list():
    """缺 item_progress（旧 payload）时退化为只列缺项，绝不丢段。"""
    rendered = _render_payload(
        {
            "scene": "FloorPlan1",
            "step_budget": {"current_step": 1, "max_steps": 10},
            "visible_objects": ["Bread_1"],
            "task_progress": {
                "completed_count": 0,
                "total_count": 2,
                "missing_subtasks": ["NavigateTo(Bread)", "PickupObject(Bread)"],
            },
        }
    )

    assert _progress_block(rendered) == [
        "### Task Progress",
        "Completed: 0/2 subtasks",
        "- Missing: NavigateTo(Bread), PickupObject(Bread)",
    ]
