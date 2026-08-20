# Memory System Redesign 设计方案（Revision 1）

> **状态：REVISED — fresh independent review pending；尚未获实施 `APPROVE`。**
>
> **实施约束：**后续必须按 phase 串行实施；每个 phase 完成后由父侧独立验收。出现 Major/Blocker 时，必须修订设计并进行 fresh review，得到明确 `APPROVE` 后才能进入下一 phase。

## 冻结输入与范围

- **唯一实施目标：**`/home/wyh/daily_work/LLaMAR-memory-redesign`
- **目标分支 / HEAD：**`feat/memory-redesign` @ `e1a5a01392c814ddcbc33f5e17aa5cad5d9e8fe6`
- **源码证据边界：**`src/`、`sar_orch/`、`pyproject.toml` 在此 HEAD 相对工作树为 clean；`AGENTS.md` 的预存修改不属于本设计改动，实施者不得覆盖。后续 Phase 3 前必须再次执行 `git diff --quiet -- src sar_orch pyproject.toml`。
- `LLaMAR/main@d1dada92…` 与原 `LLaMAR_evel@339fc3b` 仅保留为历史设计/审查取证基线，**不再是本次 H1 或后续实施的事实目标**。
- H1 的规范性人工审核交付物固定为：`.hermes/plans/memory-system-redesign-h1-projection-contract-card.md`。它和本文任一字节变化均使旧审批失效，须 fresh review。
- 本文继承的旧 source citation 必须在当前 `feat/memory-redesign` 目标树上由 fresh reviewer 重新核验；不得把旧 `d1dada92` 审批或行号当作当前证据。
- 本文只定义架构、数据契约、迁移与验收；本文修订本身**不实施代码、不运行实验、不修改现有运行行为**。
- 本文覆盖：SAR/A2A 活跃路径中的 Context、语义地图、事件、任务生命周期、Worker/节点状态。
- 本文不覆盖：重写 SARBarrier、重写 A2A SDK、改变 `MissionRuntime` 的任务控制权、引入新的 LLM prompt 策略或跨 episode 默认长期记忆。
- **重新冻结规则：**目标 HEAD、源码边界或本文 bytes 任一变化，均须重新记录 SHA-256、重跑 source citation checker，并执行 fresh independent review；不得复用本 revision 的审批结果。

## 30 秒结论

当前系统不是一个统一的 Memory 子系统，而是多组运行态 Store 经 `StateProvider → ContextManager` 自动投影为末尾的 legacy `## Context Memory` user message：

```text
SemanticMapStore / EventStore / MissionRuntime / Registry / Worker evidence
        ↓
StateProvider RuntimeState
        ↓
ContextManager pinned projection + assemble()
        ↓
末尾 role=user 的 legacy “## Context Memory”
        ↓
下一轮 LLM
```

目标不是把这个块机械改名，而是建立清晰边界：

```text
Memory = 可寻址、可版本化、可查询的领域事实与事件
Context = 一次 LLM 调用的消息历史、预算、选择、渲染产物
Environment State = Context 从 Memory/运行时只读视图组装出的当前环境投影
```

推荐采用 **“Memory Kernel + 三类领域 Memory + Context Read Port + 渐进 adapter”**：

```text
Worker 工具 / A2A callback / MissionRuntime
        ↓
Memory Ingestor（确定性的框架层）
        ↓
Memory Kernel
├── Spatial Memory
├── Temporal Memory
└── Embodied Memory
        ↓ 只读查询
EnvironmentStateProvider
        ↓
ContextAssembler
        ↓
## Environment State
```

不推荐直接把 MissionRuntime/TaskStore 改造成 generic CRUD 数据；物理 dispatch 状态必须继续由 `MissionRuntime.PhysicalDispatch.state` 作为唯一任务控制真相源。

`SARBarrier`/simulator truth 不属于在线 Memory 的事实来源：它只能模拟 Worker 局部传感器、服务 explicit oracle/debug mode，或在 run terminal 后被 evaluator-only 读取；不得初始化、修正或回填 semantic/shadow/read_port Memory。

---

# 1. 当前事实与问题定义

## 1.1 Context 当前实际行为

### 已证实的源码事实

- Coordinator `ContextManager.assemble()` 将 memory block 作为末尾 `role=user` 消息追加，不是写入 system prompt：
  `src/Agent/router_agent/context.py:629-649`。
- Worker 侧同构仅限 assemble 的尾部注入：`src/Agent/worker_agent/context.py:687-707`。
- LLM 可见标题为 `## Context Memory`：
  - `src/Agent/router_agent/context.py:692`
  - `src/Agent/worker_agent/context.py:769`
- pre-LLM 顺序为：`prepare_runtime_state → refresh_runtime_state → prune_history → assemble`：
  `src/Agent/router_agent/hooks.py:70-84`。
- Context 已通过 `StateProvider` 协议避免直接 import SAR backend：
  `src/Agent/router_agent/state_provider.py:42-75`。
- 但 Context 仍持有和提取领域状态：`pinned`、snapshot、`_loaded_skills`、`observe()`、`_project_runtime_state_to_pinned()`。
  关键位置：
  - `src/Agent/router_agent/context.py:65-83, 118-160, 187-225, 591-605`
  - `src/Agent/worker_agent/context.py:593-683, 687-797`
- Router/Worker 的 pruning 并不等价：router 只做压缩；worker 还会做 count-based 删除、orphan tool 修复和 episodic 累积。迁移不能把二者误当作同一语义。
- `Context Memory` 被 Coordinator/Worker prompt、semantic/oracle prompt 和可注入 skill 深度引用；改渲染标题而不改这些行为指令会改变模型行为。
  - `sar_orch/prompts/coordinator/{system.md,system.semantic.md,system.oracle.md}`
  - `sar_orch/prompts/worker/system.md`
  - `sar_orch/skills/{coordinator,worker}/**/SKILL.md`

### 问题

当前 `ContextManager` 同时承担：

```text
conversation / token 管理
+ RuntimeState 投影
+ tool result 解析
+ pinning
+ 类 Memory 生命周期
+ prompt 渲染
```

因此“Context Memory”容易被误解成一个长期、统一、可检索的 memory store；实际上它是临时 prompt 视图，生命周期与领域事实不一致。

## 1.2 当前领域事实分散

| 目标领域 | 当前素材 | 当前问题 |
|---|---|---|
| Spatial | `sar_orch/map/store.py:126` `SemanticMapStore` | name/位置混合 key、无空间索引、agent 的库存/任务混入地图 |
| Temporal | `src/a2a/coordinator/event_store.py:43` `EventStore`、TaskLogger、SupervisionStateStore、mission_graph JSONL | 多写者、模块级单例但语义上 mission 级、缺少统一因果/任务索引 |
| Embodied | `AgentSemanticState`、AgentRegistry、WorkerRegistry、Team/Heartbeat/Supervision、Worker pinned state | 无统一 node identity、能力/资源/可用性索引和事实优先级 |

当前 active `SemanticMapStore` 已拥有有价值的基础行为：observation 合并、乱序保护、冲突标记、revision 和 JSONL append。详见 `sar_orch/map/store.py:244-417, 498-510`。

当前 `MissionRuntime` 已拥有必须保留的控制语义：admission、physical state 单调变迁、context/worker-task callback 路由、abort 和 recovery。关键边界：

- `src/a2a/coordinator/mission_runtime.py:1400-1437`
- `sar_orch/coordinator_state_provider.py:443-449`

后者明确：physical runtime state 为 canonical；事件只是诊断/证据来源，不能反向覆盖任务状态。当前 `handle_callback()` 对 `stale_transition` 仍返回 `dispatch_id` 供 observation 路由；它不是认证、持久 scope-close 或 Memory 幂等机制，不能被新设计“继承后默认安全”。

---

# 2. 目标架构与职责边界

## 2.1 三个平面

```text
┌──────────────────── Control Plane ────────────────────┐
│ MissionRuntime / TaskStore / A2A transport / Worker tools │
│ - 控制任务合法状态、消息传输和 Worker 可上报 evidence      │
└────────────────────────────────────────────────────────┘
                         │ emits authenticated evidence / control receipts
                         ▼
┌──────────────────── Memory Plane ─────────────────────┐
│ Memory Kernel                                            │
│ - Spatial / Temporal / Embodied 的版本化事实与审计证据  │
│ - create/read/update/delete + relation + indexes         │
└────────────────────────────────────────────────────────┘
                         │ read-only views
                         ▼
┌──────────────────── Context Plane ────────────────────┐
│ ContextSession / ContextAssembler / Budget Policy        │
│ - history、snapshot、token 预算、选择和渲染              │
│ - 输出 Environment State，不拥有领域事实                 │
└────────────────────────────────────────────────────────┘
```

`SARBarrier` 在该图之外：它可作为 Worker-local sensor adapter 的仿真后端，或作为 terminal 后 evaluator 的私有 truth source；Memory Plane 与 Context Plane 对它没有在线依赖。

## 2.2 Context Management 的唯一职责

Context 只负责：

```text
- 原始 message 与 tool-call 协议历史
- token budget、裁剪与会话压缩
- NeedInput snapshot / resume 与 tool-call 协议闭合检查
- 维护 `(scope_id, viewer_id)` 命名空间内单调的 temporal cursor
- 计算 memory token budget，发出 EnvironmentStateQuery
- 将查询结果按 role / task / budget 渲染为 prompt
```

Context 不负责：

```text
- 保存世界/节点/任务领域事实或 RuntimeState payload
- 从 ToolResult 中合并 observation
- 存储跨任务事件
- 执行 Memory CRUD
- 决定 physical task state
```

设计上的目标命名：

```text
ContextSession          = message/snapshot 生命周期
ContextAssembler        = prompt 组装
EnvironmentStateProvider= 组合读端口构造投影
EnvironmentStateRenderer= 纯渲染
MemoryReadPort          = Spatial / Temporal / Embodied 的只读接口
ControlPlaneReadPort    = MissionRuntime/TaskStore 的只读 task view 接口
```

迁移期可保留 `ContextManager` 作为兼容 facade，但不应继续把 `pinned` 当作领域 Memory 的真相源。`ContextSnapshotV2` 仅保存 messages、cursor 和 `LoadedSkillRef{name, canonical_source_relative_path, content_sha256}`；恢复时只允许从配置的 skill root 重读同路径、同 hash 的内容，hash 不匹配则跳过并渲染 `SKILL_RELOAD_REQUIRED`，绝不把旧 skill content 当作领域数据复制。snapshot 不得保存 `pinned`、RuntimeState payload 或任一领域 projection。旧 snapshot 读取后只用于一次性恢复消息，再删除；缺失 cursor 时以当前 scope 的 sequence=0 开始，绝不跨 scope 推断。

## 2.3 LLM 可见的 `Environment State`

目标 prompt 结构：

```text
system prompt
  ├─ 稳定规则
  ├─ 工具契约
  └─ Output / Response Contract

conversation history
  └─ 原始 user / assistant / tool messages

role=user:
  ## Environment State
  ├─ Spatial State
  ├─ Embodied State
  ├─ Relevant Recent Events
  ├─ Task Execution State
  └─ Freshness / Conflicts / Evidence
```

规则：

1. legacy phrase `Context Memory`（包括 `## Context Memory` heading）只在下列 **active migration manifest** 中必须归零：
   - `src/Agent/{router_agent,worker_agent}/{context.py,hooks.py}`；
   - `src/a2a/builtin_tools/query_task_events.py`、`src/a2a/coordinator/{team_status_auth.py,team_partition_service.py}`、`sar_orch/coordinator_state_provider.py`；
   - `sar_orch/prompts/coordinator/{system.md,system.semantic.md,system.oracle.md}`、`sar_orch/prompts/worker/system.md`；
   - `sar_orch/skills/coordinator/{fire-suppression,person-rescue}/SKILL.md` 与 `sar_orch/skills/worker/{firefighting,inventory-management,navigation,person-rescue}/SKILL.md`；
   - `tests/{test_coordinator_semantic_mode.py,test_phase3_read_mailbox.py,test_phase5_team_status_auth.py}`；
   - `AGENTS.md`、`docs/system_docs/{contextmanager.md,route_strategy.md,semantic_map.md}`、`tool_gap_analysis.md`。
   scanner 的输入**只**是上述 manifest，不以全仓 `grep` 作为 gate。历史计划、`docs/plans/`、`docs/paper/`、`sar_orch/results/`、`logs/`、审查报告和 `.hermes/` 均为历史证据，明确豁免且不得批量改写；未加载的 `sar_orch/prompts/**/*.bak`、`*.svg`、`docs/system_docs_html/`、`docs/superpowers/`、`.agents/handovers/` 也明确属于 inert/historical 表面，禁止批量改写。`sar_orch/coordinator_state_provider.py` 在本 target 当前没有该 legacy term，但仍在 Phase 1 Modify ledger 中作为 Environment State adapter transition owner，不应被误判为 no-op string edit。
