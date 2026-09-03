---
日期: 2026-07-26（2026-09-03 校准对齐代码现状）
文档类型: 技术文档
文档概述: LLaMAR 项目完整日志系统映射表 — 每个记录点的触发条件、记录内容、输出文件路径
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）；核对口径：类/函数名 grep -n，行号以当前工作区实测为准。
---

# 日志系统映射表

> 路径口径：SAR 实验的 `<log_dir>` = `sar_orch/results/{YYYYMMDD_HHMMSS}_s{scene}_s{seed}_a{agents}/`（experiment.py:47,440–442），其下分 `coordinator/`、`workers/<AgentName>/`、`supervision/` 子目录（448–459 行）。standalone CLI 未传 log dir 时，AgentLogger 默认落 `<cwd 上级>/logs/agent/`（worker_agent/logger.py:43），TaskLogger 默认落 `logs/`（server.py:408 `TaskLogger(base_dir=... or "logs")`）。

## 输出文件清单

| 文件 | 写入方式 | 来源 |
|------|---------|------|
| `<log_dir>/trajectory.csv` | CSV append | `ExperimentLogger.log_step` |
| `<log_dir>/agent_interactions.csv` | CSV append | `ExperimentLogger.log_agent_interaction` / `log_coordinator_state` |
| `<log_dir>/router_interactions.csv` | CSV append | `ExperimentLogger.log_router_interaction` |
| `<log_dir>/token_usage.csv` | CSV append | `ExperimentLogger.log_token_usage` |
| `<log_dir>/summary.csv` | CSV overwrite | `ExperimentLogger._write_summary` |
| `<log_dir>/metadata.json` | JSON overwrite | `ExperimentLogger.write_metadata` |
| `<log_dir>/events.ndjson` | NDJSON append | `ExperimentLogger.log_event` |
| `<log_dir>/subtasks.csv` | CSV append | `ExperimentLogger.log_subtask` |
| `<log_dir>/run_metrics.json` | JSON overwrite | `experiment.py`（run 内 995–997 行先写，`main()` 1220–1224 行重写） |
| `<coordinator_log_dir>/snapshot_<task_id>.json` | JSON write/delete | `ContextManager.save_snapshot/load_snapshot` (INPUT_REQUIRED 暂停恢复) |
| `<coordinator_log_dir>/semantic_map.jsonl` | NDJSON append | `SemanticMapStore.set_jsonl_path`（coordinator.py:459–461，Coordinator 侧语义地图日志） |
| `<coordinator_log_dir>/long_term/long_term.sqlite3` | SQLite | `LongTermMemoryStore`（contracts.py:847；coordinator.py:538–546 以 `memory_root=log_dir` 实例化，仅 `--long-term-mode != off`） |
| `<coordinator_log_dir>/diagnosis/diagnosis.sqlite3` | SQLite | `DiagnosisMemoryStore`（contracts.py:883；coordinator.py:583–585，fail-closed） |
| `<truth_output_dir>/truth_trace.jsonl` / `truth_manifest.json` | JSONL/JSON append+finalize | `sar_orch/eval/truth_recorder.py`（Phase 5 evaluator-private，@1721713 2026-08-08；输出目录必须在 run results 外，experiment.py:476–485 强制校验） |
| `logs/agent/sar_coordinator/<task_id>.ndjson` | NDJSON append | `AgentLogger`（Coordinator RouterAgent；dashboard 模式路径 @launch_dashboard.py:46） |
| `logs/<task_id>.ndjson` | NDJSON append | `TaskLogger` |
| `logs/agent/sar_coordinator/events_<task_id>.ndjson` | NDJSON append | `EventStore` |
| `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson` | NDJSON append | `AgentLogger`（Worker Agent；dashboard 模式路径 @launch_dashboard.py:47） |
| stdout/stderr | Python logging | 所有 `logger.*` 调用 |

### Benchmark 额外文件

