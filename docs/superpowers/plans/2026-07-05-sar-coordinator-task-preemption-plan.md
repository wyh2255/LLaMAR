---
日期: 2026-07-05
文档类型: 实现计划
文档概述: SAR Coordinator 任务抢占机制的具体实现步骤
---

# SAR Coordinator 任务抢占机制实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 SAR Coordinator 能主动取消 Worker 正在运行的探索任务，使系统能从探索阶段过渡到灭火/救援阶段。

**Architecture:** 利用 A2A 标准 `tasks/cancel` RPC + Agent 框架已有的 `cancel_event` 机制实现真正的任务取消；在 `AgentAdapter` 层按 `task_id` 维护独立取消事件，避免伤害共享的 SAR `context_id`；同时通过 prompt 约束让 Worker 探索任务具备量化退出条件，并在 poll 循环中实时更新 step budget。

**Tech Stack:** Python 3.10, asyncio, pytest, A2A protocol, Mini-Agent framework, ruff

## Global Constraints

- `CancelTaskTool` 必须放在 `src/a2a/builtin_tools/`（通用）。
- 取消操作只影响指定 `task_id`，不伤害共享的 `context_id`。
- Worker 探索退出条件：连续 3 步无新发现则调用 `finish_task`。
- 所有代码必须通过 `ruff check` 和 `ruff format`。
- 每个任务必须有测试覆盖，测试通过后才能进入下一任务。
- 不改 A2A server/request handler 等第三方依赖代码。

---

## File Structure

| 文件 | 责任 |
|------|------|
| `src/a2a/builtin_tools/cancel_task.py` | 新增：Coordinator 调用，通过 A2A cancel 终止 Worker 任务 |
| `src/a2a/builtin_tools/__init__.py` | 导出 `CancelTaskTool` |
| `src/a2a/coordinator/agent_executor.py` | 把 `CancelTaskTool` 注入 Coordinator 工具列表 |
| `src/a2a/worker/agent_adapter.py` | 按 `task_id` 维护独立 `cancel_event`，修复误杀 context_id 的问题 |
| `sar_orch/prompts/coordinator/system.md` | 增加 cancel/re-dispatch 策略说明 |
| `sar_orch/prompts/worker/system.md` | 增加 `finish_task`、探索退出条件、任务取消响应规则 |
| `sar_orch/experiment.py` | poll 循环中调用 `semantic_map.update_step_budget()` |
| `tests/test_cancel_task.py` | 测试 `CancelTaskTool` |
| `tests/test_agent_adapter_cancel.py` | 测试 `AgentAdapter` 按 task_id 取消 |

---

### Task 1: 新增 CancelTaskTool

**Files:**
- Create: `src/a2a/builtin_tools/cancel_task.py`
- Modify: `src/a2a/builtin_tools/__init__.py`
- Test: `tests/test_cancel_task.py`

**Interfaces:**
- Consumes: `TaskStore`（提供 `resolve_dispatch_id`, `get_node`, `_dispatch_to_worker`），`AgentRegistry`（提供 `get(worker_id) -> AgentInfo`）
- Produces: `CancelTaskTool.execute(task_id: str) -> ToolResult`

- [ ] **Step 1: 编写测试**

