# Map Agent MCP 化与队友协作状态增强计划

日期: 2026-07-18
文档类型: 架构设计 / 实施计划
状态: 草案（已经过代码审查，关键事实已修正）
关联文档:
  - [`semantic_map.md`](../system_docs/semantic_map.md) — 语义地图子系统现状
  - [`2026-07-17-map-module-and-step-continuity.md`](2026-07-17-map-module-and-step-continuity.md) — 上一阶段地图模块化成果
  - [`peer_mail.md`](../system_docs/peer_mail.md) — Worker 小队通信机制
  - [`2026-07-18-code-review-report.md`](2026-07-18-code-review-report.md) — 代码审查报告（对照验证）

## 审查修正记录

本文档已经过 subagent 严格代码审查（见 `2026-07-18-code-review-report.md`），以下是审查中发现并已修正的关键事实：

| # | 问题 | 严重度 | 修正位置 |
|---|------|--------|---------|
| 1 | `SARWorkerStateProvider.snapshot()` 是同步方法，原计划写成 `async def snapshot` | 🔴 严重 | 第 2.1 节新增"方案 A/B/C 对比表"，Phase 4 代码改用方案 A（`pre_llm` async 部分拉取 + `snapshot()` 保持同步） |
| 2 | `load_mcp_tools` 函数名错误，实际是 `load_mcp_tools_async` | 🔴 严重 | 第 2.1 节 MCP server 行明确标注函数名 |
| 3 | `mcp` 包来自 `mini-agent` git 依赖（非 anthropic），且 `.venv` 中**没有**安装 | 🟡 中等 | 第 2.1 节修正来源描述，Phase 0 必须先加 pyproject.toml 依赖 |
| 4 | `mcp_loader.py` 不会自动加 server 前缀（如 `map_agent__`） | 🟡 中等 | Phase 1 代码示例改用 `@mcp.tool(name="map_agent__get_fire_info")` 显式命名 |
| 5 | `observed_cells` 位于 `SemanticObject.attributes["observed_cells"]` 而非顶层字段 | 🟢 轻微 | Phase 1 字段裁剪规则明确标注 |
| 6 | `barrier.get_env_snapshot()` 返回的 `inventory` 是 dict（如 `{"supply": "sand", "person": null}`）而非 list | 🟢 轻微 | Phase 4 `/team-status` 代码增加 dict→list 转换逻辑 |
| 7 | `FastMCP().streamable_http_app()` 默认路由是 `/mcp`，直接 mount 会暴露成 `/mcp/map/mcp` | 🟡 中等 | Phase 1 代码示例修正为 `FastMCP(streamable_http_path="/")` |

**审查结论**：计划总体可执行性约 75%，修正上述问题后可达 90%+。

## 用户最新架构决定（2026-07-19）

| # | 决定 | 影响 |
|---|------|------|
| 1 | **Map Agent 用 LangGraph 实现**（Phase B 的 LLM 驱动部分） | Phase 3 技术选型确定为 LangGraph `create_react_agent`；`pyproject.toml` 需加 `langgraph>=0.2.0` 依赖；Map Agent 作为独立 agent 进程，内部用 LangGraph 编排"接收查询 → 调底层工具 → 裁剪返回"的 ReAct 循环 |
| 2 | **Worker 端直接复用 `worker_agent` 的 `load_mcp_tools_async`** | Phase 2 不重写加载逻辑，直接在 `sar_orch/worker.py` 的工具装配段调用 `from Agent.worker_agent.tools.mcp_loader import load_mcp_tools_async`；生成 per-worker `mcp.json` 后 `await load_mcp_tools_async(str(mcp_config_path))` 并入 tools 列表 |

---

## 实施跟踪

| Phase | Subagent | 主 Agent 审查 | 状态 | 说明 |
|---|---|---|---|---|
| 0 | k3 | ✅ 通过 | 完成 | pyproject.toml 加 mcp>=1.28.0 + langgraph>=0.2.0；uv sync 成功（mcp 1.28.0, langgraph 1.2.9） |
| 1 | k3 | ✅ 通过 | 完成 | sar_orch/map_agent/ 4 工具 + FastMCP server + coordinator 集成；25 测试全 GREEN；ruff 无错误 |
| 2 | k3 | ✅ 通过 | 完成 | Worker MCP 接入 + `query_shared_memory` 退役；新建 worker_mcp_config.py；worker.py async 改造（_assemble_tools_async）；prompt 替换；747 tests passed |
| 3 | k3 | ✅ 通过 | 完成 | Map Agent LLM 驱动（Phase B）：llm_query.py（LangGraph create_react_agent）+ server.py 注册 query_natural + coordinator.py 注入 LLM client/token sink；38 tests passed |
| 4 | k3 | ✅ 通过 | 完成 | /team-status 端点 + SARWorkerStateProvider.fetch_team_status_async + hooks.py pre_llm await + context.py _render_team_coordination；28 tests passed |
| 5 | — | ✅ 通过 | 完成 | 真实 smoke：scene 1 / agents 2 / seed 42 / 26 steps / Finished=True；6 检查点全过；报告见 docs/plans/2026-07-19-phase5-smoke-report.md |

---

## 1. 背景与目标

### 1.1 现状问题

当前 `query_shared_memory` 工具的设计存在三个结构性缺陷：

| 维度 | 现状 | 问题 |
|------|------|------|
| 返回内容 | 全量 `SemanticMapStore.snapshot()` JSON（2-5KB） | Worker 拿到 90% 与当前任务无关的信息，token 浪费且易分心 |
| 查询语义 | 无参数，一次性拉取 | 无法表达"我只要 CaldorFire 的类型"这类精确意图 |
| 工具注册 | 硬编码在 `SAR_WORKER_TOOLS`，worker 启动时固定加载 | 想调整返回策略必须改代码并重启所有 worker |
| 管控主体 | 完全由 worker LLM 决定何时调用 | Coordinator 无法干预信息暴露（如避免越位决策） |

同时，多人救援、避免重复灭火等场景需要 worker 感知**队友实时状态**（位置 / 任务 / 是否搬人），当前只有 `team_summary` 提供 team_id 和成员列表，**没有协作语义**。

### 1.2 目标

