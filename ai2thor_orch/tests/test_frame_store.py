"""FrameStore 单测（F-frame；设计 2026-09-17 §1.4 帧存储契约逐条）。

覆盖：ring 语义（latest N，缺省 1）/ ``latest()`` 读面形状 / 落盘路径契约
``<run_dir>/frames/<AgentName>/round_<N>_<tag>.png`` / RGB→BGR 编码正确性 /
tag 白名单 fail-fast / 参数校验 / 内存-only 与写盘失败的 best-effort 语义。
"""

from __future__ import annotations

import numpy as np
import pytest

from ai2thor_orch.frames import VALID_TAGS, FrameStore

pytestmark = pytest.mark.unit


def _frame(value: int = 1, shape: tuple[int, int, int] = (2, 2, 3)) -> np.ndarray:
    return np.full(shape, value, dtype=np.uint8)


class TestRingSemantics:
    """内存 ring：每 agent 保留 latest N 帧（缺省 N=1），读面始终最新。"""

    def test_latest_default_ring_size_one(self) -> None:
        store = FrameStore()
        assert store.latest(0) is None  # 无帧 → None

        store.record(0, 0, _frame(1), "init")
        store.record(0, 1, _frame(2), "navigate_ok")
        round_no, frame = store.latest(0)
        assert round_no == 1
        assert int(frame[0, 0, 0]) == 2  # 最新一帧（N=1 顶掉旧帧）
        assert len(store.entries(0)) == 1

    def test_ring_size_two_keeps_latest_two(self) -> None:
        store = FrameStore(ring_size=2)
        store.record(0, 1, _frame(1), "navigate_ok")
        store.record(0, 2, _frame(2), "open_ok")
        store.record(0, 3, _frame(3), "done")

        entries = store.entries(0)
        assert [(r, int(f[0, 0, 0]), tag) for r, f, tag in entries] == [
            (2, 2, "open_ok"),
            (3, 3, "done"),
        ]
        assert store.latest(0)[0] == 3

    def test_per_agent_rings_isolated(self) -> None:
        store = FrameStore()
        store.record(0, 1, _frame(10), "pickup_ok")
        store.record(1, 1, _frame(20), "put_ok")

        assert int(store.latest(0)[1][0, 0, 0]) == 10
        assert int(store.latest(1)[1][0, 0, 0]) == 20
        assert store.latest(2) is None  # 无记录的槽位 → None

    def test_recorded_count(self) -> None:
        store = FrameStore()
        assert store.recorded_count == 0
        store.record(0, 1, _frame(), "init")
        store.record(1, 1, _frame(), "init")
        assert store.recorded_count == 2


class TestDiskContract:
    """落盘路径契约与编码（设计 §1.4：``<run_dir>/frames/<AgentName>/round_<N>_<tag>.png``）。"""

    def test_path_contract_with_agent_names(self, tmp_path) -> None:
        store = FrameStore(tmp_path, agent_names=["Alice", "Bob"])
        path = store.record(0, 0, _frame(7), "init")

        assert path == tmp_path / "frames" / "Alice" / "round_0_init.png"
        assert path.exists()
        assert store.frames_dir == tmp_path / "frames"

        bob = store.record(1, 12, _frame(7), "navigate_ok")
        assert bob == tmp_path / "frames" / "Bob" / "round_12_navigate_ok.png"

    def test_path_falls_back_to_slot_name(self, tmp_path) -> None:
        store = FrameStore(tmp_path)  # 无 agent_names → Agent{idx}
        path = store.record(1, 3, _frame(), "final")
        assert path == tmp_path / "frames" / "Agent1" / "round_3_final.png"

    def test_rgb_channels_preserved(self, tmp_path) -> None:
        """record 收 RGB → 落盘转 BGR（cv2 约定）；读回即原色（不做通道检查
        会红蓝互换——原版 cv2img 已是 BGR，这里明确以 event.frame 的 RGB 为准）。"""
        import cv2

        store = FrameStore(tmp_path, agent_names=["Alice"])
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        frame[0, 0] = (255, 0, 0)  # 红（RGB）
        frame[1, 1] = (0, 128, 255)  # 橙-蓝混合样本
        path = store.record(0, 7, frame, "pickup_ok")

        back = cv2.imread(str(path))
        assert back is not None
        assert back.shape == (3, 4, 3)
        assert np.array_equal(back[:, :, ::-1], frame)

    def test_all_frozen_tags_writable(self, tmp_path) -> None:
        store = FrameStore(tmp_path, agent_names=["Alice"])
        for tag in sorted(VALID_TAGS):
            store.record(0, 1, _frame(), tag)
        names = sorted(p.name for p in (tmp_path / "frames" / "Alice").iterdir())
        assert names == sorted(f"round_1_{tag}.png" for tag in VALID_TAGS)

    def test_unknown_tag_fails_fast(self) -> None:
        store = FrameStore()
        with pytest.raises(ValueError, match="tag"):
            store.record(0, 1, _frame(), "pickup")  # 笔误不进文件名

    def test_memory_only_store_writes_nothing(self, tmp_path) -> None:
        store = FrameStore()  # run_dir=None
        assert store.record(0, 1, _frame(), "init") is None
        assert store.frames_dir is None
        assert not (tmp_path / "frames").exists()

    def test_disk_failure_is_best_effort(self, tmp_path) -> None:
        """磁盘故障（frames 被文件占据）不抛、不阻塞——ring 读面不受影响。"""
        (tmp_path / "frames").write_text("occupied", encoding="utf-8")
        store = FrameStore(tmp_path)
        assert store.record(0, 1, _frame(5), "init") is None  # 写盘失败 → None
        latest = store.latest(0)
        assert latest is not None and latest[0] == 1  # 内存 ring 照常
        assert int(latest[1][0, 0, 0]) == 5


class TestArgumentValidation:
    """参数校验：编码缺陷响亮失败（不静默落垃圾文件名）。"""

    def test_none_frame_rejected(self) -> None:
        with pytest.raises(ValueError, match="frame_rgb"):
            FrameStore().record(0, 1, None, "init")

    def test_bad_agent_idx_rejected(self) -> None:
        store = FrameStore()
        with pytest.raises(ValueError, match="agent_idx"):
            store.record(-1, 1, _frame(), "init")
        with pytest.raises(ValueError, match="agent_idx"):
            store.record(True, 1, _frame(), "init")  # type: ignore[arg-type]

    def test_bad_round_no_rejected(self) -> None:
        store = FrameStore()
        with pytest.raises(ValueError, match="round_no"):
            store.record(0, -1, _frame(), "init")

    def test_ring_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="ring_size"):
            FrameStore(ring_size=0)
