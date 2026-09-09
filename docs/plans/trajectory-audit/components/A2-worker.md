# A2 Worker/Agent 侧轨迹落地盘点

日期: 2026-09-09（只读调研，零写仓改动）
代码基线: main@3ad6980 工作区（行号以实测 grep 为准）
实际产物样本: sar_orch/results/20260721_201943_s3_s42_a4（场景3/seed42/4 agents/30步，模型 deepseek-v4-flash，prompt_version=baseline，code_commit=0119d39，metadata.json 实测）

## 0. 范围与代码入口（类/模块 + file:line）

Worker 侧 Agent 轨迹落地共 4 条通路（SAR 实验模式）：

| 通路 | 入口 | 落盘 |
|------|------|------|
| 实验级交互 CSV | sar_orch/worker.py:275-337 `_on_step_event`（step_callback） | agent_interactions.csv / token_usage.csv（经 ExperimentLogger） |
| Agent 级 NDJSON | src/Agent/worker_agent/logger.py:86-181 AgentLogger（llm_request/llm_response/tool_result） | workers/<Agent>/<worker_id>/<task_id>.ndjson |
| A2A 事件推送 | src/a2a/worker/sink.py:53-119 A2AWorkerSink.emit（[DATA] 块） | 不落盘，推送 coordinator（见 A1） |
| 环境步轨迹 | sar_orch/barrier.py:387-399 drain_step_logs + 566-585 step log 快照 | trajectory.csv（含 Actions/TimeoutAgents） |

关键接线：
- 事件分发：src/a2a/worker/agent_adapter.py:204-210 `TeeSink([A2AWorkerSink, CallbackSink(ext_cb)])`——同一份 step 事件同时进 A2A 推送与 `sar_orch/worker.py._on_step_event`。
- AgentLogger 注入：src/a2a/worker/a2a_server.py:216 `worker_log_dir = log_dir / worker_id`（实验下 worker_id=agent_name，experiment.py:715），SARWorker 构造 log_dir=workers/<AgentName>/（experiment.py:448-460, 726）。
- 事件源：src/Agent/worker_agent/agent.py:549/600/817 在 LLM 调用前后与工具执行后调用 AgentLogger；:620/727/852 同步调 step_callback（传入**完整未截断** content）。
- 上下文裁剪：src/Agent/worker_agent/hooks.py:70-86 `WorkerSARHooks.pre_llm` → context.py `prune_history`（:851-906）+ `assemble`（:946-975），在 agent.py:546 先于 :549 log_request 执行——**日志记录的是裁剪后的 messages**。
- 空闲心跳 auto-noop：sar_orch/worker.py:533-552（`submit_action("NoOp", advance=False)`）；超时注入：sar_orch/barrier.py:243-247（advance=True）。
- 工具集：sar_orch/tools/worker/ 15 个（navigate_to/move/explore/carry_person/drop_off_person/get_supply/store_supply/use_supply/clear_inventory/no_op/get_agent_state/report_observation/query_shared_memory/finish_task + MCP map_agent_*）。
- Prompt：sar_orch/prompts/worker/system.md（72 行，prompt_version="baseline"，无版本哈希）。

## 1. 轨迹产物清单