| 文件 | 写入方式 | 来源 |
|------|---------|------|
| results/benchmark/scene_{S}/agents_{A}/seed_{N}/meta.json | JSON overwrite | `benchmark.py:286`（仅 scene/agents/seed 三字段） |
| .../result.json | JSON overwrite | `benchmark.py:357 / 414` |
| `.../stdout.log` | Bytes write | `benchmark.py:376 / 456`（子进程） |
| `.../stderr.log` | Bytes write | `benchmark.py:341 / 378 / 458`（子进程） |
| `.../error.log` | Text write | `benchmark.py:474`（异常） |
| results/benchmark/progress.json | JSON overwrite | `benchmark.py:67-75`（状态变更，原子写 tmp→rename） |
| results/benchmark/index.json | JSON overwrite | `benchmark.py:738`（全部结束；字段 scene/agents/seed/status/elapsed/error/log_dir） |
| `results/benchmark_aggregated.tsv` | TSV overwrite | `aggregate.py:19,134-139`（15 列，见 experiment_design.md §3.4） |

---

## 1. 实验轨迹 — trajectory.csv

### 记录点: `sar_orch/experiment.py:808-817`（poll 循环）→ `sar_orch/logger.py:133-204` (log_step)
- **触发条件**: poll 循环调用 `barrier.drain_step_logs()` 取出上次轮询以来完成的**全部** step（不再只看最新一步，避免步进快于轮询间隔时中间 step 被静默丢弃），逐条写入
- **字段**: `Step`, `Actions`, `Successes`, `Observations`, `Coverage`, `TransportRate`, `Finished`, MapRecall, `Freshness`, `TimeoutAgents`, `RunID`, `MaxSteps`, `RemainingSteps`, `WallTimeSinceStart`, `StepDurationMs`, `ErrorTypes`, `CompletedSubtasksDelta`, `EndReason`（写行 logger.py:177–196，header :592–611；列名是 **`ErrorTypes`**，per-agent 列表，非 `ErrorTypeByAgent`）
- **EndReason 终态回填**: `set_end_reason`（logger.py:206–245）把终态值回填到全部缓存行并整体重写 trajectory.csv
- **输出**: `<log_dir>/trajectory.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

---

## 2. Agent 交互 — agent_interactions.csv

### 记录点 2a: `sar_orch/worker.py:275-338` (`_on_step_event` → tool_result 分支，`log_agent_interaction` 调用在 322–345 行)
- **触发条件**: Worker Agent 工具执行完，有 `_pending_tool` 数据
- **字段**: `Step`, `Agent`, `ToolName`, `ToolArgs`(JSON), `Action`(SAR 语义), `Observation`, `LLMInput`(最近6条消息摘要), `LLMOutput`, `Thinking`, `RunID`, `CorrelationID`, `EventType`, `ToolLatencyMs`, `Success`, `ErrorType`（写行 logger.py:292–307，header :612–628）
- **correlation id 生成**: worker.py:312（`{agent}-tool-{seq}`，tool_start 分支）
- **输出**: `<log_dir>/agent_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

### 记录点 2b: `sar_orch/coordinator.py:438-446` (`_on_router_event` → query_sar_state tool_result)
- **触发条件**: Coordinator 收到 `query_sar_state` 的 `tool_result` 事件
- **字段**: `Agent="Coordinator"`, `ToolName="query_sar_state"`, `ToolArgs`=[state_summary[:2000]], `RunID`, `CorrelationID`, `EventType="query_sar_state"`（logger.py `log_coordinator_state` :316–357）
- **输出**: 同上 `agent_interactions.csv`

---

## 3. 路由调度 — router_interactions.csv

