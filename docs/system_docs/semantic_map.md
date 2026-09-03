---
日期: 2026-07-26
文档类型: 系统架构文档
文档概述: 语义地图（Semantic Map）子系统完整设计，涵盖 SemanticMapStore 数据模型、
  Worker→Coordinator 观测摄入管道、MapDiffCalculator 差分、MapSummarizer 摘要、
  StateProvider 运行时注入及相关工具/API
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）
核对口径: 类/函数名 grep -n，行号以当前工作区实测为准
---

# 语义地图（Semantic Map）

## 1. 概述

语义地图是 Coordinator 侧维护的**环境共识层**：Worker 在执行动作时产生结构化观测（observation），通过 A2A push 通知回传 Coordinator，由 `SemanticMapStore` 合并为全局的火灾/被困者/智能体状态视图。在 `--mode semantic`（默认）下，Coordinator 每轮 LLM 调用前自动注入该地图快照 + 增量摘要，**无需调用任何工具**即可获知战场态势。

核心价值：
- **去 oracle 化**：semantic 模式下 Coordinator 看不到 `query_sar_state`（全知环境真相），只能依赖 Worker 上报的语义地图做决策
- **降噪**：Worker 端 `WorkerReportPublisher` 预过滤、Store 端 `_merge_locked` 二次去重，避免重复观测淹没上下文
- **连续性**：`MapDiffCalculator` 计算快照差分 + `MapSummarizer` 生成中文摘要，Coordinator 看到的是"变化"而非全量 JSON
- **可观测性**：每次观测/摘要都持久化到 JSONL，支持离线回放与指标分析

代码位置：

| 模块 | 文件 | 职责 |
|------|------|------|
| Store | `sar_orch/map/store.py` | 内存状态 + 合并 + 快照 + JSONL 持久化 |
| Publisher | `sar_orch/map/publisher.py` | Worker 端观测预去重 |
| Diff | `sar_orch/map/diff.py` | 纯函数快照差分计算器 |
| Summarizer | `sar_orch/map/summarizer.py` | LLM 驱动的增量摘要（带预算/超时/单飞） |
| StateProvider | `sar_orch/coordinator_state_provider.py` | 把地图投影为 Coordinator RuntimeState |
| 服务端点 | `src/a2a/coordinator/server.py` | 观测摄入 + `/semantic-map` HTTP API |
| Map Agent MCP | `sar_orch/map_agent/server.py` | worker 侧地图查询 MCP（get_fire_info / get_person_info / get_reservoir_info / get_task_context / query_natural），经 `server.py:1022 set_semantic_map` 挂载到 `/mcp/map` |
| 旧兼容层 | `sar_orch/semantic_map.py` | re-export，新代码用 `sar_orch.map` |

## 2. 架构总览

```
┌─ Worker (Alice/Bob/...) ──────────────────────────────────────────────┐
│  动作工具 (navigate_to / explore / use_supply / ...)                   │
│    └→ SARBarrier.submit_action() → 返回 observation + structured_*     │
│    └→ tool_result_from_barrier() 构建 ToolResult(                      │
│         content=观察文本,                                              │
│         data={observations, position, inventory})                      │
│    └→ WorkerReportPublisher.apply_to_data()  ← 预去重                  │
│         （key = name 或 type:pos，比对 position/attrs/confidence）      │
└────────────────────────────┬───────────────────────────────────────────┘
                             │ A2A push notification ([DATA] blocks)
                             ▼
┌─ Coordinator Server (src/a2a/coordinator/server.py) ──────────────────┐
│  push callback → _extract_observations_with_provenance 主路径          │
│    （server.py:99 定义，callback 于 :1784 调用；provenance 标签         │
│      worker_sensor_tool / worker_observation；observations=None 时     │
│      兜底 _extract_auto_observations :313 → :1074）                    │
│    ├─ 新格式：tool_result.structured_data.observations[]              │
│    └─ 旧格式：report_observation tool_result.content (JSON)           │
│  → scan_forbidden_truth_fields 剥离禁读真值字段（:1078-1082）          │
│  → canonical Memory 先写（:1843-1856），legacy 摄入受                  │
│    allow_legacy_observation_write 配对门控（:1858-1863）               │
│  → EventStore.append("observation_report")                            │
│  → SemanticMapStore.ingest_observation(obs)  ← 合并 + 二次去重        │
│  → TaskWatchdog.record_progress(source="observation_report")          │
└────────────────────────────┬───────────────────────────────────────────┘
                             │
                             ▼
┌─ SARCoordinatorStateProvider (coordinator_state_provider.py) ─────────┐
│  prepare_for_llm() (异步，每轮 LLM 调用前):                            │
│    1. _try_snapshot_with_revision() 原子读 (revision, snapshot)       │
│       （provider:425 包装 store.snapshot_with_revision :358；          │
│        legacy/mock store 无该方法时回退 (0, snapshot()) :435-436）     │
│    2. revision 变化 → MapDiffCalculator.diff(prev, curr)              │
│    3. delta 非空 → MapSummarizer.maybe_summarize() (单飞)             │
│  snapshot():                                                          │
│    投影 RuntimeState.payload:                                         │
│      semantic_summary / team_status_summary / map_revision /          │
│      map_delta / map_summary / map_summary_revision / step_budget /   │
│      task_status_view / mission_dag_view /                            │
│      physical_dispatches_view / ...                                   │
└────────────────────────────┬───────────────────────────────────────────┘
                             │ ContextManager 注入 system prompt
                             ▼
                       Coordinator LLM
```

