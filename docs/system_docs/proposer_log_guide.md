---
日期: 2026-09-09
文档类型: 诊断指南（runbook）
文档概述: 面向自进化 proposer 的 SAR run 日志速查手册 — 症状 → 文件 → 字段 → 判读标准，
   用于在一次实验结束后快速定位问题根因（调度、行为、系统、数据链四类）。
校准基线: main@79bd95b；以 smoke run `20260909_232036_s3_s42_a4`（scene3/seed42/4agents/30步）实测产物为准。
   字段级真源见 `docs/system_docs/logging_map.md`，数据流见 `docs/system_docs/data_flow.md`。
---

# Proposer 日志诊断指南

> 目标读者：自进化循环里的 proposer / 诊断者。读完一次 run 的产出后，应能回答三件事：
> **结局是否正常 → 异常从哪一步开始 → 根因在哪一层（环境 / worker 行为 / coordinator 调度 / 基础设施）。**

## 0. 三十秒版

```
1. run_metrics.json   → end_reason / finished / coverage / transport_rate   （结局）
2. summary.csv        → 终值指标 + 各 agent token 总量                       （花费）
3. trajectory.csv     → 按 Step 看 Actions / Successes / NoOpSource 曲线     （过程）
4. 按 §2 症状索引表下钻到具体文件
```

run 目录：`sar_orch/results/{YYYYMMDD_HHMMSS}_s{scene}_s{seed}_a{agents}/`。
truth（评测真值）在目录**外**：`sar_orch/results/truth/<run_dir_basename>-<run_id末段uuid8>/`（如 `20260909_232036_s3_s42_a4-sar-scene3-agents4-seed42-099a58c3/`），metadata.json 的 `truth_dir` 字段记录路径。命名带 run_id 末段 uuid8（2026-09-10 审计 A4 §2 修复后规则），保证多 run 不共享目录；修复前直接取 basename（如 `truth/seed_0/`）会导致多 run trace 混合，属**已修复缺陷**。

---

## 1. Run 目录地图

| 路径 | 内容 | 什么时候看 |
|------|------|-----------|
| `metadata.json` | 场景/种子/模型/prompt 指纹/code_commit/truth_dir | 复现条件核对；判断两个 run 是否可比 |
| `run_metrics.json` | 结局：finished、end_reason、coverage、transport_rate、耗时 | **第一眼看这里** |
| `summary.csv` | 单行汇总：终值指标 + 每个 agent 的 token 累计 | 花费与产出比 |
| `trajectory.csv` | 每个环境 step 一行：动作/成败/观测/覆盖率/NoOp 来源 | 过程曲线、找异常步 |
| `agent_interactions.csv` | 每次工具调用一行（worker 全部工具 + coordinator 的 query_sar_state） | 单个 agent 行为审计 |
| `router_interactions.csv` | coordinator 每次工具调用（四类 dispatch + update_plan/finish_task/query_*） | 调度决策审计 |
| `subtasks.csv` | 子任务生命周期：assigned / canceled / mission 终态 | 任务是否派发、是否被收回、结局 |
| `token_usage.csv` | **每次 LLM 请求一行**：token、缓存命中、Status(ok/error) | LLM 报错、token 膨胀 |
| `events.ndjson` | coordinator 语义事件流：assign_task / cancel_task / send_message | 调度事件时序 |
| `scene_config.json` | 初始网格布局（step 前落盘一次，不再变） | 核对初始条件、对象清单 |
| `semantic_map.jsonl` | 语义地图观测摄入事件 | 观测链是否工作 |
| `coordinator/<task_id>.ndjson`（uuid 命名） | coordinator ReAct **全量**轨迹（AgentLogger：llm_request 带完整 messages） | coordinator 到底"想了什么"——读这个 |
| `coordinator/<YYYYMMDD_HHMMSS>.ndjson`（时间戳命名） | TaskLogger 任务生命周期轨迹（llm_response/tool_result 内容截断 2000/3000 字符，**无完整 messages**） | 任务生命周期事件时序 |
| `coordinator/events_<task_id\|dsp_id>.ndjson` | EventStore 事件流（push 回调、supervision 事件） | worker 回报/求助/告警时序 |
| `coordinator/mission_graph.jsonl` | MissionGraph 声明快照 | plan 结构审计 |
| `coordinator/long_term/long_term.sqlite3` | 长期记忆库（**仅 `--long-term-mode != off` 时存在**，默认 off） | 长期记忆发布内容 |
| `coordinator/diagnosis/` | diagnosis.sqlite3 + transcripts.ndjson 系统健康诊断（随 long-term 自动接线，无独立开关） | 诊断循环结论与证据 |
| `coordinator/reflection_trace.ndjson` | rolling 反思轨迹（同上，仅 long-term 开启时） | 反思循环审计 |
| `workers/<Name>/<Name>/<task_id>.ndjson` | 该 worker 每个 dispatch 的 ReAct 全轨迹 | worker 到底"想了什么" |
| `workers/<Name>/<Name>/context/` | prune_events.ndjson + discards.ndjson（上下文裁剪审计） | 上下文被裁导致的行为异常 |
| `supervision/supervision_<dsp_id>.ndjson` | 每个 dispatch 的 watchdog 状态快照（每刷新一行） | 任务停滞/失联告警 |
| `sar_orch/results/truth/<run>/` | truth_trace.jsonl + truth_manifest.json（agent 不可见） | 评测口径核对（proposer 可读）；目录名 = `<run_dir_basename>-<run_id末段uuid8>`（修复后规则） |

