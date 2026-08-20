# G4/R4 审查包（完整版）— 长期记忆正式可用边界

> **性质：** 本文件是人工必要审查点 R4（G4）的审查材料，由父 agent 基于 P0–P5 真实实现、G1–G3 审批链与真实运行证据生成，提交用户审批。批准后放行：标记 `long_term_mode=read` 正式可用并更新系统文档/进度（R4 定义）。
> **绑定基线：** HEAD `c866cc3`（P5 + G3/G4 审批链已提交）；主方案 SHA-256 `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`；人工审查点补充 `218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d`；G3 审批记录 reviewed_commit `8869328`（`长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md`）。
> **状态：** ✅ **完整版**（2026-08-12）——read 10-run 矩阵 10/10 有效、full suite fresh 全绿、recovery/rollback 演练通过、独立 review APPROVE（0 Blocker/0 Major/3 Minor 全处理）。
> **结论：** ✅ **APPROVE（2026-08-12，用户原话「批准」）**——审批记录见 [`长期记忆_反思机制+动态Agentcard接入_G4-approval-record.md`](长期记忆_反思机制+动态Agentcard接入_G4-approval-record.md)（G4-1 正式可用 + P6 收口 / G4-2 retention 接受 / G4-3 V1 边界确认 / G4-4 Minor 追认）。

---

## 1. 审批范围（R4 定义，精确到边界）

**批准后放行：**
- 标记 `long_term_mode=read`（coordinator Context 注入 published 长期记忆）**正式可用**；
- 更新系统文档/进度状态（`docs/system_docs/memory.md`、AGENTS.md、待办状态——P6 文档收口，且不覆盖用户既有 dirty hunk）；
- 接受长期库随 run 日志的**保留/备份/手动清理责任**（主方案明确不做自动 purge，保留期限由用户/项目惯例决定）。

**不授权（G4 放行边界）：**
- canonical schema migration、Context cutover、commit/push（提交时机由用户另行指示）；
- 反思输入范围扩展（仍只读 `callback.*`/`evidence.projection`/`supervision.*`/`control.*` 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动；
- worker 侧任何形式的长期记忆可见性（ACL 冻结不变）；
- future in-process AgentCard mutation（`agent_card_changed` 消息与动态 skill 增量的实现）——本 Gate 只要求给出**明确结论**（建议：维持 V1 边界，待真实 producer 出现后再设计，见决策项 G4-3）。

## 2. 证据汇总（已冻结 + 待补）

### 2.1 已冻结证据（G1–G3 链 + P5 实现，父侧独立核验）

| 项 | 证据 | 状态 |
|---|---|---|
| P5 实现与测试 | 5 文件 **83 passed**（12 RED 全 GREEN + 71 守护；78 + M-1/budget 修复 5 项防回归）、相关回归 217、全量 1850 passed 4 skipped 0 failed（144s）；ruff 零新增（基线 35 项逐行核对） | ✅ 已核验（2026-08-12） |
| 独立交叉审查 | 0 Blocker / 1 Major（M-1 已修：`_validate_long_term_mode_combo` fail closed）/ 10 Minor（m2-m5 已修，m1/m6-m10 记录追认） | ✅ review 报告 `subagent-summary-0-20260812_180703_616233.txt` |
| hash 绑定链 | 主方案 `169f9f7d…`、原子快照 `54e259ea…`、跨 Run `0146bf81…`、人工审查点 `218d7990…`、G1 审查包 `1faeff92…`、G2 审查包（HEAD 版）`d7d2fbf0…` | ✅ 全部与实际文件一致（父侧复验） |
| G3 审批 | G3-1 放行 read / G3-2 观察点关闭（supersede 链实锤）/ G3-3 Minor 追认 + 数字修正追认 | ✅ `G3-approval-record.md`（2026-08-12，用户原话「按照你的建议来」） |
| shadow 10-run（G2 基线） | 10/10 全绿：框架错误码全 0、quality 全优（traceability 1.0 / violation 0 / conflict 0 / supersede 0）、reflection_run 全 completed、memories 全 published（13-41/run） | ✅ `long_term_memory_20260812_130327/` |
| worker ACL 零泄漏 | 双门控 `_is_system and long_term_mode=="read"`（environment_state_provider.py:375-382）+ GREEN 守护 + review 独立复现；server.py:2213 裸构造因 `viewer_role="worker"` 安全 | ✅ 零泄漏（G3 审查包 §2.3） |
| G3-2 实据 | scene_2_agents_4 Charlie capability：worker_observation claim 已被 registry claim 正式 supersede（`evt_3e91…`，evidence_id=`reg:Charlie:…`） | ✅ 兜底闭环实测 |

