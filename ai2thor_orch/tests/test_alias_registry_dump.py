"""F-seed ``alias_registry.json`` 落盘 + round-trip 测试。

契约（卡 t_00358887 / 设计 2026-09-17 §1.4-R3）：

- run 终结（``run_experiment`` 返回或抛异常）后 ``<run_dir>/alias_registry.json``
  存在，且可无损 round-trip（双向映射 + 计数器）；
- F-frame replay 消费面：``AliasRegistry.load(path).raw(alias)`` 解析回
  rawObjectId（replay 重放 ``Teleport(Fridge_1)`` 这类动作的前提）。

``run_assembly`` 被 ``_BarrierStub`` 替换（真建 barrier 后立即返回/抛错），
以离线方式精确落在「run 终结」时刻。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ai2thor_orch.experiment import ai2thor_experiment
from ai2thor_orch.experiment.ai2thor_experiment import run_experiment
from ai2thor_orch.visibility import AliasRegistry

pytestmark = pytest.mark.unit

MUG_RAW = "Mug|-01.5|+00.9|+02.3"
FRIDGE_RAW = "Fridge|-02.10|+00.00|+01.07"
APPLE_RAW = "Apple|+01.2|+00.5|+00.8"


def _filled_registry() -> AliasRegistry:
    registry = AliasRegistry()
    registry.register(MUG_RAW)
    registry.register(FRIDGE_RAW)
    registry.register(APPLE_RAW)
    registry.register("Mug|+00.0|+00.0|+00.0")
    return registry


class _BarrierStub:
    """Async ``run_assembly`` 替身：真建 barrier（可选登记 alias）后返回/抛错。"""

    def __init__(
        self,
        *,
        register: dict[str, str] | None = None,
        boom: str | None = None,
        build: bool = True,
    ) -> None:
        self.spec: Any = None
        self.barrier: Any = None
        self._register = register or {}
        self._boom = boom
        self._build = build

    async def __call__(self, spec):
        self.spec = spec
        if self._build:
            self.barrier = spec.env_pack.build_barrier(
                num_agents=spec.num_agents, seed=spec.seed, **spec.env_params
            )
            for raw_id, alias in self._register.items():
                assert self.barrier.alias_registry.register(raw_id) == alias
        if self._boom is not None:
            raise RuntimeError(self._boom)
        return {
            "run_id": spec.run_id,
            "steps": 3,
            "finished": False,
            "end_reason": "max_steps_reached",
            "elapsed_seconds": 1.0,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 1. dump / load round-trip（纯注册表层面）
# ═══════════════════════════════════════════════════════════════════════════


class TestDumpRoundTrip:
    def test_dump_writes_self_describing_payload(self, tmp_path) -> None:
        registry = _filled_registry()
        path = registry.dump(tmp_path / "alias_registry.json")

        assert path == tmp_path / "alias_registry.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == AliasRegistry.DUMP_SCHEMA_VERSION
        assert payload["raw_to_alias"][MUG_RAW] == "Mug_1"
        assert payload["alias_to_raw"]["Mug_1"] == MUG_RAW
        assert payload["counters"]["Mug"] == 2
        assert payload["counters"]["Fridge"] == 1

    def test_load_restores_both_directions(self, tmp_path) -> None:
        registry = _filled_registry()
        registry.dump(tmp_path / "alias_registry.json")

        loaded = AliasRegistry.load(tmp_path / "alias_registry.json")

        assert loaded.size == registry.size
        for raw_id, alias in registry.to_dict()["raw_to_alias"].items():
            assert loaded.alias(raw_id) == alias
            assert loaded.raw(alias) == raw_id
        # F-frame replay 消费面：alias 动作解析回 raw id。
        assert loaded.raw("Fridge_1") == FRIDGE_RAW

    def test_redump_is_byte_identical_and_counters_continue(self, tmp_path) -> None:
        registry = _filled_registry()
        first = registry.dump(tmp_path / "a.json")
        loaded = AliasRegistry.load(first)

        # 计数器恢复：新注册不撞已有 alias（Mug 已有 2 → 下一个是 Mug_3）。
        assert loaded.register("Mug|+09.9|+00.0|+00.0") == "Mug_3"
        assert loaded.alias("Mug|+09.9|+00.0|+00.0") == "Mug_3"

        second = loaded.dump(tmp_path / "b.json")
        # 新增一条注册后仍可再 round-trip（dump → load → dump 幂等）。
        reloaded = AliasRegistry.load(second)
        assert reloaded.raw("Mug_3") == "Mug|+09.9|+00.0|+00.0"
        assert reloaded.dump(tmp_path / "c.json").read_text(
            encoding="utf-8"
        ) == second.read_text(encoding="utf-8")

    def test_empty_registry_dumps_valid_payload(self, tmp_path) -> None:
        path = AliasRegistry().dump(tmp_path / "alias_registry.json")
        loaded = AliasRegistry.load(path)
        assert loaded.size == 0

    def test_load_rejects_unknown_schema_version(self, tmp_path) -> None:
        path = tmp_path / "alias_registry.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 99,
                    "raw_to_alias": {},
                    "alias_to_raw": {},
                    "counters": {},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schema_version"):
            AliasRegistry.load(path)

    def test_load_rejects_missing_fields(self, tmp_path) -> None:
        path = tmp_path / "alias_registry.json"
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="缺字段"):
            AliasRegistry.load(path)

    def test_load_rejects_inconsistent_bidirectional_mapping(self, tmp_path) -> None:
        path = tmp_path / "alias_registry.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "raw_to_alias": {MUG_RAW: "Mug_1"},
                    "alias_to_raw": {"Mug_1": "Mug|+09.9|+09.9|+09.9"},
                    "counters": {"Mug": 1},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="双向映射不一致"):
            AliasRegistry.load(path)

    def test_load_rejects_non_object_payload(self, tmp_path) -> None:
        path = tmp_path / "alias_registry.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(TypeError, match="顶层"):
            AliasRegistry.load(path)


# ═══════════════════════════════════════════════════════════════════════════
# 2. run 终结落盘（run_experiment 收尾段）
# ═══════════════════════════════════════════════════════════════════════════


class TestRunEndDump:
    def test_run_end_writes_alias_registry(self, monkeypatch, tmp_path) -> None:
        stub = _BarrierStub(register={MUG_RAW: "Mug_1", FRIDGE_RAW: "Fridge_1"})
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", stub)

        asyncio.run(
            run_experiment(
                mode="fake",
                num_agents=1,
                seed=42,
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        path = tmp_path / "alias_registry.json"
        assert path.exists()
        loaded = AliasRegistry.load(path)
        # 落盘的是 barrier 的共享实例（worker 工具 / 观测脱敏同一注册表）。
        assert loaded.to_dict() == stub.barrier.alias_registry.to_dict()
        assert loaded.raw("Mug_1") == MUG_RAW
        assert loaded.raw("Fridge_1") == FRIDGE_RAW

    def test_run_end_dump_survives_assembly_error(self, monkeypatch, tmp_path) -> None:
        stub = _BarrierStub(register={APPLE_RAW: "Apple_1"}, boom="assembly boom")
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", stub)

        with pytest.raises(RuntimeError, match="assembly boom"):
            asyncio.run(
                run_experiment(
                    mode="fake",
                    num_agents=1,
                    seed=42,
                    max_steps=4,
                    log_dir=str(tmp_path),
                )
            )

        path = tmp_path / "alias_registry.json"
        assert path.exists()
        assert AliasRegistry.load(path).raw("Apple_1") == APPLE_RAW

    def test_no_barrier_no_file_no_crash(self, monkeypatch, tmp_path) -> None:
        stub = _BarrierStub(build=False)
        monkeypatch.setattr(ai2thor_experiment, "run_assembly", stub)

        result = asyncio.run(
            run_experiment(
                mode="fake",
                num_agents=1,
                seed=42,
                max_steps=4,
                log_dir=str(tmp_path),
            )
        )

        assert result["steps"] == 3  # 替身 metrics 原样返回
        assert not (tmp_path / "alias_registry.json").exists()
