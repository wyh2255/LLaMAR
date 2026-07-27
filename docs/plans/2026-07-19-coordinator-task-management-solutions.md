# Coordinator 任务管理混乱 — 综合解决方案

**日期**: 2026-07-19
**问题**: 3 agents 场景下 coordinator 频繁 cancel/re-dispatch，导致任务堆积、cancel_task 失败、重复 dispatch

---

## 方案 A：Coordinator Prompt 强化（短期，立即可实施）

**目标**: 通过 prompt 约束 LLM 行为，减少任务切换频率

**改动文件**: `sar_orch/prompts/coordinator/system.semantic.md`

### A1. 新增 "Task Assignment Discipline" 章节

在 `## Critical Rules` 后插入：

```markdown
## Task Assignment Discipline (CRITICAL — prevents chaos)

**One agent, one mission**: Each agent should have ONE clear mission at a time. Do NOT switch an agent's mission mid-task unless the current task is complete or has failed.

**NEVER re-dispatch to an agent with an active task**: If an agent shows RUNNING or DISPATCHED in Context Memory, do NOT assign them a new task. Wait for completion or explicitly cancel first.

**Cancel before re-dispatch**: If you must change an agent's mission, ALWAYS `cancel_task` the old task BEFORE `assign_task` the new one. Check Context Memory to confirm CANCELED state before dispatching.

**Phase-based assignment**: Assign tasks by phase:
1. **Exploration phase** (steps 1-5): All agents explore
2. **Firefighting phase** (steps 6-25): Assign agents to specific fires — one agent per fire region
3. **Rescue phase** (steps 20+): Assign 2+ agents to rescue each person — these agents should NOT be fighting fires simultaneously

**3-agent special rule**: With only 3 agents, you have LIMITED parallelism. Prioritize:
- Agent 1: Firefighting (CaldorFire)
- Agent 2: Firefighting (GreatFire)  
- Agent 3: Exploration → then assist with firefighting or rescue
Do NOT reassign Agent 1 or Agent 2 to rescue until their fire is fully extinguished.
```

### A2. 强化 "Canceling and Re-dispatching" 章节

在 `## Canceling and Re-dispatching` 后添加：

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

### A3. 预期效果

- 减少 50%+ 的 cancel/re-dispatch 操作
- 消除任务堆积（9 active tasks → 3-4 active tasks）
- 消除 `unknown_task_id` 错误

### A4. 风险

- LLM 可能仍忽略 prompt 约束（LLM 行为不确定性）
- 需要验证 prompt 长度增加对 token 消耗的影响

---

## 方案 B：SendMessageTool 框架层防护（中期，推荐）

**目标**: 在工具层强制阻止重复 dispatch，不依赖 LLM 自律

**改动文件**: `src/a2a/builtin_tools/send_message.py`

### B1. 新增 `_check_worker_busy` 方法

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
        if node.worker_id == who and node.state in ("running", "pending"):
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

### B2. 在 `_handle_assign_task` 中调用

在 `_handle_assign_task` 的 worker 验证后插入：

```python
async def _handle_assign_task(
    self,
    content: str | None,
    who: str | None,
    related_task_id: str | None,
) -> ToolResult:
    if not who:
        return ToolResult(
            success=False,
            content="assign_task requires `who` (target worker ID).",
            error="missing_who",
        )
    if not content:
        return ToolResult(
            success=False,
            content="assign_task requires `content` (task instruction).",
            error="missing_content",
        )
    # Validate that target worker exists in registry early
    try:
        self._registry.get(who)
    except AgentNotFoundError:
        return ToolResult(
            success=False,
            content=f"Worker '{who}' not found in registry.",
            error="worker_not_found",
        )
    
    # NEW: Check if worker already has active tasks
    busy_check = await self._check_worker_busy(who)
    if busy_check is not None:
        return busy_check
    
    return await self._dispatch_tool.execute(
        agent_id=who,
        prompt=content,
        task_id=related_task_id,
    )
```

### B3. 预期效果

- 100% 阻止重复 dispatch（框架层强制）
- 消除任务堆积
- 提供清晰的错误信息，指导 LLM 先 cancel

### B4. 风险

- 可能误伤合法的并行任务（如多人救援需要 2 agents 同时 carry）
- 需要处理 `update_plan` 的 `Removed` 任务状态同步

**缓解**: `_check_worker_busy` 只检查 `state in ("running", "pending")`，不检查 `done`/`failed`/`verified`。

---

## 方案 C：update_plan 自动 cancel removed tasks（中期）

**目标**: 消除 `unknown_task_id` 错误，保持 plan 与实际任务状态一致

**改动文件**: `src/a2a/coordinator/task_store.py`

### C1. 新增 `cancel_removed_tasks` 回调

在 `TaskStore.update_plan` 中添加：

```python
def update_plan(self, plan: list[dict[str, Any]]) -> dict[str, Any]:
    """替换整个计划，返回 diff（added/removed/modified）。
    
    已执行节点的 state/result/retry_count 会被保留。
    新增：removed 任务自动标记为 canceled。
    """
    # ... 现有代码 ...
    
    removed = [n.task_id for n in self._plan if n.task_id not in new_ids]
    
    # NEW: 自动标记 removed 任务为 canceled
    for task_id in removed:
        node = old_by_id.get(task_id)
        if node is not None and node.state not in ("done", "failed", "verified"):
            node.state = "canceled"
            # 从 _results 中移除（避免干扰结果查询）
            self._results.pop(task_id, None)
    
    # ... 现有代码 ...
```