### 2.2 read 10-run 矩阵（✅ 10/10 有效，2026-08-12 完成）

矩阵：`sar_orch/results/long_term_memory_read_20260812_200708/`（`run_g3_read_matrix.sh`，5 scenes × `{2,4}` agents × seed 42，max_steps 20；scene_4_agents_2 为重跑版 `scene_4_agents_2_rerun/`）。

| run | 状态 | steps | coverage | transport | 长期段注入（去重渲染 key） | memories/pub/reflections |
|---|---|---|---|---|---|---|
| scene_1_agents_2 | ✅ | 20 | 0.667 | 0.667 | 12/12 含段，15 key | 19/19/6 |
| scene_1_agents_4 | ✅ | 20 | 1.0 | 0.933 | 26/26 含段，25 key | 27/27/7 |
| scene_2_agents_2 | ✅ | 20 | 0.667 | 0.733 | 15/15 含段，22 key | 32/32/7 |
| scene_2_agents_4 | ✅ | 20 | 0.667 | 0.733 | 31/31 含段，55 key | 59/59/13 |
| scene_3_agents_2 | ✅ | 20 | 0.714 | 0.722 | 13/13 含段，18 key | 25/25/7 |
| scene_3_agents_4 | ✅ | 20 | 1.0 | 0.944 | 22/22 含段，28 key | 34/34/10 |
| scene_4_agents_2（重跑） | ✅ | 20 | 0.5 | 0.6 | 11/11 含段，13 key | 20/20/5 |
| scene_4_agents_4 | ✅ | 15 | 1.0 | 1.0 | 18/18 含段，22 key（fin=True） | 30/30/7 |
| scene_5_agents_2 | ✅ | 20 | 0.6 | 0.571 | 33/33 含段，18 key | 23/23/6 |
| scene_5_agents_4 | ✅ | 20 | 1.0 | 0.929 | 33/33 含段，32 key | 32/32/9 |

> **口径披露（R4-1 修订，2026-08-12）**："去重渲染 key" = 全 run 所有 llm_request 长期段中出现过的**去重 memory_key 数**（每 key 至少被渲染一次）；该值 ≤ DB published 总数（后写入的记忆可能未再渲染，属预期）。早期 `(none published yet)` 为滚动反思积累窗口。

**注入实证（10/10）**：llm_request **100% 含 `### Long-term Memory` 段**，中后期均有真实 published 记忆（资源位置 / task_stale 模式 / 工具用法策略等）。**框架错误码**：`memory_acceptance.json` 指定错误码全 0（scene_2_agents_4 等 `failed_tool_rows` 为 LLM 工具调用失败行，非框架错误，G2 同口径）；**TimeoutAgents**（barrier 超时填充）0-8 步/run 不等，与 G2 shadow 矩阵同一量级。

> **R4-2 定位注（2026-08-12，review 复核）**：`long_term_revision` 为 **JSON 视图层元数据**——渲染器 `render_environment_state_view` 的 freshness 块不输出该行（environment_state.py:255-267），coordinator LLM 永不可见；它对工具/H2 shadow compare 可见（allowlist 因此存在，测试断言 JSON 层）。现实现与测试/文档自洽，本注明确边界。

### 2.2a scene_4_agents_2 异常（根因分析，2026-08-12）

- **现象**：steps=0、`end_reason=coordinator_finished_early`、耗时 1067s（启动阶段反复重试后退出）；rc=0（脚本判定无框架异常）；
- **根因**：worker 启动期 `load_mcp_tools_async`（MCP map_agent 连接）`ConnectError: All connection attempts failed`——前序 run（scene_3_agents_4）结束 0 秒即启动本 run，MCP 端口未释放/竞争；worker 崩于 `_assemble_tools_async`（worker.py:225）→ coordinator 收不到 agent → 提前结束；
- **定性**：**环境性启动时序问题，非长期记忆功能缺陷**（9/10 run 正常执行、注入实证完整；G2 shadow 矩阵 10/10 全绿证明基础设施本身 OK）；
- **处置**：✅ **已重跑成功（2026-08-12 22:27-22:33）**——`scene_4_agents_2_rerun/`：steps=20、cov=0.5、tr=0.6（与 shadow 版 0.50/0.60 完全一致）、11/11 llm_request 含段、13 去重渲染 key；`memory_acceptance` gate **pass**（worker_busy / task_not_routable_yet / unknown_task_id 全 0）；终末反思 completed（drain joined，written 2），quality 全优（traceability 1.0 / truth violation 0 / conflict 0 / supersede 0）；原失败目录保留作证据（`scene_4_agents_2/`，end_reason=coordinator_finished_early）。