## 3. SemanticMapStore

### 3.1 数据模型

```python
@dataclass
class ObservationRecord:      # 单条观测上报（Worker 产生）
    reporter: str             # 上报者 agent 名
    step: int                 # 环境步数
    object_type: str          # fire | person | reservoir | deposit | agent | ...
    name: str | None          # 对象名（fire_3, person_A, ...）
    position: tuple[int,int,int] | None
    attributes: dict          # 任意 KV，如 {intensity: "HIGH", status: "active"}
    confidence: float = 1.0
    source_task_id: str       # 产生该观测的任务 ID
    note: str                 # 自由文本备注

@dataclass
class SemanticObject:         # 合并后的对象状态（Store 内部）
    object_type, name, position, attributes
    status: str               # 顶层状态（rescued/extinguished/active/trapped/...）
    last_seen_step: int       # 最后观测步
    last_seen_ts: float       # 最后观测墙钟时间
    sources: list[dict]       # 来源记录（保留最近 50 条，store.py:456-457）
    confidence: float
    conflict: bool            # 同步内属性冲突标记
    conflicts: list[dict]     # C3 逐字段冲突记录（store.py:100）
    field_last_seen_steps: dict[str, int]  # 每字段最后更新步（store.py:109）

@dataclass
class AgentSemanticState:     # 智能体状态（精简）
    agent_id, last_position, inventory
    current_task_id, task_state
    last_seen_step, last_message
    inventory_last_seen_step  # 库存字段独立步戳（store.py:142）
```

### 3.2 存储分区

```python
class SemanticMapStore:
    reservoirs: dict[str, SemanticObject]   # 先验已知（init_priors 注入）
    deposits:   dict[str, SemanticObject]   # 先验已知
    fires:      dict[str, SemanticObject]   # 动态发现
    persons:    dict[str, SemanticObject]   # 动态发现
    agents:     dict[str, AgentSemanticState]
    observations: list[dict]                # 最近 N 条原始观测（max_observations=1000）
    rules: dict                              # 灭火规则（Chemical→Sand, ...）
    step_budget: {current_step, max_steps, remaining}
    task_objective: str
    # 另有 _unknown_types 兜底分区（store.py:172，经 :659 _partition_for 路由）：
    # 未知 object_type 的观测落入合并，但不出现在快照里
```

### 3.3 合并规则（`_merge_locked` → direct / cell 双路径）

每条观测按键（`_record_key`，store.py:661）定位到目标对象后，`_merge_locked`（store.py:426）按 `is_cell = object_type=="fire" 且 attributes.parent_fire 非空`（:429）分流到两条合并路径：

