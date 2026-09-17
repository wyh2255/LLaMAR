"""Tests for AI2ThorBarrier — round semantics, timeout, stop, run status."""

from __future__ import annotations

import asyncio

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.tests.fakes import FakeController, FakeEvent, make_default_metadata


@pytest.mark.asyncio
class TestNormalRound:
    """2 agents submit normally → round advances."""

    async def test_two_agents_round_advances(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=5.0,
        )

        # Both agents submit concurrently
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        result0, result1 = await asyncio.gather(agent_0(), agent_1())

        assert result0.success
        assert result1.success
        assert result0.action == "MoveAhead"
        assert result1.action == "RotateLeft"
        # Each agent's action triggers one controller.step() call
        assert ctrl.step_call_count == 2
        status = barrier.get_run_status()
        assert status.step == 1  # advanced after one round
        assert status.finished is False

    async def test_round_advances_step_counter(self):
        """Two rounds with 2 agents each."""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10, step_timeout=5.0,
        )

        # Round 1
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.get_run_status().step == 1
        assert ctrl.step_call_count == 2

        # Round 2
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.get_run_status().step == 2
        assert ctrl.step_call_count == 4


@pytest.mark.asyncio
class TestTimeout:
    """1 agent times out → auto NoOp, round still advances."""

    async def test_timeout_triggers_noop(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,  # short timeout
        )

        # Agent 0 submits, agent 1 never submits
        result0 = await barrier.submit_action(0, "MoveAhead")

        assert result0.success
        status = barrier.get_run_status()
        assert status.step == 1
        # Agent 1 should be in timeout_agents
        assert 1 in status.timeout_agents
        # 2 agents → 2 controller step() calls: MoveAhead + NoOp
        assert ctrl.step_call_count == 2

    async def test_only_late_agent_is_timeout(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=3,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,
        )

        # Agents 0 and 1 submit, agent 2 is late
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        await asyncio.gather(agent_0(), agent_1())

        status = barrier.get_run_status()
        # After timeout, both agents returned and round advanced
        assert status.step == 1
        # Agent 2 should be in timeout_agents (not 0 or 1)
        assert status.timeout_agents == [2], (
            f"Expected [2], got {status.timeout_agents} (step={status.step})"
        )

    async def test_timeout_does_not_hang(self):
        """Agent that timed out can submit next round normally."""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=0.2,
        )

        # Round 1: only agent 0 submits → agent 1 times out
        await barrier.submit_action(0, "MoveAhead")
        assert barrier.get_run_status().step == 1

        # Round 2: both submit normally
        async def agent_0():
            return await barrier.submit_action(0, "MoveAhead")

        async def agent_1():
            return await barrier.submit_action(1, "RotateLeft")

        await asyncio.gather(agent_0(), agent_1())
        assert barrier.get_run_status().step == 2