### 2.2b shadow vs read 前后对比（10/10 同口径，2026-08-12）

| 指标 | shadow（G2，10/10） | read（G3，10/10 含重跑） | 差异 |
|---|---|---|---|
| avg coverage | 0.885 | 0.781 | -0.103 |
| avg transport | 0.837 | 0.783 | -0.054 |
| 全绿场景（cov=1.0） | 5/10 | 4/10（scene_1_agents_4/3_agents_4/4_agents_4/5_agents_4） | — |
| 框架错误码 | 全 0 | 全 0（`memory_acceptance.json`） | 持平 |
| scene 对照（4_agents_2） | cov=0.50 tr=0.60 | cov=0.50 tr=0.60（重跑） | 一致 |

**解读**：read 模式指标低于 shadow（avg -0.10/-0.05），处于同量级、无框架错误、无崩溃；差异主要来自 scene_2/5 的 coverage 波动（0.67 vs 1.00 / 0.60 vs 0.80——barrier 超时填充与 LLM 决策差异，TimeoutAgents 同量级佐证），scene_4_agents_2 逐 run 对照完全一致（0.50/0.60）。长期记忆注入未造成决策退化（4/10 全绿 + 无错误码），也未带来显著提升——与"记忆为新增信息，在短 run（max_steps=20）中影响有限"的预期一致。**如需更强结论，可后续用更长 max_steps 或更多 seed 复测（不属本 Gate 范围）。**

### 2.3 R4 新增证据（✅ 已补齐，2026-08-12）

| 项 | 内容 | 状态 |
|---|---|---|
| full pytest / ruff fresh | 矩阵完成后全量 pytest **1856 passed, 4 skipped, 0 failed**（195s；1850 基线 + 并行 team 功能新增 6 测试；首轮 2 failed 为并发时序 flaky，`--lf` 重跑全绿）；长期记忆相关 6 文件独立验证 **132 passed**；ruff 7 源文件与 HEAD 基线对比零新增（G3 已核验，P5 后无新代码改动） | ✅ 实测 |
| recovery 验证 | 长期库权限 `drwx------`/`-rw-------`（owner-only）；`schema_migrations` 1 行（migration 版本 1，带时间戳 + 内容 hash `a55fd6a8…`）；`long_term_revision` scope 级 revision=27；reopen 正常读出 27 条 published；WAL/SHM 残留由 SQLite 下次打开自动恢复（不丢数据，实测 reopen 成功） | ✅ 实测（scene_1_agents_4 长期库） |
| `read_port → legacy` rollback 演练 | legacy 模式 mini run（`rollback_drill_legacy_20260812/`，scene_1_agents_2，max_steps=5，`--memory-read-mode legacy --long-term-mode off`，端口 8090/8201）：启动/关闭正常无崩溃；**read run 长期库 hash 前后一致（`55274fff…`）零触碰**；legacy run 未创建 `long_term/` 目录（零长期库 I/O）；canonical 数据保留（`memory.sqlite3` 350 temporal_events / 64 projection_fields 原样） | ✅ 演练通过 |
| retention 责任 | 主方案无自动 purge；`<results>/<run>/coordinator/long_term/` 随 run 目录保留（权限 700/600），清理为手动责任 | 📝 结论（运行治理归属） |
| 独立验收 | 独立 review subagent 交叉审查（19 文件 + 7 段复现脚本 + 937 个 worker 请求泄漏扫描）：**0 Blocker / 0 Major / 3 Minor**（R4-1 表格口径已修 / R4-2 JSON 层定位已注 / R4-3 rerun 证据卫生已注），结论 **APPROVE**（附 Minor 修订要求，全部处理）；报告 `subagent-summary-0-20260812_224346_657298.txt` | ✅ 完成 |

## 3. 披露（本 Gate 前已知）

