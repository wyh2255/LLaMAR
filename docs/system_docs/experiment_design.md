---
日期: 2026-07-26（2026-09-03 校准对齐代码现状）
文档类型: 实验方案
文档概述: 面向 LLaMAR 多智能体框架的通用实验评测方案，以 SAR 作为首个仿真环境实例，定义测试启动方式、实验流程、指标体系、轨迹记录、日志完整性评估、环境替换接口以及框架与 Prompt 问题的结果归因方法。
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）；核对口径：类/函数名 grep -n，行号以当前工作区实测为准。
---

# LLaMAR 实验评测方案

## 1. 目标与设计原则

本方案用于评估 LLaMAR 多智能体框架在长程任务、多智能体协作、A2A 调度、工具调用、环境交互和 Prompt 策略上的实际表现。SAR 是当前可运行的首个实验环境，但实验设计不应绑定 SAR。更合适的结构是：

- 通用评测协议：定义所有仿真环境都应遵循的 run 生命周期、日志字段、轨迹格式和归因方法。
- 框架评测层：评估 Coordinator、Worker、A2A 通信、Agent ReAct loop、barrier 同步、token 成本和日志链路。
- 环境实例层：将 SAR 的 scene、agent 数量、seed、coverage、transport rate、救援和灭火任务映射到通用指标。

设计原则：

- 可复现：每次实验必须记录环境、scene、seed、agent 数量、模型、Prompt 版本、代码版本和超时配置。
- 可归因：失败不只记录为失败，还要能区分框架问题、Prompt 问题、模型问题、环境问题和预算问题。
- 可替换：SAR 只是一个环境实例，未来接入其他仿真环境时，尽量复用 benchmark、日志、聚合和分析逻辑。
- 可诊断：轨迹不只记录任务进度，还要记录动作来源、动作结果、延迟、错误类型和跨日志关联 ID。

## 2. 当前系统实验能力概览

当前项目已经具备基础实验能力：

- `sar_orch/experiment.py`：单次 SAR 实验入口，启动 `SARBarrier`、Coordinator、多个 Worker，并执行完整任务。
- `sar_orch/benchmark.py`：批量实验入口，支持 scene、agent 数量、seed 的组合 sweep，并支持并发和 run timeout。
- `sar_orch/aggregate.py`：聚合实验结果，输出 scene、agents、seed、steps、balance、coverage、success_rate、transport_rate、end_reason、failure_class、max_steps、elapsed_seconds、run_id、model、prompt_version 等指标。
- `sar_orch/logger.py`：写入 trajectory、agent interactions、router interactions、token usage、summary 等 CSV，以及 metadata.json、events.ndjson、subtasks.csv。
- `docs/system_docs/logging_map.md`：记录当前日志系统的写入点、字段和用途。
- `docs/system_docs/data_flow.md`：记录 context_id、task_id、query、Coordinator、Worker、A2A 和 SARBarrier 的端到端数据流。

当前日志能支撑基础评估，但还不足以完整支持性能归因。已有日志偏向“完成了多少任务”和“调用了哪些工具”，缺少延迟、错误类型、Prompt 版本、环境配置快照、dispatch 到环境动作的关联 ID，以及 subtask 完成时间线。

## 3. 如何开始测试

### 3.1 单次实验

用于验证环境、A2A、LLM、工具和日志链路是否可用。

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="$(pwd):src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42
```

CLI 参数全量清单（`sar_orch/experiment.py` `main()` argparse，实测 1073–1193 行）：

| 参数 | 类型 | 默认值 | 可选值 | 说明 |
| --- | --- | --- | --- | --- |
| `--scene` | int | `1` | 1–5 | SAR 场景编号 |
| `--agents` | int | `2` | 1–6 | 救援机器人数量 |
| `--seed` | int | `42` | — | 随机种子 |
| `--model` | str | `.env` 的 `model`，兜底 `deepseek-v4-flash` | — | LLM 模型（experiment.py:1081–1086） |
| `--provider` | str | `.env` 的 `provider`，兜底 `openai` | — | LLM provider |
| `--api-base` | str | `.env` 的 `api_base`，兜底 `https://api.deepseek.com` | — | API base URL |
| `--max-steps` | int | `None` → 取 scene 的 `task_timeout`（见下） | — | 最大环境步数 |
| `--coordinator-port` | int | `8080` | — | Coordinator 服务器端口 |
| `--agent-base-port` | int | `8191` | — | Worker A2A 基础端口 |
| `--log-dir` | str | `None`（自动生成时间戳目录，见 §3.4） | — | 显式日志目录 |
| `--mode` | str | `semantic` | semantic, oracle | Coordinator 状态源 |
| `--sandbox-profile` | str | `workspace` | off, workspace | 工具沙箱配置（详见 sandbox.md） |
| `--coordinator-prompt` | str | `None` | — | 覆盖下发到 Coordinator 的初始任务文本 |
| `--enable-peer-mail` | flag | `False` | — | 启用签名信封式 peer messaging |
| `--memory-read-mode` | str | `read_port` | legacy, shadow, read_port | 记忆读取模式；H3（@79e20bc，2026-08-10）起默认 `read_port`，`legacy` 保留为回滚目标 |
| `--long-term-mode` | str | `off` | off, shadow, read | run-local 长期记忆模式；`read` 是 G4（2026-08-12）批准的官方可用模式但**非默认** |
| `--truth-manifest` | str | `None` | — | evaluator-private truth manifest（terminal-only memory_projection_quality 评测器） |
| `--truth-trace` | str | `None` | — | manifest 内 truth trace 路径覆盖 |
| `--truth-output-dir` | str | `None` | — | Phase 5 truth recorder 输出目录；**必须位于 run results 目录之外**（运行期强制校验，experiment.py:476–485） |

