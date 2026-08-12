# 长期记忆反思机制 + 动态 AgentCard 接入 — 实施进度记录

> **用途：** 跟踪以下设计文件的 Phase/Gate 状态与验收证据：
> - [`长期记忆_反思机制+动态Agentcard接入_实施方案.md`](长期记忆_反思机制+动态Agentcard接入_实施方案.md)
> - [`长期记忆_反思机制+动态Agentcard接入_实施方案补充-原子快照.md`](长期记忆_反思机制+动态Agentcard接入_实施方案补充-原子快照.md)
> - [`长期记忆_反思机制+动态Agentcard接入_实施方案补充-跨Run存储与迁移.md`](长期记忆_反思机制+动态Agentcard接入_实施方案补充-跨Run存储与迁移.md)
> - [`长期记忆_反思机制+动态Agentcard接入_实施方案补充-人工必要审查点.md`](长期记忆_反思机制+动态Agentcard接入_实施方案补充-人工必要审查点.md)
>
> **边界：** 本文件只记录状态、Gate 与可复核证据；设计语义只在上述实施方案及补充中变更。不得用本文件替代 H1 contract card、approval record 或真实验收产物。可复用经验另入 `经验.md`/skill，不在此累积。
>
> **当前状态：** **P0–P4 已通过；P4→P5 smoke 5/5 通过；shadow 10-run 矩阵完成（2026-08-12）。** G2 审查包已准备，等待用户审批（人工审查点 R2）。真实实验（read 模式）、schema migration（canonical）、Context cutover、commit/push 仍一律禁止。
>
> **更新约定：**
> - 进度三态按“实施中 / 验证中 / 可提交”汇报，并附真实产物路径或命令输出；
> - 没有独立测试和真实 smoke 证据，任何 Phase 不得标记为“通过”；
> - Gate 必须记录日期、绑定 HEAD/hash、审查材料、用户决议、授权范围与遗留风险；
> - Gate 打回时区分“契约修订（回设计）”与“实现缺陷（直接修复）”。

---

## 1. 当前位置

- 阶段：**G0/G1 已批准（2026-08-12），P0–P4 已通过，P4→P5 smoke 5/5 与 shadow 10-run 已完成**；**G2（long_term_mode off→shadow）待审批（人工审查点，2026-08-12 停）**；P5/P6 仍依赖后续 Gate 与用户明确授权。
- 当前 HEAD：`54137050a8d5332e7f81e24b9a1f45857bd8455f`（`feat/memory-redesign`）。
- 设计证据冻结：
  - 主方案 SHA-256：`169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`
  - 原子快照补充 SHA-256：`54e259eabeaef0f08b506b86af12a97d185cec2f0427d2325dc18a2f0c029a98`
  - 跨 Run 存储/迁移补充 SHA-256：`0146bf810c589f132fa6ab9749d385e44c308d3aa50ea687a194fc17234a1ea8`
  - 人工必要审查点补充 SHA-256：`218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d`
  - 代码事实总览：[`README_代码事实探索.md`](README_代码事实探索.md)（SHA-256 `e4beeaf897426662d1e75c531bf7e6d2d0dfd441e87279f244463e8ef88508f8`，历史探索快照未改动）；
  - 分域探索报告（历史快照，hash 冻结 2026-08-12）：01 `af5251f37d7dcb43915d4d4580ae9e4af9ff377a8fe911e8962779bee734ada0`、02 `0b6392f7a864e9206d8eba9508011aff7692eea670c5f384477ba4af1d7734d6`、03 `46ceacdc482bf97af76ec688c6b1604045e1b5ac433e6cad47c5cf6d36fa28a6`。
- 当前工作区：`src/`、`sar_orch/`、`tests/` 无 tracked dirty path；`docs/system_docs/memory.md` 是本 feature 之外的既有用户 dirty 文件，实施时只能三方合并本 feature hunk，不能覆盖。
- 下一个动作：G2 审查包已准备（P0–P4 证据 + smoke 5/5 + shadow 10-run），等待用户审批。审批通过后：P5（coordinator-only read-port 注入）实施 → G3 审批材料。
- 已结束历史：既有 Memory H1–H3/read_port 迁移是本功能的前置基线，不自动构成长期记忆/telemetry/AgentCard Phase 的通过证据；其历史细节不在此重复。

## 2. Phase 状态表

状态枚举：未开始 / 实施中 / 验证中 / 待审(G?) / 通过 / 阻塞(附原因)。

