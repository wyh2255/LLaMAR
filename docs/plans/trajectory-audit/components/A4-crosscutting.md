# A4 横切组件轨迹落地盘点

盘点日期: 2026-09-09 | 基线: main@3ad6980（只读调研，零仓库改动）
范围: canonical Memory / 长期记忆 / 系统健康诊断 / TaskWatchdog+SupervisionStateStore / ContextManager / EventStore+events.ndjson
证据口径: 行号以当前工作区实测为准；样例 run 为 `sar_orch/results/20260721_201943_s3_s42_a4`（最近 run）与 `20260721_201943_s4_s42_a5`、`20260719_200429_s5_s42_a2`（含 supervision NDJSON 唯一样例）。

## 0. 范围与代码入口（类/模块 + file:line）

| 组件 | 代码入口 |
|---|---|
| Canonical Memory（store/ingest/auth/projection/export/recovery） | `src/a2a/coordinator/memory/store.py`（SQLite 单写者）、`ingestor.py`（唯一变更所有者，:366 ingest_callback）、`callback_auth.py`、`projections.py`、`exporter.py`（:47-55 CANONICAL_ARTIFACTS）、`recovery.py`、`redaction.py` |
| 长期记忆 | `src/a2a/coordinator/memory/long_term.py:194 LongTermMemoryStore`（`publish_memory` :573）、`reflection.py:759 run_reflection`、`sar_orch/long_term_reflection.py`（rolling 触发 :395 maybe_trigger_rolling_reflection）、`sar_orch/coordinator.py:527-551`（接线） |
| 系统健康诊断 | `sar_orch/diagnosis_loop.py:198 DiagnosisLoop`（`run()` :236）、`src/a2a/coordinator/memory/diagnosis.py:414 DiagnosisMemoryStore`（`save_diagnoses` :664）、`sar_orch/long_term_reflection.py:320 _run_diagnosis_channel`、`sar_orch/coordinator.py:561-599`（接线） |
| TaskWatchdog + SupervisionStateStore | `src/a2a/coordinator/task_watchdog.py:42`（检测 :304-482）、`supervision_state_store.py:110`（`_persist` :188-208）、canonical 侧 `ingestor.py:992 SupervisionEventAdapter`、`server.py:1168`（log_dir 接线） |
| ContextManager | `src/Agent/router_agent/context.py:580 compress_with_llm`、`worker_agent/context.py:579`；snapshot `router:318/359`、`worker:337/378`；Phase 1 裁剪 `router:477`；摘要持久化 `router:783-805` |
| EventStore / events.ndjson | `src/a2a/coordinator/event_store.py:75 append`；`sar_orch/logger.py:377-401 log_event`；`sar_orch/coordinator.py` 5 处调用（:188/:215/:240/:254/:266）；TaskLogger `task_logger.py:109`；AgentLogger（logger.py llm_request/response/tool_result） |

模式接线（fail-closed）：`--memory-read-mode legacy|shadow|read_port`（默认 read_port）→ `coordinator.py:642-673` 建 MemoryStore/Ingestor；`--long-term-mode off|shadow|read` → `coordinator.py:527-551`（long_term）与 :561-599（diagnosis，随同一开关，`inject_enabled` 独立旋钮）；shadow+read 组合被拒（coordinator.py:23-38）。

## 1. 轨迹产物清单

