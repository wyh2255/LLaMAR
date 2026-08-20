---
日期: 2026-07-26
文档类型: 系统架构文档
文档概述: SAR agentic 编排的显式 MissionGraph DAG、物理 A2A dispatch 与排他 TeamPartition peer mail 协作模型 — 三层状态机、生命周期、授权与恢复机制的当前实现说明。
---

# Agentic 显式 DAG 路由与 Team Partition 策略

本文描述 SAR `orchestration_mode="agentic"` 路径下，Coordinator 如何把"计划声明、依赖顺序、物理派发安全、团队通信授权和终止清理"从 LLM 提示词收回到框架层。Router 负责提出逻辑任务及参与者；框架负责验证、占用、建队、派发、聚合和回收。

该设计按 6 个 Phase 实施，已全部完成并合入主分支（`d67b0a2`/`bd56f29`/`b0104a6`/`c0f2c60`/`f4da71b`/`6c93f15`/Phase 6），详见 [`docs/plans/2026-07-20-explicit-dag-team-partition-peer-mail-progress.md`](../plans/2026-07-20-explicit-dag-team-partition-peer-mail-progress.md)。DAG 模式（`RouterAgent.DAGPlan` + `_execute_dag_loop()`）保持不变，与本文描述的 agentic 模式并存，互不干扰。

已知未实现的两处能力见 §3.2：多 Mission 并发（`ActiveMissionRegistry`，设计里明确保留的未来扩展点）与节点级 team 释放/复用（同一 Mission 内共享参与者的多人节点目前无法在 Mission 结束前互相复用队伍）。

## 1. 术语

- **Mission**：一次外部请求在 Coordinator 内形成的完整 agentic 编排实例，由 `context_id` 标识。
- **逻辑节点 / logical node**：`MissionGraph` 中的 DAG 节点，由 Router 通过 `update_plan` 声明，例如 `rescue-thomas`。表示一个逻辑目标，不等于一次 Worker A2A 任务。
- **参与者 / participant**：执行一个逻辑节点的 Worker ID 集合。单参与者节点也走同一套激活流程，只是不需要多成员 TEAM_UPDATE。
- **物理派发 / physical dispatch**：发往某一个 Worker 的一次 A2A 任务，及其状态、Worker 侧任务 ID、结果缓冲和取消记录。物理 ID 格式为 `dsp_<uuid>`，由 `MissionRuntime` 生成，opaque 且不可预测。
- **TeamPartition**：`TeamPartitionRegistry`/`TeamPartitionService` 维护的在线 Worker 通信划分。任何在线 Worker 同一时刻只能属于一个 active team。
- **peer mail**：Worker 到 Worker 的直接、签名 MAIL 旁路（见 [`peer_mail.md`](peer_mail.md)）。用于协作，不改变 DAG 节点完成状态，也不替代 Coordinator 对任务的派发权。这是与 TeamPartition 独立的另一套团队机制（`CoordinatorTeamRegistry`），见 §3.3 结尾说明。
- **VERIFIED**：`COMPLETED` 的可选加强形态，预留给未来引入的 verifier 复核路径。当前未实现 verifier，所有 "completed/verified" 表述等价于 `COMPLETED`。
- **skipped**：逻辑节点的声明态豁免标记，由 Router 在 `update_plan` 中声明；对依赖它的节点视同 `COMPLETED`。已进入 `activating/active` 的节点不能被改回 skipped。
- **team_partition_revision**：`TeamPartitionRegistry` 内部单调递增的拓扑版本号，用于 cache key 与观察日志；与 `team_epoch`（Coordinator-global membership generation）不同——前者标识拓扑快照的版本，后者标识 Worker 成员关系的代次。

### 交叉文档

- 现有框架说明：[`框架.md`](框架.md)
- 现有数据流：[`data_flow.md`](data_flow.md)
- 系统架构入口导览：[`系统架构概览.md`](系统架构概览.md)
- Peer mail 说明：[`peer_mail.md`](peer_mail.md)

## 2. 三层模型

### 2.1 MissionGraph：逻辑 DAG

`MissionGraph`（`src/a2a/coordinator/mission_graph.py`）保存一次 Mission 的逻辑真相。每个节点由 `MissionNodeSpec` 声明：

- `logical_id`：Router 声明的逻辑节点 ID，在该 Mission 内唯一；
- `participant_ids`：非空、无重复的 Worker ID 列表；兼容旧调用时由 `worker_id` 归一化为单元素列表；
- `objective` 与按 Worker 划分的 `assignments`；
- `depends_on`：只允许引用本图节点，不能自依赖，不能形成环；
- 声明状态（pending/skipped），不允许覆盖框架维护的执行状态。

`MissionGraph.replace(specs)`（`mission_graph.py:322`）在写锁内做完整验证后原子替换：拒绝对 `activating`/`active` 节点的移除或参与者/依赖/objective 变更（`mission_graph.py:330-352`）。`get_node()`/`get_ready_nodes()`/`can_activate()` 只接受 logical ID，不查询物理 namespace。

运行态节点状态机：