2. `Output Format` 不属于 Environment State；`ContextConfig.output_schema` 的动态渲染必须迁为 router/worker 的稳定 system prompt 构造，不得以末尾 role=user state block 注入。
3. `Environment State` 是可重建 view，不是持久化 truth source。
4. 每次投影必须带上：`scope_id`、`as_of_sequence`、`memory_revision`、`freshness`、`conflicts`、`evidence_refs`。
5. `Freshness` 只有 `FRESH | STALE | UNAVAILABLE`：旧成功投影可作为 `STALE` 显示但必须带原因和来源 revision；没有安全投影或 read error 时显示 `UNAVAILABLE`；禁止静默沿用旧 state。
6. `assemble()` 在追加 Environment State 前必须验证 history 中不存在未闭合的 assistant tool call；不满足时拒绝本轮组装并由现有 controller/NeedInput 回填路径闭合协议。

---

# 3. 统一数据身份、作用域与关系

## 3.1 统一 Envelope

所有三类 Memory record 使用共同外壳：

```text
MemoryRecordEnvelope
- record_id                 全局唯一（推荐 ULID）
- domain                    spatial | temporal | embodied
- scope_id                  project / experiment / context / runtime_epoch
- schema_version
- revision
- created_at
- observed_at               源事件发生时刻
- ingested_at               Coordinator 接收时刻
- actor_id                  worker / coordinator / system
- provenance                worker_sensor_tool / worker_telemetry / worker_observation / peer_report / registry / control / supervision
- confidence
- source_priority
- causation_id              例如 tool_call_id
- correlation_id            一整条任务链的关联 ID
- idempotency_key
- tombstone / deleted_at
```

推荐 scope：

```text
MemoryScopeV1 =
(project_id, experiment_id, context_id, runtime_epoch)

scope_id = sha256(canonical UTF-8 JSON of the four named fields)
UNIQUE(project_id, experiment_id, context_id, runtime_epoch)
```

字段来源固定如下，缺任一字段则拒绝领域写入并记录脱敏 security audit：

```text
project_id       = MemoryConfig.project_id，MVP 固定为 "llamar"
experiment_id    = SAR run metadata 的 run_id；standalone A2A 必须显式配置，不得从 callback 猜测
context_id       = MissionRuntimeManager.admit() 的 admitted context_id
runtime_epoch    = admission 时读取的 MissionRuntimeManager epoch；重启 recovery 后的新 epoch 创建新 scope
```

不能只用 `context_id`。当前进程内旧 callback 隔离主要依赖 context_id + worker-task 映射，epoch 只在 recovery 时推进；Memory 必须增加持久化的 scope-close fence，而不是复用当前有界/瞬态路由语义。`MemoryScopeFactory.activate()` 对相同四元组的规则固定为：只有同一仍 active 的 runtime 可取得既有 scope handle；任何已关闭 scope 或不同 runtime 的同 tuple admission 返回 `scope_tuple_reuse`，调用方必须生成新的 `context_id`，绝不 reopen/reuse 已关闭 scope。

默认策略：每个评测 episode / experiment scope 相互隔离；跨 episode 长期记忆必须是显式 opt-in namespace，不能默认为共享。

### 3.1.1 在线 truth isolation（H1-INV-1）

`barrier`、`oracle`、`ground_truth`、checker coverage 或 direct world snapshot 不是合法的在线 provenance。`MemoryIngestor` 在 reducer 前必须只接受 H1 card 定义的 Worker、静态 registry、control、supervision provenance allowlist；registry 仅允许 capability/identity/sensor metadata，不得提供动态场景字段。任何 oracle/direct-world candidate 返回 typed `online_truth_forbidden`，仅留下脱敏 diagnostic，零 Temporal/Spatial/Embodied/revision/outbox 写入。

Worker-local tool 可以在仿真中由 Barrier 驱动，但 Coordinator/Memory 只接收其受认证、已脱敏、规范化的 ToolResult/observation；不得持有或调用全局 `SARBarrier.get_env_snapshot()`。终止后的 evaluator truth trace 也不得作为 candidate event、correction、backfill 或 Context 数据。

## 3.2 关系图

```text
TemporalEvent --observed_by--> EmbodiedNode
TemporalEvent --about--> SpatialEntity
TemporalEvent --caused_by--> tool_call / callback
TemporalEvent --occurs_in--> PhysicalDispatch
TemporalEvent --supersedes--> TemporalEvent

EmbodiedNode --located_at--> SpatialEntity / Geometry
EmbodiedNode --has_capability--> Capability
EmbodiedNode --holds_resource--> Resource

PhysicalDispatch --assigned_to--> EmbodiedNode
PhysicalDispatch --concerns--> SpatialEntity
```

使用显式 `MemoryRelation`，而不是靠 `name`、`task_id`、`worker_task_id` 等字符串猜测关联：

```text
MemoryRelation
|- relation_id
|- scope_id
|- from_ref                 {namespace: memory|control, id}
|- relation_type
|- to_ref                   {namespace: memory|control, id}
|- valid_from / valid_to
|- source_event_id
|- confidence
```

`PhysicalDispatch` 使用 `namespace=control`，不得伪装成 Memory record；跨 namespace relation 只读引用控制面 ID，绝不赋予 Memory 修改 dispatch 的能力。

---

# 4. 三类 Memory 设计

## 4.1 Spatial Memory：空间几何与语义地图

### 职责

```text
- 空间实体：fire / person / reservoir / deposit / obstacle / region
- 几何：point / grid cell / region / bounding box
- 拓扑：inside / adjacent_to / reachable_from / blocked_by
- 语义：status / intensity / resource_type / danger_level
- observation 证据、置信度、冲突、freshness
```

### 目标实体

```text
SpatialEntity
- entity_id                 稳定 ID；name 仅是属性
- entity_type
- geometry
- attributes
- state
- confidence
- last_seen_step
- source_event_ids[]
- conflict_set[]
- revision
```

### 所有权划分

```text
Spatial Memory:
- 环境对象及其空间/语义事实
- agent 与 location 的空间关系

Embodied Memory:
- agent inventory、capability、availability、current dispatch

Temporal Memory:
- position/inventory/对象状态改变的不可变证据
```

现有 `SemanticMapStore.agents` 中的 position / inventory / current task 需要逐步拆分；Spatial 不再拥有 agent 的资源和任务控制状态。

### 合并与冲突

1. 每个 observation 先 create 为 TemporalEvent。
2. Spatial reducer 依据 `observed_at / env_step / source_priority / confidence` 更新当前投影。
3. 较旧 observation 不得覆盖较新 observation。
4. 同步、同优先级的矛盾声明进入 `conflict_set`，不可静默抹掉。
5. `rescued`、`extinguished` 是状态更新，不等于删除历史实体。
6. `delete` 只用于错误、撤销、失效数据，采用 tombstone。

上述规则的候选只来自 H1-INV-1 allowlist。`sequence/event_id` 只稳定排序，不能在同 step、同 priority、同 confidence 的不同值之间制造“胜出真值”；该字段必须进入 `CONFLICTED`。旧 step evidence 仍可保留 Temporal relation，但若不改变默认可见投影则标记 `ignored_out_of_order`，不推进实体/view revision。

### 索引

```text
(scope_id, entity_id)                       主键
(scope_id, entity_type)                     类型检索
(scope_id, cell_x, cell_y, cell_z)          初期网格检索
(scope_id, entity_type, status)             状态筛选
(scope_id, last_seen_step)                  新鲜度筛选
bbox / RTree                                后续非网格场景扩展
```

SAR 第一阶段使用 discrete grid / bbox 即可；不应在第一版为了抽象 GIS 过度设计。

## 4.2 Temporal Memory：工具反馈、汇报与任务事件

### 职责

Temporal Memory 是 append-only 的证据日志，存储：

```text
- tool.started / tool.succeeded / tool.failed
- Worker ToolResult 的结构化 data 与结果摘要
- worker.reported_observation
- dispatch 生命周期
- artifact / supervision / user command 审计事件
- correction / tombstone
```

目标模型：

```text
TemporalEvent
- event_id
- scope_id
- sequence                  Coordinator 分配的单调序号
- event_type
- occurred_at
- ingested_at
- actor_id
- logical_task_id
- dispatch_id
- worker_task_id
- tool_call_id
- success / error
- payload / payload_ref
- causation_id
- correlation_id
- related_entity_ids[]
- supersedes_event_id
```

排序优先使用 scope 内单调 `sequence`，不能只依赖 wall-clock timestamp。

### 与 MissionRuntime 的边界

```text
MissionRuntime
  = 任务状态变迁的控制与真相源

Temporal Memory
  = 状态变迁、callback 和执行结果的审计事实层
```

合法链路：

```text
MissionRuntime.apply_physical_status(...)
  → lifecycle event
  → Temporal Memory
  → Context 读取只读任务视图
```

禁止链路：

```text
memory.update(task_state="COMPLETED")
  → 修改 MissionRuntime
```

`Memory.delete()` 同样不能取消或删除 dispatch；任务结束必须走既有 `MissionRuntime.cancel/abort` 语义。

### Callback 认证、准入与幂等

`/a2a/push-callback` 是唯一 Worker→Coordinator 的 Memory 写入口，但当前实现仅 `ParseDict` 后路由（`src/a2a/coordinator/server.py:833-1000`）。Revision 1 明确：**任何未认证 callback 都不得创建 TemporalEvent、更新 reducer 或进入 Context。**

#### CallbackProofV1

Worker 在 HTTP header `X-A2A-Callback-Proof` 发送 base64 proof；proof 的 canonical payload 为：

```text
worker_id.timestamp.nonce.body_sha256
HMAC-SHA256(coordinator_secret, canonical payload)
```

```text
worker_id       = 请求声明的 worker；随后必须匹配 resolved PhysicalDispatch.worker_id
timestamp       = Unix seconds；max_age=60s，clock_skew=10s
nonce           = 16-byte random hex
body_sha256     = 原始 request body 的 SHA-256；ParseDict 前计算并绑定
```

`CallbackAuthenticator` 复用 `TeamStatusProof` 的 HMAC/TTL 规则，但不能复用其未绑定 body 的 v2 格式。认证后的 nonce 必须写入 SQLite `callback_nonce`（`UNIQUE(worker_id, nonce)`，expiry=timestamp+70s）；该 ledger 是 durable replay fence，不得只依赖进程内 `UsedNonceStore`。认证失败只产生 redacted security log（reason、body digest 前缀、worker claim），**不保存原始 body，也不创建领域 Memory record**。

`MIN_CALLBACK_SECRET_BYTES=16`，与当前 `MIN_COORDINATOR_SECRET_LENGTH` 保持一致。无论 `enable_peer_mail` 是否开启，`memory_read_mode=shadow|read_port` 的 SARCoordinator/SARWorker/standalone CLI 都必须从受保护配置显式加载并校验此 secret；缺失、为空或短于 16 bytes 时拒绝启动并返回 `memory_auth_not_configured`。只允许保留 `legacy` mode，禁止以“可信本机网络”或“未启用 peer mail”为由静默降级为未认证写入。

`shadow|read_port` 的认证 gate 位于 `/a2a/push-callback` 路由的任何 `MissionRuntime`、`EventStore`、`SemanticMapStore`、TaskWatchdog 或 MemoryIngestor 调用之前：proof 失败时上述所有写者均为零调用。只有 `legacy` mode 为旧部署保留当前未签名 callback 兼容行为，而且它不得同时启动 canonical Memory writer 或被宣称为安全 rollout。