| 产物(文件/存储) | 内容字段 | 触发点 file:line | 写入方式/粒度 | 保留策略 | 实际样例路径 |
|---|---|---|---|---|---|
| agent_interactions.csv | Step,Agent,ToolName,ToolArgs(JSON),Action,SAR Observation,LLMInput(6×200字符),LLMOutput,Thinking,RunID,CorrelationID,EventType,ToolLatencyMs,Success,ErrorType | worker.py:314-337 tool_result 分支（每工具执行完一条） | CSV append+flush；1 行/工具执行 | run 目录保留 | sar_orch/results/20260721_201943_s3_s42_a4/agent_interactions.csv（441 行，全部 EventType="tool_result"） |
| token_usage.csv | Step,Agent,PromptTokens,CompletionTokens,TotalTokens,CacheHitTokens,CacheMissTokens,RunID,LLMLatencyMs,Model,PromptVersion | worker.py:295-305 llm_response 分支（每收到含 usage 的响应） | CSV append+flush；1 行/LLM 响应 | 同上 | 同 run/token_usage.csv（440 行；Alice/Bob/Charlie/David/Coordinator/MapAgent） |
| workers/<Agent>/<worker_id>/<task_id>.ndjson | llm_request: messages 全量(role/content/thinking/tool_calls/tool_call_id/name)+tools+step_index；llm_response: content/thinking/tool_calls(完整)/finish_reason/usage；tool_result: tool_name/arguments/success/result/error | logger.py:86-121 / 122-155 / 156-181；调用点 agent.py:549/600/817 | NDJSON append+flush；1 行/事件 | 按 task_id 分文件；无截断（仅 SensitiveTextRedactor 脱敏） | 同 run/workers/Alice/Alice/52d00fd1-….ndjson 等 28 个文件（4 agent；事件类型实测 llm_request 323 / llm_response 311 / tool_result 441） |
| A2A [DATA] 推送（不落盘） | llm_response: content[:2000]；tool_start: arguments[:1000]；tool_result: content[:12000]+error_code+structured_data | sink.py:53-119（emit）；TeeSink 接线 agent_adapter.py:204-210 | 事件推送 → EventQueue → coordinator TaskLogger（见 A1） | 无 worker 侧留存 | （跨端，见 A1） |
| trajectory.csv（环境步） | Step,Actions,Successes,Observations,Coverage,TransportRate,Finished,MapRecall,Freshness,TimeoutAgents,RunID,MaxSteps,RemainingSteps,WallTimeSinceStart,StepDurationMs,ErrorTypes,CompletedSubtasksDelta,EndReason | barrier.py:566-585 step log → experiment.py poll 循环 drain_step_logs → logger.py:133-204 | CSV append；1 行/环境步 | run 目录保留 | 同 run/trajectory.csv（30 行） |
| snapshot_<task_id>.json | pinned,loaded_skills,messages(裁剪后完整列表) | context.py:337-377 save_snapshot（INPUT_REQUIRED 暂停时） | JSON overwrite；加载后删除 | 暂停恢复临时文件 | 样本 run 无（无 INPUT_REQUIRED 事件） |
| mailbox.ndjson / team_state.json | 邮件/团队状态 | worker.py:153-166（enable_peer_mail 时） | NDJSON/JSON | log_dir 保留 | 样本 run 无 peer_mail |
| mcp_<agent>.json | MCP 配置 | worker.py:223-227（write_worker_mcp_config） | JSON | log_dir 保留 | 同 run/workers/Alice/mcp_Alice.json |

## 2. 三问回答

### Q1 本组件是否有完整轨迹记录？（部分 + 证据）

Worker 侧**没有单文件"一步一轨迹"**，但三通道交叉可还原每步完整过程：
- LLM 输入输出全量：ndjson llm_request/llm_response（logger.py:86-181；实测单条消息最长 13700 字符未截断）。
- 工具调用全量：ndjson tool_result.result 完整（实测最长 1903 字符）+ csv Observation 完整（实测最长 1903，与 ndjson 一致——同一事件源双写）。
- 但**缺少工具开始事件**：AgentLogger 只有 llm_request/llm_response/tool_result 三类（logger.py 全文无 log_tool_start），工具执行起点与耗时只在 csv 的 ToolLatencyMs（worker.py:319-321 monotonic 计时）体现。
- 环境动作轨迹：trajectory.csv 每步 Actions/Observations/TimeoutAgents 全量（barrier.py:566-585）。

结论：有完整轨迹记录（部分缺 tool_start 事件与裁剪前原文，见 Q3/Gap#4、#2）。

### Q2 交互记录是否保存？（逐项）

