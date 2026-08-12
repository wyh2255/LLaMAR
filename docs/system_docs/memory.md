---
日期: 2026-08-10
文档类型: 系统架构文档
文档概述: Canonical Memory 子系统完整文档——Coordinator 侧统一记忆（Temporal / Spatial / Embodied）的
  数据契约、存储、摄入管道、认证、投影、导出、恢复、上下文集成与评估体系；
  覆盖 memory-read-mode 三态（legacy/shadow/read_port）接线与在线真相隔离边界
---

# Canonical Memory 系统

## 1. 概述

`src/a2a/coordinator/memory/` 是 **Coordinator 侧统一记忆（canonical Memory）** 的官方实现。Memory 重构（H1→H3，2026-07 至 2026-08-10）把分散在语义地图、EventStore、任务状态里的"领域事实"收拢为一份**可认证、幂等、可恢复、与仿真真值隔离**的持久记忆，并通过 `memory-read-mode`（默认 `read_port`）成为 Worker/Coordinator LLM 上下文的正式读路径。

30 秒结论：

- **三类记忆**：`spatial`（场景物体投影）+ `embodied`（机器人节点投影）+ `temporal`（事件流水线，含 callback/control/supervision/evidence 四类事件）。<br>规划中：**第四类长期记忆（Long-term Memory）**——基于近期内容与 Coordinator 决策反思生成，见 §7 规划方向块。
- **写入只有一条路**：Worker 经 `POST /a2a/push-callback`（`CallbackProofV1` HMAC + nonce 防重放）→ `MemoryIngestor` 在单个 `BEGIN IMMEDIATE` 事务内完成幂等账本 + Temporal 事件 + 投影 + revision + outbox，全有或全无。
- **在线真相隔离（H1-INV-1）**：在线 Memory 只消费 worker report / tool / telemetry 等白名单来源，Barrier / oracle / ground truth 在入口被整包拒绝（零领域写入）；仿真真值仅由 `TruthRecorder` 单向写入 evaluator 私有 trace，评测器以 `mode=ro` 只读比对，**绝不回写同一 run 的 Memory**。
- **读路径**：`read_port`（默认）下 Worker 经 HTTP `/environment-state`、Coordinator 经进程内 `EnvironmentStateProvider` 直接读 canonical 投影；失败时一次性 latch 回退 legacy，永不混用两种真相。
- **持久化**：SQLite（WAL + synchronous=FULL）是唯一事实源，7 类 JSONL 兼容产物由 `MemoryExporter` 在 run 终结/崩溃恢复时**确定性重建**（temp + fsync + manifest + replace），历史遗留 artifact 只标记 `legacy_unmigrated` 永不回填。
- **验收**：16 个 `test_memory_*.py` 契约测试（P0–P5 全量 1636 passed）+ 每 run `memory_acceptance.json` + terminal-only `memory_projection_quality.json` 三层闭环；10-run 固定矩阵 10/10 both-pass，H3 于 2026-08-10 批准（默认值切至 `read_port`，`legacy` 保留为回滚目标）。

## 2. 架构总览

```
┌─ Worker (Alice/Bob/...) ────────────────────────────────────────────────┐
│  SignedPushNotificationSender（CallbackProofV1 + nonce，指数退避重试）   │
│    └─ POST /a2a/push-callback（唯一 Worker→Coordinator Memory 写入口）  │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─ Coordinator Server（src/a2a/coordinator/server.py）────────────────────┐
│  路由 gate：HMAC 纯校验 → 控制面解析 → nonce 预留 → 脱敏 fan-out        │
│    ├─ MissionRuntime 控制面转移（物理状态机，journal receipt）           │
│    ├─ legacy 写点（EventStore / SemanticMapStore / TaskWatchdog）       │
│    └─ MemoryIngestor（唯一 canonical 变更所有者）                        │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─ Canonical Memory（src/a2a/coordinator/memory/）────────────────────────┐
│  MemoryStore（SQLite 单写者，BEGIN IMMEDIATE 精确事务）                 │
│    ├─ temporal_event / idempotency_ledger / control_receipt / outbox    │
│    ├─ projection_field（spatial|embodied 字段级投影）                   │
│    └─ memory_revision / security_audit / callback_nonce / ...          │
│  MemoryExporter（确定性 JSONL 物化，run 终结 / 崩溃恢复触发）           │
│  MemoryRecovery（journal 对账 / scope fence / outbox 重放 / 只读校验）   │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─ 读路径（read_port）────────────────────────────────────────────────────┐
│  Worker：pre_llm hook → fetch_environment_state_async（HTTP + HMAC）    │
│  Coordinator：EnvironmentStateProvider（MemoryReadPort + 进程内注入）    │
│    → render_environment_state_view → ContextManager 尾部 Environment    │
│      State 块（role=user）→ LLM                                        │
└──────────────────────────────────────────────────────────────────────────┘
```

模块职责总览：
| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| 契约层 | `memory/contracts.py` | 552 | 纯数据 + 确定性序列化：scope 身份、命名空间关系、控制面日志条目、规范化投影输入、内存配置校验、摘要函数。不触碰控制面（contracts.py:1-7） |
| 存储层 | `memory/store.py` | 1203 | Coordinator 独占的 stdlib SQLite 存储：schema、scope 生命周期（含 closed-scope 复用拒绝）、关系、控制日志镜像、安全审计、nonce 预留、canonical 事务、Temporal/投影/outbox 原语、只读 facade `MemoryService` |
| 恢复层 | `memory/recovery.py` | 294 | 控制日志对账、scope fence、committed 集校验、outbox 重放、legacy artifact 标记 |
| 摄入层 | `memory/ingestor.py` | 986 | 唯一变更所有者：认证回调 / 控制回执 / 监督事件的 canonical 写入（单一 BEGIN IMMEDIATE 事务） |
| 导出层 | `memory/exporter.py` | 609 | JSONL 兼容物确定性 materialization（temp + fsync + replace），at-least-once 消费，不参与 canonical 事务 |
| 认证层 | `memory/callback_auth.py` | 268 | CallbackProofV1（HMAC 绑定 body）+ SQLite 持久 nonce 栅栏 |
| 投影层 | `memory/projections.py` | 363 | 字段级确定性 reducer（env_step → 源优先级 → 置信度 → 值相等 → 冲突） |

数据流一句话：Worker 回调 → `CallbackAuthenticator.authenticate()`（认证 + nonce 预留，零领域写入）→ `MemoryIngestor.ingest_callback()`（单一 canonical 事务：幂等账本 + temporal 事件 + 控制回执 + 投影 + revision + outbox 全有或全无）→ 导出/恢复时 `MemoryExporter.export_scope()` 从 committed 集确定性重建 JSONL。

---

## 3. 数据模型与契约
### 1. 统一 Envelope：规范化投影输入 `NormalizedProjectionInputV1`

认证后的 Worker 证据在进入投影 reducer 之前被归一化为字段级声明（frozen dataclass，contracts.py:205-260）：

```python
@dataclass(frozen=True)
class NormalizedProjectionInputV1:
    scope_id: str
    event_id: str          # 外部证据身份（如 callback causation id）
    sequence: int          # store 分配的 canonical scope 内序号
    env_step: int | None
    actor_id: str
    provenance: str        # 必须 ∈ ONLINE_PROVENANCE_ALLOWLIST
    domain: str            # "spatial" | "embodied"
    entity_id: str
    entity_type: str
    field_name: str
    value: Any
    confidence: float = 1.0
    runtime_epoch: int | None = None
    dispatch_id: str | None = None
    worker_task_id: str | None = None
    correlation_id: str | None = None
```

- `event_id` 是**外部证据身份**；store 为每个证据束另行铸造 canonical Temporal 事件 UUID（`evt_<uuid4.hex>`，store.py:1141-1143），所有投影字段/关系/outcome 引用该 canonical UUID，保证 Temporal → 投影 join 稳定（contracts.py:209-215）。
- `validate()` 强制：`scope_id/event_id/entity_id/entity_type/field_name` 非空字符串、`domain ∈ {"spatial","embodied"}`、`provenance ∈ ONLINE_PROVENANCE_ALLOWLIST`，否则抛 `MemoryContractError("invalid_projection_input", ...)`（contracts.py:245-260）。
- `source_priority` 属性按字段查 `FIELD_SOURCE_POLICY`，同一 provenance 对 position 与 battery 的排名可以不同（contracts.py:241-243, 187-199）。

**领域分类（domain）**：代码中只有两种——`"spatial"`（空间实体）与 `"embodied"`（具身节点）。约束出现在 `projection_field.domain` 列的 `CHECK(domain IN ('spatial','embodied'))`（store.py:171）与输入校验（contracts.py:251）。对应存储表为 `spatial_entity`（store.py:153-160）与 `embodied_node`（store.py:161-168）。未发现独立的 "temporal 记忆类型" 枚举——Temporal 不是 domain，而是事件流水线（`temporal_event` 表，store.py:100-123）。

**相关辅助契约**：
- `ProjectionEntityRevision`（contracts.py:263-271）：scope/domain/entity 的 revision + `as_of_sequence`。
- `ProjectionViewRevision`（contracts.py:274-280）：viewer 可见快照 revision。
- `MemoryRevision`（contracts.py:544-552）：per-scope 单调投影 revision（`scope_id, revision, updated_at`）。
- `normalize_inventory()`（contracts.py:118-151）：Worker 上报的 inventory（list / count dict / 字符串化字面量）规范化为排序去重的资源名列表；只用 `ast.literal_eval` 结构化解析，**永不 eval**，解析失败折叠为空列表。

### 2. 作用域（scope）语义与身份

`MemoryScopeV1`（frozen dataclass，contracts.py:330-376）是 canonical memory 的作用域身份：

```python
@dataclass(frozen=True)
class MemoryScopeV1:
    project_id: str
    experiment_id: str
    context_id: str
    runtime_epoch: int
```

- **scope_id 派生**：四个字段的 canonical UTF-8 JSON（`sort_keys=True, separators=(",",":")`, contracts.py:283-287）的 SHA-256（contracts.py:373-376）。字段缺一不可：`validate()` 对三个字符串要求非空、`runtime_epoch` 要求非 bool 的 `int >= 0`，失败抛 `MemoryScopeValidationError`（code=`missing_scope_field`，contracts.py:355-371, 318-319），**fail-closed，任何领域写入不得继续**（contracts.py:336-337）。
- 数据库侧 `memory_scope` 表对 `(project_id, experiment_id, context_id, runtime_epoch)` 有 `UNIQUE` 约束（store.py:53），即同一元组全局唯一；`scope_id` 为主键（store.py:47）。
- `MemoryScopeFactory.resolve(context_id, runtime_epoch)` 用 `MemoryConfig` 中的 `project_id`/`experiment_id` 装配 scope（ingestor.py:223-235）——scope 只从可信的 Coordinator 配置 + 控制面状态派生，**从不来自回调数据**（ingestor.py:285-292）。

**scope 生命周期状态**（见「生命周期」节）：激活（ACTIVE）→ 关闭（`closed_at` 非空）→ 不可复用（SCOPE_TUPLE_REUSE / `MemoryScopeReuseError`）。无代码内 "生命周期状态枚举" 表示运行态流转；控制面的状态流转由 `ControlTransitionJournalEntry.state/previous_state` 以字符串记录（见 §5）。

### 3. 引用与关系：`MemoryRef` / `MemoryRelation`

```python
@dataclass(frozen=True)
class MemoryRef:          # contracts.py:379-398
    namespace: str        # "memory"（领域记录）| "control"（控制面只读引用）
    id: str

@dataclass(frozen=True)
class MemoryRelation:     # contracts.py:401-413
    relation_id: str
    scope_id: str
    from_ref: MemoryRef
    relation_type: str
    to_ref: MemoryRef
    valid_from: str | None = None
    valid_to: str | None = None
    source_event_id: str | None = None
    confidence: float = 1.0
```

- `MemoryRef.__post_init__` 校验 namespace ∈ `{"memory","control"}`、id 非空；违反抛 `MemoryRefValidationError`（code=`invalid_ref`，contracts.py:391-398, 322-323）。
- **control 引用只读**：`MemoryService._reject_control()` 对 `namespace=="control"` 的写操作一律拒绝（返回 `MemoryCommandResult(ok=False, error="control_mutation_forbidden")`，store.py:1156-1159），呼应 store 模块文档 "exposes no control-plane mutation"（store.py:1-7）。
- 存储：`memory_relation` 表，`from_namespace`/`to_namespace` 有 `CHECK IN ('memory','control')`（store.py:59-71），按 scope 建索引（store.py:72）。写入为 `INSERT OR IGNORE`（幂等，store.py:419-440）。
- 投影 reducer 自动维护关系：`relation_type` 对 embodied 取 `"observed_by"`、对 spatial 取 `"about"`（projections.py:138-139）。

### 4. 生命周期：scope 激活 / 复用拒绝 / 关闭

| 操作 | 实现 | 语义 |
|------|------|------|
| `activate_scope(scope)` | store.py:311-350 | 先 `validate()`（失败记录 `security_audit("scope_rejected", ...)` 后重抛，store.py:313-320）；BEGIN IMMEDIATE 内查元组：已存在且 `closed_at IS NOT NULL` → 返回 `SCOPE_TUPLE_REUSE`（reason=`scope_tuple_reuse`，store.py:43, 325-330）；已存在且未关闭 → 返回既有 `scope_id`（ACTIVE，store.py:331-333）；不存在 → 插入 scope + `memory_revision(revision=0)`（store.py:334-349） |
| `activate_runtime_scope()` | ingestor.py:285-299 | 准入新 runtime 时调用；`SCOPE_TUPLE_REUSE` 结果直接抛 `MemoryScopeReuseError`（code=`scope_tuple_reuse`，ingestor.py:127-143），即关闭过的 scope 元组**永远不能重开**，调用方必须换 `context_id` |
| `close_scope(scope_id)` | store.py:382-390 | `UPDATE memory_scope SET closed_at=<UTC ISO> WHERE closed_at IS NULL`，返回 `rowcount > 0`（幂等：已关闭返回 False） |
| `scope_closed(scope_id)` | store.py:597-601 | 事务内检查（供 ingest 在 canonical 事务中 fail-closed） |
| 关闭后写入 | ingestor.py:366-369 | `ingest_callback` 在 canonical 事务内先查 `scope_exists`/`scope_closed`，closed → 返回 `CallbackIngestResult("scope_closed")`，零领域写入 |

**状态枚举**：`ScopeActivationStatus(str, Enum)`（store.py:211-213）只有两个成员：

| 成员 | 值 | 出处 |
|------|-----|------|
| `ACTIVE` | `"active"` | store.py:212 |
| `SCOPE_TUPLE_REUSE` | `"scope_tuple_reuse"` | store.py:213（与模块级常量 `scope_tuple_reuse = "scope_tuple_reuse"`，store.py:43，及 `MemoryScopeReuseError.code` 同值，ingestor.py:134） |

返回载体 `ScopeActivationResult(status, scope_id, reason)`（store.py:216-220）。

### 5. 控制面日志条目：`ControlTransitionJournalEntry`（+ `CallbackProofV1`）

控制面生命周期流转（PhysicalDispatch 状态迁移）以不可变日志条目镜像进 Memory（frozen dataclass，contracts.py:416-507）：

```python
@dataclass(frozen=True)
class ControlTransitionJournalEntry:
    context_id: str
    runtime_epoch: int
    dispatch_id: str
    control_revision: int
    previous_state: str
    state: str
    source: str
    observed_at: str
    result_digest: str | None      # 原始 result/body 的唯一痕迹
    journal_sha256: str            # 命名字段的 canonical digest（raw body 排除）
```

- **主键** `transition_id = (context_id, runtime_epoch, dispatch_id, control_revision)`（contracts.py:436-444），与 `control_transition_journal` 表主键一致（store.py:84）。
- `build()`：`result_digest = digest_payload(result)`，`journal_sha256 = control_transition_digest(命名字段)`（contracts.py:462-491, 302-304）；`from_dict()`/`to_dict()` 提供确定性序列化（contracts.py:459-507）。
- 存储：`record_journal_entry()`（`INSERT OR IGNORE`，store.py:462-482）；查询 `control_journal_entries(context_id/runtime_epoch/dispatch_id 过滤)`（store.py:484-505）。
- 摘要原语：`canonical_json_bytes`（contracts.py:283-287）、`digest_bytes` = SHA-256（contracts.py:290-291）、`digest_payload`（先 canonical JSON 再 SHA-256，`default=str`，contracts.py:294-299）。**payload 原文只以 digest 形式落库**。

**CallbackProofV1**（callback_auth.py:48-154）——Worker→Coordinator 写入口 `/a2a/push-callback` 的认证凭证，base64url 编码的 `worker_id.ts.nonce.body_sha256.sig` 五段串（callback_auth.py:49, 92-96, 98-105）：