| Phase | 内容 | 状态 | 主要产出（路径） | 验证证据 | 完成日期 |
|---|---|---|---|---|---|
| P0 | 契约卡与 RED 测试；生产代码零改动 | **通过**（2026-08-12） | `tests/test_memory_embodied_telemetry.py`、`tests/test_agentcard_memory_projection.py`、`tests/test_long_term_*.py` 等 7 新建 + 7 现有测试文件增补 | 106 intentional RED（15 AssertionError + 18 ImportError + 10 AttributeError + 62 ModuleNotFoundError + 1 TypeError 未来API）+ 179 GREEN；零 collection ERROR；7 增补文件零回归；`:!tests/ :!docs/` diff 为空；ruff 4 条全为基线债务 | 2026-08-12 |
| P1 | 独立长期记忆 kernel、run-local 独立文件、project+scope 隔离、versioned schema migration | **通过**（2026-08-12） | `src/a2a/coordinator/memory/long_term.py`（850 行）、`reflection.py`（228 行）、`contracts.py`（+279）、`__init__.py`（+44）、`sar_orch/long_term_reflection.py`（22 行最小 D9 re-export） | contracts+store 46/46 GREEN；reflection 9/22（RED 全为 P4 预期：16 ImportError + 6 AttributeError）；回归 scope+ingestor 25/25；focused suite 67 RED 全合法；ruff 新增零错误；F1（D9 quality_enabled 键映射断裂）独立证实+修复，F2-F6 Minor 一并修复+5 防回归测试 | 2026-08-12 |
| P2 | 已认证 callback 的 Embodied telemetry（position/inventory/step） | **通过**（2026-08-12） | `sar_orch/barrier.py`（+2）、`sar_orch/tools/worker/_barrier_helpers.py`（±4）、`src/a2a/coordinator/server.py`（+193：`_extract_worker_telemetry_with_provenance`/`_telemetry_inventory_parseable`/`_normalize_telemetry_projection_inputs` + callback 同事务合并）、`src/a2a/coordinator/memory/contracts.py`（D1 FIELD_SOURCE_POLICY） | 136 passed（6 文件：P0 冻结 15 项全绿 + P2 e2e 3 用例 + M1 防回归 1 用例）；独立 review 无 Blocker（M1 Major + m1 Minor 修复，m2/m3/m4 记录）；ruff P2 新增零错误（48 基线债务逐文件 HEAD 对比）；diff 干净；心跳零改动 | 2026-08-12 |
| P3 | AgentCard registry projection：新 worker 注册 bootstrap（不含断线重连同步） | **通过**（2026-08-12） | `src/a2a/coordinator/memory/registry_projection.py`（新建 186 行）、`agent_registry.py`（AgentInfo +3 字段、parser/digest、contains）、`server.py`（hook bootstrap + `_ingest_registry_snapshot` + 首次注册判定）、`a2a_server.py`（sensors 参数）、`memory/__init__.py`（exports） | 200 passed + 4 P5 预期 RED（前台实测）；独立 review 无 Blocker（M1 静态配置未认证能力投影 + M2 首次判定失效 + m1-m3/m5-m6 已修复，m4 记录）；ruff P3 新增零违规（29 基线逐行核对）；diff 干净；D2/D3 边界守（无 agent_card_changed、重连零摄入、sensor_type 空不写） | 2026-08-12 |
| P4 | 滚动+terminal 反思 source window、run-local shadow trigger、只读质量评估 | **通过（离线部分）**（2026-08-12） | `store.py`（scope_event_snapshot 原子快照 + supervision_event_count）、`reflection.py`（collector/cursor/run_reflection/事务）、`sar_orch/long_term_reflection.py`（trigger/drain/coalesce/D8 骨架）、`sar_orch/eval/long_term_memory_quality.py`（新建）、`coordinator.py`/`experiment.py`（接线） | 111 passed（P4 组）+ 196 passed（回归）+ 61 passed（experiment 导入面）；独立 review 无 Blocker（M1 空窗口跳过 + M2 D8 read 缺 key 拒绝 + M3-M7/M9 修复，M8 记录）；ruff P4 新增零违规（13 基线）；P0 冻结修正 3 处（父侧裁决，见变更日志） | 2026-08-12 |
| P5 | Coordinator-only long-term read-port 注入与 ACL/budget/cache 契约 | 未开始（依赖 P4；G3 审批在 P5 实现与 shadow 10-run 之后） | 计划修改 `environment_state.py`、`environment_state_provider.py`、`coordinator_state_provider.py` | 尚无 shadow/read diff、worker ACL negative、Context 实证 | — |
| P6 | 文档收口、focused/full suite、真实 run rollout、Gate 证据包 | 未开始（依赖 P0–P5、G4） | 计划更新 `docs/system_docs/memory.md`、AGENTS/待办状态 | 尚无 pytest、ruff、5 次真实模型或 10 组 run 证据 | — |

