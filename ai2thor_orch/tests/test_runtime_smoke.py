"""ai2thor_runtime_smoke.py 探针单测（env-contract P5-4）。

本机无 GPU —— unity 分支用注入式 ``controller_factory``（ai2thor 5.0 形状的
``MockA2TController``）跑完整探针逻辑，验证：

1. fake 模式报告 schema / checks / info；
2. unity 分支的 gating 断言全过（含 executor 轮 + 空动作映射 + 归一化消费面）；
3. 断言失败 / 启动失败 → status=error 且错误类型正确；
4. 退出码映射（0 / 1 / 2 / 3）与 ``--report`` 落盘。

真实 Unity 首跑由 A100 上的
``LLAMAR_AI2THOR_MODE=unity python scripts/ai2thor_runtime_smoke.py --report ...``
执行（见 docs/system_docs/ai2thor_a100_runbook.md）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai2thor_orch.tests.fakes import MockA2TController
from scripts import ai2thor_runtime_smoke as smoke

GATING_CHECKS = [
    "import_ok",
    "controller_started",
    "multi_agent_events",
    "per_agent_metadata",
    "executor_round_ok",
    "adapter_surface_ok",
    "stop_clean",
]


def _mock_factory(controller: MockA2TController | None = None):
    """注入式 controller 工厂（对齐 UnityController 的工厂契约）。"""
    captured: dict[str, Any] = {}

    def _factory(options: dict[str, Any]) -> MockA2TController:
        captured.update(options)
        if controller is not None:
            return controller
        return MockA2TController(agent_count=int(options["agentCount"]))

    _factory.captured = captured  # type: ignore[attr-defined]
    return _factory


class TestFakeMode:
    def test_report_shape_and_checks(self):
        report = smoke.run_fake(scene="FloorPlan1", agents=2)
        assert report["status"] == "ok"
        assert report["mode"] == "fake"
        assert report["agents"] == 2
        assert all(report["checks"][name] for name in GATING_CHECKS)
        assert report["metadata_schema"]["has_objects"] is True
        assert report["metadata_schema"]["reachable_count"] == 5
        assert report["info"]["fake"] is True
        # 位置面与旧版报告字段兼容
        assert report["agent_start"]["x"] == -1.5
        assert report["agent_after_move"]["z"] == 0.25


class TestUnityModeWithMock:
    def test_probe_passes_on_healthy_controller(self):
        factory = _mock_factory()
        report = smoke.run_unity(
            scene="FloorPlan1", timeout=30, agents=2, controller_factory=factory
        )
        assert report["status"] == "ok", report["error"]
        assert all(report["checks"][name] for name in GATING_CHECKS)
        # 工厂收到 agentCount=2（多 agent 初始化生效）
        assert factory.captured["agentCount"] == 2
        assert report["ai2thor_version"] is None or isinstance(
            report["ai2thor_version"], str
        )
        # MoveAhead 位移 + NoOp→Pass 空动作映射 + reachable 记录
        assert report["info"]["move_caused_displacement"] is True
        assert report["info"]["done_action_supported"] is True
        assert report["info"]["reachable_positions_count"] == 3
        assert report["agent_start"] != report["agent_after_move"]
        assert report["metadata_schema"]["has_objects"] is True

    def test_probe_fails_when_executor_round_fails(self):
        """NoOp → Pass 被 build 拒绝（ValueError → 软失败）→ executor 轮断言失败。"""
        controller = MockA2TController(agent_count=2, fail_on={"Pass"})
        report = smoke.run_unity(
            scene="FloorPlan1",
            timeout=30,
            agents=2,
            controller_factory=_mock_factory(controller),
        )
        assert report["status"] == "error"
        assert report["error"]["type"] == "AssertionError"
        assert "executor_round_ok" in report["error"]["message"]
        assert report["checks"]["executor_round_ok"] is False
        # 结构断言仍然成立（说明失败定位精确，不是环境整体挂掉）
        assert report["checks"]["multi_agent_events"] is True
        assert report["checks"]["adapter_surface_ok"] is True

    def test_probe_fails_when_agent_count_mismatch(self):
        """agentCount 未生效（返回 1 agent 事件）→ 启动即失败。"""
        controller = MockA2TController(agent_count=1)
        report = smoke.run_unity(
            scene="FloorPlan1",
            timeout=30,
            agents=2,
            controller_factory=_mock_factory(controller),
        )
        assert report["status"] == "error"
        assert report["error"]["type"] == "RuntimeError"
        assert "agentCount" in report["error"]["message"]


class TestExitCodes:
    def test_ok(self):
        assert smoke.exit_code_for({"status": "ok"}) == 0

    def test_assertion_failure(self):
        report = {"status": "error", "error": {"type": "AssertionError"}}
        assert smoke.exit_code_for(report) == 1

    def test_import_error(self):
        report = {"status": "error", "error": {"type": "ImportError"}}
        assert smoke.exit_code_for(report) == 2

    def test_timeout(self):
        report = {"status": "error", "error": {"type": "TimeoutError"}}
        assert smoke.exit_code_for(report) == 3

    def test_unknown_mode_exit_code(self):
        assert (
            smoke.exit_code_for({"status": "error", "error": {"type": "ValueError"}})
            == 1
        )


class TestCli:
    def test_fake_cli_writes_report(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        report_path = tmp_path / "reports" / "fake_smoke.json"
        monkeypatch.setattr(
            "sys.argv",
            [
                "ai2thor_runtime_smoke.py",
                "--mode",
                "fake",
                "--report",
                str(report_path),
            ],
        )
        assert smoke.main() == 0
        payload = json.loads(report_path.read_text())
        assert payload["status"] == "ok"
        assert payload["mode"] == "fake"

    def test_unity_cli_uses_injected_probe(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        """--mode unity 走 run_unity（此处替换为桩，仅验证 CLI 接线与退出码）。"""
        monkeypatch.setattr(
            smoke,
            "run_unity",
            lambda **kwargs: smoke.make_report(
                status="error",
                mode="unity",
                scene=kwargs["scene"],
                ai2thor_version="stub",
                duration_seconds=0.0,
                metadata_schema=smoke._empty_schema(),
                agent_start=None,
                agent_after_move=None,
                error=smoke._error_dict("ImportError", "stub"),
                agents=kwargs.get("agents", 2),
            ),
        )
        monkeypatch.setattr("sys.argv", ["ai2thor_runtime_smoke.py", "--mode", "unity"])
        assert smoke.main() == 2