在 `shadow|read_port`，认证成功后的 payload 也必须先经 `RedactionPolicy.sanitize_callback()`，再 fan-out 到 **全部**写者：MemoryIngestor、legacy EventStore、SemanticMapStore JSONL、TaskWatchdog、异常/安全审计。sanitizer 的输入/输出必须保留 body digest、event identity 和非敏感结构字段，但替换 secret/HMAC/proof/Authorization/Cookie/mailbox body/credential 原文；合法签名不等于可免除脱敏。`EventStore.append()` 和 `dispatch_task.py` 的内部 task-created/failure adapter 还必须执行同一 policy 的 `sanitize_event()` defensive boundary，保证非 callback producer 不能绕过。`legacy` mode 的旧输出不获得 #12 的安全保证，且不得作为 secure rollout 的验收样本。

为避免 Agent Context 反向泄露，Phase 2 新增 backend-independent `src/Agent/redaction.py`（`SensitiveTextRedactor`）；Coordinator `RedactionPolicy` 复用其同一规则。router/worker agent 在将失败 `ToolResult` 写入 tool `Message`、AgentLogger、step callback 或 A2A sink 前，必须对 `content`、`error` 和递归 `data` 应用它；原始 error 只可在进程内短暂用于 taxonomy，绝不作为 Context、CSV、[DATA] JSON 或日志字段输出。

#### Worker sender bootstrap

当前 push 实际由 `src/a2a/worker/a2a_server.py` 中的 `BasePushNotificationSender(httpx_client=...)` 发出；`CoordinatorWebSocketClient` 只发送心跳，不能承担 callback signing。V1 因此新增 `SignedPushNotificationSender`：它在序列化后的 HTTP body 上计算 `body_sha256`，生成 CallbackProofV1，并在每次重试使用新 nonce。`create_worker_a2a_server()` 接收 `callback_signer`，替换默认 sender；`SARWorker` 与 standalone worker CLI 只从本地受保护配置加载 coordinator secret 并传入 signer，绝不经 task prompt、A2A message、EventQueue、Context 或日志传递 secret。

启用 `shadow|read_port` 时 worker 必须在启动期构造 signer；构造失败时 `create_worker_a2a_server()` 的 AgentCard 必须将 `capabilities.push_notifications=false`。Coordinator `RouterAgent.send_task_async()` 在创建 `TaskPushNotificationConfig` 前读取已注册 AgentCard capability；secure mode 遇到 false 必须返回 typed `memory_auth_not_configured`，不发送 task、不创建 callback URL、更不得 fallback 为无签名 push。`test_worker_callback_signing.py` 必须断言发出的 request header 绑定实际 body、重试 nonce 改变而 body idempotency key 保持、AgentCard capability 与 signer 一致、secret 未出现在 sink/status text；`test_send_message_tool.py` 必须断言 Coordinator 对 capability=false 返回该 typed error。

#### Ingestor 输入与来源

认证成功后，server 必须先通过 active runtime 的 `context_id + worker_task_id` 解析 dispatch，且 header `worker_id == PhysicalDispatch.worker_id`。只有这个步骤成功才构造 `AuthenticatedCallbackEnvelope`：

```text
scope_id          = MemoryScopeFactory 从 admitted context_id + active epoch + configured project/run 解析
dispatch_id        = resolved PhysicalDispatch.dispatch_id
worker_task_id     = A2A task id
actor_id           = resolved PhysicalDispatch.worker_id，不信任 body 自报值
runtime_epoch      = active scope 的 epoch，不信任 callback 字段
callback_kind      = task | status_update | artifact_update
body_sha256        = CallbackProofV1 已绑定的 digest
correlation_id     = "dispatch:" + dispatch_id（MVP）；不得伪造不存在的 tool_call_id
causation_id       = "callback:" + worker_task_id + ":" + body_sha256[:16]
idempotency_key    = sha256(canonical JSON(scope_id, dispatch_id, worker_task_id,
                     callback_kind, normalized_state, body_sha256))
```

现有 worker sink 不传 tool-call/correlation identity；MVP 明确将 `tool_call_id` 设为 nullable，不把 logger 内的局部 correlation 假装成 A2A 协议字段。以后若扩展 sink，必须版本化 envelope，不能改变 V1 key 的解释。

#### 准入状态机

```text
raw HTTP body
  → CallbackProofV1 verify（纯计算，无写入）
  → active scope / dispatch / actor match（无 Memory transaction）
  → short nonce-reservation transaction: BEGIN IMMEDIATE → UNIQUE nonce claim → COMMIT
  → MissionRuntime.apply_physical_status（仅控制面状态；不持有 SQLite transaction）
  → canonical SQLite transaction: idempotency ledger + event + reducer + revision + outbox
  → commit 后 publish revision / exporter
```

- `unknown_context`、`stale_context`、unknown worker task、closed scope、actor mismatch、认证失败：返回明确拒绝；不写任何 Spatial/Embodied/Temporal domain record。
- `callback_replay` 在 nonce reservation 阶段返回，绝不调用 `apply_physical_status`。若发送端因网络重试，必须用新 nonce 重签同一 body；它在 canonical transaction 中由 idempotency ledger 返回首次 receipt。
- `stale_transition`：物理状态不变，但已认证且属于当前 scope 的新 body 可单独形成 observation/event；同一 body 的新 nonce retry 由 idempotency ledger 返回首次 receipt。
- `memory_scope.closed_at` 是 durable fence；新 admission 或 recovery 激活新 scope 前必须关闭旧 scope。关闭后 callback 永远不得重新打开旧 scope，也不得污染新 scope。
- `MissionRuntime` 仍是 state authority；MemoryIngestor 只消费它的 acceptance/rejection 结果，绝不回写 state。

#### 幂等 receipt

```text
idempotency_ledger
- scope_id
- idempotency_key
- event_id
- receipt_sha256
- committed_revision
- created_at
PRIMARY KEY(scope_id, idempotency_key)
```

同 key 的重复请求必须返回原 receipt，不增加 event、projection revision 或 outbox；同 key 但 canonical payload digest 不同必须返回 `idempotency_conflict` 并停止。`sequence` 由单一 SQLite transaction 在 scope 内分配，`UNIQUE(scope_id, sequence)`。

### 索引

```text
(scope_id, sequence)                        事件时间线
(scope_id, dispatch_id, sequence)           dispatch 时间线
(scope_id, actor_id, sequence)              worker 时间线
(scope_id, event_type, sequence)            类型查询
UNIQUE(scope_id, idempotency_key)            callback/retry receipt
causation_id / correlation_id               因果追踪
related_entity_id                           实体反查
FTS                                         可选文本辅助检索
```

## 4.3 Embodied Memory：机器人节点、资源和可用性

### 职责

```text
- node / agent identity
- endpoint、role、capabilities、sensors
- position reference、定位质量
- inventory / resources
- availability / heartbeat / communication state
- current dispatch reference
- team membership
- health / supervision facts
```

目标模型：

```text
EmbodiedNode
- node_id / agent_id
- worker_endpoint
- role
- capabilities[]
- sensors[]
- resource_inventory
- pose_ref
- localization_quality
- availability                online/offline/idle/busy/degraded
- current_dispatch_id
- heartbeat_at
- communication_state
- team_membership
- revision
- source_event_ids[]
```

### 字段权威来源

```text
capabilities / sensors        ← AgentRegistry / AgentCard
online / heartbeat            ← WorkerRegistry / watchdog
position / inventory          ← authenticated Worker sensor/tool result、worker observation、授权 peer evidence；禁止 Coordinator/Memory 直接读 Barrier
electricity / localization    ← authenticated Worker telemetry 或 structured local tool result；WorkerRegistry 保存 resolved 动态视图与 provenance
current dispatch              ← MissionRuntime PhysicalDispatch
worker report                 ← evidence；依字段 policy 与结构化 Worker tool/telemetry 比较，不得无条件覆盖
LLM summary / inference       ← 不得覆盖事实
```

### 索引

```text
(scope_id, node_id)                          主键
(scope_id, availability)                     可用性
(scope_id, capability)                       能力选择
(scope_id, resource_type)                    资源选择
current_dispatch_id                          任务关联
node_id → Spatial geometry                   位置关联
```

### 安全

```text
- team secret、HMAC key、credential 永不进入 Memory。
- mailbox 正文默认留在 transport/mailbox；Temporal 只存必要元数据。
- MemoryIngestor 在持久化前执行 RedactionPolicy：coordinator/team secret、HMAC/proof、Authorization/Cookie、API key、signed envelope、credential、mail body/subject 一律替换为 [REDACTED:<kind>:<sha256-prefix>]；原文不入 SQLite、JSONL、异常或 Context。
- Worker 只能读取授权后的 Embodied view；`viewer_id` 只能来自认证后的 principal，不能信任 Context query 参数。
```

---

# 5. 对外 CRUD 契约

对外仅提供四类操作，但必须是类型化、受权限和 revision 保护的命令：

```text
MemoryService
- create(command)
- read(query)
- update(command)
- delete(command)
```

所有 mutation command 均需要：

```text
scope_id
principal                  已认证的 system/coordinator/worker principal，不是任意字符串 actor
actor_id                   由 principal 派生
domain
idempotency_key
causation_id
correlation_id
expected_revision            update/delete 必填
```

`MemoryService` 是 Coordinator 内部 facade，不是 LLM 工具、HTTP generic CRUD 或 Worker 直连数据库接口。Worker 只能提交受签 callback/领域工具结果；`MemoryIngestor` 是唯一把 candidate event 变成 mutation command 的 owner。

## create

适合：

```text
- 创建 SpatialEntity
- 追加 observation / TemporalEvent
- 注册 EmbodiedNode
- 创建 relation
```

最重要的 Worker 路径：

```text
ToolResult / report_observation
  → create(TemporalEvent)
  → Memory reducer
      ├─ create/update Spatial projection
      └─ update Embodied projection（若含位置、库存、可用性）
```

Worker 不应通过 LLM 驱动的一次调用直接“三写”三个 store。

## read

`read` 必须显式 scope，默认拒绝跨 experiment / context / epoch 查询。

支持：

```text
SpatialQuery:   entity / type / region / freshness / conflict
TemporalQuery:  dispatch/task/worker/type/sequence/causal chain/entity
EmbodiedQuery:  node/capability/resource/availability/spatial relation
GraphQuery:     root relation + edge types + depth
```

统一结果：

```text
MemoryReadResult
- scope_id
- snapshot_revision
- as_of
- records[]
- relations[]
- conflicts[]
- freshness
- next_cursor
- evidence_refs[]
```

## update

```text
Spatial:
  必走 merge/conflict policy，禁止裸字段覆盖。

Temporal:
  不更新旧 event；以 correction / superseding event 表示修正。

Embodied:
  expected_revision + 字段权威来源 + source priority。

Task lifecycle:
  禁止用 Memory update 修改 MissionRuntime。
```

## delete

```text
Spatial:
  tombstone；从 active projection 移除但保留证据。

Temporal:
  retraction/tombstone；不抹除审计事件。

Embodied:
  decommissioned/revoked/expired；不抹除节点历史。

Task/Dispatch:
  不属于 Memory.delete；仅 MissionRuntime cancel/abort。
```

## 权限

```text
Worker:
- 只能通过 authenticated callback 提交自己的 tool/observation candidate
- 不得直接调用 MemoryService、SQLite 或 shared object
- read 仅限 provider 过滤后的授权范围
- 无共享事实 delete/tombstone 权限

Coordinator/System:
- 全域 read
- 校验后的 create/update
- 审计式 tombstone

LLM:
- 不暴露任意 generic CRUD
- 只暴露受限领域工具，例如 report_observation / read_environment_state
```

这保证写入由 framework 层确定性地产生，而不是依赖模型是否“记得正确写 Memory”。

---

# 6. Context 从 Memory 的读路径

Context 的唯一 backend-facing dependency 是 generic `EnvironmentStateProvider`；其 concrete provider 可以组合两个只读端口：

```text
ContextAssembler
  → EnvironmentStateProvider
       ├─ MemoryReadPort          (Spatial / Temporal / Embodied)
       └─ ControlPlaneReadPort    (MissionRuntime / TaskStore task view)
```

Context 不允许直接 import 或实例化：

```text
SemanticMapStore
EventStore
WorkerRegistry
MissionRuntime
SARBarrier
oracle evaluator / truth trace
```

查询输入：

```text
EnvironmentStateQuery
- scope_id
- principal                  authenticated coordinator/worker identity
- viewer_role                coordinator | worker，由 principal 派生
- viewer_id                  由 principal 派生，不接受 LLM/HTTP 任意声明
- current_dispatch_id
- task_focus
- spatial_focus
- temporal_cursor
- token_budget
- required_sections
```