- canonical payload 为 `worker_id.timestamp.nonce.body_sha256` 的 HMAC-SHA256(coordinator_secret)（callback_auth.py:60-66, 91）。
- 与不绑 body 的旧 `TeamStatusProof` v2 不同，此 proof 绑定确切序列化 HTTP body，篡改在任何写者运行前即被拒绝（callback_auth.py:50-52）。
- 常量：`MIN_CALLBACK_SECRET_BYTES=16`（callback_auth.py:33）、`DEFAULT_MAX_AGE_SECONDS=60`（:37）、`DEFAULT_CLOCK_SKEW_SECONDS=10`（:38）、`DEFAULT_NONCE_TTL_SECONDS=70`（:39）、`NONCE_BYTES=16`（:36）。
- `verify()` 纯校验（无写入），失败原因：`worker_id mismatch` / `body_sha256 mismatch (tampered body)` / `timestamp in future (clock skew)` / `proof expired` / `invalid signature` / `malformed proof` / 空 secret（callback_auth.py:115-154）。
- `CallbackAuthenticator.authenticate()`（callback_auth.py:238-259）两段式：① `verify_proof` 纯 HMAC；② `reserve_nonce` 在 SQLite `callback_nonce`（`UNIQUE(worker_id, nonce)`，store.py:93-98, 544-573）持久预留 nonce 防重放。任一步失败 → 仅写脱敏安全审计 `callback_auth_rejected`，**零领域写入**（callback_auth.py:244-258, 1-11）。
- 构造期校验：secret 缺失/空/<16 字节抛 `MemoryAuthNotConfiguredError`（code=`memory_auth_not_configured`，callback_auth.py:42-45, 193-201），安全模式启动 fail-closed。

### 6. CRUD 契约：`MemoryService`

`MemoryService` 是 Coordinator 内部只读 facade，"guarantees no control-plane mutation"（store.py:1150-1203）。所有写操作先过 `_reject_control`：

| 方法 | 签名要点 | 语义 | 出处 |
|------|---------|------|------|
| `create` | `(scope_id, domain, ref=None, payload=None)` | control ref → `ok=False, error="control_mutation_forbidden"`；否则 `ok=True, value=payload`（Phase 0 占位） | store.py:1161-1172 |
| `update` | `(scope_id, domain, ref, expected_revision=None, payload=None)` | control ref 拒绝；否则 `ok=False, error="unsupported_update"`（**未实现**） | store.py:1174-1186 |
| `delete` | `(scope_id, domain, ref)` | control ref 拒绝；否则 `ok=False, error="unsupported_delete"`（**未实现**） | store.py:1188-1194 |
| `read` | `(scope_id, kind="scope")` | `kind="scope"` → `get_scope`；`kind="relations"` → `relations_for_scope`；其余 `ok=True, value=None` | store.py:1196-1203 |

返回载体 `MemoryCommandResult(ok, error, value)`（store.py:223-227）。**注意**：update/delete 在 Phase 0 未实现，返回 `unsupported_update` / `unsupported_delete` 错误串；真实的"更新"语义由投影层的 upsert（`upsert_projection_field`，store.py:899-953）与 supersede 链（`superseded_event_id`）承担，见「存储层 §2.6」。

### 7. 权限与准入（provenance / truth 隔离）

- **在线来源白名单** `ONLINE_PROVENANCE_ALLOWLIST`（contracts.py:54-64）：`worker_sensor_tool, worker_telemetry, worker_observation, peer_report, registry, control, supervision`。白名单外的来源（barrier/oracle/ground_truth/checker/simulator/direct world snapshot）以类型化 `online_truth_forbidden` 结果拒绝，**零领域写入**（contracts.py:48-53）。
- **禁用 truth 词** `FORBIDDEN_TRUTH_TERMS`（16 个，大小写不敏感，contracts.py:72-91）：`oracle, ground_truth, ground-truth, ground truth, checker, simulator, sim_truth, world_snapshot, world snapshot, direct_world, direct world, truth_trace, coverage_truth, env.controller, get_env_snapshot, object_priors`。即使 allowlisted 来源的值里夹带这些词也算掩蔽 truth 候选，整包拒绝（H1-INV-1，contracts.py:68-71）。
- `scan_forbidden_truth_fields()` 递归扫描 dict key / 字符串值 / 列表（contracts.py:94-115）；`truth_scan_denied()` 返回通用标记（不回显原始词/值，ingestor.py:108-118）。`ingest_callback` 在开事务**之前**执行扫描，命中则记 `security_audit("online_truth_forbidden")` 并返回 `CallbackIngestResult("online_truth_forbidden")`（ingestor.py:351-364）。
- **字段级源策略** `FIELD_SOURCE_POLICY`（contracts.py:158-178）：按字段族给出有序来源类（高→低），如 `position: (worker_sensor_tool, worker_observation, peer_report)`、`battery: (worker_telemetry, worker_sensor_tool, worker_observation)`、`capability/sensor_type: (registry,)`；无专属策略的字段回退 `DEFAULT_FIELD_SOURCE_POLICY`（contracts.py:180-184, 195）。`field_source_priority()` 返回 0=最高优先级索引，策略外来源排最后（仍可作 Worker 证据，只是不能压过高权威候选，contracts.py:187-199）。优先级按字段比较、**从不按整个事件**比较；AgentRegistry 静态元数据不参与动态仲裁（contracts.py:153-157）。

### 8. 错误码 / 状态值总表（附录素材）

**异常类 code（`Exception.code` 属性）**：

| code | 异常类 | 出处 |
|------|--------|------|
| `memory_contract_error` | `MemoryContractError`（稳定基类） | contracts.py:307-315 |
| `missing_scope_field` | `MemoryScopeValidationError` | contracts.py:318-319 |
| `invalid_ref` | `MemoryRefValidationError`（含子原因 `invalid_namespace` / `missing_ref_id`） | contracts.py:322-323, 391-398 |
| `invalid_config` | `MemoryConfigError`（子原因 `missing_experiment_id` / `invalid_memory_root`） | contracts.py:326-327, 527-541 |
| `memory_auth_not_configured` | `MemoryAuthNotConfiguredError` | callback_auth.py:42-45 |
| `idempotency_conflict` | `IdempotencyConflictError`（同 key 不同 payload digest） | ingestor.py:121-124 |
| `scope_tuple_reuse` | `MemoryScopeReuseError`（关闭 scope 重开） | ingestor.py:127-143 |
| `control_receipt_conflict` | `ControlReceiptConflictError`（同迁移不同 journal digest） | ingestor.py:146-149 |
| `memory_export_error` | `MemoryExportError`（unknown scope / 不安全导出目录） | exporter.py:77-84 |

**MemoryCommandResult.error 字符串**：

| error 值 | 含义 | 出处 |
|----------|------|------|
| `control_mutation_forbidden` | 对 control 命名空间 ref 执行写操作 | store.py:1158 |
| `unsupported_update` | Phase 0 update 未实现 | store.py:1186 |
| `unsupported_delete` | Phase 0 delete 未实现 | store.py:1194 |

**Ingest 结果状态串**：

| 状态串 | 出处（定义） | 语义 |
|--------|-------------|------|
| `ok` / `duplicate` / `scope_closed` / `unknown_scope` / `online_truth_forbidden` | `CallbackIngestResult.status`，ingestor.py:206-212 | 回调摄入结果 |
| `ok` / `duplicate` / `scope_closed` / `unknown_scope` | `ControlReceiptResult.status`，ingestor.py:215-220 | 控制回执摄入结果 |
| `ok` / `duplicate` / `online_truth_forbidden` / `scope_closed` / `unknown_scope` / `mixed_scope_bundle` / `mixed_epoch_bundle` / `runtime_epoch_mismatch` / `invalid_input` | `ProjectionIngestResult.status`，projections.py:64-74 | 投影束摄入结果 |

**claim 三元组**：`claim_callback_idempotency` / `claim_control_receipt` 返回 `("fresh", None)` / `("duplicate", row)` / `("conflict", row)`（store.py:609-628, 630-652）。`conflict` = 同 key 不同 canonical digest，必须 fail-loud。

**outbox 状态**：`pending`（默认，store.py:149, 780）→ `exported`（`mark_outbox_exported` 只更新 pending 行，store.py:862-875）。at-least-once：已 exported 的行永不重写。

**投影 outcome 枚举** `ProjectionOutcome(str, Enum)`（projections.py:43-48）：

| 成员 | 值 |
|------|-----|
| `MATERIAL` | `material` |
| `IGNORED_OUT_OF_ORDER` | `ignored_out_of_order` |
| `SUPERSEDED_BY_HIGHER_AUTHORITY` | `superseded_by_higher_authority` |
| `CONFLICTED` | `conflicted` |
| `NO_CHANGE` | `no_change` |

---

## 4. 存储层
### 1. 单写者原则

- **进程内单连接 + 单锁**：`MemoryStore` 只维护一个 SQLite 连接 `self._conn`（store.py:278-280），所有访问由 `threading.RLock` `self._lock` 串行化（store.py:271, 272-277 注释）。连接在启动线程创建、被 server 线程与 worker 回调读写，`check_same_thread=False` + RLock 保证 BEGIN IMMEDIATE 事务内语句不会与并发读写交错（store.py:272-277）。
- **写入入口唯一**：`MemoryIngestor` 是认证回调、控制面生命周期回执、可信监督事件的**唯一变更所有者**（ingestor.py:1-8, 244-250）；允许的领域写入只有 Temporal 事件 + 配对的幂等/控制回执 + scope revision + outbox，全部在一个 canonical 事务内（ingestor.py:247-249）。store 本身 "exposes no control-plane mutation"（store.py:1-7）。
- **导出层是消费者而非写者**：exporter 从不参与 canonical 事务、不宣称物理 JSONL append exactly-once、不做 retention purge、不重写其他写者拥有的 artifact（mission_graph.jsonl / supervision_<dispatch>.ndjson / map_summary.jsonl / events.ndjson+CSV / snapshot_<task>.json）（exporter.py:11-15）。
- **只读 facade**：`MemoryService` 保证不触碰控制面（store.py:1150-1151）；`MemoryStore` 文档明确 "Nothing here can transition a PhysicalDispatch"（store.py:1-7）。

### 2. 精确事务与失败语义（原子性）

- **canonical 事务**：`canonical_transaction()` 上下文管理器 = `BEGIN IMMEDIATE` → yield → `COMMIT` / 异常 `ROLLBACK`（store.py:577-593）。文档明确：receipt + event + projection/revision + outbox **要么全部提交要么全部回滚**；调用方不得在块内做网络 / LLM / 文件导出（store.py:579-583）。
- **事务内原语（不自行 commit）**：`scope_exists` / `scope_closed` / `claim_callback_idempotency` / `claim_control_receipt` / `next_sequence` / `append_temporal_event` / `insert_idempotency_ledger` / `insert_control_receipt` / `write_outbox` / `bump_revision_in_tx` / `get_projection_field` / `upsert_projection_field` / `bump_entity_revision` / `bump_view_revision` / `record_projection_outcome` / `add_relation_in_tx`（store.py:595-1043 区间，注释 "Transaction-scoped primitives — must be called inside canonical_transaction()"，store.py:595）。
- **失败语义示例**：幂等 claim 为 `conflict` 时直接 `raise IdempotencyConflictError`，整事务回滚（ingestor.py:374-377）；控制回执 digest 不匹配 raise `ControlReceiptConflictError`（ingestor.py:446-449）；truth 扫描命中在事务外拒绝（ingestor.py:351-364）。
- **`_begin_immediate`**（内部短事务辅助，store.py:298-307）与 `reserve_callback_nonce` 的自管事务（先清过期再插 UNIQUE，IntegrityError → ROLLBACK → 返回 False 表示重放，store.py:544-573）。
- **持久化参数**：`PRAGMA journal_mode=WAL`、`PRAGMA synchronous=FULL`、`PRAGMA foreign_keys=ON`、`PRAGMA user_version=3`（store.py:286-289）。文件权限：DB 目录 `0700`（store.py:266-270）、DB 文件 `0600`（store.py:283-285）。
- **失败即关**：`close_scope` 幂等（`WHERE closed_at IS NULL`，store.py:382-390）；scope 校验失败先记审计再抛（store.py:313-320）。

### 3. Canonical persistence：SQLite 事实源 + JSONL materialization

- **事实源**：`MemoryConfig.db_path = <memory_root>/memory/memory.sqlite3`（contracts.py:523-525）；`memory_root` 必须是本地绝对路径，拒绝来自 worker/LLM/request 的路径（`"://" in str(root)` 即拒绝，contracts.py:527-541）；`project_id` 默认 `"llamar"`（contracts.py:521）。
- **JSONL 兼容物 = 确定性 materialization，不是第二个写者**（exporter.py:1-16）：exporter 对每个 `(scope_id, artifact_kind, canonical_revision)` 从 **committed canonical 记录** 重建 JSONL：内存构建内容 → temp 文件 + fsync → 写 manifest digest → `os.replace()` 到最终路径（exporter.py:518-568, 149-187）。兼容承诺是记录 schema、字段、排序与 run-close 可读性，**不是**"每个 callback 一次物理 append"（exporter.py:8-9, 526-527）。
- **7 个 canonical artifact**（`CANONICAL_ARTIFACTS`，exporter.py:47-55）：`temporal.jsonl` / `spatial.jsonl` / `embodied.jsonl` / `revision.jsonl` / `outbox.jsonl` / `relations.jsonl` / `semantic_map.jsonl`（exporter.py:39-45）。其中 `semantic_map.jsonl` 是 legacy 兼容物：`{ts, event_type, observation, object}` 行，`object` 通过把 committed spatial 证据事件按与 canonical reducer 相同的 H1 比较顺序（env_step → 源优先级 → confidence → 值相等 → 冲突）重放重建（exporter.py:369-497, 220-259）。
- **materialize 触发时机**（两处）：
  1. **run 终止**：`server.py:582-639 materialize_compatibility_artifacts()` 对 active/last scope 调 `export_scope`（server.py:609），并把 legacy EventStore / SupervisionStateStore 调试产物标记 `legacy_unmigrated`（server.py:611-623）；`legacy` 模式下为 no-op（server.py:595-599）。
  2. **kill/restart 恢复**：`MemoryRecovery.replay_outbox()` 标记 pending outbox 为 `exported` 后无条件重建（recovery.py:223-264, 254）。
- 确定性排序依据：`entity_revisions` 按 domain（spatial → embodied）与 entity id 排序（store.py:1111-1131）、`_outbox_records` 按 `(created_at, outbox_id)` 排序（exporter.py:344-347）、`render_jsonl_bytes` 单行一条记录（exporter.py:140-146）。
- **manifest**：`export_manifest.json`（`EXPORT_MANIFEST_FILENAME`，exporter.py:34），schema v1（exporter.py:35），每 artifact 记 `scope_id / artifact_kind / canonical_revision / record_count / payload_sha256 / artifact_sha256 / schema_version`（exporter.py:87-113, 501-516）。manifest 先原子替换，再逐个 `os.replace` artifact temp，最后 fsync 目录（exporter.py:557-560）。
- **幂等账本 / 控制回执 / outbox 与 Temporal 同事务**：见 ingestor.py:365-438（callback 束）与 476-508（control receipt 束）。

### 4. 线程安全

- `threading.RLock` 保护一切（store.py:271）；连接 `check_same_thread=False`（store.py:278-280），WAL 模式（store.py:286）。
- 所有公开读写方法均 `with self._lock:`（如 get_scope store.py:366-373、outbox_entries store.py:847-853、projection_fields store.py:1053-1071）；事务方法在锁内执行 BEGIN IMMEDIATE（store.py:585-593）。
- RLock（可重入）允许事务内原语互相调用而不死锁（store.py:271, 595 注释）。

### 5. 索引结构（schema 全表）

`_SCHEMA`（store.py:45-208）共 **15 张表**：

| 表 | 主键 / 唯一约束 | 索引 | 行号 |
|----|----------------|------|------|
| `memory_scope` | `scope_id` PK；`UNIQUE(project_id, experiment_id, context_id, runtime_epoch)` | — | store.py:46-54 |
| `memory_revision` | `scope_id` PK → FK memory_scope | — | store.py:55-58 |
| `memory_relation` | `relation_id` PK；from/to namespace `CHECK IN ('memory','control')` | `ix_memory_relation_scope(scope_id)` | store.py:59-72 |
| `control_transition_journal` | `PK(context_id, runtime_epoch, dispatch_id, control_revision)` | — | store.py:73-85 |
| `security_audit` | `id` AUTOINCREMENT | — | store.py:86-92 |
| `callback_nonce` | `PK(worker_id, nonce)` | `ix_callback_nonce_expiry(expires_at)` | store.py:93-99 |
| `temporal_event` | `event_id` PK；`UNIQUE(scope_id, sequence)`；`UNIQUE(scope_id, idempotency_key)` | `ix_temporal_scope_seq`、`ix_temporal_scope_dispatch` | store.py:100-123 |
| `idempotency_ledger` | `PK(scope_id, idempotency_key)` | — | store.py:124-133 |
| `control_receipt` | `PK(scope_id, dispatch_id, control_revision)` | — | store.py:134-142 |
| `memory_outbox` | `outbox_id` PK；`status` 默认 `'pending'` | `ix_outbox_scope(scope_id)` | store.py:143-152 |
| `spatial_entity` | `PK(scope_id, entity_id)`；revision 默认 0 | — | store.py:153-160 |
| `embodied_node` | `PK(scope_id, node_id)`；revision 默认 0 | — | store.py:161-168 |
| `projection_field` | `PK(scope_id, domain, entity_id, field_name)`；domain `CHECK IN ('spatial','embodied')`；含 supersede/conflict 追踪列 | `ix_projection_field_scope`、`ix_projection_field_event` | store.py:169-190 |
| `projection_outcome` | `id` AUTOINCREMENT | `ix_projection_outcome_scope(scope_id)` | store.py:191-203 |
| `memory_view_revision` | `scope_id` PK → FK memory_scope | — | store.py:204-207 |

