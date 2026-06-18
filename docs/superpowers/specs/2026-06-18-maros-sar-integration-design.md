---
日期: 2026-06-18
文档类型: 技术设计文档
文档概述: MARoS 分布式多智能体系统集成到 LLaMAR SAR 环境的技术设计方案，保留 A2A 协议，采用同步屏障机制
---

# MARoS × LLaMAR SAR 集成设计文档

## 1. 核心目标

将 MARoS 的分布式多 LLM 架构（Coordinator + A2A Worker + `@tool` 机制）接入 LLaMAR 的 SAR（Search & Rescue）网格环境进行实验验证。仅使用 SAR 环境，不涉及 AI2-THOR。

**范围**：仅 SAR 环境，不含 AI2-THOR
**集成方式**：保留 MARoS 的 Coordinator + A2A JSON-RPC 架构，替换底层环境为 SAR
**同步模型**：同步屏障（Barrier）— 每时间步所有 agent 提交 action → 统一执行 → 广播观测

## 2. 两个系统的角色

| 组件 | 来源 | 角色 |
|------|------|------|
| **Coordinator (RouterAgent)** | MARoS | 任务分解、子任务派发、全局协调 |
| **A2A JSON-RPC 协议** | MARoS | Coordinator ↔ Worker 通信 |
| **A2A Transport + @tool + mini-agent** | MARoS | A2A HTTP server、LLM ReAct loop、tool 管理 |
| **`@tool` 装饰器** | MARoS | Tool 定义与 JSON Schema 生成 |
| **TaskLogger** | MARoS | JSONL 结构化日志 |
| **SAREnv + Controller** | LLaMAR | 网格环境、对象管理、action 执行 |
| **Scene 定义 + Checker** | LLaMAR | 救援场景、完成度检查 |
| **SARBarrier** | ★ 新增 | 同步屏障 — 收集 action → env.step() → 广播 obs |

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│  MARoS Coordinator (保留)                                        │
│  RouterAgent (ReAct + tools)                                     │
│  接收任务 → 分解 → push_task 到各 Worker                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ A2A JSON-RPC (push_task)
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Worker Alice │ │ Worker Bob   │ │ Worker Charlie│  ← 自包含 A2A Worker (无 ROS)
│ LLM: 独立    │ │ LLM: 独立    │ │ LLM: 独立    │
│ tools: SAR   │ │ tools: SAR   │ │ tools: SAR   │
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘
       │ action          │ action         │ action
       └─────────────────┼────────────────┘
                         ▼
              ┌─────────────────────┐
              │   SARBarrier        │  ← ★ 新增核心组件
              │   收集所有 agent     │
              │   的 action 后统一    │
              │   调用 env.step()    │
              └──────────┬──────────┘
                         ▼
              ┌─────────────────────┐
              │   SAREnv            │  ← LLaMAR 现有（不变）
              │   Controller        │
              │   Field / GridEngine │
              └──────────┬──────────┘
                         │ observations
                         ▼
              ┌─────────────────────┐
              │   SARBarrier        │
              │   解析 env.step()   │
              │   返回 obs → 广播    │
              └─────────────────────┘
