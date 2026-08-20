# LLaMAR SAR 系统带读交接

> 用途：在新 Hermes 会话中恢复这次慢速、源码证据优先的系统带读。
> 状态：只读学习；未修改源码、未修改 system_docs、未运行实验/服务/测试。

## 1. 冻结的源码边界

- 仓库根目录：`/home/wyh/daily_work/LLaMAR`
- 最近核验 HEAD：`d1dada92b100d078630f321ca49d9d6712a72877`
- 最近分支状态：`main...origin/main [ahead 13, behind 192]`
- 初始工作树：曾有外部文档/流程图改动；本带读不把它们当源码变更。
- 本交接新增：未跟踪目录 `.agents/handovers/`（仅含本文件）；未修改源码或 `docs/system_docs` 正文。
- 本次恢复核验：当前工作树另含论文 Markdown、计划文件和系统流程 SVG；`git status --short -- src sar_orch tests` 为空。

新会话恢复时必须先重新执行：

```text
git status --short --branch
git rev-parse HEAD
```

若 HEAD、分支或目标文件改变，停止使用本交接中的行号作为当前事实，先重新读取直接源码。

## 2. 学习目标与讲解约束

目标：沿默认 SAR 实验主线理解一次完整系统流程，而不是一次性讲完所有模块。

默认主线：

```text
state_mode=semantic
orchestration_mode=agentic
peer_mail 默认关闭
```

讲解规则：中文、一步一跳、先人话再代码；每轮只讲一个函数级跳转；区分：

1. 源码直接事实；
2. 基于调用关系的推断；
3. 实际运行证据。

涉及 Tool 或激活/派发函数时，固定交代：输入、读取/改变的状态、成功/失败反馈，以及下游如何使用该反馈。

本带读已读取历史 SAR trace 作为 `query_task_results` 的实际使用证据；但当前 HEAD 未重新运行实验、服务或测试，绝不能把历史 trace 称为当前版本端到端成功。

## 3. 已完成的主线跳转

```text
sar_orch/experiment.py:run_experiment()
  → 创建 SARBarrier、ExperimentLogger、实验身份/元数据
  → 创建并启动 SARCoordinator
      → SemanticMapStore / SARCoordinatorStateProvider
      → 通用 CoordinatorServer
      → 注入 Barrier、SemanticMap，后台线程 server.run()
  → 创建并启动 N 个 SARWorker
      → 每 Worker 独立 A2A 端口、独立线程/asyncio loop
      → 共享同一 SARBarrier，靠 agent_idx 区分环境槽位
  → SARCoordinator.submit_task()
      → A2A SDK SendMessageRequest
  → Coordinator A2A Server
      → DefaultRequestHandler（SDK）
      → CoordinatorAgentExecutor.execute()
  → 规范化 A2A Task：task_id / context_id / query
  → _execute_agentic()
      → MissionRuntimeManager.admit(context_id)
      → TaskStore + MissionRuntime + StateProvider + Watchdog 装配
  → AgentController.submit()
      → context_id 复用 ContextManager；每轮新建 Router Agent
  → RouterAgent.run()
      → hooks.pre_llm()
      → ContextManager.refresh_runtime_state()
      → SARCoordinatorStateProvider.snapshot(None)
      → CoordinatorContextManager 将 DAG / physical dispatch 投影并渲染为 Context Memory
      → Context Memory 作为 raw history 之后的 user message 送入 LLM
  → Router 首次 LLM 决策 → UpdatePlanTool.execute(plan)
  → TaskStore.replace_mission_graph() → MissionGraph.replace()
      → 完整 plan 校验、逻辑节点/依赖建立、frontier 重算
  → Router send_message(message_type="activate_plan_node", related_task_id=<logical_id>)
  → SendMessageTool._handle_activate_plan_node()
  → MissionRuntime.activate_plan_node()
      → _run_dag_gate() → _run_atomic_claim() → （可选）_run_team_ack_saga()
```

关键源码锚点：

