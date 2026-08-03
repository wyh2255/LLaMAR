---
日期: 2026-07-26
文档类型: 系统架构文档
文档概述: 语义地图（Semantic Map）子系统完整设计，涵盖 SemanticMapStore 数据模型、
  Worker→Coordinator 观测摄入管道、MapDiffCalculator 差分、MapSummarizer 摘要、
  StateProvider 运行时注入及相关工具/API
---

# 语义地图（Semantic Map）

## 1. 概述

语义地图是 Coordinator 侧维护的**环境共识层**：Worker 在执行动作时产生结构化观测（observation），通过 A2A push 通知回传 Coordinator，由 `SemanticMapStore` 合并为全局的火灾/被困者/智能体状态视图。在 `--mode semantic`（默认）下，Coordinator 每轮 LLM 调用前自动注入该地图快照 + 增量摘要，**无需调用任何工具**即可获知战场态势。

核心价值：
- **去全知化**：Coordinator 看不到环境的全知快照（ground truth），只能依赖 Worker 上报的语义地图做决策
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
│  push callback → _extract_auto_observations(status_text)              │
│    ├─ 新格式：tool_result.structured_data.observations[]              │
│    └─ 旧格式：report_observation tool_result.content (JSON)           │
│  → EventStore.append("observation_report")                            │
│  → SemanticMapStore.ingest_observation(obs)  ← 合并 + 二次去重        │
│  → TaskWatchdog.record_progress(source="observation_report")          │
└────────────────────────────┬───────────────────────────────────────────┘
                             │
                             ▼
┌─ SARCoordinatorStateProvider (coordinator_state_provider.py) ─────────┐
│  prepare_for_llm() (异步，每轮 LLM 调用前):                            │
│    1. snapshot_with_revision() 原子读取 (revision, snapshot)          │
│    2. revision 变化 → MapDiffCalculator.diff(prev, curr)              │
│    3. delta 非空 → MapSummarizer.maybe_summarize() (单飞)             │
│  snapshot():                                                          │
│    投影 RuntimeState.payload:                                         │
│      semantic_summary / team_status_summary / map_revision /          │
│      map_delta / map_summary / step_budget / task_status_view / ...   │
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
    sources: list[dict]       # 来源记录（保留最近 50 条）
    confidence: float
    conflict: bool            # 同步内属性冲突标记

