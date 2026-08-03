---
日期: 2026-07-26
文档类型: 技术文档
文档概述: LLaMAR 项目完整日志系统映射表 — 每个记录点的触发条件、记录内容、输出文件路径
---

# 日志系统映射表

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
| `<log_dir>/run_metrics.json` | JSON overwrite | `experiment.py` main |
| `<log_dir>/snapshot_<task_id>.json` | JSON write/delete | `ContextManager.save_snapshot/load_snapshot` (INPUT_REQUIRED 暂停恢复) |
| `<log_dir>/semantic_map.jsonl` | NDJSON append | `SemanticMapStore.set_jsonl_path` (Coordinator 侧语义地图日志) |
| `logs/agent/sar_coordinator/<task_id>.ndjson` | NDJSON append | `AgentLogger` (Coordinator RouterAgent) |
| `logs/<task_id>.ndjson` | NDJSON append | `TaskLogger` |
| `logs/agent/sar_coordinator/events_<task_id>.ndjson` | NDJSON append | `EventStore` |
| `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson` | NDJSON append | `AgentLogger` (Worker Agent) |
| stdout/stderr | Python logging | 所有 `logger.*` 调用 |

### Benchmark 额外文件

| 文件 | 写入方式 | 来源 |
|------|---------|------|
| results/benchmark/.../meta.json | JSON overwrite | `benchmark.py` |
| results/benchmark/.../result.json | JSON overwrite | `benchmark.py` |
| `results/benchmark/.../stdout.log` | Bytes write | `benchmark.py` (子进程) |
| `results/benchmark/.../stderr.log` | Bytes write | `benchmark.py` (子进程) |
| `results/benchmark/.../error.log` | Text write | `benchmark.py` (异常) |
| `results/benchmark/.../summary.csv` | File copy | `benchmark.py` (从实验拷贝) |
| results/benchmark/progress.json | JSON overwrite | `benchmark.py` (状态变更) |
| results/benchmark/index.json | JSON overwrite | `benchmark.py` (全部结束) |
| `results/benchmark_aggregated.tsv` | TSV overwrite | `aggregate.py` |

---

## 1. 实验轨迹 — trajectory.csv

### 记录点: `sar_orch/experiment.py:396-444`
- **触发条件**: poll 循环调用 `barrier.drain_step_logs()` 取出上次轮询以来完成的**全部** step（不再只看最新一步，避免步进快于轮询间隔时中间 step 被静默丢弃），逐条写入
- **字段**: `Step`, `Actions`, `Successes`, `Observations`, `Coverage`, `TransportRate`, `Finished`, MapRecall, `Freshness`, `TimeoutAgents`, `RunID`, `MaxSteps`, `RemainingSteps`, `WallTimeSinceStart`, `StepDurationMs`, `ErrorTypes`, `CompletedSubtasksDelta`, `EndReason`
- **输出**: `<log_dir>/trajectory.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

---

## 2. Agent 交互 — agent_interactions.csv

### 记录点 2a: `sar_orch/worker.py:351-405` (_step_callback → tool_result)
- **触发条件**: Worker Agent 工具执行完，有 `_pending_tool` 数据
- **字段**: `Step`, `Agent`, `ToolName`, `ToolArgs`(JSON), `Action`(SAR 语义), `Observation`, `LLMInput`(最近6条消息摘要), `LLMOutput`, `Thinking`, `RunID`, `CorrelationID`, `EventType`, `ToolLatencyMs`, `ErrorType`
- **输出**: `<log_dir>/agent_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

### 记录点 2b: `sar_orch/coordinator.py:325-334` (_router_cb → tool_result + query_sar_state)
- **触发条件**: Coordinator 收到 `query_sar_state` 的 `tool_result` 事件
- **字段**: `Agent="Coordinator"`, `ToolName="query_sar_state"`, `ToolArgs`=[state_summary[:2000]], `RunID`, `CorrelationID`, `EventType`, `ToolLatencyMs`, `ErrorType`
- **输出**: 同上 `agent_interactions.csv`

---

## 3. 路由调度 — router_interactions.csv

