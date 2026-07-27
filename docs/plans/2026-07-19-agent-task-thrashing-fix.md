# 2026-07-19 — Agent 任务抖动修复 (Task Thrashing Fix)

## 问题描述

3-agent SAR 实验 (scene 1, agents=3) 中，coordinator 反复 dispatch 同一任务给同一 worker，导致：
- `unknown_task_id` 错误：worker 收到不存在的任务 ID
- 任务状态不一致：coordinator 认为任务已 dispatch，worker 却找不到
- 实验卡在 Step 0，无法推进

## 根本原因

1. **重复 dispatch**：coordinator 在 LLM 循环中反复发送相同任务，没有检查 worker 是否已有活跃任务
2. **状态同步缺失**：coordinator 和 worker 之间的任务状态没有定期同步，导致 `UNKNOWN` 状态
3. **update_plan 状态残留**：移除的任务节点没有标记为 `canceled`，残留为 `UNKNOWN`
4. **worker 注册延迟**：coordinator 在 worker 还没注册完成时就尝试 dispatch

## 修复方案（4 层防御）

### Phase 1: Prompt 强化 (commit f531479)
- 文件：`sar_orch/prompts/coordinator/system.semantic.md`
- 内容：添加明确的任务管理规则
  - 每个 worker 同时只能执行一个任务
  - 重复 dispatch 前必须先 cancel
  - 使用 `query_task_events` 查询任务状态
  - 避免无限探索循环

### Phase 2: SendMessageTool 防护 + TaskStore 自动 cancel (commit d51eaef)
- 文件：`src/a2a/builtin_tools/send_message.py`
  - 新增 `_check_worker_busy(who)` 方法
  - 在 `_handle_assign_task` 中，dispatch 前检查 worker 是否有活跃任务
  - 有活跃任务则返回 `worker_busy` 错误，提示先 cancel
- 文件：`src/a2a/coordinator/task_store.py`
  - 新增 `get_active_tasks_by_worker(worker_id)` 方法
  - `update_plan` 中 removed 节点自动标记 `state="canceled"`
- 文件：`tests/test_send_message_tool.py`
  - 新增 3 个测试：`test_assign_task_to_busy_worker_returns_error`、`test_assign_task_to_idle_worker_succeeds`、`test_update_plan_marks_removed_as_canceled`

### Phase 3: 状态同步修复 (commit ba907ce)
- 文件：`src/a2a/coordinator/agent_executor.py`
  - 新增 `sync_task_states()` 方法：定期同步 coordinator 和 worker 的任务状态
  - 新增 `_periodic_state_sync()` 协程：每 10s 执行一次状态同步
  - 在 `lifespan` 中启动同步任务
- 文件：`src/a2a/coordinator/server.py`
  - 在 `create_server` 中注入 `agent_executor` 到 lifespan
- 文件：`src/a2a/coordinator/task_store.py`
  - 新增 `get_task_state(task_id)` 方法

### Phase 4: 修复 worker_busy 误报 + 增加注册等待 (commit 63071df)
- 文件：`src/a2a/builtin_tools/send_message.py`
  - `_check_worker_busy` 只检查 `running/dispatched` 状态，不再检查 `pending`
  - `pending` 只是 coordinator 计划状态，worker 可能尚未收到，不应视为忙碌
- 文件：`sar_orch/experiment.py`
  - worker 注册等待从 2.0s 增加到 5.0s，减少 `task_not_routable_yet` 错误

## 验证结果

### 测试
- 全量测试：**798 passed, 1 skipped**（修复前 775 passed）

### 实验验证 (scene 1, agents=3, seed=42, max_steps=30)
| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| `unknown_task_id` | 频繁出现 | **0 次** |
| `worker_busy` | N/A | 13 次（初始 dispatch 被正确阻止） |
| `task_not_routable_yet` | 68 次 | **0 次** |
| 实验推进 | 卡在 Step 0 | **推进到 Step 8/30** |
| Coverage | 0.00 | **0.67** |

## 后续优化建议

1. **worker_busy 深层原因**：coordinator 在 worker 执行长任务时尝试 dispatch 新任务，被防护阻止。建议：
   - 在 prompt 中明确告知 coordinator：worker 执行任务时不要 dispatch 新任务
   - 或增加任务队列机制，允许排队 dispatch

2. **任务取消机制**：`task_not_routable_yet` 在 cancel 时仍可能出现（worker 还没收到任务）。建议：
   - cancel 时如果任务还没 dispatch，直接从 plan 中移除，不发送 cancel 消息

3. **状态同步频率**：当前 10s 同步一次，可根据实验长度调整

## 相关文件

- `src/a2a/builtin_tools/send_message.py` — SendMessageTool 防护逻辑
- `src/a2a/coordinator/task_store.py` — TaskStore 状态管理
- `src/a2a/coordinator/agent_executor.py` — 状态同步逻辑
- `src/a2a/coordinator/server.py` — lifespan 集成
- `sar_orch/prompts/coordinator/system.semantic.md` — Prompt 规则
- `sar_orch/experiment.py` — 实验启动时序
- `tests/test_send_message_tool.py` — 测试用例

## 提交记录

| Phase | Commit | 描述 |
|-------|--------|------|
| 1 | f531479 | Prompt 强化任务管理规则 |
| 2 | d51eaef | SendMessageTool 重复 dispatch 防护 + TaskStore 自动 cancel |
| 3 | ba907ce | 任务状态定期同步 |
| 4 | 63071df | 修复 worker_busy 误报 + 增加 worker 注册等待 |