```text
planned -> blocked -> ready -> activating -> active -> completed
                                               │
                                               ├-> failed
                                               └-> canceled
```

- `blocked`：存在尚未成功终态的依赖；
- `ready`：全部依赖为 completed/verified，参与者和资源满足激活前置条件；
- `activating`：已经 claim，但还在 team saga 或物理 dispatch acceptance 中；
- `active`：team 已安装（或已跳过 saga），且至少一个物理派发被接受或正在执行；
- `completed`：全部参与者物理派发成功终态；
- `failed`：任一参与者失败/取消，或 activating 阶段的 team/acceptance 失败；
- `canceled`：被 `MissionRuntime.abort()` 或显式取消终止；
- `skipped`：仅存在于声明态，对依赖它的节点视同 completed。

三个终态（completed/failed/canceled）不允许回退。逻辑节点的完成是其全部 `PhysicalDispatch` 聚合结果的结果，不是 peer mail 的结果。

### 2.2 PhysicalDispatch：物理 A2A dispatch

`PhysicalDispatch`（`mission_runtime.py:110`）是每个 Worker 的独立物理记录，包含 `dispatch_id`（`dsp_<uuid>`）、`logical_node_id`、`worker_id`、`worker_task_id`、canonical physical state、有界 artifact/result 快照、cancellation outcome 与 finalization guard。

物理记录先在 `MissionRuntime.create_dispatches()`（`mission_runtime.py:229`）一次性预分配为 `PREPARED`，再进行网络 I/O；分配失败会回滚已分配的记录（`:246-249`）。每个参与者对应一个 dispatch；多个 dispatch 共同构成一个逻辑节点的 fan-out。物理查询、取消、watchdog、push callback 和周期 sync 一律用 `dispatch_id` 查询，不做"两个字典都试一遍"的兼容尝试。

### 2.3 TeamPartition：排他通信分区

`TeamPartitionRegistry`/`TeamPartitionService`（`team_partition_service.py:182,756`）是 Coordinator 生命周期级的通信拓扑权威，供 Team ACK saga 使用：

- 每个在线 Worker 恰好属于一个 active team；
- 默认队伍为 `solo:<worker_id>`（`ensure_singletons()`，`:204`），singleton 不需要多成员 TEAM_UPDATE，但仍是有效的 peer-mail authorization domain；
- singleton 的 membership epoch 来自 Coordinator-global 计数器（`self._global_epoch`，registry 内部单调递增），确保旧 epoch 的 TEAM_UPDATE/REVOKE 被拒绝；
- 多人临时协作队伍只在一个逻辑节点 `activating`/`active` 期间存在，例如 `team:rescue-thomas:<revision>`；
- 该逻辑节点终态后，其成员的 team fence **不会**自动释放（见 §5.6 的限制说明）——只有整个 Mission `abort()` 时才会释放；
- 任一 Worker 不能同时属于两个 collaborative team，也不能在 `INSTALLING`/`COMPENSATING`/`DEGRADED` 的 transition lease 中被另一个节点 claim。

Team ID、密钥、epoch、endpoint roster、transition ID 均由框架生成；Router 只能声明 participants 和 objective。

**与 peer-mail 的 `CoordinatorTeamRegistry` 的关系**：`TeamPartitionRegistry` 支持多个并发队伍（`[Alice,Bob]` 与 `[Charlie,David]` 可并存）；而 `enable_peer_mail=True` 时通过 `ConfigureTeamTool`/`DisbandTeamTool`/`SyncTeamTool`（`sar_orch/coordinator.py:412-456` 注入）操作的 `CoordinatorTeamRegistry`（`team_registry.py:67`）**只维护一个全局 team**（类文档字符串："Maintains exactly one team at a time"）。两者是完全独立的两套团队机制，服务不同目的：`TeamPartitionService` 是 Team ACK saga 的权威（本文主题），`CoordinatorTeamRegistry` 是 peer-mail 工具集使用的单一 roster（详见 [`peer_mail.md`](peer_mail.md)）。Router 需要记得先调用 `configure_team` 才能使用 peer mail；这个手动步骤未被自动化。

三层关系：

```text
MissionGraph (逻辑真相)
  logical node rescue-thomas
       │ participants=[Alice, Bob]
       │ depends_on=[scout-north]
       ▼
PhysicalDispatch (执行真相)
  dsp_7f... -> Alice -> worker_task_id=...
  dsp_91... -> Bob   -> worker_task_id=...
       │ terminal aggregation only
       ▼
TeamPartition (通信真相)
  team:rescue-thomas:r42 = [Alice, Bob]
  solo:Charlie             = [Charlie]
  solo:David               = [David]
```

TeamPartition 是 MissionGraph 的运行时派生拓扑，不产生新的逻辑依赖，也不改变节点结果。PhysicalDispatch 是逻辑节点的物理展开，不拥有新的逻辑目标。

## 3. Mission admission 与状态所有权

### 3.1 单一 active agentic Mission