@pytest.mark.asyncio
class TestRequestStop:
    """request_stop() → all blocking submit_action return immediately."""

    async def test_request_stop_wakes_waiters(self):
        ctrl = FakeController(delay_seconds=0.5)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=10,
            step_timeout=5.0,
        )

        # Agent 0 submits and blocks waiting for agent 1
        async def waiter():
            return await barrier.submit_action(0, "WaitAction")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # Let agent 0 start waiting

        # Stop from "outside"
        barrier.request_stop("test_cancel")
        result = await task

        assert result.success is False
        status = barrier.get_run_status()
        assert status.stopped is True
        assert status.stop_reason == "test_cancel"

    async def test_request_stop_is_idempotent(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        barrier.request_stop("first")
        barrier.request_stop("second")  # should be no-op
        assert barrier.get_run_status().stop_reason == "first"

    async def test_submit_after_stop_returns_empty(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        barrier.request_stop("done")
        result = await barrier.submit_action(0, "MoveAhead")
        assert result.success is False
        assert result.observation == ""


@pytest.mark.asyncio
class TestRunStatus:
    """get_run_status() field completeness."""

    async def test_fields_populated(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=50,
            step_timeout=5.0,
        )

        status = barrier.get_run_status()
        assert status.step == 0
        assert status.max_steps == 50
        assert status.finished is False
        assert status.stopped is False
        assert status.stop_reason == ""
        assert status.timeout_agents == []
        assert isinstance(status.domain_metrics, dict)

        # After one round
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        status = barrier.get_run_status()
        assert status.step == 1
        assert "round_success" in status.domain_metrics

    async def test_max_steps_reached(self):
        """Budget exhaustion (P5-3): last round executes, then the gate closes.

        ``max_steps`` is a step budget, not task success — the design-doc §5.3
        success truth requires postcondition verification, so
        ``get_metrics()["finished"]`` stays False.  The *run* terminates:
        ``is_finished()`` / ``status.finished`` flip True (自然收官, kernel
        contract in ``tests/test_run_control.py``) while ``stopped`` stays
        False, and further submissions are refused without executing a round.
        """
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=2, step_timeout=5.0
        )

        # Round 1
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.is_finished() is False

        # Round 2 — consumes the budget
        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert barrier.is_finished() is True  # run over (natural end)
        status = barrier.get_run_status()
        assert status.step == 2
        assert status.finished is True
        assert status.stopped is False
        assert barrier.get_metrics()["finished"] is False  # budget ≠ success
        assert barrier.get_metrics()["steps"] == 2
        assert ctrl.step_call_count == 4

        # Budget gate: further submissions are refused, no extra round runs
        refused = await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert all(not r.success for r in refused)
        assert barrier.get_metrics()["steps"] == 2
        assert ctrl.step_call_count == 4

    async def test_state_verdict_complete_but_tally_unfilled_is_not_finished(self):
        """(b) verifier True 而 tracker 未满 → finished=False。

        理论上不该出现：搬运动作真的执行过时，tracker 的动作证据账应同步
        记满；出现该组合即暴露 tracker 记账缺陷（动作证据漏记），而不是
        run 该判成功的理由——成功真值只看 tracker（论文口径）。
        """
        from ai2thor_orch.contracts.task import load_task

        groceries = ["Bread", "Tomato", "Lettuce", "Apple", "Potato"]
        metadata = {
            "agents": [
                {"name": "Agent0", "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                 "rotation": {}, "inventory": {"objects": []}},
                {"name": "Agent1", "position": {"x": 1.0, "y": 0.0, "z": 0.5},
                 "rotation": {}, "inventory": {"objects": []}},
            ],
            "objects": [
                {"objectType": name, "parentReceptacles": ["Fridge"]}
                for name in groceries
            ],
            "lastActionSuccess": True,
        }
        ctrl = FakeController(metadata_override=metadata)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=5,
            step_timeout=5.0,
            contract=load_task("3_transport_groceries"),
        )

        results = await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )
        assert all(r.success for r in results)

        # 状态级审计照旧：verdict 计算、写入、可查（审计字段一个不删）
        assert (
            barrier.get_run_status().domain_metrics["verified_completion"] is True
        )
        assert barrier.get_metrics()["coverage"] == 1.0
        assert barrier.drain_step_logs()[0]["verified_completion"] is True

        # 论文口径成功真值：动作证据账未记满 → 不判完成、不提前收官
        assert barrier.get_task_metrics()["completed_subtask_count"] == 0
        assert barrier.get_metrics()["finished"] is False
        assert barrier.is_finished() is False
        assert barrier.get_run_status().finished is False


class TestIsFinished:
    def test_initial_not_finished(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )
        assert barrier.is_finished() is False

    def test_finished_after_stop(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )
        barrier.stop()
        assert barrier.is_finished() is True


