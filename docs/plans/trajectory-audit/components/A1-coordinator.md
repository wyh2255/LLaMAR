# A1 Coordinator 侧轨迹落地盘点

> 只读盘点，证据以当前工作区（HEAD 45299ce，2026-09-03）实测为准；实际产物样本来自 `sar_orch/results/` 最近 run（2026-07-19/21，code_commit=0119d39）。产物全部早于 Phase 5 与新版 Context 代码，凡涉及新旧差异处已显式标注。

## 0. 范围与代码入口（类/模块 + file:line）

| 角色 | 模块 | 关键入口 |
|------|------|---------|
| SAR 编排壳 | `sar_orch/coordinator.py`（1033 行） | `_log_send_message` :154-272；`_append_decision_event` :273-326；`_log_router_outcome` :344-367；`_on_router_event` :369-445；`start()` :447-838（semantic_map 接线 :458-476；MapSummarizer :486-518；long-term :520-551；diagnosis :553-599；state_provider :601-634；memory ingestor :640-673；ContextConfig :711-717；EventStore log_dir :826-829） |
| 实验 CSV 日志 | `sar_orch/logger.py`（727 行） | `log_router_interaction` :435-478；`log_coordinator_state` :316-361；`log_event` :377-401；`log_subtask` :406-429；`log_token_usage` :484-549；header :591-665 |
| Coordinator Agent 内核 | `src/Agent/router_agent/agent.py`（850 行） | run 循环 :429-836；`log_request` :531-533；`log_response` :582-588；`log_tool_result` :758-764；step_callback 三事件 :600-607/:706-712/:792-806 |
| AgentLogger NDJSON | `src/Agent/router_agent/logger.py`（207 行） | `log_request` :93-129；`log_response` :130-163；`log_tool_result` :164-189；文件名 :69 |
| Context 注入 | `src/Agent/router_agent/context.py`（1598 行） | `assemble` :888-922；`_render_memory_block` :1035-1072；`_render_environment_view` :1123-1320；`_render_current_state` :1322-1435；`_render_task_plan` :1461-1519；`save_snapshot` :318-358；CoordinatorPinnedState :1078-1102 |
| Hooks | `src/Agent/router_agent/hooks.py` | `CoordinatorSARHooks.pre_llm` :70-84（prune_history + assemble） |
| 控制器 | `src/Agent/controller/controller.py` | `submit` :164-277（task_id 默认 None；snapshot 守卫 :274-275） |
| 编排执行 | `src/a2a/coordinator/agent_executor.py` | `_execute_agentic` :305-425（sink=TeeSink :373-377；submit 不传 task_id :396-403） |
| Server | `src/a2a/coordinator/server.py`（2846 行） | RouterAgent 构造 :415-431；set_semantic_map :1022-1031；observation 摄入 :1105-1106；control-state :481 |
| 事件存储 | `src/a2a/coordinator/event_store.py` | `append` :75-127（落盘字段 :106-118，**丢弃 observation**） |
| TaskLogger | `src/a2a/coordinator/task_logger.py` | `init_task` :60-77；`log_event` :109-186（截断 content→2000/tool_arguments→1000） |
| SendMessageTool | `src/a2a/builtin_tools/send_message.py`（831 行） | execute :116-135；`_handle_activate_plan_node` :750-794 |
| Prompt | `sar_orch/prompts/coordinator/` | system.md / system.semantic.md / system.oracle.md（语义模式由 coordinator.py:815-820 加载 system.semantic.md） |
| 语义地图 | `sar_orch/map/store.py` | `set_jsonl_path` :183-186；`_append_jsonl_locked` :703-715 |
| 实验装配 | `sar_orch/experiment.py` | long_term_mode 默认 off :403；SARCoordinator 构造 :611-640；semantic_map.jsonl 重定向 run 根 :702-708 |
| 文档 | `docs/system_docs/logging_map.md`（校准基线 main@a459481） | 记录点 2b :71-74；3 :78-87；4b/4c :98-103；5a :130-137；5b :141-148；5c :152-158；7 :190-201；8 :205-225；9 :229-241；11 :259-275 |

## 1. 轨迹产物清单