## 3. Gate 决议日志

| Gate | 审查内容 | 状态 | 决议摘要 | 日期 |
|---|---|---|---|---|
| G0 | D1–D9 设计冻结 | **通过**（2026-08-12） | 用户明确“根据相关文档可以开始实施计划了”= G0 最终确认 + 实施启动信号；仅授权 P0 test-only RED，不授权生产代码/真实模型/真实实验/migration/Context cutover/commit/push | 2026-08-12 |
| G1 | H1 projection semantics extension（条件 Gate） | **通过**（2026-08-12） | 用户逐项确认 C1–C5 全部决策后批准（APPROVE），原话“批准G1通过”；授权仅放行 P2，不自动启动实施；审批记录 `长期记忆_反思机制+动态Agentcard接入_G1-approval-record.md` | 2026-08-12 |
| G2 | `long_term_mode: off → shadow` | **待审批（审查包已备 2026-08-12）** | P0–P4 GREEN（111+196+61 passed）、function-call smoke 5/5（3 次独立运行）、shadow 10-run 全绿（框架错误码全 0、quality 指标全优、原子快照绑定一致）、独立 review 无 Blocker（M1 生产上下文已修复）；已知缺口：long_term_support.source_revision/event_digest 未填充（P1 移交项）、Charlie 他报 capability 观察点、m8 drain 表级 timeout 行记录 | — |
| G3 | `long_term_mode: shadow → read` | 待审批 | 需要 shadow 10-run 矩阵、ACL、质量 artifact、零指定框架错误 | — |
| G4 | 正式可用 | 待审批 | 需要 read 10-run 矩阵、full pytest/ruff、独立验收和文档一致性 | — |

### 人工必要审查点

人工只审查不可由自动化/独立 agent 替代的语义、持久化、LLM 暴露和正式可用边界；不为每个 Phase 增设人工阻塞。完整证据包、划分依据、顺序和拒绝处理见 [`实施方案补充-人工必要审查点`](长期记忆_反思机制+动态Agentcard接入_实施方案补充-人工必要审查点.md) §3–§5。

| 审查点 | 对应 Gate | 人工判断 | 当前状态 |
|---|---|---|---|
| R0：设计/权限冻结 | G0 | D1–D9 与未授权范围 | **已批准**（2026-08-12，见 G0 决议；仅授权 P0 test-only RED） |
| R1：投影权威扩展 | G1（条件，已触发） | telemetry source priority 与 H1 语义 | **已批准**（2026-08-12，APPROVE，C1–C5 全确认；记录见 `长期记忆_反思机制+动态Agentcard接入_G1-approval-record.md`） |
| R2：长期记忆写入准入 | G2 | run-local 长期库、migration、atomic snapshot、function-call、candidate/published（单 run） | 待审批 |
| R3：Context 暴露准入 | G3 | coordinator Context 样本、worker ACL、budget/rollback | 待审批 |
| R4：正式可用/保留边界 | G4 | read 矩阵、recovery、retention/rollback | 待审批 |

### 决议详情

**G0（通过，2026-08-12）** — 用户明确“根据相关文档可以开始实施计划了”（2026-08-12）即 G0 整体最终确认 + “开始实施”信号，批准 P0 test-only RED。

