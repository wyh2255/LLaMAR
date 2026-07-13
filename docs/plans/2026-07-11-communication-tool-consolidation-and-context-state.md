---
日期: 2026-07-11
文档类型: 架构设计计划
文档概述: 评估 Coordinator-Worker 通信工具收敛、环境状态自动注入 Context、Worker 汇报机制分层的设计建议与迁移路径
---

# 通信工具收敛与 Context 状态注入设计建议

## 1. 背景

当前系统中，Coordinator 与 Worker 的交互主要通过多个 LLM 可见工具完成，包括 `dispatch_task`、`respond_worker`、`query_task_events`、`query_task_results`、`cancel_task` 等。同时，SAR 环境状态、语义地图、团队状态也以工具形式暴露给 LLM，例如 `query_sar_state`、`query_semantic_map`、`query_team_status`、`get_agent_state`、`query_shared_memory`。

这种设计具备清晰的功能分离，但也带来几个问题：

- LLM 需要在多个通信工具之间做工具选择，增加决策负担。
- 状态获取依赖 LLM 主动调用查询工具，容易出现“需要状态才能决策，但需要先决策是否查询状态”的循环。
- Worker 观测汇报依赖 LLM 主动调用 `report_observation`，可能漏报或延迟上报。
- 工具数量较多，Coordinator prompt 需要大量约束来维持正确使用方式。

本建议聚焦于三件事：

- 将 Coordinator-Worker 的通信入口收敛为一个 LLM 可见的 `send_message` 门面工具。
- 将地图、团队状态、Worker 实时状态从“LLM 主动查询”迁移到 Context 层自动注入。
- 将 Worker 汇报机制拆分为规则汇报和 LLM 语义汇报。
- 使用系统级 `TaskWatchdog` 监督任务停滞和 deadline，不通过通用 cron 周期唤醒 LLM。

## 2. 核心判断

通信工具可以收敛，但不应简单删除现有底层能力。

状态获取应更多由系统自动完成，而不是交给 LLM 自行决定。

Worker 汇报应同时包含代码规则触发和 LLM 总结触发，不能完全依赖定期 LLM 汇报。

短周期任务进度监督应由事件驱动的系统组件完成，而不是由 LLM 创建 cron 并定期调用查询工具。

## 3. 工具分层建议

当前工具应按职责重新分类：

| 类别 | 当前工具 | 是否适合暴露给 LLM | 建议 |
|------|----------|-------------------|------|
| A2A 通信/编排 | `dispatch_task`, `respond_worker`, `cancel_task` | 是，但可以收敛 | 使用 `send_message` 作为 LLM 门面 |
| A2A 状态读取 | `query_task_events`, `query_task_results` | 部分 | 长期迁移到 Context 自动注入 |
| Worker 注册表 | `query_workers` | 部分 | 可迁移到 Context 自动注入在线 Worker 摘要 |
| Coordinator 状态查询 | `query_sar_state`, `query_semantic_map`, `query_team_status` | 不理想 | 主路径迁移到 Context 自动刷新，工具保留为 debug/fallback |
| Worker 环境查询 | `get_agent_state`, `query_shared_memory` | 不理想 | 主路径迁移到 Worker Context 自动刷新 |
| Worker 领域动作 | `NavigateTo`, `Move`, `Explore`, `UseSupply`, `CarryPerson`, `DropOffPerson` 等 | 是 | 保留独立动作工具 |
| Worker 汇报/求助 | `report_observation`, `ask_coordinator` | 部分 | `ask_coordinator` 保留，`report_observation` 逐步自动化 |

## 4. `send_message` 门面工具设计

### 4.1 目标

减少 Coordinator LLM 可见通信工具数量，让 LLM 使用一个统一入口表达“给 Worker 发消息”的意图。

目标不是推翻现有 A2A 协议和任务生命周期，而是在 LLM 工具层增加一个语义门面。

### 4.2 建议接口

推荐接口：

```text
send_message(
  message_type: "assign_task" | "reply_to_help" | "cancel_task",
  content?: string,
  who?: string,
  related_task_id?: string
)
```

字段说明：

| 字段 | 是否必需 | 说明 |
|------|----------|------|
| `message_type` | 是 | 强枚举，避免 LLM 自造类别 |
| `content` | 条件必需 | `assign_task` 和 `reply_to_help` 必需；`cancel_task` 不需要 |
| `who` | 条件必需 | 仅 `assign_task` 必需，作为新任务的目标 Worker |
| `related_task_id` | 条件必需 | `reply_to_help` 和 `cancel_task` 必需；`assign_task` 可选，用作显式 subtask id |

参数约束必须由工具执行层校验，不能只依赖 prompt：

| `message_type` | 权威路由字段 | 校验规则 |
|----------------|--------------|----------|
| `assign_task` | `who` | `who` 和 `content` 必需；`related_task_id` 可选 |
| `reply_to_help` | `related_task_id` | `content` 必需；由 TaskStore 推导 Worker，不信任 `who` |
| `cancel_task` | `related_task_id` | 由 TaskStore 推导 Worker；忽略或拒绝 `who` 和 `content` |

对于已有任务，`related_task_id` 是唯一权威路由来源。执行层通过 TaskStore 完成 `dispatch_id -> worker_id -> worker_task_id` 映射，避免 `who` 与 task id 指向不同 Worker。

### 4.3 与现有工具的映射

| `message_type` | 当前能力 | 说明 |
|----------------|----------|------|
| `assign_task` | `dispatch_task(agent_id, prompt, task_id)` | 创建新的 Worker 子任务 |
| `reply_to_help` | `respond_worker(task_id, response)` | 回复 INPUT_REQUIRED，恢复已有任务 |
| `cancel_task` | `cancel_task(task_id)` | 取消已有任务 |

第一阶段不引入 `notify`。普通 A2A SendMessage 当前会进入 Worker task 和 ReAct 生命周期，“只通知但不执行”的行为尚未定义。若后续有明确需求，应单独设计 mailbox/event 语义，明确是否创建 task、是否唤醒 LLM、是否需要确认以及如何处理并发任务。

### 4.4 为什么不只保留三参数

不建议只使用 `who/type/content` 三个参数，也不建议所有消息类型都强制提供 `who`。

原因是 `reply_to_help` 和 `cancel_task` 必须绑定已有任务。当前 INPUT_REQUIRED 恢复链路依赖 task id 与 snapshot 的对应关系。如果没有 `related_task_id`，系统无法可靠恢复 Worker 的暂停状态。

因此，`related_task_id` 应作为条件必需字段保留。对于 `reply_to_help` 和 `cancel_task`，目标 Worker 必须从 TaskStore 推导。

### 4.5 不建议合并的能力

`query_task_events` 不应合并进 `send_message`，也不应在第一阶段隐藏。

