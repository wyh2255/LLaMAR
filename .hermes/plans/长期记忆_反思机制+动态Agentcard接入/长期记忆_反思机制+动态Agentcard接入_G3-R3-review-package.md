# G3/R3 审查包 — `long_term_mode: shadow → read` 放行（coordinator Context 注入）

> **性质：** 本文件是人工必要审查点 R3（G3）的审查材料，由父 agent 基于 P5 真实实现、独立 review 与真实运行证据生成，提交用户审批。批准后放行 `long_term_mode=read`（真实 SAR run 中把 published 长期记忆作为受预算约束的 Environment State section 注入 coordinator Context；worker 永不可见）。
> **绑定基线：** HEAD `8869328`（P5 工作区未 commit 改动，随审批后提交）；主方案 SHA-256 `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`；人工审查点补充 `218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d`；G2 审批记录 reviewed_commit `8869328`。
> **结论：** ✅ **APPROVE（2026-08-12，用户原话「按照你的建议来」）**——审批记录见 [`长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md`](长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md)（G3-1 放行 read / G3-2 选项 A 观察点关闭 / G3-3 Minor 追认 + P2 数字修正追认）。

---

## 1. 审批范围（精确到边界）

**批准后放行：**
- `long_term_mode=read`：真实 SAR run 中 coordinator Context 注入 published-only 长期记忆段（`### Long-term Memory`），受 token budget 约束（丢弃顺序 Task > Spatial > Embodied > **Long-term** > Temporal > Freshness，低预算裁剪留 TRUNCATED）；
- read 10-run 真实验证（5 scenes × `{2,4}` agents × seed 42，`--long-term-mode read`），作为 G3 审批记录绑定证据与 G4 材料；
- coordinator 每轮看到长期段 → LLM 决策受其影响（这是本 Gate 的人工判断核心）。

**不授权（G3 放行边界）：**
- canonical schema migration、Context cutover、commit/push（提交时机由用户另行指示）；
- 反思输入范围扩展（仍只读 callback.*/evidence.projection/supervision.*/control.* 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动；
- worker 侧任何形式的长期记忆可见性（ACL 冻结：worker 视图永不包含长期段与 long_term_revision）。

## 2. 证据汇总（全部真实产物，父侧独立核验）

### 2.1 P5 实现与测试（2026-08-12）

| 项 | 证据 |
|---|---|
| 12 项 P0 冻结 RED 契约转 GREEN | `pytest tests/test_long_term_environment_state.py tests/test_environment_state_acl.py tests/test_coordinator_state_provider.py tests/test_environment_state_provider.py tests/test_long_term_read_port.py` → **83 passed**（12 RED 全 GREEN + 71 项守护零回归；78 为 M-1/budget 修复前数字，修复追加 5 项防回归，见 §3.1/§3.4） |
| 相关回归 9 文件 | **217 passed**（shadow compare / context_snapshot / contracts / store 46 / reflection / agentcard / embodied / push_callback） |
| 全量回归 | `pytest tests/ -q` → **1850 passed, 4 skipped, 0 failed**（144s） |
| ruff | 改动 7 源文件逐文件与 HEAD 基线对比，**零新增违规**（基线债务 35 项逐行核对未新增） |
| 独立交叉审查 | 独立 review subagent（577s，逐文件读码 + 独立复现脚本 + 367 项相关测试）：**0 Blocker / 1 Major / 10 Minor**；Major M-1 已修复（见 §3.1） |
| 防回归测试 | `tests/test_long_term_read_port.py`（新建 14 项）+ `tests/test_long_term_reflection.py` 追加 1 项（source_revision 回填） |

### 2.2 Coordinator Context 样本（R3 人工判断核心）

**Before（shadow run 真实样本）** — `sar_orch/results/long_term_memory_20260812_130327/scene_1_agents_4/coordinator/unnamed_task.ndjson`（llm_request 事件，read_port 路径真实渲染，redacted 后）：