- **`_merge_direct_locked`（store.py:460）**：非 cell 观测（fire 无 parent_fire、person、agent 等）逐字段合并。
- **`_merge_cell_locked`（store.py:534）**：cell 观测**只写入自己独立的 `observed_cells` 条目**（:545-582，每个单元格一份 attributes + `field_last_seen_steps`），不再把多个单元格的属性混写进父火场对象；父对象仅接受 `_CELL_DERIVED_PARENT_KEYS = {"fire_type"}`（:424）白名单内的区域共享键做派生共识（:584-597）。
- **C3 冲突记录 `_record_conflict`（store.py:600）**：同 step 内字段值不一致时**保留现值**并把双方 candidates 记入 `conflicts` 列表（非 last-write-wins），同时置 `conflict=True`。旧文档"fire 的 intensity 例外"机制已被 cell 模型取代——不同单元格强度天然各记各的，不再需要例外分支。

1. **键的选择**（`_record_key`）：
   - fire 且 `attributes.parent_fire` 非空 → 用 `parent_fire`（cell 观测挂到父火场的 `observed_cells`）
   - 否则用 `name`
   - 否则用 `"{type}:{position}"`

2. **属性更新**（direct 路径）：
   - 若 `rec.step >= existing.last_seen_step`（新观测更新）
   - 或新状态在 `TERMINAL_STATUS_ORDER` 中排名 ≥ 旧状态**且新状态 rank > 0**（`store.py:484-487` 的 `new_rank > 0` 前置条件，status 分支另有一处 `store.py:512-515`；未知状态 rank 恒为 0，不参与竞争，避免一条无法识别 status 的旧观测靠 `0 >= 0` 覆盖掉更新的记录——终态本身不可回退）
   - 每次字段更新同步写 `field_last_seen_steps[key] = rec.step`（:476/:489/:496/:518/:532）

3. **位置更新**：仅在 `rec.step >= last_seen_step` 时覆盖（:532 附近）

4. **观测记录保留**：fire/person 等仅当满足 `_is_observation_noteworthy`（store.py:627；新对象 / 位置变化 / 属性变化 / 状态变化 / confidence 提升）才追加到 `observations` 列表（:334-339），避免重复观测占满 1000 条上限；**例外**：agent 型观测无条件 append（:307-311），不过 noteworthy 判定

5. **终态保护**：`TERMINAL_STATUS_ORDER = {rescued:3, extinguished:3, complete:3, active:1, trapped:1}`（store.py:24-30），高 rank 状态不会被低 rank 覆盖

### 3.4 快照输出（`snapshot` / `snapshot_with_revision`）

```python
{
  "step_budget": {"current_step": 12, "max_steps": 50, "remaining": 38},
  "task_objective": "Extinguish all fires and rescue all persons",
  "rules": {"Chemical": "Sand", "Non-chemical": "Water"},
  "known_priors": {
    "reservoirs": [...],   # 水源点
    "deposits": [...],     # 沙袋存放点
  },
  "known_dynamic_objects": {
    "fires":   [...],      # SemanticObject dict
    "persons": [...],
  },
  "agents": [...],         # AgentSemanticState dict
  "recent_observations": [...],   # 最近 10 条原始观测
  "stale_entries":  [...],   # current_step - last_seen_step > 5 的 fire/person
  "conflicts":      [...],   # conflict=True 的 fire/person
  "unknowns":       [...],   # 如 "fire locations incomplete"
}
```

`snapshot_with_revision()` 在同一把锁内返回 `(revision, snapshot)`（store.py:358-368），供 StateProvider 做版本比对；`snapshot()` 即委托它取 [1]（:356）。另有 `worker_public_snapshot(viewer_id)`（store.py:370-391）做按 viewer 的 ACL 裁剪视图，**当前尚无调用方接线**。

### 3.5 指标

| 方法 | 含义 |
|------|------|
| `map_recall()` | 已发现对象数 / ground truth 对象数（需 `set_ground_truth`） |
| `freshness()` | 所有对象平均 `current_step - last_seen_step`，越小越新鲜 |

两者都由 `experiment.py` 每步写入 `summary.csv`（experiment.py:828-837 计算，logger.py:185-186 落 `MapRecall`/`Freshness` 列）。**注意**：全仓无 `set_ground_truth` 生产调用方（仅 store.py:227 定义与 tests），故线上 `map_recall` 恒为 0.0——§9.1 的"可选注入"实为从未接线。

### 3.6 持久化

