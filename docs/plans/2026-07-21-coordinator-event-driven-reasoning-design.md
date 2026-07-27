# Coordinator 事件驱动推理架构设计

**状态：** APPROVED（设计）— 最终独立审核 `APPROVE`；仅准入 E0 实施准备，尚未修改业务源码
**日期：** 2026-07-21
**范围：** SAR `agentic` Coordinator；不改变 Worker 的自主执行模型
**关联文档：**

- `docs/plans/2026-07-21-coordinator-explicit-feedback.md`：任务反馈的状态投影方案
- `docs/plans/2026-07-11-communication-tool-consolidation-and-context-state.md`：Watchdog / Context / 未来 WakeQueue 的既有边界
- `docs/system_docs/data_flow.md`、`docs/system_docs/框架.md`：A2A 与上下文主链路

---

## 0. 30 秒决策卡

### 要解决的问题

Coordinator 的 `send_message(assign_task)` 成功只表示异步消息/物理派发已接受，不表示 Worker 已完成。当前 `Agent.run()` 在工具返回后会立即进入下一次 LLM 调用；若 Worker 尚未产生新状态、地图也未产生有效变化，Coordinator 仍会重复推理，既浪费 token，又可能重复分配、反复查询或产生相互矛盾的决策。

### 本设计的结论

引入一个**Coordinator 所属、单消费者、事件驱动的决策运行时**：

1. 先由现有权威状态源完成状态写入；
2. 再发布一个带版本、可去重、可批处理的 `CoordinatorDecisionEvent`；
3. `WakeArbiter` 决定该事件是仅记录、合并，还是唤醒下一次 Coordinator 决策；
4. 被唤醒后，Coordinator 不信任事件 payload 作为事实，而是重新读取 `MissionRuntime`、`SemanticMapStore`、`SupervisionStateStore`、`TaskStore` 等权威快照；
5. Context 明确渲染“为何这次被唤醒”，从而让任务完成/失败、Worker 求助、地图突变和规则告警成为显式反馈，而非让 LLM 从当前地图猜测；
6. 在任务派发批次已经稳定后，Coordinator 进入等待；**普通 env step、RUNNING heartbeat、重复观察都不触发 LLM**。

### 不是本设计要做的事

- 不把 `send_message` 改为同步等待 Worker 完成；这会破坏异步并行。
- 不让每个事件各自启动一个新的 Coordinator Agent loop。
- 不把现有 A2A 输出 `EventQueue` 当作 Coordinator 的输入/唤醒队列。
- 不用 prompt 要求 LLM “记得等待”作为正确性机制。
- 不为语义地图保存每一步完整历史快照；保留当前快照 + 有界 delta / 事件因果即可。
- 不将 `SARBarrier` 改成 `asyncio` 同步原语，也不跨线程直接操作 `asyncio.Queue`、`asyncio.Event` 或 `AgentController` 的锁。

---

## 1. 已核验的当前行为与根因

### 1.1 当前实际调用链

```text
用户 / experiment.submit_task()
  → A2A CoordinatorAgentExecutor._execute_agentic()
    → MissionRuntime admission
    → 新建 TaskStore，并注入 StateProvider / TaskWatchdog
    → AgentController.submit(context_id, user_request, ...)
      → 持有该 context 的 asyncio.Lock
      → 新建一个 Router Agent，并 attach ContextManager
      → Agent.run() 内部 while step < max_steps
        → CoordinatorSARHooks.pre_llm()
          → prepare_runtime_state() / refresh_runtime_state()
        → LLM
        → Tool 批次（包括 send_message）
        → 直接 step += 1，下一轮 LLM
```

已核验的关键代码事实：

| 事实 | 证据 |
|---|---|
| Agentic coordinator 的整个编排由一次 `controller.submit()` 驱动；任务完成前不会自然返回等待外部事件。 | `src/a2a/coordinator/agent_executor.py:305-450` |
| `AgentController.submit()` 对同一个 `context_id` 持锁，并在一次 submit 内新建 Agent、attach Context、await `agent.run()`。 | `src/Agent/controller/controller.py:172-250` |
| `Agent.run()` 每次执行完一整批 tool calls 后仅 `step += 1`，随后立即开始下一轮 LLM；当前没有 tool-batch 后的 suspend/wake hook。 | `src/Agent/router_agent/agent.py:465-512`、`661-799` |
| Coordinator hook 每次 LLM 前都会调用 `prepare_runtime_state()`、`refresh_runtime_state()`，但这只是刷新 Context，并不决定是否应该推理。 | `src/Agent/router_agent/hooks.py:70-84` |
| `send_message` 包含 `assign_task`、`activate_plan_node`、`reply_to_help`、`cancel_task`；成功派发不等价于 Worker 结果。 | `src/a2a/builtin_tools/send_message.py:25-133` |
| `activate_plan_node` 已可按 MissionGraph 读取 participant/objective，完成 Team ACK 后 fan-out 多个 physical dispatch，是当前最适合作为“派发批次”边界的 API。 | `src/a2a/builtin_tools/send_message.py:393-472` |

因此，问题不是 StateProvider 没有刷新，而是：**没有一个确定性组件在“工具结果已完整写入消息历史”与“下一次 LLM 请求”之间决定是否应当等待外部事实。**

### 1.2 为什么当前反馈过于隐式

Worker 的异步反馈现在主要进入下列存储，随后仅在下一次恰好发生的 `pre_llm` 被 Context 投影：

```text
Worker A2A push callback
  → MissionRuntime canonical physical transition
  → EventStore status/artifact/help/observation records
  → TaskWatchdog contact/progress/supervision state
  → SemanticMapStore.ingest_observation()
  → 下一次 Coordinator pre_llm() 的 StateProvider.snapshot()
```

这有三个缺口：

1. **没有唤醒语义。** 状态被写入不意味着正在运行的 Coordinator 会停止空转或在正确时刻恢复。
2. **没有因果提示。** Context 主要显示当前状态；Coordinator 需要从地图/任务列表自行推断“这是刚完成、刚失败，还是仅仅地图当前如此”。
3. **地图 delta 的计算时机倒置。** 当前 `SARCoordinatorStateProvider.prepare_for_llm()` 才读取地图 revision、计算 `MapDiffCalculator.diff()` 并可调用 `MapSummarizer`。如果没有下一次 LLM，本身不会产生一个可用于唤醒的地图变化信号。见 `sar_orch/coordinator_state_provider.py:89-166`。

### 1.3 当前组件的真实边界

| 组件 | 当前职责 | 不能误认为它已经负责的事 |
|---|---|---|
| `MissionRuntime` / `PhysicalDispatch` | context-bound 的物理派发权威、callback fence、状态转移、abort/recovery | 通用事件队列、每个转移的通用 wake revision |
| `EventStore` | 线程安全的 worker 事件记录和 NDJSON 追踪 | 事件调度、消费 cursor、ack、无界/完整事件历史；每 task 内存上限为 500，且 agentic run 开始时会 `clear()` |
| `TaskWatchdog` + `SupervisionStateStore` | 以单个 asyncio task 生成去重 stale/unreachable/deadline 告警，并保存未确认告警 | 唤醒 Coordinator、启动另一个 Agent loop；代码/历史文档的“只告警”描述还需与 hard deadline 的实际 remote cancel 行为对齐 |
| `SemanticMapStore` | 当前语义快照、revision、观测融合 | 判断任意 revision 是否值得 LLM 推理；agent observation 也会递增 revision |
| `MapSummarizer` | 对指定 delta 进行有预算、单飞、超时隔离的 LLM 摘要 | 主 Coordinator 的事件源或调度器；它当前由 pre-LLM 准备阶段调用 |
| `SARBarrier` | 以 `threading.Event` / `threading.Lock` 同步跨 Worker loop 的 env step | Coordinator asyncio event loop 的队列 owner |
| A2A `event_queue` 参数 | 向 A2A 客户端输出 task status / artifact stream | Coordinator 内部等待 Worker 反馈的输入队列 |

特别说明：`TaskWatchdog` 文档字符串仍写“只生成告警、不自动取消”，但当前 hard deadline 分支会调用 `runtime.cancel_dispatch_remote(...)`（`src/a2a/coordinator/task_watchdog.py:351-378`）。事件设计必须以代码真实行为为准：hard deadline 是**确定性安全动作 + 高优先级重规划信号**，不能再假设它只写一条告警。

---

## 2. 设计目标、术语与不变量

### 2.1 设计目标

1. **无事不推理。** 已完成派发批次且无有效新事实时，Coordinator 不产生额外 LLM 调用。
2. **反馈显式化。** 每个新的 Coordinator 决策都携带有限、结构化的 `Why awakened` 原因；当前状态仍来自权威快照。
3. **异步不变。** Worker 继续并行执行；Coordinator 等待事件不阻塞 Worker、barrier 或 A2A callback。
4. **单 context 单决策者。** 同一 active mission 同时最多一个 Coordinator LLM decision epoch；事件只排队，不能重入。
5. **确定性优先。** 事件分类、去重、阈值、固定检查器、唤醒与节流由框架执行；prompt 仅解释已发生的框架决策。
6. **线程安全与可恢复。** 遵守 ADR-011；事件丢失/重复不应造成无限睡眠或跨 mission 污染。
7. **可观测、可灰度、可回退。** 在真正 gate LLM 前可 shadow 记录“本会唤醒/本会抑制”的决策。

### 2.2 核心术语