注：Phase 0 存储层为 SQLite 关系表（含索引），**没有**独立的进程内内存索引结构——"索引"即上述 SQL 索引；投影的当前可见值即 `projection_field` 的 upsert 结果（每 scope/domain/entity/field 一行）。

### 6. update / delete / scope close 语义

- **update 语义（投影层）**：字段更新是 upsert + supersede 链，非原地覆盖——`upsert_projection_field` 用 `ON CONFLICT(scope_id, domain, entity_id, field_name) DO UPDATE` 整体替换当前行，并记录 `superseded_event_id`（store.py:899-953）；每次变更同时写 `projection_outcome`（outcome ∈ ProjectionOutcome 枚举，store.py:995-1022）并 bump 实体/视图 revision（store.py:955-993）。reducer 的裁决顺序：`env_step 栅栏 → 字段源优先级 → confidence → 值相等 → 显式冲突`（projections.py:95-134）；同 step/优先级/confidence 但值不同 → `conflicted`，当前持有者保留、失败候选记入 `conflict_candidates`（projections.py:129-134, 254-259）。
- **delete 语义**：`MemoryService.delete` 返回 `unsupported_delete`（store.py:1188-1194）——**领域删除在 Phase 0 未实现**；数据的"作废"通过 supersede/conflict 追踪实现。物理清理（retention purge）在 exporter/recovery 中明确禁止（exporter.py:12-13, 526-527；recovery.py:18-20）。
- **scope close 语义**：见「数据模型 §4」——`closed_at` 置位后：任何新写入（callback/control receipt）返回 `scope_closed`（ingestor.py:368-369, 491-492）；元组级重开被 `UNIQUE` 约束 + `SCOPE_TUPLE_REUSE`/`MemoryScopeReuseError` 双重拒绝（store.py:325-330, ingestor.py:295-296）。

---

## 5. 恢复与容错
### 1. 恢复流程（重启顺序由组合根负责，模块提供确定性接缝）

`MemoryRecovery`（recovery.py:108-124）构造注入 `store / ingestor / scope_factory / bridge / exporter`（exporter 缺省自建，recovery.py:124）。模块文档给出三步恢复接缝（recovery.py:1-21）：

1. **journal ↔ control_receipt 对账**：`reconcile_control_journal(journal_entries)` 把每个缺失于 `control_receipt` 的持久化 journal 条目以同一 canonical 生命周期束写入；已匹配的回执返回原 event，**绝不追加第二个 event/outbox**；digest 不匹配 fail-loud、零部分写入（底层 canonical 事务回滚）（recovery.py:134-161, 6-10）。实现：逐条调 `ingestor.ingest_control_receipt(entry)`，`status=="ok"` 计 written，`"duplicate"` 跳过，其他记 warning（recovery.py:143-160）。
2. **scope fence**：`close_old_scope(context_id, runtime_epoch)`（= `ingestor.close_scope`，recovery.py:163-165）先关闭旧 epoch scope，`activate_new_scope(context_id, runtime_epoch)` 再激活新 epoch scope（recovery.py:167-169）——持久 closed-scope 栅栏保证回调永远无法重开旧 scope（recovery.py:10-12）。
3. **outbox 重放**：见 §3。

### 2. 幂等 cancel 与幂等回执

- 代码中无 "cancel" 字样的幂等取消 API；幂等由三套 key + 账本实现：
  - `callback_idempotency_key(scope_id, dispatch_id, worker_task_id, callback_kind, normalized_state, body_sha256)`（ingestor.py:43-62）；
  - `control_idempotency_key(scope_id, journal_sha256)`（ingestor.py:65-66）；
  - `supervision_idempotency_key(scope_id, event_id)`（ingestor.py:69-70）；
  - `projection_idempotency_key(scope_id, inputs)`：由 canonical scope + 排序后的完整证据身份（event_id + env_step + domain/entity/field + 脱敏值 digest）派生，同一证据束恒映射同 key，重复束返回类型化 duplicate、零新增 Temporal/revision/outbox 行（ingestor.py:73-95）。
- 重复送达路径：`claim_callback_idempotency` 返回 `duplicate` → `CallbackIngestResult("duplicate", event_id=原事件, receipt_sha256, committed_revision)`，不重放（ingestor.py:378-385）；`conflict`（同 key 异 payload）→ 抛 `IdempotencyConflictError` 回滚（ingestor.py:374-377）。
- 控制回执重复：`claim_control_receipt` duplicate → 返回原 event/commit revision（ingestor.py:450-451, 494-504）；conflict → `ControlReceiptConflictError`（ingestor.py:446-449）。
- **回执哈希**：`receipt_sha256 = digest_payload({"event_id", "committed_revision"})` 写入 `idempotency_ledger`（ingestor.py:415-425），供调用方验证。

### 3. Outbox 重放（kill/restart 后）

`replay_outbox(scope_id, export_dir)`（recovery.py:223-264）：

1. scope 不存在 → `OutboxReplayResult(reason="unknown_scope")`，`artifacts_consistent=False`（recovery.py:237-246）。
2. 取全部 `status=="pending"` 的 outbox 行（recovery.py:247-249）。
3. 先逐行 `mark_outbox_exported`（只动 pending 行，store.py:862-875），计 `replayed`（recovery.py:251-253）。
4. **不重跑 reducer、不写新 canonical event**（recovery.py:226-228）；随后 `export_scope` 从 committed canonical 集无条件重建兼容物（temp+fsync+manifest+replace）（recovery.py:254, 228-233）。重建是无条件幂等的 at-least-once：已一致的文件被重写为相同字节，同时修复被篡改/缺失的 artifact——**绝不盲目 append**（recovery.py:231-233）。
5. 返回 `OutboxReplayResult(scope_id, pending, replayed, artifacts_consistent, scope_closed, revision, manifest_sha256, reason)`（recovery.py:53-64）；`replayed != pending` 时 `reason="outbox_rewrite_failed"`（recovery.py:263）。`artifacts_consistent` 由 `_materialized_match` 逐文件比对磁盘 SHA-256 与 manifest digest（recovery.py:266-275）。

### 4. committed 集校验（只读）

`verify_committed_canonical(scope_id, export_dir)`（recovery.py:173-219）返回 `CanonicalVerification`（recovery.py:67-80）：`scope_exists / scope_closed / revision / pending_outbox / manifest_present / artifacts_consistent / missing_artifacts`。判定：
- scope 缺失 → `missing_artifacts = 全部 CANONICAL_ARTIFACTS`（recovery.py:184-193）；
- manifest 需存在且 `scope_id` 匹配（recovery.py:197-200）；
- 一致性 = 无缺失 artifact 且磁盘每个文件 SHA-256 == 由 committed 集确定性重建字节的 SHA-256（recovery.py:201-210）；
- `ok()` = `scope_exists and manifest_present and artifacts_consistent`（recovery.py:79-80）。
- **只读校验，不修复任何东西**；scope fence 违规（closed scope 表现 active）或 artifact 与确定性重建不符均报告 inconsistent（recovery.py:178-182）。

### 5. Scope close 与遗留 artifact（legacy artifact）

- **永不自动回填**：歧义的历史 JSONL/NDJSON artifact 不自动 backfill 进 canonical Memory；保持只读并标记 `legacy_unmigrated`（recovery.py:18-20, 277-294；exporter.py:570-609）。manifest 的 `legacy_artifacts` 字段记录每个遗留文件的 `{schema_version, artifact_kind, legacy_unmigrated: True, reason}`（exporter.py:595-603）；未来迁移必须显式操作（recovery.py:288-291）。
- 触发点：run 终止时 `server.py:611-623` 收集 `event_store.legacy_artifact_filenames()`（events_<task>.ndjson）与 SupervisionStateStore 的（supervision_<dispatch>.ndjson），调用 `mark_legacy_unmigrated`。
- **恢复永不执行 retention purge**（recovery.py:18-20）；遗留文件不删除（exporter.py:581-582）。

## 6. 摄入管道与回调认证
### 摄入管道（输入源 / 准入状态机 / 幂等 / 去重 / 重试）

#### 输入来源与唯一写入口

- **`/a2a/push-callback` 是唯一 Worker→Coordinator 的 Memory 写入口**（`src/a2a/coordinator/memory/callback_auth.py:1-8` 模块 docstring；路由定义 `src/a2a/coordinator/server.py:1315`）。Worker 端由 `SignedPushNotificationSender` 发出（`src/a2a/worker/callback_sender.py:71-77`）。
- **MemoryIngestor 是"唯一 mutation owner"**：canonical 写入全部发生在一个 `BEGIN IMMEDIATE` SQLite 事务里（幂等 ledger + temporal event + projection/revision + outbox 全有或全无），且它从不改写 MissionRuntime 状态，只消费 runtime 的接受/拒绝结果与 journal receipt（`src/a2a/coordinator/memory/ingestor.py:1-8`；`store.canonical_transaction` 见 `src/a2a/coordinator/memory/store.py:577-593`）。
- 除 callback 外还有三条**内部/受信来源**，均汇聚到同一 ingestor：
  1. **控制面 journal receipt**：`MissionRuntimeManager` 在锁内为每个 accepted transition 追加不可变 `ControlTransitionJournalEntry`（`src/a2a/coordinator/memory/contracts.py:417-430` 起；`mission_runtime.py:1490-1497 record_journal_entry`）。两条消费路径：callback-origin transition 由 push-callback handler 用 `callback_bundle()` 直接打包（`ingestor.py:310-324`、`server.py:1463/1512`），internal-origin transition（dispatch/cancel/watchdog）经 `MemoryLifecycleBridge.enqueue/drain`（`ingestor.py:804-838`）调用 `ingest_control_receipt`（`ingestor.py:476-508`）；重启时 `MemoryLifecycleBridge.reconcile` 以 journal 与 `control_receipt` 的差集补齐（`ingestor.py:843-854`）。
  2. **TaskWatchdog 监督事件**：`SupervisionEventAdapter`（`ingestor.py:857-965`）作为 dispatch-bound sink 注入 watchdog（`server.py:522-526` → `task_watchdog.py:75-77,106`），五种事件类型 `WORKER_UNREACHABLE / TASK_STALE / TASK_DEADLINE_EXCEEDED / TASK_DEADLINE_WARNING / TASK_RECOVERED`（`tests/test_memory_supervision_ingest.py:17-23`）。
  3. **观察证据（projection inputs）**：callback 正文中提取的 Worker observation 由 server 归一化为 `NormalizedProjectionInputV1`，与 callback Temporal event 在同一 canonical 事务中原子 reduce（`server.py:1576-1603` → `_normalize_observation_projection_inputs` `server.py:700-807` → `ingest_callback(projection_inputs=...)` `ingestor.py:335-438`）。
- **读取/导出侧**：`MemoryExporter` 只读 store、消费 outbox（`server.py:600-609`），不参与摄入。

#### 准入状态机（route-front gate → 控制面 → canonical 事务）

`shadow`/`read_port` 模式下，认证 gate 位于路由内**任何** MissionRuntime / EventStore / SemanticMapStore / TaskWatchdog / MemoryIngestor 调用之前（`server.py:1353-1411`）。完整顺序（代码事实）：

1. **HMAC 纯校验（无写入）**：`X-A2A-Worker-Id` + `X-A2A-Callback-Proof` 头 → `CallbackAuthenticator.verify_proof`（`server.py:1367-1382`；`callback_auth.py:211-220`）。
2. **控制面解析（无 Memory 事务）**：active runtime 必须存在（否则 `_reject("unknown_context")`）；body 中的 context 必须等于 active `context_id`（`stale_context`）；`resolve_worker_task(task_id)` 必须命中 dispatch（`unknown_worker_task`）；header worker_id 必须等于 `PhysicalDispatch.worker_id`（`worker_mismatch`）；epoch 取自 `active_runtime._manager.epoch`，**从不信任 body**（`server.py:1384-1402`）。
3. **短 nonce 预留事务**：`reserve_nonce` → SQLite `BEGIN IMMEDIATE` → 清理过期 → `INSERT callback_nonce`（`UNIQUE(worker_id, nonce)`），重放返回 `nonce already used (replay)`（`callback_auth.py:222-236`；`store.py:544-573`；表定义 `store.py:93-99`）。失败即 401 拒绝，**不进入** `apply_physical_status`。
4. **脱敏后 fan-out**：合法签名不免除脱敏——`RedactionPolicy.sanitize_callback(body)` 在 fan-out 到全部写者之前执行（`server.py:1408-1411`）。
5. **控制面转移（SQLite 事务之外）**：`MissionRuntimeManager.handle_callback` / `handle_artifact`（`mission_runtime.py:1686-1723 / 1725-1738`）→ `apply_physical_status`（`mission_runtime.py:499-531`），原子落 control snapshot + journal（temp+fsync+replace，`mission_runtime.py:1442-1483`），锁释放后经 receipt seam 交付 journal receipt。
6. **canonical 事务**：`ingest_callback`（`ingestor.py:335-438`）在一个 `BEGIN IMMEDIATE` 内完成：scope 存在/未关闭断言 → claim callback 幂等 → append callback Temporal event → 逐条 claim/append control receipt → 同事务 reduce projection → bump revision → insert idempotency ledger → 写 outbox。commit 后由外部发布 revision / exporter（见设计文档 §7.2 顺序，代码实现即上述步骤）。

**状态/错误分类**（均为代码中的真实字符串）：

| 分类 | 触发点 | 行为 |
|---|---|---|
| `unknown_context` / `stale_context` | 路由 gate（server.py:1388-1394）；handle_callback（mission_runtime.py:1696-1705） | 401/ignored，零写入 |
| `unknown_worker_task` | 路由 gate（server.py:1398-1399）；handle_callback（mission_runtime.py:1706-1709） | 401/ignored；worker 侧视为**可重试**的瞬态拒绝（见下文 backoff） |
| `worker_mismatch` | 路由 gate（server.py:1400-1401） | 401，零写入 |
| `stale_transition` | handle_callback：物理状态未变（mission_runtime.py:1717-1722） | 路由返回 ignored，但 observation/event 仍可独立摄入（server.py:1647-1653） |
| `unknown_scope` / `scope_closed` | canonical 事务内（ingestor.py:366-369；store.py:597-607） | 返回 typed result，零领域写入；`closed_at` 是 durable fence |
| `scope_tuple_reuse` | `activate_runtime_scope`（ingestor.py:285-299；store.py:211-214 `ScopeActivationStatus.SCOPE_TUPLE_REUSE`） | 抛 `MemoryScopeReuseError`，admission 被拒而非重开已关闭 scope |
| `duplicate` | 幂等 claim（ingestor.py:378-385） | 返回首次 event_id/receipt/revision，零新写入 |
| `conflict`（`idempotency_conflict` / `control_receipt_conflict`） | 同 key 不同 digest（ingestor.py:374-377、446-449；store.py:609-628、630-652） | 抛 typed 异常，整个事务 rollback |
| `online_truth_forbidden` | H1-INV-1 门（ingestor.py:108-118、350-364、616-629） | 整包拒绝、零领域写入，仅记脱敏 security audit（ingestor.py:703-718） |
| `invalid_input` / `mixed_scope_bundle` / `mixed_epoch_bundle` / `runtime_epoch_mismatch` | `ingest_projection` bundle fencing（ingestor.py:599-649） | typed result，零写入 |

**物理状态机**（控制面权威，`mission_runtime.py:39-108`）：`PREPARED → DISPATCHING → ACCEPTED → RUNNING ⇄ INPUT_REQUIRED`，`CANCEL_PENDING → CANCELED/FAILED`，终态 `COMPLETED/FAILED/CANCELED`（`_ALLOWED_TRANSITIONS` `mission_runtime.py:67-108`；`_TERMINAL_STATES` `mission_runtime.py:61-65`）。`normalize_physical_state` 把 A2A/protobuf 字符串归一化到该枚举（`mission_runtime.py:1745-1777`）。**历史/逻辑节点取消**属于控制面而非 Memory 摄入：`cancel_dispatch` 进入 `CANCEL_PENDING` fence（`mission_runtime.py:443-451`），`cancel_dispatch_remote` 只应用远端返回的终态（`mission_runtime.py:453-497`）；已清理的物理 dispatch 通过 `resolve_historical_dispatch`（`mission_runtime.py:342-352`）仍可解析用于幂等控制动作；激活逻辑节点的幂等 cancel/reply 由 `TaskStore.classify_logical_node_dispatches` 支撑——有物理绑定（全终态或已清理）的逻辑节点是真实编排对象而非未知 id（`task_store.py:365-376` 起）。这些状态转移经 journal receipt 进入 Memory（`control.*` 事件），但取消动作本身不直接写 Memory。

