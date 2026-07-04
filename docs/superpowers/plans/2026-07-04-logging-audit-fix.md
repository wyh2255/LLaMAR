# 日志系统修补实施计划

## 文件结构总表

| 文件 | 操作 | 责任 |
|------|------|------|
| `src/Agent/worker_agent/agent.py` | 修改 | Task 1, 7, 8 — NeedInputError 时回调 tool_result；llm_response 回调携带 input_messages；step 递增处发 step_boundary |
| `src/a2a/worker/sink.py` | 修改 | Task 2 — 增加可选 `log_dir` 参数，同步写入 NDJSON |
| `sar_orch/worker.py` | 修改 | Task 1, 2, 7 — 扩展 `_step_callback`；创建 sink 时传入 log_dir |
| `src/a2a/coordinator/agent_executor.py` | 修改 | Task 3 — `execute()` 入口记 TaskLogger event |
| `src/a2a/worker/agent_adapter.py` | 修改 | Task 1, 3 — log pause/resume；entry 记 Python logger |
| `sar_orch/coordinator.py` | 修改 | Task 4, 6 — `_router_cb` 扩展 |
| `sar_orch/logger.py` | 修改 | Task 4 — 新增 `log_coordinator_state()` 方法 |
| `src/a2a/coordinator/event_store.py` | 修改 | Task 5 — NDJSON 持久化 + `max_events_per_task` 上限 |
| `src/a2a/coordinator/task_logger.py` | 修改 | Task 3 — 新增 `log_raw_request()` 方法 |
| `src/Agent/worker_agent/schema/schema.py` | 不变 | 不修改 |
| `src/Agent/controller/sink.py` | 不变 | CallbackSink/TeeSink 已兼容，不改 |
| `src/Agent/worker_agent/hooks.py` | 不变 | |
| `src/Agent/controller/controller.py` | 不变 | |

---

## Task 1: ask_coordinator 暂停/恢复日志化

### Step 1.1 — 修改 `agent.py`：NeedInputError 时调用 tool_result 回调

**文件**: `src/Agent/worker_agent/agent.py`

**目标**: `AskCoordinatorTool.execute()` 抛出 `NeedInputError`，`Agent.run()` 在 line 683 捕获后直接 `return RunResult`——不对 `ask_coordinator` 的 tool 名和参数发给 step_callback。需要在返回前调用 `step_callback("tool_result", ...)`。

**代码变更**:

`oldString` (line 681-688):
```python
                    except NeedInputError as e:
                        return RunResult(
                            content=e.question,
                            success=False,
                            need_input=True,
                        )
```

`newString`:
```python
                    except NeedInputError as e:
                        # 将 ask_coordinator 的 tool_result 发给 step_callback
                        if step_callback is not None:
                            try:
                                await step_callback(
                                    "tool_result",
                                    tool_name=function_name,
                                    success=True,
                                    content=e.question,
                                )
                            except Exception:
                                logger.exception(
                                    "step_callback(tool_result for NeedInputError) failed"
                                )
                        return RunResult(
                            content=e.question,
                            success=False,
                            need_input=True,
                        )
```

**验证**: `uv run --with ruff ruff check src/Agent/worker_agent/agent.py`

---

### Step 1.2 — 修改 `agent_adapter.py`：暂停事件记 Python logger，恢复事件也记

**文件**: `src/a2a/worker/agent_adapter.py`

**目标**: `execute()` 中 `requires_input()` 调用前记结构化日志；从 snapshot 恢复时标记。

**代码变更 1** (line 168-171, `requires_input` 前):

`oldString`:
```python
            if result.need_input:
                await updater.requires_input(message=new_text_message(result.content))
                return
```

`newString`:
```python
            if result.need_input:
                logger.info(
                    "[PAUSE] task=%s context=%s question=%s",
                    task_id, context_id, result.content[:200],
                )
                await updater.requires_input(message=new_text_message(result.content))
                return
```

**代码变更 2** (line 144-151, snapshot resume 前加日志):

`oldString`:
```python
        if snapshot:
            await updater.start_work(message=new_text_message("Resuming after help"))
            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id, query, sink,
                task_id=task_id,
                initial_messages=snapshot,
            )
```