```python
# tests/test_cancel_task.py
"""Tests for CancelTaskTool."""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.coordinator.task_store import PlanNode
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus, AgentNotFoundError


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_node = MagicMock()
    store._dispatch_to_worker = {}
    store._worker_to_dispatch = {}
    return store


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get = MagicMock()
    return registry


@pytest.fixture
def tool(mock_store, mock_registry):
    return CancelTaskTool(store=mock_store, registry=mock_registry)


class TestCancelTaskToolProperties:
    def test_name(self, tool):
        assert tool.name == "cancel_task"

    def test_parameters_schema(self, tool):
        schema = tool.parameters
        assert "task_id" in schema["properties"]
        assert "task_id" in schema["required"]


@pytest.mark.asyncio
async def test_execute_cancels_worker_task(tool, mock_store, mock_registry):
    """正常流程：dispatch_id → worker_task_id → A2A cancel."""
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = "worker-uuid-42"
    mock_store.get_node.return_value = node
    mock_registry.get.return_value = AgentInfo(
        agent_id="worker-1",
        description="Test worker",
        endpoint="http://worker-1:8090",
        status=AgentStatus.ONLINE,
    )

    mock_task = MagicMock()
    mock_task.status.state = 5  # TASK_STATE_CANCELED

    mock_client = MagicMock()
    mock_client.cancel_task = AsyncMock(return_value=mock_task)
    mock_client.close = AsyncMock()

    patched_create_client = AsyncMock(return_value=mock_client)
    with patch(
        "a2a.builtin_tools.cancel_task.create_client",
        patched_create_client,
    ):
        result = await tool.execute(task_id="task-42")

    assert result.success is True
    assert "worker-uuid-42" in result.content
    mock_client.cancel_task.assert_called_once()
    patched_create_client.assert_awaited_once()
    assert patched_create_client.call_args[0][0] == "http://worker-1:8090"


@pytest.mark.asyncio
async def test_execute_task_not_found(tool, mock_store):
    mock_store.resolve_dispatch_id.return_value = None
    result = await tool.execute(task_id="nonexistent")
    assert result.success is False
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_execute_worker_not_in_registry(tool, mock_store, mock_registry):
    node = PlanNode(task_id="task-99", worker_id="lost-worker")
    mock_store.resolve_dispatch_id.return_value = "task-99"
    mock_store._dispatch_to_worker["task-99"] = "worker-uuid-99"
    mock_store.get_node.return_value = node
    mock_registry.get.side_effect = AgentNotFoundError("lost-worker")

    result = await tool.execute(task_id="task-99")
    assert result.success is False
    assert "not found in registry" in result.content


@pytest.mark.asyncio
async def test_execute_not_yet_dispatched(tool, mock_store):
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.resolve_dispatch_id.return_value = "task-42"
    mock_store._dispatch_to_worker["task-42"] = ""
    mock_store.get_node.return_value = node

    result = await tool.execute(task_id="task-42")
    assert result.success is False
    assert "not yet dispatched" in result.content
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/test_cancel_task.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'a2a.builtin_tools.cancel_task'`

- [ ] **Step 3: 实现 CancelTaskTool**

```python
# src/a2a/builtin_tools/cancel_task.py
"""CancelTaskTool — Coordinator 取消 Worker 正在运行的任务。"""

from __future__ import annotations

import logging
from typing import Any

from httpx import AsyncClient, Timeout
from a2a.client import create_client, ClientConfig
from a2a.types.a2a_pb2 import CancelTaskRequest, TaskState

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError

logger = logging.getLogger(__name__)


class CancelTaskTool(Tool):
    """Cancel a running worker task by its coordinator dispatch id."""

    def __init__(self, store: TaskStore, registry: AgentRegistry) -> None:
        super().__init__()
        self._store = store
        self._registry = registry

    @property
    def name(self) -> str:
        return "cancel_task"

    @property
    def description(self) -> str:
        return (
            "Cancel a running worker task. Provide the dispatch task_id "
            "used in dispatch_task(). The worker will stop its current "
            "Agent loop and its state will become CANCELED. After canceling, "
            "call query_task_events to confirm, then dispatch a new task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The dispatch task_id to cancel",
                },
            },
            "required": ["task_id"],
        }

    async def execute(self, task_id: str) -> ToolResult:
        dispatch_id = self._store.resolve_dispatch_id(task_id)
        if dispatch_id is None:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in dispatched tasks.",
                error="unknown_task_id",
            )

        node = self._store.get_node(dispatch_id)
        if node is None or not node.worker_id:
            return ToolResult(
                success=False,
                content=f"Task '{dispatch_id}' has no assigned worker.",
                error="no_worker",
            )

        worker_task_id = self._store._dispatch_to_worker.get(dispatch_id)  # noqa: SLF001
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=(
                    f"Task '{dispatch_id}' has not been dispatched to a worker yet. "
                    "Wait for the worker to acknowledge before canceling."
                ),
                error="not_yet_dispatched",
            )

        try:
            agent_info = self._registry.get(node.worker_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{node.worker_id}' not found in registry.",
                error="worker_not_found",
            )

        config = ClientConfig(
            streaming=False,
            httpx_client=AsyncClient(timeout=Timeout(30.0)),
        )

        try:
            client = await create_client(agent_info.endpoint, config)
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Failed to connect to worker: {e}",
                error="connection_failed",
            )

        try:
            request = CancelTaskRequest(id=worker_task_id)
            result_task = await client.cancel_task(request)
            state_name = TaskState.Name(result_task.status.state)
        except Exception as e:
            await client.close()
            return ToolResult(
                success=False,
                content=f"Cancel request failed: {e}",
                error="cancel_failed",
            )

        await client.close()
        return ToolResult(
            success=True,
            content=(
                f"Task '{task_id}' (worker task {worker_task_id}) "
                f"cancelled. Worker state: {state_name}."
            ),
        )
```

