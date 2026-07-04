"""A2AWorkerSink — 把控制器的 step 事件翻译为 A2A EventQueue 事件。

实现 Agent.controller.EventSink，逻辑原样承接自旧 AgentAdapter._on_step_event：
把 llm_response / tool_start / tool_result 三类事件构造成 TaskStatusUpdateEvent，
文本体附带 [DATA] JSON 块供上游（coordinator._log_worker_events）解析。

Agent 运行日志由 AgentLogger 通过 NDJSON 格式持久化到 {task_id}.ndjson。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from a2a.server.events import EventQueue
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.helpers import new_text_message

logger = logging.getLogger(__name__)


class A2AWorkerSink:
    """把 Mini-Agent 步进事件推送为 A2A worker 事件的 EventSink。"""

    def __init__(self, event_queue: EventQueue, task_id: str, context_id: str):
        self._event_queue = event_queue
        self._task_id = task_id
        self._context_id = context_id

    async def _enqueue_working(self, text: str) -> None:
        status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
        status.message.CopyFrom(new_text_message(text))
        await self._event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=self._task_id,
                context_id=self._context_id,
                status=status,
            )
        )

    async def emit(self, type_: str, /, **data: Any) -> None:
        if type_ == "llm_response":
            content = data.get("content")
            if not content:
                return
            display = content[:2000] + ("..." if len(content) > 2000 else "")
            text = f"[LLM] {display}"
            data_json = json.dumps(
                {
                    "ev": "llm_response",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "content": content[:2000],
                },
                ensure_ascii=False,
            )
            text += f"\n[DATA]\n{data_json}"
            await self._enqueue_working(text)

        elif type_ == "tool_start":
            tool_name = data.get("tool_name", "")
            tool_args = data.get("arguments", {})
            args_str = json.dumps(tool_args, ensure_ascii=False) if tool_args else "{}"
            args_preview = args_str[:100] + ("..." if len(args_str) > 100 else "")
            text = f"[Tool] {tool_name}: {args_preview}"
            data_json = json.dumps(
                {
                    "ev": "tool_start",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool_name": tool_name,
                    "arguments": args_str[:1000],
                },
                ensure_ascii=False,
            )
            text += f"\n[DATA]\n{data_json}"
            await self._enqueue_working(text)

        elif type_ == "tool_result":
            tool_name = data.get("tool_name", "")
            success = data.get("success", False)
            content = data.get("content", "")
            label = "[Result]" if success else "[Error]"
            truncated = content[:197] + "..." if len(content) > 200 else content
            text = f"{label} {tool_name}: {truncated}"
            data_json = json.dumps(
                {
                    "ev": "tool_result",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool_name": tool_name,
                    "success": success,
                    "content": (content or "")[:3000],
                },
                ensure_ascii=False,
            )
            text += f"\n[DATA]\n{data_json}"
            await self._enqueue_working(text)

        else:
            logger.debug("Unhandled step event type: %s", type_)