| 主题 | 源码锚点 |
|---|---|
| 实验入口 | `sar_orch/experiment.py:130` `run_experiment()` |
| Coordinator 启动 | `sar_orch/coordinator.py:185` `SARCoordinator.start()` |
| Worker 启动 | `sar_orch/worker.py:246` `SARWorker.start()` |
| 初始 A2A 任务 | `sar_orch/coordinator.py:484` `submit_task()` |
| A2A 协议接线 | `src/a2a/coordinator/a2a_server.py:87-124` |
| Coordinator 执行入口 | `src/a2a/coordinator/agent_executor.py:187` `execute()` |
| Agentic admission | `src/a2a/coordinator/agent_executor.py:305` `_execute_agentic()` |
| 会话与 Agent 生命周期 | `src/Agent/controller/controller.py:114-277` |
| Router LLM 前钩子 | `src/Agent/router_agent/agent.py:509-522`; `hooks.py:70-84` |
| Runtime state 生成 | `sar_orch/coordinator_state_provider.py:186-309` |

## 4. 当前精确暂停点

### 已完成到此处的主线

1. **Context → Router**
   - `mission_dag_view` 和 `physical_dispatches_view` 经 `ContextManager._project_runtime_state_to_pinned()` 进入 `CoordinatorContextManager._render_current_state()`；位于 `### Current State` 内，随 Context Memory 作为 raw history 之后的一条 **user message** 送给 Router，不是 system prompt。
   - DAG 实际显示：`logical_id/state/participant_ids/depends_on`，可选 `objective/failure_reason`；不会显示 provider 中的 `assignments/status/dispatch_count/team_id/terminal_workers`。
   - physical dispatch 实际显示：`dispatch_id/worker_id/state/worker_task_id`，可选 preview；不会显示 `logical_node_id`。
   - 锚点：`src/Agent/router_agent/context.py:203-225,629-720,949-1062`。

2. **结果可见性**
   - `artifact_preview` 超过 80 字符时取前 80 字符并追加 `...`；`result_preview` 只取前 80 字符且**不**追加省略号。二者都不能被当作完整产物。
   - 未完成 dispatch 若已有 `artifact`/`result`，Router 可显式调用 `query_task_events(task_ids=[dispatch_id], timeout=0)`；该工具读取 active `PhysicalDispatch` 并把 evidence 放入 `text`，工具自身无 80 字符截断。若 Worker 尚未 `artifact_update`，则没有可查询详情，须等待或让 Worker 产生中间证据。
   - `QueryTaskResultsTool` 读当前 `TaskStore._results` live dict：指定 `task_ids` 每项最多 2,000 字符；省略 ID 时每项 300 字符摘要。正常 physical 路径的结果可来自终态 `COMPLETED/FAILED/CANCELED` 的 `result` 或 fallback `artifact`，并非严格只含成功完成任务；`set_state(..., result=...)` 还可在非终态写入。
   - 历史真实 SAR trace 已见调用与非空返回：`sar_orch/results/20260721_005150_s1_s42_a2/coordinator/unnamed_task.ndjson:67-68`；该 run 的 `metadata.json:4` 是旧 `code_commit=c1f408d`，因此仅作为历史运行证据。
   - 锚点：`sar_orch/coordinator_state_provider.py:554-584`、`src/a2a/builtin_tools/query_task_events.py:101-126`、`src/a2a/builtin_tools/query_task_results.py:52-83`、`src/a2a/coordinator/task_store.py:406-515,762-783`。

3. **Router 计划 → 逻辑图 → 激活请求**
   - `RouterAgent.run()` 将 LLM 返回的 `tool_calls` 按名称从 `self.tools` 取出并 `await tool.execute(**arguments)`；LLM 是否选择 `update_plan` 是模型决策，工具分派及其后状态变更是确定性的。
   - `UpdatePlanTool` 输入是完整 `plan` 列表（非增量）；反馈为 `ToolResult`，成功携带 diff/snapshot，失败携带校验 error。它调用 `TaskStore.replace_mission_graph()` → `MissionGraph.replace()`，没有派发 Worker。
   - `_validate_specs()` 拒绝空/重复 logical ID、缺失/重复参与者、非法 assignment、self/unknown dependency、环；`_recompute_frontier()` 仅把无依赖或依赖均 `completed`（或声明 `skipped`）的非活动节点标为 `ready`。失败/取消依赖会令下游保持 `blocked`。
   - 锚点：`src/Agent/router_agent/agent.py:506-522,664-740`、`src/a2a/builtin_tools/update_plan.py:135-247`、`src/a2a/coordinator/mission_graph.py:186-247,322-453,476-485,643-665`。