- 审查材料：主方案及三份强制补充（hash 见 §1）、[代码事实总览](README_代码事实探索.md)、三份分域探索报告。
- 用户原话（2026-08-12）：“根据相关文档可以开始实施计划了，你作为协调者和进度记录者，具体编码任务交给 subagent 完成。你做严格审查，如果你认为审查范围大也可以开另一个 agent review。”
- 授权范围：仅 P0 的 test-only RED 合同（tests/ 目录内新建与增补）；生产代码、真实模型调用、真实 SAR run、schema migration、Context cutover、commit/push 一律未授权。
- 放行边界：G0 批准只允许 P0 的 test-only RED；P1 起每个 Phase 仍须按方案完成 RED→GREEN 与父侧独立验收，G1/G2/G3/G4 仍待审批（截至 2026-08-12：G1 已通过，G2/G3/G4 仍待审批）。
- G0 拍板记录（2026-08-12 用户逐项确认）：
  1. D1：接受——`worker_telemetry` 升为 position/inventory 第一权威，必须带 `env_step`；battery/localization_quality/node_telemetry V1 不写 → **R1 触发**（需新 H1 contract card + fresh review）；
  2. D2：接受修正——"动态"= 新 worker 加入（首次注册）能力 bootstrap；断线重连的能力变更同步不在 V1；
  3. D3：接受 a)——SAR 虚拟仿真无 sensor，V1 只做机制（sensor_type 恒空，验证走 fixture）；
  4. D4/D5：接受（2026-08-11 单 run 语义）：run-local 长期库 `<memory_root>/long_term/long_term.sqlite3`、`off|shadow|read`、coordinator-only；
  5. D6：接受——validator 通过后同事务直接 published；supersede 更新；同 key 同 digest 零写入；
  6. D7：接受——增量窗口 + 窗口上限/触发节奏全部 `long_term.config` 可调；反思**异步**执行（coalesce 防重入，terminal join）；
  7. D8：接受——`.env` 独立配置口 `reflection_provider` / `reflection_api_key` / `reflection_api_base` / `reflection_model`，默认 `openai` / `deepseek-v4-flash`，缺 key fail closed；5/5 烟测；
  8. D9（新增）：可调参数集中到新建仓库根 `long_term.config`（ini，与 `.env` 平级）；
  9. 其余定义冻结：kind 枚举 `strategy|lesson|hazard|pattern|status`；`canonicalized(statement)` = lowercase → strip → 空白折叠 → 去首尾标点；质量指标单 run 版（source traceability / violation 恒 0 / 同 key 冲突率 / supersede 链 / 延迟与 token）；P2 后 shadow 对比方向反转属预期。
- 剩余待决：G0 整体最终确认（当前仍为待审批，未授予实施权限）。
- 放行边界：即使 G0 被批准，也只冻结设计；仍需用户明确“开始实施”才允许 P0 的 test-only RED。G0 不授权生产代码、真实模型调用、真实 SAR run、schema migration、Context cutover、commit 或 push。

**G1（通过，2026-08-12）** — 用户逐项确认审查包 §5 五个决策项后批准（原话："这轮5项决策都同意"、"好的，批准G1通过，可以准备一下写进进度文档然后，开放写下来的内容了。但是不要开始实施计划"）。

- 审查材料：`长期记忆_反思机制+动态Agentcard接入_G1-R1-H1-contract-card-worker-telemetry.md`（SHA-256 `1faeff92de86ebc44e4fef2cb42732491f416f3edfc9dc9b71465646e898b9a4`，绑定 HEAD `5413705`）。
- 决议摘要：C1 锁定 position+inventory；C2 接受同 step 自报优先；C3 接受 worker_id == agent name 约定；C4 确认无 step 零写入；C5 确认 battery 零写入。
- 授权范围：放行 P2（callback telemetry producer + source policy 调整，详见审批记录 allowlist）；**不自动启动实施**，P2 编码/验证仍需用户明确“开始实施”。
- 放行边界：不授权扩充心跳、battery 合成、覆盖 env_step fence、worker 直写 canonical DB、P2 之外的任何生产改动；P3–P6 仍依赖后续 Gate。
- 残留风险（用户接受）：telemetry 乱序覆盖（mandatory step + fence + P0 测试）、伪造身份（auth_dispatch 绑定）、自报与观测冲突（C3 CONFLICTED）、battery 误写（白名单 + 负例）、双实体（V1 接受约定）。

**G2–G4（待审批）** — 审查包尚不存在，必须在相应 Phase 真实产物完成后创建并绑定 fresh HEAD/hash；不得提前填入测试数、模型结果或 run 指标。

## 4. 验收标准勾选（主方案 §5–§7 与三份补充）

### 已有设计/证据

- [x] 三待办代码事实探索已完成并独立抽检：[`README_代码事实探索.md`](README_代码事实探索.md) + `代码事实探索_01/02/03_*.md`。
- [x] 主实施方案和三份强制补充已生成并 hash 冻结：§1 四个 SHA-256。
- [x] 最小人工必要审查点及其划分依据已绑定 G0–G4：[`实施方案补充-人工必要审查点`](长期记忆_反思机制+动态Agentcard接入_实施方案补充-人工必要审查点.md)；R0/R1 已批准（2026-08-12），G2–G4 仍待审批（审批记录见 §3）。
- [x] G0 决策 D1–D9 已逐项拍板并回写决议详情（2026-08-12）；**G0 已批准**（用户明确“开始实施”，2026-08-12），授权仅限 P0 test-only RED。
- [x] **G1/R1 已批准**（2026-08-12，APPROVE）：C1–C5 全确认，审批记录 [`长期记忆_反思机制+动态Agentcard接入_G1-approval-record.md`](长期记忆_反思机制+动态Agentcard接入_G1-approval-record.md)（绑定 HEAD `5413705` + 审查包 SHA-256 `1faeff92…b9a4`）；仅放行 P2，不自动启动实施。
- [x] 跨 Run 前提已只读核验：run-local DB 的实际路径/单 scope/`closed_at=NULL`/`user_version=3` 已检查；细节见跨 Run 存储补充 §1。
- [x] 进度写作未修改任何 `src/`、`sar_orch/`、`tests/` 文件；本文件创建时对应 dirty-path 检查为零。