- `set_jsonl_path(path)` 启用后，每次 `ingest_observation` 追加一行（`_append_jsonl_locked`，store.py:703-715）：
  `{"ts": ..., "event_type": "observation_ingested", "observation": {...}, "object": {...}}`
  ——键名是 **`event_type`**（非 `type`），顶层含墙钟 `ts`
- 写入前观测经 `RedactionPolicy` 脱敏（store.py:18-21 模块级 `_REDACTION`，:307/:331-332 调用 `redact_data`）：`semantic_map.jsonl` 永不携带原始 secrets，这是防御性边界
- 默认路径 `<log_dir>/semantic_map.jsonl`（coordinator.py:459-461）；`experiment.py` 会重定向到实验目录顶层（:702-708）
- Store 内部所有公共方法都持 `_lock`（threading.Lock），多线程安全

## 4. 观测摄入管道

### 4.1 Worker 端

**自动上报**（推荐路径）：每个 SAR 动作工具（`navigate_to` / `explore` / `use_supply` / `carry_person` / `drop_off_person` / `get_supply` / `store_supply` / `move`）通过 `tool_result_from_barrier()` 返回 `ToolResult.data = {observations, position, inventory}`。`_barrier_helpers.py` 中的模块级 `WorkerReportPublisher` 在 `apply_to_data()` 里按 `(name 或 type:pos)` 键过滤掉与上次上报相同的观测，保留 `position`/`inventory` 元数据即使观测全被过滤。

**手动上报**：`report_observation` 工具由 LLM 显式调用，产出单条 JSON 观测。仍然走同一管道。

### 4.2 传输

Worker 的 `A2AWorkerSink` 把 `ToolResult.data` 序列化为 `[DATA] {json}` 块附加到状态更新文本，通过 A2A push notification 推给 Coordinator。上限 12000 字符限的是单条 tool_result 的 **`content` 字符串**（src/a2a/worker/sink.py:99 `content_limit = 12000`、:105 截断），`structured_data` 整块不截断。

### 4.3 Coordinator 端（`src/a2a/coordinator/server.py`）

push callback 收到状态更新后（`_ingest_observations_from_status`，server.py:1055）：

1. `_extract_worker_data_blocks(text)` 解析所有 `[DATA]` JSON 块（server.py:65）
2. 观测提取：主路径是 `_extract_observations_with_provenance`（server.py:99，callback :1784 调用，带 provenance 标签 worker_sensor_tool / worker_observation）；observations=None 时才兜底 `_extract_auto_observations`（:313，:1074 调用）。两种格式都处理：
   - 新格式：`tool_result` 事件的 `structured_data.observations[]`（自动上报）
   - 旧格式：`tool_name == "report_observation"` 的 `content` JSON（手动上报）
   - 按 `"{object_type}:{name}:{step}"` 去重（:123/:137）
3. `_is_step_observation_known(obs)` 跳过同 step 已见的观测（:1034，dedup key=(scope, object_type, name, step)，scope=`context_id|worker` :1093）
4. `scan_forbidden_truth_fields` 剥离含禁读真值字段的观测（:1078-1082）
5. `EventStore.append(task_id, "observation_report", ...)` 写事件日志（:1099-1101）
6. `SemanticMapStore.ingest_observation(obs)` 合并进语义地图（:1106）；`TaskWatchdog.record_progress(source="observation_report")` 标记任务有进展（:1110-1114）

前置门控：canonical Memory 先写（:1843-1856），legacy 摄入仅在 `allow_legacy_observation_write`（:1858-1863：memory 未启用，或 canonical 写入 ok/duplicate 且 dispatch 已认证）时执行。另有 legacy helper `_extract_observation_from_status_text`（:81，docstring 标 Legacy）生产零调用。

## 5. 运行时状态注入（semantic 模式）

### 5.1 两阶段协议

`SARCoordinatorStateProvider` 实现 `AsyncStatePreparer` 接口，与 `ContextManager` 协作分两个阶段：

**阶段 1：`prepare_for_llm(llm_client)`**（异步，每轮 LLM 调用前）

1. `_try_snapshot_with_revision()` 原子读 `(revision, snapshot)`（provider:425→:461）
2. `_runtime_version = max(_runtime_version, env_step)`（环境步前进也算运行时变化）
3. 若 revision 与上次相同 → 直接返回（no-op）
4. 首次调用 → 建立 baseline，不做 diff
5. revision 变化：
   - `MapDiffCalculator.diff(prev, curr)` 计算结构化差分
   - 差分非空且存在 `MapSummarizer` → `maybe_summarize()`（单飞，LLM 生成中文摘要）
   - 更新 `_last_map_delta` / `_last_summary`