它的本质是状态读取，不是通信动作。当前它还承担指定任务查询、等待 actionable 状态、合并 dispatch id 与 worker task id、读取结果等职责，简单的事件文本摘要不能替代它。长期可以由 Context 层自动注入结构化任务状态，但在自动状态视图经过验证前继续保留该工具。

`query_workers` 也不适合并入 `send_message`。它更像 Worker registry 的状态视图，适合自动注入在线 Worker 摘要。

## 5. Context 状态自动注入

### 5.1 当前问题

当前 Coordinator Context 已经有环境视图和 pinned state 机制，但许多字段需要通过 LLM 调用查询工具后才会更新。

这会形成不理想链路：

```text
LLM 需要状态才能决策
但状态更新依赖 LLM 先决定调用状态查询工具
```

### 5.2 目标链路

建议改为：

```text
pre_llm hook
  -> StateProvider.snapshot()
  -> ContextManager.refresh_runtime_state(snapshot)
  -> assemble() 自动注入精简状态
  -> LLM 只负责计划和动作选择
```

也就是系统保证 LLM 每轮开始前都看到最新的任务相关状态。

ContextManager 不应直接 import 或持有 `SARBarrier`、`SemanticMapStore`、`TaskStore`、ROS2 client 或全局 EventStore。建议在构建 Agent 时注入只读状态提供者：

```text
CoordinatorStateProvider.snapshot(context_id) -> CoordinatorRuntimeState
WorkerStateProvider.snapshot(context_id, worker_id) -> WorkerRuntimeState
```

状态快照至少包含：

```text
- version
- env_step
- observed_at
- payload
- stale
- refresh_error
```

StateProvider 负责适配 simulator、A2A store 或 ROS2，ContextManager 只消费统一 DTO。刷新失败时继续使用最近一次快照，同时注入状态年龄和错误标记，不能因为状态源暂时不可用而阻止 LLM 调用。

当前 `CoordinatorContextManager._render_memory_block()` 仍会直接 import 全局 EventStore。迁移到 StateProvider 时必须删除这条直接读取路径，避免同一批事件同时通过旧摘要和 runtime state 重复注入。过渡期由 StateProvider 包装 `EventStore.get_summary()` 或结构化查询，不允许两条路径并存。

这里的“每轮”明确指每次 LLM request 前检查最新快照，而不是每次主动触发传感器采集。StateProvider 应独立维护状态；Context 仅在 `version` 或 `env_step` 变化时替换动态状态，避免同一 SAR step 内重复读取和序列化。

### 5.3 Coordinator 自动注入内容

建议每轮自动注入：

- 当前 step、max steps、remaining steps。
- 已知 fire/person/reservoir/deposit 摘要。
- 关键 stale entries 和 conflicts。
- Worker 在线状态、位置、库存、当前任务。
- 最近 task events，包括 `RUNNING`、`COMPLETED`、`FAILED`、`INPUT_REQUIRED`。
- 最近新增或变化的观测 delta。

任务状态应使用结构化视图，而不是只注入 `EventStore.get_summary()` 的全局文本：

```text
TaskStatusView:
- dispatch_id
- worker_task_id
- worker_id
- state
- latest_result
- help_request
- updated_at
- acknowledged_by_coordinator
```

Context 自动展示所有活跃任务和最近发生变化的终态任务。`query_task_events` 继续用于显式等待和深度查询，直到实验确认自动状态视图可以稳定替代它。

示例：

```text
Step 17/50, remaining 33
Known fires:
- fire_3 at (4,2,0), intensity high, last_seen step 16, confidence 0.9
Known persons:
- person_1 at (7,5,0), status unrescued, blocked_by fire_3
Workers:
- Alice at (4,1,0), inventory [Water], current_task alice-extinguish-1 running
- Bob at (7,4,0), inventory [], current_task bob-rescue-1 input_required
Recent changes:
- Alice observed fire_3 weakened at step 16
- Bob failed carry_person due to nearby fire
```

### 5.4 避免全量注入

不建议每轮注入完整 grid 或完整 semantic map JSON。

更合理的是注入：

- 摘要。
- 增量变化。
- 高优先级对象。
- 冲突和过期信息。
- 必要时提供 debug/fallback 查询工具。

`query_sar_state`、`query_semantic_map`、`query_team_status` 可以保留，但不应作为主路径依赖。

运行时状态必须与记忆策略解耦。即使 Context strategy 为 `raw`，也应注入 runtime state block；只有 episodic、summary、pinned history 等记忆内容受 strategy 控制：

```text
system prompt
conversation history
runtime state block       # 所有 strategy 都注入
memory block              # 按 memory strategy 决定是否注入
```

## 6. Worker 实时状态与 ROS2 适配

如果后续接入 ROS2，ROS2 不应直接暴露给 LLM 作为低层工具。

建议链路：

```text
ROS2 topics/services/actions
  -> RobotStateAdapter / SensorAdapter
  -> WorkerLocalWorldModel
  -> WorkerContextManager.refresh_environment()
  -> pre_llm 自动注入
```

Worker LLM 每轮看到的是结构化后的实时状态，而不是原始传感器流。

示例：

```text
Local State:
- pose: (3.1, 4.8), yaw 90deg, source /tf, age 120ms
- inventory: Water x1, source robot_state, age 80ms
- nearby hazards: fire at (4,5), intensity medium, source thermal_camera, age 500ms
- blocked cells: [(4,5), (4,6)], source local_costmap, age 300ms
```

约束：

- 自动注入的是压缩后的状态，不是原始 sensor stream。
- 每条状态应携带来源、时间戳或 age、置信度。
- 过期状态要显式标记，避免 LLM 当作当前事实。
- ROS2 adapter 按固定频率更新 WorkerLocalWorldModel，LLM 调用只读取快照，不主动触发 topic/service 采集。
- 每类状态需要定义权威来源，例如 pose 使用 `/tf`、localization 或 simulator ground truth 中的哪一个。

在当前 SAR simulator 阶段，优先级更高的是完善 Context 自动刷新机制，而不是立即引入 ROS2。

## 7. Worker 汇报机制分层

### 7.1 当前问题

`report_observation` 依赖 Worker LLM 主动调用，容易漏报。

### 7.2 建议分层

| 汇报类型 | 触发方式 | 内容 | 是否需要 LLM |
|----------|----------|------|--------------|
| 规则汇报 | 每次动作后或观测变化后 | 结构化事实：位置、库存、火、人、失败原因 | 否 |
| 语义汇报 | 子任务完成、阶段总结、异常解释 | 做了什么、为什么失败、下一步建议 | 是 |
| 求助汇报 | 卡住或需要 Coordinator 决策 | 明确问题和可选方案 | 是或半模板 |
| 心跳汇报 | 定期 | alive、current_task、progress | 否 |

### 7.3 自动观测汇报链路

建议将动作工具结果自动转换为结构化 A2A 观测事件，再由 Coordinator 接入 semantic map：

