# G3 审批记录 — System Health 注入段放行（P4 产物验收 + R3 修订条件）

> **性质：** 本记录固化 G3（注入与权威性）的人工审查决议。审查材料为 [`系统健康诊断_agentic审查者_G3-R3-review-packet.md`](系统健康诊断_agentic审查者_G3-R3-review-packet.md)（SHA-256 `d7f82a53848c7d23996a632cd7cf037e697bc1414245ee9dccb2ad8bcbdb20dc`），审查方式为用户带读（父侧逐判断项展示代码证据 + 实际渲染样本，用户逐项拍板）。
> **reviewed_commit：** `bfd1b47`（P0–P3 已提交）；P4 产物 + R3 修订在工作区未提交（提交时机由用户另行指示）。
> **结论：** ✅ **APPROVE（2026-08-16）**——G3 通过，附带 R3-2 修订条件（已实施并验收）；P5 未授权。

---

## 1. 决议摘要

| 判断项 | 结论 | 用户拍板原话 | 附注 |
|---|---|---|---|
| R3-1 诊断段观感（喧宾夺主？） | **通过** | 「通过」 | 密度/语气/位置/门控证据见审查包 §2.2 |
| R3-2 SECTION_PRIORITY 档位 | **通过 + 修订条件** | 「合理，但是阈值要可调整给它留出一个配置空间再config中」 | 档位合理；threshold 从硬编码 3 改为 `[diagnosis] section_budget_threshold` 可配置（默认 3，行为不变） |
| R3-3 结构性隔离（B6） | **通过** | 「成立，通过」 | 独立 store + 前缀隔离 + allowlist 不动 + worker 零可见，证据见审查包 §2.6 |

## 2. 授权范围

**批准后放行：**
- P4 产物正式验收（System Health 注入段全链路）；
- **R3-2 修订条件实施**：`section_budget_threshold` 配置面（契约修订，主方案 §3.3/§5/D5 已更新，新 hash `ffba3034ac0ddcd896911288ab23ae2ab7e56d7dab440cbefa186f6d01a4c3a0`；实施由实现 subagent 完成、父侧独立验收通过，见 §4）；
- 进入 P5（真实模型诊断 smoke 3/3 + read 10-run 矩阵 + 文档收口）——**但 P5 启动仍需用户另行明确指示**（用户原话「随后我再定P5」）。

**不授权（G3 放行边界）：**
- commit/push（提交时机用户另行指示）；
- canonical schema migration、Context cutover 之外的任何结构变更；
- 诊断循环 agentic 框架增强（单轮并行多工具调用，已记录讨论方向，未授权实施）；
- 任何对 H1 reducer、真相隔离、认证边界的改动；诊断进投影事实域（B6 冻结）。

## 3. 用户拍板记录

- 2026-08-16：R3-1「通过」；R3-2「合理，但是阈值要可调整给它留出一个配置空间再config中」→ 修订条件成立；R3-3「成立，通过」。
- 2026-08-16：修订实施授权「先实施修订 随后我再定P5」——仅授权 R3-2 修订实施，P5 待后续指示。
- 修订设计确认：键名 `[diagnosis] section_budget_threshold`，默认 3，与 `min_confidence`（置信度过滤）语义区分；只配置 system_health 自身阈值，`long_term_memory: 3`（G4 冻结值）不动。

## 4. R3-2 修订实施与验收（2026-08-16）

**契约修订**（主方案 §3.3「预算档」、§5「轮次上限与超时」、D5 行）：
- threshold=3 → **默认 3**，可由 `[diagnosis] section_budget_threshold` 配置（D9 精神不硬编码；配置为非 3 时自动解除「与 long_term 同档」语义）；
- 主方案 SHA-256 重算：`9dc21222…` → `ffba3034ac0ddcd896911288ab23ae2ab7e56d7dab440cbefa186f6d01a4c3a0`，三处绑定（进度文档 §1 / 审查包 / 人工审查点补充头部）已回写。