输出：

```text
EnvironmentStateView
- spatial_state
- embodied_state
- relevant_events
- task_execution_state       仅来自 ControlPlaneReadPort，含 control_revision
- freshness                  FRESH | STALE | UNAVAILABLE + reason + source revision
- conflicts
- evidence
- next_cursor
```

选择策略：

```text
Coordinator:
- 当前计划关联的空间对象
- 全队 Embodied 摘要
- 每个 dispatch 的 canonical task view（ControlPlaneReadPort）
- 自上次 cursor 后的关键事件

Worker:
- 自己的 Embodied State
- 当前任务相关空间区域
- 同队协作摘要
- 当前 dispatch 的 Temporal delta
```

`task_execution_state` 不得由 TemporalEvent 重建；删改 Temporal history 不得改变 task view。worker ACL 在 `EnvironmentStateProvider` 内部过滤：只允许自身 Embodied、当前 dispatch 相关空间、经过 `safe_team_view` 脱敏的队伍摘要和授权 Temporal delta。

Coordinator provider 使用本地 `system` principal；worker provider 不得继续直读全局 `/semantic-map`。Phase 4 新增 coordinator `/environment-state` route：它接收 `EnvironmentStateQueryV1`，以 `TeamStatusProof` 验证 worker identity、从当前 `TeamPartitionService` 与 resolved dispatch 推导 scope/viewer，再在 Coordinator 内部执行 provider ACL。请求携带的 `viewer_id`、scope、dispatch 只能作为候选，任何与认证 principal/当前 admission 不符的值均拒绝；renderer 永远不承担权限过滤。

Cursor 由 `ContextSession` 维护，key 为 `(scope_id, viewer_id)`：只可由 `next_cursor` 单调推进；scope/epoch 不同则 reset=0 并渲染 `SCOPE_RESET`，不得用旧 cursor 读取新 scope。token budget 由 ContextAssembler 在组装前计算：

```text
available = token_limit - estimated(system_prompt) - estimated(history) - reserved_completion
reserved_completion = max(configured_min_completion, 1024)
state_budget = max(0, available)

固定优先级：Task Execution 25% → Spatial 35% → Embodied 20% → Temporal 15% → Freshness/Evidence 5%
未使用配额按该优先级回收；state_budget=0 时仅渲染 scope、freshness 与一个截断标记。
```

不要将整段 Temporal history 每轮塞进 prompt。Context 应根据此 budget 读取“当前相关空间快照 + 时间增量 + 必要具身状态”。任何 MemoryReadPort/ControlPlaneReadPort error 必须转化为 `STALE` 或 `UNAVAILABLE` view，renderer 不得抛出并中断 pre-LLM。

`MapSummarizer` 的定位调整为 `Derived View / Context optimization`，不是 Spatial truth source。任何摘要都要附：

```text
source_revision
input_event_ids
derived_at
stale_after_revision
```

---

# 7. 并发、跨线程、跨 A2A 与持久化

## 7.1 单写者原则

```text
Coordinator process
  └─ MemoryIngestor / MemoryWriter
       └─ 三类 Memory、nonce ledger、idempotency ledger、outbox 的唯一 mutation owner
```

Worker 写入路径：

```text
Worker ToolResult
  → A2A sink
  → signed CallbackProofV1 callback
  → Coordinator CallbackAuthenticator
  → MemoryIngestor
  → MemoryWriter
```

禁止 Worker 直接共享 Coordinator 内存对象或直接写同一个数据库。

### Callback / EventStore producer matrix（实施于 Phase 2；目标树冻结于 `e1a5a013…`）

```text
src/a2a/builtin_tools/dispatch_task.py:136 = task_created legacy EventStore write；canonical source 是 MissionRuntime post-lock DISPATCHING receipt
src/a2a/builtin_tools/dispatch_task.py:158 = dispatch failure legacy EventStore write；canonical source 是 MissionRuntime post-lock FAILED receipt

src/a2a/coordinator/server.py:414 = parsed observation report；authenticated callback 后进入 MemoryIngestor
src/a2a/coordinator/server.py:915 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行
src/a2a/coordinator/server.py:966 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行
src/a2a/coordinator/server.py:1019 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行
src/a2a/coordinator/server.py:1052 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行
src/a2a/coordinator/server.py:1085 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行
src/a2a/coordinator/server.py:1126 = artifact/status legacy callback branch；shadow/read_port 在 route-front auth gate 后执行

src/a2a/coordinator/task_watchdog.py:274 = WORKER_UNREACHABLE trusted supervision_event write
src/a2a/coordinator/task_watchdog.py:324 = TASK_STALE trusted supervision_event write
src/a2a/coordinator/task_watchdog.py:364 = TASK_DEADLINE_EXCEEDED trusted supervision_event write
src/a2a/coordinator/task_watchdog.py:392 = TASK_DEADLINE_WARNING trusted supervision_event write
src/a2a/coordinator/task_watchdog.py:415 = TASK_RECOVERED trusted supervision_event write
  = 每个均必须经 dispatch-bound SupervisionEventAdapter 进入 MemoryIngestor
```

`shadow|read_port` 要求 TaskStore 已绑定 active MissionRuntime；无法产生 immutable control receipt 的旧 direct-dispatch path 只能运行 `legacy`，不得声明 canonical coverage。TaskWatchdog 运行在 Coordinator owner event loop，属于 trusted internal producer：`SupervisionEventAdapter` 实现在 `memory/ingestor.py`，由 `server.py`/production composition root 注入到 TaskWatchdog 的 typed `supervision_event_sink` callback；TaskWatchdog 不直接 import Memory。adapter 从 active `PhysicalDispatch` 解析 scope/actor，使用 `idempotency_key=sha256(scope_id, "supervision", event_id)` 写 TemporalEvent；unknown/closed scope 只保留 redacted local diagnostic，不能写 canonical/legacy Memory。`tests/test_memory_producer_matrix.py` 必须对上述 14 个调用点做 source-level inventory assertion，并在新增写点时要求同时声明 canonical event source、idempotency key 和 auth mode。

控制面 lifecycle 的写入路径不同：`MissionRuntime` 在自己的锁内为每个 accepted transition 分配 dispatch-local 单调 `control_revision`，追加 immutable `ControlTransitionJournalEntry(context_id, runtime_epoch, dispatch_id, control_revision, previous_state, state, source, observed_at, result_digest, journal_sha256)`，并与 control-state snapshot 在**同一次** `_persist()` temp+fsync+replace 中原子持久化。`journal_sha256` 是上述命名字段（不含原始 result/body）的 canonical JSON SHA-256；journal 不保存原始 result/body，只保存 redacted metadata 与 digest。`transition_id = (context_id, runtime_epoch, dispatch_id, control_revision)` 是其主键，journal 只在 scope archive 后才可压缩。

锁释放后，runtime 以 owner-event-loop-safe ingress 交付 journal receipt。**callback-origin** transition 的 `/a2a/push-callback` handler 是第一 owner：它把刚返回的 receipt 直接传入自己的 MemoryIngestor bundle，并抑制对此 receipt 的独立 bridge enqueue；**internal-origin** transition（dispatch/cancel/watchdog）才由 `MemoryLifecycleBridge` 排队消费。两条路径都以 `control_idempotency_key=sha256(scope_id, "control", journal_sha256)` 调用 MemoryWriter 的**同一 canonical bundle transaction**；SQLite unique 是 callback/restart race 的第二道 fence，不是替代 owner routing。该 transaction 必须原子写入 `control_receipt`、对应唯一 control-lifecycle TemporalEvent、任意 reducer/relation/revision 与 outbox；bridge **不得**先或独立写 `control_receipt`。若 callback 触发 accepted transition，MemoryIngestor 在同一 transaction 同时 claim callback idempotency 与 control receipt，写 callback evidence event + control-lifecycle event；若 journal 已有 receipt 而 callback body 是新 observation，只写新的 callback event，不重复 control event。桥接通知失败、进程在通知前崩溃或 canonical transaction 失败时，journal 仍是恢复的 source of truth；任何 restart reconciliation 以 journal 与 `control_receipt` 的差集调用同一个 lifecycle bundle，已存在 matching receipt 返回其 event/receipt，绝不新增 event/outbox。此桥接禁止在 MissionRuntime/TaskStore lock 内执行 SQLite、网络、LLM 或 exporter。

## 7.2 精确事务与失败语义

```text
0. 在任何 Memory lock / SQLite transaction 之外：HMAC verify、active scope/dispatch/actor resolve
1. 短 nonce reservation transaction：BEGIN IMMEDIATE → 清理 expiry → INSERT callback_nonce → COMMIT；重复 nonce 立即拒绝
2. 在任何 SQLite transaction 之外：MissionRuntime.apply_physical_status 原子落 control snapshot + ControlTransitionJournalEntry；锁释放后取得 journal-backed immutable receipt
3. BEGIN IMMEDIATE（canonical Coordinator-owned SQLite transaction）
4. 断言 memory_scope 未关闭；若有 callback 则 claim callback `idempotency_ledger`；若有 journal 则 claim `control_receipt(scope_id, dispatch_id, control_revision, journal_sha256)`
5. callback key 重复：返回原 callback receipt；matching control receipt 重复：返回原 control event/receipt并只跳过 control-lifecycle branch（不吞掉 nonduplicate callback observation）；同 unique key 但 digest 不同必须 `idempotency_conflict` / `control_receipt_conflict` 后 rollback
6. 分配 scope sequence；为新 callback append evidence event，为新 journal append 唯一 control-lifecycle event（两者同 bundle 时均在本 transaction）
7. 在同一 transaction 执行 Spatial / Embodied reducer、relation、tombstone 与 scope revision bump
8. 在同一 transaction 写 `idempotency_ledger` / `control_receipt(event_id, committed_revision, journal_sha256)` 与 `memory_outbox`（canonical event_id、scope revision、export kind、payload digest）
9. COMMIT；因此 crash 时 receipt+event+projection+revision+outbox 要么全无、要么全在
10. commit 后才发布 revision、唤醒 exporter；发布失败不回滚 canonical 数据
```

规则：

```text
- callback_nonce 是独立、短小的 durable ingress reservation；canonical SQLite 的 callback/control event + projection + revision + callback idempotency receipt + control_receipt + outbox 必须同一 transaction；任何 canonical failure 均 rollback。
- exporter 是 at-least-once consumer；它不参与 canonical transaction，也不得宣称物理 JSONL append exactly-once。
- retry 必须使用新 proof nonce、相同 callback body；新 nonce 通过认证后由相同 idempotency key 返回旧 receipt。
- Memory lock / SQLite transaction 内不得网络调用、LLM 调用、MissionRuntime 回调或文件 export。
- Context 只读取 immutable snapshot。
- Memory lock 与 MissionRuntime lock 不可嵌套。
- 在线 Memory 路径不得读取 Barrier/ground truth；Worker-local sensing、debug UI/oracle mode 和 terminal 后 evaluator 必须在独立边界中运行，不能在 Memory lock/SQLite transaction 内调用。
```

## 7.3 Canonical persistence

MVP 使用 Coordinator-owned stdlib SQLite + WAL；不依赖 sqlite-vec/pysqlite3。原因：三域需要事务、关系、scope、revision 和索引；JSONL 不能成为唯一查询与恢复源。`MemoryConfig.storage_root` 必须由 Coordinator 的已解析 run log root 产生，canonical path 固定为 `<storage_root>/memory/memory.sqlite3`；standalone 模式必须显式配置本地绝对 `--memory-root`，拒绝来自 worker/LLM/request 的路径。数据库目录必须 `0700`、数据库/manifest 文件必须 `0600`（或以显式 umask 达到等效权限）。

逻辑表与不可省略约束：