| 术语 | 定义 |
|---|---|
| **原始事件（raw event）** | callback、map mutation、barrier step、watchdog finding 等源侧通知；并非都值得唤醒 LLM。 |
| **决策事件（decision event）** | 经 normalizer / policy 分类后，可被 WakeArbiter 处理的不可变 envelope。 |
| **唤醒（wake）** | 允许一个等待中的 Coordinator 进入下一次 LLM decision epoch，不等于“事件已处理”。 |
| **决策批次（DecisionEventBatch）** | 在一个防抖窗口内合并的一个或多个 decision event；通常只产生一次 LLM epoch。 |
| **决策 epoch** | 从获得 event batch、刷新权威状态、调用一次或有限次本地续航 LLM，到进入等待/完成的闭环。 |
| **派发批次（dispatch batch）** | 一次逻辑节点 activation 或明确的批量派发提交；其成功表示 outbound command 已建立，不表示 Worker 完成。 |
| **状态栅栏（state fence）** | event payload 所携带的 map revision / dispatch revision / supervision event id 等最低可见版本，用于确保决策前读取的快照不早于事件。 |

### 2.3 不变量

1. **先状态、后事件。** 任何 event publisher 都必须先完成权威状态写入，再投递事件；事件 payload 永远不是状态真相。
2. **只有一个 WakeArbiter。** 不允许 Watchdog、Map、Barrier、UI 分别直接 `LLM.generate()` 或启动第二个 `AgentController.submit()`。
3. **一条 event 不等于一轮 LLM。** 所有事件都先经过优先级、去重、合并、节流和可行动性判定。
4. **只在完整 tool batch 后挂起。** 一个 assistant message 的所有 tool calls 都必须已有 tool result；不得在 tool-call / tool-result 对中间暂停或插入外部 user message。
5. **事件可重复，动作必须幂等。** 唤醒语义采用 at-least-once；状态转移、cancel、activation 与分配仍由现有 `MissionRuntime` / `TaskStore` fence 保证幂等。
6. **context/mission fence 必须存在。** 迟到 callback 或旧队列 event 不得唤醒新 mission。只用当前 `MissionRuntimeManager._epoch` 不足：它主要在 recovery 时推进，正常 `admit()` 不会为每个 mission 建立新 epoch（`mission_runtime.py:1285-1306`、`1327-1339`）。
7. **普通 step 不直接唤醒。** `ENV_STEP_COMPLETED` 只驱动确定性检查器与状态对账；除非检查器发现阈值/不变量变化，否则不调用 LLM。
8. **安全取消可以抢占，但不重入。** 用户 cancel / shutdown /硬 deadline 的确定性取消可立即执行；普通 priority event 在当前 tool batch 的安全边界后处理。

---

## 3. 目标架构

### 3.1 总体结构

```text
                         authoritative state plane
 ┌─────────────────────────────────────────────────────────────────────┐
 │ MissionRuntime / TaskStore / EventStore / SupervisionStateStore     │
 │ SemanticMapStore / SARBarrier / OperatorCommandStore                │
 └─────────────────────────────────────────────────────────────────────┘
         │  ① authoritative mutation committed
         ▼
 ┌──────────────────────────┐      append-only audit plane
 │ Source adapters           │ ───► CoordinatorDecisionJournal (NDJSON)
 │ - physical transition     │
 │ - push callback/report    │      (audit/cursor only, not state truth)
 │ - watchdog                │
 │ - map delta classifier    │
 │ - barrier/checker bridge  │
 │ - user command ingress    │
 └─────────────┬────────────┘
               │ ② normalized, fenced event
               ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ CoordinatorEventBus (CoordinatorServer owner loop)                │
 │  - context + mission-run fence                                    │
 │  - dedupe / priority lanes / bounded pending set                  │
 │  - thread-safe ingress via loop.call_soon_threadsafe              │
 └─────────────┬────────────────────────────────────────────────────┘
               ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ WakeArbiter                                                       │
 │  - Wake / RecordOnly / Coalesce / DeterministicOnly policy        │
 │  - debounce, batch, rate-limit, no-lost-wake cursor               │
 │  - one active decision epoch per context                           │
 └─────────────┬────────────────────────────────────────────────────┘
               ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ CoordinatorDecisionSession                                        │
 │  - persistent Agent.run session / safe-point gate                 │
 │  - fresh StateProvider snapshot at state fence                     │
 │  - explicit DecisionTrigger context rendering                      │
 │  - post-tool-batch quiescence policy                              │
 └─────────────┬────────────────────────────────────────────────────┘
               │ ③ coordinator tool actions
               ▼
          MissionRuntime / SendMessageTool / DAG activation
```

### 3.2 组件职责

| 新组件（均为**提议名称**） | Owner | 责任 | 明确不负责 |
|---|---|---|---|
| `CoordinatorEventBus` | `CoordinatorServer` 的 owner event loop | 接收、fence、去重、缓存 pending event、等待 batch | 保存任务/地图真相、做 LLM 决策 |
| `CoordinatorEventNormalizer` | source adapter | 将 callback、watchdog finding、map delta 等翻译成统一 envelope | 直接调用 LLM |
| `WakeArbiter` | 每个 active mission 一个 | 按 policy 选择 wake / merge / suppress，生成 `DecisionEventBatch` | 从 event payload 推断完整任务真相 |
| `CoordinatorDecisionSession` | `CoordinatorAgentExecutor` | 维护一次 mission 的 Agent conversation、决策 epoch、safe point | 新建多个并发 Agent session |
| `CoordinatorTurnPolicy` | DecisionSession | 根据完整 tool batch 与 canonical dispatch frontier 决定 `CONTINUE_LOCAL` / `WAIT` / `FINISH` | 依赖 LLM 自己说“我已经派发完” |
| `MapImpactClassifier` | Semantic map adapter | 用确定性 MapDiff 判定地图变化是否可能改变调度 | 调用主 Coordinator LLM |
| `ConditionScheduler` | Coordinator owner loop | 运行有界、确定性的电量/步骤预算/工作流不变量检查 | 轮询调用 LLM |
| `CoordinatorDecisionJournal` | log dir | 记录 ingress、suppression、batch、delivery、decision outcome | 取代 EventStore 或 MissionRuntime 持久化 |

### 3.3 两条平面必须分离

- **事实平面：** `MissionRuntime` 的 `PhysicalDispatch` 是物理任务状态权威；`SemanticMapStore` 是语义地图当前事实；`SupervisionStateStore` 是监督告警权威；`EventStore` 是 callback 证据和历史摘要。
- **调度平面：** EventBus / WakeArbiter 只回答“现在是否值得让 Coordinator 再想一次”。

这条分离直接解决“地图不保存上一 step 内容怎么办”：Coordinator 不需要完整地图历史来知道为何被叫醒。它得到的是一个带 `base_revision → revision`、对象集合和原因码的**有界 delta 事件**，然后读取当前地图快照做决策。若需要审计，查询 event journal，而不是把每份完整地图快照永久塞进 Context。

### 3.3.1 三条通道不得混用

| 通道 | 当前/提议 owner | 保存什么 | 绝不作为 |
|---|---|---|---|
| **Physical authority channel** | `MissionRuntime` / `TaskStore` / `SemanticMapStore` / `SupervisionStateStore` | 当前可执行物理状态、地图事实、监督状态 | 一条“是否该推理”的 FIFO 信号 |
| **Diagnostic log channel** | 当前 `EventStore`，以及提议的 `CoordinatorDecisionJournal` | callback/artifact/observation 证据、事件投递与 batch 审计 | 物理状态的第二权威或 command queue |
| **Wake bus** | 提议 `CoordinatorEventBus` + `WakeArbiter` | 有界 pending event、优先级、coalesce、delivery cursor | 持久任务状态、地图真相、任务成功证明 |

只有 Physical authority channel 可以改变任务/环境事实；Diagnostic log 可以丢弃老的展示记录但不能影响任务状态；Wake bus 可以重复投递但不能直接派发 Worker。这个三通道边界也禁止把现有 A2A 输出 `event_queue` 误接为 Wake bus。

### 3.4 唯一仲裁点与原子 claim

`WakeArbiter.claim_next_batch()` 是同一 mission 唯一允许从 `WAITING` 进入 `DECIDING` 的线性化点，必须在 Coordinator owner loop 内原子完成下列操作：

1. 检查 `context_id + mission_run_id` 仍是当前 admission；
2. 从 pending set 取出满足 policy 的最高优先级事件及其可合并事件；
3. 写入 `decision_id`、event sequence range、`DELIVERED` journal record；
4. 把 session 状态置为 `DECIDING`，阻止第二个 consumer claim 同一批；
5. 返回 batch 后才允许 Context refresh / LLM。

epoch 结束进入 `WAITING` 时，同一临界区必须先检查是否已有更新 sequence；若有则直接 claim 下一批，不得先 clear 一个裸 event 再等待。任何 source adapter 都不得绕过该仲裁点直接调用 Agent 或 LLM。

---

## 4. 事件契约与唤醒策略

### 4.1 统一 envelope（提议 DTO）

```text
CoordinatorDecisionEvent
  event_id: UUID / ULID
  sequence: int                         # Bus 内严格单调
  context_id: str
  mission_run_id: str                   # 每次 admit 的独立 generation，不复用旧 epoch 语义
  kind: EventKind
  source: str                           # push_callback / watchdog / map / barrier / user / sync ...
  priority: CRITICAL | HIGH | NORMAL | LOW
  occurred_at_monotonic: float
  occurred_at_wallclock: ISO-8601
  dedupe_key: str
  coalesce_key: str | None
  state_fence: {
      dispatch_id?, dispatch_revision?, worker_task_id?,
      map_base_revision?, map_revision?, env_step?,
      supervision_event_id?, condition_revision?
  }
  related: {worker_id?, logical_node_id?, team_id?, task_id?}
  payload_preview: bounded JSON         # 仅展示；不是权威事实
  cause_event_ids: list[str]            # 聚合后保留来源
```

要求：

