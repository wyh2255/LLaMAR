# G3/R3 审查包 — System Health 注入段放行（P4 产物，诊断进 coordinator Context）

> **性质：** 本文件是人工必要审查点 R3（G3）的审查材料，由父 agent 基于 P4 真实实现、父侧独立验收与真实渲染证据生成，提交用户审批。批准后放行 System Health 注入段（`### System Health` 作为 coordinator-only 平级新段进入 Context，参与下一轮决策；worker 永不可见），并进入 P5（真实模型诊断 smoke 3/3 + read 10-run 矩阵对比 + 文档收口）。
> **绑定基线：** HEAD `bfd1b47`（P0–P3 已提交；P4 产物在工作区未提交，待 R3 通过后提交）；主方案 SHA-256 `ffba3034ac0ddcd896911288ab23ae2ab7e56d7dab440cbefa186f6d01a4c3a0`（**2026-08-16 R3 修订：§3.3/§5/D5 增 `section_budget_threshold` 配置面**，旧 hash `9dc21222…`）；人工审查点补充 SHA-256 `2d57120252c7e9bcd15c7b27940b9b0b87ba366b3c45f6abd5344202b61342ec`；P4 完成证据见 [`系统健康诊断_agentic审查者_实施进度.md`](系统健康诊断_agentic审查者_实施进度.md) §1/§2/§5。
> **结论：** ✅ **APPROVE（2026-08-16）**——三判断项全通过（R3-1「通过」/ R3-2「通过 + 修订条件：section_budget_threshold 配置化」/ R3-3「成立，通过」）；R3-2 修订已实施并父侧验收（全量 1935 passed 零回归、ruff 零新增）；审批记录见 [`系统健康诊断_agentic审查者_G3-approval-record.md`](系统健康诊断_agentic审查者_G3-approval-record.md)。P5 待用户另行指示。

---

## 1. 审批范围（精确到边界）

**批准后放行（G3 范围）：**
- P4 产物正式验收：`### System Health` 段进入 coordinator Context 的注入链路（provider 门控 + renderer 渲染 + 预算档 + ACL + TRUNCATED 泛化 + 诊断循环触发接线）；
- 进入 P5 实施（真实模型诊断 smoke 3/3 + read 10-run 矩阵注入实证与 cov/tr 对比 + memory.md/AGENTS.md/待办文档收口）；
- 后续 R4/G4 按人工必要审查点顺序放行（read 矩阵、smoke、文档收口；诊断注入前后性能显著退化则打回——本 feature 独有验收维度）。

**不授权（G3 放行边界）：**
- commit/push（提交时机由用户另行指示；P4 产物与审查链文档随 R3 通过后一并提交）；
- canonical schema migration、Context cutover 之外的任何结构变更；
- 诊断循环 agentic 框架增强（单轮并行多工具调用——已记录讨论方向，未授权实施，代码零改动）；
- 任何对 H1 reducer、真相隔离、认证边界的改动；诊断进投影事实域（B6 结构性隔离冻结，见 §2.6）。

## 2. 证据汇总（全部真实产物，父侧独立核验）

### 2.1 P4 实现与测试（2026-08-15 完成，父侧独立复跑核验）

| 项 | 证据 |
|---|---|
| P4 实现面 | 6 生产文件 + 1 测试文件：`src/Agent/environment_state.py`（`_SECTION_HEADINGS` + `_format_system_health` :123-138/214-238/276-291）；`sar_orch/environment_state_provider.py`（SECTION_PRIORITY/SECTION_WEIGHTS/_SECTION_BUDGET_THRESHOLD :42-80 + `MemoryReadPort.diagnoses` :213-227 + provider 旋钮 :283-321 + `_system_health_section`/`_apply_budget` TRUNCATED 泛化 :416-523）；`sar_orch/coordinator.py`（诊断 store 组装 :550-600 + 透传 :615-630）；`sar_orch/coordinator_state_provider.py`（透传 :45-70/205-225）；`sar_orch/long_term_reflection.py`（`configure_diagnosis_runtime` :124-147 + `_run_diagnosis_channel` :295-336，并存不替代）；`sar_orch/experiment.py`（接线 :590-610/663-690）；`tests/test_long_term_environment_state.py`（冻结前缀 [:4]→[:5] 预期冲突，见 §3） |
| injection 契约测试 | `tests/test_system_health_injection.py` **8/8 GREEN**（6 RED→GREEN：heading 注册 / 优先级插档 / threshold 同档 / 一行一诊断渲染 / 注入旋钮默认 true / `diagnoses()` 读侧；2 GREEN 守护：既有优先级相对序不破坏 + worker 视图零诊断段） |
| 全量回归 | `pytest tests/ -q` → **1927 passed, 0 failed, 4 skipped**（188.41s，父侧独立复跑；基线 1921 + 6 新 GREEN） |
| ruff / format | 27 错误 + 5 format 文件全部经 worktree HEAD 对比实证为基线债务，**新增行零错误** |
| 触发接线 wiring smoke | 3/3：wired 通道 ran / unwired 通道 skip / 无模型端口双通道 skip（fail-closed，`long_term_reflection.py` `_run_diagnosis_channel`） |
| 现场复跑（2026-08-16） | `pytest tests/test_system_health_injection.py tests/test_system_health_echo_chamber.py tests/test_long_term_read_port.py -q` → **28 passed in 8.63s** |

