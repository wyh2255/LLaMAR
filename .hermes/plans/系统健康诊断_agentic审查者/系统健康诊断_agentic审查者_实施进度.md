# 系统健康诊断 · agentic 审查者 — 实施进度记录

> **用途：** 跟踪以下设计文件的 Phase/Gate 状态与验收证据：
> - [`系统健康诊断_agentic审查者_实施方案.md`](系统健康诊断_agentic审查者_实施方案.md)
> - [`系统健康诊断_agentic审查者_实施方案补充-人工必要审查点.md`](系统健康诊断_agentic审查者_实施方案补充-人工必要审查点.md)
> - 代码事实探索 01/02/03（同目录，只读，2026-08-13）
>
> **边界：** 本文件只记录状态、Gate 与可复核证据；设计语义只在主方案及补充中变更。不得用本文件替代 approval record 或真实验收产物。可复用经验另入 `经验.md`/skill，不在此累积。
>
> **当前状态：** G0 已通过（2026-08-13，D1–D10 全拍板）；独立审查 3 维度完成并修订主方案；**修订决策项 A1–A6 全部拍板**（A1 追认 G4 边界；A2 独立旋钮 `[diagnosis] inject_enabled` 供消融实验；A3–A6 按建议）；**B6/B9 结构性隔离修订 + R0–R4 审查点已插入主方案 §9.1**。主方案 SHA-256 `9dc21222bb9611584580a1d0801d0b670dae8362dccd904cfe25e022f89edd74`（**2026-08-16 R3 修订后重算：`ffba3034ac0ddcd896911288ab23ae2ab7e56d7dab440cbefa186f6d01a4c3a0`**）。**用户授权「启动 P1，且后续 P2/P3 连续做，到 R2 前才停」（2026-08-13）——P0/P1/P2/P3 全部完成并通过父侧独立验收，已提交 `bfd1b47`（2026-08-14）**；**R1 视为已被父侧验收覆盖、不再单审（用户拍板），G1 随 P1 验收通过**；**R2 已通过（2026-08-14，三判断项全拍板：样本质量通过 / 防回声室充分 / D8 降级接受，完整 drain 实测放 R4）**；**P4（System Health 注入段）已完成并通过父侧独立验收（2026-08-15，injection 8/8 GREEN、全量 1927 passed 零回归、ruff 新增行零错误）**；**R3/G3 已通过（2026-08-16，三判断项全拍板：①观感通过 ②档位通过+修订条件 ③B6 隔离通过）**；**R3-2 修订（`[diagnosis] section_budget_threshold` 配置面，默认 3）已实施并通过父侧独立验收（2026-08-16，全量 1935 passed 零回归、ruff 零新增）**；**P5 已完成（2026-08-16，父侧独立验收通过：真实模型 smoke 3/3 + read 10-run 矩阵 10/10 + 文档收口 hunk 逐字复验通过）**；**R4/G4 已通过（2026-08-16，四判断项用户带读全拍板、无修订条件）——System Health 诊断通道正式可用宣告，本 feature 关闭**（审批记录 [`系统健康诊断_agentic审查者_G4-approval-record.md`](系统健康诊断_agentic审查者_G4-approval-record.md)）。G4 审查链文档（审查包结论区回写 + 审批记录 + 本文件）在工作区未提交，提交时机用户另行指示。
>
> **更新约定：**
> - 进度三态按“实施中 / 验证中 / 可提交”汇报，并附真实产物路径或命令输出；
> - 没有独立测试和真实 smoke 证据，任何 Phase 不得标记为“通过”；
> - Gate 必须记录日期、绑定 HEAD/hash、审查材料、用户决议、授权范围与遗留风险；
> - Gate 打回时区分“契约修订（回设计）”与“实现缺陷（直接修复）”。

---

## 1. 当前位置

