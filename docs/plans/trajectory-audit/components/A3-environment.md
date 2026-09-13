# A3 环境/仿真每步状态落地盘点

> 调研 worker 只读盘点，2026-09-09。仓库：LLaMAR（main@45299ce 附近）。
> 本卡灵魂问题：**每一步的环境是否有保存？能否离线回收？**
> 实测样本：`sar_orch/results/20260721_201943_s3_s42_a4/`（scene3, 4 agents, seed42, 30 步, max_steps_reached，results 下最新完整 run）。

## 0. 范围与代码入口（类/模块 + file:line）

| 组件 | 位置 |
|---|---|
| 仿真引擎 | `SAR/env.py`（`generate_obs_text` :282-361，`get_agent_state` :363-378）；`SAR/base_env.py` |
| 同步屏障 | `sar_orch/barrier.py`（`SARBarrier` :82；`get_env_snapshot` :327-373；`drain_step_logs` :387-400；`_execute_step` 日志缓冲 :455-592，尤其 :570-586） |
| 实验日志 | `sar_orch/logger.py`（`ExperimentLogger.log_step` :133-204 → trajectory.csv；`set_end_reason` :206-241） |
| 实验主循环 | `sar_orch/experiment.py`（poll 循环 :762-895，log_step 调用 :813-850，truth recorder 触发 :858-860/:944-965，run_metrics :995-1003） |
| 语义地图 | `sar_orch/map/store.py`（`SemanticMapStore` :157；`update_step_budget` :212；`snapshot_with_revision` :358；`ingest_observation` :275-344；`_append_jsonl_locked` :703-715） |
| SSE 快照 | `src/a2a/coordinator/server.py`（`set_barrier` :566，`/map/state` SSE :2234-2285；`set_semantic_map` :1022） |
| 状态提供器 | `sar_orch/coordinator_state_provider.py`（oracle 模式 `global_snapshot` :593-598） |
| 真值记录 | `sar_orch/eval/truth_recorder.py`（`TruthRecorder` :134；`record_step` :184；`_claims_for_step` :209-237） |
| 元数据/指标 | `sar_orch/logger.py`（`write_metadata` :362-371）、`experiment.py`（run_metrics :995-1003, main :1220-1224） |

## 1. 轨迹产物清单

| 产物(文件/存储) | 内容字段 | 触发点 file:line | 写入方式/粒度 | 保留策略 | 实际样例路径 |
|---|---|---|---|---|---|
| `trajectory.csv` | Step, Actions(每agent action字符串), Successes, Observations(每agent**文本观察全文**), Coverage, TransportRate, Finished, MapRecall, Freshness, TimeoutAgents, RunID, MaxSteps, RemainingSteps, WallTimeSinceStart, StepDurationMs, ErrorTypes, CompletedSubtasksDelta, EndReason | experiment.py:813-850 每 poll 循环 drain_step_logs → logger.py:177-196 | CSV append+flush，每 env step 1 行 | 常驻 results，EndReason 终态回填重写（logger.py:206-241） | `sar_orch/results/20260721_201943_s3_s42_a4/trajectory.csv`（31 行=1 header+30 步） |
| `agent_interactions.csv` | Step, Agent, ToolName, ToolArgs, Action, Observation(同文本观察), LLMInput, LLMOutput, Thinking, RunID, CorrelationID, EventType, ToolLatencyMs, ErrorType | worker.py:322-345 / coordinator.py:438-446 → logger.py:292-307 | CSV append，每次工具调用 1 行 | 常驻 results | 同 run 目录，441 行 |
| `router_interactions.csv` | Step, Subtask, AssignedTo, RunID, CorrelationID, WorkerTaskID, EventType, Success, ErrorType | coordinator.py:369-446 → logger.py:465-474 | CSV append，每次调度 1 行 | 常驻 | 同 run 目录 |
| `events.ndjson` | timestamp, event_type(assign_task/cancel_task/send_message), run_id, payload | coordinator.py:154-272 → logger.py:377-401 | NDJSON append，每次消息 1 条 | 常驻 | 同 run 目录（实测仅 3 类调度事件，无环境状态） |
| `semantic_map.jsonl` | `{ts, event_type:"observation_ingested", observation{reporter,step,object_type,name,position,attributes}, object{合并后全量 to_dict 含 last_seen_step/field_last_seen_steps}}` | store.py:313-315/:340-343（ingest_observation 内） | NDJSON append，仅**值得记录的观察**（is_new or noteworthy, store.py:334-336） | 常驻；**非每 run 必产物**（最新 run 20260721 无此文件，07-19 run 有） | `sar_orch/results/20260719_200429_s5_s42_a2/semantic_map.jsonl`（31 条全部 observation_ingested） |
| `run_metrics.json` | finished, steps, coverage, transport_rate, elapsed_seconds, end_reason, run_id, max_steps, log_dir | experiment.py:995-1003（先写）/ :1220-1224（main 重写） | JSON overwrite，run 终态 | 常驻 | 同 run 目录 |
| `metadata.json` | run_id, env_name, scenario_id, scene, seed, agent_count, model, provider, api_base, max_steps, wall_clock_timeout, sandbox_profile, task_objective, success_criteria, prompts, code_commit | experiment.py:499-517 → logger.py:362-371 | JSON overwrite，run 启动 | 常驻 | 同 run 目录 |
| `truth_trace.jsonl` / `truth_manifest.json` | 每 step 每有名字对象逐字段 claim：`{step, domain, entity_id, field, value}`（position/inventory/status/load/fire_type/average_intensity/intensity/parent_fire/resource_type/deposit supplies） | truth_recorder.py:184-205（每步），:384-407（终态 manifest） | JSONL append，每 env step 一批 claims | **输出目录强制在 run results 外**（experiment.py:476-485），evaluator-private；opt-in（`--truth-output-dir`） | **全仓零产物**（find 无任何 truth_trace 文件——从未在真实 run 启用） |
| `summary.csv` | ExperimentName, LogDir, TotalSteps, FinalCoverage, FinalTransportRate, Finished, Token 累计 | logger.py:677-727 | CSV overwrite，每 poll flush + close | 常驻 | 同 run 目录 |
| benchmark 侧 | meta.json / result.json / stdout.log / stderr.log / error.log / progress.json / index.json | benchmark.py:286/:357/:376/:341/:456/:474/:67-75/:738 | 子进程实验 + 聚合 | 常驻 | `sar_orch/results/benchmark/scene_1/agents_2/seed_10/`（experiment_logs/ 内仅 metadata/run_metrics/summary/trajectory 4 件） |

