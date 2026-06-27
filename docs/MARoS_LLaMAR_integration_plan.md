---
日期: 2026-06-12
文档类型: 技术方案
文档概述: 将 MARoS 分布式多智能体系统集成到 LLaMAR 实验环境中进行测试对比的技术方案
---

# MARoS × LLaMAR 集成测试方案

## 1. 背景与动机

### 1.1 两套系统简介

| | **MARoS** | **LLaMAR** |
|---|---|---|
| **全称** | Multi-Agent Robot Orchestration System | Long-Horizon Planning for Multi-Agent Robots |
| **核心范式** | 分布式 A2A 多智能体系统 | 集中式 Plan-Act-Correct-Verify 循环 |
| **协调方式** | Coordinator (RouterAgent ReAct loop) + 每个 Agent 独立 LLM | 单个 LLM 同时为所有 Agent 生成动作 |
| **通信协议** | A2A HTTP/JSON-RPC + ROS 2 服务 | Prompt 内嵌 `SendMessage` + 共享记忆文本 |
| **环境** | 10×10 食物广场 (map_server + Gazebo) | AI2-THOR 3D 室内 + SAR 30×30 网格搜救 |
| **Agent 类型** | 异构 (drone/cleaning/delivery/arm/patrol) | 同构 (Alice/Bob/Charlie... 能力完全相同) |
| **记忆系统** | SQLite + sqlite-vec (长期持久化) | Prompt 内嵌 (previous observation/action/failure) |
| **任务分解** | Coordinator → 层级分解 → 分发到 worker | Planner prompt → subtask list, 所有 agent 共享 |
| **发表** | 待发表 | NeurIPS 2024 |

### 1.2 为什么要做这个集成

**核心科学问题**：在多智能体任务规划中，**分布式多 LLM**（MARoS 范式）与**集中式单 LLM**（LLaMAR 范式）哪种更优？

LLaMAR 提供了经过验证的实验环境（已发表 NeurIPS 2024），正好可以作为 MARoS 的**第三方测试平台**。通过这个集成可以：

1. **公平对比**：在完全相同的任务上对比两种范式的 success rate / efficiency
2. **消融分析**：隔离 MARoS 各组件的贡献（Coordinator / per-agent LLM / memory / A2A）
3. **生态扩展**：为 MARoS 增加 SAR 和 AI2-THOR 两个成熟的任务领域
4. **验证泛化**：证明 MARoS 架构不依赖特定仿真器

---

## 2. 系统架构对比

### 2.1 LLaMAR 集中式架构

```
┌──────────────────────────────────────────┐
│              GPT-4V/Turbo                │
│                                          │
│  Planner ──► Actor ──► Corrector ──► Verifier │
│     ▲                                    │     │
│     └─────────── feedback ──────────────┘     │
└──────────┬────────────────────┬──────────────┘
           │                    │
     ┌─────▼─────┐       ┌─────▼─────┐
     │  AI2-THOR │       │    SAR    │
     │  3D 室内  │       │  网格搜救  │
     └───────────┘       └───────────┘
```

- 单个 LLM 调用输出所有 agent 的动作
- 观察、记忆、计划都在一个 prompt 上下文中
- 动作解析：LLM 文本 → `parse_action()`  → 环境执行

### 2.2 MARoS 分布式架构

```
┌──────────────┐
│  Coordinator │  LLM (claude-opus-4-5)
│  RouterAgent │  任务分解 + 分发
└──────┬───────┘
       │ A2A HTTP/JSON-RPC
       ▼
┌───────────────────────────────────────────┐
│  Worker: drone         LLM: deepseek-v4   │
│  Worker: cleaning_bot  LLM: deepseek-v4   │
│  Worker: delivery_bot  LLM: deepseek-v4   │
│  Worker: robot_arm     LLM: deepseek-v4   │
│  Worker: patrol_bot    LLM: deepseek-v4   │
│                                            │
│  每个 Worker 独立 ReAct loop              │
│  Tool selection → map_server 调用          │
└──────────────┬────────────────────────────┘
               │ ROS 2 服务
               ▼
       ┌───────────────┐
       │   map_server  │  10×10 网格
       │   Gazebo      │  物理仿真
       └───────────────┘
```

- 每个 agent 独立 LLM，独立决策
- Coordinator 负责任务分解和分派
- 通过 map_server 的 12 个 ROS 2 服务操作环境

