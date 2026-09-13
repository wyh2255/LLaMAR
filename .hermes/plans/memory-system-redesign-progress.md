# Memory System Redesign — 实施进度记录

> **用途：** 跟踪设计文档 `memory-system-redesign-design.md` 的 Phase/Gate 状态与验收证据。每完成一个 Phase 或 Gate 决议后更新本文件。
>
> **边界：** 本文件只记状态与证据；设计语义变更必须回改设计文档，不在此记录设计内容；可复用经验沉淀到 `经验.md`。
>
> **当前状态：** H1–H3 全部关闭；legacy 主路径退役已提交（`79e20bc`，2026-08-10）。`memory_read_mode` 默认 `read_port`，canonical Memory 为官方路径；`legacy` 仅作受控 rollback。本线无进行中 Phase。后续独立 feature（长期记忆 G0–G4）已另档闭环，见 [`长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md`](长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md)。
>
> **更新约定：**
>
> - 进度三态汇报：实施中 / 验证中 / 可提交，附可核验产物路径或测试证据；
> - 没有独立测试与真实 smoke 证据不得把 Phase 标记为完成；
> - Gate 决议以文字形式记录（日期、审查材料、用户拍板项、放行结论、遗留处置项）；
> - 被 Gate 打回的项：记录是契约修订（回设计流程）还是实现缺陷（直接修）。

---

## 1. 当前位置

- 阶段：**本线完成**——H3 APPROVE + retirement change 已提交（默认值 13 处 `legacy→read_port` + 入口适配 + 矩阵 10/10）
- 本线收口 HEAD：`79e20bc`（`feat/memory-redesign`）。仓库当前 HEAD 可能更新（长期记忆 / 并行 SAR 修复），不改变本文件跟踪的 H1–H3 结论
- 下一个动作：**无本线待实施项**。未授权可选项（H3 放行边界内，需独立 review）：standalone coordinator CLI / dashboard 真正接入 canonical Memory（当前入口仍可显式走 legacy）；删除 legacy consumer / 使 legacy 数据不可用 / 自动 retention purge
- 已结束历史（P0–P4 逐项修复、P5 九轮矩阵、retirement 实施细节）已收敛；细节见各 approval record / review packet
- 后继 feature 不在本文件决策：长期记忆 / 反思 / AgentCard 见上引实施进度（G0–G4 / P0–P6，正式可用）

## 2. Phase 状态表

状态枚举：未开始 / 实施中 / 验证中 / 待审(H?) / 通过 / 阻塞(附原因)

| Phase | 内容 | 状态 | 主要产出（路径） | 验证证据 | 完成日期 |
|---|---|---|---|---|---|
| P0 | 契约与最小 kernel scaffold（MemoryScope/关系/Freshness、control journal 原子持久化） | 通过 | memory kernel + `tests/test_memory_contracts.py` 等 | 26 passed | 2026-08-06 |
| P1 | active 名称迁移、Context 协议与 feature flag（`memory_read_mode=legacy` 默认） | 通过 | ContextSnapshotV2、Environment State 渲染统一 | 54 passed | 2026-08-06 |
| P2 | authenticated Temporal shadow write（CallbackProofV1、nonce/idempotency ledger、脱敏、supervision adapter） | 通过 | callback auth/ingestor/redaction/signing | 110 passed | 2026-08-06 |
| P3 | Spatial/Embodied projection 与在线 truth boundary（H1 语义落地） | 通过 | projection reducer、`test_memory_online_truth_boundary.py` | 复核后 249 passed | 2026-08-06 |
| P4 | EnvironmentStateProvider read-port cutover（H2 授权后启用） | 通过 | `EnvironmentStateProvider` + ACL/route/shadow/rollback | 149→171 passed；真实 rollout PASS | 2026-08-08 |
| P5 | recovery、compatibility 物化与 legacy retirement 准备（H3 证据） | 通过 | exporter/recovery、error taxonomy、evaluators、truth_recorder | 1636 passed；10-run 矩阵 10/10 both-pass | 2026-08-09 |

## 3. Gate 决议日志

| Gate | 审查内容 | 状态 | 决议摘要 | 日期 |
|---|---|---|---|---|
| H1 | Projection Semantics Lock（在线 Memory 只消费 authenticated evidence） | 已批准 | APPROVE；绑定 H1 card 与冻结 hashes，授权 Phase 3 allowlist 内 test-first 实现 | 2026-08-06 |
| H2 | Read-Port Cutover（`read_port` 启用授权） | 已批准 | APPROVE；绑定 candidate `0414701` + packet v2，随后真实 rollout 通过 | 2026-08-08 |
| H3 | Retirement and Release（legacy 主路径退役） | 已批准 | APPROVE；绑定 packet `718a73a2…` + target `a0d6712` + 矩阵根目录；授权 canonical Memory 正式运行与退役流程启动 | 2026-08-10 |

### 决议详情

