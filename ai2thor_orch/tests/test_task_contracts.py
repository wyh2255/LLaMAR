"""Task-contract loading (PA-W1): whitelist removal + D5 layout wiring.

覆盖：

1. **白名单拆除**：任何 ``AI2Thor/Tasks/<task_id>/checker.py`` 存在的任务都可
   加载（不再限于 ``3_transport_groceries``）；目录/checker 缺失 → fail-fast；
2. **checker 异常面**：无 ``Checker`` 类、scene 文件导入失败 / 无
   ``SceneInitializer`` 类的行为；
3. **D5 布局接线**：``SceneInitializer`` 装入契约 + ``invoke_scene_preinit``
   双签名（带 ``self`` 绑定方法 / 缺 ``self`` 裸函数）+ fail-fast 包装；
4. **真实任务全量可加载性**：``AI2Thor/Tasks/`` 下全部任务类型扫一遍。

真实 Unity 行为由 W3 smoke 在 A100 验证，本文件不依赖 ai2thor 运行。
"""

from __future__ import annotations

import os

import pytest

from ai2thor_orch.contracts import task as task_mod
from ai2thor_orch.contracts.task import (
    TaskContract,
    invoke_scene_preinit,
    load_task,
)
from ai2thor_orch.tests.fakes import FakeController

# ── 任务集快照路径（与 load_task 同一根：<repo>/AI2Thor/Tasks）───────────


def _tasks_root() -> str:
    return task_mod._TASKS_ROOT