**阶段 2：`snapshot()`**（同步，渲染 Environment State 时调用）

- 语义地图快照在同一 env step 内缓存，避免重复序列化
- `team_status_summary` / `task_status_view` / `recent_changes` / `supervision` 每次重建（廉价）
- 失败时返回上次快照 + `stale=True` + `refresh_error`

### 5.2 注入 payload（semantic 模式）

```python
RuntimeState.payload = {
  "state_mode": "semantic",
  "mission_finished": bool,
  "step_budget": {...},
  "semantic_summary": <SemanticMapStore.snapshot()>,      # 全量地图
  "team_status_summary": {                                 # 团队视图
     "workers": [...],           # worker 观测上报的位置/库存 + AgentRegistry capabilities
     "agent_summaries": [...],   # 人类可读摘要行
     "recent_observations": [...],
     "stale_entries": [...],
     "conflicts": [...],
     "pending_requests": [...],
  },
  "map_revision": int,
  "map_delta": <MapDiffCalculator.diff() 输出或 None>,
  "map_summary": str,            # MapSummarizer 生成的中文摘要
  "map_summary_revision": int,
  "mission_dag_view": [...],     # 任务 DAG 视图
  "physical_dispatches_view": [...],  # 物理 dispatch 视图
  "task_status_view": [...],     # 每个 dispatch 任务的状态
  "recent_changes": [...],       # 最近 5 条人类可读变化行
  "supervision": {...},          # TaskWatchdog 告警
}
```

注意：
- **agent 位置权威源是 worker 观测**（H1-INV-1）：`_build_team_status`（provider:643）docstring 明示 "Barrier/simulator is never read in the online semantic path"（:648-650），位置/库存取自 `self._semantic_map.snapshot()`（:660）——即 worker 上报进语义地图的值；provider 内 barrier 仅用于 `_step_counter`（:453/:537）、`is_finished`（:564）、oracle `global_snapshot`（:595），**不再覆盖** `AgentSemanticState.last_position`。capabilities 仍来自 AgentRegistry（:681-689）
- `long_term_memory` / `system_health` **不在本 payload**——由 EnvironmentStateProvider sections 注入（environment_state_provider.py:50/:53 声明、:442/:456 填充），经 provider `_attach_read_port_provider` 装配
- oracle 模式下不注入 `semantic_summary` / `map_delta` / `map_summary`，改为 `global_snapshot = barrier.get_env_snapshot()`（:593-599，注释明示 oracle 不得暴露 continuity 字段）

## 6. MapDiffCalculator

纯函数差分器（`sar_orch/map/diff.py`），无实例状态、无 I/O、无 LLM 调用。

```python
delta = MapDiffCalculator.diff(prev_snapshot, curr_snapshot,
                               base_revision=r0, revision=r1)
```

输出结构（无变化时返回 `None`）：

```python
{
  "env_step": 12,
  "base_revision": 41, "revision": 43,
  "change_count": 5,
  "fires": {
    "gained":            [{"name","position","intensity"}],
    "lost":              [],                     # 预留，恒空
    "intensity_changed": [{"name","old","new"}],
    "status_changed":    [{"name","old","new"}],
    "position_changed":  [{"name","old","new"}],
    "attributes_changed":[{"name","key","old","new"}],
  },
  "persons":             {同上但无 intensity_changed},
  "conflicts_new":       [{"name"}],
  "conflicts_resolved":  [{"name"}],
  "stale_new":           [完整 entry],
  "stale_resolved":      [{"name"}],
}
```

对齐键 `(object_type, name)`；agent 移动**不计入** change_count。

## 7. MapSummarizer

LLM 驱动的增量摘要器（`sar_orch/map/summarizer.py`），Phase 4 引入。

### 7.1 特性

