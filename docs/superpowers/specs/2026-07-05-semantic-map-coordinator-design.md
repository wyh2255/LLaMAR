---
日期: 2026-07-05
文档类型: 设计规格
文档概述: 设计 SAR coordinator 的语义地图机制，移除正式决策中的全局 oracle 视角，并通过 worker 非阻塞上报维护共享团队记忆。
---

# SAR Coordinator Semantic Map Design

## 背景

当前 SAR coordinator 可以通过 `query_sar_state` 直接调用 `SARBarrier.get_env_snapshot()`，后者从底层环境读取完整对象列表，包括 agents、fires、persons、reservoirs、deposits 和 flammables。这让 coordinator 拥有全局真值视角，等价于 oracle。

这个设定对真实 SAR 场景不成立。现实中的调度者不应直接读取完整环境状态，而应基于先验、机器人报告、任务事件和共享记忆逐步形成语义地图。当前全局视角还会把大体积环境快照写入 coordinator 上下文，降低 prompt token 效率和 KV cache 命中率。

本设计将正式实验模式改为“弱先验 + worker 上报”的语义地图模式。`query_sar_state` 保留为 debug/oracle 模式工具，但默认不参与 coordinator 决策。

## 目标

- Coordinator 维护唯一的团队级 `SemanticMapStore`。
- `SemanticMapStore` 允许初始化弱先验：reservoirs、deposits、agent roster、SAR 规则、step budget 和任务目标。
- Fires、persons、flammables 等具体环境发现只能来自 worker 上报或任务事件，不允许从底层 env oracle 读取。
- Worker 可以通过非阻塞 `report_observation` 工具把局部观察上报给 coordinator。
- `report_observation` 走 A2A push notification 通道，不暂停 worker，不触发 `INPUT_REQUIRED`。
- Coordinator 通过 `query_semantic_map()` 查询已知语义地图，通过 `query_team_status()` 查询 worker 任务状态。
- Worker 需要团队共享记忆时，可以通过工具/MCP 向 coordinator 查询语义地图。
- 正式模式默认禁用 `query_sar_state`，只在 debug/oracle 模式启用。
- 语义地图输出保持紧凑，并作为动态上下文放在 messages 末尾，尽量提高 DeepSeek 自动前缀缓存命中率。

## 非目标

- 不在本设计中重写 SAR 环境、地图 UI 或底层 `SARBarrier`。
- 不禁止 debug UI 使用 `barrier.get_env_snapshot()` 进行可视化。
- 不要求 worker 拥有完整本地地图；worker 只负责局部观察、执行动作和必要时查询团队共享记忆。
- 不实现复杂概率 SLAM。初版采用结构化事实表、时间戳、来源和简单冲突处理。

## 架构概览

正式模式下的数据流如下：

```text
SAR weak priors
  -> SemanticMapStore.init_priors(reservoirs, deposits, agents, rules, step_budget)

Coordinator LLM
  -> query_semantic_map()
  -> query_team_status()
  -> dispatch_task(worker, prompt)

Worker LLM
  -> local SAR tools: explore/navigate/get_supply/use_supply/carry/drop_off
  -> report_observation(...)
       -> A2A push notification
          via TASK_STATE_WORKING status_update emitted by A2AWorkerSink
       -> EventStore / ObservationStore
       -> SemanticMapStore.ingest_observation(...)

Worker needs shared memory
  -> query coordinator semantic map tool/MCP
       -> SemanticMapStore.query(...)
```

`SemanticMapStore` 是 coordinator 侧唯一的语义地图写入点。Coordinator、worker 和日志系统都可以读取它，但具体事实的写入必须经过初始化先验或 worker 上报。

## SemanticMapStore

### 位置

建议新增：`sar_orch/semantic_map.py`。

### 职责

`SemanticMapStore` 负责保存团队共享语义地图。它不直接读取底层环境对象，不调用 `barrier.get_env_snapshot()`。

主要职责：

- 初始化弱先验。
- 接收 worker observation report。
- 从 task artifact / help request / event callback 中抽取结构化事实。
- 合并重复或冲突观察。
- 输出紧凑 semantic map。
- 输出 staleness/conflict 信息，提醒 coordinator 不要把旧观察当作真值。

### 数据模型