```
---
## Environment State
---
### Embodied State
- Alice (agent)
  - capability: ['firefighting', 'navigation', 'rescue', 'sar']
...
---
### Relevant Recent Events
- seq=1 evidence.projection actor=Alice dispatch=
...
---
### Freshness / Conflicts / Evidence
- scope_id: a1d5956e…
- memory_revision: 1 / view_revision: 4
- freshness: FRESH
- evidence_refs: 4
```

**After（P5 read 模式真实渲染，离线 fixture 生成，2026-08-12）** — 同一渲染函数 `render_environment_state_view` 在 `long_term_mode="read"` 下输出（3 条 published 记忆，long_term_revision=3）：

```
---
## Environment State
---
### Long-term Memory
- hazard_chemical_fire_water [hazard] (confidence=0.95): chemical fires require sand; water is ineffective and wastes transport time
- lesson_fire_coordination [lesson] (confidence=0.9): coordinate at fires: assign one worker per fire region before splitting teams
- pattern_rescue_priority [pattern] (confidence=0.8): rescue trapped persons near spreading fires first
---
### Freshness / Conflicts / Evidence
- scope_id: 7d123b84…
- memory_revision: 0 / view_revision: 0
- freshness: FRESH
```

（freshness 另携带 `long_term_revision: 3`，见 P5 契约 #3：长期 revision 参与 provider freshness/可观测 metadata，同 env_step 变更可见。）

**预算行为**：budget=1/2 → 长期段被裁 + `TRUNCATED` 显式标记；budget=3+ → 保留。长期段为固定上限摘要（每 key 一行），不可突破 token limit（冻结顺序 Task > Spatial > Embodied > Long-term > Temporal > Freshness，2026-08-12）。

### 2.3 Worker ACL 零泄漏证据

| 路径 | 证据 |
|---|---|
| worker 视图（read 模式 + 已接线 store + 有内容） | `test_long_term_read_port.py::test_worker_view_never_gets_long_term_section_even_in_read_mode`（实测断言无 `long_term_memory` 键、无 `freshness.long_term_revision`） |
| worker 视图 GREEN 守护（P0 冻结） | `test_long_term_environment_state.py::test_worker_view_never_contains_long_term_memory`、`test_environment_state_acl.py::test_worker_view_never_exposes_long_term_memory` |
| read failure | STALE/UNAVAILABLE 且无长期段（`test_read_failure_never_mixes_long_term_with_legacy_canonical`） |
| 注入门控 | `environment_state_provider.py:375-382`：`_is_system and long_term_mode == "read"` 双条件；worker HTTP 端点（server.py:2213 裸构造）因 `viewer_role="worker"` 安全 |
| review 独立验证 | 审查员独立复现：worker 全路径无泄漏；STALE/UNAVAILABLE 视图无 sections |

### 2.4 哪些进入 Context、哪些永不进入（R3 材料 #5）

**进入 coordinator Context（`long_term_mode=read`）：**
- `long_term_memory` 段：仅 `status='published'` 且当前有效（supersede 后旧行自动排除）的记忆，按 memory_key 一行一摘要：`- {memory_key} [{kind}] (confidence={confidence}): {statement}`；
- freshness 元数据：`long_term_revision`（scope 级，可观测）。

**永不进入 Context：**
- candidate / rejected / superseded 状态行（store 查询 `status='published'` 硬过滤）；
- 证据链原始事件、audit 条目、reflection_run 原始记录、模型原始输出（trace 只在 `<results>/coordinator/reflection_trace.ndjson`，truth terms 脱敏）；
- worker 视图：长期段与 long_term_revision 全部不可见（§2.3）。

### 2.5 shadow 10-run 矩阵（G2 已 APPROVE，G3 前置证据，复述）

`sar_orch/results/long_term_memory_20260812_130327/` 10/10 全绿：终末 7 completed + 3 skipped（空窗口契约正确）、框架错误码全 0、quality 全优（traceability 1.0 / violation 0 / conflict 0 / supersede 0）、reflection_run 全 completed、memories 全 published（13-41/run）、support refs 26-74/run scope 绑定一致。

### 2.6 G2-3 digest 列落地（P5 完成，G2 缺口关闭）

