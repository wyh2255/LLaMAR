---
日期: 2026-07-05
文档类型: 技术设计规格
文档概述: SAR Coordinator 任务抢占机制 —— 通过 A2A 标准 tasks/cancel 让 Coordinator 主动取消 Worker 正在运行的探索任务，从而过渡到灭火/救援阶段
---

# SAR Coordinator 任务抢占机制设计

## 1. 问题背景

端到端 SAR 实验中，Coordinator 把探索任务派给 Worker 后，Worker 的探索循环不结束，Coordinator 无法向同一个 Worker 派发灭火/救援任务，系统永久卡在探索阶段。

具体表现：
- `scene=1, agents=2, seed=42` 实验中，Coordinator 仅在 Step 0 分发 2 个探索任务
- 30 步全部消耗在 Agent 循环探索已知位置 + Coordinator 轮询任务状态
- 最终 `max_steps_reached`，覆盖率 83.3%，灭火/救援次数均为 0
- 数据佐证：`dispatch_task` 4 次全是 "explore"，`query_task_events` 全部返回 `RUNNING`

根因：
1. **A2A 无任务抢占/取消机制**：`dispatch_task` 只能发给空闲 Agent，Worker 一直 `RUNNING`，Coordinator 无法派新任务。
2. **Worker 探索任务永不完结**：prompt 只说 "systematically cover the grid"，没有量化退出条件。
3. **`update_step_budget()` 从未被调用**：`semantic_map.py:167` 定义了方法，但 Coordinator 没调用过，`current_step` 永远为 0。
4. **`finish_task` 工具已注册但 Worker prompt 没提到**：子任务完成后 Worker 只会 `no_op()` 或 `ask_coordinator()` 等待。

## 2. 设计目标

1. Coordinator 能主动取消任意 Worker 正在运行的任务（基于 A2A 标准 `tasks/cancel`）。
2. Worker 探索任务有量化退出条件：连续 3 步无新发现则调用 `finish_task` 结束。
3. Coordinator 实时掌握当前 step budget。
4. Worker 明确知道 `finish_task` 的使用时机。
5. 取消操作只影响指定 task，不伤害共享的 SAR context_id。

## 3. 关键发现

- A2A SDK Client 已支持 `client.cancel_task(CancelTaskRequest)`。
- `DefaultRequestHandler.on_cancel_task()` 已支持调用 `agent_executor.cancel(context, queue)`。
- Agent 框架 `Agent.run()` 已监听 `cancel_event`，被设置后返回 `"Task cancelled by user."`。
- `AgentController` 内部按 `context_id` 索引取消事件；但 SAR 中 Coordinator 和所有 Worker 共享同一个 `context_id`，因此**不能**通过 `AgentController.cancel(context_id)` 取消。
- 正确做法：在 `AgentAdapter` 层按 `task_id` 维护独立的 `cancel_event`。

## 4. 架构总览

```
Coordinator                                      Worker
══════════                                       ══════
LLM 决定转阶段
  → cancel_task(task_id="alice-task")
    │
    ├─ dispatch_id → worker_task_id (TaskStore)
    ├─ worker_id   → endpoint       (AgentRegistry)
    ▼
  A2A client.cancel_task(CancelTaskRequest(id=worker_task_id))
    │                                              │
    └──────────────────────────────────────────────┘
                                                   ▼
                              DefaultRequestHandler.on_cancel_task
                                                   │
                              ├─ agent_executor.cancel(context, queue)
                              │    AgentAdapter.cancel(context, queue)
                              │      task_id = context.task_id
                              │      ev = _task_cancel_events[task_id]
                              │      ev.set()                 # 只杀这个 task
                              │      updater.cancel()         # A2A CANCELED
                              │
                              └─ producer_task.cancel()
                                                   │
                                                   ▼
                              Agent.run() 检查 cancel_event → 退出
                                                   │
                              push callback: TASK_STATE_CANCELED
                                                   │
                                                   ▼
                              Coordinator query_task_events → CANCELED
                                                   │
                                                   ▼
                              dispatch_task(agent_id="Alice", prompt="灭火/救援...")
```

## 5. 详细设计

### 5.1 AgentAdapter 按 task_id 取消

文件：`src/a2a/worker/agent_adapter.py`

新增实例变量：
```python
self._task_cancel_events: dict[str, asyncio.Event] = {}
```

修改 `execute()`：
```python
async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
    ...
    task_id = task.id
    context_id = task.context_id

    cancel_event = asyncio.Event()
    self._task_cancel_events[task_id] = cancel_event

    try:
        result = await self._controller.submit(
            context_id,
            query,
            sink,
            cancel_event=cancel_event,   # 每个 task 独立
            task_id=task_id,
        )
        ...
    finally:
        self._task_cancel_events.pop(task_id, None)
```

修改 `cancel()`：
```python
async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
    task_id = context.task_id or ""
    ev = self._task_cancel_events.get(task_id)
    if ev is not None:
        ev.set()                         # 只取消该 task，不碰 context_id

    updater = TaskUpdater(event_queue, task_id, context.context_id or "")
    await updater.cancel()
    # 不再调用 self._controller.cancel(context_id)
```