| 特性 | 实现 |
|------|------|
| 单飞（single-flight） | `asyncio.Lock` + `_last_attempted_revision`，同一 revision 只生成一次 |
| 超时隔离 | `asyncio.wait_for(generate(), timeout=5.0s)`，超时/异常都保留上次成功摘要；乱序防护——旧 revision 迟到的成功响应不回退新摘要（summarizer.py:159-164） |
| 触发条件 | `fire_change` / `person_change` / `conflict` / `stale` / `periodic`（每 5 步） |
| 输入压缩 | `_build_compact_input()`：最多 10 个活动对象 + 5 个 stale/conflict + delta + step_budget + previous_summary，剔除 `sources` / `observed_cells` / `recent_observations` / `confidence` / `last_seen_ts` |
| 输出限制 | `max_summary_chars=150`，Unicode 安全截断 |
| Token 追踪 | `token_usage_sink(agent="MapSummarizer", ...)` 写入 `token_usage.csv` |
| 全量持久化 | 每次尝试（success/timeout/error）追加 `map_summary.jsonl` |

### 7.2 摘要 Prompt

中文 system prompt，要求"只描述已确认的事实变化，不要猜测原因或未来状态"，不超过 150 字。user prompt 携带 compact JSON。

### 7.3 生命周期

在 `SARCoordinator.start()` 中构造，仅当：
- `state_mode == "semantic"`
- `map_summary_path` 已配置（`experiment.py` 传入 `<exp_dir>/map_summary.jsonl`）
- `exp_logger` 已注入（用于 token sink）

否则记 warning 并置 `map_summarizer=None`，StateProvider 跳过摘要阶段。

## 8. 查询工具与 HTTP API

### 8.1 LLM 工具

| 工具 | 端 | 用途 | 当前状态 |
|------|-----|------|----------|
| `query_semantic_map` | Coordinator | 返回 `semantic_map.snapshot()` JSON | **debug/fallback** — semantic 模式下数据已自动注入，不再注册为 LLM 工具 |
| `query_team_status`   | Coordinator | 返回 workers/recent_observations/stale/conflicts | **debug/fallback** — 同上 |
| `query_shared_memory` | Worker | HTTP GET Coordinator `/semantic-map` | **已从 worker 工具装配摘除** — 不在 `SAR_WORKER_TOOLS`（worker/__init__.py:19-36 十六项无它），仅存实现；现行 worker 侧地图查询通道是 Map Agent MCP（`/mcp/map`：get_fire_info / get_person_info / get_reservoir_info / get_task_context / query_natural） |
| `report_observation`  | Worker | 手动上报单条观测 | 仍注册，自动上报之外的补充 |

工具实现在 `sar_orch/tools/coordinator/query_semantic_map.py` / `query_team_status.py` 与 `sar_orch/tools/worker/query_shared_memory.py` / `report_observation.py`。

### 8.2 HTTP 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/semantic-map` | GET | 返回 `SemanticMapStore.snapshot()` JSON（coordinator 全量视图；store.py:370 的 worker_public_snapshot ACL 视图尚未接线）；store 未注入时返回 `{"status":"unavailable",...}` |
| `/map/state`     | GET (SSE) | 500ms 推送实时网格（物理真相，不经过语义地图） |

### 8.3 诊断通道只读工具（query_projection）

`query_projection`（sar_orch/tools/coordinator/query_projection.py:30）**不进 coordinator LLM 工具集**，仅作为 DiagnosisLoop 内部只读证据工具（diagnosis_loop.py:218 装配、:370 特判）。其数据源是 MemoryReadPort 的 spatial/embodied_snapshot（environment_state_provider.py:124/:128）——读 **canonical MemoryStore，不是 SemanticMapStore.snapshot**；ACL 为 scope_id + system principal。即诊断通道与语义地图平级、互不读取。

## 9. 配置与生命周期

### 9.1 初始化顺序（`SARCoordinator.start()`）

