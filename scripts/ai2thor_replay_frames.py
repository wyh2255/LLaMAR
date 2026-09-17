#!/usr/bin/env python3
"""ai2thor_replay_frames.py — run 目录动作序列重放 → 全量帧补全（F-frame 方案 b）。

用途
----
对**已落盘的 AI2Thor run 目录**做离线重放：读 ``agent_interactions.csv`` 的
（Step, Agent, Action）序列，在**同参重建**的 Unity 场景里按序重放每个动作，
逐步抓取每个 agent 的 POV 帧，产出可直接拼视频的帧序列。不调 LLM、不重演
决策（设计 2026-09-17 §1.4「replay 补帧」；A100 上运行，不进 CI）。

输入契约（run 目录）
--------------------
- ``metadata.json``：``scene`` / ``num_agents`` / ``agent_names`` 为必需；
  ``spawn_mode``（缺省按 ``default`` 解释——旧 run 兼容，设计 §3）与
  ``spawn_seed`` 决定初始布局重建；``width`` / ``height`` 可选（旧 run 未
  记录渲染尺寸 → 用 ``--width/--height``，缺省 600×600）。
- ``agent_interactions.csv``：使用 ``Step`` / ``Agent`` / ``Action`` /
  ``Success`` 列（Observation 含内嵌换行，由 csv 模块按引号语义切分）。
  ``Action`` 为工具的 AI2Thor 动作串（``MoveAhead`` / ``LookUp(30)`` /
  ``PickupObject(<alias>)`` / ``Teleport(<target>)`` / ``Done`` …；口径见
  ``Ai2ThorEnvPack.format_worker_action``），对象引用保留 worker 可见
  alias（绝不暴露 raw objectId）。
- ``alias_registry.json``（新 run 起落盘；可选）：alias→rawObjectId 双向映射。

重建与动作映射
--------------
- 同参 Controller 启动：``headless=False + platform=CloudRendering``（设计
  §1.3 实测：``headless=True`` 时 ``step()`` 强制 ``renderImage=False``，
  帧全 None）；``spawn_mode=random`` 时由 ``UnityController`` 构造期执行一次
  ``InitialRandomSpawn(randomSeed=spawn_seed)``（与在线执行同一构造路径，
  失败 fail-fast）。
- 动作映射**逐字复用** ``UnityController.build_action``：本脚本把 CSV 动作
  规整为编排层动作串/dict 后，一律经 ``unity.step_for_agent()`` 下发——与
  ``ControllerExecutor.execute_step`` 的在线执行是同一条映射/归一化路径。
- ``Teleport(<target>)`` 是 navigate 宏动作：重放时按 **NavigateTool 同款
  算法**（``nearest_candidates`` + ``facing_yaw``，复用
  ``ai2thor_orch.tools.worker.navigate`` 的同一实现）从目标对象当前位置与
  ``GetReachablePositions`` 缓存重算出 position/rotation（在线执行本就是该
  确定性计算的产物），再交 ``build_action``（含 horizon 夹取注入）。
- 初始帧：设计 §1.3 实测「初始 ``last_event`` 帧为 None，需先空动作强制
  渲染」——重放前执行一次 ``Pass`` 并抓 ``frame_000.png``。

alias 处理（R3）
---------------
- run 目录**有** ``alias_registry.json`` → alias 动作解析回 rawObjectId 后
  重放（全覆盖）：``PickupObject(Apple_1)`` → ``Apple|+01.2|+00.5|+00.8``；
  ``Teleport(Fridge_1)`` 解析后按 navigate 同款算法重算宏动作。裸类型名
  （如 ``Teleport(Fridge)``）按 NavigateTool 同口径唯一命中解析，多命中
  fail-closed 跳过（绝不猜测）。
- **无**该文件的历史 run → **降级尽力而为**：raw id 动作直接重放
  （``PickupObject(Apple|+01.2|+00.5|+00.8)``），alias 动作跳过并计数；
  结束时打印覆盖率报告（跳过数/总数，见 ``<out>/replay_report.json``）。

输出
----
- ``<out>/frames/<Agent>/frame_<step>.png``：每一步每个 agent 的 POV 帧
  （``frame_000.png`` = 初始帧；同一步内多次渲染只保留**最后一次**，即回合
  末视图）。
- ``<out>/overhead/overhead_step_<step>.png``：``--overhead-every K`` 时
  每 K 步（步号为 K 的倍数）一张俯视图 + 末步收尾一张（``ToggleMapView``
  拍完即 toggle 回原位；设计 §1.3 实测路径）。
- ``<out>/videos/<Agent>.mp4|.gif``：``--gif`` 时经 ffmpeg 拼装（ffmpeg
  缺失只告警，不影响帧产物）。
- ``<out>/replay_report.json``：覆盖率与跳过明细（``ReplayStats``）。

已知边界（如实记录，不宣称超出）
--------------------------------
- **R4 物理微差**：同 seed / 同 build 下宏观可复现，但 PutObject 后物体
  沉降等物理细节可能与原 run 有**像素级差异**——POV 观看无影响；不宣称
  逐像素复现，帧级 diff 验证需另行评估（设计 §1.5 R4）。
- **回合内交错顺序 best-effort**：多 agent 同一步的动作按 CSV 记录顺序
  重放；净状态在多数情况下一致，但动作相互竞争（如两 agent 抢同一物体）
  时可能与在线执行有微差。
- **跳过失败行**：``Success=False`` 的行不重放（可能未提交仿真动作 / 零
  净效应），单独计数；不计入覆盖缺口。
- **帧尺寸**：旧 run 未记录渲染尺寸，缺省 600×600（``--width/--height``
  可覆盖），与在线观测尺寸可能不同（尺寸不参与物理，不影响动作结果）。
- **运行环境**：需 GPU + Unity build（远程 A100；``uv sync --extra
  ai2thor-unity``）；fake 模式不适用（本脚本面向真实 Unity 渲染）。

用法
----
  # A100：对历史 run 降级补帧（无 alias_registry → alias 动作跳过）
  PYTHONPATH=src uv run python scripts/ai2thor_replay_frames.py \\
      --run-dir reports/a100_firstrun_20260914/l3_short \\
      --out /tmp/replay_l3_short --overhead-every 4 --gif

  # 新 run（带 alias_registry.json）→ 全覆盖 + 俯视 + 视频
  PYTHONPATH=src uv run python scripts/ai2thor_replay_frames.py \\
      --run-dir sar_orch/results/<run> --out /tmp/replay_full --gif
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 仓库根 + src 入 sys.path：直跑 `python scripts/ai2thor_replay_frames.py` 时
# 也能 import ai2thor_orch / Agent（与 scripts/ai2thor_runtime_smoke.py 同款）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai2thor_orch.executor.unity_controller import (
    ActionMappingError,
    UnityController,
)
from ai2thor_orch.visibility import AliasRegistry

logger = logging.getLogger("ai2thor_replay_frames")

# ── 动作解析面 ─────────────────────────────────────────────────────────────

#: 与 ``unity_controller._ACTION_RE`` 同形（``Name`` / ``Name(inner)``）。
_ACTION_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*(.*?)\s*\))?\s*$")

#: 与 ``AliasRegistry`` 同形的 raw objectId 模式（``Mug|-01.5|+00.9|+02.3``）。
_RAW_ID_RE = re.compile(
    r"[A-Za-z]\w*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*"
)

#: 携带单个对象引用的动作（inner = alias / 裸类型名 / raw id）。
_OBJECT_REF_ACTIONS = frozenset(
    {"PickupObject", "OpenObject", "CloseObject", "PutObject"}
)

#: navigate 宏动作（inner = 目标对象引用；重放需按 navigate 同款算法重算）。
_TELEPORT_ACTION = "Teleport"

#: 无对象引用、``build_action`` 直通的动作（``LookUp(30)`` / ``RotateLeft``
#: 的度数由 build_action 解析；``Done``/``NoOp`` 映射为空动作 ``Pass``）。
_PLAIN_ACTIONS = frozenset(
    {
        "MoveAhead",
        "MoveBack",
        "MoveLeft",
        "MoveRight",
        "RotateLeft",
        "RotateRight",
        "LookUp",
        "LookDown",
        "Done",
        "NoOp",
        "Idle",
        "Pass",
    }
)

_REPLAYABLE_ACTIONS = _OBJECT_REF_ACTIONS | _PLAIN_ACTIONS | {_TELEPORT_ACTION}

_FRAME_NAME = "frame_{step:03d}.png"
_OVERHEAD_NAME = "overhead_step_{step:03d}.png"

#: 缺省帧尺寸（旧 run 未记录渲染尺寸；帧尺寸不参与物理）。
_DEFAULT_SIZE = 600


# ── 基础工具 ───────────────────────────────────────────────────────────────


def _safe_int(value: Any) -> int:
    """容错整数解析（CSV 单元格 → int；不可解析回退 0）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _sanitize_name(name: str) -> str:
    """目录名安全化（保留字母数字与 ``._-``，其余替换为 ``_``）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "agent"


def parse_action(action: str) -> tuple[str | None, str | None]:
    """``Name`` / ``Name(inner)`` → ``(name, inner)``；不可解析 → ``(None, None)``。"""
    match = _ACTION_RE.match(action or "")
    if match is None:
        return None, None
    inner = match.group(2)
    return match.group(1), (inner if inner else None)


def resolve_object_ref(
    ref: str | None, registry: AliasRegistry | None
) -> tuple[str | None, str]:
    """对象引用（alias / 裸类型名 / raw id）→ raw objectId（fail-closed 不猜测）。

    与 ``NavigateTool._resolve_target`` 同口径：精确 alias 优先、裸类型名
    唯一命中回退；无 registry（历史 run 降级）时仅接受 raw id 形式。

    Returns:
        ``(raw_id, "")`` 命中；``(None, reason)`` 未命中（reason 进跳过明细）。
    """
    token = (ref or "").strip()
    if not token:
        return None, "动作缺少对象参数"
    if registry is not None:
        raw = registry.raw(token)
        if raw:
            return raw, ""
    if _RAW_ID_RE.fullmatch(token):
        return token, ""
    if registry is None:
        return None, f"{token} 是 alias 形态但无 alias_registry.json（历史 run 降级）"
    matches = registry.aliases_for_type(token)
    if len(matches) == 1:
        raw = registry.raw(matches[0])
        if raw:
            return raw, ""
    if matches:
        return None, f"{token} 裸类型名多命中（{len(matches)} 个），fail-closed 跳过"
    return None, f"{token} 不在 alias_registry.json 中"


def encode_png(frame: Any) -> bytes:
    """RGB 帧（numpy ``HxWx3``）→ PNG bytes（cv2；项目核心依赖 opencv-python）。"""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError(
            "帧编码需要 opencv-python / numpy（uv sync --extra ai2thor）"
        ) from exc

    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError(f"帧形状异常（期望 HxWx3）: {array.shape}")
    bgr = cv2.cvtColor(array[:, :, :3], cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("cv2.imencode('.png') 失败")
    return buffer.tobytes()


def _first_frame(event: Any) -> Any:
    """取事件里第一张可用帧（MultiAgentEvent → 各 agent 事件；回退顶层 frame）。"""
    events = getattr(event, "events", None) or [event]
    for agent_event in events:
        frame = getattr(agent_event, "frame", None)
        if frame is not None:
            return frame
    return getattr(event, "frame", None)


# ── 数据面 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSpec:
    """run 目录的重放规格（读自 ``metadata.json``）。"""

    run_dir: Path
    scene: str
    num_agents: int
    seed: int | None
    spawn_mode: str
    spawn_seed: int | None
    agent_names: tuple[str, ...]
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class ActionRow:
    """``agent_interactions.csv`` 的一行（重放关心的四列）。"""

    step: int
    agent: str
    tool: str
    action: str
    success: str


@dataclass
class ReplayStats:
    """覆盖率统计（``replayed + 各跳过桶 == task_rows`` 恒等式由测试锁定）。"""

    rows_total: int = 0
    non_action_rows: int = 0
    task_rows: int = 0
    replayed: int = 0
    replayed_soft_failures: int = 0
    skipped_original_failure: int = 0
    skipped_unreplayable: int = 0
    skipped_alias_unresolved: int = 0
    skipped_teleport_unresolved: int = 0
    frames_written: int = 0
    frames_distinct: int = 0
    steps_total: int = 0
    steps_with_frames: int = 0
    overhead_frames: int = 0
    skip_examples: list[str] = field(default_factory=list)

    @property
    def skipped_total(self) -> int:
        return (
            self.skipped_original_failure
            + self.skipped_unreplayable
            + self.skipped_alias_unresolved
            + self.skipped_teleport_unresolved
        )

    @property
    def coverage_rate(self) -> float:
        """重放覆盖率 = replayed / task_rows（无任务行时为 0.0）。"""
        if self.task_rows <= 0:
            return 0.0
        return self.replayed / self.task_rows

    def add_skip_example(self, message: str, limit: int = 5) -> None:
        """记录前 ``limit`` 条跳过样例（报告的可读证据）。"""
        if len(self.skip_examples) < limit:
            self.skip_examples.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_total": self.rows_total,
            "non_action_rows": self.non_action_rows,
            "task_rows": self.task_rows,
            "replayed": self.replayed,
            "replayed_soft_failures": self.replayed_soft_failures,
            "skipped": {
                "original_failure": self.skipped_original_failure,
                "unreplayable": self.skipped_unreplayable,
                "alias_unresolved": self.skipped_alias_unresolved,
                "teleport_unresolved": self.skipped_teleport_unresolved,
            },
            "coverage_rate": round(self.coverage_rate, 4),
            "frames_written": self.frames_written,
            "frames_distinct": self.frames_distinct,
            "steps_total": self.steps_total,
            "steps_with_frames": self.steps_with_frames,
            "overhead_frames": self.overhead_frames,
            "skip_examples": list(self.skip_examples),
        }


def read_run_spec(run_dir: str | Path) -> RunSpec:
    """读 ``metadata.json`` 合成重放规格（缺必需字段 fail-fast）。"""
    run_path = Path(run_dir)
    meta_path = run_path / "metadata.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"run 目录缺 metadata.json: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise TypeError(f"metadata.json 顶层必须是对象: {meta_path}")

    scene = meta.get("scene")
    if not isinstance(scene, str) or not scene:
        raise ValueError(f"metadata.json 缺 scene 字段: {meta_path}")
    num_agents = meta.get("num_agents")
    if (
        isinstance(num_agents, bool)
        or not isinstance(num_agents, int)
        or num_agents < 1
    ):
        raise ValueError(
            f"metadata.json 的 num_agents 必须是 >=1 整数: {num_agents!r}"
        )
    agent_names = meta.get("agent_names")
    if (
        not isinstance(agent_names, list)
        or len(agent_names) != num_agents
        or not all(isinstance(name, str) and name for name in agent_names)
    ):
        raise ValueError(
            "metadata.json 的 agent_names 必须是长度 = num_agents 的非空字符串"
            f"列表: {agent_names!r}"
        )

    # 旧 run 无 spawn 字段 → 缺省按 default 解释（设计 §3：历史数据口径 A）。
    spawn_mode = meta.get("spawn_mode", "default")
    spawn_seed = meta.get("spawn_seed")
    if spawn_seed is not None and (
        isinstance(spawn_seed, bool) or not isinstance(spawn_seed, int)
    ):
        raise ValueError(f"metadata.json 的 spawn_seed 必须是整数: {spawn_seed!r}")

    def _optional_int(key: str) -> int | None:
        value = meta.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"metadata.json 的 {key} 必须是整数: {value!r}")
        return value

    return RunSpec(
        run_dir=run_path,
        scene=scene,
        num_agents=num_agents,
        seed=_optional_int("seed"),
        spawn_mode=str(spawn_mode),
        spawn_seed=spawn_seed,
        agent_names=tuple(agent_names),
        width=_optional_int("width"),
        height=_optional_int("height"),
    )


def read_action_rows(run_dir: str | Path) -> list[ActionRow]:
    """读 ``agent_interactions.csv`` 的行（按 Step 稳定排序，同 step 保留文件序）。

    Observation 含内嵌换行——csv 模块按引号语义切分；``ToolName`` 取不到时
    回退空串（旧 run 列名可能有出入）。
    """
    csv_path = Path(run_dir) / "agent_interactions.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"run 目录缺 agent_interactions.csv: {csv_path}")
    rows: list[ActionRow] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        missing = sorted({"Step", "Agent", "Action", "Success"} - fields)
        if missing:
            raise ValueError(f"agent_interactions.csv 缺列 {missing}: {csv_path}")
        for raw in reader:
            rows.append(
                ActionRow(
                    step=_safe_int(raw.get("Step")),
                    agent=str(raw.get("Agent") or "").strip(),
                    tool=str(raw.get("ToolName") or "").strip(),
                    action=str(raw.get("Action") or ""),
                    success=str(raw.get("Success") or "").strip(),
                )
            )
    rows.sort(key=lambda row: row.step)
    return rows


# ── 重放主体 ───────────────────────────────────────────────────────────────


class ReplayRunner:
    """按 CSV 动作序列重放并抓帧。

    Args:
        spec: run 规格（:func:`read_run_spec`）。
        rows: 动作行（:func:`read_action_rows`，已按 Step 稳定排序）。
        unity: :class:`UnityController`（或同形替身——单测/冒烟注入）。
        out_dir: 输出目录。
        registry: alias 映射（``None`` = 历史 run 降级模式）。
        overhead_every: 每 K 步抓一张俯视图（0 = 关闭；另末步追加一张）。
        fps: ``--gif`` 视频帧率。
        width / height: 帧尺寸（报告用）。
        encode_frame: 帧编码器（缺省 :func:`encode_png`；单测注入）。
    """

    def __init__(
        self,
        *,
        spec: RunSpec,
        rows: Sequence[ActionRow],
        unity: Any,
        out_dir: str | Path,
        registry: AliasRegistry | None = None,
        overhead_every: int = 0,
        fps: int = 4,
        width: int = _DEFAULT_SIZE,
        height: int = _DEFAULT_SIZE,
        encode_frame: Callable[[Any], bytes] | None = None,
    ) -> None:
        self._spec = spec
        self._rows = list(rows)
        self._unity = unity
        self._out = Path(out_dir)
        self._registry = registry
        self._overhead_every = max(0, int(overhead_every))
        self._fps = max(1, int(fps))
        self._width = int(width)
        self._height = int(height)
        self._encode = encode_frame or encode_png
        self._agent_index: dict[str, int] = {
            name: idx for idx, name in enumerate(spec.agent_names)
        }
        self._stats = ReplayStats()
        self._reachable: list[dict[str, float]] | None = None
        self._frames: set[tuple[str, int]] = set()
        self._overhead_steps: list[int] = []
        self._warnings: list[str] = []
        self._video_report: dict[str, Any] = {}

    # ── 主流程 ─────────────────────────────────────────────────────────

    def run(self) -> ReplayStats:
        """执行完整重放（初始帧 → 逐步重放 → 俯视帧 → 统计收口）。"""
        self._out.mkdir(parents=True, exist_ok=True)
        self._validate_rows()
        stats = self._stats
        stats.rows_total = len(self._rows)
        stats.steps_total = len(
            {row.step for row in self._rows if self._is_task_row(row)}
        )

        # 初始帧：设计 §1.3 —— 初始 last_event 帧为 None，先空动作强制渲染。
        self._unity.step_for_agent(agent_idx=0, action="Pass")
        self._capture_frames(0, self._raw_event())

        try:
            previous_step: int | None = None
            for row in self._rows:
                if not self._is_task_row(row):
                    stats.non_action_rows += 1
                    continue
                stats.task_rows += 1
                if previous_step is not None and row.step != previous_step:
                    self._maybe_overhead(previous_step)
                previous_step = row.step
                self._replay_row(row)
            if previous_step is not None:
                self._maybe_overhead(previous_step)  # 命中 K 倍数的步边界
                self._capture_final_overhead(previous_step)  # 末步收尾（去重）
        finally:
            stats.frames_distinct = len(self._frames)
            stats.steps_with_frames = len(
                {step for _, step in self._frames if step != 0}
            )
        if stats.frames_written == 0:
            self._warn(
                "未捕获到任何帧（controller 不提供 last_event/.frame，"
                "或渲染未开启——确认 headless=False + CloudRendering）"
            )
        return stats

    def report(self) -> dict[str, Any]:
        """组装 ``replay_report.json`` 载荷。"""
        return {
            "run_dir": str(self._spec.run_dir),
            "out_dir": str(self._out),
            "scene": self._spec.scene,
            "num_agents": self._spec.num_agents,
            "seed": self._spec.seed,
            "spawn_mode": getattr(self._unity, "spawn_mode", self._spec.spawn_mode),
            "spawn_seed": getattr(self._unity, "spawn_seed", self._spec.spawn_seed),
            "frame_size": {"width": self._width, "height": self._height},
            "alias_registry": {
                "present": self._registry is not None,
                "entries": self._registry.size if self._registry is not None else 0,
            },
            "coverage": self._stats.to_dict(),
            "overhead": {
                "every": self._overhead_every,
                "steps": list(self._overhead_steps),
            },
            "video": self._video_report,
            "warnings": list(self._warnings),
        }

    # ── 行级分派 ───────────────────────────────────────────────────────

    @staticmethod
    def _is_task_row(row: ActionRow) -> bool:
        """worker 工具行动作行（Coordinator 状态行 / 空动作行排除）。"""
        return bool(row.action.strip()) and row.agent != "Coordinator"

    def _validate_rows(self) -> None:
        """启动前全量校验 agent 名映射（fail-fast，重放中途不因数据错中断）。"""
        known = set(self._agent_index)
        unknown = sorted(
            {row.agent for row in self._rows if self._is_task_row(row)} - known
        )
        if unknown:
            raise ValueError(
                f"agent_interactions.csv 出现未知 agent {unknown}；"
                f"metadata.json agent_names={list(self._spec.agent_names)}"
            )

    def _replay_row(self, row: ActionRow) -> None:
        """单行动作：分类 → 解析 → 执行（或计数跳过）。"""
        stats = self._stats
        if row.success == "False":
            # 失败行不重放：可能未提交仿真动作 / 零净效应（模块 docstring 边界）。
            stats.skipped_original_failure += 1
            return
        name, inner = parse_action(row.action)
        if name is None or name not in _REPLAYABLE_ACTIONS:
            stats.skipped_unreplayable += 1
            stats.add_skip_example(
                f"step {row.step} {row.agent}: 不可重放动作 {row.action!r}"
            )
            return
        agent_idx = self._agent_index[row.agent]
        try:
            if name == _TELEPORT_ACTION:
                executed = self._replay_teleport(row, agent_idx, inner)
            elif name in _OBJECT_REF_ACTIONS:
                executed = self._replay_object_action(row, agent_idx, name, inner)
            else:
                self._execute(row.step, agent_idx, row.action)
                executed = True
        except ActionMappingError as exc:
            # 数据与映射契约不符（如度数非法）：跳过并留痕，不拖垮整个重放。
            stats.skipped_unreplayable += 1
            stats.add_skip_example(
                f"step {row.step} {row.agent}: 映射失败 {exc}"
            )
            return
        if executed:
            stats.replayed += 1

    def _replay_object_action(
        self, row: ActionRow, agent_idx: int, name: str, inner: str | None
    ) -> bool:
        """``PickupObject`` / ``OpenObject`` / ``CloseObject`` / ``PutObject``。"""
        raw_id, reason = resolve_object_ref(inner, self._registry)
        if raw_id is None:
            self._skip_alias(row, reason)
            return False
        self._execute(row.step, agent_idx, f"{name}({raw_id})")
        return True

    def _replay_teleport(
        self, row: ActionRow, agent_idx: int, inner: str | None
    ) -> bool:
        """``Teleport(<target>)`` 宏动作：按 NavigateTool 同款算法重算后重放。"""
        raw_id, reason = resolve_object_ref(inner, self._registry)
        if raw_id is None:
            self._skip_alias(row, reason)
            return False
        position = self._object_position(raw_id)
        if position is None:
            self._skip_teleport(row, f"目标 {raw_id!r} 不在当前 controller metadata")
            return False
        reachable = self._query_reachable_positions()
        if not reachable:
            self._skip_teleport(row, "GetReachablePositions 查询为空（fail-closed）")
            return False
        # 惰性 import：navigate 模块拖 Agent/barrier 依赖，仅 Teleport 行需要。
        from ai2thor_orch.tools.worker.navigate import (
            facing_yaw,
            nearest_candidates,
        )

        candidates = nearest_candidates(position, reachable)
        if not candidates:
            self._skip_teleport(row, "无可达候选点")
            return False
        for candidate in candidates:
            yaw = facing_yaw(
                float(position["x"]) - candidate["x"],
                float(position["z"]) - candidate["z"],
            )
            action = {
                "action": "Teleport",
                "position": dict(candidate),
                "rotation": {"x": 0.0, "y": yaw, "z": 0.0},
            }
            # navigate 在线语义：候选点逐个尝试，成功即停（同一确定性算法）。
            if self._execute(row.step, agent_idx, action):
                return True
        return True  # 候选全败也视为已重放（软失败计数已在 _execute 记录）

    def _skip_alias(self, row: ActionRow, reason: str) -> None:
        self._stats.skipped_alias_unresolved += 1
        self._stats.add_skip_example(
            f"step {row.step} {row.agent}: {row.action} → {reason}"
        )

    def _skip_teleport(self, row: ActionRow, reason: str) -> None:
        self._stats.skipped_teleport_unresolved += 1
        self._stats.add_skip_example(
            f"step {row.step} {row.agent}: {row.action} → {reason}"
        )

    # ── 执行 / 读面 ────────────────────────────────────────────────────

    def _execute(self, step: int, agent_idx: int, action: Any) -> bool:
        """经 ``unity.step_for_agent`` 执行一次动作并抓帧（返回 lastActionSuccess）。"""
        event = self._unity.step_for_agent(agent_idx=agent_idx, action=action)
        success = bool((getattr(event, "metadata", {}) or {}).get("lastActionSuccess"))
        if not success:
            self._stats.replayed_soft_failures += 1
        self._capture_frames(step, self._raw_event())
        return success

    def _raw_event(self) -> Any:
        """底层 controller 的最近原始事件（帧读取面；ai2thor 的 ``last_event``）。"""
        return getattr(getattr(self._unity, "controller", None), "last_event", None)

    def _object_position(self, raw_id: str) -> dict[str, Any] | None:
        """从最近一次 controller metadata 找对象当前位置（navigate 同读面）。"""
        last_metadata = getattr(self._unity, "last_metadata", {}) or {}
        for metadata in last_metadata.values():
            objects = metadata.get("objects") if isinstance(metadata, dict) else None
            if not isinstance(objects, list):
                continue
            for obj in objects:
                if not isinstance(obj, dict) or obj.get("objectId") != raw_id:
                    continue
                position = obj.get("position")
                if isinstance(position, dict) and "x" in position and "z" in position:
                    return dict(position)
        return None

    def _query_reachable_positions(self) -> list[dict[str, float]]:
        """``GetReachablePositions`` 只读查询（按 run 缓存；navigate 同口径）。"""
        if self._reachable is not None:
            return self._reachable
        event = self._unity.step_for_agent(
            agent_idx=0, action="GetReachablePositions"
        )
        metadata = getattr(event, "metadata", {}) or {}
        raw_positions = metadata.get("actionReturn") or metadata.get(
            "reachablePositions"
        )
        positions = (
            [dict(point) for point in raw_positions if isinstance(point, dict)]
            if isinstance(raw_positions, list)
            else []
        )
        self._reachable = positions
        return positions

    # ── 帧捕获 ─────────────────────────────────────────────────────────

    def _capture_frames(self, step: int, event: Any) -> int:
        """把事件里各 agent 的渲染帧写入 ``frames/<Agent>/frame_<step>.png``。"""
        if event is None:
            return 0
        events = getattr(event, "events", None) or [event]
        written = 0
        for idx, agent_event in enumerate(events):
            if idx >= self._spec.num_agents:
                break
            frame = getattr(agent_event, "frame", None)
            if frame is None:
                continue
            directory = self._out / "frames" / _sanitize_name(
                self._spec.agent_names[idx]
            )
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / _FRAME_NAME.format(step=step)
            path.write_bytes(self._encode(frame))
            self._frames.add((directory.name, step))
            written += 1
        self._stats.frames_written += written
        return written

    def _maybe_overhead(self, step: int) -> None:
        """步边界钩子：步号为 ``--overhead-every`` 倍数时抓俯视帧（去重）。"""
        if self._overhead_every <= 0 or step <= 0:
            return
        if step % self._overhead_every != 0 or step in self._overhead_steps:
            return
        self._capture_overhead(step)

    def _capture_final_overhead(self, step: int) -> None:
        """末步收尾俯视（``--overhead-every`` 开启时；已在倍数边界拍过则去重）。"""
        if self._overhead_every <= 0 or step <= 0 or step in self._overhead_steps:
            return
        self._capture_overhead(step)

    def _capture_overhead(self, step: int) -> None:
        """``ToggleMapView`` 俯视帧（拍完 toggle 回原位；设计 §1.3 实测路径）。

        该动作不在编排层动作表内（不经 ``build_action``）——它只服务抓帧，
        不参与 agent 决策语义；失败只告警，不中断重放。
        """
        controller = getattr(self._unity, "controller", None)
        if controller is None:
            return
        frame = None
        try:
            event = controller.step({"action": "ToggleMapView", "agentId": 0})
            frame = _first_frame(event)
        except Exception as exc:  # noqa: BLE001 - 俯视是增强项，绝不阻塞
            self._warn(f"step {step} ToggleMapView 失败（俯视帧跳过）: {exc}")
        finally:
            try:
                controller.step({"action": "ToggleMapView", "agentId": 0})
            except Exception as exc:  # noqa: BLE001 - 回位失败只告警
                self._warn(f"ToggleMapView 回位失败（可能影响后续帧视图）: {exc}")
        if frame is None:
            self._warn(f"step {step} 俯视帧为空（已跳过）")
            return
        directory = self._out / "overhead"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _OVERHEAD_NAME.format(step=step)
        path.write_bytes(self._encode(frame))
        self._overhead_steps.append(step)
        self._stats.overhead_frames += 1

    # ── 视频（可选） ───────────────────────────────────────────────────

    def render_videos(self) -> dict[str, Any]:
        """``--gif``：ffmpeg 把各 agent 帧序列拼 mp4 + gif（缺 ffmpeg 只告警）。"""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self._warn("未找到 ffmpeg，--gif 跳过（帧已全量落盘）")
            self._video_report = {"ffmpeg": None, "targets": {}}
            return self._video_report
        targets: dict[str, Any] = {}
        for name, directory in self._video_sequence_dirs():
            if len(sorted(directory.glob("*.png"))) < 2:
                continue
            targets[name] = self._render_one(ffmpeg, name, directory)
        self._video_report = {"ffmpeg": ffmpeg, "targets": targets}
        return self._video_report

    def _video_sequence_dirs(self) -> Iterable[tuple[str, Path]]:
        frames_root = self._out / "frames"
        if frames_root.is_dir():
            for directory in sorted(p for p in frames_root.iterdir() if p.is_dir()):
                yield directory.name, directory
        overhead_root = self._out / "overhead"
        if overhead_root.is_dir():
            yield "overhead", overhead_root

    def _render_one(self, ffmpeg: str, name: str, directory: Path) -> dict[str, Any]:
        videos = self._out / "videos"
        videos.mkdir(parents=True, exist_ok=True)
        pattern = str(directory / "*.png")
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        base = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(self._fps),
            "-pattern_type",
            "glob",
            "-i",
            pattern,
        ]
        mp4 = videos / f"{name}.mp4"
        gif = videos / f"{name}.gif"
        palette = f"{scale},split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        return {
            "mp4": self._run_ffmpeg(
                base
                + [
                    "-vf",
                    scale,
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(mp4),
                ],
                mp4,
            ),
            "gif": self._run_ffmpeg(
                base + ["-vf", palette, "-loop", "0", str(gif)], gif
            ),
        }

    @staticmethod
    def _run_ffmpeg(command: list[str], target: Path) -> dict[str, Any]:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False
        )
        ok = completed.returncode == 0 and target.is_file()
        payload: dict[str, Any] = {"ok": ok}
        if not ok:
            tail = (completed.stderr or "").strip().splitlines()[-3:]
            payload["error"] = " | ".join(tail) or f"exit={completed.returncode}"
        return payload

    # ── 杂项 ───────────────────────────────────────────────────────────

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning("replay: %s", message)


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai2thor_replay_frames.py",
        description=(
            "AI2Thor run 目录动作重放 → 全量帧补全（F-frame 方案 b；A100 上运行）"
        ),
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="run 目录（含 metadata.json / agent_interactions.csv / 可选 alias_registry.json）",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="输出目录（frames/ overhead/ videos/ replay_report.json）",
    )
    parser.add_argument(
        "--overhead-every",
        type=int,
        default=0,
        metavar="K",
        help="每 K 步抓一张 ToggleMapView 俯视帧（缺省 0 = 关闭；末步追加一张）",
    )
    parser.add_argument(
        "--gif",
        action="store_true",
        help="用 ffmpeg 拼每个 agent 帧序列的 mp4 + gif（ffmpeg 缺失只告警）",
    )
    parser.add_argument(
        "--width", type=int, default=None, help="渲染宽（缺省：metadata.width 或 600）"
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="渲染高（缺省：metadata.height 或 600）",
    )
    parser.add_argument("--fps", type=int, default=4, help="--gif 视频帧率（缺省 4）")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    return parser


def run_replay(
    args: argparse.Namespace, *, controller_factory: Any = None
) -> dict[str, Any]:
    """执行一次重放并返回报告载荷（``controller_factory`` 供单测/冒烟注入）。"""
    started = time.time()
    spec = read_run_spec(args.run_dir)
    rows = read_action_rows(args.run_dir)
    width = args.width or spec.width or _DEFAULT_SIZE
    height = args.height or spec.height or _DEFAULT_SIZE
    if width < 1 or height < 1:
        raise ValueError(f"width/height 必须为正: {width}x{height}")

    registry = None
    registry_path = spec.run_dir / "alias_registry.json"
    if registry_path.is_file():
        registry = AliasRegistry.load(registry_path)
        logger.info("alias_registry.json 已加载（%d 条映射）", registry.size)
    else:
        logger.warning(
            "无 alias_registry.json —— 降级尽力而为（alias 动作跳过并计数）"
        )

    # 同参重建：headless=False + CloudRendering（headless=True 无帧，设计
    # §1.3）；spawn_mode=random 时由 UnityController 构造期执行
    # InitialRandomSpawn（同一构造路径，失败 fail-fast）。
    unity = UnityController(
        scene=spec.scene,
        num_agents=spec.num_agents,
        width=width,
        height=height,
        headless=False,
        platform="CloudRendering",
        spawn_mode=spec.spawn_mode,
        spawn_seed=spec.spawn_seed,
        controller_factory=controller_factory,
    )
    runner = ReplayRunner(
        spec=spec,
        rows=rows,
        unity=unity,
        out_dir=args.out,
        registry=registry,
        overhead_every=args.overhead_every,
        fps=args.fps,
        width=width,
        height=height,
    )
    try:
        runner.run()
        if args.gif:
            runner.render_videos()
    finally:
        unity.stop()
    report = runner.report()
    report["duration_seconds"] = round(time.time() - started, 2)
    return report


def _print_summary(report: dict[str, Any], report_path: Path) -> None:
    coverage = report["coverage"]
    skipped = coverage["skipped"]
    registry = report["alias_registry"]
    registry_text = (
        f"已加载（{registry['entries']} 条映射，alias 全覆盖口径）"
        if registry["present"]
        else "缺失 —— 降级尽力而为（alias 动作跳过并计数）"
    )
    frame_size = report["frame_size"]
    lines = [
        "",
        "=" * 64,
        "AI2Thor Replay Frames 覆盖率报告",
        "=" * 64,
        f"run_dir       : {report['run_dir']}",
        f"out           : {report['out_dir']}",
        (
            f"scene         : {report['scene']} | agents={report['num_agents']}"
            f" | spawn={report['spawn_mode']}({report['spawn_seed']})"
            f" | frame={frame_size['width']}x{frame_size['height']}"
        ),
        f"alias_registry: {registry_text}",
        (
            f"动作覆盖      : {coverage['replayed']}/{coverage['task_rows']}"
            f" ({coverage['coverage_rate'] * 100:.1f}%) —— 跳过:"
            f" original_failure={skipped['original_failure']}"
            f" alias_unresolved={skipped['alias_unresolved']}"
            f" teleport_unresolved={skipped['teleport_unresolved']}"
            f" unreplayable={skipped['unreplayable']}"
        ),
        f"重放软失败    : {coverage['replayed_soft_failures']}",
        (
            f"帧            : 写入 {coverage['frames_written']} 张 / 去重"
            f" {coverage['frames_distinct']} 张（覆盖"
            f" {coverage['steps_with_frames']}/{coverage['steps_total']} 步），"
            f"俯视 {coverage['overhead_frames']} 张"
        ),
    ]
    for example in coverage["skip_examples"]:
        lines.append(f"  跳过样例: {example}")
    video = report.get("video") or {}
    if video:
        targets = video.get("targets") or {}
        mp4_ok = sum(1 for item in targets.values() if item.get("mp4", {}).get("ok"))
        lines.append(
            f"视频          : ffmpeg={video.get('ffmpeg')}，目标 {len(targets)} 个"
            f"（mp4 成功 {mp4_ok}）"
        )
    for warning in report["warnings"]:
        lines.append(f"  [warn] {warning}")
    lines.append(f"报告          : {report_path}")
    lines.append("=" * 64)
    print("\n".join(lines))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        report = run_replay(args)
    except KeyboardInterrupt:
        logger.warning("重放被中断（帧已落盘部分保留）")
        return 130
    except Exception:
        logger.exception("replay 失败")
        return 1
    report_path = Path(args.out) / "replay_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _print_summary(report, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