| 产物(文件/存储) | 内容字段 | 触发点 file:line | 写入方式/粒度 | 保留策略 | 实际样例路径 |
|---|---|---|---|---|---|
| `coordinator/memory/memory.sqlite3` | 14 表：temporal_event（event_id/sequence/actor/dispatch/worker_task_id/success/payload/causation_id/correlation_id/idempotency_key/supersedes_event_id）、projection_field、idempotency_ledger、control_receipt、outbox、security_audit、callback_nonce、memory_revision… | MemoryIngestor 单一 BEGIN IMMEDIATE 事务（ingestor.py:366+；投影归约 :747-767）；watchdog 监督经 adapter（ingestor.py:992-1067，event_type=`supervision.{type}`） | 每次 callback/监督事件一条 canonical 记录 | run-local（memory_root=log_dir，contracts.py:524-525；目录 0700/文件 0600 store.py:266-283） | 本仓 results 已清理，无现存样例；表结构实证见 docs/system_docs/memory.md §13.1.2（logs/h2_shadow_audit_run2/coordinator/memory/memory.sqlite3 已删） |
| `coordinator/memory/temporal.jsonl / spatial.jsonl / embodied.jsonl / revision.jsonl / outbox.jsonl / relations.jsonl / semantic_map.jsonl` + `export_manifest.json` | 确定性重建的兼容产物 + 每产物 sha256/record_count（exporter.py:87-113） | run-terminal 物化（server.py:582-642；experiment.py:196-201 仅 shadow/read_port）；崩溃恢复 outbox 重放（recovery.py:223-264） | 全量重建（temp+fsync+manifest+atomic replace，exporter.py:149-187） | 随 run；legacy 产物只标记不回填（exporter.py:570-609） | 同 memory.sqlite3（无现存样例） |
| `coordinator/long_term/long_term.sqlite3` | long_term_memory（memory_key/kind∈strategy,lesson,hazard,pattern,status/statement/confidence/status∈candidate,published,superseded/supersedes_memory_id/policy_version）、reflection_run（status∈pending,completed,rejected,failed,timeout）、long_term_support（source_refs）、long_term_audit、long_term_revision | publish_memory（long_term.py:573-690）；claim/mark_reflection_run（reflection.py:759-959） | SQLite 事务；duplicate 幂等、supersede 显式 | run-local（contracts.py:846-847） | **无现存样例**：所有 results run 均未开 long-term-mode（metadata 无该字段，跑前默认 off） |
| `coordinator/reflection_trace.ndjson` | 完整 model 输入（system/user/tool schema）+ 原始输出（function_call+usage）+ validation status（reflection.py:711-734） | run_reflection 每次模型调用 | NDJSON append（失败吞掉不阻塞） | run-local | 无样例（同上） |
| `coordinator/diagnosis/diagnosis.sqlite3` | diagnosis（diagnosis_key/kind/target/finding/suggestion/confidence/policy_version/created_at，UNIQUE(scope,key)）、diagnosis_support（source_scope/source_event） | save_diagnoses（diagnosis.py:664-712，INSERT OR IGNORE） | SQLite 事务；同 key 重复忽略（首见保留） | run-local（contracts.py:882-883） | **无现存样例**（未开模式） |
| canonical `diagnosis.audit` temporal 事件 | count/diagnosis_keys/policy_version（diagnosis_loop.py:422-470） | DiagnosisLoop 验证通过后 best-effort 追加 | 单事件 | run-local（memory.sqlite3） | 无样例 |
| `coordinator/supervision_<dispatch>.ndjson` | 每次 update 全状态行：`{ts, state{supervision_state, active_alerts, unacknowledged_events, acknowledged_event_ids, last_progress_*, last_metrics, terminal, …}}`（supervision_state_store.py:188-208） | TaskWatchdog._check_all 每 tick 末尾 update（task_watchdog.py:259）+ 事件写入后 | NDJSON append，整行全状态（时间序列可重建） | run-local（server.py:1168 set_log_dir(coordinator log_dir)） | **实证**：`results/20260719_200429_s5_s42_a2/coordinator/supervision_1c364fe4-….ndjson`（1 行 HEALTHY 全状态）；07-21 run 无该文件（见 Gap#6） |
| `coordinator/events_<task_id>.ndjson`（EventStore，legacy debug adapter） | ts/task_id/context_id/event_type/state/text[:500]（event_store.py:110-125）；类型 status_update/artifact_update/help_request/supervision_event/task_created/observation_report | push-callback（server.py:1694/1766/1902 等）+ watchdog supervision_event（task_watchdog.py:327-332/379-384/421-426/451-456/476-481） | NDJSON append；每 task 500 条上限（:103-105）；run 终结标 legacy_unmigrated（server.py:815-822） | run-local | **实证**：`results/20260721_201943_s3_s42_a4/coordinator/events_dsp_*.ndjson`（28 文件；supervision_event 39 / status_update 32 / task_created 16 / artifact_update 5） |
| `<run>/events.ndjson` | timestamp/event_type/run_id/payload(+step/agent)（logger.py:377-401）；类型 assign_task/reply_to_help/cancel_task/send_message | coordinator `_log_send_message` 5 处（coordinator.py:188/215/240/254/266） | NDJSON append | run-local | **实证**：`results/20260721_201943_s3_s42_a4/events.ndjson`（79 行：assign_task 30/cancel_task 28/send_message 21） |
| `coordinator/<task_id>.ndjson`（TaskLogger） | task_start/agentic_start/llm_response/tool_start/tool_result/task_complete…（task_logger.py:109-186，超 10MB 跳过） | executor/router/sink 调用 | NDJSON append | run-local | **实证**：`…_a4/coordinator/20260721_201951.ndjson` |
| `workers/<Agent>/<task_id>.ndjson` + `coordinator/<task_id>.ndjson`（AgentLogger） | llm_request（**完整 messages 列表**+tools+step_index）/llm_response（content/thinking/tool_calls/usage）/tool_result（logger.py §8a-c） | 每次 LLM 调用前/后、工具执行后 | NDJSON append | run-local | **实证**：`…_a4/workers/Alice/Alice/2cd7ae0b-….ndjson`（16 request/16 response/18 tool_result；llm_request 每条 3 messages） |
| `coordinator/memory_rollout_audit.ndjson` | shadow 对比差异 / read_port rollback latch（coordinator_state_provider.py:64-92、113-125） | shadow 模式每 env step 对比；rollback 一次 | NDJSON append | run-local | 本仓已清理；docs/system_docs/memory.md §12.1.3 记录 logs/h2_shadow_audit_run2 样例（20 条，mode=shadow） |
| `coordinator/snapshot_<task_id>.json` | version/loaded_skills/messages（ContextSnapshotV2；router_agent/context.py:318-357） | INPUT_REQUIRED 暂停（save_snapshot）；加载后磁盘删除（load_snapshot :359-417 os.remove） | JSON 覆盖写 | run-local（恢复即删） | results 内无残留（全部恢复或未暂停） |
| `coordinator/compression_summary.json` | 仅 `{summary: <Phase 3 摘要全文>}`（router_agent/context.py:783-793） | `_generate_compression_summary` 成功后 | JSON 覆盖写（只留最后一份，无元数据） | run-local | **无样例**：`compress_with_llm` 无任何调用点（见 Gap#3） |
| `<run>/supervision/` 目录 | 空目录 | experiment.py:448-459 mkdir | — | run-local | **实证**：`…_a4/supervision/` 存在但空 |