旧 run 可能没有新列（如 `NoOpSource`、`Status`）——以当前代码表头为准，本文 §3 列的是当前格式。

---

## 2. 症状 → 文件 快速定位索引

### A. 结局类

| 症状 | 看哪里 | 判读 |
|------|--------|------|
| run 没跑完 | `run_metrics.json` → `end_reason` | `max_steps_reached`=步数耗尽（正常截断）；`finished`=任务完成；其它值/缺失=异常终止 |
| 指标异常低 | `trajectory.csv` 的 Coverage / TransportRate 列 | 从哪一步开始长期不动 → 去该步看 Actions |
| mission 成败口径 | `subtasks.csv` 里 `SubtaskID=mission` 的行 | Status=completed/failed 是 coordinator 自报；与 run_metrics 的 finished 交叉验证 |
| 怀疑场景本身变了 | `scene_config.json` 与基线 run 对比 | objects 清单/位置/属性是否一致；metadata.json 的 code_commit + prompt 指纹是否一致 |

### B. Worker 行为类

| 症状 | 看哪里 | 判读 |
|------|--------|------|
| agent 空转/不动 | `trajectory.csv` 的 Actions + `NoOpSource` | `llm`=LLM 自己选 NoOp（prompt/决策问题）；`idle_heartbeat`=主任务已完成、等收尾（正常）；`timeout_injected`=60s 内没提交动作（**系统问题，见 D**） |
| 动作反复失败 | `agent_interactions.csv` 筛 `Success=False`，看 `ErrorType` + `Observation` | 同一动作连续失败 → 动作前提不满足（没水灭火、人太重抬不动）；配合 scene_config 核对资源类型 |
| agent 行为"犯傻"（绕路、乱选目标） | `workers/<A>/<A>/<task>.ndjson` 按时间序读 llm_response → tool_start → tool_result | 看 LLM 实际看到的 messages（llm_request 里是完整输入）和它给的理由；`Thinking` 列在 agent_interactions.csv 里也有 |
| 上下文被裁导致失忆 | `workers/<A>/<A>/context/prune_events.ndjson` + `discards.ndjson` | prune_events 有 token_before/after；discards 存被裁原文——对比裁剪前后行为突变点 |
| 某 agent 全程无动作 | `trajectory.csv` TimeoutAgents 列 + `subtasks.csv` 该 agent 是否有 assigned 行 | coordinator 没派活 → 转 C 类；派了但没动作 → 看该 worker ndjson 是否有 llm_response |