```text
worker action tool result
  -> ObservationExtractor
  -> WorkerReportPublisher
  -> A2A status/artifact/data event
  -> Coordinator push callback
  -> SemanticMapStore.ingest_observation()
  -> Worker Context + Coordinator Context 下一轮自动看到
```

LLM 仍可补充语义总结，但不再承担全部事实上报责任。

Worker 不得直接持有或调用 Coordinator 的 `SemanticMapStore`。本地 SAR、独立进程和 ROS2 部署都应通过统一的 `WorkerReportPublisher` 接口发送，以保留 A2A 边界、task/context 关联和现有日志链路。

自动汇报不应依赖解析给 LLM 阅读的自然语言。动作层应提供机器可读数据和自然语言两个通道：

```text
ToolResult:
- content: 给 LLM 阅读的文本
- data: 结构化 observation DTO
```

`ObservationExtractor` 优先消费 `data`。如果现有 ToolResult 暂不支持 `data`，应先让 Barrier 或动作工具返回结构化 observation，再统一渲染文本和发布事件；正则解析仅可作为兼容或诊断手段，不能作为 semantic map 的权威写入路径。

自动汇报启用前还应为 `SemanticMapStore.observations` 设置有界保留策略，例如最多保留最近 1000 条或最近 N 个 step 的记录。对象聚合状态继续保存在 fires/persons/agents 等结构中，历史 observation 仅用于 delta、诊断和日志，不能无界增长。

### 7.4 去重与节流

自动汇报应避免噪音过多。

建议只在以下情况上报：

- 新对象首次出现。
- 已知对象状态变化。
- Agent 位置或库存变化。
- 动作失败。
- 发现冲突信息。
- 每 N step 心跳。

## 8. 推荐目标架构

```text
LLM 工具层：
- Coordinator: send_message, query_task_events, finish_task, maybe debug query tools
- Worker: domain actions, finish_task, ask_coordinator

Context 层：
- StateProvider 维护版本化 runtime snapshot
- 每次 LLM request 前按版本刷新 semantic map summary
- 每次 LLM request 前按版本刷新 team/task status
- Worker 按版本读取 local state / sensor state

Event/Report 层：
- 动作结果通过 A2A 自动结构化上报并入库
- Worker 子任务完成时 LLM 总结
- 异常/冲突时主动事件

Supervision 层：
- TaskWatchdog 读取 TaskStatusView 和 deadline
- 正常心跳只更新状态，不调用 LLM
- FAILED / INPUT_REQUIRED / STALE / DEADLINE_EXCEEDED 形成 actionable event
- actionable event 进入 Coordinator runtime state，按编排循环边界触发处理
```

目标是从：

```text
LLM 决定要不要看状态 -> LLM 看状态 -> LLM 决定动作
```

转为：

```text
系统保证状态可见 -> LLM 只决定下一步动作/任务
```

## 9. TaskWatchdog 任务监督

### 9.1 定位

当前 SAR 任务不引入 Hermes 式通用 cron 工具。A2A push 已经负责实时状态传播，`query_task_events` 负责显式等待和深查，TaskWatchdog 只补充“任务长时间无进展”和“超过 deadline”检测。

TaskWatchdog 是系统组件，不是 LLM 周期调用工具，也不应固定间隔创建新的 Agent 会话。它可以使用轻量 ticker 检查 deadline，但判断依据是结构化状态和事件时间戳，而不是周期调用 LLM。

第一版 TaskWatchdog 必须作为 Coordinator 所在 asyncio event loop 中的单个 `asyncio.Task` 运行，不使用独立 `threading.Thread`。现有 TaskStore 和 WorkerRegistry 的同步读写方法没有跨线程保护；它们在同一 event loop、且方法内部没有 `await` 时可以作为原子同步片段使用，但不得从 Watchdog 线程直接访问。若未来迁移到独立线程或进程，必须先增加线程安全快照接口或消息传递边界。

### 9.2 输入与输出

输入：

- `TaskStatusView` 中的任务状态、`updated_at`、worker 和 task id 映射。
- Worker heartbeat 或最近 A2A push 时间。
- 任务创建时间、开始时间、stale threshold 和 hard deadline。
- 当前 SAR step、wall clock、barrier timeout 和实验停止状态。

输出：

- 更新 `SupervisionStateStore`，由它与任务基础状态共同生成 `TaskStatusView` 的监督字段。
- 向 EventStore 写入结构化监督事件。
- 将 actionable event 暴露给 CoordinatorStateProvider。
- 可选执行预先配置的确定性安全动作，但默认不自动取消任务。

建议监督事件：

| 事件 | 条件 | 默认处理 |
|------|------|----------|
| `TASK_STALE` | 任务处于 RUNNING 且超过阈值无进展事件 | 写入事件并请求 Coordinator 处理 |
| `WORKER_UNREACHABLE` | heartbeat 和 A2A push 均超过 contact 阈值未更新 | 写入事件并标记 Worker 状态 |
| `TASK_DEADLINE_WARNING` | 接近 hard deadline | 提前通知 Coordinator |
| `TASK_DEADLINE_EXCEEDED` | 超过 hard deadline | 写入高优先级事件，等待 Coordinator 决定取消或重派 |
| `TASK_RECOVERED` | stale/unreachable 后重新收到有效事件 | 清除告警并记录恢复 |

正常 `RUNNING` 和普通 heartbeat 不触发 LLM，只更新 `last_seen`。

现有 push callback 只按 worker task id 写 EventStore，并不会更新 WorkerRegistry 联系时间。实施时需要通过 TaskStore 的 worker-task 映射解析 worker id，并在收到有效 push 后更新独立的 `last_contact_at`。WebSocket heartbeat 也更新同一字段。不要直接把 `last_heartbeat` 政名为 `last_contact_at`，两者应分别保留，以便区分 WebSocket 心跳故障与 Worker 整体不可达。

### 9.3 进展定义

不能把“收到任意事件”都视为任务有进展，否则重复心跳会永久掩盖卡死任务。建议分别维护：

```text
- last_contact_at: 最近 heartbeat 或任意 A2A 消息
- last_progress_at: 最近状态转换、有效 artifact、有效 observation 或领域动作结果
- last_state_change_at: 最近 task state 变化
```

`TASK_STALE` 根据 `last_progress_at` 判断，`WORKER_UNREACHABLE` 根据 `last_contact_at` 判断。

有效进展事件需要白名单化，例如：

- task state 发生变化。
- 收到非重复 artifact/result。
- 新增或改变 semantic observation。
- Worker 完成一个产生有效领域状态 delta 的动作，例如位置、库存、火势、人员状态、覆盖率或运输率发生变化。

LLM response、重复 heartbeat、重复 observation、纯日志消息、零成本状态查询、NoOp，以及仅推进 step 但没有领域状态变化的动作默认不刷新 `last_progress_at`。进展判断应优先消费结构化 action result 和环境 delta，不能仅检查位置或库存，也不能把 step 增长本身当作有效进展。

