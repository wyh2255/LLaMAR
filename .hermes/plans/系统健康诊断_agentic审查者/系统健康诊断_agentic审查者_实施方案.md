---
文档类型: 实施方案（设计冻结稿，G0 待审）
日期: 2026-08-13
状态: 提案，未实施。本文件只冻结建议的目标、契约、分期和验收；不构成代码实施授权。依照项目实施门控，只有用户明确"开始实施"后才进入 Phase 0。
绑定: 本文件 G0 审查时以 SHA-256 冻结
依赖前置: 长期记忆 G0–G4（已 APPROVE，`long_term_mode=read` 正式可用）、canonical Memory H1–H3（已关闭，`memory_read_mode=read_port` 默认）
探索基线: 代码事实探索 01/02/03（同目录，只读，2026-08-13，commit `32bfe57`）
---

# 系统健康诊断 · agentic 审查者 — 实施方案

## 0. 30 秒摘要

长期记忆反思者升级为 **agentic 系统健康审查者**：每 N 步带只读查询工具（投影/时间线/supervision/control）多轮推理，产出**诊断**（方向性纠错建议），以 `### System Health` 平级新段注入 coordinator Context（在线闭环，改变决策）。coordinator 决策文本以「决策即事件」进入 canonical temporal 流水，成为审查者的输入。诊断独立短命 store，**不进投影事实域**（结构性隔离），与 worker evidence 无冲突通道。

## 1. 目标

1. **决策即事件**：coordinator dispatch 语义内容（任务文本，非 thinking）作为新 TemporalEvent 类型 `coordinator_decision.*` 进入 canonical temporal 流水，成为可审计、可被反思消费的一等事件。
2. **agentic 审查者**：反思者从「单轮 function-call 记忆提炼」升级为「多轮只读查询 + 方向性纠错」。只读工具集四类：投影 / temporal 流水 / supervision / control journal。禁读 barrier/oracle（H1 精神内扩展）。
3. **诊断在线闭环**：诊断产物（findings + suggestions）以 `### System Health` 段注入 coordinator Context，直接参与下一轮决策；worker 永不可见（复用现有 ACL 三道门）。
4. **防误导三重锁**：诊断 source_ref 禁指向既有诊断（防回声室）；置信度阈值门控注入；诊断不进投影事实域（结构性隔离，B6）

## 2. 边界（用户拍板，2026-08-13）

| # | 边界 | 内容 |
|---|---|---|
| B1 | 决策原文粒度 | dispatch 语义内容（任务文本），**非** LLM 完整输出/thinking |
| B2 | 诊断消费方 | coordinator 在线闭环（改变决策）；不给人/评测器消费面 |
| B3 | 诊断产物落点 | 独立 store/表（短命生命周期），不复用 long_term 表 |
| B4 | 注入形态 | `### System Health` 平级新段，coordinator-only，预算与长期记忆段独立 |
| B5 | 防回声室 | 诊断 source_ref 只允许指向四类在线证据，禁指向既有诊断；置信度阈值门控 |
| B6 | 权威性 | **诊断不进投影事实域**（结构性隔离，强于优先级让位）：诊断只落独立 store + `diagnosis.audit` temporal audit 事件，不进 spatial/embodied 投影 reducer，FIELD_SOURCE_POLICY 不扩。原「reflection 最低优先级让位」表述废止（allowlist 撤销后已无让位对象，2026-08-13 修订） |
| B7 | 触发时机 | 保持现状（每 N 步滚动 + terminal），不新增事件驱动触发 |
| B8 | 工具集边界 | 只读四类查询（投影/时间线/supervision/control）；禁 barrier/oracle/truth |
| B9 | H1 精神 | 决策事件与诊断 audit 均为协调层自身行为记录，非真值通道；allowlist/FIELD_SOURCE_POLICY 均不扩展（独立审查 03 后修订）；agentic 工具集禁读 barrier/oracle/truth |
| B10 | 不触碰 | canonical 既有表零 DDL（探索已证 temporal_event 无 CHECK、payload 自由 JSON）；不触碰既有四类事件 producer；不触碰并行 team 功能文件 |

## 3. 契约