1. **Map Agent MCP 化**：把 `query_shared_memory` 升级为独立的 Map Agent 服务，通过 MCP 协议暴露给 worker；Coordinator 全程管控该服务的查询策略与返回内容
2. **语义化查询（Phase A）**：提供结构化查询接口（按对象名 / 按意图类型），返回裁剪后的子集
3. **自然语言意图理解（Phase B）**：Map Agent 内部用 LLM 理解自由文本查询，自主决定返回哪些字段
4. **队友协作状态增强**：Worker context 自动注入同队成员的位置 / 任务 / 库存（不含环境信息），支撑多人救援与避免重复劳动

### 1.3 非目标

- **不做主动推送（原 Phase C）**：Map Agent 不主动向 worker 推送地图变化；主动推送由 Router Agent 通过 A2A 通信完成（已有 `send_message` / peer mail 通道），与 MCP 化的 Map Agent 是**两条独立通道**
- 不改变 SAR 环境规则、worker 观测上报格式、A2A 协议
- 不让 worker 直接访问 `SemanticMapStore` 内存对象（始终通过 HTTP/MCP 边界）
- 不在每步、每轮 LLM 调用都拉取队友状态（缓存 + env step 变化才刷新）

---

## 2. 已确认的现状与约束

### 2.1 代码事实（2026-07-18 与源码核对）

| 事实 | 当前实现 | 对本计划的约束 |
|------|---------|---------------|
| `query_shared_memory` 是 worker 工具 | `sar_orch/tools/worker/query_shared_memory.py`，通过 HTTP GET `/semantic-map` | 替换时需要保持同名工具或修改 worker prompt |
| Worker 工具注册是硬编码列表 | `sar_orch/tools/worker/__init__.py` 导出 `SAR_WORKER_TOOLS`，`sar_orch/worker.py` 按类名特判构造 | 新增 MCP 工具需要不同的加载路径（不能放进 `SAR_WORKER_TOOLS`） |
| MCP 客户端已实现 | `src/Agent/worker_agent/tools/mcp_loader.py`，支持 stdio / SSE / streamable_http；`load_mcp_tools_async(config_path)` 返回 `list[Tool]` | 直接复用，无需重写客户端；但**当前代码里没有任何地方调用 `load_mcp_tools_async`**，需要新增 worker 启动时的集成点 |
| MCP 包当前是传递依赖 | `uv.lock` 有 `mcp 1.28.0`（来自 `mini-agent` git 依赖，非 anthropic），但 `pyproject.toml` 未声明，`uv run` 和 `.venv` 环境都 `import mcp` 失败（`.venv/lib/python3.10/site-packages/` 中**没有** mcp 包） | **必须显式加入 `pyproject.toml` 主依赖**，并 `uv sync` 重装环境 |
| MCP server 通过配置文件加载 | `mcp_loader.load_mcp_tools_async(config_path)` 读 `mcp.json`，格式 `{"mcpServers": {...}}`；`type` 支持 `stdio`/`sse`/`http`/`streamable_http`；**注意：函数名是 `load_mcp_tools_async`，不是 `load_mcp_tools`** | worker 启动时需要生成/指向包含 map agent URL 的 mcp.json；Phase 2 代码必须用 `await load_mcp_tools_async(...)` |
| Coordinator 用 FastAPI 暴露 HTTP | `src/a2a/coordinator/server.py`，已有 `/semantic-map`、`/map/state` 等端点 | Map Agent MCP server 可以挂在同一 FastAPI app 下（新路径） |
| Router Agent 有独立的任务分发通道 | `SARCoordinator.submit_task` 用 A2A SDK `send_message`，worker 通过 `finish_task` 回传 | 主动推送走这条通道，与 MCP 无关 |
| Worker 小队元数据由 coordinator 定义 | `CoordinatorTeamRegistry` 维护 team_id / members / endpoints / shared_secret，通过 `TEAM_UPDATE` 信封下发 | 队友列表的权威源在 coordinator，worker 端 `WorkerTeamState` 是只读副本 |
| Worker 邮箱已支持点对点 | `WorkerPeerSenderService.send_mail` + `WorkerMailboxStore`，HMAC 签名 | 队友协作状态不通过邮箱（邮箱是异步、需要 LLM 主动 read），而是 context 注入 |
| `SARWorkerStateProvider` 构造参数 `semantic_map_url` 已传入但**当前未使用** | `sar_orch/worker_state_provider.py:46` 传入但 `_extract_fires_from_obs` 只解析本地观测文本 | 可以复用此参数或新增 `team_status_url`，在 `snapshot()` 里按需拉取 |
| Worker context 已有 pinned state 渲染 | `WorkerContextManager._render_environment_view` / `_render_current_state` | 新增 `_render_team_coordination` 渲染层 |
| Worker 端 `snapshot()` 是同步方法 | `sar_orch/worker_state_provider.py:55` `def snapshot(...)` 是同步；`src/Agent/worker_agent/hooks.py:72` 的 `pre_llm` 是 `async` 但内部调用 `refresh_runtime_state()` 是同步 | **队友状态拉取不能用 async httpx**；可选方案：① 用同步 `httpx.Client` ② 在 `pre_llm` 的 async 部分先拉取再调 `refresh_runtime_state` ③ 给 worker 端 ContextManager 加 `prepare_runtime_state`（对齐 router 端） |
| Router 端有 `AsyncStatePreparer` 两阶段协议 | `src/Agent/router_agent/context.py:169` `prepare_runtime_state` (async) + `refresh_runtime_state` (sync)；worker 端无此协议 | 若 worker 端需要 async 准备阶段，需新增 `prepare_runtime_state` 并修改 `pre_llm` |
| MCP loader 未集成到启动流程 | `load_mcp_tools_async` 存在但**代码里没有任何地方调用它** | Phase 2 需要在 `sar_orch/worker.py` 启动流程中新增调用 |

### 2.2 架构边界

- **Coordinator 权威**：`SemanticMapStore` 只在 coordinator 进程内；worker 永远通过 HTTP/MCP 访问，不共享内存
- **Router Agent vs Map Agent 职责分离**：
  - Router Agent（现有）：决策"分配什么任务、主动推送什么信息"，通道是 A2A `send_message` / peer mail
  - Map Agent（新增）：决策"如何回答地图查询、返回什么粒度的信息"，通道是 MCP
  - 两者**不直接通信**，都读写同一个 `SemanticMapStore`
- **Worker 局部性**：worker 默认只看本地观测 + 队友协作状态；全局环境信息仍需显式 MCP 调用（保留单体抽象）

---

## 3. 目标架构

### 3.1 总体数据流