`TASK_STALE` 用于检测跨多个 SAR step 持续没有有效领域进展，不替代 `SARBarrier.STEP_TIMEOUT` 对单步缺失 action 的处理。第一版默认 stale 阈值不得短于 `2 * STEP_TIMEOUT`，并应同时支持按连续无进展 step 数判断，避免正常等待其他 Agent 时误报。

### 9.4 状态机与去重

每个任务维护独立监督状态：

```text
HEALTHY -> STALE -> HEALTHY
HEALTHY -> DEADLINE_WARNING -> DEADLINE_EXCEEDED
HEALTHY/STALE -> WORKER_UNREACHABLE -> HEALTHY
任意状态 -> TERMINAL
```

约束：

- terminal task 不再产生 stale/deadline 事件。
- 同一种告警在状态未变化时只发一次，避免每次 tick 重复唤醒 Coordinator。
- 恢复时产生一次 `TASK_RECOVERED`，并清除对应告警状态。
- Watchdog 重启后根据持久化任务时间和事件重建状态；不能把重启时间当作任务进展时间。
- 使用 `(task_id, event_type, supervision_epoch)` 或等价幂等键去重。
- 新任务从创建时间开始进入 grace period。grace period 内记录 contact/progress，但不产生 stale、unreachable 或 deadline warning；hard deadline 仍按任务定义计算。初始时间不得使用 `0` 或 Watchdog 首次扫描时间。

监督状态不能只依赖 EventStore 的普通事件列表。当前 EventStore 每个 task 最多保留 500 条事件，密集 status/artifact 可能淘汰尚未消费的告警。TaskWatchdog 应维护独立、持久化的 `SupervisionStateStore`，保存每个任务的当前监督状态、时间戳和未确认 actionable event；EventStore 仅接收用于追踪和展示的副本。Coordinator 确认处理后再将监督事件标为 acknowledged。

### 9.5 与 Coordinator 的唤醒边界

TaskWatchdog 不应直接并发启动第二个 Coordinator Agent loop。当前同一个 `context_id` 由 `AgentController` 锁串行化，监督事件应进入现有 EventStore/runtime snapshot，并由既有编排循环处理。

第一版不实现 WakeQueue，也不主动 resume 已退出的 Coordinator。监督事件由下一次既有 Coordinator `pre_llm` 通过 StateProvider 读取；若 Coordinator 已结束，事件只持久化供诊断。由于当前实验中的 Coordinator 持续运行，该边界足够覆盖 SAR 场景。

如果未来 Coordinator 会在等待期间休眠，应新增单一 `CoordinatorWakeQueue`：

```text
TaskWatchdog actionable event
  -> CoordinatorWakeQueue.put(context_id, reason, event_id)
  -> 当前 loop 在安全边界消费，或在没有运行中 loop 时启动一次 resume
```

WakeQueue 必须按 `context_id + event_id` 去重，并保证同一 context 同时最多有一个 Coordinator run。不能由每个 watchdog tick 创建新 Agent 会话。

如果 WakeQueue 的生产者与 Coordinator event loop 不在同一线程，必须使用 `loop.call_soon_threadsafe()` 投递；不能跨线程直接调用 `asyncio.Queue.put_nowait()` 或操作 AgentController 的 `asyncio.Lock/Event`。

### 9.6 阈值和时钟

建议所有内部 deadline 使用 monotonic time 计算，持久化时同时记录 wall-clock timestamp 以支持重启恢复和日志分析。

阈值应由系统配置给出，不由 LLM 随意创建：

```text
task_stale_seconds
worker_unreachable_seconds
deadline_warning_seconds
task_hard_deadline_seconds
watchdog_tick_seconds
```

默认值需要与 `SARBarrier.STEP_TIMEOUT`、实验 wall-clock limit 和任务规模协调，避免 Watchdog 在正常 barrier 等待期间误报。第一版只允许 Coordinator 读取告警和决定动作，不提供 `watch_task` 或 cron 管理工具给 LLM。

### 9.7 与现有组件的边界

| 组件 | 职责 | 不负责 |
|------|------|--------|
| A2A push callback | 实时接收 Worker 状态、artifact 和观测 | 判断任务是否停滞 |
| EventStore | 保存事件 | 主动调度或取消任务 |
| TaskStatusView | 提供当前结构化任务状态 | 定时检测 deadline |
| TaskWatchdog | 检测 stale、unreachable 和 deadline | 执行领域规划、周期调用 LLM |
| SupervisionStateStore | 持久保存当前监督状态和未确认告警 | 保存完整 A2A 事件流 |
| `query_task_events` | 显式等待和深查指定任务 | 后台持续监督 |
| Coordinator | 根据 actionable event 决定回复、取消或重派 | 维护 ticker 和 heartbeat 规则 |

### 9.8 未来通用 Scheduler

只有在需要跨小时/跨天巡检、Coordinator 退出后定时重启任务、周期巡逻或持久化提醒时，才考虑 Hermes 式 Scheduler。届时应单独设计持久化 job store、claim、防重复执行、重启恢复和工具递归保护，不与 TaskWatchdog 混为同一组件。

## 10. 建议迁移步骤

### 阶段 1：增加 `send_message` 门面

> **状态：已完成并清理（commit 00387b6 + 后续清理）**
> 
> 已新增 `SendMessageTool`，内部复用 `DispatchTaskTool`/`RespondWorkerTool`/`CancelTaskTool`，按 `message_type` 做参数校验，TaskStore 作为已有任务权威路由源。已从 agentic 编排的 LLM 工具列表中移除旧通信工具，prompt 统一改为 `send_message` 作为唯一通信入口。新增 `tests/test_send_message_tool.py` 并跑通端到端 smoke test。
> 
> 已清理：移除 `sar_orch/coordinator.py` 中旧的 `dispatch_task`/`respond_worker` 回调 handler，并更新 `src/a2a/coordinator/sink.py` 使其将 `send_message` 的 `tool_start`/`tool_result` 翻译为 dispatch/reply/cancel 语义事件。

- 新增 `SendMessageTool`。
- 内部复用现有 `DispatchTaskTool`、`RespondWorkerTool`、`CancelTaskTool` 逻辑。
- 按 `message_type` 执行条件参数校验，并以 TaskStore 作为已有任务的权威路由源。
- 第一版仅支持 `assign_task`、`reply_to_help`、`cancel_task`，不支持 `notify`。
- Coordinator prompt 改为统一使用 `send_message` 作为唯一通信入口。
- 暂时保留旧工具实现作为内部 service/helper，但同一次 LLM 调用中只暴露一套通信 schema，避免重复工具进一步增加选择负担。
- 日志仍记录具体语义事件，例如 `assign_task`、`reply_to_help`、`cancel_task`。