### 3.1 决策事件契约（DecisionEventV1）

- **event_type**：新顶层前缀 `coordinator_decision.*`，五种取值：
  - `coordinator_decision.assign_task` — dispatch 语义内容（content 全文）、who（worker 名）、correlation_id、worker_task_id
  - `coordinator_decision.cancel_task` — related_task_id
  - `coordinator_decision.reply_to_help` — related_task_id + response_preview
  - `coordinator_decision.activate_plan_node` — related_task_id（DAG 逻辑节点激活；participants/objective/assignments 由 MissionGraph 声明读取，事件只记激活行为）
  - `coordinator_decision.update_plan` — 声明式计划提交摘要（tool_start 的 args 为完整 plan 列表；**前后对比在 tool_start 拿不到**——diff 是 MissionGraph.replace 的执行结果，update_plan.py:67-90，独立审查 03 实证）。事件记提交的计划节点列表摘要（logical_id/participants/deps/objective）
- **写入点**：`SARCoordinator._log_send_message` 四分支（coordinator.py:148-209，含 activate_plan_node）；update_plan 走 `_on_router_event` tool_start 分支新增 elif（coordinator.py:253-257，现只路由 send_message/query_sar_state）；else 泛型 send_message 不进 canonical
- **actor_id**：`"Coordinator"`；**env_step**：tool_start 时刻 `barrier._step_counter`（即 `_log_send_message` 的 step 参数，coordinator.py:240），放 payload JSON
- **provenance/allowlist（2026-08-13 审查修订）**：**allowlist 不动**。`append_temporal_event`（store.py:695）无 provenance 参数——provenance gate 只作用于 `ingest_projection` 的投影输入（ingestor.py:620）。决策事件与 `control.*`/`supervision.*` 同构：纯 temporal 流水事件，不经投影域、不需 provenance。原方案「allowlist 7→8」为过度设计，撤销（见独立审查 03）
- **幂等**：复用 canonical JSON SHA-256 幂等键 + idempotency_ledger claim（ingestor.py:43-95）
- **脱敏**：写入前显式套 RedactionPolicy（现 `_log_send_message` 写原文无脱敏，探索 01 确认坐标/名字不会被现有规则误伤，但 secret 模式必须过滤）
- **零 DDL**：temporal_event 无 event_type CHECK、payload 为自由 TEXT JSON（store.py:134-155）——只加新取值，不动表结构

### 3.2 诊断输出契约（DiagnosisCandidateV1）

- 独立于 LongTermMemoryCandidateV1；新 validator `validate_diagnosis_response`（复用 reflection.py:134-211 的纯校验段：形状/去重/truth 词/ref 可验证性/confidence）
- 字段：`diagnosis_key`（系统派生 = SHA-256(canonical_json(scope+target+kind+statement))）、`kind`（diagnosis）、`target`（coordinator|worker:<id>|system）、`finding`（语义内容）、`suggestion`（方向性建议）、`confidence`（0-1，注入阈值默认 0.6，config 流出）、`source_refs`（只允许指向四类在线证据的 event_id，**禁指向既有诊断**）、`policy_version`
- 短命生命周期：run 结束即归档，不跨 run；不参与长期记忆 supersede 链
- **防回声室（2026-08-13 审查强化）**：三重防护——①诊断 source_ref 只允许指向四类在线证据的 event_id，禁指向既有诊断；②诊断 audit 事件用**独立顶层前缀 `diagnosis.audit`**（不在 `_REFLECTION_EVENT_PREFIXES` 四前缀内，收集器天然不收 → 诊断永不进反思输入窗口）；③时间线只读工具对诊断 audit 事件做视图过滤（供评测/审计用，不进审查者输入）。原 D9 的 `evidence.diagnosis_audit` 命名撤销——`evidence.*` 前缀会被收集器回收形成回声
- **诊断循环与滚动反思的关系（2026-08-13 审查澄清）**：**并存，不替代**。既有滚动反思（long_term_reflection.py:183）继续产出长期记忆；诊断循环是同一触发点上的**第二通道**（独立 store、独立窗口消费、coalesce 单飞槽位共享但结果互不阻塞）。原「反思者升级为」表述废止——避免隐性改变 G4 已批准功能