`ActiveMissionAdmission`/`MissionRuntimeManager`（`mission_runtime.py:1138,1151`）是硬约束：同一 Coordinator 同时只允许一个 active agentic Mission。`_execute_agentic()` 创建 `TaskStore` 前先调用 `manager.admit(context_id)` 取得 lease；第二个并发请求返回 `mission_already_active`，不会覆盖第一个 Mission 的状态。

Admission lease 的所有权范围覆盖：`MissionGraph`、`PhysicalDispatch` map、结果缓冲、`TaskWatchdog`/state-provider attachment、`TeamPartition` transition ownership、callback/future 清理。

### 3.2 已知未实现的能力

两处能力在设计上被预留，但当前代码中确认未实现：

- **`ActiveMissionRegistry`（多 Mission 并发）**：未实现，也不是默认启用的组件——只有在 `EventStore`、`TaskWatchdog`、state projection、push callback、artifact buffer、Future registry 和 `TeamPartition` lease 都按 `context_id` 隔离后，才能将单一 admission 扩展为多 Mission。目前用 admission 失败换取安全，而不是承担数据串线的风险。
- **节点级 team 释放/复用**（见 §5.6）：`release_node_team()` 只在整个 Mission `abort()` 时调用一次，逻辑节点单独 `completed` 不会释放其 participant 的 team fence。同一 Mission 内想串联多个共享参与者的多人节点，目前会在第二个节点卡在 `participant_busy`，没有比"整 Mission 结束"更细粒度的释放/迁移路径。

### 3.3 运行态所有权

```text
Coordinator lifetime
  TeamPartitionService
  persisted control secret + global membership epoch (0600, mission_runtime.py:1248-1257)
  PartitionTransition journal
  ActiveMissionAdmission

Mission lifetime (one context_id)
  MissionRuntime
  MissionGraph
  PhysicalDispatch records
  result/artifact buffers
  watchdog/provider attachment
  callback/future ownership

Worker lifetime
  WorkerTeamState (one active membership)
  MailboxStore
  peer sender/inbound authorization context
```

跨层引用一律通过明确的 owner API；不通过 module/global cache 查找当前 Mission 或当前 team——`task_store.py:38-60` 的模块级 `_global_future_registry`/`_worker_to_dispatch_map` 是 pre-MissionRuntime 时代的遗留兼容路径，只在没有 `MissionRuntime` 实例（`store._runtime is None`）时才是激活路径。

## 4. ID 命名空间与 canonical 物理状态机

### 4.1 logical / physical ID 完全分离

1. Logical ID 由 Router 在 `MissionGraph` 中声明，只在 `MissionGraph` API 中解释。
2. Physical dispatch ID 由 `MissionRuntime` 用 `uuid4` 生成，格式 `dsp_<uuid>`（`mission_runtime.py:277`），opaque、不可预测。
3. Worker A2A task ID 是 Worker/SDK 产生的第三个 ID，不冒充 logical ID 或 dispatch ID。
4. `MissionRuntime._worker_to_dispatch`（实例作用域，非模块级）负责 `worker_task_id -> dispatch_id` 解析（`:175,329,347`）；对外查询、取消和 callback 路由必须显式给出对应命名空间。
5. 结果、artifact、watchdog、cancel、help reply 都先解析到唯一 `PhysicalDispatch`。

### 4.2 唯一 canonical `apply_physical_status()`

`TaskStore.apply_physical_status(dispatch_id, raw_state, source, observed_at, result=None)` 是改变 `PhysicalDispatch` lifecycle 的唯一入口。以下来源全部调用它：

- awaited dispatch acceptance；
- `/a2a/push-callback`（`server.py:833`，`manager.active_runtime is not None` 时走此路径）；
- `_periodic_state_sync()`（`agent_executor.py:975`，优先检查 `store._runtime` 存在时走此路径）；
- native cancellation 的结果；
- timeout、parent cancel、shutdown cleanup；
- startup recovery/reconcile。

该方法负责：将 A2A/protobuf 原始状态映射为有限 canonical enum；检查 transition 是否允许，拒绝终态回退；第一次终态时保存有界结果并设置 finalization guard；只触发一次 `MissionGraph.mark_dispatch_terminal()` 和逻辑聚合；产出派发级事件；将重复、过晚或无法归属的 callback 记录为 bounded diagnostic，不改变新 Mission。

物理状态机：

```text
                         ┌──────────────┐
                         │ INPUT_REQUIRED│
                         └──────┬───────┘
                                │ help/reply
                                ▼
PREPARED -> DISPATCHING -> ACCEPTED -> RUNNING ───────┐
    │          │            │          │              │
    │          │            └──────────┴──────────────┤
    │          │                                       ▼
    │          └──────────────> CANCEL_PENDING -> CANCELED
    │                                             or FAILED
    │
    └──────────────────────────────────────> FAILED (acceptance error)

ACCEPTED/RUNNING/INPUT_REQUIRED -> COMPLETED | FAILED | CANCELED
COMPLETED/FAILED/CANCELED are terminal; terminal state never regresses.
```