建议采用普通 dataclass 或 Pydantic model。初版字段如下：

```python
class SemanticMapStore:
    reservoirs: dict[str, SemanticObject]
    deposits: dict[str, SemanticObject]
    fires: dict[str, SemanticObject]
    persons: dict[str, SemanticObject]
    agents: dict[str, AgentSemanticState]
    observations: list[ObservationRecord]
    rules: dict[str, Any]
    step_budget: dict[str, int]
```

`SemanticMapStore` 必须是线程安全的。初版使用 `threading.Lock` 保护所有写入和读取快照的方法，保持和 `EventStore`、`SARBarrier` 的线程模型一致。任何 `query_*()` 方法都返回深拷贝或只读序列化结果，避免调用方持有内部 dict/list 引用。

`ObservationRecord` 最小字段：

```json
{
  "reporter": "Alice",
  "step": 12,
  "object_type": "fire",
  "name": "CaldorFire_Region_1",
  "position": [4, 4, 0],
  "attributes": {
    "fire_type": "Chemical",
    "intensity": "Medium",
    "status": "active"
  },
  "confidence": 1.0,
  "source_task_id": "alice-scout-north",
  "note": "Observed while scouting north sector"
}
```

`SemanticObject` 应包含：

- `object_type`
- `name`
- `position`
- `attributes`
- `status`
- `last_seen_step`
- `last_seen_ts`
- `sources`
- `confidence`
- `conflict`

`AgentSemanticState` 应包含：

- `agent_id`
- `last_position`
- `inventory`
- `current_task_id`
- `task_state`
- `last_seen_step`
- `last_message`

## 弱先验

`SemanticMapStore` 可以预先知道：

- reservoirs：位置和 `resource_type`
- deposits：位置和 inventory/用途
- agent roster：agent id、能力、初始可用状态
- SAR 规则：例如 Chemical fire 需要 Sand，Non-chemical fire 可用 Water
- step budget：current/max/remaining
- task objective：灭火、救援等目标描述

`SemanticMapStore` 不应预先知道：

- fires 的精确位置、强度、状态
- persons 的精确位置、状态
- flammables 全局网格
- 未被 worker 观察到的动态环境状态

## Coordinator 工具

### query_semantic_map

正式模式下，coordinator 使用 `query_semantic_map()` 代替 `query_sar_state()`。

返回内容应紧凑，避免完整历史。建议结构：

```json
{
  "step_budget": {"current_step": 12, "max_steps": 120, "remaining": 108},
  "known_priors": {
    "reservoirs": [...],
    "deposits": [...]
  },
  "known_dynamic_objects": {
    "fires": [...],
    "persons": [...]
  },
  "agents": [...],
  "stale_entries": [...],
  "conflicts": [...],
  "unknowns": ["fire locations incomplete", "person rescue status unknown"]
}
```

输出原则：

- 只返回已知事实和不确定性。
- 每条动态事实带 `source` 和 `last_seen_step`。
- 不输出 flammables 全局网格。
- 对 stale/conflict 条目显式标记。

`CoordinatorContextManager` 集成要求：

- `CoordinatorPinnedState` 不再以 oracle `global_snapshot` 为核心。
- 新增 `semantic_summary`、`team_status_summary` 或等价字段，用于保存 `query_semantic_map()` 和 `query_team_status()` 的紧凑结果。
- `_extract_pinned()` 需要处理 `tool_name == "query_semantic_map"` 和 `tool_name == "query_team_status"`。
- `_render_environment_view()` 从语义摘要渲染已知 fires/persons/reservoirs/deposits 数量、stale/conflict 提醒和 worker 状态摘要，不读取 env snapshot。

### query_team_status

`query_team_status()` 返回 worker 执行状态，不返回底层 env。

它不替代 `query_task_events()` 的精确任务查询能力。两者边界如下：

- `query_task_events(task_ids)`：面向指定 task id 的精确事件追踪，适合等待某个派发任务完成、失败或进入 `INPUT_REQUIRED`。
- `query_team_status()`：面向全队的概览查询，适合 coordinator 在规划前快速了解所有 worker 的当前任务、最近 observation 和 pending help request。

Coordinator prompt 应要求：需要检查某个具体 task 的完成结果时使用 `query_task_events()`；需要全局团队态势时使用 `query_team_status()`。