class TestSnapshotPublic:
    def test_snapshot_returns_observation(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )

        # No round executed yet
        obs = barrier.snapshot_public(0)
        assert obs.agent_idx == 0
        assert obs.step == 0
        assert obs.text == ""

    @pytest.mark.asyncio
    async def test_snapshot_after_action(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=10
        )

        await barrier.submit_action(0, "MoveAhead")
        obs = barrier.snapshot_public(0)
        assert obs.step == 1
        assert obs.text != ""
        # visible_objects should be aliased
        for v in obs.visible_objects:
            assert "|" not in v  # no raw objectId

    @pytest.mark.asyncio
    async def test_snapshot_filters_hidden_objects(self):
        """visible=False 对象不得进入 worker 视野（RP1b：全屋清单冒充视野）。"""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        obs = barrier.snapshot_public(0)

        assert "Mug_1" in obs.visible_objects
        assert "Apple_1" in obs.visible_objects
        # 隐藏对象（visible=False）：既无别名，也不得带出原始 objectId 线索
        assert "Knife_1" not in obs.visible_objects
        assert not any("Knife" in v for v in obs.visible_objects)

    @pytest.mark.asyncio
    async def test_snapshot_all_hidden_is_empty_not_error(self):
        """全部对象 hidden 时：空列表，不得抛异常。"""
        metadata = make_default_metadata(
            scene="FloorPlan1", num_agents=1, has_objects=True
        )
        for obj in metadata["objects"]:
            obj["visible"] = False
        ctrl = FakeController(metadata_override=metadata)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        obs = barrier.snapshot_public(0)
        assert obs.visible_objects == []

    @pytest.mark.asyncio
    async def test_snapshot_missing_visible_key_treated_as_visible(self):
        """metadata 缺 visible 键 → 按可见处理（保守口径，与 coordinator 一致）。"""
        metadata = make_default_metadata(
            scene="FloorPlan1", num_agents=1, has_objects=True
        )
        for obj in metadata["objects"]:
            obj.pop("visible", None)
        ctrl = FakeController(metadata_override=metadata)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        obs = barrier.snapshot_public(0)
        # 4 个对象（含无键的 Knife）全部按可见处理
        assert len(obs.visible_objects) == 4

    def test_snapshot_invalid_agent(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )
        with pytest.raises(ValueError):
            barrier.snapshot_public(5)


class TestSnapshotCoordinator:
    @pytest.mark.asyncio
    async def test_coordinator_snapshot(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2, executor=exec_, max_steps=10
        )

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )

        snap = barrier.snapshot_coordinator()
        assert snap.round_no > 0
        assert len(snap.agents) == 2
        assert snap.step == 1
        # Objects should have aliases
        if snap.objects:
            assert "alias" in snap.objects[0]

    @pytest.mark.asyncio
    async def test_coordinator_snapshot_filters_hidden_objects(self):
        """visible=False 对象不得进入 coordinator 视图（与 worker 口径一致）。"""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=2, executor=exec_, max_steps=10)

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )

        snap = barrier.snapshot_coordinator()
        aliases = [obj.get("alias", "") for obj in snap.objects]
        raw_ids = [obj.get("objectId", "") for obj in snap.objects]
        assert "Mug_1" in aliases
        assert "Knife_1" not in aliases
        assert not any("Knife" in rid for rid in raw_ids)

    @pytest.mark.asyncio
    async def test_coordinator_snapshot_fills_scene_name(self):
        """Scene 从 metadata sceneName 回填（RP1b：Scene:  | 空渲染）。"""
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        snap = barrier.snapshot_coordinator()
        assert snap.scene == "FloorPlan1"

    @pytest.mark.asyncio
    async def test_coordinator_snapshot_empty_when_all_hidden(self):
        """全部 hidden：objects 为空列表，scene 仍回填，不得抛异常。"""
        metadata = make_default_metadata(
            scene="FloorPlan1", num_agents=1, has_objects=True
        )
        for obj in metadata["objects"]:
            obj["visible"] = False
        ctrl = FakeController(metadata_override=metadata)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        snap = barrier.snapshot_coordinator()
        assert snap.objects == []
        assert snap.scene == "FloorPlan1"

    @pytest.mark.asyncio
    async def test_coordinator_snapshot_missing_visible_key(self):
        """缺 visible 键 → 按可见处理（两处口径一致的回归钉子）。"""
        metadata = make_default_metadata(
            scene="FloorPlan1", num_agents=1, has_objects=True
        )
        for obj in metadata["objects"]:
            obj.pop("visible", None)
        ctrl = FakeController(metadata_override=metadata)
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10)

        await barrier.submit_action(0, "MoveAhead")
        snap = barrier.snapshot_coordinator()
        assert len(snap.objects) == 4


class TestConstructor:
    def test_invalid_num_agents(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=0, executor=exec_, max_steps=10)

    def test_invalid_max_steps(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=0)

    def test_invalid_step_timeout(self):
        ctrl = FakeController()
        exec_ = ControllerExecutor(ctrl)
        with pytest.raises(ValueError):
            AI2ThorBarrier(num_agents=1, executor=exec_, max_steps=10, step_timeout=0)