`max_steps` 默认值来源：`max_steps = max_steps or barrier.env.task_timeout`（experiment.py:435），即不传 `--max-steps` 时取 scene 定义的 `task_timeout`：

| scene | task_timeout | 定义位置 |
| --- | --- | --- |
| 1 | **1200** | `SAR/Scenes/scene_1.py:39` |
| 2 | 35 | `SAR/Scenes/scene_2.py:39` |
| 3 | 35 | `SAR/Scenes/scene_3.py:39` |
| 4 | 35 | `SAR/Scenes/scene_4.py:37` |
| 5 | 35 | `SAR/Scenes/scene_5.py:38` |

wall-clock 兜底：单次 run 的 `wall_clock_limit = 3600.0` 秒（experiment.py:469），poll 循环超时判断在 787 行，写入 metadata 的 `wall_clock_timeout` 字段（95 行），并参与 `classify_end_reason`（121 行）。

诊断通道（System Health）没有独立 CLI 参数：它随 `--long-term-mode != off` 自动接线（coordinator.py:553–599 打开 run-local `DiagnosisMemoryStore`，fail-closed；experiment.py:667–699 `configure_diagnosis_runtime(...)`），触发跟随 rolling 反思每 5 个环境步（`LongTermRuntimeConfig.every_env_step = 5` @ `src/a2a/coordinator/memory/contracts.py:954`，另有 `min_interval_sec=30` 节流 @955；可被仓库根 `long_term.config` 的 `[trigger]` 段覆盖，ini 映射 @969）。

### 3.2 推荐测试顺序

1. Smoke test：`scene=1, agents=1, seed=42`，确认单 agent、环境和日志可用。
2. 协作最小测试：`scene=1, agents=2, seed=42`，确认 Coordinator 能分配任务，Worker 能进入 barrier 并提交动作。
3. 稳定性测试：固定 scene 和 agent 数量，运行 5 个 seed，观察成功率和结果方差。
4. 规模测试：固定 scene 和 seed，比较 `agents=1/2/4/6`，观察协作收益与调度开销。
5. 完整 benchmark：运行所有 scene、agent 数量、seed 组合，得到主结果表。
6. 消融实验：替换 Prompt、模型、Coordinator 查询策略、任务粒度策略或工具集合，定位系统瓶颈。

### 3.3 完整 Benchmark

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="$(pwd):src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 3600
```

完成后聚合：

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="$(pwd):src:$PYTHONPATH" \
  uv run python sar_orch/aggregate.py
```

`--run-timeout` 默认 3600 秒，避免单个卡死 run 阻塞整个 benchmark。

