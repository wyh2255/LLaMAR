# Coordinator 任务管理混乱 — 详细实施计划

**日期**: 2026-07-19
**目标**: 实施全部4个方案，解决3 agents场景下的任务管理混乱问题
**总工期**: 约 4-6 小时（含验证）

---

## 实施跟踪表

| Phase | 内容 | 负责 | 状态 | 验证结果 |
|-------|------|------|------|----------|
| 1 | Prompt 强化 | subagent | ⏳ 待开始 | - |
| 2 | SendMessageTool 防护 + TaskStore 自动 cancel | subagent | ⏳ 待开始 | - |
| 3 | 状态同步修复 | subagent | ⏳ 待开始 | - |
| 4 | 综合验证 + 文档更新 | 主 agent | ⏳ 待开始 | - |

---

## Phase 1: Prompt 强化（预计 1 小时）

### 目标
通过 prompt 约束 LLM 行为，减少任务切换频率，消除任务堆积。

### 改动文件
- `sar_orch/prompts/coordinator/system.semantic.md`

### 具体改动

#### 1.1 新增 "Task Assignment Discipline" 章节（插入在 `## Critical Rules` 之后）

```markdown
## Task Assignment Discipline (CRITICAL — prevents chaos)

**One agent, one mission**: Each agent should have ONE clear mission at a time. Do NOT switch an agent's mission mid-task unless the current task is complete or has failed.

**NEVER re-dispatch to an agent with an active task**: If an agent shows RUNNING or DISPATCHED in Context Memory, do NOT assign them a new task. Wait for completion or explicitly cancel first.

**Cancel before re-dispatch**: If you must change an agent's mission, ALWAYS `cancel_task` the old task BEFORE `assign_task` the new one. Check Context Memory to confirm CANCELED state before dispatching.

**Phase-based assignment**: Assign tasks by phase:
1. **Exploration phase** (steps 1-5): All agents explore
2. **Firefighting phase** (steps 6-25): Assign agents to specific fires — one agent per fire
3. **Rescue phase** (steps 20+): Assign 2+ agents to rescue each person — these agents should NOT be fighting fires simultaneously

**3-agent special rule**: With only 3 agents, you have LIMITED parallelism. Prioritize:
- Agent 1: Firefighting (CaldorFire)
- Agent 2: Firefighting (GreatFire)  
- Agent 3: Exploration → then assist with firefighting or rescue
Do NOT reassign Agent 1 or Agent 2 to rescue until their fire is fully extinguished.
```

#### 1.2 强化 "Canceling and Re-dispatching" 章节（在现有内容后追加）

```markdown
**WARNING — Task pile-up kills missions**: Every time you cancel and re-dispatch, the old task may not cancel cleanly. After 3+ cancel/re-dispatch cycles on the same agent, the task tracking system becomes unreliable. **Minimize cancellations.**

**When cancellation is unavoidable**:
1. `cancel_task` the old task
2. **Wait one round** and check Context Memory for CANCELED state
3. Only then `assign_task` the new mission
4. If `cancel_task` returns `unknown_task_id`, the task is already gone — do NOT retry

**Never cancel these**:
- A task that is actively making progress (check Recent Changes)
- A firefighting task when the fire is still spreading (medium+ intensity)
- A rescue task when the person is being carried
```

#### 1.3 在 "Strategy — How to Command" 中添加第 8 条

```markdown
8. **Respect task boundaries**: Once you assign a mission to an agent, let them finish it. Do not micromanage or reassign unless the task is complete, failed, or the mission priorities have fundamentally changed (e.g., person discovered near spreading fire).
```

### 验证方法
1. 运行 `uv run pytest tests/ -k "coordinator" -x -q`
2. 运行 scene 1 / a3 实验，检查：
   - cancel_task 失败次数 < 3
   - 同时活跃任务数 <= 4
   - coverage > 0.8

---

## Phase 2: SendMessageTool 防护 + TaskStore 自动 cancel（预计 2 小时）

### 目标
在框架层强制阻止重复 dispatch，并消除 `unknown_task_id` 错误。

### 改动文件
- `src/a2a/builtin_tools/send_message.py`
- `src/a2a/coordinator/task_store.py`
- `tests/test_send_message_tool.py`（新增测试）

### 具体改动

#### 2.1 SendMessageTool 新增 `_check_worker_busy` 方法

在 `SendMessageTool` 类中添加：

