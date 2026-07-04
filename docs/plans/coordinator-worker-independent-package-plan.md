---
日期: 2026-06-23
文档类型: 技术方案
文档概述: 独立 Coordinator + Worker 包重构方案，消除对 openharness_a2a 的依赖，仅保留 a2a-sdk 作为协议层依赖，构建可跨项目复用的任务协调框架
---

# 独立 Coordinator + Worker 包重构方案

## 1. 背景与目标

### 1.1 现状问题

当前 `integration/coordinator/` 对 `openharness_a2a` 有 **7 处导入**，涉及 14 个类/函数：

| 模块 | 导入的类 |
|------|----------|
| `router_agent` | `RouterAgent` |
| `router_tools` | `BashTool, CancelTaskTool, PushTaskTool, QueryMemoryTool, SessionNoteTool, WaitForResultTool` |
| `router_tools.base` | `RouterTool, ToolResult` |
| `a2a_server` | `create_coordinator_a2a_server` |
| `agent_executor` | `CoordinatorAgentExecutor` |
| `routes` | `health, workers` |
| `server` | `CoordinatorServer` |

核心矛盾：`SARCoordinator` 创建了一个完整的 `CoordinatorServer`（触发 rclpy、mini_agent、a2a_lib 初始化），然后只借用其 4 个内部属性（`_llm_client`, `_agent_registry`, `_memory_client`, `_task_queue`），再自己重写了 `_build_app()` 的大部分逻辑。

### 1.2 重构目标

- **独立可复用**：构建不绑定任何特定项目的 coordinator 和 worker 包
- **协议兼容**：与现有 MARoS worker 的 A2A 协议互通
- **最小依赖**：仅保留 `a2a-sdk`（Google 官方协议库）作为外部协议依赖
- **代码简洁**：消除重复代码，提升可读性

### 1.3 确认的决策

| # | 决策 | 结论 |
|---|------|------|
| 1 | 外部协议依赖 | 仅 `a2a-sdk`，通过 `uv` 管理 |
| 2 | Worker 包 | 不含 mini_agent 适配，参考 coordinator 架构 |
| 3 | QueryMemoryTool | 保留代码，不注册到 RouterAgent |
| 4 | health/workers 路由 | 保留 |

---

## 2. 目标架构

### 2.1 目录结构

```
integration/
├── coordinator/
│   ├── __init__.py               # 公开 API：SARCoordinator
│   ├── sar_coordinator.py        # 重构后的主协调器（消除 CoordinatorServer 依赖）
│   ├── router_agent.py           # 本地化 RouterAgent（支持 system_prompt 参数）
│   ├── llm_shim.py               # LLM 客户端（保持不变）
│   ├── tools/
│   │   ├── __init__.py           # 导出所有工具类
│   │   ├── base.py               # RouterTool, ToolResult, PendingTask, extract_final_text
│   │   ├── push_task.py          # PushTaskTool
│   │   ├── wait_for_result.py    # WaitForResultTool
│   │   ├── cancel_task.py        # CancelTaskTool
│   │   ├── query_memory.py       # QueryMemoryTool（保留但不注册）
│   │   ├── query_sar_state.py    # QuerySARStateTool（原 sar_router_tools.py）
│   │   ├── session_note.py       # SessionNoteTool
│   │   └── bash.py               # BashTool
│   └── a2a/
│       ├── __init__.py           # 导出 A2A 核心类
│       ├── types.py              # 数据模型（DistributedTask, TaskStatus 等）
│       ├── task_queue.py         # 任务队列
│       ├── agent_registry.py     # Agent 注册中心
│       ├── coordinator_server.py # A2A JSON-RPC 服务（create_coordinator_a2a_server）
│       └── coordinator_executor.py # CoordinatorAgentExecutor
│
├── worker/                       # 独立 Worker 包
│   ├── __init__.py               # 公开 API：SARWorker
│   ├── worker.py                 # Worker 主类（A2A 注册 + 任务接收 + 心跳）
│   └── executor.py               # 任务执行器基类 + SAR 适配
│
├── test_sar_coordinator.py       # 更新 import 路径
└── ...（其他现有文件保持不变）
```

### 2.2 依赖关系

