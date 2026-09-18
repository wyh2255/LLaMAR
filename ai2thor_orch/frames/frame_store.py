"""FrameStore —— 运行时关键帧存储（F-frame；设计 2026-09-17 §1.4 帧存储契约）。

设计基线（``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md``
§1.4 逐条落地）：

- **内存 ring**：每 agent 保留 latest N 帧（缺省 N=1——VLM 只读最新一帧；加大
  只为排障留档，读面 :meth:`FrameStore.latest` 始终返回最新一条）；
- **可选落盘**：``<run_dir>/frames/<AgentName>/round_<N>_<tag>.png``
  （tag ∈ :data:`VALID_TAGS`）；``run_dir=None`` = 内存-only（单测 / 无落盘
  的嵌入用法）；
- **读面** :meth:`FrameStore.latest`：``(round_no, frame) | None``——下游
  F-vlm（VisionHooks）按「round_no == 当前 step」的新鲜度契约消费
  （设计 §2.3；不匹配则不附图，fail-open）。

编码约定：``record`` 的帧入参是 **RGB**（ai2thor ``Event.frame`` 的原生
顺序）；落盘经 OpenCV 时转 BGR（``cv2.imwrite`` 的通道约定），读回即得
原色——不要用裸 ``cv2.imwrite(frame_rgb)`` 直接写 RGB 数组（红蓝互换）。

线程安全：``record`` 由 controller 执行线程（ControllerExecutor 的单线程池/
收尾线程）调用，``latest`` 预计由 worker 线程（F-vlm 注入时）调用——单锁
串行化。写盘失败**不阻塞回合**（ring 读面不受影响；失败响亮记一次日志，
不刷屏）——与 SightingStore 的「审计/素材通道」定位一致。

不复用 SAR 的帧/地图组件（设计 §4.3 已定：轻量专用 store）。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 落盘 tag 白名单（设计 §1.4 枚举基 + 捕获点③的 run 级 ``overhead``
#: + D4 新交互动词 ``slice_ok`` / ``clean_ok`` / ``toggle_on_ok`` /
#: ``toggle_off_ok``——与 ``_ACTION_FRAME_TAGS`` 的取值域保持一致，
#: 由 ``test_frame_capture.py::test_action_frame_tags_are_valid_store_tags``
#: 钉子防再漂移）。
#: 文件名契约 ``round_<N>_<tag>.png`` 直接消费该值——非法 tag 在 ``record``
#: 层 fail-fast（绝不把笔误写进文件名）。
VALID_TAGS: frozenset[str] = frozenset(
    {
        "init",
        "pickup_ok",
        "put_ok",
        "open_ok",
        "close_ok",
        "navigate_ok",
        "done",
        "final",
        "overhead",
        # D4：任务集其余交互动词（与 pickup 同待遇）。
        "slice_ok",
        "clean_ok",
        "toggle_on_ok",
        "toggle_off_ok",
    }
)


class FrameStore:
    """每 agent 关键帧的内存 ring + 可选落盘（设计 §1.4）。

    Args:
        run_dir: run 目录；帧落 ``<run_dir>/frames/<AgentName>/``（父目录首次
            写入时惰性创建——装配期 barrier 先于 run 目录建立）。``None`` =
            内存-only，不写文件。
        ring_size: 每 agent 保留的最新帧数（缺省 1，供 VLM 读；必须 >= 1）。
        agent_names: agent 槽位 → 目录名（run 级 ``Alice`` / ``Bob`` …）；
            缺省槽位名 ``Agent{idx}``（与 unity 事件归一化的 agent 名同构）。
    """

    def __init__(
        self,
        run_dir: str | Path | None = None,
        *,
        ring_size: int = 1,
        agent_names: Any = None,
    ) -> None:
        if ring_size < 1:
            raise ValueError(f"ring_size must be >= 1, got {ring_size}")
        self._lock = threading.Lock()
        self._ring_size = int(ring_size)
        self._agent_names: list[str] = (
            [str(name) for name in agent_names] if agent_names is not None else []
        )
        #: agent_idx → ring（每条 = (round_no, frame, tag)，最新在右）。
        self._rings: dict[int, deque[tuple[int, Any, str]]] = {}
        self._root: Path | None = (
            Path(run_dir) / "frames" if run_dir is not None else None
        )
        #: 写盘失败的一次性日志闩（磁盘问题不刷屏、不阻塞回合）。
        self._write_failed: bool = False
        self._recorded_count: int = 0

    # ── 写面（controller 捕获点调用）─────────────────────────────────────

    def record(
        self, agent_idx: int, round_no: int, frame_rgb: Any, tag: str
    ) -> Path | None:
        """记一帧：更新该 agent 的 ring；落盘开启时同步写 PNG。

        Args:
            agent_idx: agent 槽位（0-based）。
            round_no: 帧所属回合编号（与 coordinator 环境视图的 step 同口径；
                init 帧 = 0）。
            frame_rgb: RGB 帧（numpy 数组；``None`` 视为调用方缺陷，fail-fast）。
            tag: 关键帧类型，必须 ∈ :data:`VALID_TAGS`。

        Returns:
            落盘路径；内存-only 或写盘失败时 ``None``。

        Raises:
            ValueError: ``agent_idx`` / ``round_no`` 非法、``frame_rgb`` 为
                ``None`` 或 ``tag`` 不在白名单（编码缺陷响亮失败，不静默）。
        """
        if isinstance(agent_idx, bool) or not isinstance(agent_idx, int) or agent_idx < 0:
            raise ValueError(f"agent_idx 必须是非负 int，得到 {agent_idx!r}")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 0:
            raise ValueError(f"round_no 必须是非负 int，得到 {round_no!r}")
        if frame_rgb is None:
            raise ValueError("frame_rgb 不能为 None（调用方应先取帧并跳过缺失）")
        if tag not in VALID_TAGS:
            raise ValueError(f"未知帧 tag {tag!r}；可选 {sorted(VALID_TAGS)}")

        with self._lock:
            ring = self._rings.get(agent_idx)
            if ring is None:
                ring = deque(maxlen=self._ring_size)
                self._rings[agent_idx] = ring
            ring.append((int(round_no), frame_rgb, str(tag)))
            self._recorded_count += 1
            return self._write_png(agent_idx, round_no, frame_rgb, tag)

    def _write_png(
        self, agent_idx: int, round_no: int, frame_rgb: Any, tag: str
    ) -> Path | None:
        """同步写 ``<run_dir>/frames/<AgentName>/round_<N>_<tag>.png``（caller 持锁）。

        RGB → BGR（cv2 约定）；失败只记一次日志（素材通道可缺、回合不可断）。
        """
        root = self._root
        if root is None:
            return None
        try:
            import cv2
            import numpy as np

            path = root / self._agent_dir_name(agent_idx) / f"round_{round_no}_{tag}.png"
            # 每 agent 一个子目录——必须逐次 mkdir（单「父目录已建」闩会在
            # 第二个 agent 的目录上放行不存在的路径，写盘直接失败）。
            path.parent.mkdir(parents=True, exist_ok=True)
            array = np.asarray(frame_rgb)
            if array.ndim == 3 and array.shape[2] == 3:
                array = np.ascontiguousarray(array[:, :, ::-1])  # RGB → BGR
            elif array.ndim == 3 and array.shape[2] == 4:
                array = np.ascontiguousarray(array[:, :, [2, 1, 0, 3]])  # RGBA → BGRA
            if not cv2.imwrite(str(path), array):
                raise OSError(f"cv2.imwrite 返回失败: {path}")
            return path
        except Exception:
            if not self._write_failed:
                self._write_failed = True
                logger.exception(
                    "帧落盘失败（ring 读面不受影响；后续同类失败不再刷屏）"
                )
            return None

    def _agent_dir_name(self, agent_idx: int) -> str:
        """槽位目录名：优先 run 级 agent 名，缺失回退 ``Agent{idx}``。"""
        if 0 <= agent_idx < len(self._agent_names):
            return self._agent_names[agent_idx]
        return f"Agent{agent_idx}"

    # ── 读面（F-vlm 注入 / 测试）────────────────────────────────────────

    def latest(self, agent_idx: int) -> tuple[int, Any] | None:
        """该 agent 最新一帧 ``(round_no, frame)``；无帧时 ``None``。

        F-vlm 的新鲜度契约：``round_no`` 必须等于当前 Environment State 的
        step 才附图（设计 §2.3，fail-open 不附陈旧帧）。返回的 frame 归调用方
        只读使用（不复制数组）。
        """
        with self._lock:
            ring = self._rings.get(agent_idx)
            if not ring:
                return None
            round_no, frame, _tag = ring[-1]
            return round_no, frame

    def entries(self, agent_idx: int) -> list[tuple[int, Any, str]]:
        """ring 快照 ``[(round_no, frame, tag), …]``（最旧→最新；排障/测试读面）。"""
        with self._lock:
            ring = self._rings.get(agent_idx)
            return list(ring) if ring else []

    @property
    def frames_dir(self) -> Path | None:
        """帧根目录 ``<run_dir>/frames``（``None`` = 内存-only）。"""
        return self._root

    @property
    def recorded_count(self) -> int:
        """``record`` 累计调用次数（审计读面）。"""
        with self._lock:
            return self._recorded_count


__all__ = ["VALID_TAGS", "FrameStore"]