### 记录点: `sar_orch/coordinator.py:369-446` (`_on_router_event` → tool_start 分发)
- **触发条件**: Coordinator 开始调用 `send_message(message_type="assign_task"/"reply_to_help"/"cancel_task")`（三者共用 `_log_send_message`，`:154-272`）/ `send_message(message_type="activate_plan_node")`（走 DAG 激活路径，不经 `_log_send_message`）/ `update_plan`（`:391-396`）/ `query_sar_state`（`:405-417`, 仅 oracle 模式）/ `query_task_events`（`:419-425`）/ `finish_task`（`:426-432`）
- **字段**: `Step`, `Subtask`(工具描述+参数), `AssignedTo`(目标 agent 或 "Coordinator"), `RunID`, `CorrelationID`, `WorkerTaskID`, `EventType`, `Success`, `ErrorType`（写行 logger.py:465–474，header :629–639）
- **correlation id 生成**: coordinator.py:171（`coordinator-dispatch-{seq}`）
- **输出**: `<log_dir>/router_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

> 注：`SendMessageTool` 实际支持 `assign_task`/`reply_to_help`/`cancel_task`/`activate_plan_node` 四种 `message_type`（`src/a2a/builtin_tools/send_message.py:74-81`），比早期文档描述的三合一多了 `activate_plan_node`（DAG 节点激活，读取 MissionGraph 声明的 participants/assignments，不依赖 LLM 传入的 `who`/`content`）。

---

## 4. Token 用量 — token_usage.csv

### 记录点 4a: `sar_orch/worker.py:297-305` (`_on_step_event` → llm_response 分支)
- **触发条件**: Worker Agent 每次收到 LLM 响应（含 usage）
- **字段**: `Step`, `Agent`(=agent_name), `PromptTokens`, `CompletionTokens`, `TotalTokens`, `CacheHitTokens`, `CacheMissTokens`, `RunID`, `LLMLatencyMs`, `Model`, `PromptVersion`（写行 logger.py:515–526，header :640–652；`LLMLatencyMs`/`Model`/`PromptVersion` 缺省时由 logger 的 default 兜底）
- **输出**: `<log_dir>/token_usage.csv`

### 记录点 4b: `sar_orch/coordinator.py:374-385` (`_on_router_event` → llm_response)
- **触发条件**: Coordinator RouterAgent 每次收到 LLM 响应（含 usage）
- **字段**: `Agent="Coordinator"`, 其余同上

### 记录点 4c: 语义地图辅助模型
- `coordinator.py:498`（MapSummarizer）与 `:752`（MapAgent）也经 `log_token_usage` 落同一文件，`Agent` 字段区分来源。

### 缓存字段来源
| 提供方 | `CacheHitTokens` | `CacheMissTokens` |
|--------|-----------------|-------------------|
| DeepSeek (OpenAI 协议) | `response.usage.prompt_cache_hit_tokens` | `response.usage.prompt_cache_miss_tokens` |
| OpenAI | `response.usage.prompt_tokens_details.cached_tokens` | `prompt_tokens - cached_tokens` |
| Anthropic | `response.usage.cache_read_input_tokens` | `response.usage.input_tokens + response.usage.cache_creation_input_tokens` |

保证 `CacheHitTokens + CacheMissTokens == PromptTokens`。

---

## 5. 实验汇总 — summary.csv

### 记录点: `sar_orch/logger.py:677-727` (_write_summary)
- **触发条件**: `flush_summary()` 每步 poll 调用，或 `close()` 时
- **字段**: `ExperimentName`, `LogDir`, `TotalSteps`, `FinalCoverage`, `FinalTransportRate`, `Finished`, `TotalAgentInteractions`, `TotalRouterInteractions`, `{Agent}PromptTokens`, `{Agent}CompletionTokens`, `{Agent}TotalTokens`, `{Agent}CacheHitTokens`, `{Agent}CacheMissTokens`
- **输出**: `<log_dir>/summary.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), **覆盖写入**（w 模式）

### 内存累计源
- `log_step()` 更新: `_step_count`, `_last_coverage`, `_last_transport_rate`, `_finished`（`logger.py:201-204`）
- `log_token_usage()` 更新: `_token_accumulator[agent]`（`logger.py:532-546`）

---

## 5a. 子任务跟踪 — subtasks.csv