- `long_term_support.source_revision`：**已补填**——reflection publish 调用点回填 `snapshot.memory_revision`（reflection.py:899-907，同 scope 同快照），防回归测试 `test_run_reflection_backfills_source_revision_on_support_rows`（断言 == 7）；
- `long_term_support.event_digest`：**定义为可选列**——canonical `temporal_event` 表无 per-event digest 字段（store.py:74-95），idempotency_ledger/outbox digest 非 per-event 权威（探索证据）；四元组 `(scope_id, event_id, source_revision, event_digest)` 落库能力已就绪（`test_publish_memory_support_ref_four_tuple_backfills_optional_columns`），二元组保持 NULL 合法。

## 3. 披露（P5 期间父侧裁决与修复）

### 3.1 M-1（review Major，已修复）

`memory_read_mode="shadow"` + `long_term_mode="read"` 组合会产生 H2 shadow compare 非 allowlist diff（`.long_term_memory`）并污染 rollout audit。修复：SARCoordinator 构造期交叉校验（`_validate_long_term_mode_combo`，typed `MemoryConfigError("invalid_mode_combo")` fail closed），仅禁止该组合；`read_port+read`（G3 目标）、`shadow+shadow/off`、`read_port+shadow/off` 均放行。防回归测试 3 项（函数级非法/合法 + 构造级）。

### 3.2 测试断言更新（P0 冻结测试必要更新，同 P4 修正 3 处处理方式）

`tests/test_long_term_environment_state.py::test_existing_budget_priority_order_task_spatial_embodied_temporal` 原断言 `SECTION_PRIORITY[:4]==(task, spatial, embodied, relevant_events)` 与 P5 RED 契约（`index(embodied) < index(long_term) < index(relevant_events)`）**数学上互斥**（Long-term 插入后前 4 个必含 Long-term）；测试 docstring 本身即要求"P5 必须把 Long-term 插入 Embodied 之后、Temporal 之前"。按 docstring 意图更新为 P5 冻结前缀，断言处已注释披露。

### 3.3 默认值裁决

- `EnvironmentStateProvider.long_term_mode` 默认 `"read"`：P0 冻结 RED 契约以无参构造要求 coordinator/system 注入长期段；worker 由 `_is_system` 门控永不泄漏（§2.3 证据）。真实生产链路由 `SARCoordinatorStateProvider`（默认 `"off"`）+ `coordinator.py` 显式传参控制，默认 read 仅影响直接构造的测试/裸 provider。
- `SARCoordinatorStateProvider.long_term_mode` 默认 `"off"`：既有构造点（含测试 double）零影响。

### 3.4 review Minor 记录（10 项：1 已修 4 已修 5 记录）

| # | 内容 | 处理 |
|---|---|---|
| m1 | `EnvironmentStateProvider.long_term_store` 参数存储后未读（实际读取走 MemoryReadPort） | 记录（双接线冗余，接口对称保留） |
| m2 | publish_memory docstring 与实现矛盾 | ✅ 已修（改为 source_revision 已回填 / event_digest 可选列） |
| m3 | 测试文件头声明失实 | ✅ 已修（准确声明 + 指向 §3.2 披露） |
| m4 | SECTION_WEIGHTS 合计 1.05 | ✅ 已修（long_term 0.10→0.05，合计 1.00；权重仅文档载体，未动阈值） |
| m5 | budget 边界 2→3 无断言 | ✅ 已修（追加 budget=2 裁段 TRUNCATED / budget=3 保留不 TRUNCATED 两测试） |
| m6 | publish 中途失败 trace 双行（先 ok 后 failed） | 记录（诊断可读性瑕疵，不影响语义） |
| m7 | context 双保险对 raw provider 镜像恒 "off"（依赖 off==read 语义） | 记录（功能无影响，注释已说明） |
| m8 | snapshot 缺 memory_revision 时回填 0 而非 NULL | 记录（真实快照恒有该字段） |
| m9 | `_is_system` 为 role OR viewer_id 门（worker+id="system" 理论可见长期段） | 记录（继承既有 embodied/task ACL 同模式，全仓库无此组合构造点） |
| m10 | 空段注入使裸 coordinator provider 在 budget=1/2 时 TRUNCATED=True（行为变化） | 记录（ACL 测试 docstring 已注预期，provider 文档已补） |