`INPUT_REQUIRED` 是 active 物理状态，不是逻辑完成；`MAIL` 也不是完成信号。父级终止时若无法确认远端取消，保留 `CANCEL_PENDING`，不伪装成 `CANCELED`。

### 4.3 逻辑聚合规则

- 所有参与者 dispatch 为 `COMPLETED` 时，逻辑节点变为 `completed`；
- 任一参与者为 `FAILED` 或 `CANCELED` 时，逻辑节点变为 `failed`（当前无 retry policy）；
- 任一参与者为 `INPUT_REQUIRED` 时，逻辑节点保持 `active`，帮助请求只路由到对应 dispatch；
- 依赖豁免：声明态为 `skipped` 的节点在 `depends_on` 检查中等价于 `completed`；
- 聚合由第一个物理终态触发的 canonical 方法完成 exact-once 保护。

## 5. `activate_plan_node` 时序

`send_message(message_type="activate_plan_node", related_task_id=<logical_id>)` 是 Router 激活节点的正常入口（`send_message.py:393`，委派给 `MissionRuntime.activate_plan_node()`，`mission_runtime.py:603`）。它不接受 `who` 或 `content`；participants、objective、assignment 全部来自已验证的 `MissionGraph`。

### 5.1 六阶段时序

成功路径：

```text
Router
  │ update_plan(full declarative graph)
  ▼
[1 DAG gate]
  │ ready? dependencies succeeded? admission lease?
  ▼
[2 participant claim]
  │ claim node + every Worker; reject busy/transition lease
  ▼
[3 Team ACK saga（有 delivery adapter 时）/ 直接跳过（无 adapter 时，见 5.4）]
  ▼
[4 awaited fan-out acceptance]
  │ preallocate opaque dispatch IDs
  │ dispatch every participant concurrently
  │ await each first A2A acceptance and save worker_task_id
  ▼
[5 physical terminal aggregation]
  │ push/sync/cancel/timeout -> apply_physical_status()
  │ all terminal -> logical node terminal
  ▼
[6 release / reconcile]
  │ newer-epoch singleton restore or next-node direct transition
  │ release claims, clear buffers, emit final events
```

失败路径（任何阶段失败都走 release/reconcile，不允许悬空）：

```text
[1 DAG gate]            fail -> 返回稳定错误 (node_not_ready / participant_busy / ...)
                                 无 claim 写入，直接返回

[2 participant claim]   fail -> 整体回滚已写入的 fence/PREPARED dispatch
                                 返回 participant_busy 或内部错误

[3 Team ACK saga]       fail -> transition COMPENSATING
                                 ├─ all compensation ACK  -> COMPENSATED（释放 claim，返回 team_setup_failed）
                                 └─ compensation timeout   -> DEGRADED（保留 fence/journal，等待 reconcile）
                                 任何情况下都不发送 participant task

[4 fan-out acceptance]  Nth participant fail -> 已接受集合走 canonical 取消
                                                无法确认 -> CANCEL_PENDING
                                                logical node = failed -> [6]

[5 physical terminal]   任一 dispatch FAILED/CANCELED -> logical node failed
                                                        finalizer 取消其它 dispatch -> [6]

[6 release/reconcile]   任何失败 -> DEGRADED，保留 fence/journal，不释放 admission
```

### 5.2 阶段 1：DAG gate（`mission_runtime.py:698`，`_run_dag_gate`）

无锁预检，按顺序返回稳定错误：

1. `related_task_id` 对应逻辑节点存在，否则 `node_not_found`；
2. 节点为 `ready`，否则 `node_not_ready`（含 `dependency_incomplete` 细分）；
3. 当前 `context_id` 持有 active admission lease；
4. MissionGraph 层 claim 检查：`participant_is_busy()`；
5. TeamPartition 层 fence 检查：`registry.check_participants_free()`（对所有节点生效，不止多参与者节点）；
   任一检查失败均返回 `participant_busy`。

阶段 1 是无锁快照检查，两个并发 `activate_plan_node` 可能同时通过；最终判定在阶段 2 的原子 claim 中重做一次，以阶段 2 结果为准。

已声明 logical ID 的图管理任务必须用 `activate_plan_node` 激活；`dispatch_task` 对未知 ID 返回 `undeclared_task`，要求先 `update_plan` 声明（`dispatch_task.py:88-101`，仅当 `mission_node_count > 0` 时生效）。

### 5.3 阶段 2：participant claim（`mission_runtime.py:734`，`_run_atomic_claim`）

在 mission-scoped 锁内原子完成：在锁内重跑竞争检查（`can_activate()` + `participant_is_busy()` + TeamPartition fence，任一失败返回 `participant_busy`）；将 logical node 置为 `activating`；写入 mission-scoped claim/fence；预留 transition ownership；预分配全部 `PhysicalDispatch` 为 `PREPARED`；固化 objective/assignments/team epoch 预期。claim 未释放前，另一个逻辑节点不得接管该 Worker。

### 5.4 阶段 3：Team ACK saga（`mission_runtime.py:826-830` 门控，`team_partition_service.py` 状态机）

