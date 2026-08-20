# G4 审批记录 — System Health 诊断通道正式可用宣告（P5 产物验收 + feature 关闭）

> **性质：** 本记录固化 G4（正式可用）的人工审查决议。审查材料为 [`系统健康诊断_agentic审查者_G4-R4-review-packet.md`](系统健康诊断_agentic审查者_G4-R4-review-packet.md)（SHA-256 `2a1fcc6aa9c63dc8d69ee6434295827202f68a27696ad248233a6dd93fe51c35`，结论区已回写 ✅），审查方式为用户带读（父侧逐判断项展示一手核验证据与真实产物，用户逐项拍板）。
> **reviewed_commit：** `42ac461`（P0–P5 全部已提交，`feat/memory-redesign`）；G4 审查链文档（审查包结论区回写 + 本记录 + 进度回写）在工作区未提交，提交时机由用户另行指示。
> **绑定 hash（审批时点终值）：** 主方案 `7c3a4a5eb9fa367b149af55bb21af11b586469633689505df98d1139659cff29`；人工审查点补充 `aef1f3d65d4636c46cf0b4782e41ce857aaf0d2ce39636f59736c313db249e96`；进度文档 `d91f44132a7ecc95fc5ef862e0d2c739862cbfce42a5247fa1eac291385ad49b`（`review_progress_sha256_current`：`894a3793725081ed0f620a1332e77f6154d07cd4a020b6787d6a7e1b1cd84552`——2026-08-17 diagnosis deadline 代码修复、focused/full/ruff 与 targeted smoke 验收记录追加，非 G4 冻结语义变更，沿用 H3 dual-hash 先例）。
> **产物锚点：** smoke `sar_orch/results/diagnosis_smoke_20260816_092513.json`；read 矩阵 `sar_orch/results/long_term_memory_read_20260816_172405/`（含 `analysis_p5.json`）；G4 基线 `sar_orch/results/long_term_memory_read_20260812_200708/`。
> **结论：** ✅ **APPROVE（2026-08-16）**——R4 四判断项全过、**无修订条件**；宣告 System Health 诊断通道（agentic 审查者）**正式可用**（真实 SAR run 随 `--long-term-mode != off` 自动接线，非独立 CLI 参数），**本 feature 关闭**（P0–P5 全链完成，G4 为终 Gate）。

---

## 1. 决议摘要

| 判断项 | 结论 | 用户拍板原话 | 附注 |
|---|---|---|---|
| R4-1 smoke 样本质量（建议安全性核心） | **通过** | 「通过」 | 3/3 ok、rounds 1-2、avg 19.74s；finding 与 fixture 语义对齐、refs 窗口内真实事件（validator 强制）、零 truth 词；与 R2 已 APPROVE smoke 同质复验，证据见审查包 §2.1 |
| R4-2 read 矩阵注入实证 + 无退化判定 | **通过** | 「通过」 | 10/10 完成、三码全 0、worker 零泄漏、SH 段与 store 一致；avg cov 0.741 vs 0.781 / avg tr 0.725 vs 0.783（delta<0.1，同长期记忆 G4 口径），证据见审查包 §2.2 |
| R4-3 披露项接受 | **接受** | 「接收」 | 4/10 零注入 = D8 设计内（3 run 90s 超时丢写 + 1 run 末期落库无后续请求）；2 个 4-agent run 波动 = LLM 随机性 + max_steps=20 截断，诊断精确命中真实失败、无误导迹象；不构成打回，增强属未来排期 |
| R4-4 G4 正式可用宣告 | **批准** | 「批准」 | 文档收口完成、全量 1945 passed 零回归（带读期间父侧新鲜复跑 168.30s）、前置链 R0–R3 全 APPROVE |

## 2. 授权范围

**批准后放行（G4 范围，同长期记忆 G4 惯例）：**
- 宣告 System Health 诊断通道**正式可用**：真实 SAR run 默认可用（随 `--long-term-mode != off` 自动接线）；作业文档（AGENTS.md / `docs/system_docs/memory.md` / issues.md）已收口；
- **关闭本 feature**：诊断通道 P0–P5 全链完成，R0–R4 / G0–G4 全通过。

**不授权（G4 放行边界，批准不自动授权以下任何一项）：**
- commit/push——G4 审查链文档（审查包结论区回写、本记录、进度回写）在工作区未提交，**提交时机由用户另行指示**；
- canonical schema migration、Context cutover、H1 reducer / 真相隔离 / 认证边界的任何改动（冻结不变）；
- 诊断循环 agentic 框架增强（单轮并行多工具调用——讨论方向已记录于进度 §1，未授权实施，代码零改动）；
- 注入覆盖率优化（更大 `diagnosis_sec` 等——4/10 run 零注入属 D8 设计内，增强属未来排期）；
- `inject_enabled` 消融实测（A2 旋钮已备，启动需用户明确指示）；
- worker 侧任何诊断可见性（ACL 冻结不变）。

## 3. 用户拍板记录