### C. Coordinator 调度类

| 症状 | 看哪里 | 判读 |
|------|--------|------|
| coordinator 不派活/派错活 | `router_interactions.csv`（EventType=send_message 系）+ `subtasks.csv` | 每个 step 派了几个任务、派给谁；对照 `coordinator/<task>.ndjson` 的 llm_response 看决策理由 |
| 派了任务又频繁取消 | `events.ndjson` 数 assign_task vs cancel_task；`subtasks.csv` canceled 行 | cancel 风暴通常意味着 plan 不稳定或 watchdog 告警驱动 |
| worker 求助没人理 | `coordinator/events_dsp_*.ndjson` 找 `help_request`；再看 `router_interactions.csv` 有无对应 reply_to_help | 有求助无回复 → coordinator 决策缺陷 |
| coordinator 自己卡住 | `coordinator/<task>.ndjson` 尾部事件类型；`token_usage.csv` 里 Coordinator 行 | 停在 llm_request 无 response → LLM 请求挂起；有 response 无 tool → finish/循环判断问题 |
| plan 结构怀疑 | `coordinator/mission_graph.jsonl` + router_interactions 里 update_plan/activate_plan_node 行 | DAG 节点激活顺序是否符合预期 |

### D. 系统/基础设施类

| 症状 | 看哪里 | 判读 |
|------|--------|------|
| barrier 超时注入 NoOp | `trajectory.csv` NoOpSource 含 `timeout_injected`、TimeoutAgents 非空 | 该 agent 60s 没提交动作 → 它的 worker ndjson 看是否 LLM 响应慢/工具挂起 |
| LLM 请求报错 | `token_usage.csv` 筛 `Status=error`（0 值行） | 集中在某时段 → 网关/配额问题；集中在某 agent → 输入过长或格式问题 |
| token 异常膨胀 | `token_usage.csv` PromptTokens 按 step 看增长；`summary.csv` 各 agent 总量 | 单调暴涨 → 上下文累积；配合 context/prune_events.ndjson 看裁剪是否生效 |
| Cache 命中率为 0 | `token_usage.csv` CacheHitTokens 恒 0 | prompt 前缀不稳定（每轮注入内容位置变化）→ 成本问题 |
| 某 step 耗时突增 | `trajectory.csv` StepDurationMs | 对照该步 agent_interactions 的 ToolLatencyMs 找慢工具 |
| 任务停滞告警 | `supervision/supervision_<dsp>.ndjson` 的 supervision_state / active_alerts | TASK_STALE=进度不刷新；WORKER_UNREACHABLE=失联；注意 progress 只在真实域进展时刷新，纯 NoOp 不算 |

### E. 数据链类（观测/记忆/语义地图）

| 症状 | 看哪里 | 判读 |
|------|--------|------|
| coordinator "看不见"worker 发现的东西 | `semantic_map.jsonl` 是否有摄入事件；`coordinator/events_dsp_*.ndjson` 的 observation_report | worker 调了 report_observation（agent_interactions.csv）但 semantic_map 无事件 → 推送链断；有事件但 coordinator 决策无视 → prompt/决策问题 |
| 记忆系统产物核对 | `run_metrics.json` 的 memory_terminal.acceptance_gate | pass/measured 之外的值 = 记忆链有缺陷；细节在 metadata.json truth_dir 指向的 truth 产物 |
| run 里缺 `long_term/`、`diagnosis/`、`reflection_trace.ndjson`、`long_term_memory_quality.json` | `run_metrics.json` 的 `long_term_reflection.status` 与 `diagnosis` 字段 | 三态判定见下表；「目录存在但库空」≠ 未接线 |
| truth 缺失 | `metadata.json` 的 truth_dir + 该目录 manifest | truth recorder 默认开启，缺失说明 run 异常终止或 legacy 模式 |

**long-term / 诊断接线三态判定矩阵**（2026-09-10 审计 A3 G9 补充；判据 = `run_metrics.json` 的 `long_term_reflection` 块）：