### 3.3 注入契约（System Health 段）

- 新 section `system_health`，渲染标题 `### System Health`（_SECTION_HEADINGS，environment_state.py:123-133）
- 注入门控：与 long_term_memory 同 if 块并列分支（provider:374-375），`_is_system and long_term_mode == "read"` **且 `diagnosis_inject_enabled`（独立旋钮，A2 拍板）** 且诊断存在。独立旋钮理由：消融实验（开/关诊断对比）需要在不切换 long_term_mode 的情况下单独关掉诊断注入
- 预算档：SECTION_PRIORITY 插在 embodied 与 long_term 之间（Task > Spatial > Embodied > **System Health** > Long-term > Temporal）；threshold **默认 3（与 long_term 同档）**，可由 `[diagnosis] section_budget_threshold` 配置流出（**2026-08-16 R3 修订**：预算档位配置面，D9 精神不硬编码；默认 3 行为不变，配置为非 3 时自动解除「与 long_term 同档」语义）；TRUNCATED 特判泛化（provider:421-424 的 long_term_dropped 逻辑扩为多段）
- 渲染：一行一诊断，`target: finding → suggestion (confidence=N)`

## 4. 关键流程

```
每 N 步滚动触发（experiment.py:829-832，保持现状）
  └─ coalesce 单飞（long_term_reflection.py:267-277，daemon 线程）
       ├─ [新增] 决策事件已随 dispatch 进入 temporal 流水（_log_send_message 写入）
       ├─ 快照 scope_event_snapshot（含 coordinator_decision.*，收集器前缀 +1）
       ├─ agentic 诊断循环（新，见 §5）：
       │    LLM 多轮（上限 max_rounds，默认 3）
       │      ├─ 轮内工具调用 → 只读四类查询（投影/时间线/supervision/control）
       │      └─ 终止：产出 DiagnosisCandidateV1 批 / 放弃
       ├─ validate_diagnosis_response（防回声室 + truth 词 + ref 可验证性）
       └─ 落独立诊断 store + 命中阈值诊断进 sections["system_health"]
            └─ EnvironmentStateProvider._apply_budget → renderer `### System Health`
                 └─ coordinator 下一轮 LLM Context 含诊断段（改变决策）
