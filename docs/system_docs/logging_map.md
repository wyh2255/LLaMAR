---
日期: 2026-07-26（2026-09-09 校准对齐代码现状，W1-W3 轨迹改造合并后）
文档类型: 技术文档
文档概述: LLaMAR 项目完整日志系统映射表 — 每个记录点的触发条件、记录内容、输出文件路径
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）；核对口径：类/函数名 grep -n，行号以当前工作区实测为准。
---

# 日志系统映射表

> 路径口径：SAR 实验的 `<log_dir>` = `sar_orch/results/{YYYYMMDD_HHMMSS}_s{scene}_s{seed}_a{agents}/`（experiment.py:49 `_RESULTS_ROOT`，run 目录构造 :637–639），其下分 `coordinator/`、`workers/<AgentName>/`、`supervision/` 子目录（experiment.py:640–653 创建，其中 supervision_dir 同时注入 SARCoordinator 供 watchdog 落盘）。evaluator-private truth 默认外置 `sar_orch/results/truth/<run_name>/`（experiment.py:680–682，W1 起默认开启，`--truth-output-dir` 可覆盖）。standalone CLI 未传 log dir 时，AgentLogger 默认落 `<cwd 上级>/logs/agent/`（worker_agent/logger.py:43），TaskLogger 默认落 `logs/`（server.py:408 `TaskLogger(base_dir=... or "logs")`）。

## 输出文件清单

| 文件 | 写入方式 | 来源 |
|------|---------|------|
| `<log_dir>/trajectory.csv` | CSV append | `ExperimentLogger.log_step`（logger.py:133，含 W3 `NoOpSource` 列） |
| `<log_dir>/agent_interactions.csv` | CSV append | `ExperimentLogger.log_agent_interaction` / `log_coordinator_state`（logger.py:253/:327，含 W3 `LLMInputChars` 列） |
| `<log_dir>/router_interactions.csv` | CSV append | `ExperimentLogger.log_router_interaction`（logger.py:447；覆盖四类 dispatch + update_plan/finish_task/query_*） |
| `<log_dir>/token_usage.csv` | CSV append | `ExperimentLogger.log_token_usage`（logger.py:496；含 W1 `Status` 列，每 request 一行） |
| `<log_dir>/summary.csv` | CSV overwrite | `ExperimentLogger._write_summary`（logger.py:698） |
| `<log_dir>/metadata.json` | JSON overwrite | `ExperimentLogger.write_metadata`（logger.py:374） |
| `<log_dir>/events.ndjson` | NDJSON append | `ExperimentLogger.log_event`（logger.py:389） |
| `<log_dir>/subtasks.csv` | CSV append | `ExperimentLogger.log_subtask`（logger.py:418；含 assigned/canceled/mission 终态行） |
| `<log_dir>/run_metrics.json` | JSON overwrite | `experiment.py`（run 内 :1202–1204 先写，`main()` :1430 重写） |
| `<log_dir>/scene_config.json` | JSON overwrite | `experiment.py:_dump_scene_config`（:415 定义，:661 首次落盘，env 初始化后、任何 step 前） |
| `<coordinator_log_dir>/snapshot_<task_id>.json` | JSON write/delete | `ContextManager.save_snapshot/load_snapshot`（worker_agent/context.py:338/:379；router_agent/context.py:319/:360；INPUT_REQUIRED 暂停恢复） |
| `<coordinator_log_dir>/semantic_map.jsonl` | NDJSON append | `SemanticMapStore.set_jsonl_path`（coordinator.py:503，Coordinator 侧语义地图日志） |
| `<coordinator_log_dir>/long_term/long_term.sqlite3` | SQLite | `LongTermMemoryStore`（contracts.py:847；coordinator.py:538–546 以 `memory_root=log_dir` 实例化，仅 `--long-term-mode != off`） |
| `<coordinator_log_dir>/diagnosis/diagnosis.sqlite3` | SQLite | `DiagnosisMemoryStore`（contracts.py:883；coordinator.py:583–585，fail-closed） |
| `<coordinator_log_dir>/diagnosis/transcripts.ndjson` | NDJSON append | `diagnosis_loop.py` 每轮 `kind=diagnosis_round` + `long_term_reflection.py:450` `kind=rolling_state` 中间态（W2，best-effort） |
| `<agent_log_dir>/context/prune_events.ndjson` | NDJSON append | `ContextManager._append_prune_event`（worker_agent/context.py:509；router_agent/context.py:510；W2 裁剪 instrumentation） |
| `<agent_log_dir>/context/discards.ndjson` | NDJSON append | `ContextManager._append_prune_discard`（worker_agent/context.py:525；router_agent/context.py:526；被裁原文另存） |
| `<truth_output_dir>/truth_trace.jsonl` / `truth_manifest.json` | JSONL/JSON append+finalize | `sar_orch/eval/truth_recorder.py`（truth_recorder.py:57–58 文件名；W1 起**默认开启**，默认目录 `sar_orch/results/truth/<run_name>/` 必须在 run results 外，experiment.py:680–690 强制校验；metadata 记 `truth_dir` :722） |
| `logs/agent/sar_coordinator/<task_id>.ndjson` | NDJSON append | `AgentLogger`（Coordinator RouterAgent；dashboard 模式路径 @launch_dashboard.py:46；文件名 = task_id，logger.py:69） |
| `logs/<task_id>.ndjson` | NDJSON append | `TaskLogger`（文件名 = safe_name（friendly_name）或 task_id，task_logger.py:53/94–103） |
| `logs/agent/sar_coordinator/events_<task_id>.ndjson` | NDJSON append | `EventStore`（task_id = `coordinator` 或 `dsp_<uuid>`，见 §11） |
| `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson` | NDJSON append | `AgentLogger`（Worker Agent；dashboard 模式路径 @launch_dashboard.py:47；文件名 = task_id，worker_agent/logger.py:62） |
| `<coordinator_log_dir>/supervision/supervision_<dispatch_id>.ndjson` | NDJSON append | `SupervisionStateStore`（supervision_state_store.py:194；W3 起归位 `<run>/supervision/` 子目录，server.py:466–485） |
| `<coordinator_log_dir>/<safe_name|task_id>.ndjson` 与 `unknown.ndjson` | NDJSON append | TaskLogger 顶层任务；任务上下文缺失时 task_id 回落 `unknown`（agent_executor.py:200） |
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