```
coordinator/
├── 外部依赖: a2a-sdk, fastapi, uvicorn, httpx, openai/anthropic, pyyaml
├── 不依赖: openharness_a2a, mini_agent, rclpy, ROS2
└── 导出: SARCoordinator, RouterAgent

worker/
├── 外部依赖: a2a-sdk, fastapi, uvicorn
├── 不依赖: openharness_a2a, mini_agent
└── 导出: SARWorker, TaskExecutor, SARTaskExecutor
```

### 2.3 数据流

```
experiment.py
  → SARCoordinator.start()
       → _build_router()              # RouterAgent + 7 个工具
       → _build_app()                 # FastAPI + A2A server + WebSocket
       → uvicorn.Server               # 启动 HTTP 服务
  → SARCoordinator.submit_task()
       → RouterAgent.run()            # ReAct 循环：LLM 决策 → 工具调用 → 返回
            → PushTaskTool            # 通过 A2A JSON-RPC 分发给 Worker
                 → SARWorker          # 接收任务 → TaskExecutor.execute() → 返回结果
```

---

## 3. 核心模块设计

### 3.1 RouterAgent（本地化）

**文件**: `coordinator/router_agent.py`（~300 行）

**来源**: 从 `openharness_a2a.coordinator.router_agent` 提取，消除餐厅场景硬编码。

**关键改动**:
- 构造函数接受 `system_prompt` 参数（替代硬编码的 `ROUTER_SYSTEM_PROMPT`）
- 消除 `SARRouterAgent` 子类，直接用 `RouterAgent(system_prompt=SAR_COORDINATOR_SYSTEM_PROMPT, ...)`
- 记忆注入逻辑保持不变

```python
class RouterAgent:
    """LLM 驱动的任务路由器 — 接收自然语言任务，分解为子任务并分发。"""

    def __init__(
        self,
        system_prompt: str,              # 支持自定义 prompt
        llm_client,
        registry=None,
        memory_client=None,
        max_iterations: int = 10,
    ):
        self._system_prompt = system_prompt
        self._llm_client = llm_client
        self._registry = registry
        self._memory_client = memory_client
        self._max_iterations = max_iterations
        self._tools: dict[str, RouterTool] = {}
        self._messages: list = []

    def register_tool(self, tool: RouterTool) -> None:
        """注册一个工具供 ReAct 循环使用。"""
        self._tools[tool.name] = tool

    def _build_system_prompt(self, user_request: str) -> str:
        """构建系统提示词 — 注入智能体信息和历史记忆。"""
        agents_text = ""
        if self._registry is not None:
            agents_text = self._registry.get_all_agents_prompt_text()
        base = self._system_prompt.format(agents_text=agents_text)

        # 被动记忆注入
        if user_request and self._memory_client is not None:
            try:
                past = self._memory_client.search_events_sync(
                    query=user_request, top_k=3, event_type="user_request"
                )
                if past:
                    base += "\n\n## Past similar user requests:\n"
                    for i, ev in enumerate(past, 1):
                        payload = ev.get("payload", {})
                        base += (
                            f"{i}. User: {payload.get('user_request', '')[:200]}\n"
                            f"   Routed to: {payload.get('subtasks', '')}\n"
                            f"   Reasoning: {payload.get('reasoning', '')[:200]}\n\n"
                        )
            except Exception as e:
                logger.warning(f"Memory injection failed: {e}")
        return base

    async def run(self, user_request: str) -> str:
        """ReAct 循环：LLM 思考 → 工具调用 → 观察 → 直到完成。"""
        ...
```

### 3.2 SARCoordinator（重构）

**文件**: `coordinator/sar_coordinator.py`（~250 行，当前 508 行）

**关键改动**:
- 消除 `CoordinatorServer` 依赖，直接创建轻量组件
- 消除 `SARRouterAgent` 子类，用 `RouterAgent(system_prompt=...)` 替代
- `pending_tasks` 从 `start()` 局部变量提升为实例属性
- WebSocket handler 抽取为独立方法

