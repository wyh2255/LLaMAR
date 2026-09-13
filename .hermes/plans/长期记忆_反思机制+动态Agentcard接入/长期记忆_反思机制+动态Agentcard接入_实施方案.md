# 长期记忆反思机制 + 动态 AgentCard 接入实施方案

> **状态：提案，未实施。** 本文件只冻结建议的目标、契约、分期和验收；不构成代码实施授权。依照项目实施门控，只有用户明确说“开始实施”后才进入 Phase 0。
>
> **冻结输入：** `feat/memory-redesign@5413705`；探索总览 SHA-256 `e4beeaf897426662d1e75c531bf7e6d2d0dfd441e87279f244463e8ef88508f8`；三份代码探索 SHA-256 见 [README_代码事实探索.md](README_代码事实探索.md)。本方案写作时 `docs/system_docs/memory.md` 为用户已有 dirty 文件，不在本方案阶段改写。
>
> **For Hermes:** 实施时按本方案逐 Phase 执行：每个 Phase 由一名实现 subagent 负责，父 agent 独立复验 RED/GREEN、工作区边界与真实 run 证据；不得在同一 Phase 提前实现后续 Phase。

**Goal：** 在不破坏 H1-INV-1 在线真相隔离、H3 read_port 主路径和现有 Spatial/Embodied reducer 语义的前提下，完成三项能力：

1. 将 worker 回调中已有的 agent 自身 `position`/`inventory` 作为可追溯 Embodied telemetry 写入 canonical Memory；
2. 将 AgentCard 的能力元数据作为 `registry` 来源的 Embodied 字段快照写入（新 worker 加入时 bootstrap；断线重连同步不在 V1）；
3. 建立 run-local 的第四类长期记忆：单次 run 内从受控在线事件窗口**滚动反思**（LLM 总结已发生事实），先 shadow 写入，再以 coordinator-only read-port 注入 run 内后续轮次；跨 run 复用不属本方案（用户拍板 2026-08-11：用 skill/文档沉淀）。

**Architecture：** 维持现有 run-local `MemoryStore` 作为 Temporal/Spatial/Embodied 的唯一 canonical source；不扩展 `projection_field.domain`。新增 run-local `LongTermMemoryStore`（独立文件、独立 schema、与 canonical 同生命周期），只保存经过 allowlist、脱敏、证据引用校验后的反思产物和审计元数据。反思写入与现有 callback/reducer 事务分离；其输入是已提交的 canonical event snapshot，LLM 调用绝不在 SQLite transaction 内进行。

**Tech Stack：** Python 3.10+、stdlib SQLite/WAL、现有 `MemoryStore`/`MemoryIngestor`/`EnvironmentStateProvider`、A2A AgentCard、现有 OpenAI-compatible provider 配置；不新增第三方依赖。

---

## 1. 先校正前提：本方案不接受的错误实现

| 易错前提 | 已证实事实 | 本方案处理 |
|---|---|---|
| “心跳携带 position/inventory/battery” | 心跳 payload 仅有 `worker_id`（`src/a2a/shared/types.py:93-94`）；battery 当前没有生产者 | V1 不扩充心跳、不伪造 battery；只使用签名 callback 中已有的 position/inventory |
| “补 `obj_type == agent` 即可有 Embodied 数据” | SAR 观测列表没有 agent **自身**（其他 agent 可经 `AbsAgent→agent` 映射被观测，`barrier.py:27-34`）；agent 自身状态在 `structured_data` 到达 coordinator 后被丢弃（`server.py:98-142`） | 新增独立 telemetry 提取/归一化路径，不依赖环境对象观测 |
| “AgentCard 已支持运行期动态 append” | `skills.append` 发生在 `create_worker_a2a_server()` 构造 AgentCard 前（`a2a_server.py:164-199`），无运行期更新 API | V1 的“动态”定义为**新 worker 加入（首次注册）时的能力 bootstrap**；断线重连的能力变更同步不在 V1；真正 in-process skill mutation 另设 Gate，不凭空实现 |
| “长期记忆可加为第三种投影 field” | `projection_field` 仅允许 `spatial|embodied`，且其 revision/env_step 语义是单 scope 当前态（`store.py:169-207`） | 长期记忆使用独立 run-local store/table，不污染 H1 reducer 或 `MemoryReadPort` 的当前态语义 |
| “现有 canonical DB 能跨 run” | `MemoryConfig.db_path=<memory_root>/memory/memory.sqlite3`（`contracts.py:510-541`）；SARCoordinator 用本 run `log_dir` 作为 root（`sar_orch/coordinator.py:357-385`） | 长期记忆是独立 run-local 文件（`<memory_root>/long_term/long_term.sqlite3`），路径由同一 `MemoryConfig.memory_root` 派生，不新增独立 root 参数 |
| “control.* 已含 coordinator 决策原文” | control canonical payload 只含 `journal_sha256`、`result_digest`（`ingestor.py:555-570`） | V1 只使用可验证的 control lifecycle/action 证据，不采集 raw LLM reasoning/CoT；原文决策摘要是独立的后续设计项 |
| “可在 scope close 触发反思” | `close_scope()` 有实现，但当前生产调用仅见 recovery facade，run terminal 只 materialize/evaluate（`store.py:382-390`、`experiment.py:173-255`） | V1 挂在滚动反思 hook（run 内触发点）与 terminal committed-revision snapshot；不在本待办里暗改 scope-close 生命周期 |
| “长期记忆必须跨 run 聚合沉淀” | 反思产物 run-local 落库；跨 run 复用走 skill/文档沉淀（用户拍板 2026-08-11） | V1 长期记忆为单 run 语义：不做跨 run 查询/注入/导入 |