`newString`:
```python
        if snapshot:
            logger.info(
                "[RESUME] task=%s context=%s snapshot_length=%d",
                task_id, context_id, len(snapshot),
            )
            await updater.start_work(message=new_text_message("Resuming after help"))
            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id, query, sink,
                task_id=task_id,
                initial_messages=snapshot,
            )
```

**验证**: `uv run --with ruff ruff check src/a2a/worker/agent_adapter.py`

---

## Task 2: Worker A2AWorkerSink 本地 NDJSON 持久化

### Step 2.1 — 修改 `sink.py`：新增 `log_dir` 参数 + NDJSON 写入

**文件**: `src/a2a/worker/sink.py`

**目标**: `__init__` 增加 `log_dir: str | None = None` 参数；每次 `emit()` 在写 EventQueue 的同时同步写 NDJSON 行到 `{log_dir}/{task_id}.ndjson`。

**完整文件替换** (98 行 → ~130 行):

```python
"""A2AWorkerSink — 把控制器的 step 事件翻译为 A2A EventQueue 事件。

实现 Agent.controller.EventSink，逻辑原样承接自旧 AgentAdapter._on_step_event：
把 llm_response / tool_start / tool_result 三类事件构造成 TaskStatusUpdateEvent，
文本体附带 [DATA] JSON 块供上游（coordinator._log_worker_events）解析。

v2: 新增 log_dir 参数，同步写入本地 NDJSON 文件实现持久化。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2a.server.events import EventQueue
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.helpers import new_text_message

logger = logging.getLogger(__name__)


class A2AWorkerSink:
    """把 Mini-Agent 步进事件推送为 A2A worker 事件的 EventSink。

    当 log_dir 非空时，同步写入 NDJSON 文件 {log_dir}/{task_id}.ndjson。
    文件操作异常不传播——只记警告。
    """

    def __init__(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
        log_dir: str | None = None,
    ):
        self._event_queue = event_queue
        self._task_id = task_id
        self._context_id = context_id
        self._log_dir = log_dir
        self._ndjson_fh = None
        if log_dir is not None:
            try:
                os.makedirs(log_dir, exist_ok=True)
                path = os.path.join(log_dir, f"{task_id}.ndjson")
                self._ndjson_fh = open(path, "a", encoding="utf-8")
            except OSError as e:
                logger.warning("A2AWorkerSink: cannot open NDJSON file: %s", e)
                self._ndjson_fh = None

    def _write_ndjson(self, data: dict) -> None:
        if self._ndjson_fh is not None:
            try:
                self._ndjson_fh.write(json.dumps(data, ensure_ascii=False) + "\n")
                self._ndjson_fh.flush()
            except OSError as e:
                logger.warning("A2AWorkerSink NDJSON write failed: %s", e)

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
        ndjson_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "context_id": self._context_id,
            "event": type_,
        }

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
            ndjson_entry["content"] = content[:2000]
            if data.get("tool_calls"):
                ndjson_entry["tool_calls"] = [
                    tc.function.name for tc in data["tool_calls"]
                ]
            if data.get("usage"):
                ndjson_entry["usage"] = {
                    "prompt_tokens": data["usage"].prompt_tokens,
                    "completion_tokens": data["usage"].completion_tokens,
                    "total_tokens": data["usage"].total_tokens,
                }
            self._write_ndjson(ndjson_entry)
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
            ndjson_entry["tool_name"] = tool_name
            ndjson_entry["arguments"] = tool_args
            self._write_ndjson(ndjson_entry)
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
            ndjson_entry["tool_name"] = tool_name
            ndjson_entry["success"] = success
            ndjson_entry["content"] = (content or "")[:3000]
            self._write_ndjson(ndjson_entry)
            await self._enqueue_working(text)

        else:
            ndjson_entry["data"] = {k: str(v) for k, v in data.items()}
            self._write_ndjson(ndjson_entry)
            logger.debug("Unhandled step event type: %s", type_)
```

**验证**: `uv run --with ruff ruff check src/a2a/worker/sink.py`

---

### Step 2.2 — 修改 `worker.py`：创建 sink 时传入 log_dir

**文件**: `sar_orch/worker.py`

**目标**: 在 `A2AWorkerSink` 构造时传入 `self._log_dir` 作为本地持久化目标目录。

`oldString` (in `start()` method, line ~134):
```python
        self._server = create_worker_a2a_server(
            worker_id=self.worker_id,
            ...
            log_dir=Path(self._log_dir) if self._log_dir else None,
            ...
        )
```