### 记录点: `sar_orch/logger.py:406-430` (log_subtask)
- **触发条件**: Coordinator 通过 `send_message(message_type="assign_task")` 派发新任务
- **字段**: `RunID`, `Step`, `SubtaskID`, `Status`, `AssignedTo`, `Subtask`, `CreatedAt`, `UpdatedAt`, `FailureClass`, `Details`（header :653–664）
- **输出**: `<log_dir>/subtasks.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush
- **调用来源**: `sar_orch/coordinator.py:181` (`_log_send_message` 内，仅 `assign_task` 分支)

---

## 5b. 事件流 NDJSON — events.ndjson

### 记录点: `sar_orch/logger.py:377-401` (log_event)
- **触发条件**: Coordinator 通过 `send_message` 派发/回复/取消任务等语义事件
- **字段**: 统一 schema `timestamp`, `event_type`, `run_id`, `payload`（+ 附加 kwargs 如 step/agent）
- **输出**: `<log_dir>/events.ndjson`
- **写入**: NDJSON append
- **调用来源**: `sar_orch/coordinator.py` `_log_send_message` 全函数体（:154–272）内 5 处 `log_event` 调用（:188/:215/:240/:254/:266）

---

## 5c. 实验元数据 — metadata.json

### 记录点: `sar_orch/logger.py:362-371` (write_metadata)
- **触发条件**: 实验启动时（experiment.py:499–517 构造并写入）
- **内容**: 由 `experiment.py:68-103` `build_run_metadata` 构造 — `run_id`, `env_name`, `scenario_id`, `scene`, `seed`, `agent_count`, `model`, `provider`, `api_base`, `max_steps`, `wall_clock_timeout`, `sandbox_profile`, `task_objective`, `success_criteria`, `coordinator_prompts`, `worker_prompts`, `prompt_version`, `code_commit`
- **输出**: `<log_dir>/metadata.json`
- **写入**: JSON overwrite（sort_keys）

---

## 5d. 长期记忆与诊断持久化（2026-08 新增）

### long_term.sqlite3
- **位置**: `<coordinator_log_dir>/long_term/long_term.sqlite3`（contracts.py:847 `long_term_db_path`）
- **触发条件**: `--long-term-mode != off` 时 coordinator.py:538–546 以 `memory_root=Path(log_dir)` 实例化 `LongTermMemoryStore`，`open()` 内部 `sqlite3.connect`
- **性质**: coordinator Python 代码直接写盘，不经 LLM 工具通道，不受 SandboxPolicy 约束（见 sandbox.md Non-goals 延伸）

### diagnosis.sqlite3
- **位置**: `<coordinator_log_dir>/diagnosis/diagnosis.sqlite3`（contracts.py:883）
- **触发条件**: 同上随 long_term_mode 接线（coordinator.py:553–599，fail-closed：打不开仅禁用通道，不阻塞启动）；rolling 反思每 `every_env_step=5` 步触发诊断循环（`_run_diagnosis_channel` @long_term_reflection.py:310/320，节流 `min_interval_sec=30` @contracts.py:955）

### truth trace / truth manifest（evaluator-private）
- **位置**: `--truth-output-dir` 指定的目录（必须在 run results 目录之外，experiment.py:476–485 运行期强制校验）；文件名 `truth_trace.jsonl` / `truth_manifest.json`（truth_recorder.py:56–57）
- **触发条件**: Phase 5 truth recorder（@1721713，2026-08-08）。run 终态时 `_finalize_truth_recorder`（experiment.py:130–170）冻结 manifest；terminal-only 评测器 `memory_projection_quality` 经 `--truth-manifest` 读回（experiment.py:241–254）
- **约束**: legacy memory 模式或无法解析 canonical scope 时静默跳过（返回 None，不失败 run）

---

## 6. 实验结果 JSON — run_metrics.json

### 记录点: `sar_orch/experiment.py:995-997`（run 内先写）与 `main()` 内 `1220-1224`（结束时重写）
- **触发条件**: 实验结束（`run_experiment()` 返回后，`main()` 把 metrics 落盘）；run 内先写一次供验收评测器读取非空 coverage/transport_rate
- **字段**: `finished`, `steps`, `coverage`, `transport_rate`, `elapsed_seconds`, `log_dir`, `end_reason`, `run_id`, `max_steps` + `barrier.get_metrics()`
- **输出**: `<log_dir>/run_metrics.json`
- **写入**: `json.dump` (覆盖)

---

## 7. ContextManager Snapshot — snapshot_{task_id}.json

### 记录点 7a: `src/Agent/worker_agent/context.py:337-377` / `src/Agent/router_agent/context.py:318-358` (save_snapshot)
- **触发条件**: Worker 进入 `INPUT_REQUIRED` 暂停状态时，`AgentController.submit()` 对当前 `task_id` 保存消息历史
- **字段**: `pinned` (typed pinned state 或 dict), `loaded_skills` (已加载的技能), `messages` (完整 Message 列表)
- **输出**: `<log_dir>/snapshot_<task_id>.json`
- **写入**: JSON overwrite，同时保存内存副本 `_task_snapshots[task_id]`

### 记录点 7b: `src/Agent/worker_agent/context.py:378-413` / `src/Agent/router_agent/context.py:359-409` (load_snapshot)
- **触发条件**: Worker 收到 Coordinator 回复后按同一 `task_id` 恢复任务
- **读取顺序**: 内存 snapshot 优先，磁盘 snapshot 作为回退
- **清理**: 磁盘文件加载后删除

---

## 8. Agent 运行日志 NDJSON — `{task_id}.ndjson`

AgentLogger 统一以 NDJSON 格式输出 Agent 运行日志（按 task_id 分文件，logger.py `_ensure_file_open`）。

### 记录点 8a: `src/Agent/router_agent/logger.py:93-129` / `src/Agent/worker_agent/logger.py:86-121` (log_request)
- **触发条件**: 每次 LLM 调用前
- **事件**: `event="llm_request"`
- **字段**: `ts`(ISO UTC), `task_id`, `context_id`, `event`, `messages`(完整消息列表), `tools`(工具名列表), `step_index`

### 记录点 8b: `src/Agent/router_agent/logger.py:130-163` / `src/Agent/worker_agent/logger.py:122-155` (log_response)
- **触发条件**: 每次收到 LLM 响应后
- **事件**: `event="llm_response"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `content`, `thinking`, `tool_calls`(完整 schema), `finish_reason`, `usage`(prompt/completion/total/cache_hit/cache_miss tokens)