### C2. 新增 `get_active_tasks_by_worker` 方法

```python
def get_active_tasks_by_worker(self, worker_id: str) -> list[str]:
    """返回指定 worker 的活跃任务 ID 列表（running/pending/dispatched）。"""
    active = []
    for node in self._plan:
        if node.worker_id == worker_id and node.state in ("running", "pending", "dispatched"):
            active.append(node.task_id)
    return active
```

### C3. 预期效果

- `update_plan` 的 `Removed` 任务自动标记为 `canceled`
- 后续 `cancel_task` 能找到这些任务（状态为 canceled）
- 消除 `unknown_task_id` 错误

### C4. 风险

- 需要确保 `cancel_task` 能正确处理 `canceled` 状态的任务
- 可能影响依赖 removed 任务的其他任务

---

## 方案 D：任务状态同步修复（长期）

**目标**: 消除 `UNKNOWN` 状态，确保 coordinator 和 worker 状态一致

**改动文件**: `src/a2a/coordinator/agent_executor.py`, `src/a2a/coordinator/server.py`

### D1. 根因分析

`UNKNOWN` 状态出现的原因：
1. `update_plan` 的 `Removed` 把任务从 plan 中移除，但 `_dispatch_to_worker` 映射还在
2. Worker 端任务已结束，但 coordinator 未收到 push notification
3. `cancel_task` 后，worker 端状态未同步回 coordinator

### D2. 修复方案

在 `CoordinatorAgentExecutor` 中添加状态同步：

```python
async def _sync_task_states(self) -> None:
    """定期同步 worker 任务状态到 coordinator plan。"""
    for node in self._store.get_plan():
        if node.state in ("running", "pending", "dispatched"):
            # 检查 worker 端实际状态
            worker_task_id = self._store._dispatch_to_worker.get(node.task_id)
            if worker_task_id:
                # 通过 A2A 查询 worker 端状态
                actual_state = await self._query_worker_task_state(worker_task_id)
                if actual_state != node.state:
                    node.state = actual_state
```

### D3. 预期效果

- 消除 `UNKNOWN` 状态
- Coordinator 始终看到准确的 worker 任务状态

### D4. 风险

- 增加网络开销（定期查询 worker 状态）
- 需要处理 worker 离线场景

---

## 综合实施计划

### Phase 1：Prompt 强化（立即，30 分钟）

1. 修改 `sar_orch/prompts/coordinator/system.semantic.md`
2. 添加 "Task Assignment Discipline" 和强化 "Canceling" 章节
3. 运行 scene 1 / a3 验证

**验证标准**:
- cancel_task 失败次数 < 3
- 同时活跃任务数 <= 4
- scene 1 / a3 coverage > 0.8

### Phase 2：框架层防护（1-2 小时）

1. 修改 `src/a2a/builtin_tools/send_message.py`
2. 添加 `_check_worker_busy` 方法
3. 修改 `src/a2a/coordinator/task_store.py`（方案 C）
4. 运行全部测试 + scene 1 / a3 验证

**验证标准**:
- 重复 dispatch 被 100% 阻止
- `unknown_task_id` 错误消除
- 多人救援场景正常工作

### Phase 3：状态同步（长期，可选）

1. 修改 `src/a2a/coordinator/agent_executor.py`
2. 添加 `_sync_task_states` 方法
3. 验证 `UNKNOWN` 状态消除

---

## 推荐方案组合

| 优先级 | 方案 | 改动量 | 预期效果 | 风险 |
|--------|------|--------|----------|------|
| P0 | A: Prompt 强化 | 小 | 减少 50% 任务切换 | LLM 可能忽略 |
| P1 | B: SendMessageTool 防护 | 中 | 100% 阻止重复 dispatch | 可能误伤并行任务 |
| P2 | C: update_plan 自动 cancel | 中 | 消除 unknown_task_id | 需处理 canceled 状态 |
| P3 | D: 状态同步 | 大 | 消除 UNKNOWN 状态 | 增加网络开销 |

**推荐**: 先实施 A（立即见效），再实施 B+C（框架保障），D 作为长期优化。

---

## 验证计划

### 测试场景

| 场景 | Agents | 验证点 |
|------|--------|--------|
| scene 1 / a3 | 3 | 核心验证：任务不堆积，cancel 不失败 |
| scene 1 / a4 | 4 | 回归验证：多人救援正常 |
| scene 2 / a3 | 3 | 复杂场景验证 |
| scene 2 / a4 | 4 | 回归验证 |

### 成功标准

1. **任务堆积消除**: 同一 worker 同时活跃任务数 <= 1
2. **cancel_task 成功率**: > 95%
3. **Coverage**: scene 1 / a3 > 0.8, scene 2 / a3 > 0.8
4. **多人救援**: 4 agents 场景正常完成
5. **无回归**: 现有测试全部通过
