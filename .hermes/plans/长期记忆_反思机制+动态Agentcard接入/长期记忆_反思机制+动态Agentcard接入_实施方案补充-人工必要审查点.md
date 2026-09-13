# 长期记忆实施方案补充：人工必要审查点

> **性质：** 本文件是 [`长期记忆_反思机制+动态Agentcard接入_实施方案.md`](长期记忆_反思机制+动态Agentcard接入_实施方案.md) 及其原子快照/跨 Run 存储补充的强制审查约束。
>
> **绑定主方案 SHA-256：** `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`
> **创建时间：** `2026-08-11T23:20:55+08:00`
> **状态：** 设计提案；当前没有任何人工 Gate 已批准，也不构成实施授权。

## 1. 审查原则：最少、必要、不可替代

人工审查只覆盖无法由 pytest、静态检查、独立 agent 验收或真实 artifact 自动替代的决定：

1. 改变 online fact 的**权威来源与语义**；
2. 允许系统向 run-local 长期库**持久化**反思内容（新文件 + schema migration）；
3. 允许长期内容进入 LLM 的 **Coordinator Context**；
4. 宣告功能正式可用并接受持久化/回滚/保留边界。

以下事项**不是**人工审查点：单个测试实现、代码风格、普通 bug 修复、测试失败定位、Phase 内部设计细节、常规 parent/subagent 独立验收。它们仍由实施者 + 父 agent 的 RED→GREEN、范围审计和独立复验闭环处理。

每个人工点只有三种有效结论：`APPROVE`、`REJECT`、`REVISE`。`APPROVE` 只放行对应的下一边界，**不自动启动实施**；仍需用户明确说“开始实施”才能从当前允许的 Phase 继续执行。

## 2. 人工必要审查点

| 审查点 | 对应 Gate | 触发时机 | 人工必须判断的内容 | 放行后允许做什么 | 未批准时必须保持 |
|---|---|---|---|---|---|
| R0：设计/权限冻结 | G0 | P0 之前 | D1–D9：telemetry authority、AgentCard V1 动态范围（新 worker bootstrap）、sensor tag（机制-only）、run-local 长期库、`off|shadow|read`、单 run 发布语义、增量窗口/异步执行、`long_term.config` + `.env` `reflection_*` 模型契约 | 仅允许在收到“开始实施”后进入 P0 test-only RED | 不写测试/生产代码/DB，不跑真实模型或实验 |
| R1：投影权威扩展 | G1（条件） | P0 RED 完成、P2 前；仅 D1 改变 position/inventory source priority 时触发 | `worker_telemetry` 是否可高于现有来源；step 缺失/乱序/伪造身份的行为；battery 仍无生产者 | 实施 P2 的 source-policy 与 callback telemetry 写入 | 不提升 telemetry authority；不得用别的 provenance 偷换已冻结语义 |
| R2：长期记忆写入准入 | G2 | P0–P4 GREEN、第一次 `off → shadow` 前 | run-local 长期库、scope 隔离、schema migration、atomic source snapshot、函数调用 5/5、candidate/published（单 run 语义）、无 truth/CoT 写入 | 对本 run 长期库启用 `long_term_mode=shadow` | 保持 `off`；长期库零 I/O |
| R3：LLM Context 暴露准入 | G3 | shadow 10-run 证据完成、`shadow → read` 前 | Coordinator 实际 Context 样本是否有用且不过量；worker ACL 零泄漏；published-only/budget/rollback 行为；质量 artifact 与错误码 | 启用 coordinator-only `long_term_mode=read` | 保持 shadow；长期内容不得进入任何 LLM Context |
| R4：正式可用/保留边界 | G4 | read 10-run + full suite + 独立验收后 | 长期库随 run 日志的保留/备份/清理责任、migration/recovery、read_port rollback、真实指标与剩余风险 | 标记功能正式可用并更新系统文档/进度 | 保持受控 read 或 shadow；不宣告 release/不改文档状态为完成 |

## 3. 为什么这样划分

### 3.1 按“不可逆边界”划分，而不是按文件或 Phase 数量划分

| 审查点 | 不能只靠自动化决定的原因 | 代码/契约依据 |
|---|---|---|
| R0 / G0 | D1–D9 决定“什么算 agent 自身事实”“谁有权写长期内容”“什么内容能成为 run 内长期指导”。这些是产品/研究语义与权限选择，pytest 只能证明实现符合某个选择，不能替人选择该选择。 | `FIELD_SOURCE_POLICY` 已按字段定义 authority（`src/a2a/coordinator/memory/contracts.py:153-199`）；当前 AgentCard 也没有运行期动态 API（`src/a2a/worker/a2a_server.py:164-199`）。 |
| R1 / G1（条件） | 把 `worker_telemetry` 提升为 position/inventory 的权威来源，会改变同 step 冲突时的可见当前事实；这不是普通实现细节，而是 H1 reducer 的领域语义。若 D1 不改变 policy，则无需制造一次人工审批。 | reducer 按 env_step、field source priority、confidence、冲突顺序裁决（`src/a2a/coordinator/memory/projections.py:95-134`）；当前 telemetry 尚未列为 position/inventory authority（`contracts.py:158-178`）。 |
| R2 / G2 | 首次从 `off` 进入 `shadow` 会向 run-local 长期库写入反思内容，涉及新文件、schema migration、保留与错误恢复；即使 Context 尚未暴露，持久化已经是不可逆外部副作用。 | 长期库独立于 canonical 16 表（`store.py:46-206`），需要独立 DB 文件、migration 与 fail-closed；路径一律由 `MemoryConfig.memory_root` 派生（`sar_orch/coordinator.py:357-385`、`contracts.py:510-541`）。 |
| R3 / G3 | `shadow → read` 改变 Coordinator 每轮真正看到的 Environment State，进而改变 LLM 决策；“内容是否足够、有无误导、是否过量”需要人类从实际 Context 样本判断，不能由单元测试替代。 | Context 会把 read-port view 作为 user message 注入（`src/Agent/router_agent/context.py:918-977`）；provider 在 worker 侧实施 ACL（`sar_orch/environment_state_provider.py:264-342`），必须以真实 shadow artifact 证明。 |
| R4 / G4 | 正式可用意味着接受长期库随 run 日志的保留/备份/手动清理责任及 rollback 边界；这是运行治理和责任归属，不是“测试全绿”能自动授权的结论。 | 主方案明确不做自动 purge，并要求 `read_port → legacy` rollback 不删除 canonical/long-term 数据；正式状态须绑定真实 read 10-run、recovery 与文档证据。 |