- [ ] **Step 4: 导出 CancelTaskTool**

```python
# src/a2a/builtin_tools/__init__.py 添加以下两行：

# 在文件顶部 imports 区域加入：
from a2a.builtin_tools.cancel_task import CancelTaskTool

# 在 __all__ 列表中加入：
__all__.extend(["CancelTaskTool"])
```

- [ ] **Step 5: 运行测试，确认通过**

```bash
uv run pytest tests/test_cancel_task.py -v
```

Expected: 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/a2a/builtin_tools/cancel_task.py src/a2a/builtin_tools/__init__.py tests/test_cancel_task.py
git commit -m "feat(a2a): add CancelTaskTool for coordinator to cancel worker tasks"
```

---

### Task 2: 修复 AgentAdapter 按 task_id 取消

**Files:**
- Modify: `src/a2a/worker/agent_adapter.py`
- Test: `tests/test_agent_adapter_cancel.py`

**Interfaces:**
- Consumes: `RequestContext.task_id`, `RequestContext.context_id`
- Produces: `AgentAdapter._task_cancel_events: dict[str, asyncio.Event]`, `AgentAdapter.cancel()` 只设置对应 task 的事件

- [ ] **Step 1: 编写测试**

```python
# tests/test_agent_adapter_cancel.py
"""Tests for AgentAdapter cancel-by-task-id behavior."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from a2a.worker.agent_adapter import AgentAdapter
from Agent.worker_agent.schema import RunResult


class _FakeUpdater:
    def __init__(self):
        self.start_work = AsyncMock()
        self.requires_input = AsyncMock()
        self.add_artifact = AsyncMock()
        self.complete = AsyncMock()
        self.failed = AsyncMock()
        self.cancel = AsyncMock()


class _FakeContext:
    def __init__(self, task_id="task-1", context_id="ctx-shared", user_input="go"):
        self._task_id = task_id
        self._context_id = context_id
        self._task = MagicMock()
        self._task.id = task_id
        self._task.context_id = context_id
        self._input = user_input

    @property
    def task_id(self):
        return self._task_id

    @property
    def context_id(self):
        return self._context_id

    @property
    def current_task(self):
        return self._task

    def get_user_input(self):
        return self._input


class _FakeEventQueue:
    def __init__(self):
        self.enqueue_event = AsyncMock()


@pytest.mark.asyncio
async def test_execute_registers_cancel_event_per_task():
    """Each execute() call registers a cancel_event keyed by task_id."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    assert "task-1" not in adapter._task_cancel_events  # cleaned up in finally
    # Verify submit was called with a cancel_event
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("cancel_event") is not None