- `payload_preview` 必须有大小上限、字段白名单和文本截断；Worker 文本是外部数据，不能作为 system instruction。
- `mission_run_id` 应是每次 admission 新建并持久化/可审计的 ID；不要复用当前仅在 recovery 推进的 manager epoch。
- `state_fence` 用于 decision 前最低版本检查：例如 event 带 `map_revision=31`，则状态投影至少要读到 revision 31；读到更高 revision 是允许的。
- `event_id` 的“已投递给 epoch”与 Supervision 的“已解决/acknowledged”不是同一概念。前者可自动记录；后者必须由确定性状态变化、显式处理语义或现有 Supervision 规则决定，不能在 LLM 看见事件时盲目 ack。

### 4.1.1 ID 命名空间与权威 owner

| ID | owner / 当前来源 | 允许用途 | 禁止用途 |
|---|---|---|---|
| `context_id` | A2A top-level context / `MissionRuntime` | 关联一个用户会话和 active mission | 作为物理 dispatch 或 worker task lookup 的 fallback |
| `mission_run_id`（提议） | admission 与 EventBus registration 的同一临界区 | fence 迟到 event、journal 分区、session lifecycle | 复用当前仅 recovery 推进的 `_epoch` 而不说明 admission 语义 |
| `logical_node_id` | MissionGraph | DAG dependency / activation frontier | 直接路由 worker callback |
| `dispatch_id` | `MissionRuntime` 的 opaque `PhysicalDispatch` | canonical physical state、cancel、状态转移 | 被 EventStore 文本或 user task ID 替代 |
| `worker_task_id` | Worker A2A | callback / state-sync 的第三方任务关联 | 跨 context 查询或作为 logical node ID |
| `event_id` / `sequence` | DecisionJournal / EventBus | 去重、delivery cursor、审计 | 作为任务成功或 alert 已解决的证明 |

`mission_run_id` 的分配、`MissionRuntimeManager.admit()` 成功与 EventBus `register_mission()` 必须在同一 owner/manager 临界区完成。预检通过不构成 event 投递授权；发布时仍要原子复检 active context/run，避免旧队列在新 mission admission 后穿透 fence。

### 4.2 事件分类表

| EventKind | 当前/提议来源 | 默认动作 | 原因 |
|---|---|---|---|
| `MISSION_STARTED` | 新 mission admission | 立即 wake | 首次规划必须发生 |
| `OPERATOR_COMMAND` | 新的受控用户命令入口 | HIGH wake；`cancel` 走确定性路径 | 用户指令不能等待普通 worker event |
| `PHYSICAL_STATE_CHANGED` | 仅 canonical accepted transition | terminal / failed 高优先级 wake；`RUNNING` record-only；**不承载 help 语义的 `INPUT_REQUIRED`** | 防止普通 ACK / heartbeat 空转 |
| `DISPATCH_REJECTED` | outbound activation/assign 被拒绝 | HIGH wake（有界） | 当前决策的前提失效，需要恢复/重规划 |
| `WORKER_INPUT_REQUIRED` | A2A `TASK_STATE_INPUT_REQUIRED` + help text | CRITICAL/HIGH wake | Coordinator 必须回复或重配 |
| `WORKER_REPORT` | artifact / structured observation | policy 分类后 coalesced wake 或 record-only | 不是每条上报都值得 LLM |
| `MAP_MATERIAL_CHANGE` | `MapImpactClassifier` | NORMAL/HIGH coalesced wake | 新火源、人员状态、强度、冲突、stale 等可能改变计划 |
| `SUPERVISION_ALERT` | TaskWatchdog 新产生的 active alert | warning/stale NORMAL；deadline/unreachable HIGH | Watchdog 已做去重，应避免再次轮询 |
| `ENV_STEP_COMPLETED` | Barrier step bridge | record-only，触发 checker | step 增长本身不是 Coordinator 决策理由 |
| `CONDITION_RAISED` | 固定检查器 | 按 severity wake | 电量/步骤预算/工作流不变量跨阈值 |
| `MISSION_FINISHED` | barrier completion validator | HIGH wake 或确定性 finalization | 允许 Coordinator 执行明确 `finish_task` |
| `RECONCILIATION_REQUIRED` | state sync / startup recovery / safety tick | 仅发现真实分歧时 wake | 防止丢 push 导致永久等待 |

### 4.3 优先级与抑制规则

| 优先级 | 代表事件 | 行为 |
|---|---|---|
| `CRITICAL` | user cancel、shutdown、worker `INPUT_REQUIRED`、mission terminal | 立即使等待者可运行；若当前在执行 tool batch，则在该 batch 完整后优先处理。cancel 不等待 LLM。 |
| `HIGH` | task FAILED/COMPLETED/CANCELED、deadline exceeded、worker unreachable、material map emergency | 立即或极短防抖后创建 decision batch。 |
| `NORMAL` | stale、deadline warning、可行动 worker report、新火/新人员、重要冲突 | 同 context 在短窗口合并后 wake。 |
| `LOW` | RUNNING、heartbeat、普通 agent position/inventory、step telemetry、重复 observation | 记录/更新状态，但不直接 wake。 |

明确禁止：

- `TASK_STATE_WORKING`、重复 heartbeat、普通 A2A artifact、agent position/inventory 变化、无领域 delta 的 `NoOp`、单纯 step 增加，均不得默认触发 Coordinator LLM。
- 一条 `MAP_REVISION_ADVANCED` 不得直接等于 wake；`SemanticMapStore.ingest_observation()` 对 agent observation 也会递增 revision（`sar_orch/map/store.py:244-298`）。

### 4.4 去重、合并与背压

1. **物理状态：** dedupe key 至少为 `(mission_run_id, dispatch_id, dispatch_revision)`。当前 `PhysicalDispatch` 仅有 terminal-oriented `finalization_seq`，没有所有状态转移均递增的 revision（`mission_runtime.py:109-124`）。因此 **E0 准入前必须**新增持久化的 `dispatch_revision`，或让 canonical transition 生成等价 immutable `PhysicalTransition` record；它必须在同一 runtime lock 内、每次 accepted state change（包括 `INPUT_REQUIRED`）单调递增。不得以 EventStore `updated_at` 或 env step 替代该键。
2. **Watchdog：** 使用已有 `supervision event_id` 作为 dedupe key。只有 active alert 新建、严重级别升级或受控 reminder 才可产生新的 wake。
3. **地图：** 合并窗口内保留 `base_revision` 与最高 `revision`，合并对象/原因集合；不能用“最后一条文本覆盖”丢掉 fire/person 两类同时变化。
4. **Worker report：** 以 `(dispatch_id, report_class, semantic object)` 聚合；`INPUT_REQUIRED`、终态、显式 blocker 永不与普通 report 混合。
5. **用户命令：** 以 request id 去重但保持顺序，不与地图/状态事件合并。
6. **背压：** pending set 必须有上限；LOW 事件可计数合并，CRITICAL/HIGH 不可静默丢弃。超限时写 journal 与 health metric，触发确定性 reconciliation，而不是悄悄丢事件。
7. **无丢失等待：** WakeArbiter 使用 sequence/cursor 或受锁保护的 pending set；decision epoch 结束后必须先检查 `sequence > last_consumed` 再进入等待，不能使用会发生 clear race 的裸 `asyncio.Event` 模式。

### 4.5 事件批次而非逐事件 LLM

推荐语义：

```text
事件 e1（Alice completed）
事件 e2（Bob completed）
事件 e3（map fire extinguished）
        │ 在短防抖窗口内
        ▼
一个 DecisionEventBatch
        │
        ▼
一次“当前状态 + 为什么醒来”的 Coordinator epoch
```

这既避免了多 Agent 同时回调产生 N 次 LLM，又保留全部 cause event id 供 Context、日志和审计使用。

---

## 5. 源适配器与当前代码接线点

### 5.1 物理任务转移：唯一的高价值反馈源

**原则：** 只在 `MissionRuntime` 接受了 context-bound callback / sync transition 后发布，不在 raw HTTP callback 到达时提前发布。

当前路径：

- active runtime callback：`src/a2a/coordinator/server.py:791-885`
- 5 秒 state-sync fallback：`src/a2a/coordinator/agent_executor.py:975-1038`
- physical state authority：`src/a2a/coordinator/mission_runtime.py`

**提议：** 引入一个不依赖 SAR 的 `PhysicalTransitionObserver` protocol，以及 immutable `PhysicalTransition`：`dispatch_id`、`before_state`、`after_state`、`dispatch_revision`、`source`、`mission_run_id`、结果摘要。`MissionRuntime` 必须在同一 lock 内完成“合法性复检 → state 更新 → `dispatch_revision += 1` → persist”；只有该 accepted transition 才在锁外通知 observer。Server callback、periodic sync、watchdog remote cancel、显式 cancel 都通过同一 observer 归一化，避免各路径手工 publish 导致重复或遗漏。

```text
accepted transition (唯一可 publish 的路径)
  PREPARED → DISPATCHING → RUNNING → COMPLETED / FAILED / CANCELED
       │
       ├─ canonical runtime state updated
       ├─ dispatch_revision incremented and persisted
       ├─ EventStore evidence appended（若适用）
       ├─ supervision contact/progress/state updated（若适用）
       └─ PhysicalTransitionObserver.publish(...)
```

`CallbackResult.status == "ignored"`、context 不匹配、未知 worker task、重复同状态、非法回退均只能写诊断，**绝不 publish wake event**。尤其不能只检查 `_apply_physical_status()` 返回了 `PhysicalDispatch`：当前 API 对 duplicate 也返回 dispatch，实施时必须显式返回/传递 `accepted=False` 或上述 immutable transition，防止 `INPUT_REQUIRED` 的 push/state-sync 重复观察造成重复 wake。

### 5.2 Worker 求助、artifact 与 observation

A2A push endpoint 已能识别：

- task/status update；
- `TASK_STATE_INPUT_REQUIRED` 与 help text；
- artifact update；
- status message 内自动提取 observation 后写 EventStore / SemanticMapStore。