```

诊断写入 canonical temporal（`diagnosis.audit`，独立顶层前缀）仅作 audit 事件，不进投影 reducer 的事实域；诊断的「事实性」由独立 store 承载，与 spatial/embodied 投影彻底结构性隔离（B6）。

## 5. agentic 审查者设计

- **执行模型**：同步 while 循环（与现有 ReflectionModelPort 同步封装一致，reflection.py:250-256），不引入 async；每轮一次 LLM function-call（复用 complete_with_function_call，换诊断工具 schema 无需改解析——按 tools[0] 动态匹配）
- **只读工具集（4 件，新封装层）**：数据读取层已全部存在（探索 02 确认）——MemoryReadPort 投影（environment_state_provider.py:109-137）、temporal 流水（store.py:835-840 / snapshot 845+）、supervision 计数（store.py:952-961）、control journal 读 API（store.py:518-528）。**缺的是工具封装 + 注册**，本 Phase 补齐
- **轮次上限与超时**：`[diagnosis] max_rounds=3 / diagnosis_sec=90 / inject_enabled=true / min_confidence=0.6 / section_budget_threshold=3` 流出 long_term.config（2026-08-16 R3 修订：+section_budget_threshold 预算档位配置面，默认 3）；terminal drain 60s 冲突由 §7 风险 R4 处置
- **输出**：仍是 function-call 契约（不自由文本）；agentic 的是推理过程

## 6. Phase 划分

| Phase | 内容 | Gate | 状态 |
|---|---|---|---|
| P0 | 契约卡 + RED 测试（零生产改动）：DecisionEventV1 写入契约、DiagnosisCandidateV1 validator、注入段渲染、四件只读工具 schema | — | 未开始 |
| P1 | 决策即事件 producer（_log_send_message 四分支 + tool_start update_plan elif + 收集器前缀 +1 + 脱敏；**allowlist 不动**） | G1 | 未开始 |
| P2 | 诊断独立 store + validator + 防回声室 + 置信度门控 | G2 | 未开始 |
| P3 | agentic 审查循环（四件只读工具封装 + 多轮循环 + config 流出） | G2（合并审查） | 未开始 |
| P4 | System Health 注入段（provider + renderer + 预算档 + ACL 复验 + TRUNCATED 泛化） | G3 | 未开始 |
| P5 | 真实模型 smoke（诊断 3/3）+ read 矩阵 10-run 对比 + 文档收口 | G4 | 未开始 |

## 7. 风险

| # | 风险 | 缓解 | 状态 |
|---|---|---|---|
| R1 | 诊断误导决策（无独立真值锚） | 置信度门控 + 结构性隔离（诊断不进事实域）+ 注入样本人工可查 + read 矩阵前后对比 | 设计缓解 |
| R2 | 回声室（诊断引诊断） | source_ref 白名单禁诊断 + validator 拒绝 + 测试守护 | 设计缓解 |
| R3 | 决策文本敏感泄漏（secret 进入 dispatch 内容） | 写入前显式 RedactionPolicy（现有 logs 写原文的缺口一并堵） | 设计缓解 |
| R4 | agentic 多轮撞 terminal drain 60s 超时，结果丢弃 | 滚动触发全异步；terminal 时降级单轮或接受丢弃（诊断是增强非必需）；diagnosis_sec 上限 90s 与 drain 的时序冲突在 P3 实测确认 | 待 P3 验证 |
| R5 | token 预算：诊断段挤占长期记忆段 | 独立 threshold 档；TRUNCATED 语义明确；生产预算 78976 恒 ≥ 门槛，实际不裁剪（探索 03） | 低 |
| R6 | audit 事件回声/前缀冲突（诊断事件被反思窗口误收） | 独立顶层前缀 `diagnosis.audit`（不在收集器四前缀内）；时间线工具视图过滤；测试守护「诊断永不进反思窗口」 | 设计缓解 |
| R7 | 多一轮 LLM 成本（agentic 3 轮 vs 单轮） | 诊断与反思同模型配置；触发频率保持现状（每 5 步 + terminal）；每 run 预估 +6-10 次调用 | 接受 |

## 8. 验收

- **测试层**：P0 契约卡 RED→GREEN（沿用 test_memory_* 风格，新增 tests/test_system_health_*.py 族）
- **框架层**：5 scenes × 2 agent counts 交叉验证（max_steps=20, seed=42），零框架错误码（worker_busy/task_not_routable_yet/unknown_task_id 全 0）
- **真实模型**：诊断 smoke 3 次独立运行 3/3 通过（诊断产出 + source_refs 可验证 + 零 truth 词）
- **矩阵**：read 10-run（5 scenes × agents {2,4} × seed 42）诊断注入实证（llm_request 含 System Health 段比例）+ 与 G4 基线 cov/tr 对比（无退化判定同 G4 口径）
- **防回声室**：validator 拒绝诊断引诊断的测试守护；confidence 阈值边界测试
- **ACL**：worker 视图零 System Health 段（937 请求实测口径复用）
- **文档**：memory.md / AGENTS.md / 待办状态收口

## 9. Gate 与人工审查点

| Gate | 内容 | 人工判断 |
|---|---|---|
| G0 | 本方案冻结（hash）+ 决策清单 D1–D10 逐条拍板 | D1–D10 语义与边界 |
| G1 | 决策事件通道（P1 产物） | 事件写入路径与幂等；脱敏路径；event 采样（provenance/allowlist 不再涉及，已撤销） |
| G2 | 诊断 store + agentic 循环（P2/P3 产物） | 防回声室语义；只读工具边界；多轮超时实测 |
| G3 | 注入与权威性（P4 产物） | 预算档；ACL 复验；TRUNCATED 语义 |
| G4 | 正式可用（P5 产物） | read 矩阵对比 + smoke + 文档 |

### 9.1 人工审查点（R0–R4）

人工只审查不可由自动化/独立 agent 替代的**语义、持久化、LLM 暴露和正式可用边界**；不为每个 Phase 增设人工阻塞。本 feature 与长期记忆 feature 的区别：**诊断会改变决策**（在线闭环），审查重心从「事实正确性」转移到「**建议安全性**」——诊断错误不再是记忆污染，而是决策误导。

| 审查点 | 对应 Gate | 人工判断 | 不可自动化理由 |
|---|---|---|---|
| R0 | G0 | D1–D10 语义与边界 | 决策事件覆盖范围/诊断权威性是产品语义，无客观真值 |
| R1 | G1 | 决策事件写入路径与幂等；脱敏路径；event 采样（allowlist/provenance 不再涉及） | 决策事件是「协调层行为记录」还是「世界状态断言」的边界需人工确认 |
| R2 | G2 | 防回声室语义；只读工具边界；多轮超时实测结果 | 回声室是语义性风险，测试只能守机制不能守语义 |
| R3 | G3 | 预算档；ACL 复验；TRUNCATED 语义；结构性隔离守护 | 诊断段优先级影响 LLM 决策质量，是产品权衡 |
| R4 | G4 | read 矩阵对比 + smoke + 文档 | 正式可用宣告，同 G4 惯例 |

**审查点详情**：

- **R0（2026-08-13 已过）**：D1–D10 全拍板 + A1–A6 全拍板（见下方清单）。
- **R1（G1）材料**：`coordinator_decision.*` 事件真实采样（5-10 条 JSON，人工查：语义决策、无 thinking、无 secret、脱敏生效）；写入路径证据（append_temporal_event 调用点、幂等测试）；收集器前缀 +1 diff + 反思窗口含 decision 事件测试；`diagnosis.audit` 永不进反思窗口测试。**判断**：①决策事件是否被误认为真值通道（应是「协调层自身行为记录」）；②采样语义意外；③五类型覆盖完整性。
- **R2（G2）材料**：真实模型诊断 smoke 3 次独立运行的诊断样本（人工读 3-5 条：finding 站得住？suggestion 方向合理？幻觉性纠错？）；防回声室测试；多轮轮次分布实测；terminal drain 时序实测。**判断**：①样本质量=建议安全性核心证据；②防回声室语义充分性（间接引用怎么算）；③超时冲突程度决定 D8 降级策略是否够用。
- **R3（G3）材料**：coordinator Context before/after 样本（含 `### System Health` 段）；worker 视图零诊断段（ACL 复验）；预算裁剪 TRUNCATED 测试；结构性隔离守护测试（诊断不进投影 reducer）。**判断**：①诊断段是否喧宾夺主；②SECTION_PRIORITY 档位合理性；③结构性隔离（B6）在代码里的表达成立。
- **R4（G4）**：read 10-run 矩阵 + smoke + 文档收口。**额外维度**：诊断注入前后 cov/tr 对比——诊断显著降低性能则打回（本 feature 独有验收维度）。

