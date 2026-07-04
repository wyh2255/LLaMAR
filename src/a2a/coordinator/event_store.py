"""EventStore — Worker 任务事件记录器。

记录从任务分发到完成的所有事件（含时间戳），供 Agent 上下文注入使用。
Push callback 写入，ContextManager._render_memory_block() 读取。
"""

from __future__ import annotations

import threading
import time


class EventRecord:
    """单个事件记录。"""

    def __init__(
        self,
        task_id: str,
        event_type: str,
        *,
        state: str | None = None,
        text: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.event_type = event_type  # "task_created", "artifact_update", "status_update"
        self.state = state
        self.text = text
        self.ts = time.time()


class EventStore:
    """带时间戳的任务事件存储。

    线程安全：所有变异和读取操作通过 _lock 保护。
    Push callback 和 Agent loop 运行在不同协程/线程中。
    """

    def __init__(self) -> None:
        self._events: dict[str, list[EventRecord]] = {}
        self._lock = threading.Lock()

    def append(
        self,
        task_id: str,
        event_type: str,
        *,
        state: str | None = None,
        text: str | None = None,
    ) -> None:
        with self._lock:
            self._events.setdefault(task_id, []).append(
                EventRecord(
                    task_id=task_id,
                    event_type=event_type,
                    state=state,
                    text=text,
                )
            )

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

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
                        task_lines.append(f"  {ts_str} CREATED → {r.state or 'PENDING'}")
                    elif r.event_type == "status_update":
                        task_lines.append(f"  {ts_str} STATUS: {r.state or 'UNKNOWN'}")
                    elif r.event_type == "artifact_update":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} ARTIFACT: {text_short}")
                    elif r.event_type == "help_request":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} HELP: {text_short}")
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


# 模块级单例 — push callback 写入，ContextManager 读取
event_store = EventStore()