多人节点从当前 singleton 迁移到目标 collaborative team。TeamPartitionService 在 registry lock 内创建不可变 delivery plan，网络发送在锁外执行；每个目标 Worker 必须返回 terminal ACK。

**仅当 `team_service.has_delivery_adapter` 为真时才运行该 saga**。`enable_peer_mail=False`（`experiment.py` 默认配置）时没有配置真实 delivery adapter，多人节点与单人节点一样直接跳过 saga——这不是简化，而是修复了一个曾出现的永久死锁：没有 adapter 就不可能收到任何 ACK，saga 必然超时进入 `DEGRADED`，而 `DEGRADED` 按设计永久保留其 participant fence，没有任何生产代码路径能清除它，会导致该 Worker 之后的每一次激活（包括单人节点）永久失败在 `participant_busy`。Worker 独占性本身已经由 `MissionGraph` 自己的 `_ACTIVE_CLAIM` 状态机保证，这个 fence 只在 Worker 间 TEAM_UPDATE/REVOKE 通知真正接线（`enable_peer_mail=True`，生产路径）时才有额外价值。跳过 saga 时 `transition` 为 `None`，直接进入阶段 4。

配置了真实 delivery adapter 时，saga 按以下状态机运行：

- 全部 TEAM_UPDATE ACK：`PartitionTransition=INSTALLED`，节点继续；
- 任一发送/ACK 超时或拒绝：不派发任何 participant task，transition 进入 `COMPENSATING`；
- compensation ACK 全部成功：`COMPENSATED`，节点释放 claim，返回 `team_setup_failed`；
- compensation 失败/超时：`DEGRADED`，保留 member fence 和 transition journal，返回 `team_setup_failed`，等待 service/recovery reconcile。

Team ACK 是"网络 saga 已完成"的证据，不是分布式原子事务。日志只记录 worker ID、transition ID、epoch、ACK outcome，不记录 secret。

### 5.5 阶段 4：awaited fan-out acceptance（`mission_runtime.py:921`，`dispatch_prepared_many`）

Team 安装完成（或跳过）后，`MissionRuntime` 用预分配的 `PhysicalDispatch` 构造每个 Worker prompt；prompt 含 team ID、成员 ID、objective、assignment，不含 peer secret、Coordinator secret、签名、原始 endpoint credential。

每个 dispatch 依次经历 `PREPARED -> DISPATCHING -> ACCEPTED`，all-or-nothing 语义：等待全部 acceptance；第一份 A2A acceptance 返回前不宣称成功。若第 N 个 participant acceptance 失败：已接受集合走 canonical 取消流程；无法确认的记为 `CANCEL_PENDING`；logical node 为 `failed`；activation 整体返回 `dispatch_acceptance_failed`。不使用仅创建后台 task 即返回的旧 `DispatchTaskTool.execute()` 作为 graph activation primitive。

### 5.6 阶段 5/6：物理终态、release 与 reconcile

Worker 的 push 或周期 sync 只更新对应 dispatch。`apply_physical_status()`/`_aggregate_graph_terminal()`（`task_store.py:446-471`）首次聚合出 logical terminal 后只调用 `MissionGraph.mark_dispatch_terminal()`，**不会**触发任何 TeamPartition 相关动作。

**已知限制：节点级 team 释放未实现，只有整 Mission abort 才释放 fence。** `release_node_team()`（`team_partition_service.py:896`）在全代码库只有一个调用点：`MissionRuntime.abort()`（`mission_runtime.py:582`），即只在整个 Mission 终止时才会释放 team fence。逻辑节点单独 `completed` 并不会释放其 participant 的 fence——`TeamPartitionRegistry.prepare_activation()` 对任何仍被 fence 的成员会无条件抛 `ParticipantBusyError`，没有代码路径识别"同一 Mission 的下一节点"并跳过这个检查或复用已装好的 team。实际效果：一旦某个多参与者节点安装了 collaborative team，其成员会在**整个 Mission 剩余时间**保持 fenced（`participant_busy`），后续想复用同一批 Worker 的节点会在 DAG gate / atomic claim 卡住，直到 Mission `abort()` 释放。`TransitionStatus.RELEASE_PREPARING`（`team_partition_service.py:44`）这个枚举值已声明，但在当前代码中从未被赋值使用（不是真实存在的 transition，只是预留但未接线的状态）——同一 Mission 内串联多个共享参与者的多人节点，目前没有比"整 Mission abort/reconcile"更细粒度的团队释放/复用路径。这与 §3.2 的 `ActiveMissionRegistry` 一样，是一个明确的未实现能力，不是已完成的优化。

释放（Mission abort 时）不是"删除状态"：先完成或记录 team delivery outcome，再解除 member fence；未解决的远端取消或 membership ACK 以 `CANCEL_PENDING`/`DEGRADED` 真实呈现。

## 6. TeamPartition transition、epoch 与 Coordinator recovery

### 6.1 `PartitionTransition` 状态机

