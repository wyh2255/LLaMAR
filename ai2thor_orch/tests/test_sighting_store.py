"""SightingStore 单测（P2 空间记忆；设计 §4.3 冻结 schema 逐条）。

覆盖：
- ndjson append-only 落盘（惰性建父目录；字段集 = RECORD_FIELDS）；
- 去重键 ``(alias, agent)``：保留最新 sighting + 首见 step；
- 读面 ``latest_sightings()``：最新优先（newest-first）+ 返回副本；
- 内存-only 模式（无 run_dir 不落盘）；坐标非数值归一 ``None``；
- 并发写（barrier 回合线程 vs 读面线程）单锁串行化。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ai2thor_orch.memory.sighting_store import RECORD_FIELDS, SightingStore


def _read_lines(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
    ]


class TestRecordAndNdjson:
    def test_record_appends_frozen_schema_line(self, tmp_path):
        run_dir = tmp_path / "run_1"  # 父目录尚不存在 → 惰性创建
        store = SightingStore(run_dir=run_dir)
        store.record(
            step=3,
            agent="Alice",
            alias="Bread_1",
            object_type="Bread",
            x=-1.5,
            z=2.25,
        )

        path = run_dir / "sightings.ndjson"
        assert path.exists()
        records = _read_lines(path)
        assert len(records) == 1
        rec = records[0]
        assert set(rec) == set(RECORD_FIELDS)
        assert rec == {
            "step": 3,
            "agent": "Alice",
            "alias": "Bread_1",
            "object_type": "Bread",
            "x": -1.5,
            "z": 2.25,
            "visible_now": True,
        }
        assert store.path == path

    def test_append_only_stream_grows_per_record(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        store.record(
            step=1, agent="Alice", alias="Mug_1", object_type="Mug", x=0.0, z=0.0
        )
        store.record(
            step=2, agent="Alice", alias="Mug_1", object_type="Mug", x=1.0, z=1.0
        )
        # 去重视图 1 条，ndjson 仍是逐条追加的 2 行（审计流 ≠ 去重视图）。
        assert store.count == 1
        assert len(_read_lines(tmp_path / "sightings.ndjson")) == 2

    def test_non_numeric_coordinates_become_none(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        entry = store.record(
            step=1, agent="Alice", alias="Mug_1", object_type="Mug", x="oops", z=None
        )
        assert entry["x"] is None and entry["z"] is None
        assert _read_lines(tmp_path / "sightings.ndjson")[0]["x"] is None


class TestDedupeView:
    def test_dedupe_key_alias_agent_keeps_latest_plus_first_seen(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        store.record(
            step=1, agent="Alice", alias="Bread_1", object_type="Bread", x=0.0, z=0.0
        )
        entry = store.record(
            step=5, agent="Alice", alias="Bread_1", object_type="Bread", x=2.0, z=3.0
        )
        assert store.count == 1
        assert entry["step"] == 5           # 最新 sighting
        assert (entry["x"], entry["z"]) == (2.0, 3.0)
        assert entry["first_seen_step"] == 1  # 首见 step 保留
        assert store.latest_sightings() == [entry]

    def test_same_alias_different_agent_are_separate_entries(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        store.record(
            step=1, agent="Alice", alias="Mug_1", object_type="Mug", x=0.0, z=0.0
        )
        store.record(
            step=1, agent="Bob", alias="Mug_1", object_type="Mug", x=0.0, z=0.0
        )
        assert store.count == 2
        assert {e["agent"] for e in store.latest_sightings()} == {"Alice", "Bob"}

    def test_latest_sightings_newest_first_with_stable_tiebreak(self):
        store = SightingStore()  # 内存-only
        store.record(
            step=1, agent="Alice", alias="A_1", object_type="A", x=0.0, z=0.0
        )
        store.record(
            step=3, agent="Alice", alias="C_1", object_type="C", x=0.0, z=0.0
        )
        store.record(
            step=2, agent="Bob", alias="B_1", object_type="B", x=0.0, z=0.0
        )
        # 同一 step 的并列：按 (agent, alias) 降序确定（Bob > Alice）。
        store.record(
            step=4, agent="Alice", alias="Z_1", object_type="Z", x=0.0, z=0.0
        )
        store.record(
            step=4, agent="Bob", alias="Y_1", object_type="Y", x=0.0, z=0.0
        )
        ordered = store.latest_sightings()
        assert [(e["step"], e["agent"], e["alias"]) for e in ordered] == [
            (4, "Bob", "Y_1"),
            (4, "Alice", "Z_1"),
            (3, "Alice", "C_1"),
            (2, "Bob", "B_1"),
            (1, "Alice", "A_1"),
        ]

    def test_latest_sightings_returns_copies(self):
        store = SightingStore()
        store.record(
            step=1, agent="Alice", alias="A_1", object_type="A", x=1.0, z=2.0
        )
        out = store.latest_sightings()
        out[0]["x"] = 999.0
        out[0]["alias"] = "HACKED"
        assert store.latest_sightings()[0]["x"] == 1.0
        assert store.latest_sightings()[0]["alias"] == "A_1"


class TestMemoryOnlyAndConcurrency:
    def test_memory_only_without_run_dir(self):
        store = SightingStore(run_dir=None)
        assert store.path is None
        store.record(
            step=1, agent="Alice", alias="Mug_1", object_type="Mug", x=0.0, z=0.0
        )
        assert store.count == 1
        assert store.latest_sightings()[0]["alias"] == "Mug_1"

    def test_concurrent_record_and_read(self, tmp_path):
        store = SightingStore(run_dir=tmp_path)
        workers = 4
        per_worker = 25

        def _write(worker_idx: int) -> None:
            for i in range(per_worker):
                store.record(
                    step=i + 1,
                    agent=f"Agent{worker_idx}",
                    alias=f"Obj{worker_idx}_{i}",
                    object_type="Obj",
                    x=float(i),
                    z=0.0,
                )
                store.latest_sightings()  # 并发读面（coordinator 线程语义）

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_write, range(workers)))

        expected = workers * per_worker
        assert store.count == expected
        lines = _read_lines(tmp_path / "sightings.ndjson")
        assert len(lines) == expected
        assert len(store.latest_sightings()) == expected