见 `src/a2a/coordinator/server.py:767-1059`。

提议适配规则：

1. `INPUT_REQUIRED` **只**发布一个 `WORKER_INPUT_REQUIRED` decision event，payload 只保留关联 dispatch、worker、问题摘要；物理状态仍更新到 canonical snapshot，但 normalizer 不再为同一 accepted transition 另发 `PHYSICAL_STATE_CHANGED`，避免 shadow/enabled 双 wake。
2. artifact 先保留 EventStore / progress 语义。仅当 artifact 带明确 blocking / completion / verified-result 分类时发布 `WORKER_REPORT`；普通 artifact 不直接 wake。
3. observation 先成功 `semantic_map.ingest_observation()`，再交给 Map adapter；不能因为报告文本先到就让 Coordinator 根据未融合的事实推理。
4. 可选的任务—地图影响关联只能使用 `source_task_id`、dispatch、worker、时间/revision 范围等可验证证据；在多 worker 同时观察时不得宣称“某任务导致了该地图变化”。

### 5.3 Watchdog

现有 Watchdog 已在 Coordinator event loop 中单 task 运行，5 秒 tick，生成去重的 `TASK_STALE`、`WORKER_UNREACHABLE`、`TASK_DEADLINE_WARNING`、`TASK_DEADLINE_EXCEEDED` 等状态（`src/a2a/coordinator/task_watchdog.py:108-215`、`252-420`）。

**提议接线：** 在 `TaskWatchdog` 增加可选 `actionable_event_sink`，且仅在下列时刻调用：

- 首次创建 active alert；
- severity 升级；
- 有配置的 reminder / recovery；
- hard deadline 已触发确定性 cancel 后，发布可用于重规划的终态/取消相关事件。

sink 的调用顺序为：`SupervisionStateStore.update()` 与 EventStore tracking record 成功后 → 发布 decision event。Watchdog 不拥有 queue，也不启动 Agent loop。

**hard deadline 的目标策略（E0 必须锁定）：** 保持“deadline exceeded 会尝试 remote cancel”的现码语义，而不是退回成纯告警；但该尝试必须由 `SupervisionStateStore` 中的 `cancel_attempted_for_dispatch_revision`（或等价持久化 cancel-attempt record）fence。对同一 `(dispatch_id, dispatch_revision, TASK_DEADLINE_EXCEEDED event_id)` 最多发起一次有效 remote cancel；只有显式、可审计的 retry policy 才能建立新的 attempt。当前实码在 deadline exceeded 后每 tick 都可能调用 `cancel_dispatch_remote()`，实施不得把这种重复调用当作可接受的默认行为。

### 5.4 Semantic Map：把“是否值得叫醒”从 pre-LLM 中移出

当前 `prepare_for_llm()` 做了三个本应分层的动作：读取 revision、计算 delta、可选 LLM summary。事件驱动后应拆为：

```text
SemanticMapStore mutation committed
  → MapRevisionAdapter notices revision
  → MapImpactClassifier (deterministic)
      → no material impact: record-only
      → material delta: MAP_MATERIAL_CHANGE
  → WakeArbiter decides whether to wake
  → 被唤醒后才可选执行 MapSummarizer，作为 Context enrichment
```

`MapImpactClassifier` 应复用/提炼当前 `MapSummarizer._determine_trigger()` 中的结构化条件，而不是调用 LLM：

- fire gained / intensity changed / status changed；
- person gained / status changed；
- conflict new/resolved；
- stale new/resolved；
- 可配置的步骤预算临界点。

当前 `MapSummarizer` 的 `periodic` reason 只能用于**摘要策略**，不能单独造成 Coordinator LLM 周期唤醒。其 single-flight / timeout / failure isolation 特性应保留（`sar_orch/map/summarizer.py:76-178`）。

实现上有两条可行路径，推荐 A：

- **A（推荐）：** 扩展 `SemanticMapStore.ingest_observation()` 的返回结果或增加 post-commit listener，返回不可变 `MapMutation(revision, observation, changed_keys)`；listener 在 store lock 外调度 `MapImpactClassifier`。这样不会让 server 采用两次 snapshot 的竞争性 diff。
- **B（过渡）：** server 在 observation 融合成功后向 owner loop 投递 `MAP_REVISION_ADVANCED`，debouncer 在短窗口后用 `snapshot_with_revision()` + 自己维护的 baseline 计算 delta。必须保证 baseline 与 revision 成对持有，且多 mutation 时按 revision 范围合并。

### 5.5 Barrier 与固定检查器

`SARBarrier` 明确使用 `threading.Event` / `threading.Lock`，并通过 `asyncio.to_thread()` 执行 `_execute_step()`（`sar_orch/barrier.py:1-9`、`134-219`、`350-422`）。

提议新增轻量、同步的 `BarrierStepListener`：

- 将 `_execute_step()` 改为在持有 `_step_lock` 时仅构造并返回不可变 `BarrierStepTelemetry`（step、metrics、timeout agents、error types、completed subtasks delta、finished）；
- 调用方在 `await asyncio.to_thread(self._execute_step, ...)` **返回后**、且确认不为 `None` 时才调用 listener；不得在 `_execute_step()` 函数尾部（此时 `with self._step_lock` 尚未退出）调用；
- listener 只调用 `event_bus.publish_threadsafe(telemetry_event)`，不读取/写入 asyncio 原语、不执行 I/O、不调用 LLM；
- `ENV_STEP_COMPLETED` 默认 record-only，并驱动下节的确定性 checker。

这让固定程序检查有可靠时钟，而不是让 Coordinator 每个 step 都推理。

### 5.6 用户请求

当前 `/tasks` 是创建新顶层任务的入口（`src/a2a/coordinator/server.py:477-488`）；单 active mission admission 下，不能把“运行中的用户补充指令”实现成第二个 `AgentController.submit()`。

提议新增独立的、鉴权后的 **operator command ingress**：

```text
POST /missions/{context_id}/commands
  command_id (idempotency key)
  type: MESSAGE | PRIORITY_UPDATE | ADD_OBJECTIVE | CANCEL | QUERY
  content / structured fields
```

- `CANCEL` 走当前确定性 abort/cancel 路径，不等待 LLM。
- 其余改变计划的命令写入 `OperatorCommandStore` / journal，并投递 `OPERATOR_COMMAND`。
- 唤醒后，DecisionSession 在安全边界将命令作为受限、标记来源的输入摘要投影到 Context；不允许任意 HTTP handler 并发修改 `agent.messages`。
- 纯查询可走只读 API；只有确实需要 Coordinator 判断的请求才 wake LLM。

---

## 6. 线程模型、生命周期与安全边界

### 6.1 单 owner loop

`CoordinatorServer.lifespan()` 已捕获 `self._owner_loop`，并在同一 loop 启动 Watchdog、A2A server、periodic sync（`src/a2a/coordinator/server.py:390-459`）。新的 EventBus 必须在这里创建/绑定。

建议接口：

```text
async publish(event)                 # 只能由 owner loop 调用
publish_threadsafe(event)            # 任意线程可调用
async next_batch(context_id, ...)    # 仅 DecisionSession 调用
close_mission(context_id, run_id)
```

`publish_threadsafe()` 的实现原则：捕获 owner loop，并用 `owner_loop.call_soon_threadsafe()` 调度 owner-loop 内的 enqueue。**禁止**从 barrier worker thread 直接对 `asyncio.Queue.put_nowait()`、`asyncio.Event.set()` 或 `AgentController` 内部对象操作。

### 6.2 为什么不使用“每个生产者一个 queue”

- 多 queue 会打破全局优先级、跨源合并和单一 cursor。
- Watchdog、Map、Barrier 分别唤醒，会重新制造同 context 的并发 LLM。
- 当前 agentic mission 已有 `MissionRuntimeManager` 单 admission 与 `AgentController` per-context lock；EventBus 应与之对齐，而非绕过。

### 6.3 生命周期顺序

```text
CoordinatorServer lifespan starts
  → capture owner loop
  → create EventBus + journal + checker scheduler
  → recovery / reconcile existing runtime
  → start Watchdog / state sync / A2A server

new agentic mission admitted
  → allocate mission_run_id
  → register DecisionSession + source fences
  → publish MISSION_STARTED
  → run coordinator until explicit finish / abort

shutdown / explicit cancel
  → stop accepting new events for mission_run_id
  → deterministic runtime.abort()
  → wake/cancel DecisionSession so it never stays blocked
  → flush journal, stop source tasks, close bus
```

所有迟到 event 需同时匹配 `context_id + mission_run_id`；否则写有界 diagnostic 并丢弃。不能只依据 dispatch/task text 或 module-global mapping。

---

## 7. Coordinator 决策运行时与 Agent loop 改造

### 7.1 不可采用的恢复方式

**不要**在每条 event 到达时重新调用 `AgentController.submit()`：当前 Controller 每次 submit 都新建 Agent；普通结束不会保存完整 `agent.messages`，只有 `need_input` + `task_id` 才保存 snapshot（`src/Agent/controller/controller.py:217-249`）。这样会丢失 tool conversation continuity，且会与已有 context lock 冲突。

也不要让 LLM 用 `query_task_events(timeout=...)` 充当 scheduler。该工具可继续作为调试/深查兼容接口，但它将“是否等待”交给模型且可能形成轮询，不能承担框架唤醒职责。

### 7.2 推荐：单一持久 DecisionSession + 安全点 gate

Coordinator 的顶层 A2A task 在 mission 结束前保持 `WORKING`；同一个 Agent instance / messages / ContextManager 保持存活。Agent 在每次完整 tool batch 后由 gate 决定下一轮是否等待事件。

