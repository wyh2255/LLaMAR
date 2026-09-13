# G4/R4 审查包 — System Health 诊断通道正式可用（P5 产物，smoke 3/3 + read 10-run 矩阵 + 文档收口）

> **性质：** 本文件是人工必要审查点 R4（G4）的审查材料，由父 agent 基于 P5 真实运行产物（真实模型 smoke 3/3 + read 10-run 矩阵 + 文档收口）、父侧独立核验（不采信 subagent 自报，全部重查原始产物）生成，提交用户审批。批准后宣告 System Health 诊断通道（agentic 审查者）**正式可用**（同长期记忆 G4 惯例），feature 关闭。
> **绑定基线：** HEAD `42ac461`（P0–P4/R3-2/修复已提交，P5 收口提交 `42ac461` docs-only，2026-08-16）；主方案 SHA-256 `7c3a4a5eb9fa367b149af55bb21af11b586469633689505df98d1139659cff29`；人工审查点补充 SHA-256 `aef1f3d65d4636c46cf0b4782e41ce857aaf0d2ce39636f59736c313db249e96`（fd7594f 提交版；旧绑定值 `2d571202…` 为 G3 审批时刻工作区中间值，2026-08-16 R4 审查时修正）；进度文档 [`系统健康诊断_agentic审查者_实施进度.md`](系统健康诊断_agentic审查者_实施进度.md)（P5 行已更新）。G3 审查包 [`系统健康诊断_agentic审查者_G3-R3-review-packet.md`](系统健康诊断_agentic审查者_G3-R3-review-packet.md)（R3 已 APPROVE 2026-08-16）。
> **结论：** ✅ **APPROVE（2026-08-16）**——R4 四判断项用户带读逐项拍板：R4-1 smoke 样本质量「通过」/ R4-2 注入实证+无退化「通过」/ R4-3 披露项「接收」/ R4-4 G4 正式可用宣告「批准」，**无修订条件**；审批记录 [`系统健康诊断_agentic审查者_G4-approval-record.md`](系统健康诊断_agentic审查者_G4-approval-record.md)。审查中带读瑕疵修复 2 项（补充文档 hash 绑定修正为 `aef1f3d6…`、§2.3 numstat 数字修正）+ §2.2 基线口径注记，设计语义零改动。

---

## 1. 审批范围（精确到边界）

**批准后放行（G4 范围，同长期记忆 G4 惯例）：**
- 宣告 System Health 诊断通道**正式可用**：真实 SAR run 默认可用（随 `--long-term-mode != off` 自动接线，非独立 CLI 参数）；作业文档与 AGENTS.md 已收口（§2.3）；
- 关闭本 feature（诊断通道 P0–P5 全链完成，G4 为终 Gate）。

**不授权（G4 放行边界）：**
- commit/push（P5 收口提交 `42ac461` 已完成；后续改动提交时机由用户另行指示）；
- canonical schema migration、Context cutover、H1 reducer / 真相隔离 / 认证边界的任何改动（冻结不变）；
- 诊断循环 agentic 框架增强（单轮并行多工具调用——讨论方向已记录，未授权实施，代码零改动）；
- 注入覆盖率优化（4/10 run 零注入属 D8 设计内，见 §2.2 披露与 §4 残留风险；增强属未来排期）；
- worker 侧任何诊断可见性（ACL 冻结不变）。

## 2. 证据汇总（全部真实产物，父侧独立核验）

### 2.1 P5-1 真实模型诊断 smoke 3/3（G4 判断项 ①）

**证据：** `sar_orch/results/diagnosis_smoke_20260816_092513.json`（父侧全文重读；exit 0；模型 deepseek-v4-flash，api_key `<set>` 未泄露）。

- **summary**：`gate_passed=true`，ok_runs=3，rejected=0 / timeout=0 / rounds_exhausted=0，round_distribution `{"ok": 3}`，avg_latency_sec=19.74；
- **三条 run（均 status=ok、reason=None、port_error=None）**：

