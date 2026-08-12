# G2/R2 审查包 — `long_term_mode: off → shadow` 放行

> **性质：** 本文件是人工必要审查点 R2（G2）的审查材料，由父 agent 基于真实实现与真实运行证据生成，提交用户审批。批准后放行 `long_term_mode=shadow`（真实 SAR run 中启用 run-local 长期记忆反思写入）。
> **绑定基线：** HEAD `54137050a8d5332e7f81e24b9a1f45857bd8455f`（工作区含 P0–P4 未 commit 改动）；主方案 SHA-256 `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`；原子快照补充 `54e259eabeaef0f08b506b86af12a97d185cec2f0427d2325dc18a2f0c029a98`；跨 Run 补充 `0146bf810c589f132fa6ab9749d385e44c308d3aa50ea687a194fc17234a1ea8`；人工审查点补充 `218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d`。
> **结论占位：** 待审批（`APPROVE` / `REJECT` / `REVISE`）。

---

## 1. 审批范围（精确到边界）

**批准后放行：**
- `long_term_mode=shadow`：真实 SAR run 中滚动 + 终末反思写入 run-local 长期库（`<memory_root>/long_term/long_term.sqlite3`），published 产物**只落库不注入 Context**；
- shadow run 会触发真实模型反思调用（deepseek-v4-flash，经 opencode.ai 网关）——2026-08-12 已确认成本属预期；
- P5（coordinator-only read-port 注入）的**实现**（编码 + 离线测试）可继续，但其真实验证依赖 G3。

**不授权（G2 放行边界）：**
- `long_term_mode=read`（Context 注入）——属 G3，需 shadow 10-run 矩阵 + ACL 证据 + 独立审批；
- canonical schema migration、Context cutover、commit/push；
- 反思输入范围扩展（仍只读 callback.*/evidence.projection/supervision.*/control.* 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动。

## 2. 证据汇总（全部真实产物，父侧独立核验）

### 2.1 P0–P4 实现与测试（2026-08-12）

| Phase | 内容 | 证据 |
|---|---|---|
| P0 | 契约卡 + RED 测试（106 RED + 179 GREEN，生产零改动） | 进度文档 §2 P0 行 |
| P1 | 独立长期记忆 kernel（long_term.py 850 行 + reflection.py 骨架 + contracts） | 46/46 + 25/25 GREEN，F1-F6 修复 |
| P2 | Embodied callback telemetry（worker_telemetry 第一权威） | 136 passed；review 无 Blocker（M1+m1 修复）；P0 断言零改动 |
| P3 | AgentCard registry projection（新 worker 注册 bootstrap） | 200 passed + 4 P5 预期 RED；review 无 Blocker（M1/M2 静态配置修复） |
| P4 | 原子快照 + 增量窗口 + 异步 trigger + 只读 evaluator + 接线 | 111 + 196 + 61 passed；review 无 Blocker（M1/M2 + 6 Minor 修复） |

**独立交叉审查**：P2/P3/P4 各一轮独立 review subagent，无 Blocker；全部 Major 已修复并复验。

### 2.2 真实模型 function-call smoke（主方案 §5.3 硬门，5/5）

**3 次独立运行全部 5/5**（证据 JSON ×3）：
- `sar_orch/results/long_term_smoke_20260812_044511.json`（实现 subagent）
- `sar_orch/results/long_term_smoke_20260812_044654.json`（父侧独立重跑）
- `sar_orch/results/long_term_smoke_20260812_050154.json`（M1 修复后复跑）

每次运行：5/5 completed、所有候选 refs ∈ 输入窗口、fail-closed sanity 3/3（漏字段/伪造 ref/truth term 全 rejected 零写入）、模型=deepseek-v4-flash/provider=openai/api_base_host=opencode.ai、api_key 全程 `<set>` 脱敏、总 tokens 7784-8007。

**M1 修复记录（review 发现的生产缺陷）**：smoke 5/5 后独立 review 发现生产终末路径（async run_experiment 内同步调用）`asyncio.run` 必然 RuntimeError → 终末反思 fail closed。shadow run 1 实测实锤（rejected/missing_function_call）。修复：loop 探测 + offload daemon 线程 + bounded join。修复后 loop 上下文 A-E 验证全过（含真实模型 in-loop 调用）、smoke 复跑 5/5、矩阵重启后终末反思恢复正常。

