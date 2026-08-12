# G4/R4 审查包（收敛版）— 长期记忆正式可用边界

> **性质：** 本文件是人工必要审查点 R4（G4）的审查材料，由父 agent 基于 P0–P5 真实实现、G1–G3 审批链与真实运行证据生成，提交用户审批。批准后放行：标记 `long_term_mode=read` 正式可用并更新系统文档/进度（R4 定义）。
> **绑定基线：** HEAD `8869328`（P5 工作区未 commit 改动，随审批后提交）；主方案 SHA-256 `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`；人工审查点补充 `218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d`；G3 审批记录 reviewed_commit `8869328`（`长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md`）。
> **状态：** ⏳ **收敛版**（2026-08-12）——read 10-run 矩阵执行中（5/10 完成），矩阵相关证据为占位，完成后由父侧补全再提交审批；本版先收敛 R4 审批范围、已冻结证据与决策项。
> **结论：** ⏳ 待审批（矩阵补全后提交）。

---

## 1. 审批范围（R4 定义，精确到边界）

**批准后放行：**
- 标记 `long_term_mode=read`（coordinator Context 注入 published 长期记忆）**正式可用**；
- 更新系统文档/进度状态（`docs/system_docs/memory.md`、AGENTS.md、待办状态——P6 文档收口，且不覆盖用户既有 dirty hunk）；
- 接受长期库随 run 日志的**保留/备份/手动清理责任**（主方案明确不做自动 purge，保留期限由用户/项目惯例决定）。

**不授权（G4 放行边界）：**
- canonical schema migration、Context cutover、commit/push（提交时机由用户另行指示）；
- 反思输入范围扩展（仍只读 `callback.*`/`evidence.projection`/`supervision.*`/`control.*` 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动；
- worker 侧任何形式的长期记忆可见性（ACL 冻结不变）；
- future in-process AgentCard mutation（`agent_card_changed` 消息与动态 skill 增量的实现）——本 Gate 只要求给出**明确结论**（建议：维持 V1 边界，待真实 producer 出现后再设计，见决策项 G4-3）。

## 2. 证据汇总（已冻结 + 待补）

### 2.1 已冻结证据（G1–G3 链 + P5 实现，父侧独立核验）

| 项 | 证据 | 状态 |
|---|---|---|
| P5 实现与测试 | 5 文件 **83 passed**（12 RED 全 GREEN + 71 守护；78 + M-1/budget 修复 5 项防回归）、相关回归 217、全量 1850 passed 4 skipped 0 failed（144s）；ruff 零新增（基线 35 项逐行核对） | ✅ 已核验（2026-08-12） |
| 独立交叉审查 | 0 Blocker / 1 Major（M-1 已修：`_validate_long_term_mode_combo` fail closed）/ 10 Minor（m2-m5 已修，m1/m6-m10 记录追认） | ✅ review 报告 `subagent-summary-0-20260812_180703_616233.txt` |
| hash 绑定链 | 主方案 `169f9f7d…`、原子快照 `54e259ea…`、跨 Run `0146bf81…`、人工审查点 `218d7990…`、G1 审查包 `1faeff92…`、G2 审查包（HEAD 版）`d7d2fbf0…` | ✅ 全部与实际文件一致（父侧复验） |
| G3 审批 | G3-1 放行 read / G3-2 观察点关闭（supersede 链实锤）/ G3-3 Minor 追认 + 数字修正追认 | ✅ `G3-approval-record.md`（2026-08-12，用户原话「按照你的建议来」） |
| shadow 10-run（G2 基线） | 10/10 全绿：框架错误码全 0、quality 全优（traceability 1.0 / violation 0 / conflict 0 / supersede 0）、reflection_run 全 completed、memories 全 published（13-41/run） | ✅ `long_term_memory_20260812_130327/` |
| worker ACL 零泄漏 | 双门控 `_is_system and long_term_mode=="read"`（environment_state_provider.py:375-382）+ GREEN 守护 + review 独立复现；server.py:2213 裸构造因 `viewer_role="worker"` 安全 | ✅ 零泄漏（G3 审查包 §2.3） |
| G3-2 实据 | scene_2_agents_4 Charlie capability：worker_observation claim 已被 registry claim 正式 supersede（`evt_3e91…`，evidence_id=`reg:Charlie:…`） | ✅ 兜底闭环实测 |