## 2. 目标边界与冻结建议

### 2.1 必须保持的不变量

1. **H1-INV-1 不变：** 反思、telemetry、registry snapshot 只能使用 `ONLINE_PROVENANCE_ALLOWLIST` 七类来源；`barrier`、`oracle`、`ground_truth`、checker、truth manifest 一律不能进入 online Memory（`contracts.py:48-115`、`ingestor.py:578-635`）。
2. **控制面不变：** `MissionRuntime.PhysicalDispatch.state` 仍是唯一任务状态真相；Memory 不得新增控制面 mutation API（`tests/test_memory_ingestor.py:574-578`）。
3. **当前态 reducer 不变：** Spatial/Embodied 继续 Temporal-first、field-level arbitration、env_step/source priority/confidence/conflict 顺序（`projections.py`、`tests/test_memory_projections.py`）。
4. **认证边界不变：** worker 仍只能经签名 push callback 写入 coordinator-owned Memory；不得共享数据库或直接调用 Coordinator 内存对象（`docs/system_docs/memory.md:473-485`）。
5. **read-port 回滚不变：** 任一 Environment State 读取异常仍产生 `FRESH|STALE|UNAVAILABLE`，并保持现有 `read_port → legacy` rollback latch（`router_agent/context.py:926-1046`）。
6. **无 LLM fallback：** 反思模型漏字段、发明 evidence ref、非 function-call、含禁止真值词，均 fail closed：不写长期记忆，不以正则/自由文本/默认值补全。
7. **单 run 边界不变：** 长期记忆只在当前 run 内反思、写入与注入；不提供跨 run 读取、聚合或导入 API。

### 2.2 冻结决策（D1–D9 已逐项拍板，2026-08-12）

| 决策 | 推荐默认 | 理由与影响 |
|---|---|---|
| D1：self telemetry 权威性 | `position`/`inventory` 的直接自报使用 `worker_telemetry`，并将它置于这两个字段 source policy 的第一位；必须带 `env_step` | 原目标要求 telemetry 来源；当前 policy 未给其 position/inventory 优先级（`contracts.py:158-178`）。无 step 一律跳过，避免覆盖新事实 |
| D2：V1 AgentCard “动态”语义 | 新 worker 加入（首次注册）时拉取 AgentCard 并 bootstrap 能力快照入 embodied 投影；**不**承诺断线重连的能力变更同步，**不**新增运行期 add-skill API | 当前无 producer/API；先把已存在的注册链变成 canonical producer；断线重连的卡片变化不在 V1 验收范围（用户拍板 2026-08-12） |
| D3：sensor_type 声明 | 采用 `AgentSkill.tags` 中的保留格式 `sensor_type:<slug>`；registry 解析成排序去重列表 | A2A AgentCard 无独立 sensor 字段；tag 是现有标准载体。SAR 为虚拟仿真、无 sensor，V1 只做机制（sensor_type 恒空，验证走 fixture，用户拍板 2026-08-12）；无有效 tag 不写 `sensor_type`，不能把空值当事实 |
| D4：长期记忆物理根 | run-local 独立文件 `<memory_root>/long_term/long_term.sqlite3`（随 run 生命周期，不新增 CLI root 参数）；`long_term_mode` 默认 `off` | 单 run 语义无需稳定跨 run root；独立文件避免污染 canonical 15 张业务表与 `user_version=3`；跨 run 素材由导出/文档/skill 承担 |
| D5：长期记忆可见性 | `long_term_mode=off|shadow|read`；`read` 时仅 coordinator/system principal 可读，worker V1 不注入 | 长期策略性内容不应默认扩散给每个 worker；shadow 可先验证而不改变 LLM 上下文 |
| D6：发布语义 | 反思产物经 validator 通过后**同事务直接以 `published` 写入**（本 run 内生效；candidate 仅作审计轨迹，无异步转正）；同 `memory_key` 更新以 `supersedes_memory_id` 取代旧项；同 key 同 digest 零写入 | 用户已收缩为单 run 语义（2026-08-11）；幻觉防护由 fail-closed validator + function-call 契约 + shadow 模式承担，不再依赖跨 scope 投票 |
| D7：反思输入与执行 | 只取已提交 canonical `callback.*`、`evidence.projection`、`supervision.*`、`control.*` 的**增量窗口**（上次反思之后的 sequence 区间，首次为 run 开始）；窗口上限（默认 ≤200 事件 / ≤8k 字符，超出按 sequence 取最近并记 `truncated`）与触发节奏（默认：每 task 完成 + 每 supervision 事件 + 每 5 env_step，最小间隔 30s）全部在 `long_term.config` 可调；反思在独立线程**异步**执行（进行中反思 coalesce 跳过，terminal 收尾 join 等待）；不读 EventStore、CSV、原始 LLM trace、truth artifact | 保持 provenance/脱敏/审计闭环；滚动增量保证每次反思只总结“新发生的事”；异步避免阻塞 poll loop（用户拍板 2026-08-12） |
| D8：模型契约 | 只允许已实测可用的 function-calling/严格 schema adapter（复用现有 `LLMClient + tools` 栈）；`.env` 新增独立配置口 `reflection_provider` / `reflection_api_key` / `reflection_api_base` / `reflection_model`（默认 `openai` / `deepseek-v4-flash`）；机制：P4 经 `load_env_file` 读取 dict 或显式 `os.environ.update` 注入（`env_loader.py` 不写 os.environ，2026-08-12 实测）；缺 `reflection_api_key` 时 **read 模式启动显式拒绝（typed 错误），不静默降级**，保持 off/shadow；真实模型 5 次烟测必须 5/5 有效后才从 shadow 切 read | 已知可选 structured output 不稳定；不允许“缺字段自动补全”破坏 fail-closed 语义 |
| D9：可调参数配置面 | 新建仓库根 `long_term.config`（ini 格式，与 `.env` 平级）集中承载窗口上限/触发节奏/反思超时等 tunable；模型凭证走 `.env` 的 `reflection_*`；缺失项用默认值；**文件 unparseable 或值非法时 typed 错误 + `long_term_mode` 强制 off + audit，不静默回退默认** | 避免 tunable 散落硬编码（用户拍板 2026-08-12） |