```text
┌─ Worker (Alice/Bob/...) ─────────────────────────────────────────────┐
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Context Memory (auto-injected each LLM round)                  │  │
│  │  ├─ Pinned State (local)                                       │  │
│  │  │   position / inventory / step / known_fires (local obs)     │  │
│  │  ├─ Team Coordination (NEW, pulled from coordinator)           │  │
│  │  │   teammates: [{agent_id, position, inventory, task}]        │  │
│  │  └─ Mailbox Summary / Team Summary                             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Tools:                                                               │
│   ├─ SAR action tools (navigate_to / use_supply / ...)               │
│   ├─ report_observation / finish_task / read_mailbox / ...           │
│  ✅└─ map_agent__query_map (MCP, NEW) — 语义化地图查询               │
│  ❌└─ query_shared_memory (REMOVED in Phase 2)                       │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │ MCP (streamable_http / sse)
                                 │ HTTP GET /team-status (per env step)
                                 ▼
┌─ Coordinator ─────────────────────────────────────────────────────────┐
│  FastAPI app (src/a2a/coordinator/server.py)                          │
│   ├─ /semantic-map          (existing, full snapshot, debug)          │
│   ├─ /team-status           (NEW, 队友协作状态)                       │
│   ├─ /mcp/map               (NEW, MCP endpoint, mounted FastMCP)      │
│   │                          └─ handled by MapAgent                   │
│   ├─ /map/state             (existing SSE, physical grid)             │
│   └─ A2A push callback      (existing, observation ingestion)         │
│                                                                       │
│  MapAgent (NEW, sar_orch/map_agent/)                                  │
│   ├─ Phase A: 4 structured query tools (no LLM)                       │
│   │    get_fire_info / get_person_info /                              │
│   │    get_reservoir_info / get_task_context                          │
│   └─ Phase B: 1 LLM-driven tool                                       │
│        query_map_natural(query: str, task_context: str)               │
│                                                                       │
│  SemanticMapStore (existing, shared, in-memory)                       │
└───────────────────────────────────────────────────────────────────────┘
                                 ▲
                                 │ A2A send_message / peer mail
                                 │ (Router Agent 主动推送通道，与本计划无关)
┌─ Router Agent (coordinator LLM) ──────────────────────────────────────┐
│  Existing orchestration loop — 不改动                                  │
└───────────────────────────────────────────────────────────────────────┘
```

### 3.2 Map Agent 内部结构（Phase A → Phase B 演进）

```text
Phase A (确定性):
  MCP request → FastMCP router → 4 tool handlers
                                  └─ 直接读 SemanticMapStore.snapshot()
                                  └─ 按 schema 裁剪字段
                                  └─ 返回 JSON

Phase B (LLM 驱动):
  MCP request → FastMCP router → query_map_natural handler
                                  └─ 读 SemanticMapStore.snapshot()
                                  └─ 构造 LLM prompt (snapshot + query + task_context)
                                  └─ LLM 生成裁剪后的 JSON
                                  └─ 返回
```

Phase B 复用现有 LLM client（与 coordinator 相同的 `openai_client`），token 记录到 `MapAgent` 行（类似 `MapSummarizer`）。

---

## 4. 接口契约

### 4.1 Phase A：4 个结构化查询工具

所有工具都是 MCP tool，命名遵循 `<server>__<tool>` 约定（MCP client 自动加前缀）。

#### `map_agent__get_fire_info`

```json
{
  "input": {
    "fire_name": "CaldorFire"        // 可选；不传则返回所有已知火的精简列表
  },
  "output": {
    "fires": [
      {
        "name": "CaldorFire",
        "fire_type": "Chemical",     // 关键灭火决策信息
        "required_supply": "Sand",
        "intensity": "medium",
        "status": "active",
        "regions": [                  // 从 observed_cells 展开
          {"name": "CaldorFire_Region_1", "position": [3,4,0]},
          {"name": "CaldorFire_Region_2", "position": [3,5,0]}
        ],
        "nearest_reservoir_with_sand": {"name": "Reservoir_2", "position": [1,1,0]}
      }
    ]
  }
}
```

**关键裁剪**：不含 `sources` / `observed_cells` 原始观测 / `confidence` / `last_seen_ts`；`nearest_reservoir_with_sand` 由 Map Agent 根据 fire_type 和 reservoir 库存计算（而不是让 worker 自己遍历）。

#### `map_agent__get_person_info`

```json
{
  "input": {
    "person_name": "Person_A"        // 可选
  },
  "output": {
    "persons": [
      {
        "name": "Person_A",
        "position": [5, 7, 0],
        "status": "trapped",         // trapped | being_carried | rescued
        "carriers": ["Bob"],          // 从 agents 里 inventory 含 Person 的查
        "nearest_deposit": {"name": "Deposit_1", "position": [0,0,0]}
      }
    ]
  }
}
```

#### `map_agent__get_reservoir_info`

```json
{
  "input": {
    "supply_type": "Sand"             // 可选: "Sand" | "Water"
  },
  "output": {
    "reservoirs": [
      {"name": "Reservoir_1", "position": [1,1,0], "supply_type": "Sand"}
    ]
  }
}
```

#### `map_agent__get_task_context`

```json
{
  "input": {
    "task_description": "extinguish CaldorFire"   // 自由文本，Map Agent 做关键词匹配
  },
  "output": {
    "mentioned_objects": [
      {"type": "fire", "name": "CaldorFire", "fire_type": "Chemical", "required_supply": "Sand"}
    ],
    "suggested_supply": "Sand",
    "suggested_first_region": "CaldorFire_Region_1"
  }
}
```

**这是 Phase A 的核心价值**：worker 把 coordinator 派发的任务描述原样传过来，Map Agent 返回执行该任务所需的最小信息集。

### 4.2 Phase B：1 个 LLM 驱动工具

#### `map_agent__query_map_natural`

```json
{
  "input": {
    "query": "CaldorFire 是什么类型？需要带什么物资？",
    "worker_task_context": "extinguish CaldorFire",   // 可选，辅助 LLM 判断相关性
    "worker_position": [3, 4, 0]                       // 可选，用于距离计算
  },
  "output": {
    "answer": "CaldorFire 是化学火，需要 Sand。最近的含 Sand 水源 Reservoir_2 位于 (1,1,0)，距离你 4 步。",
    "structured_data": {
      "fire_type": "Chemical",
      "required_supply": "Sand",
      "nearest_reservoir": {"name": "Reservoir_2", "position": [1,1,0]}
    }
  }
}
```