### 阶段 2：Coordinator Context 自动刷新

> **状态：已完成（commit 后续推进）**
>
> 已实现：
> - 新增 `Agent.router_agent.state_provider.RuntimeState` DTO 与 `StateProvider` 协议。
> - 新增 `sar_orch.coordinator_state_provider.SARCoordinatorStateProvider`，从 `SARBarrier`、`SemanticMapStore`、`EventStore` 读取状态，支持 `set_task_store` 在 agentic 执行时注入单次 `TaskStore`。
> - 在 `CoordinatorContextManager` 增加 `state_provider` 注入、`refresh_runtime_state()` 与 `_project_runtime_state_to_pinned()`，删除 `_render_memory_block()` 对 `EventStore` 的直接 import。
> - `CoordinatorSARHooks.pre_llm()` 在每轮 LLM 前调用 `refresh_runtime_state(context_id)`，并将状态投影到 `CoordinatorPinnedState`。
> - `Agent.run()` 保存 `_run_context_id`，`Agent.attach_context()` 从 context 复制 `_state_provider`。
> - `RouterControllerBuildOptions` 增加 `state_provider` 并通过 session_factory 传入 `CoordinatorContextManager`。
> - `CoordinatorServer`/`create_server`/`create_coordinator_a2a_server`/`CoordinatorAgentExecutor` 全链路增加 `state_provider` 参数；`CoordinatorAgentExecutor._execute_agentic()` 在创建 `TaskStore` 后调用 `state_provider.set_task_store(store)`。
> - `SARCoordinator.start()` 构造 `SARCoordinatorStateProvider` 并注入 `create_server`；同时从 `extra_tools` 移除 `QuerySemanticMapTool` 和 `QueryTeamStatusTool`（语义模式下 Coordinator 不再通过 LLM 工具主动查询，状态由 Context 自动注入）。
> - `CoordinatorPinnedState` 扩展 `task_status_view` 与 `recent_changes` 字段；`_render_current_state()` 渲染任务状态视图与最近变化。
> - `EventStore.get_task_state()` 返回新增 `updated_at` 字段，供 `TaskStatusView` 使用。
> - 更新 `sar_orch/prompts/coordinator/system.semantic.md`：移除要求 LLM 调用 `query_semantic_map()` / `query_team_status()` 的指令，改为要求读取每轮自动注入的 Context Memory 块。
> - 更新 `AGENTS.md` 的 “Semantic vs Oracle mode” 与 “Semantic map query tools” 条目，说明语义模式下状态由 Context 自动注入，工具类仅作为 debug/fallback。
> - 新增 `tests/test_coordinator_state_provider.py` 覆盖 snapshot、缓存、故障降级、任务视图、recent_changes、Context 注入与渲染。
>
> 验证：
> - `ruff check src/ sar_orch/` 通过。
> - `pytest tests/test_coordinator_semantic_mode.py tests/test_context_snapshot.py tests/test_send_message_tool.py tests/test_semantic_tools.py tests/test_coordinator_state_provider.py` 全部通过。
> - 真实 `SARBarrier` + `SemanticMapStore` 构造 provider 并 snapshot 成功。
>
> 保留：
> - `query_task_events` 继续作为 LLM 可见工具，用于显式等待和深度查询。
> - `query_sar_state` 在 oracle 模式下仍注册为 LLM 工具。
> - `QuerySemanticMapTool`/`QueryTeamStatusTool` 工具类保留，仅降级为调试/回退工具，不再默认注册到 Coordinator LLM 工具列表。

- 定义 `CoordinatorStateProvider`、runtime state DTO 和失败策略。
- 在 `CoordinatorSARHooks.pre_llm()` 中按 snapshot version 刷新 runtime state。
- StateProvider 读取 `SemanticMapStore`、TaskStore、EventStore，ContextManager 不直接依赖这些实现。
- 删除 `CoordinatorContextManager._render_memory_block()` 对全局 EventStore 的直接 import，避免重复注入。
- 注入 step budget、semantic summary、team status、recent task events。
- 将 runtime state 与 `raw/summary/hybrid` 等 memory strategy 解耦。
- 保留 `query_task_events` 作为显式等待和深查能力。
- 将 `query_semantic_map`、`query_team_status` 降级为 debug/fallback。

### 阶段 3：TaskWatchdog 监督

> **状态：已完成**
>
> 已实现：
> - 新增 `sar_orch.supervision_state_store.SupervisionStateStore`：独立、持久化、线程安全的任务监督状态存储，支持 NDJSON 持久化和 `acknowledge_event`。
> - 新增 `sar_orch.task_watchdog.TaskWatchdog` 与 `WatchdogConfig`：在 Coordinator event loop 内以单个 `asyncio.Task` 运行，检测 `TASK_STALE`、`WORKER_UNREACHABLE`、`TASK_DEADLINE_WARNING`、`TASK_DEADLINE_EXCEEDED`，并生成 `TASK_RECOVERED` 恢复事件。
> - 实现 `last_contact_at` / `last_progress_at` / `last_state_change_at` 分离：心跳与 A2A push 共同更新 `last_contact_at`；有效进展事件（terminal status、artifact、observation、INPUT_REQUIRED、环境领域 delta）更新 `last_progress_at`。
> - 扩展 `TaskStatusView`：`SARCoordinatorStateProvider._build_task_status_view()` 合并 SupervisionStateStore 的监督字段。
> - `SARCoordinatorStateProvider` 新增 `_build_supervision_view()`，将未确认事件和活跃告警注入 runtime state payload。
> - `CoordinatorContextManager` 的 `CoordinatorPinnedState` 新增 `supervision` 字段，并在 `_render_current_state()` 中渲染告警。
> - 更新 `WorkerRegistry`：新增 `last_contact_at` 字典、`update_contact()` 和 `get_last_contact_at()`；保留原有 `last_heartbeat` 不变。
> - 更新 A2A push callback（`src/a2a/coordinator/server.py`）：通过 TaskStore worker-task 映射解析 worker_id，调用 `TaskWatchdog.record_worker_contact()` / `record_progress()` / `record_state_change()`。
> - 更新 WebSocket heartbeat handler：调用 `TaskWatchdog.record_worker_contact(worker_id)`。
> - 在 `CoordinatorServer` lifespan 中启动/停止 `TaskWatchdog`，并将 `SARBarrier` 注入 watchdog 以检测领域 delta。
> - 全链路参数传递：`create_server` → `CoordinatorServer` → `create_coordinator_a2a_server` → `CoordinatorAgentExecutor`，并支持外部注入 `supervision_state_store` / `task_watchdog`。
> - `SARCoordinator.start()` 构造 `SupervisionStateStore` 并注入 `SARCoordinatorStateProvider` 与 `create_server`，确保 state provider 和 watchdog 共享同一 store。
> - 新增 `tests/test_task_watchdog.py` 覆盖 SupervisionStateStore、TaskWatchdog 状态机、检测逻辑、WorkerRegistry 联系时间、领域 delta 进展。
> - 更新 `tests/test_coordinator_state_provider.py` 覆盖 supervision 字段注入与 Context 渲染。
> - 更新 `AGENTS.md`：新增 TaskWatchdog、SupervisionStateStore、进度规则、边界说明条目。
>
> 验证：
> - `ruff check src/ sar_orch/` 通过。
> - `pytest tests/test_coordinator_semantic_mode.py tests/test_context_snapshot.py tests/test_send_message_tool.py tests/test_semantic_tools.py tests/test_coordinator_state_provider.py tests/test_task_watchdog.py tests/test_coordinator_push_callback.py tests/test_cancel_task.py tests/test_respond_worker.py` 全部通过。
> - 关键端到端路径未改变：push callback、cancel task、respond worker 测试全部通过。
>
> 保留：
> - 第一版只告警，不自动取消或重派任务。
> - 不实现 WakeQueue；监督事件由既有编排循环在下一次 `pre_llm` 时读取。
> - `last_heartbeat` 与 `last_contact_at` 分别维护，保留区分 WebSocket 心跳与 Worker 整体不可达的能力。