决策已冻结（2026-08-12）；任何 Phase 均需用户明确“开始实施”才允许执行，Phase 0（测试契约）也不例外。

## 3. 目标架构与数据流

```text
A. Embodied telemetry（同一已认证 callback）
SARBarrier structured state
  -> ToolResult.data {step, position, inventory}
  -> A2AWorkerSink [DATA].structured_data
  -> CoordinatorServer telemetry extractor
  -> NormalizedProjectionInputV1(domain=embodied, provenance=worker_telemetry)
  -> ingest_callback() same canonical transaction
  -> Temporal evidence + embodied projection + relation/outbox

B. AgentCard registry snapshot（scope-aware）
Worker AgentCard.skills
  -> WS_REGISTER（新 worker 首次注册）-> Coordinator fetches card
  -> AgentRegistry normalized AgentInfo(capabilities, sensor_types, card_digest)
  -> active runtime scope bootstrap / active-scope re-sync
  -> registry projection input (domain=embodied, provenance=registry)
  -> existing ingest_projection() + generic projection_field

C. Long-term memory（run-local，独立 store）
run-local canonical MemoryStore committed revision（滚动触发）或 final（terminal 收尾）
  -> ReflectionSourceCollector (bounded, allowlisted, redacted source window)
  -> LongTermMemoryStore.reflection_run claim (no LLM in DB transaction)
  -> ReflectionModelPort function call -> typed candidate validation
  -> long_term_memory + support refs + project revision
  -> shadow audit OR coordinator-only EnvironmentStateView.long_term_memory
```

### 3.1 Telemetry payload V1

Worker 的 `ToolResult.data` 增加并只允许以下自报字段：

```text
{
  "observations": [...],             # 保持旧语义
  "step": <int>,                     # 与同次 observation 使用同一采样 step
  "position": [x, y, z] | null,
  "inventory": [resource, ...] | {...} | null
}
```

- 身份不相信 payload：`entity_id`/`actor_id` 一律来自已认证 `auth_dispatch.worker_id`，不是 `reporter`、`name` 或客户端传的 ID。注：观测路径的 entity_id 取 `name`（`server.py:771`），telemetry 与观测一致依赖项目约定 worker_id == agent name（SAR 成立，2026-08-12 确认）；若未来分离则可能出现双实体，届时另设计。
- `step` 缺失、非整数、position 非三维数值、inventory 无法 `normalize_inventory()` 时，对应 field **不产生 claim**；callback 自身的 Temporal 审计保持现有行为。
- `battery`、`localization_quality`、`node_telemetry` 仅在 worker 有真实结构化生产者后另开 Phase；禁止写 0、`None` 或推断值。
- telemetry evidence id 为 callback/body/worker/step 的确定性摘要，不能与环境 observation evidence id 冲突。

### 3.2 AgentCard metadata V1

```text
AgentSkill(id=<capability>, tags=[...])          -> capability: sorted unique skill_id list
AgentSkill(tags=["metadata", "sensor_type:<slug>"])
                                                  -> sensor_type: sorted unique slug list
backend/model metadata                            -> 保持现有 parser，不写 capability
AgentCard fetch 失败 / minimal registration       -> 不产生空 capability/sensor_type claim
```

- `AgentInfo` 增加 `sensor_types`、`agent_card_digest`、`agent_card_available`（或等价的可判定字段）。
- 快照 claim 使用 `env_step=None`：能力为静态 registry 元数据，不与物理 step 比较；其改变由 card digest/值和现有 idempotency 识别。
- runtime 创建时：先激活 scope，再对当前 online、card-available agent 做 bootstrap snapshot；**新 worker 首次注册**（registry 中此前不存在）成功拉卡后，对 active runtime/scope 摄入能力快照。
- V1 不声明“实时更新”，也不承诺断线重连的能力变更同步（用户拍板 2026-08-12）：worker 能力在注册时一次性快照；真正 `agent_card_changed` 消息和 in-process mutation 只有在出现真实 producer 后再设计。

### 3.3 长期记忆契约 V1

`LongTermMemoryStore` 是新 SQLite 文件，位于 `<memory_root>/long_term/long_term.sqlite3`（复用 `MemoryConfig.memory_root` 派生；独立文件、独立 schema、自带 migration runner，见跨 Run 补充）。最小表/关系：

| 持久化单元 | 主键/唯一约束 | 用途 |
|---|---|---|
| `long_term_revision` | `(project_id, scope_id)` | run-local 单调可见 revision |
| `reflection_run` | `(project_id, scope_id, source_memory_revision, snapshot_digest, policy_version)` | source snapshot 的 exactly-once persistence；状态 `pending|completed|rejected|failed|timeout`；附 `window_end_sequence`（本次增量窗口终点，游标推进依据）、`truncated`（窗口超限标志）；`scope_id` 即 run 隔离键 |
| `long_term_memory` | `memory_id`；`(project_id, scope_id, memory_key, content_digest)` 去重 | candidate/published/superseded 的长期内容，保存 redacted statement、kind、policy/model digest |
| `long_term_support` | `(memory_id, source_scope_id, source_event_id)` | 可重放证据链；每条 ref 绑定 source revision/event digest |
| `long_term_audit` | append-only | 只记红acted failure reason/digest，不写 raw prompt、CoT、secret、truth |