**LLM prompt 要点**（写进 Map Agent 实现）：
- 输入：snapshot JSON + query + task_context + worker_position
- 输出约束：JSON，必含 `answer` (中文, ≤100字) 和 `structured_data` (裁剪后的字段)
- 明确禁止：不允许返回 snapshot 中不存在的对象、不允许推测未来状态

### 4.3 队友协作状态端点

#### `GET /team-status?agent_id=<name>`

**Query 参数**：`agent_id`（必需）— 用于过滤掉自己

**Response**：

```json
{
  "current_step": 12,
  "teammates": [
    {
      "agent_id": "Bob",
      "position": [3, 5, 0],
      "inventory": ["Person"],
      "current_task_id": "rescue_person_A",
      "task_state": "RUNNING",
      "is_carrying_person": true
    },
    {
      "agent_id": "Carol",
      "position": [1, 2, 0],
      "inventory": ["Sand", "Sand"],
      "current_task_id": "extinguish_CaldorFire",
      "task_state": "RUNNING",
      "is_carrying_person": false
    }
  ]
}
```

**数据来源**：
- `position` / `inventory`：`SARBarrier.get_env_snapshot()`（实时，每步刷新）
- `current_task_id` / `task_state`：`SemanticMapStore.agents`（观测驱动，有延迟）
- `is_carrying_person`：从 `inventory` 派生

**关键决策**：只暴露**协作必需**的字段，不含环境对象（火/人位置）— 那些仍由 Map Agent 或 Router Agent 任务描述传递。

### 4.4 Worker Context 渲染

在 `WorkerContextManager` 新增 `_render_team_coordination()`：

```text
Team coordination:
  - Bob: at (3,5,0) | task=rescue_person_A (RUNNING) | [CARRYING PERSON]
  - Carol: at (1,2,0) | task=extinguish_CaldorFire (RUNNING) | carrying Sand x2
```

**渲染规则**：
- 仅在 `teammates` 非空时渲染
- `is_carrying_person=true` 的队友用 `[CARRYING PERSON]` 标记（多人救援决策关键信号）
- 库存渲染简化（Sand x2 / Water x1 / Person），不展开数组

---

## 5. 关键设计决策

### 5.1 为什么 MCP 而不是纯 HTTP？

| 方案 | 优点 | 缺点 |
|------|------|------|
| **纯 HTTP**（worker 直接 httpx 调用 `/mcp/map/...`） | 简单，无 MCP 协议开销 | 工具需要硬编码进 `SAR_WORKER_TOOLS`；不能动态发现；与现有 MCP 生态不兼容 |
| **MCP over streamable_http**（推荐） | 工具自动发现、动态加载；Future 可热插拔其他 MCP server；与 `mcp_loader.py` 现有能力匹配 | 需要维护 `mcp.json` 配置；协议略复杂 |

**选 MCP** 的核心原因是**可演进性**：未来可能有第二个、第三个工具服务（如记忆服务、技能库），MCP 让 worker 端代码零改动。

### 5.2 为什么 Map Agent 挂在 Coordinator FastAPI 而不是独立进程？

| 方案 | 优点 | 缺点 |
|------|------|------|
| **独立进程** | 故障隔离；可独立扩缩容 | 需要跨进程访问 `SemanticMapStore`（共享内存/redis/序列化）；部署复杂度↑ |
| **同一 FastAPI app**（推荐） | 直接读写 `SemanticMapStore`（内存共享）；复用现有 lifecycle；uvicorn 单进程 | Map Agent 故障可能影响 coordinator HTTP |

**选同一进程** 因为 `SemanticMapStore` 是线程安全的（`threading.Lock`），且 Map Agent 查询是只读操作，性能开销可忽略。故障风险通过 FastMCP 的异常隔离（tool 级 try/catch）缓解。

### 5.3 Phase B 的 LLM 选择

**不在 Phase A 就引入 LLM** 的原因：
- Phase A 已经能解决 80% 的噪声问题（结构化查询 + 字段裁剪）
- LLM 引入不确定性（可能返回错误信息、增加延迟、token 成本）
- 需要先用 Phase A 的查询日志评估"什么样的自然语言查询真的需要 LLM"

**Phase B 触发条件**（写进验收标准）：
- Phase A 上线后，统计 `get_task_context` 的 `mentioned_objects` 命中率
- 如果 worker 仍频繁调用全量 `/semantic-map`（说明 Phase A 覆盖不足），才启动 Phase B
- Phase B 的 LLM 使用与 coordinator 相同的 model（`deepseek-v4-flash`），token 记录到 `MapAgent` 行

### 5.4 队友状态的"实时性"权衡

| 方案 | 实时性 | token 成本 | 实现复杂度 |
|------|--------|-----------|----------|
| 每轮 LLM 都拉取 | 最高 | 最高（~100 token/轮） | 简单 |
| **每 env step 拉取一次**（推荐） | 中（同 step 内位置可能变） | 低（~100 token/step） | 需要缓存逻辑 |
| 仅在 mailbox 有消息时拉取 | 低 | 最低 | 复杂，且漏掉无消息的协作场景 |

**选每 env step 一次**，与 `SARWorkerStateProvider` 现有的 `env_step` 缓存机制一致。

### 5.5 为什么队友状态通过 context 注入而非 peer mail？

| 通道 | 延迟 | 需要 LLM 主动 | 适合场景 |
|------|------|--------------|---------|
| **Peer mail** | 异步（worker 需要 read_mailbox） | 是 | 非紧急协调（"我完成了，你可以接手"） |
| **Context 注入**（推荐） | 同步（每步自动） | 否 | 实时协作信号（"Bob 正在搬人，你别去"） |

两者互补：context 注入处理"被动感知"，peer mail 处理"主动沟通"。

---

## 6. 信噪比衡量（Layer 3 目标）

按用户要求，文档标注**未来要做到 Layer 3（KL 散度）**。

### 6.1 分层指标体系