@pytest.mark.asyncio
async def test_execute_non_snapshot_path_registers_cancel_event():
    """非 snapshot 路径也注册 cancel_event."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller._get_session = None  # force non-snapshot path
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._task_cancel_events = {}

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-3", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    assert "task-3" not in adapter._task_cancel_events  # cleaned up in finally
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("cancel_event") is not None


@pytest.mark.asyncio
async def test_cancel_sets_event_for_specific_task_only():
    """cancel() should set only the target task's event and call updater.cancel()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.cancel = MagicMock()  # should NOT be called
    adapter._task_cancel_events = {
        "task-1": asyncio.Event(),
        "task-2": asyncio.Event(),
    }

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", context_id="ctx-shared")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        await adapter.cancel(fake_ctx, fake_queue)

    assert adapter._task_cancel_events["task-1"].is_set()
    assert not adapter._task_cancel_events["task-2"].is_set()
    fake_updater.cancel.assert_called_once()
    adapter._controller.cancel.assert_not_called()
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/test_agent_adapter_cancel.py -v
```

Expected: FAIL because `_task_cancel_events` does not exist and `cancel()` does not behave as tested.

- [ ] **Step 3: 修改 AgentAdapter**

```python
# src/a2a/worker/agent_adapter.py

# 在 __init__ 末尾添加：
self._task_cancel_events: dict[str, asyncio.Event] = {}

# 修改 execute()：
# ⚠️ 当前代码有两个 submit() 分支（snapshot 和 非 snapshot），
# cancel_event 必须在 if/else 之前创建，并注入到两个分支中：
async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
    ...
    task_id = task.id
    context_id = task.context_id

    cancel_event = asyncio.Event()
    self._task_cancel_events[task_id] = cancel_event

    try:
        ...
        if hasattr(self._controller, "_get_session"):
            snapshot = await ctx.load_snapshot(task_id)
        ...
        if snapshot:
            # snapshot 分支 — 同样传入 cancel_event
            result = await self._controller.submit(
                context_id,
                query,
                sink,
                cancel_event=cancel_event,
                task_id=task_id,
                initial_messages=...,
            )
        else:
            # 非 snapshot 分支
            result = await self._controller.submit(
                context_id,
                query,
                sink,
                cancel_event=cancel_event,
                task_id=task_id,
            )
        ...
    finally:
        self._task_cancel_events.pop(task_id, None)

# 修改 cancel()：
async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
    """取消任务。只取消指定 task_id 的事件，不伤害共享 context_id。"""
    task_id = context.task_id or ""
    context_id = context.context_id or ""

    ev = self._task_cancel_events.get(task_id)
    if ev is not None:
        ev.set()

    updater = TaskUpdater(event_queue, task_id, context_id)
    await updater.cancel()
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
uv run pytest tests/test_agent_adapter_cancel.py -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/a2a/worker/agent_adapter.py tests/test_agent_adapter_cancel.py
git commit -m "fix(a2a): cancel worker task by task_id instead of context_id"
```

---

### Task 3: 注册 CancelTaskTool 到 Coordinator

**Files:**
- Modify: `src/a2a/coordinator/agent_executor.py`
- Test: import smoke test

**Interfaces:**
- Consumes: `CancelTaskTool(store, registry)`
- Produces: Coordinator LLM 可见 `cancel_task` 工具

- [ ] **Step 1: 修改 agent_executor.py**

```python
# src/a2a/coordinator/agent_executor.py
from a2a.builtin_tools.cancel_task import CancelTaskTool

# 在 _execute_agentic 的 tools 列表中加入：
tools = [
    UpdatePlanTool(store),
    DispatchTaskTool(
        store,
        coordinator_host=self._coordinator_host,
        coordinator_port=self._coordinator_port,
    ),
    QueryTaskEventsTool(store),
    CancelTaskTool(store, self._registry),
    VerifyResultTool(store),
    QueryTaskResultsTool(store.results),
    RespondWorkerTool(store, self._registry),
    SARFinishTaskTool(store),
]
```

- [ ] **Step 2: import smoke test**

```bash
uv run python -c "from a2a.coordinator.agent_executor import CoordinatorAgentExecutor; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/a2a/coordinator/agent_executor.py
git commit -m "feat(coordinator): register CancelTaskTool in agentic executor"
```

---

### Task 4: 更新 Coordinator Prompt

**Files:**
- Modify: `sar_orch/prompts/coordinator/system.md`

**Interfaces:**
- Consumes: `cancel_task` 工具
- Produces: Coordinator LLM 知道何时取消并重新派活

- [ ] **Step 1: 在 Handling Worker Status 节后新增 Canceling and Re-dispatching 节**

```markdown
## Canceling and Re-dispatching (CRITICAL)