#### 幂等 receipt

- **Callback 幂等键**：`callback_idempotency_key(scope_id, dispatch_id, worker_task_id, callback_kind, normalized_state, body_sha256)`（`ingestor.py:43-62`），在 `AuthenticatedCallbackEnvelope.build` 中计算（`ingestor.py:183-190`）。`correlation_id="dispatch:{dispatch_id}"`、`causation_id="callback:{worker_task_id}:{body_sha256[:16]}"`（`ingestor.py:181-182`）。
- **Ledger 语义**：`claim_callback_idempotency` 返回 `fresh/duplicate/conflict`（`store.py:609-628`）。重复 → 原 receipt 原样返回（event_id、receipt_sha256、committed_revision），不新增 event/revision/outbox（`ingestor.py:378-385`；测试 `tests/test_memory_ingestor.py:107-123`）。同键不同 payload digest → `IdempotencyConflictError` + 全量 rollback（`ingestor.py:121-124,374-377`；测试 `test_memory_ingestor.py:126-141`）。
- **Control receipt**：以 `(scope_id, dispatch_id, control_revision)` 为 claim 键 + `journal_sha256` 防伪（`store.py:630-652`）；重复返回原事件，digest 不匹配 `ControlReceiptConflictError`（`ingestor.py:146-149,446-449`；伪造 journal 测试 `test_memory_ingestor.py:363-403`）。`control_idempotency_key=sha256(["control", scope_id, journal_sha256])`（`ingestor.py:65-66`，写入 Temporal event 的 idempotency_key，`ingestor.py:573`）。
- **Supervision 幂等**：`supervision_idempotency_key=sha256(["supervision", scope_id, event_id])`（`ingestor.py:69-70`），同一 event_id 重发是 no-op（`ingestor.py:915-925`；测试 `test_memory_supervision_ingest.py:85-90`）。
- **Projection bundle 幂等**：`projection_idempotency_key` 由 scope + 有序证据身份（event_id、env_step、domain/entity/field、脱敏值 digest）派生（`ingestor.py:73-95`），重复 bundle 返回 typed `duplicate`（`ingestor.py:662-668`）。
- **重启对账**：`MemoryRecovery.reconcile_control_journal` 以 journal 差集写缺失 receipt，已存在 matching receipt 不重复写（`ingestor.py:843-854`；测试 `test_memory_ingestor.py:314-360`、`406-474` callback/restart 竞态单 bundle）。崩溃注入验证 canonical bundle 全有或全无（`test_memory_ingestor.py:273-311`）。
- **nonce ledger 是第二道 replay fence**：即使绕过幂等键，同一 proof nonce 重放也会在路由 gate 被拒（`callback_auth.py:222-236`；跨重启持久性测试 `test_memory_callback_auth.py:264-278`）。

#### 去重策略（按层）

1. **路由层**：nonce 一次性（durable SQLite，`store.py:93-99`）。
2. **canonical 层**：callback/control/supervision/projection 四类幂等键（见上）。
3. **legacy 层**：`_is_step_observation_known` 以 `(scope_id, object_type, name, step)` 键去重，`dedup_scope = "{context_id}|{worker_id}"`——**按 (mission context, worker) 作用域**，同一 worker 自己的重发（spam）被抑制，但不同 worker 在相同 env step 报告的相同证据不被误杀（`server.py:831-850,881-893`）。同一 callback 内的 observation 去重发生在 `_extract_observations_with_provenance`（`server.py:109-140`，`seen_keys` 按 `object_type:name:step`）。
4. **canonical 优先配对**：legacy observation 写入严格与 canonical 写配对——仅当 `canonical_status in ("ok","duplicate")` 且绑定认证 dispatch 时 legacy 才写（`server.py:1605-1646`），避免"canonical 被 fence 而 legacy 泄漏"。

#### Backoff 与重试（callback 发送端）

- Worker 侧 `SignedPushNotificationSender._dispatch_notification`：默认最多 **6 次**尝试，指数退避 `callback_retry_delay * 2**attempt`（0.2s 起、上限 `callback_retry_max_delay=2.0s`），每次重试**重新签名（fresh nonce）、body 保持逐字节一致**——Coordinator 幂等 ledger 因此返回首次 receipt（`src/a2a/worker/callback_sender.py:84-98,130-188`；测试契约 `test_memory_ingestor.py:107-123`）。
- **只有 `unknown_worker_task` 被视为可重试的瞬态拒绝**（startup 时 worker task 绑定尚未落到 Coordinator）：仅当 HTTP 401/403 且响应 reason 恰为 `unknown_worker_task` 才退避重试；proof 失败、身份不匹配等"真拒绝"绝不重试（fail closed）（`callback_sender.py:190-206`）。
- 发送端 secret 只存在于受保护本地配置对象，不进入 prompt/A2A 消息/上下文/日志（`callback_sender.py:1-9` docstring；worker CLI 从 `.env` 的 `coordinator_secret` 或环境变量 `A2A_COORDINATOR_SECRET` 加载，`src/a2a/worker/cli.py:72-89`）。

### 回调认证（CallbackProofV1 / secret / fail-closed）

#### CallbackProofV1 签名方案

- **格式**：base64url 编码的 `worker_id.timestamp.nonce.body_sha256.sig` 五段（`callback_auth.py:48-53,92-96`）。`SEPARATOR="."`（`callback_auth.py:35`），`NONCE_BYTES=16`（`callback_auth.py:36`）。
- **canonical payload**：`worker_id.timestamp.nonce.body_sha256`，UTF-8 编码（`callback_auth.py:60-66`）；`sig = HMAC-SHA256(coordinator_secret, payload)`（`callback_auth.py:91`）。
- **与 TeamStatusProof 的区别**：V1 绑定精确序列化 HTTP body（`body_sha256` 参与签名），篡改 body 在签名校验即被拒（`callback_auth.py:49-53`；测试 `test_memory_callback_auth.py:75-81`）。`generate` 要求 secret/worker_id/body_sha256 非空（`callback_auth.py:68-96`）。
- **verify 检查序列**（`callback_auth.py:115-154`）：secret 非空 → proof 非空 → decode 五段（畸形拒绝）→ `worker_id == expected_worker_id` → `compare_digest(body_sha256)` → timestamp 为整数 → 未来时间容差 `clock_skew=10s` → 过期上限 `max_age_seconds=60s` → 重算 HMAC 比对。任何一步失败返回 `(False, reason)`，纯计算无写入。
- 头名：`X-A2A-Callback-Proof`、`X-A2A-Worker-Id`（`server.py:1367-1368`；worker 端同名单量 `callback_sender.py:35-36`）。

#### secret 配置与校验

- 常量 `MIN_CALLBACK_SECRET_BYTES = 16`（`callback_auth.py:33`；测试断言 `test_memory_callback_auth.py:123-124`）。
- **Coordinator 侧**：`Coordinator.configure_memory` 要求 ingestor+config+secret 齐备；secret 缺失/非 bytes/短于 16 字节抛 `MemoryAuthNotConfiguredError`（code=`memory_auth_not_configured`，`server.py:504-512`）；构造 `CallbackAuthenticator(secret, ingestor.store)`（`server.py:516`；`callback_auth.py:184-206`）。`memory_read_mode ∈ {shadow, read_port}` 时在 server 构造期即调用（`server.py:341-346`）。
- **Worker 侧**：`CallbackSigner.__init__` 同样校验 secret 并 fail closed（`callback_sender.py:47-57`）；`create_worker_a2a_server` 的 AgentCard `capabilities.push_notifications = callback_signer is not None`（`src/a2a/worker/a2a_server.py:185-199`）——无 signer 时 secure Coordinator 不会为无签名 push 创建回调配置（`a2a_server.py:157-160`；注册侧读取该 capability：`src/a2a/coordinator/agent_registry.py:146-149`）。
- 测试覆盖 secret fences：短/空/缺失均 `memory_auth_not_configured`（`test_memory_callback_auth.py:127-143`）。

#### 失败行为（fail closed）

- 认证失败 → `_reject(reason)` 返回 HTTP 401（`server.py:1371-1382`），且**零领域写者调用**（EventStore/SemanticMapStore/TaskWatchdog/MemoryIngestor 均不触达）；仅 `record_security_audit("callback_auth_rejected", reason, digest_prefix=body_sha256[:16])`——只留 reason + body digest 前缀，不保存原始 body（`callback_auth.py:238-259,261-268`；`store.py:519-533`；测试 `test_memory_callback_auth.py:251-261` 断言 secret 不出现在 audit 中）。
- 测试以 writer-spy 断言"缺 proof / 篡改 body / 过期 / worker 不匹配 / nonce 重放"五种失败均零写者调用（`test_memory_callback_auth.py:31-59,191-248`）。
- 合法认证也**不豁免脱敏**：`sanitize_callback` 在 fan-out 前执行（`server.py:1408-1411`），且 SQLite、EventStore NDJSON、SemanticMapStore JSONL 三处均无原始 secret（`test_memory_ingestor.py:482-571`）。

### 生产者矩阵（谁写什么）

| 生产者 | 触发 | canonical 写入（MemoryIngestor） | legacy 写入（配对规则） | 认证 |
|---|---|---|---|---|
| Worker（`SignedPushNotificationSender`，`callback_sender.py:71-206`） | `POST /a2a/push-callback`（server.py:1315） | `callback.status_update` / `callback.artifact_update` Temporal event（ingestor.py:512-541；server.py:1473-1483、1617-1628） | EventStore `status_update`/`artifact_update` + watchdog（server.py:1491-1506、1562-1574）；SemanticMap 仅在 observation 路径（server.py:902-903） | CallbackProofV1 + nonce（shadow/read_port） |
| Worker observation（同一 callback body 内提取） | `_extract_observations_with_provenance`（server.py:98-142） | `evidence.projection` 事件 + Spatial/Embodied 投影，与 callback 同事务（server.py:1576-1603；ingestor.py:393-413,720-801） | `observation_report` EventStore + SemanticMapStore（server.py:895-904），canonical 成功才配对写（server.py:1630-1646） | 同上 |
| 控制面（MissionRuntime dispatch/cancel/watchdog 内部转移） | journal receipt → `MemoryLifecycleBridge.enqueue/drain`（ingestor.py:820-838） | `control.{source}.{state}` 事件 + `control_receipt` 行（ingestor.py:543-574,476-508） | 无（EventStore 由 server 其他路径写） | 受信内部来源（无 HMAC） |
| 控制面（callback-origin 转移） | `callback_bundle()` 捕获 receipt（ingestor.py:310-324；server.py:1463,1512） | 同上，与 callback 事件同一 bundle，绝不 double bridge-enqueue（测试 test_memory_ingestor.py:199-240） | 无 | 由外层 callback 认证背书 |
| 重启对账（`MemoryRecovery` / `bridge.reconcile`） | `reconcile_control_journal`（ingestor.py:843-854；tests:314-360） | 同上（差集补齐，重复 no-op） | 无 | 受信内部来源 |
| TaskWatchdog（5 种监督事件，task_watchdog.py:305-337 等） | `SupervisionEventAdapter` sink（ingestor.py:880-965；server.py:522-526） | `supervision.{kind}` 事件（幂等键 per event_id） | 无（watchdog 自身另有 debug artifact） | 受信内部来源；scope 必须来自 trusted `dispatch.context_id` + 显式 `runtime_epoch`，缺 epoch fail closed（ingestor.py:887-893；test_memory_supervision_ingest.py:188-200） |
| Coordinator run-terminal 物化 | `materialize_compatibility_artifacts`（server.py:582-642） | 只读导出（`MemoryExporter`，server.py:600-609），不新增摄入 | 重建 semantic_map.jsonl + manifest，标记 legacy artifact | — |

**明确禁止**：Worker 直接共享 Coordinator 内存对象或直写同一数据库（设计 §7.1；代码上 Worker 唯一路径是 HTTP push）；`MemoryIngestor` 无 `apply_physical_status`/`transition_dispatch` API，绝不回写控制面（`ingestor.py:1-8`；测试 `test_memory_ingestor.py:574-578`）。server.py 中各 legacy 写点均带 `memory-producer:` 注释声明 canonical source / idempotency / auth 模式（如 server.py:895、1490、1561、1673、1707、1741）。

## 7. 投影与读路径
### 投影模型：Spatial / Embodied 两类域投影

Memory 系统没有独立的"团队状态/能力标签"投影对象；投影是**字段级（field-level）**的两类域投影：`spatial`（场景物体）与 `embodied`（agent/节点），持久化在 `projection_field` 表中，主键为 `(scope_id, domain, entity_id, field_name)`（`src/a2a/coordinator/memory/store.py:169-188`）。

- 投影的写入方是 `MemoryProjectionReducer`（`src/a2a/coordinator/memory/projections.py:85-90`）——"Deterministic field-level reducer over a canonical store transaction"，**所有方法必须在 `canonical_transaction()` 打开期间调用，自身不提交**。
- 每个 field claim 独立裁决（per-field policy，绝不做 whole-event 裁决），裁决结果落 `ProjectionFieldResult`（`projections.py:51-61`）。
- 实体/视图修订号由 store 维护：`bump_entity_revision`（`store.py:955`）、`bump_view_revision`（`store.py:982`）、`entity_revision_of`（`store.py:1073`）、`view_revision_of`（`store.py:1103`）；另有 `projection_outcome` 审计表（`store.py:191-202`）与 `memory_view_revision` 快照修订表（`store.py:204-207`）。
- 域投影通过关系表可回溯：`_relation_type` 规定 spatial 用 `about`、embodied 用 `observed_by`（`projections.py:138-139`）；`_add_relation` 建立的 relation 的 `from_ref` 指向 **canonical Temporal 事件 UUID**、`to_ref` 指向 `memory:{domain}:{entity_id}`，保证 Temporal → 投影 join 稳定（`projections.py:141-163`）。
- "能力标签"不是单独投影，而是投影字段族：`capability` / `sensor_type` 在字段源策略中**只允许 `registry` 来源**（`contracts.py:176-177`）；`availability` / `heartbeat` 允许 `control` / `supervision` / telemetry / observation（`contracts.py:169-175`）。

> **⚠️ 已知缺口（待接入，2026-08-11 记录）——Embodied 域当前生产零数据**
>
> 1. **问题**：embodied 投影在生产 run 中从未产生数据。观测路径只把 `obj_type == "agent"` 的观测映射为 embodied 域（`server.py:746`），但实际 run 中 worker 观测对象均为环境物体（fire/person/…），agent 自身状态**不以观测形式上报** → `embodied_node` 表与 `projection_field(domain='embodied')` 恒为空。实测 `logs/h2_shadow_audit_run2`：38 行投影全为 spatial。
> 2. **后续接入方向**：agent 的 position / inventory / battery 等具身状态**直接随 worker 心跳/回调返回**（`worker_registry.update_heartbeat` 目前只更新时间戳，`worker_registry.py:105-109`；A2A push 回调携带状态文本）——后续应从回调/心跳状态中提取，以 `worker_telemetry` 来源摄入 embodied 域。
> 3. **capability 来源**：能力标签实际对应 **A2A AgentCard**——worker 注册时声明的 `skills`/`capabilities`（`src/a2a/worker/a2a_server.py:185-199` AgentCard 构造；`AgentCapabilities` 目前只含 `streaming`/`push_notifications`，能力清单在 `AgentCard.skills`）。当前 `FIELD_SOURCE_POLICY` 仅标注 `registry` 来源且**无实际生产者**——后续应接入 AgentCard 作为 `capability`/`sensor_type` 的摄入源。
>    - **AgentCard 留有动态添加接口**：worker 侧 `create_worker_a2a_server(capabilities=[...])` 参数会为每个 cap 动态生成一个 `AgentSkill`（`a2a_server.py:164-176`，`skills.append(...)` 可继续追加）；coordinator 侧 `agent_registry.py:135-146` 注册时从 AgentCard 提取 skill 为 `capabilities` 列表（过滤 `metadata`/`backend`/`model` 标签）——这条"worker 声明 → AgentCard → registry 提取"链路已存在，后续接入 embodied 投影时可直接对接 registry 的提取结果作为 `capability` 字段的权威来源。

> **📌 规划方向（待设计/待添加，2026-08-11 记录）——反思机制与长期记忆（第四类记忆）**
>
> 当前 Memory 只有三类：Temporal（事件流水线）/ Spatial（场景物体投影）/ Embodied（agent 节点投影）。规划新增：
>
> 1. **反思机制**：基于**近期返回的内容**（worker callback / 观测证据 / supervision 事件等）+ **Coordinator 的决策**（`control.*` 生命周期事件），进行反思（reflection），**生成长期记忆**。
> 2. **长期记忆（Long-term Memory）**：作为独立于 temporal/spatial/embodied 的第四类记忆（可理解为对短期事实与决策模式的**跨 run / 跨 scope 聚合沉淀**，而非逐事件的流水记录）。
>
> 待设计点（仅记录方向，未实施）：
> - 反思的触发时机与输入窗口（近期内容的界定、coordinator 决策的选取范围）；
> - 长期记忆的存储形态（独立 domain / 派生表 / 聚合产物）与读取侧接入（read_port 读路径 → Context 注入）；
> - 与在线真相隔离边界的兼容（反思输入只能来自在线来源，长期记忆生成不得引入真值）。