### 2.3 shadow 10-run 矩阵（主方案 §5.4，5 scenes × {2,4} agents × seed 42，max_steps=20）

目录：`sar_orch/results/long_term_memory_20260812_130327/`

| run | 终末反思 | written | coverage | transport | 框架错误码 | quality |
|---|---|---|---|---|---|---|
| scene_1_agents_2 | skipped(空窗口) | 0 | 0.833 | 0.867 | 0/0/0 | 1.0/0/0/0 |
| scene_1_agents_4 | completed | 4 | 1.0 | 1.0 | 0/0/0 | 1.0/0/0/0 |
| scene_2_agents_2 | skipped(空窗口) | 0 | 1.0 | 0.933 | 0/0/0 | 1.0/0/0/0 |
| scene_2_agents_4 | completed | 4 | 1.0 | 0.933 | 0/0/0 | 1.0/0/0/0 |
| scene_3_agents_2 | completed | 3 | 0.714 | 0.722 | 0/0/0 | 1.0/0/0/0 |
| scene_3_agents_4 | completed | 6 | 1.0 | 0.944 | 0/0/0 | 1.0/0/0/0 |
| scene_4_agents_2 | completed | 3 | 0.5 | 0.6 | 0/0/0 | 1.0/0/0/0 |
| scene_4_agents_4 | completed | 4 | 1.0 | 0.8 | 0/0/0 | 1.0/0/0/0 |
| scene_5_agents_2 | skipped(空窗口) | 0 | 0.8 | 0.714 | 0/0/0 | 1.0/0/0/0 |
| scene_5_agents_4 | completed | 6 | 1.0 | 0.857 | 0/0/0 | 1.0/0/0/0 |

（quality 列 = source_traceability_rate / forbidden_truth_violation / same_key_conflict_rate / supersede_chain_length；框架错误码 = worker_busy / task_not_routable_yet / unknown_task_id）

**落库核验（每 run SQLite 直查）：**
- reflection_run 全部 `completed`（4-10 条/run），零 rejected/failed/timeout；`long_term_memory` 全部 `published`（13-41 条/run）；`long_term_support` refs 26-74 条/run，source_scope_id 与 reflection_run scope 全一致；
- canonical Embodied：position/inventory 以 `worker_telemetry` 为第一权威落库（P2）、capability 以 `registry` 落库（P3）；
- quality artifact 10/10 存在：traceability=1.0、truth violation=0、conflict=0、supersede=0、p50 11.7-16.1s；
- skipped 的 3 个 run 是**契约正确行为**（滚动反思已消化窗口 → §3.4.2 空窗口跳过），非失败。

## 3. P0 冻结修正披露（父侧裁决 2026-08-12，共 3 处，全部附注释）

| # | 位置 | 修正 | 理由 |
|---|---|---|---|
| 1 | test_long_term_reflection.py:209-217 | 断言 `"evt_interleaved" in event_id 集合` → `causation_id == "evidence:evt_interleaved"` | 原断言与冻结 ingestor 的 event_id 生成语义矛盾（ingestor.py:766 系统生成 `evt_<uuid>`，输入 id 落在 causation_id:797）；P0 阶段该测试为 AttributeError RED，可满足性未验证；独立复现证实；快照一致性语义不变（其余 4 断言 + 3 新 store 测试覆盖） |
| 2 | test_long_term_reflection.py:506-527 | snapshot 补 events + 第二次调用 pin window_end_sequence=0 | 原 dict snapshot 无 events=空窗口，与 §3.4.2"窗口为空跳过"冲突；补 canonical 事件使幂等断言（duplicate/len==1）在正确语义下成立 |
| 3 | test_long_term_reflection.py:361/389 区 | collector fixtures 补 canonical event_type | M7 fail-closed 修复（event_type None 拒绝）下 sequence-only fixtures 全被拒 → 窗口恒空；补 allowlist 类型，断言逐字不变 |