`LongTermMemoryCandidateV1` 至少含：`schema_version`、`memory_key`、`kind`、`statement`、`confidence`、`source_refs`。`memory_key` 由**系统确定性派生**：`sha256(project_id, kind, policy_version, canonicalized(statement))`；`content_digest = sha256(canonicalized(statement))`；两者都基于**最终存储的 post-redaction 文本**派生（redaction 先于 key/digest 计算，保证审计可复算，2026-08-12 冻结）；`kind` 为受限枚举 `strategy|lesson|hazard|pattern|status`（validator 拒绝枚举外值，2026-08-12 冻结），schema 对 `kind` 加 `CHECK(kind IN (...))` 双保险；`canonicalized(statement)` 规则定死：lowercase → strip → 空白折叠 → 去首尾标点；`policy_version` 为代码常量（初始 1），随 canonicalize/redaction 策略变更人工递增；模型不可见、不可选最终 key，key 只用于同 run 内去重与 supersede 关联。确定性 validator 必须保证：

- 每个 `source_ref` 都在本次 input window 内，且 scope/event digest 一致；
- `statement`/字段通过 redaction 与 forbidden-truth scan（词表复用 `FORBIDDEN_TRUTH_TERMS`，`contracts.py:72-91`）；
- `project_id`/scope/`memory_key` 不来自模型输出；
- output 非法（含同一响应内重复 `memory_key`）时只更新 `reflection_run.status=rejected`/audit，零 `long_term_memory` 与零 read-port 可见内容；
- model 调用、网络、export 不得落在 SQLite `BEGIN IMMEDIATE` 内；
- long-term store 要设置 WAL、busy timeout、有限重试和唯一幂等约束，支持多写入者（同 run 内线程，防御性多进程）终结阶段同时写入；锁耗尽返回 typed retryable 状态，绝不部分提交。

### 3.4 反思窗口与发布语义

1. 滚动反思 hook（run 内触发点与节流由 `long_term.config` 控制，默认：每 task 完成 + 每 supervision 事件 + 每 5 env_step，最小间隔 30s）与 terminal 收尾各取一次 `scope_id + committed memory_revision` 的一致快照（原子快照契约，见原子快照补充）；反思在独立线程**异步**执行，触发时若已有进行中的反思则 coalesce 跳过；terminal 收尾**先 drain 进行中的滚动反思（join）再取终局快照**，join 带超时（`long_term.config` `[timeout] reflection_sec`，默认 60s），超时写 typed timeout status 且不阻塞 run 退出；不调用 `close_scope()`。
2. 事件过滤为 `callback.*`、`evidence.projection`、`supervision.*`、`control.*`，取**增量窗口**（上次 `completed` 反思的 `window_end_sequence` 之后的 sequence 区间，首次为 run 开始；**窗口为空则跳过本次反思**），按 sequence 排序；以最新 `control.*` 为锚，附带同 dispatch/correlation 的受限上下文，固定最大事件数/字节数（超限取最近并记 `truncated=true`）并记录 `input_digest`。
3. **窗口游标推进：** `window_end_sequence` 只在 reflection_run 达到 `completed` 时推进；`failed`/`rejected`/`timeout` 不推进——下次触发取新快照重新覆盖该区间，不静默丢事件；进程崩溃恢复从长期库最近一条 `completed` 的 `window_end_sequence` 续读，不依赖内存状态。
4. 输入再次执行 truth scan（词表 `FORBIDDEN_TRUTH_TERMS`）/redaction。worker callback 的自由文本只能作为已脱敏、长度受限证据，不允许把原始 `[DATA]` 全量复制到 long-term store。
5. 反思产物 validator 通过后**同事务直接以 published 写入**（本 run 内生效；candidate 仅作审计轨迹，无异步转正流程）；同 `memory_key` 的新产物以显式 `supersedes_memory_id` 取代旧项（绝不原地篡改来源链），同 key 同 `content_digest` 零写入（幂等，`reflection_run` 标 `completed`）；long-term revision 在 published 与 supersede 时均递增。
6. 反思 V1 不读取 raw LLM reasoning。若后续确需“决策说明”，必须先设计一个只含结构化 tool/action 摘要、`provenance=control`、独立脱敏/长度/审计契约的 producer；它不属于本次实施范围。

## 4. 分期实施计划

### Phase 0：契约卡与 RED 测试（只改 tests）

**目标：** 先锁定新 API、状态机和边界；生产代码零改动。

**Create：**
- `tests/test_memory_embodied_telemetry.py`
- `tests/test_agentcard_memory_projection.py`
- `tests/test_long_term_memory_contracts.py`
- `tests/test_long_term_memory_store.py`
- `tests/test_long_term_reflection.py`
- `tests/test_long_term_environment_state.py`
- `tests/test_long_term_memory_quality.py`

**Modify（仅测试）：**
- `tests/test_auto_observation.py`
- `tests/test_memory_projections.py`
- `tests/test_memory_ingestor.py`
- `tests/test_memory_online_truth_boundary.py`
- `tests/test_environment_state_provider.py`
- `tests/test_environment_state_acl.py`
- `tests/test_coordinator_state_provider.py`