4. **当前已读的激活控制平面**
   - Router 使用 `send_message(message_type="activate_plan_node", related_task_id=<logical_id>)`；输入 ID 是逻辑节点 ID，`who/content` 在该分支被忽略，参与者/目标/assignments 从 MissionGraph 读取。
   - `SendMessageTool._handle_activate_plan_node()` 的成功反馈含 `team_id/team_epoch/dispatches`；失败反馈含稳定 `error/reason`。它本身不发 A2A，转交给 `MissionRuntime.activate_plan_node()`。
   - `_run_dag_gate()` 是只读检查：节点必须 ready、runtime 未 abort、参与者未被活动节点或团队 transition lease 占用；输出 `{pass: True, node}` 或稳定拒绝码。
   - `_run_atomic_claim()` 在锁内复查竞争、预创建每个参与者的 `PREPARED` dispatch、绑定 graph dispatches 并将逻辑节点标为 `activating`；成功反馈 `dispatch_ids/transition/team_id/team_epoch`，失败反馈如 `participant_busy/dispatch_allocation_failed/team_preparation_failed`。尚未发送 A2A。
   - 可选 `_run_team_ack_saga()` 输入 transition/team service/30s timeout；`INSTALLED` 才返回 `team_id/team_epoch`。失败时外层 `_rollback_claim()` 删除 PREPARED dispatch，并将逻辑节点标为 canceled；因此也尚未发送 A2A。
   - 锚点：`src/a2a/builtin_tools/send_message.py:393-472`、`src/a2a/coordinator/mission_runtime.py:603-919`。

### 进度更正：后续会话已越过本节旧的下一跳

`MissionRuntime.dispatch_prepared_many()` 已在 session `20260728_150852_43de63#2913` 完成带读；随后已完成：

```text
RouterAgent.send_task_async()
  → Worker A2A JSON-RPC ingress / ActiveTask
  → AgentAdapter.execute()
  → AgentController.submit()
  → Agent.run() 的 Context Memory、首次 LLM 与通用 Tool 分派
  → AskCoordinatorTool 的 snapshot 暂停—同 task_id 恢复
  → active-runtime callback 的 INPUT_REQUIRED question 可见性追踪
  → QueryTaskEventsTool：默认 active status-only INPUT_REQUIRED 仍不能取回 question；artifact/legacy help_request 是不同通道
```

本文件此前写的“进入 `dispatch_prepared_many()`”只保留为历史导航，不再是当前暂停点。

### 当前精确暂停点（恢复前须重读）

已完成 `GetSkillTool` 与 Worker 版 `FinishTaskTool` 的带读：

```text
GetSkillTool
  → SkillLoader.loaded_skills 内存查询
  → Skill.to_prompt() 完整正文
  → Worker role="tool" history
  → WorkerSARHooks.on_skill_loaded()
  → snapshot 保存 loaded_skills

FinishTaskTool
  → ToolResult(task_complete=True, mission_success=<success>)
  → WorkerSARHooks.post_tool()
  → Agent.run() 返回 RunResult(task_complete=True)
  → AgentAdapter.add_artifact(result.content)
  → TaskUpdater.complete()
```

仍保留的 peer-mail 结论：`A2ASendMailTool` 的 terminal ACK 只说明 receiver mailbox durable 接收；随后 `ReadMailboxTool` 在单锁内选择当前 unread、对且仅对选中的记录追加 read event / 设置 `read_at`、再返回 defensive copies。正文是原文前 500 字符加 `...` 的预览，并不自动回复 sender。

### 当前待验证问题

`FinishTaskTool` 的 `summary` 确定进入 Worker 的 ToolResult 与 `role="tool"` history，但 `Agent.run()` 提前终止时取最近 assistant message 作为 `RunResult.content`；Adapter 又用 `result.content` 写最终 artifact，并无本地 `success/task_complete` 分支。用户本次明确要求记录的候选边界是：业务成功/失败先进入 `RunResult.success`，Adapter 没有继续投影；只有非空 `result.content` 才会 `add_artifact()`，而 `updater.complete()` 不附带 summary。因此最后一条 assistant 消息无文本时，A2A 可能只收到 `COMPLETED` 而没有 artifact。尚不能确认：