```text
memory_scope             UNIQUE(project_id, experiment_id, context_id, runtime_epoch), closed_at
memory_revision          PRIMARY KEY(scope_id), revision
temporal_event           UNIQUE(scope_id, sequence), UNIQUE(scope_id, idempotency_key)
spatial_entity           PRIMARY KEY(scope_id, entity_id)
spatial_geometry         PRIMARY KEY(scope_id, entity_id, geometry_revision)
embodied_node            PRIMARY KEY(scope_id, node_id)
memory_relation          namespace-qualified refs; no mutable control-plane endpoint
idempotency_ledger       PRIMARY KEY(scope_id, idempotency_key), event_id, receipt_sha256
callback_nonce           UNIQUE(worker_id, nonce), expires_at
control_receipt          UNIQUE(scope_id, dispatch_id, control_revision), journal_sha256, event_id, committed_revision; only written inside canonical bundle transaction
control_transition_journal MissionRuntime control-state snapshot 内的 durable PRIMARY KEY(context_id, runtime_epoch, dispatch_id, control_revision)
memory_outbox            PRIMARY KEY(outbox_id), event_id, payload_sha256, status
export_checkpoint        PRIMARY KEY(scope_id, artifact_kind), canonical_revision, artifact_sha256
derived_view             source_revision, input_event_ids, stale_after_revision
tombstone                immutable retraction/retention audit
```

### JSONL compatibility is materialization, not a second writer

迁移期有两种明确模式：

```text
legacy mode  = existing EventStore/SemanticMapStore 继续产生既有 artifact；Memory 仅 shadow write 与 compare
read_port mode = canonical SQLite 是唯一新事实源；compatibility exporter 从 committed canonical records 重建 artifact
```

在 `read_port mode`，exporter 对每个 `(scope_id, artifact_kind, canonical_revision)` 生成 temp file、fsync、写 manifest digest 后 `os.replace()` 到最终路径。兼容承诺是**记录 schema、字段、排序和 run-close 可读性**，不是“每个 callback 对最终 JSONL 做一次物理 append”。Live tailing JSONL 不属于 V1 保证；实时 UI 继续读取 live projection/API，不读取 export 文件。

artifact ownership matrix：

```text
semantic_map.jsonl         Memory exporter；逐行冻结 {ts,event_type:"observation_ingested",observation,object}；observation 必含 reporter/step/object_type/name/position/attributes/confidence/source_task_id/note，object 为该 event 后的 Spatial projection；顺序按 canonical sequence
events_<task>.ndjson       EventStore legacy debug adapter；保留 text[:500] 的 lossy 语义，非 canonical export
mission_graph.jsonl        MissionGraph control-plane debug artifact；不由 Memory exporter 重写
supervision_<dispatch>.ndjson  SupervisionStateStore debug artifact；不由 Memory exporter 重写
map_summary.jsonl          MapSummarizer/experiment artifact；保留既有 consumer，不由 Memory exporter 重写
events.ndjson + CSV        ExperimentLogger artifact；保留既有 consumer，不由 Memory exporter 重写
snapshot_<task>.json / compression_summary.json  ContextSession artifact；按 ContextSnapshotV2 迁移，不混入 Memory export
```

每个 exporter output 必须在 `export_manifest.json` 写入：`scope_id`、`artifact_kind`、`canonical_revision`、record count、canonical payload digest、artifact SHA-256、schema version。重启时只根据 checkpoint/manifest 重建，不对旧文件盲目 append。

```text
SQLite = canonical state / structured query
JSONL  = deterministic compatibility materialization
```

现有 JSONL 文件名、字段和消费链不能被一次性改掉；评测和渲染工具可能依赖它们。目标树当前没有 `sar_orch/eval/dataset.py`，不得引用历史树路径；Phase 5 必须用冻结 fixture 比较字段、排序和 digest，并以新建 `sar_orch/eval/memory_acceptance.py` 的 fixture reader 和真实 `skills/render-sar-report/render_sar_report/loaders.py`（`load_coordinator_events()`、`load_semantic_map()`）验证；不得只检查文件存在。

### Recovery、scope close 与 legacy artifact

启动/恢复顺序固定：

```text
1. 打开 SQLite、校验 schema/version、加载 scope/revision/outbox/checkpoint；不关闭任何旧 scope
2. MissionRuntimeManager 加载 persisted control snapshot + ControlTransitionJournal，保留 pre-recovery epoch/context tuple
3. MemoryLifecycleBridge 对账 journal ↔ `control_receipt`：每个缺失条目仅以 `control_idempotency_key(scope_id,"control",journal_sha256)` 调用同一 lifecycle bundle transaction，绝不尝试重建 callback body/key 或写 callback evidence；matching receipt 校验 journal_sha256 后不重复 event/outbox，mismatch fail-loud、零部分写入
4. MissionRuntimeManager.recover_and_reconcile() 完成 control-plane recovery 并推进 epoch
5. MemoryLifecycleBridge 关闭已对账的旧 scope，并以新 epoch 激活新 scope
6. replay 未完成 outbox；确定性重建 compatibility artifact
7. 安装 CallbackAuthenticator；只有此后才监听 /a2a/push-callback
```

MVP **不自动把历史 NDJSON/JSONL 回填为 canonical Memory**：旧 run 的 scope/identity 不能无歧义解析时，保留为只读 legacy artifact 并在 manifest 标记 `legacy_unmigrated`。未来 backfill 必须是单独 migration，输入需有 `(project_id, experiment_id, context_id, runtime_epoch)` manifest；损坏、截断或歧义输入必须 fail-loud、零部分导入。

MVP retention 固定为：运行中和已完成 scope 不自动 purge；只允许显式 `archive_scope` 生成不可变 manifest 后移出热库。tombstone/retraction 永远保留足以审计的 event、actor、reason、revision；compatibility adapter 的 500 条内存窗口不得裁剪 canonical Temporal history。

## 7.4 向量检索的定位

`pyproject.toml` 提到 MARoS memory_server 的可选 SQLite/vector 依赖。对其源码只读核对后：它具备 SQLite WAL、append-only event、因果 parent ID 与 vector search，但：

```text
- 是 ROS service 架构；
- 没有完整 CRUD；
- 没有 scope/context/epoch；
- 没有三类 Memory 模型；
- 会自动把历史文本注入 system prompt。
```

因此不能直接作为本项目 canonical Memory。

可借鉴：

```text
SQLite WAL
append-only event
FTS/vector 作为 Temporal Recall 的可选辅助
causal parent relation
```

向量检索必须排在结构化 scope/filter 之后：

```text
scope/type/time/entity filter
  → FTS/vector ranking
  → 返回 evidence_refs
```

不能用 semantic similarity 决定当前空间或节点事实。

---

# 8. 当前组件到目标组件的映射

| 当前组件 | 目标角色 | 迁移原则 |
|---|---|---|
| `SemanticMapStore` | Spatial projection / 初期 adapter | 保留 merge、conflict、revision；逐步迁出 agent inventory/task |
| `EventStore` | Temporal compatibility adapter | 移除模块级生命周期错配；按 scope/dispatch 读取 |
| `TaskLogger` / Supervision / mission graph JSONL | 分别为 event source / debug artifact | 以 producer matrix 明确 owner；不把所有 JSONL 误当成 Memory export |
| `MissionRuntime` | Control-plane canonical owner + post-lock receipt source | 仅 emit immutable lifecycle receipt，不接收 generic Memory state mutation |
| `TaskStore` | 逻辑计划控制 | 保持 plan/dispatch 映射与关闭/abort 顺序 |
| `AgentRegistry` | 静态 capability adapter | 提供 identity/role/capability/sensor/AgentCard metadata；其 status/heartbeat 仅为 compatibility mirror |
| `WorkerRegistry` | 动态 Embodied view owner | 提供 heartbeat/availability，持有 Worker telemetry 与 local-tool evidence 归约后的动态字段及 provenance |
| `SARBarrier` | Worker-local sensor simulator / evaluator-only truth source | 不得通过 Coordinator/Memory adapter 写入 observation 或 embodied evidence；只可模拟 Worker 局部工具、explicit oracle/debug mode、terminal 后私有 evaluator |
| `SARCoordinatorStateProvider` | `EnvironmentStateProvider` coordinator adapter | 以 `ControlPlaneReadPort` 提供 task view，以 `MemoryReadPort` 提供领域投影；禁止从 Temporal 重建 task state |
| `SARWorkerStateProvider` | `EnvironmentStateProvider` worker adapter | 将现有 `/team-status` proof 转为 authenticated principal，按 ACL 返回脱敏视图 |
| `ContextManager` | ContextSession + ContextAssembler compatibility facade | 只读 generic provider，移出 observe/pinned truth；保留 router/worker 不同 pruning 的等价不变式 |
| Worker mailbox | 传输/本地消息系统 | 仅投影授权摘要或 Temporal metadata；正文默认不入共享 Memory |

新增目标模块的边界固定如下：

```text
src/Agent/{environment_state.py,redaction.py,error_taxonomy.py}
  = generic query/view 与纯 redaction/error-code taxonomy；不得 import a2a 或 sar_orch

src/a2a/coordinator/memory/{contracts.py,store.py,ingestor.py,callback_auth.py,redaction.py,exporter.py,recovery.py}
  = Coordinator-owned canonical Memory、认证 receipt、outbox、recovery；不得 import Agent ContextManager

sar_orch/environment_state_provider.py
  = concrete composition root；实现 generic provider，组合 MemoryReadPort 与 ControlPlaneReadPort
```

补充：`EventStore.get_summary()` 当前在可见生产路径中未找到 prompt 消费方，主要是测试/诊断使用。迁移期保留它作为 compatibility adapter，不能把其当前格式误当成新的 `Environment State` prompt 契约。

---

# 9. 分期迁移计划与每阶段门禁

## 9.1 全局 rollout / rollback 合同

新增 `ContextConfig.memory_read_mode` 及 router/worker build option：

```text
legacy     = 旧 StateProvider + pinned read path；默认值
shadow     = legacy read + authenticated canonical Memory shadow write + compare
read_port  = EnvironmentStateProvider read path；legacy store 仅 compatibility adapter
```

`context_pinned_enabled` 只控制遗留 pinned 行为，不能代替上述 rollout flag。任何 phase 只可向前推进一个 mode；rollback 只允许 `read_port → legacy`，必须保留 SQLite 和 outbox 只读、停止 exporter 写入、不得删除 event/projection/snapshot。rollback 后产生的差异写入 `memory_rollout_audit`，不静默丢弃。

所有 phase 的共同 entry：fresh review `APPROVE`、目标 HEAD/source-scope check 通过、上一 phase exit 全绿。共同禁止项：不改 A2A SDK、不给 LLM 暴露 generic CRUD、不把 Memory 写回 `MissionRuntime`、不顺手重构未列文件。每一 phase 的 RED→GREEN 必须先写失败测试再写最小实现。

## 9.2 最小人类介入门禁

人工介入用于锁定领域方向和授权不可逆 rollout，而不替代每个 phase 的父侧独立代码验收。除下列三个门禁外，phase exit 由冻结的契约、focused tests、lint 和独立审查自动判定；缺少任一所需人工审批时必须 fail-closed，保留当前较低风险 mode。

| Gate | 时机与唯一目的 | 人类最小审批问题 | 必需验收包 | 拒绝或缺失时的动作 |
|---|---|---|---|---|
| H1: Projection Semantics Lock | Phase 3 实施前；锁定“什么是当前 Spatial/Embodied 事实” | 在线 Memory 是否严格无 simulator/oracle truth；Worker evidence 如何按字段归约；乱序、冲突、跨 epoch、freshness、revision 与脱敏如何呈现；terminal 后 truth comparison 是否永不回写 | `.hermes/plans/memory-system-redesign-h1-projection-contract-card.md`：C1–C7、source policy、三层 revision、online-truth denial、evaluator-only artifact | 不得开始 Phase 3；保持 `legacy` read path |
| H2: Read-Port Cutover | Phase 4 完成且 `shadow` compare allowlist 为零后；授权新 view 进入每轮 LLM Context | 新 `Environment State` 是否足以支持协作；不同 worker 是否只见授权范围；可见差异和 freshness 降级是否可接受 | legacy/new Environment State 并列 diff、零 non-allowlist diff、ACL negative tests、`FRESH|STALE|UNAVAILABLE` 样例、`read_port -> legacy` rollback 演练与 audit | 保持 `shadow`；不得启用 `read_port` |
| H3: Retirement and Release | Phase 5 完成后；授权 canonical Memory 正式运行和 legacy 主路径退役 | 是否接受恢复、兼容 artifact 与真实运行证据，并接受受控 rollback 保留 canonical 数据 | 10-run `memory_acceptance.json` + `memory_projection_quality.json` 汇总、fixture 与 render consumer 兼容结果、kill/restart/outbox recovery、error-code/secret-leak summary、rollback audit | 保留 legacy compatibility；不得 retirement 或删除任何 legacy consumer |