### 记录点: `sar_orch/experiment.py:1014–1017`（poll 循环 drain_step_logs）→ `sar_orch/logger.py:133-210` (log_step)
- **触发条件**: poll 循环调用 `barrier.drain_step_logs()` 取出上次轮询以来完成的**全部** step（不再只看最新一步，避免步进快于轮询间隔时中间 step 被静默丢弃），逐条写入
- **字段**: `Step`, `Actions`, `Successes`, `Observations`, `Coverage`, `TransportRate`, `Finished`, MapRecall, `Freshness`, `TimeoutAgents`, `NoOpSource`（W3 新增，与 Actions 对齐的 per-agent NoOp 来源列表）, `RunID`, `MaxSteps`, `RemainingSteps`, `WallTimeSinceStart`, `StepDurationMs`, `ErrorTypes`, `CompletedSubtasksDelta`, `EndReason`（写行 logger.py:177–196，header :618–635；列名是 **`ErrorTypes`**，per-agent 列表，非 `ErrorTypeByAgent`）
- **NoOpSource 取值**: `llm`（LLM 主动调用 no_op 工具）/ `idle_heartbeat`（worker idle 心跳占位）/ `timeout_injected`（barrier 超时自动注入）；真实动作恒为 `""`。派生规则见 barrier.py:203–225（submit_action 内），timeout 注入见 barrier.py:275（`("NoOp", True, "timeout_injected")`），快照字段 `noop_sources` barrier.py:431
- **EndReason 终态回填**: `set_end_reason`（logger.py:211）把终态值回填到全部缓存行并整体重写 trajectory.csv
- **输出**: `<log_dir>/trajectory.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

---

## 2. Agent 交互 — agent_interactions.csv

### 记录点 2a: `sar_orch/worker.py:279-366` (`_on_step_event` → tool_start/tool_result 分支，`log_agent_interaction` 调用在 :350)
- **触发条件**: Worker Agent 工具执行完，有 `_pending_tool` 数据
- **字段**: `Step`, `Agent`, `ToolName`, `ToolArgs`(JSON), `Action`(SAR 语义), `Observation`, `LLMInput`(最近6条消息摘要), `LLMInputChars`（W3 新增：本轮完整未截断 LLM 输入消息总字符数，LLMInput 列只保留 6×200 摘要）, `LLMOutput`, `Thinking`, `RunID`, `CorrelationID`, `EventType`, `ToolLatencyMs`, `Success`, `ErrorType`（写行 logger.py:295–310，header :636–652）
- **correlation id 生成**: worker.py:340（`{agent}-tool-{seq}`，tool_start 分支）
- **输出**: `<log_dir>/agent_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