```python
async def _check_worker_busy(self, who: str) -> ToolResult | None:
    """检查 worker 是否有活跃任务，有则返回错误。
    
    返回 None 表示 worker 空闲，可以继续 dispatch。
    返回 ToolResult 表示 worker 忙碌，包含具体任务信息。
    """
    # 从 plan 中查找该 worker 的活跃任务
    active_tasks = []
    for node in self._store.get_plan():
        if node.worker_id == who and node.state in ("running", "pending", "dispatched"):
            active_tasks.append(node.task_id)
    
    if active_tasks:
        return ToolResult(
            success=False,
            content=(
                f"Worker '{who}' already has active task(s): {active_tasks}. "
                f"Cancel the existing task(s) before dispatching a new one. "
                f"Use send_message(message_type='cancel_task', related_task_id='{active_tasks[0]}') to cancel."
            ),
            error="worker_busy",
        )
    return None
```

#### 2.2 在 `_handle_assign_task` 中调用防护

在 `_handle_assign_task` 的 worker 验证后插入：

```python
# NEW: Check if worker already has active tasks
busy_check = await self._check_worker_busy(who)
if busy_check is not None:
    return busy_check
```

#### 2.3 TaskStore 新增 `get_active_tasks_by_worker` 方法

```python
def get_active_tasks_by_worker(self, worker_id: str) -> list[str]:
    """返回指定 worker 的活跃任务 ID 列表（running/pending/dispatched）。"""
    active = []
    for node in self._plan:
        if node.worker_id == worker_id and node.state in ("running", "pending", "dispatched"):
            active.append(node.task_id)
    return active
```

#### 2.4 TaskStore `update_plan` 自动标记 removed 为 canceled

在 `update_plan` 的 `removed` 计算后添加：

```python
# NEW: 自动标记 removed 任务为 canceled
for task_id in removed:
    node = old_by_id.get(task_id)
    if node is not None and node.state not in ("done", "failed", "verified", "canceled"):
        node.state = "canceled"
        # 从 _results 中移除（避免干扰结果查询）
        self._results.pop(task_id, None)
```

#### 2.5 新增测试 `tests/test_send_message_tool.py`

```python
"""Tests for SendMessageTool worker busy protection."""

import pytest
from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.agent_registry import AgentRegistry


class TestWorkerBusyProtection:
    """Test that SendMessageTool prevents duplicate dispatch to busy workers."""

    @pytest.mark.asyncio
    async def test_assign_task_to_busy_worker_returns_error(self):
        """Dispatching to a worker with active tasks should fail."""
        store = TaskStore("test request", router=None)
        registry = AgentRegistry()
        registry.register_agent("Alice", "http://localhost:8001", ["sar"])
        
        # Simulate an active task for Alice
        store.add_adhoc_node("alice-task-1", worker_id="Alice", description="Active task")
        store.set_state("alice-task-1", "running")
        
        tool = SendMessageTool(store, registry)
        result = await tool.execute(
            message_type="assign_task",
            who="Alice",
            content="New task",
        )
        
        assert not result.success
        assert result.error == "worker_busy"
        assert "alice-task-1" in result.content

    @pytest.mark.asyncio
    async def test_assign_task_to_idle_worker_succeeds(self):
        """Dispatching to an idle worker should succeed."""
        store = TaskStore("test request", router=None)
        registry = AgentRegistry()
        registry.register_agent("Bob", "http://localhost:8002", ["sar"])
        
        tool = SendMessageTool(store, registry)
        # Note: This will fail at dispatch level (no real worker), but should pass busy check
        result = await tool.execute(
            message_type="assign_task",
            who="Bob",
            content="New task",
        )
        
        # Should not fail with worker_busy
        assert result.error != "worker_busy"

    @pytest.mark.asyncio
    async def test_update_plan_marks_removed_as_canceled(self):
        """Removed plan nodes should be marked as canceled."""
        store = TaskStore("test request", router=None)
        
        # Add initial plan
        store.update_plan([
            {"task_id": "task-1", "worker_id": "Alice", "description": "Task 1"},
            {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
        ])
        store.set_state("task-1", "running")
        
        # Update plan, removing task-1
        result = store.update_plan([
            {"task_id": "task-2", "worker_id": "Bob", "description": "Task 2"},
        ])
        
        assert "task-1" in result["removed"]
        node = store.get_node("task-1")
        assert node.state == "canceled"
```

### 验证方法
1. `uv run pytest tests/test_send_message_tool.py -x -q`
2. `uv run pytest tests/ -x -q --ignore=tests/test_render_report.py`
3. 运行 scene 1 / a3 实验，检查：
   - 重复 dispatch 被 100% 阻止
   - `unknown_task_id` 错误消除

---

## Phase 3: 状态同步修复（预计 1-2 小时）

### 目标
消除 `UNKNOWN` 状态，确保 coordinator 和 worker 状态一致。

### 改动文件
- `src/a2a/coordinator/agent_executor.py`
- `src/a2a/coordinator/server.py`
- `src/a2a/coordinator/task_store.py`