```python
semantic_map = SemanticMapStore()
semantic_map.set_jsonl_path(log_dir / "semantic_map.jsonl")
semantic_map.init_priors(
    reservoirs=[],            # Phase 3 H1-INV-1：在线地图纯 worker 证据，
    deposits=[],              #   绝不用 simulator priors 播种（coordinator.py:467-475）
    agents=[{"agent_id": n} for n in _agent_names],
    rules={"Chemical": "Sand", "Non-chemical": "Water"},
    step_budget=self._initial_step_budget(),
    task_objective="Extinguish all fires and rescue all persons",
)
# 注意：set_ground_truth(env.checker.coverage) 并未接线——全仓无生产调用方，
# 线上 map_recall 恒 0.0（见 §3.5）
server.set_semantic_map(semantic_map)                  # 注入 HTTP 层 + /mcp/map

state_provider = SARCoordinatorStateProvider(
    barrier=barrier,
    semantic_map=semantic_map,
    event_store=event_store,
    state_mode="semantic",
    supervision_state_store=...,
    map_summarizer=map_summarizer,   # 可选
    log_dir=...,
    memory_read_mode=...,
    long_term_mode=..., long_term_store=...,
    diagnosis_store=..., diagnosis_inject_enabled=...,
    diagnosis_min_confidence=..., diagnosis_budget_threshold=...,
)
# agent_registry 不在构造参数里：coordinator.py:732-733 构造后
# 直接赋值 state_provider._agent_registry（取自 server._agent_registry）
```

### 9.2 步进同步

`experiment.py` 对每个被记录的 env step（poll 循环内 per-step-log 处理块，drain_step_logs 排空，experiment.py:853-857）调用：

```python
semantic_map.update_step_budget(current_step=step, max_steps=max_steps)
```

`update_step_budget` 内部检测到变化会自增 `_revision`，从而触发下一轮 `prepare_for_llm` 的 diff + 摘要。

### 9.3 实验产物

| 文件 | 内容 |
|------|------|
| `<exp_dir>/semantic_map.jsonl` | 每次 `ingest_observation` 的原始观测 + 合并后对象 |
| `<exp_dir>/map_summary.jsonl`  | 每次摘要尝试（含 trigger_reasons / status / token_usage） |
| `summary.csv` | `map_recall` / `map_freshness` 每步指标列 |

## 10. 设计要点与坑

- **revision 是乐观锁**：`snapshot_with_revision()` 在同一把锁内返回 (revision, snapshot)，StateProvider 据此判断"地图是否变化"，避免基于内容的昂贵比对。
- **环境步前进独立算变化**：即使地图 revision 没变，`env_step` 增加也会推进 `_runtime_version`，防止 ContextManager 版本冻结。
- **Worker 端去重 ≠ Store 端去重**：`WorkerReportPublisher` 是轻量预过滤（减少 A2A 流量），`_merge_locked` + `_is_observation_noteworthy` 才是权威判断。两层并存。
- **fire 聚合**：同一 `parent_fire` 的多个单元格观测**各存各的** `attributes.observed_cells` 条目（`_merge_cell_locked`，store.py:534-582），父火场对象只从白名单 `_CELL_DERIVED_PARENT_KEYS={"fire_type"}`（:424）派生共识；旧"同 step intensity 不同不算冲突"的例外机制已被 cell 模型取代。
- **终态不可回退**：`rescued`/`extinguished`/`complete` 在 `TERMINAL_STATUS_ORDER` 中 rank=3，旧的 `active`/`trapped`（rank=1）无法覆盖。
- **agent 位置权威源是 worker 观测**（H1-INV-1）：在线语义路径禁读 barrier，`AgentSemanticState.last_position` 即权威值，注入 Coordinator 时不再被 barrier 覆盖。参见 `Environment State principles` 与 §5.2。
- **观测大小限制**：`A2AWorkerSink` 对单条 tool_result 的 `content` 字符串限 12000 字符（sink.py:99/:105），超出截断；`structured_data` 不截断。
- **mode 切换**：`--mode oracle` 下 `query_sar_state` 注册为 LLM 工具，语义地图仍运行但**不注入** Context；`--mode semantic` 下 `query_sar_state` 不注册，全靠语义地图注入。
- **stale 阈值**：`snapshot(max_stale_steps=5)`，超过 5 步未再观测的 fire/person 进入 `stale_entries`，是触发"再侦查"决策的信号。

## 11. 交叉引用

- [`框架.md`](框架.md) — 系统整体架构
- [`data_flow.md`](data_flow.md) — A2A / context_id / task_id 数据流追踪
- [`contextmanager.md`](contextmanager.md) — 三层记忆模型与 StateProvider 协作
- [`logging_map.md`](logging_map.md) — 所有日志记录点与输出文件
- [`experiment_design.md`](experiment_design.md) — 实验编排与 `--mode` 开关