**审查员独立证实**：#1 正确且必要（独立验证 causation_id 真实生成路径）；#2/#3 为同型对齐修正，断言逐字未变。

## 4. 已知缺口（3 项，均不阻塞 G2，G3 前处理或明确定义）

1. **`long_term_support.source_revision/event_digest` 列未填充（NULL）**：P1 移交项（"P4 填充"），P4 实现未补。当前可重放性由 (source_scope_id, source_event_id) 保证（traceability=1.0 实证）；digest 列是增强审计。建议：G3 前补填或明确定义为可选列。
2. **观察点：他报 capability**（scene_2_agents_4 Charlie）：Charlie 拉卡失败 → registry 零 claim；其他 agent 观测其能力属性 → worker_observation 低优先级 claim 落库（reducer 既有机制，`field_source_priority` 注释明示 policy 外来源仍可 admit）。非 violation，但与"能力来自 registry"的严格语义有出入，G3 时决定是否在观测路径过滤 capability 字段。
3. **m8（记录项）**：drain 超时只写 run_metrics + audit，reflection_run 表无 timeout 状态行。已定义可接受（G2 验收口径：run_metrics 有 `drain=timeout` 记录即可）。

## 5. 风险与残留

| 风险 | 缓解 | 状态 |
|---|---|---|
| 反思模型输出幻觉/伪造 ref | fail-closed validator（缺 function-call/伪造 ref/truth term/重复 key 全拒）+ shadow 不注入 Context | 已实现 + smoke sanity 3/3 + 10-run 零 rejected |
| 模型调用阻塞实验 | 异步线程 + coalesce + drain join 超时（60s）不阻塞退出 | 10-run 实测无阻塞 |
| 终末反思生产上下文故障 | M1 修复（loop 探测 + offload 线程） | 矩阵重启后 7 completed + 3 skipped 验证 |
| 长期库写入污染 canonical | 独立文件独立 schema；无 LLM 在事务内；锁耗尽 typed retryable | 10-run 无异常 |
| shadow 模式不注入 → 无 Context 风险 | shadow 下不渲染长期段（P5 实现约束） | 设计冻结 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens（实测） | 2026-08-12 已确认属预期 |

## 6. 需用户拍板的决策项

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| G2-1 | **放行 `long_term_mode=shadow`** | off（默认） | 批准 shadow：真实 run 启用 run-local 长期记忆写入（滚动+终末），published 只落库不注入 | 是否 APPROVE |
| G2-2 | **P0 冻结修正 3 处追认** | 父侧裁决已执行 | 追认（审查员已独立证实 #1 正确必要，#2/#3 断言逐字不变） | 是否追认 |
| G2-3 | **support digest 列缺口** | NULL（P1 移交项） | 接受为已知缺口，G3 前补填或定义可选 | 是否接受 |
| G2-4 | **他报 capability 观察点** | 既有 reducer 机制 | 接受为观察点，G3 决定是否过滤 | 是否接受 |
| G2-5 | **P5 实施授权** | P5 未开始 | G2 批准后 P5（read-port 注入实现 + 离线测试）可实施；真实验证与 G3 绑定 | 是否授权 |

## 7. 证据清单

- 进度文档：`.hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md`（§1/§2/§4/§5，含各 Phase 验收数字）
- smoke 证据：`sar_orch/results/long_term_smoke_20260812_044511.json` / `_044654.json` / `_050154.json`
- 10-run 矩阵：`sar_orch/results/long_term_memory_20260812_130327/`（每 run：run_metrics.json / long_term_memory_quality.json / coordinator/long_term/long_term.sqlite3 / coordinator/memory/memory.sqlite3）
- 测试：`env PYTHONPATH="src:$PYTHONPATH" uv run pytest`（P4 组 111 + 回归组 196 + experiment 面 61）
- P0 冻结修正：#1-#3 在 `tests/test_long_term_reflection.py` 内注释 + 本节披露

---

*本审查包由父 agent 生成（2026-08-12），提交用户对 G2/R2 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入审批记录并回写进度文档。*