# ── P5-1: 三路径对齐（advance=False 全 idle 占位）+ NoOp 来源标记 ────────────


def _make_barrier(
    num_agents: int = 2, *, max_steps: int = 10, step_timeout: float = 5.0
):
    """Build a (FakeController, AI2ThorBarrier) pair for P5-1 tests."""
    ctrl = FakeController()
    exec_ = ControllerExecutor(ctrl)
    barrier = AI2ThorBarrier(
        num_agents=num_agents,
        executor=exec_,
        max_steps=max_steps,
        step_timeout=step_timeout,
    )
    return ctrl, barrier


@pytest.mark.asyncio
class TestIdlePlaceholderPath:
    """advance=False 全 idle 占位：无限等待、不烧 step；真实动作触发执行。"""

    async def test_all_idle_placeholders_never_reach_timeout_fill(self):
        ctrl, barrier = _make_barrier(step_timeout=0.2)

        t0 = asyncio.create_task(barrier.submit_action(0, "NoOp", advance=False))
        t1 = asyncio.create_task(barrier.submit_action(1, "NoOp", advance=False))
        # 远超 step_timeout：占位状态不得触发超时补 NoOp（否则会烧掉一步）
        await asyncio.sleep(0.5)

        assert not t0.done() and not t1.done()
        assert ctrl.step_call_count == 0
        assert barrier.get_run_status().step == 0

        # 同一 agent 的真实动作覆盖自己的占位槽 → 触发执行
        real = await barrier.submit_action(0, "MoveAhead")
        r0 = await asyncio.wait_for(t0, timeout=5.0)
        r1 = await asyncio.wait_for(t1, timeout=5.0)

        assert real.success is True and real.action == "MoveAhead"
        assert r0.success is True and r1.success is True
        assert barrier.get_run_status().step == 1
        # 两个 agent 槽位各一次 controller.step 调用
        assert ctrl.step_call_count == 2

    async def test_all_idle_placeholders_stop_releases_waiters(self):
        ctrl, barrier = _make_barrier(step_timeout=0.2)

        t0 = asyncio.create_task(barrier.submit_action(0, "NoOp", advance=False))
        t1 = asyncio.create_task(barrier.submit_action(1, "NoOp", advance=False))
        await asyncio.sleep(0.05)

        barrier.request_stop("cancel:idle")

        r0 = await asyncio.wait_for(t0, timeout=5.0)
        r1 = await asyncio.wait_for(t1, timeout=5.0)
        assert r0.success is False and r1.success is False
        assert ctrl.step_call_count == 0
        assert barrier.get_run_status().step == 0
        assert barrier.is_finished() is True


@pytest.mark.asyncio
class TestNoOpSourceMarking:
    """NoOp 来源标记：timeout_injected / llm / idle_heartbeat；真实动作恒 ""。"""

    async def test_timeout_injected_source_recorded(self):
        _, barrier = _make_barrier(step_timeout=0.2)

        await barrier.submit_action(0, "MoveAhead")

        status = barrier.get_run_status()
        assert status.timeout_agents == [1]
        assert status.domain_metrics["noop_sources"] == ["", "timeout_injected"]

        log = barrier.get_last_round_log()
        assert log["step"] == 1
        assert log["finished"] is False
        assert log["actions"] == ["MoveAhead", "NoOp"]
        assert log["noop_sources"] == ["", "timeout_injected"]
        assert log["timeout_agents"] == [1]

    async def test_llm_noop_source_recorded(self):
        _, barrier = _make_barrier()

        await asyncio.gather(
            barrier.submit_action(0, "NoOp"),
            barrier.submit_action(1, "MoveAhead"),
        )

        log = barrier.get_last_round_log()
        assert log["noop_sources"] == ["llm", ""]
        assert log["timeout_agents"] == []

    async def test_idle_heartbeat_source_recorded(self):
        _, barrier = _make_barrier()

        t1 = asyncio.create_task(
            barrier.submit_action(1, "NoOp", advance=False, source="idle_heartbeat")
        )
        await asyncio.sleep(0.05)
        await barrier.submit_action(0, "MoveAhead")
        await asyncio.wait_for(t1, timeout=5.0)

        assert barrier.get_last_round_log()["noop_sources"] == ["", "idle_heartbeat"]

    async def test_real_action_source_is_empty_even_when_pinned(self):
        _, barrier = _make_barrier()

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead", source="llm"),
            barrier.submit_action(1, "RotateLeft"),
        )

        assert barrier.get_last_round_log()["noop_sources"] == ["", ""]

    async def test_invalid_explicit_source_rejected(self):
        _, barrier = _make_barrier()

        with pytest.raises(ValueError):
            await barrier.submit_action(0, "NoOp", source="bogus")

        assert barrier.get_run_status().step == 0