### 比较裁决顺序（H1）

`reduce()`（`projections.py:95-134`）实现模块 docstring 声明的 H1 逐字段比较序（`projections.py:5-13`）：

1. scope/epoch 栅栏——由 ingestor 在进入 reducer 前强制（`ingestor.py:603-614`，`ingestor.py:637-649`）；
2. `env_step` 栅栏——已有物理字段时，缺失 env_step 或更旧的 env_step 永远不能覆盖当前字段（`projections.py:107-110`），缺失 env_step 的证据只进 Temporal 审计（`test_memory_projections.py:222-244`）；
3. 同 step 时按**字段源优先级**（per-field policy，`contracts.py:158-184`，`field_source_priority` 在 `contracts.py:187-199`，未知来源排最低位）；
4. 优先级相同时比 `confidence`；
5. 全相等时比**值相等**——值相同为 `no_change`，值不同为显式 `conflicted`（sequence 永不参与选胜，`projections.py:129-134`）。

五种裁决结果由 `ProjectionOutcome` 枚举定义（`projections.py:43-48`）：`material` / `ignored_out_of_order` / `superseded_by_higher_authority` / `conflicted` / `no_change`。**只有 material 和 conflicted 改变可见投影**，从而推进实体/视图修订与 `as_of_sequence`（`projections.py:14-17`）：

- `_materialize`（`projections.py:201-245`）：bump 实体修订 + bump 视图修订 + `upsert_projection_field`（携带 value/env_step/provenance/source_priority/confidence/event_id/evidence_id/sequence/outcome）+ 写 outcome 审计 + 建 relation；
- `_conflict`（`projections.py:279-353`）：**当前持有者保持不变**（其 event/provenance/confidence/sequence 不被重贴标签），双方 canonical event id 都进 `conflict_event_ids`/`conflict_candidates`（带 provenance，不选胜者），同时推进实体与视图修订；
- `_ignored`（`projections.py:247-256`）与 `_superseded`（`projections.py:258-277`）：只写 outcome 审计 + 对应类型的 relation（`ignored_out_of_order` / `superseded_by_higher_authority`），**不推进任何修订**；
- `_no_change`（`projections.py:355-363`）：仅审计。

值相等判定用规范化 JSON 序列化（`_value_json` / `_values_equal`，`projections.py:77-82`），保证确定性。修订语义由 `test_canonical_entity_view_revisions_are_separate` 钉死：迟到的旧 step 证据推进 canonical revision 但不推进 entity/view 修订（`test_memory_projections.py:554-578`）。

### 触发时机与写入路径

投影由 `MemoryIngestor` 在**同一条 canonical 事务**内触发，Temporal-first、reducer-second：

- 独立入口 `ingest_projection()`（`ingestor.py:578-701`）：先做 bundle 栅栏（mixed_scope_bundle / mixed_epoch_bundle，`ingestor.py:603-614`）与 H1-INV-1 真值门（见下章），然后在 `canonical_transaction()`（`ingestor.py:637`）内按 `event_id` 分组，每组先 `_append_projection_event` 写一条 `event_type="evidence.projection"` 的 Temporal 事件（`ingestor.py:720-754`，`ingestor.py:756-801`），再对组内每个 claim 调 `reducer.reduce()`（`ingestor.py:747-753`），最后 bump revision + 写 idempotency ledger + 写 outbox（`ingestor.py:676-695`）。
- 回调路径 `ingest_callback()`（`ingestor.py:335-438`）：当携带 `projection_inputs` 时，在同一事务里先写回调 Temporal 事件、control receipts，再 `_reduce_evidence_in_tx`（`ingestor.py:393-399`），全部 all-or-nothing。
- 幂等：`projection_idempotency_key` 由 scope + 排序后的证据身份（event_id、env_step、domain、entity_id、entity_type、field_name、值摘要）确定性派生（`ingestor.py:73-95`）；重复 bundle 返回 typed `duplicate`，零新增 Temporal/revision/outbox（`ingestor.py:651-668`）。
- 生产链路：`server.py` 的 `_normalize_observation_projection_inputs` 把 worker observation 映射为 `NormalizedProjectionInputV1` 字段 claim（position、最多 8 个 attributes、agent inventory 经 `normalize_inventory` 归一化；`server.py:700-807`），再由 `_ingest_callback_to_memory` 传入 `ingest_callback`（`server.py:655-698`）。观察对象缺 `name` 直接跳过（`server.py:734-736`），命中禁止真值词表的观察直接丢弃（`server.py:737-740`）。

### 谁消费投影（读路径）

读路径（`read_port` 模式）**读的就是投影**，由 `MemoryReadPort` 直接读 canonical store 的 `projection_field` / `temporal_event` 表：

- `MemoryReadPort`（`sar_orch/environment_state_provider.py:68-147`）：`spatial_snapshot()` / `embodied_snapshot()` 把 `store.projection_fields()` 按 entity 分组为 `{entity_type, fields:{field_name: row}}`（`environment_state_provider.py:89-117`）；`temporal_events(after_sequence)` 增量读 Temporal（`environment_state_provider.py:119-124`）；`conflicts()` 只挑 `outcome == "conflicted"` 的行（`environment_state_provider.py:126-139`）；`evidence_refs()` 汇总 `evidence_id`（`environment_state_provider.py:141-147`）。
- `EnvironmentStateProvider` 组合 `MemoryReadPort` + `ControlPlaneReadPort`（control-plane 任务视图，Temporal 事件永不改写它；`environment_state_provider.py:150-192`、`194-260`），viewer ACL 在 provider 内做（`environment_state_provider.py:225-239`）。Coordinator 侧的 `SARCoordinatorStateProvider` 同样组合 `MemoryReadPort(ingestor.store, scope_id)`（`sar_orch/coordinator_state_provider.py:179-185`）。
- 视图 DTO：`EnvironmentStateView` 是"可重建的读，不是 truth"（`src/Agent/environment_state.py:91-98`），带 `Freshness`（FRESH/STALE/UNAVAILABLE，`environment_state.py:15-25`）；纯渲染 `render_environment_state_view`（`environment_state.py:180+`）。
- agent Context 在 `memory_read_mode == "read_port"`（默认值，`src/Agent/worker_agent/context.py:130`）下渲染 `_render_read_port_block`（`worker_agent/context.py:985-1028`）；provider 出错或视图 STALE/UNAVAILABLE 时触发一次性的 read_port→legacy 回滚闩锁（`worker_agent/context.py:1034-1046`）。Router 侧同构（`src/Agent/router_agent/context.py:128`、`926-986`）。
- 遗留工具 `query_shared_memory`（HTTP 拉语义地图）已标记 DEPRECATED，引导使用 Map Agent MCP 工具（`sar_orch/tools/worker/query_shared_memory.py:1-5`）。

## 8. 导出与兼容性
### 导出内容与格式

`MemoryExporter`（`src/a2a/coordinator/memory/exporter.py:262`）是**确定性的兼容物化器**：canonical SQLite 是唯一事实源，导出从已提交的 canonical 记录重建 JSONL 兼容产物（`exporter.py:1-16`）。产物清单 `CANONICAL_ARTIFACTS`（`exporter.py:47-55`）：

| 文件名 | 内容 | 构建函数 |
|---|---|---|
| `temporal.jsonl` | 全部 Temporal 事件（success 0/1→bool、payload 反序列化） | `_temporal_records`（`exporter.py:290-298`） |
| `spatial.jsonl` / `embodied.jsonl` | 对应域的 `projection_field` 行原样 | `_projection_records`（`exporter.py:300-305`） |
| `revision.jsonl` | `scope` / `view_revision` / `entity_revision` 三类记录 | `_revision_records`（`exporter.py:307-342`） |
| `outbox.jsonl` | outbox 行，按 `(created_at, outbox_id)` 排序 | `_outbox_records`（`exporter.py:344-347`） |
| `relations.jsonl` | 关系记录（from/to namespace+id、type、valid_from/to、source_event_id、confidence） | `_relation_records`（`exporter.py:349-367`） |
| `semantic_map.jsonl` | **legacy 兼容产物**，`{ts, event_type, observation, object}` | `_semantic_map_records`（`exporter.py:369-497`） |

- JSONL 渲染 `render_jsonl_bytes`：每行一条记录、`ensure_ascii=False`、紧凑分隔符（`exporter.py:140-146`）。
- `build_artifacts` 是 store 的纯函数："identical input always yields identical records"（`exporter.py:274-288`）。
- `semantic_map.jsonl` 的兼容契约（`exporter.py:369-426`）：行序跟随 canonical sequence；`object` 是**该事件时刻的 Spatial 投影快照**——把已提交的 spatial evidence 事件按与 canonical reducer 相同的 H1 比较序（env_step → 字段源优先级 → confidence → 值相等 → 冲突）重放得到（`_apply_claim`，`exporter.py:220-259`）；`observation` 固定携带 reporter/step/object_type/name/position/attributes/confidence/source_task_id/note（`exporter.py:457-467`）；`object` 含 object_type/name/position/attributes/status/last_seen_step/last_seen_ts/sources/confidence/conflict/conflicts/field_last_seen_steps（`exporter.py:468-491`）。

### 原子性与清单契约

- 每次导出按 `(scope_id, artifact_kind, canonical_revision)` 做**全量重建**：内存构建 → 临时文件 + `fsync`（`_write_temp`，`exporter.py:149-166`）→ 写 manifest 摘要 → `os.replace` 原子替换（`_atomic_replace`，`exporter.py:182-187`）→ `_fsync_dir`（`exporter.py:169-179`、`560`）。**绝不 append、绝不做 retention purge、绝不动其他写入方的产物**（mission_graph.jsonl、supervision_*.ndjson、map_summary.jsonl、events.ndjson/CSV、snapshot_*.json；`exporter.py:10-15`）。
- manifest `export_manifest.json`（`EXPORT_MANIFEST_FILENAME`，`exporter.py:34`；schema v1，`exporter.py:35`）每个产物条目含 `schema_version/scope_id/artifact_kind/canonical_revision/record_count/payload_sha256/artifact_sha256`（`ExportArtifactEntry.to_dict`，`exporter.py:87-113`；`_build_manifest`，`exporter.py:501-516`）。未知 scope 抛 `MemoryExportError`（`exporter.py:77-84`、`529-530`）。
- 契约测试：字节级确定性（同输入同 bytes，`tests/test_memory_compat_export.py:159-177`）、无残留 `.tmp` 文件（`180-187`）、原子覆盖旧文件（`190-199`）、unknown_scope 抛错（`202-204`）、manifest 条目与磁盘字节 SHA-256 一致（`212-244`）、各产物内容形状（`258-296`）。

### 触发时机

导出**不在 scope close 时触发**，而是在 run 终结（run-terminal）与崩溃恢复（kill/restart）两个时机：

- **run-terminal 物化**：`server.materialize_compatibility_artifacts()`（`src/a2a/coordinator/server.py:582-642`）→ `_resolve_export_scope_id()`（活动 runtime 的 scope，否则按修订号取最近 scope；`server.py:551-580`）→ `MemoryExporter.export_scope`（`server.py:608-609`），并把遗留调试产物 `events_<task>.ndjson`（EventStore）与 `supervision_<dispatch>.ndjson`（SupervisionStateStore）标记为 `legacy_unmigrated`（`server.py:611-623`）。该函数由 `sar_orch/experiment.py:203` 的 `_invoke_run_terminal_memory_eval` 调用，**仅在 `shadow`/`read_port` 模式下执行**（`experiment.py:196-201`）。
- **outbox 重放**：`MemoryRecovery.replay_outbox`（`src/a2a/coordinator/memory/recovery.py:223-264`）在 kill/restart 后先把 pending outbox 行标 `exported`，再无条件（幂等、at-least-once）重建全部产物（`recovery.py:226-234`、`254`），顺带修复被篡改/缺失的产物文件；`verify_committed_canonical` 用确定性重建 + SHA-256 比对校验磁盘产物一致性，只报告不修复（`recovery.py:173-219`、`83-93`）。历史遗留产物**永不回填** canonical Memory，只读保留并标记（`recovery.py:18-20`、`279-294`；`exporter.mark_legacy_unmigrated`，`exporter.py:570-609`）。

### 与 legacy artifact 的兼容

- `mark_legacy_unmigrated`（`exporter.py:570-609`）：scope/身份无法明确解析的历史 JSONL/NDJSON **绝不回填**，文件原样留在磁盘，manifest 的 `legacy_artifacts` 下每个文件记 `legacy_unmigrated: true` + reason（`exporter.py:577-583`）。测试钉死文件内容不被改动、不出现在 `artifacts` 中（`test_memory_compat_export.py:388-408`）。
- `semantic_map.jsonl` 必须被真实消费者 `render_sar_report` 的 `load_semantic_map` 读回：测试断言对象名/类型/3 条 observation 均兼容（`test_memory_compat_export.py:373-385`），且顶层字段序与 `observation` 字段序被冻结（`304-370`）。
- 导出方不参与 canonical 事务、不声明物理 append 的 exactly-once（`exporter.py:10-15`）；确定性由"相同 canonical 集 → 相同字节"保证（`test_memory_compat_export.py:159-177`，manifest 确定性依赖固定 `exported_at`，`exporter.py:551`）。

## 9. 脱敏
### 脱敏引擎与规则

`RedactionPolicy`（`src/a2a/coordinator/memory/redaction.py:20-49`）包装后端无关的 `SensitiveTextRedactor`（`src/Agent/redaction.py:94-178`）。**每个认证回调 payload 在扇出给所有写入方之前必须脱敏**（MemoryIngestor、legacy EventStore、SemanticMapStore JSONL、TaskWatchdog、异常/安全审计）；**有效签名绝不豁免脱敏**（`redaction.py:2-9`）。

- 敏感字典键 `_SENSITIVE_KEY_KINDS`（`Agent/redaction.py:30-60`）：`authorization`/`cookie`/`credentials`/`password`/`secret`/`client_secret`/`api_key`/`access_token`/`refresh_token`/`bearer_token`/`hmac`/`signature`/`proof`/`callback_proof`/`signed_envelope`/`mail_body`/`mail_body_text`/`mailbox_body` 等——键名保留、**值整体替换**为标记，保住结构字段。
- 正则模式 `_PATTERNS`（`Agent/redaction.py:65-91`）：`X-A2A-Callback-Proof`、`Authorization: Bearer/Basic`、`Cookie`、`api_key`、`password|secret|credential|access_token|refresh_token|private_key`、64 位 hex 的 hmac（`\b[a-f0-9]{64}\b`）。
- 精确 secret：构造时传入的 `secrets` 字节串全文替换（`Agent/redaction.py:108-112`、`132-134`）。
- 替换标记格式 `[REDACTED:<kind>:<sha256-prefix>]`（`redacted_marker`，`Agent/redaction.py:23-25`）。
- `redact_data` 深拷贝递归脱敏（dict/list/str，`Agent/redaction.py:137-152`）；`redact_tool_result` 对 `content`/`error`/`data` 脱敏且**绝不原地修改原对象**（`Agent/redaction.py:154-178`）。

### 应用时机

- **ingestor 侧**：`_reduce_evidence_in_tx` 在 reducer 之前对每个 claim 的 `value` 做 `redactor.redact_data`（`ingestor.py:747-753`）；`_append_projection_event` 对进 Temporal payload 的 value 同样脱敏（`ingestor.py:767`）。因此投影字段、relation 审计、projection outcome 审计、Temporal payload 全部无原始 secret（`tests/test_memory_redaction.py:297-368`）。
- **server 回调侧**：回调路由对解析后的 body 调用 `self._memory_redactor.sanitize_callback(body)`（`server.py:1410`，redactor 取自 `ingestor.redaction`，`server.py:515`）；`RedactionPolicy.sanitize_callback` 返回脱敏深拷贝，body digest/事件身份/非敏感结构保留（`redaction.py:37-45`）；`sanitize_event` 是面向单文本字段的防御性边界（`redaction.py:47-49`）。
- **worker 侧**：`A2AWorkerSink` 在 enqueue 前对 `[DATA]` 内容脱敏（`test_memory_redaction.py:264-291`）；router/worker agent 的失败 `ToolResult` 在进入对话历史/日志前脱敏（`test_memory_redaction.py:147-261`）。
- 契约测试：精确 secret（`test_memory_redaction.py:24-28`）、hmac hex（`31-35`）、Authorization/Cookie 头名保留值替换（`38-44`）、api_key（`47-50`）、proof 头（`53-58`）、递归结构保持（`61-84`）、ToolResult（`87-112`）、policy 深拷贝（`115-137`）。

## 10. 在线真相隔离（truth boundary）
### 准入白名单与禁止词表

在线 Memory/Context 只消费 worker report / tool / telemetry 等**在线来源**，Barrier/仿真真值在入口被拒绝：

