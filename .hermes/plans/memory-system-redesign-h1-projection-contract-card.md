---
title: H1 Projection Semantics Lock — Online Evidence Contract Card
status: DRAFT — USER_DECISIONS_CAPTURED / FRESH_REVIEW_PENDING
design_parent: .hermes/plans/memory-system-redesign-design.md
implementation_target: /home/wyh/daily_work/LLaMAR-memory-redesign
branch: feat/memory-redesign
target_head: e1a5a01392c814ddcbc33f5e17aa5cad5d9e8fe6
source_scope: src/ + sar_orch/ + pyproject.toml clean at card freeze
human_gate: H1
---

# H1 Projection Semantics Lock

> 本卡是 Phase 3 的人工审核交付物，不是实现批准。只有针对本卡和主设计精确 bytes 的 fresh independent `APPROVE`，才能开始 Phase 3。

## 1. 目标与不可越过的边界

在线 Memory 只能依据受认证 Worker evidence、明确允许的静态 AgentRegistry metadata 和控制/监督事实形成 Spatial / Embodied 当前视图；它不能读取、查询、初始化于或由任何仿真器/物理世界 oracle 真值修正。

```text
Worker local sensor/tool result / telemetry / report_observation
  → signed callback → MemoryIngestor → Temporal evidence
  → deterministic reducer → Spatial / Embodied projection
  → MemoryReadPort → Environment State

SARBarrier / simulator ground truth
  ├─ worker-local sensing adapter（只向该 Worker 暴露其允许的局部观测）
  ├─ explicit oracle/debug mode（不属于 semantic/read_port Memory）
  └─ run 终止后的 evaluator-only truth trace（只读比较，不回写）
```

### H1-INV-1：在线无 oracle 真值

在 `semantic`、`shadow`、`read_port` 的在线路径中，`MemoryIngestor`、reducer、MemoryReadPort、EnvironmentStateProvider 和默认 Context 不得调用、接收或派生自：

- `SARBarrier.get_env_snapshot()`、`env.controller` 或 simulator checker；
- 场景 object priors、ground-truth object names、coverage truth；
- oracle-mode `query_sar_state` 输出；
- post-run evaluator 的 raw truth trace、逐项判分结果或任何 correction/backfill。

允许的在线来源是：Worker 本地经过权限限制的传感器/工具输出、authenticated Worker telemetry/observation、授权 peer evidence、AgentRegistry 的静态 capability metadata，以及 MissionRuntime/TaskWatchdog 的控制/监督事实。Worker evidence 的上游在仿真中可由 Barrier 模拟，但 Coordinator/Memory 只能看见 Worker 返回的规范化结果，不能直接读取全局环境；AgentRegistry 不得提供 battery、position、inventory、availability 等动态场景字段。

违反 H1-INV-1 时，拒绝本次领域写入，产生脱敏 diagnostic；不得创建 Temporal event、projection、revision、outbox 或 Context 内容。

## 2. 已锁定的事实所有权

| 领域 / 字段 | 当前事实 owner | 可接受在线 evidence | 不可接受在线来源 |
|---|---|---|---|
| agent 身份、角色、能力、传感器类型、静态 endpoint metadata | `AgentRegistry` / AgentCard | 注册配置、AgentCard | heartbeat、LLM 推断、oracle |
| 连通性、heartbeat、通信健康 | `WorkerRegistry` | authenticated heartbeat、watchdog、已认证 transport telemetry | AgentRegistry 镜像状态、oracle |
| 电量、定位质量、节点本体 telemetry | `WorkerRegistry` 的动态 Embodied view | authenticated Worker telemetry / structured local tool result | Coordinator 直接读 Barrier、LLM summary |
| position、inventory、场景对象事实 | Spatial / Embodied projection | authenticated Worker sensor/tool result、`report_observation`、授权 peer evidence | Barrier/simulator direct read、ground truth、Coordinator inference |
| current dispatch、任务物理状态 | `MissionRuntime.PhysicalDispatch` | MissionRuntime control receipt | Temporal/Spatial/Embodied Memory mutation |

`AgentRegistry.status` / heartbeat 是迁移期 compatibility mirror，不得作为新 Embodied 动态事实的胜出来源。`WorkerRegistry` 是动态视图 owner，但每个字段仍须保留上游 evidence、时间、置信度和 source class；它不是凭空生成物理真相。

## 3. 已锁定的归约与版本规则

### 3.1 比较顺序

同一 scope、同一实体、同一字段的候选 evidence 按下列顺序处理：

1. `scope_id / runtime_epoch` fence；不匹配先拒绝；
2. 对需要场景时序的字段比较 `env_step`；缺失 `env_step` 的 Worker report 可留作 Temporal evidence，但不得覆盖已有当前物理字段；
3. 在同 step 比较**该字段**的 source class / source priority；不能按整条 event 一刀切；
4. 再比较 confidence；
5. `sequence / event_id` 只用于稳定存储/展示排序，不得把同级矛盾事实伪装成胜出真相。

推荐 source class 不是全局总序，而是 field policy：