- 阶段：**本 feature 全部完成并关闭**——P0–P5 + R0–R4/G0–G4 全通过（**R4/G4 通过 2026-08-16**：System Health 诊断通道正式可用宣告，随 `--long-term-mode != off` 自动接线）。提交状态：P0–P3 `bfd1b47`、P4 + R3-2 + 修复 `fd7594f`、P5 文档收口 `42ac461` 均已提交；**G4 审查链文档（审查包/审批记录/本文件）在工作区未提交，提交时机用户另行指示**；结果产物不入库。
- 绑定 HEAD：`42ac461`（`feat/memory-redesign`，P0–P5 全部已提交；G4 审查链文档在工作区未提交，结果产物不入库）
- 设计证据冻结：
  - 主方案 SHA-256（R0–R4 插入 + B6/B9 修订 + **2026-08-16 R3 修订：section_budget_threshold 配置面** + **2026-08-16 review 修正：§3.3 行号/表述**后）：`7c3a4a5eb9fa367b149af55bb21af11b586469633689505df98d1139659cff29`
  - 人工审查点补充 SHA-256：`aef1f3d65d4636c46cf0b4782e41ce857aaf0d2ce39636f59736c313db249e96`（fd7594f 提交版；旧绑定 `2d571202…` 为 G3 审批时刻工作区中间值，2026-08-16 R4 审查修正）
  - 代码事实探索 SHA-256：01 `67ee73eeb7650a1c6d1ff543dfbac7e55f61eb9910d2ec7ca7b2fc11bb984f2e`（含 superseded 注记）、02 `6bdb39d3bdec17dbd9b4830144ef24ff8a99f0d16469027409443dd5a40f7028`、03 `afae83a2b44afad821610014d27189a59a25457f992a819c15a9bc68505cf08d`
- 用户拍板记录（2026-08-13）：
  - G4 边界讨论：C（AgentCard mutation）维持 V1；A（O-A）升级为 agentic 诊断通道，九条方向全锁定（记录于长期记忆进度文档 §1）
  - G0：D1 补 activate_plan_node + update_plan；D2–D10 按建议
- 用户拍板记录（2026-08-14）：R1（决策事件通道）**视为已被父侧验收覆盖、不再单审**——G1 随 P1 验收通过；决策事件采样审查需求由 P1 验收证据（decision_events 16/16 GREEN + 全量零回归）覆盖
- 用户拍板记录（2026-08-14，R2 审查中）：**判断项 2 防回声室语义充分性——通过**。用户确认「只吃原始证据，不吃以往自己的产出」：机制级三重防护（validator 拒绝诊断引诊断 + 前缀隔离 + 时间线视图过滤）充分；内容级回声残余风险由置信度门控 + 人工抽检 + R4 read 矩阵兜底
- 用户拍板记录（2026-08-14，R2 审查中）：**判断项 3 多轮超时 vs drain 冲突——接受 D8 降级策略**（丢弃不阻塞 + terminal 降级单轮）；真实冲突程度推算余量薄（3 轮最坏 ~54s vs drain 60s），**完整接线 drain 时序实测放 R4 强制材料**
- 用户拍板记录（2026-08-16，R4/G4 带读逐项拍板）：R4-1 smoke 样本质量「通过」；R4-2 矩阵注入实证 + 无退化「通过」；R4-3 披露项「接收」（4/10 零注入 D8 设计内 + 2 个 4-agent run 波动 = LLM 随机性，诊断无误导，不构成打回）；R4-4 G4 正式可用宣告「批准」——**System Health 诊断通道正式可用，feature 关闭**，无修订条件。审批记录 [`系统健康诊断_agentic审查者_G4-approval-record.md`](系统健康诊断_agentic审查者_G4-approval-record.md)
- 讨论记录（2026-08-16，G4 后边界讨论，**用户拍板 2026-08-16：① C / ② B / ③ 放缓**，②实施待另行授权）：**③ 消融实验设计（已记录，用户指示「放缓」——设计保留，节奏后续另定）**——实验组 = P5 矩阵（`inject_enabled=true`，`long_term_memory_read_20260816_172405/` 已有）；对照组 = `[diagnosis] inject_enabled=false` 新跑 10 run（同口径：5 scenes × agents{2,4} × seed 42 × max_steps=20 × `--long-term-mode read`）。关键口径：inject_enabled 只关注入、不关循环（coordinator.py:619-621 → environment_state_provider.py:445-452）——循环照跑、store 照写，诊断仅不进 Context；测的是「注入的因果效应」，循环成本不在对比内。指标：avg cov/tr（G4 口径）+ **诊断-决策遵循度**（两组 store 均有诊断，可离线对比 coordinator 行为 vs 诊断建议——比 cov/tr 更敏感的直接证据）+ 建议安全性质性抽检。局限：seed 42 固定，LLM 随机性下 delta 噪声大，先跑 42 看信号再扩 seeds。**拍板结论**：①单轮并行多工具 = **C（先不动，等数据）**——共同论证弱点 = 未量化真实 run 单轮 latency 分布（smoke 13-23s 但矩阵 timeout run 只跑 1-2 轮即超 90s，真实单轮可能 60s+）；若瓶颈在单轮 latency 而非「白查一轮」，并行化收益有限。②diagnosis_sec = **B（先补观测再动手）**——run_metrics 补「timeout 发生于滚动 or terminal drain」触发类型记录（terminal 受 drain 60s 约束，若 timeout 全在 terminal 则调大无效）；**补观测属代码改动，实施待用户另行授权**。③ 消融放缓。
- 讨论记录（2026-08-14，方向已定、**未授权实施**）：**DiagnosisLoop agentic 框架增强方向**——①单轮可并行多工具调用：现 port 解析只匹配 `tools[0]`（reflection.py:343-346，非 tools[0] 调用被静默跳过），底层 response.tool_calls 已透传多调用（reflection.py:335），改动面 = port 解析层或 DiagnosisLoop 自解析，收益 = 一轮查多证据（projection+temporal+supervision+journal 并行）→ 减少轮数 → 缓解 drain 时序压力；②`max_rounds`/`timeout` 保留配置空间：现状已流出（`[diagnosis] max_rounds=3 / diagnosis_sec=90`，contracts.py:1055+），并行化改造后继续保留。**待 R2 审查结论后另行排期，代码零改动**
- 下一个动作：**无待实施项（2026-08-16 R4/G4 通过，feature 关闭）**；原「R4 人工审查点停」已结案（四判断项全过，§3 G4 行 + G4 审批记录）。未授权观察项保留（2026-08-16 拍板状态：①单轮并行多工具 = C 先不动等数据；②diagnosis_sec = B 先补 run_metrics 观测再定调参，**补观测实施待另行授权**；③`inject_enabled` 消融 = 放缓，设计已记录节奏另定）。R2 决议依据（历史，已审）：①真实模型诊断 smoke 3/3（`sar_orch/results/diagnosis_smoke_20260813_084820.json`）：run1（variant_a）target=coordinator conf=0.75「dispatch 未 journal/无 ack、supervision id 不匹配」→ sugg journal 一致化+对齐 id+解决 stale；run2（variant_b）target=system conf=0.72「control journal 空、dispatch 未跟踪」→ sugg 落 journal+查 worker 可达性；run3（variant_c）target=coordinator conf=0.72「计划 objective 与证据矛盾（bob 需 sand 非 water、chemical non-chemical）」→ sugg 按证据修订 plan。三条全部 refs 指向窗口内真实事件、零 truth 词、finding 站得住无幻觉；②防回声室测试（echo_chamber_source_ref rejected + collector 拒绝 diagnosis.*）→ 判断项 2 通过；③轮次分布实测（3/3 全 1 round）+ drain 时序推算（3 轮最坏 ~54s vs 60s）→ 判断项 3 接受 D8，完整接线实测放 R4。**P4 已完成（2026-08-15）**。
- 已结束历史：长期记忆 G0–G4 是本 feature 的前置（`long_term_mode=read` 正式可用）；其进度见 `长期记忆_反思机制+动态Agentcard接入_实施进度.md`。