```text
Worker summary / mission_success
  → A2A terminal result/artifact
  → Coordinator physical dispatch / TaskStore
  → Router Context
```

是否完整保留。该问题已登记为学习笔记 `LL-008`，属于当前源码事实基础上的契约候选，不是已端到端复现的 bug。

### 本步已完成：QueryTaskResultsTool

`src/a2a/builtin_tools/query_task_results.py:52-83` 由 Coordinator 注入 `TaskStore.results` 的 live mapping（注册点：`src/a2a/coordinator/agent_executor.py:356-370`）。指定 `task_ids` 时每项保留原文前 2000 字符并追加 `...[truncated]`；省略 ID 时每项保留前 300 字符并追加 `...`。它不读取 raw Worker history，也不直接查询 `PhysicalDispatch`，没有严格的 terminal-state predicate；因此如果 Worker 终态没有产生 `result` 或 `artifact`，该工具无法补回 summary。

### 本步已完成：Coordinator FinishTaskTool

`sar_orch/tools/coordinator/finish_task.py:47-72` 在 `success=True` 时先运行可选 `completion_validator`；未通过时返回 `ToolResult(success=False, error="mission_not_finished")`，不写 `TaskStore` 终态。验证通过或 `success=False` 时调用 `TaskStore.mark_finished(success)`，再返回 `ToolResult(task_complete=True, mission_success=success)`。`agent_executor.py:406-425` 明确允许 `success=False + task_complete=True`，随后 `_finish()`（`:858-872`）写入任务队列并发出顶层 A2A `COMPLETED`；`finally` 再关闭 store 并 abort runtime。

### 本次已完成：H — Router 查询、重规划与结束

`query_task_events` 读取 Worker dispatch 状态，`query_task_results` 读取 `TaskStore.results` 的结果投影；在默认 `agentic` 路径中，二者的 `ToolResult` 回到 Router 下一轮 LLM，由 Router 决定继续派发、调用 `update_plan` 重规划，或调用 `finish_task`。`RouterAgent.plan_next()` 只在 `orchestration_mode="dag"` 分支使用，不是默认 SAR 主线。

### 本次已完成：activate_plan_node 控制平面

`send_message(message_type="activate_plan_node", related_task_id=<logical_id>)` → `MissionRuntime.activate_plan_node()`：先执行只读 DAG gate，再在锁内复查竞争并预创建 `PREPARED` physical dispatch，绑定 `logical_id → worker_id → dispatch_id`，可选执行 Team ACK，随后进入 `dispatch_prepared_many()`。已区分 `logical_id`、`dispatch_id` 与 A2A 返回的 `worker_task_id`，并核对全体 acceptance 与失败补偿反馈。锚点：`src/a2a/builtin_tools/send_message.py:393-472`、`src/a2a/coordinator/mission_runtime.py:603-1098`。

### 当前唯一下一跳

`src/a2a/coordinator/agent_executor.py:440-450`：`store.close()` → `MissionRuntime.abort("mission_complete")`。本步只看终止清理如何冻结新激活、取消非终态 dispatch、释放 TeamPartition、清理映射并调用 `MissionRuntimeManager._release()`；不进入 SAR action。

### G 审计补充：自动汇报 vs LLM 主动汇报

父侧复核子代理 `deleg_d1baba9d` 的只读报告后确认：

- 自动路径：9 个环境 action tool 调用 `tool_result_from_barrier()`；`SARBarrier.env.step()` 生成 `structured_observations`，Worker publisher 去重后进入 `ToolResult.data["observations"]`，经 A2A `[DATA].structured_data` 上报。`no_op` 会触发 Barrier step，但不把 structured data 放入 ToolResult，因此不进入该自动 SemanticMap 上报路径。
- 主动路径：LLM 显式调用 `report_observation()`；只生成 `ToolResult.content` JSON，不调用 Barrier、不消耗 step、不经过 publisher；经 `[DATA].content` 的嵌套 JSON 进入同一 Coordinator parser。
- 没有 `annotation` 字段或固定注解格式。system prompt 要求发现新事实/变化时使用 structured JSON fields；工具 schema 只有 `object_type` required，`note` 可选。skill 示例写 `properties`，实际代码读取 `attributes`；该问题已登记为学习笔记 `LL-D05`，仍是文档—源码漂移候选。
- 测试源码被子代理读取但未执行；没有真实 A2A/SAR 运行证据。完整只读报告：`/home/wyh/.hermes/profiles/teacher/cache/delegation/subagent-summary-0-20260730_145301_276593.txt`。