建议字段：

```json
{
  "workers": [
    {
      "agent_id": "Alice",
      "task_id": "alice-scout-north",
      "state": "RUNNING",
      "last_event": "observation_report",
      "last_message": "Found Chemical fire at (4,4,0)",
      "last_seen_step": 12,
      "input_required": false
    }
  ],
  "pending_requests": [...],
  "recent_observations": [...]
}
```

### query_sar_state

`query_sar_state()` 保留，但必须被 oracle/debug 开关保护。

默认正式实验中：

- coordinator 工具列表不注入 `query_sar_state`
- coordinator prompt 不推荐或不提及 `query_sar_state`
- metadata 记录 `oracle_mode=false`

debug/oracle 模式中：

- 允许注入 `query_sar_state`
- metadata 记录 `oracle_mode=true`
- token 和成功率指标应和 semantic mode 分开统计

## Worker 上报工具

### report_observation

新增 worker 工具：`report_observation`。

职责：把 worker 局部观察以结构化事件非阻塞上报给 coordinator。

约束：

- 不暂停 worker。
- 不触发 `INPUT_REQUIRED`。
- 不调用 `AskCoordinatorTool`。
- 不保存/恢复 task snapshot。
- 继续保持 worker task 为 `WORKING`。
- 通过 A2A push notification 通道发送 observation event。

参数建议：

```json
{
  "object_type": "fire|person|reservoir|deposit|agent|status|unknown",
  "name": "optional object name",
  "position": [0, 0, 0],
  "attributes": {},
  "confidence": 1.0,
  "note": "short natural-language observation"
}
```

工具结果返回给 worker：

```text
Observation reported to coordinator. Continue current task.
```

### A2A push notification 事件

`report_observation` 应通过 A2A push notification 产生非终态事件。

初版不要求 worker tool 直接访问 A2A push sender。现有 worker 运行链路已经会把每个 tool result 交给 `A2AWorkerSink.emit("tool_result", ...)`，并由 sink 生成 `TASK_STATE_WORKING` 的 `TaskStatusUpdateEvent`。该 status update 会随 dispatch task 的 push notification 配置推送到 coordinator 的 `/a2a/push-callback`。

因此初版传输路径固定为：

```text
report_observation tool
  -> ToolResult(success=True, content=json observation summary)
  -> Agent step_callback: tool_result
  -> A2AWorkerSink.emit("tool_result", tool_name="report_observation", ...)
  -> TaskStatusUpdateEvent(state=WORKING, message includes [DATA])
  -> A2A push notification to coordinator /a2a/push-callback
  -> push callback parses [DATA]
  -> EventStore.append(event_type="observation_report", ...)
  -> SemanticMapStore.ingest_observation(...)
```

建议事件语义：

- `event_type = "observation_report"`
- `task_id = current worker task id`
- `context_id = mission context id`
- `state = WORKING`
- `text = compact observation summary`
- `[DATA].tool_name = "report_observation"`
- `[DATA].content = structured observation payload serialized as JSON`

接收端处理：

- EventStore 记录 `observation_report`。
- ObservationStore 或 SemanticMapStore ingest 结构化 payload。
- Coordinator 后续 `query_semantic_map()` 可读到该观察。

如果未来需要绕过 `[DATA]` 文本解析，可再扩展专用 observation endpoint；但初版必须优先复用现有 A2A push notification 的 `WORKING status_update` 路径，避免引入第二条并行通信链路。

## Worker 查询共享记忆

Worker 如果需要团队共享记忆，可以通过工具/MCP 向 coordinator 查询语义地图。

建议新增 worker-side 工具：`query_shared_memory` 或复用 MCP tool。

初版实现为直接 worker tool，并通过 coordinator HTTP endpoint 查询 `SemanticMapStore` 的只读摘要。该 endpoint 不暴露 env snapshot，不调用 `barrier.get_env_snapshot()`。

约束：

- Worker 查询的是 `SemanticMapStore`，不是 env。
- 返回紧凑已知事实，不返回 oracle snapshot。
- 该工具不消耗 SAR env step。
- 查询结果可用于 worker 后续导航、补给、救援协作。

## 语义地图合并策略

合并规则：