---

## 3. 集成方案对比

### 方案 A：完整 ROS 2 环境适配器（最重）

实现一个遵循 `map_server` 全部 12 个 ROS 2 服务接口的节点，内部封装 LLaMAR 环境。

```
MARoS Workers (不改)
    │ ROS 2 服务
    ▼
┌──────────────────────────┐
│  LLaMARMapServerNode     │  ← 新实现
│  接口 = map_server 的    │
│  12个 ROS 2 服务         │
│  内部 = SAR/AI2Thor env  │
└──────────────────────────┘
```

| 优点 | 缺点 |
|------|------|
| MARoS 代码零改动 | 工程极重：ROS 2 + AI2THOR Python 环境冲突 |
| 测试完整部署链路 | 语义不匹配：食物广场 vs 家庭/搜救场景 |
| | AI2-THOR 同步 API 包装为异步 ROS 2 服务极其别扭 |

**结论**：❌ 不推荐，工程代价远超收益

### 方案 B：Worker 抽象替换

每个 MARoS worker 继承 `A2AWorkerNode`，替换 `_get_tools()` 返回的 tool 实现。

| 优点 | 缺点 |
|------|------|
| 复用 A2A + per-agent LLM | 需为每个 LLaMAR 动作重写所有 tool |
| 保持 MARoS 通信机制 | 仍需运行 ROS 2 基础设施 |

**结论**：⚠️ 中等，但 ROS 2 依赖是负担

### 方案 C：轻量直接集成（最轻）

写新的实验脚本，直接调用 LLaMAR 环境 + MARoS 的 LLM 组件，去掉 ROS 2。

| 优点 | 缺点 |
|------|------|
| 最灵活，无 ROS 2 依赖 | 不测试 MARoS 完整部署链路 |
| 可直接做对比实验 | |

### 方案 D：Tool 实现替换

替换 MARoS worker 中 tool 的**实现体**（从调 map_server → 调 LLaMAR 环境）。

| 优点 | 缺点 |
|------|------|
| 测试决策链路 (LLM tool selection + ReAct) | 绕过了 map_server |
| 不经过 ROS 2，更纯粹 | |

**结论**：✅ 推荐 C + D 混合

---

## 4. 推荐方案：分层适配器架构

**核心思路**：保留 MARoS 的**决策层**（Coordinator 任务分解 + per-agent LLM + ReAct tool selection），替换 tool 的**执行层**（从调 map_server → 调 LLaMAR 环境）。

### 4.1 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                    MARoS_LLaMAR 集成实验                          │
│                                                                   │
│  ┌─────────────────────┐      ┌───────────────────────────────┐  │
│  │ MARoS 决策层 (保留)  │      │ LLaMAR 环境层 (保留)           │  │
│  │                     │      │                               │  │
│  │ Coordinator         │      │ SAREnv(agents, scene, seed)   │  │
│  │  RouterAgent        │      │   .reset() → observation_str  │  │
│  │  任务分解 → 分发     │      │   .step(actions) → (obs, suc) │  │
│  │                     │      │                               │  │
│  │ Per-Agent LLMs      │      │ AI2ThorEnv(args)              │  │
│  │  mini-agent ReAct   │      │   .reset(task) → obs_str      │  │
│  │  Tool selection     │      │   .step(actions) → (obs, suc) │  │
│  └─────────┬───────────┘      └───────────────▲───────────────┘  │
│            │                                    │                  │
│            │    ┌───────────────────────────────┘                  │
│            │    │                                                  │
│            ▼    │                                                  │
│  ┌──────────────────────────────────────────┐                     │
│  │          适配器层 (新增)                    │                     │
│  │                                           │                     │
│  │  ┌─────────────────┐  ┌────────────────┐  │                     │
│  │  │ LLaMARObsAdapter│  │LLaMARActMapper │  │                     │
│  │  │                 │  │                │  │                     │
│  │  │ env obs str     │  │ tool call      │  │                     │
│  │  │ → MapState dict │  │ → action str   │  │                     │
│  │  │                 │  │                │  │                     │
│  │  │ 含：grid map,   │  │ navigate()     │  │                     │
│  │  │ objects list,   │  │ → NavigateTo() │  │                     │
│  │  │ agent states,   │  │ pick()         │  │                     │
│  │  │ semantic tags   │  │ → PickupObj()  │  │                     │
│  │  └─────────────────┘  └────────────────┘  │                     │
│  └──────────────────────────────────────────┘                     │
│            │                                                       │
│            ▼                                                       │
│  ┌──────────────────────────────────────────┐                     │
│  │        LLaMARWorkerNode (新增)             │                     │
│  │                                           │                     │
│  │  继承自 A2AWorkerNode 的模式，重写         │                     │
│  │  _get_tools() —— 所有 tool 实现            │                     │
│  │  都通过适配器调 LLaMAR 环境                 │                     │
│  └──────────────────────────────────────────┘                     │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 数据流