```text
Layer 1: Token 层（每次跑都记录，已在项目里）
  ├─ 总 token / cache hit rate / cost per coverage
  └─ Map Agent 相关：map_agent__* 工具调用的 prompt/completion token

Layer 2: 行为层（离线分析，从 CSV 可算）
  ├─ Action Relevance = 与任务成功相关动作 / 总动作
  ├─ Tool Call Precision = 产生新信息的工具调用 / 总工具调用
  ├─ Recovery Rate = 失败/重试动作占比
  └─ Map Agent 相关：调用 map_agent__* 后下一个动作与返回信息的相关性

Layer 3: Ablation 层（需要对照实验，本计划的目标）
  ├─ 同 scene/seed/agents，A 组用 Phase A 工具，B 组用现有 query_shared_memory
  ├─ 对比：成功率 / 总 token / Action Relevance / Tool Call Precision
  └─ **未来扩展：KL 散度**
     KL(P(action | with_map_agent) ‖ P(action | without_map_info))
     衡量 Map Agent 返回信息对决策分布的实际影响
     需要：① 同一状态下的动作分布采样（多次 LLM 调用） ② 或训练 probe 模型
```

### 6.2 KL 散度的具体实施路径（未来工作）

**不在本计划范围内**，但提前标注需要的基础设施：

1. **动作分布采样**：同一 worker state 下，用 temperature > 0 采样 N 次（如 N=10），得到动作经验分布
2. **对照组**：相同 state，屏蔽 Map Agent 返回（或返回空），再采样 N 次
3. **计算 KL**：`KL(P_with ‖ P_without)`，越高说明 Map Agent 信息越影响决策
4. **工具**：可用 `src/Agent/worker_agent/llm/` 里的 client 直接批量调用

**当前阶段的替代方案**（Layer 2 即可）：用 **Action Relevance** 作为代理指标，通过人工抽样标注 20 个 step 的"动作是否被 Map Agent 返回信息影响"。

---

## 7. 目录与文件改动

### 7.1 新增

```text
sar_orch/
├── map_agent/                      # NEW
│   ├── __init__.py                 # 导出 MapAgent 类
│   ├── server.py                   # FastMCP 实例 + 4 个 Phase A 工具
│   ├── tools.py                    # 工具实现（读 SemanticMapStore）
│   └── llm_query.py                # Phase B 自然语言查询（Phase 3 才实现）
│
docs/plans/
└── 2026-07-18-map-agent-mcp-and-team-coordination.md   # 本文档

tests/
├── test_map_agent_tools.py         # NEW: Phase A 工具契约测试
├── test_map_agent_mcp.py           # NEW: MCP 协议集成测试（用 mcp client 真实调用）
├── test_team_status_endpoint.py    # NEW: /team-status 端点测试
└── test_worker_team_coordination.py # NEW: worker context 渲染测试
```

### 7.2 修改

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | 主依赖加 `mcp>=1.28.0` |
| `src/a2a/coordinator/server.py` | 挂载 FastMCP 到 `/mcp/map`；新增 `/team-status` 端点 |
| `sar_orch/coordinator.py` | 初始化 Map Agent 并传给 server |
| `sar_orch/worker.py` | 生成 worker 的 `mcp.json`（含 map agent URL）；从 `SAR_WORKER_TOOLS` 移除 `QuerySharedMemoryTool`（Phase 2） |
| `sar_orch/worker_state_provider.py` | 新增 team_status 拉取逻辑（每 env step 一次） |
| `src/Agent/worker_agent/context.py` | 新增 `_render_team_coordination` 渲染 |
| `sar_orch/prompts/worker/system.md` | 更新工具列表（移除 query_shared_memory，新增 map_agent__*）；更新多人救援决策提示（引用 team coordination 而非"check team status"） |
| `sar_orch/tools/worker/__init__.py` | 从 `SAR_WORKER_TOOLS` 移除 `QuerySharedMemoryTool`（Phase 2） |
| `docs/system_docs/semantic_map.md` | 更新"查询工具与 HTTP API"章节 |
| `docs/system_docs/data_flow.md` | 新增 MCP 通道的数据流 |
| `docs/system_docs/logging_map.md` | 新增 Map Agent token 记录（`Agent=MapAgent`） |

### 7.3 退役

| 文件 | 何时 |
|------|------|
| `sar_orch/tools/worker/query_shared_memory.py` | Phase 2 完成后标记 deprecated，Phase 5 验证通过后删除 |

---

## 8. 实施计划

### Phase 0：契约测试与数据模型

**目标**：用失败测试锁定 Phase A 工具的输入/输出 schema、`/team-status` 响应格式、worker context 渲染。

**文件**：
- 新建：`tests/test_map_agent_tools.py`
- 新建：`tests/test_team_status_endpoint.py`
- 新建：`tests/test_worker_team_coordination.py`
- 修改：`pyproject.toml`（加 `mcp>=1.28.0` + `langgraph>=0.2.0` 到主依赖）

**pyproject.toml 改动**：

```toml
dependencies = [
    # ... 现有依赖 ...
    "mcp>=1.28.0",
    "langgraph>=0.2.0",  # Phase B: Map Agent LLM 驱动用 create_react_agent
]
```

然后 `uv sync` 重装环境。验证：

```bash
.venv/bin/python -c "import mcp; print(mcp.__version__)"       # 期望 1.28.0+
.venv/bin/python -c "import langgraph; print(langgraph.__version__)"  # 期望 0.2.0+
.venv/bin/python -c "from langgraph.prebuilt import create_react_agent; print('OK')"
```

**关键测试用例**：

```python
# test_map_agent_tools.py
def test_get_fire_info_filters_fields():
    """返回的 fire 不含 sources/observed_cells/confidence/last_seen_ts"""

def test_get_fire_info_includes_nearest_reservoir():
    """Chemical fire 返回最近的 Sand reservoir"""

def test_get_task_context_extracts_fire_name():
    """从 'extinguish CaldorFire' 提取 CaldorFire 并返回 fire_type"""

# test_team_status_endpoint.py
def test_team_status_excludes_self():
    """?agent_id=Alice 的响应不含 Alice"""

def test_team_status_marks_carrying_person():
    """inventory 含 Person 的队友 is_carrying_person=true"""

def test_team_status_uses_barrier_position():
    """position 来自 barrier.get_env_snapshot 而非 semantic map"""

# test_worker_team_coordination.py
def test_render_team_coordination_marks_carrying():
    """渲染时 [CARRYING PERSON] 标记出现"""

def test_render_team_coordination_empty_when_no_teammates():
    """teammates 为空时不渲染该层"""
```

**验收**：所有测试 RED（因为功能未实现），pytest 收集到但 assert 失败。

---

### Phase 1：Map Agent MCP Server（Phase A）