| 状态 | 表现 | 含义 | 磁盘佐证 |
|------|------|------|---------|
| 未接线 | `long_term_reflection.status = "off"` | 启动时没传 `--long-term-mode`（默认 off），不是故障；`coordinator/long_term/`、`coordinator/diagnosis/`、`reflection_trace.ndjson`、`long_term_memory_quality.json` **全部不存在** | 目录缺失 = 未接线 |
| 已接线、未触发 | `status != "off"` 但 `diagnosis = null`（或 `diagnosis` 键缺失） | long-term 已接线，但诊断循环未触发/未落终局字段——滚动反思**每 5 步**才触发诊断循环，短 run（<5 步）属设计内；也可能是 run 末期 rolling 仍在飞、60s drain 超时按 D8 设计不阻塞退出（终局 summary 里 `diagnosis=null`，见 §7 对照 run 实测） | **目录存在但库空**（diagnosis.sqlite3 表在、0 行，transcripts.ndjson 可能不存在）＝已接线未触发，**≠ 未接线** |
| 已触发 | `diagnosis = {status: "timeout" \| "completed", rounds: N, ...}` | 诊断循环至少跑过一轮并落盘（timeout=诊断超时丢写，D8 设计内，以磁盘 transcripts 行数为准） | `diagnosis.sqlite3` 有行、`transcripts.ndjson` 存在 |

判读原则：**先看 `status`（off？）→ 再看 `diagnosis`（null？timeout？completed？）→ 最后落磁盘核对**（库行数 / transcripts 行数），不要只看终局字段——`diagnosis=null` 时磁盘上 transcripts.ndjson 仍可能有轮次记录。

---

## 3. 关键文件字段详解

### 3.1 trajectory.csv —— 每环境步一行

```
Step, Actions, Successes, Observations, Coverage, TransportRate, Finished,
MapRecall, Freshness, TimeoutAgents, NoOpSource, RunID, MaxSteps,
RemainingSteps, WallTimeSinceStart, StepDurationMs, ErrorTypes,
CompletedSubtasksDelta, EndReason
```

- `Actions` / `Successes` / `ErrorTypes` / `NoOpSource` 都是 **per-agent 列表**，顺序与 agent 注册顺序一致（场景 a4 = Alice, Bob, Charlie, David）。
- `NoOpSource` 四值：`""`（真实动作）/ `llm` / `idle_heartbeat` / `timeout_injected`。**诊断时先靠它区分"agent 不想动"和"系统让它没动成"**。
- `TimeoutAgents`：该步被 barrier 超时自动填 NoOp 的 agent 下标列表；`[]` 为正常。
- `EndReason`：终态值回填到**所有行**，所以随便哪一行都能看到结局——不要只看最后一行。
- `Observations` 列很长（全文观测），命令行只看结构，细读用 Python 取单行。

### 3.2 agent_interactions.csv —— 每次工具调用一行

```
Step, Agent, ToolName, ToolArgs, Action, Observation, LLMInput, LLMInputChars,
LLMOutput, Thinking, RunID, CorrelationID, EventType, ToolLatencyMs, Success, ErrorType
```

- `LLMInput` 只是最近 6 条消息各 200 字符的摘要；**完整输入长度看 `LLMInputChars`，完整原文去 worker ndjson 的 llm_request**。
- `Thinking`：模型思考内容（网关支持时）。
- 审计单个 agent：按 Agent + Step 排序读 ToolName/Action/Success/ErrorType，配合 Observation 看环境反馈。

### 3.3 router_interactions.csv —— coordinator 每次工具调用一行

```
Step, Subtask, AssignedTo, RunID, CorrelationID, WorkerTaskID, EventType, Success, ErrorType
```

- `EventType` 覆盖四类 dispatch（assign_task / reply_to_help / cancel_task / activate_plan_node）+ update_plan / finish_task / query_workers / query_task_events / query_sar_state（仅 oracle 模式）。
- `WorkerTaskID` 是 worker 侧 opaque id，用它去 `workers/<A>/<A>/` 找对应 ndjson 文件。