源码边界仍为 `d1dada9`；本轮未运行测试、服务、实验或端到端 A2A。

## 5. 已确认的核心理解

### 5.1 生命周期与状态作用域

```text
MissionRuntimeManager
  = Coordinator 进程生命周期对象
  = 当前只维护一个 active MissionRuntime

MissionRuntime
  = 绑定一个已 admission 的 context_id
  = 物理派发、Worker task 映射、future、abort/recovery 的权威

TaskStore
  = 一次顶层 A2A 请求 / agentic 编排周期
  = 逻辑计划、MissionGraph、结果、逻辑节点↔物理派发映射

ContextManager
  = 按 context_id 复用的会话记忆
Agent 实例
  = 每次 AgentController.submit() 新建
```

`MissionRuntime` 不是 `dict[context_id, MissionRuntime]` 式的多 context 全局表。`MissionRuntimeManager.admit()` 发现未 abort 的 active runtime 时会拒绝新 admission。

### 5.2 动态状态进入 Router Prompt 的路径

```text
TaskStore / MissionRuntime / SemanticMap / Watchdog
  → SARCoordinatorStateProvider.snapshot(None)
  → RuntimeState.payload
  → ContextManager._project_runtime_state_to_pinned()
  → ContextManager.assemble()
  → messages_for_llm
  → Router LLM
```

`ContextManager.refresh_runtime_state()` 未传 context_id，因此调用 `snapshot(None)`。当前 Provider 不用该参数选 context，而是读取 `_execute_agentic()` 注入的 `self._task_store` 与 `self._runtime`。这依赖 Coordinator 同时只有一个 active MissionRuntime 的约束。

Provider payload 的重要字段：

```text
step_budget
mission_finished
mission_dag_view
physical_dispatches_view
task_status_view
recent_changes
supervision
semantic_summary / team_status_summary / map_revision / map_delta / map_summary
```

`prepare_for_llm()` 在 semantic 模式可计算地图差异，并在版本变化时调用 MapSummarizer；这可能产生辅助 LLM 调用。刷新失败时 Provider 返回旧快照并标记 `stale=True`，不直接中断 Router 主循环。

### 5.3 INPUT_REQUIRED 的边界

已实现且已静态核对：

```text
Worker → Coordinator 请求帮助
AskCoordinatorTool
  → NeedInputError
  → RunResult(need_input=True)
  → Worker AgentController 保存 snapshot（有 worker task_id 时）
  → AgentAdapter.updater.requires_input()
  → A2A TASK_STATE_INPUT_REQUIRED
```

尚未找到完整的：

```text
Coordinator → Human
请求输入 → 顶层任务暂停 → 人类通知 → 关联回复 → snapshot 恢复
```

另一个已确认缺口：Coordinator 调用 `AgentController.submit()` 时未传 `task_id`；因此 `controller.py:274-275` 的 `ctx.save_snapshot(task_id, agent.messages)` 不会在当前 Coordinator 主路径执行。不要把 Worker 的暂停/恢复机制误认为 Coordinator 向人请求输入的机制。

### 5.4 Tool / 函数反馈阅读约定

后续每个 Tool 或激活/派发函数，固定按以下顺序带读：

```text
输入 → 读取/改变的状态 → 成功反馈（content/data/返回 dict）
→ 失败反馈（error/reason）→ 下游如何消费反馈
```

不要只说“调用了某工具”而省略它需要的 ID、是否忽略某些参数、或 Router 最终实际看到的结果。

## 6. 已父侧独立复核的文档漂移（不要立刻修）