- 白名单 `ONLINE_PROVENANCE_ALLOWLIST`（`src/a2a/coordinator/memory/contracts.py:54-64`）：`worker_sensor_tool`、`worker_telemetry`、`worker_observation`、`peer_report`、`registry`、`control`、`supervision`——集合外（barrier、oracle、ground_truth、checker、simulator、direct world snapshot）一律拒绝（`contracts.py:50-53`）。
- 禁止词表 `FORBIDDEN_TRUTH_TERMS`（`contracts.py:72-91`）：`oracle`、`ground_truth`、`ground-truth`、`ground truth`、`checker`、`simulator`、`sim_truth`、`world_snapshot`、`world snapshot`、`direct_world`、`direct world`、`truth_trace`、`coverage_truth`、`env.controller`、`get_env_snapshot`、`object_priors`。
- `scan_forbidden_truth_fields` 递归扫描 dict 键与字符串值（`contracts.py:94-115`），`truth_scan_denied` 返回通用标记（绝不回显原始词/值，`ingestor.py:108-118`）。

### 拒绝语义（zero domain writes）

`ingest_projection` 两道门（`ingestor.py:616-629`）：门 #1 禁止非白名单 provenance；门 #2 禁止"白名单 provenance 但 value 中掩藏直接世界/oracle 字段"（防绕过，测试见 `tests/test_memory_online_truth_boundary.py:185-219`）。任一成员违规 → 整个 bundle 以 typed `online_truth_forbidden` 拒绝（`contracts.py:66`；`ProjectionIngestResult.status` 枚举含该值，`projections.py:68-70`），**零域写入**：Temporal 0 条、revision 0、投影空、relation 空、outbox 空、无 Context 内容（`test_memory_online_truth_boundary.py:101-115`、`145-160`、`254-272`）。回调路径同样在进事务前拒绝（`ingestor.py:351-364`）。

唯一保留的诊断是**脱敏后的安全审计**：`_record_truth_denial` 只写 `online_truth_forbidden` 标记 + scope/event 摘要前缀（`ingestor.py:703-718`；`store.record_security_audit`，`store.py:519`；`security_audit_entries`，`store.py:535`），原始候选值、禁止词、secret 均不落盘（`test_memory_online_truth_boundary.py:118-142`、`222-251`、`315-329`）。投影链路对合法证据里的 secret 同样全链路脱敏（`test_memory_online_truth_boundary.py:280-312`）。

### 源码级边界

测试用 `inspect.getsource` 钉死源码约束：`ingestor.py` 与 `projections.py` 的源码不得出现 `get_env_snapshot`、`QuerySARStateTool`、`SARBarrier`、`from sar_orch.barrier`（`test_memory_online_truth_boundary.py:354-372`）。语义模式（semantic）的 Coordinator 状态提供器在 `state_mode="semantic"` 下**绝不调用 `barrier.get_env_snapshot()`**（SpyBarrier 断言 `calls == []`），payload 无 `global_snapshot`，worker 状态来自 worker-only 语义地图（观察到的 `last_position=[1,2,0]`，而非 barrier 真值 `[99,99,0]`；`test_memory_online_truth_boundary.py:375-443`）。

### 真值的 post-run 使用（evaluator 专用，单向）

仿真真值只存在于 post-run evaluator 侧，且**不回写同一 run 的 Memory**：

- **运行期录真**：`TruthRecorder`（`sar_orch/eval/truth_recorder.py:134+`）运行期从 `SARBarrier` **只读** `get_env_snapshot()` 取真值（`truth_recorder.py:4-5`），按 canonical 投影的命名约定（domain/entity_id/field/value 形状，`truth_recorder.py:10-20`）只追加写入 evaluator 私有的 `<truth-output-dir>/truth_trace.jsonl`（默认在 results 目录之外），run 终结时冻结 `truth_manifest.json`（`truth_recorder.py:22-26`、`33-37`、`52-57`）。**硬边界：绝不写 canonical Memory、Context、语义地图或任何运行期 agent/worker 可读的产物**（`truth_recorder.py:22-26`）；legacy 模式下无法解析 canonical scope 时 `finalize` 返回 None、什么都不写（`truth_recorder.py:36-37`）。
- **终结后评测（只读）**：`memory_projection_quality.py` 仅在 run 达到终态、canonical 快照冻结后运行（`sar_orch/eval/memory_projection_quality.py:1-33`）：canonical DB 以 `mode=ro` 打开（`220-229`），读 frozen Temporal evidence（`244-268`）与投影字段（`271-294`）与 truth trace（`156-200`）做 post-hoc 比对，只写 `<results_dir>/memory_projection_quality.json`。硬边界明确："Memory access is strictly read-only……oracle truth is used exclusively for post-hoc comparison, never as online correction: nothing is written back to Memory"（`9-17`）。
- **验收评测**：`memory_acceptance.py` 读 `run_metrics.json`、交互 CSV、canonical DB（`mode=ro`，`227-235`、`238-261`）与 export manifest（`264-300`），写 `memory_acceptance.json`；同样只读。
- **编排接线**：`experiment.py` 的 run-terminal 流程仅在 `shadow`/`read_port` 下物化导出（`experiment.py:196-201`），随后跑 acceptance 与 projection-quality 评测（`experiment.py:210-239`）；acceptance gate 只记录日志、不 crash run（`experiment.py:191-193`、`230-237`）。`eval/__init__.py:3-6` 明确两者定位：acceptance 是 run 验收产物，projection-quality 是 terminal-only 只读真值比对。

综上，真相隔离在代码中是**单向 + 双闸**：在线侧（白名单 provenance + 禁止词值扫描，`ingestor.py:616-629`）把 Barrier/真值挡在 Memory 之外；真值侧（truth_recorder → truth_trace/truth_manifest）只进 evaluator 私有目录，评测器只读 canonical DB（`mode=ro`）且只写评测产物，两条路径无任何回写交点。

## 11. 上下文集成
#### 1.1 分层结构总览

上下文组装（ContextManager）与运行时状态注入（StateProvider）是解耦的两层：

- **ContextManager**（`src/Agent/worker_agent/context.py` 与 `src/Agent/router_agent/context.py`，二者共享同一基类实现）负责在每次 LLM 调用前把「稳定的 system prompt + 历史消息 + 尾部 Environment State 块（role=user）」组装成最终消息列表。基类 `ContextManager` 的 docstring 明确其记忆模型为 "pinned + episodic + recent-window memory"（worker_agent/context.py:142-143）。
- **StateProvider**（`AsyncStatePreparer` 接口）负责把 SAR 后端（barrier / 语义地图 / event store / canonical Memory / control plane）投影成 `RuntimeState` DTO；`ContextManager` 不直接 import 任何 SAR 后端（worker_agent/context.py:182-184）。

worker 侧实现：`WorkerContextManager`（worker_agent/context.py:1171）；coordinator 侧实现：`CoordinatorContextManager`（router_agent/context.py:1101）。二者都继承基类 `ContextManager`，各自持有 typed pinned state（`WorkerPinnedState` worker_agent/context.py:1157-1168；`CoordinatorPinnedState` router_agent/context.py:1074-1098）。

#### 1.2 ContextConfig：策略与 memory_read_mode

`ContextConfig` 字段（worker_agent/context.py:114-130，router 端同构，router_agent/context.py:128）：

```python
strategy: str = "hybrid"   # "none" | "summary" | "hybrid" | "raw"（context.py:118）
recent_messages: int = 12
summary_trigger_ratio: float = 0.8   # 80% token_limit 触发 Phase 3 LLM 压缩（120-122）
pinned_enabled: bool = True
episodic_max_items: int = 20
state_mode: str = "semantic"
memory_read_mode: str = "read_port"  # H3 retirement 后默认 read_port（127-130）
```

实际运行接线：coordinator 侧 `ContextConfig(strategy="hybrid", recent_messages=12, pinned_enabled=True, state_mode=self._state_mode, memory_read_mode=self._memory_read_mode)`（sar_orch/coordinator.py:429-435）；worker 侧 `ContextConfig(strategy="hybrid", recent_messages=12, pinned_enabled=True, state_mode="semantic", memory_read_mode=self._memory_read_mode)`（sar_orch/worker.py:481-487）。`token_limit=80000`（coordinator.py:436、worker.py:488）。

**strategy 语义**（worker_agent/context.py 实际行为）：
- `raw`：`observe()` 立即返回（818-819）、`prune_history()` 跳过（861-862）、`assemble()` 透传 system + raw messages、不注入 Environment State 块（960-967）。
- `none`：`prune_history()` 跳过，但 `observe()` 正常执行（pinned + episodic 仍更新），`assemble()` 仍注入 memory block。
- `hybrid`/`summary`：完整 prune + 注入。

#### 1.3 注入时序：hooks.pre_llm → refresh_runtime_state → assemble

worker 侧 hook（src/Agent/worker_agent/hooks.py:69-84）是每轮注入的入口：

```python
async def pre_llm(self, agent, messages):
    if read_mode == "read_port" and fetch_env is not None:
        await fetch_env()          # 异步拉取 /environment-state（canonical 读路径）
    elif fetch_team is not None:
        await fetch_team()         # 否则拉团队状态（legacy 侧）
    self._ctx.refresh_runtime_state()   # 同步 snapshot + 投影到 pinned
    self._ctx.prune_history(agent.messages)
    return self._ctx.assemble(agent.system_prompt, agent.messages)
```

- `refresh_runtime_state`（worker_agent/context.py:414-428）：调 `state_provider.snapshot(context_id)`，缓存到 `_runtime_state`，再 `_project_runtime_state_to_pinned` 把 `position / inventory / step / known_fires / known_persons / mission_status / current_task` 投影进 typed pinned state（430-445）。
- `assemble`（946-981）：`system（稳定前缀）→ 历史消息[1:] → memory block（role=user 追加在最后）`；若最近一条 assistant 消息仍有未闭合 tool call 则拒绝追加本轮 state block（978-979，由 controller/NeedInput 恢复路径闭合协议）。Design 里注明把 state block 放尾部是为 DeepSeek auto-prefix 缓存保持稳定前缀（952-954）。
- `observe()`（816-841）：每条工具结果 → `_extract_pinned` 更新 pinned + 追加 `_Episode(tool_name, summary)` 到 episodic，`_prune_episodic` 裁剪到 `episodic_max_items=20`（843-847）。worker 端 `_extract_pinned` 用正则/JSON 解析工具结果提取 position/inventory 等（1318 起）。

#### 1.4 coordinator 侧：SARCoordinatorStateProvider 注入 RuntimeState

`SARCoordinatorStateProvider`（sar_orch/coordinator_state_provider.py:22-33）实现 `AsyncStatePreparer`，把 barrier / semantic map / event store / task store / supervision store 桥接成 `RuntimeState`：

- **异步预准备 `prepare_for_llm`**（388-465，semantic 模式）：原子读 `(revision, snapshot)`（`_try_snapshot_with_revision`，375-386）；首次调用建立基线；revision 变化时用 `MapDiffCalculator.diff` 计算 `map_delta`（434-441），并可能调用 `map_summarizer.maybe_summarize` 一次（LLM 摘要，452-465，失败保留上一次摘要）。
- **同步 `snapshot`**（467-591）：组装 `RuntimeState(version, env_step, observed_at, payload)`；payload 含 `state_mode / mission_finished / step_budget / semantic_summary / team_status_summary / map_revision / map_delta / map_summary / mission_dag_view / physical_dispatches_view / task_status_view / recent_changes / supervision`（511-558）。语义地图快照在同 env step 内缓存（493-509）；任何异常回退到上次快照并置 `stale=True`（577-591）。
- `CoordinatorPinnedState` 对应字段（router_agent/context.py:1074-1098），含 Phase 6 的 `map_revision / map_delta / map_summary / map_summary_revision` 与 Phase 2 的 `mission_dag_view / physical_dispatches_view`。

#### 1.5 read_port 读路径（canonical Memory → LLM 视图）

`_render_memory_block`（worker_agent/context.py:1114-1151；router 端 1031-1068 同构）：

```python
if self.config.memory_read_mode == "read_port":
    provider = self._state_provider
    rollout_active = getattr(provider, "rollout_active", None)
    if rollout_active is None or rollout_active():      # 未 latch 回滚
        read_port_text = self._render_read_port_block()
        if read_port_text:
            return read_port_text
# 否则走 legacy pinned 渲染（Environment / Current State / Task Plan 三节）
```

`_render_read_port_block`（worker_agent/context.py:985-1044）：

1. 从 provider 取 `scope_id / viewer_id / viewer_role / current_dispatch_id`（1011-1014）；
2. 用 `(scope_id, viewer_id)` 命名空间的 temporal cursor（`get_cursor`，1016；`_cursor_sequences` 单调推进，1041-1043）；
3. 构造 `EnvironmentStateQuery(temporal_cursor=cursor, token_budget=token_limit-1024)`（1017-1024，预算见 `_read_port_token_budget` 1053-1055，注释引用 design §6）；
4. 调 `provider.query_environment_state(query)`：异常 → `_trigger_read_port_rollback` 并返回空串（1027-1029）；视图非 `Freshness.FRESH` → 特殊处理：`environment_state_pending_admission`（启动期 dispatch 绑定竞态）**不** latch 回滚、仅本轮 fallback legacy（1037-1038），其余 UNAVAILABLE/STALE 一律 `_trigger_read_port_rollback`（1030-1040）；
5. 成功则 `render_environment_state_view(view)` 渲染（1044）。

**"永不混用两种真相"**：read-port 渲染失败后 latch read_port→legacy，`_render_memory_block` 随后 fall through 到 legacy pinned 渲染（1114-1129 注释）——同一轮视图只来自一个来源。rollback 触发器 `rollback_environment_state(reason)` 只触发一次、写一条审计记录（coordinator_state_provider.py:113-125；worker 侧 `rollout_rolled_back`/`rollout_active` 在 worker_state_provider.py:225-231）。

worker 侧 read_port 数据来源是 **coordinator 的 HTTP 端点**：`fetch_environment_state_async`（worker_state_provider.py:286-369）在 pre_llm 阶段 POST `/environment-state`，请求携带绑定 server 下发 opaque `worker_task_id` 的 HMAC proof（`TeamStatusProof.generate(secret, worker_task_id)`，322-325）；启动期 `environment_state_unknown_worker_task` 403 属于 transient admission，按 `admission_retry_limit=5`、`admission_retry_delay=0.05` 退避重试后 defer 而不 latch（349-357）；其他 HTTP/ACL 失败 `_trigger_read_port_rollback` fail-closed（358-361）。

coordinator 侧 read_port 数据来源是 **进程内 EnvironmentStateProvider 组合根**：`_attach_read_port_provider`（coordinator_state_provider.py:174-191）在 MissionRuntime 被 admit 时（`set_runtime`，149-168）构建 `EnvironmentStateProvider(MemoryReadPort(ingestor.store, scope_id), ControlPlaneReadPort(runtime), scope_id=…, viewer_role="coordinator", viewer_id="system")`，其中 `scope_id = ingestor.scope_id_for(runtime.context_id, runtime_epoch)`（183）。无 provider 时 `query_environment_state` 返回 `Freshness.UNAVAILABLE(reason="environment_state_provider_not_configured")`（220-234）。

`EnvironmentStateProvider.query_environment_state`（sar_orch/environment_state_provider.py:243-…）：先 `_validate_principal` 做 ACL（scope/viewer/dispatch 声明必须与 principal 一致，225-239，ACL 只在 provider 做、绝不在 renderer）；`MemoryReadPort`（68-147）只读 canonical Memory 的 `spatial_snapshot / embodied_snapshot / temporal_events / conflicts / evidence_refs / memory_revision / view_revision / latest_sequence`；`ControlPlaneReadPort`（150-191）只读 MissionRuntime 的 dispatch 视图（worker viewer 只能看到自己的 dispatch）。

## 12. memory-read-mode 三态接线
#### 1.1 参数入口与 fail-closed 校验

`memory_read_mode` 从 CLI → `run_experiment` → `SARCoordinator` / `SARWorker` → `ContextConfig` / StateProvider 逐层透传，全部默认 `"read_port"`：

| 位置 | 默认值 | 出处 |
|---|---|---|
| CLI `--memory-read-mode`（choices legacy\|shadow\|read_port） | `read_port` | sar_orch/experiment.py:882-889 |
| `run_experiment(memory_read_mode=…)` | `read_port` | experiment.py:274 |
| `SARCoordinator.__init__` | `read_port` | sar_orch/coordinator.py:49 |
| `SARWorker.__init__` | `read_port` | sar_orch/worker.py:51 |
| `ContextConfig.memory_read_mode`（router/worker） | `read_port` | worker_agent/context.py:130、router_agent/context.py:128 |
| `SARCoordinatorStateProvider` / `SARWorkerStateProvider` | `read_port` | coordinator_state_provider.py:45、worker_state_provider.py:107 |
| 独立 a2a worker CLI（typer） | `read_port` | src/a2a/worker/cli.py:57-62 |

