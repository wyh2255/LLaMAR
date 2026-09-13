---
title: G4/R4 正式可用审批记录（long_term_mode=read 正式可用）
schema_version: 1
gate_id: G4
review_point: R4
conclusion: APPROVE
approved_at: 2026-08-12
reviewed_commit: c866cc3
branch: feat/memory-redesign
review_packet: .hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_G4-R4-review-package.md
design_sha256: 169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab
review_points_doc_sha256: 218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d
---

# G4/R4 审批记录（`long_term_mode=read` 正式可用）

## 人工决议

用户在本次会话（2026-08-12）批准 G4，原话：

> 批准

即 **APPROVE**：标记 `long_term_mode=read`（coordinator Context 注入 published 长期记忆）正式可用，接受保留/回滚边界（G4-2），确认 AgentCard future mutation 边界（G4-3），追认 review Minor 与观察项（G4-4），并执行 P6 文档收口（memory.md / AGENTS / 待办状态，不覆盖用户 dirty hunk）。

## 决策项结论（G4-1 ~ G4-4）

| # | 决策项 | 结论 |
|---|---|---|
| G4-1 | 标记 `long_term_mode=read` 正式可用（含 P6 文档收口：`docs/system_docs/memory.md` / AGENTS.md / 待办状态，不覆盖用户 dirty hunk） | **APPROVE**：正式可用；P6 收口执行 |
| G4-2 | retention/保留边界：长期库随 run 日志保留、不自动 purge，清理为手动责任 | **接受**：运行治理归属用户/项目惯例（权限 700/600 owner-only） |
| G4-3 | future in-process AgentCard mutation | **确认**：维持 V1 边界（仅注册 bootstrap），`agent_card_changed` 待真实 producer 出现再设计，不属本 feature 范围 |
| G4-4 | review Minor（R4-1 口径重算 / R4-2 JSON 层定位注 / R4-3 rerun 证据卫生注）与观察项追认 | **追认**：3 Minor 已全部修订；观察项（supersede 链 read 侧全 0 / m1/m6-m10 复核 / store 关闭后查询 docstring 补注建议）记录在案 |

## 授权范围（allowlist）

- 标记 `long_term_mode=read` **正式可用**（真实 SAR run 的默认可用模式之一；G3 已放行注入行为，本 Gate 宣告 release 状态）；
- **P6 文档收口**：更新 `docs/system_docs/memory.md`（长期记忆 section，三方合并本 feature hunk，不覆盖用户既有 dirty 内容）、AGENTS.md（memory 相关说明）、待办文档状态（`长期记忆_反思机制+动态Agentcard接入.md` 由"待办"转"已实施"）；
- 长期库随 run 日志保留（手动清理责任），rollback 路径 `read_port → legacy` 已演练不删数据。

## 不授权（G4 放行边界）

- canonical schema migration、Context cutover、commit/push（提交时机由用户另行指示）；
- 反思输入范围扩展（仍只读 `callback.*`/`evidence.projection`/`supervision.*`/`control.*` 四类）；O-A 观察点（top-k 已有记忆注入）保持未来设计；
- 任何对 H1 reducer、真相隔离、认证边界的改动；
- worker 侧任何形式的长期记忆可见性（ACL 冻结不变）；
- future in-process AgentCard mutation 的实现（G4-3 确认维持 V1 边界）；
- 并行 team 功能未提交改动（worker.py/mission_runtime.py 等 9 文件，用户另行开发）——不属本 Gate，P6 收口不触碰。

## 前提核验（父侧独立复验 + 独立 review，2026-08-12）

- read 10-run 矩阵 **10/10 有效**（scene_4_agents_2 重跑版 cov/tr 与 shadow 对照一致；注入实证 10/10 全含段、去重渲染 key 13-55/run；框架错误码全 0）；
- shadow vs read 对比：avg cov 0.885→0.781、tr 0.837→0.783（同量级、无退化、无框架错误）；
- full pytest fresh：**1856 passed, 4 skipped, 0 failed**；长期记忆 6 文件 132 passed；
- recovery：权限 700/600、migration v1 带 hash、reopen 正常；rollback 演练：read 长期库 hash 零触碰、legacy 零长期库 I/O、canonical 数据保留；
- 独立 review（G4）：**0 Blocker / 0 Major / 3 Minor**（R4-1/2/3 全部修订），结论 APPROVE；
- 独立 review（G3）：0 Blocker / 1 Major（M-1 已修）/ 10 Minor（追认）。

## 残留风险（用户接受）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 长期记忆误导决策 | read 10-run 前后对比（无退化）+ 注入样本人工可查；如需更强结论可更长 max_steps 复测（非本 Gate） | 已评估 |
| 长期段 token 开销 | 固定上限摘要 + 预算裁剪留 TRUNCATED；丢弃顺序钉死 | 实现 + 测试 |
| 保留膨胀（无自动 purge） | 手动清理责任明确（G4-2 接受）；每 run 量级小（20-59 memories） | 接受 |
| worker 泄漏回归 | ACL 双门控 + GREEN 守护 + 937 请求实测零泄漏 | 冻结 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens | 已确认属预期 |
| 并行 team 改动工作树影响 | P6 收口仅动 memory.md/AGENTS/待办文档，不触碰并行文件；矩阵证据已按工作树口径记录 | 接受 |

---

*本记录由父 agent 生成（2026-08-12），绑定用户原话「批准」。审批结论已回写进度文档（§1/§2/§3/§4）并执行 P6 文档收口。*