## 2. 三问回答（按六组件逐个）

### 组件 1：Canonical Memory（memory/ 子系统）

- **Q1 完整轨迹记录？有。** 每次认证 callback/监督事件在单事务内写 temporal_event（全字段账本）+ projection_field + revision + outbox（ingestor.py:366+），导出物确定性重建（exporter.py）。拒绝路径（truth 越界/认证失败）零域写入但留脱敏 security_audit（ingestor.py:383/740；store.py:519）。轨迹粒度=事件级（每证据一条，含 causation/correlation/idempotency）。
- **Q2 交互记录保存？有（读侧+写侧）。** 写侧=temporal_event 账本+security_audit；读侧=AgentLogger `llm_request.messages` 完整记录每轮实际送入 LLM 的消息（含 read-port 视图），实证 workers/Alice/….ndjson（16 条 llm_request，每条 3 messages）。
- **Q3 相关状态逐步保存？有。** projection_field 按 (domain,entity,field) 每事件演进 + memory_revision 单调（contracts.py:544-552）；export manifest 固化 sha256；恢复层 verify_committed_canonical 只读比对（recovery.py:173-219）。

### 组件 2：长期记忆（LongTermMemoryStore）

- **Q1 完整轨迹记录？有。** publish_memory 落 long_term_memory（含 content_digest、supersedes 链、policy_version）+ long_term_support 证据引用；每次模型调用的完整输入输出另落 reflection_trace.ndjson（reflection.py:711-734）。
- **Q2 交互记录保存？有。** reflection_run 表记录每轮反思 claim→终态（pending/completed/rejected/failed/timeout，long_term.py:103-121 CHECK；mark 调用点 reflection.py:859/886/921/932/959）；terminal drain 超时写 long_term_audit `reflection_timeout`（experiment.py:309-317）；drain 结果（含 diagnosis）进 `run_metrics.json["long_term_reflection"]`（experiment.py:1025）。
- **Q3 相关状态逐步保存？有。** long_term_revision 每次内容写入递增；superseded 旧行保留（status 翻转，不删除）；audit 表追加式。
- **注入段**：仅 `_is_system + long_term_mode=="read"`（environment_state_provider.py:442）；预算档 threshold 3（environment_state.py:134 标题 + provider :544 裁剪标记 TRUNCATED）。

### 组件 3：系统健康诊断（DiagnosisLoop + DiagnosisMemoryStore）