### 2.2 Coordinator Context before/after 样本（R3 人工判断核心 ①）

**Before（P4 前形状，无诊断段）→ After（含诊断段）唯一差异** —— 同一渲染函数 `render_environment_state_view` 离线实测输出（fixture 生成，脚本可复现）：

```
---                              ---
## Environment State             ## Environment State
---                              ---
### Spatial State                ### Spatial State
- fire-3 (fire)                  - fire-3 (fire)
  - intensity: high                - intensity: high
---                              ---
                                 ### System Health
                                 coordinator: dispatch d-2 has no ack in control journal → verify Bob reachability before assigning follow-ups (confidence=0.72)
                                 worker:bob: stale embodied telemetry at reservoir → refresh position/inventory before next task (confidence=0.64)
                                 ---
### Long-term Memory             ### Long-term Memory
- lt-1 [strategy] ...            - lt-1 [strategy] ...
---                              ---
### Relevant Recent Events       ### Relevant Recent Events
...                              ...
---                              ---
### Task Execution State         ### Task Execution State
...                              ...
---                              ---
### Freshness / Conflicts        ### Freshness / Conflicts
...
```

- 一行一诊断：`target: finding → suggestion (confidence=N)`，按 target 排序稳定渲染；无 severity、无 thinking、无 truth 词（R2 smoke 已验样本质量）；
- **诊断不存在时键不注入**（`environment_state_provider.py:425-432`：`_is_system and long_term_mode=="read" and inject_enabled` 三条件 + 非空才注入）——coordinator 视图无诊断时与 P4 前完全同形；
- 预算权重仅 **5%**（`SECTION_WEIGHTS` :56-63，与长期记忆同档）——低预算最先整体裁剪，不留半截段；
- 置信度门槛 `min_confidence=0.6`（D4，`_system_health_section` :466-484）过滤后才渲染，低于阈值不注入。

### 2.3 Worker ACL 零泄漏证据

| 路径 | 证据 |
|---|---|
| worker 视图零诊断段（GREEN 守护） | `test_system_health_injection.py::test_worker_view_never_contains_system_health`（断言 `system_health` 不在 sections 也不在 evidence）✅ GREEN |
| ACL 三道门 | 注入门控在 provider（`_build_view` :419-432，ACL 决策永远不进 renderer）；worker 天然不满足 `_is_system`；renderer 纯格式化零过滤（design §6） |
| worker HTTP 端点 | 既有 `viewer_role="worker"` 构造路径零改动（P4 未触碰 server.py 端点） |

### 2.4 哪些进入 Context、哪些永不进入（R3 材料 ①/③）

**进入 coordinator Context（read 模式 + inject_enabled + 诊断存在）：**
- `system_health` 段：仅 `confidence >= min_confidence(0.6)` 的诊断，`{target: {finding, suggestion, confidence}}`，一行一诊断；
- 诊断来源：独立 `DiagnosisMemoryStore`（`<memory_root>/diagnosis/diagnosis.sqlite3`，短命生命周期，run 结束即归档，不跨 run、不参与 supersede 链）。

**永不进入 Context：**
- 置信度低于阈值的诊断（provider 过滤）、rejected 诊断行（store 查询只读）；
- `diagnosis.audit` 事件（canonical temporal，仅供 audit/评测，不进反思窗口、不进投影 reducer、不进收集器前缀）；
- worker 视图：`system_health` 段永不可见（§2.3）；
- 决策事件原文之外的 LLM thinking / 完整输出（B1，决策原文 = dispatch 语义内容）。