| 字段族 | 在线优先级（高 → 低） | 说明 |
|---|---|---|
| position / inventory / 场景对象属性 | authenticated structured Worker sensor/tool result → structured `report_observation` → authorized peer report | 都是 Worker evidence；没有 Barrier/oracle 候选 |
| battery / localization quality / node telemetry | authenticated Worker telemetry → structured local tool result → Worker report | WorkerRegistry 保存 resolved 动态视图与 field-level provenance |
| availability / heartbeat | authenticated heartbeat / watchdog → Worker transport telemetry → Worker self report | `AgentRegistry` status 只做 compatibility mirror |
| capability / sensor type | AgentRegistry / AgentCard | 静态 metadata，不参与动态字段裁决 |

同一事件可按字段部分 material；`AgentRegistry` 不参与动态字段裁决；oracle 永远不在候选集合中。

### 3.2 冲突、迟到与 relation

- 旧 `env_step` evidence：append 到 Temporal；可追溯地关联实体并标记 `ignored_out_of_order`，但不改变当前字段。
- 同 step、同字段、同 source priority、同 confidence 且值不同：字段进入 `CONFLICTED`；默认 Environment State 不得把任一候选当成确定事实。
- 不同 priority 的矛盾：高 priority 字段值胜出；低 priority claim 以 `superseded_by_higher_authority` 保留审计，不等同 `CONFLICTED`。
- 所有 accepted event 的 relation 必须可从 Temporal 追溯到 Spatial/Embodied；默认 `evidence_refs` 只展示支撑当前值或当前冲突的 evidence，非 material evidence 只在 audit / TemporalQuery 中可查。

### 3.3 三层版本语义

| 名称 | 语义 | 何时变化 |
|---|---|---|
| `memory_revision` / canonical revision | Phase 2 已有 scope 级 canonical bundle 版本；用于 receipt、outbox、export 与审计 | 每个 accepted canonical bundle |
| `SpatialEntity.revision` / `EmbodiedNode.revision` | 当前可见投影的实体级 revision | 当前字段、freshness、冲突或默认可见 evidence 改变 |
| `EnvironmentStateView.snapshot_revision` | 当前 viewer、ACL、所选实体和可见投影的版本 | viewer 实际会看到的 projection 内容变化 |

`as_of_sequence` 指向最后一个**实际参与形成当前可见投影**的 Temporal event；不是任意最新收到 event。内部 reducer checkpoint 可以前进，但不能仅因隐藏审计 evidence 让 Context 重渲染。

## 4. 7 个可追溯案例

每个案例都将成为 `tests/test_memory_projections.py` 或明示的 Phase 5 evaluator test 的 RED 来源。案例中的 event 必须先经过认证、principal/dispatch/scope 校验和 RedactionPolicy。

### C1 — Worker structured observation 正常形成当前事实

**输入：**active scope 内，Worker `alice` 的结构化 local sensor/tool result 在 `env_step=8` 报告 position、inventory 或空间对象；source class 对该字段有权威，confidence 合法。

**预期：**Temporal event append；canonical `memory_revision` 前进；对应 Spatial/Embodied 字段、实体 revision、`as_of_sequence`、material evidence 和授权 viewer 的 `snapshot_revision` 前进；不修改 MissionRuntime task state。

### C2 — 迟到旧 Worker evidence 只进入 Temporal 审计

**输入：**当前 position/inventory 已由 `env_step=8` 的 material evidence 形成；之后到达同一 active scope 的 `env_step=5` Worker evidence。

**预期：**Temporal 与 canonical revision 前进；当前字段、实体 revision、`as_of_sequence`、默认 `evidence_refs` 和 Environment State snapshot revision 不变；审计可查 `ignored_out_of_order` relation；不得因该 event 重渲染 LLM 当前状态。

### C3 — 同级矛盾 Worker evidence 显式冲突

**输入：**同一 `env_step`、同一字段、同 source priority、同 confidence 的两个 Worker evidence 提供不同值。

**预期：**Temporal/canonical revision 前进；字段标记 `CONFLICTED`，两个 evidence 在默认 conflict/evidence view 可见；实体 revision、`as_of_sequence` 和 authorized snapshot revision 前进；`FRESH` 与 `CONFLICTED` 正交，不能用 sequence 选一个“真值”。

### C4 — 跨 epoch / closed scope 的迟到 callback 零领域写入

**输入：**`S3(epoch=3)` 已关闭，`S4(epoch=4)` 为新 active scope；合法格式的旧 worker callback 解析到 S3。

**预期：**返回 typed `scope_closed`；S3/S4 均无 Temporal、projection、relation、revision、outbox 或 Context 变化；旧 scope 不 reopen，旧 evidence 不自动导入新 scope。

### C5 — 字段级 Worker evidence policy，不再使用 Barrier 裁决

**输入：**同一 active scope 的 authenticated Worker evidence 同时携带：

- 对 inventory 的低 priority 自由 report，和已有同 step 更高 priority structured local tool result 矛盾；
- 对 battery/localization 的 authenticated telemetry，且该 telemetry 是该字段最高在线来源。