- **Q1 完整轨迹记录？部分。** 成功诊断→diagnosis.sqlite3（含 confidence/policy_version）+ canonical diagnosis.audit 事件；**但每轮 LLM 输入（四件工具查到的证据 transcript）只在内存**（diagnosis_loop.py:246 transcript、:368-392 收集），不落盘；拒绝/超时/轮次耗尽只经 DiagnosisLoopResult 返回（:236-326），rolling 通道内 `_inflight["result"]` 被内存覆盖（long_term_reflection.py:377-392），中间轮次状态在 terminal drain 前丢失。
- **Q2 交互记录保存？部分。** 成功轮：诊断结论+置信度+source_refs 落库；**超时被丢弃的轮次**仅在最后一次 drain 的 result 里以 status/rounds/reason 出现（experiment.py:308 透传），中间轮次无独立记录；round_latencies/evidence_sec/duration_sec 为 R4 补观测（diagnosis_loop.py:159-166），同样只经 drain 落 run_metrics.json。
- **Q3 相关状态逐步保存？部分。** diagnosis 表 UNIQUE(scope_id, diagnosis_key) + INSERT OR IGNORE（diagnosis.py:671-690）——同 key 首见保留，后续同 key 新结论**不更新**（演进被忽略，只留首条）；无 revision 列。
- **注入段**：仅 coordinator + read + `diagnosis_inject_enabled` + min_confidence(0.6) 过滤 + budget≥3（environment_state_provider.py:449-457、490-519、528-545）；无注入审计。

### 组件 4：TaskWatchdog + SupervisionStateStore

- **Q1 完整轨迹记录？有（检测→三路落盘）。** 检测（task_watchdog.py:165-259 tick）→ 事件 dict（event_id=`{dispatch_id}::{counter}` :51-53）→ 三路：① SupervisionStateStore.update 全状态快照行（:188-208）② EventStore.append supervision_event（:327-332 等 5 处）③ _emit_to_memory→canonical `supervision.{type}` temporal 事件（:79-108；ingestor.py:992-1067，幂等键 sha256(scope,"supervision",event_id)）。
- **Q2 交互记录保存？有。** 事件四类+恢复类全落三处；实证 20260721 run events_dsp 39 条 supervision_event（TASK_STALE 带 progress_age_seconds/steps_since_progress；TASK_RECOVERED 带 recovered_from）。**生命周期最后一环"注入 runtime state/Context"无独立留痕**——CoordinatorPinnedState.supervision 字段（contextmanager.md §5.1）→ pre_llm Environment State 渲染，事后只能从 AgentLogger llm_request 反推。
- **Q3 相关状态逐步保存？有。** SupervisionState 每次 update 整行快照（active_alerts/unacknowledged/acknowledged/terminal/last_metrics 全字段），时间序列可完整重建状态机演进；acknowledge_event 只改内存+落盘下一行（:218-230）。

### 组件 5：ContextManager

- **Q1 完整轨迹记录？部分。** snapshot_<task_id>.json 只在 INPUT_REQUIRED 暂停时写、恢复即删（router:318-417）——正常运行无残留；**摘要生成轨迹**：compression_summary.json 保存摘要全文（:783-793）但 `compress_with_llm` 无调用点（grep 实证仅定义，router:580 / worker:579），Phase 3 是死代码路径；Phase 1 裁剪（router:477-529）静默（无日志/计数）。
- **Q2 交互记录保存？无（裁剪侧）。** 何时裁、裁了多少条、触发 token 数均无记录；episodic 记忆（worker 端 20 条上限）只存内存，不进 snapshot（ContextSnapshotV2 只含 messages+LoadedSkillRef，contextmanager.md §6）；worker 计数剪枝把被移除的 assistant 消息转 episodic（worker_agent/context.py）同样不留盘。
- **Q3 相关状态逐步保存？部分。** snapshot 文件本身即状态持久化（暂停→恢复闭环）；但 pinned/episodic/runtime_state 均不落盘（内存视图），压缩摘要只有最后一份（覆盖写）。

### 组件 6：EventStore / events.ndjson

- **Q1 完整轨迹记录？有（三层事件面）。** ① sar_orch events.ndjson（语义事件 assign/reply/cancel/send_message，实证 79 行）；② EventStore events_<tid>.ndjson（任务级事件流，实证 92 条含 watchdog 39 条）；③ TaskLogger/AgentLogger NDJSON（任务生命周期+LLM 调用全量）。EventStore 定位为 legacy debug adapter（event_store.py:8-11），正式事件面=events.ndjson+supervision 目录+canonical。
- **Q2 交互记录保存？有。** 事件带 ts/task_id/state/text/context_id，AgentLogger 带完整 messages/usage（LLM 交互全量）。
- **Q3 相关状态逐步保存？部分。** events_<tid>.ndjson 每 task 上限 500 条（超限丢最旧，event_store.py:103-105）；TaskLogger 超 10MB 跳过（task_logger.py:241）；events.ndjson 无上限。