- 扩展 `TaskStatusView`，从任务基础状态和 SupervisionStateStore 投影 `last_contact_at`、`last_progress_at`、`last_state_change_at` 和监督状态，不直接向 TaskStore 节点写入监督字段。
- 以 Coordinator event loop 内的单个 `asyncio.Task` 运行 Watchdog，不跨线程访问 TaskStore 或 WorkerRegistry。
- 保留 `last_heartbeat`，并增加由 heartbeat 和有效 A2A push 共同更新的 `last_contact_at`。
- 实现独立持久化的 `SupervisionStateStore`，监督告警只向 EventStore 写追踪副本。
- 定义有效进展事件白名单和重复事件判定规则。
- 实现单实例 TaskWatchdog ticker、告警去重、恢复事件和 terminal task 停止监督。
- 将监督事件写入 EventStore，并通过 CoordinatorStateProvider 注入 runtime state。
- 第一版只告警，不自动取消或重派任务。
- 第一版不实现 WakeQueue，告警在现有编排循环下一次 `pre_llm` 时被读取。
- 增加新任务 grace period，并将 stale 定义为跨多 step 无有效领域 delta。
- 增加 ticker 健康状态和 watchdog 检查延迟日志。

### 阶段 4：Worker Context 自动刷新

> **状态：已完成（commit f622f32）**
>
> 已实现：
> - 新增 `sar_orch.worker_state_provider.SARWorkerStateProvider`，实现 `StateProvider` 协议，从 `SARBarrier` 读取 position/inventory/step/obs。构造参数 `(barrier, agent_idx, semantic_map_url=None)`。
> - 使用 `RuntimeState` DTO（复用 `Agent.router_agent.state_provider`），payload 包含 `position`、`inventory`、`step`、`known_fires`、`known_persons`、`mission_status`、`age_ms`。
> - `known_fires`/`known_persons` 优先来自 barrier `get_current_obs()`（局部视口），预留 `semantic_map_url` 用于 Phase 5 的全局摘要 HTTP 获取。
> - 版本化缓存：以 SAR env step 为 version，同一 step 内返回同一 snapshot 对象引用；`age_ms` 基于上一 snapshot 的 `observed_at` 计算。
> - 刷新失败时保留最近快照并标记 `stale=True` + `refresh_error`；首次失败无先验时返回空 stale snapshot。
> - Worker 基类 `ContextManager.__init__()` 增加 `state_provider` 参数（与 Coordinator 基类签名一致），存储 `self._state_provider` 和 `self._runtime_state`。
> - `WorkerContextManager.__init__()` 接受 `state_provider` 参数并传递给基类；新增 `refresh_runtime_state()`（注意：使用此命名保持与 Coordinator 统一，非 `refresh_environment`）和 `_project_runtime_state_to_pinned()`，字段映射为 `position`/`inventory`/`step`/`known_fires`/`known_persons`/`mission_status`。
> - `WorkerSARHooks.pre_llm()` 在 `prune_history()` 前调用 `self._ctx.refresh_runtime_state()`，确保每轮 LLM 请求前状态已自动刷新。
> - `WorkerPinnedState` 增加 `state_mode` 字段（默认 `"semantic"`），支持语义模式与 oracle 模式渲染切换。
> - `WorkerContextConfig` 增加 `state_mode` 字段（默认 `"semantic"`），与 Coordinator `ContextConfig` 对齐。
> - `_render_current_state()` 渲染 age 标注，例如 `source barrier, age 150ms`；同时兼容 `_extract_pinned()` 的增量更新——自动注入保证基础可见性，tool result 的 extract 补充执行后的增量变化。
> - 清理 `worker_agent/context.py` 中陈旧的 `CoordinatorPinnedState`/`CoordinatorContextManager` 副本（已在 `router_agent/context.py` 定义）。
> - 全链路参数传递：`SARWorker.start()` 构造 `SARWorkerStateProvider` → `create_worker_a2a_server` → `AgentAdapter` → `build_controller` → `session_factory` → `WorkerContextManager`。
> - 更新 `sar_orch/prompts/worker/system.md`：规则 1/4/5 和策略段改为引用 Context Memory 自动注入状态，`get_agent_state` 降级为调试工具。
> - `AgentAdapter.__init__()` 增加 `state_provider` 类型注解（`StateProvider | None`）。
> - `sar_orch/worker.py` 创建 `SARWorkerStateProvider` 时传入 `barrier`、`agent_idx` 和从 `coordinator_url` 推导的 HTTP URL。
> - 保留 `get_agent_state` 和 `query_shared_memory` 工具作为 debug/fallback。
>
> 验证：
> - `ruff check src/ sar_orch/ tests/` 通过。
> - `pytest tests/test_worker_state_provider.py` 13/13 通过。
> - `pytest tests/` 204/210 通过（6 个预存失败与本次变更无关）。
>
> 保留：
> - `get_agent_state` 保留为 LLM 可见的调试/确认工具。
> - `query_shared_memory` 保留为 debug/fallback。
> - ROS2 adapter 延后到后续阶段。
> - `semantic_map_url` HTTP 获取延后到 Phase 5。

