"""A2ACoordinatorSink — 把控制器的 step 事件翻译为 coordinator 的 A2A 事件。

实现 Agent.controller.EventSink，逻辑原样承接自旧
CoordinatorAgentExecutor._emit_agentic_event：
把 RouterAgent 编排循环里的 llm_response / tool_start / tool_result 事件翻译为
带 metadata（google.protobuf Struct）的 TaskStatusUpdateEvent，并写入 TaskLogger；
collect_results 成功时展开为逐子任务的 task_complete 子事件。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from a2a.server.events import EventQueue
from a2a.types import TaskState, TaskStatusUpdateEvent
from a2a.helpers import new_text_message

logger = logging.getLogger(__name__)


class A2ACoordinatorSink:
    """把 RouterAgent 步进事件推送为 coordinator A2A 事件的 EventSink。"""

    def __init__(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
        task_logger: Any | None,
        store: Any,
    ):
        self._event_queue = event_queue
        self._task_id = task_id
        self._context_id = context_id
        self._task_logger = task_logger
        self._store = store

    async def emit(self, event_type: str, /, **kw: Any) -> None:
        """将 Agent step_callback 事件翻译为 EventQueue 事件 + TaskLogger 记录。"""
        task_id = self._task_id
        context_id = self._context_id
        store = self._store

        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
        )
        event.status.state = TaskState.TASK_STATE_WORKING
        msg_text = ""
        log_data: dict[str, Any] = {}

        if event_type == "llm_response":
            content = kw.get("content", "")
            tool_calls = kw.get("tool_calls")
            tool_names = [tc.function.name for tc in tool_calls] if tool_calls else []
            event.metadata.update(
                {
                    "event_type": "llm_thinking",
                    "detail": (content or "")[:500],
                    "tool_calls": tool_names,
                }
            )
            msg_text = f"[Thinking] {(content or '')[:200]}..."
            log_data = {"content": (content or "")[:2000], "tool_calls": tool_names}

        elif event_type == "tool_start":
            tool_name = kw.get("tool_name", "")
            arguments = kw.get("arguments", {})

            if tool_name == "send_message":
                msg_type = arguments.get("message_type", "unknown")
                if msg_type == "assign_task":
                    tid = arguments.get("related_task_id", "")
                    wid = arguments.get("who", "")
                    event.metadata.update(
                        {
                            "event_type": "dispatch",
                            "task_id": tid,
                            "worker_id": wid,
                            "state": "running",
                        }
                    )
                    msg_text = f"[{tid}] Dispatching → {wid}"
                    log_data = {
                        "tool_name": tool_name,
                        "task_id": tid,
                        "worker_id": wid,
                        "message_type": msg_type,
                    }
                elif msg_type == "reply_to_help":
                    tid = arguments.get("related_task_id", "")
                    event.metadata.update(
                        {
                            "event_type": "reply_to_help",
                            "task_id": tid,
                        }
                    )
                    msg_text = f"[{tid}] Replying to help request"
                    log_data = {
                        "tool_name": tool_name,
                        "task_id": tid,
                        "message_type": msg_type,
                    }
                elif msg_type == "cancel_task":
                    tid = arguments.get("related_task_id", "")
                    event.metadata.update(
                        {
                            "event_type": "cancel",
                            "task_id": tid,
                        }
                    )
                    msg_text = f"[{tid}] Cancelling task"
                    log_data = {
                        "tool_name": tool_name,
                        "task_id": tid,
                        "message_type": msg_type,
                    }
                else:
                    event.metadata.update(
                        {
                            "event_type": "tool_call",
                            "tool_name": tool_name,
                            "message_type": msg_type,
                        }
                    )
                    msg_text = f"Tool {tool_name}: {msg_type}"
                    log_data = {"tool_name": tool_name, "message_type": msg_type}

            elif tool_name == "query_task_events":
                tids = arguments.get("task_ids", [])
                event.metadata.update(
                    {
                        "event_type": "query_task_events",
                        "task_ids": tids,
                    }
                )
                msg_text = f"Querying events: {tids}"
                log_data = {"tool_name": tool_name, "task_ids": tids}

            elif tool_name == "verify_result":
                tid = arguments.get("task_id", "")
                event.metadata.update(
                    {
                        "event_type": "verify",
                        "task_id": tid,
                        "state": "running",
                    }
                )
                msg_text = f"[{tid}] Verifying"
                log_data = {"tool_name": tool_name, "task_id": tid}

            elif tool_name == "update_plan":
                event.metadata.update(
                    {
                        "event_type": "replan",
                        "detail": "Plan updated",
                    }
                )
                msg_text = "Plan updated"
                log_data = {"tool_name": tool_name, "arguments": str(arguments)[:1000]}

            else:
                event.metadata.update(
                    {
                        "event_type": "tool_call",
                        "tool_name": tool_name,
                    }
                )
                msg_text = f"Tool: {tool_name}"
                log_data = {"tool_name": tool_name}

        elif event_type == "tool_result":
            tool_name = kw.get("tool_name", "")
            success = kw.get("success", False)
            content = kw.get("content", "")

            if tool_name == "query_task_events" and success:
                try:
                    results = json.loads(content)
                    for r in results:
                        tid = r.get("task_id", "")
                        state_name = r.get("state", "UNKNOWN").lower()
                        if state_name == "completed":
                            state = "done"
                        elif state_name == "input_required":
                            state = "input_required"
                        elif state_name in ("failed", "canceled"):
                            state = "failed"
                        else:
                            state = "running"
                        detail = (r.get("text") or "")[:200]
                        sub_event = TaskStatusUpdateEvent(
                            task_id=task_id,
                            context_id=context_id,
                        )
                        sub_event.status.state = TaskState.TASK_STATE_WORKING
                        label = {
                            "done": "Done",
                            "failed": "Failed",
                            "input_required": "Help",
                            "running": "Running",
                        }.get(state, state.upper())
                        sub_event.status.message.CopyFrom(
                            new_text_message(f"[{tid}] {label}: {detail}...")
                        )
                        sub_event.metadata.update(
                            {
                                "event_type": "task_complete"
                                if state in ("done", "failed")
                                else "help_request"
                                if state == "input_required"
                                else "task_status",
                                "task_id": tid,
                                "state": state,
                                "detail": detail,
                            }
                        )
                        await self._event_queue.enqueue_event(sub_event)
                        if self._task_logger is not None:
                            self._task_logger.log_event(
                                task_id,
                                "task_complete"
                                if state in ("done", "failed")
                                else "help_request"
                                if state == "input_required"
                                else "task_status",
                                {
                                    "subtask_id": tid,
                                    "state": state,
                                    "detail": detail,
                                },
                                source="router",
                            )
                except (json.JSONDecodeError, TypeError):
                    pass
                if self._task_logger is not None:
                    self._task_logger.log_event(
                        task_id,
                        "tool_result",
                        {
                            "tool_name": tool_name,
                            "success": success,
                            "content": content[:2000],
                        },
                        source="router",
                    )
                return

            elif tool_name == "verify_result" and success:
                try:
                    report = json.loads(content)
                    tid = report.get("task_id", "")
                    passed = report.get("passed", False)
                    summary = report.get("summary", "")
                    event.metadata.update(
                        {
                            "event_type": "verify",
                            "task_id": tid,
                            "passed": passed,
                            "summary": summary[:200],
                            "state": "verified" if passed else "failed",
                        }
                    )
                    msg_text = (
                        f"[{tid}] {'PASS' if passed else 'FAIL'} — {summary[:100]}"
                    )
                    log_data = {
                        "tool_name": tool_name,
                        "task_id": tid,
                        "passed": passed,
                        "summary": summary[:500],
                    }
                except (json.JSONDecodeError, TypeError):
                    event.metadata.update(
                        {
                            "event_type": "verify",
                            "detail": content[:200],
                        }
                    )
                    msg_text = f"Verify result: {content[:100]}"

            elif tool_name == "send_message":
                event.metadata.update(
                    {
                        "event_type": "dispatch",
                        "detail": content[:200],
                    }
                )
                msg_text = content[:150]
                log_data = {"tool_name": tool_name, "detail": content[:500]}

            elif tool_name == "update_plan":
                event.metadata.update(
                    {
                        "event_type": "replan",
                        "detail": content[:200],
                    }
                )
                msg_text = content[:150]
                log_data = {"tool_name": tool_name, "detail": content[:500]}

            elif tool_name == "query_sar_state":
                event.metadata.update(
                    {
                        "event_type": "tool_call",
                        "tool_name": tool_name,
                        "success": success,
                    }
                )
                msg_text = f"Tool {tool_name}: {'OK' if success else 'FAIL'}"
                context = content[:2000]
                log_data = {
                    "tool_name": tool_name,
                    "success": success,
                    "context": context,
                }

            else:
                event.metadata.update(
                    {
                        "event_type": "tool_call",
                        "tool_name": tool_name,
                        "success": success,
                    }
                )
                msg_text = f"Tool {tool_name}: {'OK' if success else 'FAIL'}"
                log_data = {"tool_name": tool_name, "success": success}

        event.metadata.update({"progress": store.progress})
        event.status.message.CopyFrom(new_text_message(msg_text or event_type))
        await self._event_queue.enqueue_event(event)

        if self._task_logger is not None:
            self._task_logger.log_event(task_id, event_type, log_data, source="router")