```

### 复用清单

| 来源 | 保留 | 丢弃 |
|------|------|------|
| MARoS | Coordinator, RouterAgent, A2A JSON-RPC, transport.py, `@tool`, mini-agent, TaskLogger, manifest 机制 | ROS 2 (rclpy), A2AWorkerNode 父类, map_server, memory_server, robot_pkgs/, Gazebo |
| LLaMAR | SAREnv, Controller, Field, GridEngine, Scene 定义, Checker, object_actions (部分) | Planner/Verifier/Actor prompt (被 MARoS 的 ReAct 替代) |

## 4. SARBarrier — 同步屏障

### 4.1 设计目标

在保留 SAR 同步 step 语义的前提下，让每个 MARoS Worker 以异步方式提交 action，由 Barrier 统一收集并执行。

### 4.2 接口

```python
class SARBarrier:
    """多 agent 同步屏障，封装 SAREnv"""

    def __init__(self, num_agents: int, scene: int = 1, seed: int = 42):
        self.env = SAREnv(num_agents=num_agents, scene=scene, seed=seed)
        self.env.reset()
        self.num_agents = num_agents
        self._action_queue: dict[int, str] = {}
        self._obs_queue: dict[int, str] = {}
        self._step_ready = asyncio.Event()
        self._obs_events: list[asyncio.Event]  # per-agent obs ready signals
        self._step_counter: int = 0

    async def submit_action(self, agent_idx: int, action: str) -> str:
        """
        Worker 提交 action 并等待执行结果。

        1. 将 action 写入 _action_queue[agent_idx]
        2. 如果所有 agent 都已提交 → 调用 _execute_step()
        3. 等待该 agent 的 _obs_events[agent_idx]
        4. 返回该 agent 的观测文本
        """

    def _execute_step(self):
        """
        1. 按 agent_idx 顺序取出所有 action
        2. 调用 env.step([actions])
        3. 解析每个 agent 的观测
        4. 设置 _obs_events，唤醒所有等待的 Worker
        5. 重置 _action_queue 和 _obs_events
        """

    def is_finished(self) -> bool:
        """检查 checker 是否判定任务完成"""

    def get_metrics(self) -> dict:
        """获取 coverage, transport_rate 等指标"""

    def get_env_snapshot(self) -> dict:
        """获取当前环境状态快照（供 Coordinator query_sar_state 使用）"""
```

### 4.3 超时处理

当第一个 agent 提交 action 时，Barrier 启动一个计时器（默认 30 秒，可配置）。如果超时后仍有 agent 未提交 action，Barrier 自动以 `NoOp` 填充缺失的 agent 并强制执行 step。这防止单个 agent 的 LLM 调用卡死导致整个系统阻塞。

同时，Barrier 维护一个全局的 `task_timeout` 计数器（来自 `env.task_timeout`），在所有 agent 的 `NoOp` 次数超过阈值后自动终止。`submit_action` 返回的观测中会包含 `finished` 标志，Worker 据此决定是否继续 ReAct loop。

### 4.4 关键约束

- Barrier 和所有 Worker 共享同一个 `asyncio` event loop
- 每个 Worker 的 `submit_action` 调用是独立的 `async` 协程
- `_execute_step` 中对 `env.step()` 的调用需要 `asyncio.to_thread()` 包装（因为 `env.step()` 是同步函数，不能阻塞 event loop）

## 5. SAR Worker 设计

### 5.1 结构

SAR Worker 是**自包含的 A2A Worker**，不继承 `A2AWorkerNode`（以避免 ROS 2 依赖），直接组装 MARoS 的无 ROS 组件（`transport.py`、`@tool`、mini-agent）：

```
integration/sar_workers/
├── sar_worker.py       # SARWorker 类，自包含 A2A Worker (~120 行)
├── tools.py            # SAR 领域 @tool 函数
├── skills.py           # Skill 定义
├── prompt.md           # System prompt 模板
└── manifest.yaml        # Worker 元数据
```

### 5.2 SARWorker 类

```python
class SARWorker:
    """自包含 A2A Worker — 不依赖 ROS 2，直接使用 MARoS transport + mini-agent。

    每个 SARWorker 代表 SAR 环境中的一个 agent（Alice/Bob/Charlie...）。
    """

    def __init__(
        self,
        agent_name: str,             # "Alice" / "Bob" / "Charlie" ...
        agent_idx: int,              # 在 SAR 环境中的索引 (0-based)
        barrier: SARBarrier,         # 共享屏障
        port: int,                   # A2A HTTP 端点端口
        coordinator_url: str,        # Coordinator WebSocket 地址
    ):
        self._agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port

        # 1. 绑定 tool（注入 barrier 引用）
        self._tools = [t.bind(self) for t in SAR_TOOLS]

        # 2. 构建初始 system prompt
        self._system_prompt = self._build_system_prompt()

        # 3. 启动 A2A HTTP Server（复用 MARoS transport.py 的 start_a2a_transport）
        self._server = start_a2a_transport(
            worker_id=agent_name,
            tools=self._tools,
            system_prompt_factory=self._build_system_prompt,
            port=port,
            coordinator_url=coordinator_url,
        )

    def _build_system_prompt(self) -> str:
        """动态生成 system prompt，注入当前观测"""
        base = SYSTEM_PROMPT_TEMPLATE  # 从 prompt.md 读取
        obs = self._barrier.get_current_obs(self._agent_idx)
        return base + "\n\n" + obs