**RED contract：**
1. telemetry callback 在同一 canonical bundle 内产生 callback Temporal + evidence Temporal + `embodied.position/inventory`，且 `provenance=worker_telemetry`；重复 callback 不新增行；缺/坏 step 不产生 telemetry claim。
2. newer-step telemetry 不被旧/无 step 回退；同 step 时 D1 的 telemetry priority 规则可验证；所有没有真实 battery 来源的测试断言“不写 battery”。
3. AgentCard capability/sensor tag 归一化、minimal registration 零 claim、scope bootstrap、same-card duplicate、新 worker 首次注册的摄入契约均有 contract（重复注册不摄入）。
4. `LongTermMemoryConfig` 拒绝相对路径/URI；run-local root 自动派生（`<memory_root>/long_term/`）；跨 scope（run）读写隔离——两个 scope 的长期数据互不可见；同 source revision 重试不重复持久化；lock/retry 无 partial write。
5. source window 必须拒绝 legacy EventStore/truth/barrier/raw LLM trace；伪造 source ref、truth term、缺 function-call 必须 fail closed。
6. worker 视图永远没有 `long_term_memory`；coordinator read mode 才有，预算不足先丢长期段或低优先级事件，不能静默泄漏或造成 rollback 失效。
7. evaluator 只能 `mode=ro` 打开长期库，输出质量 artifact，不写 source/run/project DB。

**RED 验证：** 每个未来模块以函数内 import 保证 pytest 可收集；记录每个失败是预期 `ImportError`/`AttributeError`/`AssertionError`，不接受 fixture/collection `ERROR` 或 MagicMock 假绿。

**Exit：** `git diff --stat -- ':!tests/'` 为空；所有未来契约准确 RED；现有 Memory focused regression 仍绿。此 Phase 结束后停下，等待 Phase 1 实施授权。

### Phase 1：独立长期记忆 kernel 与配置（无生产触发、无 LLM）

**目标：** 建立独立 run-local store、typed DTO 和跨进程安全事务，不改变 run-local MemoryStore、callback 或 Context。

**Create：**
- `src/a2a/coordinator/memory/long_term.py` — `LongTermMemoryStore`、schema、transaction/retry、read API
- `src/a2a/coordinator/memory/reflection.py` — source/candidate DTO、deterministic validator、`ReflectionModelPort` protocol（只定义，不调用真实模型）

**Modify：**
- `src/a2a/coordinator/memory/contracts.py` — `LongTermMemoryConfig`、candidate/ref DTO、稳定错误码
- `src/a2a/coordinator/memory/__init__.py` — 显式 exports

**实现步骤：**
1. 先让 `test_long_term_memory_contracts.py` 和 `test_long_term_memory_store.py` RED。
2. 实现 run-local root 派生与校验、project namespace + scope 隔离、独立 schema（自带 migration runner）；不修改 `_SCHEMA` 中 `projection_field` 的 domain CHECK。
3. 实现短 transaction、WAL、`busy_timeout`、有限 `BEGIN IMMEDIATE` retry；在 network/model 调用前后均不得持锁。
4. 实现 reflection-run claim、candidate/support/publish/supersede 的幂等状态转换；测试两个独立 store instance 指向同一个 tmp DB 的并发/重试语义。
5. 保持 `MemoryStore`/run DB 一字不动；不接 experiment/coordinator，不产生任何真实长期数据。

**Exit：** Phase 0 相关长期 kernel tests GREEN；未来 reflection trigger/read-port tests 仍 RED；`tests/test_memory_scope.py` 和 `tests/test_memory_ingestor.py` 回归 GREEN。

### Phase 2：Embodied callback telemetry producer

**目标：** 让已有 worker 自身状态通过已认证 callback 落入 Embodied，而非扩展空心跳。

**Modify：**
- `sar_orch/barrier.py` — structured state 带当前采样 `step`
- `sar_orch/tools/worker/_barrier_helpers.py` — 保留 `step` 到 `ToolResult.data`
- `src/a2a/coordinator/server.py` — 独立 telemetry extractor/normalizer；同 callback 合并 observation + telemetry projection inputs
- `src/a2a/coordinator/memory/contracts.py` — 仅在 D1 批准后调整 position/inventory 的 source policy
- `tests/test_auto_observation.py`
- `tests/test_memory_embodied_telemetry.py`
- `tests/test_memory_projections.py`
- `tests/test_memory_ingestor.py`
- `tests/test_memory_online_truth_boundary.py`
- `tests/test_coordinator_push_callback.py`

**实施步骤：**
1. RED：写 signed callback 的真实 `[DATA].structured_data` fixture，明确 entity identity 来自 authenticated dispatch。
2. GREEN：`_extract_worker_telemetry_with_provenance()` 只提取字段白名单；`_normalize_telemetry_projection_inputs()` 仅在值/step 合法时构造 `NormalizedProjectionInputV1`。
3. 在现有 `server.py:1576-1628` callback path 合并 inputs，复用 `ingest_callback(... projection_inputs=...)` 的同事务机制；不新开旁路 transaction。
4. 核验 Temporal→Embodied relation、redaction、idempotency、older/no-step 不能覆盖和 closed/unknown scope 零写入。
5. 明确回归：心跳 `WS_HEARTBEAT` 分支 (`server.py:2275-2279`) 不增加状态 payload/Memory 写入；battery 仍没有字段行。

**Exit：** callback 原子性和 H1-INV-1 tests 全绿；真实 shadow run 中 `embodied_node`/`projection_field(domain='embodied')` 非空，并且只出现来自 callback 的合法 telemetry。注：P2 后 shadow 对比审计会从 run2 历史的“legacy 有 embodied、canonical 无”反转为“canonical 有、legacy 无”——这是预期行为，不是回归（2026-08-12 确认）。

### Phase 3：AgentCard registry projection（新 worker 注册 bootstrap）