# ── P5-3: 装配消费面（get_metrics / drain_step_logs / 逐回合验证）───────────


def _fridge_metadata(groceries: list[str] | None = None) -> dict:
    """Controller metadata with every grocery inside the Fridge (verified)."""
    groceries = groceries or ["Bread", "Tomato", "Lettuce", "Apple", "Potato"]
    return {
        "agents": [
            {"name": "Agent0", "position": {"x": 0.0, "y": 0.0, "z": 0.0},
             "rotation": {}, "inventory": {"objects": []}},
            {"name": "Agent1", "position": {"x": 1.0, "y": 0.0, "z": 0.5},
             "rotation": {}, "inventory": {"objects": []}},
        ],
        "objects": [
            {"objectType": name, "parentReceptacles": ["Fridge"]} for name in groceries
        ],
        "lastActionSuccess": True,
    }


@pytest.mark.asyncio
class TestAssemblySurface:
    """P5-3：装配 poll 循环消费的 barrier 表面（与 SARBarrier 对齐）。"""

    async def test_get_metrics_neutral_without_contract(self):
        _, barrier = _make_barrier()
        assert barrier.get_metrics() == {
            "coverage": 0.0,
            "transport_rate": 0.0,
            "interaction_coverage": 0.0,
            "steps": 0,
            "finished": False,
        }
        assert barrier.get_task_metrics() == {}

    async def test_drain_step_logs_returns_every_round_and_clears(self):
        _, barrier = _make_barrier()
        assert barrier.drain_step_logs() == []

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )

        drained = barrier.drain_step_logs()
        assert len(drained) == 1
        entry = drained[0]
        assert entry["step"] == 1
        assert entry["actions"] == ["MoveAhead", "RotateLeft"]
        assert entry["successes"] == [True, True]
        assert entry["observations"][0] != ""
        assert entry["noop_sources"] == ["", ""]
        assert entry["timeout_agents"] == []
        assert entry["error_types"] == []
        assert entry["completed_subtasks_delta"] == []
        assert entry["finished"] is False
        assert entry["step_duration_ms"] > 0
        # A drain clears the buffer (no duplicate rows on the next poll).
        assert barrier.drain_step_logs() == []

    async def test_multiple_rounds_between_polls_are_not_lost(self):
        _, barrier = _make_barrier()
        for _ in range(3):
            await asyncio.gather(
                barrier.submit_action(0, "MoveAhead"),
                barrier.submit_action(1, "MoveAhead"),
            )

        drained = barrier.drain_step_logs()
        assert [entry["step"] for entry in drained] == [1, 2, 3]
        assert all(entry["actions"] == ["MoveAhead", "MoveAhead"] for entry in drained)

    async def test_timeout_round_records_noop_provenance_in_step_log(self):
        _, barrier = _make_barrier(step_timeout=0.2)
        await barrier.submit_action(0, "MoveAhead")

        entry = barrier.drain_step_logs()[0]
        assert entry["noop_sources"] == ["", "timeout_injected"]
        assert entry["timeout_agents"] == [1]
        assert entry["actions"] == ["MoveAhead", "NoOp"]

    async def test_contract_round_records_verifier_and_metrics_in_step_log(self):
        from ai2thor_orch.contracts.task import load_task

        ctrl = FakeController(metadata_override=_fridge_metadata())
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=2,
            executor=exec_,
            max_steps=5,
            step_timeout=5.0,
            contract=load_task("3_transport_groceries"),
        )

        await asyncio.gather(
            barrier.submit_action(0, "MoveAhead"),
            barrier.submit_action(1, "RotateLeft"),
        )

        entry = barrier.drain_step_logs()[0]
        assert entry["verified_completion"] is True
        assert entry["coverage"] == 1.0
        # Neither action carries an object argument → no interaction coverage
        assert entry["interaction_coverage"] == 0.0
        assert entry["transport_rate"] == 0.0  # no matching subtask executed

        metrics = barrier.get_metrics()
        assert metrics["coverage"] == 1.0
        # 状态级审计 ≠ 论文口径成功真值（动作证据账未记 → finished False）
        assert metrics["finished"] is False

        # Tracker snapshot: cumulative reliability metrics, fed to summary.json
        task_metrics = barrier.get_task_metrics()
        assert task_metrics["action_attempts"] == 2
        assert task_metrics["successful_actions"] == 2
        assert task_metrics["action_success_rate"] == 1.0
        assert task_metrics["balance"] == 1.0
        assert task_metrics["total_subtasks"] > 0

    async def test_failed_action_records_error_type(self):
        class _FailController(FakeController):
            def step(self, action_or_dict):
                event = super().step(action_or_dict)
                event.metadata["lastActionSuccess"] = False
                event.metadata["errorMessage"] = "object not visible"
                return event

        exec_ = ControllerExecutor(_FailController())
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=5, step_timeout=5.0
        )
        await barrier.submit_action(0, "PickupObject(Mug_1)")

        entry = barrier.drain_step_logs()[0]
        assert entry["successes"] == [False]
        assert entry["error_types"] == ["object not visible"]

    async def test_execution_error_still_records_step_log(self):
        ctrl = FakeController(fail_on_action="MoveAhead")
        exec_ = ControllerExecutor(ctrl)
        barrier = AI2ThorBarrier(
            num_agents=1, executor=exec_, max_steps=5, step_timeout=5.0
        )

        result = await barrier.submit_action(0, "MoveAhead")
        assert result.success is False
        assert "Execution error" in result.observation

        entry = barrier.drain_step_logs()[0]
        assert entry["step"] == 1
        assert entry["successes"] == [False]
        assert "execution_error" in entry["error_types"][0]
        assert barrier.get_metrics()["steps"] == 1