```
1. 任务输入: 自然语言 (如 "extinguish all fires, rescue LostPersonTimmy")
                 │
2. Coordinator RouterAgent: 将任务分解为子任务
   [{subtask_id: "1", skill: "firefighting", target: "CaldorFire", agent: "Alice"},
    {subtask_id: "2", skill: "rescue", target: "LostPersonTimmy", agents: ["Bob","Charlie"]}]
                 │
3. 分发到各 LLaMARWorker
                 │
4. Worker LLM (mini-agent ReAct loop):
   while not done:
     a. 获取当前 observation (通过 LLaMARObsAdapter)
     b. LLM 选择 tool
     c. 执行 tool → LLaMARActMapper 转换为 env action → env.step()
     d. 获取结果 → 判断是否继续
                 │
5. 所有 Worker 完成 → 上报 Coordinator
                 │
6. Coordinator 判断任务完成 / 重新规划
```

### 4.3 需要新建/修改的文件

```
LLaMAR/
├── integration/                          # 新增目录
│   ├── __init__.py
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── obs_adapter.py               # LLaMARObsAdapter
│   │   │   # class SARObsAdapter        SAR observation → MapState dict
│   │   │   # class AI2ThorObsAdapter    AI2THOR observation → MapState dict
│   │   │
│   │   └── action_mapper.py             # LLaMARActionMapper
│   │       # class SARActionMapper       tool call ↔ SAR action string
│   │       # class AI2ThorActionMapper  tool call ↔ AI2THOR action string
│   │
│   ├── worker.py                        # LLaMARWorker
│   │   # class LLaMARWorker             A2AWorkerNode-like, 进程内运行
│   │   # 重写 _get_tools() 使用适配器
│   │
│   ├── coordinator.py                   # 简化版 Coordinator
│   │   # class IntegrationCoordinator   进程内任务分解 + 分发
│   │   # 去掉 A2A 网络层
│   │
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── sar_tools.py                 # SAR 领域 tool 定义
│   │   │   # navigate_to, extinguish_fire, rescue_person,
│   │   │   # get_supply, store_supply, explore, ...
│   │   │
│   │   └── ai2thor_tools.py            # AI2THOR 领域 tool 定义
│   │       # navigate_to, pickup_object, put_object,
│   │       # open_object, close_object, slice_object, ...
│   │
│   ├── experiment.py                    # 主实验脚本
│   │   # class IntegrationExperiment
│   │   # run_episode(scene_id, agents, max_steps)
│   │   # 记录 success_rate, steps, llm_calls, tokens
│   │
│   └── compare.py                       # 对比实验
│       # run_sar_comparison()
│       # run_ai2thor_comparison()
│       # 在相同种子/场景下跑 LLaMAR baseline vs MARoS-integration
│       # 输出对比表格 + 图表
```

### 4.4 核心接口设计

#### `LLaMARObsAdapter` (SAR 为例)

```python
class SARObsAdapter:
    """
    将 SAR 环境的文本 observation 转换为 MARoS 风格的 MapState dict。
    """

    def to_map_state(self, env_obs_str: str, agent_name: str) -> dict:
        """
        返回:
        {
            "grid": [[...], ...],          # 2D grid 每个 cell 的 object 列表
            "objects": [                    # 全局物体列表
                {
                    "id": "CaldorFire",
                    "type": "fire",
                    "position": (15, 22),
                    "properties": {
                        "intensity": "MEDIUM",
                        "fire_type": "chemical",
                        "average_intensity": "Medium"
                    }
                },
                ...
            ],
            "agents": [
                {
                    "name": "Alice",
                    "position": (14, 20),
                    "inventory": {"Sand": 1, "Water": 0, "Person": 0}
                },
                ...
            ],
            "my_state": {                   # 当前 agent 的状态
                "position": (14, 20),
                "inventory": {...},
                "local_view": {
                    "Up": [...],
                    "Down": [...],
                    ...
                }
            }
        }
        """
```