**顺序与拒绝处理**：R0（已过）→ P1 → R1 → P2/P3 → R2 → P4 → R3 → P5 → R4。Gate 打回区分「契约修订（回主方案，重算 hash）」与「实现缺陷（直接修复）」。R2 拒绝不得进入 P4（注入面建立在诊断质量之上）。涉及 H1 边界的行为必须停在 R1/R3。

### G0 决策清单（待用户逐条拍板）

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| D1 | 决策事件前缀 | 无 | `coordinator_decision.{assign_task,cancel_task,reply_to_help,activate_plan_node,update_plan}`；else 泛型 send_message 不进 canonical | **已拍板（2026-08-13）**：补 activate_plan_node（DAG 节点激活）+ update_plan（计划修改，独立 tool_start 写入点），其余按建议 |
| D2 | 决策内容粒度 | logs 写全量 content | assign_task 用 content 全文（语义内容）；reply_to_help 用 content[:200]（与 logs 同口径） | **已拍板（2026-08-13）** 按建议 |
| D3 | 诊断字段集 | 无 | diagnosis_key/kind/target/finding/suggestion/confidence/source_refs/policy_version（§3.2） | **已拍板（2026-08-13）** 按建议 |
| D4 | 注入阈值 | 无 | confidence ≥ 0.6 才注入，`[diagnosis] min_confidence` 流出 config | **已拍板（2026-08-13）** 按建议 |
| D5 | 诊断预算档 | 无 | SECTION_PRIORITY 插 embodied 与 long-term 之间；threshold **默认 3 同 long-term，可由 `[diagnosis] section_budget_threshold` 配置（2026-08-16 R3 修订）** | **已拍板（2026-08-13）** 按建议（配置面 2026-08-16 追加） |
| D6 | 诊断 store 位置 | long_term.sqlite3 同库不同表 vs 独立文件 | 独立文件 `<memory_root>/diagnosis/diagnosis.sqlite3`（模板 = LongTermMemoryStore，long_term.py:194-379） | **已拍板（2026-08-13）** 按建议 |
| D7 | agentic 轮次上限 | 无 | max_rounds=3 / diagnosis_sec=90，`[diagnosis]` 段流出 config | **已拍板（2026-08-13）** 按建议 |
| D8 | terminal 超时策略 | drain 60s | 滚动异步不受限；terminal 诊断降级单轮（仍超时则丢弃，诊断非必需） | **已拍板（2026-08-13）** 按建议 |
| D9 | 诊断 audit 事件 | 无 | 诊断产出同时写 canonical temporal `diagnosis.audit`（独立顶层前缀，不进反思收集器；仅供 audit/评测，不进事实域） | **审查修订（2026-08-13）**：原 `evidence.diagnosis_audit` 命名撤销（会形成回声），改为 `diagnosis.audit` |
| D10 | 触发频率 | 每 5 步 + terminal | 保持现状，不新增事件驱动 | **已拍板（2026-08-13）** 按建议 |