**目标：** 将现有 AgentCard → AgentRegistry 链路转化为 scope-aware canonical producer（新 worker 注册 bootstrap；断线重连同步不在 V1），同时不把“静态能力”误当动态世界状态。

**Create：**
- `src/a2a/coordinator/memory/registry_projection.py` — AgentInfo/card digest 到 `NormalizedProjectionInputV1` 的纯函数与 idempotency input builder

**Modify：**
- `src/a2a/worker/a2a_server.py` — 支持声明 `sensor_type:<slug>` metadata tags（默认无 sensors）
- `src/a2a/coordinator/agent_registry.py` — `sensor_types`、`agent_card_digest`、`agent_card_available` 和排序去重 parser
- `src/a2a/coordinator/server.py` — runtime scope bootstrap + `WS_REGISTER` 成功拉卡后的 active-scope re-sync
- `src/a2a/coordinator/memory/__init__.py`
- `tests/test_agentcard_memory_projection.py`
- `tests/test_worker_callback_signing.py`（只保留/扩展现有 card fixture，避免改签名契约）
- `tests/test_memory_projections.py`
- `tests/test_environment_state_provider.py`

**实施步骤：**
1. RED：card parser 的 capability/sensor normalized contract；失败拉卡不得投影空列表。
2. GREEN：scope bootstrap 在 `configure_memory()` 的 runtime-created hook 中遵循“activate scope → snapshot online available cards → attach receipt sink”的顺序；每个 worker 生成 `capability`/`sensor_type` list field，`provenance=registry`、`domain=embodied`。
3. 新 worker 首次注册成功后，仅对 active scope 写能力快照；重复注册（断线重连）不摄入；same digest 走 duplicate/no-change，不能 revision storm。
4. 不增加 `agent_card_changed` WS 消息，不增加 worker 运行期 add-skill API；若 D2 改为真正实时动态，需要新方案、消息认证和独立 Phase 0。

**Exit：** 新 run 的 read-port Embodied 段能看到 capability（来自新 worker 注册 bootstrap）；重复注册（重连）零重复摄入；AgentCard fetch 失败不把“未知”持久化成“无能力”；sensor_type 恒空（SAR 无 sensor，机制验证走 fixture）。

### Phase 4：反思 source window + long-term shadow trigger

**目标：** 在滚动与 terminal committed snapshot 上生成可审计、尚未注入 Context 的 published 产物（validator 通过即同事务 published，shadow 下仅落库不注入）；先验证存储与模型边界。

**Create：**
- `sar_orch/long_term_reflection.py` — SAR terminal wiring 与已有 provider/model config 的 adapter
- `sar_orch/eval/long_term_memory_quality.py` — 只读合规/证据质量 evaluator；指标（单 run 版，2026-08-12 冻结）：source traceability rate、forbidden-truth violation 恒 0、同 key 冲突率、supersede 链长度、反思延迟/token 统计

**Modify：**
- `sar_orch/coordinator.py` — 仅当 `long_term_mode != off` 时组装 store/service（root 由 `MemoryConfig.memory_root` 派生）
- `sar_orch/experiment.py` — `run_experiment()` 参数、CLI `--long-term-mode`（root 自动 run-local，无独立参数）、滚动触发点 + terminal invoke 顺序
- `src/a2a/coordinator/memory/reflection.py` — bounded source collector、function-call response validator、typed result
- `tests/test_long_term_reflection.py`
- `tests/test_long_term_memory_quality.py`

**实施步骤：**
1. 滚动与 terminal 反思均复用 `materialize_compatibility_artifacts()` 的 scope/revision 解析（经原子快照 API），构建一致 source snapshot；不调用 Barrier、EventStore、truth recorder、raw LLM trace。
2. 先持久化/claim `reflection_run`，释放 SQLite 锁后才调用 `ReflectionModelPort`；结果写回时再次校验证据 refs、redaction、truth scan 和 project scope。
3. 反思在独立线程**异步**执行：触发时若已有进行中的反思则 coalesce 跳过；validator 通过即同事务写 **published** 产物/audit/质量 artifact（candidate 仅审计轨迹）；`long_term_mode=shadow` 下不注入 Context；`off`：零 long-term DB I/O。terminal 收尾反思先 drain 进行中的滚动反思（join）再取终局快照；join 带超时（`long_term.config` `[timeout] reflection_sec`，默认 60s），超时写 typed timeout status 且不阻塞 run 退出；结果写入 `run_metrics`/terminal memory summary 的 typed reflection status（失败不中止 run，不篡改既有 acceptance artifact，不回写 source run store）。
4. 真实模型未通过 function-call smoke 时，保持 shadow 或 off；禁止普通 JSON 文本 fallback。

**Exit：** 同 run 内多次反思：重复反思零重复、同 `memory_key` 更新走 supersede；异步执行下触发 coalesce 生效（进行中反思不重入）；两个不同 scope（run）的长期数据互不可见；恶意 truth/伪造 ref/模型漏字段零长期内容写入；quality evaluator 能 `mode=ro` 产出 artifact。

### Phase 5：Coordinator-only long-term read-port 注入

**目标：** 在通过 shadow 证据后，将 published 长期记忆作为一个受预算约束的 Environment State section 交给 coordinator（注入 run 内后续轮次）；worker 仍不可见。