- 优先用 `name` 匹配对象。
- 无 `name` 时用 `object_type + position` 匹配。
- 同一字段冲突时，优先采用更高 `step` 或更晚 timestamp 的观察。
- 终态可覆盖非终态，例如 `extinguished` 覆盖 `active`，`rescued` 覆盖 `trapped`。
- 冲突字段保留 `conflict=true` 和 `sources`，不静默丢弃历史。
- 每个对象保留来源列表：`reporter`、`task_id`、`step`、`confidence`、`note`。

Staleness：

- 动态对象超过阈值未更新时标记 stale。
- query 输出 stale entries，coordinator prompt 要求重新验证旧信息。
- 初版不自动删除 stale fire/person，只标记。

## Context 与 KV Cache

上下文拼接原则：

- `system prompt + conversation history` 保持稳定前缀。
- `Context Memory` 放在 messages 末尾。
- `SemanticMapStore` 的动态摘要只作为末尾 memory block 或显式工具结果出现。
- 不把完整 env snapshot 或 flammables 网格写入 coordinator history。
- `query_team_status()` 和 `query_semantic_map()` 返回紧凑结构，避免重复巨型 JSON。

期望效果：

- coordinator prompt tokens 下降。
- coordinator cache hit ratio 上升。
- semantic mode 的 token 数据和 oracle mode 分开记录。

## Prompt 更新

Coordinator prompt 必须明确：

- 正式模式下禁止依赖 env oracle。
- 使用 `query_semantic_map()` 了解已知世界。
- 使用 `query_team_status()` 了解 worker 状态和待处理请求。
- 未知 fire/person 位置必须通过 worker scouting 获得。
- 对 stale/conflict 语义地图条目要重新派 worker 验证。

Worker prompt 必须明确：

- 发现 fire/person/reservoir/deposit/status 变化时调用 `report_observation`。
- `report_observation` 非阻塞，调用后继续当前任务。
- 需要团队共享记忆时调用 shared memory 查询工具，不直接请求 env oracle。
- `ask_coordinator` 仅用于需要协调决策或无法继续的情况，不用于普通观察上报。

## 集成点

需要覆盖的主要文件：

- `sar_orch/semantic_map.py`：新增 `SemanticMapStore`。
- `sar_orch/tools/coordinator/query_sar_state.py`：加 oracle/debug gating。
- `sar_orch/tools/coordinator/__init__.py`：导出新 coordinator 工具。
- `sar_orch/tools/coordinator/query_semantic_map.py`：新增。
- `sar_orch/tools/coordinator/query_team_status.py`：新增。
- `sar_orch/tools/worker/report_observation.py`：新增。
- `sar_orch/tools/worker/__init__.py`：导出 worker 工具。
- `src/a2a/coordinator/event_store.py`：支持 `observation_report` 摘要或接入 ObservationStore。
- `src/a2a/worker/sink.py`：确保 `report_observation` 的 tool_result 在 `[DATA]` 中保留结构化 JSON 内容。
- `src/a2a/coordinator/server.py`：在 `/a2a/push-callback` 的 `status_update` 分支解析 `[DATA]`，识别 `tool_result/report_observation` 并写入 semantic map。
- `src/Agent/router_agent/context.py`：CoordinatorPinnedState 不再以 oracle `global_snapshot` 为核心。
- `sar_orch/coordinator.py`：创建 `SemanticMapStore`，注入 semantic tools，控制 oracle mode。
- `sar_orch/worker.py`：注入 `report_observation` 和 shared memory 查询工具。
- `sar_orch/experiment.py`：增加 oracle/semantic 模式参数并写 metadata。
- `sar_orch/prompts/coordinator/`：更新 coordinator 指令。
- `sar_orch/prompts/worker/`：更新 worker 指令。
- `sar_orch/logger.py`：记录 semantic map 规模、staleness、observation count。

## 实施分期

### Phase 1: 语义地图内核

- 新增线程安全 `SemanticMapStore`。
- 支持弱先验初始化、observation ingest、merge、stale/conflict 标记和只读 query。
- 增加 `semantic_map.jsonl` 持久化 API。

### Phase 2: Observation 传输链路