**目标**：实现 4 个结构化查询工具，通过 FastMCP 暴露在 `/mcp/map`。

**文件**：
- 新建：`sar_orch/map_agent/__init__.py`
- 新建：`sar_orch/map_agent/server.py`
- 新建：`sar_orch/map_agent/tools.py`
- 修改：`src/a2a/coordinator/server.py`（挂载 FastMCP）
- 修改：`sar_orch/coordinator.py`（初始化 Map Agent）

**关键实现**：

```python
# sar_orch/map_agent/server.py
from mcp.server.fastmcp import FastMCP
from sar_orch.map import SemanticMapStore

# 注意：streamable_http_path 默认为 /mcp，需要显式设为 / 以便 mount 后路径正确
mcp = FastMCP("map_agent", streamable_http_path="/")

# 注意：mcp_loader.py 不会自动加 server 前缀，工具名需显式带前缀
@mcp.tool(name="map_agent__get_fire_info")
def get_fire_info(fire_name: str | None = None) -> dict:
    """Get fire suppression info (type, required supply, regions, nearest reservoir)."""
    ...

@mcp.tool(name="map_agent__get_person_info")
def get_person_info(person_name: str | None = None) -> dict:
    """Get person rescue info (status, carriers, nearest deposit)."""
    ...

@mcp.tool(name="map_agent__get_reservoir_info")
def get_reservoir_info(supply_type: str | None = None) -> dict:
    """Get reservoir locations filtered by supply type."""
    ...

@mcp.tool(name="map_agent__get_task_context")
def get_task_context(task_description: str) -> dict:
    """Extract mentioned objects from task description and return minimal execution info."""
    ...

def mount_to_fastapi(app, semantic_map: SemanticMapStore):
    """Mount FastMCP to existing FastAPI app at /mcp/map."""
    # FastMCP streamable_http_app 内部路由为 streamable_http_path
    # 设为 / 后，mount("/mcp/map") 的实际端点为 /mcp/map
    app.mount("/mcp/map", mcp.streamable_http_app())
```

**挂载路径验证**（mcp 1.28.0 实测）：
- `FastMCP()` 默认 `streamable_http_path="/mcp"`
- `streamable_http_app()` 返回 Starlette app，内部路由为该路径
- 若 `app.mount("/mcp/map", ...)` 而内部路径是 `/mcp`，实际暴露为 `/mcp/map/mcp`（错误）
- 修正：`FastMCP(streamable_http_path="/")`，使 mount 后路径正好是 `/mcp/map`

**字段裁剪规则**（`tools.py`）：
- 从 `SemanticMapStore.snapshot()` 读取
- 剔除：`sources` / `confidence` / `last_seen_ts` / `last_seen_step` / `recent_observations`
- `observed_cells` 位于 `SemanticObject.attributes["observed_cells"]`（**不是顶层字段**），展开为 `regions` 列表
- 计算派生字段：`nearest_reservoir_with_sand` / `nearest_deposit` / `carriers` / `is_carrying_person`（需额外逻辑，snapshot 不直接提供）

**验收**：Phase 0 的 `test_map_agent_tools.py` 全 GREEN；`test_map_agent_mcp.py` 用真实 MCP client 调用成功。

---

### Phase 2：Worker MCP 接入 + `query_shared_memory` 退役

**目标**：worker 启动时通过 `mcp.json` 加载 Map Agent 工具；移除硬编码的 `QuerySharedMemoryTool`。**直接复用 `worker_agent` 现有的 `load_mcp_tools_async`**，不重写加载逻辑。

**文件**：
- 修改：`sar_orch/worker.py`（在工具装配段插入 MCP 加载逻辑）
- 修改：`sar_orch/tools/worker/__init__.py`（移除 `QuerySharedMemoryTool`）
- 修改：`sar_orch/prompts/worker/system.md`（工具名替换）
- 新建：`sar_orch/worker_mcp_config.py`（生成 per-worker mcp.json 的辅助函数）

**关键实现**（直接复用现有 `load_mcp_tools_async`）：

```python
# sar_orch/worker_mcp_config.py
import json
from pathlib import Path

def write_worker_mcp_config(log_dir: str, agent_name: str, coordinator_http_url: str) -> Path:
    """Generate per-worker mcp.json pointing to the Map Agent endpoint."""
    config = {
        "mcpServers": {
            "map_agent": {
                "url": f"{coordinator_http_url}/mcp/map",
                "type": "streamable_http",
            }
        }
    }
    path = Path(log_dir) / f"mcp_{agent_name}.json"
    path.write_text(json.dumps(config, indent=2))
    return path
```

```python
# sar_orch/worker.py — 在现有工具装配段（worker.py:248-274 附近）插入
from Agent.worker_agent.tools.mcp_loader import load_mcp_tools_async
from sar_orch.worker_mcp_config import write_worker_mcp_config

# ... 现有 SAR_WORKER_TOOLS 循环之后 ...

# 加载 Map Agent MCP 工具
mcp_config_path = write_worker_mcp_config(
    log_dir=log_dir,
    agent_name=self.agent_name,
    coordinator_http_url=http_url,
)
mcp_tools = await load_mcp_tools_async(str(mcp_config_path))
tools.extend(mcp_tools)
logger.info(
    "Loaded %d MCP tools from map_agent: %s",
    len(mcp_tools),
    [t.name for t in mcp_tools],
)
```

**注意**：`load_mcp_tools_async` 是 async 函数，调用点必须在 async 上下文中。`sar_orch/worker.py` 的工具装配段目前在同步方法里 — 需要把这段逻辑移到 async 启动流程（或在 async 入口用 `await` 调用装配函数）。

**`query_shared_memory` 退役**：
- 从 `SAR_WORKER_TOOLS` 移除 `QuerySharedMemoryTool`
- worker prompt 中所有 `query_shared_memory()` 替换为 `map_agent__get_fire_info()` / `map_agent__get_task_context()` 等
- 保留 `sar_orch/tools/worker/query_shared_memory.py` 文件（标记 deprecated），Phase 5 后删除

**验收**：worker 启动日志显示 `Connected to MCP server 'map_agent'`，列出 4 个工具（`map_agent__get_fire_info` / `map_agent__get_person_info` / `map_agent__get_reservoir_info` / `map_agent__get_task_context`）；旧 `query_shared_memory` 不在工具列表。

---