| run | variant | rounds / latency | target / conf | finding 全文 | suggestion 全文 | refs |
|---|---|---|---|---|---|---|
| 1 | a | 1 / 13.27s | coordinator / 0.75 | "The coordinator dispatched task 'dispatch alpha to sector 7' (dispatch-1) for the warehouse fire, but the dispatched task went stale (supervision.TASK_STALE) with no worker acknowledgement, completion, or follow-up. The control journal is empty despite a control.dispatch.RUNNING event, indicating no state transition was journaled and the stale dispatch was never recovered or re-dispatched." | "Investigate why worker alpha never picked up dispatch-1, ensure every control dispatch records its RUNNING->terminal transition in the control journal, and add a staleness recovery path (re-dispatch, reassign, or escalate) when supervision flags a task as stale." | evt_ctl_1 / evt_dec_1 / evt_sup_1（窗口内） |
| 2 | b | 1 / 22.37s | coordinator / 0.80 | "The coordinator responded to alpha being blocked by issuing a 'reroute via east' decision tied to 'dispatch-1', but the committed control journal contains no dispatch-1 entry and the only dispatch event (control.dispatch.RUNNING) carries no dispatch_id; immediately after the decision, supervision reported alpha WORKER_UNREACHABLE, so the reroute plan is ungrounded and cannot be executed by alpha." | "Validate dispatch state against the control journal before issuing task-specific decisions; upon WORKER_UNREACHABLE, re-dispatch the task to an available worker and journal the corresponding control transition instead of relying on an unreachable worker." | evt_ctl_1 / evt_cb_1 / evt_dec_1 / evt_sup_1（窗口内） |
| 3 | c | 2 / 23.57s | coordinator / 0.75 | "The coordinator updated the plan for the \"extinguish chemical spill at depot\" task with only the objective \"extinguish\", failing to incorporate committed evidence that the material is non-chemical and the callback that the worker needs sand (not water) to extinguish it. The plan omits the required resource substitution, and no supervision event was recorded to reconcile the plan against this conflicting evidence." | "Revise the plan node to explicitly specify the extinguishing agent (sand/dry material instead of water) based on the non-chemical material evidence and the worker status update, and trigger a supervision check to reconcile the plan with the evidence before dispatch." | evt_cb_1 / evt_dec_1 / evt_ev_1 / evt_ctl_1（窗口内） |

- **父侧判断**：三条 finding 与各自 fixture 证据语义对齐（a：stale 无 ack + journal 空；b：reroute 无 journal 记录 + worker unreachable；c：计划忽略 non-chemical/sand 证据），suggestion 方向合理、可直接执行；refs 全部指向窗口内真实事件（validator 强制）、零 truth 词、无幻觉迹象。**与 R2 已 APPROVE 的 smoke（`diagnosis_smoke_20260813_084820.json`）同质复验通过。**

### 2.2 P5-2 read 10-run 矩阵（G4 判断项 ②，含披露）

**证据：** `sar_orch/results/long_term_memory_read_20260816_172405/`（脚本 `run_g3_read_matrix.sh`，5 scenes × agents{2,4} × seed 42，max_steps=20，`--long-term-mode read`；脚本退出码 0，10/10 rc=0）；聚合报告 `analysis_p5.json` 同目录（父侧独立复算 summary.csv 交叉核对一致）。

| run | steps | FinalCoverage | FinalTransportRate | Finished | 框架错误码（worker_busy / task_not_routable_yet / unknown_task_id） | SH 注入（含段 llm_request / 总） |
|---|---|---|---|---|---|---|
| scene_1_agents_2 | 20 | 0.833 | 0.800 | False | 0 / 0 / 0 | 0 / 18 |
| scene_1_agents_4 | 20 | 1.000 | 0.933 | False | 0 / 0 / 0 | 0 / 14 |
| scene_2_agents_2 | 20 | 0.667 | 0.667 | False | 0 / 0 / 0 | 6 / 23（26%） |
| scene_2_agents_4 | 20 | 1.000 | 0.867 | False | 0 / 0 / 0 | 11 / 19（58%） |
| scene_3_agents_2 | 20 | 0.714 | 0.722 | False | 0 / 0 / 0 | 0 / 14 |
| scene_3_agents_4 | 20 | 1.000 | 0.944 | False | 0 / 0 / 0 | 0 / 22 |
| scene_4_agents_2 | 20 | 0.500 | 0.600 | False | 0 / 0 / 0 | 7 / 12（58%） |
| scene_4_agents_4 | 20 | 0.500 | 0.500 | False | 0 / 0 / 0 | 4 / 20（20%） |
| scene_5_agents_2 | 20 | 0.800 | 0.714 | False | 0 / 0 / 0 | 17 / 25（68%） |
| scene_5_agents_4 | 20 | 0.400 | 0.500 | False | 0 / 0 / 0 | 8 / 18（44%） |
| **avg** | 20 | **0.741** | **0.725** | 0/10 | **全 0** | 6/10 run 实际注入 |