| 产物(文件/存储) | 内容字段 | 触发点 file:line | 写入方式/粒度 | 保留策略 | 实际示例路径 |
|---|---|---|---|---|---|
| run 根 `router_interactions.csv` | 旧产物 7 列：Step/Subtask/AssignedTo/RunID/CorrelationID/WorkerTaskID/EventType；当前代码 9 列（+Success/ErrorType，logger.py:629-639） | tool_start 分发 coordinator.py:386-432（assign_task :389-390、update_plan :391-404、query_sar_state :405-418、query_task_events :419-425、finish_task :426-432）+ tool_result 统一 outcome :433-445→:344-367 | CSV append+flush；assign_task 记 content/who 全文，reply/cancel 记摘要 | 每次 run 覆盖目录 | `sar_orch/results/20260721_201943_s3_s42_a4/router_interactions.csv`（83 行：assign_task 30 / query_task_events 25 / cancel_task 28；**无 reply_to_help/activate_plan_node/update_plan 行**） |
| run 根 `agent_interactions.csv` | 15 列（Step/Agent/ToolName/…/Success/ErrorType，logger.py:612-628） | **Coordinator 行仅 oracle 模式**：query_sar_state tool_result → `log_coordinator_state` coordinator.py:438-445（state_summary[:2000]） | CSV append+flush | 同上 | 全部现有产物 Coordinator 行 = 0（semantic 模式无 query_sar_state 工具，coordinator.py:676-677）；示例 `20260721_201943_s4_s42_a5/agent_interactions.csv`（7216 行，Coordinator 0 行） |
| run 根 `token_usage.csv` | 11 列（Step/Agent/…/CacheMissTokens/RunID/LLMLatencyMs/Model/PromptVersion） | router llm_response coordinator.py:374-385；MapSummarizer sink :492-507；MapAgent sink :745-763 | CSV append+flush；每轮一条 | 同上 | `20260721_201943_s3_s42_a4/token_usage.csv`：Coordinator 71 行（== 该 run llm_request 71 次，一一对应）、MapAgent 58 行 |
| run 根 `subtasks.csv` | 10 列（logger.py:653-664） | 仅 assign_task 分支 coordinator.py:181 | CSV append；**Status 恒为 assigned**（无终态更新调用点，log_subtask 全仓仅 coordinator.py:181 一处） | 同上 | 同上 run（30 行全部 `assigned`） |
| run 根 `events.ndjson` | `{agent, correlation_id, event_type, payload}` | `_log_send_message` 内 5 处 coordinator.py:188/:215/:240/:254/:266 | NDJSON append | 同上 | 同上 run（assign_task 30 / cancel_task 28 / send_message 21=**全部 activate_plan_node**） |
| coordinator/`<task_id>.ndjson`（AgentLogger） | `llm_request`：ts/task_id/context_id/event/messages(**完整消息列表=最终 prompt 全文**)/tools/step_index；`llm_response`：content/thinking(代码有，旧产物无)/tool_calls(完整 schema)/finish_reason/usage；`tool_result`：tool_name/arguments/success/result/error | agent.py:531-533 / :582-588 / :758-764 → logger.py:93-129/:130-163/:164-189 | NDJSON append+flush；每 LLM 调用一条 request+response、每工具一条 result | 按 task_id 分文件；**coordinator 恒为 `unnamed_task.ndjson`**（executor submit 不传 task_id，agent_executor.py:396-403→controller.py:263-268） | `20260721_201943_s3_s42_a4/coordinator/unnamed_task.ndjson`（267 行：llm_request 71 / llm_response 71 / tool_result 125；req[-1] 注入块 7565 字符全文） |
| coordinator/`<friendly>.ndjson`（TaskLogger） | meta/task_start/agentic_start/llm_response/tool_start/tool_result/task_final 等；截断 content→2000、tool_arguments→1000（task_logger.py:109-186） | executor/router 各事件源（agent_executor.py:390-393/:427-435 等） | NDJSON append；>10MB 跳过 | 按任务 | `20260721_201943_s3_s42_a4/coordinator/20260721_201951.ndjson` |
| coordinator/`events_<task_id>.ndjson`（EventStore） | ts/task_id/context_id/event_type/state/text[:500]（event_store.py:106-118；**observation 字段落盘丢弃**） | push-callback 各事件 + watchdog supervision（event_store.py:75-127；task_watchdog.py:379-383） | NDJSON append；每 task 上限 500 条 | 按 task | 同上 run `events_dsp_*.ndjson`×28（supervision_event 39 / status_update 32 / task_created 16 / artifact_update 5）；7/19 run 另有 observation_report 事件 |
| coordinator/`supervision_<task_id>.ndjson` | 同上 EventStore schema | task_watchdog 监督事件 | NDJSON append | 按 task | 同上 run ×28 |
| coordinator/`snapshot_<task_id>.json` | pinned/loaded_skills/messages（context.py:318-358） | **coordinator 侧永不触发**：controller.py:274 `if result.need_input and task_id`，task_id 恒 None；worker 侧正常 | JSON overwrite | 加载后删除 | worker 侧：`20260721_020418_s2_s42_a2/workers/Alice/Alice/snapshot_1d2cfb32-*.json`；coordinator 侧无任何样本 |
| run 根 `semantic_map.jsonl` | 每行完整观测（observation_ingested 等，store.py:703-715） | SemanticMapStore 摄入（server.py:1105-1106 ingest_observation）；路径重定向 run 根 experiment.py:702-708 | NDJSON append | 每次 run | 7/19 run 有（`20260719_200429_s5_s42_a2/semantic_map.jsonl`）；**7/21 全部 run 无**（对应 run 内 observation_report 事件为零） |
| coordinator/`coordinator-control-state.json` | {epoch, active_context_id, dispatches}（MissionRuntimeManager，server.py:481） | 运行时控制状态持久化 | JSON overwrite | 每次 run | `20260721_201943_s3_s42_a4/coordinator/coordinator-control-state.json`（logging_map 未列） |
| `long_term/long_term.sqlite3`、`diagnosis/diagnosis.sqlite3` | 反思/诊断事件（contracts.py:847/:883） | coordinator.py:520-551/:553-599；`--long-term-mode != off`（experiment.py:403 默认 off） | SQLite | 每次 run | 全部产物无（默认 off） |
| run 根 summary.csv / trajectory.csv / run_metrics.json / metadata.json | 实验级汇总（metadata.json 5c 字段与产物比对一致） | logger.py:677-727 等 | 覆盖/append | 每次 run | `20260721_201943_s3_s42_a4/metadata.json`（字段 21 项，与 logging_map 5c 描述一致） |