**fail-closed 规则（缺 secret / log_dir 时直接抛错，不在运行中降级）**：
- `shadow` / `read_port` 模式要求 `coordinator_secret` 为 `bytes` 且 `len >= 16`，否则抛 `MemoryAuthNotConfiguredError("memory_auth_not_configured: …")`（coordinator.py:79-93；worker.py:75-88，`MIN_COORDINATOR_SECRET_LENGTH = 16` 定义于 worker.py:19）。
- `shadow` / `read_port` 模式要求 `log_dir` 非空（canonical Memory 落地根），否则抛 `MemoryAuthNotConfiguredError("…requires log_dir")`（coordinator.py:361-369）。
- `legacy` 模式两者都不要求：secret 校验分支在 `if enable_peer_mail` 之外独立存在（coordinator.py:73-78 是 peer mail 的校验）。
- 独立 a2a worker 的 secret 来源是 `.env` 的 `coordinator_secret` 或环境变量 `A2A_COORDINATOR_SECRET`，缺失同样抛 `MemoryAuthNotConfiguredError`（src/a2a/worker/cli.py:72-89）。

#### 1.2 三态对比

| 维度 | `legacy` | `shadow` | `read_port`（默认） |
|---|---|---|---|
| canonical Memory 初始化（MemoryStore/Ingestor） | 不初始化（coordinator.py:359-360 分支外，`memory_config`/`memory_ingestor` 保持 None 传入 create_server，445-446） | 初始化（coordinator.py:361-385） | 初始化（同左） |
| 启动时额外组件 | 无 | `ShadowCompareService`（审计文件 `memory_rollout_audit.ndjson`，coordinator_state_provider.py:64-75） | `MemoryRolloutController` + `RolloutAuditWriter`（同路径，77-92） |
| LLM 的 Context 来源 | legacy pinned 渲染（Environment / Current State / Task Plan 三节） | **legacy pinned Context 仍是 LLM 源**，canonical 视图仅做对比证据（coordinator_state_provider.py:292-294、310-371） | canonical read-port 视图（`query_environment_state` → `render_environment_state_view`），失败才回退 legacy（context.py:1123-1129） |
| worker 读路径 | 本地 global-map 直读视图（semantic_map_url / team status） | 同 legacy（对比服务额外跑 canonical query） | HTTP `POST /environment-state`（worker_state_provider.py:286-369，proof 绑定 worker_task_id） |
| coordinator 读路径 | `snapshot()` 语义地图缓存 + legacy 渲染 | 同 legacy + `_run_shadow_compare`（每个 env step 一次，settled horizon 对比，310-371） | 进程内 `EnvironmentStateProvider`（MemoryReadPort + ControlPlaneReadPort，174-191） |
| 失败行为 | 无 latch | 对比失败仅记 `mark_error` 审计（361-365） | read_port→legacy rollback latch（一次），后续请求走 legacy（context.py:1027-1039、1046-1051） |
| run 末期 memory eval（materialize/acceptance/projection） | 跳过（experiment.py:196-197 直接返回） | 运行 | 运行 |
| 回滚目标 | — | — | `legacy` 保留为回滚目标（AGENTS.md:21） |

#### 1.3 shadow 对比服务与 read_port 回滚 latch

- **shadow 对比**：`_run_shadow_compare(env_step, payload)`（coordinator_state_provider.py:310-371）每 env step 至多一次；legacy 视图 = 当前 payload 快照，canonical 视图 = 同一 scope/viewer 的 `query_environment_state`；对比使用 settled horizon（排除当前 in-flight step 的证据，337-340）；非 allowlist 差异写入 `memory_rollout_audit.ndjson`。实际运行产物示例（logs/h2_shadow_audit_run2/coordinator/memory_rollout_audit.ndjson，20 条，均 `"mode": "shadow"`）：`{"kind":"memory_rollout_audit","scope_id":"2bcd…","mode":"shadow","path":".embodied_state.Alice","legacy":{…},"read_port":null}`。
- **read_port 回滚 latch**：`MemoryRolloutController.rollback(reason)` 首次调用置 `rolled_back=True` 并写审计；`rollout_active()` 返回 False 后 `_render_memory_block` 不再尝试 read-port（context.py:1125-1126）。canonical DB / outbox 永不被回滚逻辑触碰（coordinator_state_provider.py:113-119 注释）。

## 13. 运行与产物
#### 1.1 CLI 参数与 per-run secret 自动生成

`python sar_orch/experiment.py --memory-read-mode read_port`（AGENTS.md:5-21）。secret 生成逻辑在 `run_experiment`（experiment.py:411-428）：

```python
if enable_peer_mail:
    coordinator_secret = secrets.token_bytes(32)        # 411-418
if memory_read_mode in ("shadow", "read_port"):
    coordinator_secret = coordinator_secret or secrets.token_bytes(32)   # 421-428
```

即：`enable_peer_mail` 与 `shadow/read_port` 任一命中都会自动生成 32 字节 per-run secret，并同时传给 `SARCoordinator(coordinator_secret=…, memory_read_mode=…)`（447-469）与每个 `SARWorker(coordinator_secret=…, memory_read_mode=…)`（489-507）。与 `--enable-peer-mail`（store_true，876-881）类似，`--memory-read-mode` 也是纯 CLI 参数（882-889），不读取 .env。

#### 1.2 canonical Memory 落地文件

- **路径约定**：`MemoryConfig(experiment_id=run_id, memory_root=Path(log_dir)).validate()`（coordinator.py:377-380）；`db_path = memory_root / "memory" / "memory.sqlite3"`（src/a2a/coordinator/memory/contracts.py:524-525）。`validate()` 强制 `experiment_id` 非空、`memory_root` 为本地绝对路径（拒绝任何 `://` 路径，527-541）。
- **实际产物**（logs/h2_shadow_audit_run2/coordinator/memory/memory.sqlite3，368KB；run1 同构）：coordinator 的 `log_dir` 即 `<run>/coordinator`，所以 canonical DB 落在 `<run>/coordinator/memory/memory.sqlite3`。文件权限：目录 0700、DB 文件 0600（src/a2a/coordinator/memory/store.py:266-283）。
- **表结构**（sqlite_master 实测）：`memory_scope, memory_revision, memory_relation, control_transition_journal, security_audit, callback_nonce, temporal_event, idempotency_ledger, control_receipt, memory_outbox, spatial_entity, embodied_node, projection_field, projection_outcome, memory_view_revision`。
- **样例数据**（run2 实测）：
  - `memory_scope`：`scope_id=2bcd…, project_id='llamar', experiment_id='sar-scene1-agents2-seed42-ccece10f', context_id='b306f2b5-…', runtime_epoch=1`；
  - `memory_revision`：`revision=46`（per-scope 单调投影 revision，contracts.py:544-552）；
  - `temporal_event`：63 行，事件类型如 `callback.status_update`，带 `event_id / sequence / actor_id / dispatch_id / worker_task_id / tool_call_id / success / payload(JSON) / causation_id / correlation_id / idempotency_key / supersedes_event_id`；
  - `projection_field`：38 行，`domain=spatial|embodied`，样例 `(CaldorFire, fire, position, [7,4,0], env_step=1, provenance='worker_observation', confidence=1.0, evidence_id='cb:…:fire:CaldorFire:1', outcome='material')`；
  - `memory_outbox`：56 行 pending 控制事件（如 `control.lifecycle`），`idempotency_ledger` 40 行、`callback_nonce` 40 行、`security_audit` 3 行、`control_receipt` 6 行。
- **run 末期接线**：`run_experiment` 先把 `run_metrics.json` 写入结果目录（experiment.py:742-748），再调 `_invoke_run_terminal_memory_eval`（749-755，定义 173-255）：仅 `shadow/read_port` 执行；`server.materialize_compatibility_artifacts(exp_dir)` 从 canonical 集合确定性重建兼容产物（`semantic_map.jsonl` + export manifest，203-204）；随后写 `memory_acceptance.json`（221-222，gate 只记录不阻断，231-237）与可选 `memory_projection_quality.json`（--truth-manifest 提供时，241-254）。
- **兼容产物**：顶层实验目录仍写 `semantic_map.jsonl`（coordinator.py 启动后由 experiment.py 重定向路径，experiment.py:477-484），即 legacy 观测流（`observation_ingested` 事件）与 canonical Memory 双写并存；canonical 是官方路径。

#### 1.3 观测/记忆相关的 prompt 指令

实际运行使用 `sar_orch/prompts/`（experiment.py:43-44、456、500；`src/prompts/` 下 system.md 与 memory/观测 无相关内容，grep 0 命中）：

- **coordinator（system.semantic.md）**：§"Environment State (auto-injected every round)"（24-33）——每轮自动注入 Environment/Step Budget/Task Plan & Progress/Recent Changes/Supervision Alerts，明确"不需要调 query_task_events"（33）；"trust worker autonomy… They have access to shared memory"（69）；semantic 模式说明：世界事实/团队/任务状态每轮自动注入 Environment State，`query_task_events` 仅调试用，未知火/人由 worker 侦察 + `report_observation` 发现（168）。
- **worker（sar_orch/prompts/worker/system.md）**：每次 action 后细读 observation（29）；发现火/人/水库/状态变化时调 `report_observation()` 结构化 JSON（33）；"Your state is auto-refreshed… automatically injected into the Environment State block before each LLM call"（22）；团队协调块 [CARRYING PERSON]（21、50）；Environment State 是"auto-injected every round"，读它而不是浪费 `get_agent_state()` 调用（64、68）。

#### 1.4 文档一致性检查（docs/system_docs/contextmanager.md）

- **已过时**：`docs/system_docs/contextmanager.md:48` 写 `memory_read_mode: str = "legacy"  # 读路径 feature flag；默认 legacy，暂不切换任何读路径`——与当前代码默认 `read_port` 矛盾（worker_agent/context.py:130、router_agent/context.py:128、build.py:74、experiment.py:885）。该文档日期 2026-07-26，早于 H3 retirement（2026-08-10，提交 79e20bc）。
- **仍准确**：三层记忆模型（pinned + episodic + recent window）、strategy 对比表（docs:56-61 与代码 818-819/861-862/960-967 一致）、`assemble()` 未闭合 tool call 拒绝追加（docs:30 ↔ context.py:978-979）、ContextConfig 其余字段默认值（docs:37-47 ↔ context.py:118-126）。

## 14. 评估与验收体系
LLaMAR Memory 重构的评估/验收分三层：**①单元/契约测试层**（`tests/test_memory_*.py`，16 个文件）；**②运行期验收评测器**（`sar_orch/eval/memory_acceptance.py`，每 run 一个 `memory_acceptance.json`）；**③终端只读投影质量评测器**（`sar_orch/eval/memory_projection_quality.py`，每 run 一个 `memory_projection_quality.json`，配套 evaluator-private truth 记录器 `sar_orch/eval/truth_recorder.py`）。三者共同构成设计文档 §9 Gate 门禁（H1/H2/H3）与 §10.1 指标契约的实现。

### 验收评测（memory_acceptance）

**定位**：Phase 5 run-acceptance 评测器，读取一次运行的结果产物，写出固定 schema 的 `<results_dir>/memory_acceptance.json`；失败行只从 Phase 5 logger API 产出的结构化 `{Success, ErrorType}` outcome 行聚合，**从不 grep 运行时日志**（`sar_orch/eval/memory_acceptance.py:2-23`）。

**输入产物**（按固定候选路径查找）：

| 输入 | 路径候选 | 出处 |
|---|---|---|
| `coverage` / `transport_rate` | `<results_dir>/run_metrics.json`，两者必须非 null 且可转 float，否则 `EXIT_METRICS_MISSING` | memory_acceptance.py:103-128 |
| 失败 outcome 行 | `<results_dir>/agent_interactions.csv` + `router_interactions.csv`，缺任一文件 → `EXIT_ARTIFACTS_MISSING` | memory_acceptance.py:150-158 |
| export manifest | `coordinator/memory/export_manifest.json` / `memory/export_manifest.json` / `export_manifest.json` | memory_acceptance.py:63-67 |
| canonical DB | `coordinator/memory/memory.sqlite3` / `memory/memory.sqlite3` | memory_acceptance.py:69-72 |

**失败行判定**：CSV 中 `Success` 列取值 `false/0/no`（小写化后）即失败；若无 `Success` 列，则 `ErrorType` 非空视为失败（memory_acceptance.py:131-147）。

**输出 schema**（`schema_version=1`，memory_acceptance.py:52,311-322）：`scope_id`、`memory_revision`、`export_manifest_sha256`、`coverage`、`transport_rate`、`failed_tool_rows`、`missing_error_code_rows`、`framework_error_counts`（固定三键 `worker_busy` / `task_not_routable_yet` / `unknown_task_id`，来自 `REPORTED_FRAMEWORK_ERROR_CODES`）、`known_allowlisted_counts`。scope 解析：manifest 必须恰好声明一个 `scope_id`（memory_acceptance.py:196-207），revision 取 manifest 中 `canonical_revision`/`memory_revision` 最大值（memory_acceptance.py:210-224）；manifest 中的 scope 必须在 canonical DB `memory_scope` 表存在（memory_acceptance.py:227-235,278-282）；无 manifest 时回退到 DB 中 revision 最大的活跃 scope（memory_acceptance.py:238-261,289-300）。

**判定 Gate**（`gate()`，memory_acceptance.py:326-351）与退出码（memory_acceptance.py:52-61）：

| 条件 | 行为 | 退出码 |
|---|---|---|
| `missing_error_code_rows > 0`（失败行 ErrorType 为空） | `instrumentation_missing` | 3 |
| 三个 `framework_error_counts` 任一 > 0 | `framework_error`，打印 code=count | 5 |
| 存在不在 `FRAMEWORK_ERROR_CODES` 全量白名单中的 code（含哨兵 `unclassified_tool_error`、`missing_error_code`） | `unknown_error_code`，打印 code/count | 4 |
| 白名单内但非三报告码（如 `graph_activation_required`） | 计入 `known_allowlisted_counts`，**通过** gate | 0 |
| `run_metrics.json` 缺失/非对象/指标为 null/非数值 | `EXIT_METRICS_MISSING` | 6 |
| outcome CSV 缺失 | `EXIT_ARTIFACTS_MISSING` | 7 |
| manifest/DB 缺失、scope 数≠1、manifest scope 与 DB 不符 | `EXIT_INVALID_MEMORY_MANIFEST` | 8 |
| 全部通过 | 打印 `memory_acceptance: PASS scope=… coverage=… transport_rate=…` | 0 |

**运行方式**：`uv run python sar_orch/eval/memory_acceptance.py --results-dir <results_dir>`（唯一 CLI 参数 `--results-dir`，memory_acceptance.py:26,361-366）。10-run 固定验收矩阵（5 scenes × agents {2,4} × seed 42，max_steps 20、mode semantic、`--memory-read-mode read_port`）由设计文档 §10.2 给出，每 run 依次执行 experiment → acceptance → projection quality（`.hermes/plans/memory-system-redesign-design.md:1475-1495`）。

**真实输出样例**（`sar_orch/results/memory_acceptance_a0d6712_20260809_153738/scene_1_agents_2/memory_acceptance.json`）：`coverage=0.667`、`transport_rate=0.733`、`failed_tool_rows=0`、`missing_error_code_rows=0`、三框架错误码全 0、`memory_revision=256`、`export_manifest_sha256=ad72bfc7…`。该 10-run 矩阵最终 **10/10 both-pass**（计划文档 `.hermes/plans/memory-system-redesign-progress.md:36,56`；绑定矩阵根 `memory_acceptance_a0d6712_20260809_153738`，H3 审批记录 `.hermes/plans/memory-system-redesign-h3-approval-record.md`）。较早一轮（candidate `3202abb`，`sar_orch/results/memory_acceptance_3202abb_v2_20260809_082951/final_report.md:11,38-41`）曾出现 9/10：run 10 因 shutdown 路径 `A2AClientError` 不可 JSON 序列化缺陷，teardown 期 7 行 post-terminal 失败行混入 `router_interactions.csv`（含哨兵 `unclassified_tool_error=2`），acceptance 按设计 exit 4——证明 gate 对「结构化 artifact 污染」敏感。

### 投影质量评测（memory_projection_quality）

**定位**：Phase 5 终端（terminal-only）评测器（H1 card C7），只在 run 到达 terminal、canonical Memory 快照冻结后运行；以只读方式把冻结的 Worker evidence（`temporal_event` 中 `evidence.projection`）与 Memory projection 快照同 **evaluator-private** truth manifest/trace 对比，写出 `<results_dir>/memory_projection_quality.json`（`sar_orch/eval/memory_projection_quality.py:1-33`）。

**硬边界**（memory_projection_quality.py:9-17）：canonical SQLite 只以 `mode=ro` 打开，绝不写 SQLite/projection/revision/outbox/Context/兼容 artifact；raw truth trace 只在 evaluator-private manifest 位置读取，不复制进 results dir 或候选可读路径；oracle truth 只用于事后对比，绝不做在线修正。