### 2.5 预算 / TRUNCATED 行为（R3 材料 ①，判断项 2 支撑）

- 冻结丢弃序（`SECTION_PRIORITY` :43-55）：`Task > Spatial > Embodied > System Health > Long-term > Temporal`——诊断高于长期记忆；
- `threshold=3` 与 long_term 同档（`_SECTION_BUDGET_THRESHOLD` :73-80）：budget=1/2 时整体裁剪；
- **TRUNCATED 泛化**（`_apply_budget` :501-506）：`fixed_cap_dropped` 由 long_term 单段泛化为 `(long_term_memory, system_health)` 多段——任何固定上限摘要段被裁都显式标记 `TRUNCATED`，**永不会静默消失**；
- 守护测试：`test_long_term_read_port.py::test_budget_below_long_term_threshold_drops_section_and_truncates` / `test_budget_at_long_term_threshold_keeps_section`（泛化后仍绿，现场复跑通过）+ `test_system_health_injection.py::test_existing_priority_order_preserved_after_system_health_insert`（相对索引序守护，P4 插档不破坏既有序、long_term 档位不漂移）。

### 2.6 结构性隔离（B6）代码表达（R3 人工判断核心 ③）

| 隔离面 | 代码证据 |
|---|---|
| 诊断只落独立 store，不进投影事实域 | `DiagnosisMemoryStore`（独立 sqlite）→ `MemoryReadPort.diagnoses()`（`environment_state_provider.py:215-227`，store 未接线返回 `[]` 永不 raise）→ 只注入 `sections["system_health"]`（:430-432）；**全链路无任何写 projection_fields / spatial / embodied reducer 的路径** |
| `diagnosis.audit` 前缀永不进反思窗口 | `test_system_health_echo_chamber.py::test_reflection_prefixes_keep_four_families_and_never_diagnosis`（四前缀在场 + `diagnosis.` 不在 `_REFLECTION_EVENT_PREFIXES`）+ `test_collector_never_accepts_diagnosis_audit_event_type`（collector 对 `diagnosis.audit` / 裸 `diagnosis` 恒 False，reflection.py:473-477 按前缀过滤）✅ GREEN |
| allowlist / FIELD_SOURCE_POLICY 不扩展 | `test_allowlist_never_extended_for_decision_or_diagnosis`（B9：决策事件与 diagnosis.audit 是协调层自身行为记录，provenance gate 只作用于 ingest_projection 投影输入，append_temporal_event 无 provenance 参数）✅ GREEN |
| 诊断与 worker evidence 无冲突通道 | 不进投影 → 无 conflict_candidates、不参与 FIELD_SOURCE_POLICY；「建议安全性」由置信度门控 + R4 read 矩阵兜底 |

### 2.7 R2 前置证据（诊断质量，R3 判断项 ① 的前提，已 APPROVE）

- 真实模型诊断 smoke 3/3（`sar_orch/results/diagnosis_smoke_20260813_084820.json`）：run1（variant_a）target=coordinator conf=0.75「dispatch 未 journal/无 ack、supervision id 不匹配」；run2（variant_b）target=system conf=0.72「control journal 空、dispatch 未跟踪」；run3（variant_c）target=coordinator conf=0.72「计划 objective 与证据矛盾」。三条全部 refs 指向窗口内真实事件、零 truth 词、finding 站得住无幻觉；
- 轮次分布实测：3/3 全 1 round（模型一轮即产出 record_diagnosis，latency 10-18s/轮，远低于 diagnosis_sec=90）；
- 防回声室：validator 拒绝诊断引诊断（`test_system_health_diagnosis_validator.py::test_echo_chamber_source_ref_rejected`）+ 前缀隔离（§2.6）。

## 3. 披露（P4 期间父侧裁决与记录）

| # | 披露项 | 处理 |
|---|---|---|
| 1 | `tests/test_long_term_environment_state.py` 冻结前缀 `[:4]`→`[:5]` | **P0 契约卡预告的预期冲突**（P4 插档后前 5 个 section 必含 system_health），非回归；按 P4 契约意图更新，断言处已注释披露 |
| 2 | ROS 插件坑复现 | 后台 login shell 注入 `/opt/ros/humble` 至 PYTHONPATH → `launch_testing` 缺 `lark` 收集失败；改用干净 `PYTHONPATH="src"` 跑全量（环境性，非代码问题） |
| 3 | terminal drain 无独立诊断运行 | D8 语义：诊断非必需、绝不阻塞退出——drain 只 surface in-flight 结果；完整接线 drain 时序实测放 R4 强制材料（R2 已接受该降级策略） |