# ── 论文口径成功真值：finished 挂 tracker 22/22（verifier 降级审计）─────────


_GROCERIES = ("Bread", "Tomato", "Lettuce", "Apple", "Potato")


class _InventoryTrackingController:
    """FakeController 变体：跨回合维护 agent 库存，使搬运序列能记满 22/22。

    Tracker 给 ``NavigateTo(Fridge, X)`` / ``PutObject(Fridge, X)`` 记账用的是
    **上一回合**的库存，所以假控制器必须跨 ``step()`` 保持库存状态：
    ``PickupObject(X)`` 后库存 = [X]，``PutObject`` 后清空。基础
    FakeController 恒报空库存（够 verifier 用，永远记不满动作证据账）。

    Args:
        in_fridge: 物体终态是否已在 Fridge（决定 verifier 审计判定）。
    """

    def __init__(self, *, in_fridge: bool = False) -> None:
        self._inventory: list[str] = []
        self._in_fridge = in_fridge
        self.step_call_count = 0

    def step(self, action_or_dict: str | dict) -> FakeEvent:
        self.step_call_count += 1
        action = (
            action_or_dict
            if isinstance(action_or_dict, str)
            else str(action_or_dict.get("action", ""))
        )
        if action.startswith("PickupObject("):
            raw = action.split("(", 1)[1].rstrip(")")
            self._inventory = [raw.split("|", 1)[0].split("_", 1)[0]]
        elif action.startswith("PutObject("):
            self._inventory = []

        metadata = {
            "agents": [
                {
                    "name": "Agent0",
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "rotation": {},
                    "inventory": {
                        "objects": [{"objectType": name} for name in self._inventory]
                    },
                }
            ],
            "objects": [
                {
                    "objectType": name,
                    "parentReceptacles": ["Fridge"] if self._in_fridge else [],
                }
                for name in _GROCERIES
            ],
            "sceneName": "FloorPlan1",
            "lastActionSuccess": True,
        }
        return FakeEvent(metadata=metadata)