## 4. 已知缺口与观察点（G3 前处理或明确定义）

1. **G2-4 观察点（他报 capability）**：scene_2_agents_4 Charlie 拉卡失败 → registry 零 claim，其他 agent 观测其能力属性 → worker_observation 低优先级 claim 落库（reducer 既有机制）。**父侧复核（2026-08-12）**：`projection_field` 实测 Charlie capability = `provenance='worker_observation'`、`source_priority=1`（含 supersede 记录），Alice/Bob/David 均为 `registry`（`source_priority=0`）——观察点成立且有实据。**本 Gate 需决策**：是否在观测路径过滤 capability 字段（G3-2）。
2. **read 10-run 尚未执行**：依赖 G3 批准后运行（`experiment.py --long-term-mode read` 入口已就绪；benchmark.py 无 long-term-mode 透传，10-run 按 G2 方式逐 run 执行）。

## 5. 风险与残留

| 风险 | 缓解 | 状态 |
|---|---|---|
| 长期记忆误导 coordinator 决策 | read 10-run 前后对比（coverage/transport/框架错误码）；G3 审批时人工看 Context 样本 | read 10-run 待 G3 后执行 |
| 长期段 token 开销 | 固定上限摘要 + 预算裁剪留 TRUNCATED；丢弃顺序钉死 | 实现 + 测试 |
| 注入内容过期/矛盾 | supersede 机制（旧行自动排除）+ published-only 过滤 + long_term_revision 可观测 | 实现 + 测试 |
| worker 泄漏 | ACL 双门控 + GREEN 守护 + review 独立验证 | 零泄漏（§2.3） |
| shadow compare 污染 | M-1 交叉校验 fail closed（shadow+read 组合禁止） | 已修复 + 防回归 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens（实测） | 已确认属预期 |

## 6. 需用户拍板的决策项

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| G3-1 | **放行 `long_term_mode=read`**（coordinator Context 注入 published 长期记忆） | shadow（G2 放行） | 批准 read：真实 run 启用注入；随后执行 read 10-run 矩阵作为 G3 证据与 G4 材料 | 是否 APPROVE |
| G3-2 | **G2-4 他报 capability 观察点**：是否在观测路径过滤 capability 字段 | 既有 reducer 机制 | 选项 A：保持现状（观察点关闭，风险可接受）；选项 B：观测路径过滤 capability（缩小 reducer 变更范围，需独立实现） | 选 A 或 B |
| G3-3 | **review Minor 记录追认**（m1/m6/m7/m8/m9/m10 记录、m2-m5 已修） | 已处理 | 追认记录项 | 是否追认 |

## 7. 证据清单

- 进度文档：`.hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md`（§1/§2/§4/§5）
- P5 测试：`env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_long_term_environment_state.py tests/test_environment_state_acl.py tests/test_coordinator_state_provider.py tests/test_environment_state_provider.py tests/test_long_term_read_port.py -q`（83 passed，父侧 2026-08-12 复验）；全量 `pytest tests/ -q`（1850 passed）
- review 报告：`/home/wyh/.hermes/profiles/coder/cache/delegation/subagent-summary-0-20260812_180703_616233.txt`
- Context before 样本：`sar_orch/results/long_term_memory_20260812_130327/scene_1_agents_4/coordinator/unnamed_task.ndjson`（llm_request）
- Context after 样本：P5 渲染函数离线输出（§2.2，fixture 生成，脚本可复现）
- shadow 10-run：`sar_orch/results/long_term_memory_20260812_130327/`
- smoke：`sar_orch/results/long_term_smoke_20260812_*.json`（G2 已 APPROVE）

---

*本审查包由父 agent 生成（2026-08-12），提交用户对 G3/R3 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入审批记录并回写进度文档。*