### 3.4 subtasks.csv —— 任务生命周期

```
RunID, Step, SubtaskID, Status, AssignedTo, Subtask, CreatedAt, UpdatedAt, FailureClass, Details
```

- Status 流转：`assigned`（派发行）→ 终态行 `canceled`（cancel_task）；mission 级终态行 `SubtaskID=mission`，Status=completed/failed（coordinator finish_task 自报）。
- append-only 不回填：一个 dispatch 会有 assigned 行 + 可能的终态行两条。

### 3.5 token_usage.csv —— 每次 LLM 请求一行

```
Step, Agent, PromptTokens, CompletionTokens, TotalTokens, CacheHitTokens,
CacheMissTokens, RunID, LLMLatencyMs, Model, PromptVersion, Status
```

- **每请求一行**（含 Coordinator、MapAgent、MapSummarizer），不是每步一行。
- `Status=error` 行是失败路径补的 0 值行——筛它就是 LLM 故障清单。
- 恒等式：`CacheHitTokens + CacheMissTokens == PromptTokens`，不等说明采集有 bug。

### 3.6 NDJSON 轨迹文件（AgentLogger / TaskLogger）

coordinator 目录下**有两类 ndjson，别读错**：

- **AgentLogger 全量轨迹 = uuid 命名**（`<task_id>.ndjson`）：`llm_request` 带完整 `messages`——验证 Context 注入（semantic map / Long-term Memory / System Health 段）必须读这个文件。
- **TaskLogger 生命周期轨迹 = 时间戳命名**（`YYYYMMDD_HHMMSS.ndjson`）：事件含 meta/task_start/agentic_start/task_status/task_complete，content 按规则截断（llm_response 2000 / tool_result 3000），**messages 字段为空**——在里面 grep 注入段会得出"没注入"的假阴性结论。

worker 与 coordinator 的 AgentLogger 轨迹共用事件schema，按 `ts` 排序读：

| event | 含义 | 关键字段 |
|-------|------|---------|
| `llm_request` | LLM 调用前 | `messages`（完整输入）、`tools`、`step_index` |
| `llm_response` | LLM 响应后 | `content`、`thinking`、`tool_calls`、`usage`、`finish_reason`；可带 `status=aborted/cancelled/error` 终止标记 |
| `tool_start` | 工具执行前 | `tool_name`、`arguments` |
| `tool_result` | 工具执行后 | `success`、`result` / `error` |

TaskLogger 另有 `task_start` / `agentic_start` / `task_status` / `task_complete` / `task_final` / `task_error` 等生命周期事件；任务上下文缺失时落 `unknown.ndjson`（**出现 unknown.ndjson 且非空 = 日志归因缺陷信号**）。

**定位"agent 为什么这么做"的标准动作**：找到对应 step 的 `llm_request`，读 `messages` —— 那就是模型当时看到的全部世界。不要凭 system prompt 猜。

### 3.7 supervision/supervision_<dsp_id>.ndjson

每行是一次 watchdog 刷新后的完整快照：`supervision_state`（HEALTHY / TASK_STALE / WORKER_UNREACHABLE / TASK_DEADLINE_*）、`active_alerts`、`last_contact_at` vs `last_progress_at`。
注意：**进度只在终态更新、artifact、观测、求助或域指标变化时刷新**，纯心跳/NoOp 不算进展——所以 TASK_STALE 不一定是死，可能只是长期无域进展。

### 3.8 events.ndjson / coordinator/events_<task>.ndjson

- 顶层 `events.ndjson`：coordinator 语义事件（assign_task / cancel_task / send_message …），看调度时序。
- `coordinator/events_coordinator.ndjson` / `events_dsp_<uuid>.ndjson`：EventStore 流，事件类型 `status_update` / `artifact_update` / `help_request` / `observation_report` / `supervision_event` / `supervision_injected`。每个 task_id 最多留 500 条（超限丢最旧）。