### 未完成的实施/验证

- [ ] P0：全部 contract tests 已收集，intentional RED 的异常类型正确，非 Phase 0 回归 GREEN。✅ **2026-08-12 完成**：106 intentional RED（15 AssertionError + 18 ImportError + 10 AttributeError + 62 ModuleNotFoundError + 1 TypeError 未来API）+ 179 GREEN、零 collection ERROR、7 增补文件零回归；独立交叉审查（无 Blocker，10 Major 修复）+ 父侧复验；`:!tests/ :!docs/` diff 为空。移交 P1 已知项：测试自造 API（`simulate_read_error`/`force_schema_version` 等 9 个需按测试实现或 fault injection）、WAL/busy_timeout 直接断言、版本记录≠schema、D6 supersede 链、反思输入 redaction/长度上限覆盖缺口。
- [ ] P1：独立长期 store 的 run-local root、scope isolation、transactional migration、unknown-version、reopen/lock/retry 契约 GREEN。✅ **2026-08-12 完成**：contracts+store 46/46 GREEN（含 5 条 review 防回归）、reflection validator/DTO/protocol 9 项转 GREEN、回归 25/25；独立交叉审查（无 Blocker，F1 Major 修复 + F2-F6 Minor 修复）；移交 P2+ 已知项：store 层 `long_term_support.source_revision/event_digest` 列 P1 暂 NULL（P4 填充）、candidate memory_key 需 P4 wiring 用系统派生替换模型 key、canonicalize 句子标点集解释（保留内容括号）已按 P0 测试契约落地。
- [x] P2：callback telemetry 同事务落库；position/inventory 不乱序回退；不写 battery；H1-INV-1/脱敏均 GREEN。✅ **2026-08-12 完成**：136 passed（P0 冻结 15 项全绿）；独立 review 无 Blocker（M1 inventory=None/int/bool→空库存 claim 已修复 + m1 body_sha256 死参数删除 + 防回归测试）；已知记录项（m2）：telemetry evidence id 的 block digest 含 sink `ts`，同内容新 ts 重推产生新 evidence 行（reducer no_change，字段不受影响，与观测路径既有行为一致，非 P2 回归）。
- [x] P3：AgentCard capability/sensor snapshot、minimal registration 零空事实、scope bootstrap、新 worker 首次注册摄入/重复注册零摄入幂等 GREEN。✅ **2026-08-12 完成**：P0 冻结 17 契约全绿 + 8 P3 增补 + 2 修复测试；独立 review 无 Blocker（M1/M2 静态配置路径修复、m1 metadata 排除、m2 非 dict 容错、m3 内部排序、m5 显式处理；m4 canonical_json_bytes default 记录）；静态配置 agent 首注册后仍可经 register_from_agent_card 认证摄入。
- [x] P4：atomic `scope_event_snapshot`、合法 source refs、同 run 多次反思去重/supersede、跨 scope 隔离、拒绝路径零长期内容、只读 quality evaluator GREEN。✅ **2026-08-12 完成（离线部分）**：111+196+61 passed；review 无 Blocker（M1/M2 修复、M3-M7/M9、M8 记录）；P0 冻结修正 3 处（父侧裁决：209-217 causation_id 对齐冻结 ingestor、506-527 空窗口 snapshot 补 events、361/389 区 fixtures 补 canonical event_type——断言逐字不变，G2 披露）。
- [x] P4→P5：生产 reflection adapter 真实模型连续 5 次 function-call smoke 5/5 通过。✅ **2026-08-12 完成**：3 次独立运行全 5/5（subagent + 父侧 + 修复后复跑，证据 JSON ×3：`sar_orch/results/long_term_smoke_*.json`）；fail-closed sanity 3/3；review 发现 M1（生产终末路径 running loop 内 asyncio.run RuntimeError）已修复（offload daemon 线程 + bounded join）+ m2/m3；111+196 测试全绿。
- [ ] P5：coordinator-only long-term read-port、worker ACL negative、budget/cache/rollback contracts GREEN。
- [x] G2：shadow 10-run（5 scenes × agents `{2,4}` × seed 42）证据完整，指定框架错误码均为 0；长期库随 run-local 目录新建。✅ **2026-08-12 完成（证据已备，待审批）**：10/10 run（`sar_orch/results/long_term_memory_20260812_130327/`）：终末反思 7 completed（written 3-6）+ 3 skipped（空窗口契约正确）、框架错误码全 0、quality artifact 全存在（traceability 1.0 / truth violation 0 / conflict 0 / supersede 0）、reflection_run 全 completed、long_term_memory 全 published（13-41/run）、support refs 26-74/run 且 scope 绑定一致、embodied position/inventory=worker_telemetry + capability=registry 落库。已知缺口：support.source_revision/event_digest 列未填充（P1 移交项，P4 未补，G2 审批时披露）。
- [ ] G3/G4：read 10-run、full pytest、ruff、独立验收及 fresh approval record 完成。
- [ ] P6：仅在实现/证据真实完成后更新 `docs/system_docs/memory.md`、AGENTS 和待办状态，且不覆盖用户已有 dirty hunk。

