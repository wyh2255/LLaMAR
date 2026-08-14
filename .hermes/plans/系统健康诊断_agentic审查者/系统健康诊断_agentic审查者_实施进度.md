# 系统健康诊断 · agentic 审查者 — 实施进度记录

> **用途：** 跟踪以下设计文件的 Phase/Gate 状态与验收证据：
> - [`系统健康诊断_agentic审查者_实施方案.md`](系统健康诊断_agentic审查者_实施方案.md)
> - [`系统健康诊断_agentic审查者_实施方案补充-人工必要审查点.md`](系统健康诊断_agentic审查者_实施方案补充-人工必要审查点.md)
> - 代码事实探索 01/02/03（同目录，只读，2026-08-13）
>
> **边界：** 本文件只记录状态、Gate 与可复核证据；设计语义只在主方案及补充中变更。不得用本文件替代 approval record 或真实验收产物。可复用经验另入 `经验.md`/skill，不在此累积。
>
> **当前状态：** G0 已通过（2026-08-13，D1–D10 全拍板）；独立审查 3 维度完成并修订主方案；**修订决策项 A1–A6 全部拍板**（A1 追认 G4 边界；A2 独立旋钮 `[diagnosis] inject_enabled` 供消融实验；A3–A6 按建议）；**B6/B9 结构性隔离修订 + R0–R4 审查点已插入主方案 §9.1**。主方案 SHA-256 `9dc21222bb9611584580a1d0801d0b670dae8362dccd904cfe25e022f89edd74`。**用户授权「启动 P1，且后续 P2/P3 连续做，到 R2 前才停」（2026-08-13）——P0/P1/P2/P3 全部完成并通过父侧独立验收**；R2（诊断质量人工审查）材料准备中（真实模型诊断 smoke 3 次 + 轮次分布实测 + terminal drain 时序），完成后停下等用户审查。
>
> **更新约定：**
> - 进度三态按“实施中 / 验证中 / 可提交”汇报，并附真实产物路径或命令输出；
> - 没有独立测试和真实 smoke 证据，任何 Phase 不得标记为“通过”；
> - Gate 必须记录日期、绑定 HEAD/hash、审查材料、用户决议、授权范围与遗留风险；
> - Gate 打回时区分“契约修订（回设计）”与“实现缺陷（直接修复）”。

---

## 1. 当前位置

- 阶段：**P0/P1 完成（2026-08-13）**——RED 契约卡 + 决策事件通道均已交付并通过父侧独立验收（见 §2）。实施中（P2）。
- 绑定 HEAD：`32bfe57`（`feat/memory-redesign`，探索基线；P0 未改生产代码；P1 已改 4 生产文件未提交）
- 设计证据冻结：
  - 主方案 SHA-256（R0–R4 插入 + B6/B9 修订后）：`9dc21222bb9611584580a1d0801d0b670dae8362dccd904cfe25e022f89edd74`
  - 人工审查点补充 SHA-256：`2d57120252c7e9bcd15c7b27940b9b0b87ba366b3c45f6abd5344202b61342ec`（R0 决议同步 A1–A6）
  - 代码事实探索 SHA-256：01 `67ee73eeb7650a1c6d1ff543dfbac7e55f61eb9910d2ec7ca7b2fc11bb984f2e`（含 superseded 注记）、02 `6bdb39d3bdec17dbd9b4830144ef24ff8a99f0d16469027409443dd5a40f7028`、03 `afae83a2b44afad821610014d27189a59a25457f992a819c15a9bc68505cf08d`
- 用户拍板记录（2026-08-13）：
  - G4 边界讨论：C（AgentCard mutation）维持 V1；A（O-A）升级为 agentic 诊断通道，九条方向全锁定（记录于长期记忆进度文档 §1）
  - G0：D1 补 activate_plan_node + update_plan；D2–D10 按建议
- 下一个动作：**R2 人工审查点材料已备，停下等用户审查**。材料：①真实模型诊断 smoke 3/3（`sar_orch/results/diagnosis_smoke_20260813_084820.json`，样本见下）；②防回声室测试（P2 已交付：echo_chamber_source_ref rejected + collector 拒绝 diagnosis.*）；③轮次分布实测（3/3 全 1 round，模型一轮即产出 record_diagnosis，latency 10-18s/轮，远低于 diagnosis_sec=90）；④terminal drain 时序（真实单轮 10-18s，3 轮最坏 ~54s 逼近 drain 60s 边界——完整接线后实测留 R3/R4 材料）。诊断样本速览：run1（variant_a）target=coordinator conf=0.75「dispatch 未 journal/无 ack、supervision id 不匹配」→ sugg journal 一致化+对齐 id+解决 stale；run2（variant_b）target=system conf=0.72「control journal 空、dispatch 未跟踪」→ sugg 落 journal+查 worker 可达性；run3（variant_c）target=coordinator conf=0.72「计划 objective 与证据矛盾（bob 需 sand 非 water、chemical non-chemical）」→ sugg 按证据修订 plan。三条全部 refs 指向窗口内真实事件、零 truth 词、finding 站得住无幻觉。**R2 通过 → P4（注入段）；打回 → 修复**。R2 之后 R3（P4 产物）→ R4（P5 产物）。
- 已结束历史：长期记忆 G0–G4 是本 feature 的前置（`long_term_mode=read` 正式可用）；其进度见 `长期记忆_反思机制+动态Agentcard接入_实施进度.md`。

## 2. Phase 状态表

状态枚举：未开始 / 实施中 / 验证中 / 待审(G?) / 通过 / 阻塞(附原因)。