### 3.2 为什么审查顺序是 R0 → R1（条件）→ R2 → R3 → R4

1. **先冻结语义，再写契约。** R0 先确定 D1–D9，P0 的 RED 才有唯一目标；否则测试会把尚未拍板的选择伪装成事实。
2. **authority 先于 producer。** 只有 D1 修改 source priority 时，R1 才在 P2 前锁定 H1 语义；先接 callback 再讨论谁更权威会让落库事实和 Context 可见结果反复变化。
3. **先 shadow 写入，再暴露给 LLM。** R2 放行的是 run-local 长期库的持久化验证；R3 只能在 shadow 的真实 10-run 样本、ACL 和质量证据出现后判断“是否值得注入”。
4. **最后才接受运行责任。** R4 必须等 read-mode 的真实矩阵、recovery 和 rollback 证据齐全；它不回头重审每个内部实现，而是审查正式运行边界。

### 3.3 为什么不在每个 Phase 后设置人工点

- P0 的 API/RED 合同、P1 的 migration/reopen、P2/P3 的 producer/idempotency、P4 的 validator 都有确定性不变量，可由实现 subagent、父 agent 独立复验、focused/full pytest、ruff 和 disposable probe 证明。
- 如果每个 Phase 都要求人工确认，会把可重复的工程判断错误上移为人工瓶颈，反而弱化真正需要人判断的 authority、长期持久化、LLM exposure 和 release 责任。
- 因此每个 Phase 仍有严格的机器/父侧验收；人工点只位于**语义或外部影响由低风险模式跨到高风险模式**的转换处。任何 Major/Blocker 仍会立即回退为 invariant + test；不等到下一次人工 Gate 才处理。

## 4. 每个审查点的最小证据包

### R0 / G0：设计冻结包

- 绑定文件及 SHA-256：主方案、原子快照补充、跨 Run 存储补充、本文件；
- 代码事实探索总览与三份分域探索；
- D1–D9 决策卡：推荐值、替代值、拒绝/延后时的 fail-closed 行为；
- 明确授权边界：G0 不授权生产代码、真实模型调用、真实实验、schema migration、Context cutover、commit/push。

### R1 / G1：H1 扩展包（条件）

- fresh H1 projection contract card，绑定当前 source hash；
- P0 RED/guard 测试记录：same-step source priority、older/no-step、identity binding、battery absence、truth denial；
- `worker_telemetry` payload 的生产/传输/提取链路图；
- 结论必须明确“批准 authority 的字段集合”，禁止泛化为所有 telemetry 字段。

### R2 / G2：长期库 shadow 写入包

- LongTermMemoryStore migration/reopen/unknown-version/rollback/lock-retry 测试；
- atomic `scope_event_snapshot` 与 reflection idempotency 的测试和可复算 digest；
- 真实 adapter 的 5 次 function-call smoke：模型/配置来源、成功分布、拒绝样本、零 fallback 证明；
- 长期库路径（`<memory_root>/long_term/`）、scope 隔离、candidate/support 审计样本；`long_term.config` 损坏 fail-closed 负例（unparseable/非法值 → typed 错误 + mode 强制 off）；
- truth/secret/raw CoT zero-write 负例与 quality evaluator `mode=ro` 证据。

### R3 / G3：Context 暴露包

- shadow 10-run（5 scenes × `{2,4}` agents × seed 42）汇总与每 run artifact；
- coordinator Context 的 redacted before/after 样本、token budget/truncation 行为；
- worker HTTP/view/Context ACL negative evidence；
- `memory_acceptance.json` 指定框架错误码为 0、`long_term_memory_quality.json`、rollback audit；
- 明确哪些 memory status/字段会进入 Context，哪些永久不进入。

### R4 / G4：正式可用包

- read 10-run 矩阵、focused/full pytest、ruff、独立验收结果；
- 长期库随 run 日志的权限/迁移/recovery 验证及手动 retention 责任；
- `read_port → legacy` rollback 演练，不删除 canonical 或 run-local long-term 数据；
- 已知限制、未解决风险、是否允许 future in-process AgentCard mutation 的明确结论。

## 5. 记录与拒绝处理

1. 人工结论落入 `<feature>-<gate>-approval-record.md`，必须绑定 fresh HEAD、全部输入 SHA-256、审查包路径、用户原话、allowlist 与明确未授权项。
2. approval record 写入后，才同步进度文档的：当前状态、Phase 表、Gate 表、决议详情、验收勾选和变更日志。
3. `REJECT/REVISE` 若改变 D1–D9 或长期存储契约，属于**契约修订**：回到主方案/补充并重新 hash，旧审批自动失效；若只发现实现不符合已冻合同，属于**实现缺陷**：回到对应 Phase RED 修复。
4. 未产生 fresh approval record 时，任何人/agent 都不得将 Gate 标为通过或绕过 lower-risk mode。