- 定义 `SARWorkerStateProvider`，实现 `StateProvider` 协议（复用 `Agent.router_agent.state_provider`），从 `SARBarrier` 读取 Worker 实时状态。构造参数 `(barrier, agent_idx, semantic_map_url=None)`，其中 `agent_idx` 用于从 barrier 查询正确智能体的位置/库存/观察。
- 使用 `sar_orch.worker_state_provider.SARWorkerStateProvider` 作为具体实现，注入到 Worker 创建链路。
- Worker runtime state DTO 复用 `Agent.router_agent.state_provider.RuntimeState`，payload 包含 `position`、`inventory`、`step`、`known_fires`、`known_persons`、`mission_status`、`age`。其中 `known_fires`/`known_persons` 优先来自 barrier 的当前观察（局部视口），如果有 `semantic_map_url` 则从 `/semantic-map` 获取全局摘要补充。
- Worker 基类 `ContextManager.__init__()` 增加 `state_provider` 参数（保持与 Coordinator 基类签名一致），存储 `self._state_provider` 和 `self._runtime_state`。
- `WorkerContextManager.__init__()` 接受 `state_provider` 参数并传递给基类，增加 `refresh_runtime_state()` 方法和 `_project_runtime_state_to_pinned()`。
- `refresh_runtime_state()` 按 snapshot version 执行版本化刷新：同一 SAR env step 内不重复读取 barrier，刷新失败时保留最近快照并标记 `stale`/`refresh_error`。
- 更新 `WorkerSARHooks.pre_llm()` 在 `prune_history()` 前调用 `self._ctx.refresh_runtime_state()`，确保每轮 LLM 请求前状态已自动刷新。
- 更新 `WorkerContextConfig` 增加 `state_mode` 字段（默认 `"semantic"`），与 Coordinator `ContextConfig` 对齐。
- 更新 `WorkerPinnedState` 增加 `state_mode` 字段，支持语义模式与 oracle 模式的渲染切换。
- `_project_runtime_state_to_pinned()` 的字段映射：
  ```python
  for key in ("position", "inventory", "step", "known_fires", "known_persons", "mission_status"):
      if key in payload and hasattr(self._pinned_state, key):
          setattr(self._pinned_state, key, payload[key])
  ```
- 自动注入的内容：当前位置 `(x,y,z)`、库存列表、当前 step、已知 fires/persons 摘要、使命状态。来源 age 标注示例：
  ```text
  - Position: (3,1,0), source barrier, age 150ms
  - Inventory: ['Water'], source barrier, age 150ms
  - Known fires: 1, source local_obs
  ```
- `_render_current_state()` 和 `_render_environment_view()` 渲染自动注入的状态，同时兼容旧的 `_extract_pinned()` 增量更新。
- 在 `sar_orch/worker.py` 的 `SARWorker.start()` 中创建 `SARWorkerStateProvider` 实例，传入 `barrier`、`agent_idx` 和可选的 `semantic_map_url`。
- 全链路参数传递：`create_worker_a2a_server` → `AgentAdapter` → `build_controller` → `session_factory` → `WorkerContextManager`，逐层增加 `state_provider` 参数支持。
- 更新 `sar_orch/prompts/worker/system.md`：降低对 `get_agent_state()` 的强调，改为提示 LLM 每轮 Context Memory 中已自动注入位置/库存/step，`get_agent_state` 仅作为调试和确认工具使用。
- 保留 `get_agent_state` 工具作为 LLM 可见的细粒度调试/确认工具，但 Worker 不再主要依赖它获取基础状态。
- 保留 `query_shared_memory` 工具作为 debug/fallback；语义模式下 Worker Context 已包含已知对象摘要。
- `get_agent_state` 等工具的 `_extract_pinned()` 与 `refresh_runtime_state()` 的自动注入是互补关系：自动注入保证基础状态每轮可见，工具调用后的 extract 更新增量变化（如工具执行后的新位置）。
- 清理 `worker_agent/context.py:418-528` 中陈旧的 `CoordinatorPinnedState`/`CoordinatorContextManager` 副本（已在 `router_agent/context.py` 中定义，且 worker 侧不应持有 coordinator 的状态类），或与 router 版本保持同步。
- 新增 `tests/test_worker_state_provider.py`，覆盖：snapshot 构建、版本化缓存、刷新降级、payload 字段映射、Context 注入渲染。
- 后续再引入 ROS2 adapter，将实时传感器状态接入 `WorkerLocalWorldModel`，当前阶段聚焦 Context 自动刷新机制完善。

### 阶段 5：自动观测汇报

> **状态：已完成（commit a118a6e）**
>
> 已实现：
> - `ToolResult` 增加 `data: dict | None` 字段，支持结构化 observation 载荷传递。
> - `SARBarrier._build_structured_obs()` 在每步执行后提取可见对象，映射为 `ObservationRecord` 兼容的 dict 列表。类型映射：`Fire→fire`、`Person→person`、`Reservoir→reservoir`、`Deposit→deposit`、`AbsAgent→agent`、`Flammable→fire`。
> - 新增 `sar_orch/tools/worker/_barrier_helpers.py`，提供模块级 `_publisher` 和 `tool_result_from_barrier()` 共享辅助函数，所有 SAR 动作工具统一使用。
> - 9 个动作工具（NavigateTo、Move、Explore、CarryPerson、DropOffPerson、GetSupply、UseSupply、StoreSupply、ClearInventory）写入 `ToolResult(data=...)`。`report_observation` 保留为 LLM 补充汇报工具。`get_agent_state`（GPS 无 step 消耗工具）不产生结构化观测。
> - 新增 `sar_orch/observation_publisher.py` 中的 `WorkerReportPublisher`，提供 worker 端去重：`apply_to_data()` 过滤出已变化观测，保留 position/inventory 元数据。
> - `sar_orch/worker.py` 创建 `WorkerReportPublisher` 并调用 `set_publisher()` 注入模块级 publisher。
> - Agent 框架 `step_callback("tool_result", ..., data=result.data)` 传递结构化数据（worker_agent + router_agent 对称更新）。
> - `A2AWorkerSink.emit("tool_result")` 在 `[DATA]` JSON 块中包含 `structured_data`。内容限制统一提升至 12000。
> - Coordinator 端 `_extract_auto_observations()`（`src/a2a/coordinator/server.py`）支持新旧两种格式提取，跨格式去重基于 `object_type:name:step` 键。
> - `SemanticMapStore` 增加 `max_observations`（默认 1000）有界保留策略，`_merge_locked()` 中 sources 列表截断至 50 条。
> - 新增 `set_max_observations(n)` 方法用于运行中配置。
> - 移除 `WorkerSARHooks` 中未使用的 `publisher` 参数和 dead code（去重已由模块级 publisher 覆盖）。
> - 新增 `tests/test_auto_observation.py`（21 个测试用例）。
>
> 验证：
> - `ruff check src/ sar_orch/ tests/` 通过。
> - `ruff format --check` 通过。
> - `pytest tests/test_auto_observation.py tests/test_report_observation.py tests/test_coordinator_push_callback.py tests/test_semantic_map.py tests/test_worker_state_provider.py tests/test_sar_barrier_observability.py tests/test_semantic_tools.py` 全部通过。
> - `pytest tests/` 225 通过 / 6 个预设失败（与本次变更无关）。
>
> 保留：
> - `report_observation` 保留为 LLM 可见的补充/调试工具。
> - `get_agent_state` 保留为 LLM 可见的细粒度确认工具。
> - `_extract_worker_data_blocks()` 仍然用于 agent_executor 的日志记录路径。
> - ROS2 adapter 延后到后续阶段。

