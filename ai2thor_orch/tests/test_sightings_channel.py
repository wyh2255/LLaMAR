"""P2 sightings 通道集成单测（SightingStore → barrier 写点 → provider → context 读点）。

覆盖（设计 ``.hermes/ai2thor/20260917-...-design.md`` §4.3 契约逐条）：

- barrier 写点：``_execute_round`` 末尾从 **visible 过滤后的感知面** 入账
  （隐藏对象零入账、raw objectId 零泄漏、step 编号同 step log 口径、
  错误回合不入账、去重跨回合保留最新 + 首见）；
- provider 读点：payload 增 ``sightings``（最新优先；store 未接线 → 空列表）；
- coordinator ``### Sightings`` 段渲染 + 预算截断（最新 K 条，缺省 30，
  config 可调）+ 与既有各段隔离（空则整段省略）；
- worker 侧不注入；env_pack 接线（run_dir → store；budget → session）；
- experiment 装配层透传 run_dir。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.executor.controller_executor import ControllerExecutor
from ai2thor_orch.memory.sighting_store import SightingStore
from ai2thor_orch.state.context import (
    AI2ThorCoordinatorContextManager,
    AI2ThorWorkerContextManager,
)
from ai2thor_orch.state.coordinator_state_provider import (
    AI2ThorCoordinatorStateProvider,
)
from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider
from ai2thor_orch.tests.fakes import FakeController, make_default_metadata

# fake metadata 的感知面（visible=True）= 3 件；Knife 为 visible=False 样本。
_VISIBLE_ALIASES = {"Mug_1", "CounterTop_1", "Apple_1"}


def _metadata(num_agents: int = 2, names: tuple[str, ...] = ("Alice", "Bob")):
    meta = make_default_metadata(
        scene="FloorPlan1", num_agents=num_agents, has_objects=True
    )
    for i in range(min(num_agents, len(names))):
        meta["agents"][i]["name"] = names[i]
    return meta


def _barrier(store=None, *, metadata=None, num_agents: int = 2, controller=None):
    ctrl = (
        controller
        if controller is not None
        else FakeController(metadata_override=metadata or _metadata(num_agents))
    )
    return AI2ThorBarrier(
        num_agents=num_agents,
        executor=ControllerExecutor(ctrl),
        max_steps=10,
        step_timeout=5.0,
        sighting_store=store,
    )


async def _run_round(barrier, num_agents: int = 2, action: str = "MoveAhead") -> None:
    await asyncio.gather(
        *(barrier.submit_action(i, action) for i in range(num_agents))
    )


def _coordinator_render(barrier) -> str:
    provider = AI2ThorCoordinatorStateProvider(barrier)
    ctx = AI2ThorCoordinatorContextManager(state_provider=provider)
    ctx.refresh_runtime_state()
    return ctx._render_environment_view()


def _coordinator_ctx(barrier):
    """CoordinatorEnv 替身（与 test_env_pack._coordinator_ctx 同形状）。"""
    from orchestration.env_pack import CoordinatorEnv

    return CoordinatorEnv(
        barrier=barrier,
        log_dir=None,
        run_id="test-run",
        state_mode="semantic",
        max_steps=50,
        model="test-model",
        api_base="http://localhost:1",
        api_key_env="TEST_KEY",
        exp_logger=None,
        event_store=None,
        supervision_state_store=None,
        user_command_queue=None,
        memory_read_mode="legacy",
        long_term_mode="off",
        long_term_store=None,
        diagnosis_store=None,
        diagnosis_config=None,
        prune_policy="count_window",
    )


# ── barrier 写点（自动 ingest 感知面）───────────────────────────────────────


class TestBarrierWritePoint:
    async def test_ingests_visible_objects_only(self, tmp_path):
        """只 ingest visible 过滤后的感知面；隐藏对象与 raw id 零入账。"""
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(store)
        await _run_round(barrier)

        entries = store.latest_sightings()
        assert store.count == 6  # 3 件可见对象 × 2 agent
        assert {e["alias"] for e in entries} == _VISIBLE_ALIASES
        assert {e["agent"] for e in entries} == {"Alice", "Bob"}
        assert all(e["step"] == 1 and e["first_seen_step"] == 1 for e in entries)
        assert all(e["visible_now"] is True for e in entries)

        mug = next(
            e for e in entries if e["alias"] == "Mug_1" and e["agent"] == "Alice"
        )
        assert mug["object_type"] == "Mug"
        assert (mug["x"], mug["z"]) == (-1.5, 2.3)

        text = (tmp_path / "sightings.ndjson").read_text(encoding="utf-8")
        assert "Knife" not in text  # visible=False 永不入账（真值边界不破）
        assert not barrier.alias_registry.is_raw_id_leaked(text)
        assert len(text.strip().splitlines()) == 6

    async def test_step_numbers_and_dedupe_across_rounds(self, tmp_path):
        """step 编号同 step log（1-based）；去重键保留最新 + 首见 step。"""
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(store)
        await _run_round(barrier)
        await _run_round(barrier)

        assert store.count == 6  # 去重键 (alias, agent)
        entries = store.latest_sightings()
        assert all(e["step"] == 2 for e in entries)  # 保留最新 sighting
        assert all(e["first_seen_step"] == 1 for e in entries)  # 首见保留

        lines = [
            json.loads(line)
            for line in (tmp_path / "sightings.ndjson")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        assert len(lines) == 12  # 2 回合 × 6 条（append-only 全量）
        assert [rec["step"] for rec in lines] == [1] * 6 + [2] * 6

    async def test_error_round_ingests_nothing(self, tmp_path):
        """执行异常回合（无有效 metadata）不入账，跑批不因 store 中断。"""
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(
            store, controller=FakeController(fail_on_action="MoveAhead")
        )
        await _run_round(barrier)

        assert store.count == 0
        assert barrier.get_run_status().step == 1  # 回合照常推进
        assert not (tmp_path / "sightings.ndjson").exists()

    async def test_barrier_without_store_keeps_legacy_behavior(self):
        """store 未接线（默认 None）：行为与接线前一致，不报错。"""
        barrier = _barrier(None)
        assert barrier.sighting_store is None
        await _run_round(barrier)
        assert barrier.get_run_status().step == 1
        assert barrier.snapshot_coordinator().step == 1


# ── provider 读点 ─────────────────────────────────────────────────────────


class TestProviderReadPoint:
    async def test_payload_sightings_newest_first(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(store)
        await _run_round(barrier)
        await _run_round(barrier)

        state = AI2ThorCoordinatorStateProvider(barrier).snapshot()
        sightings = state.payload["sightings"]
        assert isinstance(sightings, list) and len(sightings) == 6
        steps = [s["step"] for s in sightings]
        assert steps == sorted(steps, reverse=True)

    def test_payload_sightings_empty_without_store(self):
        barrier = _barrier(None)
        state = AI2ThorCoordinatorStateProvider(barrier).snapshot()
        assert state.payload["sightings"] == []


# ── coordinator 渲染点（### Sightings 段 + 预算截断）─────────────────────


class TestCoordinatorRender:
    async def test_render_contains_sightings_section(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(store)
        await _run_round(barrier)

        env = _coordinator_render(barrier)
        assert "### Sightings" in env
        assert "step 1 Alice saw Mug_1 (-1.5, 2.3)" in env

        lines = env.splitlines()
        idx = lines.index("### Sightings")
        assert lines[idx - 1] == ""  # 与上段空行隔离（prompt diff 可控）
        assert all(line.startswith("step ") for line in lines[idx + 1 :])
        assert len(lines) - idx - 1 == 6  # 一行一条

    def test_empty_sightings_render_unchanged(self):
        """sightings 为空（store 未接线）：整段省略，渲染保持既有形态。"""
        barrier = _barrier(None)
        env = _coordinator_render(barrier)
        assert "Sightings" not in env
        assert "saw" not in env
        assert env.splitlines()[-1].startswith("Objects of interest:")

    def test_budget_truncates_to_latest_k(self):
        """预算截断 = 最新 K 条（缺省 30）；sightings_budget 可调。"""
        store = SightingStore()  # 内存-only 即可（不跑回合，直接灌 35 条）
        for i in range(1, 36):
            store.record(
                step=i,
                agent="Alice",
                alias=f"Obj_{i}",
                object_type="Obj",
                x=float(i),
                z=0.0,
            )
        barrier = _barrier(store)

        env = _coordinator_render(barrier)
        lines = [line for line in env.splitlines() if line.startswith("step ")]
        assert len(lines) == 30
        assert lines[0] == "step 35 Alice saw Obj_35 (35.0, 0.0)"  # 最新优先
        assert lines[-1] == "step 6 Alice saw Obj_6 (6.0, 0.0)"  # 截掉最旧 5 条

        provider = AI2ThorCoordinatorStateProvider(barrier)
        small = AI2ThorCoordinatorContextManager(
            state_provider=provider, sightings_budget=2
        )
        small.refresh_runtime_state()
        lines2 = [
            line
            for line in small._render_environment_view().splitlines()
            if line.startswith("step ")
        ]
        assert lines2 == [
            "step 35 Alice saw Obj_35 (35.0, 0.0)",
            "step 34 Alice saw Obj_34 (34.0, 0.0)",
        ]

    def test_missing_coordinates_render_placeholder(self):
        store = SightingStore()
        store.record(step=1, agent="Alice", alias="Odd_1", object_type="Odd")
        barrier = _barrier(store)
        env = _coordinator_render(barrier)
        assert "step 1 Alice saw Odd_1 (?, ?)" in env

    def test_invalid_budget_rejected(self):
        with pytest.raises(ValueError, match="sightings_budget"):
            AI2ThorCoordinatorContextManager(sightings_budget=0)


# ── worker 侧不注入（本期间契约）────────────────────────────────────────


class TestWorkerIsolation:
    async def test_worker_render_has_no_sightings(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        barrier = _barrier(store)
        await _run_round(barrier)

        provider = AI2ThorWorkerStateProvider(barrier, 0)
        ctx = AI2ThorWorkerContextManager(state_provider=provider)
        ctx.refresh_runtime_state()
        env = ctx._render_environment_view()
        assert "Sightings" not in env
        assert "saw " not in env
        assert "sightings" not in provider.snapshot().payload


# ── env_pack / experiment 接线 ─────────────────────────────────────────────


class TestEnvPackWiring:
    def _pack(self, **kwargs):
        from ai2thor_orch.env_pack import Ai2ThorEnvPack

        return Ai2ThorEnvPack(**kwargs)

    def test_build_barrier_creates_store_with_run_dir(self, tmp_path):
        pack = self._pack(run_dir=tmp_path)
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=5)
        assert barrier.sighting_store is not None
        assert barrier.sighting_store.path == tmp_path / "sightings.ndjson"

    async def test_pack_store_receives_round_records(self, tmp_path):
        pack = self._pack(run_dir=tmp_path)
        barrier = pack.build_barrier(num_agents=1, seed=42, max_steps=5)
        await barrier.submit_action(0, "MoveAhead")
        assert (tmp_path / "sightings.ndjson").exists()
        assert barrier.sighting_store.count == 3  # fake 感知面 3 件

    def test_pack_without_run_dir_is_memory_only(self):
        pack = self._pack()
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        assert barrier.sighting_store is not None
        assert barrier.sighting_store.path is None

    def test_pack_sightings_budget_reaches_session(self):
        pack = self._pack(sightings_budget=7)
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        pack.build_coordinator_state_provider(_coordinator_ctx(barrier))
        session = pack.build_session_factory(role="coordinator")()
        assert session.sightings_budget == 7

    def test_default_session_budget_is_30(self):
        pack = self._pack()
        barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=5)
        pack.build_coordinator_state_provider(_coordinator_ctx(barrier))
        session = pack.build_session_factory(role="coordinator")()
        assert session.sightings_budget == 30

    def test_pack_rejects_zero_budget(self):
        with pytest.raises(ValueError, match="sightings_budget"):
            self._pack(sightings_budget=0)


class TestExperimentWiring:
    def test_run_experiment_passes_run_dir_to_pack(self, tmp_path, monkeypatch):
        """装配层透传：experiment 的 log_dir 成为 store 落盘目录。"""
        import ai2thor_orch.experiment.ai2thor_experiment as exp_mod

        captured = {}

        async def _spy(spec):
            captured["spec"] = spec
            return {"run_id": spec.run_id, "steps": 0, "finished": False}

        monkeypatch.setattr(exp_mod, "run_assembly", _spy)
        asyncio.run(
            exp_mod.run_experiment(
                num_agents=2,
                seed=42,
                mode="fake",
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        barrier = captured["spec"].env_pack.build_barrier(
            num_agents=2, seed=42, max_steps=4
        )
        assert barrier.sighting_store.path == tmp_path / "sightings.ndjson"


# ── 依赖独立探针（不复用 SAR SemanticMapStore）────────────────────────────


class TestDependencyIndependence:
    def test_memory_package_imports_without_sar_orch(self):
        """ai2thor_orch.memory / barrier 不得引入 sar_orch（设计 §4.3）。"""
        import os
        import subprocess
        import sys
        import textwrap
        from pathlib import Path

        code = textwrap.dedent(
            """
            import sys

            import ai2thor_orch.memory.sighting_store  # noqa: F401
            import ai2thor_orch.barrier.ai2thor_barrier  # noqa: F401

            leaked = sorted(
                m for m in sys.modules
                if m == 'sar_orch' or m.startswith('sar_orch.')
            )
            print('LEAKED=' + repr(leaked))
            sys.exit(1 if leaked else 0)
            """
        ).strip()
        env = dict(os.environ)
        env["PYTHONPATH"] = "src" + os.pathsep + env.get("PYTHONPATH", "")
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