H1 是实施前的领域语义决策，不因 Phase 3 focused tests 通过而自动满足。H1 必须单独确认 H1-INV-1：在线 Memory 无 Barrier/oracle truth、terminal evaluator 不回写。H2 和 H3 是 mode/retirement 授权，不因 pytest、lint 或父侧 phase acceptance 而自动满足。审批记录必须包含 gate ID、目标 commit、本文及 H1 card 的 SHA-256、验收包路径、结论（`APPROVE` 或 `REJECT`）和明确的 allowlist；无记录即视为 `REJECT`。

## Phase 0 — 契约与最小 kernel scaffold

**Entry：**目标树仍为 `LLaMAR-memory-redesign/feat/memory-redesign@e1a5a013…` 或已按 §冻结规则重新审查；本文当前状态为 `REVISED — fresh independent review pending`，不得据此开始实施。

> **历史状态：**Phase 0–2 已在当前目标树完成并记录于 progress；下列 `Existing (historical create)` 仅保留实现 provenance，不是任何后续 phase 的 Create 操作。plan ledger verifier 只把仍待实施的 `Create` / `Modify` 行当作前向文件操作。

**Machine-readable files：**

- Existing (historical create): `src/Agent/environment_state.py`
- Existing (historical create): `src/a2a/coordinator/memory/__init__.py`
- Existing (historical create): `src/a2a/coordinator/memory/contracts.py`
- Existing (historical create): `src/a2a/coordinator/memory/store.py`
- Existing (historical create): `tests/test_memory_contracts.py`
- Existing (historical create): `tests/test_memory_scope.py`
- Existing (historical create): `tests/test_memory_control_journal.py`
- Modify: `src/a2a/coordinator/mission_runtime.py` — 持久 `control_revision` / ControlTransitionJournal / lock-external receipt seam。
- Modify: `src/a2a/coordinator/cli.py` — 仅增加 validated local `--memory-root` / MemoryConfig 入口。
- Modify: `tests/test_mission_runtime_lifecycle.py`

**Contract / tests：**先让 `test_memory_contracts.py` 断言 `MemoryScopeV1` 的 canonical serialization、namespace relation、`FRESH|STALE|UNAVAILABLE` 和无控制面 mutation；再实现最小 DTO/store schema。`test_memory_scope.py` 必须覆盖缺 project/run/context/epoch 的 fail-closed、同 context 不同 epoch 隔离、closed scope 不可重开。`test_memory_control_journal.py` 必须注入“control `_persist()` 成功、bridge 尚未运行”的崩溃点，断言 restart 后仍有唯一 journal entry 可供 reconciliation，并验证 journal canonical digest 不含 raw callback body。

**Forbidden：**不接 callback、不切读路径、不写 JSONL export、不迁移旧 artifact。

**Exit invariant：**`PhysicalDispatch.state` 仍是唯一 control truth；每个 accepted lifecycle transition 同时有单调 `control_revision` 与 durable journal entry，但尚未改变生产 callback 写路径。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_memory_contracts.py tests/test_memory_scope.py tests/test_memory_control_journal.py tests/test_mission_runtime_lifecycle.py -q
```

## Phase 1 — active 名称迁移、Context 协议和 feature flag

**Machine-readable files：**

- Existing (historical create): `tests/test_environment_state_rename.py`
- Existing (historical create): `tests/test_context_protocol_closure.py`
- Modify: `src/Agent/router_agent/context.py`
- Modify: `src/Agent/router_agent/hooks.py`
- Modify: `src/Agent/router_agent/agent.py`
- Modify: `src/Agent/router_agent/build.py`
- Modify: `src/Agent/worker_agent/context.py`
- Modify: `src/Agent/worker_agent/hooks.py`
- Modify: `src/Agent/worker_agent/agent.py`
- Modify: `src/Agent/worker_agent/build.py`
- Modify: `src/Agent/router_agent/tools/skill_loader.py`
- Modify: `src/Agent/worker_agent/tools/skill_loader.py`
- Modify: `src/a2a/builtin_tools/query_task_events.py`
- Modify: `src/a2a/coordinator/team_status_auth.py`
- Modify: `src/a2a/coordinator/team_partition_service.py`
- Modify: `sar_orch/coordinator_state_provider.py`
- Modify: `sar_orch/prompts/coordinator/system.md`
- Modify: `sar_orch/prompts/coordinator/system.semantic.md`
- Modify: `sar_orch/prompts/coordinator/system.oracle.md`
- Modify: `sar_orch/prompts/worker/system.md`
- Modify: `sar_orch/skills/coordinator/fire-suppression/SKILL.md`
- Modify: `sar_orch/skills/coordinator/person-rescue/SKILL.md`
- Modify: `sar_orch/skills/worker/firefighting/SKILL.md`
- Modify: `sar_orch/skills/worker/inventory-management/SKILL.md`
- Modify: `sar_orch/skills/worker/navigation/SKILL.md`
- Modify: `sar_orch/skills/worker/person-rescue/SKILL.md`
- Modify: `tests/test_coordinator_semantic_mode.py`
- Modify: `tests/test_phase3_read_mailbox.py`
- Modify: `tests/test_phase5_team_status_auth.py`
- Modify: `AGENTS.md`
- Modify: `docs/system_docs/contextmanager.md`
- Modify: `docs/system_docs/route_strategy.md`
- Modify: `docs/system_docs/semantic_map.md`
- Modify: `tool_gap_analysis.md`
- Modify: `tests/test_context_snapshot.py`

**Contract / tests：**把 renderer title 改为 `## Environment State`；把 `Output Format` 移到稳定 system prompt；引入 `memory_read_mode=legacy` 默认值。`ContextSnapshotV2` 禁止 serializing pinned/RuntimeState。rename test 必须只扫描 §2.3 active manifest，历史目录须作为显式 excludes；protocol test 覆盖 router/worker 各自 prune 后无 orphan/pending tool-call，并验证 user state block 不含 output contract。

**Dirty-doc rule：**本 phase 对已有 dirty docs 只允许三方合并目标术语相关 hunk；实施者不得覆盖其余用户改动。

**Forbidden：**不启用 SQLite callback 写入、不移除 legacy pinned fallback、不改变 worker 的 pruning 语义，只把其等价不变式写成测试。

**Exit invariant：**manifest 中 legacy term 为零；三种 coordinator prompt、worker prompt、skills 与 renderer 同步；`legacy` 仍是默认 read path。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_environment_state_rename.py tests/test_context_protocol_closure.py tests/test_context_snapshot.py tests/test_worker_state_provider.py -q
```

## Phase 2 — authenticated Temporal shadow write

**Machine-readable files：**

- Existing (historical create): `src/a2a/coordinator/memory/callback_auth.py`
- Existing (historical create): `src/a2a/coordinator/memory/ingestor.py`
- Existing (historical create): `src/a2a/coordinator/memory/redaction.py`
- Existing (historical create): `src/a2a/coordinator/memory/recovery.py` — Phase 2 control-journal bundle reconciliation；Phase 5 扩展 outbox/export recovery。
- Existing (historical create): `src/Agent/redaction.py`
- Existing (historical create): `src/a2a/worker/callback_sender.py`
- Existing (historical create): `tests/test_memory_callback_auth.py`
- Existing (historical create): `tests/test_memory_ingestor.py`
- Existing (historical create): `tests/test_memory_redaction.py`
- Existing (historical create): `tests/test_worker_callback_signing.py`
- Existing (historical create): `tests/test_memory_producer_matrix.py`
- Existing (historical create): `tests/test_memory_supervision_ingest.py`
- Modify: `src/a2a/coordinator/server.py`
- Modify: `src/a2a/coordinator/mission_runtime.py`
- Modify: `src/a2a/coordinator/event_store.py`
- Modify: `sar_orch/map/store.py`
- Modify: `src/Agent/router_agent/agent.py`
- Modify: `src/Agent/router_agent/logger.py`
- Modify: `src/Agent/worker_agent/agent.py`
- Modify: `src/Agent/worker_agent/logger.py`
- Modify: `src/a2a/worker/sink.py`
- Modify: `src/a2a/worker/a2a_server.py`
- Modify: `src/a2a/worker/cli.py`
- Modify: `src/a2a/coordinator/router.py`
- Modify: `src/a2a/coordinator/task_watchdog.py`
- Modify: `sar_orch/worker.py`
- Modify: `src/a2a/builtin_tools/dispatch_task.py`
- Modify: `sar_orch/coordinator.py` — 注入 MemoryConfig(run_id/log root) 与 server ingestor。
- Modify: `sar_orch/experiment.py` — 显式传递 run_id/log root，不从 callback 推断 experiment scope。
- Modify: `tests/test_coordinator_push_callback.py`
- Modify: `tests/test_send_message_tool.py`
- Modify: `tests/test_memory_control_journal.py`
- Modify: `tests/test_task_watchdog.py`

**Contract / tests：**实现 CallbackProofV1、SignedPushNotificationSender、durable nonce、AuthenticatedCallbackEnvelope、scope-close fence、shared SensitiveTextRedactor/RedactionPolicy、idempotency receipt、SupervisionEventAdapter 与 journal-backed post-lock lifecycle bridge。测试先覆盖 missing-secret/unsigned/expired/body-tampered/worker-mismatch/replayed nonce 全拒绝，且断言 EventStore/SemanticMapStore/TaskWatchdog/Memory 均零调用；再用**有效签名**的 secret/HMAC/Authorization/mailbox-body payload、worker/router failed ToolResult 以及 `dispatch_task` failure payload 断言 SQLite、`events_<task>.ndjson`、`semantic_map.jsonl`、Context、AgentLogger、A2A `[DATA]`、error/security-audit 均无原文；触发 5 类 TaskWatchdog supervision event，断言每个 event_id 仅产生一个 canonical TemporalEvent，closed/unknown scope 零 Memory 写入；再覆盖 14-point producer matrix、同 body + 新 nonce 的 retry 返回同 receipt、600 个 canonical events 不裁剪、旧 scope callback 零 projection 改动。断言 callback-origin journal 由 handler bundle 直接消费、不会被 bridge 二次 enqueue；对 lifecycle bundle 注入「BEGIN 后/commit 前」和「commit 后/publish 前」crash，以及 callback/restart reconciliation 并发竞争：每种情况均断言 `control_receipt`、control event、projection/revision、outbox 要么全无要么各一个；journal digest mismatch 必须 fail-loud、零部分写入。

**Forbidden：**`memory_read_mode` 必须保持 `shadow` 或 `legacy`；不能让 Context 读取新库，不能删除 EventStore/SemanticMapStore 旧写路径。

**Exit invariant：**每个已认证 callback 的 `(scope_id,idempotency_key)` 至多一个 callback TemporalEvent 和一次 projection；每个 `(scope_id,dispatch_id,control_revision,journal_sha256)` 至多一个 control-lifecycle event/receipt/outbox bundle；所有未认证 callback 为零领域写入；MissionRuntime 仍是 state authority。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_memory_callback_auth.py tests/test_memory_ingestor.py tests/test_memory_redaction.py tests/test_worker_callback_signing.py tests/test_memory_producer_matrix.py tests/test_memory_supervision_ingest.py tests/test_memory_control_journal.py tests/test_coordinator_push_callback.py tests/test_task_watchdog.py tests/test_mission_runtime_lifecycle.py -q
```

## Phase 3 — Spatial / Embodied projection 与脱敏

**Human entry gate：**开始 Phase 3 前，H1 `Projection Semantics Lock` 必须对当前目标 commit 批准；H1 card 的 C1–C6 成为 `tests/test_memory_projections.py` / `tests/test_memory_online_truth_boundary.py` 的可追溯测试来源，C7 由 Phase 5 evaluator test 验证。

**Machine-readable files：**