### 记录点: `sar_orch/coordinator.py:285-333` (_router_cb → tool_start, router 工具)
- **触发条件**: Coordinator 开始调用 `send_message(message_type="assign_task"/"reply_to_help"/"cancel_task")`（三者共用 `_log_send_message`，`:114-179`）/ `send_message(message_type="activate_plan_node")`（走 DAG 激活路径，不经 `_log_send_message`）/ `query_task_events`（`:320-326`）/ `finish_task`（`:327-333`）
- **字段**: `Step`, `Subtask`(工具描述+参数), `AssignedTo`(目标 agent 或 "Coordinator"), `RunID`, `CorrelationID`, `WorkerTaskID`, `EventType`
- **输出**: `<log_dir>/router_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

> 注：`SendMessageTool` 实际支持 `assign_task`/`reply_to_help`/`cancel_task`/`activate_plan_node` 四种 `message_type`（`src/a2a/builtin_tools/send_message.py:74-79`），比早期文档描述的三合一多了 `activate_plan_node`（DAG 节点激活，读取 MissionGraph 声明的 participants/assignments，不依赖 LLM 传入的 `who`/`content`）。

---

## 4. Token 用量 — token_usage.csv

### 记录点 4a: `sar_orch/worker.py:352-374` (_step_callback → llm_response)
- **触发条件**: Worker Agent 每次收到 LLM 响应（含 usage）
- **字段**: `Step`, `Agent`(=agent_name), `PromptTokens`, `CompletionTokens`, `TotalTokens`, `CacheHitTokens`, `CacheMissTokens`, `RunID`, `LLMLatencyMs`, `Model`, `PromptVersion`
- **输出**: `<log_dir>/token_usage.csv`

### 记录点 4b: `sar_orch/coordinator.py:280-291` (_router_cb → llm_response)
- **触发条件**: Coordinator RouterAgent 每次收到 LLM 响应（含 usage）
- **字段**: `Agent="Coordinator"`, 其余同上（含 `RunID`, `LLMLatencyMs`, `Model`, `PromptVersion`）

### 缓存字段来源
| 提供方 | `CacheHitTokens` | `CacheMissTokens` |
|--------|-----------------|-------------------|
| DeepSeek (OpenAI 协议) | `response.usage.prompt_cache_hit_tokens` | `response.usage.prompt_cache_miss_tokens` |
| OpenAI | `response.usage.prompt_tokens_details.cached_tokens` | `prompt_tokens - cached_tokens` |
| Anthropic | `response.usage.cache_read_input_tokens` | `response.usage.input_tokens + response.usage.cache_creation_input_tokens` |

保证 `CacheHitTokens + CacheMissTokens == PromptTokens`。

---

## 5. 实验汇总 — summary.csv

### 记录点: `sar_orch/logger.py:636-686` (_write_summary，代码行号已核实)
- **触发条件**: `flush_summary()` 每步 poll 调用，或 `close()` 时
- **字段**: `ExperimentName`, `LogDir`, `TotalSteps`, `FinalCoverage`, `FinalTransportRate`, `Finished`, `TotalAgentInteractions`, `TotalRouterInteractions`, `{Agent}PromptTokens`, `{Agent}CompletionTokens`, `{Agent}TotalTokens`, `{Agent}CacheHitTokens`, `{Agent}CacheMissTokens`
- **输出**: `<log_dir>/summary.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), **覆盖写入**（w 模式）

### 内存累计源
- `log_step()` 更新: `_step_count`, `_last_coverage`, `_last_transport_rate`, `_finished`（`logger.py:183-186`）
- `log_token_usage()` 更新: `_token_accumulator[agent]`（`logger.py:493-508`）

---

## 5a. 子任务跟踪 — subtasks.csv