```

### 5.3 动态 System Prompt

每次 Barrier 执行 `env.step()` 后，Worker 的 system prompt 会被更新，包含最新的环境状态：

- **局部观测**：agent 周围 8 个方向的物体（`Directly around me, I can see: {Left: ..., Right: ..., ...}`）
- **全局观测**：视野内所有物体的名称列表
- **自身状态**：当前位置、背包内容
- **当前子任务**：Coordinator 分配的子任务描述
- **历史失败记录**：最近失败的 action 列表

## 6. SAR Tools 设计

### 6.1 Tool 映射

LLaMAR 的 9 种 SAR action 映射为 MARoS `@tool` 函数：

| LLaMAR Action | MARoS Tool | 参数 | 说明 |
|---|---|---|---|
| `NavigateTo(obj_id)` | `navigate_to` | `target_id: str` | 导航到目标对象 |
| `Move(direction)` | `move` | `direction: str` | 向指定方向移动一格 |
| `Explore()` | `explore` | 无 | 多步探索周围区域 |
| `Carry(person_id)` | `carry_person` | `person_id: str` | 抬起受困者 |
| `DropOff(person_id, deposit_id)` | `drop_off_person` | `person_id: str, deposit_id: str` | 将受困者送到安全点 |
| `GetSupply(source_id, type)` | `get_supply` | `source_id: str, type: str` | 取灭火物资 |
| `StoreSupply(deposit_id)` | `store_supply` | `deposit_id: str` | 将物资存入仓库 |
| `UseSupply(fire_id, type)` | `use_supply` | `fire_id: str, supply_type: str` | 用物资灭火 |
| `ClearInventory()` | `clear_inventory` | 无 | 清空背包 |
| `NoOp` | `no_op` | 无 | 等待 |

### 6.2 Tool 实现模式

所有 tool 遵循统一的实现模式：

```python
from a2a_lib.tool_decorator import tool

@tool(name="navigate_to", description="Navigate to an object by its ID")
async def navigate_to(node, target_id: str) -> str:
    """
    Navigate to the specified object in the SAR grid.

    Args:
        target_id: ID of the target object (e.g., "WaterSource_1", "GreatFire_Region_1")
    """
    action = f"NavigateTo({target_id})"
    obs = await node._barrier.submit_action(node._agent_idx, action)
    return obs

@tool(name="carry_person", description="Pick up a trapped person. Requires >= 2 agents.")
async def carry_person(node, person_id: str) -> str:
    """
    Pick up a trapped person. At least 2 agents must carry simultaneously.

    Args:
        person_id: ID of the person to carry (e.g., "LostTimmy")
    """
    action = f"Carry({person_id})"
    obs = await node._barrier.submit_action(node._agent_idx, action)
    return obs