这段代码创建 worker A2A server 时已经把 `log_dir` 传给了 `AgentAdapter`。但是 `A2AWorkerSink` 是 `AgentAdapter.execute()` 内部创建的，没有从外部传入 log_dir。

需要在 `AgentAdapter` 上增加 `log_dir` → `sink` 的传递链。最简单的方案：在 `AgentAdapter` 的 `execute()` 中创建 `A2AWorkerSink` 时使用 `self._log_dir`。

检查 `agent_adapter.py` line 134:
```python
        sink = A2AWorkerSink(event_queue, task_id, context_id)
```

需要改为：
```python
        log_dir = str(self._log_dir) if self._log_dir else None
        sink = A2AWorkerSink(event_queue, task_id, context_id, log_dir=log_dir)
```

但注意 `self._log_dir` 已经是 `Path | None`。修改 agent_adapter.py：

`oldString` (line 134):
```python
        sink = A2AWorkerSink(event_queue, task_id, context_id)
```

`newString`:
```python
        sink = A2AWorkerSink(
            event_queue, task_id, context_id,
            log_dir=str(self._log_dir) if self._log_dir else None,
        )
```

**验证**: `uv run --with ruff ruff check src/a2a/worker/agent_adapter.py`

---

## Task 3: A2A 入口请求体日志

### Step 3.1 — 修改 `agent_executor.py`：`execute()` 入口记 TaskLogger event

**文件**: `src/a2a/coordinator/agent_executor.py`

**目标**: 在 `execute()` 方法开头（line 172），记录包含原始消息的日志事件。

`oldString` (line 172):
```python
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """处理 A2A 任务：使用 RouterAgent DAG 计划 + 闭环执行。"""
        task = context.current_task
```

`newString`:
```python
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """处理 A2A 任务：使用 RouterAgent DAG 计划 + 闭环执行。"""
        # 记录原始请求体
        if self._task_logger is not None:
            try:
                raw_query = context.get_user_input() or ""
                msg = context.message
                request_info = {
                    "query_preview": raw_query[:500],
                    "has_metadata": bool(msg and msg.HasField("metadata")),
                    "task_id": context.task_id,
                    "context_id": context.context_id,
                }
                if msg and msg.HasField("metadata"):
                    request_info["metadata"] = {k: str(v) for k, v in msg.metadata.items()}
                self._task_logger.log_event(
                    context.current_task.id if context.current_task else "unknown",
                    "raw_request",
                    request_info,
                    source="executor",
                )
            except Exception as exc:
                logger.debug("Failed to log raw request: %s", exc)
        task = context.current_task
```

**验证**: `uv run --with ruff ruff check src/a2a/coordinator/agent_executor.py`

---

### Step 3.2 — 修改 `agent_adapter.py`：`execute()` 入口记 Python logger

**文件**: `src/a2a/worker/agent_adapter.py`

**目标**: `execute()` 入口用 `logger.info()` 记录入站请求摘要。

`oldString` (line 119):
```python
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """A2A AgentExecutor 接口实现。经统一控制器驱动一次 Agent 运行。"""
        task = context.current_task
```

`newString`:
```python
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """A2A AgentExecutor 接口实现。经统一控制器驱动一次 Agent 运行。"""
        query_preview = (context.get_user_input() or "")[:200]
        logger.info(
            "[ENTRY] task=%s context=%s query=%s",
            context.task_id, context.context_id, query_preview,
        )
        task = context.current_task
```

**验证**: `uv run --with ruff ruff check src/a2a/worker/agent_adapter.py`

---

## Task 4: query_sar_state 返回结构化到日志

### Step 4.1 — 修改 `logger.py`：新增 `log_coordinator_state()` 方法

**文件**: `sar_orch/logger.py`

**目标**: 在 `ExperimentLogger` 上新增方法，将 coordinator 看到的 SAR 状态结构化摘要写入 `agent_interactions.csv`。

**变更**: 在 `log_agent_interaction` 方法（line 132）之后添加：

`oldString` (line 173):
```python
    # ------------------------------------------------------------------
    # Router interactions
    # ------------------------------------------------------------------
```