**父侧独立核验**（不采信 subagent 自报）：
- 错误码：10/10 run 权威 `run_metrics.json` `framework_error_counts` 三码全 0，且 `acceptance_gate=pass`、`failed_tool_rows=0`（独立读取，非 grep 日志）；
- worker 零泄漏：全量 grep 10 run 的 `Alice/` `Bob/` 目录，`System Health` 出现文件数 **0**（ACL 双门控端到端实证）；
- SH 段真实内容：抽查 scene_2_agents_2 一条含段 llm_request —— `### System Health\ncoordinator: Coordinator issued an overlapping and mis-specified assist dispatch: dsp_61e2a15e assigned Bob to fight T…`，与 diagnosis store 记录（conf=0.85）一致，格式符合 R3 渲染规范（一行一诊断）。

**与 G4 基线对比（无退化判定，同 G4 口径：avg delta < 0.1）：**

| 指标 | P5（诊断注入 read） | G4 基线（`long_term_memory_read_20260812_200708`，无诊断） | delta |
|---|---|---|---|
| avg FinalCoverage | 0.741 | 0.781 | **-0.040** |
| avg FinalTransportRate | 0.725 | 0.783 | **-0.059** |

- 判定：同量级、无框架错误 → **无退化判定成立**（avg 口径，同长期记忆 G4 先例：shadow 0.885 → read 0.781 / 0.837 → 0.783 亦判 pass）；
- 基线口径注记：G4 基线目录含 1 个 steps=0 死 run（`scene_4_agents_2`，另有 `_rerun` 完成版）；均值口径 = 10 run 剔除死 run 取 rerun（父侧独立复算 0.7815/0.7832 与声称一致）；
- **披露（R4 判断项 ③）**：per-run 有 2 个 4-agent run 较基线大幅波动——scene_4_agents_4（cov 1.0→0.5、tr 1.0→0.5）、scene_5_agents_4（cov 1.0→0.4、tr 0.929→0.5）。其诊断 suggestion 方向合理、无误导迹象（scene_4_agents_4 两条：「规划基于不完整地图（RedFire 仅 3 区、地图已扩至 14 区）」→ 建议按新投影重规划重派发；scene_5_agents_4 两条：「占位坐标 [0,0,0] 被提交为已定位」→ 建议按 sensor 证据修正并拒绝占位值），判定为 **LLM 随机性 + max_steps=20 截断**（两代基线 Finished 均 0-1/10，run 走向本身波动大），avg 同量级判定成立。

### 2.3 P5-3 文档收口 + 全量回归（G4 判断项 ④）

| 文件 | 改动 | 父侧复验 |
|---|---|---|
| `AGENTS.md` | Options 区「System Health 诊断通道」说明 + Key Gotchas `official since P5` 条目 | diff 逐字核验：数值与证据一致、既有内容零改动 |
| `docs/system_docs/memory.md` | §1 概述第五通道注记 + §7 ✅ 已实施块 + §15 交叉引用 | 同上（numstat 实测 +13/-1） |
| `docs/project_notes/issues.md` | 2026-08-16 工作日志 | 同上 |
| 进度文档 | P5 行 未开始→通过、G4 行 待R4、变更日志 | 父侧更新 |

- 全量回归：`pytest tests/ -q` → **1945 passed, 4 skipped, 0 failed**（137.53s，父侧独立复跑，2026-08-16，P5 提交前确认；与 fd7594f 基线一致）；ruff 23=23 零新增（2026-08-16 独立审查修复后基线）。

### 2.4 G4 放行的前置链（R0–R3 已 APPROVE，快照）

| Gate | 结论 | 关键 |
|---|---|---|
| G0 | ✅ 2026-08-13 | D1–D10 + A1–A6 全拍板，主方案冻结 |
| G1 | ✅ 2026-08-14 | 决策事件通道（P1）父侧验收覆盖 |
| G2 | ✅ 2026-08-14 | 诊断 store + agentic 循环（P2/P3），R2 三判断项通过 |
| G3 | ✅ 2026-08-16 | 注入与权威性（P4），R3 三判断项通过 + R3-2 配置面修订实施 |

## 3. 哪些已验证、哪些不在本 Gate 范围