If a worker has been exploring for many steps and you have enough map information to transition to firefighting or rescue, you MAY cancel its current task and immediately give it a new task.

When to cancel:
- The worker's current task is no longer useful (e.g., endless exploration with no new findings).
- You need to transition phases (explore → firefighting → rescue) but the worker is still RUNNING.
- The step budget is tight and the worker is wasting steps.

How to cancel:
1. Call `cancel_task(task_id="<dispatch-id>")`.
2. Call `query_task_events(["<dispatch-id>"])` to confirm the state is `CANCELED`.
3. Immediately call `dispatch_task(agent_id="<same-agent>", prompt="<new firefighting/rescue chain>", task_id="<new-id>")`.
4. Call `query_task_events(["<new-id>"])` to track progress.

Do NOT leave an agent without a task after canceling — the barrier will wait 60s and waste a step.
```

- [ ] **Step 2: 验证内容**

```bash
grep -A 20 "Canceling and Re-dispatching" sar_orch/prompts/coordinator/system.md
```

Expected: 显示新增的 markdown 内容

- [ ] **Step 3: Commit**

```bash
git add sar_orch/prompts/coordinator/system.md
git commit -m "prompt(coordinator): add cancel and re-dispatch instructions"
```

---

### Task 5: 更新 Worker Prompt

**Files:**
- Modify: `sar_orch/prompts/worker/system.md`

**Interfaces:**
- Consumes: `finish_task` 工具
- Produces: Worker LLM 知道探索退出条件和子任务完成信号

- [ ] **Step 1: 修改 Critical Rules**

原 Rule 6：
```markdown
6. **Ask coordinator after main task**: After completing your assigned actions, call `ask_coordinator("Mission complete. What should I do next?")` to wait for the coordinator's instructions. Do NOT use `no_op()` to wait — the coordinator needs to know you're done and give you the next task.
```

改为：
```markdown
6. **Finish your subtask**: After completing ALL assigned actions, call `finish_task(success=True, summary="Brief summary of what you did and the outcome", task_description="The original task you were given")` to mark the subtask complete. Do NOT use `no_op()` or `ask_coordinator()` to wait — the coordinator will see the completed task and give you the next assignment.
```

新增 Rule 7 和 8：
```markdown
7. **Explore exit condition**: If you call `explore` for 3 consecutive steps and discover no new fire, person, reservoir, or deposit, your exploration subtask is complete. Call `finish_task(success=True, summary="Explored area, no new objects found")` and wait for the next instruction.
8. **Task cancellation**: If the coordinator cancels your task, stop immediately. The Agent loop will exit on its own; do not continue the previous plan.
```

- [ ] **Step 2: 验证内容**

```bash
grep -E "Finish your subtask|Explore exit condition|Task cancellation" sar_orch/prompts/worker/system.md
```

Expected: 三行都匹配到

- [ ] **Step 3: Commit**

```bash
git add sar_orch/prompts/worker/system.md
git commit -m "prompt(worker): add finish_task, explore exit, and cancellation rules"
```

---

### Task 6: 实时更新 Step Budget

**Files:**
- Modify: `sar_orch/experiment.py`

**Interfaces:**
- Consumes: `barrier.get_metrics()["steps"]`, `max_steps`
- Produces: `semantic_map.update_step_budget(current_step, max_steps)` 被调用

- [ ] **Step 1: 修改 poll 循环**

在 `sar_orch/experiment.py` 的 poll 循环中，找到 `_last_step_logged` 更新处（约 line 364），在该分支内加入：

```python
# Inside the "if step_log and ... metrics['steps'] > _last_step_logged" branch
# after exp_logger.flush_summary()
if coordinator is not None and coordinator._semantic_map is not None:
    coordinator._semantic_map.update_step_budget(
        current_step=metrics["steps"],
        max_steps=max_steps,
    )