**truth 输入格式**：truth manifest（JSON，必含 `scope_id`，可含 `trace` 相对路径，memory_projection_quality.py:482-498）+ truth trace（JSONL，每行必含 `step`/`domain`/`entity_id`/`field`/`value`，缺字段或 step 非整数 → `EXIT_INVALID_INPUT`，memory_projection_quality.py:156-200）。truth 由 `sar_orch/eval/truth_recorder.py` 在 run 期间从 `SARBarrier.get_env_snapshot()` **只读**采集，按 canonical 命名（domain=embodied/spatial、entity_id 为 worker 观察对象名、field/value 为 canonical 形状），只追加 `<truth-output-dir>/truth_trace.jsonl`，terminal 时写 `truth_manifest.json`，全程不写 canonical Memory/Context/map（truth_recorder.py:1-38,56-57）。

**终端状态分类**：由 `run_metrics.json` 的 `finished`/`end_reason` 映射为 `completed`/`timeout`/`failed`/`cancelled` 四类（memory_projection_quality.py:52-66,127-153），无法识别 → `EXIT_INVALID_INPUT`。

**指标与公式**（`_finalize_quality`，memory_projection_quality.py:429-460）：

- **worker_report_quality**（Worker 汇报质量，同 step 口径，memory_projection_quality.py:366-390）：对每条 truth claim 取 step ≤ claim.step 的最新同 key（domain,entity_id,field）Worker report；`observable_field_count`=step 恰等于 claim.step 的报告数；`correct_field_count`=其中值相等者；`false_claim_count`=值不等者；`stale_report_count`=仅剩旧 step 报告者。`precision = correct/(correct+false_claim)`，`recall = correct/observable`（memory_projection_quality.py:444-448）。
- **memory_integration_quality**（Memory 集成质量，memory_projection_quality.py:393-426）：`evaluated_projection_field_count`=truth claim 有对应投影字段数；`traceable_field_count`=投影 `event_id` 存在于 canonical `temporal_event` 者；`conflicted_field_count`=outcome=`conflicted` 或 `conflict_candidates` 非空者；`stale_projection_count`=投影 `env_step` < claim.step 者；`correct_projection_field_count`=非冲突、step 相同且值相等者。`precision = correct/evaluated`，`recall = evaluated/total_claims`，`conflict_precision = 冲突中值正确数/conflicted_field_count`（分母为零时为 0.0），`evidence_traceability_rate = traceable/evaluated`（memory_projection_quality.py:449-459）。
- **null 规则**：`total_claims==0` 或 `observable==0 and evaluated==0` 时全部 rate 置 null，`metric_status=not_applicable`（memory_projection_quality.py:436-442）；否则 `measured`。invalid/missing 输入一律非零退出（`EXIT_INVALID_INPUT=3`，memory_projection_quality.py:48-50）。
- 附带输出 `memory_manifest_sha256`：对 scope/revision/projection 字段/temporal/outcome/relation 计数的 canonical JSON 做 SHA-256（memory_projection_quality.py:304-337），确定性可复现（测试 `test_memory_manifest_digest_is_deterministic`，tests/test_memory_projection_quality.py:607）。

**运行方式**：`uv run python sar_orch/eval/memory_projection_quality.py --results-dir <results_dir> --truth-manifest <evaluator-private>/truth_manifest.json [--truth-trace <path>]`（memory_projection_quality.py:29-33,544-559）。

**真实输出样例**（`memory_acceptance_a0d6712_20260809_153738/scene_1_agents_2/memory_projection_quality.json`）：`metric_status=measured`、`terminal_status=timeout`、`worker_report_quality.precision/recall=1.0`（observable 24、stale 138）、`memory_integration_quality.precision=0.05 / recall=0.49`、`conflict_precision=0.0`（conflicted_field_count=0，分母为零）、`evidence_traceability_rate=1.0`（620/620）。计划文档说明：broad truth 口径下低 Memory precision 是「最终态投影 vs 每步 claim」的测量校准产物，`conflict_precision=0.0` 实为分母为零，非框架缺陷（`.hermes/plans/memory-system-redesign-progress.md:52`）。

### 测试矩阵（tests/test_memory_*.py，16 个文件）

| 文件（行数） | 覆盖契约 / 模块 | Phase |
|---|---|---|
| test_memory_contracts.py（223） | MemoryScopeV1 的 scope_id=SHA-256(canonical JSON)、命名空间 relations 只读 control refs、`FRESH\|STALE\|UNAVAILABLE` 三值、MemoryService 禁 control-plane mutation、control_transition_digest 确定性且不含 raw body、MemoryConfig 校验本地绝对根、normalize_inventory 永不 eval 任意字符串 | P0 |
| test_memory_scope.py（95） | scope 生命周期：tuple 唯一/重复激活同 handle、同 context 不同 epoch 隔离、closed scope 不可重开、缺字段 fail-closed 并审计、负 epoch 拒绝 | P0 |
| test_memory_control_journal.py（222） | control_revision 单调且 dispatch-local、rejected transition 不推进 revision/journal、journal digest 排除 raw callback body、receipt seam 在 runtime lock 外投递、bridge 未运行前崩溃 journal 仍存活、restart reconciliation 只补一次缺失 receipt | P0 |
| test_memory_callback_auth.py（284） | CallbackProofV1 生成/验证往返、拒 body 篡改/过期/worker 不匹配/伪造签名/畸形 proof、`MIN_CALLBACK_SECRET_BYTES` 常量、短/空/缺 secret 启动 fail-closed（`memory_auth_not_configured`）、nonce 持久保留且重放被拒、认证失败零领域写入、失败只产脱敏 security audit、nonce 跨 store 重启持久 | P2 |
| test_memory_ingestor.py（716） | MemoryIngestor：一次 callback 一个 canonical bundle、同 body 新 nonce 返回原 receipt（幂等）、同 key 异 digest 冲突回滚、unknown/closed scope 零写入、600 条 canonical events 不裁剪、callback 原发 receipt 走 bundle 而非 bridge、control receipt 至多一个 bundle、commit 前崩溃 bundle all-or-none、commit 后 publish 前 restart reconciliation、digest 不匹配 fail-loud 零写入、callback+restart 竞争仍单 bundle、有效 callback 任何 sink 不落 raw secret、ingestor 从不读 control state、投影原子归约、forbidden truth 投影零写入、投影输入脱敏 | P2 |
| test_memory_redaction.py（480） | SensitiveTextRedactor + RedactionPolicy：secret/HMAC/API key/proof/Authorization/Cookie 替换为 `[REDACTED:<kind>:<sha256-prefix>]`、非敏感结构保留、工具结果 content/error/data 递归脱敏、router/worker failed tool result 脱敏、sink 在 a2a enqueue 前脱敏、reducer 派生投影无 raw secret、masked oracle truth 被拒且诊断脱敏 | P2 |
| test_memory_producer_matrix.py（179） | 源码级 inventory：14 个 callback/EventStore/supervision 写点必须声明 `# memory-producer:` 标记（canonical_source/idempotency/auth 三字段），总数恰为 14，新增 writer 必须带同三字段 | P2 |
| test_memory_supervision_ingest.py（200） | TaskWatchdog 五类事件（WORKER_UNREACHABLE/TASK_STALE/TASK_DEADLINE_EXCEEDED/TASK_DEADLINE_WARNING/TASK_RECOVERED）各自恰好一条 canonical TemporalEvent、event_id 按 kind 互异、closed/unknown scope 零写入、payload 脱敏安全、真实 mission runtime dispatch 集成、缺 runtime epoch fail-closed 零写入 | P2 |
| test_memory_store_thread_safety.py（293） | 跨线程安全（H2）：`check_same_thread=False` 单连接多线程可用、并发 reader/writer 无 `InterfaceError`、reader 永不见半个事务（RLock 串行化 BEGIN IMMEDIATE）、跨线程 relation 读写 | P2 |
| test_memory_projections.py（854） | C1–C5 投影契约：C1 结构化观察成当前事实并推进 canonical/entity/view revision 与 as_of_sequence；C2 迟到旧 step evidence 只进 Temporal audit（`ignored_out_of_order`）不回退；C3 同 step 同优先级冲突显式 CONFLICTED（不选赢家）、等值不算冲突；C4 跨 epoch/closed scope 类型化 `scope_closed`/`unknown_scope` 零写入且不重开；C5 字段级 source policy 部分物化、高优先级赢、`superseded_by_higher_authority` 审计、新 step 赢过优先级；另覆盖三 revision 分离、混合 scope/epoch bundle 拒绝、重复 bundle 类型化 duplicate 无新行、认证关联保留、correlation 不伪造 dispatch_id、projection 字段可 join 回 temporal event | P3 |
| test_memory_online_truth_boundary.py（443） | H1-INV-1：forbidden provenance（barrier/oracle/ground_truth/checker/simulator/world_snapshot…）零领域写入、只留脱敏诊断、混合 bundle 任一成员含 forbidden 全拒、provenance allowlist 只含在线 worker 源、masked truth 拒绝且诊断不含 raw 词/secret、secret+oracle 不进 audit、NormalizedProjectionInputV1 拒缺 scope 字段、**源码级**：memory 模块不得引用 `SARBarrier.get_env_snapshot()`/checker truth/oracle tools、semantic mode 不读 barrier world 字段 | P3 |
| test_memory_recovery.py（401） | 启动顺序 seam 组合、kill/restart 后 outbox replay 物化 artifacts、replay 幂等且确定性、unknown scope replay 类型化、committed canonical set + scope fence 校验通过、篡改 artifact 被 verify 检出且 replay 修复、缺 scope 报 inconsistent、当前 scope 缺 manifest 报错、legacy-unmigrated 不自动 backfill 不 purge、无自动 retention purge、exporter 实例注入 | P5 |
| test_memory_compat_export.py（416） | exporter：同 canonical 集 → 字节级确定性导出、无残留 temp 文件、temp+fsync+replace 原子覆盖、unknown scope 抛错、manifest entries 契约、artifact 内容、`semantic_map.jsonl` fixture 字段/顺序与 legacy writer 兼容、真实 `render_sar_report.load_semantic_map` 可读、legacy-unmigrated 文件保持只读、导出目录缺失自动创建 | P5 |
| test_memory_legacy_unmigrated_marking.py（82） | legacy debug adapter（EventStore `events_<task>.ndjson`、SupervisionStateStore `supervision_<dispatch>.ndjson`）枚举面：无 log_dir 为空、与写入文件名一致、文件只读不重写、只枚举目标文件、helper 只返回相对名 | P5 |
| test_memory_acceptance_metrics.py（662） | memory_acceptance 契约（§10.1）：干净 run 带 DB / 带 export manifest 均通过；worker_busy/router 失败计入并使 gate 失败；双 CSV 失败聚合；空 ErrorType → `instrumentation_missing`；未知 code exit 4 且打印 code/count；白名单非报告码计 known 并过 gate；哨兵 `unclassified_tool_error`/`missing_error_code` 字面量 exit unknown；Success+ErrorType 并存不算失败；null coverage/transport 拒绝；缺 outcome artifact 拒绝；缺 memory+manifest 拒绝；manifest scope 与 DB 不匹配拒绝；DB fallback scope/revision；coordinator/memory 布局；main/CLI 脚本 clean exit 0 与各非零退出路径 | P5 |
| test_memory_projection_quality.py（713） | 投影质量评测器（H1 C7）：completed+measured 正确投影、worker precision/recall/staleness、同 step 冲突校准、迟到证据不回退投影、空 truth trace / 无证据无投影 → not_applicable 全 null、terminal status 映射、未知 terminal 非法、scope 不匹配/missing canonical DB/missing manifest 非法、truth trace override、**只写 quality artifact 一个文件**、memory manifest digest 确定性、coordinator/memory 布局、main/CLI 干净退出 | P5 |

> 配套但不在 `test_memory_*` 命名下的相关测试：`tests/test_worker_callback_signing.py`（Phase 2 签名推送）、`tests/test_tool_result_error_protocol.py`（error taxonomy，Phase 5）、`tests/test_truth_recorder.py`（truth recorder，8 个用例：claim 形状与 canonical 命名、live barrier 富化、逐 step 记录与幂等、manifest digest/必需键、只写 evaluator-private 文件、legacy 模式跳过 manifest、端到端 measured 数值、readable enum 归一化）、`tests/test_environment_state_acl.py`（read-port ACL）、`tests/test_environment_state_rollback.py`（read_port→legacy rollback 与 audit）。

### 与设计阶段（Phase 0–5）的对应

| Phase | 内容 | 对应测试/评测 | Gate |
|---|---|---|---|
| P0 | 契约与最小 kernel scaffold | test_memory_contracts / scope / control_journal | — |
| P1 | active 名称迁移、Context 协议、`memory_read_mode=legacy` 默认 | test_environment_state_rename / test_context_protocol_closure | — |
| P2 | authenticated Temporal shadow write | callback_auth / ingestor / redaction / producer_matrix / supervision_ingest / store_thread_safety | — |
| P3 | Spatial/Embodied projection 与在线 truth boundary | projections（C1–C5）/ online_truth_boundary（H1-INV-1） | **H1** Projection Semantics Lock（实施前，`.hermes/plans/memory-system-redesign-design.md:1105`） |
| P4 | EnvironmentStateProvider read-port cutover | test_environment_state_provider / ACL / rollback | **H2** Read-Port Cutover（shadow compare allowlist 为零后，design.md:1106；真实 rollout 99/99 FRESH、零 rollback、零 secret 泄漏，progress.md:50） |
| P5 | recovery、compatibility 物化、legacy retirement 准备 | recovery / compat_export / legacy_unmigrated_marking / acceptance_metrics / projection_quality；evaluators 与 truth_recorder | **H3** Retirement and Release（10-run `memory_acceptance.json` + `memory_projection_quality.json` 汇总等验收包，design.md:1107；2026-08-10 APPROVE，绑定 `a0d6712`，progress.md:44,52） |

**验收门槛与实测结论**（出处：`.hermes/plans/memory-system-redesign-progress.md`）：

- Phase 退出计数：P0 26 passed（:31）、P1 54（:32）、P2 110（:33）、P3 复核后 249（:34）、P4 149→171 + 真实 rollout PASS（:35）、P5 全量 **1636 passed**、10-run 矩阵 **10/10 both-pass**（:36）。
- H3 门禁证据勾选（:56-62）：10-run acceptance 10/10 通过、`missing_error_code_rows=0`、`worker_busy`/`task_not_routable_yet`/`unknown_task_id` 全 0（矩阵根 `sar_orch/results/memory_acceptance_a0d6712_20260809_153738/final_report.md`，artifact SHA-256 绑定）；projection quality 10/10 `metric_status=measured`、`evidence_traceability_rate=1.0`；recovery 与 compatibility 测试证据；error-code taxonomy 全白名单化；secret-leak 零 secrets、truth manifest 在 run results 目录外且 evaluator-private。
- 通过条件（design.md:1497）：10 个 acceptance + 10 个 quality json 均存在且 schema valid、coverage/transport_rate 非 null、`missing_error_code_rows=0`、三框架错误码全 0、quality digest/denominator 规则通过；任何 timeout/缺 artifact/instrumentation_missing/unknown code/digest 不一致均为失败，不得只报「pytest 通过」。

**运行入口汇总**：评测器 CLI `python sar_orch/eval/memory_acceptance.py --results-dir <dir>` 与 `python sar_orch/eval/memory_projection_quality.py --results-dir <dir> --truth-manifest <path>`；focused pytest 见各 Phase independent command（design.md:1136-1141,1191-1196,1358-1363）与全量清单（design.md:1463-1473）；10-run 矩阵脚本（design.md:1477-1495）。AGENTS.md 未收录 acceptance 运行命令（grep 无匹配），实际入口以设计文档 §10.2 与两个评测器 docstring 为准。

## 15. 交叉引用

- [`semantic_map.md`](semantic_map.md) — 语义地图（Semantic Map）子系统：观测摄入管道、MapDiff/MapSummarizer、StateProvider 注入。canonical Memory 的 `semantic_map.jsonl` 兼容产物即由此物化（见 §8 导出与兼容性），二者在 `--mode semantic` 下并存：legacy 观测流与 canonical 双写，canonical 为官方路径。
- [`contextmanager.md`](contextmanager.md) — ContextManager 三层记忆模型（pinned + episodic + recent window）。注意该文档日期 2026-07-26，其中 `memory_read_mode: str = "legacy"` 已过时——H3 retirement（2026-08-10）后代码默认 `read_port`。
- [`框架.md`](框架.md) — 系统整体架构（A2A 传输、Agent kernel、SAR 编排）
- [`data_flow.md`](data_flow.md) — A2A / context_id / task_id 数据流追踪
- [`logging_map.md`](logging_map.md) — 所有日志记录点与输出文件
- [`experiment_design.md`](experiment_design.md) — 实验编排与 `--mode` 开关
- [`sandbox.md`](sandbox.md) — Agent 沙箱策略
- 设计文档：[`.hermes/plans/memory-system-redesign-design.md`](../../.hermes/plans/memory-system-redesign-design.md) — 设计意图与分期迁移（本系统文档以真实代码为准）
- 进度记录：[`.hermes/plans/memory-system-redesign-progress.md`](../../.hermes/plans/memory-system-redesign-progress.md) — Phase/Gate 状态与验收证据