### 记录点 2b: `sar_orch/coordinator.py:485` (`_on_router_event` → query_sar_state tool_result)
- **触发条件**: Coordinator 收到 `query_sar_state` 的 `tool_result` 事件
- **字段**: `Agent="Coordinator"`, `ToolName="query_sar_state"`, `ToolArgs`=[state_summary[:2000]], `RunID`, `CorrelationID`, `EventType="query_sar_state"`（logger.py `log_coordinator_state` :327–370）
- **输出**: 同上 `agent_interactions.csv`

---

## 3. 路由调度 — router_interactions.csv

### 记录点: `sar_orch/coordinator.py:154-272`（`_log_send_message`，四类 dispatch 共用）与 `:382-490`（`_on_router_event` 其余工具）
- **触发条件**:
  - dispatch 类：`send_message(message_type="assign_task"/"reply_to_help"/"cancel_task"/"activate_plan_node")`，四类全部经 `_log_send_message` 落 router_interactions.csv（coordinator.py:173/:209/:234/:261）
  - 其余类：`update_plan`（:373）/ `query_sar_state`（:424，仅 oracle 模式）/ `query_task_events`（:447）/ `finish_task`（:455）/ `query_workers`（:462）等 `_on_router_event` 内 tool_start 分支
- **字段**: `Step`, `Subtask`(工具描述+参数), `AssignedTo`(目标 agent 或 "Coordinator"), `RunID`, `CorrelationID`, `WorkerTaskID`, `EventType`, `Success`, `ErrorType`（写行 logger.py:470–480，header :648–658）
- **correlation id 生成**: coordinator.py:171（`coordinator-dispatch-{seq}`）
- **输出**: `<log_dir>/router_interactions.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush

> 注：`SendMessageTool` 实际支持 `assign_task`/`reply_to_help`/`cancel_task`/`activate_plan_node` 四种 `message_type`（`src/a2a/builtin_tools/send_message.py:74-81`）；W1 起四种 dispatch 全部进入 router_interactions.csv（早期文档只覆盖前三种）。`activate_plan_node`（DAG 节点激活）与 `update_plan`（declarative commit 摘要）读取 MissionGraph 声明，不依赖 LLM 传入的 `who`/`content`。

---

## 4. Token 用量 — token_usage.csv

### 记录点 4a: `sar_orch/worker.py:310-324` (`_on_step_event` → llm_response 分支)
- **触发条件**: Worker Agent 每次收到 LLM 响应（含 usage）；LLM 异常/失败路径（agent.py run 循环直接 return）不发 llm_response 事件，由 worker 补 0 值行并标记 `Status="error"`，保证**每 request 一行**
- **字段**: `Step`, `Agent`(=agent_name), `PromptTokens`, `CompletionTokens`, `TotalTokens`, `CacheHitTokens`, `CacheMissTokens`, `RunID`, `LLMLatencyMs`, `Model`, `PromptVersion`, `Status`（W1 新增，`ok`/`error`；写行 logger.py:505–520，header :655–668；`LLMLatencyMs`/`Model`/`PromptVersion` 缺省时由 logger 的 default 兜底）
- **输出**: `<log_dir>/token_usage.csv`

### 记录点 4b: `sar_orch/coordinator.py:391-405` (`_on_router_event` → llm_response)
- **触发条件**: Coordinator RouterAgent 每次收到 LLM 响应（含 usage）；异常路径同样补 `Status="error"` 0 值行
- **字段**: `Agent="Coordinator"`, 其余同上

### 记录点 4c: 语义地图辅助模型
- `coordinator.py:542`（MapSummarizer）与 `:796`（MapAgent）也经 `log_token_usage` 落同一文件，`Agent` 字段区分来源。

### 缓存字段来源
| 提供方 | `CacheHitTokens` | `CacheMissTokens` |
|--------|-----------------|-------------------|
| DeepSeek (OpenAI 协议) | `response.usage.prompt_cache_hit_tokens` | `response.usage.prompt_cache_miss_tokens` |
| OpenAI | `response.usage.prompt_tokens_details.cached_tokens` | `prompt_tokens - cached_tokens` |
| Anthropic | `response.usage.cache_read_input_tokens` | `response.usage.input_tokens + response.usage.cache_creation_input_tokens` |

保证 `CacheHitTokens + CacheMissTokens == PromptTokens`。

---

## 5. 实验汇总 — summary.csv

### 记录点: `sar_orch/logger.py:698-750` (_write_summary)
- **触发条件**: `flush_summary()` 每步 poll 调用，或 `close()` 时
- **字段**: `ExperimentName`, `LogDir`, `TotalSteps`, `FinalCoverage`, `FinalTransportRate`, `Finished`, `TotalAgentInteractions`, `TotalRouterInteractions`, `{Agent}PromptTokens`, `{Agent}CompletionTokens`, `{Agent}TotalTokens`, `{Agent}CacheHitTokens`, `{Agent}CacheMissTokens`
- **输出**: `<log_dir>/summary.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), **覆盖写入**（w 模式）

