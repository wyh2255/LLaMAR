"""P1 审计日志：可恢复的 append-only JSONL journal + 严格递增 sequence allocator。

§1.2-7 / §3.3-3：单一 durable allocator 从 committed tail 分配严格递增 sequence；
resume 不得重置序号；JSONL 断尾只能丢弃未 commit tail 并记录 recovery diagnostic。
artifact 必须先 atomic commit，再 append 成功事件（调用方保证顺序）。

本模块不含 workflow、CLI 或任何 LLM 调用。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sar_orch.eval.contracts import canonical_json

__all__ = ["AuditError", "AuditJournal", "CorruptJournalError", "RecoveryReport"]


class AuditError(Exception):
    """审计日志基异常。"""


class CorruptJournalError(AuditError, ValueError):
    """日志中出现非末尾的坏行 / sequence 违例（严格 append-only 不应出现）。"""


@dataclass(frozen=True)
class RecoveryReport:
    """recovery 诊断：只允许丢弃未 commit 的 torn 尾行。"""

    dropped_lines: int
    dropped_bytes: int
    reason: str
    valid_lines: int
    tail_seq: int


class AuditJournal:
    """append-only JSONL journal，seq 严格 1..N 递增。

    - `append(event)`：分配 next seq 并追加一行（可选 fsync）；重新打开 journal 时
      tail 不重置（resume 语义）。
    - `recover()`：若文件末尾有未终结（torn）JSONL 行，截断丢弃并返回诊断；
      非末尾的坏行或 seq 间隙/重复属于严格违例，抛 `CorruptJournalError`。
    - `read_all()`：recovery 后返回全部已 commit 事件。
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── recovery ────────────────────────────────────────────────────────────
    def recover(self) -> RecoveryReport:
        with self._lock:
            return self._recover_locked()

    def _recover_locked(self) -> RecoveryReport:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return RecoveryReport(0, 0, "", 0, 0)
        data = self.path.read_bytes()
        seq = 0
        valid = 0
        last_complete_end = 0
        idx = 0
        while True:
            nl = data.find(b"\n", idx)
            if nl == -1:
                break
            line = data[idx:nl]
            if line.strip():
                parsed = self._parse_line(line, expected_seq=seq + 1)
                seq = parsed["seq"]
                valid += 1
            last_complete_end = nl + 1
            idx = nl + 1
        torn = data[idx:]
        if not torn:
            return RecoveryReport(0, 0, "", valid, seq)
        dropped_bytes = len(torn)
        fd = os.open(self.path, os.O_RDWR)
        try:
            os.ftruncate(fd, last_complete_end)
            os.fsync(fd)
        finally:
            os.close(fd)
        return RecoveryReport(
            dropped_lines=1,
            dropped_bytes=dropped_bytes,
            reason="torn final JSONL line",
            valid_lines=valid,
            tail_seq=seq,
        )

    def _parse_line(self, line: bytes, expected_seq: int) -> dict[str, Any]:
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptJournalError(
                f"invalid JSONL line near seq {expected_seq}: {line[:80]!r}"
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("seq"), int):
            raise CorruptJournalError(
                f"line missing integer seq near seq {expected_seq}: {line[:80]!r}"
            )
        if event["seq"] != expected_seq:
            raise CorruptJournalError(
                f"sequence violation: got {event['seq']}, expected {expected_seq}"
            )
        return event

    # ── read ────────────────────────────────────────────────────────────────
    def read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._recover_locked()
            if not self.path.exists() or self.path.stat().st_size == 0:
                return []
            events: list[dict[str, Any]] = []
            for raw in self.path.read_text("utf-8").splitlines():
                if not raw.strip():
                    continue
                events.append(json.loads(raw))
            return events

    def tail_seq(self) -> int:
        with self._lock:
            return self._recover_locked().tail_seq

    def event_count(self) -> int:
        with self._lock:
            return self._recover_locked().valid_lines

    # ── append ──────────────────────────────────────────────────────────────
    def append(self, event: dict[str, Any], *, fsync: bool = True) -> int:
        """从 committed tail 分配下一个 seq 并追加；返回分配的 seq。

        跨进程严格序列化由 series lease（AttemptLease）保证；本方法内部再持
        threading.Lock 防止同进程并发重复分配。
        """
        with self._lock:
            report = self._recover_locked()
            seq = report.tail_seq + 1
            payload = dict(event)
            payload.setdefault("schema_version", self.SCHEMA_VERSION)
            payload["seq"] = seq
            line = canonical_json(payload) + "\n"
            with open(self.path, "ab") as fh:
                fh.write(line.encode("utf-8"))
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
            return seq