注意：`AgentController.submit()` 会把 `cancel_event` 赋给 `agent.cancel_event`，所以 `Agent.run()` 每步检查的就是这个 task 专属事件。

### 5.2 新增 CancelTaskTool

文件：`src/a2a/builtin_tools/cancel_task.py`

```python
class CancelTaskTool(Tool):
    """Cancel a running worker task by task_id (coordinator dispatch id)."""

    def __init__(self, store: TaskStore, registry: AgentRegistry):
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

        worker_task_id = self._store._dispatch_to_worker.get(dispatch_id)
        if not worker_task_id:
            return ToolResult(
                success=False,
                content=f"Task '{dispatch_id}' has no worker task id yet.",
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
            httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(30.0)),
        )

        try:
            client = await create_client(agent_info.endpoint, config)
        except Exception as e:
            return ToolResult(success=False, content=f"Failed to connect: {e}")

        try:
            request = CancelTaskRequest(id=worker_task_id)
            result_task = await client.cancel_task(request)
            state = TaskState.Name(result_task.status.state)
        except Exception as e:
            await client.close()
            return ToolResult(success=False, content=f"Cancel failed: {e}")

        await client.close()
        return ToolResult(
            success=True,
            content=f"Task '{task_id}' (worker task {worker_task_id}) cancelled. State: {state}.",
        )
```

### 5.3 注册 CancelTaskTool

文件：`src/a2a/coordinator/agent_executor.py`

在 `_execute_agentic()` 的 `tools` 列表中加入：
```python
from a2a.builtin_tools.cancel_task import CancelTaskTool
...
tools = [
    UpdatePlanTool(store),
    DispatchTaskTool(...),
    QueryTaskEventsTool(store),
    CancelTaskTool(store, self._registry),
    VerifyResultTool(store),
    QueryTaskResultsTool(store.results),
    RespondWorkerTool(store, self._registry),
    SARFinishTaskTool(store),
]
```

### 5.4 Coordinator Prompt 更新

文件：`sar_orch/prompts/coordinator/system.md`

在 `Handling Worker Status` 节后新增 `Canceling and Re-dispatching` 节：

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

### 5.5 Worker Prompt 更新

文件：`sar_orch/prompts/worker/system.md`

1. 在 `Available Tools` 区域加入 `finish_task` 说明（工具列表由 build.py 自动生成，但需在 Critical Rules 中显式引导）。

2. 修改 Critical Rules：
   - Rule 6 改为：
     ```markdown
     6. **Finish your subtask**: After completing ALL assigned actions, call `finish_task(success=True, summary="...", task_description="...")` to mark the subtask complete. Do NOT use `no_op()` or `ask_coordinator()` to wait.
     ```
   - 新增 Rule 7/8：
     ```markdown
     7. **Explore exit condition**: If you call `explore` for 3 consecutive steps and discover no new fire, person, reservoir, or deposit, your exploration subtask is complete. Call `finish_task(success=True, summary="...")`.
     8. **Task cancellation**: If the coordinator cancels your task, stop immediately. The Agent loop will exit on its own; do not continue the previous plan.
     ```

### 5.6 Step Budget 实时更新

文件：`sar_orch/experiment.py`

在 poll 循环每次记录 step 后（`_last_step_logged` 更新处）加入：
```python
if (
    step_log
    and step_log.get("actions")
    and metrics["steps"] > _last_step_logged
):
    ...
    if coordinator is not None and coordinator._semantic_map is not None:
        coordinator._semantic_map.update_step_budget(
            current_step=metrics["steps"],
            max_steps=max_steps,
        )
```

这样 `query_semantic_map()` 返回的 `step_budget` 是实时的，Coordinator LLM 有时间紧迫感。

### 5.7 Push Callback 状态追踪

文件：`src/a2a/coordinator/server.py`

现有 `handle_push_notification` 已经会把 `TASK_STATE_CANCELED` 写入 `event_store.append(task_id, "status_update", state="TASK_STATE_CANCELED")`。`EventStore.get_task_state()` 会识别为 `CANCELED`。

需要验证：cancel 后 Worker 的 A2A server 确实会 push 状态更新。根据 `DefaultRequestHandler.on_cancel_task`，它会通过 `result_aggregator.consume_all()` 消费 queue 中的事件，这些事件会被 push_sender 推送（如果配置了 push notification）。SAR 场景中 `send_task_async` 已配置 push notification，因此 cancel 状态会到达 Coordinator。

## 6. 数据流

### 6.1 正常取消并重派

1. Coordinator `dispatch_task(agent_id="Alice", prompt="explore", task_id="alice-task")`
2. `DispatchTaskTool` 调用 `send_task_async`，Worker A2A server 返回 `worker-task-uuid`
3. `TaskStore.register_worker_task_id("alice-task", "worker-task-uuid")`
4. Worker 开始探索，状态 `RUNNING`
5. Coordinator 判断地图足够，调用 `cancel_task(task_id="alice-task")`
6. `CancelTaskTool` 解析为 `worker-task-uuid`，调用 A2A `cancel_task`
7. Worker `DefaultRequestHandler.on_cancel_task` 调用 `AgentAdapter.cancel(context)`
8. `AgentAdapter` 设置该 task 的 `cancel_event`
9. Worker `Agent.run()` 下一步检查到 cancel，返回 cancel 结果
10. Worker A2A server push `TASK_STATE_CANCELED` 到 Coordinator
11. Coordinator `query_task_events(["alice-task"])` 返回 `CANCELED`
12. Coordinator `dispatch_task(agent_id="Alice", prompt="firefighting chain", task_id="alice-fire-1")`