### 内存累计源
- `log_step()` 更新: `_step_count`, `_last_coverage`, `_last_transport_rate`, `_finished`（`logger.py:201-204`）
- `log_token_usage()` 更新: `_token_accumulator[agent]`（`logger.py:532-546`）

---

## 5a. 子任务跟踪 — subtasks.csv

### 记录点: `sar_orch/logger.py:418-440` (log_subtask)
- **触发条件**: Coordinator 派发/终结任务时写行——`assign_task` 派发新任务写 `assigned`（coordinator.py:181）；`cancel_task` 补 `canceled` 终态行（coordinator.py:240，W1 起）；`finish_task` 补 mission 级终态行 `subtask_id="mission"`，按提交的 `success` 标志写 `completed`/`failed`（coordinator.py:470，append-only 不回填）
- **字段**: `RunID`, `Step`, `SubtaskID`, `Status`, `AssignedTo`, `Subtask`, `CreatedAt`, `UpdatedAt`, `FailureClass`, `Details`（header :666–678）
- **输出**: `<log_dir>/subtasks.csv`
- **写入**: CSV DictWriter (QUOTE_ALL), append + flush
- **调用来源**: `sar_orch/coordinator.py:181`（`_log_send_message` 内，仅 `assign_task` 分支）/ `:240`（cancel_task）/ `:470`（finish_task mission 终态）

---

## 5b. 事件流 NDJSON — events.ndjson

### 记录点: `sar_orch/logger.py:389-417` (log_event)
- **触发条件**: Coordinator 通过 `send_message` 派发/回复/取消任务等语义事件
- **字段**: 统一 schema `timestamp`, `event_type`, `run_id`, `payload`（+ 附加 kwargs 如 step/agent）
- **输出**: `<log_dir>/events.ndjson`
- **写入**: NDJSON append
- **调用来源**: `sar_orch/coordinator.py` `_log_send_message` 全函数体（:154–272）内多处 `log_event` 调用（:188/:215/:240/:254/:266 等）

---

## 5c. 实验元数据 — metadata.json