## 2. 三问回答

### Q1 本组件是否有完整轨迹记录？（有 + 证据）

**有，Coordinator 每轮 LLM 调用的完整 prompt 全文落盘。**
- AgentLogger `llm_request` 记录 `hooks.pre_llm` 之后的最终消息列表（agent.py:531-533，`messages_for_llm` 即已注入 Context 的完整 prompt），含 system prompt 全文、完整历史、注入块全文；字段逐消息含 role/content/thinking/tool_calls/tool_call_id（logger.py:93-129）。
- 注入 Context 内容：system.semantic.md（semantic 模式，coordinator.py:815-820）+ Environment State 块（`assemble` context.py:888-922；`_render_memory_block` :1035-1072 渲染 Environment/Current State/Task Plan & Progress 各段；read_port 模式走 provider 渲染含 Long-term Memory/System Health 段，data_flow.md:456）。**旧版产物注入块为「## Context Memory」格式**（7/21 run），当前代码为「--- ENVIRONMENT STATE ---」格式（context.py:1051），**新版注入格式无真实产物样本**。
- 产物验证：`unnamed_task.ndjson` 71 条 llm_request，末轮消息 150+ 条、注入块 7565 字符全文；token_usage.csv Coordinator 71 行与 llm_request 数严格一致。

### Q2 交互记录是否保存？（部分，逐项结论+证据）

| 项 | 结论 | 证据 |
|---|---|---|
| LLM 输入（完整 prompt） | ✅ 保存（全文） | AgentLogger llm_request logger.py:93-129；产物 unnamed_task.ndjson |
| LLM 输出（content/tool_calls/finish_reason/usage） | ✅ 保存 | AgentLogger llm_response logger.py:130-163；产物 71 条（含 tool_calls 完整 schema 与 usage） |
| LLM 输出（thinking/决策理由） | ⚠️ 代码有字段、无真实产物 | logger.py:150-151 `if thinking: entry["thinking"]=...`；7/21 产物 llm_response 均无 thinking 键（当时代码/模型未产出） |
| 工具调用（参数+结果） | ✅ 保存 | AgentLogger tool_result logger.py:164-189（arguments 全文+result）；TaskLogger tool_start/tool_result（截断） |
| 消息收发（dispatch 四类） | ⚠️ 部分 | assign_task/reply_to_help/cancel_task → router_interactions.csv + events.ndjson 全参（coordinator.py:169-250）；**activate_plan_node 只进 events.ndjson（event_type=send_message，:251-264）不进 router_interactions.csv**；update_plan 只进 decision event（:391-404，依赖默认未启用的 memory ingestor） |
| 消息收发（工具结果） | ✅ 保存 | tool_result outcome 统一写 router_interactions.csv（coordinator.py:433-445→:344-367） |
| finish_task 判断依据 | ✅ 可追溯 | finish_task 调用前完整上下文在 llm_request 全文；调用本身在 router_interactions.csv + AgentLogger tool_result + TaskLogger |