## 2. 三问回答

### Q1 本组件是否有完整轨迹记录？（部分）

**有"行为轨迹"（每步 action+文本观察+聚合指标），无"状态轨迹"（全量环境状态）。**

- 每步完整记录：Actions / Successes / Observations（文本全文）/ Coverage / TransportRate / Finished / MapRecall / Freshness / TimeoutAgents / ErrorTypes / CompletedSubtasksDelta（trajectory.csv，logger.py:177-196）。
- Observations 是 `generate_obs_text` 的**文本快照**（SAR/env.py:282-361），含：agent 自身坐标+库存（`get_agent_state` :377）、邻域 5 格（局部观测）、Globally 可见物体列表（全局观测）。这是"观察文本"，不是"真值状态"。
- 聚合指标（coverage/transport_rate）来自 checker（barrier.py:582-583），每步 1 值。
- 证据：实测 trajectory.csv 30 行字段与上表一致；benchmark seed_10 的 experiment_logs 只有 4 件产物（连 trajectory 都只有 1 行数据，属 abort run）。

### Q2 交互记录是否保存？（环境侧=action 提交/observation 返回的逐步记录，逐项）

| 交互 | 保存？ | 证据 |
|---|---|---|
| action 提交（每 agent 每步字符串） | 有 | trajectory.csv Actions 列（logger.py:179）；agent_interactions.csv Action 列（logger.py:295） |
| action 成功/失败标志 | 有 | Successes 列（logger.py:180）、ErrorType(s) 列（logger.py:193） |
| observation 返回（每 agent 每步文本） | 有 | trajectory.csv Observations 列（logger.py:181） |
| 结构化 observation（position/inventory/可见对象列表） | 无独立列；仅实时通道（barrier `_current_structured_obs` :117）与 semantic_map.jsonl 的 observation_ingested 事件（store.py:313-315） | structured obs 不直接落盘 |
| barrier 超时/NoOp 填充 | 有 | TimeoutAgents 列（logger.py:187） |
| 每步耗时/墙钟 | 有 | StepDurationMs / WallTimeSinceStart（logger.py:192-193） |

### Q3 每一步的环境状态是否保存？（本卡主问：逐类状态 有/部分/无 + 证据）