- 2026-08-16（带读逐项拍板）：R4-1「通过」；R4-2「通过」；R4-3「接收」（=接受披露项）；R4-4「批准」。
- 带读中途用户追问证据完整性（「日志那些是否都齐全」）与机制确认（「反思确实真的可以识别错误原因并指一个正确的方向对嘛」）——父侧补做产物完整性盘点（10/10 run 标准 9 件 + memory 导出 10 件 + 三 sqlite + coordinator/worker ndjson 全齐；真实性三件套：TotalSteps=20、llm_request 12-25/run 与注入表逐格吻合、agent_interactions 2791-9116 行/run）与证据边界澄清（识别错误原因已实证；指正确方向为方向合理性实证，改进效果待 `inject_enabled` 消融）。
- 带读中瑕疵修复（符合联合审查惯例「能直接修复的先修掉」，设计语义零改动）：①补充文档绑定值 `2d571202…` 为 G3 审批时刻工作区中间值（从未入库），修正为 fd7594f 提交版 `aef1f3d6…`——审查包头部/§6 + 进度 §1 三处回写；②审查包 §2.3 memory.md「24 行新增 0 删除」修正为 numstat 实测 +13/-1；③审查包 §2.2 补基线口径注记（基线目录含 steps=0 死 run `scene_4_agents_2` + `_rerun` 完成版，均值剔除死 run，父侧复算 0.7815/0.7832 与声称一致）。

## 4. 父侧独立核验台账（带读过程中完成，非抄审查包自述）

| 声称 | 核验方式 | 结果 |
|---|---|---|
| HEAD `42ac461` / 主方案 `7c3a4a5e…` / 补充 `aef1f3d6…` | `git log` + `sha256sum` | ✅（补充绑定值原滞后，已修正） |
| smoke summary（gate_passed、3/3 ok、0 rejected/timeout/exhausted、avg 19.74s） | JSON 全文重读 | ✅ 逐字段一致 |
| smoke 三条 run rounds/latency/conf/target/refs | JSON run 级字段直读 | ✅ refs 全为窗口内 evt_* 事件 |
| 矩阵 per-run 表格 10 行 + avg 0.741/0.725 | 10 个 summary.csv 独立复算 | ✅ 逐格一致 |
| G4 基线 0.781/0.783 | 基线目录独立复算（剔除 steps=0 死 run 取 rerun） | ✅ 0.7815/0.7832 |
| 错误码三码全 0 + acceptance_gate=pass + failed_tool_rows=0 | 直读 `run_metrics.json` `memory_terminal.acceptance` | ✅ 10/10 |
| worker 零泄漏 | 全量 grep 10 run 的 workers/ 目录 | ✅ 零命中（命中仅 6 个 coordinator 目录，与注入表一致） |
| SH 段内容（scene_2_agents_2） | coordinator ndjson `messages[]` 实录抽取 | ✅ 与包内引文逐字一致 |
| 零注入归因（3 timeout + 1 末期落库） | 直读 4 个 run 的 `run_metrics.long_term_reflection.diagnosis` | ✅ 3 run `timeout/written=0/diagnosis_sec_exceeded` + scene_3_agents_4 `ok/written=1` 且零 SH 命中 |
| 波动 run 诊断无误导（scene_4/5_agents_4） | 直读两 run `diagnosis.sqlite3` 全文 | ✅ 4 条诊断精确命中真实失败模式（3 区旧计划 vs 14 区新地图未重规划；[0,0,0] 占位坐标污染投影），suggestion 方向合理 |
| 全量回归 1945 passed / 4 skipped / 0 failed | 带读期间后台独立复跑 | ✅（168.30s，与包内数字逐字一致） |
| 文档收口 4 文件 31+/7- | `git show --numstat 42ac461` | ✅ |
| 假完成三件套 | TotalSteps=20 全 ✅；coordinator llm_request 12-25/run（与注入表「总」列逐格吻合）；run log 有 Step 推进 + `Cleanup complete` + `end_reason=max_steps_reached` | ✅ |

## 5. 披露与残留（G4 后状态）

- **注入覆盖率**：短 run（max_steps=20）40% 零注入，全部 D8 设计内（超时丢写不阻塞 / 末期落库无后续请求）；增强（单轮并行多工具、更大 `diagnosis_sec`）属未来排期，不阻塞正式可用。
- **per-run 波动**：scene_4_agents_4 / scene_5_agents_4 较基线大幅下降，判定 LLM 随机性 + 截断；两 run 诊断精确命中真实失败，反证诊断通道抓错能力。如需更强结论可更长 max_steps 复测（另行排期）。
- **基线口径**：G4 基线目录含 1 个 steps=0 死 run（`scene_4_agents_2`）+ `_rerun` 完成版；均值口径 = 剔除死 run 取 rerun（本记录与审查包 §2.2 均已注明）。
- **数据保留**：诊断 store 随 run 日志保留，手动清理责任（同长期记忆惯例）。
- **观察项（未授权，保留至未来边界讨论）**：DiagnosisLoop 单轮并行多工具增强；注入覆盖率优化；`inject_enabled` 消融实测（验证「诊断使系统变好」的因果证据——当前证据为「无退化 + 方向合理」）。

## 6. 前置链（R0–R3 全 APPROVE，快照）

| Gate | 结论 | 关键 |
|---|---|---|
| G0 | ✅ 2026-08-13 | D1–D10 + A1–A6 全拍板，主方案冻结 |
| G1 | ✅ 2026-08-14 | 决策事件通道（P1）父侧验收覆盖 |
| G2 | ✅ 2026-08-14 | 诊断 store + agentic 循环（P2/P3），R2 三判断项通过 |
| G3 | ✅ 2026-08-16 | 注入与权威性（P4），R3 三判断项通过 + R3-2 配置面修订实施验收；独立交叉审查 0 Blocker / 4 Major 全修复 |

---

*审批记录由父 agent 生成（2026-08-16）。G4 通过后进度文档头部/§1/§3/§5 已回写「feature 关闭」；审查链文档提交时机待用户另行指示。*