**预期：**inventory 保留 higher-priority Worker tool result，并留下 `superseded_by_higher_authority` 审计；battery/localization 更新为 telemetry 值；同一 event 可对不同字段部分 material；node revision/snapshot revision 因后两字段变化前进；每个字段保留独立 provenance/evidence。

### C6 — secret 与 oracle 真值均不越过 Ingestor

**输入：**有效签名 Worker callback 同时含 secret/HMAC/Authorization/Cookie/mailbox 原文，以及伪装为 observation 的 `oracle` / `ground_truth` provenance 或 direct world snapshot 字段。

**预期：**敏感字段在任何 Temporal payload、projection、relation、JSONL candidate、Context、exception、audit export 中无原文；oracle/direct-world candidate 被 H1-INV-1 拒绝，零领域写入；安全 diagnostic 仅保留脱敏 reason/digest。

### C7 — terminal 后 evaluator-only truth 比较，永不回写

**输入：**运行终止（completed、timeout、failed 或 cancelled）后，harness 关闭本 scope 的在线写入，冻结 canonical Memory manifest 与 evaluator-private truth trace。

**预期：**独立 evaluator 只读比较 Worker evidence / projection 与同 step truth，产出版本化 quality artifact：

- worker report quality（field precision、可观测范围内 recall、staleness、false-claim / conflict calibration）；
- memory integration quality（projection field match、coverage、freshness/conflict calibration、evidence traceability）；
- `target_head`、Memory manifest digest、truth-trace digest、evaluator version、终止状态。

该 evaluator 不得向 Memory 写 TemporalEvent、projection、revision、outbox、export 或 Context；raw truth trace 不得进入 Agent 可读目录、prompt、日志或 compatibility artifact。若将质量结果用于未来运行的 source policy，只能通过显式、版本化、离线审批的 policy 更新；不得同 run 反馈。

C7 的 aggregate schema 固定为：

```text
worker_report_quality:
  evaluated_report_count: integer
  observable_field_count: integer
  correct_field_count: integer
  false_claim_count: integer
  stale_report_count: integer
  precision: number|null
  recall: number|null

memory_integration_quality:
  evaluated_projection_field_count: integer
  correct_projection_field_count: integer
  stale_projection_count: integer
  conflicted_field_count: integer
  traceable_field_count: integer
  precision: number|null
  recall: number|null
  conflict_precision: number|null
  evidence_traceability_rate: number|null
```

所有 `null` rate 必须仅表示分母为零并同时带 `metric_status=not_applicable`；不能用 null、空列表或缺字段伪装成零错误。

## 5. 实现闭环：owner、gate、测试

| 决策 | Durable owner / 禁止边界 | owning phase | 正向断言 | 反向断言 |
|---|---|---|---|---|
| H1-INV-1 online truth isolation | `NormalizedProjectionInput` provenance allowlist；MemoryIngestor/reducer 不持有 Barrier/oracle dependency | Phase 3 | Worker evidence 形成 projection | direct Barrier/ground_truth candidate 零领域写入 |
| field-level source policy | Embodied/Spatial field provenance + relation outcome | Phase 3 | C5 partial-material update | lower-priority claim 不覆盖高优先级字段 |
| canonical vs projection revisions | existing `memory_revision` + entity/view revision contract | Phase 3 | C1 / C3 visible update 前进 | C2 hidden late evidence 不刷新 view |
| evaluator-only truth | evaluator-private trace + immutable run/Memory manifest | Phase 5 | C7 生成 digest-bound quality artifact | evaluator 回写 Memory 或 candidate 可读 truth trace 必须失败 |

Phase 3 必须明确修改 `memory/contracts.py`、`memory/store.py`、`memory/ingestor.py` 和 `sar_orch/coordinator.py`：前者定义/保存规范化 evidence 与 projection outcome，后者移除 online semantic map 的 Barrier priors / ground-truth 注入。Phase 5 必须新增独立 projection-quality evaluator，并通过 `sar_orch/experiment.py` 在 terminal 后执行；它的 raw truth trace 必须在 candidate/Memory 读写边界之外。

## 6. H1 人工审批清单

审批者只需确认以下问题均为“是”：

1. 在线 Memory 永不把 simulator/barrier truth 当作来源或修正手段；
2. AgentRegistry 是静态能力 owner，WorkerRegistry 是带 provenance 的动态视图 owner；
3. Worker evidence 的比较顺序、迟到处理、同级冲突和字段级 policy 符合预期；
4. canonical revision 与 projection/view revision 分离；
5. 跨 epoch callback 零领域写入，秘密和 oracle truth 均不泄露；
6. terminal 后真值只产生隔离的质量评测 artifact，不回写本次或当前运行 Memory；
7. C1–C7 足以转写为 Phase 3/5 正反测试。

审批记录必须在外部 review record 中绑定：本 card 路径、主设计路径、精确 SHA-256、目标 HEAD、source-scope 状态、验收结论（`APPROVE` 或 `REJECT`）及允许的案例/allowlist。无此记录即 H1 `PENDING`。