| 状态类 | 结论 | 证据与说明 |
|---|---|---|
| agent 位置 | **有**（每步每 agent） | 文本观察 "I am at co-ordinates: (x,y,z)"（SAR/env.py:377，注入 barrier `full_obs` barrier.py:552）；实测 30 步×4 agents 全部可提取 |
| agent inventory | **有**（每步每 agent） | "I am holding {'Sand':0,'Water':0,'Person':0}"（同上）；实测逐步可提取 |
| fire 平均强度 | **部分**（枚举 Low/Medium/High/None + fire_type，仅可见时） | Globally 列表（SAR/env.py:354）；实测 EmberFire Low→None 的熄灭演化可重建；但扑灭后 Region 从列表消失，且**无数值强度** |
| flammable 单元格（_Region）强度 | **部分**（可见时枚举） | 同 Globally 列表；熄灭后消失（实测 step11 后 EmberFire_Region_1 从列表消失） |
| persons 位置 | **无** | 文本观察仅 "LostPersonThomas requiring 2 agents to carry"（load），**从不含坐标**；get_env_snapshot 有 position（barrier.py:360-363）但无落盘路径 |
| persons 状态（Grounded/Trapped/Rescued） | **无**（仅偶发 "LostPersonThomas at DepositFacility" 文本） | 同上；truth_recorder 会记录 status 但未启用 |
| reservoir 类型 | **部分** | "ReservoirTaj containing Sand" 文本有类型；**剩余水量/容量从未落盘**（get_env_snapshot 也无此字段 barrier.py:364-366） |
| deposit 库存 | **有** | "DepositFacility containing {'Sand':0,'Water':0,'Person':0}"（实测 step30 变 Person:3 可追溯） |
| 全量网格地形/每格状态 | **无** | 只有邻域 5 格文本（SAR/env.py:351-352），无全地图格状态 |
| 完整 grid 快照（fires/persons/agents/reservoirs/deposits/flammables 全量对象） | **无（默认）**；opt-in 可落 truth_trace.jsonl | get_env_snapshot 结构齐全（barrier.py:327-373），但消费方只有 SSE/内存/工具（见 Q4）；TruthRecorder 是唯一落盘路径（truth_recorder.py:211），需 `--truth-output-dir`，**全仓无产物** |

#### 离线重建验证（必做实验，实际执行）

**做了什么**：选 `sar_orch/results/20260721_201943_s3_s42_a4/`（30 步×4 agents），只读解析 trajectory.csv（/tmp/a3_replay_probe*.py），尝试回答"第 N 步时 agent X 在什么位置、火 Y 的强度是多少"。

**读到了什么**：
- 第 6 步 agent0 在 (9,17,0)、agent1 (13,10,0)、agent2 (12,10,0)、agent3 (5,20,0) ✓（"I am at co-ordinates" 文本）
- 第 6 步火强度：EmberFire=Low(Chemical)、EmberFire_Region_1=Low(Chemical)、AgniFire=Low(Non-chemical)、AgniFire_Region_1=Low、DowntownFire=Low、DowntownFire_Region_1=Low ✓（Globally 列表）
- 第 11 步：EmberFire 变 None（熄灭演化可见），EmberFire_Region_1 从列表消失
- 第 30 步 deposit：Person:3（被运送进度可见）
- 第 30 步 LostPersonThomas 位置：**不可得**（文本无坐标）

**结论：部分可回收**。
- 证据链完整：trajectory.csv（Observations 列）→ 文本正则提取 → 位置/库存/可见对象状态。
- 缺环（三类状态从未落盘）：
  1. **persons 坐标**（观察文本无、无结构化落盘）——"第 N 步时 person X 在哪"无法回答；
  2. **reservoir 剩余水量**（快照无此字段）——"第 N 步 reservoir 还能供多少水"无法回答；
  3. **全量网格与对象数值强度**（只有枚举+可见时状态）——扑灭后对象从可见列表消失，无法区分"已扑灭"与"未观测"；无法重建无 agent 经过区域的状态。

### Q4 get_env_snapshot() 快照结构、消费方、是否落盘