`PartitionTransition`（`team_partition_service.py:89`）持久记录：`transition_id`、`context_id`、`source_node_id`、before/after `TeamAssignment` snapshot、`affected_workers`、`epoch`、`status`（`TransitionStatus` 枚举）、`member_deliveries`、`team_secret`/`team_id`、`compensation_plan`、`reason`、`created_at`/`updated_at`。

```text
                         all UPDATE ACK
                 ┌──────────────────────────┐
                 │                          ▼
PREPARING -> INSTALLING ----------------> INSTALLED
                  │  │                       │
       ACK/网络失败│  │ timeout               │ node terminal
                  ▼  │                       ▼
             COMPENSATING ----------------> RELEASE PREPARING
                  │  all compensation ACK       │
                  ▼                             ▼
             COMPENSATED                 INSTALLING(singletons/next team)

INSTALLING or COMPENSATING
          │ compensation/ACK timeout or unrecoverable mismatch
          ▼
       DEGRADED

DEGRADED：保留 fence、before/after snapshot 和 journal；禁止新 claim，
由 service/recovery 以更大 epoch reconcile，直到出现明确 terminal outcome。
```

`DEGRADED` 不是成功，也不是静默回滚，表示网络世界与 Coordinator 记录尚未一致。

### 6.2 全局持久 membership epoch

`MessageEnvelope.team_epoch` 的语义是 Coordinator-global membership generation，而不是每个 team 各自从 1 开始的计数器——`TeamPartitionRegistry._global_epoch`（`team_partition_service.py:195`）单调递增，跨所有 team 共享。这样 Alice 从 `solo:Alice` 移到 rescue team 再回 singleton 时，Worker 只用一个单调代数就能判断旧 TEAM_UPDATE/REVOKE 是否可接受。

Coordinator control state（含 control secret、下一 epoch、非 secret topology snapshot、`PartitionTransition` journal）原子写入且权限 `0600`（`mission_runtime.py:1248,1257`）。`WorkerTeamState` 同样持久化 `last_generation` 并以 `0600` 保存单 active team（`src/a2a/worker/team_state.py`），把收到的 epoch 解释为全局 generation；相同 team/epoch/内容可幂等重装，旧 revoke 不能删除更新后的新 membership。

### 6.3 Coordinator restart/recovery

启动恢复顺序：读取 Coordinator persisted control state 和 transition journal → 分配严格大于已知本地/Worker generation 的新 epoch（绝不重置为 1） → 将此前未完成的 activating/active logical nodes 标记为 abort/reconcile（不做隐式 resume） → 取消或查询已知 PhysicalDispatch，无法确认的记为 `CANCEL_PENDING` → 以新 epoch 为所有 online Worker 重新建立 singleton → 解决 `DEGRADED` transition 的 fence/reconcile 后才释放 admission 并接受新 Mission。`server.py:478` 的 `lifespan()` 在 `startup_recovery` 阶段调用 `MissionRuntimeManager.abort("startup_recovery")` 实现这一流程的清理部分。

恢复过程幂等：重复启动、重复 TEAM_UPDATE、迟到旧 callback、旧 TEAM_REVOKE 都不会把 Worker 恢复到旧队伍，也不会修改新 Mission。

## 7. Parent timeout/cancel/shutdown：`MissionRuntime.abort()` finalizer

`MissionRuntime.abort(reason)`（`mission_runtime.py:538`）是唯一 parent-level cleanup primitive，当前接入的调用点：

| 入口 | 调用位置 | reason |
|---|---|---|
| 正常 mission 完成 | `agent_executor.py:450` | `mission_complete` |
| executor 显式取消 | `agent_executor.py:898` | `executor_cancel` |
| Coordinator 启动恢复 | `server.py:478` | `startup_recovery` |
| Coordinator 正常关闭 | `server.py:516`,`1671` | `coordinator_shutdown` |
| 显式取消端点 | `server.py:592` | `explicit_cancel` |

finalizer 执行顺序（`mission_runtime.py:538-591`）：冻结新激活 → 枚举 context-owned 的非终态 dispatch，`asyncio.wait(..., timeout=30.0)` 有界等待远程取消 ACK → 未确认的标记 `CANCEL_PENDING` → 未完成的 future 标记 `CANCELED` → 若配置了 `TeamPartitionService` 则调用 `release_node_team(context_id, ack_timeout=10.0)` 做 team 补偿/降级 → 清空 `_worker_to_dispatch`/`_futures` 映射 → 持久化最终状态（`_persist()`） → 释放 `ActiveMissionAdmission` lease（`self._manager._release(self)`）。

`abort()` 在锁内检查 `self._aborted` 实现幂等：二次调用直接返回。`CANCEL_PENDING`/`DEGRADED` 保留为事实，不会为了让父任务显示成功而强制写成 completed。

## 8. Worker peer mail、`/team-status` 与 Environment State

### 8.1 Worker peer mail 的 team authorization 边界