def _all_task_ids() -> list[str]:
    root = _tasks_root()
    return sorted(
        name
        for name in os.listdir(root)
        if os.path.isfile(os.path.join(root, name, "checker.py"))
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. 白名单拆除
# ═══════════════════════════════════════════════════════════════════════════


def test_non_whitelist_task_loads() -> None:
    """非 grocery 任务（2_open_all_cabinets）如今可加载（白名单已拆）。"""
    contract = load_task("2_open_all_cabinets", "FloorPlan1")

    assert contract.task_id == "2_open_all_cabinets"
    assert contract.scene == "FloorPlan1"
    assert contract.subtasks, "checker 应提供 subtasks"
    assert contract.coverage_objects, "checker 应提供 coverage"
    assert contract.scene_initializer is not None


def test_all_task_types_load() -> None:
    """``AI2Thor/Tasks/`` 下全部任务类型均可加载（论文任务集解锁）。"""
    task_ids = _all_task_ids()
    # 论文任务集为 24 个任务类型；保守下界防快照损坏（只增不减地断言）。
    assert len(task_ids) >= 20

    loaded: list[TaskContract] = []
    for task_id in task_ids:
        contract = load_task(task_id, "FloorPlan1")
        assert contract.task_id == task_id
        assert contract.subtasks, f"{task_id} 的 checker 应提供 subtasks"
        assert contract.coverage_objects, f"{task_id} 的 checker 应提供 coverage"
        loaded.append(contract)

    # 非 grocery 任务确实在集合内（白名单拆除的直接证据）。
    assert any("grocer" not in c.task_id for c in loaded)


def test_missing_task_dir_fails_fast() -> None:
    with pytest.raises(NotImplementedError, match="not found"):
        load_task("999_no_such_task", "FloorPlan1")


def test_missing_checker_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """任务目录存在但无 checker.py → fail-fast。"""
    (tmp_path / "42_empty_task").mkdir()
    monkeypatch.setattr(task_mod, "_TASKS_ROOT", str(tmp_path))

    with pytest.raises(NotImplementedError, match="no checker.py"):
        load_task("42_empty_task", "FloorPlan1")


def test_checker_without_class_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """checker.py 未定义 Checker 类 → fail-fast。"""
    task_dir = tmp_path / "42_broken_task"
    task_dir.mkdir()
    (task_dir / "checker.py").write_text("SUBTASKS = []\n", encoding="utf-8")
    monkeypatch.setattr(task_mod, "_TASKS_ROOT", str(tmp_path))

    with pytest.raises(NotImplementedError, match="does not define a Checker"):
        load_task("42_broken_task", "FloorPlan1")


# ═══════════════════════════════════════════════════════════════════════════
# 2. scene 文件（FloorPlan*.py）加载面
# ═══════════════════════════════════════════════════════════════════════════


def test_scene_file_missing_is_none() -> None:
    """该任务没有对应场景布局文件 → ``scene_initializer=None``（不适用分支）。"""
    contract = load_task("3_transport_groceries", "FloorPlan999")
    assert contract.scene_initializer is None


def test_scene_file_import_error_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """场景文件存在但导入失败 → 响亮抛（布局错 = 结果无对齐意义）。"""
    task_dir = tmp_path / "42_exploding_task"
    task_dir.mkdir()
    (task_dir / "checker.py").write_text(
        "class Checker:\n    subtasks = ['NavigateTo(X)']\n    coverage = ['X']\n",
        encoding="utf-8",
    )
    (task_dir / "FloorPlan1.py").write_text(
        "raise RuntimeError('boom at import')\n", encoding="utf-8"
    )
    monkeypatch.setattr(task_mod, "_TASKS_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="boom at import"):
        load_task("42_exploding_task", "FloorPlan1")


def test_scene_file_without_scene_initializer_class_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """场景文件存在但无 ``SceneInitializer`` 类 → 警告 + ``None``。"""
    task_dir = tmp_path / "42_classless_task"
    task_dir.mkdir()
    (task_dir / "checker.py").write_text(
        "class Checker:\n    subtasks = ['NavigateTo(X)']\n    coverage = ['X']\n",
        encoding="utf-8",
    )
    (task_dir / "FloorPlan1.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(task_mod, "_TASKS_ROOT", str(tmp_path))

    with caplog.at_level("WARNING", logger=task_mod.__name__):
        contract = load_task("42_classless_task", "FloorPlan1")

    assert contract.scene_initializer is None
    assert any("no SceneInitializer" in record.message for record in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# 3. invoke_scene_preinit：双签名 + fail-fast
# ═══════════════════════════════════════════════════════════════════════════


def test_invoke_preinit_none_returns_event() -> None:
    assert invoke_scene_preinit(None, "EVENT", None) == "EVENT"


def test_invoke_preinit_bound_form_steps_kwargs() -> None:
    """带 ``self`` 的绑定方法（1_put_bread...）：preinit 走 ``controller.step``
    关键字形态放置物体——fake controller 的 kwargs 合并路径由此得到真实验证。"""
    contract = load_task("1_put_bread_lettuce_tomato_fridge", "FloorPlan1")
    controller = FakeController()

    result = invoke_scene_preinit(contract.scene_initializer, None, controller)

    assert result is controller.last_event
    assert controller.step_call_count == 3
    for action in controller.actions_received:
        assert action["action"] == "PlaceObjectAtPoint"
        raw = action["raw"]
        assert raw["objectId"].startswith(("Bread|", "Tomato|", "Lettuce|"))
        assert set(raw["position"]) == {"x", "y", "z"}


def test_invoke_preinit_missing_self_form_returns_event() -> None:
    """缺 ``self`` 的裸函数形态（3_transport_groceries）：按 (event, controller)
    直调，不用实例调用（后者会把实例当第一个位置参数多传）。"""
    contract = load_task("3_transport_groceries", "FloorPlan1")

    result = invoke_scene_preinit(
        contract.scene_initializer, "EVENT", FakeController()
    )

    assert result == "EVENT"


def test_invoke_preinit_staticmethod_form() -> None:
    class _StaticInit:
        @staticmethod
        def preinit(event, controller):  # type: ignore[no-untyped-def]
            return (event, controller)

    controller = FakeController()
    result = invoke_scene_preinit(_StaticInit(), "EVENT", controller)

    assert result == ("EVENT", controller)


def test_invoke_preinit_missing_method_raises() -> None:
    class _NoPreinit:
        pass

    with pytest.raises(RuntimeError, match="defines no preinit"):
        invoke_scene_preinit(_NoPreinit(), None, FakeController())


def test_invoke_preinit_failure_wrapped_fail_fast() -> None:
    class _ExplodingInit:
        def preinit(self, event, controller):  # type: ignore[no-untyped-def]
            raise ValueError("bad layout")

    with pytest.raises(RuntimeError, match="preinit failed"):
        invoke_scene_preinit(_ExplodingInit(), None, FakeController())
