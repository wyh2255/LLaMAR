---
title: G3/R3 Read 模式放行审批记录（long_term_mode: shadow → read）
schema_version: 1
gate_id: G3
review_point: R3
conclusion: APPROVE
approved_at: 2026-08-12
reviewed_commit: 8869328
branch: feat/memory-redesign
review_packet: .hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_G3-R3-review-package.md
design_sha256: 169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab
review_points_doc_sha256: 218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d
---

# G3/R3 审批记录（`long_term_mode: shadow → read` 放行）

## 人工决议

用户在本次会话（2026-08-12）批准 G3，原话：

> 按照你的建议来。

即 **APPROVE**：放行 `long_term_mode=read`（真实 SAR run 中把 published 长期记忆作为受预算约束的 Environment State section 注入 coordinator Context；worker 永不可见），并授权执行 read 10-run 真实验证（5 scenes × `{2,4}` agents × seed 42，`--long-term-mode read`）作为 G3 绑定证据与 G4 材料。

## 决策项结论（G3-1 ~ G3-3）

| # | 决策项 | 结论 |
|---|---|---|
| G3-1 | 放行 `long_term_mode=read`（coordinator Context 注入 published 长期记忆） | **APPROVE**：真实 run 启用注入；随后执行 read 10-run 矩阵作为 G3 证据与 G4 材料 |
| G3-2 | G2-4 他报 capability 观察点：是否在观测路径过滤 capability 字段 | **选项 A（保持现状）**：观察点关闭。父侧复核实据：`projection_field` 实测 Charlie capability 为 `worker_observation`（source_priority=1）且已被后续 `registry` claim（`evt_3e91…`，evidence_id=`reg:Charlie:…`）正式 supersede——他报只是临时占位，权威 registry claim 到达即被 reducer 覆盖，兜底机制闭环经真实数据验证；选项 B（过滤 capability）会引入 H1 reducer 变更，扩大 G3 放行面，不作选择 |
| G3-3 | review Minor 记录追认（m1/m6/m7/m8/m9/m10 记录、m2-m5 已修） | **追认**：10 项全部接受；另追认审查时父侧数字修正（P5 测试 78→83 passed，78 为 M-1/budget 修复前数字，修复追加 5 项防回归，审查包 §2.1/§7 与进度文档已同步） |

## 授权范围（allowlist）

- `long_term_mode=read`：coordinator Context 注入 published-only 长期记忆段（`### Long-term Memory`，每 memory_key 一行摘要，`status='published'` 硬过滤 + supersede 后旧行自动排除），受 token budget 约束（丢弃顺序 Task > Spatial > Embodied > Long-term > Temporal > Freshness，低预算裁剪留 `TRUNCATED`）；
- freshness 元数据 `long_term_revision`（scope 级）随 read 模式注入 coordinator 视图（同 env_step 变更可见）；
- read 10-run 真实验证（`sar_orch/run_g3_read_matrix.sh`，`--max-steps 20` 与 G2 矩阵一致）；
- G3 审批后 P5 工作区改动随用户指示提交（提交时机由用户另行指示）。

## 不授权（G3 放行边界）

- canonical schema migration、Context cutover、commit/push（提交时机由用户另行指示）；
- 反思输入范围扩展（仍只读 `callback.*`/`evidence.projection`/`supervision.*`/`control.*` 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动（G3-2 选项 A 即不触碰 reducer）；
- worker 侧任何形式的长期记忆可见性（ACL 冻结：worker 视图永不包含长期段与 `long_term_revision`）。

## 前提核验（父侧独立复验，2026-08-12）

- hash 绑定核对：主方案 `169f9f7d…`、原子快照补充 `54e259ea…`、跨 Run 补充 `0146bf81…`、人工审查点补充 `218d7990…`、G1 审查包 `1faeff92…`、G2 审查包（HEAD 版）`d7d2fbf0…` —— 全部与实际文件一致；
- P5 测试：5 文件实测 **83 passed**（12 RED 全 GREEN + 71 项守护零回归；78 + M-1/budget 修复 5 项防回归），全量 1850 passed 4 skipped 0 failed（审查包 §2.1，数字已修正并注明）；
- review 报告（0 Blocker/1 Major/10 Minor）存在且与引用一致；M-1 修复 `_validate_long_term_mode_combo`（coordinator.py:23-37）fail closed 实测存在；
- worker ACL 双门控 `_is_system and long_term_mode=="read"`（environment_state_provider.py:375-382）实测存在；server.py:2213 worker HTTP 端点裸构造因 `viewer_role="worker"` 安全；
- before 样本：`long_term_memory_20260812_130327/scene_1_agents_4/coordinator/unnamed_task.ndjson` llm_request 真实渲染无长期段，与审查包引用一致；
- G3-2 实据：`scene_2_agents_4` projection_field 全量 capability claims（4 行）实测——Charlie `worker_observation`（priority 1，env_step 3，含 supersede 记录）、Alice/Bob/David `registry`（priority 0）；supersede 链目标事件为 Charlie 自身 registry claim（`reg:Charlie:c75ffac5f0e254ff:capability`）；
- read 10-run 已于审批后启动（后台 `run_g3_read_matrix.sh`），结果将汇总为 G3 绑定证据与 G4 材料。

## 残留风险（用户接受）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 长期记忆误导 coordinator 决策 | read 10-run 前后对比（coverage/transport/框架错误码）；G3 审批时人工看 Context 样本 | read 10-run 执行中 |
| 长期段 token 开销 | 固定上限摘要 + 预算裁剪留 TRUNCATED；丢弃顺序钉死 | 实现 + 测试 |
| 注入内容过期/矛盾 | supersede 机制（旧行自动排除）+ published-only 过滤 + long_term_revision 可观测 | 实现 + 测试 |
| worker 泄漏 | ACL 双门控 + GREEN 守护 + review 独立验证 | 零泄漏（审查包 §2.3） |
| shadow compare 污染 | M-1 交叉校验 fail closed（shadow+read 组合禁止） | 已修复 + 防回归 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens（实测） | 已确认属预期 |

---

*本记录由父 agent 生成（2026-08-12），绑定用户原话「按照你的建议来」。审批结论已回写进度文档（§1/§2/§3/§4）。read 10-run 结果完成后再补充绑定证据（追加记录或 G4 审查包引用）。*