### 记录点: `sar_orch/logger.py:374-388` (write_metadata)
- **触发条件**: 实验启动时（experiment.py:723 构造并写入）
- **内容**: 由 `experiment.py:97-135` `build_run_metadata` 构造 — `run_id`, `env_name`, `scenario_id`, `scene`, `seed`, `agent_count`, `model`, `provider`, `api_base`, `max_steps`, `wall_clock_timeout`, `sandbox_profile`, `task_objective`, `success_criteria`, `coordinator_prompts`, `worker_prompts`, `prompt_version`, `code_commit` + W1 新增 `worker_prompt_sha256`/`coordinator_prompt_sha256`（实际加载 prompt 文件内容 sha256 前 12 位，`_sha256_file_fingerprint` experiment.py:69；semantic 模式解析为 `system.semantic.md`，其余 `system.md`）+ 运行期补充 `state_mode`/`oracle_mode`/`enable_peer_mail`/`truth_dir`（experiment.py:718–722）
- **输出**: `<log_dir>/metadata.json`
- **写入**: JSON overwrite（sort_keys）

---

## 5d. 长期记忆与诊断持久化（2026-08 新增）

### long_term.sqlite3
- **位置**: `<coordinator_log_dir>/long_term/long_term.sqlite3`（contracts.py:847 `long_term_db_path`）
- **触发条件**: `--long-term-mode != off` 时 coordinator.py:538–546 以 `memory_root=Path(log_dir)` 实例化 `LongTermMemoryStore`，`open()` 内部 `sqlite3.connect`
- **性质**: coordinator Python 代码直接写盘，不经 LLM 工具通道，不受 SandboxPolicy 约束（见 sandbox.md Non-goals 延伸）

### diagnosis.sqlite3 / transcripts.ndjson
- **位置**: `<coordinator_log_dir>/diagnosis/diagnosis.sqlite3`（contracts.py:883）与 `<coordinator_log_dir>/diagnosis/transcripts.ndjson`（W2 起）
- **触发条件**: 同上随 long_term_mode 接线（coordinator.py:553–599，fail-closed：打不开仅禁用通道，不阻塞启动）；rolling 反思每 `every_env_step=5` 步触发诊断循环（`_run_diagnosis_channel` @long_term_reflection.py:354，节流 `min_interval_sec=30` @contracts.py:955）
- **transcripts.ndjson 记录点**: 每执行一轮诊断循环追加一行 `kind="diagnosis_round"`（round/ts/scope_id/evidence/conclusion/state/reason，evidence 即评审实际看到的四件只读工具视图，diagnosis_loop.py）；rolling 反思每轮中间态追加 `kind="rolling_state"`（long_term_reflection.py:450–480，:332–345 调用）；best-effort（写失败仅日志，D8 绝不阻塞）

### truth trace / truth manifest（evaluator-private）
- **位置**: **默认开启**（W1 起，3384310）：默认 `sar_orch/results/truth/<run_name>/`（experiment.py:680–682，`_RESULTS_ROOT` 下 run 外独立目录），`--truth-output-dir` 可覆盖；目录必须在 run results 之外（experiment.py:680–690 强制校验）；metadata.json 记录 `truth_dir`（:722）。文件名 `truth_trace.jsonl` / `truth_manifest.json`（truth_recorder.py:57–58），manifest 含 `truth_trace_sha256`（truth_recorder.py:431）
- **触发条件**: Phase 5 truth recorder（@1721713，2026-08-08；W1 起默认接线）。run 终态时 `_finalize_truth_recorder`（experiment.py:166）冻结 manifest；terminal-only 评测器 `memory_projection_quality` 经 `--truth-manifest` 读回（experiment.py:241–254）
- **W1 字段补充**: truth claims 增加 persons 的 `load`/`status`/`spotted`/`deposited` 与 reservoirs 的 `resource_type`/`available`（`_reservoir_available` truth_recorder.py:315–318/:386，`math.inf` 归一化为 `"infinite"`，与 barrier 快照一致 barrier.py:367–380）
- **约束**: legacy memory 模式或无法解析 canonical scope 时静默跳过（返回 None，不失败 run）

---

## 5e. 场景初始布局快照 — scene_config.json

