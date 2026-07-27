"""EventStore — Worker 任务事件记录器。

记录从任务分发到完成的所有事件（含时间戳），供 Agent 上下文注入使用。
Push callback 写入，ContextManager._render_memory_block() 读取。

v2: 新增 NDJSON 文件持久化 + max_events_per_task 上限。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class EventRecord:
    """单个事件记录。"""

    def __init__(
        self,
        task_id: str,
        event_type: str,
        *,
        context_id: str | None = None,
        state: str | None = None,
        text: str | None = None,
        observation: dict[str, Any] | None = None,
    ) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self.event_type = event_type
        self.state = state
        self.text = text
        self.observation = observation
        self.ts = time.time()


class EventStore:
    """带时间戳的任务事件存储。

    线程安全：所有变异和读取操作通过 _lock 保护。
    当 log_dir 非空时同步写入 NDJSON 文件持久化。

    max_events_per_task: 每个 task_id 最多保留的事件数（默认 500）。
    超出时丢弃最旧记录。
    """

    def __init__(
        self,
        log_dir: str | None = None,
        max_events_per_task: int = 500,
    ) -> None:
        self._events: dict[str, list[EventRecord]] = {}
        self._lock = threading.Lock()
        self._log_dir = log_dir
        self._max_events_per_task = max_events_per_task

    def append(
        self,
        task_id: str,
        event_type: str,
        *,
        context_id: str | None = None,
        state: str | None = None,
        text: str | None = None,
        observation: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            records = self._events.setdefault(task_id, [])
            records.append(
                EventRecord(
                    task_id=task_id,
                    event_type=event_type,
                    context_id=context_id,
                    state=state,
                    text=text,
                    observation=observation,
                )
            )
            if len(records) > self._max_events_per_task:
                excess = len(records) - self._max_events_per_task
                del records[:excess]

        if self._log_dir is not None:
            try:
                os.makedirs(self._log_dir, exist_ok=True)
                path = os.path.join(self._log_dir, f"events_{task_id}.ndjson")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "ts": time.time(),
                                "task_id": task_id,
                                "context_id": context_id,
                                "event_type": event_type,
                                "state": state,
                                "text": (text or "")[:500],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except OSError as e:
                logger.warning("EventStore NDJSON write failed: %s", e)

    def set_log_dir(self, log_dir: str) -> None:
        """Set the NDJSON persistence directory (thread-safe)."""
        self._log_dir = log_dir

    def clear(self, context_id: str | None = None) -> None:
        with self._lock:
            if context_id is None:
                self._events.clear()
            else:
                filtered: dict[str, list[EventRecord]] = {}
                for task_id, records in self._events.items():
                    remaining = [
                        record
                        for record in records
                        if record.context_id != context_id
                    ]
                    if remaining:
                        filtered[task_id] = remaining
                self._events = filtered

    def get_summary(
        self,
        task_ids: set[str] | None = None,
        max_events_per_task: int = 3,
        max_chars: int = 2000,
    ) -> str:
        """生成紧凑的事件摘要，供 Agent 上下文注入。

        Args:
            task_ids: 只显示的 task_id 集合。None 显示全部。
            max_events_per_task: 每个任务最多显示的事件数。
            max_chars: 摘要总字符上限。

        Returns:
            格式化字符串，空事件时返回空字符串。
        """
        with self._lock:
            lines: list[str] = []
            char_count = 0

            for tid in sorted(self._events.keys()):
                if task_ids is not None and tid not in task_ids:
                    continue
                records = self._events[tid]
                if not records:
                    continue

                subset = records[-max_events_per_task:]
                task_lines: list[str] = []
                for r in subset:
                    ts_str = time.strftime("%H:%M:%S", time.localtime(r.ts))
                    if r.event_type == "task_created":
                        task_lines.append(
                            f"  {ts_str} CREATED → {r.state or 'PENDING'}"
                        )
                    elif r.event_type == "status_update":
                        task_lines.append(f"  {ts_str} STATUS: {r.state or 'UNKNOWN'}")
                    elif r.event_type == "artifact_update":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} ARTIFACT: {text_short}")
                    elif r.event_type == "help_request":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} HELP: {text_short}")
                    elif r.event_type == "observation_report":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} OBSERVATION: {text_short}")
                    else:
                        task_lines.append(f"  {ts_str} {r.event_type}: {r.state or ''}")

                if not task_lines:
                    continue

                header = f"- {tid}:"
                remaining = max_chars - char_count
                entry = "\n".join([header] + task_lines) + "\n"
                if remaining <= 0:
                    break
                if len(entry) > remaining:
                    entry = entry[:remaining]
                    lines.append(entry.rstrip())
                    char_count = max_chars
                    break
                lines.append(entry.rstrip())
                char_count += len(entry)

            if not lines:
                return ""
            return "### Worker Events\n" + "\n".join(lines)

    def get_recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            observations = []
            for records in self._events.values():
                for record in records:
                    if record.event_type == "observation_report" and record.observation:
                        observations.append(
                            {
                                "task_id": record.task_id,
                                "ts": record.ts,
                                **record.observation,
                            }
                        )
        observations.sort(key=lambda item: item["ts"])
        return observations[-limit:]

    def get_task_state(
        self,
        dispatch_id: str,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """查询单个任务的结构化状态。

        合并 dispatch_id（派发阶段事件）和 worker_id（push callback 事件）
        下的所有事件，按时间排序推断当前状态与有效文本。

        Returns:
            {
                "task_id": dispatch_id,
                "state": "DISPATCHED|RUNNING|COMPLETED|FAILED|CANCELED|INPUT_REQUIRED|UNKNOWN",
                "text": str,
                "events": list[str],
            }
        """
        with self._lock:
            all_records: list[EventRecord] = []
            for tid in (dispatch_id, worker_id):
                if tid is None:
                    continue
                all_records.extend(self._events.get(tid, []))

        all_records.sort(key=lambda r: r.ts)

        events = [r.event_type for r in all_records]
        state = "UNKNOWN"
        text = ""
        updated_at = 0.0

        # 反向遍历，以最新决定状态的事件为准
        for r in reversed(all_records):
            if r.event_type == "help_request":
                state = "INPUT_REQUIRED"
                text = r.text or ""
                updated_at = r.ts
                break
            if r.event_type == "artifact_update":
                # Artifacts are evidence, not an implicit terminal signal.
                # Keep looking for the canonical status transition below.
                if not text:
                    text = r.text or ""
                    updated_at = r.ts
                continue
            if r.event_type == "status_update" and r.state:
                raw = r.state.upper()
                # A2A protobuf enum names come as "TASK_STATE_XXX"
                upper = raw.removeprefix("TASK_STATE_")
                if upper in ("COMPLETED", "FAILED", "CANCELED"):
                    state = upper
                    text = r.text or ""
                    updated_at = r.ts
                    break
                if upper in ("WORKING", "INPUT_REQUIRED"):
                    state = "RUNNING" if upper == "WORKING" else "INPUT_REQUIRED"
                    text = r.text or ""
                    updated_at = r.ts
                    break
            if r.event_type == "task_created":
                if state == "UNKNOWN":
                    state = "DISPATCHED"
                    updated_at = r.ts

        return {
            "task_id": dispatch_id,
            "state": state,
            "text": text,
            "updated_at": updated_at,
            "events": events,
        }


# 模块级单例 — push callback 写入，ContextManager 读取
event_store = EventStore()