### 记录点: `sar_orch/logger.py:377-400` (log_subtask)
- **触发条件**: Coordinator 通过 `send_message(message_type="assign_task")` 派发新任务
- **字段**: `RunID`, `Step`, `SubtaskID`, `Status`, `AssignedTo`, `Subtask`, `CreatedAt`, `UpdatedAt`, `FailureClass`, `Details`
- **输出**: `<log_dir>/subtasks.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush
- **调用来源**: `sar_orch/coordinator.py:134-140` (`_log_send_message`, 仅 `assign_task` 分支)

---

## 5b. 事件流 NDJSON — events.ndjson

### 记录点: `sar_orch/logger.py:348-371` (log_event)
- **触发条件**: Coordinator 通过 `send_message` 派发/回复/取消任务时（`_log_send_message` 内 `assign_task`/`reply_to_help`/`cancel_task`/其它 分支分别调用）
- **字段**: `timestamp`, `event_type`, `run_id`, `payload`
- **输出**: `<log_dir>/events.ndjson`
- **写入**: NDJSON append
- **调用来源**: `sar_orch/coordinator.py:114-183` (`_log_send_message` 全函数体，4 处 `log_event` 调用)

---

## 5c. 实验元数据 — metadata.json

### 记录点: `sar_orch/logger.py:333-342` (write_metadata)
- **触发条件**: 实验启动时
- **内容**: `run_id`, `env_name`, `scene`, `seed`, `agent_count`, `model`, `provider`, `api_base`, `state_mode`, `sandbox_profile`, `prompt_version`, `max_steps`, `wall_clock_timeout`, 提示词路径等可重现性字段
- **输出**: `<log_dir>/metadata.json`
- **写入**: JSON overwrite

---

## 6. 实验结果 JSON — run_metrics.json

### 记录点: `sar_orch/experiment.py:619-623`（`main()` 内）
- **触发条件**: 实验结束（`run_experiment()` 返回后，`main()` 把 metrics 落盘）
- **字段**: `finished`, `steps`, `coverage`, `transport_rate`, `elapsed_seconds`, `log_dir`, `end_reason`, `run_id`, `max_steps` + `barrier.get_metrics()`
- **输出**: `<log_dir>/run_metrics.json`
- **写入**: `json.dump` (覆盖)

---

## 7. ContextManager Snapshot — snapshot_{task_id}.json

### 记录点 7a: `src/Agent/worker_agent/context.py:136-156` / `src/Agent/router_agent/context.py:118-138` (save_snapshot)
- **触发条件**: Worker 进入 `INPUT_REQUIRED` 暂停状态时，`AgentController.submit()` 对当前 `task_id` 保存消息历史
- **字段**: `pinned` (typed pinned state 或 dict), `loaded_skills` (已加载的技能), `messages` (完整 Message 列表)
- **输出**: `<log_dir>/snapshot_<task_id>.json`
- **写入**: JSON overwrite，同时保存内存副本 `_task_snapshots[task_id]`

### 记录点 7b: `src/Agent/worker_agent/context.py:158-182` / `src/Agent/router_agent/context.py:140-165` (load_snapshot)
- **触发条件**: Worker 收到 Coordinator 回复后按同一 `task_id` 恢复任务
- **读取顺序**: 内存 snapshot 优先，磁盘 snapshot 作为回退
- **清理**: 磁盘文件加载后删除

---

## 8. Agent 运行日志 NDJSON — `{task_id}.ndjson`

AgentLogger 统一以 NDJSON 格式输出 Agent 运行日志。

### 记录点 8a: `src/Agent/router_agent/logger.py:87-122` / `src/Agent/worker_agent/logger.py:80-114` (log_request)
- **触发条件**: 每次 LLM 调用前
- **事件**: `event="llm_request"`
- **字段**: `ts`(ISO UTC), `task_id`, `context_id`, `event`, `messages`(完整消息列表), `tools`(工具名列表), `step_index`

### 记录点 8b: `src/Agent/router_agent/logger.py:124-156` / `src/Agent/worker_agent/logger.py:116-148` (log_response)
- **触发条件**: 每次收到 LLM 响应后
- **事件**: `event="llm_response"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `content`, `thinking`, `tool_calls`(完整 schema), `finish_reason`, `usage`(prompt/completion/total/cache_hit/cache_miss tokens)