benchmark.py 参数全量清单（argparse 实测 488–534 行）：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--concurrency` | int | `2` | 并发实验数（benchmark.py:490–495） |
| `--retry` | int | `0` | 失败 run 的重试次数 |
| `--resume` | flag | `False` | 跳过已成功的 run，重试失败的 run |
| `--run-timeout` | int | `3600` | 每个 run 的 wall-clock 超时（秒）（benchmark.py:510） |
| `--mode` | str | `semantic` | Coordinator 状态源，choices semantic/oracle |
| `--max-steps` | int | `50` | 基于步数的截断值（benchmark.py:523）；设为 `0` 禁用步数截断 |
| `--scene` | int nargs+ | `None` | 过滤特定 scene，如 `--scene 5` 或 `--scene 1 3 5` |

Sweep 组合：`SCENES=[1..5]`（37 行）、`AGENT_COUNTS=[2,3,4,5]`（38 行）、`SEEDS=[0,10,20,30,40]`（39 行），共 100 runs。子进程透传参数见 291–309 行（含 `--coordinator-port`/`--agent-base-port`/`--log-dir`；`max_steps>0` 与 `mode≠semantic` 才追加对应参数）。

并发端口机制：每个并发 run 分配一个 10 端口块（`_PORT_BLOCK_SIZE=10` @benchmark.py:46，扫描区间 50000–59999 @47–48）。块内 `coordinator_port = base`、`agent_base_port = base + 2`，占用集合为 `[base, base+1] + [base+2 .. base+2+agents-1]`（`_ports_for_block`，134–140 行）；`_find_free_blocks`（143–157 行）逐块探测空闲，凑不够则 RuntimeError。worker 取到 base 后、启动子进程前再做一次逐一验证（race-guard，248–260 行），冲突则该 run 标 failed（error=`Port conflict: [...]`）。

### 3.4 输出目录与文件

单次实验输出目录：`sar_orch/results/{YYYYMMDD_HHMMSS}_s{scene}_s{seed}_a{agents}/`（`_RESULTS_ROOT` @experiment.py:47，命名规则 440–442 行；`--log-dir` 显式指定时以其为准）。内部子目录（448–459 行）：

```text
sar_orch/results/<ts>_s1_s42_a2/
├── coordinator/          # Coordinator 侧日志（含 memory/diagnosis sqlite、events_*.ndjson）
├── workers/<AgentName>/  # 每个 Worker 的独立日志目录
├── supervision/          # 监督事件
├── run_metrics.json      # 995–997 行先写，main() 结束时（1220–1224 行）重写
├── trajectory.csv / agent_interactions.csv / router_interactions.csv /
│   token_usage.csv / summary.csv / metadata.json / events.ndjson / subtasks.csv
```

benchmark 输出布局：`sar_orch/results/benchmark/scene_{S}/agents_{A}/seed_{N}/`（benchmark.py:208–215），每个 run 目录含 `meta.json`（286 行，仅 scene/agents/seed 三字段）、`result.json`（357/414 行）、`stdout.log`（376/456 行）、`stderr.log`（341/378/458 行）、失败时 `error.log`（474 行）。全局状态文件：`benchmark/progress.json`（原子写 tmp→rename，67–75 行）与全部结束后 `benchmark/index.json`（738 行，字段 scene/agents/seed/status/elapsed/error/log_dir）。

聚合输出：`aggregate.py` 默认读 `sar_orch/results/benchmark`（18 行），写 `sar_orch/results/benchmark_aggregated.tsv`（19 行），支持 `--input/--output` 覆盖（172–176 行）。TSV 共 15 列（fieldnames @aggregate.py:116–132，DictWriter 写出 134–139 行）：`scene, agents, seed, steps, balance, coverage, success_rate, transport_rate, end_reason, failure_class, max_steps, elapsed_seconds, run_id, model, prompt_version`。

## 4. 实验流程

每个 run 应遵循统一生命周期。

### 4.1 Run 初始化

记录实验元数据：

- `run_id`
- `env_name`
- `scenario_id`
- `seed`
- `agent_count`
- `model`
- `provider`
- `api_base`
- `prompt_version`
- `code_commit`
- `max_steps`
- `wall_clock_timeout`
- `sandbox_profile`
- `task_objective`
- `success_criteria`

SAR 当前任务目标是 `Extinguish all fires and rescue all persons`，成功条件由 SAR checker 判断所有 subtasks 是否完成。

### 4.2 环境启动

SAR 当前由 `SARBarrier` 包装 `SAREnv`，负责多 agent 动作同步。每个 Worker 通过工具调用提交动作，barrier 收齐所有 agent 动作或等待超时后调用 `env.step(actions)`。

对于其他仿真环境，应提供同等能力：重置、执行一步、返回观测、返回快照、返回指标、判断结束、序列化动作、分类错误。

### 4.3 Coordinator 启动

Coordinator 使用 RouterAgent 运行 ReAct loop，主要工具包括：

- `query_sar_state`：查询环境状态（仅 oracle 模式注册）。
- `send_message`：统一派发/激活/回复/取消入口，`message_type` 取 `assign_task`（分配子任务）/`activate_plan_node`（DAG 节点激活）/`reply_to_help`（回应 Worker 的 help request）/`cancel_task` 之一。
- `query_task_events`：查询 Worker 子任务状态和回调事件。
- `finish_task`：结束顶层任务。

实验中应记录 Coordinator 的工具调用频率、任务分配粒度、重复分配、过早结束、查询频率和 token 成本。

### 4.4 Worker 启动

Worker 使用 a2a Worker 框架执行 SAR 工具。Worker 的核心职责是解释 Coordinator 下发的子任务，选择工具，向 barrier 提交环境动作，并将结果通过 A2A callback 返回。

实验中应记录每个 Worker 的：

- 可用工具。
- Prompt 版本。
- 工具调用序列。
- 动作成功率。
- timeout 次数。
- token 成本。
- 无效动作、重复动作、错误目标和过早 NoOp 情况。

### 4.5 逐步执行与记录

每个环境 step 至少记录：

- 当前 step 和剩余预算。
- 每个 agent 的动作。
- 每个动作是否成功。
- 每个 agent 的观察。
- 全局任务进度。
- 探索进度。
- 本步新增完成的 subtask。
- timeout agent。
- 错误类型。
- step wall-clock duration。

框架层同时记录：

- Coordinator dispatch。
- Worker task lifecycle。
- A2A callback。
- LLM latency。
- tool latency。
- token usage。
- context_id、coordinator task_id、worker task_id。

### 4.6 结束判定

结束原因必须明确记录，不应只记录 `finished=true/false`。

推荐 `end_reason` 枚举：

- `success`：环境成功条件满足。
- `max_steps_reached`：环境步数耗尽。
- `wall_clock_timeout`：run 超过 wall-clock 限制。
- `coordinator_finished_early`：Coordinator 调用 finish，但环境未成功。
- `a2a_task_done`：A2A 顶层任务完成。
- `worker_timeout`：一个或多个 Worker 长时间未提交动作。
- `framework_error`：A2A、server、callback、barrier 或 Agent loop 异常。
- `environment_error`：环境 step、checker、动作解析或状态序列化异常。
- `manual_interrupt`：人为中断。

## 5. 主实验矩阵

SAR 主实验矩阵建议如下：

| 因素 | 取值 |
| --- | --- |
| 环境 | SAR |
| Scene | 1, 2, 3, 4, 5 |
| Agents | 2, 3, 4, 5 |
| Seeds | 5 个固定 seed |
| Model | baseline 模型，默认 `deepseek-v4-flash` |
| Prompt | baseline Prompt + 消融版本 |
| Run timeout | 3600 秒 |
| Max steps | scene 默认值，必要时增加 controlled variant |

建议分三组执行：

- 基线实验：固定模型和 Prompt，跑完整 scene-agent-seed 矩阵。
- Prompt 消融：固定模型和环境，比较不同 Prompt 策略。
- 框架消融：固定模型和 Prompt，比较 Coordinator 查询频率、任务粒度、工具可见性、worker 数量等框架策略。

## 6. 指标体系

### 6.1 任务完成指标

| 指标 | 含义 | SAR 映射 |
| --- | --- | --- |
| `success_rate` | 成功 run 占比 | `finished` |
| `objective_progress` | 任务目标完成比例 | `transport_rate` |
| `exploration_progress` | 环境探索比例 | `coverage` |
| `steps_to_success` | 成功所需环境步数 | `Step` |
| `progress_auc` | 进度曲线面积 | 基于每步 `transport_rate` |
| `coverage_auc` | 探索曲线面积 | 基于每步 `coverage` |

### 6.2 效率指标

| 指标 | 含义 |
| --- | --- |
| `wall_clock_seconds` | run 总耗时 |
| `step_duration_avg` | 每个环境 step 平均耗时 |
| `llm_latency_avg` | LLM 平均响应耗时 |
| `tool_latency_avg` | 工具平均执行耗时 |
| `dispatch_latency_avg` | Coordinator dispatch 到 Worker 接收或首个工具调用的耗时 |
| `tokens_per_success` | 每个成功 run 的 token 成本 |
| `tokens_per_progress` | 单位任务进度消耗 token |

### 6.3 协作指标

| 指标 | 含义 |
| --- | --- |
| `agent_action_share` | 每个 agent 的动作占比 |
| `agent_success_rate` | 每个 agent 的动作成功率 |
| `idle_rate` | NoOp 或无有效动作比例 |
| `timeout_rate` | agent 被 barrier 自动 NoOp 的比例 |
| `duplicate_action_rate` | 多个 agent 重复执行相同或等价目标的比例 |
| `conflict_rate` | 多个 agent 在同一资源或目标上产生冲突的比例 |
| `load_balance` | agent 间任务量或 token 消耗均衡程度 |

### 6.4 框架指标

| 指标 | 含义 |
| --- | --- |
| `dispatch_count` | Coordinator 分配子任务次数 |
| `query_state_count` | Coordinator 查询环境状态次数 |
| `query_events_count` | Coordinator 查询 Worker 事件次数 |
| `callback_count` | Worker push callback 数量 |
| `barrier_timeout_count` | barrier 超时次数 |
| `a2a_error_count` | A2A 通信错误数量 |
| `task_state_mismatch_count` | A2A task done 但环境未完成等状态不一致次数 |

### 6.5 Prompt 与模型质量指标

| 指标 | 含义 |
| --- | --- |
| `invalid_tool_call_rate` | 工具名或参数非法比例 |
| `invalid_target_rate` | 目标对象不存在或命名错误比例 |
| `repeated_plan_rate` | 重复规划或重复 dispatch 比例 |
| `premature_finish_rate` | 环境未成功但 Coordinator 调用 finish 的比例 |
| `premature_noop_rate` | Worker 过早 NoOp 的比例 |
| `observation_ignore_rate` | LLM 输出与最新 observation 明显矛盾的比例 |
| `format_violation_rate` | 输出格式不符合工具调用或协议约束的比例 |

## 7. 实验轨迹设计

当前 `trajectory.csv` 包含：

- `Step`
- `Actions`
- `Successes`
- `Observations`
- `Coverage`
- `TransportRate`
- `Finished`
- MapRecall / `Freshness`
- `TimeoutAgents`
- `RunID`
- `MaxSteps` / `RemainingSteps`
- `WallTimeSinceStart` / `StepDurationMs`
- `ErrorTypes` / `CompletedSubtasksDelta`
- `EndReason`

这足以观察基础任务进度，但不足以完整反映性能。建议将轨迹拆成环境轨迹和框架轨迹。

> 现状注（2026-09-03 校准）：下表是通用协议设计字段。当前实现中 `trajectory.csv` 已覆盖大部分环境字段（见 §13 表 #2/#3），但 per-agent 拆分列（`ActionsByAgent` 等）以 JSON 列表形式合并在 `Actions`/`Successes`/`ErrorTypes`/`Observations` 单列中；`ErrorTypeByAgent` 的实际列名为 `ErrorTypes`。框架轨迹字段分散在 `agent_interactions.csv`/`router_interactions.csv`/`events.ndjson` 中，其中 `ContextID`、`CoordinatorTaskID` 两列不存在，跨日志关联由 `CorrelationID`/`WorkerTaskID` 承担（见 §13 表 #4）。

### 7.1 环境轨迹字段

| 字段 | 说明 |
| --- | --- |
| `RunID` | 当前 run 的唯一 ID |
| `EnvName` | 环境名称，如 `SAR` |
| `ScenarioID` | 场景编号 |
| `Seed` | 随机种子 |
| `Step` | 当前环境步数 |
| `MaxSteps` | 最大环境步数 |
| `RemainingSteps` | 剩余环境步数 |
| `WallTimeSinceStart` | 从 run 开始到当前 step 的耗时 |
| `StepDurationMs` | 当前 step 耗时 |
| `ActionsByAgent` | 每个 agent 的动作 |
| `ActionSuccessByAgent` | 每个动作是否成功 |
| `ErrorTypeByAgent` | 每个动作失败类型 |
| `ObservationByAgent` | 每个 agent 的 observation |
| `CompletedSubtasksDelta` | 当前 step 新完成的子任务 |
| `ObjectiveProgress` | 通用任务进度 |
| `ExplorationProgress` | 通用探索进度 |
| `GlobalStateDigest` | 环境状态摘要 |
| `Finished` | 环境是否成功 |
| `EndReason` | run 结束原因 |

### 7.2 框架轨迹字段

| 字段 | 说明 |
| --- | --- |
| `RunID` | 当前 run 的唯一 ID |
| `ContextID` | A2A 上下文 ID |
| `CoordinatorTaskID` | 顶层任务 ID |
| `WorkerTaskID` | Worker 子任务 ID |
| `Step` | 当前环境步数 |
| `Agent` | Coordinator 或 Worker 名称 |
| `EventType` | dispatch、tool_call、callback、llm_call、barrier_submit 等 |
| `ToolName` | 工具名 |
| `ToolArgs` | 工具参数 |
| `AssignedAgent` | 被分配任务的 agent |
| `SubtaskText` | Coordinator 下发的子任务文本 |
| `LLMLatencyMs` | LLM 响应耗时 |
| `ToolLatencyMs` | 工具执行耗时 |
| `DispatchLatencyMs` | dispatch 链路耗时 |
| `PromptTokens` | prompt token 数 |
| `CompletionTokens` | completion token 数 |
| `CacheHitTokens` | prompt cache 命中 token 数 |
| `CacheMissTokens` | prompt cache 未命中 token 数 |
| `PromptVersion` | Prompt 版本 |
| `FailureClass` | 初步失败归因类别 |

### 7.3 文件组织（现状）

当前实现保留 CSV 主文件并已完成字段扩展（2026-09-03 校准）：

- `trajectory.csv`：每步环境轨迹主表，含 MaxSteps/RemainingSteps/WallTimeSinceStart/StepDurationMs/ErrorTypes/CompletedSubtasksDelta/EndReason。
- `agent_interactions.csv`：Worker 与 Coordinator 工具调用，含 `RunID`、`CorrelationID`、`ToolLatencyMs`、`ErrorType`。
- `router_interactions.csv`：Coordinator 调度，含 `RunID`、`CorrelationID`、`WorkerTaskID`、`Success`、`ErrorType`。
- `token_usage.csv`：token 用量，含 `LLMLatencyMs`、`Model`、`PromptVersion`。
- `summary.csv`：crash-safe 最新摘要（覆盖写）。
- `metadata.json`：run 元数据、Prompt 版本、代码 commit（`build_run_metadata`，见 §13 表 #1）。
- `subtasks.csv`：subtask 分配/状态时间线（见 §13 表 #6）。
- `events.ndjson`：统一事件流（timestamp/event_type/run_id/payload），作为跨 CSV 关联和 debug 的事实来源（见 §13 表 #7）。

## 8. 当前日志完整性评估

### 8.1 当前日志能回答的问题

| 问题 | 当前是否支持 | 依据 |
| --- | --- | --- |
| 任务是否完成 | 支持 | `run_metrics.json`、`summary.csv`、`trajectory.csv` |
| 最终 coverage 和 transport rate | 支持 | `trajectory.csv`、`summary.csv` |
| 每步执行了什么动作 | 支持 | `trajectory.csv` |
| 哪些 agent 被 timeout 自动 NoOp | 支持 | `TimeoutAgents` |
| Coordinator 分配了哪些任务 | 部分支持 | `router_interactions.csv` |
| Worker 调用了哪些工具 | 支持 | `agent_interactions.csv` |
| token 成本是多少 | 支持 | `token_usage.csv`、`summary.csv` |
| 崩溃时是否保留最新摘要 | 支持 | `summary.csv` 每轮刷新 |

### 8.2 当前日志不能充分回答的问题

| 问题 | 当前缺口 |
| --- | --- |
| 慢在哪里 | 缺少 LLM、tool、dispatch、step latency |
| 为什么动作失败 | 缺少环境 `error_type` 和失败分类 |
| dispatch 是否成功到达 worker | 缺少贯穿 Coordinator、A2A、Worker、barrier 的 correlation id |
| Prompt 改动是否影响结果 | 缺少 Prompt 版本、Prompt hash 和完整实验元数据 |
| 任务是如何逐步完成的 | 缺少 subtask completion timeline |
| Worker 是否因为模型慢而 timeout | `TimeoutAgents` 只有 agent index，没有原因 |
| Coordinator 是否过早 finish | 需要将 A2A task 状态与环境 success 明确关联 |
| 哪个 agent 对进度贡献最大 | 缺少 per-agent progress attribution |
| 环境是否可比 | 缺少 scene config snapshot 和难度摘要 |

结论：当前日志可以反映基础任务表现，但还不能完整反映系统性能，尤其不利于定位框架瓶颈、Prompt 缺陷和环境设计问题。

## 9. 仿真环境可替代性设计

为了让实验系统不绑定 SAR，建议定义统一环境适配接口。不同环境只需要实现该接口，即可复用同一套 Coordinator、Worker、benchmark、logger 和分析脚本。

```text
EnvironmentAdapter
- env_name
- scenario_id
- seed
- max_steps
- reset()
- step(actions_by_agent)
- get_snapshot()
- get_metrics()
- is_finished()
- get_success_criteria()
- serialize_action(action)
- classify_error(result)
```

通用 `get_metrics()` 至少返回：

```text
Metrics
- step_count
- max_steps
- objective_progress
- exploration_progress
- success
- done
- end_reason
```

通用 `step()` 返回：

```text
StepResult
- observations_by_agent
- success_by_agent
- error_type_by_agent
- completed_subtasks_delta
- global_state_digest
- raw_snapshot_ref
```

SAR 映射关系：

| 通用字段 | SAR 当前字段 |
| --- | --- |
| `objective_progress` | `transport_rate` |
| `exploration_progress` | `coverage` |
| `success` | `finished` |
| `step_count` | `Step` |
| `success_by_agent` | `Successes` |
| `observations_by_agent` | `Observations` |
| `timeout_agents` | `TimeoutAgents` |
| `snapshot` | `get_env_snapshot()` |

需要补充的 SAR 映射：

- 将 `SAREnv` 动作解析和执行结果中的 `error_type` 暴露给 logger。
- 将 checker 中 subtask 完成变化记录为 `completed_subtasks_delta`。
- 将 scene 参数摘要写入 `metadata.json`。
- 将 `max_steps` 和剩余步数写入每步轨迹。

## 10. 结果分析与问题归因

实验结果不应只按成功或失败统计。每个失败 run 应尽量归为一个主因和若干次因。

### 10.1 框架问题分析

框架问题通常表现为：任务策略看似合理，但系统链路没有稳定执行。

分析路径：

1. 从 `run_metrics.json` 找出失败 run。
2. 查看 `trajectory.csv` 的 `TimeoutAgents` 是否频繁出现。
3. 查看 `router_interactions.csv` 是否存在 dispatch 后 worker 无响应。
4. 查看 `agent_interactions.csv` 是否有 worker LLM 输出但没有对应环境动作。
5. 查看 NDJSON 日志中的 A2A task lifecycle、push callback、EventStore 事件。
6. 对比 A2A task done 状态与环境 `Finished` 状态是否一致。
7. 如果补充 latency 字段，则定位瓶颈在 LLM、tool、A2A、barrier 还是 environment step。

典型信号：

| 信号 | 可能原因 |
| --- | --- |
| 多个 agent timeout | Worker 卡住、LLM 慢、barrier 等待、A2A 回调异常 |
| Coordinator 反复 query 但不 dispatch | Coordinator 状态机或 Prompt 策略问题 |
| 有 dispatch 但无 Worker 工具调用 | A2A dispatch、worker task 或 callback 链路问题 |
| Worker 有 LLM 输出但无环境 step | 工具执行、barrier submit 或 action serialization 问题 |
| A2A task done 但环境未 finished | 框架结束条件和环境成功条件不一致 |

### 10.2 Prompt 问题分析

Prompt 问题通常表现为：框架链路正常，但智能体决策质量差。

分析路径：

1. 查看 `agent_interactions.csv` 中工具调用和参数。
2. 查看 `router_interactions.csv` 中 Coordinator 的任务粒度和重复分配。
3. 对照 `trajectory.csv` 的动作成功率、重复动作、NoOp、timeout。
4. 抽样查看 NDJSON 中完整 LLM 输入输出，判断是否忽略 observation、误解任务状态或违反工具约束。
5. 与 Prompt 消融版本对比，观察成功率、步数、token、invalid action、repeated action 的变化。

典型信号：

| 现象 | 可能 Prompt 问题 |
| --- | --- |
| 重复 NavigateTo 同一目标 | 缺少目标完成判定或状态记忆约束 |
| 无效目标名或错误工具参数 | 工具 schema 和对象命名约束不足 |
| Worker 长时间 NoOp | 任务完成判断过早或 observation 解释失败 |
| Coordinator 分配任务过小 | 调度粒度太细，A2A 开销过大 |
| Coordinator 分配任务过大 | Worker 执行链太长，容易 timeout 或偏航 |
| 反复 query_sar_state | Coordinator 缺少计划持久化或不信任 worker 反馈 |
| 消防和救援顺序不合理 | Prompt 缺少资源、优先级和依赖关系说明 |

### 10.3 模型问题分析

模型问题通常表现为：同一 Prompt 和环境下，不同模型出现明显稳定性差异。

建议观察：

- 格式遵循率。
- 工具参数合法率。
- 长上下文下是否遗忘任务。
- token 成本与成功率的性价比。
- cache hit rate。
- LLM latency 和 timeout 关联。

### 10.4 环境问题分析

环境问题通常表现为：agent 决策合理，但任务定义、动作抽象或 checker 使结果失真。

SAR 当前需要注意：

- `NavigateTo` 是直接定位，移动成本被弱化，不能代表真实路径规划能力。
- `coverage` 基于对象是否被 action 命名，可能高估探索能力。
- `transport_rate` 来自 rule-based checker 的 subtasks，适合作为 SAR 任务完成代理，但不是通用规划质量指标。
- Scene 1 的 `task_timeout=1200` 步与 run 级 wall-clock 3600 秒预算下，2 agents + A2A + LLM overhead 仍可能偏紧，可能还没进入完整消防阶段就接近预算。
- timeout 和 max_steps 同时影响结果，必须同时记录 wall-clock 和环境步数。

### 10.5 预算问题分析

预算问题不是框架或 Prompt 本身失败，而是给定时间或步数不足。

判断依据：

- 进度曲线持续上升，但到达 `max_steps` 或 `wall_clock_timeout`。
- 无明显 invalid action 或 dispatch 断链。
- 增加 `max_steps` 或 timeout 后成功率显著提升。
- 多 agent 下 wall-clock timeout 增多但环境 step 不多，说明调度和 LLM overhead 成为主要预算压力。

## 11. Prompt 消融实验建议

建议至少设计以下 Prompt 版本：

| 版本 | 改动 | 观察指标 |
| --- | --- | --- |
| `baseline` | 当前 Prompt | 主实验对照 |
| `tool_schema_strict` | 强化工具参数、对象命名、非法目标禁止 | invalid tool/target rate |
| `step_budget_aware` | 强化剩余步数和 wall-clock 意识 | steps_to_success、timeout rate |
| `task_completion_guard` | 强化完成条件，禁止过早 NoOp/finish | premature noop/finish rate |
| `coordinator_granularity` | 约束 Coordinator 子任务长度和粒度 | dispatch_count、worker timeout、token cost |
| `state_memory` | 强化已完成目标、已探索目标和资源状态记忆 | repeated action rate、progress_auc |

Prompt 消融需要记录 Prompt 文件路径、hash、版本名和关键差异说明，否则结果不可复现。

## 12. 报告输出建议

最终实验报告应至少包含以下图表或表格：

- 成功率按 scene 和 agent 数量分组。
- 最终 `transport_rate` 和 `coverage` 分布。
- `transport_rate` 随 step 的曲线。
- `coverage` 随 step 的曲线。
- token 成本按 agent 和 Coordinator 分解。
- timeout rate 按 agent 和 scene 分解。
- dispatch count、query count 与成功率的关系。
- invalid action、repeated action、premature finish 的比例。
- failure taxonomy：framework、prompt、model、environment、budget、unknown。
- 代表性成功 run 和失败 run 的轨迹案例分析。

## 13. 最小补充实现清单（已实现）

以下观测性项已按 `docs/plans/2026-07-05-experiment-observability-improvements.md` 落地，逐项现状与代码证据：

| # | 条目 | 现状 | 代码证据 |
| --- | --- | --- | --- |
| 1 | `metadata.json` | ✅ 已实现 | `sar_orch/logger.py:362–371` `write_metadata`；字段清单由 `sar_orch/experiment.py:68–103` `build_run_metadata` 构造（run_id/env_name/scenario_id/seed/agent_count/model/provider/api_base/max_steps/wall_clock_timeout/sandbox_profile/prompt_version/code_commit/task_objective/success_criteria/prompts）；调用点 experiment.py:499–517 |
| 2 | trajectory 增 `MaxSteps`/`RemainingSteps`/`WallTimeSinceStart`/`StepDurationMs`/`EndReason` | ✅ 全部存在 | `logger.py:189–195`（写行）+ 230–236（header）；`EndReason` 终态回填 `set_end_reason` @logger.py:206–214 |
| 3 | 环境动作错误类型 | ✅ 已实现（列名为 `ErrorTypes`） | trajectory.csv 实际列名是 **`ErrorTypes`**（per-agent 列表，logger.py:193/234）；agent/router_interactions 另有 `ErrorType` 列（:306/:474）。字面 `ErrorTypeByAgent` 列不存在 |
| 4 | interactions 增关联 ID | ✅ 大部分已实现 | agent_interactions：`RunID`+`CorrelationID`（logger.py:301–302，header :622–623）；router_interactions：`RunID`+`CorrelationID`+`WorkerTaskID`（:469–471，header :633–635）。correlation id 生成点：worker.py:312（`{agent}-tool-{seq}`）、coordinator.py:171（`coordinator-dispatch-{seq}`）。⚠ `ContextID`、`CoordinatorTaskID` 两列**不存在**，跨日志关联以 `CorrelationID`/`WorkerTaskID` 承担 |
| 5 | token_usage 增 `LLMLatencyMs`/`Model`/`PromptVersion` | ✅ 已实现 | logger.py:523–526（写行）+ header :646–651 |
| 6 | `subtasks.csv` | ✅ 已实现 | logger.py:406–430 `log_subtask`（RunID/Step/SubtaskID/Status/AssignedTo/Subtask/CreatedAt/UpdatedAt/FailureClass/Details）；调用点 coordinator.py:181 |
| 7 | 统一 `events.ndjson` | ✅ 已实现 | logger.py:383–401 `log_event`（统一 schema：timestamp/event_type/run_id/payload）；调用点 coordinator.py:188/215/240/254/266 |

## 14. 实施状态

实验可观测性改进已按 `docs/plans/2026-07-05-experiment-observability-improvements.md` 实施。新增输出包括 `metadata.json`、`events.ndjson`、`subtasks.csv`，并扩展 `trajectory.csv`、`agent_interactions.csv`、`router_interactions.csv`、`token_usage.csv` 和聚合 TSV 字段。

8 月记忆/诊断子系统验收证据（详见 `docs/system_docs/memory.md`）：

- H3（2026-08-10）：legacy 主路径退役、默认切 `read_port`，10-run 矩阵 10/10 both-pass（memory.md:22；commit 79e20bc）；
- G0–G4 审批链（2026-08-12）：长期记忆正式可用，read 10-run 矩阵 10/10，avg coverage 0.781 / transport rate 0.783（memory.md:505/512）；
- P5（2026-08-16）：System Health 诊断通道收口——真实模型 smoke 3/3 + read 10-run 矩阵 10/10（memory.md:524；commit 42ac461）。复跑材料：`sar_orch/run_g3_read_matrix.sh`（G3 read 矩阵 runner）与 `sar_orch/scripts/diagnosis_smoke.py`（R2 gate 材料，exit 0 仅当 3/3 通过）。

## 15. 总结

当前 LLaMAR 已具备运行 SAR 单次实验、完整 benchmark、基础 CSV 日志和聚合分析的能力。现有记录可以回答“是否完成、完成到什么程度、调用了哪些工具、消耗多少 token”，但还不能充分回答“为什么失败、慢在哪里、框架和 Prompt 各自贡献了什么问题”。

推荐将后续实验体系升级为“通用评测协议 + SAR 实例化”：SAR 继续作为主要验证环境，但指标、轨迹、日志和归因方法都按照环境无关方式设计。这样既能支撑当前 SAR 实验分析，也能为后续替换仿真环境保留一致的评测框架。