### 6.2 Worker 自主结束探索

1. Worker 连续 3 次 `explore` 无新发现
2. LLM 根据 prompt 规则调用 `finish_task(success=True, summary="...")`
3. `FinishTaskTool.execute()` 返回 `ToolResult(task_complete=True)`
4. `AgentHooks` 设置 `agent._task_complete = True`
5. `Agent.run()` 下一步检查到 `_task_complete`，返回结果
6. Worker A2A server push `TASK_STATE_COMPLETED` 到 Coordinator
7. Coordinator 看到 `COMPLETED`，派发新任务

## 7. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `src/a2a/builtin_tools/cancel_task.py` | 新增 | Coordinator 取消 Worker 任务工具 |
| `src/a2a/builtin_tools/__init__.py` | 修改 | 导出 `CancelTaskTool` |
| `src/a2a/coordinator/agent_executor.py` | 修改 | 注册 `CancelTaskTool` |
| `src/a2a/worker/agent_adapter.py` | 修改 | 按 `task_id` 维护 `cancel_event` |
| `sar_orch/prompts/coordinator/system.md` | 修改 | 增加 cancel/re-dispatch 策略 |
| `sar_orch/prompts/worker/system.md` | 修改 | 增加 `finish_task`、探索退出条件、取消响应 |
| `sar_orch/experiment.py` | 修改 | poll 循环调用 `update_step_budget` |
| `src/a2a/coordinator/server.py` | 验证 | 确认 CANCELED 状态正确进入 EventStore |

## 8. 边界情况与处理

| 场景 | 处理 |
|------|------|
| Worker 已完成任务，调用 cancel_task | A2A server 返回 `TaskNotCancelableError`，工具返回失败信息 |
| Worker 还未返回 worker_task_id | `CancelTaskTool` 返回 `not_yet_dispatched` 错误，Coordinator 应稍后再试 |
| Worker 正在执行长时间工具 | `cancel_event` 在工具返回后、下一步开始时生效；`explore` 通常很快，可接受 |
| 同 Worker 多个任务 | 每个 task 独立 `cancel_event`，互不影响 |
| AgentAdapter.cancel() 时 task 已结束 | `ev` 不存在，仅调用 `updater.cancel()` 更新状态 |

## 9. 测试计划

1. **单元测试**
   - `CancelTaskTool.execute()`：mock `TaskStore` 和 `AgentRegistry`，验证能正确解析 dispatch_id 并调用 `client.cancel_task`。
   - `AgentAdapter._task_cancel_events`：验证 execute() 注册、cancel() 设置、execute() 结束后清理。

2. **集成测试**
   - 运行 `scene=1, agents=2, seed=42`：
     - 观察 Coordinator 是否在探索一段时间后调用 `cancel_task`
     - 观察 Worker 状态是否从 `RUNNING` → `CANCELED`
     - 观察 Coordinator 是否立即派发灭火/救援任务
   - 验证 `agent_interactions.csv` 中出现 `finish_task` 调用。
   - 验证 `query_semantic_map` 返回的 `current_step` 不为 0。

3. **回归测试**
   - 运行 `scene=1, agents=2, seed=42` 多次，确保没有新的 framework crash。
   - 验证 `respond_worker` 仍然正常工作（INPUT_REQUIRED 路径未被破坏）。

## 10. 风险与缓解

| 风险 | 可能性 | 影响 | 缓解 |
|------|--------|------|------|
| A2A `cancel_task` 在某些 transport 上行为不一致 | 中 | 取消不生效 | 使用 streaming=False 的 JSONRPC client，与 `respond_worker` 一致 |
| Worker cancel 后没有立即 push CANCELED 状态 | 低 | Coordinator 无法确认 | 设置短超时重试 `query_task_events` |
| AgentAdapter 修改影响 INPUT_REQUIRED 恢复 | 中 | Worker 无法从 ask_coordinator 恢复 | 确保 `cancel_event` 只在 execute() 运行期间存在，不影响 snapshot/resume 路径 |
| Prompt 改动后 LLM 不遵循探索退出条件 | 中 | 仍无限探索 | 结合 `cancel_task` 作为兜底；后续可加更严格的 few-shot 示例 |

## 11. 后续可选优化

1. **自动阶段转换**：Coordinator 不依赖 LLM 决策，而是根据 semantic map 自动判断何时转阶段并主动 cancel + re-dispatch。
2. **任务超时**：为每个 dispatch 任务设置 wall-clock 超时，超时自动 cancel。
3. **Few-shot 示例**：在 Coordinator prompt 中加入 cancel/re-dispatch 的示例，提高 LLM 遵循度。