### 修订决策项（2026-08-13 独立审查后新增，待用户拍板）

独立审查 01/03（1 Blocker / 7 Major / 8 Minor 中未消解部分）暴露以下方案级问题，需在 G0 追认或修订：

| # | 决策项 | 审查发现 | 建议 | 需确认 |
|---|---|---|---|---|
| A1 | G4 边界追认 | 收集器前缀 +1 属 G4 审批记录「不授权」清单 L43 的「反思输入范围扩展」 | 显式追认：G4 当时禁的是「已有长期记忆注入反思输入」，本次加的是「coordinator_decision 决策事件进反思窗口」，语义不同；但形式上需用户追认 G4 边界扩展 | **已拍板（2026-08-13）**：按建议追认 |
| A2 | 注入门控开关 | System Health 段绑定 `long_term_mode == "read"` 无独立开关 | 与 long_term 共用 `read` 门控（诊断是长期记忆 read 模式的增强面，不单独开旋钮）；若未来要独立关诊断，再补 `[diagnosis] enabled` 配置 | **已拍板（2026-08-13）**：**开启独立旋钮**——`[diagnosis] inject_enabled`（默认 true，config 可配），供消融实验开/关诊断注入；门控 = `_is_system and long_term_mode=="read" and inject_enabled` |
| A3 | update_plan 事件语义 | 前后对比在 tool_start 拿不到（diff 是 MissionGraph.replace 结果） | 记声明式提交摘要（提交的 plan 节点列表），不改 tool_result 回调 | **已拍板（2026-08-13）**：按建议（声明式） |
| A4 | 诊断与反思并存确认 | 「升级为/保持现状/同模型配置」三处表述冲突 | 并存：滚动反思不动，诊断是第二通道（已写入 §3.2） | **已拍板（2026-08-13）**：按建议（并存） |
| A5 | severity 字段 | D3 字段集无 severity 但渲染格式曾引用 | 从渲染格式移除（字段集不扩）；若诊断分级有实际需求再单列决策 | **已拍板（2026-08-13）**：按建议（不加） |
| A6 | smoke 强度 | 诊断 smoke 3/3 弱于长期记忆 5/5 先例 | 统一为 3 次独立运行 3/3（与 G2 长期记忆 smoke 5/5 是不同功能的证据强度，诊断有 read 矩阵兜底） | **已拍板（2026-08-13）**：按建议（3/3） |