- LLM 输入（messages 全文）：**保存但不完整**。ndjson llm_request 记录 messages 全文（logger.py:101-115 无截断），但记录的是 pre_llm 裁剪/组装后的 messages（agent.py:546 hooks.pre_llm → :549 log_request；hooks.py:85-86 prune_history+assemble）。csv LLMInput 为最近 6 条×每条 200 字符摘要（worker.py:287-292），无法还原完整 prompt。
- LLM 输出全文：**保存**。ndjson llm_response 记录 content+thinking+tool_calls 完整 schema+finish_reason+usage（logger.py:122-154；agent.py:600-606）；csv LLMOutput 记当前步 content 全文（worker.py:283）。实测样本 thinking 全空（该模型无思考流）。
- 工具调用（参数/结果）：**保存**。ndjson tool_result.arguments（redact_data 后）与 result 全文（logger.py:156-181）；csv ToolArgs JSON 全量（worker.py:326）+ Observation 全量（worker.py:328）。
- 消息收发（A2A 通道）：worker 侧 sink.py:53-119 推 [DATA] 块（content[:2000]/args[:1000]/content[:12000]），不落盘；原文已在 ndjson/csv 留存（Q6）。

### Q3 与本组件相关的状态是否逐步保存？（部分 + 证据）

- 环境/实验状态：逐步保存——trajectory.csv 每步 Coverage/TransportRate/Finished/TimeoutAgents（barrier.py:566-585）；token_usage.csv 每次 LLM 响应（worker.py:295-305）。
- Agent 内部上下文状态（ContextManager messages/episodic/pinned）：**不逐步保存**。episodic 只存内存（context.py:170, 830-847，max 20 条，每条为 `tool_name → status: content[:500] 截断文本` 摘要）；pinned 随 snapshot 保存（context.py:337-377，仅 INPUT_REQUIRED 暂停时）；被 prune 裁掉的消息原始内容**不落盘**（context.py:877-905 就地丢弃，episodic 摘要非原文）。
- 压缩摘要：_save_compression_summary 写 compression_summary.json（context.py:780-790），但 compress_with_llm（Phase 3）**全仓无调用点**（grep src/ sar_orch/ 仅 router_agent/context.py:843 注释提及），故该文件实际不生成（样本 run 确认不存在）。
- 还原性：单 run 内可通过**早期 llm_request 快照**还原被裁内容——每步 request 都记录了当时完整 messages，被裁消息在被裁前一步的快照中仍有全文；跨 run/session 恢复（snapshot 路径）只能拿到裁剪后状态，无法还原裁剪前历史。

## 3. 与 logging_map.md 对照（逐条）

| logging_map 条目 | 判定 | 证据 |
|---|---|---|
| 记录点 2a：worker.py:275-338 _on_step_event，csv 字段含 ErrorType（非 ErrorTypeByAgent） | 一致 | worker.py:275-337 实测；csv header 实测 `ErrorType` |
| 2a LLMInput=最近 6 条消息摘要 | 一致 | worker.py:287-292（msgs[-6:] 每条 c[:200]）；实测 LLMInput 最长 1246 字符 |
| 2a correlation id=`{agent}-tool-{seq}`（worker.py:312） | 一致 | worker.py:312 实测 |
| 记录点 4a：worker.py:297-305 token_usage | 一致 | worker.py:295-305 实测；csv 11 列实测一致 |
| 记录点 8a/8b/8c：AgentLogger log_request/log_response/log_tool_result（worker_agent/logger.py:86-121/122-155/156-181） | 一致 | 行号实测一致；事件类型与字段实测一致 |
| 记录点 10：A2AWorkerSink 不写盘、TeeSink 并行 | 一致 | sink.py:53-119；agent_adapter.py:204-210 实测 |
| §8 输出路径 `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson`（dashboard 模式） | 补充 | 实验模式下实际为 `workers/<Agent>/<worker_id>/<task_id>.ndjson`（experiment.py:448-460 + a2a_server.py:216），文档 §10 路径口径未列实验模式双层目录 |
| 记录点 7a：snapshot_<task_id>.json 保存完整 Message 列表 | 一致（含裁剪后语义） | context.py:337-377；保存的 messages 已是压缩后状态 |
| （文档未记）AgentLogger 无 tool_start 事件 | 补充 | logger.py 仅 3 个 log_* 方法 |
| （文档未记）空闲心跳 auto-noop 记录路径 | 补充 | worker.py:533-552 直接 submit_action，无 agent 侧记录；仅 trajectory Actions 可见 |
| （文档未记）compress_with_llm 无调用点、被裁内容不另存 | 补充 | grep 全仓；context.py:851-906、780-790 |