```

每个 tool 的关键行为：格式化为 LLaMAR action string → `barrier.submit_action()` → 返回新观测文本。LLM 根据返回的观测文本决定下一步 tool 调用，形成 ReAct loop。

### 6.3 协作 Tool

`carry_person` 需要至少 2 个 agent 同时执行。如果只有一个 agent 执行 `Carry`，action 会失败。Coordinator 需要在任务层保证多个 agent 同时被派发 `carry_person` 子任务。

`drop_off_person` 同样需要所有耦合的 agent 同时到达 deposit 位置才能成功。

## 7. Coordinator 适配

### 7.1 保留的 Router Tools

| 工具 | 用途 |
|------|------|
| `push_task` | 向 SAR Worker 派发子任务 |
| `wait_for_result` | 等待 Worker 完成当前子任务 |
| `cancel_task` | 取消/重新分配子任务 |
| `note` | 记录协调状态 |

### 7.2 新增 SAR 专用 Tool

```python
# query_sar_state — 替代原 query_map
# 直接调用 SARBarrier.get_env_snapshot() 获取当前环境状态：
#   - 所有火源位置、强度、类型
#   - 所有受困人员位置
#   - 所有水源/沙源位置
#   - 所有 agent 位置及背包内容
#   - 已完成的子任务列表
```

### 7.3 System Prompt

```
你是一个搜索救援（Search & Rescue）任务的协调者。你的职责：

1. 接收救援任务描述
2. 将任务分解为子任务，分配给救援机器人（Alice, Bob, Charlie...）
3. 监控子任务完成状态，根据环境变化动态调整计划

可用机器人的能力：
- 每个机器人都可以：移动、取水/沙、灭火、抬人、送人、清空背包
- 抬人（carry_person）需要至少 2 个机器人同时协作
- 每个机器人背包容量为 3 个单位

环境信息（可通过 query_sar_state 获取）：
- 火灾位置及强度（none/low/medium/high），强度会随时间增长
- 火灾类型（chemical/non-chemical），需要不同灭火材料
  - non-chemical: 水或沙均可
  - chemical: 仅沙可灭
- 受困人员位置，每人需要至少 2 个机器人搬运
- 水源（无限水）和沙源（无限沙）位置

你需要关注的全局状态：
- 哪些火正在被处理，哪些尚未分配
- 哪些人员正在被救援，哪些尚未分配
- agent 之间的协作（谁去取物资、谁灭火、谁抬人）
```

## 8. 完整数据流

### 8.1 启动流程

```
1. 创建 SARBarrier(num_agents, scene, seed)
   └── 内部创建 SAREnv + reset()
2. 创建 N 个 SARWorker(agent_name, agent_idx, barrier, port)
   └── 每个 Worker 启动 A2A HTTP server
3. 启动 Coordinator
   └── RouterAgent 连接各 Worker 的 AgentCard
4. 用户通过 A2A 接口提交任务
```

### 8.2 Step 级别数据流

```
Step N:
  ┌─────────────────────────────────────────────────────────┐
  │ 1. 每个 Worker 的 LLM 独立推理                            │
  │    Alice:   "我看到前方有火源，我需要取水"                  │
  │    Bob:     "我在水源旁，我应该取水然后送给 Alice"           │
  │    Charlie: "我在受困者旁边，等待另一个 agent 来帮忙抬人"    │
  │                                                         │
  │ 2. 每个 Worker 调用 tool → barrier.submit_action()       │
  │    Alice:   NavigateTo(WaterSource_1)                    │
  │    Bob:     GetSupply(WaterSource_1, Water)              │
  │    Charlie: NoOp                                        │
  │                                                         │
  │ 3. Barrier 收集齐所有 action → env.step([...])            │
  │    - Alice 移动到 WaterSource_1 ✓                        │
  │    - Bob 从 WaterSource_1 取水 ✓                         │
  │    - Charlie 等待 ✓                                      │
  │                                                         │
  │ 4. Barrier 解析 obs，广播给各 Worker                       │
  │    Alice:   "我在 WaterSource_1，周围有..."               │
  │    Bob:     "我在 WaterSource_1，背包: [Water×1]"         │
  │    Charlie: "我在 LostTimmy 旁，状态不变"                  │
  └─────────────────────────────────────────────────────────┘