### 记录点 8c: `src/Agent/router_agent/logger.py:158-182` / `src/Agent/worker_agent/logger.py:150-174` (log_tool_result)
- **触发条件**: 每次工具执行后
- **事件**: `event="tool_result"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `tool_name`, `arguments`, `success`, `result`(成功时), `error`(失败时)

- **输出**: `logs/agent/sar_coordinator/<task_id>.ndjson` 或 `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson`
- **写入**: NDJSON append（文件句柄 `open(a)` → 追加 + flush，按 task_id 分文件）

---

## 9. TaskLogger NDJSON — {task_id}.ndjson

### 记录点 9a: `src/a2a/coordinator/task_logger.py:60-76` (init_task)
- **触发条件**: 新任务开始
- **内容**: `{"event":"meta", "task_id":..., "friendly_name":...}`

### 记录点 9b: `src/a2a/coordinator/task_logger.py:109-185` (log_event)
- **触发条件**: executor/router/worker/verifier 调用 `log_event()`
- **事件类型**: `raw_request`, `task_start`, `agentic_start`, `task_error`, `task_final`, `done`, `plan`, `layer_start`, `task_complete`, `replan`, `verify`, `llm_response`, `tool_result` 等
- **字段**: `timestamp`(ISO), `event`, `source`, `data`(自动截断: `content`→2000 (llm_response), `content`→3000 (tool_result), `tool_arguments`→1000)
- **输出**: `logs/<task_id>.ndjson`
- **写入**: NDJSON append
- **约束**: 文件超 10MB 时记录 warning 后跳过

---

## 10. A2AWorkerSink — EventQueue 推送

`src/a2a/worker/sink.py` 的 `A2AWorkerSink` 将 Worker Agent step 事件实时推入 A2A EventQueue（→ Coordinator 侧 TaskLogger），**不写入磁盘文件**。它由 `AgentAdapter.execute()`（`src/a2a/worker/agent_adapter.py:166`）构造，与 `sar_orch/worker.py:351-405` 的 `_step_callback`（负责 CSV/NDJSON 落盘）通过 `TeeSink`（`src/a2a/worker/agent_adapter.py:167-171`）并行接收同一份 step 事件——两者是互补的两条通路，不是互斥/替代关系。

### 记录点: `src/a2a/worker/sink.py:43-102` (emit)
- **触发条件**: Worker Agent 的 step_callback 事件（由 `TeeSink` 分发）
- **推送内容**: `[LLM]` / `[Tool]` / `[Result]` 文本 + `[DATA]` JSON 块（含 content[:2000]、tool_name、arguments 等）
- **流向**: `EventQueue` → `TaskStatusUpdateEvent` → A2A push → Coordinator (TaskLogger)
- **持久化**: ❌ 不写文件（CSV/NDJSON 落盘由 `sar_orch/worker.py` 的 `_step_callback` 分支 + `AgentLogger` 承担）

---

## 11. EventStore NDJSON — events_{task_id}.ndjson

### 记录点: `src/a2a/coordinator/event_store.py:61-104` (append)
- **触发条件**: `/a2a/push-callback` 收到 Worker 推送
- **字段**: `ts`, `task_id`, `event_type`, `state`, `text`[:500]

| event_type | 触发条件 |
|-----------|---------|
| `status_update` | push-callback 收到 TASK_STATE_* 变更 |
| `artifact_update` | push-callback 收到 artifact 推送 |
| `help_request` | push-callback 收到 INPUT_REQUIRED |

- **输出**: `<coordinator_log_dir>/events_<task_id>.ndjson`
- **写入**: `open(a)` → NDJSON append
- **约束**: 每个 task_id 最多保留 500 条记录（超限丢弃最旧）

---

## 12. SSE 实时推送

Coordinator (`src/a2a/coordinator/server.py`) 实际提供 4 个 SSE 端点（均以 `media_type="text/event-stream"` 返回），并未移除：

| 端点 | 用途 | 数据来源 |
|------|------|---------|
| `/logs/{task_id}/stream` (`:745`) | 尾随读取任务 NDJSON 日志，逐行推送 | `logs/<task_id>.ndjson` 文件轮询 |
| `/map/state` (`:1237`) | 实时推送 SAR 网格地图状态 | `SARBarrier` 当前 step 快照 |
| `/dashboard/stream` (`:1290`) | 统一仪表盘数据流 | 汇总 barrier/semantic map/token 等状态 |
| `/api/a2a/jsonrpc` (`:1382`, POST) | 把 JSON-RPC 请求流式代理到内部 A2A Server (8081)，支持 SSE | `httpx` 流式转发上游响应 |

`/ui/debug`（`:819`）本身不是 SSE 端点，而是消费 `/logs/{task_id}/stream` 的调试查看器页面。

---

## 13. Python Logging（logger.*）

所有 `logger.info/warning/error/exception` 通过 Python logging 模块输出，由顶层配置决定目标：

| 配置入口 | 输出目标 |
|---------|---------|
| `sar_orch/experiment.py:23-26` | stdout/stderr（StreamHandler） |
| `sar_orch/benchmark.py:26-29` | stdout/stderr（StreamHandler） |
| `src/a2a/coordinator/cli.py:81` | stdout/stderr（StreamHandler） |
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
```