### 2.2 read 10-run 矩阵（⏳ 执行中，5/10 完成 — 完成后补全本表）

矩阵：`sar_orch/results/long_term_memory_read_20260812_200708/`（`run_g3_read_matrix.sh`，5 scenes × `{2,4}` agents × seed 42，max_steps 20）。

| run | 状态 | steps | coverage | transport | 长期段注入 |
|---|---|---|---|---|---|
| scene_1_agents_2 | ✅ rc=0 | 20 | 0.667 | 0.667 | 12/12 llm_request 全含段；step 11 起真实记忆（pattern：资源位置） |
| scene_1_agents_4 | ✅ rc=0 | 20 | 1.0 | 0.933 | 26/26 全含段；step 25 hazard（task_stale 模式） |
| scene_2_agents_2 | ✅ rc=0 | 20 | 0.667 | 0.733 | 15/15 全含段；step 14 strategy（get_supply 工具用法） |
| scene_2_agents_4 | ✅ rc=0 | — | — | — | 待补 |
| scene_3_agents_2 | ✅ rc=0 | — | — | — | 待补 |
| scene_3_agents_4 | 🔄 运行中 | — | — | — | 待补 |
| scene_4_agents_2 | ⏳ 待跑 | — | — | — | — |
| scene_4_agents_4 | ⏳ 待跑 | — | — | — | — |
| scene_5_agents_2 | ⏳ 待跑 | — | — | — | — |
| scene_5_agents_4 | ⏳ 待跑 | — | — | — | — |

**注入实证（已完成 run）**：llm_request 100% 含 `### Long-term Memory` 段；早期为 `(none published yet)`（滚动反思积累窗口，预期），中后期真实 published 记忆进入 coordinator Context（资源位置 / task_stale 模式 / 工具用法策略）。**框架错误码统计、shadow vs read 前后对比（coverage/transport/error_counts）待矩阵完成后汇总。**

> ⚠️ **观察**：矩阵运行节奏存在波动（单 run 4.5–14 分钟不等），用户 2026-08-12 反馈"过程有点卡住，后续看日志修复"。若个别 run 出现 barrier 超时/模型调用阻塞，将如实记录 TimeoutAgents 与 end_reason，不掩盖。

### 2.3 R4 新增证据（⏳ 待补，矩阵后执行）

| 项 | 内容 | 状态 |
|---|---|---|
| full pytest / ruff fresh | 矩阵后重跑全量（基线 1850 passed 4 skipped 0 failed）与 ruff 逐文件 HEAD 对比 | ⏳ 待矩阵完成 |
| recovery 验证 | 长期库随 run 日志的权限（目录属主/模式）、迁移（`schema_migrations` 1 row）、reopen/lock-retry（P1 已 GREEN 46/46，复用） | ⏳ 汇总 |
| `read_port → legacy` rollback 演练 | 不删除 canonical 或 run-local long-term 数据；G3 后无 schema 变更，回滚面 = `long_term_mode=off|shadow` 切换 + memory_read_mode 旋钮 | ⏳ 待执行（小演练脚本） |
| retention 责任 | 主方案无自动 purge；`<results>/<run>/coordinator/long_term/` 随 run 目录保留，清理为手动责任 | 📝 结论可先给出（运行治理归属） |
| 独立验收 | 独立 review subagent 对 P5 diff + read 矩阵产物交叉审查（复现 G3 review 方式） | ⏳ 待矩阵完成 |

## 3. 披露（本 Gate 前已知）