```text
BOOTSTRAP
  └─ MISSION_STARTED permit
      ↓
DECIDING
  └─ fresh snapshot + trigger projection + LLM + complete tool batch
      ↓
CoordinatorTurnPolicy
  ├─ FINISH                  → FINISHING / Agent returns
  ├─ CONTINUE_LOCAL          → DECIDING（严格有界）
  ├─ WAIT_FOR_EXTERNAL       → WAITING
  └─ FAIL_CLOSED             → terminal framework failure / input-required policy

WAITING
  └─ WakeArbiter emits a DecisionEventBatch → DECIDING

WAITING / DECIDING
  ├─ idle recheck deadline → RECONCILING（仅 state-sync / checker，不直接 LLM）
  └─ user cancel / shutdown / mission deadline → CANCELLING → TERMINATED
```

状态约束：

- `FINISHING` 与 `TERMINATED` 是 session 终态；一旦进入，不能因迟到 event 回到 `DECIDING`。迟到 event 只写 diagnostic/journal。
- `WAITING → DECIDING` 只能由第 3.4 节的 atomically claimed batch 触发；`DECIDING` 中新到事件只进入 pending set。
- `FAIL_CLOSED` 必须终止顶层 A2A task 或转入明确的人工输入策略；不得回退为未受限的 legacy LLM while-loop。
- 一次 assistant response 的多个 tool calls 被视作一个不可分割的 command batch；当前 batch 的所有 tool result 写入后才检查 terminal / wait directive。

**安全点：** 必须位于一条 assistant message 的所有 tool call 已执行、所有对应 tool result 已追加到 `agent.messages` 后。当前恰好位于 `Agent.run()` 的 tool loop 结束、`step += 1` 之前（`src/Agent/router_agent/agent.py:661-799`）。

### 7.2.1 长等待、mission deadline 与 cancel 的单一退出状态机

当前 `CoordinatorAgentExecutor._execute_agentic()` 用 `asyncio.wait_for(controller.submit(...), timeout=self._orchestration_timeout)` 包住**整次** Agent run，默认值为 600 秒（`src/a2a/coordinator/agent_executor.py:123`、`396-404`；`src/a2a/coordinator/server.py:175`）。因此 event-driven mode **不得**直接在现有 outer timeout 内无限 `await gate.next_permit()`；否则合法 WAIT 会被误判为 orchestration failure。

提议 `EventDrivenTimeoutPolicy`，并明确将旧配置与新语义分离：

| 预算 / 信号 | 适用状态 | 行为 |
|---|---|---|
| `legacy_orchestration_timeout` | 仅 legacy continuous loop | 保留当前整次 `controller.submit()` 的 600s 保护 |
| `mission_deadline_seconds`（提议） | 全 mission wall-clock | Event-driven runner 的显式总 deadline；到期直接进入 `CANCELLING`，不伪造一轮 LLM 完成 |
| `max_active_epoch_seconds`（提议） | `DECIDING` | 只约束 Context preparation、LLM 与当前完整 tool batch；超时只 latch deadline，绝不切断未完成的 tool-call / tool-result pairing；在下一 safe point 才走明确 `FAIL_CLOSED` / cancel policy |
| `max_idle_wait_seconds`（提议） | `WAITING` | 到期只进入 `RECONCILING`，运行 state-sync / checker；无 actionable finding 则重新等待，不直接消耗 LLM 或判失败 |
| user cancel / shutdown | 任意非终态 | owner loop 内优先进入同一个 `CANCELLING`，关闭 admission、唤醒 gate、幂等 `runtime.abort()`，随后 `TERMINATED` |

实施时 event-driven path 必须移除/绕开现有“600 秒包住整个 `controller.submit()`”的语义，改由上述 mission supervisor 管理；`mission_deadline_seconds` 与 SAR experiment 的 wall-clock limit 必须在集成配置中明确对齐。当前 `sar_orch/experiment.py:200` 的 runtime wall-clock 是 3600 秒，根 `AGENTS.md` 中的旧 600 秒说明不得继续被当作有效配置；E2/E4 必须记录和校验有效 mission deadline、experiment wall-clock、A2A timeout 三者的关系。cancel、shutdown、mission deadline 只能竞争同一个 `CANCELLING` owner，先获 owner 的路径执行 abort，其他路径观察已终止状态而不重复 finish/abort。

### 7.3 建议的框架接口变化（提议）

1. 在 `AgentHooks` 增加 `after_tool_batch(agent, outcomes) -> TurnDirective`；默认实现永远 `CONTINUE_LOCAL`，从而不影响 Worker / 普通 Agent。
2. 在 `Agent.run()` 的 tool loop 完整结束后调用该 hook；不得在单个 `post_tool` 时直接 sleep。
3. `CoordinatorSARHooks` 持有本 mission 的 `CoordinatorDecisionGate`：
   - `after_tool_batch()` 将 outbound / local tool outcome 交给 `CoordinatorTurnPolicy`；
   - 下一次 `pre_llm()` 在需要等待时 `await gate.next_permit(cancel_event)`；
   - 获得 batch 后才执行当前的 `prepare_runtime_state()`、`refresh_runtime_state()`、`prune_history()`、`assemble()`。
4. 在 `Agent.run()` 开始时暴露 context/run identity 给 hook，或由 ContextManager 绑定 context-specific gate；当前 `Agent.run(..., context_id=...)` 未把该值保存为 hook 可直接读取的稳定字段，因此这是显式改动点。
5. `AgentController.submit()` 仍只执行一次；不要在外部事件到达时构造新的 Agent。用户命令进入 inbox/event bus，由等待中的 session 在安全点消费。

`after_tool_batch` 仅是 **router/coordinator** 路径的可选 hook：实施应在 `src/Agent/router_agent/hooks.py` 增加默认 `CONTINUE_LOCAL`，并由 Router Agent 使用；不得要求 `src/Agent/worker_agent/hooks.py` 的独立 protocol 改变 Worker loop 语义。

命名必须固定：`CONTINUE_LOCAL` / `WAIT_FOR_EXTERNAL` / `FINISH` / `FAIL_CLOSED` 是 `TurnDirective`；`WAITING` / `DECIDING` / `CANCELLING` 是持久 `SessionState`；伪代码中的 `WAIT_ARMED` 只是 `end_epoch_and_arm_wait()` 的内部返回值，绝不可写入第三套 session state。

这样对 Agent kernel 的改动小于“每 event 重建 session”，又保留完整 tool-call pairing 和当前 Context 压缩策略。

### 7.3.1 `WAIT_FOR_EXTERNAL` 的线性化 handoff（必须按此顺序实现）

`TurnPolicy=WAIT_FOR_EXTERNAL` 不能只在内存里留一个“下次 pre_llm 再等”的标志。它必须在完整 tool batch 结束的 safe point，调用 owner-loop 的唯一接口 `arbiter.end_epoch_and_arm_wait()`：

```text
after_tool_batch(outcomes):
  directive = turn_policy.classify(outcomes, canonical_frontier)
  if directive != WAIT_FOR_EXTERNAL:
      return directive

  # 同一 owner-loop 临界区：没有第二个 WAITING 写入点
  end_epoch_and_arm_wait():
      last_consumed = current_epoch.batch.high_sequence
      stage DECISION_ENDED(last_consumed)       # 此处不得 await 落盘
      if pending has eligible event with sequence > last_consumed:
          batch = claim_next_batch()            # 同一临界区直接 DECIDING
          session.install_preclaimed_permit(batch)
          return CONTINUE_LOCAL
      session.state = WAITING
      bus.arm_waiter(session_id, last_consumed)
      return WAIT_ARMED

pre_llm():
  batch = gate.take_preclaimed_permit() or await gate.await_armed_permit(cancel_event)
  assert session.state == DECIDING
  # 只有此处取得 permit 后才 prepare → refresh → prune → assemble
```

`pending` 检查 → pre-claim 或 `WAITING + arm_waiter` 必须是**无 yield**的 owner-loop 临界区；若 journal 需要异步 I/O，只能在该临界区外异步 flush 已 stage 的记录，不能在 cursor 检查与 arm 之间 `await`。事件若在 tool batch 与 `end_epoch_and_arm_wait()` 之间 ingress，会先进入 pending set，并被该临界区立即 claim；`pre_llm()` 不得 `clear()` 另一个 event，也不得维护第二套等待原语。由此把 §3.4 的 no-lost-wake 不变量映射到现有 `Agent.run()` 的“tool loop 结束 → 下一轮 pre_llm”间隙。

### 7.4 何时允许同一 epoch 的本地续航

“事件驱动”不等于任何 tool result 后都立即睡眠。一个 mission start 或高优先级事件可能需要有限的本地规划链，例如：`update_plan` → `activate_plan_node`。推荐的确定性 policy：

| 完整 tool batch 的结果 | 指令 | 原因 |
|---|---|---|
| `SARFinishTaskTool` 确认完成 | `FINISH` | 显式完成契约 |
| `UpdatePlanTool` 成功，存在尚未 activation 的 ready graph node | `CONTINUE_LOCAL`（有界） | 需要把已提交计划转化为真正的 physical dispatch |
| `activate_plan_node` 成功且当前 ready frontier 已完成 activation | `WAIT_FOR_EXTERNAL` | 该 API 已 fan-out physical dispatch；接下来需要 Worker/环境事实 |
| 直接 `assign_task` 成功 | 仅在明确 batch commit/frontier 未完成时 `CONTINUE_LOCAL`；否则 `WAIT_FOR_EXTERNAL` | 单条 assign 成功不是“全部派发完成” |
| `reply_to_help` / `cancel_task` 成功 | 通常 `WAIT_FOR_EXTERNAL` | 等待 Worker ack/state transition；不能把 tool success 当最终结果 |
| 只读 query 返回 actionable 新结果 | 最多一次 `CONTINUE_LOCAL` | 允许 LLM 消费刚读到的信息并采取动作 |
| 只读 query 无 actionable 结果 | `WAIT_FOR_EXTERNAL` | 避免 `query_task_events` 空轮询 |
| recoverable tool failure / dispatch rejection | 有界 `CONTINUE_LOCAL`，随后 `FAIL_CLOSED` 或等待真实状态 | 给一次纠错机会，不允许无限自我对话 |
| 无工具纯文本且 mission 未完成、没有 active work | 一次确定性 corrective nudge；仍无行动则 `FAIL_CLOSED` | 防止 initial planning 静默死锁 |