```python
class SARCoordinator:
    def __init__(
        self,
        barrier,                         # SARBarrier
        agent_names: list[str],          # ["Alice", "Bob", ...]
        worker_ports: dict[str, int],    # {"Alice": 8191, "Bob": 8192}
        port: int = 8080,
        model: str = "gpt-4o",
    ):
        self._barrier = barrier
        self._agent_names = list(agent_names)
        self._worker_ports = dict(worker_ports)
        self._port = port
        self._model = model
        self._pending_tasks: dict = {}

        # 构建系统提示词
        agent_descs = []
        for name in self._agent_names:
            wp = self._worker_ports.get(name, 8190)
            agent_descs.append(
                f"- **{name}**: SAR rescue robot. A2A endpoint at "
                f"http://localhost:{wp}/. Can navigate, collect supplies, "
                f"extinguish fires, carry persons, explore."
            )
        self._system_prompt = SAR_COORDINATOR_SYSTEM_PROMPT.format(
            agents_text="\n".join(agent_descs),
        )

        # 延迟初始化
        self._router: Optional[RouterAgent] = None
        self._server_task: Optional[asyncio.Task] = None
        self._experiment_logger = None

    async def start(self):
        """启动协调器：建组件 → 建 Router → 建 App → 启动 uvicorn。"""
        import uvicorn
        from integration.coordinator.llm_shim import SimpleLLMClient
        from integration.coordinator.a2a.agent_registry import AgentRegistry
        from integration.coordinator.a2a.task_queue import TaskQueue

        # 1. 直接创建轻量组件（不经过 CoordinatorServer）
        self._llm_client = SimpleLLMClient(model=self._model)
        self._agent_registry = AgentRegistry()
        self._task_queue = TaskQueue()

        # 2. 构建 RouterAgent + 工具
        self._router = self._build_router()

        # 3. 构建 FastAPI 应用
        app = self._build_app()

        # 4. 启动 uvicorn
        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="info")
        self._uvicorn_server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._uvicorn_server.serve())

        logger.info("SAR Coordinator starting on port %d with agents: %s",
                     self._port, ", ".join(self._agent_names))
        await asyncio.sleep(0.5)

    def _build_router(self) -> RouterAgent:
        """创建 RouterAgent 并注册所有 SAR 工具。"""
        from integration.coordinator.tools import (
            PushTaskTool, WaitForResultTool, CancelTaskTool,
            QuerySARStateTool, SessionNoteTool, BashTool,
        )

        router = RouterAgent(
            system_prompt=self._system_prompt,
            llm_client=self._llm_client,
            registry=self._agent_registry,
        )

        # 任务类工具
        router.register_tool(PushTaskTool(
            registry=self._agent_registry,
            pending_tasks=self._pending_tasks,
        ))
        router.register_tool(WaitForResultTool(self._pending_tasks))
        router.register_tool(CancelTaskTool(
            task_queue=self._task_queue,
            pending_tasks=self._pending_tasks,
        ))

        # 查询类工具
        router.register_tool(QuerySARStateTool(self._barrier))

        # 辅助类工具
        router.register_tool(SessionNoteTool())
        router.register_tool(BashTool())

        return router

    def _build_app(self) -> "FastAPI":
        """构建 FastAPI 应用，注册 A2A 服务和路由。"""
        ...

    async def _handle_worker_ws(self, websocket: WebSocket, worker_id: str):
        """Worker WebSocket 连接处理（独立方法，可测试）。"""
        ...

    async def submit_task(self, task_description: str) -> str:
        """提交任务到 RouterAgent 进行分解和分配。"""
        ...

    async def stop(self):
        """优雅关闭协调器服务。"""
        ...
```

### 3.3 A2A 协议层

**目录**: `coordinator/a2a/`（~550 行）

从 `openharness_a2a` 提取，修改 import 路径，保持协议兼容。

| 文件 | 行数 | 来源 | 改动 |
|------|------|------|------|
| `types.py` | ~96 | `shared/types.py` | 直接复制，零改动 |
| `task_queue.py` | ~99 | `task_queue.py` | 修改 import 路径 |
| `agent_registry.py` | ~175 | `agent_registry.py` | 修改 import 路径 |
| `coordinator_server.py` | ~70 | `a2a_server.py` | 修改 import 路径 |
| `coordinator_executor.py` | ~207 | `agent_executor.py` | 修改 import 路径，适配本地 RouterAgent |

**保留 `a2a-sdk` 依赖**：这是 Google A2A 协议的官方实现，保证与 MARoS worker 的协议兼容。

### 3.4 Tools 子包

**目录**: `coordinator/tools/`（~500 行）

