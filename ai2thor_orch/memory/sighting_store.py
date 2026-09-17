"""SightingStore —— coordinator 空间记忆（P2 观测通道；A 案自动 ingest）。

设计基线（``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md``
§4.3，D3 = A 案）逐条落地：

- **append-only**：内存去重视图 + ``<run_dir>/sightings.ndjson`` 逐条追加
  （每次 ``record`` 追加一行原始 sighting；文件是审计流，不是去重视图）；
- **记录字段**（冻结 schema）：``{step, agent, alias, object_type, x, z,
  visible_now}``；
- **去重键** ``(alias, agent)``：内存视图保留**最新** sighting（step / 位置 /
  类型）+ 该键的 ``first_seen_step``（首见 step）；
- **读面** :meth:`SightingStore.latest_sightings`：去重视图，**最新优先**
  （newest-first），供 coordinator Environment State 的 ``### Sightings`` 段
  按预算（最新 K 条）截断渲染（渲染层预算见
  ``ai2thor_orch.state.context``）。

真值边界（与设计 §4.2 A 案论证绑定）：本 store 只接收 **visible 过滤后的
感知面**对象——写点 ``AI2ThorBarrier._execute_round`` 先滤 ``visible`` 再入账，
**永不 ingest 全屋 metadata**（缺 key 视为可见，与 ``snapshot_public`` /
``snapshot_coordinator`` 同过滤口径）；alias 经共享 ``AliasRegistry`` 归一，
raw objectId 不入账。

线程安全：``record`` 由 barrier 回合执行线程调用，``latest_sightings`` 由
coordinator 线程调用——单锁串行化。ndjson 追加失败**不阻塞**回合（内存视图
不受影响；失败响亮记一次日志，不刷屏）——与 store 的「审计流」定位一致。

不复用 SAR ``SemanticMapStore``（设计 §4.3 已定）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ndjson 单条记录的字段集（设计 §4.3 冻结 schema；去重读面另带
#: ``first_seen_step``，它是读面属性而非每次观测的原始事实）。
RECORD_FIELDS: tuple[str, ...] = (
    "step",
    "agent",
    "alias",
    "object_type",
    "x",
    "z",
    "visible_now",
)


def _coord_or_none(value: Any) -> float | None:
    """坐标值归一（非数值 → ``None``；bool 不是坐标）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class SightingStore:
    """Per-(alias, agent) sighting ledger（内存去重视图 + ndjson 追加流）。

    Args:
        run_dir: run 目录；``sightings.ndjson`` 落在其下（父目录首次写入时
            惰性创建——装配期 barrier 先于 run 目录建立）。``None`` =
            内存-only（单测 / 无落盘的嵌入用法），不写文件。
    """

    def __init__(self, run_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        #: (alias, agent) → 去重视图条目（最新 sighting + first_seen_step）。
        self._latest: dict[tuple[str, str], dict[str, Any]] = {}
        self._path: Path | None = (
            Path(run_dir) / "sightings.ndjson" if run_dir is not None else None
        )
        self._parent_ready: bool = False
        #: ndjson 写入失败的一次性日志闩（磁盘问题不刷屏、不阻塞回合）。
        self._write_failed: bool = False

    # ── 写面（barrier 回合末尾调用）──────────────────────────────────────

    def record(
        self,
        *,
        step: int,
        agent: str,
        alias: str,
        object_type: str,
        x: Any = None,
        z: Any = None,
        visible_now: bool = True,
    ) -> dict[str, Any]:
        """记一条 sighting：ndjson 追加 + 去重视图更新（返回读面条目副本）。

        去重键 ``(alias, agent)``：条目更新为本次观测（step / 位置 / 类型 /
        visible_now），``first_seen_step`` 保留该键首见的 step。
        """
        rec: dict[str, Any] = {
            "step": int(step),
            "agent": str(agent),
            "alias": str(alias),
            "object_type": str(object_type),
            "x": _coord_or_none(x),
            "z": _coord_or_none(z),
            "visible_now": bool(visible_now),
        }
        key = (rec["alias"], rec["agent"])
        with self._lock:
            prev = self._latest.get(key)
            entry = {
                **rec,
                "first_seen_step": prev["first_seen_step"] if prev else rec["step"],
            }
            self._latest[key] = entry
            self._append_ndjson(rec)
        return dict(entry)

    def _append_ndjson(self, rec: dict[str, Any]) -> None:
        """追加一行原始 sighting（caller 持锁）。

        失败（磁盘 / 权限）只记录一次：审计流可缺、回合不可断。
        """
        if self._path is None:
            return
        try:
            if not self._parent_ready:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._parent_ready = True
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            if not self._write_failed:
                self._write_failed = True
                logger.exception(
                    "sightings.ndjson 追加失败（内存视图不受影响；后续同类失败不再刷屏）"
                )

    # ── 读面（coordinator state provider 调用）─────────────────────────

    def latest_sightings(self) -> list[dict[str, Any]]:
        """去重视图（每 ``(alias, agent)`` 一条），**最新优先**（newest-first）。

        排序键 ``(step, agent, alias)`` 降序——同一 step 内顺序确定，预算截断
        （渲染层取前 K 条）语义 = 「最新 K 条」。返回副本，调用方不可变内部
        状态。
        """
        with self._lock:
            entries = sorted(
                self._latest.values(),
                key=lambda e: (e["step"], e["agent"], e["alias"]),
                reverse=True,
            )
        return [dict(e) for e in entries]

    @property
    def count(self) -> int:
        """去重视图条目数（``(alias, agent)`` 键数）。"""
        with self._lock:
            return len(self._latest)

    @property
    def path(self) -> Path | None:
        """ndjson 落盘路径（``None`` = 内存-only）。"""
        return self._path


__all__ = ["RECORD_FIELDS", "SightingStore"]