### 阶段 6：对比实验

先采用消融实验分别验证每项改动，避免同时改变通信 schema、状态获取和汇报方式后无法归因：

| 组别 | 通信工具 | 状态获取 | 汇报 |
|------|----------|----------|------|
| A 基线 | 多工具 | LLM 主动查询 | LLM 主动汇报 |
| B | `send_message` | LLM 主动查询 | LLM 主动汇报 |
| C | 多工具 | Context 自动注入 | LLM 主动汇报 |
| D | 多工具 | LLM 主动查询 | 规则自动汇报 |
| E | `send_message` | Context 自动注入 | 规则自动汇报 |

使用 benchmark 比较：

- 完成率。
- 平均步数。
- `timeout_agents` 数量。
- token usage。
- 状态查询工具调用次数。
- 漏报率或 semantic map 完整度。
- INPUT_REQUIRED 恢复成功率。
- 非法工具参数率。
- 工具调用重试率。
- 错误路由率。
- Context snapshot 刷新失败率和状态年龄。
- `TASK_STALE` 误报率和漏报率。
- 从真实停滞到告警的检测延迟。
- 同一告警的重复事件数。
- Watchdog 触发后 Coordinator 的恢复、取消或重派成功率。

只有在自动 `TaskStatusView` 能稳定表达等待、结果和 INPUT_REQUIRED 状态后，才单独实验隐藏 `query_task_events`。

## 11. 风险与约束

| 风险 | 说明 | 缓解 |
|------|------|------|
| `send_message` 语义过宽 | LLM 可能误用 message type | 使用强枚举和参数校验 |
| `who` 与 task id 冲突 | 两个字段可能指向不同 Worker | 已有任务只信任 `related_task_id`，由 TaskStore 推导 Worker |
| 丢失任务生命周期信息 | 如果不保留 task id，会破坏 snapshot 恢复 | `related_task_id` 条件必需 |
| 日志分析变差 | 全部记录为 `send_message` 会失去语义 | 日志中记录底层语义事件 |
| Context 注入过大 | 全量地图会增加 token | 注入摘要、delta、高优先级对象 |
| Context 与运行时后端耦合 | 直接依赖 Barrier/ROS2/store 会降低复用性 | 注入 StateProvider 和统一 DTO |
| 高频刷新增加延迟 | 一个 env step 内可能多次 LLM 调用 | 使用版本化快照，只读取不主动采集 |
| 自动汇报污染 semantic map | 规则解析错误会写入错误事实 | 增加来源、置信度、冲突检测 |
| 自动汇报噪音过多 | 事件过密影响分析 | 去重、节流、只汇报变化 |
| 自动汇报绕过 A2A | 本地直写 store 无法支持独立部署 | 统一通过 WorkerReportPublisher 和 A2A push 链路 |
| Watchdog 将心跳误判为进展 | 卡死任务持续发送心跳而永不告警 | 分离 `last_contact_at` 与 `last_progress_at` |
| Watchdog 重复唤醒 Coordinator | 每次 tick 重复产生同一告警 | 使用监督状态机和幂等事件键 |
| Watchdog 与 Coordinator 并发运行 | 产生两个同 context 的编排循环 | 事件进入现有 loop；未来通过单一 WakeQueue 和 context lock 恢复 |
| 阈值与 Barrier 冲突 | 正常等待其他 Agent 时被误判 stale | 阈值与 STEP_TIMEOUT、wall-clock limit 联合配置并做实验校准 |
| 系统时钟变化影响 deadline | wall clock 回拨或跳变导致错报 | 运行期使用 monotonic time，持久化 wall-clock 用于恢复 |
| 监督告警被普通事件淘汰 | EventStore 每个 task 有容量上限 | 使用独立 SupervisionStateStore，EventStore 只保存追踪副本 |
| Watchdog 跨线程访问运行时 store | TaskStore/WorkerRegistry 不提供跨线程保护 | 第一版固定在 Coordinator event loop 内运行 |
| A2A push 未更新 Worker contact | 心跳中断时可能误报不可达 | 分别维护 heartbeat 与 contact，push callback 解析 worker 后更新 contact |
| 新任务立即被判 stale | 初始时间缺失或使用零值 | 从 task created 时间初始化并设置 grace period |
| 派发映射尚未建立 | 极早到达的 push/help 只有 worker task id，暂时无法 reply/cancel | 保留 worker task 事件，映射建立后关联；门面返回可重试的 `task_not_routable_yet` |

## 12. 保留项

以下能力不建议删除：

- Worker 领域动作工具，例如 `use_supply`、`carry_person`、`drop_off_person`。它们对动作约束、日志和评估很重要。
- 底层 `dispatch_task`、`respond_worker`、`cancel_task` 实现。它们应变为内部 service/helper，而不是被直接删除。
- `query_sar_state` 在 oracle benchmark 和 debug 中仍有价值。
- `query_task_events` 的系统能力仍需要保留，只是不一定继续暴露给 LLM 作为主路径工具。
- 通用 cron/Scheduler 不属于当前短周期 SAR 监督范围，TaskWatchdog 不提供 LLM 管理工具。

## 13. 待确认设计问题

- 当前方案只收敛 Coordinator 到 Worker 的通信工具，不合并 Worker SAR 领域动作工具，也不将 Worker 到 Coordinator 汇报统一为同一个 LLM 门面。
- 是否允许同一 Worker 同时存在多个活跃任务，需要在 TaskStore 和 Worker 执行模型中明确；在此之前不能仅依赖 `who` 表达已有任务。
- ROS2 pose、inventory、hazard 等状态的权威 topic/service 需要在 adapter 设计时逐项确定。
- 自动状态注入属于所有 Context strategy 的基础运行时输入，而不是特定 memory strategy 的实验特性。
- TaskWatchdog 的默认阈值需要结合实际 barrier 等待分布和 benchmark 数据确定，不能直接复制 Hermes 的 60 秒 ticker。

## 14. 结论

推荐采用“LLM 层收敛、系统层保留强语义、Context 层自动刷新”的方案。

也就是：

```text
LLM 层：一个 send_message 门面
系统层：保留 dispatch/reply/cancel 的强语义实现
日志层：继续记录具体语义事件
Context 层：通过 StateProvider 接管 task status、team status、map status
汇报层：结构化观测经 A2A push 进入 Coordinator，不跨节点直写 store
监督层：TaskWatchdog 只生成去重后的 actionable event，不周期调用或并发唤醒 LLM
```

这个方向可以减少 LLM 工具选择负担，同时保留 A2A 生命周期、可观测性和实验数据质量。