冲突 0 条；一致 7 条；补充 4 条。

## 4. Gap 清单

| # | Gap | 证据 file:line | 对"用轨迹探索框架优化"的影响 |
|---|---|---|---|
| 1 | csv LLMInput 为 6×200 字符摘要，无法从 csv 还原完整 prompt（须回退 ndjson） | worker.py:287-292 | 中：按 csv 做快速统计会低估输入上下文，完整还原必须 join ndjson |
| 2 | 被 ContextManager 裁掉的原始消息不另存（episodic 仅内存、compression_summary.json 无写入路径），跨 run 无法还原裁剪前历史；单 run 内需靠早期 llm_request 快照拼接 | context.py:851-906, 780-790, 843-847；hooks.py:85-86 | 高：若分析"压缩前后决策差异"或恢复完整历史，现轨迹缺原文（当前 run 未触发压缩，风险是潜伏的） |
| 3 | auto-noop 无显式来源标记：空闲心跳（worker.py:533-552）与超时注入（barrier.py:243-247）只在 trajectory Actions 出现"NoOp"；仅超时注入有 TimeoutAgents 列；LLM 主动 no_op 有 csv/ndjson 记录。区分需差值推导 | worker.py:533-552；barrier.py:243-247；trajectory.csv 实测 15 个 NoOp=8 LLM 主动+7 系统侧 | 中：no-op 行为分析（主动等待 vs 系统补位）需交叉三处数据，无现成标记 |
| 4 | AgentLogger 无 tool_start 事件：工具执行起点、参数仅 csv 侧有（ToolLatencyMs），ndjson 无法独立重建工具时序 | logger.py 全文；worker.py:306-313, 319-321 | 低-中：时序/延迟分析只能依赖 csv，且 csv 缺"调用开始"时间戳列（只有耗时） |
| 5 | token_usage 只覆盖成功收到 usage 的响应（实测 311 response=311 行）；LLM 异常/NeedInput 路径无 token 记录 | agent.py:557-572（异常直接 return），752-790（NeedInput return）；实测 323 request vs 311 usage | 低：token 核算偏小，但方向可知 |
| 6 | worker prompt 无内容指纹/版本演进记录（prompt_version 恒 "baseline"） | metadata.json 实测；sar_orch/prompts/worker/system.md | 低-中：prompt 版本与行为差异无法在轨迹上归因 |
| 7 | A2A [DATA] 块截断（2000/1000/12000）在 worker 侧无完整镜像文件（完整版在 ndjson/csv，跨端链路见 A1） | sink.py:59, 83, 105 | 低：worker 侧原文已存，不影响本侧还原 |

## 5. 初步观察：现有轨迹能支撑哪些框架优化分析

1. **每步 LLM 输入重建**：llm_request 快照（含 usage）→ 输入上下文规模、token 预算占用、缓存命中率的时间曲线（单 run 内全量可还原）。
2. **工具行为归因**：csv ToolName/Action/Observation/Success/ErrorType + ToolLatencyMs → 工具成功率、延迟、错误类型分布；配合 map_agent 工具名可做"工具选型质量"分析。
3. **no-op 语义分层**：LLM 主动 no_op（csv/ndjson 有记录）vs 超时注入（TimeoutAgents 列）vs 空闲心跳（差值推导）→ 可分析 agent 主动等待 vs 被动补位的占比（当前需脚本差值）。
4. **LLM 输出质量**：ndjson content/thinking/tool_calls 全文 → 思维链（若有）、工具参数合法性、finish_reason 分布。
5. **上下文管理效果**：prune_history 触发点（messages 长度/占用突变）与 llm_request 快照对比 → 裁剪策略对输入构成的影响（当前 run 未触发压缩，需更长/更复杂任务样本）。
6. **上报链路还原**：report_observation 的 ToolArgs/Observation 对（实测完整 JSON）→ 可校验 agent 上报内容与 coordinator 语义地图的一致性（跨 A1/A4）。