### 记录点: `sar_orch/experiment.py:415-570` (_dump_scene_config)，调用点 :661
- **触发条件**: env 初始化后、任何 step 执行前调用一次（W3，4f67004），dump 初始网格/对象布局
- **schema**: 顶层 `schema_version`/`scene`/`seed`/`num_agents`/`grid`{width,height,altitude}/`objects`{agents,fires,flammables,persons,reservoirs,deposits}；每个对象含 `id`/`name`/`type`，具体对象含初始 `position`{x,y,z}，抽象 Fire 聚合无网格位置、列 `flammable_ids`；类型专属属性（Fire average_intensity/fire_type、Person load/status/spotted/deposited、Reservoir resource_type/available、Deposit/AbsAgent inventory）；枚举 stringify（read_enum），`math.inf` 归一化 `"infinite"`（JSON-safe）
- **输出**: `<log_dir>/scene_config.json`
- **写入**: JSON dump（首次落盘后不再更新；稳定跨 run 基线）

---

## 6. 实验结果 JSON — run_metrics.json

### 记录点: `sar_orch/experiment.py:1202-1204`（run 内先写）与 `main()` 内 `1430`（结束时重写）
- **触发条件**: 实验结束（`run_experiment()` 返回后，`main()` 把 metrics 落盘）；run 内先写一次供验收评测器读取非空 coverage/transport_rate
- **字段**: `finished`, `steps`, `coverage`, `transport_rate`, `elapsed_seconds`, `log_dir`, `end_reason`, `run_id`, `max_steps` + `barrier.get_metrics()`
- **输出**: `<log_dir>/run_metrics.json`
- **写入**: `json.dump` (覆盖)

---

## 7. ContextManager Snapshot — snapshot_{task_id}.json

### 记录点 7a: `src/Agent/worker_agent/context.py:338-378` / `src/Agent/router_agent/context.py:319-359` (save_snapshot)
- **触发条件**: Worker 进入 `INPUT_REQUIRED` 暂停状态时，`AgentController.submit()` 对当前 `task_id` 保存消息历史
- **字段**: `pinned` (typed pinned state 或 dict), `loaded_skills` (已加载的技能), `messages` (完整 Message 列表)
- **输出**: `<log_dir>/snapshot_<task_id>.json`
- **写入**: JSON overwrite，同时保存内存副本 `_task_snapshots[task_id]`

### 记录点 7b: `src/Agent/worker_agent/context.py:379-413` / `src/Agent/router_agent/context.py:360-409` (load_snapshot)
- **触发条件**: Worker 收到 Coordinator 回复后按同一 `task_id` 恢复任务
- **读取顺序**: 内存 snapshot 优先，磁盘 snapshot 作为回退
- **清理**: 磁盘文件加载后删除

---

## 8. Agent 运行日志 NDJSON — `{task_id}.ndjson`

AgentLogger 统一以 NDJSON 格式输出 Agent 运行日志（**文件名 = task_id**，`f"{self._task_id}.ndjson"` worker_agent/logger.py:62、router_agent/logger.py:69，logger.py `_ensure_file_open`；未传 task_id 时默认 `unnamed_task`，logger.py:45–46）。

### 记录点 8a: `src/Agent/router_agent/logger.py:93-129` / `src/Agent/worker_agent/logger.py:86-121` (log_request)
- **触发条件**: 每次 LLM 调用前
- **事件**: `event="llm_request"`
- **字段**: `ts`(ISO UTC), `task_id`, `context_id`, `event`, `messages`(完整消息列表), `tools`(工具名列表), `step_index`

### 记录点 8b: `src/Agent/router_agent/logger.py:130-163` / `src/Agent/worker_agent/logger.py:122-155` (log_response)
- **触发条件**: 每次收到 LLM 响应后
- **事件**: `event="llm_response"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `content`, `thinking`, `tool_calls`(完整 schema), `finish_reason`, `usage`(prompt/completion/total/cache_hit/cache_miss tokens)

### 记录点 8c: `src/Agent/router_agent/logger.py:181-202` / `src/Agent/worker_agent/logger.py:173-194` (log_tool_result)
- **触发条件**: 每次工具执行后
- **事件**: `event="tool_result"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `tool_name`, `arguments`, `success`, `result`(成功时), `error`(失败时)