### Q3 与本组件相关的状态是否逐步保存？（部分 + 证据）

- **内存态（pinned state）**：有，`CoordinatorPinnedState`（context.py:1078-1102）每轮 tool_result 后 observe 更新（:1521-1598 _extract_pinned），但**只存内存**。
- **磁盘状态**：部分。
  - coordinator 侧 snapshot：**无**（controller.py:274 守卫 task_id，executor 不传 → 永不 save_snapshot；worker 侧正常，产物见 workers/*/snapshot_*.json）。
  - 语义地图：有 jsonl（7/19 run）但 **7/21 run 缺失**（该 run observation_report 事件为零，摄入未触发）；跨 run 行为不一致，原因未查证。
  - EventStore observation 字段：**落盘丢弃**（event_store.py:106-118 只写 text[:500]），结构化观测仅内存 EventRecord + semantic_map.jsonl。
  - long-term / diagnosis：默认 off（experiment.py:403），全部产物无 sqlite。
  - MissionRuntime 控制状态：有（coordinator-control-state.json，server.py:481）。

## 3. 与 logging_map.md 对照（逐条标 一致/冲突/补充）

| 记录点 | 结论 | 证据 |
|---|---|---|
| 3 路由调度：assign/reply/cancel 触发点与字段 | ✅ 一致 | coordinator.py:169-250 vs :78-87；产物 83 行验证（EventType 分布与 correlation_id `coordinator-dispatch-N` 一致） |
| 3 注：activate_plan_node「不经 _log_send_message」 | ⚠️ 冲突(表述) | 实际在 `_log_send_message` 内处理（coordinator.py:251-264），只是不写 router_interactions.csv；行为一致（不经 CSV）但调用路径表述错误 |
| 4b/4c token（Coordinator/MapSummarizer/MapAgent） | ✅ 一致 | coordinator.py:374-385/:492-507/:745-763；产物 Coordinator 71 + MapAgent 58 行 |
| 5a subtasks.csv | ⚠️ 部分冲突 | 字段与触发点一致（logger.py:406-429、coordinator.py:181）；但产物 Status 恒为 assigned，文档未声明「无终态更新」 |
| 5b events.ndjson | ✅ 一致 | coordinator.py 5 处 log_event；产物 assign/cancel/activate 全验证 |
| 5c metadata.json | ✅ 一致 | experiment.py:68-103 vs 产物字段 21 项全对上 |
| 7 snapshot | ⚠️ 部分冲突 | 实现存在（context.py:318-358）但 coordinator 侧因 task_id=None 永不触发（controller.py:274）；文档未注明 coordinator 侧不适用 |
| 8 AgentLogger NDJSON | ⚠️ 部分冲突 | 事件/字段一致（logger.py:93-189，产物验证）；**文件名恒为 unnamed_task.ndjson**（executor 不传 task_id），与「`<task_id>.ndjson`」不符 |
| 9 TaskLogger | ✅ 一致 | task_logger.py:60-77/:109-186；产物 meta/task_start/agentic_start/llm_response/tool_* 全验证 |
| 11 EventStore | ⚠️ 部分一致 | schema/截断一致（event_store.py:106-118）；**产物另有 task_created 事件类型**（16 次）文档未列；observation 字段落盘丢弃未声明 |
| 2b query_sar_state → agent_interactions Coordinator 行 | ⚠️ 部分冲突 | 代码存在（coordinator.py:438-445）但仅 oracle 模式注入 query_sar_state 工具（:676-677）；全部现有产物（semantic）Coordinator 行 = 0，文档未标模式条件 |
| （无对应记录点）coordinator-control-state.json | ➕ 补充 | server.py:481 MissionRuntimeManager 控制状态，logging_map 未列 |
| （无对应记录点）semantic_map.jsonl 跨 run 缺失 | ➕ 补充 | 7/19 run 有 / 7/21 run 无（observation_report 零事件） |
| （无对应记录点）产物 router_interactions 无 9 列样本（7 列或早期 3 列） | ➕ 补充 | 77 个 run：65 个 7 列 + 12 个 3 列（sar_experiment_20260704/05 早期），全部无 Success/ErrorType 列；9 列（logger.py:629-639）无任何真实样本 |

汇总：一致 6、冲突 3（activate 表述 / subtasks 无终态 / snapshot 与 unnamed_task 文件名 / 2b 模式条件，计 4 处文档未声明或表述不符）、补充 4。

## 4. Gap 清单

| # | Gap | 证据 file:line | 对"用轨迹探索框架优化"的影响 |
|---|---|---|---|
| G1 | **全部现有产物（2026-07-19/21，code_commit=0119d39）早于 Phase 5 与新版 Context**：9 列 router_interactions、thinking、ENVIRONMENT STATE 块、read_port、long-term、diagnosis 均无真实样本 | 77 个 run 含 router CSV：65 个 7 列 + 12 个 3 列（sar_experiment_20260704/05 早期 run），9 列零样本；7/21 注入块为旧「Context Memory」格式 | 高：框架优化若依赖新字段，必须先跑新基线 run 验证实际落盘 |
| G2 | **activate_plan_node / update_plan 不进 router_interactions.csv**；update_plan 的 decision event 依赖默认未启用的 memory ingestor | coordinator.py:251-264（activate 无 log_router_interaction）；:391-404 + :640-673（ingestor 条件）；产物：activate 21 次仅在 events.ndjson | 高：DAG 计划与激活的 dispatch 记录在 CSV 层断链，需跨 AgentLogger/events.ndjson 拼装 |
| G3 | **EventStore 落盘丢弃 observation 结构化字段**（仅 text[:500]）；7/21 run 连 semantic_map.jsonl 都无 | event_store.py:106-118；observation_report 事件在 7/19 有 / 7/21 无 | 高：Worker 观测证据（语义地图/探索轨迹的源头）在事件流不可追溯 |
| G4 | coordinator 侧 snapshot 永不落盘（暂停/恢复上下文不可重建） | controller.py:274 `if result.need_input and task_id`；agent_executor.py:396-403 不传 task_id | 中：coordinator 侧 help 流程的现场无法事后恢复（worker 侧正常） |
| G5 | subtasks.csv 只有 assigned 快照，无 completed/failed/失败类 | coordinator.py:181（唯一调用点）；产物 30 行全 assigned | 中：任务生命周期需跨 subtasks/TaskLogger/EventStore 4 文件拼装 |
| G6 | coordinator AgentLogger 文件名恒为 `unnamed_task.ndjson`，与文档 `<task_id>.ndjson` 不符；长会话无法按任务切分 | agent_executor.py:396-403 → logger.py:69 | 低-中：单任务 run 内容完整，但文件语义误导；多任务场景混淆 |
| G7 | oracle 模式（query_sar_state / 记录点 2b / finish_task 路径）零产物样本 | 全部 run oracle_mode=false | 低：oracle 路径代码在但从未跑过 |
| G8 | thinking 字段只有代码、无产物验证 | logger.py:150-151；7/21 产物 llm_response 均无 thinking | 中：决策理由分析（thinking）依赖它，需新 run 确认 |
| G9 | 7/21 run semantic_map.jsonl 缺失（摄入未触发），跨 run 行为不一致 | server.py:1105-1106 摄入线 vs 7/21 产物无 observation_report/semantic_map.jsonl | 中：语义地图演化只在部分 run 可重建；原因未查证（疑配置/版本差异） |

## 5. 初步观察：现有轨迹能支撑哪些框架优化分析

1. **决策上下文重建（Q4=可重建）**：llm_request 全文（含注入块）支撑"第 N 轮 coordinator 看到了什么"的逐轮还原，71 轮样本完整；是决策质量/上下文相关性分析的主数据源。
2. **每轮成本与缓存分析**：token_usage.csv Coordinator 行与 llm_request 严格一一对应，支撑每轮 token/缓存命中率/摘要触发时刻分析。
3. **上下文增长与压缩分析**：llm_request 消息数/注入块体积逐轮演化（末轮 150+ 消息、7.5KB 块），可分析 context 增长曲线与 `_summarize_messages`/`compress_with_llm` 触发合理性。
4. **dispatch 行为审计**：router_interactions + events.ndjson 支撑 assign/cancel/reply/activate 的频次、参数、时序分析（activate 需从 events.ndjson 补）。
5. **工具调用时序**：TaskLogger NDJSON 时间戳支撑每轮工具调用序列与延迟分析。
6. **观测→语义地图链路**：7/19 run（observation_report + semantic_map.jsonl 全）可分析 Worker 证据如何组成语义地图；7/21 run 不可。
7. **基线对照需求明确**：G1-G3/G8 均需先跑新代码基线 run（或复用 7/19 类 run）才能支撑对应分析；当前产物只能支撑 1-6 中与旧版字段兼容的部分。