**实现**（实现 subagent，改动面）：
- `contracts.py`：`DiagnosisConfig.section_budget_threshold=3` + validate（int 排除 bool、>=1，`invalid_section_budget_threshold`）；`DiagnosisRuntimeConfig` 同字段；`_DIAGNOSIS_CONFIG_DEFAULTS` 加键；`load_diagnosis_config` int 分支 >=1 校验（`DiagnosisConfigError("invalid_value")`）；
- `environment_state_provider.py`：`EnvironmentStateProvider` 新增 `diagnosis_budget_threshold=3` 参数；`_apply_budget` 提取 `_threshold(name)` 局部函数（system_health 走实例参数，其余走模块常量），`fixed_cap_dropped` 与保留循环两处统一；模块常量 `system_health: 3` 保持（P0 契约守护）；
- `coordinator.py`：`DiagnosisConfig(...)` 构造 + `SARCoordinatorStateProvider(...)` 透传（`else 3` 兜底）；
- `coordinator_state_provider.py`：参数 + 存储 + `EnvironmentStateProvider(...)` 透传；
- `experiment.py`：**零改动**（`diagnosis_tunables=diag_runtime` 已透传，字段随 frozen dataclass 自动携带）；
- `long_term.config`：新增 `[diagnosis]` 段，5 键全显式（inject_enabled/min_confidence/max_rounds/diagnosis_sec/section_budget_threshold=3）带注释；
- 测试新增 8 项：`test_system_health_injection.py`（threshold=2 保留 / threshold=4 裁剪 / 默认行为不变 3 项）+ `test_system_health_diagnosis_validator.py`（默认 3、非法值拒绝、解析 5 项）。

**父侧独立验收（2026-08-16，非 subagent 自报）**：
- diff 逐文件核验：6 个改动文件 + config + 2 个测试文件，改动点与契约一致，无越界改动；
- focused 7 文件：**98 passed**（独立复跑）；
- 全量：**1935 passed, 4 skipped, 0 failed**（独立复跑 145.99s；基线 1927 + 8 新增）；
- ruff：改动文件 23 errors 与 HEAD 基线 **23 = 23，零新增**（worktree vs HEAD 对比实证）；
- P0 契约守护 `test_system_health_budget_threshold_same_tier_as_long_term` 保持 GREEN（模块常量默认 3 未动）。

**规格偏差披露与裁决（父侧）**：
- 任务原文测试 (a)「threshold=2 + budget=2 → TRUNCATED 不为 True」架构上不可达——read 模式 coordinator 视图恒注入 `long_term_memory` 段（P5 行为，空 dict 也注入），长期段阈值 3 > budget 2 → 必裁 → TRUNCATED=True（既有测试 `test_budget_below_long_term_threshold_drops_section_and_truncates` 即此断言）。
- **裁决：接受适配**。测试 (a) 保留核心断言（threshold=2 时 system_health 在 budget=2 保留 = R3 配置生效特征），TRUNCATED 语义另以 budget=3 场景验证（两段都保留、TRUNCATED 不为 True）；docstring 注明原因。此偏差属委派规格未考虑 P5 长期段恒注入行为，非实现缺陷。

## 5. 遗留与下一步

- **P5 待用户明确指示**：真实模型诊断 smoke 3/3 + read 10-run 矩阵（诊断注入实证 + cov/tr 对比，显著退化则打回）+ 文档收口 → R4/G4；
- P4 产物 + R3 修订 + 审查链文档（审查包/审批记录/进度回写）在工作区未提交，提交时机用户另行指示；
- 已记录观察项：DiagnosisLoop 单轮仅 tools[0] 解析（并行多工具调用增强方向），待 R2 结论后另行排期，代码零改动。

---

*审批记录由父 agent 生成（2026-08-16）。G3 通过后 P4 状态转「通过（待提交）」，进度文档 G3 行已回写。*