peer mail 是 Worker 直接连接 Worker，Coordinator 只负责安装/撤销 roster 和授权材料，不作为 MAIL relay。基础协议、MAIL/TASK/TEAM_UPDATE 的种类与 HMAC 规则见 [`peer_mail.md`](peer_mail.md)。授权判定同时要求：envelope 的 recipient 是接收 Worker 本身；`team_id` 与当前 active membership 相同；`team_epoch` 与当前 global generation 相同；sender ID 在当前 team roster 内且不是 self；HMAC 用当前 team secret 校验成功；Worker signer 只能发 `MAIL`，不能借 MAIL ingress 发 `TASK` 或伪造 Coordinator directive。

Worker 的单 active team 状态由 `WorkerTeamState` 提供原子 `delivery_snapshot()`（`src/a2a/worker/team_state.py:257-283`），把 team、epoch、members、endpoint 和 secret 作为同一锁下的 immutable snapshot 返回。

### 8.2 `/team-status` 认证

`/team-status` 端点（`server.py:1179-1230`）由 `TeamStatusProof.verify()` 校验请求携带的 timestamped HMAC proof，proof 绑定请求中的 `agent_id`，配合 nonce store 防 replay。成员关系查询 `TeamPartitionService.get_assignment(agent_id)`，不从 semantic map 重建 membership。singleton team 返回空 peer list；collaborative team 通过 `TeamStatusProof.safe_team_view()` 返回脱敏成员列表（不含 team secret、Coordinator secret、HMAC、签名、原始 token）。认证 proof 只存在于传输层请求，不注入 Environment State 或日志。

### 8.3 `/team-status` 缓存

`SARWorkerStateProvider.fetch_team_status_async()`（`sar_orch/worker_state_provider.py:64-87`）缓存键为 `(env_step, team_generation, known_server_revision)` 三元组；同一 SAR step 内的 team 迁移或服务端 `team_partition_revision` 更新都会触发重新请求。Worker context 的 version tuple 为 `(env_step, mailbox_version, team_generation)`（`:132`）。

### 8.4 Environment State 渲染边界

Coordinator 与 Worker 分开渲染，不把物理 dispatch、全局拓扑和秘密混为一段 prompt：

- Coordinator 渲染 Mission DAG（节点状态、依赖、participants、team ID/epoch）与 Physical Dispatches（Worker、dispatch ID、脱敏状态）；
- Worker（`sar_orch/worker_state_provider.py:193-206`，`team_summary`/`team_coordination` 构造）只渲染当前 Worker 可用于行动的 safe fields：`team_id`、`team_epoch`、当前 member IDs、objective、team peer 的 position/inventory/task status、mailbox unread summary；不渲染 team secret、Coordinator secret、原始 endpoint credential、HMAC/signature、request proof。

Environment State 是观察投影，不是状态 authority；Router 不能通过修改 prompt 中的 team 文本获得授权，Worker ingress 和 Coordinator service 一律依据当前 partition/epoch 重新判定。

### 8.5 禁止进入日志/Environment State 的字段

Coordinator secret、team secret、HMAC、签名、request token、原始 signed envelope、peer endpoint credential、认证 proof、mail body/subject（除非有明确脱敏审计字段）不得出现在观察日志、EventStore event data、Environment State、异常文本或实验 payload 中。

## 9. 核心数据流与状态机图

### 9.1 核心数据流

```text
External A2A request
        │ task_id/context_id/query
        ▼
CoordinatorAgentExecutor
        │ acquire ActiveMissionAdmission
        ▼
MissionRuntime + MissionGraph
        │ Router: update_plan (logical specs only)
        │ framework: DAG gate / claims
        ▼
TeamPartitionService ── signed TEAM_UPDATE ──> WorkerTeamState × N
        │ (跳过 saga 时：无 delivery adapter，直接进入下一步)
        │ all ACK / 已跳过
        ├───────────────────────────────> exclusive TeamPartition
        │
        │ preallocate dsp_<uuid> records
        ▼
awaited fan-out A2A dispatch ────────────> Worker Agent × N
        │ acceptance -> worker_task_id
        │
        ├── push callback ─┐
        ├── periodic sync ─┼──> TaskStore.apply_physical_status()
        ├── cancel result ──┘             │ exact-once terminal guard
        │                                  ▼
        │                         MissionGraph aggregation
        │                                  │ logical terminal
        ▼                                  ▼
release/reconcile TeamPartition <── MissionRuntime.abort/finalizer
        │
        └──> Coordinator/Worker Environment State + secret-free events
```

### 9.2 逻辑节点激活与回收状态机

```text
                 deps incomplete
          ┌─────────────────────────┐
          ▼                         │
       BLOCKED ── deps success ──> READY
                                     │ activate gate
                                     ▼
                                ACTIVATING
                               │          │
                  team saga fail│          │ team ACK（或跳过）+ acceptance
                               ▼          ▼
                             FAILED      ACTIVE
                                           │
                   any dispatch fail/cancel│ all dispatch terminal success
                                           ▼                 ▼
                                        FAILED          COMPLETED
                                           └──────┬──────────┘
                                                  │ release/reconcile
                                                  ▼
                                           claims released
```

### 9.3 物理状态、逻辑聚合和团队状态的分工