**Modify：**
- `src/Agent/environment_state.py` — `long_term_memory` section DTO/rendering；Freshness 元数据携带 project long-term revision
- `sar_orch/environment_state_provider.py` — optional long-term read port、coordinator-only ACL、预算/section priority
- `sar_orch/coordinator_state_provider.py` — 注入 project long-term reader 到 coordinator provider
- `sar_orch/coordinator.py` — 传递 service/read port
- `tests/test_long_term_environment_state.py`
- `tests/test_environment_state_provider.py`
- `tests/test_environment_state_acl.py`
- `tests/test_coordinator_state_provider.py`
- `tests/test_context_snapshot.py`（若 Context snapshot/section fixture 有固定 section 集）

**实施步骤：**
1. 扩展纯 renderer：为长期条目使用单独格式，不复用 `relevant_events` 的 sequence formatter。
2. `viewer_role=coordinator` 才读 `published`，worker 的 sections/evidence/read response 均无长期条目；未配置 long-term store 时 section 缺失或空，绝不报错。
3. long-term revision 必须参加 provider freshness/可观测 metadata；新增同 env_step 下 project revision 变更的回归，避免 Context cache 看不到更新。
4. token budget 下丢弃顺序钉死：Task > Spatial > Embodied > **Long-term** > Temporal > Freshness（2026-08-12 冻结）；长期段为固定上限的摘要，低预算时可被完全裁剪并留 `TRUNCATED`，不可突破 token limit。
5. 在 `long_term_mode=shadow` 时不渲染长期段，避免污染现有 shadow compare；`read` 失败依旧触发现有 read-port rollback 语义。

**Exit：** coordinator fresh view 有且仅有已 published、可证据追溯的条目；worker ACL negative tests 证明零泄漏；same-step long-term revision 变化能得到新 view；read failure 没有混合 legacy/canonical 内容。

### Phase 6：文档、独立评估与 rollout gate

**Modify（最后且仅在代码验收后）：**
- `docs/system_docs/memory.md` — 用真实实现替换“已知缺口”；由于当前文件 dirty，只三方合并本 feature 的 hunk
- `AGENTS.md` — 增加 long-term CLI/验收命令（若已稳定）
- `sar_orch/experiment.py` terminal summary schema（如 Phase 4 未已覆盖）
- `.hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入.md` — 标记实际完成项与证据，禁止预先写完成

**Exit：** 文档与实际默认值/路径/ACL 一致；没有新 Truth source；README/待办不再保留已被代码推翻的 heartbeat/battery 表述。

## 5. 验收与验证阶梯

### 5.1 每个 Phase 的通用验证

```bash
# 每次最终编辑后，针对本 Phase 的文件集执行；真实命令以实现时的文件名为准。
env PYTHONPATH="src:$PYTHONPATH" uv run pytest <phase-test-files> -q
uv run --with ruff ruff check <changed-src-files> <changed-test-files>
git diff --check
git status --short
```

- Phase 0：记录 intentional RED 的测试名/异常类型；剩余基线回归必须 GREEN。
- Phase 1+：先运行该 Phase RED 子集，确认是契约期望失败；最小实现后转 GREEN；后续 Phase 的 ImportError/AssertionError 仍须保持 RED。
- 每个 Phase 由父 agent 独立读实际改动、重跑命令、检查 untracked 文件；不以 subagent 自报替代验收。
- 不执行 commit/push/格式化全仓库；用户决定提交时机。

### 5.2 focused suite（Phase 6 前）

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_memory_contracts.py tests/test_memory_scope.py \
  tests/test_memory_ingestor.py tests/test_memory_projections.py \
  tests/test_memory_online_truth_boundary.py tests/test_memory_supervision_ingest.py \
  tests/test_auto_observation.py tests/test_coordinator_push_callback.py \
  tests/test_agentcard_memory_projection.py tests/test_memory_embodied_telemetry.py \
  tests/test_long_term_memory_contracts.py tests/test_long_term_memory_store.py \
  tests/test_long_term_reflection.py tests/test_long_term_environment_state.py \
  tests/test_long_term_memory_quality.py \
  tests/test_environment_state_provider.py tests/test_environment_state_acl.py \
  tests/test_coordinator_state_provider.py -q

uv run --with ruff ruff check src/ sar_orch/ tests/
env PYTHONPATH="src:$PYTHONPATH" uv run pytest -q
```

记录实际 pass/skip/fail 数，不沿用历史基线数字。

### 5.3 真实模型 smoke（Phase 4 → Phase 5 的硬门）

在临时 absolute long-term root 和 fixture source snapshot 上，以最终 production adapter 连续运行 5 次：

- 每次都必须生成合法 function call，所有 ref 都属于输入窗口；
- 每次都必须通过 redaction/truth scan；
- 故意漏字段/伪造 ref/含 truth term 的 response 必须被拒绝且零长期条目；
- 记录实际 model/provider/config 来源、成功数、拒绝类别、token/延迟；
- 少于 5/5 有效时，保持 `shadow`，不得启用 `long_term_mode=read`，修复路径只能是 function-call binding/模型选择/输入可见性，不能补造模型输出。

### 5.4 框架层真实 run 矩阵

每个会改变生产写入或 Context 的 rollout Gate，串行跑 `5 scenes × {2,4} agents × seed 42 = 10` 组，`max_steps=20`；每 run 长期库随 run-local 目录新建，不接触用户已有库。先做 `long_term_mode=shadow`，read gate 批准后再重复 `read` 矩阵。

```bash
set -euo pipefail
root="sar_orch/results/long_term_memory_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$root"
root="$(realpath "$root")"

for scene in 1 2 3 4 5; do
  for agents in 2 4; do
    run="$root/scene_${scene}_agents_${agents}"
    env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
      uv run python sar_orch/experiment.py \
      --scene "$scene" --agents "$agents" --seed 42 --max-steps 20 \
      --mode semantic --memory-read-mode read_port \
      --long-term-mode shadow \
      --log-dir "$run"
  done