- Modify: `src/a2a/coordinator/memory/contracts.py` — `NormalizedProjectionInputV1`、在线 provenance allowlist、field-level source policy、canonical/entity/view revision DTO。
- Modify: `src/a2a/coordinator/memory/store.py` — projection relation/outcome、entity revision 与 view-visible revision 的原子读写；canonical `memory_revision` 语义保持不变。
- Modify: `src/a2a/coordinator/memory/ingestor.py` — 在 canonical transaction 内将规范化 Worker evidence 交给 reducer；oracle/direct-world candidate 在 reducer 前 fail-closed。
- Create: `src/a2a/coordinator/memory/projections.py`
- Create: `tests/test_memory_projections.py`
- Create: `tests/test_memory_online_truth_boundary.py`
- Modify: `tests/test_memory_redaction.py`
- Modify: `tests/test_memory_ingestor.py`
- Modify: `sar_orch/map/store.py`
- Modify: `sar_orch/coordinator.py` — 移除 online semantic map 的 Barrier priors / checker ground-truth 注入；只保留 Worker evidence composition。
- Modify: `sar_orch/coordinator_state_provider.py`
- Modify: `sar_orch/worker_state_provider.py`
- Modify: `src/a2a/coordinator/agent_registry.py`
- Modify: `src/a2a/coordinator/worker_registry.py`
- Modify: `src/a2a/coordinator/task_watchdog.py`
- Modify: `tests/test_semantic_map.py`

**Contract / tests：**先规范化并验证 Worker/control evidence 的 scope、env_step、provenance、source priority、confidence、evidence identity；Temporal first、reducer second。断言 H1-INV-1 在线无 oracle 真值、C1–C6 的正常更新/乱序/同级冲突/跨 epoch/字段级 Worker policy/脱敏语义；`memory_revision`（每个 accepted canonical bundle）与 entity/view projection revision 分离。扩展 Phase 2 redaction test，覆盖 reducer 派生字段、relation outcome、export candidate、Context/error text 均无原文或 oracle truth。

**Forbidden：**不切 Context read path、不删除 `SemanticMapStore`，不以 vector similarity 决定当前事实；不让 Memory/Coordinator 直接调用 `SARBarrier.get_env_snapshot()`、checker/ground truth 或 oracle evaluator；不以 simulator truth 初始化、修正、回填 online Memory。

**Exit invariant：**同一 Worker evidence/event relation 可追踪 Temporal→Spatial/Embodied；旧 evidence 不回退当前字段；同级矛盾显式 conflict；跨 epoch 零领域写入；canonical/entity/view revision 语义分别成立；机密和 oracle truth 不跨 Ingestor 边界。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_memory_projections.py tests/test_memory_online_truth_boundary.py tests/test_memory_redaction.py tests/test_memory_ingestor.py tests/test_semantic_map.py -q
```

## Phase 4 — EnvironmentStateProvider read-port cutover

**Machine-readable files：**

- Create: `sar_orch/environment_state_provider.py`
- Create: `tests/test_environment_state_provider.py`
- Create: `tests/test_environment_state_acl.py`
- Modify: `src/Agent/router_agent/context.py`
- Modify: `src/Agent/router_agent/hooks.py`
- Modify: `src/Agent/router_agent/agent.py`
- Modify: `src/Agent/router_agent/build.py`
- Modify: `src/Agent/worker_agent/context.py`
- Modify: `src/Agent/worker_agent/hooks.py`
- Modify: `src/Agent/worker_agent/agent.py`
- Modify: `src/Agent/worker_agent/build.py`
- Modify: `src/a2a/coordinator/server.py`
- Modify: `src/a2a/coordinator/team_status_auth.py`
- Modify: `sar_orch/coordinator_state_provider.py`
- Modify: `sar_orch/worker_state_provider.py`
- Modify: `sar_orch/map/store.py`
- Modify: `sar_orch/coordinator.py` — 注入 coordinator EnvironmentStateProvider/system principal。
- Modify: `sar_orch/worker.py` — 注入 authenticated worker provider，不再构造全局-map 直读 view。
- Modify: `tests/test_worker_state_provider.py`
- Modify: `tests/test_context_snapshot.py`

**Contract / tests：**concrete provider 组合 `MemoryReadPort + ControlPlaneReadPort`；task state 必须来自 control plane，Temporal 删除/重排不能改变它。覆盖 cursor monotonicity、跨 epoch reset、budget section priority、FRESH/STALE/UNAVAILABLE、worker A 无法读 worker B inventory/position、NeedInput resume tool-call 闭合。先在 `shadow` compare legacy/new view；只有 diff allowlist 为零且 H2 `Read-Port Cutover` 对当前目标 commit 批准后，才可切 `read_port`。

**Forbidden：**不允许 worker 继续 HTTP 直读全局 `/semantic-map`；不允许 renderer 代替 provider 做 ACL；不允许同时把 legacy 和 read-port 的领域 truth 拼入同一 view；不允许在此 phase 删除 legacy adapter 或修改 control-plane state 机。

**Rollback：**任何 provider/error/ACL gate 失败，切回 `legacy` 并保留 canonical DB/outbox 只读；写 `memory_rollout_audit`，不删除已有事件。

**Exit invariant：**Context 不直接 import backend；task view 有 control revision；snapshot 无领域 truth；worker view 由 authenticated principal 限制。未获 H2 时，本 phase 的实现与 shadow compare 可验收，但 rollout mode 必须保持 `shadow`。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_environment_state_provider.py tests/test_environment_state_acl.py tests/test_context_protocol_closure.py tests/test_worker_state_provider.py -q
```

## Phase 5 — recovery、compatibility materialization 与 legacy retirement

**Machine-readable files：**

- Create: `src/a2a/coordinator/memory/exporter.py`
- Modify: `src/a2a/coordinator/memory/recovery.py`
- Create: `src/Agent/error_taxonomy.py`
- Create: `sar_orch/eval/memory_acceptance.py`
- Create: `sar_orch/eval/memory_projection_quality.py` — terminal-only, read-only comparison of Worker evidence/Memory snapshot against evaluator-private truth trace。
- Create: `tests/test_memory_recovery.py`
- Create: `tests/test_memory_compat_export.py`
- Create: `tests/test_memory_acceptance_metrics.py`
- Create: `tests/test_memory_projection_quality.py`
- Create: `tests/test_tool_result_error_protocol.py`
- Modify: `src/a2a/coordinator/server.py`
- Modify: `src/a2a/coordinator/event_store.py`
- Modify: `src/a2a/coordinator/supervision_state_store.py`
- Modify: `sar_orch/experiment.py`
- Modify: `sar_orch/logger.py`
- Modify: `sar_orch/worker.py`
- Modify: `sar_orch/coordinator.py`
- Modify: `src/Agent/controller/sink.py`
- Modify: `src/Agent/router_agent/agent.py`
- Modify: `src/Agent/worker_agent/agent.py`
- Modify: `src/a2a/worker/sink.py`
- Modify: `skills/render-sar-report/render_sar_report/loaders.py`

**Contract / tests：**验证启动顺序、outbox replay、temp+fsync+replace、export manifest/digest、legacy-unmigrated 标记和 `semantic_map.jsonl` fixture 的字段/排序兼容。`memory_acceptance.py` 必须写 `memory_acceptance.json`：`coverage`、`transport_rate`、`framework_error_counts`、`failed_tool_rows`、`missing_error_code_rows`、scope/revision/export digest。`memory_projection_quality.py` 只在 run terminal、canonical Memory freeze 后读取 evaluator-private truth manifest/trace 与只读 Memory snapshot，写 `<results_dir>/memory_projection_quality.json`：`schema_version`、`scope_id`、`terminal_status`、`memory_manifest_sha256`、`truth_trace_sha256`、`evaluator_version`、Worker report quality、Memory integration quality；不得写 SQLite、projection、revision、outbox、Context 或 compatibility artifact，也不得把 raw truth trace 放进 candidate/Agent 可读路径。其质量指标只比较同 step、Worker 可观测范围内的 report/projection accuracy、coverage、freshness/conflict calibration 和 evidence traceability；不得把 oracle 结果作为本次 run 的 online correction。Phase 5 新建 pure `Agent/error_taxonomy.py`：它将失败 ToolResult 的结构化 error 分类为 allowlisted framework code、`unclassified_tool_error` 或 `missing_error_code`；不得解析 `content="Error: …"`。router/worker Agent 在 redaction 前产生公开 `error_code`、在 redaction 后把它随 `tool_result` event 传出；raw error 不入 Message/logger/[DATA]。随后两条真实 producer 接入 outcome：`sar_orch/worker.py` 以 `{success,error_code}` 调 `log_agent_interaction`；`sar_orch/coordinator.py` 在 router `tool_result` 后以同一字段记录 `send_message`/router tool outcome，`sar_orch/logger.py` 扩展 `router_interactions.csv` schema/API 支持 `Success` 与 `ErrorType`。`memory_acceptance.py` 聚合两个 CSV 的失败 outcome，不能靠 grep 日志猜测。

**Forbidden：**不自动 backfill 歧义历史 artifact；不删除旧 artifact consumer，直至兼容测试和 run-close eval/render 比较通过。

**Exit invariant：**kill/restart 后 committed canonical set、scope fence、outbox/materialized artifact 一致；compatibility consumer 在冻结 fixture 与真实 run 上可读；terminal 后 `memory_projection_quality.json` 与 frozen Memory/truth/evaluator digests 一致且 evaluator 零 canonical writes；无自动 retention purge。只有 H3 `Retirement and Release` 对当前目标 commit 批准后，才可 retirement legacy 主路径或删除 legacy consumer。

**Independent command：**

```bash
# Run from target root after: export PYTHONPATH="src:$PYTHONPATH"
uv run pytest tests/test_memory_recovery.py tests/test_memory_compat_export.py tests/test_memory_acceptance_metrics.py tests/test_memory_projection_quality.py tests/test_tool_result_error_protocol.py -q
```

---

# 10. 实施前和切换前的硬验收

以下不是待讨论项，而是后续准入条件：

1. `MissionRuntime.PhysicalDispatch.state` 保持唯一任务状态控制真相源。
2. Context 无 backend import，且不执行 Memory mutation。
3. 每个 accepted TemporalEvent 可关联 `scope_id / dispatch_id / worker_task_id / actor_id / correlation_id / idempotency_key / sequence`；`tool_call_id` 在 V1 可为 null，不能伪造。
4. 每条 accepted observation 同时具备 Temporal evidence、Spatial projection，并在必要时更新 Embodied；三者共享 evidence/event relation。
5. `delete` 不得篡改任务审计；只能 tombstone/archive，且不自动 purge。
6. Spatial 的乱序、冲突、revision 契约保持或更严格。
7. stale/closed/unknown scope callback 不得污染新 admission / 新 epoch；该断言依赖 durable scope-close fence，不依赖进程内 set。
8. Worker 不得直写 Coordinator Memory Store；未通过 CallbackProofV1 的请求产生零领域写入。
9. prompt 名称与行为指令必须在 §2.3 active manifest 内同阶段同步迁移；历史 artifact 明确豁免。
10. `semantic_map.jsonl`、eval、render 和各自仍归属的 debug adapter 在兼容期按 fixture schema/read path 可读。
11. 任何 Memory/ControlPlane 读失败均显式提供 `FRESH|STALE|UNAVAILABLE`，不静默使用旧投影。
12. 在 `shadow|read_port` 及其 canonical/compatibility artifacts 中，Embodied projection、export、Context、异常和审计不泄露 team secret、HMAC、credential、proof、Authorization/Cookie 或未授权 mailbox 正文；`legacy` mode 明确不属于 secure rollout 或本项验收样本。
13. canonical callback/control event、projection/revision、callback receipt、`control_receipt` 与 outbox 必须在一个 SQLite transaction；JSONL 仅是 transaction 后的确定性 materialization。
14. rollback `read_port → legacy` 不删除 canonical 数据，并产生 rollout audit。
15. 运行期间 Memory/Context/Coordinator semantic path 不得读取 simulator/oracle truth；任何 `barrier`/`oracle`/`ground_truth` candidate 在 reducer 前 fail-closed，产生零领域写入。
16. terminal 后的 evaluator 只能以只读方式比较 frozen Worker evidence/Memory 与 evaluator-private truth trace，写独立 `memory_projection_quality.json`；不得回写 Memory、Context、export 或向 candidate 暴露 raw truth。

## 10.1 验收 artifact 与指标契约

Phase 5 新建的 `sar_orch/eval/memory_acceptance.py` 必须为每个 run 写 `<results_dir>/memory_acceptance.json`：

```json
{
  "schema_version": 1,
  "scope_id": "…",
  "memory_revision": 0,
  "export_manifest_sha256": "…",
  "coverage": 0.0,
  "transport_rate": 0.0,
  "failed_tool_rows": 0,
  "missing_error_code_rows": 0,
  "framework_error_counts": {
    "worker_busy": 0,
    "task_not_routable_yet": 0,
    "unknown_task_id": 0
  }
}
```