- **结构**（barrier.py:327-373）：`{agents:[{name,position,inventory}], fires:[{name,position,average_intensity,fire_type}], persons:[{name,position,load,status}], reservoirs:[{name,position,resource_type}], deposits:[{name,position,inventory}], flammables:[{name,position,intensity}]}`（all_objects(expand=True) 全量）。
- **消费方**：
  1. `/map/state` SSE（server.py:2248）—— 每 0.5s 轮询推送，**纯实时、不落盘**（server.py:2234-2285 无任何写文件路径）；
  2. `coordinator_state_provider` oracle 模式（coordinator_state_provider.py:593-598）—— 构建内存 RuntimeState，供 /environment-state 查询，不落盘；
  3. `query_sar_state` 工具（tools/coordinator/query_sar_state.py:28）—— 给 Coordinator agent 查询用，不落盘；
  4. `TruthRecorder._claims_for_step`（truth_recorder.py:211）—— **唯一落盘路径**（truth_trace.jsonl），但 opt-in 且全仓零产物。
- **结论：实时快照（SSE/dashboard 消费的）本身没有任何路径写进 run 产物。** 落盘只发生在"恰好在 run 中启用了 truth recorder"时。

### Q5 semantic_map.jsonl 事件流能否重建任意 step 的语义地图状态？（部分）

- 有版本/步标：每条 `observation_ingested` 带 `observation.step`；合并对象 `object` 带 `last_seen_step` 与逐字段 `field_last_seen_steps`（store.py:109, to_dict :124）；ts 时间戳。
- **能重建**：被观察对象的"观察发生时刻状态"——按 step 顺序重放事件，可恢复每个对象被每次观察更新后的合并状态。
- **不能重建**：
  1. `init_priors`（reservoirs/deposits/agents/rules/step_budget/task_objective）**从不写 JSONL**（coordinator.py:467-472 传入空 priors；store.py:187-210 无 JSONL 事件）——先验缺失；
  2. 非 noteworthy 观察被吞（store.py:334-336：`is_new or _is_observation_noteworthy` 才 append），内存状态更新了但事件没写——**事件流不完整**；
  3. 无周期性全量 snapshot 事件（只有 observation_ingested 一种，实测 31 条全为此类型）——"第 N 步 map 长什么样"需自行重放+外推；
  4. 最新 run（20260721）**无 semantic_map.jsonl**（07-19 run 有）——非必产物，依赖 worker 观察注入路径。
- **结论：不能可靠重建任意 step 的完整语义地图**；只能重建"已观察事件"的演化。

### Q6 truth_recorder 粒度，能否作环境回放真值源？（设计可，实际未启用）

- **粒度**：每 env step（0-based，record_step 收 1-based 步号转 step-1，truth_recorder.py:188-192），每个**有名字**对象逐字段 claim：`{step, domain(embodied/spatial), entity_id(对象名), field, value}`；字段覆盖 position / inventory(规范化资源列表) / status / load / fire_type / average_intensity / intensity / parent_fire / resource_type / deposit supplies(按资源计数)（truth_recorder.py:257-319）。
- **取值**：get_env_snapshot() 为主 + `_resolve_live` 从 live 对象补读快照缺失字段（deposit storage、reservoir type、flammable parent/fire_type，truth_recorder.py:239-255）。
- **边界**：无名字对象跳过（:229-231）；reservoir 仍只有 resource_type **无水量**（:309-314）；agent 无独立"水量"（inventory 即资源列表）。
- **读者**：terminal-only `memory_projection_quality` 评测器（experiment.py:173-254 接线，`--truth-manifest`）；输出目录强制在 run results 外（experiment.py:476-485，evaluator-private，防 worker 运行期读真值）。
- **实测**：全仓 find 无任何 truth_trace.jsonl —— **从未在真实 run 启用**（opt-in 参数 `--truth-output-dir` 无历史使用记录）。
- **结论**：设计上是唯一"每步全量环境状态"落盘器、可作回放真值源（逐字段规范 claim 可重建），但（a）未启用；（b）即使启用仍缺 reservoir 水量、persons 仅位置/status/load 无格级强度数值。

## 3. 与 logging_map.md 对照（逐条）