**H1（2026-08-06）** — 审批记录 `memory-system-redesign-h1-approval-record.md`；H1 card 锁定 C1–C7。核心语义：在线 Memory 只能消费 authenticated Worker evidence、允许的静态 AgentRegistry metadata 与 control/supervision facts；`SARBarrier`/simulator ground truth 只能服务 Worker-local sensing、explicit oracle/debug mode 或 terminal 后 evaluator，不能初始化、修正、回填 Memory。

**H2（2026-08-08）** — 审批记录 `memory-system-redesign-h2-approval-record.md`（packet v2 SHA-256 `d9f84e35…`，绑定 candidate `0414701`）；授权 `memory_read_mode=read_port`。真实 rollout（candidate `51e9e39`）PASS：99/99 worker Environment State block FRESH、零 rollback、零 secret 泄漏。legacy 仍为默认，shadow 保留对比；`SHADOW_COMPARE_ALLOWLIST` 零 non-allowlist diff 为 gate。

**H3（2026-08-10）** — 审批记录 `memory-system-redesign-h3-approval-record.md`（绑定 packet SHA-256 `718a73a2…`、target HEAD `a0d6712`、矩阵根 `sar_orch/results/memory_acceptance_a0d6712_20260809_153738`）。用户审阅完整 packet 后 APPROVE；三项残留风险接受理由：①broad truth 口径下低 Memory precision 为测量校准产物（最终态投影 vs 每步 claim；conflict_precision=0.0 实为分母为零），非框架缺陷；②recovery 为 Phase 5 测试级证据；③rollback 路径受控保留。授权 canonical Memory 正式运行（`read_port` 主路径）与退役流程启动。退役流程已于同日落地并提交（`79e20bc`：默认切 `read_port`，legacy 保留为 rollback）。删 legacy consumer / 使 legacy 数据不可用 / 启用自动 retention purge **仍需独立 review**（H3 allowlist 未授权删除）。

## 4. 验收标准勾选（H3 门禁证据）

- [x] 10-run acceptance 矩阵（5 scenes × agents {2,4} × seed 42，max_steps 20，semantic + `read_port`）：10/10 通过；`missing_error_code_rows=0`，`worker_busy`/`task_not_routable_yet`/`unknown_task_id` 全 0（证据：`sar_orch/results/memory_acceptance_a0d6712_20260809_153738/final_report.md`，artifact SHA-256 绑定）
- [x] projection quality：10/10 `memory_projection_quality.json` `metric_status=measured`，evidence_traceability_rate=1.0（evaluator-private truth manifest）
- [x] recovery：kill/restart 后 outbox replay、committed canonical set/scope fence/artifact 一致性（`tests/test_memory_recovery.py`）
- [x] compatibility：确定性 JSONL 物化 + manifest digest + legacy-compatible `semantic_map.jsonl` + render loader 冻结 fixture（`tests/test_memory_compat_export.py`、`test_render_loader_compat.py`）
- [x] error-code：taxonomy 全白名单化，无 unknown/sentinel code（`tests/test_tool_result_error_protocol.py` 等）
- [x] secret-leak：零 secrets、脱敏诊断、truth manifest 在 run results 目录外且 evaluator-private
- [x] H3 人工批准（2026-08-10 APPROVE，记录 `memory-system-redesign-h3-approval-record.md`）

## 5. 变更日志

| 日期 | 变更 | 关联 |
|---|---|---|
| 2026-08-06 | 文档初始化；H1 批准 | Phase 3 |
| 2026-08-08 | H2 批准；`read_port` 真实 rollout 验证通过 | Phase 4 |
| 2026-08-09 | Phase 5 证据齐备；H3 review packet 冻结（`718a73a2…`） | H3 |
| 2026-08-10 | 收敛重写：压缩已结束的 P0–P4 修复过程与 P5 九轮矩阵迭代细节，聚焦 H3 决策点；计划文件去除日期前缀 | 文档整理 |
| 2026-08-10 | H3 APPROVE（绑定 `a0d6712` + packet `718a73a2…`）；生成 approval record，全部 Gate 关闭 | H3 |
| 2026-08-10 | Retirement change 实施：`memory_read_mode` 默认 `legacy→read_port`（13 处，含 experiment CLI argparse）；测试构造点适配（补 secret / 显式 legacy）；AGENTS.md 文档同步；subagent 核查发现 experiment CLI 默认遗漏（B1）与 coordinator CLI / launch_dashboard 启动回归（M1/M2）并修复；全量 1636 passed 零回归；10 组交叉验证（scene 1-5 × agents 2/4，不显式传 mode）10/10 通过、30 个框架错误计数全零、memory_revision 非零证明 canonical Memory 写入生效 | Retirement |
| 2026-08-13 | **文档收口（状态对齐，不改设计语义）**：抬头/§1 原先自相矛盾（「下一步执行 retirement」vs 正文已写 retirement 完成并提交）。现统一为：H1–H3 关闭、retirement 已提交、本线无待实施项；H3 删除/purge 边界与 standalone CLI/dashboard 可选项保持未授权。后继长期记忆 feature 改指向独立进度文档 | 文档整理 |