### 记录点 8c: `src/Agent/router_agent/logger.py:164-189` / `src/Agent/worker_agent/logger.py:156-181` (log_tool_result)
- **触发条件**: 每次工具执行后
- **事件**: `event="tool_result"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `tool_name`, `arguments`, `success`, `result`(成功时), `error`(失败时)

- **输出**: SAR/dashboard 模式为 `logs/agent/sar_coordinator/<task_id>.ndjson` 或 `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson`（launch_dashboard.py:46–47）；实验模式下 log_dir 注入为 run 内各 agent 子目录；未注入时默认 `<cwd 上级>/logs/agent/`（worker_agent/logger.py:43）
- **写入**: NDJSON append（文件句柄 `open(a)` → 追加 + flush，按 task_id 分文件）

---

## 9. TaskLogger NDJSON — {task_id}.ndjson

### 记录点 9a: `src/a2a/coordinator/task_logger.py:60-77` (init_task)
- **触发条件**: 新任务开始
- **内容**: `{"event":"meta", "task_id":..., "friendly_name":...}`

### 记录点 9b: `src/a2a/coordinator/task_logger.py:109-186` (log_event)
- **触发条件**: executor/router/worker/verifier 调用 `log_event()`
- **事件类型**: `raw_request`, `task_start`, `agentic_start`, `task_error`, `task_final`, `done`, `plan`, `layer_start`, `task_complete`, `replan`, `verify`, `llm_response`, `tool_result` 等
- **字段**: `timestamp`(ISO), `event`, `source`, `data`(自动截断: `content`→2000 (llm_response), `content`→3000 (tool_result), `tool_arguments`→1000)
- **输出**: `<base_dir>/<task_id>.ndjson`（SAR 实验 base_dir=coordinator log_dir；standalone 默认 `logs/`，server.py:408）
- **写入**: NDJSON append
- **约束**: 文件超 10MB 时记录 warning 后跳过；OSError 静默降级不抛异常

---

## 10. A2AWorkerSink — EventQueue 推送

`src/a2a/worker/sink.py` 的 `A2AWorkerSink` 将 Worker Agent step 事件实时推入 A2A EventQueue（→ Coordinator 侧 TaskLogger），**不写入磁盘文件**。它由 `AgentAdapter.execute()`（`src/a2a/worker/agent_adapter.py:204`）构造，与 `sar_orch/worker.py:275-358` 的 `_on_step_event`（负责 CSV/NDJSON 落盘）通过 `TeeSink`（`agent_adapter.py:206-210`）并行接收同一份 step 事件——两者是互补的两条通路，不是互斥/替代关系。

### 记录点: `src/a2a/worker/sink.py:53-119` (emit)
- **触发条件**: Worker Agent 的 step_callback 事件（由 `TeeSink` 分发）
- **推送内容**: `[LLM]` / `[Tool]` / `[Result]` 文本 + `[DATA]` JSON 块（含 content[:2000]、tool_name、arguments、error_code、structured_data 等）
- **流向**: `EventQueue` → `TaskStatusUpdateEvent` → A2A push → Coordinator (TaskLogger)
- **持久化**: ❌ 不写文件（CSV/NDJSON 落盘由 `sar_orch/worker.py` 的 `_on_step_event` 分支 + `AgentLogger` 承担）

> push callback admission 时序（@a0d6712，2026-08-09）：worker 侧 A2A server 先返回 task id、后准入 push callback 注册（`src/a2a/worker/a2a_server.py`），避免 Coordinator 在拿到 task_id 前推送事件被丢弃；对日志的影响是 TaskLogger/EventStore 首条事件不早于 task 创建响应。

---

## 11. EventStore NDJSON — events_{task_id}.ndjson

### 记录点: `src/a2a/coordinator/event_store.py:75-127` (append)
- **触发条件**: `/a2a/push-callback` 收到 Worker 推送；以及 task_watchdog 监督事件（`TASK_STALE` 等经 `supervision_event` 类型写入，task_watchdog.py:379–383）
- **字段**: `ts`, `task_id`, `event_type`, `state`, `text`[:500]

| event_type | 触发条件 |
|-----------|---------|
| `status_update` | push-callback 收到 TASK_STATE_* 变更 |
| `artifact_update` | push-callback 收到 artifact 推送 |
| `help_request` | push-callback 收到 INPUT_REQUIRED |
| `supervision_event` | watchdog 监督告警（state=`TASK_STALE` 等） |

- **输出**: `<coordinator_log_dir>/events_<task_id>.ndjson`
- **写入**: `open(a)` → NDJSON append；写失败仅 warning（event_store.py:126–127）
- **约束**: 每个 task_id 最多保留 500 条记录（超限丢弃最旧）
- **定位**: Phase 5 起 EventStore 是 *legacy debug adapter*（文件头注释 :8），正式事件面在 `events.ndjson` 与 supervision 目录

---

## 12. SSE 实时推送

Coordinator (`src/a2a/coordinator/server.py`) 实际提供 4 个 SSE 端点（均以 `media_type="text/event-stream"` 返回）：

| 端点 | 用途 | 数据来源 |
|------|------|---------|
| `/logs/{task_id}/stream` (`:1430`) | 尾随读取任务 NDJSON 日志，逐行推送 | TaskLogger NDJSON 文件轮询 |
| `/map/state` (`:2234`) | 实时推送 SAR 网格地图状态 | `SARBarrier` 当前 step 快照 |
| `/dashboard/stream` (`:2287`) | 统一仪表盘数据流 | 汇总 barrier/semantic map/token 等状态 |
| `/api/a2a/jsonrpc` (`:2379`, POST) | 把 JSON-RPC 请求流式代理到内部 A2A Server，支持 SSE | `httpx` 流式转发上游响应 |

`/ui/debug`（`:1504`）本身不是 SSE 端点，而是消费 `/logs/{task_id}/stream` 的调试查看器页面。

---

## 13. Python Logging（logger.*）

所有 `logger.info/warning/error/exception` 通过 Python logging 模块输出，由顶层配置决定目标：

| 配置入口 | 输出目标 |
|---------|---------|
| `sar_orch/experiment.py:23-26` | stdout/stderr（StreamHandler） |
| `sar_orch/benchmark.py:26-29` | stdout/stderr（StreamHandler） |
| `src/a2a/coordinator/cli.py:87` | stdout/stderr（basicConfig） |
| Worker uvicorn | stdout/stderr（uvicorn logger） |

benchmark 模式下，stdout/stderr 被重定向到 `stdout.log` / `stderr.log`。

### 主要 Logger 来源

| 模块 | 记录内容 |
|------|---------|
| `sar_orch/experiment.py` | 实验起止、步骤进度、异常 |
| `sar_orch/coordinator.py` | 服务启动、任务提交失败 |
| `sar_orch/worker.py` | session 清理警告 |
| `sar_orch/benchmark.py` | 进度、超时、异常 |
| `a2a/coordinator/server.py` | Worker 注册/断开、心跳 |
| `a2a/coordinator/agent_executor.py` | 编排超限、异常 |
| `a2a/coordinator/router.py` | 工具加载失败 |
| `a2a/coordinator/event_store.py` | NDJSON 写入失败 |
| `a2a/coordinator/task_logger.py` | 文件超 10MB 跳过 |
| `a2a/coordinator/task_watchdog.py` | TASK_STALE 监督告警 |
| `a2a/worker/agent_adapter.py` | 任务入口、暂停、恢复 |
| `Agent/worker_agent/agent.py` | step_callback 异常 |
| `Agent/controller/controller.py` | 引擎兼容性警告 |

---

## 数据流 vs 日志覆盖一览

```
外部 Client → A2A SendMessage
  │                                      ├── TaskLogger: raw_request
  ▼
