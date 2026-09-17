"""F-seed spawn 接线（``--spawn-mode`` / ``--spawn-seed``）离线测试。

验收硬口径（卡 t_00358887 / 设计 2026-09-17 §3.2）：

- 传递链 ``CLI → run_experiment → Ai2ThorEnvPack → create_controller → controller``
  全链可断言（fake 路径记录调用；unity 路径经 UnityController 接线）；
- ``spawn_mode=default`` 零副作用（零 ``InitialRandomSpawn`` 调用、构造调用
  形状逐字不变）；
- ``spawn_mode=random`` 失败响亮抛（fail-fast，绝不静默跑默认布局）；
- metadata 双 seed 分列（``seed`` = LLM 采样 / ``spawn_seed`` = 布局）；
- benchmark 侧独立参数面（下传子进程命令行）。

``run_assembly`` 在这类测试中被替换为 ``_SpecSpy``（本套件保持离线确定性；
完整装配循环由根套件与 CLI fake 短跑覆盖）。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import ai2thor_orch.env_pack as env_pack_mod
from ai2thor_orch.executor.unity_controller import (
    UnityController,
    apply_initial_random_spawn,
)
from ai2thor_orch.experiment import ai2thor_experiment
from ai2thor_orch.experiment.ai2thor_experiment import run_experiment
from ai2thor_orch.tests.fakes import FakeController, MockA2TController

pytestmark = pytest.mark.unit

SPAWN_ACTION = "InitialRandomSpawn"


class _SpecSpy:
    """Async ``run_assembly`` 替身：捕获 spec；可选真建 barrier（走装配链）。"""

    def __init__(self, *, build_barrier: bool = False, boom: str | None = None) -> None:
        self.spec: Any = None
        self.barrier: Any = None
        self._build_barrier = build_barrier
        self._boom = boom

    async def __call__(self, spec):
        self.spec = spec
        if self._build_barrier:
            # 用 spec 携带的 env_pack 走真实 build_barrier（create_controller
            # 会被下面的记录桩拦到）——链路后端全部为真实现。
            self.barrier = spec.env_pack.build_barrier(
                num_agents=spec.num_agents, seed=spec.seed, **spec.env_params
            )
        if self._boom is not None:
            raise RuntimeError(self._boom)
        return {
            "run_id": spec.run_id,
            "steps": 3,
            "finished": False,
            "end_reason": "max_steps_reached",
            "elapsed_seconds": 1.0,
        }


def _spy_create_controller(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 ``env_pack.create_controller`` 换成记录桩（透传真实实现）。"""
    captured: dict[str, Any] = {}
    real = env_pack_mod.create_controller

    def _spy(**kwargs: Any) -> Any:
        captured["kwargs"] = dict(kwargs)
        controller = real(**kwargs)
        captured["controller"] = controller
        return controller

    monkeypatch.setattr(env_pack_mod, "create_controller", _spy)
    return captured


class _SpawnMovingMock(MockA2TController):
    """``InitialRandomSpawn`` 落地时挪动 agent 位姿（验证 spawn 后缓存刷新）。"""

    def step(self, action: dict[str, Any]):
        if action.get("action") == SPAWN_ACTION:
            for idx in range(self.agent_count):
                self._positions[idx]["x"] += 100.0
        return super().step(action)


class _RefusingSpawnController:
    """最小替身：``InitialRandomSpawn`` 一律软失败（fail-fast 探针）。

    ``last_event`` 保持 None —— ``UnityController._verify_initial_agent_count``
    对注入式假 controller 早退（agent 数校验不适用）。
    """

    def __init__(self) -> None:
        self.agent_count = 1
        self.steps: list[dict[str, Any]] = []
        self.last_event = None

    def step(self, action: dict[str, Any]) -> Any:
        self.steps.append(dict(action))
        return SimpleNamespace(
            metadata={
                "lastActionSuccess": False,
                "errorMessage": "InitialRandomSpawn: unsupported in this build",
            }
        )