### 记录点 8d: `src/Agent/router_agent/logger.py:164-180` / `src/Agent/worker_agent/logger.py:156-172` (log_tool_start，W1 起)
- **触发条件**: 每次工具执行**前**（W1，87774a1；两份 agent.py 的 run 循环 tool 执行前调用，router_agent/agent.py:713–719 区域、worker_agent/agent.py 同位置）
- **事件**: `event="tool_start"`
- **字段**: `ts`, `task_id`, `context_id`, `event`, `tool_name`, `arguments`(redacted)
- **用途**: 与 llm_request/llm_response/tool_result 组成完整时序（无 tool_start 时 llm_response 与 tool_result 间的时间差无法归因到工具执行）

- **输出**: SAR/dashboard 模式为 `logs/agent/sar_coordinator/<task_id>.ndjson` 或 `logs/agent/sar_worker/<worker_id>/<task_id>.ndjson`（launch_dashboard.py:46–47）；实验模式下 log_dir 注入为 run 内各 agent 子目录；未注入时默认 `<cwd 上级>/logs/agent/`（worker_agent/logger.py:43）
- **写入**: NDJSON append（文件句柄 `open(a)` → 追加 + flush，按 task_id 分文件）

---

## 9. TaskLogger NDJSON — {task_id}.ndjson

### 记录点 9a: `src/a2a/coordinator/task_logger.py:60-77` (init_task)
- **触发条件**: 新任务开始
- **内容**: `{"event":"meta", "task_id":..., "friendly_name":...}`；friendly_name 存在时后续日志写 `<safe_name>.ndjson`（task_logger.py:60–68 `_name_map`），否则 `<task_id>.ndjson`（:53/:94–103）

### 记录点 9b: `src/a2a/coordinator/task_logger.py:109-186` (log_event)
- **触发条件**: executor/router/worker/verifier 调用 `log_event()`
- **事件类型**: `raw_request`, `task_start`, `agentic_start`, `task_error`, `task_final`, `done`, `plan`, `layer_start`, `task_complete`, `replan`, `verify`, `llm_response`, `tool_result` 等
- **字段**: `timestamp`(ISO), `event`, `source`, `data`(自动截断: `content`→2000 (llm_response), `content`→3000 (tool_result), `tool_arguments`→1000)
- **输出**: `<base_dir>/<task_id>.ndjson`（SAR 实验 base_dir=coordinator log_dir；standalone 默认 `logs/`，server.py:408）。任务上下文缺失时（executor 无 current_task）task_id 回落 `"unknown"`，日志落 `unknown.ndjson`（agent_executor.py:200）
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

### 记录点: `src/a2a/coordinator/event_store.py:75-130` (append)
- **触发条件**: `/a2a/push-callback` 收到 Worker 推送；以及 task_watchdog 监督事件（`TASK_STALE` 等经 `supervision_event` 类型写入，task_watchdog.py:379–383）；以及 supervision view 注入审计（见下）
- **字段**: `ts`, `task_id`, `event_type`, `state`, `text`[:500], `observation`（W1 起，event_store.py:121；observation_report 的结构化观测体，经 redactor 脱敏后原样落盘）
- **文件名**: `events_<task_id>.ndjson`（task_id 即顶层任务 `coordinator` 或 dispatch id `dsp_<uuid>`；SAR run 内 top-level task_id="coordinator" → `events_coordinator.ndjson`）

| event_type | 触发条件 |
|-----------|---------|
| `status_update` | push-callback 收到 TASK_STATE_* 变更 |
| `artifact_update` | push-callback 收到 artifact 推送 |
| `help_request` | push-callback 收到 INPUT_REQUIRED |
| `observation_report` | push-callback 收到 observation 推送（带 `observation` 结构化键） |
| `supervision_event` | watchdog 监督告警（state=`TASK_STALE` 等） |
| `supervision_injected` | W3（0d28fd6）：Coordinator Context / runtime state 投影注入非空 supervision view 时的审计事件（alerts 数、alert 类型列表、注入文本字符数；`SARCoordinatorStateProvider._emit_supervision_injected` @coordinator_state_provider.py:830–880 区域；纯附加，不改注入内容/触发条件） |

- **输出**: `<coordinator_log_dir>/events_<task_id>.ndjson`
- **写入**: `open(a)` → NDJSON append；写失败仅 warning（event_store.py:126–127）
- **约束**: 每个 task_id 最多保留 500 条记录（超限丢弃最旧）
- **定位**: Phase 5 起 EventStore 是 *legacy debug adapter*（文件头注释 :8），正式事件面在 `events.ndjson` 与 supervision 目录