def _full_transport_sequence() -> list[str]:
    """能记满 22/22 的动作序列：open + 5×(pickup/put) + close，共 12 回合。"""
    sequence = ["OpenObject(Fridge_1)"]
    for grocery in _GROCERIES:
        sequence.extend([f"PickupObject({grocery}_1)", "PutObject(Fridge_1)"])
    sequence.append("CloseObject(Fridge_1)")
    return sequence


@pytest.mark.asyncio
class TestPaperGaugeFinished:
    """成功真值换挂（论文口径）：finished = tracker 动作证据账记满。

    论文 baseline（llamar/coela）以 ``checker.check_success()`` = 22/22
    子任务判 finished，从不校验物体终态；状态级 verifier 降级为审计字段
    （``domain_metrics["verified_completion"]`` / ``coverage`` 照常写入）。
    """

    async def test_tracker_tally_full_sets_finished_and_end_reason_success(self):
        """(a) tracker 满 22/22 且 verifier False → finished=True、success。"""
        from ai2thor_orch.contracts.task import load_task
        from orchestration.assembly import classify_end_reason

        ctrl = _InventoryTrackingController(in_fridge=False)
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=16,
            step_timeout=5.0,
            contract=load_task("3_transport_groceries"),
        )

        for action in _full_transport_sequence():
            result = await barrier.submit_action(0, action)
            assert result.success is True

        task_metrics = barrier.get_task_metrics()
        assert task_metrics["completed_subtask_count"] == 22
        assert task_metrics["total_subtasks"] == 22
        assert task_metrics["transport_rate"] == 1.0

        # (a-1) 成功真值 = 动作证据账记满，与状态级 verifier 无关
        metrics = barrier.get_metrics()
        assert metrics["finished"] is True
        assert barrier.is_finished() is True
        assert barrier.get_run_status().finished is True

        # (a-2) 审计字段保留：物体终态未满足 → verdict False / coverage 0
        status = barrier.get_run_status()
        assert status.domain_metrics["verified_completion"] is False
        assert metrics["coverage"] == 0.0
        assert barrier.drain_step_logs()[-1]["verified_completion"] is False

        # (a-3) end_reason 分类链（实现点：orchestration.assembly.classify_end_reason）：
        # finished=True 先于步数判定 → success（而非 max_steps_reached）
        assert (
            classify_end_reason(
                finished=metrics["finished"],
                steps=metrics["steps"],
                max_steps=16,
                elapsed_seconds=1.0,
                wall_clock_limit=3600.0,
                a2a_done=False,
                a2a_error=False,
                coordinator_error=False,
            )
            == "success"
        )

        # (a-4) 提前收官：记满后新提交被拒，不再烧步
        steps_before = metrics["steps"]
        refused = await barrier.submit_action(0, "MoveAhead")
        assert refused.success is False
        assert barrier.get_metrics()["steps"] == steps_before

    async def test_budget_exhausted_without_full_tally_is_max_steps_reached(self):
        """预算耗尽且未满 → 非 success；end_reason=max_steps_reached。"""
        from ai2thor_orch.contracts.task import load_task
        from orchestration.assembly import classify_end_reason

        ctrl = _InventoryTrackingController(in_fridge=False)
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=3,
            step_timeout=5.0,
            contract=load_task("3_transport_groceries"),
        )

        for action in _full_transport_sequence()[:3]:
            await barrier.submit_action(0, action)

        task_metrics = barrier.get_task_metrics()
        completed = task_metrics["completed_subtask_count"]
        assert 0 < completed < task_metrics["total_subtasks"]

        metrics = barrier.get_metrics()
        assert metrics["finished"] is False
        assert barrier.is_finished() is True  # 预算自然收官
        assert barrier.get_run_status().stopped is False
        assert (
            classify_end_reason(
                finished=metrics["finished"],
                steps=metrics["steps"],
                max_steps=3,
                elapsed_seconds=1.0,
                wall_clock_limit=3600.0,
                a2a_done=False,
                a2a_error=False,
                coordinator_error=False,
            )
            == "max_steps_reached"
        )

    async def test_no_contract_success_truth_only_via_stop_or_budget(self):
        """(c) 无 contract → tracker 不接线：动作跑满也只是普通回合；

        成功真值恒 False（动作不能把它翻真），run 终止仅能由 stop()/预算
        触发——``request_stop`` 置位 ``_finished`` 属既有停止语义。
        """
        ctrl = _InventoryTrackingController()
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=16,
            step_timeout=5.0,
        )

        for action in _full_transport_sequence():
            result = await barrier.submit_action(0, action)
            assert result.success is True

        assert barrier.get_metrics()["finished"] is False
        assert barrier.get_task_metrics() == {}
        assert barrier.is_finished() is False

        barrier.request_stop("test_stop")
        assert barrier.is_finished() is True
        assert barrier.get_run_status().stopped is True