## 2. Phase 状态表

状态枚举：未开始 / 实施中 / 验证中 / 待审(G?) / 通过 / 阻塞(附原因)。

| Phase | 内容 | 状态 | 主要产出（路径） | 验证证据 | 完成日期 |
|---|---|---|---|---|---|
| P0 | 契约卡与 RED 测试（零生产改动） | **通过**（父侧独立验收，生产零改动） | `tests/test_system_health_*.py` 5 文件：decision_events / diagnosis_validator / injection / read_tools / echo_chamber | 66 tests（54 RED + 12 GREEN）；全量 1873 passed（基线 1861 + 12 新 GREEN），54 failed 全为预期 RED，零真实回归；ruff All checks passed；失败类型 ∈ {ImportError, ModuleNotFoundError, AttributeError, AssertionError} | 2026-08-13 |
| P1 | 决策即事件 producer（四分支 + update_plan elif + 收集器前缀 +1 + 脱敏；**allowlist 不动**） | **通过**（父侧独立验收） | contracts.py `DecisionEventV1` :540-645；ingestor.py `coordinator_decision_idempotency_key` :98-124 + `append_decision_event` :836-953；coordinator.py `_append_decision_event` :263-316 + `_summarize_plan_node` :318-332 + 四分支 :148-261 + update_plan elif :381-394；reflection.py 前缀 +1 :433-439 | decision_events 16/16 GREEN；全量 1885 passed（基线 1861 + 12 P0 + 12 P1），42 failed 全为 P2-P4 预期 RED，零真实回归；ruff 零新增（9 错误全为 HEAD 基线债务） | 2026-08-13 |
| P2 | 诊断独立 store + validator + 防回声室 + 置信度门控 | **通过**（父侧独立验收） | `src/a2a/coordinator/memory/diagnosis.py`（新，~710 行：DiagnosisCandidateV1 / DiagnosisValidationResult / validate_diagnosis_response / DiagnosisMemoryStore / DIAGNOSIS_AUDIT_EVENT_TYPE）；contracts.py `DiagnosisConfig` :856-915 | diagnosis_validator 22/22 + echo_chamber 6/6 全 GREEN；全量 1907 passed（基线 1861 + 12 + 12 + 22），20 failed 全为 P3/P4 预期 RED；ruff 新模块 clean | 2026-08-13 |
| P3 | agentic 审查循环（四件只读工具封装 + 多轮循环 + config 流出） | **通过**（父侧独立验收） | `sar_orch/tools/coordinator/query_{projection,temporal_flow,supervision,control_journal}.py`（4 新工具，含 diagnosis.audit 视图过滤）；`sar_orch/diagnosis_loop.py`（DiagnosisLoop：max_rounds=3/diagnosis_sec=90/4 typed 状态/audit 写入）；contracts.py `DiagnosisRuntimeConfig` + `load_diagnosis_config` :1017-1126 | read_tools 14/14 GREEN；全量 1921 passed（基线 1861 + 12+12+22+14），6 failed 全为 P4 预期 RED；ruff 新文件 clean | 2026-08-13 |
| P4 | System Health 注入段（provider + renderer + 预算档 + ACL 复验 + TRUNCATED 泛化 + 诊断循环触发接线） | **通过**（父侧独立验收 2026-08-15；R3/G3 APPROVE 2026-08-16；R3-2 修订 section_budget_threshold 配置面一并验收） | environment_state 相关 + 触发接线（`src/Agent/environment_state.py` `_SECTION_HEADINGS`+`_format_system_health` :127-132/214-241/276-291；`sar_orch/environment_state_provider.py` SECTION_PRIORITY/SECTION_WEIGHTS/_SECTION_BUDGET_THRESHOLD :42-80 + `MemoryReadPort.diagnoses` :213-227 + provider 旋钮 :283-328 + `_system_health_section`/`_apply_budget` TRUNCATED 泛化 :466-540 + R3-2 `_threshold` 配置覆盖 :509-512；`sar_orch/coordinator.py` 诊断 store 组装 :550-600 + 透传 :615-640；`sar_orch/coordinator_state_provider.py` 透传 :45-75/205-228；`sar_orch/long_term_reflection.py` `configure_diagnosis_runtime` :124-147 + `_run_diagnosis_channel` :295-336（并存不替代）；`sar_orch/experiment.py` 接线 :590-610/663-690；`long_term.config` `[diagnosis]` 段（R3-2）；`tests/test_long_term_environment_state.py` 冻结前缀 [:4]→[:5]） | injection 8/8 GREEN（6 RED→GREEN + 2 GREEN 守护）；全量 1927 passed 零回归（基线 1921 + 6 新 GREEN，0 failed）；ruff/format 27+5 全部为 HEAD 基线（新增行零错误）；触发接线 wiring smoke 3/3（wired 通道 ran / unwired 通道 skip / 无模型端口双通道 skip，fail-closed）；**R3-2 修订后全量 1935 passed 零回归（+8 新测试）、ruff 零新增（23=23 HEAD 对比）** | 2026-08-15（R3-2 2026-08-16） |
| P5 | 真实模型 smoke（3/3）+ read 矩阵 10-run 对比 + 文档收口 | **通过**（父侧独立验收 2026-08-16，到 R4 停） | smoke 证据 `sar_orch/results/diagnosis_smoke_20260816_092513.json`；read 矩阵 `sar_orch/results/long_term_memory_read_20260816_172405/`（含 analysis_p5.json 聚合）；文档收口 memory.md / AGENTS.md / issues.md（工作区未提交） | smoke 3/3（3 runs ok、rounds 1-2、avg 19.74s、refs 窗口内零 truth 词）；read 10-run 10/10 rc=0（错误码三码全 0、worker 零 SH 泄漏、6/10 run 注入 20-68%、avg cov 0.741 vs 基线 0.781 / avg tr 0.725 vs 0.783 无退化）；文档 hunk 父侧 diff 逐字复验通过 | 2026-08-16 |