#### `LLaMARActionMapper` (SAR 为例)

```python
class SARActionMapper:
    """
    将 MARoS worker 的 tool 调用映射为 LLaMAR SAR 动作字符串。
    """

    # Tool → SAR action string
    TOOL_ACTION_MAP = {
        "navigate_to":     "NavigateTo({target_name})",
        "move":            "Move({direction})",
        "explore":         "Explore",
        "extinguish_fire": "UseSupply({fire_name}, {resource_type})",
        "get_supply":      "GetSupply({source_name}, {resource_type})",
        "store_supply":    "StoreSupply({deposit_name})",
        "rescue_carry":    "Carry({person_name})",
        "rescue_dropoff":  "DropOff({deposit_name}, {person_name})",
        "clear_inventory": "ClearInventory",
        "wait":            "Idle",
    }

    def tool_to_action(self, tool_name: str, tool_args: dict) -> str:
        """将 tool 调用转换为 env.step() 可接受的动作字符串"""

    def parse_tool_result(self, action_success: bool, env_obs: str) -> str:
        """将环境执行结果转换为 tool 返回格式"""
```

#### `LLaMARWorker` (SAR 为例)

```python
class LLaMARWorker:
    """
    进程内运行的 MARoS 风格 worker。
    保留：per-agent LLM + ReAct loop + tool selection
    替换：tool 实现 → LLaMAR 环境调用
    去掉：A2A HTTP、ROS 2 依赖
    """

    def __init__(self, agent_name: str, env, obs_adapter, action_mapper,
                 llm_config: dict):
        self.agent_name = agent_name
        self.env = env
        self.obs_adapter = obs_adapter
        self.action_mapper = action_mapper
        self.agent = self._build_mini_agent(llm_config)

    def _get_tools(self) -> list:
        """返回 SAR 相关的 tool 列表，实现体内调 LLaMAR 环境"""
        return [
            tool("navigate_to", self._navigate_to),
            tool("extinguish_fire", self._extinguish_fire),
            tool("get_supply", self._get_supply),
            ...
        ]

    async def execute_subtask(self, subtask: dict) -> TaskResult:
        """
        执行一个子任务。
        mini-agent 在 ReAct loop 中选择 tool，
        tool 通过 action_mapper 转换为 env.step() 调用。
        """
```

#### `IntegrationCoordinator`

```python
class IntegrationCoordinator:
    """
    简化版 Coordinator，进程内运行。
    保留：RouterAgent 任务分解 + 分派逻辑
    去掉：A2A HTTP 服务、WebSocket 注册
    """

    def __init__(self, llm_config: dict, workers: dict[str, LLaMARWorker]):
        self.router_agent = self._build_router_agent(llm_config)
        self.workers = workers

    async def run_task(self, task_description: str) -> ExperimentResult:
        """
        1. RouterAgent 分解任务 → subtask list
        2. 按依赖关系分发 subtask 到 worker
        3. 等待所有 worker 完成
        4. 汇总结果
        """
```

---

## 5. 实验设计

### 5.1 SAR 场景对比

| 场景 | 火源数 | 人员数 | 资源点 | 最优步数 | 挑战 |
|------|--------|--------|--------|----------|------|
| Scene 1 | 2 | 1 | 2 reservoir + 1 deposit | ~30 | 基础：协调灭火+救援 |
| Scene 2 | 3 | 1 | 2 reservoir + 1 deposit | ~45 | 火势分散，需分工 |
| Scene 3 | 2 | 2 | 3 reservoir + 1 deposit | ~55 | 多人救援需要 ≥2 agent 协作 |
| Scene 4 | 4 | 2 | 3 reservoir + 2 deposit | ~70 | 大规模，资源竞争 |
| Scene 5 | 3 | 1 | 2 reservoir + 1 deposit | ~40 | 火势蔓延快，时间压力 |

**假设**：