| logging_map.md 条目 | 状态 | 说明 |
|---|---|---|
| §1 trajectory.csv 字段/触发（logger.py:133-204，experiment.py:808-817） | 一致 | 实测 30 行、字段完全一致；EndReason 回填确认（set_end_reason logger.py:206-241） |
| §2/3/4/5 agent_interactions / router / token / summary / events.ndjson | 一致 | 实测 441 行 agent_interactions；events.ndjson 实测仅 assign_task/cancel_task/send_message |
| §5c metadata.json（experiment.py:499-517） | 一致 | 实测含全部字段 + state_mode/oracle_mode 附加字段 |
| §5d truth trace/manifest（truth_recorder.py） | 一致 | 代码路径、强制外置目录（experiment.py:476-485）确认 |
| §6 run_metrics.json（experiment.py:995-997 / 1220-1224） | 一致 | 实测字段匹配 |
| 语义地图 semantic_map.jsonl（coordinator.py:459-461 + experiment.py:702-709 重定向） | 一致+补充 | 文档未记"仅 noteworthy 观察写事件"（store.py:334-336）与"非每 run 必产物"（07-21 最新 run 无文件） |
| /map/state SSE 与 get_env_snapshot 消费 | 补充 | logging_map.md 未列 SSE 快照链路（server.py:2234-2285）；data_flow.md:388-393 有记录（纯实时，不落盘） |
| benchmark 产物（benchmark.py 各触发点） | 一致 | 实测 seed_10 目录含 meta/result/stdout/stderr + experiment_logs 4 件 |
| experiment_design.md §7「这足以观察基础任务进度，但不足以完整反映性能」 | 一致 | 本卡结论与之一致：**聚合指标完整、环境全量状态不足**；且 §13 已标注「环境是否可比 | 缺少 scene config snapshot」 |

对照统计：一致 8，冲突 0，补充 2（semantic_map.jsonl 事件过滤行为；SSE 快照链路无落盘）。

## 4. Gap 清单

| # | Gap | 证据 file:line | 对"用轨迹探索框架优化"的影响 |
|---|---|---|---|
| 1 | **persons 位置/状态从不落盘**（观察文本无坐标，快照有但无落盘路径） | SAR/env.py:282-361（generate_obs_text 无 person 坐标）；barrier.py:360-363（快照有，无写盘）；trajectory.csv 实测 | 高——救援任务核心进度（人救没救、在哪）无法离线重放 |
| 2 | **reservoir 剩余水量从不记录**（文本/快照/truth 均无） | barrier.py:364-366；truth_recorder.py:309-314 | 中——资源耗竭分析无法做 |
| 3 | **全量 grid/对象状态无逐步持久化**（默认路径下唯一"每步"落盘是文本观察 + 聚合指标） | trajectory.csv（logger.py:177-196）；experiment.py:813-850；SSE 不落盘 server.py:2234-2285 | 高——"第 N 步环境长什么样"只能在文本观察覆盖范围内回答 |
| 4 | **truth recorder 是唯一全量状态落盘路径但从未启用**（opt-in + 零产物） | truth_recorder.py:184-237；experiment.py:476-497；find 全仓 0 个 truth_trace | 高——现成能力未接线，框架优化的状态回放无真值底座 |
| 5 | **semantic_map.jsonl 事件不完整**（非 noteworthy 观察被吞、init_priors 不写、无快照事件、非每 run 必产物） | store.py:334-336, 187-210, 703-715；coordinator.py:467-472 | 中——语义地图状态只能部分重建 |
| 6 | fire/flammable 强度只有枚举且扑灭后从可见列表消失（无法区分"已扑灭"与"未观测"） | SAR/env.py:347,354；实测 step11 后 Region 消失 | 中——火势演化/蔓延分析有盲区 |
| 7 | scene config（初始网格/对象布局）无快照 | experiment_design.md:438 已自标；metadata.json 无 scene 内容 | 中——跨 run 可比性与场景重建缺基准 |

## 5. 初步观察：现有轨迹能支撑哪些框架优化分析

1. **agent 行为轨迹分析**：actions/successes/error_types/超时/耗时逐步完整 → 可做动作成功率、重复动作、NoOp/超时占比、task thrashing（配合 router_interactions/subtasks）。
2. **探索-覆盖分析**：coverage 逐步曲线 + agent 位置序列 → 可做探索路径可视化、coverage_auc（experiment_design.md:276 已有聚合）、冗余探索检测。
3. **火势阶段分析**（弱）：Globally 可见列表能给出火对象熄灭时刻的**下界**（可见时 Low→None 翻转可精确定位熄灭步），但无法重建蔓延过程。
4. **运输任务进度**（部分）：deposit 库存文本逐步可提取 → 运输完成时序可重建（本 run deposit Person:0→3 可追溯）；但中途中转状态（谁扛着人）只能从 agent inventory 重建。
5. **语义地图演化分析**（部分）：semantic_map.jsonl 事件 + snapshot API（store.py:350）可供在线调试，离线只能重放 noteworthy 观察。
6. **truth recorder 一旦启用**（--truth-output-dir）：可得逐 step 全量对象 claims，支撑记忆投影质量评测（memory_projection_quality）与真值级回放——建议作为框架优化分析的数据底座。