def _make_unity_controller(mock: Any, **kwargs: Any) -> UnityController:
    """UnityController + 注入 mock controller 工厂（不起真机）。"""
    return UnityController(
        scene="FloorPlan1",
        num_agents=mock.agent_count,
        controller_factory=lambda options: mock,
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. CLI 参数面
# ═══════════════════════════════════════════════════════════════════════════


class TestCliSurface:
    def test_cli_threads_spawn_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ai2thor_orch.experiment.__main__ as cli

        captured: dict[str, Any] = {}

        async def fake_run(**kwargs: Any) -> dict:
            captured.update(kwargs)
            return {"finished": False}

        monkeypatch.setattr(cli, "run_experiment", fake_run)
        monkeypatch.setattr(
            sys,
            "argv",
            ["experiment", "--spawn-mode", "random", "--spawn-seed", "11"],
        )
        with pytest.raises(SystemExit):
            asyncio.run(cli.main())

        assert captured["spawn_mode"] == "random"
        assert captured["spawn_seed"] == 11

    def test_cli_defaults_to_default_mode_and_unset_seed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ai2thor_orch.experiment.__main__ as cli

        captured: dict[str, Any] = {}

        async def fake_run(**kwargs: Any) -> dict:
            captured.update(kwargs)
            return {"finished": False}

        monkeypatch.setattr(cli, "run_experiment", fake_run)
        monkeypatch.setattr(sys, "argv", ["experiment"])
        with pytest.raises(SystemExit):
            asyncio.run(cli.main())

        assert captured["spawn_mode"] == "default"
        assert captured["spawn_seed"] is None  # 缺省解析（= seed）在 run_experiment


# ═══════════════════════════════════════════════════════════════════════════
# 2. run_experiment → env_pack / metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestRunExperimentSurface:
    def test_spawn_params_reach_env_pack_and_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        spy = _SpecSpy()
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        asyncio.run(
            run_experiment(
                mode="fake",
                seed=42,
                spawn_mode="random",
                spawn_seed=7,
                log_dir=str(tmp_path),
            )
        )

        assert spy.spec.env_pack.spawn_mode == "random"
        assert spy.spec.env_pack.spawn_seed == 7
        assert spy.spec.metadata["spawn_mode"] == "random"
        assert spy.spec.metadata["spawn_seed"] == 7
        # seed（LLM 采样）与 spawn_seed（布局）分列。
        assert spy.spec.metadata["seed"] == 42

    def test_spawn_seed_defaults_to_run_seed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        spy = _SpecSpy()
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        asyncio.run(run_experiment(mode="fake", seed=9, log_dir=str(tmp_path)))

        assert spy.spec.env_pack.spawn_mode == "default"
        assert spy.spec.env_pack.spawn_seed == 9
        assert spy.spec.metadata["spawn_mode"] == "default"
        assert spy.spec.metadata["spawn_seed"] == 9
        assert spy.spec.metadata["seed"] == 9

    def test_explicit_spawn_seed_wins_over_run_seed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        spy = _SpecSpy()
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        asyncio.run(
            run_experiment(mode="fake", seed=9, spawn_seed=5, log_dir=str(tmp_path))
        )

        assert spy.spec.env_pack.spawn_seed == 5
        assert spy.spec.metadata["spawn_seed"] == 5
        assert spy.spec.metadata["seed"] == 9

    def test_invalid_spawn_mode_rejected_before_assembly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        spy = _SpecSpy(boom="assembly must not be reached")
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        with pytest.raises(ValueError, match="spawn_mode"):
            asyncio.run(
                run_experiment(
                    mode="fake", spawn_mode="bogus", log_dir=str(tmp_path)
                )
            )


# ═══════════════════════════════════════════════════════════════════════════
# 3. 全链：run_experiment → build_barrier → create_controller → controller
# ═══════════════════════════════════════════════════════════════════════════


class TestControllerChain:
    def test_random_spawn_executes_once_on_fake_controller(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        captured = _spy_create_controller(monkeypatch)
        spy = _SpecSpy(build_barrier=True)
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        asyncio.run(
            run_experiment(
                mode="fake",
                num_agents=1,
                seed=42,
                spawn_mode="random",
                spawn_seed=7,
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        # env_pack → create_controller：spawn 参数显式下传。
        assert captured["kwargs"]["spawn_mode"] == "random"
        assert captured["kwargs"]["spawn_seed"] == 7
        # controller：InitialRandomSpawn 恰好执行一次，randomSeed = spawn_seed。
        controller = captured["controller"]
        spawn_calls = [
            a for a in controller.actions_received if a["action"] == SPAWN_ACTION
        ]
        assert len(spawn_calls) == 1
        assert spawn_calls[0]["raw"] == {
            "action": SPAWN_ACTION,
            "randomSeed": 7,
        }

    def test_default_spawn_zero_side_effect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        captured = _spy_create_controller(monkeypatch)
        spy = _SpecSpy(build_barrier=True)
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", spy)

        asyncio.run(
            run_experiment(
                mode="fake",
                num_agents=1,
                seed=42,
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        # default 口径：不带新增形参下传（既有调用形状逐字不变）……
        assert "spawn_mode" not in captured["kwargs"]
        assert "spawn_seed" not in captured["kwargs"]
        # ……且零 InitialRandomSpawn 调用（接受标准）。
        controller = captured["controller"]
        assert all(
            action["action"] != SPAWN_ACTION
            for action in controller.actions_received
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. create_controller 分支面
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateControllerBranches:
    def test_fake_random_records_spawn_call(self) -> None:
        controller = env_pack_mod.create_controller(
            mode="fake",
            scene="FloorPlan1",
            num_agents=1,
            spawn_mode="random",
            spawn_seed=3,
        )
        assert isinstance(controller, FakeController)
        assert controller.actions_received == [
            {"action": SPAWN_ACTION, "raw": {"action": SPAWN_ACTION, "randomSeed": 3}}
        ]

    def test_fake_default_zero_calls(self) -> None:
        controller = env_pack_mod.create_controller(
            mode="fake", scene="FloorPlan1", num_agents=1
        )
        assert isinstance(controller, FakeController)
        assert controller.actions_received == []

    def test_unity_default_call_shape_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai2thor_orch.executor import unity_controller as uc_mod

        seen: dict[str, Any] = {}

        class _Stub:
            def __init__(self, *, scene: str, num_agents: int) -> None:
                seen.update(scene=scene, num_agents=num_agents)

        monkeypatch.setattr(uc_mod, "UnityController", _Stub)
        controller = env_pack_mod.create_controller(
            mode="unity", scene="FloorPlan1", num_agents=2
        )
        assert isinstance(controller, _Stub)
        assert seen == {"scene": "FloorPlan1", "num_agents": 2}

    def test_unity_random_passes_spawn_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai2thor_orch.executor import unity_controller as uc_mod

        seen: dict[str, Any] = {}

        class _Stub:
            def __init__(
                self,
                *,
                scene: str,
                num_agents: int,
                spawn_mode: str,
                spawn_seed: int | None,
            ) -> None:
                seen.update(
                    scene=scene,
                    num_agents=num_agents,
                    spawn_mode=spawn_mode,
                    spawn_seed=spawn_seed,
                )

        monkeypatch.setattr(uc_mod, "UnityController", _Stub)
        env_pack_mod.create_controller(
            mode="unity",
            scene="FloorPlan1",
            num_agents=2,
            spawn_mode="random",
            spawn_seed=7,
        )
        assert seen == {
            "scene": "FloorPlan1",
            "num_agents": 2,
            "spawn_mode": "random",
            "spawn_seed": 7,
        }

    def test_unknown_spawn_mode_rejected_for_fake(self) -> None:
        with pytest.raises(ValueError, match="spawn_mode"):
            env_pack_mod.create_controller(
                mode="fake", scene="FloorPlan1", num_agents=1, spawn_mode="nope"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 5. UnityController 接线与 fail-fast
# ═══════════════════════════════════════════════════════════════════════════


class TestUnityControllerSpawn:
    def test_random_spawn_first_action_after_init(self) -> None:
        mock = MockA2TController(agent_count=2)
        controller = _make_unity_controller(mock, spawn_mode="random", spawn_seed=13)

        assert controller.spawn_mode == "random"
        assert controller.spawn_seed == 13
        spawn_calls = [s for s in mock.steps if s.get("action") == SPAWN_ACTION]
        assert spawn_calls == [{"action": SPAWN_ACTION, "randomSeed": 13}]
        # agentCount 校验（初始化）先于 spawn：首个动作就是 InitialRandomSpawn。
        assert mock.steps[0] == {"action": SPAWN_ACTION, "randomSeed": 13}

    def test_default_spawn_no_call(self) -> None:
        mock = MockA2TController(agent_count=2)
        controller = _make_unity_controller(mock)

        assert controller.spawn_mode == "default"
        assert controller.spawn_seed is None
        assert mock.steps == []

    def test_spawn_refreshes_metadata_cache(self) -> None:
        mock = _SpawnMovingMock(agent_count=1)
        controller = _make_unity_controller(mock, spawn_mode="random", spawn_seed=1)

        # spawn 事件（移动后位姿）必须刷进 _last_metadata（x: 0.0 → 100.0）。
        assert controller.last_metadata[0]["agent"]["position"]["x"] == 100.0

    def test_spawn_soft_failure_fails_fast(self) -> None:
        stub = _RefusingSpawnController()
        with pytest.raises(RuntimeError, match="InitialRandomSpawn"):
            _make_unity_controller(stub, spawn_mode="random", spawn_seed=1)
        assert stub.steps == [{"action": SPAWN_ACTION, "randomSeed": 1}]

    def test_random_requires_spawn_seed(self) -> None:
        mock = MockA2TController(agent_count=1)
        with pytest.raises(ValueError, match="spawn_seed"):
            _make_unity_controller(mock, spawn_mode="random")

    def test_invalid_spawn_mode_rejected_before_launch(self) -> None:
        def _explode(options: dict[str, Any]) -> Any:
            raise AssertionError("controller factory must not be reached")

        with pytest.raises(ValueError, match="spawn_mode"):
            UnityController(
                scene="FloorPlan1",
                num_agents=1,
                controller_factory=_explode,
                spawn_mode="weird",
            )

    def test_non_int_spawn_seed_rejected(self) -> None:
        with pytest.raises(TypeError, match="spawn_seed"):
            UnityController(
                scene="FloorPlan1",
                num_agents=1,
                controller_factory=lambda options: MockA2TController(agent_count=1),
                spawn_mode="random",
                spawn_seed="7",  # type: ignore[arg-type]
            )


# ═══════════════════════════════════════════════════════════════════════════
# 6. helper 面（apply_initial_random_spawn）
# ═══════════════════════════════════════════════════════════════════════════


class TestApplyInitialRandomSpawn:
    def test_fake_controller_accepts(self) -> None:
        controller = FakeController()
        event = apply_initial_random_spawn(controller, spawn_seed=9)
        assert event.metadata["lastActionSuccess"] is True
        assert controller.actions_received == [
            {"action": SPAWN_ACTION, "raw": {"action": SPAWN_ACTION, "randomSeed": 9}}
        ]

    def test_missing_seed_rejected(self) -> None:
        with pytest.raises(ValueError, match="spawn_seed"):
            apply_initial_random_spawn(FakeController(), spawn_seed=None)

    def test_soft_failure_raises_loudly(self) -> None:
        stub = _RefusingSpawnController()
        with pytest.raises(RuntimeError, match="InitialRandomSpawn"):
            apply_initial_random_spawn(stub, spawn_seed=5)
        assert stub.steps == [{"action": SPAWN_ACTION, "randomSeed": 5}]


# ═══════════════════════════════════════════════════════════════════════════
# 7. benchmark 参数面（下行 sweep 卡 G2 依赖）
# ═══════════════════════════════════════════════════════════════════════════


class TestBenchmarkSurface:
    def test_cli_threads_spawn_args_into_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import json

        import ai2thor_orch.benchmark as bench

        captured: list[Any] = []

        async def fake_run_single(run: Any, run_timeout: int = 300, max_steps: int = 50):
            captured.append(run)
            return run

        monkeypatch.setattr(bench, "run_single", fake_run_single)
        monkeypatch.setattr(bench, "_BENCHMARK_RESULTS_ROOT", tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "benchmark",
                "--agents",
                "1",
                "--seed",
                "42",
                "43",
                "--spawn-mode",
                "random",
                "--spawn-seed",
                "7",
                "--max-steps",
                "3",
            ],
        )

        asyncio.run(bench.main())

        assert [run.spawn_mode for run in captured] == ["random", "random"]
        assert [run.spawn_seed for run in captured] == [7, 7]

        index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
        assert [row["spawn_mode"] for row in index] == ["random", "random"]
        assert [row["spawn_seed"] for row in index] == [7, 7]

    def test_run_single_subprocess_cmd_carries_spawn_flags(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import ai2thor_orch.benchmark as bench

        monkeypatch.setattr(bench, "_BENCHMARK_RESULTS_ROOT", tmp_path)
        calls: list[list[str]] = []

        class _FakeProc:
            async def communicate(self):
                return b"", b""

        async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProc:
            calls.append([str(part) for part in cmd])
            return _FakeProc()

        monkeypatch.setattr(bench.asyncio, "create_subprocess_exec", fake_exec)

        # run 1：spawn_seed 缺省（None → 解析为本 run 的 seed）。
        asyncio.run(
            bench.run_single(
                bench.BenchmarkRun(agents=1, seed=42, spawn_mode="random"),
                run_timeout=5,
                max_steps=3,
            )
        )
        # run 2：显式 spawn_seed 优先。
        asyncio.run(
            bench.run_single(
                bench.BenchmarkRun(agents=1, seed=42, spawn_seed=11),
                run_timeout=5,
                max_steps=3,
            )
        )

        first, second = calls
        assert first[first.index("--spawn-mode") + 1] == "random"
        assert first[first.index("--spawn-seed") + 1] == "42"
        assert second[second.index("--spawn-mode") + 1] == "default"
        assert second[second.index("--spawn-seed") + 1] == "11"
