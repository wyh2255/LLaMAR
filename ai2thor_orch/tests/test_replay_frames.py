"""``scripts/ai2thor_replay_frames.py`` 单测 —— fake/mock controller，不碰 Unity。

覆盖（对应 F-replay 卡验收）：
- 动作解析 / CLI 面；
- ``build_action`` 透传调用（动作 dict 形状逐字断言，含 ``agentId`` /
  ``Done→Pass`` / Teleport 的 horizon 注入）；
- alias 映射命中（registry → raw id）与降级路径（无 registry → alias 跳过、
  raw id 直接放）；
- 覆盖率统计数学（各桶互斥完备：``replayed + skipped == task_rows``）；
- spawn 重建接线（random → ``InitialRandomSpawn(randomSeed=...)``）；
- 帧落盘产物（``frames/<Agent>/frame_<step>.png``）+ 俯视帧 + 真 PNG 编码。

本文件不标记 ``unity``（fake 口径，随 ``-m "not unity"`` 全量跑）。
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ai2thor_orch.executor.unity_controller import UnityController
from ai2thor_orch.tests.fakes import MockA2TController
from ai2thor_orch.visibility import AliasRegistry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ai2thor_replay_frames.py"


def _load_replay_module() -> Any:
    """以文件路径加载 scripts 脚本（scripts/ 非包，走 importlib 显式加载）。"""
    existing = sys.modules.get("ai2thor_replay_frames")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "ai2thor_replay_frames", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai2thor_replay_frames"] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay_module()

APPLE_RAW = "Apple|+01.2|+00.5|+00.8"
COUNTER_RAW = "CounterTop|+00.0|+00.0|+00.0"
CSV_FIELDS = ["Step", "Agent", "ToolName", "Action", "Success"]

#: replay 用的场景物体（覆盖 alias 解析的主角：Apple / CounterTop）。
_DEFAULT_OBJECTS: list[dict[str, Any]] = [
    {
        "objectId": APPLE_RAW,
        "objectType": "Apple",
        "position": {"x": 1.2, "y": 0.5, "z": 0.8},
        "visible": True,
        "parentReceptacles": [],
    },
    {
        "objectId": COUNTER_RAW,
        "objectType": "CounterTop",
        "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        "visible": True,
        "parentReceptacles": [],
    },
]


class _FrameMockController(MockA2TController):
    """``MockA2TController`` + 每事件帧（replay 帧捕获面的最小形状）。

    ``frame`` 为 ``4x4x3`` 的确定性小数组（值 = agent 序号 + 1），避免单测
    依赖真实渲染；``ToggleMapView`` 等未登记动作由基类记软失败——不影响帧。
    """

    FRAME_SHAPE = (4, 4, 3)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._attach_frames(self.last_event)

    def step(self, action: dict[str, Any]) -> Any:
        event = super().step(action)
        self._attach_frames(event)
        return event

    def reset(self, scene: str) -> Any:
        event = super().reset(scene)
        self._attach_frames(event)
        return event

    def _attach_frames(self, event: Any) -> None:
        events = getattr(event, "events", None) or [event]
        for idx, agent_event in enumerate(events):
            agent_event.frame = np.full(self.FRAME_SHAPE, idx + 1, dtype=np.uint8)


# ── 测试工具 ───────────────────────────────────────────────────────────────


def _row(
    step: int, agent: str, tool: str, action: str, success: bool = True
) -> dict[str, str]:
    return {
        "Step": str(step),
        "Agent": agent,
        "ToolName": tool,
        "Action": action,
        "Success": str(success),
    }


def _write_run_dir(
    tmp_path: Path,
    *,
    rows: list[dict[str, str]],
    with_registry: bool = True,
    spawn_mode: str = "default",
    spawn_seed: int | None = None,
    include_spawn: bool = True,
) -> Path:
    """合成一个最小 run 目录（metadata + CSV + 可选 alias_registry）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metadata: dict[str, Any] = {
        "scene": "FloorPlan1",
        "num_agents": 2,
        "seed": 42,
        "agent_names": ["Alice", "Bob"],
    }
    if include_spawn:
        metadata["spawn_mode"] = spawn_mode
        if spawn_seed is not None:
            metadata["spawn_seed"] = spawn_seed
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    if with_registry:
        registry = AliasRegistry()
        registry.register(APPLE_RAW)
        registry.register(COUNTER_RAW)
        registry.dump(run_dir / "alias_registry.json")
    with open(
        run_dir / "agent_interactions.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return run_dir


def _make_frame_mock_unity(
    *,
    spawn_mode: str = "default",
    spawn_seed: int | None = None,
    objects: list[dict[str, Any]] | None = None,
) -> tuple[UnityController, _FrameMockController]:
    """真实 ``UnityController`` + 帧 mock（走与在线执行同一条构造/映射路径）。"""
    created: list[_FrameMockController] = []

    def _factory(options: dict[str, Any]) -> _FrameMockController:
        mock = _FrameMockController(
            agent_count=int(options["agentCount"]),
            scene=str(options["scene"]),
            objects=objects if objects is not None else list(_DEFAULT_OBJECTS),
        )
        created.append(mock)
        return mock

    unity = UnityController(
        scene="FloorPlan1",
        num_agents=2,
        width=600,
        height=600,
        headless=False,
        platform="CloudRendering",
        spawn_mode=spawn_mode,
        spawn_seed=spawn_seed,
        controller_factory=_factory,
    )
    return unity, created[0]


def _make_runner(
    run_dir: Path,
    unity: UnityController,
    out_dir: Path,
    *,
    registry: AliasRegistry | None = None,
    **kwargs: Any,
) -> Any:
    return replay.ReplayRunner(
        spec=replay.read_run_spec(run_dir),
        rows=replay.read_action_rows(run_dir),
        unity=unity,
        out_dir=out_dir,
        registry=registry,
        encode_frame=lambda frame: b"FAKEPNG",
        **kwargs,
    )


# ── 1. 解析 / 统计 / CLI 面 ────────────────────────────────────────────────


def test_parse_action_forms() -> None:
    assert replay.parse_action("RotateLeft") == ("RotateLeft", None)
    assert replay.parse_action("LookDown(30)") == ("LookDown", "30")
    assert replay.parse_action(f"PickupObject({APPLE_RAW})") == (
        "PickupObject",
        APPLE_RAW,
    )
    assert replay.parse_action("Done") == ("Done", None)
    assert replay.parse_action("NoOp()") == ("NoOp", None)
    assert replay.parse_action("") == (None, None)
    assert replay.parse_action("!!bad!!") == (None, None)


def test_resolve_object_ref_paths() -> None:
    registry = AliasRegistry()
    registry.register(APPLE_RAW)
    # 1) 精确 alias 命中。
    assert replay.resolve_object_ref("Apple_1", registry) == (APPLE_RAW, "")
    # 2) raw id 直通。
    assert replay.resolve_object_ref(APPLE_RAW, registry) == (APPLE_RAW, "")
    # 3) 裸类型名唯一命中（NavigateTool 同口径）。
    assert replay.resolve_object_ref("Apple", registry) == (APPLE_RAW, "")
    # 4) 裸类型名多命中 → fail-closed。
    registry.register("Apple|+09.9|+00.5|+00.8")
    raw, reason = replay.resolve_object_ref("Apple", registry)
    assert raw is None and "多命中" in reason
    # 5) 空引用。
    raw, reason = replay.resolve_object_ref(None, registry)
    assert raw is None and "缺少对象参数" in reason
    # 6) 无 registry（历史 run 降级）：raw id 直放、alias 拒绝。
    assert replay.resolve_object_ref(APPLE_RAW, None) == (APPLE_RAW, "")
    raw, reason = replay.resolve_object_ref("Apple_1", None)
    assert raw is None and "alias_registry" in reason


def test_stats_coverage_math() -> None:
    stats = replay.ReplayStats(
        task_rows=4, replayed=3, skipped_alias_unresolved=1
    )
    assert stats.coverage_rate == 0.75
    assert stats.skipped_total == 1
    payload = stats.to_dict()
    assert payload["coverage_rate"] == 0.75
    assert payload["skipped"]["alias_unresolved"] == 1
    assert replay.ReplayStats().coverage_rate == 0.0


def test_cli_surface() -> None:
    parser = replay.build_parser()
    args = parser.parse_args(["--run-dir", "R", "--out", "O"])
    assert isinstance(args, argparse.Namespace)
    assert args.overhead_every == 0
    assert args.gif is False
    assert args.width is None and args.height is None
    assert args.fps == 4
    full = parser.parse_args(
        [
            "--run-dir",
            "R",
            "--out",
            "O",
            "--overhead-every",
            "5",
            "--gif",
            "--width",
            "640",
            "--height",
            "480",
            "--fps",
            "6",
        ]
    )
    assert full.overhead_every == 5
    assert full.gif is True
    assert (full.width, full.height, full.fps) == (640, 480, 6)
    with pytest.raises(SystemExit):
        parser.parse_args(["--out", "O"])  # 缺 --run-dir


def test_encode_png_real_roundtrip() -> None:
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[0, 0] = [255, 0, 0]
    payload = replay.encode_png(frame)
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"


# ── 2. 全覆盖路径（registry 命中 + build_action 透传） ────────────────────


def test_replay_full_coverage_with_registry(tmp_path: Path) -> None:
    rows = [
        _row(1, "Alice", "rotate", "RotateLeft"),
        _row(1, "Bob", "navigate", "Teleport(CounterTop_1)"),
        _row(2, "Alice", "pickup", "PickupObject(Apple_1)"),
        _row(3, "Bob", "done", "Done"),
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    registry = AliasRegistry.load(run_dir / "alias_registry.json")
    out = tmp_path / "out"
    runner = _make_runner(run_dir, unity, out, registry=registry)

    stats = runner.run()
    unity.stop()

    # build_action 透传面：动作 dict 形状逐字断言（agentId / 映射 / horizon）。
    assert mock.steps == [
        {"action": "Pass", "agentId": 0},  # 初始渲染帧（设计 §1.3）
        {"action": "RotateLeft", "agentId": 0},
        {"action": "GetReachablePositions", "agentId": 0},  # Teleport 宏首查
        {
            "position": {"x": -1.0, "y": 0.9, "z": 0.5},
            "rotation": {"x": 0.0, "y": 90.0, "z": 0.0},
            "action": "Teleport",
            "horizon": 0.0,  # build_action 注入的夹取 horizon
            "agentId": 1,
        },
        # alias 命中 → raw id（Apple_1 → Apple|+01.2|+00.5|+00.8）。
        {"action": "PickupObject", "objectId": APPLE_RAW, "agentId": 0},
        {"action": "Pass", "agentId": 1},  # Done → 空动作
    ]

    assert stats.task_rows == 4
    assert stats.replayed == 4
    assert stats.skipped_total == 0
    assert stats.coverage_rate == 1.0
    assert stats.replayed + stats.skipped_total == stats.task_rows

    # 帧产物：初始 000 + 每步末帧。
    for agent in ("Alice", "Bob"):
        for step in (0, 1, 2, 3):
            assert (out / "frames" / agent / f"frame_{step:03d}.png").is_file()
    assert stats.frames_distinct == 8
    assert stats.steps_total == 3
    assert stats.steps_with_frames == 3

    report = runner.report()
    assert report["alias_registry"] == {"present": True, "entries": 2}
    assert report["coverage"]["replayed"] == 4


# ── 3. 降级路径（无 registry：raw 直放 / alias 跳过） ─────────────────────


def test_replay_degraded_without_registry(tmp_path: Path) -> None:
    rows = [
        _row(1, "Alice", "rotate", "RotateLeft"),
        _row(1, "Bob", "navigate", "Teleport(CounterTop_1)"),  # alias → 跳过
        _row(2, "Alice", "pickup", f"PickupObject({APPLE_RAW})"),  # raw → 直放
        _row(2, "Bob", "pickup", "PickupObject(Apple_1)"),  # alias → 跳过
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows, with_registry=False)
    unity, mock = _make_frame_mock_unity()
    out = tmp_path / "out"
    runner = _make_runner(run_dir, unity, out, registry=None)

    stats = runner.run()
    unity.stop()

    assert [step["action"] for step in mock.steps] == [
        "Pass",
        "RotateLeft",
        "PickupObject",  # raw id 动作照常重放
    ]
    assert mock.steps[2] == {
        "action": "PickupObject",
        "objectId": APPLE_RAW,
        "agentId": 0,
    }
    assert stats.task_rows == 4
    assert stats.replayed == 2
    assert stats.skipped_alias_unresolved == 2
    assert stats.coverage_rate == 0.5
    assert stats.replayed + stats.skipped_total == stats.task_rows
    assert runner.report()["alias_registry"] == {"present": False, "entries": 0}

    # 覆盖率报告样例：可读的跳过原因。
    assert any("Teleport(CounterTop_1)" in item for item in stats.skip_examples)
    assert any("alias_registry" in item for item in stats.skip_examples)


def test_failed_and_unreplayable_rows_skipped(tmp_path: Path) -> None:
    rows = [
        # 失败行：不重放（可能未提交仿真动作）。
        _row(1, "Bob", "navigate", "Teleport(CounterTop_1)", success=False),
        _row(1, "Alice", "rotate", "RotateLeft"),
        # 非动作行：report_observation 不提交仿真动作。
        _row(2, "Alice", "report_observation", "report_observation(text=hi)"),
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    registry = AliasRegistry.load(run_dir / "alias_registry.json")
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=registry)

    stats = runner.run()
    unity.stop()

    assert [step["action"] for step in mock.steps] == ["Pass", "RotateLeft"]
    assert stats.task_rows == 3
    assert stats.replayed == 1
    assert stats.skipped_original_failure == 1
    assert stats.skipped_unreplayable == 1
    assert stats.replayed + stats.skipped_total == stats.task_rows


# ── 4. spawn 重建 / metadata 兼容 ─────────────────────────────────────────


def test_spawn_random_rebuilds_layout(tmp_path: Path) -> None:
    rows = [_row(1, "Alice", "rotate", "RotateLeft")]
    run_dir = _write_run_dir(
        tmp_path, rows=rows, spawn_mode="random", spawn_seed=11
    )
    unity, mock = _make_frame_mock_unity(spawn_mode="random", spawn_seed=11)
    assert mock.steps[0] == {"action": "InitialRandomSpawn", "randomSeed": 11}

    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=None)
    runner.run()
    unity.stop()
    assert mock.steps[0] == {"action": "InitialRandomSpawn", "randomSeed": 11}
    assert [step["action"] for step in mock.steps[1:]] == ["Pass", "RotateLeft"]


def test_default_spawn_has_no_initial_random_spawn(tmp_path: Path) -> None:
    rows = [_row(1, "Alice", "rotate", "RotateLeft")]
    run_dir = _write_run_dir(tmp_path, rows=rows, spawn_mode="default")
    unity, mock = _make_frame_mock_unity(spawn_mode="default")
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=None)
    runner.run()
    unity.stop()
    assert all(
        step["action"] != "InitialRandomSpawn" for step in mock.steps
    )