| Phase | 内容 | 状态 | 主要产出（路径） | 验证证据 | 完成日期 |
|---|---|---|---|---|---|
| P0 | 契约卡与 RED 测试（零生产改动） | **通过**（父侧独立验收，生产零改动） | `tests/test_system_health_*.py` 5 文件：decision_events / diagnosis_validator / injection / read_tools / echo_chamber | 66 tests（54 RED + 12 GREEN）；全量 1873 passed（基线 1861 + 12 新 GREEN），54 failed 全为预期 RED，零真实回归；ruff All checks passed；失败类型 ∈ {ImportError, ModuleNotFoundError, AttributeError, AssertionError} | 2026-08-13 |
| P1 | 决策即事件 producer（四分支 + update_plan elif + 收集器前缀 +1 + 脱敏；**allowlist 不动**） | **通过**（父侧独立验收） | contracts.py `DecisionEventV1` :540-645；ingestor.py `coordinator_decision_idempotency_key` :98-124 + `append_decision_event` :836-953；coordinator.py `_append_decision_event` :263-316 + `_summarize_plan_node` :318-332 + 四分支 :148-261 + update_plan elif :381-394；reflection.py 前缀 +1 :433-439 | decision_events 16/16 GREEN；全量 1885 passed（基线 1861 + 12 P0 + 12 P1），42 failed 全为 P2-P4 预期 RED，零真实回归；ruff 零新增（9 错误全为 HEAD 基线债务） | 2026-08-13 |
| P2 | 诊断独立 store + validator + 防回声室 + 置信度门控 | **通过**（父侧独立验收） | `src/a2a/coordinator/memory/diagnosis.py`（新，~710 行：DiagnosisCandidateV1 / DiagnosisValidationResult / validate_diagnosis_response / DiagnosisMemoryStore / DIAGNOSIS_AUDIT_EVENT_TYPE）；contracts.py `DiagnosisConfig` :856-915 | diagnosis_validator 22/22 + echo_chamber 6/6 全 GREEN；全量 1907 passed（基线 1861 + 12 + 12 + 22），20 failed 全为 P3/P4 预期 RED；ruff 新模块 clean | 2026-08-13 |
| P3 | agentic 审查循环（四件只读工具封装 + 多轮循环 + config 流出） | **通过**（父侧独立验收） | `sar_orch/tools/coordinator/query_{projection,temporal_flow,supervision,control_journal}.py`（4 新工具，含 diagnosis.audit 视图过滤）；`sar_orch/diagnosis_loop.py`（DiagnosisLoop：max_rounds=3/diagnosis_sec=90/4 typed 状态/audit 写入）；contracts.py `DiagnosisRuntimeConfig` + `load_diagnosis_config` :1017-1126 | read_tools 14/14 GREEN；全量 1921 passed（基线 1861 + 12+12+22+14），6 failed 全为 P4 预期 RED；ruff 新文件 clean | 2026-08-13 |
| P4 | System Health 注入段（provider + renderer + 预算档 + ACL 复验 + TRUNCATED 泛化） | 未开始 | environment_state 相关 | 无 | — |
| P5 | 真实模型 smoke（3/3）+ read 矩阵 10-run 对比 + 文档收口 | 未开始 | 矩阵目录 + 文档 | 无 | — |

## 3. Gate 决议日志

| Gate | 审查内容 | 状态 | 决议摘要 | 日期 |
|---|---|---|---|---|
| G0 | D1–D10 设计冻结 | **通过**（2026-08-13） | 用户逐项拍板：D1 补 activate_plan_node（DAG 节点激活）+ update_plan（计划修改，独立 tool_start 写入点）；D2–D10 按建议（字段集/阈值 0.6/预算档/独立 store/max_rounds=3/terminal 降级/audit 事件/触发保持）。主方案 SHA-256 `80ec9765…` 冻结。**注意：G0 只冻结设计；用户明确「不要对代码进行改动」，P0 起一切代码改动待另行授权** | 2026-08-13 |
| G1 | 决策事件通道（P1 产物） | 待 P1 | — | — |
| G2 | 诊断 store + agentic 循环（P2/P3 产物） | 待 P2/P3 | — | — |
| G3 | 注入与权威性（P4 产物） | 待 P4 | — | — |
| G4 | 正式可用（P5 产物） | 待 P5 | — | — |

## 4. 决策清单（G0 拍板存档）

- D1 决策事件前缀：`coordinator_decision.{assign_task,cancel_task,reply_to_help,activate_plan_node,update_plan}`；else 泛型 send_message 不进 canonical
- D2 决策内容粒度：assign_task 全文（语义内容）；reply_to_help content[:200]（与 logs 同口径）
- D3 诊断字段集：diagnosis_key/kind/target/finding/suggestion/confidence/source_refs/policy_version
- D4 注入阈值：confidence ≥ 0.6，`[diagnosis] min_confidence` 流出 config
- D5 诊断预算档：SECTION_PRIORITY 插 embodied 与 long-term 之间；threshold=3 同 long-term
- D6 诊断 store：独立文件 `<memory_root>/diagnosis/diagnosis.sqlite3`（模板 LongTermMemoryStore）
- D7 agentic 轮次：max_rounds=3 / diagnosis_sec=90，`[diagnosis]` 段流出 config
- D8 terminal 超时：滚动异步不受限；terminal 降级单轮，仍超时则丢弃
- D9 诊断 audit 事件：写 canonical temporal `evidence.diagnosis_audit`（provenance=reflection，仅供 audit/评测，不进事实域）
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