`newString`:
```python
    # ------------------------------------------------------------------
    # Coordinator state snapshot (query_sar_state)
    # ------------------------------------------------------------------

    def log_coordinator_state(
        self,
        step: int,
        state_summary: str,
    ):
        """Log the SAR state snapshot as seen by the coordinator.

        Writes to agent_interactions.csv with Agent="Coordinator" and
        ToolName="query_sar_state" for traceability.

        Args:
            step: Current simulation step number.
            state_summary: Structured JSON string of the SAR state.
        """
        with self._lock:
            self._ensure_file("agent_interactions")
            row = {
                "Step": step,
                "Agent": "Coordinator",
                "ToolName": "query_sar_state",
                "ToolArgs": state_summary[:2000],
                "Action": "",
                "Observation": "",
                "LLMInput": "",
                "LLMOutput": "",
                "Thinking": "",
            }
            self._writers["agent_interactions"].writerow(row)
            self._files["agent_interactions"].flush()

    # ------------------------------------------------------------------
    # Router interactions
    # ------------------------------------------------------------------
```

**验证**: `uv run --with ruff ruff check sar_orch/logger.py`

---

### Step 4.2 — 修改 `coordinator.py`：`_router_cb` 记录 query_sar_state tool_result

**文件**: `sar_orch/coordinator.py`

**目标**: `_router_cb` 中 `tool_start` 之外的 `tool_result` 也处理——当 `tool_name == "query_sar_state"` 时调用 `exp_logger.log_coordinator_state()`。

注意 `_router_cb` 目前只监听 `llm_response` 和 `tool_start`。`tool_result` 事件也是通过 `A2ACoordinatorSink` → `TeeSink` → `CallbackSink(_router_cb)` 传递的。

`oldString` (line 55-76):
```python
        # Router step_callback for logging subtask dispatches + coordinator token usage
        def _router_cb(event_type: str, **kw):
            if event_type == "llm_response" and self._exp_logger is not None:
                usage = kw.get("usage")
                if usage is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent="Coordinator",
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif (
                event_type == "tool_start"
                and kw.get("tool_name") == "dispatch_task"
                and self._exp_logger is not None
            ):
                args = kw.get("arguments", {})
                self._exp_logger.log_router_interaction(
                    step=getattr(self._barrier, "_step_counter", 0),
                    subtask=args.get("prompt", ""),
                    assigned_to=args.get("agent_id", ""),
                )
```

`newString`:
```python
        # Router step_callback for logging subtask dispatches + coordinator token usage
        def _router_cb(event_type: str, **kw):
            if self._exp_logger is None:
                return
            step = getattr(self._barrier, "_step_counter", 0)
            if event_type == "llm_response":
                usage = kw.get("usage")
                if usage is not None:
                    self._exp_logger.log_token_usage(
                        step=step,
                        agent="Coordinator",
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif event_type == "tool_start":
                tool_name = kw.get("tool_name", "")
                args = kw.get("arguments", {})
                if tool_name == "dispatch_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=args.get("prompt", ""),
                        assigned_to=args.get("agent_id", ""),
                    )
            elif event_type == "tool_result":
                tool_name = kw.get("tool_name", "")
                if tool_name == "query_sar_state":
                    content = kw.get("content", "")
                    self._exp_logger.log_coordinator_state(
                        step=step, state_summary=(content or "")[:2000],
                    )
```

**验证**: `uv run --with ruff ruff check sar_orch/coordinator.py`

---

## Task 5: EventStore 持久化 + 大小限制

### Step 5.1 — 修改 `event_store.py`：NDJSON 持久化 + max_events_per_task 上限

**文件**: `src/a2a/coordinator/event_store.py`

**目标**:
- `__init__` 增加 `log_dir: str | None = None` 参数
- `append()` 增加 `max_events_per_task` 参数（默认 500），超限时丢弃最早的记录
- 写入 NDJSON 行到 `{log_dir}/events_{task_id}.ndjson`

**完整文件替换** (130 行 → ~170 行):

```python
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

logger = logging.getLogger(__name__)


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
        self.event_type = event_type
        self.state = state
        self.text = text
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
        state: str | None = None,
        text: str | None = None,
    ) -> None:
        with self._lock:
            records = self._events.setdefault(task_id, [])
            records.append(
                EventRecord(
                    task_id=task_id,
                    event_type=event_type,
                    state=state,
                    text=text,
                )
            )
            # 超限时丢弃最旧记录
            if len(records) > self._max_events_per_task:
                excess = len(records) - self._max_events_per_task
                del records[:excess]

        # NDJSON 持久化（锁外写入，减少临界区）
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


# 模块级单例（兼容现有 import）
event_store = EventStore()
```