`TurnPolicy.FINISH` 不取代现有 `SARFinishTaskTool` / `ToolResult.task_complete` 契约：它只能在该显式 completion tool 已成功、`CoordinatorSARHooks.post_tool()` 已设置 `agent._task_complete` 后请求 loop 退出，随后仍由当前 `should_continue()` / executor 的完成语义处理 A2A terminal result。

### 7.5 “任务派发完成”的确定义

不得依据 LLM 文本“我已派发完毕”判断。应使用以下框架可验证条件：

1. 当前 MissionGraph 的 ready logical node 均已进入 activation / terminal / blocked 的 canonical 状态；
2. 一个 `activate_plan_node` 返回的全部 physical dispatch 都已被 runtime materialize；
3. 若仍使用 legacy direct `assign_task`，必须有一个**明确的 batch commit / frontier**，不能在单个 assignment 后猜测 LLM 是否还想派发其他 Worker；
4. 没有 `NO_LIVE_WORK` 不变量：mission 未完成但既没有 active/deferred dispatch，也没有可自动处理的 ready node。

当前最安全的迁移策略是：在 event-driven mode 优先走现有 `send_message(message_type="activate_plan_node")` 的 DAG fan-out。对 direct `assign_task`，先新增框架级 batch contract 或将其保留为兼容路径，不能仅凭 prompt 的“一次一个工具调用”规则自动休眠，否则会出现“只派了 Alice 就等待、Bob 永远没有任务”的回归。

### 7.6 防止内部空转与永久等待

- 每个 decision epoch 设置 `max_local_turns`、`max_no_progress_turns` 与 token budget；这些是系统配置，不由 LLM 设定。
- `WAIT_FOR_EXTERNAL` 只在存在 active/deferred physical work、或明确等待用户/Worker 回复时合法。
- mission 未完成且没有 live work 时，`ConditionScheduler` 产生一次 `NO_LIVE_WORK` finding；允许有界 corrective epoch。仍未建立工作则失败闭环/转人工，而不是无限 LLM 或无限等待。
- 长静默不直接触发 LLM。`RECONCILIATION_REQUIRED` 先执行状态 sync / deterministic checker；只在发现接受的状态转移、告警或不变量违反时才 wake。

### 7.6.1 LLM turn 预算与现有 `Agent.max_steps` 的绑定

当前 `Agent.run()` 的局部 `step` 在每个完整 tool batch 后递增，并在 `step >= self.max_steps` 时返回失败（`src/Agent/router_agent/agent.py:465`、`793-807`）；WAIT 本身不递增，但一个长 mission 的多个 wake epoch 仍会累计消耗该预算。Event-driven mode 必须显式配置并记录：

| 预算 | 语义 | 不允许的做法 |
|---|---|---|
| `max_total_llm_turns` | 整个 mission 的总 LLM/tool-batch 安全上限；明确映射到 Router Agent 的 `max_steps` | 每个 epoch 静默重置 `step`，从而取消全局安全阈值 |
| `max_local_turns_per_epoch` | `CONTINUE_LOCAL` 的上限，防止没有新事件的内部对话 | 用 prompt 约束代替计数器 |
| `max_decision_epochs` / token budget | 记录并限制大量小事件导致的总成本 | 让“等待不耗 step”被误解为“无限推理” |

若总预算耗尽，系统必须产生明确的 `COORDINATOR_DECISION_BUDGET_EXHAUSTED` terminal framework outcome，而不是模糊地以当前“任务在 N 步后未完成”掩盖原因。默认映射为 `RunResult(success=False, task_complete=False, need_input=False)`，使现有 executor 走 `TASK_STATE_FAILED`；只有命名明确的人工升级 policy 才能返回 `need_input=True` / `INPUT_REQUIRED`，不能把同一 `FAIL_CLOSED` 同时解释为两种 A2A 终态。E2 验收必须覆盖：多次 wake 会计入总 turn；纯 WAIT 不增加 turn；局部续航不能越过 epoch budget。

---

## 8. Context、显式反馈与地图历史问题

### 8.1 新的 Context 块

在已有 `CoordinatorPinnedState` 的 Mission DAG、Physical Dispatch、task status、supervision、map delta 前增加一个**短生命周期**块：

```text
### 本次唤醒原因（Decision #17）
- [HIGH] TASK_COMPLETED dispatch=dsp_... worker=Alice
- [NORMAL] MAP_MATERIAL_CHANGE revision=24 reasons=fire_change
- 同批合并：3 条 LOW telemetry（未逐条展开）

说明：以上是唤醒线索；以下“当前状态”快照才是权威事实。
```

建议字段：`decision_id`、`event sequence range`、事件 id/类型/优先级、关联 worker/dispatch、map revision、截断后的原因、coalesced count。每次 Context 注入后清空 ephemeral batch，但 journal 保留审计记录。

### 8.2 StateProvider 的变化

当前 `SARCoordinatorStateProvider.prepare_for_llm()` 将地图连续性、summary 和 Context preparation 绑定在每轮 LLM 前。提议拆分：

- `prepare_for_decision(event_batch, llm_client)`：只在 WakeArbiter 已允许的 epoch 中执行，验证 state fence、拿到当前 semantic snapshot、必要时产生 map summary；
- `snapshot(context_id)`：继续返回当前权威视图；增加 `decision_trigger_view` / source versions，但不自行决定唤醒；
- `ContextManager._render_current_state()` 的 digest 必须包含 decision batch sequence、map revision、task/supervision 版本，避免“事件变了但 state_digest 认为未变”。

这兼容当前 state injection 模型：刷新频率由“每内部 LLM round”收敛为“每个获准 decision epoch”，而非删除它。

### 8.3 与显式 TaskFeedback 方案的边界

两个方案应互补、不能互相替代：

| 层 | 回答的问题 |
|---|---|
| `TaskFeedback` / Physical Dispatch view | “任务现在是什么状态、结果/阻塞是什么？” |
| `DecisionEventBatch` | “为什么 Coordinator 现在需要看一次状态？” |
| Map delta / current snapshot | “环境具体发生了什么、现在真相是什么？” |

因此事件驱动的第一阶段可以先使用现有 `physical_dispatches_view` / `task_status_view`；TaskFeedback 的 richer projection 随后可作为 Context 质量增强，而不应被阻塞在“必须保存完整每 step 地图”上。

---

## 9. 固定检查器框架

### 9.1 Protocol（提议）

```text
CoordinatorConditionChecker
  checker_id: str
  cadence: ON_ENV_STEP | ON_RECONCILIATION | ON_STATE_CHANGE
  evaluate(snapshot: CoordinatorCheckSnapshot) -> list[ConditionFinding]

ConditionFinding
  key: str                    # e.g. battery_low:Alice
  severity: LOW|NORMAL|HIGH|CRITICAL
  active: bool
  state_revision: str/int
  evidence_preview: bounded dict
  wake: bool
```

要求：

- checker 是确定性、无 LLM、无网络 I/O、时间有界的纯规则；
- 只在 false→true、严重度升级或配置 reminder 时发布 `CONDITION_RAISED`；恢复可记录但默认不 wake；
- condition state 单独持久化/可审计，不能把每次 tick 都当作新 alert；
- checker 读取公开 snapshot，而不是直接窥探 barrier 私有字段。

### 9.2 可落地的初始检查

| 检查 | 当前数据可用性 | 触发条件 |
|---|---|---|
| `no_live_work` | MissionRuntime + TaskStore | mission 未完成且无 active/deferred dispatch、无可自动 activation frontier |
| `step_budget_pressure` | SemanticMapStore step budget / barrier metrics | remaining 跨过预设阈值 |
| `barrier_timeout_agents` | barrier last-step telemetry | 发现被系统注入 NoOp 的 timeout agents |
| `environment_error_burst` | barrier error types | 连续错误率/指定错误类型跨阈值 |
| `battery_low` | **当前 SAR snapshot 无 battery 字段** | 未来 telemetry adapter 提供 energy 后才启用；不得假装现已支持 |
| `workflow_invariant` | 场景规则 + mission graph | 固定资源/协作/依赖变量违反 |

这实现了“机器人电量不足、固定工作流变量”等需求，但不会捏造当前环境不存在的 telemetry。

---

## 10. 可靠性、恢复、取消与可观测性

### 10.1 交付语义

- EventBus 为 **at-least-once wake signal**，不是 exactly-once command bus。
- Source state transition 已由 `MissionRuntime`、TaskStore、Supervision store 的幂等 fence 保护；重复 wake 不得直接导致重复 dispatch。
- event delivery 到一个 epoch 后记录 `delivered_to_decision_id`；不等于操作已完成。
- 对未解决的 watchdog alert，SupervisionStateStore 继续保持 active/unack 状态；若 Coordinator 没有采取关联动作，受控 reminder/reconciliation 可再次发出事件，不可在首次展示时自动 ack。

### 10.1.1 Supervision ack 与 event delivery 的明确契约

`CoordinatorDecisionJournal` 的 `DELIVERED` 只表示“该 event 已被附入某个 decision epoch”；它绝不调用 `SupervisionStateStore.acknowledge_event()`。监督 ack 的 owner 和时机必须是确定性的：