@dataclass
class AgentSemanticState:     # 智能体状态（精简）
    agent_id, last_position, inventory
    current_task_id, task_state
    last_seen_step, last_message
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
```

### 3.3 合并规则（`_merge_locked`）

每条观测按键（`_record_key`）定位到目标对象后执行合并：

1. **键的选择**：
   - fire 且 `attributes.parent_fire` 非空 → 用 `parent_fire`（把多个火点单元格聚合到同一火场）
   - 否则用 `name`
   - 否则用 `"{type}:{position}"`

2. **属性更新**：
   - 若 `rec.step >= existing.last_seen_step`（新观测更新）
   - 或新状态在 `TERMINAL_STATUS_ORDER` 中排名 ≥ 旧状态**且新状态 rank > 0**（`store.py:373-385` 的 `new_rank > 0` 前置条件；未知状态 rank 恒为 0，不参与竞争，避免一条无法识别 status 的旧观测靠 `0 >= 0` 覆盖掉更新的记录——终态本身不可回退）
   - 同 step 内属性值冲突 → 置 `conflict=True`（fire 的 intensity 除外，允许多单元格不同强度）

3. **位置更新**：仅在 `rec.step >= last_seen_step` 时覆盖

4. **观测记录保留**：仅当满足 `_is_observation_noteworthy`（新对象 / 位置变化 / 属性变化 / 状态变化 / confidence 提升）才追加到 `observations` 列表，避免重复观测占满 1000 条上限

5. **终态保护**：`TERMINAL_STATUS_ORDER = {rescued:3, extinguished:3, complete:3, active:1, trapped:1}`，高 rank 状态不会被低 rank 覆盖

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

`snapshot_with_revision()` 在同一把锁内返回 `(revision, snapshot)`，供 StateProvider 做版本比对。

### 3.5 指标

| 方法 | 含义 |
|------|------|
| `map_recall()` | 已发现对象数 / ground truth 对象数（需 `set_ground_truth`） |
| `freshness()` | 所有对象平均 `current_step - last_seen_step`，越小越新鲜 |

两者都由 `experiment.py` 每步写入 `summary.csv`。

### 3.6 持久化

- `set_jsonl_path(path)` 启用后，每次 `ingest_observation` 追加一行：
  `{"type": "observation_ingested", "observation": {...}, "object": {...}, "ts": ...}`
- 默认路径 `<log_dir>/semantic_map.jsonl`；`experiment.py` 会重定向到实验目录顶层
- Store 内部所有公共方法都持 `_lock`（threading.Lock），多线程安全

## 4. 观测摄入管道

### 4.1 Worker 端

**自动上报**（推荐路径）：每个 SAR 动作工具（`navigate_to` / `explore` / `use_supply` / `carry_person` / `drop_off_person` / `get_supply` / `store_supply` / `move`）通过 `tool_result_from_barrier()` 返回 `ToolResult.data = {observations, position, inventory}`。`_barrier_helpers.py` 中的模块级 `WorkerReportPublisher` 在 `apply_to_data()` 里按 `(name 或 type:pos)` 键过滤掉与上次上报相同的观测，保留 `position`/`inventory` 元数据即使观测全被过滤。

**手动上报**：`report_observation` 工具由 LLM 显式调用，产出单条 JSON 观测。仍然走同一管道。

### 4.2 传输

Worker 的 `A2AWorkerSink` 把 `ToolResult.data` 序列化为 `[DATA] {json}` 块附加到状态更新文本（单条观测内容上限 12000 字符），通过 A2A push notification 推给 Coordinator。

### 4.3 Coordinator 端（`src/a2a/coordinator/server.py`）

push callback 收到状态更新后：

1. `_extract_worker_data_blocks(text)` 解析所有 `[DATA]` JSON 块
2. `_extract_auto_observations(text)` 同时处理两种格式：
   - 新格式：`tool_result` 事件的 `structured_data.observations[]`（自动上报）
   - 旧格式：`tool_name == "report_observation"` 的 `content` JSON（手动上报）
   - 按 `"{object_type}:{name}:{step}"` 去重
3. `_is_step_observation_known(obs)` 跳过同 step 已见的观测
4. `EventStore.append(task_id, "observation_report", ...)` 写事件日志
5. `SemanticMapStore.ingest_observation(obs)` 合并进语义地图
6. `TaskWatchdog.record_progress(source="observation_report")` 标记任务有进展

## 5. 运行时状态注入（semantic 模式）

### 5.1 两阶段协议

`SARCoordinatorStateProvider` 实现 `AsyncStatePreparer` 接口，与 `ContextManager` 协作分两个阶段：

**阶段 1：`prepare_for_llm(llm_client)`**（异步，每轮 LLM 调用前）

1. `snapshot_with_revision()` 原子读 `(revision, snapshot)`
2. `_runtime_version = max(_runtime_version, env_step)`（环境步前进也算运行时变化）
3. 若 revision 与上次相同 → 直接返回（no-op）
4. 首次调用 → 建立 baseline，不做 diff
5. revision 变化：
   - `MapDiffCalculator.diff(prev, curr)` 计算结构化差分
   - 差分非空且存在 `MapSummarizer` → `maybe_summarize()`（单飞，LLM 生成中文摘要）
   - 更新 `_last_map_delta` / `_last_summary`

**阶段 2：`snapshot()`**（同步，渲染 Context Memory 时调用）

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
     "workers": [...],           # 含 barrier 实时位置/库存 + AgentRegistry capabilities
     "agent_summaries": [...],   # 人类可读摘要行
     "recent_observations": [...],
     "stale_entries": [...],
     "conflicts": [...],
  },
  "map_revision": int,
  "map_delta": <MapDiffCalculator.diff() 输出或 None>,
  "map_summary": str,            # MapSummarizer 生成的中文摘要
  "map_summary_revision": int,
  "task_status_view": [...],     # 每个 dispatch 任务的状态
  "recent_changes": [...],       # 最近 5 条人类可读变化行
  "supervision": {...},          # TaskWatchdog 告警
}
```

