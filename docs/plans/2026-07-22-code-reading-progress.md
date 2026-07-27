---
日期: 2026-07-22
文档类型: 代码阅读进度交接文档
文档概述: 记录 LLaMAR Coordinator 编排链路的代码精读进度、关键结论、未提交改动与下一步阅读计划，供新 session 继续阅读使用
---

# Coordinator 编排链路代码精读 — 进度交接

## 阅读主线

一次真实 agentic mission 的执行顺序，从 Coordinator 启动到 Worker 回调的完整链路。

## 已完成的段落

| 段 | 内容 | 关键文件 |
|---|---|---|
| 1 | Coordinator 启动：唯一 `MissionRuntimeManager`、`TeamPartitionService`、`TaskWatchdog` 创建，lifespan recovery | `src/a2a/coordinator/server.py:250-465` |
| 2 | 新 mission 准入：`admit(context_id)` 必须先于 `EventStore.clear()`；并发 mission 拒绝；控制状态持久化（tmp+fsync+rename+0600） | `src/a2a/coordinator/agent_executor.py:305-354`、`src/a2a/coordinator/mission_runtime.py:1285-1320` |
| 3 | `TaskStore`（执行侧工作台）vs `StateProvider`（观察侧只读视图）职责区分 | `src/a2a/coordinator/task_store.py:111-160`、`sar_orch/coordinator_state_provider.py:21-73` |
| 4 | `StateProvider.snapshot()` → Context Memory：Mission DAG 视图、Physical Dispatch 视图、supervision；物理状态优先于 EventStore | `sar_orch/coordinator_state_provider.py:252-291,504-566`、`src/Agent/router_agent/context.py:167-225` |
| 5 | LLM `update_plan` → `MissionGraph.replace()`：全量校验 → active 冻结 → 合并/重置 → frontier 重算 → legacy PlanNode 兼容视图 | `src/a2a/builtin_tools/update_plan.py`、`src/a2a/coordinator/mission_graph.py:256-400` |
| 6 | 节点激活：`activate_plan_node` 四阶段（DAG gate → atomic claim → Team ACK saga → awaited fan-out）；单参与者不走 TeamPartition（`len>1` 条件） | `src/a2a/coordinator/mission_runtime.py:603-740`、`src/a2a/builtin_tools/send_message.py:393-471` |

## 穿插讨论的核心结论

- **组件分工**：`MissionRuntimeManager`（mission 生命周期）、`MissionRuntime`（物理 dispatch 状态权威）、`TeamPartitionService`（worker 独占分组）、`TaskWatchdog`（监控不决策）、`SupervisionStateStore`（告警存储）
- **EventStore 不是状态权威**：callback 先经 runtime 路由校验、更新 MissionRuntime，再写 EventStore；EventStore 只是历史/证据
- **两套账本**：MissionGraph（逻辑真相）与 legacy PlanNode（兼容视图）非原子双写，可能不一致
- **按需规划（用户核心立场，已确认正确）**：信息不足时写不出合法 DAG；当前系统已是隐式两阶段（图为空→自由派发，图非空→`undeclared_task` 强约束，`dispatch_task.py:88-95`）；占位节点方案被否决（错误信息比没有信息更有害）
- **工具面收敛方向**：activate 独立、assign 降级为兼容/逃生舱、cancel 应升级为节点级 `cancel_plan_node`、邮箱用已有的 `send_mail`

## 本轮已完成但未提交的改动

1. **update_plan 增强反馈**：`MissionGraph.replace()` 返回权威 diff（added/removed/modified 字段级/preserved/frozen/reset）；content 输出多行详情（revision、Ready now、State）
2. **mission_graph.jsonl**：每次 replace 成功写一行（ts/step/revision/changed/diff/全量节点），经 `SARCoordinatorStateProvider.set_task_store()` 接线到 `logs/<run>/mission_graph.jsonl`
3. **图模式显式切换信号**：图从空→非空时反馈追加 "Graph mode active"；三份 prompt 改为两阶段说明（Explore first → Plan when ready）
4. **测试**：92 项通过（`tests/test_phase2_update_plan.py`、`tests/test_mission_graph.py`、`tests/test_mission_graph_history.py`）；改动文件 ruff 全过

改动文件：`src/a2a/coordinator/mission_graph.py`、`src/a2a/coordinator/task_store.py`、`src/a2a/builtin_tools/update_plan.py`、`sar_orch/coordinator_state_provider.py`、`sar_orch/coordinator.py`、`sar_orch/prompts/coordinator/system{,.semantic,.oracle}.md`、`tests/test_phase2_update_plan.py`、`tests/test_mission_graph_history.py`（新增）

**注意**：工作区还有与本任务无关的既有改动（docs/、sar_orch/docs_alignment/），提交时只 stage 上述文件。仓库既有 8 个 ruff 错误在无关文件（`evolve_alignment.py`、`launch_dashboard.py`、`map_agent/server.py`）。

## 下一步（第 7 段）

Worker 侧执行与回流：

- Worker 收到 dispatch 后如何执行（`sar_orch/worker.py`）
- observation 如何经 `[DATA]` 块回流到 SemanticMapStore（`src/a2a/coordinator/server.py:782-900` push-callback → `_extract_observation_from_status_text` → `ingest_observation`）
- physical terminal 如何聚合回 MissionGraph（`mark_dispatch_terminal`）并触发下游节点 ready
- deferred assignment 在 terminal 后的自动激活（`task_store.py:513-591`）

## 后续可选议题

- 实现 `cancel_plan_node`（节点级取消，强制补偿）
- `activate_plan_node` 独立成工具 + deferred 语义并入
- reply/cancel 反馈对齐 update_plan 风格（带节点状态变化）
- 阶段 2/3/4 实验（smoke、10 组交叉矩阵、行为对比）——见对话中的实验计划

## 新 session 恢复提示词

> 继续阅读 LLaMAR Coordinator 编排链路，进度见 docs/plans/2026-07-22-code-reading-progress.md，从第 7 段（Worker 侧执行与 observation 回流）开始，按执行顺序逐段讲解。
