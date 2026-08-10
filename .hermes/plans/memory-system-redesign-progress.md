# Memory System Redesign — 实施进度记录

> **用途：** 跟踪设计文档 `memory-system-redesign-design.md` 的 Phase/Gate 状态与验收证据。每完成一个 Phase 或 Gate 决议后更新本文件。
>
> **边界：** 本文件只记状态与证据；设计语义变更必须回改设计文档，不在此记录设计内容；可复用经验沉淀到 `经验.md`。
>
> **当前状态：** H3 已批准（2026-08-10）——全部 Gate 关闭；下一步是制定并执行 legacy retirement change（独立 review）。
>
> **更新约定：**
>
> - 进度三态汇报：实施中 / 验证中 / 可提交，附可核验产物路径或测试证据；
> - 没有独立测试与真实 smoke 证据不得把 Phase 标记为完成；
> - Gate 决议以文字形式记录（日期、审查材料、用户拍板项、放行结论、遗留处置项）；
> - 被 Gate 打回的项：记录是契约修订（回设计流程）还是实现缺陷（直接修）。

---

## 1. 当前位置

- 阶段：H3 已批准，进入 retirement 流程（legacy 退役 change 需独立 review）
- 当前 HEAD：`a0d6712f26d5e80afdb349e6d65a89703b1514ed`（`feat/memory-redesign`）
- 下一个动作：制定 legacy retirement change（删 legacy consumer / 使 legacy 数据不可用 / 启用自动 retention purge 均需独立 review，绑定 H3 approval record）
- 已结束历史（P0–P4 逐项修复过程、P5 九轮矩阵迭代与候选修复）已收敛压缩，不影响当前决策；细节见各 approval record / review packet。

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

**H3（2026-08-10）** — 审批记录 `memory-system-redesign-h3-approval-record.md`（绑定 packet SHA-256 `718a73a2…`、target HEAD `a0d6712`、矩阵根 `sar_orch/results/memory_acceptance_a0d6712_20260809_153738`）。用户审阅完整 packet 后 APPROVE；三项残留风险接受理由：①broad truth 口径下低 Memory precision 为测量校准产物（最终态投影 vs 每步 claim；conflict_precision=0.0 实为分母为零），非框架缺陷；②recovery 为 Phase 5 测试级证据；③rollback 路径受控保留。授权 canonical Memory 正式运行（`read_port` 主路径）与退役流程启动；删 legacy consumer / 使 legacy 数据不可用 / 启用自动 retention purge 仍需独立 retirement change review。

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