@pytest.mark.asyncio
class TestNavigateQuerySurface:
    """F-nav 只读查询面：latest_object_metadata / query_reachable_positions /
    dict 宏动作回合（navigate → Teleport）。"""

    async def test_dict_macro_action_rides_through_the_round(self):
        """dict 动作原样到达 controller；回合日志按动作名标注。"""
        ctrl = FakeController()
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=5,
            step_timeout=5.0,
        )
        action = {
            "action": "Teleport",
            "position": {"x": -1.0, "y": 0.9, "z": 0.5},
            "rotation": {"x": 0.0, "y": 90.0, "z": 0.0},
        }

        result = await barrier.submit_action(0, action)

        assert result.success is True
        assert ctrl.actions_received[-1]["action"] == "Teleport"
        assert ctrl.actions_received[-1]["raw"] == action
        assert barrier.get_last_round_log()["actions"] == ["Teleport"]
        assert barrier.get_run_status().step == 1

    async def test_latest_object_metadata_reads_latest_round(self):
        metadata = make_default_metadata(num_agents=1, has_objects=True)
        ctrl = FakeController(metadata_override=metadata)
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=5,
            step_timeout=5.0,
        )

        # 没有任何回合 → fail-closed None
        assert barrier.latest_object_metadata("Mug|-01.5|+00.9|+02.3") is None

        await barrier.submit_action(0, "Pass")

        entry = barrier.latest_object_metadata("Mug|-01.5|+00.9|+02.3")
        assert entry is not None
        assert entry["objectType"] == "Mug"
        assert entry["position"] == {"x": -1.5, "y": 0.9, "z": 2.3}
        # 返回副本：改动不污染回合状态
        entry["position"]["x"] = 999.0
        reread = barrier.latest_object_metadata("Mug|-01.5|+00.9|+02.3")
        assert reread is not None
        assert reread["position"]["x"] == -1.5
        # 未见过 / 未登记的对象 → None
        assert barrier.latest_object_metadata("Ghost|+09.0|+09.0|+09.0") is None

    async def test_query_reachable_positions_cached_and_fail_closed(self):
        ctrl = FakeController()
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=5,
            step_timeout=5.0,
        )

        positions = await barrier.query_reachable_positions()
        assert positions == [
            {"x": float(i), "y": 0.0, "z": float(i)} for i in range(10)
        ]
        # 查询经 executor 下发（一次 controller 调用），不烧 barrier 回合
        assert ctrl.step_call_count == 1
        assert barrier.get_run_status().step == 0

        # 第二次：命中缓存，不再触碰 controller
        assert await barrier.query_reachable_positions() == positions
        assert ctrl.step_call_count == 1

        # 返回副本：外部改动不污染缓存
        positions[0]["x"] = 999.0
        assert (await barrier.query_reachable_positions())[0]["x"] == 0.0

    async def test_query_reachable_positions_fail_closed_when_stopped(self):
        ctrl = FakeController()
        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=5,
            step_timeout=5.0,
        )
        barrier.request_stop("test_stop")

        assert await barrier.query_reachable_positions() == []
        assert ctrl.step_call_count == 0

    async def test_query_reachable_positions_empty_on_controller_error(self):
        # 注入式故障：step 抛 RuntimeError → 查询 fail-closed 返回空
        ctrl = FakeController(
            metadata_override=make_default_metadata(num_agents=1),
            fail_on_action="GetReachablePositions",
        )

        barrier = AI2ThorBarrier(
            num_agents=1,
            executor=ControllerExecutor(ctrl),
            max_steps=5,
            step_timeout=5.0,
        )
        assert await barrier.query_reachable_positions() == []
        # 失败不缓存：后续重试仍会（重新）查询
        assert await barrier.query_reachable_positions() == []