## 3. 与 logging_map.md 对照（逐条）

| logging_map.md 条目 | 判定 | 证据 |
|---|---|---|
| long_term.sqlite3 位置 `<coordinator_log_dir>/long_term/`、contracts.py:847、仅 long-term-mode≠off（:27/:164-167） | 一致 | contracts.py:834-847（LongTermMemoryConfig.long_term_db_path）；coordinator.py:527-551；行号 847 实测吻合 |
| diagnosis.sqlite3 位置/接线/fail-closed（:28/:169-171） | 一致 | contracts.py:861-883；coordinator.py:561-599；rolling 触发 long_term_reflection.py:395 |
| events.ndjson 记录点（:141-148，5 处 log_event） | 一致 | logger.py:377-401；coordinator.py:188/215/240/254/266 |
| EventStore events_<tid>.ndjson 四类事件+500 上限（:259-275） | 一致 | event_store.py:75-127、103-105；实证 events_dsp 92 条 |
| TaskLogger/AgentLogger NDJSON（§8/§9） | 一致 | task_logger.py:109-186；实证 workers/Alice/….ndjson（llm_request 完整 messages） |
| snapshot_<task_id>.json（§7，router context.py:337-377） | 一致 | router_agent/context.py:318/359（行号偏移 19 行，语义同） |
| `<log_dir>/supervision/` 子目录为监督产物所在（:10 路径口径） | 冲突 | experiment.py:448-459 只建空目录；SupervisionStateStore NDJSON 实际落 `<coordinator_log_dir>/supervision_<dispatch>.ndjson`（server.py:1168 set_log_dir=self._log_dir，log_dir=coordinator 目录）——实证 20260719 run 在 coordinator/ 下，`<run>/supervision/` 为空 |
| events.ndjson 类型仅 assign/reply/cancel（:148 只列 3 类） | 补充 | activate_plan_node 与 fallback 共用 `send_message` 类型（coordinator.py:254/266）；实证 79 行含 21 条 send_message |
| 长期记忆/诊断注入段（文档未记） | 补充 | `### Long-term Memory`/`### System Health` 标题注册于 environment_state.py:130-134；注入门控 environment_state_provider.py:442-457（_is_system+read+inject_enabled+min_confidence+budget） |
| supervision_<dispatch>.ndjson（文档未列） | 补充 | supervision_state_store.py:188-208；实证 20260719 run |
| reflection_trace.ndjson / long_term_audit / reflection_run 状态机（文档未列） | 补充 | reflection.py:711-734；long_term.py:96-160；experiment.py:309-317 |
| compression_summary.json（文档未列） | 补充 | router_agent/context.py:783-805；且 Phase 3 无调用点（见 Gap#3） |
| memory_rollout_audit.ndjson（logging_map 未列，memory.md §12.1.3 有） | 补充 | coordinator_state_provider.py:64-92、113-125 |
| truth trace/manifest 输出目录须在 results 外（:29） | 一致 | experiment.py:476-485；truth_recorder.py |

## 4. Gap 清单