| 事件 | 物理层动作 | 逻辑层动作 | TeamPartition 动作 |
|---|---|---|---|
| acceptance | `DISPATCHING -> ACCEPTED` | 节点保持 activating/active | 保持已安装 team（或已跳过） |
| Worker running | `ACCEPTED -> RUNNING` | 节点 active | 保持 team |
| input required | `RUNNING -> INPUT_REQUIRED` | active，绑定 help dispatch | 保持 team |
| one terminal failure | 该 dispatch terminal | 节点 failed | finalizer 取消其它已接受 dispatch，随后 release |
| all terminal success | 所有 dispatch terminal | 节点 completed | **不释放**（见 §5.6 已知限制），fence 保留至 Mission abort |
| parent timeout/cancel | nonterminal -> CANCEL_PENDING/CANCELED/FAILED | mission aborting/canceled | compensation/reconcile，不遗留 lease |
| late/duplicate callback | 忽略或 bounded diagnostic | 不变 | 不变 |

## 10. 错误处理与不变式

### 10.1 主要失败信号

以下是实际观察到的失败标识，具体呈现形式因来源而异（工具错误字符串、状态枚举值、或通用 `reject`/`reason` 结果，未必是逐字的独立错误码）：

| 信号 | 触发条件 | 呈现方式 |
|---|---|---|
| `mission_already_active` | 已有其它 active agentic Mission | `ActiveMissionAdmission.admit()` 拒绝 |
| `undeclared_task` | graph-managed run 中派发未知 logical ID | `dispatch_task.py:93` 工具错误字符串 |
| `planned_worker_mismatch` | legacy worker_id 与声明的 participant_ids 不一致 | `dispatch_task.py:99` 工具错误字符串 |
| `node_not_found` / `node_not_ready` / `dependency_incomplete` | DAG gate 预检失败 | `_run_dag_gate()` 返回值 |
| `participant_busy` | Worker 有 active dispatch、其它 claim 或非终态 transition lease | DAG gate / atomic claim 均可返回 |
| `team_setup_failed` | Team ACK 全部/compensation 失败 | Team ACK saga 终态 |
| `dispatch_acceptance_failed` | participant A2A acceptance 未成功 | `dispatch_prepared_many()` 返回值 |
| `CANCEL_PENDING` | 取消请求已发出但远端终态未确认 | `PhysicalState` 枚举值 |
| `DEGRADED` | team saga compensation 失败/超时 | `TransitionStatus` 枚举值 |
| `team_status_unauthorized` | `/team-status` proof 缺失/过期/伪造 | `server.py:1201` HTTP 403 |
| 旧 epoch 拒绝 | TEAM_UPDATE/REVOKE epoch 不新 | `WorkerTeamState._reject_stale()` |
| Worker 发 TASK / 跨队 mail 拒绝 | 非当前 roster/epoch/secret/recipient | `EnvelopeIngress` 通用 `action="reject"` + `reason` |

### 10.2 不变式

| 编号 | 事实 | 验证重点 |
|---|---|---|
| I1 | 一个 active agentic Mission admission lease 只归一个 `context_id` | 第二 Mission 不覆盖第一 Mission 状态 |
| I2 | `MissionGraph` 只含 logical ID；`PhysicalDispatch` 只通过 opaque dispatch ID 查询 | ID 碰撞、拼接和跨 namespace 测试 |
| I3 | 每个 online Worker 恰好一个 active team | singleton 建立、多人队伍并存、release 恢复 |
| I4 | 一个 Worker 不能同时被两个 active/transition node claim | 并发 activation 返回 `participant_busy` |
| I5 | Team ACK 全部成功（或已跳过）以前，不发送 participant task | partial ACK 不得出现 Worker task |
| I6 | `apply_physical_status()` 是唯一物理状态权威，终态单调且 exact-once 聚合 | duplicate/out-of-order push、sync、artifact |
| I7 | peer mail 只在当前 team、epoch、roster、secret 下被接受 | 同队成功、跨队和旧 epoch 拒绝 |
| I8 | peer mail 本身永不改变逻辑 node terminal state | MAIL/读取事件不能完成 DAG |
| I9 | parent timeout/cancel/shutdown/recovery 都调用同一个 abort finalizer | 见 §7 调用点表 |
| I10 | epoch 全局持久且 Worker generation 单调 | Coordinator restart 后严格更大，旧 revoke 不生效 |
| I11 | Environment State 与日志不含 secret、token、签名、原始 credential | 见 §8.5 |
| I12 | topology、DAG、dispatch 输出稳定排序且 bounded | 相同状态不同插入顺序得到同一渲染 |

## 11. 相关文档

- [`框架.md`](框架.md) — 系统整体框架（目录结构、配置、prompt 体系）
- [`系统架构概览.md`](系统架构概览.md) — 精简入口导览，含 has_delivery_adapter 等已修复问题的现状确认
- [`data_flow.md`](data_flow.md) — 事件系统分类总览、context_id/task_id 对照表
- [`peer_mail.md`](peer_mail.md) — Worker-to-Worker peer mail 协议、授权矩阵、已知限制
- [`logging_map.md`](logging_map.md) — 每个日志文件的精确记录点