---

## 4. 标准诊断流程（决策树）

```
run_metrics.json
│
├─ 文件缺失/字段空          → run 异常终止：看 stderr/stdout、metadata.json 是否完整
│
├─ end_reason = max_steps_reached 且 finished=false
│     → 指标够不够？够 = 正常截断；不够 → trajectory.csv 找指标停滞的起点 step S
│        → step S 的 Actions：
│           ├─ 大量 timeout_injected     → D 类：该 agent worker ndjson + token_usage Status
│           ├─ 大量 llm/idle NoOp        → B 类：为什么不动（ndjson 读 messages）
│           └─ 动作真实但失败             → B 类：agent_interactions 筛 Success=False
│
├─ finished=true            → 正常；若指标仍差 → 任务定义/成功判据问题（subtasks.csv mission 行）
│
└─ 指标正常但花费异常        → summary.csv / token_usage.csv：token 膨胀、cache 命中、error 行
```

下钻到具体 agent/任务后，**最终都以读 ndjson 的 llm_request messages 收尾**——那是行为的唯一真源。

---

## 5. 常用一行命令

```bash
RD=sar_orch/results/<run_dir>   # 设好 run 目录

# 结局
cat $RD/run_metrics.json | python3 -m json.tool | head -20

# 指标曲线（Step/Coverage/TransportRate/TimeoutAgents/NoOpSource）
python3 -c "
import csv
for r in csv.DictReader(open('$RD/trajectory.csv')):
    print(r['Step'], r['Coverage'][:5], r['TransportRate'][:5], r['TimeoutAgents'], r['NoOpSource'])"

# 失败动作清单
python3 -c "
import csv
for r in csv.DictReader(open('$RD/agent_interactions.csv')):
    if r['Success']=='False': print(r['Step'], r['Agent'], r['ToolName'], r['ErrorType'], r['Observation'][:80])"

# LLM 报错清单
python3 -c "
import csv
for r in csv.DictReader(open('$RD/token_usage.csv')):
    if r['Status']!='ok': print(r['Step'], r['Agent'], r['Status'])"

# coordinator 调度时序
python3 -c "
import csv
for r in csv.DictReader(open('$RD/router_interactions.csv')):
    print(r['Step'], r['EventType'], r['AssignedTo'], r['Subtask'][:70])"

# 读某 worker 某 dispatch 的思考链（取 llm_response 的 content/thinking）
python3 -c "
import json,sys
for line in open('$RD/workers/Alice/Alice/<task_id>.ndjson'):
    e=json.loads(line)
    if e.get('event')=='llm_response':
        print('---', e.get('step_index'), (e.get('content') or '')[:200])"

# 事件类型计数（任意 ndjson）
python3 -c "
import json,collections
print(collections.Counter(json.loads(l).get('event') or json.loads(l).get('event_type') for l in open('<file>.ndjson')))"
```

---

## 6. 判读常识与坑

1. **NavigateTo 是瞬移**：`GridEngine.move_object()` 直接 set_position，一次到位。工具返回 `"Arrived at X. Position: (x,y,z)."`。不要把它当逐格移动分析步数。
2. **get_agent_state 不消耗 step**：它是零成本 GPS 查询，不经过 barrier.submit_action。trajectory 里看不到它。
3. **barrier 60s 超时**：某 agent 60s 没交动作，系统自动填 `("NoOp", True, "timeout_injected")`。看到它先怀疑系统/LLM 延迟，不是 agent 决策。
4. **worker 会自动收尾 NoOp**：主任务完成后 worker 自动 no_op（上限 5 次后返回），NoOpSource=`idle_heartbeat` 属正常，不算"偷懒"。
5. **coordinator 每轮应给所有 agent 派活**：没任务的 agent 不交动作 → barrier 干等 60s。trajectory 里周期出现 timeout_injected 且集中在同一 agent → 调度覆盖不全。
6. **EndReason 全行回填**：trajectory 每一行的 EndReason 都是最终结局，不是该步状态。
7. **token_usage 是请求粒度**：同一 step 一个 agent 可能多行；LLM 异常时补 0 值 error 行，行数本身也是信号。
8. **semantic 模式没有 query_sar_state**：语义模式下 coordinator 的状态是 Context 自动注入的，router_interactions 里查不到 query_sar_state 属正常（oracle 模式才有）。
9. **truth 目录在 run 外**：`results/truth/<run_dir_basename>-<run_id末段uuid8>/`（修复后规则，见 §1），agent 沙箱不可读；proposer 评测口径以 truth manifest 为准。
10. **观测推送链是全自动的**：worker report_observation → [DATA] 块 → push → semantic_map.jsonl。断链时三段分别查：worker 调没调（agent_interactions）→ push 到没到（coordinator/events_dsp_*.ndjson 的 observation_report）→ 摄入有没有（semantic_map.jsonl）。