| # | Gap | 证据 file:line | 对"用轨迹探索框架优化"的影响 |
|---|---|---|---|
| 1 | DiagnosisLoop 每轮 LLM 输入（四件工具证据视图 transcript）不留盘，仅内存 | diagnosis_loop.py:246,368-392 | 高——无法事后重建"诊断者看到了什么"，证据→结论映射不可审计 |
| 2 | rolling 诊断轮次中间态（rejected/timeout/rounds_exhausted）被内存覆盖，仅 terminal drain 的最后一轮结果进 run_metrics.json | long_term_reflection.py:377-392（_inflight result 覆盖）；experiment.py:308,1025 | 高——诊断通道失败/超时轮次无独立历史，频率/模式分析缺失 |
| 3 | ContextManager Phase 3 LLM 压缩（compress_with_llm）无任何调用点——摘要生成是死代码；compression_summary.json 仅存最后一份、无元数据（何时裁/裁多少/触发 token） | context.py:580（定义）；grep 全仓无调用；:783-793 | 高——"摘要轨迹"当前不可用；若启用需补元数据 |
| 4 | Phase 1 裁剪（工具结果截断）静默执行，无日志/计数器/落盘 | context.py:477-529、836-848 | 中——上下文压缩行为完全不可追溯 |
| 5 | worker episodic 记忆（20 条上限）只存内存，snapshot 不含 episodic；进程结束即丢 | worker_agent/context.py（_Episode）；contextmanager.md §6 | 中——worker 中期记忆演进无法事后分析 |
| 6 | SupervisionStateStore NDJSON 落盘位置与文档声明的 supervision/ 子目录不符（实际 coordinator/）；且 20260721 样例 run 中 store 未落盘（watchdog 事件只进了 EventStore，无 supervision_*.ndjson） | server.py:1168；supervision_state_store.py:188-208；实证 07-21 vs 07-19 run 差异 | 中——监督状态持久化不稳定/不一致，跨 run 比对易误导（不确定 07-21 未落盘根因：该 commit 的 set_log_dir 接线状态） |
| 7 | watchdog 事件"注入 runtime state/Context"环节无独立审计记录，只能从 AgentLogger llm_request 完整 messages 反推 | task_watchdog.py:79-108（落盘三路）；注入走 CoordinatorPinnedState.supervision（contextmanager.md §5.1） | 中——注入链路可追溯但依赖 AgentLogger 开关，无注入专用事件 |
| 8 | 样例 run 全部未开启 memory/long-term/diagnosis 模式（最近 run 为 2026-07-21 legacy 时代，metadata 无 memory_read_mode）——SQLite 系产物零真实样例 | results 全扫：memory/long_term/diagnosis 目录全 NONE；run_metrics.json 无 memory_terminal/long_term_reflection 键 | 低——本报告 SQLite 部分仅依据代码+文档，真实形状未验证（需一次 read_port+long-term=read 的样例 run） |
| 9 | events.ndjson 事件类型全集不完整：activate_plan_node 与未知 message_type 共用 send_message，无法区分 | coordinator.py:254/266 | 低——调度语义事件粒度不足 |
| 10 | diagnosis 表同 key 新结论被 INSERT OR IGNORE 忽略，诊断演进不保留 | diagnosis.py:671-690 | 中——诊断随时间漂移无法分析（同 target 新发现会被吞） |

## 5. 初步观察：现有轨迹能支撑哪些框架优化分析

- **事件级因果链**：canonical temporal_event 全字段（event_id/sequence/causation/correlation/idempotency/supersedes）+ supervision.* 独立前缀 + idempotency 键 → 支撑"事件→证据→投影"全链路因果与去重分析。
- **记忆写审计**：security_audit（拒绝/truth-denial 脱敏）+ long_term_audit（reflection_timeout 等）+ reflection_run 状态机 → 支撑记忆写入的失败/重试/幂等模式分析。
- **记忆读审计（模型实际所见）**：AgentLogger llm_request 完整 messages（实证每条 3 messages）→ 支撑"某轮 coordinator 被注入了哪些记忆条目"的事后查证（Q2 核心问题可答）；shadow 模式另有 memory_rollout_audit.ndjson 差异证据。
- **诊断成本/延迟观测**：DiagnosisLoopResult.round_latencies/evidence_sec/duration_sec（R4 补观测）经 run_metrics.json.long_term_reflection 落盘 → 支撑诊断通道成本-时间分析（仅 terminal 快照，滚动轮次缺失——需 Gap#2 修复）。
- **watchdog 状态机演进**：supervision_*.ndjson 全状态快照行（active_alerts/unacknowledged/acknowledged）→ 支撑监督状态的时间序列重建与告警-恢复配对分析（实证 07-19 run 样例）。
- **上下文行为盲区**：Phase 1 裁剪/episodic/Phase 3 摘要目前零轨迹（Gap#3/4/5）→ "上下文压缩如何影响长 run"类分析当前不可做，需先补 instrumentation。
- **模式开关与产物对应**：memory/long-term/diagnosis 的产物面完全由 CLI 模式门控（fail-closed），分析前需按 run metadata 声明模式（当前样例 run 均 legacy，SQLite 产物零样例——Gap#8）。

---
（收尾说明：本报告为只读调研，未修改任何仓内文件；允许的唯一下发文件即本文档。样例 run 引用：`sar_orch/results/20260721_201943_s3_s42_a4`（事件三件套实证）、`sar_orch/results/20260719_200429_s5_s42_a2/coordinator/supervision_1c364fe4-…ndjson`（监督快照实证）。）