## 4. 风险与残留

| 风险 | 缓解 | 状态 |
|---|---|---|
| 诊断误导 coordinator 决策（建议安全性） | R2 样本质量 3/3 + 置信度门控 0.6 + 预算 5% 低占比 + R4 read 矩阵 cov/tr 前后对比（显著退化则打回） | 门控已实现；矩阵待 P5 |
| 诊断段喧宾夺主 / 被忽略 | 一行一诊断低密度、无 severity、target 明确（coordinator/worker/系统）、注入键非空才出现 | 本 Gate 人工判断 ① |
| 诊断优先级争议（高于长期记忆） | 丢弃序钉死 + 索引序守护测试；若需反转为契约修订（回主方案重算 hash） | 本 Gate 人工判断 ② |
| 回声室（诊断影响决策→决策进反思→新诊断循环） | 三重防护：source_ref 禁引诊断 + `diagnosis.audit` 独立前缀（收集器永不收）+ 时间线工具视图过滤 | 已实现 + 测试（§2.6） |
| token 开销 | 固定上限摘要 + 预算裁剪留 TRUNCATED + 5% 权重 | 已实现 + 测试 |
| 多轮超时 vs terminal drain 冲突 | D8 降级（terminal 单轮、仍超时丢弃）+ 完整实测放 R4 | R2 已接受 |
| DiagnosisLoop 单轮仅 tools[0] 被解析（并行多工具调用增强方向） | 已记录讨论（§1 不授权清单），待 R2 后另行排期；现 max_rounds=3 兜底 | 代码零改动 |

## 5. 需用户拍板的决策项（R3 三判断项）

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| R3-1 | **诊断段观感：会不会喧宾夺主** | 注入门控三条件 + 非空才注入 + 5% 预算 + 0.6 阈值（§2.2 样本） | 批准：低密度一行一诊断、建议性语气、target 定向，风险由阈值与矩阵兜底 | 是否通过 |
| R3-2 | **SECTION_PRIORITY 档位：诊断高于长期记忆是否合理** | Task > Spatial > Embodied > **System Health** > Long-term > Temporal，threshold=3 同档（§2.5） | 批准：诊断时效敏感（改变即时决策）、长期记忆为慢变量先裁不亏；若认为应反转为契约修订 | 是否通过 |
| R3-3 | **结构性隔离（B6）代码表达是否成立** | 诊断只经独立 store → `system_health` 段，零投影写入路径；`diagnosis.audit` 前缀隔离 + allowlist 不动 + worker 零诊断段守护（§2.6） | 批准：隔离机制级三重防护 + 内容级由门控/矩阵兜底，表达成立 | 是否通过 |

## 6. 证据清单

- 进度文档：`.hermes/plans/系统健康诊断_agentic审查者/系统健康诊断_agentic审查者_实施进度.md`（§1/§2/§3 G3 行/§5）
- 主方案：`.hermes/plans/系统健康诊断_agentic审查者/系统健康诊断_agentic审查者_实施方案.md`（§3.2/§3.3/§9.1，SHA-256 `9dc21222…`）
- 人工必要审查点：`系统健康诊断_agentic审查者_实施方案补充-人工必要审查点.md`（§3 R3）
- P4 测试：`env PYTHONPATH="src" uv run pytest tests/test_system_health_injection.py tests/test_system_health_echo_chamber.py tests/test_long_term_read_port.py -q`（28 passed，父侧 2026-08-15 复验 + 2026-08-16 现场复跑）；全量 `pytest tests/ -q`（1927 passed, 0 failed, 4 skipped）
- Context before/after 样本：§2.2 离线渲染实测输出（fixture 生成，脚本可复现）
- R2 前置 smoke：`sar_orch/results/diagnosis_smoke_20260813_084820.json`（已 APPROVE，G2/R2 2026-08-14）
- P0–P3 提交：`bfd1b47`（决策即事件 + 诊断通道）

---

*本审查包由父 agent 生成（2026-08-16），提交用户对 G3/R3 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入审批记录并回写进度文档。*