**验证**: `uv run --with ruff ruff check src/a2a/coordinator/event_store.py`

---

## Task 6: router_interactions.csv 扩展

### Step 6.1 — 修改 `coordinator.py`：`_router_cb` 记录更多工具调用

**文件**: `sar_orch/coordinator.py`

**目标**: 在 `_router_cb` 中记录 `respond_worker`、`query_sar_state`、`collect_results`、`finish_task`。

`oldString` (Step 4.2 已更新，在这里继续扩展 `tool_start` branch):
```python
            elif event_type == "tool_start":
                tool_name = kw.get("tool_name", "")
                args = kw.get("arguments", {})
                if tool_name == "dispatch_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=args.get("prompt", ""),
                        assigned_to=args.get("agent_id", ""),
                    )
```

`newString`:
```python
            elif event_type == "tool_start":
                tool_name = kw.get("tool_name", "")
                args = kw.get("arguments", {})
                if tool_name == "dispatch_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=args.get("prompt", ""),
                        assigned_to=args.get("agent_id", ""),
                    )
                elif tool_name == "respond_worker":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=f"respond_worker(agent_id={args.get('agent_id','')})",
                        assigned_to=args.get("agent_id", ""),
                    )
                elif tool_name == "query_sar_state":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="query_sar_state()",
                        assigned_to="Coordinator",
                    )
                elif tool_name == "collect_results":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask=f"collect_results(task_ids={args.get('task_ids', [])})",
                        assigned_to="Coordinator",
                    )
                elif tool_name == "finish_task":
                    self._exp_logger.log_router_interaction(
                        step=step,
                        subtask="finish_task()",
                        assigned_to="Coordinator",
                    )
```

**验证**: `uv run --with ruff ruff check sar_orch/coordinator.py`

---

## Task 7: agent_interactions.csv 补充 llm_input

### Step 7.1 — 修改 `agent.py`：`llm_response` 回调携带 `input_messages`

**文件**: `src/Agent/worker_agent/agent.py`

**目标**: `llm_response` 回调中增加 `messages` 参数，使下游能记录 LLM 输入。

`oldString` (line 556-563):
```python
            # Notify step callback about LLM response
            if step_callback is not None:
                try:
                    await step_callback(
                        "llm_response",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        usage=response.usage,
                    )
                except Exception:
                    logger.exception("step_callback(llm_response) failed")
```

`newString`:
```python
            # Notify step callback about LLM response
            if step_callback is not None:
                try:
                    await step_callback(
                        "llm_response",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        usage=response.usage,
                        input_messages=messages_for_llm,
                    )
                except Exception:
                    logger.exception("step_callback(llm_response) failed")
```

**验证**: `uv run --with ruff ruff check src/Agent/worker_agent/agent.py`

---

### Step 7.2 — 修改 `worker.py`：`_step_callback` 提取 `input_messages` 写入 `llm_input`

**文件**: `sar_orch/worker.py`

**目标**: `_step_callback` 在 `llm_response` 事件中保存 `input_messages` 到 `self._last_llm_input`，`tool_result` 时写入 `llm_input` 字段。

`oldString` (line 101-132):
```python
        # step_callback for logging agent interactions + token usage
        # NOTE: must be sync — AgentAdapter._step_handler does NOT await external callbacks
        def _step_callback(type_: str, **data):
            if type_ == "llm_response":
                self._last_llm_output = data.get("content", "")
                usage = data.get("usage")
                if usage is not None and self._exp_logger is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif type_ == "tool_start":
                self._pending_tool = {
                    "tool_name": data.get("tool_name", ""),
                    "arguments": data.get("arguments", {}),
                }
            elif type_ == "tool_result" and self._pending_tool is not None:
                tool_name = self._pending_tool["tool_name"]
                args = self._pending_tool["arguments"]
                exp = self._exp_logger
                if exp is not None:
                    exp.log_agent_interaction(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        tool_name=tool_name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        action=_build_action(tool_name, args),
                        observation=data.get("content", ""),
                        llm_output=self._last_llm_output,
                    )
                self._pending_tool = None
```