## 3. Gate 决议日志

| Gate | 审查内容 | 状态 | 决议摘要 | 日期 |
|---|---|---|---|---|
| G0 | D1–D10 设计冻结 | **通过**（2026-08-13） | 用户逐项拍板：D1 补 activate_plan_node（DAG 节点激活）+ update_plan（计划修改，独立 tool_start 写入点）；D2–D10 按建议（字段集/阈值 0.6/预算档/独立 store/max_rounds=3/terminal 降级/audit 事件/触发保持）。主方案 SHA-256 `80ec9765…` 冻结。**注意：G0 只冻结设计；用户明确「不要对代码进行改动」，P0 起一切代码改动待另行授权** | 2026-08-13 |
| G1 | 决策事件通道（P1 产物） | **通过**（2026-08-14，父侧验收覆盖） | P1 验收：decision_events 16/16 GREEN；全量 1885 passed 零真实回归；R1 人工判断项由父侧独立验收覆盖（写入路径/幂等/脱敏测试，见 §2 P1 行），不再单审（用户拍板 2026-08-14） | 2026-08-14 |
| G2 | 诊断 store + agentic 循环（P2/P3 产物） | **通过**（2026-08-14） | R2 三判断项全拍板：样本质量（3/3 站得住、refs 可验证、零 truth 词）/ 防回声室充分（机制级三重防护，内容级由门控+矩阵兜底）/ D8 降级接受（完整 drain 实测放 R4）。P2/P3 验收证据见 §2 | 2026-08-14 |
| G3 | 注入与权威性（P4 产物） | **通过**（2026-08-16） | 用户带读逐项拍板：R3-1 观感通过（「通过」）/ R3-2 档位通过+修订条件（「合理，但是阈值要可调整给它留出一个配置空间再config中」）/ R3-3 B6 隔离通过（「成立，通过」）。**修订条件已实施验收**：`[diagnosis] section_budget_threshold` 配置面（默认 3，行为不变），主方案 hash 重算 `ffba3034…`，全量 1935 passed 零回归、ruff 零新增。审查包 [`系统健康诊断_agentic审查者_G3-R3-review-packet.md`](系统健康诊断_agentic审查者_G3-R3-review-packet.md) + 审批记录 [`系统健康诊断_agentic审查者_G3-approval-record.md`](系统健康诊断_agentic审查者_G3-approval-record.md)。**P5 待用户明确指示** | 2026-08-16 |
| G4 | 正式可用（P5 产物） | **通过**（2026-08-16，用户带读逐项拍板，无修订条件） | R4 四判断项全过：①smoke 样本质量「通过」（3/3 finding 站得住、refs 窗口内、零 truth 词）；②注入实证 + 无退化「通过」（10/10 完成、三码全 0、worker 零泄漏、SH 段与 store 一致；avg cov 0.741 / tr 0.725 vs 基线 0.781 / 0.783，delta<0.1）；③披露项「接收」（4/10 零注入 = D8 设计内：3 timeout + 1 末期落库；2 个 4-agent run 波动 = LLM 随机性，诊断精确命中真实失败无误导）；④正式可用宣告「批准」。**System Health 诊断通道正式可用（随 `--long-term-mode != off` 自动接线），feature 关闭**。审查包 [`系统健康诊断_agentic审查者_G4-R4-review-packet.md`](系统健康诊断_agentic审查者_G4-R4-review-packet.md)（SHA-256 `2a1fcc6aa9c63dc8d69ee6434295827202f68a27696ad248233a6dd93fe51c35`，结论区已回写 ✅）+ 审批记录 [`系统健康诊断_agentic审查者_G4-approval-record.md`](系统健康诊断_agentic审查者_G4-approval-record.md) | 2026-08-16 |