### Phase 3：Map Agent LLM 驱动（Phase B，用 LangGraph 实现）

**目标**：新增 `map_agent__query_natural` 工具，用 LangGraph `create_react_agent` 编排"理解自由文本查询 → 调底层 4 个结构化工具 → 裁剪返回"的 ReAct 循环。

**前提条件**（不满足则不启动）：
- Phase A 上线后至少跑 5 个完整实验
- 统计发现 `get_task_context` 的 `mentioned_objects` 命中率 < 70%（说明关键词匹配不够用）

**文件**：
- 新建：`sar_orch/map_agent/llm_query.py`（LangGraph agent 编排）
- 修改：`sar_orch/map_agent/server.py`（注册新工具）
- 修改：`sar_orch/coordinator.py`（传入 LLM client 和 token sink）

**关键实现**（LangGraph ReAct agent）：

```python
# sar_orch/map_agent/llm_query.py
from langgraph.prebuilt import create_react_agent
from sar_orch.map_agent.tools import (
    get_fire_info,
    get_person_info,
    get_reservoir_info,
    get_task_context,
)

MAP_AGENT_SYSTEM_PROMPT = """You are the SAR Map Agent. Answer worker queries about \
the semantic map by calling the structured tools below, then return a TRIMMED \
summary (no sources/confidence/last_seen_ts/observed_cells).

Tools available:
- get_fire_info(fire_name) — fire type, required supply, regions, nearest reservoir
- get_person_info(person_name) — person status, carriers, nearest deposit
- get_reservoir_info(supply_type) — reservoir locations
- get_task_context(task_description) — extract mentioned objects

Rules:
1. Always prefer calling a tool over guessing.
2. Return at most 3 objects per query.
3. If the query mentions a specific name (fire/person/reservoir), pass it as arg.
4. Strip all metadata from tool output before answering.
"""

def build_map_agent_graph(llm):
    """Build the LangGraph ReAct agent for natural-language map queries."""
    return create_react_agent(
        llm,
        tools=[get_fire_info, get_person_info, get_reservoir_info, get_task_context],
        prompt=MAP_AGENT_SYSTEM_PROMPT,
    )
```

```python
# sar_orch/map_agent/server.py — 新增工具
@mcp.tool(name="map_agent__query_natural")
async def query_natural(
    query: str,
    worker_task_context: str = "",
    worker_position: list[int] | None = None,
) -> dict:
    """Answer free-form map queries via LangGraph ReAct agent."""
    graph = build_map_agent_graph(llm_client)
    result = await graph.ainvoke({
        "messages": [("user", f"Query: {query}\nTask context: {worker_task_context}\nPosition: {worker_position}")]
    })
    # Token tracking: agent="MapAgent"
    token_sink(agent="MapAgent", **extract_usage(result))
    return {"answer": result["messages"][-1].content}
```

**为什么用 LangGraph 而不是手写 LLM 调用**：
- `create_react_agent` 自带 ReAct 循环（思考 → 调工具 → 观察 → 再思考），不需要手写循环
- 工具调用自动解析，与 MCP 工具签名兼容
- 后续可扩展为多步推理（如"先查 fire，再查最近的 reservoir，再规划路径"）

**验收**：`tests/test_map_agent_llm.py` 全 GREEN；benchmark 中 `map_agent__query_natural` 的 token 用量单独记录到 `token_usage.csv`（Agent="MapAgent"）。

**Token 记录**：复用 `ExperimentLogger.log_token_usage`，`agent="MapAgent"`（与 `MapSummarizer` 区分）。

---

### Phase 4：队友协作状态端点 + Worker context 注入

**目标**：实现 `/team-status` 端点；`SARWorkerStateProvider` 每 env step 拉取一次；context 渲染。

**文件**：
- 修改：`src/a2a/coordinator/server.py`（新增端点）
- 修改：`sar_orch/worker_state_provider.py`
- 修改：`src/Agent/worker_agent/context.py`
- 修改：`sar_orch/prompts/worker/system.md`（多人救援决策提示）

**关键实现**：

```python
# src/a2a/coordinator/server.py
@app.get("/team-status")
async def team_status(agent_id: str):
    if self._semantic_map is None or self._barrier is None:
        return {"teammates": [], "current_step": 0}
    
    env_snap = self._barrier.get_env_snapshot()
    live = {a.get("name"): a for a in env_snap.get("agents", [])}
    
    map_snap = self._semantic_map.snapshot()
    teammates = []
    for agent in map_snap.get("agents", []):
        aid = agent["agent_id"]
        if aid == agent_id:
            continue  # 排除自己
        live_data = live.get(aid, {})
        # barrier 返回的 inventory 是 dict（如 {"supply": "sand", "person": null}），需转 list
        raw_inv = live_data.get("inventory") or agent.get("inventory") or {}
        if isinstance(raw_inv, dict):
            inv = [k for k, v in raw_inv.items() if v]
        else:
            inv = list(raw_inv)
        teammates.append({
            "agent_id": aid,
            "position": live_data.get("position") or agent.get("last_position"),
            "inventory": inv,
            "current_task_id": agent.get("current_task_id", ""),
            "task_state": agent.get("task_state", "UNKNOWN"),
            "is_carrying_person": bool(raw_inv.get("person")) if isinstance(raw_inv, dict) else "Person" in inv,
        })
    return {
        "teammates": teammates,
        "current_step": map_snap["step_budget"]["current_step"],
    }
```

```python
# sar_orch/worker_state_provider.py
# 方案 A：在 pre_llm 的 async 部分拉取（推荐，最小改动）
# src/Agent/worker_agent/hooks.py
async def pre_llm(self, agent: Any, messages: list) -> list:
    # 新增：async 拉取 team status（每 env step 一次）
    if hasattr(self._ctx._state_provider, 'fetch_team_status_async'):
        await self._ctx._state_provider.fetch_team_status_async()
    self._ctx.refresh_runtime_state()
    self._ctx.prune_history(agent.messages)
    return self._ctx.assemble(agent.system_prompt, agent.messages)

# sar_orch/worker_state_provider.py
class SARWorkerStateProvider:
    async def fetch_team_status_async(self):
        """Fetch team status from coordinator, cache by env_step."""
        if not self._team_status_url:
            return
        env_step = getattr(self._barrier, "_step_counter", 0) if self._barrier else 0
        if env_step == self._last_team_status_step:
            return  # 同一步内不重复拉取
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._team_status_url}/team-status",
                    params={"agent_id": self._agent_name},
                )
                resp.raise_for_status()
                data = resp.json()
                self._cached_teammates = data.get("teammates", [])
                self._last_team_status_step = env_step
        except Exception:
            pass  # 静默降级到上次缓存
    
    def snapshot(self, context_id=None):
        # ... 现有逻辑（保持同步） ...
        payload["team_coordination"] = {
            "teammates": self._cached_teammates,
            "teammates_count": len(self._cached_teammates),
        }
```