| 情况 | Wake bus / journal | Supervision 语义 |
|---|---|---|
| alert 首次产生 | 生成一次可行动 wake；记录 delivery cursor | 保持 active + unack |
| Coordinator 看见 alert 但没有改变权威状态 | 不重复消费同一 `event_id`；按 reminder policy 才重发 | 仍 unack，不能因“看见”自动 ack |
| `TASK_RECOVERED` / 已有 watchdog 恢复路径 | 只按 policy 记录或低优先级展示 | 由 Watchdog 现有恢复逻辑 ack/清除 |
| canonical dispatch 进入 terminal | 可投递 terminal event；过滤该 dispatch 的后续 stale/unreachable reminder | **提议**由 `SupervisionResolutionPolicy` 以 `resolution=terminal` 做一次确定性 ack/归档；不能由 LLM tool success 提前完成 |
| cancel 已发出但还未得到 terminal transition | 可等待 callback/state-sync | 仍 active/unack；不允许把 cancel request 当作已解决 |
| 人工确认但允许风险继续存在 | 仅受控 operator command 可写审计 ack | 不由 LLM 自由调用 `acknowledge_event` |

当前代码未发现 Coordinator 推理后系统调用 `acknowledge_event` 的主路径；因此 E0 必须先实现 delivery/reminder 与 terminal filtering，E1/E3 才决定是否引入上述 `SupervisionResolutionPolicy`。在此之前，WakeArbiter 必须用 `event_id + active alert revision` 去重，避免同一 unack alert 每个 5 秒 tick 都唤醒 LLM。

### 10.2 丢事件与恢复

EventStore 虽可写 NDJSON，但内存记录有上限且 agentic run 会清空全局 singleton（`src/a2a/coordinator/event_store.py:43-115`）；它不应被当作 durable wake queue。

提议 `CoordinatorDecisionJournal` 记录：

```text
INGRESSED → SUPPRESSED/COALESCED → BATCHED → DELIVERED → DECISION_ENDED
```

journal 主要用于审计、调试和恢复诊断。真正的恢复保障是：启动时/安全 tick 用权威状态重建 `RECONCILIATION_REQUIRED`，例如 active supervision alert、未终态 dispatch、map revision 与 Context baseline 不一致、或 state-sync 发现 worker 真实状态不同。

### 10.2.1 失败路径（必须实现为确定性状态机）

```text
权威状态写入成功
  ├─ journal / bus enqueue 成功 → 按正常 policy 等待或 wake
  ├─ journal 写失败或 bus 不可用
  │    → 记录 source-side delivery failure / dirty watermark
  │    → 不回滚已经接受的物理/地图/监督状态
  │    → 下一次 reconciliation 依据 authority 生成 RECONCILIATION_REQUIRED
  └─ 事件在 queue 中被 coalesce / LOW overflow
       → journal 记录 suppression reason 与计数
       → terminal / input-required / critical finding 不可按 LOW 策略丢弃

DecisionSession 被唤醒
  ├─ state fence 满足、LLM/工具成功 → 记录 DECISION_ENDED，按 TurnPolicy 转移
  ├─ snapshot 尚未达到 fence      → 不调用 LLM；重新排队/短暂 owner-loop retry
  ├─ LLM 或 context preparation 失败
  │    → 事件标记 delivered-but-unresolved；保留 authority 中 active alert/state
  │    → 受控 retry / reconciliation，而不是无限同步重试
  └─ cancel / shutdown
       → abort runtime，wake blocked session，关闭该 mission_run_id admission
```

这里的关键是：event publication failure 绝不能反向撤销已被 MissionRuntime 接受的任务状态；恢复由权威状态对账，而非伪造一次“已成功处理”的 LLM 回合。

### 10.3 取消

- explicit cancel / server shutdown 使用当前 `MissionRuntime.abort()` 与 barrier.stop() 的确定性路径；同时关闭该 `mission_run_id` 的 event admission，并取消/唤醒等待中的 DecisionSession。
- 普通事件不得直接操作 `AgentController._cancel_events`；跨线程取消也必须投递到 Coordinator owner loop。
- hard deadline 的自动 remote cancel 与 wake 需有明确顺序和单元测试：Coordinator 后续看到的是“deadline exceeded + cancel state/结果”的当前真相，而不是两次相互冲突的独立 LLM 回合。

### 10.4 指标与日志

新增每 mission 的指标：

- `decision_epochs_total`、`llm_calls_total`、`llm_calls_per_env_step`；
- `events_ingressed_total{kind}`、`events_woke_total{kind}`、`events_suppressed_total{reason}`；
- `event_to_decision_latency_ms`、`batch_size`、`coalesced_count`；
- `idle_wait_seconds`、`reconciliation_runs_total`、`missed_state_detected_total`；
- `decision_no_progress_total`、`local_turn_budget_exhausted_total`；
- queue high-watermark / dropped-low-priority count；
- `dispatch_to_terminal_event_latency_ms`、`map_revision_to_decision_latency_ms`。

建议新增 `coordinator_decision_events.ndjson`，每条记录不含 secret、token 或未截断 worker payload，并在 router interaction log 记录 `decision_id` / cause event ids 以方便对齐 LLM 调用与状态变化。

---

## 11. 分阶段实施与 Feature Flag

### Phase E0 — 最小强事件契约和 shadow 观察（不改变 LLM 时机）

目标：先验证事件来源、去重和接线完整性。

- 新增 DTO、journal、EventBus、fence / sequence 单元测试；
- 在 `MissionRuntime` canonical transition 内新增持久 `dispatch_revision` / immutable `PhysicalTransition`，observer 只接收 `accepted=True` 的转移；`INPUT_REQUIRED` 也必须可稳定去重；
- **只接入已有强事件：** MissionRuntime accepted terminal/failed/`INPUT_REQUIRED` transition、periodic state-sync 的真实转移、TaskWatchdog 新 active alert；
- 不在 E0 接 map mutation、barrier telemetry、operator command、battery/workflow checker，避免一次性把三种线程/语义模型混入第一条 vertical slice；
- 固化 hard-deadline 策略：保留 deterministic remote cancel，但以持久 cancel-attempt fence 保证每个 deadline event / dispatch revision 至多一次有效请求；
- `event_driven_mode=shadow`：照旧运行现有 Agent loop，只记录 `would_wake` / `would_suppress`；
- 不修改 prompt 行为，不将任何 event 用于等待。

通过条件：callback/sync/watchdog 相同物理转移不会产生重复 logical wake；重复 `INPUT_REQUIRED` 只产生一个 immutable transition；跨 context/旧 run event 全被拒绝；无跨线程 asyncio 错误；并在该 Phase 关闭前把根 `AGENTS.md:149`、`src/a2a/coordinator/task_watchdog.py` 的“只告警”文案与当前 hard-deadline cancel 策略通过 ADR/测试统一。

### Phase E1 — 确定性 Map / Checker / Context 反馈面

目标：使“为什么醒来”在不 gate LLM 的情况下可见。

- 提取 `MapImpactClassifier`，将 map delta 计算从 `prepare_for_llm()` 的唯一职责中拆出；
- 增加 `DecisionTriggerView` 投影与 state digest；
- 添加 `ConditionScheduler` 与 `no_live_work`、step budget、barrier timeout 的首批 checker；
- 接入 map mutation 与 barrier telemetry；TaskWatchdog 在 E0 已接入，只扩展 terminal-resolution/reminder projection。

通过条件：地图 agent position spam 不会产生 wake candidate；fire/person/conflict 等材料变化具备可审计 delta；电量 checker 仍为未启用 future adapter。

### Phase E2 — DecisionSession safe-point gate

目标：真正停止“派发后无事件的连续 Coordinator LLM 调用”。

- 为 Agent hook / agent loop 增加完整 tool-batch 后的 `TurnDirective`；
- 为 context-specific DecisionSession 种入 initial permit、等待、cancel wake、event batch injection；
- `CoordinatorAgentExecutor._execute_agentic()` 使用同一持久 Agent session，而不是每 event 新 submit；
- 采用 `EventDrivenTimeoutPolicy`：不再用 legacy 600 秒 outer `orchestration_timeout` 包住 WAIT；明确 mission deadline、active epoch、idle reconciliation 与 cancel/shutdown 的同一 `CANCELLING` owner；
- 只允许 `arbiter.end_epoch_and_arm_wait()` 在 owner-loop 临界区写入 `WAITING` 或直接 pre-claim 下一批；`pre_llm()` 只消费已 arm 的 permit；
- 明确 `max_total_llm_turns → Agent.max_steps`、每 epoch 本地续航预算和耗尽结果；仅 Router hooks 扩展，Worker hooks/loop 默认不变；
- 先只支持 DAG `activate_plan_node` 的可靠 dispatch-batch sleep；direct `assign_task` 仍走兼容策略。

通过条件：成功 activation 后，在无事件窗口中 `llm_calls_total` 不再增加；tool-batch 与 event ingress 交错时无 lost wake；合法 WAIT 不会被 legacy 600 秒 timeout 误杀；cancel/shutdown/deadline 只走一个 exit owner；多 wake 会计入总 turn、纯 WAIT 不会消耗 turn；task terminal/input_required/user command 唤醒且同 context 无并发 Agent run。

### Phase E3 — 直接分配 batch、operator command 与恢复策略

目标：消除 direct `assign_task` 的派发完成歧义，并完善控制面。

- 定义 batch commit/frontier contract 或把 direct assignment 收敛至 DAG activation；
- 实现 operator command ingress、授权和 idempotency；
- 接通 `RECONCILIATION_REQUIRED`、startup recovery、alert reminder；
- 将显式 TaskFeedback 的关联字段接入 Context。

### Phase E4 — 真实 SAR 验收与默认开启

目标：证明不是只减少 token，而是不降低协同正确性。

- shadow 与 enabled A/B 对比 token、延迟、coverage、transport rate、任务错误；
- feature flag：`off → shadow → enabled`，故障时可退回 legacy continuous loop；
- 仅在 deterministic/unit/e2e 与真实 SAR 验收均满足后设为默认。

---