`newString`:
```python
        # step_callback for logging agent interactions + token usage
        # NOTE: must be sync — AgentAdapter._step_handler does NOT await external callbacks
        def _step_callback(type_: str, **data):
            if type_ == "llm_response":
                self._last_llm_output = data.get("content", "")
                # Save input messages for tool_result logging
                msgs = data.get("input_messages")
                if msgs:
                    lines = []
                    for m in msgs[-6:]:  # last 6 messages to cap size
                        role = getattr(m, "role", "?")
                        c = getattr(m, "content", "")
                        c_str = c[:200] if isinstance(c, str) else str(c)[:200]
                        lines.append(f"{role}: {c_str}")
                    self._last_llm_input = "\n".join(lines)
                else:
                    self._last_llm_input = ""
                usage = data.get("usage")
                if usage is not None and self._exp_logger is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif type_ == "tool_start":
                self._pending_tool = {
                    "tool_name": data.get("tool_name", ""),
                    "arguments": data.get("arguments", {}),
                }
            elif type_ == "tool_result" and self._pending_tool is not None:
                tool_name = self._pending_tool["tool_name"]
                args = self._pending_tool["arguments"]
                exp = self._exp_logger
                if exp is not None:
                    exp.log_agent_interaction(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        tool_name=tool_name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        action=_build_action(tool_name, args),
                        observation=data.get("content", ""),
                        llm_input=getattr(self, "_last_llm_input", ""),
                        llm_output=self._last_llm_output,
                    )
                self._pending_tool = None
```

同时需要在 `__init__` 方法中初始化 `_last_llm_input` 字段。

`oldString` (line 57-59):
```python
        # Step callback correlation state
        self._call_seq: int = 0
        self._pending_tool: dict | None = None
        self._last_llm_output: str = ""
```

`newString`:
```python
        # Step callback correlation state
        self._call_seq: int = 0
        self._pending_tool: dict | None = None
        self._last_llm_output: str = ""
        self._last_llm_input: str = ""
```

**验证**: `uv run --with ruff ruff check sar_orch/worker.py`

---

## Task 8: ReAct 步边界标记

### Step 8.1 — 修改 `agent.py`：step 递增处发 `step_boundary` 事件

**文件**: `src/Agent/worker_agent/agent.py`

**目标**: `Agent.run()` 中 `step += 1`（line 771）前发送 `step_boundary` 事件。

`oldString` (line 769-771):
```python
            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            step += 1
```

`newString`:
```python
            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time

            # Emit step boundary for logging
            if step_callback is not None:
                try:
                    await step_callback(
                        "step_boundary",
                        step=step + 1,
                        max_steps=self.max_steps,
                        elapsed=step_elapsed,
                    )
                except Exception:
                    logger.exception("step_callback(step_boundary) failed")

            step += 1
```

**验证**: `uv run --with ruff ruff check src/Agent/worker_agent/agent.py`

---

## 验证步骤

在每个 Task 完成后执行（汇总）：

```bash
# Lint 全部修改过的文件
uv run --with ruff ruff check \
  src/Agent/worker_agent/agent.py \
  src/a2a/worker/sink.py \
  src/a2a/worker/agent_adapter.py \
  src/a2a/coordinator/agent_executor.py \
  src/a2a/coordinator/event_store.py \
  src/a2a/coordinator/task_logger.py \
  sar_orch/logger.py \
  sar_orch/coordinator.py \
  sar_orch/worker.py

# 运行测试
uv run pytest tests/ -v
```

---

## 执行顺序

1. **Task 1**: `agent.py` → `agent_adapter.py`（ask_coordinator 日志化）
2. **Task 2**: `sink.py` → `agent_adapter.py`（NDJSON 持久化）
3. **Task 3**: `agent_executor.py` → `agent_adapter.py`（入口日志）
4. **Task 4**: `logger.py` → `coordinator.py`（query_sar_state 日志）
5. **Task 5**: `event_store.py`（持久化 + 大小限制）
6. **Task 6**: `coordinator.py`（router_interactions 扩展）
7. **Task 7**: `agent.py` → `worker.py`（llm_input 补充）
8. **Task 8**: `agent.py`（step_boundary 标记）

每个 Task 内步骤串行，Task 间顺序无关（文件无冲突）。