---

## 7. 本次 smoke run 实测样例（20260909_232036_s3_s42_a4）

- 配置：scene3 / seed42 / 4 agents / max_steps=30 / deepseek-v4-flash / semantic 模式，与 `baseline_s3_s42_a4` 同场景同种子。**唯一差异**：未传 `--long-term-mode`（默认 off），故无 `coordinator/long_term/`、`coordinator/diagnosis/`、`reflection_trace.ndjson`、`long_term_memory_quality.json`；baseline 当时是 `read` 模式（其 run_metrics 有 `long_term_memory_written: 6`）。
- 结局：`run_metrics.json` → end_reason=`max_steps_reached`，30 步跑满，exit 0，耗时约 971s。
- 指标：coverage=0.857、transport_rate=0.833（基线 run 为 0.714 / 0.778；同场景同种子不同采样，波动正常）。
- 健康度：trajectory 满 30 行；170 次 agent 工具调用 0 失败；130 次 LLM 请求 0 个 Status=error；2 个 step 出现 TimeoutAgents（个别 60s 超时注入，非系统性）；truth recorder 正常落盘（`results/truth/20260909_232036_s3_s42_a4/`，manifest+trace 齐全——该 run 为 truth 命名修复**前**产物，修复后目录名带 `-<run_id末段uuid8>` 后缀）；memory acceptance_gate=pass。
- 行为样例：step 9–11 出现 `UseSupply(AgniFire_Region_1, Water)` 等真实灭火动作，NoOpSource 混有 `idle_heartbeat`（已完成任务的 agent 正常占位）。
- subtasks.csv 只有 assigned/canceled 行、无 mission 终态行：因 max_steps 截断时 coordinator 未来得及 finish_task，属预期；若 run 以 `finished=true` 结束仍无 mission 行，才是调度缺陷信号。

### 对照 run：`20260910_000018_s3_s42_a4`（+ `--long-term-mode read`）

- 同配置加 long-term read；exit 0，515s，coverage=0.714 / transport=0.778，acceptance_gate=pass，终局 reflection 写入 6 条长期记忆（traceability=1.0、truth 违规 0）。
- 产物齐了：`coordinator/long_term/long_term.sqlite3`、`coordinator/diagnosis/`（sqlite3 + transcripts.ndjson，3 轮 diagnosis_round）、`reflection_trace.ndjson`、`long_term_memory_quality.json`。
- 注入验证（读 **uuid 命名**的 AgentLogger 文件）：coordinator 22/22 个 llm_request 含 `### Long-term Memory`（run 内库为空 → 渲染 `- (none published yet)`，记忆在终局 reflection 才发布，属设计）；14/22 含 `### System Health`（首轮诊断后才开始出现）；worker 侧 0 命中（ACL 正确）。
- `long_term_reflection.drain=timeout`：run 结束时 rolling reflection 仍在飞，60s drain 超时按 D8 设计不阻塞退出，故终局 summary 里 `diagnosis=null`；磁盘上 transcripts.ndjson 已有 3 轮记录——**看磁盘文件，不看终局字段**。