注意：
- **agent 位置以 barrier 为准**（`_build_team_status` 从 `barrier.get_env_snapshot()` 实时拉取，覆盖 `AgentSemanticState.last_position`），观测只作为兜底

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
| 超时隔离 | `asyncio.wait_for(generate(), timeout=5.0s)`，超时/异常都保留上次成功摘要 |
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
| `query_shared_memory` | Worker | HTTP GET Coordinator `/semantic-map` | **debug/fallback** |
| `report_observation`  | Worker | 手动上报单条观测 | 仍注册，自动上报之外的补充 |

工具实现在 `sar_orch/tools/coordinator/query_semantic_map.py` / `query_team_status.py` 与 `sar_orch/tools/worker/query_shared_memory.py` / `report_observation.py`。

### 8.2 HTTP 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/semantic-map` | GET | 返回 `SemanticMapStore.snapshot()` JSON；store 未注入时返回 `{"status":"unavailable",...}` |
| `/map/state`     | GET (SSE) | 500ms 推送实时网格（物理真相，不经过语义地图） |

## 9. 配置与生命周期

### 9.1 初始化顺序（`SARCoordinator.start()`）

```python
semantic_map = SemanticMapStore()
semantic_map.set_jsonl_path(log_dir / "semantic_map.jsonl")
semantic_map.init_priors(
    reservoirs=...,           # 从 barrier.env 提取
    deposits=...,
    agents=[{"agent_id": n} for n in env.agent_names],
    rules={"Chemical": "Sand", "Non-chemical": "Water"},
    step_budget={"current_step":0, "max_steps":N, "remaining":N},
    task_objective="Extinguish all fires and rescue all persons",
)
semantic_map.set_ground_truth(env.checker.coverage)   # 可选，用于 map_recall
server.set_semantic_map(semantic_map)                  # 注入 HTTP 层

state_provider = SARCoordinatorStateProvider(
    barrier=barrier,
    semantic_map=semantic_map,
    event_store=event_store,
    state_mode="semantic",
    supervision_state_store=...,
    agent_registry=...,
    map_summarizer=map_summarizer,   # 可选
)
```

### 9.2 步进同步

`experiment.py` 每步 poll 循环调用：

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
- **fire 聚合**：同一 `parent_fire` 的多个单元格合并为一个 `SemanticObject`，单元格列表存在 `attributes.observed_cells`；同 step 内不同单元格 intensity 不同不算冲突。
- **终态不可回退**：`rescued`/`extinguished`/`complete` 在 `TERMINAL_STATUS_ORDER` 中 rank=3，旧的 `active`/`trapped`（rank=1）无法覆盖。
- **agent 位置权威源是 barrier**：语义地图里的 `AgentSemanticState.last_position` 仅作兜底，注入 Coordinator 时会被 barrier 实时位置覆盖。参见 `Context Memory principles`。
- **观测大小限制**：`A2AWorkerSink` 对 `report_observation` 内容限 12000 字符，超出会被截断。
- **mode**：`--mode semantic`（唯一取值）下，语义地图状态全靠 `SARCoordinatorStateProvider` 自动注入 Context，无需 LLM 主动调用工具。
- **stale 阈值**：`snapshot(max_stale_steps=5)`，超过 5 步未再观测的 fire/person 进入 `stale_entries`，是触发"再侦查"决策的信号。

## 11. 交叉引用

- [`框架.md`](框架.md) — 系统整体架构
- [`data_flow.md`](data_flow.md) — A2A / context_id / task_id 数据流追踪
- [`contextmanager.md`](contextmanager.md) — 三层记忆模型与 StateProvider 协作
- [`logging_map.md`](logging_map.md) — 所有日志记录点与输出文件
- [`experiment_design.md`](experiment_design.md) — 实验编排与 `--mode` 开关