从 `openharness_a2a.coordinator.router_tools` 提取，修改 import 路径。

| 文件 | 行数 | 来源 | 改动 |
|------|------|------|------|
| `base.py` | ~90 | `router_tools/base.py` | 直接复制，零改动 |
| `push_task.py` | ~127 | `router_tools/push_task.py` | 修改 import 路径 |
| `wait_for_result.py` | ~87 | `router_tools/wait_for_result.py` | 修改 import 路径 |
| `cancel_task.py` | ~42 | `router_tools/cancel_task.py` | 修改 import 路径 |
| `query_memory.py` | ~57 | `router_tools/query_memory.py` | 修改 import 路径（保留但不注册） |
| `query_sar_state.py` | ~69 | `sar_router_tools.py` | 修改 import 路径 |
| `session_note.py` | ~70 | `router_tools/note_tool.py` | 修改 import 路径 |
| `bash.py` | ~70 | `router_tools/bash_tool.py` | 修改 import 路径 |

### 3.5 Worker 包

**目录**: `worker/`（~300 行，新增）

```python
# worker/worker.py
class SARWorker:
    """A2A Worker — 向 Coordinator 注册自己，接收任务并执行。"""

    def __init__(
        self,
        name: str,                       # "Alice"
        coordinator_url: str,            # "http://localhost:8080"
        executor: "TaskExecutor",        # 任务执行器
        port: int = 8191,                # 本 Worker 的 A2A 服务端口
    ):
        self._name = name
        self._coordinator_url = coordinator_url
        self._executor = executor
        self._port = port
        self._server_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动 A2A 服务，向 Coordinator 注册。"""
        # 1. 创建 A2A server（使用 a2a-sdk）
        # 2. 启动 HTTP 服务
        # 3. 向 Coordinator 发送注册请求
        ...

    async def stop(self):
        """优雅关闭。"""
        ...

# worker/executor.py
class TaskExecutor(ABC):
    """任务执行器基类 — 子类实现具体的 execute 逻辑。"""

    @abstractmethod
    async def execute(self, task_description: str, context: dict) -> str:
        """执行任务并返回结果文本。"""
        ...

class SARTaskExecutor(TaskExecutor):
    """SAR 场景执行器 — 调用 SARBarrier 执行动作。"""

    def __init__(self, barrier):
        self._barrier = barrier

    async def execute(self, task_description: str, context: dict) -> str:
        """将任务描述转换为 SAR 动作序列并执行。"""
        ...
```

---

## 4. 删除的文件/依赖

| 删除项 | 原因 |
|--------|------|
| `from openharness_a2a.*` 全部 7 处导入 | 本地化替代 |
| `CoordinatorServer` 依赖 | 拆解为轻量组件 |
| `SARRouterAgent` 子类 | 合并到 `RouterAgent(system_prompt=...)` |
| `worker_registry.py` | SAR 场景 Worker 数量固定，不需要 |
| `mesh_guide.py` | SAR 场景不需要 Worker 间直连 |
| `_maros_compat.py` | 不再需要 MARoS 兼容层 |

---

## 5. 依赖变化

```
Before:
  openharness_a2a (7 处导入)  ← 消除
  a2a-sdk                     ← 保留
  fastapi, uvicorn, httpx     ← 保留
  mini_agent                  ← 消除
  rclpy, ROS2                 ← 消除

After:
  a2a-sdk                     ← 唯一外部协议依赖（uv 管理）
  fastapi, uvicorn, httpx     ← 已有依赖，不变
  openai/anthropic            ← LLM 客户端，不变
  pyyaml                      ← 配置解析，不变
```

---

## 6. 执行计划

### Phase 1：创建 `coordinator/a2a/` 子包

**目标**: 从 `openharness_a2a` 提取 A2A 协议层，消除对 `a2a_server` 和 `agent_executor` 的导入。

**步骤**:
1. 创建 `coordinator/a2a/__init__.py`
2. 复制 `shared/types.py` → `a2a/types.py`（零改动）
3. 复制 `task_queue.py` → `a2a/task_queue.py`（修改 import）
4. 复制 `agent_registry.py` → `a2a/agent_registry.py`（修改 import）
5. 复制 `a2a_server.py` → `a2a/coordinator_server.py`（修改 import）
6. 复制 `agent_executor.py` → `a2a/coordinator_executor.py`（修改 import，适配本地 RouterAgent）