Step N+1:
  ┌─────────────────────────────────────────────────────────┐
  │ 1. LLM 根据新 obs 决定下一个 action                        │
  │    Alice:   GetSupply(WaterSource_1, Water)              │
  │    Bob:     NavigateTo(GreatFire_Region_1)               │
  │    Charlie: (Coordinator 通知 Bob 已灭火, 改去抬人)        │
  │                                                         │
  │ 2. 提交 → Barrier → env.step() → 广播 obs                │
  └─────────────────────────────────────────────────────────┘

... 循环直到 checker.check_success() == True 或超时
```

### 8.3 Coordinator 层面的异步交互

```
       Coordinator                         Workers
       (ReAct loop)                   (ReAct loop per agent)
  ┌──────────────────┐           ┌─────────────────────────┐
  │ 分解任务:          │           │                         │
  │ 1. Alice+Bob 灭火  │──push───►│ Alice: 灭火子任务         │
  │ 2. Charlie 看人    │──push───►│ Bob:   灭火子任务         │
  │                    │──push───►│ Charlie: 监控人员         │
  │                    │           │                         │
  │ wait_for_result ───┼──完成?───│ Alice+Bob 灭火完成        │
  │                    │           │                         │
  │ 重新派发:           │           │                         │
  │ Alice+Bob 去抬人    │──push───►│ Alice+Bob: 抬人子任务     │
  │ Charlie 支援灭火    │──push───►│ Charlie: 灭火子任务       │
  │                    │           │                         │
  │ wait_for_result ───┼──完成?───│ 全部完成                  │
  └──────────────────┘           └─────────────────────────┘
```

## 9. 文件布局

```
LLaMAR/
├── integration/                          # 集成代码（全部新增）
│   ├── __init__.py
│   ├── sar_barrier.py                    # ★ SARBarrier：同步屏障 + env 封装 (~120 行)
│   ├── sar_workers/
│   │   ├── __init__.py
│   │   ├── sar_worker.py                 # ★ SARWorker：自包含 A2A Worker (~120 行)
│   │   ├── tools.py                      # ★ SAR 领域 @tool 函数 10 个 (~80 行)
│   │   ├── skills.py                     # Skill 定义 (~30 行)
│   │   ├── prompt.md                     # System prompt 模板
│   │   └── manifest.yaml                 # Worker 元数据
│   ├── coordinator/
│   │   ├── __init__.py
│   │   ├── sar_coordinator.py            # Coordinator 启动 + SAR 适配 (~100 行)
│   │   └── sar_router_tools.py           # query_sar_state tool (~40 行)
│   └── experiment.py                     # 主实验脚本 (~80 行)
│
├── SAR/                                  # LLaMAR 原生 SAR（不动）
│   └── ...
```

**新增代码约 570 行纯 Python + 1 个 prompt.md + 1 个 manifest.yaml。**

## 10. 依赖关系

```
experiment.py
├── SARBarrier
│   └── SAREnv (from SAR/env.py)
│       └── Controller (from SAR/core.py)
├── SARWorker (× N)                       # 自包含，不继承 A2AWorkerNode
│   ├── MARoS transport.py                # A2A HTTP server (无 ROS 依赖)
│   ├── MARoS @tool 装饰器                 # tool → JSON Schema 生成
│   ├── mini-agent (LLM ReAct loop)       # LLM backend
│   ├── SARBarrier (共享实例)
│   └── tools.py
│       └── SARBarrier.submit_action()
└── Coordinator
    ├── MARoS RouterAgent                 # ReAct 任务分解
    └── SARBarrier.get_env_snapshot()     # for query_sar_state