`memory_projection_quality.py` 另写 `<results_dir>/memory_projection_quality.json`，最小 schema 为：

```json
{
  "schema_version": 1,
  "scope_id": "string",
  "terminal_status": "completed|timeout|failed|cancelled",
  "memory_manifest_sha256": "sha256",
  "truth_trace_sha256": "sha256",
  "evaluator_version": "string",
  "metric_status": "measured|not_applicable|invalid",
  "worker_report_quality": {
    "evaluated_report_count": 0,
    "observable_field_count": 0,
    "correct_field_count": 0,
    "false_claim_count": 0,
    "stale_report_count": 0,
    "precision": null,
    "recall": null
  },
  "memory_integration_quality": {
    "evaluated_projection_field_count": 0,
    "correct_projection_field_count": 0,
    "stale_projection_count": 0,
    "conflicted_field_count": 0,
    "traceable_field_count": 0,
    "precision": null,
    "recall": null,
    "conflict_precision": null,
    "evidence_traceability_rate": null
  }
}
```

`null` rate 仅允许在对应分母为零且 `metric_status=not_applicable` 时出现；`invalid` 或缺字段必须使 evaluator 非零退出。raw truth trace、逐条 truth payload 和可用于在线修正的细节不属于该 artifact；它们留在 evaluator-private boundary。quality artifact 只报告预注册的 aggregate/diagnostic 结果，并保留 candidate、Memory manifest、truth trace 和 evaluator digest。

`coverage` 与 `transport_rate` 从 `run_metrics.json`/现有 summary artifact 读取，必须非 null；V1 将它们作为必产 telemetry，不虚构数值性能阈值或未定义的 baseline-comparison gate。`framework_error_counts` 是唯一接受的错误指标名称：

```text
producer  = ExperimentLogger.agent_interactions.csv + router_interactions.csv 的 Phase 5 {Success, ErrorType} outcome rows
instrumentation = Agent error_taxonomy 产出结构化 error_code；sar_orch/worker.py 与 sar_orch/coordinator.py 将每个 ToolResult 的 success/error_code 传入扩展后的 logger API
consumer  = sar_orch/eval/memory_acceptance.py
positive test = 分别注入 worker 与 coordinator 的失败 ToolResult(error="worker_busy")，相应 producer CSV 行的 ErrorType 必须为 worker_busy 且 aggregator count 正确
negative test = 失败 ToolResult 的 error 为空时 taxonomy=missing_error_code；任一 Success=false outcome 的 ErrorType 为空时，missing_error_code_rows>0 且脚本以 instrumentation_missing 非零退出
gate      = missing_error_code_rows=0、三个指定 code 均为 0；未知 code 使验收脚本非零退出并打印 code/count
```

不得使用不存在的 `error_counts`，也不得以 grep 运行日志代替结构化 artifact。

## 10.2 必跑测试与独立 smoke

切换前先运行所有 Phase focused tests 与 lint：

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_memory_contracts.py tests/test_memory_scope.py tests/test_memory_control_journal.py \
  tests/test_environment_state_rename.py tests/test_context_protocol_closure.py \
  tests/test_memory_callback_auth.py tests/test_memory_ingestor.py tests/test_memory_redaction.py tests/test_worker_callback_signing.py tests/test_memory_producer_matrix.py tests/test_memory_supervision_ingest.py \
  tests/test_memory_projections.py tests/test_memory_online_truth_boundary.py \
  tests/test_environment_state_provider.py tests/test_environment_state_acl.py \
  tests/test_memory_recovery.py tests/test_memory_compat_export.py \
  tests/test_memory_acceptance_metrics.py tests/test_memory_projection_quality.py tests/test_tool_result_error_protocol.py -q
uv run --with ruff ruff check src/ sar_orch/ tests/
```

再串行运行以下固定矩阵，禁止并发共享端口或 log dir：5 scenes × agent counts `{2,4}` × seed `42` = 10 组；每组 `max_steps=20`、`mode=semantic`。

```bash
set -euo pipefail
root="sar_orch/results/memory_acceptance_$(date +%Y%m%d_%H%M%S)"
for scene in 1 2 3 4 5; do
  for agents in 2 4; do
    run="$root/scene_${scene}_agents_${agents}"
    env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
      uv run python sar_orch/experiment.py \
      --scene "$scene" --agents "$agents" --seed 42 --max-steps 20 \
      --mode semantic --log-dir "$run"
    env PYTHONPATH="src:$PYTHONPATH" uv run python \
      sar_orch/eval/memory_acceptance.py --results-dir "$run"
    # truth_manifest is evaluator-private and becomes readable only after run terminal/freeze.
    env PYTHONPATH="src:$PYTHONPATH" uv run python \
      sar_orch/eval/memory_projection_quality.py \
      --results-dir "$run" --truth-manifest "$root/.evaluator/scene_${scene}_agents_${agents}/truth_manifest.json"
  done
done
```

通过条件：10 个 `memory_acceptance.json` 与 10 个 `memory_projection_quality.json` 均存在且 schema valid；每个 `coverage`/`transport_rate` 非 null、`missing_error_code_rows=0`，三个 `framework_error_counts` 均为 0；projection-quality 的 digest、metric_status 和 denominator 规则通过；export manifest 与 fixture consumer 验证通过。任何 timeout、未生成 artifact、instrumentation_missing、unknown error code、scope/revision/export/truth digest 不一致均为失败，不得只报告“pytest 通过”。

上述验证尚未执行；本文不能被解读为任何测试、smoke 或兼容性已通过。

---

# 11. Revision 1 adjudication、审查记录与设计状态

## 11.1 已裁决发现 ledger

| 原发现 | 裁决 | Revision 1 的确定性 closure | owning gate |
|---|---|---|---|
| 实施树与原冻结树不一致 | confirmed | §冻结输入指定唯一 `/home/wyh/daily_work/LLaMAR-memory-redesign@e1a5a013…`；旧 `LLaMAR/main@d1dada92…` 仅为历史；source-scope check + citation checker + re-freeze rule | Phase 0 entry |
| callback 无认证/防重放 | confirmed | CallbackProofV1、durable nonce、principal/dispatch match、零领域写入拒绝语义 | Phase 2 exit |
| idempotency/outbox/双写不原子 | confirmed | SQLite receipt transaction、post-commit at-least-once outbox、deterministic materialization | Phase 2 / 5 exit |
| restart/backfill/JSONL 兼容无契约 | confirmed | 固定 recovery 顺序、no implicit backfill、artifact ownership matrix、manifest/digest | Phase 5 exit |
| Context Memory 改名范围不一致 | confirmed | §2.3 active manifest + historical excludes + manifest scanner test | Phase 1 exit |
| Temporal 可能成为 task state 第二真相 | confirmed | `ControlPlaneReadPort` 为 task view 唯一来源；Temporal 不可重建任务状态 | Phase 4 exit |
| scope/identity 字段无生产者 | confirmed | MemoryScopeV1 sources、AuthenticatedCallbackEnvelope、nullable tool_call_id | Phase 0 / 2 exit |
| stale/cursor/snapshot/tool-call 协议未定义 | confirmed | Freshness enum、ContextSnapshotV2、cursor/budget ownership、closure assertion | Phase 1 / 4 exit |
| `error_counts` 与三错误码无观测链 | confirmed | `framework_error_counts` 的 producer/consumer/schema/10-run command | Phase 5 / §10 |
| 自动历史 JSONL backfill | obviated for MVP | 只读 `legacy_unmigrated`，未来独立 migration 才可回填 | Phase 5 forbidden |
| worker callback signing 未接到真实 push sender | confirmed | `SignedPushNotificationSender` + `a2a_server.py`/SARWorker/CLI bootstrap；不再误用 heartbeat-only `CoordinatorWebSocketClient` | Phase 2 exit |
| shadow mode 的合法 payload 可泄露到 legacy JSONL | confirmed | route-front auth 后仍先 `sanitize_callback()`，统一 fan-out 到 EventStore/SemanticMap/Memory；有效签名 secret 负测覆盖全部 sink | Phase 2 exit |
| nonce claim 与 control transition 的 transaction 顺序冲突 | confirmed | 独立 nonce-reservation transaction → 无 SQLite transaction 的 control apply → canonical event/outbox transaction | Phase 2 exit |
| 同 `(project,experiment,context,epoch)` closed scope 可重用 | confirmed | `scope_tuple_reuse` fail-closed；只允许同一 active runtime 获取既有 handle | Phase 0 / 2 exit |
| 历史 `sar_orch/eval/dataset.py` 被误作目标 consumer | confirmed | 改为 target 内 `memory_acceptance.py` fixture reader + `render_sar_report/loaders.py` | Phase 5 exit |
| ErrorType 为空可令 zero-error metric 空通过 | confirmed | 双 CSV outcome schema、failed-row/missing-code fields、positive + instrumentation_missing negative test | Phase 5 / §10 |
| Create/Modify ledger 未被标准 verifier 解析 | confirmed | 逐文件 `- Create:` / `- Modify:` 与单行 focused pytest；ledger script 必须非空 clean | §9 / verification |
| control transition 已持久化但 bridge 前崩溃会丢 receipt | confirmed | MissionRuntime durable `ControlTransitionJournal`、journal↔control_receipt recovery reconciliation、两处 crash injection | Phase 0 / 2 / recovery exit |
| raw ToolResult.error 未经协议传递且可能进入 Context/A2A | confirmed | Agent-side SensitiveTextRedactor + structured error_code taxonomy；raw error 不入 Message/logger/[DATA] | Phase 2 / 5 exit |
| secure callback secret 只在 peer-mail 配置路径加载 | confirmed | `MIN_CALLBACK_SECRET_BYTES=16`；secure mode 独立加载校验，Worker AgentCard capability + Router pre-dispatch typed failure | Phase 2 exit |
| TaskWatchdog 的五个 supervision EventStore writer 未纳入 producer inventory | confirmed | 14-point matrix、dispatch-bound `SupervisionEventAdapter`、event_id idempotency、closed/unknown-scope negative test | Phase 2 exit |
| journal 与 control receipt 分离提交会导致 crash 丢失或重放重复 lifecycle event | confirmed | journal_sha256/control key；`control_receipt`、control event、projection/revision/outbox 同一 bundle transaction；recovery 仅重放 lifecycle bundle | Phase 2 / recovery exit |
| 在线 Memory 把 Barrier/simulator truth 当作场景事实来源 | confirmed | H1-INV-1；Worker-only provenance allowlist；semantic map 不再注入 Barrier priors/checker truth；direct oracle candidate typed reject + zero-domain-write negative test；terminal truth 只进入 evaluator-private quality artifact | H1 / Phase 3 / 5 exit |

## 11.2 审查证据与下一状态

- 原设计审查的 parent evidence、5 个独立 leaf report 和 live transcript 位于：`/home/wyh/.hermes/profiles/coder/cache/delegation/live/deleg_0fb72342/` 与同目录 `subagent-summary-*`。
- 早期 Revision 1 审查批次在本文继续修订期间绑定了过期 hash，因此其 `REJECT` 不能作为当前 bytes 的内容 verdict；其中经主审复核的 B/M 已逐行裁决于 §11.1，最终 approval 只能来自重新冻结后的 reviewer。
- 本修订的实施目标由用户明确选择为 `/home/wyh/daily_work/LLaMAR-memory-redesign` `feat/memory-redesign@e1a5a013…`；`LLaMAR/main@d1dada92…` 与 `LLaMAR_evel` 只作为历史证据，不构成当前目标树的 approval。
- H1 online-truth boundary 与 C1–C7 contract card 已单独落盘；本修订仍为 `FRESH_REVIEW_PENDING`，不得把用户决策或本文修改解读为 H1 `APPROVE`。
- 本次 H1 修订修改本文、H1 card 与 progress record；未修改 `src/`、`sar_orch/`、`pyproject.toml` 或运行 artifact。
- **当前 revision exact SHA-256：**由 fresh reviewer 对最终主设计 bytes 计算并写入外部 review record；本文不内嵌自指 hash。
- **下一状态：**对当前主设计与 H1 card 的精确 bytes 在 `feat/memory-redesign@e1a5a013…` 目标树上执行 fresh independent review。只有得到明确 `APPROVE`，并重新确认 source-scope/HEAD 未漂移后，才可开始 Phase 3 实施。