1. **矩阵节奏波动**：read run 单次 4.5–14 分钟（LLM 调用为主），个别 run 可能触 barrier 60s 超时被 TimeoutAgents 填充——不影响长期记忆功能本身，但影响 run 级指标解读（过滤方式沿用 G2 惯例：按 TimeoutAgents 列区分系统注入 NoOp）。
2. **P5 数字修正追认**：78→83（M-1/budget 修复 5 项防回归），G3 已追认。
3. **m1/m6/m7/m8/m9/m10 记录项**：G3 已追认（双接线冗余/诊断双行/镜像恒 off/回填 0/supersede OR 门/空段 TRUNCATED 行为变化）。
4. **O-A 观察点（反思"失忆"=记忆压缩）**：增量窗口不含已有记忆，跨反思整合不可达——G3 边界"反思输入范围扩展不授权"保持不变；是否引入 top-k 已有记忆注入属未来设计（G4 不决策，仅记录）。

## 4. 风险与残留（G4 视角）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 长期记忆误导决策 | read 10-run 全量前后对比（coverage/transport/错误码）+ 注入样本人工可查 | 矩阵执行中 |
| 长期库随 run 日志丢失/权限问题 | run-local 目录随 results 保留；migration/reopen/lock-retry 已测试 | P1 GREEN，G4 汇总 |
| rollback 不可逆 | `read_port → legacy` 演练不删 canonical/long-term；模式切换纯旋钮 | 待演练 |
| 保留膨胀（无自动 purge） | 手动清理责任明确归属；每 run 长期库量级小（10-40 memories） | 结论待 G4 确认 |
| worker 泄漏回归 | ACL 双门控 + GREEN 守护 + review 复现（零泄漏已实证） | 冻结 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens | 已确认属预期 |

## 5. 需用户拍板的决策项

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| G4-1 | **标记 `long_term_mode=read` 正式可用**（含 P6 文档收口：memory.md / AGENTS / 待办状态，不覆盖用户 dirty hunk） | read 已放行（G3），矩阵执行中 | 矩阵 10/10 + full suite + 独立验收通过后 APPROVE | 是否 APPROVE |
| G4-2 | **retention/保留边界确认**：长期库随 run 日志保留、不自动 purge，清理为手动责任 | 主方案无自动 purge | 接受（运行治理归属用户/项目惯例） | 是否接受 |
| G4-3 | **future in-process AgentCard mutation**：是否允许未来实现 | V1 边界：仅注册 bootstrap，无动态 skill 增量 | 维持 V1 边界（`agent_card_changed` 待真实 producer 出现再设计）；不在本 feature 范围 | 是否确认 |
| G4-4 | **review Minor 记录项与 O-A 观察点追认**（m1/m6-m10 已在 G3 追认；O-A 记录为本 Gate 观察点） | 已记录 | 追认 | 是否追认 |

## 6. 证据清单

- 进度文档：`.hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md`（§1/§2/§3/§4/§5）
- G3 审批记录：`长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md`（reviewed_commit `8869328`）
- P5 测试：`env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_long_term_environment_state.py tests/test_environment_state_acl.py tests/test_coordinator_state_provider.py tests/test_environment_state_provider.py tests/test_long_term_read_port.py -q`（83 passed）
- review 报告：`/home/wyh/.hermes/profiles/coder/cache/delegation/subagent-summary-0-20260812_180703_616233.txt`
- shadow 10-run：`sar_orch/results/long_term_memory_20260812_130327/`
- read 10-run：`sar_orch/results/long_term_memory_read_20260812_200708/`（⏳ 矩阵执行中）
- 注入实证：`<read run>/coordinator/unnamed_task.ndjson`（llm_request，`### Long-term Memory` 段）

---

*本审查包（收敛版）由父 agent 生成（2026-08-12）。read 10-run 矩阵完成、full suite/独立验收/recovery/rollback 证据补齐后，更新为完整版提交用户对 G4/R4 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入 `G4-approval-record.md` 并回写进度文档。*