## 4. 决策清单（G0 拍板存档）

- D1 决策事件前缀：`coordinator_decision.{assign_task,cancel_task,reply_to_help,activate_plan_node,update_plan}`；else 泛型 send_message 不进 canonical
- D2 决策内容粒度：assign_task 全文（语义内容）；reply_to_help content[:200]（与 logs 同口径）
- D3 诊断字段集：diagnosis_key/kind/target/finding/suggestion/confidence/source_refs/policy_version
- D4 注入阈值：confidence ≥ 0.6，`[diagnosis] min_confidence` 流出 config
- D5 诊断预算档：SECTION_PRIORITY 插 embodied 与 long-term 之间；threshold=3 同 long-term
- D6 诊断 store：独立文件 `<memory_root>/diagnosis/diagnosis.sqlite3`（模板 LongTermMemoryStore）
- D7 agentic 轮次：max_rounds=3 / diagnosis_sec=90，`[diagnosis]` 段流出 config
- D8 terminal 超时：滚动异步不受限；terminal 降级单轮，仍超时则丢弃
- D9 诊断 audit 事件：写 canonical temporal `diagnosis.audit`（独立顶层前缀，不在反思收集器四前缀内，诊断永不进反思窗口；仅供 audit/评测，不进事实域）——注：G0 原案为 `evidence.diagnosis_audit`，2026-08-13 独立审查后撤销（`evidence.*` 前缀会被收集器回收形成回声，见主方案 §3.2）
- D10 触发频率：保持现状（每 5 步 + terminal）