**已验证（本 Gate 交付）：** 诊断 smoke 样本质量（§2.1）；read 矩阵注入实证 + 错误码 + worker ACL（§2.2）；cov/tr 无退化（§2.2）；文档收口 + 全量回归（§2.3）。
**不在本 Gate 范围（不阻塞放行）：** 注入覆盖率提升（4/10 零注入）、单轮并行多工具增强、更长 max_steps 复测（G4 基线同口径，如需更强结论可另行排期）。

## 4. 风险与残留（G4 后状态）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 诊断误导决策（建议安全性） | 样本质量 3/3 ×2 轮 smoke + 置信度门控 0.6 + 预算 5% + read 矩阵 avg cov/tr 无退化 | **已评估**（本 Gate） |
| 注入覆盖率低（短 run 40% 零注入） | D8 语义：诊断是增强非必需、超时丢写不阻塞；3/10-run 属诊断循环 90s 超时（`run_metrics diagnosis.timeout/diagnosis_sec_exceeded/written=0`）、1/10-run 属诊断末期落库无后续 llm_request（scene_3_agents_4 诊断写入 10:03:08 > 最后请求 10:02:54）——均设计内行为 | **已确认**（D8 接受于 R2） |
| per-run 性能波动（2 个 4-agent run 大幅下降） | avg 同量级判定（同 G4 口径）；诊断 suggestion 无误导迹象；如需更强结论可更长 max_steps | 已披露（§2.2） |
| 回声室（诊断影响决策→决策进反思→新诊断） | 三重防护：source_ref 禁引诊断 + `diagnosis.audit` 独立前缀 + 时间线视图过滤 | 已实现 + 测试（G3 复验） |
| 保留膨胀 / 数据保留 | 诊断 store 随 run 日志保留（手动清理责任，同长期记忆惯例） | 接受 |

## 5. 需用户拍板的决策项（R4 判断项）

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| R4-1 | **smoke 样本质量（建议安全性核心）** | 3/3 ok，rounds 1-2、avg 19.74s；三条 finding 语义对齐、suggestion 方向合理、refs 窗口内、零 truth 词无幻觉（§2.1） | 通过：样本质量满足「只吃原始证据、不吃以往自己产出」语义；R2 + R4 两轮 smoke 同质 | 是否通过 |
| R4-2 | **read 矩阵注入实证 + 无退化判定** | 10/10 完成、错误码三码全 0、worker 零泄漏；6/10 run 实际注入（20-68%）、SH 段与 store 一致；avg cov 0.741 vs 0.781、avg tr 0.725 vs 0.783（delta<0.1） | 通过：链路端到端实证成立、无退化成立（同 G4 口径） | 是否通过 |
| R4-3 | **披露项接受**（4/10 零注入 D8 预期 + per-run 2 个 4-agent run 波动） | 零注入全为 D8 设计内（3 timeout + 1 末期落库）；波动 run 诊断无误导迹象、avg 抹平 | 接受：不构成打回；增强（单轮并行/更大 diagnosis_sec）属未来排期 | 是否接受 |
| R4-4 | **G4 正式可用宣告** | 文档收口完成（§2.3）、全量 1945 passed、前置链 R0–R3 全 APPROVE | 批准：宣告 System Health 诊断通道正式可用，feature 关闭 | 是否批准 |

## 6. 证据清单

- smoke：`sar_orch/results/diagnosis_smoke_20260816_092513.json`（3/3 gate，exit 0）
- read 矩阵：`sar_orch/results/long_term_memory_read_20260816_172405/`（10 run 目录 + `analysis_p5.json` + `run_scene*.log`）；G4 基线 `sar_orch/results/long_term_memory_read_20260812_200708/`
- 全量回归：`env -u PYTHONPATH PYTHONPATH="src" uv run pytest tests/ -q` → 1945 passed, 4 skipped, 0 failed（137.53s，父侧独立复跑）
- 文档收口：`git show 42ac461`（4 文件 31+/7-，docs-only）
- 主方案/审查点/进度：`.hermes/plans/系统健康诊断_agentic审查者/` 下 `实施方案.md`（SHA-256 `7c3a4a5e…`）、`实施方案补充-人工必要审查点.md`（`aef1f3d6…`）、`实施进度.md`；G3 审查包与审批记录同目录
- P0–P4 提交：`bfd1b47` / `0f30127` / `fd7594f`；P5 收口：`42ac461`

---

*本审查包由父 agent 生成（2026-08-16），提交用户对 G4/R4 审批。审批结论（APPROVE / REJECT / REVISE + 授权范围 + 用户原话）将写入审批记录并回写进度文档。*