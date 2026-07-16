---
日期: 2026-07-06
文档类型: 设计文档
文档概述: event_flow.md 的设计方案 —— 在 data_flow.md 基础上补充代码级事件触发链文档
---

# event_flow.md 设计文档

## 1. 目标与读者

在现有 `docs/system_docs/data_flow.md`（数据/ID 流）基础上，新增 `docs/system_docs/event_flow.md`，聚焦**事件/控制流**的代码级细节。

**目标**：
- 说明一次 SAR 实验中，每个关键动作由什么函数/回调触发
- 覆盖 Future resolve、`TaskState` 转换、SSE 推送、step_callback 事件、threading Event 唤醒等控制面细节
- 与 `data_flow.md` 互补，不重复数据/ID 流内容

**读者**：需要调试以下问题的开发者：
- 某个 agent 为什么没有提交动作
- Coordinator 为什么提前调用 `finish_task`
- Worker push callback 为什么没有到达 Coordinator
- Barrier timeout 发生在哪一步
- 某条日志是在哪个回调里写下的

## 2. 文档结构

### 2.1 元数据头

与 system_docs 其他文档一致，开头包含 YAML 元数据：

```yaml
---
日期: 2026-07-06
文档类型: 技术文档
文档概述: LLaMAR SAR 实验的完整事件触发链 ...
---
```

### 2.2 总览图

一张横向 ASCII 主事件链路图，从 `experiment.py` 启动到实验结束，标注主要组件边界。

### 2.3 端到端主时间线（Phase 0 ~ Phase 10）

按真实执行顺序组织，每个 Phase 包含：
- 触发条件
- 调用栈（文件:函数:行号，行号为当前代码版本最新值）
- 关键事件/回调名称
- 状态变更
- 输出产物（日志文件、CSV、Future、EventStore 记录等）

Phase 划分：

| Phase | 主题 |
|-------|------|
| Phase 0 | 实验初始化：`SARBarrier`、`ExperimentLogger`、`SARCoordinator`、N 个 `SARWorker` 启动 |
| Phase 1 | Coordinator 收到顶层 A2A `SendMessage`，进入 `CoordinatorAgentExecutor.execute` |
| Phase 2 | `AgentController.submit()` 创建/复用 `ContextManager`，`RouterAgent` 开始 ReAct 循环 |
| Phase 3 | Coordinator 查询环境：`query_sar_state` / `query_semantic_map` / `query_team_status` |
| Phase 4 | Coordinator 派发子任务：`dispatch_task` → `Router.send_task_async()` → A2A `send_message(return_immediately=True)` |
| Phase 5 | Worker 接收任务：`on_message_send()` → `AgentAdapter.execute()` → Worker `Agent.run()` |
| Phase 6 | Worker 工具调用 → `barrier.submit_action()` → `_execute_step()` → `SAREnv.step()` |
| Phase 7 | Worker 工具返回 / `AskCoordinatorTool` / `FinishTaskTool` / `NoOp` 处理 |
| Phase 8 | Worker A2A Push Notification → Coordinator `/a2a/push-callback` → `EventStore.append()` |
| Phase 9 | Coordinator `query_task_events()` 收集结果，继续下一轮 dispatch / plan / finish |
| Phase 10 | 实验结束：`poll loop` 退出，写 `run_metrics.json`，清理资源 |

### 2.4 组件事件参考表

按组件分表，每行一个事件，列包括：

| 列 | 说明 |
|---|---|
| 事件名 | 如 `llm_response`、`tool_start`、`dispatch_task`、`submit_action` |
| 触发条件 | 什么情况下触发 |
| 调用栈 | 关键文件:函数:行号 |
| 消费方 | 谁处理这个事件 |
| 产物 | 写入的日志/CSV/JSON/Future/State |

组件：
- Coordinator（RouterAgent、SARCoordinator、_router_cb）
- A2A Transport（Coordinator Server、Worker Server、Router、EventStore、push-callback）
- Worker（AgentAdapter、Worker Agent、step_callback）
- SARBarrier（submit_action、_execute_step、threading Event）
- ExperimentLogger（CSV/JSON 写入）
- UI/SSE（`/map/state` 端点）

### 2.5 特殊流程附录

- **A. INPUT_REQUIRED 暂停/恢复**：`AskCoordinatorTool` → `NeedInputError` → `save_snapshot()` → Push Notification `INPUT_REQUIRED` → `respond_worker()` → `load_snapshot()` → 追加 tool_result
- **B. Barrier timeout NoOp 填充**：60s 未收齐动作 → 自动填充 `NoOp` → `TimeoutAgents` 写入 `trajectory.csv`
- **C. Crash-safe 日志刷新**：poll loop 每步调用 `log_trajectory()` + `flush_summary()`
- **D. Map SSE 状态推送**：`/map/state` 每 500ms 轮询 `barrier.get_env_snapshot()`，step 变化时 SSE 推送

### 2.6 交叉引用

- 指向 `data_flow.md`：说明 `event_flow.md` 是控制流视角补充
- 指向 `logging_map.md`：每个事件产物关联到具体日志文件
- 指向 `框架.md`：组件职责背景

## 3. 实现方式

1. 派多个 subagent 并行深入追踪各组件调用栈
2. 汇总 subagent 输出，提取准确的函数名、回调名、Future/State 名称
3. 按上述结构撰写 `docs/system_docs/event_flow.md`
4. 自审：检查与 `data_flow.md`、`logging_map.md`、源代码的一致性
5. 将本设计文档归档到 `docs/superpowers/specs/`

## 4. 成功标准

- `event_flow.md` 成功写入 `docs/system_docs/`
- 文档覆盖 Phase 0~10 的代码级事件链
- 每个关键事件都给出调用栈或回调来源
- 与现有文档无矛盾