### G3 观察点（2026-08-12 记录，不阻塞 G2；源自 reflection 代码带读，用户拍板记录）

- **[O-A] 反思"失忆"→ 本质是记忆压缩而非记忆积累**（用户定性）：反思模型每次只见增量窗口，`_build_user_prompt` 不注入长期库已有记忆（reflection.py:679-686）→ 单次反思无法引用/融合既有记忆；重复靠 `derive_memory_key` + content_digest 幂等去重（duplicate 零写）、矛盾靠 supersede 兜底，跨反思整合不可达。G3 讨论：反思输入是否注入 top-k 相关已有记忆（注意 G2 边界"反思输入范围扩展不授权"）。
- **[O-B] 窗口内部截断丢信息**：超 `max_events=200` / `max_chars=8000` 丢最旧保最近（reflection.py:518-527），`truncated` 仅标记不补救。10-run 实测窗口 4-10 条/run 远低于上限，短 run 无风险；长 run + 低触发频率会顶上限；缓解=触发频率可调（`[trigger] every_env_step / min_interval_sec`）。
- **配置空间约定**：窗口阶段参数已通过 `long_term.config [window]` 流出（max_events / max_chars，D9）；未来 O-A/O-B 相关新参数（如已有记忆注入开关/数量、截断策略）同样须经 config 流出，不硬编码。

## 5. 变更日志

