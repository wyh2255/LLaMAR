# 3 Agents 失败根因分析报告

**日期**: 2026-07-19
**分析对象**: scene 1 / a3 (20260719_135253_s1_s42_a3), scene 2 / a3 (20260719_140447_s2_s42_a3)

## 核心问题：Coordinator 任务管理混乱

### 1. 任务堆积（Task Pile-up）

**现象**: 同一 agent 被分配多个任务，旧任务未取消，新任务又 dispatch

**证据** (scene 1 / a3, step 23):
```
Active: 9 tasks
  ▶️ alice-suppress-caldor (Alice): UNKNOWN
  ▶️ bob-suppress-great (Bob): UNKNOWN
  ▶️ charlie-suppress-great (Charlie): RUNNING
  ▶️ rescue-timmy (): UNKNOWN
  ▶️ bob-suppress-caldor (Bob): RUNNING
  ▶️ alice-suppress-great (Alice): RUNNING
  ▶️ charlie-rescue-timmy (Charlie): RUNNING
  ▶️ alice-rescue-timmy (Alice): RUNNING
  ▶️ bob-fire-caldor-regions (Bob): RUNNING
```

Alice 同时有 3 个任务，Bob 有 3 个任务，Charlie 有 2 个任务。

### 2. cancel_task 失败 — `unknown_task_id`

**现象**: Coordinator 尝试取消旧任务，但系统返回 `unknown_task_id`

**证据** (17 次失败):
```
send_message(message_type="cancel_task", related_task_id="alice-fire-caldor")
→ Error: unknown_task_id
```

**根因**: `update_plan` 的 `Removed` 列表把任务从 plan 中移除，但 worker 端的任务还在运行。后续 `cancel_task` 找不到这些任务。

### 3. 重复 dispatch 相同目标

**现象**: 同一 agent 被重复分配相同目标，只是 task_id 不同

**证据**:
- `alice-fire-caldor` → `alice-suppress-caldor` → `alice-rescue-timmy`（Alice 被反复切换任务）
- `bob-fire-great` → `bob-suppress-great` → `bob-suppress-caldor` → `bob-fire-caldor-regions`（Bob 被反复切换）

### 4. 任务状态 UNKNOWN

**现象**: 大量任务状态为 `UNKNOWN`，coordinator 无法追踪

**证据**:
```
alice-suppress-caldor (Alice): UNKNOWN
bob-suppress-great (Bob): UNKNOWN
```

**根因**: 任务被 cancel 后状态未正确更新，或 worker 端任务已结束但 coordinator 未收到通知。

## 为什么 4 Agents 成功而 3 Agents 失败？

| 维度 | 3 Agents | 4 Agents |
|------|----------|----------|
| 任务复杂度 | 2 fires + 1 person = 3 个并行目标 | 2 fires + 1 person = 3 个并行目标 |
| Agent 数量 | 3 | 4 |
| 任务分配 | 每个 agent 需处理多个目标，频繁切换 | 每个 agent 专注一个目标，Charlie+David 专职救援 |
| Coordinator 决策 | 频繁 cancel/re-dispatch，任务堆积 | 一次 dispatch，任务清晰 |
| finish_task 调用 | 1 次（仅 Charlie） | 5 次（所有 agents） |

## 关键差异：4 Agents 的任务分配策略

**Scene 1 / a4 成功关键**:
- Alice: 专职灭火（CaldorFire）
- Bob: 专职灭火（GreatFire）
- Charlie: 专职救援（carry Timmy）
- David: 专职救援（carry Timmy）

**3 Agents 失败关键**:
- Alice: 灭火 → 救援 → 灭火（反复切换）
- Bob: 灭火 → 救援 → 灭火（反复切换）
- Charlie: 探索 → 灭火 → 救援（反复切换）

## 修复建议

### 1. Coordinator Prompt 强化（短期）

在 coordinator system prompt 中强调：
```
- **NEVER re-dispatch to an agent with an active task**: If an agent is RUNNING, do NOT assign a new task. Wait for completion or cancel first.
- **Cancel before re-dispatch**: Always `cancel_task` the old task before `assign_task` a new one to the same agent.
- **One agent, one mission**: Assign each agent a single clear mission (e.g., "extinguish CaldorFire" or "rescue Timmy"). Do not switch missions mid-task.
```

### 2. Coordinator 工具层修复（中期）

在 `SendMessageTool` 中增加防护：
```python
async def _handle_assign_task(self, args):
    who = args["who"]
    # 检查该 agent 是否已有 RUNNING 任务
    existing_tasks = self._task_store.get_tasks_by_worker(who, states=["RUNNING", "DISPATCHED"])
    if existing_tasks:
        return f"Error: {who} already has active task(s): {[t.id for t in existing_tasks]}. Cancel first."
```

### 3. update_plan 语义修复（中期）

`update_plan` 的 `Removed` 应该自动触发 `cancel_task`，而不仅仅是从 plan 中移除。

### 4. 任务状态同步修复（长期）

确保 cancel_task 后，worker 端状态正确同步到 coordinator，避免 `UNKNOWN` 状态。

## 结论

3 agents 失败不是框架问题，而是 **coordinator LLM 的任务管理策略问题**。4 agents 成功是因为任务分配简单清晰，3 agents 失败是因为 coordinator 频繁切换任务导致混乱。

**修复优先级**:
1. **Prompt 层**: 强化 "one agent, one mission" 和 "cancel before re-dispatch"
2. **框架层**: `SendMessageTool` 增加重复 dispatch 防护
3. **框架层**: `update_plan` 自动 cancel removed tasks