```

**MARoS 依赖**：仅复用以下无 ROS 依赖的组件（通过 PYTHONPATH 引入）：
- `my_a2a/coordinator/` — RouterAgent + RouterTools
- `maros_ws/a2a_lib/a2a_lib/transport.py` — C2FixedMiniAgentAdapter + A2A HTTP server
- `maros_ws/a2a_lib/a2a_lib/tool_decorator.py` — `@tool` 装饰器
- `mini_agent` — LLM ReAct loop（MARoS 的 Python 依赖包）

## 11. ROS 2 依赖问题及解决

### 关键发现

`A2AWorkerNode`（`maros_ws/a2a_lib/a2a_lib/a2a_worker_node.py`）**继承自 `rclpy.node.Node`**，在其 `__init__` 中创建了 ROS publishers、subscribers、callback groups，并使用 `ament_index_python` 定位包资源。这导致它**必须**在 ROS 2 环境下运行。

LLaMAR 的 SAR 环境是纯 Python 实现，不依赖 ROS 2。

### 解决方案：自包含 SARWorker

**不继承 `A2AWorkerNode`**，而是直接在 `SARWorker` 中使用 MARoS 的无 ROS 组件组装一个最小化的 A2A Worker。

`A2AWorkerNode` 的职责拆解：

| 职责 | ROS 依赖? | SAR 集成策略 |
|------|-----------|-------------|
| A2A HTTP Server 启动 | **无** (`transport.py` 用 uvicorn/starlette) | ✅ 直接复用 `transport.py` |
| LLM ReAct loop (mini-agent) | **无** | ✅ 直接复用 |
| `@tool` 装饰器 + tool schema 生成 | **无** | ✅ 直接复用 `tool_decorator.py` |
| WebSocket 向 Coordinator 注册 | **无** (纯 websocket) | ✅ 直接复用（transport 模块内置） |
| ROS publishers/subscribers | **有** | ❌ 舍弃（SAR 不需要 topic 通信） |
| `MapServerClient` | **有** | ❌ 舍弃（被 SARBarrier 替代） |
| `MemoryServerClient` / `MemoryRetriever` | **有** | ❌ 舍弃（SAR 单次运行不要跨 session 记忆） |
| `TaskLogger` | **无** (纯文件写入) | ⚠️ 可选保留 |
| `ament_index` 包资源定位 | **有** | ❌ 替换为普通文件路径 |

### SARWorker 实现概述

```python
class SARWorker:
    """自包含 A2A Worker，不依赖 ROS 2，直接使用 transport + mini-agent。"""

    def __init__(self, agent_name, agent_idx, barrier, port, coordinator_url):
        self._agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port

        # 1. 绑定 tool（注入 barrier 引用）
        self._tools = [t.bind(self) for t in SAR_TOOLS]

        # 2. 构建 system prompt（含当前观测）
        self._system_prompt = self._build_system_prompt()

        # 3. 启动 A2A HTTP Server（复用 MARoS transport.py）
        self._server = start_a2a_transport(
            worker_id=agent_name,
            tools=self._tools,
            system_prompt_factory=self._build_system_prompt,
            port=port,
            coordinator_url=coordinator_url,
        )
```

### Coordinator 的 ROS 依赖

MARoS Coordinator 的 ROS 节点极薄（`ros_node.py` 仅 ~70 行），核心 RouterAgent 逻辑在 `my_a2a/coordinator/` 中，不直接依赖 `rclpy`。SAR 集成中直接使用 `my_a2a` 的 Coordinator Server（FastAPI），跳过 ROS 包装层，或用一个最小的 `asyncio` 启动脚本替代

## 12. 未涉及的部分（明确排除）

- **AI2-THOR 集成**：不在本次范围
- **memory_server**：可选，暂不集成（SAR 场景单次运行，不需要跨 session 记忆）
- **map_server**：被 SARBarrier 替代
- **Gazebo 真机器人仿真**：被 SAR 网格替代
- **chatty GUI**：不需要，通过脚本/API 提交任务
- **对比实验脚本**：设计保留接口，实际脚本在实验阶段实现