- 新增 worker `report_observation` 工具。
- 确保 `A2AWorkerSink` 对该 tool result 保留结构化 `[DATA]`。
- 扩展 coordinator `/a2a/push-callback`，从 `WORKING status_update` 中解析 observation report。
- 将 observation 写入 EventStore 和 SemanticMapStore。

### Phase 3: Coordinator/Worker 查询工具

- 新增 `query_semantic_map()`。
- 新增 `query_team_status()`。
- 新增 worker `query_shared_memory()`。
- 明确 `query_task_events()` 与 `query_team_status()` 的 prompt 边界。

### Phase 4: Oracle gating 与上下文迁移

- semantic mode 默认不注入 `query_sar_state`。
- oracle/debug mode 才注入 `query_sar_state`。
- 修改 `CoordinatorPinnedState`、`_extract_pinned()` 和 `_render_environment_view()` 使用 semantic map 摘要。
- 更新 coordinator/worker prompts。

### Phase 5: 实验与指标

- `experiment.py` 增加 semantic/oracle mode 参数。
- run metadata 记录 `state_mode`、`oracle_mode`。
- 日志记录 observation count、semantic map size、stale/conflict count。
- 对比 semantic vs oracle 的 success、tokens、cache hit ratio。

## 验收测试

### 单元测试

- `SemanticMapStore` 可以初始化 reservoirs/deposits/agents/rules。
- `SemanticMapStore` 不从 env 读取 fire/person。
- `ingest_observation()` 能新增 fire/person。
- 同名对象新 step 覆盖旧字段。
- 冲突观察被标记，不静默丢弃。
- stale 动态对象被标记。

### 集成测试

- semantic mode 下 coordinator 工具列表不包含 `query_sar_state`。
- oracle/debug mode 下可以使用 `query_sar_state`。
- worker 调用 `report_observation` 后 task 仍为 `WORKING`。
- `report_observation` 产生 `observation_report` push event。
- coordinator `query_semantic_map()` 能读到 worker 上报对象。
- `query_team_status()` 能读到 worker task 状态和最近 observation。
- worker 查询 shared memory 不读取 env oracle。

### 实验检查

运行场景：

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --mode semantic
```

检查：

- `metadata.json` 或 run metadata 中有 `oracle_mode=false` / `state_mode=semantic`。
- `router_interactions.csv` 中没有正式 `query_sar_state` 调用。
- `agent_interactions.csv` 中存在 `report_observation`。
- `semantic_map` 日志能看到 fire/person 来源为 worker。
- `token_usage.csv` 中 coordinator prompt tokens 比 oracle baseline 降低。
- cache hit ratio 相比 oracle baseline 上升。

## 风险与缓解

### Worker 上报不足

风险：worker 如果不主动 report，semantic map 会缺信息。

缓解：worker prompt 明确要求发现关键对象立刻 `report_observation`；scouting 任务 prompt 明确要求边探索边上报。

### 信息过期

风险：火势和人员状态会变化，旧观察可能误导 coordinator。

缓解：所有动态对象带 `last_seen_step`，query 输出 stale 标记，coordinator prompt 要求重新验证 stale 目标。

### A2A 非终态 observation 不兼容

风险：现有 push callback 可能只处理 status/artifact/help request。

缓解：初版不新增 protobuf event type，复用现有 `TASK_STATE_WORKING status_update` 和 `[DATA]` 结构；push callback 解析 `tool_result/report_observation` 后生成内部 `observation_report`。

### SemanticMapStore 并发访问

风险：push callback、coordinator tool 查询和日志写入可能并发读写 semantic map。

缓解：使用 `threading.Lock` 保护内部状态；query 返回深拷贝或序列化快照；jsonl 写入也在锁内生成记录。

### 成功率短期下降

风险：去掉 oracle 后 coordinator 不再全知，探索和协作成本增加。

缓解：先用 scene 1 验证，再扩展 benchmark；保留 oracle mode 做对照组。

## 初版实现决策

- `report_observation` 初版要求 worker LLM 严格填写结构化 JSON 参数；`note` 只作为补充文本，不作为主要解析来源。
- shared memory 查询初版直接实现为 worker tool，后续如需统一外部工具协议再封装为 MCP。
- semantic map 必须持久化到实验结果目录，建议文件名为 `semantic_map.jsonl`，每次 observation ingest 和 map merge 后追加一条记录，便于复现实验和分析信息传播路径。
