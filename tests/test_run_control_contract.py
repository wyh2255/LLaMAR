"""P2a 契约卡：内核级 RunStatus / EnvironmentRunControl（纯新增，不接线）。

目标契约（对齐 ai2thor 分支 feat/ai2thor-scene-adaptation 的 API 面）：
- ``a2a.coordinator.run_control.RunStatus``：frozen dataclass，字段名/默认值与
  ai2thor 分支 ``ai2thor_orch.contracts.types.RunStatus`` 逐项一致：
  step=0 / max_steps=0 / finished=False / stopped=False / stop_reason="" /
  timeout_agents=[] / domain_metrics={}；
- ``EnvironmentRunControl``：``@runtime_checkable`` Protocol，含
  ``request_stop(reason: str)`` / ``stop()`` / ``get_run_status()`` 三方法，
  实现类（dummy）isinstance 校验为 True，缺任一方法为 False；
- 内核模块零场景包依赖：源码不出现 ``sar_orch`` / ``ai2thor_orch``
  （import 与字面量均不允许，P2a 去耦验收口径）。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

from a2a.coordinator import run_control as run_control_mod
from a2a.coordinator.run_control import EnvironmentRunControl, RunStatus

# ---------------------------------------------------------------------------
# RunStatus：字段名 / 默认值 / frozen 锁定
# ---------------------------------------------------------------------------

EXPECTED_FIELD_NAMES = [
    "step",
    "max_steps",
    "finished",
    "stopped",
    "stop_reason",
    "timeout_agents",
    "domain_metrics",
]


def test_run_status_field_names_and_order_locked():
    """字段名与顺序 = ai2thor 版（实现方案 §3.1）逐项锁定。"""
    assert [f.name for f in dataclasses.fields(RunStatus)] == EXPECTED_FIELD_NAMES


def test_run_status_defaults_locked():
    """默认值逐项锁定：与 ai2thor 版一致（全默认构造）。"""
    rs = RunStatus()
    assert rs.step == 0
    assert rs.max_steps == 0
    assert rs.finished is False
    assert rs.stopped is False
    assert rs.stop_reason == ""
    assert rs.timeout_agents == []
    assert rs.domain_metrics == {}


def test_run_status_all_fields_assignable():
    rs = RunStatus(
        step=5,
        max_steps=50,
        finished=False,
        stopped=True,
        stop_reason="cancel:test",
        timeout_agents=[1, 2],
        domain_metrics={"coverage": 0.75, "transport_rate": 0.3},
    )
    assert rs.step == 5
    assert rs.max_steps == 50
    assert rs.finished is False
    assert rs.stopped is True
    assert rs.stop_reason == "cancel:test"
    assert rs.timeout_agents == [1, 2]
    assert rs.domain_metrics["coverage"] == 0.75


def test_run_status_is_frozen():
    rs = RunStatus()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rs.stopped = True  # type: ignore[misc]


def test_run_status_mutable_defaults_are_per_instance():
    """default_factory 生效：实例间不共享 list/dict 默认值。"""
    a = RunStatus()
    b = RunStatus()
    a.timeout_agents.append(1)
    a.domain_metrics["x"] = 1
    assert b.timeout_agents == []
    assert b.domain_metrics == {}


# ---------------------------------------------------------------------------
# EnvironmentRunControl：协议形态 + isinstance 语义
# ---------------------------------------------------------------------------


class _DummyRunControl:
    """最小实现：只实现协议要求的三个方法。"""

    def __init__(self) -> None:
        self.stop_reason = ""
        self.stopped = False

    def request_stop(self, reason: str) -> None:
        self.stop_reason = reason

    def stop(self) -> None:
        self.stopped = True

    def get_run_status(self) -> RunStatus:
        return RunStatus(stopped=self.stopped, stop_reason=self.stop_reason)


class _IncompleteRunControl:
    """缺 get_run_status 的非实现类。"""

    def request_stop(self, reason: str) -> None:
        pass

    def stop(self) -> None:
        pass


def test_protocol_is_runtime_checkable():
    assert getattr(EnvironmentRunControl, "_is_runtime_protocol", False) is True
    # runtime_checkable 协议可做 isinstance（非 Protocol 类会 TypeError）
    assert isinstance(_DummyRunControl(), EnvironmentRunControl)


def test_protocol_declares_three_methods():
    for name in ("request_stop", "stop", "get_run_status"):
        assert name in EnvironmentRunControl.__dict__


def test_protocol_method_signatures_locked():
    """签名逐项锁定（与 ai2thor 版 Protocol 一致）。"""
    assert list(inspect.signature(EnvironmentRunControl.request_stop).parameters) == [
        "self",
        "reason",
    ]
    assert list(inspect.signature(EnvironmentRunControl.stop).parameters) == ["self"]
    assert list(inspect.signature(EnvironmentRunControl.get_run_status).parameters) == [
        "self"
    ]


def test_dummy_satisfies_protocol_and_methods_work():
    ctrl = _DummyRunControl()
    assert isinstance(ctrl, EnvironmentRunControl)

    ctrl.request_stop("cancel:ctx_001")
    assert ctrl.stop_reason == "cancel:ctx_001"

    status = ctrl.get_run_status()
    assert isinstance(status, RunStatus)
    assert status.stopped is False

    ctrl.stop()
    assert ctrl.stopped is True
    assert ctrl.get_run_status().stopped is True


def test_incomplete_implementation_fails_isinstance():
    assert not isinstance(_IncompleteRunControl(), EnvironmentRunControl)


# ---------------------------------------------------------------------------
# 去耦边界：内核模块源码零场景包依赖
# ---------------------------------------------------------------------------


def _module_imports(src: str) -> list[str]:
    tree = ast.parse(src)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_kernel_module_source_has_no_scenario_pack_reference():
    """与验收 grep 等价：源码（含 docstring/注释）不出现场景包模块名。"""
    src = inspect.getsource(run_control_mod)
    assert "sar_orch" not in src
    assert "ai2thor_orch" not in src


def test_kernel_module_imports_only_core_libs():
    """AST 层确认：所有 import 均非场景包（*_orch）。"""
    src = inspect.getsource(run_control_mod)
    imported = _module_imports(src)
    assert imported, "内核契约模块应至少有 dataclasses/typing import"
    assert all("_orch" not in name for name in imported), imported