done
```

最终 `read` 矩阵把最后一个 mode 改为 `read`。每组必须检查（注：shadow 阶段即产生反思真实模型调用成本，属预期，2026-08-12 确认）：

- run-local DB 中 Embodied position/inventory 和 registry capability 实际落库；
- run-local 长期库中 reflection run/status/support 可追溯，未配置/拒绝路径零内容写入；
- `memory_acceptance.json` 的 `worker_busy`、`task_not_routable_yet`、`unknown_task_id` 均为 0；
- `coverage`、`transport_rate`、error counts 均收集；
- `long_term_memory_quality.json` 存在且只读 evaluator 的 source traceability/forbidden-truth violation 指标有效；
- `long_term_mode=read` 的 coordinator Context 有 published-only 内容，worker Context/HTTP worker view 无长期条目；
- 任意 schema/secret leak/truth-boundary/framework error 失败即 Gate 不通过，不能以 pytest 绿替代真实证据。

## 6. 风险、回滚与明确不做项

| 风险 | 预防/回滚 |
|---|---|
| 长期库误共享/误删/路径漂移 | 独立文件随 run 生命周期、scope 隔离、`off` 默认、拒绝相对路径/URI；删除随 run 日志清理策略 |
| 反思模型幻觉/漏字段 | function-call + evidence subset validator + supersede/幂等去重；shadow 先行；无 fallback |
| benchmark 并发 lock | 长期库独立 busy timeout/retry/unique idempotency；不把网络调用包进 DB transaction；并发 store regression |
| telemetry 乱序覆盖 | mandatory step + H1 priority + older/missing-step tests；无 step 不写字段 |
| AgentCard fetch 失败被误写为“无能力” | `agent_card_available` gate；minimal registration 只保留 transport 可用性，不投影能力 |
| Context 膨胀/泄漏 | coordinator-only ACL、published-only、token budget、worker negative tests、shadow/read split |
| 偷改 scope close / legacy semantics | V1 不调用 `close_scope()`；不改 `memory_read_mode` rollout/rollback；任何 lifecycle finalization 另立 ADR |
| 现有 dirty documentation 被覆盖 | 文档最后改，三方合并限定 hunk；当前 user-owned `memory.md` 其他改动不触碰 |

**明确不做：** vector/RAG、自动删除/retention purge、worker-visible长期策略、raw chain-of-thought 存储、battery 合成、未有 producer 的实时 AgentCard mutation、**断线重连的能力变更同步（V1）**、对 Barrier/真值的在线读取、对 MissionRuntime 的任何反向写入、**跨 run 长期记忆查询/注入/聚合（跨 run 复用走 skill/文档沉淀）**。

## 7. Gate 与执行协议

1. **G0（设计冻结）：** 用户确认 D1–D9，特别是 telemetry 权威性、run-local 长期库、单 run 发布语义（validator 通过即生效）、function-call 模型与 V1 动态范围。
2. **G1（H1 扩展）：** 若 D1 修改 `position`/`inventory` source priority，必须补充 H1 projection contract card 并对当前 source hash 做 fresh review；未批准则 telemetry 不实施。
3. **G2（长期写入）：** Phase 0–4 GREEN + function-call 5/5 smoke + truth-boundary/lock/idempotency 审查通过，才能从 `off` 到 `shadow`。
4. **G3（长期 read）：** shadow 10-run 矩阵零指定框架错误、真实 artifact/ACL/quality evidence 通过，用户明确授权后才 `long_term_mode=read`。
5. **G4（正式可用）：** read 10-run 矩阵 + full pytest/ruff + 独立验收完成；文档状态由证据更新，非实现 subagent 更新。
6. **范围声明：** 本方案长期记忆为单 run 语义；跨 run 复用通过 skill/文档沉淀，不属 Memory 系统范围（用户拍板 2026-08-11）。

执行时主 agent 只做分期协调、进度记录和独立验收；每个 Phase 只授权一名实现 subagent，任何 Major/Blocker 转为上述 invariant + test + Gate。当前没有任何 Gate 已批准，也没有实施开始信号。

## 8. 本方案的证据基线

- 当前 run-local DB 路径与配置：`src/a2a/coordinator/memory/contracts.py:510-541`、`sar_orch/coordinator.py:357-391`
- callback atomic projection seam：`src/a2a/coordinator/server.py:1576-1628`、`src/a2a/coordinator/memory/ingestor.py:335-438`
- callback telemetry 被丢弃的具体点：`src/a2a/coordinator/server.py:98-142`
- structured self state 的真实生产点：`sar_orch/barrier.py:369-408`、`sar_orch/tools/worker/_barrier_helpers.py:21-77`、`src/a2a/worker/sink.py:90-116`
- AgentCard 生成/解析/注册：`src/a2a/worker/a2a_server.py:164-199`、`src/a2a/coordinator/agent_registry.py:110-162`、`src/a2a/coordinator/server.py:2236-2279`
- run-terminal hook 与无生产 scope close：`sar_orch/experiment.py:173-255,735-760`、`src/a2a/coordinator/memory/store.py:382-390`
- read-port/ACL/渲染：`sar_orch/environment_state_provider.py:68-342`、`src/Agent/environment_state.py:73-220`、`sar_orch/coordinator_state_provider.py:149-234`
- 真相隔离：`src/a2a/coordinator/memory/contracts.py:48-115`、`tests/test_memory_online_truth_boundary.py:101-176`

**计划阶段验证声明：** 以上是只读源码与已完成探索的证据；本方案没有运行 pytest、真实 LLM smoke、SAR experiment 或写入任何业务代码。