| 日期 | 变更 | 关联 |
|---|---|---|
| 2026-08-12T（当日） | **G3 观察点记录（代码带读产出，用户拍板）**：reflection 带读后记录——(O-A) 反思"失忆"=记忆压缩定性（增量窗口不含已有记忆，跨反思整合不可达，幂等/supersede 兜底）；(O-B) 窗口截断丢最旧仅 truncated 标记不补救（10-run 实测窗口 4-10 条/run 远低于上限）；约定窗口阶段及未来新参数一律经 `long_term.config` 流出配置空间。不阻塞 G2，G3 前处理 | reflection.py collector/prompt、long_term.config D9、进度 §4 观察点 |
| 2026-08-12T（当日） | **P2 完成**：实现 subagent 交付（barrier/helpers/server/contracts + push_callback/embodied_telemetry 测试追加）；父侧独立验收（135 passed 实测、diff 范围精确、P0 冻结断言 1-432 行抽查未动、ruff 逐文件 HEAD 对比新增零错误）；独立 review subagent 交叉审查（无 Blocker：M1 Major = inventory=None/int/bool 折叠为空库存 claim、m1 body_sha256 死参数、m2 evidence 随 ts 重推累积记录、m3 P0 空真认知提示、m4 空串语义并入 M1）；修复 subagent 完成 M1+m1+防回归测试（136 passed）；心跳零改动、无 P3/P4 内容、无 commit | 主方案 §3.1/§4 Phase 2、G1 contract card |
| 2026-08-12T（当日） | **G2 审查包备齐（等待审批）**：shadow 10-run 矩阵完成（`long_term_memory_20260812_130327/`，10/10 全绿：终末 7 completed + 3 skipped 空窗口、框架错误码全 0、quality 全优、原子快照绑定一致、embodied/registry 落库核验通过）；G2 审查包含 P0–P4 证据、smoke 5/5 ×3、10-run 全量数据、P0 冻结修正 3 处披露、已知缺口 3 项（support digest 列未填充/Charlie 他报 capability/m8）；人工审查点 R2 停 | 主方案 §5.3/§5.4、原子快照补充 §4 |
| 2026-08-12T（当日） | **P4→P5 smoke 5/5 + M1 修复**：实现 subagent 交付真实 adapter + smoke 脚本（证据 `long_term_smoke_20260812_044511.json`）；父侧重跑 5/5（`_044654.json`）；独立 review 发现 M1（生产终末路径 running event loop 内 asyncio.run → RuntimeError，shadow run1 实测 rejected/missing_function_call 实锤）——修复 subagent 完成 M1（loop 探测 + offload daemon 线程 + bounded join）+ m2（证据 scrub）+ m3（资源生命周期记录）；loop 上下文 A-E 验证全过、smoke 复跑 5/5（`_050154.json`）；矩阵重启后终末反思恢复正常（completed/skipped） | 主方案 §5.3、D8 |
| 2026-08-12T（当日） | **P4 完成（离线部分）**：实现 subagent 交付（原子快照/collector/run_reflection/trigger/evaluator/接线 + 18 新测试）；父侧独立验收（105+196 passed 实测、代码逐段重读、ruff 逐文件对比）；独立 review 交叉审查（无 Blocker：M1 同步路径空窗口不跳过 claim、M2 D8 read 缺 key 静默降级、M3-M9 Minor）；修复 subagent 完成 8 项（111+196+61 passed）；**P0 冻结修正 3 处（父侧裁决 2026-08-12，均附注释，断言逐字不变，G2 审查包披露）**：(a) 209-217 causation_id 匹配（原断言与冻结 ingestor event_id 生成语义矛盾，独立复现证实）；(b) 506-527 空窗口 snapshot 补 events + pin window_end_sequence（与 §3.4.2 空窗口跳过语义对齐）；(c) 361/389 区 collector fixtures 补 canonical event_type（M7 fail-closed 语义）；未完成：真实模型 adapter（P4→P5 smoke） | 主方案 §3.3/§3.4/§4 Phase 4、原子快照补充 |
| 2026-08-12T（当日） | **P3 完成**：实现 subagent 交付（registry_projection.py 新建 + agent_registry/server/a2a_server/__init__ + 10 测试追加）；父侧独立验收（198 passed + 4 P5 预期 RED 实测、P0 冻结断言 1-388 行抽查未动、ruff 逐文件 HEAD 对比新增零违规）；独立 review subagent 交叉审查（无 Blocker：M1 静态配置未认证能力投影、M2 contains 首次判定失效、m1 sensors 噪音能力、m2 非 dict 容错、m3 event_id 排序、m4 记录、m5 get raise、m6 测试缺口）；修复 subagent 完成 M1/M2/m1/m2/m3/m5 + m6 测试（200 passed）；用户 2026-08-12 指令“继续，没到人工审查点不用停下来”授权 P4 及后续实施 | 主方案 §3.2/§4 Phase 3、D2/D3 |
| 2026-08-12T（当日） | **G1/R1 批准**：用户逐项确认 C1–C5 后批准（原话“批准G1通过”）；新建审批记录 `长期记忆_反思机制+动态Agentcard接入_G1-approval-record.md`（绑定 HEAD `5413705`、审查包 SHA-256 `1faeff92…b9a4`、主方案与人工审查点补充冻结 hash）；G1 改通过、R1 改已批准、P2 状态改“未开始（G1 已批准，等待实施指令）”；仅放行 P2，不自动启动实施 | 审查包 G1-R1-H1-contract-card、人工必要审查点 §1/§5 |
| 2026-08-12T（当日） | **P1 完成**：实现 subagent 交付 long-term kernel（long_term.py 850 行 + reflection.py 228 行 + contracts.py +279 + __init__ +44 + sar_orch 最小 D9 re-export 22 行）；父侧复验（contracts+store 41/41、reflection 9/22 预期 RED、回归 25/25、ruff 零错误）；独立 review subagent 交叉审查（无 Blocker：F1 Major = load_long_term_config quality_enabled 键映射断裂，父侧独立证实真实 long_term.config 加载即崩；F2-F6 Minor）；修复 subagent 完成 F1-F6 + 5 防回归测试（contracts+store 46/46）；canonicalize 句子标点集按 P0 测试契约落地（保留内容括号） | 主方案 §4 Phase 1 / §3.3 / 跨Run补充 |
| 2026-08-12T（当日） | **P0 完成**：实现 subagent 交付 7 新建 + 7 增补测试文件（2600 行）；父侧独立复验（105 RED 全合法、零回归、生产零改动）；独立 review subagent 交叉审查（无 Blocker，10 Major + 9 Minor，未来 API 命名 100% 一致）；修复 subagent 完成 10/10 Major + 6 Minor（补 pytest.raises、修空真/反转/KeyError、/tmp→tmp_path、shadow mode 旋钮、D9 typed 错误断言、TRUNCATED 互斥按父侧裁决修守护）；最终 106 RED（含 1 TypeError=未来 API 缺失，父侧裁决接受）+ 179 GREEN、零 collection ERROR、ruff 4 条基线债务、`:!tests/ :!docs/` diff 为空；移交 P1 已知项见 §4 | 主方案 §4 Phase 0 / §5 |
| 2026-08-12T（当日） | **G0 批准 + P0 启动**：用户明确“根据相关文档可以开始实施计划了”（2026-08-12）视为 G0 整体最终确认与“开始实施”信号；G0 状态改为通过（绑定 HEAD `5413705`），R0 改已批准；P0 状态改为实施中；授权范围仅 P0 test-only RED，生产代码/真实模型/真实实验/migration/Context cutover/commit/push 未授权 | 主方案 §7、人工必要审查点 R0 |
| 2026-08-12T01:40:00+08:00 | Subagent 严格审查（3 并行 leaf，父侧独立复验）修复轮：进度 Gate 表 G0 行更新为"D1–D9 已逐项拍板待确认"；Phase 4 candidate→published 措辞对齐 D6；补契约——窗口游标（`window_end_sequence`，completed 才推进、failed 不推进、崩溃从长期库续读）、`content_digest=sha256(canonicalized(redacted statement))`、key/digest post-redaction 派生、kind DB CHECK、truth 词表复用 `FORBIDDEN_TRUTH_TERMS`、批内重复 key 拒绝、terminal drain+join 超时（`[timeout] reflection_sec=60`）、config 损坏 fail-closed、预算优先级 Task>Spatial>Embodied>Long-term>Temporal>Freshness、`policy_version` 代码常量、read 缺 key 显式拒绝；修正 D1–D8 陈旧引用（全部 D1–D9）、架构图"跨 run"残留、"SAR 观测列表没有 agent 自身"、表计数 15 张业务表、`env_loader` 机制（不写 os.environ，P4 显式注入）、快照读事务 finally ROLLBACK、migration failure 落点 run_metrics、探索报告 hash 补钉；hash 重算回写 | 主方案/三份补充/进度/待办 + `long_term.config` |
| 2026-08-12T01:05:00+08:00 | 配置产物同步：新建仓库根 `long_term.config`（D9 实体，configparser 可解析已验证）与 `.env.example`（D8 `reflection_*` 配置口示例，含缺 key fail closed 说明）；不触碰真实 `.env` | 主方案 D7/D8/D9 |
| 2026-08-12T00:50:00+08:00 | G0 逐项拍板回写（用户 2026-08-12 确认）：D1 接受（R1/G1 触发）；D2 改“新 worker 加入 bootstrap”，断线重连同步出 V1；D3 机制-only（SAR 无 sensor）；D6 同事务直接 published；D7 增量窗口 + 异步执行 + `long_term.config` 可调；D8 `.env` `reflection_*` 配置口（默认 openai/deepseek-v4-flash，缺 key fail closed）；新增 D9 配置面；kind/canonicalized/质量指标冻结；hash 重算并回写绑定字段 | 主方案及三份补充 |
| 2026-08-12T00:35:00+08:00 | 范围收缩修订（用户拍板 2026-08-11）：长期记忆改为**单 run 语义**——run 内滚动反思 + terminal 收尾、validator 通过即 published、supersede 更新、增量窗口；跨 run 复用走 skill/文档沉淀；存储改为 run-local 独立文件（`<memory_root>/long_term/long_term.sqlite3`），删除 2-scope 发布阈值与 `--long-term-memory-root` 参数；主方案/原子快照/跨 Run 补充/人工审查点 hash 全部重算并回写绑定字段 | 主方案及三份补充 |
| 2026-08-11T23:20:55+08:00 | 增加 R0–R4 最小人工必要审查点及其“不可逆边界/顺序/非逐 Phase”划分依据；所有 Gate 仍为待审批，未授予实施权限 | 人工必要审查点补充 |
| 2026-08-11T23:14:59+08:00 | 新建本进度文档；冻结当前 HEAD、方案 hash、G0–G4 状态和“未实施”边界 | 主方案及补充 |