1. **高：默认 agentic 派发主线**
   - 文档：`docs/system_docs/data_flow.md:391-410` 仍以 `DispatchTaskTool → dispatch-1 → register_future` 描述默认路径。
   - 当前默认源码：`src/a2a/builtin_tools/send_message.py:393-431`，是 `SendMessageTool._handle_activate_plan_node()` → `MissionRuntime.activate_plan_node()`。
   - 结论：旧 `DispatchTaskTool` 是兼容路径，不应作为当前默认带读主线。

2. **高：默认 active-runtime 的 INPUT_REQUIRED 事件语义**
   - 文档：`data_flow.md:219-221,251-252` 称 Coordinator callback 写入 `EventStore help_request(question)`。
   - 当前 active runtime：`src/a2a/coordinator/server.py:864-1000` 写入 `status_update`；显式 `help_request` 在 `server.py:1120-1131` 的 legacy/pre-admission 分支。
   - 边界：这证明文档的 `EventStore help_request` 主线说法不适用于当前默认 active runtime；尚未证明 question 是否会通过 MissionRuntime 其他字段保留，后续要单独追。

3. **中：实验结束条件**
   - 文档：`系统架构概览.md:28` 只写 Barrier 完成或超预算。
   - 源码：`sar_orch/experiment.py:357-370` 在 `a2a_task.done()` 时也会提前退出 poll loop。

其余子代理候选（Worker 默认端口、默认工具集、观测入口、日志目录）尚未由父侧独立复核；不要以它们为结论。

## 7. 后续总体计划（每轮只推进一跳）

```text
A. Context 视图 → Prompt 文本（已完成）
B. Router 第一次 LLM 决策 → UpdatePlanTool（已完成）
C. MissionGraph：校验、逻辑节点/依赖、frontier（已完成）
D. activate_plan_node 控制平面
   D1. SendMessageTool 门面与输入/反馈（已完成）
   D2. DAG gate（已完成）
   D3. atomic claim（已完成）
   D4. 可选 Team ACK saga（已完成）
   D5. dispatch_prepared_many() 实际 A2A fan-out（已完成）
E. Worker 接收与非 SAR 交互
   E1. Router send_task_async → Worker ingress → AgentAdapter/Controller → Agent.run() 通用 Tool 分派（已完成）
   E2. AskCoordinatorTool → snapshot → INPUT_REQUIRED → 同 task 恢复（已完成）
   E3. QueryTaskEventsTool 显式查询是否能取回 INPUT_REQUIRED question（已完成；默认 status-only 路径不能）
   E4. ReportObservationTool → A2A status `[DATA]` → EventStore / SemanticMap / Router Context（已完成；不进入 SAR action）
   E5. A2ASendMailTool（已完成；仅 peer_mail=True，terminal ACK ≠ receiver 已读）
   E6. ReadMailboxTool（已完成；仅 peer_mail=True，receiver 原子读取/标已读）
   E7. GetSkillTool 按需加载技能（已完成；环境无关）
   E8. FinishTaskTool 完成信号（已完成；环境无关）
   E9. Worker terminal status/artifact → Coordinator physical dispatch / TaskStore / Router Context（已完成；LL-008 候选已记录）
   E10. QueryTaskResultsTool → TaskStore.results → Router 显式结果查询（已完成）
   E11. Coordinator FinishTaskTool → TaskStore.mark_finished → 顶层 A2A COMPLETED（已完成）
   G. Worker 观测 / callback → SemanticMap / EventStore / RuntimeState 更新（已完成）
   H. Router 查询事件和结果 → re-plan / finish_task（已完成）
   D6. activate_plan_node → gate / atomic claim / Team ACK / physical dispatch（已完成）
   E12. store.close → MissionRuntime.abort → 终止清理与 admission 释放（当前下一跳）
F. SAR 工具 → Barrier 收集动作 → SAREnv.step → 观测返回（本次明确暂缓）
I. 实验 poll、停止、日志、结束原因
J. 仅在明确请求后：整理已确认文档漂移并提出修订方案；不自动修改 docs
```

## 8. 新会话恢复提示

把下面这段发给新会话即可：

```text
请读取 /home/wyh/daily_work/LLaMAR/.agents/handovers/llamar-system-walkthrough.md，
先核对当前 git HEAD 和工作树，再按其中“当前精确暂停点”的下一跳继续。保持中文慢速带读，一轮只走一个函数级跳转；不要修改源码或文档。
```