---

## 11b. Supervision 状态落盘 — supervision_<dispatch_id>.ndjson

### 记录点: `src/a2a/coordinator/supervision_state_store.py:110-200`（SupervisionStateStore，:194 写文件）
- **触发条件**: TaskWatchdog 每次监督状态刷新（周期 tick / 状态变更）把完整 per-dispatch supervision state 序列化为一行 JSON 追加
- **内容**: 每行一个完整 state 快照 — `ts` + `dispatch_id`/`worker_id`/`worker_task_id`、`supervision_state`（HEALTHY/TASK_STALE 等）、`active_alerts`、`unacknowledged_events`、`last_contact_at`/`last_progress_at`、阈值与 `last_metrics`（coverage/transport_rate/steps）等
- **位置**: **W3（6d763ae）起归位 `<run>/supervision/` 子目录**（server.py:466–485：自建 store 默认 `log_dir/supervision`，外部注入 store 原样使用；旧实现落 coordinator log 根目录）；文件名 schema `supervision_<dispatch_id>.ndjson`（supervision_state_store.py:194），`set_log_dir` 列表查询 :136
- **输出**: `<coordinator_log_dir>/supervision/supervision_<dispatch_id>.ndjson`
- **写入**: NDJSON append；best-effort（写失败不阻塞监督）
- **终验证据**: validate_w3_s3_s42_a4 run 内 `supervision/` 11 个文件，`coordinator/` 下 0 个 supervision 文件

---

## 11c. ContextManager 裁剪 instrumentation — context/prune_events.ndjson + context/discards.ndjson

### 记录点: `src/Agent/worker_agent/context.py:497-540` / `src/Agent/router_agent/context.py:498-541`（W2，a8f12e5）
- **触发条件**: ContextManager 每次对消息历史执行裁剪（phase-1 截断、count_prune 等策略）时追加审计；被裁原文逐条另存
- **prune_events.ndjson**（`_append_prune_event` worker/context.py:509、router/context.py:510）：每条裁剪事件一行 — `step`/`ts`/`policy`（如 `count_prune`）/`trigger`/`pruned_count`/`token_before`/`token_after`（count_prune 事件含 token 预算前后值；phase-1 截断事件含 `reason="phase1_truncate"`）
- **discards.ndjson**（`_append_prune_discard` worker/context.py:525、router/context.py:526）：每个被裁消息原文一行 — `step`/`ts`/`reason`（`count_prune` 或 `phase1_truncate`）/`index`/`role`/`tool_call_id`/`content`（完整原文，供审计重建被裁剪上下文）
- **位置**: `<agent_log_dir>/context/`（worker 实验模式 = `<run>/workers/<Agent>/<Agent>/context/`；`_prune_trace_dir` worker/context.py:497）
- **写入**: NDJSON append；fail-open（写失败仅打印 warning，绝不中断主流程）
- **约束**: 无 log_dir 时静默跳过（standalone 未注入目录不产生文件）

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
  └─ experiment.py poll loop             ├── trajectory.csv（含 NoOpSource）
                                        ├── summary.csv
                                        ├── token_usage.csv（含 Status）
                                        ├── run_metrics.json
                                        ├── (W3) scene_config.json（仅 step 前一次）
                                        └── (long_term!=off) rolling 反思 →
                                            long_term.sqlite3 / diagnosis.sqlite3
                                            / diagnosis/transcripts.ndjson

监督链（独立于实验 poll）:
  TaskWatchdog tick ──► SupervisionStateStore ──► <run>/supervision/supervision_<dispatch>.ndjson
        │                    │
        ├── EventStore: supervision_event（告警）
        └── SARCoordinatorStateProvider: 非空 view 注入 Context 时
            EventStore: supervision_injected（审计事件）

上下文裁剪链:
  ContextManager _compress_phase1 / count_prune
        ├── context/prune_events.ndjson（裁剪事件）
        └── context/discards.ndjson（被裁原文逐条）
```