```

具体位置：

```python
                exp_logger.flush_summary()
                _last_step_logged = metrics["steps"]
```

改为：

```python
                exp_logger.flush_summary()
                if coordinator is not None and coordinator._semantic_map is not None:
                    coordinator._semantic_map.update_step_budget(
                        current_step=metrics["steps"],
                        max_steps=max_steps,
                    )
                _last_step_logged = metrics["steps"]
```

- [ ] **Step 2: 运行现有语义地图测试**

```bash
uv run pytest tests/test_semantic_map.py tests/test_semantic_tools.py -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add sar_orch/experiment.py
git commit -m "feat(sar): update semantic_map step_budget in poll loop"
```

---

### Task 7: Lint、测试与冒烟

**Files:**
- All modified files

- [ ] **Step 1: 运行 ruff**

```bash
uv run --with ruff ruff check src/a2a/builtin_tools/cancel_task.py src/a2a/builtin_tools/__init__.py src/a2a/coordinator/agent_executor.py src/a2a/worker/agent_adapter.py sar_orch/experiment.py tests/test_cancel_task.py tests/test_agent_adapter_cancel.py
uv run --with ruff ruff format src/a2a/builtin_tools/cancel_task.py src/a2a/builtin_tools/__init__.py src/a2a/coordinator/agent_executor.py src/a2a/worker/agent_adapter.py sar_orch/experiment.py tests/test_cancel_task.py tests/test_agent_adapter_cancel.py
```

Expected: 无错误

- [ ] **Step 2: 运行新增测试**

```bash
uv run pytest tests/test_cancel_task.py tests/test_agent_adapter_cancel.py -v
```

Expected: 7 tests PASS

- [ ] **Step 3: 运行相关回归测试**

```bash
uv run pytest tests/test_respond_worker.py tests/test_query_task_events.py tests/test_agent_adapter_execute.py tests/test_semantic_map.py tests/test_semantic_tools.py -v
```

Expected: PASS

- [ ] **Step 4: 冒烟运行端到端实验**

```bash
cd /home/wyh/daily_work/LLaMAR
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 30
```

Expected: 实验能跑完 30 步不 crash；检查生成的 `agent_interactions.csv` 是否出现 `cancel_task`、`finish_task` 调用；检查 `router_interactions.csv` 是否在探索后有过渡到 firefighting/rescue 的 dispatch。

- [ ] **Step 5: Commit any ruff/format changes**

```bash
git add -A
git commit -m "style: ruff format and lint fixes"
```

---

## Self-Review

### Spec Coverage

| Spec Section | Implementing Task |
|--------------|-------------------|
| 新增 `CancelTaskTool` | Task 1 |
| 注册到 Coordinator | Task 3 |
| AgentAdapter 按 task_id 取消 | Task 2 |
| Coordinator prompt cancel/re-dispatch | Task 4 |
| Worker prompt finish_task + 探索退出条件 | Task 5 |
| Step budget 实时更新 | Task 6 |
| 测试与冒烟 | Task 7 |

### Placeholder Scan

- 无 TBD/TODO
- 无 "implement later"
- 每个步骤包含完整代码或命令
- 类型/函数名在所有任务中一致：`CancelTaskTool`, `_task_cancel_events`, `update_step_budget`

### Type Consistency

- `CancelTaskTool.execute(task_id: str) -> ToolResult` ✓
- `AgentAdapter._task_cancel_events: dict[str, asyncio.Event]` ✓
- `semantic_map.update_step_budget(current_step=int, max_steps=int)` ✓
- `TaskStore._dispatch_to_worker` 在 Task 1 和 Task 3 中一致 ✓