CoordinatorAgentExecutor.execute()
  │                                      ├── TaskLogger: task_start
  │                                      ├── TaskLogger: agentic_start
  ▼
AgentController.submit()
  │                                      ├── AgentLogger NDJSON: {task_id}.ndjson
  ▼
RouterAgent ReAct Loop
  ├─ query_sar_state(barrier)            ├── router_interactions.csv
  │                                      ├── agent_interactions.csv (Coordinator)
  │                                      ├── A2ACoordinatorSink → TaskLogger
  ├─ dispatch_task(agent, prompt)        ├── router_interactions.csv
  │                                      ├── subtasks.csv / events.ndjson
  │                                      ├── A2ACoordinatorSink → TaskLogger
  │   └─ send_task_async → Worker
  │        └─ AgentAdapter.execute()     ├── Python logging: [ENTRY]
  │           └─ AgentController.submit() ├── AgentLogger NDJSON: {task_id}.ndjson
  │              └─ Worker Agent ReAct
  │                 ├─ SAR tool           ├── agent_interactions.csv
  │                 │                    ├── TaskLogger: tool_result
  │                 ├─ ask_coordinator    ├── Python logging: [PAUSE]
  │                 │                    ├── TaskLogger: tool_result
  │                 │                    ├── EventStore NDJSON: events_{tid}.ndjson
  │                 └─ finish_task        ├── TaskLogger: task_complete
  ├─ query_task_events()                 ├── router_interactions.csv
  │                                      ├── TaskLogger: task_status / task_complete / help_request
  ├─ respond_worker(tid, response)       ├── router_interactions.csv
  │   └─ A2A send_message → Worker       ├── EventStore NDJSON
  │      └─ AgentAdapter.execute()       ├── Python logging: [RESUME]
  │         └─ initial_messages=snapshot ├── AgentLogger
  └─ finish_task()                       ├── router_interactions.csv

每步结尾:
  └─ experiment.py poll loop             ├── trajectory.csv
                                         ├── summary.csv
                                         ├── token_usage.csv
                                         ├── run_metrics.json
                                         └── (long_term!=off) rolling 反思 →
                                             long_term.sqlite3 / diagnosis.sqlite3
```