## 5. 变更日志

| 日期 | 变更 | 关联 |
|---|---|---|
| 2026-08-13 | **P3 完成**：实现 subagent 交付四件只读工具（query_projection/temporal_flow/supervision/control_journal，含 diagnosis.audit 视图过滤）+ DiagnosisLoop agentic 循环（max_rounds=3/diagnosis_sec=90/4 typed 状态/audit 写入）+ DiagnosisRuntimeConfig/load_diagnosis_config，父侧独立验收通过（read_tools 14/14 GREEN；全量 1921 passed，6 failed 全为 P4 预期 RED；ruff clean）。P2/P3 完成 → 进入 R2 材料准备 | P3 |
| 2026-08-13 | **P2 完成**：实现 subagent 交付诊断模块（diagnosis.py：DiagnosisCandidateV1 / validate_diagnosis_response 全 rejected 路径 + 防回声室 / DiagnosisMemoryStore / DIAGNOSIS_AUDIT_EVENT_TYPE）+ contracts.py DiagnosisConfig，父侧独立验收通过（diagnosis_validator 22/22 + echo_chamber 6/6 GREEN；全量 1907 passed，20 failed 全为 P3/P4 预期 RED；ruff 新模块 clean）。冻结测试要求 policy_version 为非空 str → 实现取 str(POLICY_VERSION)="1"，已披露 | P2 |
| 2026-08-13 | **P1 完成**：实现 subagent 交付决策事件通道（DecisionEventV1 DTO / coordinator_decision_idempotency_key / append_decision_event / coordinator 四分支 + update_plan elif / 收集器前缀 +1 / 脱敏；allowlist 不动），父侧独立验收通过（decision_events 16/16 GREEN；全量 1885 passed，42 failed 全为 P2-P4 预期 RED；ruff 零新增）。用户授权「启动 P1，且后续 P2/P3 连续做，到 R2 前才停」 | P1 |
| 2026-08-13 | **P0 完成**：实现 subagent 交付 `tests/test_system_health_*.py` 5 文件（66 tests = 54 RED + 12 GREEN），父侧独立验收通过（生产零改动；全量 1873 passed，54 failed 全为预期 RED 且类型合规；ruff clean）。用户授权「可以根据文档开始实施了」；P1 待确认启动。期间补 skill：ROS pytest 插件坑（`-p no:launch_testing -p no:launch_ros`） | P0 |
| 2026-08-13 | G4 边界讨论定方向：agentic 系统健康诊断通道九条锁定（见长期记忆进度 §1）；C 维持 V1 | G4 边界讨论 |
| 2026-08-13 | 代码事实探索 01/02/03（3 subagent 并行只读，父侧交叉核验：subagent 02 一处自报修正——_rolling_worker 实际存在于 long_term_reflection.py:183） | 探索 |
| 2026-08-13 | 主方案 + 人工审查点补充冻结；G0 通过（D1–D10 全拍板）；主方案 SHA-256 `80ec9765…` | G0 |
| 2026-08-13 | **独立审查 3 维度（一致性/证据抽检/实施预演）**：1 Blocker（severity 字段矛盾）+ 7 Major + 8 Minor；证据抽检 44 条 ✅41/⚠️2/❌1（❌ 即探索 01 _rolling_worker 误报，已加 superseded 注记）；实施预演给出每 Phase 遗漏改动点与测试破坏面。父侧复核后方案修订：allowlist 7→8 撤销（append_temporal_event 无 provenance 参数，provenance gate 只作用投影输入）；D9 改 `diagnosis.audit` 独立前缀（防回声）；update_plan 改声明式摘要（diff 在 tool_start 拿不到）；诊断与滚动反思并存（不替代）；修订决策项 A1–A6 全部拍板（A1 追认 G4 边界扩展；A2 独立旋钮 `[diagnosis] inject_enabled` 默认 true 供消融实验）。主方案 hash `1cff5295…` | 独立审查 |
| 2026-08-14 | **状态对齐 + R1 处置拍板**：P0–P3 已提交 `bfd1b47`（§1 绑定 HEAD 更新，原「P1 已改 4 生产文件未提交」过时）；R1（决策事件通道）视为已被父侧验收覆盖、不再单审（用户拍板），G1 随 P1 验收通过、G2 状态改为待 R2 人工审查；头部「材料准备中」改为「材料已备」并补提交事实。设计文件与审批记录冻结值不动 | 状态对齐 |
| 2026-08-14 | **讨论记录（R2 审查中，未授权实施）**：DiagnosisLoop 增强方向——单轮并行多工具调用（现 port 解析只匹配 tools[0]）+ max_rounds/timeout 保留配置空间。已记 §1，待 R2 结论后另行排期 | 讨论记录 |
| 2026-08-14 | **R2 通过 + P4 授权**：R2 三判断项全拍板（样本质量通过 / 防回声室充分 / D8 接受，完整 drain 实测放 R4）；G2 通过；用户授权「P4（System Health 注入段）实施，完成后到 R3 审查点停」——带终点连续授权，P4 内部闭环验收 | R2/G2 |
| 2026-08-14 | **P4 暂不实施（用户指示，文档收尾）**：用户「不要开始实施，确保文档收尾完成即可」——P4 状态回未开始（上行授权记录保留为历史，实施从未启动、零代码改动），实施待另行授权；R2 通过、G2 通过不受影响 | 收尾 |
| 2026-08-15 | **P4 完成（父侧独立验收通过，到 R3 审查点停）**：用户「根据进度可以开始接下来的实施了」= P4 授权（带终点连续授权：完成后到 R3 停）。实现 subagent 交付 6 生产文件 + 1 测试文件（见 §2 P4 行）。父侧独立验收：injection 8/8 GREEN（6 RED→GREEN + 2 GREEN 守护）；全量 1927 passed / 0 failed / 4 skipped（独立复跑，188.41s）；ruff 27 错误 + format 5 文件全部经 worktree HEAD 对比实证为基线债务，新增行零错误；触发接线 wiring smoke 3/3 fail-closed。**披露**：①`tests/test_long_term_environment_state.py` 冻结前缀 [:4]→[:5] 是 P0 契约卡预告的预期冲突（P4 插档），非回归；②ROS 插件坑复现——后台 shell 注入 `/opt/ros/humble` 至 PYTHONPATH 导致 `launch_testing` 缺 `lark`，改用干净 `PYTHONPATH="src"` 跑全量；③terminal drain 无独立诊断运行，只 surface in-flight 结果（D8 语义：诊断非必需、绝不阻塞退出）。G3 状态 → 待 R3（材料已备） | P4 |
| 2026-08-16 | **R3/G3 通过 + R3-2 修订实施验收完成**：用户带读逐项拍板（①「通过」②「合理，但是阈值要可调整给它留出一个配置空间再config中」③「成立，通过」）；审查包落盘（SHA-256 `d7f82a53…`）+ 审批记录 `系统健康诊断_agentic审查者_G3-approval-record.md`。R3-2 修订（`[diagnosis] section_budget_threshold` 默认 3）：主方案 §3.3/§5/D5 修订 + hash 重算 `ffba3034…`（进度/审查包/审查点补充三处绑定回写）；实现 subagent 交付（contracts.py 校验链 + provider `_threshold` + 接线链透传 + long_term.config [diagnosis] 段 + 8 新测试），experiment.py 零改动（dataclass 自动携带）；父侧独立验收：focused 98 passed、全量 1935 passed 4 skipped 0 failed、ruff 23=23 零新增、P0 守护 GREEN；规格偏差裁决（测试 (a) TRUNCATED 预期与 P5 长期段恒注入冲突 → 接受适配）。**P5 待用户明确指示** | R3/G3、R3-2 |
| 2026-08-16 | **独立交叉审查 + 修复（用户要求全面审查后定 P5）**：review subagent 审查提交 `0f30127`（独立读码 + 复跑 + 3 复现脚本）→ **0 Blocker / 4 Major / 8 Minor**——M-1 同 target 诊断坍缩（保留最高置信度，3 测试）、M-2 诊断 store 失效拖垮视图（diagnoses() try/except 返回 []，1 测试）、M-3 诊断构造异常抹反思结果（import/构造移入内部 try + 外层防御，2 测试）、M-4 drain 时序（反思完成即记录 + 诊断独立附加字段，1 测试含 drain 超时路径）+ Minor 6/8 修复（死参数/重复守卫/provider 校验/min_confidence bool 排除/默认值派生/主方案行号）；2 Minor 记录披露（loader 下划线宽容、diagnosis.audit 观测噪音）。父侧独立复验：focused 8 文件 157 passed、全量 1945 passed 4 skipped 0 failed（1935+10）、ruff 23=23 零新增；主方案 hash 重算 `7c3a4a5e…` 三处回写；审批记录 §4.5 已补。**P5 待用户明确指示** | 独立审查、修复 |
| 2026-08-16 | **P5 完成（用户指示「开始P5，你作为协调者与进度记录者，具体任务交给subagent」；父侧独立验收通过，到 R4 停）**：①smoke subagent 跑 `diagnosis_smoke.py` → 3/3 gate 通过，证据 `diagnosis_smoke_20260816_092513.json`（3 runs ok、rounds 1-2、avg 19.74s、finding 与 fixture 语义对齐、refs 窗口内零 truth 词）；②read 10-run 矩阵 subagent 跑 `run_g3_read_matrix.sh` → `long_term_memory_read_20260816_172405/` 10/10 rc=0（框架错误码三码全 0、worker 零 SH 泄漏、6/10 run 实际注入 20-68%、avg cov 0.741 vs G4 基线 0.781 / avg tr 0.725 vs 0.783 同口径无退化；聚合 `analysis_p5.json`）；③文档收口 subagent 交付 memory.md/AGENTS.md/issues.md hunk，父侧 diff 逐字复验通过（24 行新增、数值与证据一致、既有内容零改动）。父侧独立核验（不采信自报）：smoke JSON 全文重读、矩阵 summary.csv 独立聚合、SH 段真实内容抽查（scene_2_agents_2 与 store 一致）、worker 泄漏全量 grep、错误码权威 run_metrics.json 复验。**披露**：a) 4/10 run 零注入为 D8 设计内——3 run 诊断循环 90s 超时丢写（run_metrics `diagnosis.timeout/diagnosis_sec_exceeded/written=0`）+ 1 run（scene_3_agents_4）诊断在最后 llm_request 后 14s 才落库无后续请求；b) per-run 2 个 4-agent run（scene_4/5_agents_4）较基线大幅波动（cov/tr -0.5/-0.6），其诊断 suggestion 方向合理无误导迹象，判定 LLM 随机性 + max_steps 截断，avg 同量级判定成立。G3 行使旧注「P5 待用户明确指示」已由 §2/§3 更新覆盖。**G4 → 待 R4 人工审查（材料已备）** | P5 |
| 2026-08-16 | **R4 审查带读瑕疵修复（文档对齐，无设计语义变更）**：用户带读 G4 审查包，父侧核验发现 ①补充文档绑定值 `2d571202…` 为 G3 审批时刻工作区中间值，fd7594f 提交版为 `aef1f3d6…`——G4 审查包头部/§6 + 本文件 §1 三处回写修正；②审查包 §2.3 memory.md 行「24 行新增 0 删除」修正为 numstat 实测 +13/-1（24 为 AGENTS+issues+memory 三文档合计新增，memory.md 含 1 行删除）；③审查包 §2.2 补基线口径注记（基线目录含 steps=0 死 run `scene_4_agents_2` + `_rerun` 完成版，均值剔除死 run，父侧复算 0.7815/0.7832 一致）。G4 审查包 hash 重算并回写 §3 G4 行 | 收口 |
| 2026-08-16 | **R4/G4 通过 + 审批闭环**：用户带读逐项拍板（R4-1「通过」/ R4-2「通过」/ R4-3「接收」/ R4-4「批准」，无修订条件）；审批记录 `系统健康诊断_agentic审查者_G4-approval-record.md` 落盘（绑定 HEAD `42ac461` + 审查包 `2a1fcc6a…` / 主方案 `7c3a4a5e…` / 补充 `aef1f3d6…` / 本文件最终 hash）；审查包结论区 ⏳→✅ 回写（hash 重算回写 §3 G4 行）；头部/§1 状态对齐为「feature 关闭」。**System Health 诊断通道正式可用（随 `--long-term-mode != off` 自动接线）；G4 审查链文档工作区未提交，提交时机用户另行指示** | R4/G4 |
| 2026-08-16 | **G4 后边界讨论拍板（①②③，全部未授权实施）**：用户「1. C 2. B（先补观测）再动手 3. 消融实验放缓」——①单轮并行多工具 = 先不动等数据（共同论证弱点：真实 run 单轮 latency 未量化）；②diagnosis_sec = 先补 run_metrics 观测（timeout 触发类型：滚动 or terminal drain），**补观测属代码改动，实施待另行授权**；③消融实验放缓（设计已记录于 §1，节奏后续另定）。消融设计要点：对照组 inject_enabled=false 10 run 同 P5 口径，测「注入的因果效应」（该旋钮只关注入不关循环），指标含诊断-决策遵循度 | 讨论拍板 |