def test_metadata_without_spawn_fields_defaults(tmp_path: Path) -> None:
    """旧 run（RP4 口径）：metadata 无 spawn 字段 → 缺省按 default 解释。"""
    run_dir = _write_run_dir(tmp_path, rows=[], include_spawn=False)
    spec = replay.read_run_spec(run_dir)
    assert spec.spawn_mode == "default"
    assert spec.spawn_seed is None
    assert spec.seed == 42


def test_read_run_spec_missing_required_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "metadata.json").write_text(
        json.dumps({"scene": "FloorPlan1"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="num_agents"):
        replay.read_run_spec(run_dir)
    (run_dir / "metadata.json").write_text(
        json.dumps({"scene": "FloorPlan1", "num_agents": 2, "agent_names": ["A"]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="agent_names"):
        replay.read_run_spec(run_dir)
    with pytest.raises(FileNotFoundError):
        replay.read_run_spec(tmp_path / "missing")


def test_unknown_agent_fails_fast(tmp_path: Path) -> None:
    rows = [_row(1, "Carol", "rotate", "RotateLeft")]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=None)
    with pytest.raises(ValueError, match="Carol"):
        runner.run()
    assert mock.steps == []  # 校验先于任何仿真动作


# ── 5. 俯视帧 / 端到端（args → report） ───────────────────────────────────


def test_overhead_capture_every_k(tmp_path: Path) -> None:
    rows = [
        _row(1, "Alice", "rotate", "RotateLeft"),
        _row(2, "Alice", "rotate", "RotateRight"),
        _row(3, "Bob", "rotate", "RotateRight"),
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    runner = _make_runner(
        run_dir, unity, tmp_path / "out", registry=None, overhead_every=2
    )
    stats = runner.run()
    unity.stop()

    out = tmp_path / "out"
    assert (out / "overhead" / "overhead_step_002.png").is_file()  # K 倍数边界
    assert (out / "overhead" / "overhead_step_003.png").is_file()  # 末步收尾
    assert stats.overhead_frames == 2
    toggles = [s for s in mock.steps if s["action"] == "ToggleMapView"]
    assert len(toggles) == 4  # 每张俯视 = 开/关各一次（拍完回位）
    assert all(t["agentId"] == 0 for t in toggles)


def test_replay_put_object_resolves_receptacle_alias(tmp_path: Path) -> None:
    """PutObject：alias 解析 + 持物解析走在线同一条 step_for_agent 路径。"""
    rows = [
        _row(1, "Alice", "pickup", "PickupObject(Apple_1)"),
        _row(2, "Alice", "put", "PutObject(CounterTop_1)"),
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    registry = AliasRegistry.load(run_dir / "alias_registry.json")
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=registry)

    stats = runner.run()
    unity.stop()

    assert mock.steps[1] == {
        "action": "PickupObject",
        "objectId": APPLE_RAW,
        "agentId": 0,
    }
    assert mock.steps[2] == {
        "action": "PutObject",
        "objectId": COUNTER_RAW,
        "agentId": 0,
    }
    assert stats.replayed == 2
    assert stats.replayed_soft_failures == 0


def test_replay_empty_hand_put_soft_fails(tmp_path: Path) -> None:
    """空手 PutObject：在线路径的域级软失败（不崩、不提交仿真动作、计数留痕）。"""
    rows = [_row(1, "Alice", "put", "PutObject(CounterTop_1)")]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, mock = _make_frame_mock_unity()
    registry = AliasRegistry.load(run_dir / "alias_registry.json")
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=registry)

    stats = runner.run()
    unity.stop()

    assert [step["action"] for step in mock.steps] == ["Pass"]  # 未提交 PutObject
    assert stats.replayed == 1
    assert stats.replayed_soft_failures == 1
    assert stats.replayed + stats.skipped_total == stats.task_rows


def test_render_videos_builds_ffmpeg_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--gif``：ffmpeg 命令面（-vf 旗标 / glob 输入 / 每目标 mp4+gif 各一次）。"""
    import subprocess

    rows = [_row(1, "Alice", "rotate", "RotateLeft")]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    unity, _mock = _make_frame_mock_unity()
    runner = _make_runner(run_dir, unity, tmp_path / "out", registry=None)
    runner.run()
    unity.stop()

    commands: list[list[str]] = []

    def _fake_run(command: list[str], **kwargs: Any) -> Any:
        commands.append(list(command))
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        replay.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    monkeypatch.setattr(replay.subprocess, "run", _fake_run)
    report = runner.render_videos()

    assert report["ffmpeg"] == "/usr/bin/ffmpeg"
    assert set(report["targets"]) == {"Alice", "Bob"}
    assert all(item["mp4"]["ok"] and item["gif"]["ok"] for item in report["targets"].values())
    assert len(commands) == 4  # 2 agent × (mp4 + gif)
    for command in commands:
        assert "-vf" in command, command  # 滤镜必须带旗标（否则被当输出文件名）
        assert "-pattern_type" in command and "glob" in command
        assert command[command.index("-vf") + 1]
    mp4_commands = [c for c in commands if c[-1].endswith(".mp4")]
    gif_commands = [c for c in commands if c[-1].endswith(".gif")]
    assert len(mp4_commands) == 2 and len(gif_commands) == 2
    assert all("libx264" in c for c in mp4_commands)
    assert all("palettegen" in c[c.index("-vf") + 1] for c in gif_commands)


def test_run_replay_end_to_end_injected_factory(tmp_path: Path) -> None:
    """args → spec → controller → 帧（真 PNG 编码）→ report 全链路。"""
    rows = [
        _row(1, "Alice", "rotate", "RotateLeft"),
        _row(1, "Bob", "navigate", "Teleport(CounterTop_1)"),
    ]
    run_dir = _write_run_dir(tmp_path, rows=rows)
    out = tmp_path / "out"
    args = replay.build_parser().parse_args(
        ["--run-dir", str(run_dir), "--out", str(out)]
    )

    def _factory(options: dict[str, Any]) -> _FrameMockController:
        return _FrameMockController(
            agent_count=int(options["agentCount"]),
            scene=str(options["scene"]),
            objects=list(_DEFAULT_OBJECTS),
        )

    report = replay.run_replay(args, controller_factory=_factory)

    assert report["scene"] == "FloorPlan1"
    assert report["spawn_mode"] == "default"
    assert report["alias_registry"]["present"] is True
    assert report["coverage"]["replayed"] == 2
    assert report["coverage"]["coverage_rate"] == 1.0
    assert report["duration_seconds"] >= 0.0

    frame = out / "frames" / "Alice" / "frame_001.png"
    assert frame.is_file()
    assert frame.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # 真 PNG 编码产物
    assert (out / "frames" / "Bob" / "frame_001.png").is_file()