### 具体改动

#### 3.1 TaskStore 新增 `sync_task_states` 方法

```python
def sync_task_states(self, worker_states: dict[str, str]) -> list[str]:
    """同步 worker 端任务状态到 coordinator plan。
    
    Args:
        worker_states: {task_id: state} 从 worker 端查询到的实际状态
        
    Returns:
        状态发生变化的 task_id 列表
    """
    changed = []
    for node in self._plan:
        if node.task_id in worker_states:
            actual_state = worker_states[node.task_id]
            if node.state != actual_state:
                node.state = actual_state
                changed.append(node.task_id)
    return changed
```

#### 3.2 CoordinatorAgentExecutor 定期同步

在 `CoordinatorAgentExecutor` 中添加：

```python
async def _periodic_state_sync(self) -> None:
    """定期同步 worker 任务状态到 coordinator plan。"""
    while True:
        await asyncio.sleep(5)  # 每 5 秒同步一次
        
        # 收集所有活跃任务的 worker_task_id
        active_nodes = [
            n for n in self._store.get_plan() 
            if n.state in ("running", "pending", "dispatched")
        ]
        
        if not active_nodes:
            continue
            
        # 通过 A2A 查询 worker 端状态
        worker_states = {}
        for node in active_nodes:
            worker_task_id = self._store._dispatch_to_worker.get(node.task_id)
            if worker_task_id:
                try:
                    # 查询 worker 端状态
                    state = await self._query_worker_task_state(worker_task_id)
                    worker_states[node.task_id] = state
                except Exception:
                    # Worker 离线，标记为 unreachable
                    worker_states[node.task_id] = "unreachable"
        
        # 同步到 plan
        changed = self._store.sync_task_states(worker_states)
        if changed:
            logger.info(f"Task states synced: {changed}")
```

#### 3.3 在 server lifespan 中启动同步任务

在 `src/a2a/coordinator/server.py` 的 `lifespan` 中：

```python
# 启动时
await self._start_cleanup_task()
# NEW: 启动状态同步任务
sync_task = asyncio.create_task(self._periodic_state_sync())

yield

# 关闭时
sync_task.cancel()
await self._stop_cleanup_task()
```

### 验证方法
1. `uv run pytest tests/ -x -q --ignore=tests/test_render_report.py`
2. 运行 scene 1 / a3 实验，检查：
   - `UNKNOWN` 状态消除
   - 所有任务状态可追踪

---

## Phase 4: 综合验证 + 文档更新（预计 1 小时）

### 验证场景

| 场景 | Agents | 验证点 |
|------|--------|--------|
| scene 1 / a3 | 3 | 核心验证：任务不堆积，cancel 不失败，coverage > 0.8 |
| scene 1 / a4 | 4 | 回归验证：多人救援正常，无重复 dispatch 阻止误伤 |
| scene 2 / a3 | 3 | 复杂场景验证 |
| scene 2 / a4 | 4 | 回归验证 |

### 成功标准

1. **任务堆积消除**: 同一 worker 同时活跃任务数 <= 1
2. **cancel_task 成功率**: > 95%
3. **重复 dispatch 阻止率**: 100%（框架层强制）
4. **Coverage**: scene 1 / a3 > 0.8, scene 2 / a3 > 0.8
5. **多人救援**: 4 agents 场景正常完成
6. **UNKNOWN 状态消除**: 所有任务状态可追踪
7. **无回归**: 现有测试全部通过

### 文档更新

1. 更新 `docs/plans/2026-07-19-3agents-failure-analysis.md` 添加实施结果
2. 更新 `docs/plans/2026-07-19-coordinator-task-management-solutions.md` 标记完成状态
3. 更新 `docs/system_docs/框架.md` 添加任务管理防护说明
4. Commit 所有改动

---

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| Prompt 强化被 LLM 忽略 | 任务堆积仍发生 | Phase 2 框架层强制阻止 |
| 重复 dispatch 阻止误伤并行任务 | 多人救援失败 | `_check_worker_busy` 只检查 running/pending，不检查 done |
| 状态同步增加网络开销 | 性能下降 | 同步间隔 5 秒，可配置 |
| update_plan 自动 cancel 影响依赖任务 | 任务依赖断裂 | 只标记非 terminal 状态的任务为 canceled |

---

## Commit 计划

1. `feat(coordinator): Phase 1 — Prompt 强化任务管理规则`
2. `feat(coordinator): Phase 2 — SendMessageTool 重复 dispatch 防护 + TaskStore 自动 cancel`
3. `feat(coordinator): Phase 3 — 任务状态定期同步`
4. `test(coordinator): Phase 4 — 综合验证 + 文档更新`