**方案对比**：

| 方案 | 改动范围 | 风险 | 推荐度 |
|------|---------|------|--------|
| A. `pre_llm` 中 await + `snapshot` 保持同步 | worker hooks.py + worker_state_provider.py | 低 | **推荐** |
| B. `snapshot()` 改 async + 所有调用方改 await | worker_agent/context.py + agent.py + hooks.py | 高（破坏现有契约） | 不推荐 |
| C. 同步 `httpx.Client` 在 `snapshot()` 里 | worker_state_provider.py | 中（阻塞 event loop） | 备选 |

```python
# src/Agent/worker_agent/context.py
def _render_team_coordination(self) -> str:
    tc = self._runtime_state.get("team_coordination", {})
    teammates = tc.get("teammates", [])
    if not teammates:
        return ""
    
    lines = ["Team coordination:"]
    for t in teammates:
        aid = t["agent_id"]
        pos = t.get("position")
        inv = t.get("inventory", [])
        task = t.get("current_task_id", "idle")
        state = t.get("task_state", "UNKNOWN")
        
        # 库存简化渲染
        inv_str = self._format_inventory(inv)
        carrying = " [CARRYING PERSON]" if t.get("is_carrying_person") else ""
        lines.append(f"  - {aid}: at {pos} | task={task} ({state}) | {inv_str}{carrying}")
    return "\n".join(lines)

@staticmethod
def _format_inventory(inv: list) -> str:
    if not inv:
        return "empty"
    if "Person" in inv:
        return "carrying Person"
    from collections import Counter
    counts = Counter(inv)
    return " + ".join(f"{item} x{n}" for item, n in counts.items())
```

**Prompt 更新**（`sar_orch/prompts/worker/system.md`）：

```diff
- 4. **Person rescue**: Check if another robot is also carrying before you try to move. Check the team status in the Context Memory block. If you need to wait, use `no_op()`.
+ 4. **Person rescue**: Check the Team coordination block in Context Memory — if a teammate is marked [CARRYING PERSON], navigate to a deposit and wait with `no_op()`. Person rescue requires **2+ robots** carrying simultaneously.
```

**验收**：Phase 0 的 `test_team_status_endpoint.py` 和 `test_worker_team_coordination.py` 全 GREEN。

---

### Phase 5：真实 smoke 验证

**目标**：跑 scene 1 / agents 2 / seed 42，验证多人救援和避免重复灭火场景。

**步骤**：

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 30
```

**检查点**：

1. **工具发现**：worker 启动日志含 `✓ Connected to MCP server 'map_agent'`，列出 4 个工具
2. **MCP 调用**：`agent_interactions.csv` 出现 `map_agent__get_task_context` 或 `map_agent__get_fire_info` 调用
3. **Context 注入**：worker 的 `<task>.ndjson` 中 LLM 输入含 `Team coordination:` 块
4. **多人救援**：如果出现 person，观察 worker 是否根据 `[CARRYING PERSON]` 标记做决策
5. **Token 对比**：与 baseline（同 scene/seed 用旧 `query_shared_memory`）对比总 token
6. **指标**：`summary.csv` 的 `map_recall` / `freshness` 无回归

**验收**：
- 实验成功完成（不崩溃）
- Worker 至少调用一次 Map Agent 工具
- 至少一个 worker 的 context 中出现 `Team coordination:` 块
- 总 token 相比 baseline 不增加超过 10%（如果减少更好）

---

## 9. 风险与回退

| 风险 | 影响 | 缓解 | 回退方案 |
|------|------|------|---------|
| MCP 协议不稳定导致 worker 启动失败 | 高 | Phase 2 先用 try/except 包裹 MCP 加载，失败时降级到 `query_shared_memory` | 恢复 `SAR_WORKER_TOOLS` 中的 `QuerySharedMemoryTool` |
| FastMCP 与现有 FastAPI 冲突 | 中 | 挂在子路径 `/mcp/map`，不影响现有端点 | 独立端口运行 Map Agent |
| Phase B LLM 返回错误信息误导 worker | 中 | Phase B 启动前提条件（命中率 < 70%）；prompt 严格约束输出 | 关闭 `query_map_natural` 工具，只留 Phase A |
| `/team-status` 增加 coordinator 负载 | 低 | 每 env step 一次（不是每轮 LLM）；响应字段精简 | 加 feature flag 关闭注入 |
| Worker prompt 改动导致行为回归 | 中 | Phase 5 跑完整 benchmark 对比成功率 | 回退 prompt 到上一版本 |

---

## 10. 开放问题（留给后续）

1. **Map Agent 与 Router Agent 的协同**：目前两者独立；未来是否让 Router Agent 通过 Map Agent 的查询日志了解"worker 在问什么"，用于改进任务分配？
2. **多 Map Agent 实例**：如果 worker 数量增加（>10），单点 Map Agent 是否成为瓶颈？是否需要按 team 分片？
3. **Layer 3 KL 散度测量**：需要采样基础设施（同 state 多次 LLM 调用），是否值得为此建一个离线评估框架？
4. **队友状态的"新鲜度"标识**：当前 `last_seen_step` 未暴露给 worker；是否需要在 context 渲染时标注"3 步前的位置"？

---

## 11. 交叉引用

- [`semantic_map.md`](../system_docs/semantic_map.md) — 语义地图现状（本文档实施后需要更新第 8 章）
- [`peer_mail.md`](../system_docs/peer_mail.md) — 小队通信（与本计划的 context 注入互补）
- [`data_flow.md`](../system_docs/data_flow.md) — 需要新增 MCP 通道的数据流图
- [`logging_map.md`](../system_docs/logging_map.md) — 需要新增 `Agent=MapAgent` 的 token 记录点
- [`2026-07-17-map-module-and-step-continuity.md`](2026-07-17-map-module-and-step-continuity.md) — 上一阶段成果（SemanticMapStore / MapDiffCalculator / MapSummarizer）