**预计**: ~550 行，消除 2 处 openharness_a2a 导入。

### Phase 2：创建 `coordinator/tools/` 子包

**目标**: 从 `openharness_a2a.coordinator.router_tools` 提取工具类。

**步骤**:
1. 创建 `coordinator/tools/__init__.py`
2. 复制 `router_tools/base.py` → `tools/base.py`（零改动）
3. 复制 6 个工具文件 → `tools/`（修改 import）
4. 移动 `sar_router_tools.py` → `tools/query_sar_state.py`（修改 import）

**预计**: ~500 行，消除 2 处 openharness_a2a 导入。

### Phase 3：创建 `coordinator/router_agent.py`

**目标**: 本地化 RouterAgent，支持 `system_prompt` 参数。

**步骤**:
1. 从 `openharness_a2a.coordinator.router_agent` 提取 RouterAgent
2. 修改构造函数，接受 `system_prompt` 参数
3. 删除 `ROUTER_SYSTEM_PROMPT` 硬编码
4. 删除 `SARRouterAgent` 子类（从 `sar_coordinator.py`）

**预计**: ~300 行，消除 1 处 openharness_a2a 导入。

### Phase 4：重构 `coordinator/sar_coordinator.py`

**目标**: 消除 CoordinatorServer 依赖，精简启动流程。

**步骤**:
1. 修改 `start()`：直接创建 `SimpleLLMClient` + `AgentRegistry` + `TaskQueue`
2. 修改 `_build_router()`：使用本地 `RouterAgent`，消除 `SARRouterAgent`
3. 修改 `_build_app()`：使用本地 `a2a.coordinator_server` 和 `a2a.coordinator_executor`
4. 抽取 `_handle_worker_ws()` 为独立方法
5. 删除 `SARRouterAgent` 类定义

**预计**: 508 行 → ~250 行，消除 2 处 openharness_a2a 导入。

### Phase 5：创建 `worker/` 包

**目标**: 构建独立 Worker 包，参考 coordinator 架构。

**步骤**:
1. 创建 `worker/__init__.py`
2. 创建 `worker/worker.py`（SARWorker 主类）
3. 创建 `worker/executor.py`（TaskExecutor 基类 + SARTaskExecutor）

**预计**: ~300 行。

### Phase 6：更新测试 + 端到端验证

**目标**: 确保重构后功能不变，A2A 协议兼容。

**步骤**:
1. 更新 `test_sar_coordinator.py` 的 import 路径
2. 修复 `TestHandleWorkerWS` 测试（与 `_handle_worker_ws` 方法对齐）
3. 运行回归测试
4. 端到端验证：`python integration/experiment.py --scene=1 --agents=2`

**预计**: ~200 行测试代码。

---

## 7. 不动的文件

| 文件 | 原因 |
|------|------|
| `llm_shim.py` | 保持不变，SimpleLLMClient 继续使用 |
| `sar_barrier.py` | 保持不变 |
| `experiment.py` | Phase 4 后如果 SARCoordinator 构造函数签名变化则更新 |
| 其他 integration/ 下的现有文件 | 保持不变 |

---

## 8. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| a2a-sdk 接口变化 | 低 | 编译错误 | 锁定 a2a-sdk 版本 |
| 本地化 RouterAgent 行为差异 | 中 | 任务分解结果不同 | 保留原始代码注释，对比测试 |
| A2A 协议 wire format 不兼容 | 低 | Worker 无法连接 | 端到端测试验证 |
| CoordinatorAgentExecutor 适配问题 | 中 | 任务执行失败 | Phase 1 后立即测试 |
| import 路径遗漏 | 低 | ModuleNotFoundError | 全局搜索验证 |

---

## 9. 验收标准

- [ ] `grep -r "from openharness_a2a" integration/coordinator/` 返回空
- [ ] `grep -r "from openharness_a2a" integration/worker/` 返回空
- [ ] `pytest integration/test_sar_coordinator.py -v` 全部通过
- [ ] `python integration/experiment.py --scene=1 --agents=2` 端到端运行成功
- [ ] 新 coordinator 能管理现有 MARoS worker（A2A 协议兼容）