- H1: MARoS 的 per-agent LLM 在需要**并行分工**的场景（Scene 3, 4）中效率更高
- H2: LLaMAR 的集中式 LLM 在需要**全局协调**的场景（Scene 4, 5）中更不易犯错
- H3: MARoS 的 event-driven replanning 在**动态环境**（火势蔓延）中有优势

### 5.2 评估指标

| 指标 | 计算方式 | 含义 |
|------|----------|------|
| **Success Rate** | 完成任务数 / 总实验数 | 成功率 |
| **Steps to Complete** | 完成任务所用步数 | 效率 |
| **LLM Calls** | 总 LLM API 调用次数 | 通信开销 |
| **Total Tokens** | 总 token 消耗 | 成本 |
| **Coverage** | 被处理的火源/人员比例 | 任务覆盖面 |
| **Coordination Errors** | 冲突动作数 (如重复灭火) | 协调质量 |
| **Failure Recovery** | 从失败中恢复的次数 | 鲁棒性 |

### 5.3 消融实验

| 实验条件 | Coordinator | per-agent LLM | Memory | 目的 |
|----------|-------------|---------------|--------|------|
| Full MARoS | ✅ | ✅ | ✅ | 完整 MARoS |
| -Coordinator | ❌ | ✅ | ✅ | 消融 Coordinator 的作用 |
| -Memory | ✅ | ✅ | ❌ | 消融长期记忆 |
| Single LLM (LLaMAR) | ❌ | ❌ (统一 LLM) | ❌ | LLaMAR baseline |

---

## 6. 实施计划

### Phase 1: SAR 适配器 + 基础 Worker（预计 3-5 天）

1. 实现 `SARObsAdapter` — 解析 SAR observation 文本，转换为结构化 MapState dict
2. 实现 `SARActionMapper` — tool ↔ action string 双向映射
3. 实现 `LLaMARWorker` — 单 agent 在 SAR 环境中的 ReAct loop
4. 单元测试：单 agent 完成 `navigate_to` + `extinguish_fire` 基本任务

### Phase 2: Coordinator 集成（预计 2-3 天）

1. 实现 `IntegrationCoordinator` — 进程内 RouterAgent
2. 集成多 worker + Coordinator → 端到端运行
3. 在 Scene 1 上完成 "extinguish fire + rescue person" 任务

### Phase 3: 对比实验（预计 2-3 天）

1. 在 5 个 SAR 场景上跑 MARoS integration
2. 在相同种子/场景上跑 LLaMAR baseline
3. 生成对比表格和图表
4. 消融实验

### Phase 4: AI2-THOR 扩展（预计 3-5 天，可选）

1. 实现 `AI2ThorObsAdapter` + `AI2ThorActionMapper`
2. 在 4 种 MAP-THOR 任务类型上测试
3. 对比实验

---

## 7. 风险和缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| SAR observation 文本解析不稳定 | 适配器输出错误 MapState | 用 SAR unit tests 做回归测试；必要时在 core.py 中加结构化 API |
| MARoS mini-agent 与 SAR 动作空间不匹配 | LLM 频繁选择无效 tool | 在 tool description 中明确列出可行参数；用 embedding 做 closest-match |
| Coordinator 任务分解对 SAR 场景不适应 | 分解出不合理子任务 | 在 RouterAgent 的 system prompt 中加入 SAR 领域知识 + few-shot 示例 |
| LLaMAR 环境是同步的，MARoS 期望异步 | 阻塞问题 | SAR env 本身是同步的，Worker 串行执行即可；多 Worker 用 asyncio 协程交替 step |
| llm.py 硬编码 GPT 格式 | 切换到 MARoS 的 LLM 配置困难 | integration 层用自己的 LLM 调用，不依赖 LLaMAR 的 llm.py |

---

## 8. 总结

这个集成方案用**最小的工程代价**（不需要 ROS 2、不修改 MARoS 核心代码）让 MARoS 的多智能体系统在 LLaMAR 的成熟实验环境中运行，实现：

- 🎯 **公平的基线对比** — 分布式多 LLM vs 集中式单 LLM
- 🔬 **可控的消融分析** — 逐个移除 Coordinator / Memory / per-agent LLM
- 📊 **成熟的评估框架** — 复用 LLaMAR 的 5 个 SAR 场景 + 4 种 MAP-THOR 任务
- 🚀 **清晰的扩展路径** — 先 SAR 后 AI2-THOR，逐步推进