## 12. 测试矩阵与验收标准

### 12.1 单元 / 合同测试

| 类别 | 必测契约 |
|---|---|
| EventBus | sequence 无丢失、dedupe、优先级、batch、防抖、queue 背压、stale run fence |
| 跨线程 bridge | 从 barrier worker thread 发布只经 `owner_loop.call_soon_threadsafe`；不跨 loop 直接触摸 asyncio primitive |
| Physical transition adapter | `dispatch_revision` 对每次 accepted transition（含 `INPUT_REQUIRED`）单调持久化；ignored/stale/duplicate callback 不 wake；sync 与 push 同一状态不重复 |
| Watchdog adapter | 同一 active alert 一次 wake；升级可 wake；recovery 不错误唤醒；hard deadline 对同一 event/revision 至多一次有效 remote cancel，顺序固定 |
| Map classifier | agent/inventory 观测不 wake；fire/person/status/conflict/stale 的 material delta 正确；revision burst 合并不丢原因 |
| Turn policy | update-plan → activation 可本地续航；`end_epoch_and_arm_wait()` 与 ingress 交错无 lost wake；activation 成功后等待；terminal/input/user event 唤醒；无 live work 不静默等待 |
| Timeout / budget | WAIT 不被 legacy outer timeout 杀死；idle 只 reconciliation；cancel/deadline 单一 exit owner；多 epoch 总 turn、每 epoch local turn、`Agent.max_steps` 映射均可验证 |
| Context | trigger block 有界、state fence 满足、state digest 囊括 trigger revision、event payload 不覆盖 canonical snapshot |
| Recovery/cancel | 旧 context/run event 被拒绝；等待 session 可被 cancel/shutdown 唤醒退出 |

### 12.2 集成 / E2E 测试

1. 真实 FastAPI `/a2a/push-callback`：worker terminal callback → event batch → 一次 coordinator wake，验证 Context 看到 canonical physical state。
2. `INPUT_REQUIRED`：Worker 求助时 Coordinator 恰好等待，事件唤醒后可执行 `send_message(reply_to_help)`；不会创建第二个 Agent。
3. worker callback 丢失：periodic state sync 发现 terminal，走同一 transition adapter 并 wake。
4. multiple workers 同时 terminal + map delta：只产生一个 coalesced decision epoch，保留全部 cause ids。
5. barrier timeout：timeout agent 的 telemetry 进入 checker；正常 step 递增本身不触发 LLM。
6. user cancel：等待中的 session 立即退出，`runtime.abort()` / `barrier.stop()` 的已有行为不回归。

### 12.3 真实 SAR 验收（框架层改动的必做项）

按当前项目约定运行：5 个 scene × 2 个 agent counts × seed=42，10 组，`max_steps=20`。除常规 smoke 外收集：

- coverage / transport rate；
- `worker_busy`、`task_not_routable_yet`、`unknown_task_id` 错误数必须均为 0；
- decision epoch / LLM 调用数，验证无新事件时 Coordinator 不空转；
- callback→wake、map→wake 延迟与 queue health；
- barrier timeout agents 与系统注入 NoOp，避免将它们误判为 LLM 自主决策。

“测试绿”必须区分：单元合同通过、A2A 集成通过、真实 SAR 10 组交叉验证通过。任一缺失只能称为实施中/验证中，不能称完成。

---

## 13. 风险、迁移约束与回退

| 风险 | 后果 | 缓解 |
|---|---|---|
| 把 raw callback 当 wake 真相 | stale/duplicate callback 造成错误重派 | 只在 MissionRuntime accepted transition 后 publish；先 refresh canonical snapshot |
| 每 event 重建 Agent | 丢 tool history，session lock 冲突，多 Agent 并发 | 单一持久 DecisionSession / safe-point gate |
| 单个 direct assign 后睡眠 | 只派发一个 Worker，其他空闲 | 以 DAG activation / 明确 batch commit 作为 quiescence 边界 |
| 每 map revision 唤醒 | agent observation spam 导致 LLM 风暴 | deterministic MapImpactClassifier + revision debounce |
| 直接跨线程操作 asyncio queue | waiter 不被唤醒、随机死锁 | owner-loop bus + `call_soon_threadsafe`，保持 barrier threading |
| event 发布失败后永久等待 | Worker 已完成但 Coordinator 永不再想 | periodic state sync + deterministic reconciliation + journal/health metrics |
| 无界 prompt 注入 | token 增长、worker 文本 prompt injection | event preview 白名单、截断、batch cap、current state 为权威 |
| hard deadline 自动 cancel 与 alert 双重触发 | 两次重复重规划 | coalesce 关联 dispatch/revision，规定 source 顺序并测试 |
| Feature flag 一次性全开 | 真实实验回归难定位 | `off/shadow/enabled` 渐进 rollout，legacy 连续 loop 可回退 |

---

## 14. 分阶段准入决策与剩余选型

以下不是可无限延期的“开放问题”；每项都归属到具体 Phase 准入门槛。

### 14.1 E0 前必须关闭（物理事件 spine）

1. **transition observer 接线与 revision：** 选择在 `MissionRuntime` 内注入 observer，或由唯一 facade 包装；无论位置，必须从 accepted canonical transition 产出持久 `dispatch_revision` / `PhysicalTransition`，覆盖 callback、sync、cancel、watchdog，且 duplicate 不 publish。
2. **mission fence：** `mission_run_id` 的分配与 `MissionRuntimeManager.admit()`、EventBus `register_mission()` 在同一临界区完成；普通 admit 不能依赖仅 recovery 递增的 `_epoch`。
3. **hard deadline：** 目标语义已定为“一次有 fence 的 deterministic remote cancel + 事件/审计”，实现前通过 ADR/测试统一根 `AGENTS.md:149` 与 `TaskWatchdog` docstring 的旧表述。

### 14.2 E2 前必须关闭（真正 gate LLM）

1. **timeout/cancel supervisor：** `EventDrivenTimeoutPolicy` 替换 legacy outer 600 秒 WAIT 语义；A2A 长 `WORKING`、mission deadline、idle reconciliation、cancel/shutdown 需有真实集成测试。
2. **WAITING 线性化：** 只能使用 `end_epoch_and_arm_wait()` + preclaimed permit 的单一原语；禁止 `after_tool_batch` 与 `pre_llm` 各自维护等待状态。
3. **turn budget：** 明确 `max_total_llm_turns` 与实际 `Agent.max_steps` 映射；不允许 per-epoch 重置全局安全上限。
4. **router-only hook：** `after_tool_batch` 的默认/接口不破坏 `worker_agent` 独立 hooks 和 Worker loop；`FINISH` 仍依赖 `SARFinishTaskTool` 显式完成契约。

### 14.3 E3 可选实现选型（不阻塞 E0/E2）

1. direct `assign_task` 的 batch contract 是否收敛为 DAG activation，或新增显式 batch facade；E2 先只保证 `activate_plan_node` 的可靠 sleep。
2. operator command ingress 的 API、认证与 UI 呈现；纯查询应优先走只读路径。
3. richer TaskFeedback / 任务—地图影响关联的投影深度；不影响 Wake bus 的权威边界。

---

## 15. 实施前的审核门槛

本文件是架构设计，不授权直接实施。进入 E0 前必须满足：

1. 独立只读 reviewer 对当前 API/线程模型/生命周期路径完成核验；
2. 任何 blocker 被修订后需 fresh review，并获得明确 `APPROVE`；
3. 将本文件中“提议”接口转化为小范围 source-contract 测试，再开始生产改动；
4. 不将 prompt 文案当作事件驱动正确性依据；
5. 不在本设计实施过程中顺带重构无关的 Worker、TeamPartition、Map Agent 或实验 UI。

---

## 16. 审核记录

| 日期 | 审核类型 / reviewer | 实际核验范围 | 结论 | 对文档的处理 |
|---|---|---|---|---|
| 2026-07-21 | 独立只读 source audit（leaf, `grok-4.5`） | executor/controller/agent loop、barrier/experiment、push callback/MissionRuntime/EventStore、Watchdog/Supervision、SemanticMap/StateProvider/MapSummarizer、既有 WakeQueue 文档；未改代码、未跑测试 | **可行，但有硬前提**：Wake bus 只能做触发层，不能替代 MissionRuntime，也不能新建第二个同 context Agent run | 已补充三通道边界、最小 E0 强事件切片、supervision delivery/ack 契约与 terminal filtering |
| 2026-07-21 | fresh design review（leaf, `grok-4.5`） | 通读 REVISED 全文并对照 Agent/Controller、Runtime、callback/sync、Watchdog、EventStore、Map、Barrier、send_message、server lifecycle 源码；未改代码、未跑测试 | **APPROVE_WITH_CHANGES**：0 Blocker、5 Major、6 Minor | 已将 M1–M5 写入 timeout/cancel 状态机、accepted transition revision、WAIT handoff、turn budget 与 Phase 准入；将请求最终短审 |
| 2026-07-21 | 最终独立只读短审（leaf, `grok-4.5`） | 通读 1000 行全文并复核 M1–M5、barrier lock、deadline fence、router-only hook、finish contract、AGENTS 引用、Phase/test gates；未改代码、未跑测试 | **APPROVE**：0 Blocker、0 Major、7 non-blocking Minor | 已吸收 7 个澄清项（单一 `INPUT_REQUIRED` event kind、无-yield arm、准确行号、A2A terminal mapping、tool-pair timeout、wall-clock 配置、枚举命名）；获准进入 E0 实施准备，不代表 E2/default 开启 |

本表记录 reviewer 的实际范围和结论，不把“架构可行”误写成“实现已批准”。

---

*本设计的核心不是“多一个 queue”，而是将 Coordinator 的推理权从每次 tool return 的隐式自循环，迁移为由权威事实变化驱动、可审计、可批处理、可安全等待的决策 epoch。*