1. **矩阵节奏波动**：read run 单次 4.5–19 分钟（LLM 调用为主），个别 run 触 barrier 超时被 TimeoutAgents 填充（0-8 步/run）——不影响长期记忆功能本身，run 级指标解读按 TimeoutAgents 列区分系统注入 NoOp（G2 同口径）。
2. **scene_4_agents_2 首跑失败（已重跑）**：MCP 启动连接失败（环境性时序问题，根因见 §2.2a）；重跑版证据完整（steps=20、cov/tr 与 shadow 对照一致）。
3. **R4-3 rerun 证据卫生**：(a) rerun stdout 未落盘（命令走 `tail -5`），关键过程证据以 coordinator 事件 trace（`20260812_222707.ndjson`）+ `reflection_trace.ndjson` 归档；(b) rerun（22:27-22:33）执行于含并行 team 功能未提交改动的工作树（worker.py/mission_runtime.py 等 9 文件，22:26-22:31 修改），但 **P5 长期记忆相关 7 文件与 c866cc3 一致**，注入/ACL 证据不受影响；前 9 run（20:07-21:58）运行于与 c866cc3 相同内容（21:05 提交捕获同一工作树）。
4. **P5 数字修正追认**：78→83（M-1/budget 修复 5 项防回归），G3 已追认。
5. **m1/m6/m7/m8/m9/m10 记录项**：G3 已追认；G4 review 逐一复核无新利用路径。
6. **O-A 观察点（反思"失忆"=记忆压缩）**：增量窗口不含已有记忆，跨反思整合不可达——G3 边界"反思输入范围扩展不授权"保持不变；是否引入 top-k 已有记忆注入属未来设计（G4 不决策，仅记录）。
7. **store 关闭后查询理论风险（review 观察项）**：`long_term_memory()`/`revision_of()` 在 store 关闭时抛 `MemoryContractError`，与 docstring"never raises"略有不符；正常关闭序下无查询窗口，矩阵未触发，建议后续 docstring 补注（不阻塞 G4）。

## 4. 风险与残留（G4 视角）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 长期记忆误导决策 | read 10-run 全量前后对比（coverage/transport/错误码）+ 注入样本人工可查 | 矩阵执行中 |
| 长期库随 run 日志丢失/权限问题 | run-local 目录随 results 保留；migration/reopen/lock-retry 已测试 | P1 GREEN，G4 汇总 |
| rollback 不可逆 | `read_port → legacy` 演练不删 canonical/long-term；模式切换纯旋钮 | 待演练 |
| 保留膨胀（无自动 purge） | 手动清理责任明确归属；每 run 长期库量级小（10-40 memories） | 结论待 G4 确认 |
| worker 泄漏回归 | ACL 双门控 + GREEN 守护 + review 复现（零泄漏已实证） | 冻结 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens | 已确认属预期 |

## 5. 需用户拍板的决策项

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| G4-1 | **标记 `long_term_mode=read` 正式可用**（含 P6 文档收口：memory.md / AGENTS / 待办状态，不覆盖用户 dirty hunk） | read 已放行（G3），矩阵执行中 | 矩阵 10/10 + full suite + 独立验收通过后 APPROVE | 是否 APPROVE |
| G4-2 | **retention/保留边界确认**：长期库随 run 日志保留、不自动 purge，清理为手动责任 | 主方案无自动 purge | 接受（运行治理归属用户/项目惯例） | 是否接受 |
| G4-3 | **future in-process AgentCard mutation**：是否允许未来实现 | V1 边界：仅注册 bootstrap，无动态 skill 增量 | 维持 V1 边界（`agent_card_changed` 待真实 producer 出现再设计）；不在本 feature 范围 | 是否确认 |
| G4-4 | **review Minor 记录项与 O-A 观察点追认**（m1/m6-m10 已在 G3 追认；O-A 记录为本 Gate 观察点） | 已记录 | 追认 | 是否追认 |

## 6. 证据清单

- 进度文档：`.hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_实施进度.md`（§1/§2/§3/§4/§5）
- G3 审批记录：`长期记忆_反思机制+动态Agentcard接入_G3-approval-record.md`（reviewed_commit `8869328`）
- P5 测试：`env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_long_term_environment_state.py tests/test_environment_state_acl.py tests/test_coordinator_state_provider.py tests/test_environment_state_provider.py tests/test_long_term_read_port.py -q`（83 passed）
- review 报告（G4）：`/home/wyh/.hermes/profiles/coder/cache/delegation/subagent-summary-0-20260812_224346_657298.txt`（0 Blocker/0 Major/3 Minor，APPROVE）
- review 报告（G3）：`/home/wyh/.hermes/profiles/coder/cache/delegation/subagent-summary-0-20260812_180703_616233.txt`
- shadow 10-run：`sar_orch/results/long_term_memory_20260812_130327/`
- read 10-run：`sar_orch/results/long_term_memory_read_20260812_200708/`（含 `scene_4_agents_2_rerun/` 重跑版）
- rollback 演练：`sar_orch/results/rollback_drill_legacy_20260812/`
- 注入实证：`<read run>/coordinator/unnamed_task.ndjson`（llm_request，`### Long-term Memory` 段）

---

*本审查包（收敛版）由父 agent 生成（2026-08-12）。read 10-run 矩阵完成、full suite/独立验收/recovery/rollback 证据补齐后，更新为完整版提交用户对 G4/R4 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入 `G4-approval-record.md` 并回写进度文档。*
