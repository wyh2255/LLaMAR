---
日期: 2026-06-21
文档类型: 技术方案
文档概述: SARCoordinator 代码重构方案，分析当前调用结构问题，提出方法提取式重构方案及分层测试策略
---

# SARCoordinator 代码重构方案

## 1. 现状分析

### 1.1 调用结构总览

```
SARCoordinator
├── __init__()          # ~40行：存参数、构建agent描述、构建system prompt
│
├── start()             # ~150行：平铺式初始化的"大泥球"
│     ├── lazy imports (6个)
│     ├── CoordinatorServer 创建
│     ├── pending_tasks = {}          ← 局部变量
│     ├── SARRouterAgent 创建
│     ├── 7个工具逐行注册              ← 平铺无分组
│     ├── def lifespan(app):          ← 嵌套闭包函数
│     │     ├── A2A server 创建+启动
│     │     └── shutdown 清理
│     ├── FastAPI 应用创建
│     ├── @app.websocket handler      ← 嵌套内联定义
│     └── uvicorn 启动
│
├── stop()              # ~15行：只停uvicorn，A2A server靠lifespan shutdown
│
└── SARRouterAgent (嵌套类)
      └── _build_system_prompt()      # 单独提取的方法，唯一一个清晰的
```

### 1.2 具体问题清单

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 1 | `start()` 单方法 150 行，囊括导入/创建/注册/闭包/路由定义/服务启动 | L187-341 | 阅读需上下跳转，无法独立测试任何中间产物 |
| 2 | `pending_tasks` 是 `start()` 内的局部变量，传给 3 个工具共享 | L232 | 无法在方法外检查 pending 状态，重构时容易丢失引用一致性 |
| 3 | `CoordinatorAgentExecutor` 在嵌套的 `lifespan` 闭包内创建 | L284-291 | 创建逻辑藏在运行时路径中，无法独立验证参数是否正确组装 |
| 4 | WebSocket handler 内联定义在 `start()` 内 | L309-326 | 无法单独测试，复用需复制代码 |
| 5 | `cs._server_task`（CoordinatorServer内部）与 `self._server_task`（SARCoordinator）混用 | L293, L332 | 两个同名不同属的变量，阅读时容易混淆 |
| 6 | 工具注册在 `start()` 中平铺，未分组 | L239-260 | 7 个工具逐行 register_tool，增删工具需在 150 行内定位 |
| 7 | `__init__` 做了部分初始化（`QuerySARStateTool`），其余在 `start()` | L179-181 vs L239-260 | 工具创建分散在两处 |

## 2. 重构目标

### 2.1 核心原则

- **纯方法提取（Extract Method）**：不改变任何外部行为，不修改 public API 签名
- **每个方法一个职责**：`start()` 只负责"启动"，不负责"构建"
- **可独立测试**：提取出的每个方法返回可断言的对象
- **保持向后兼容**：`experiment.py` 无感知

### 2.2 目标调用结构

```
SARCoordinator
├── __init__()
│     └── self._pending_tasks = {}     ← 从局部变量提升为实例属性
│
├── _build_router() → SARRouterAgent   ← [提取] Router + 7个工具
│     ├── SARRouterAgent(...)
│     ├── ── 任务类工具 ──
│     │     ├── PushTaskTool(..., self._pending_tasks)
│     │     ├── WaitForResultTool(self._pending_tasks)
│     │     └── CancelTaskTool(..., self._pending_tasks)
│     ├── ── 查询类工具 ──
│     │     ├── QuerySARStateTool(self._barrier)
│     │     └── QueryMemoryTool(...)
│     └── ── 辅助类工具 ──
│           ├── SessionNoteTool()
│           └── BashTool()
│
├── _build_app(router) → FastAPI       ← [提取] FastAPI 组装
│     ├── lifespan 闭包（只捕获已造好的对象）
│     ├── FastAPI(lifespan)
│     ├── health / workers 路由注册
│     └── /ws/worker/{id} 路由注册
│
├── _handle_worker_ws(ws, id)          ← [提取] WebSocket 处理
│
├── start()                            ← [精简] 3 步调用
│     ├── router = self._build_router()
│     ├── app = self._build_app(router)
│     └── uvicorn.Server(app).serve()
│
└── stop()                             ← [增强] 保证 A2A server 也清理
      ├── uvicorn.should_exit = True
      ├── self._server_task.cancel()
      └── A2A server 显式停止
```

## 3. 详细设计

### 3.1 `_build_router()` 方法

需要在 `start()` 中先创建 `self._coord_server` 后可调用。导入 `QuerySARStateTool` 放在方法体开头。

```python
def _build_router(self) -> SARRouterAgent:
    """创建 SARRouterAgent 并注册所有 SAR 工具。

    Precondition: self._coord_server 必须已创建（由 start() 保证）。
    """
    from integration.coordinator.sar_router_tools import QuerySARStateTool

    router = SARRouterAgent(
        sar_system_prompt=self._system_prompt,
        llm_client=self._coord_server._llm_client,
        registry=self._coord_server._agent_registry,
        memory_client=self._coord_server._memory_client,
    )

    # ── 任务类工具 ──
    router.register_tool(PushTaskTool(
        registry=self._coord_server._agent_registry,
        pending_tasks=self._pending_tasks,    # ← 实例属性
    ))
    router.register_tool(WaitForResultTool(self._pending_tasks))
    router.register_tool(CancelTaskTool(
        task_queue=self._coord_server._task_queue,
        pending_tasks=self._pending_tasks,
    ))

    # ── 查询类工具 ──
    router.register_tool(QueryMemoryTool(
        memory_client=self._coord_server._memory_client,
    ))
    router.register_tool(QuerySARStateTool(self._barrier))

    # ── 辅助类工具 ──
    router.register_tool(SessionNoteTool())
    router.register_tool(BashTool())

    return router
```

### 3.2 `_build_app(router)` 方法

需要在 `start()` 中先创建 `self._coord_server` 和 router 后可调用。
`lifespan` 中的 A2A server task 改名为 `_a2a_server_task` 避免与 `self._server_task` 混淆。

```python
def _build_app(self, router: SARRouterAgent) -> FastAPI:
    """构建 FastAPI 应用，注册路由和生命周期管理。

    Precondition: self._coord_server 必须已创建（由 start() 保证）。
    """
    from openharness_a2a.coordinator.agent_executor import CoordinatorAgentExecutor
    from openharness_a2a.coordinator.a2a_server import create_coordinator_a2a_server
    from openharness_a2a.coordinator.routes import health, workers
    from contextlib import asynccontextmanager
    from fastapi import FastAPI

    cs = self._coord_server

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """A2A server 生命周期：启动 → yield → 关闭"""
        await cs._start_cleanup_task()
        executor = CoordinatorAgentExecutor(
            registry=cs._agent_registry,
            task_queue=cs._task_queue,
            memory_client=cs._memory_client,
            task_logger=cs._task_logger,
            llm_client=cs._llm_client,
            router=router,
        )
        a2a_srv = create_coordinator_a2a_server(
            host="0.0.0.0", port=cs._a2a_port,
            agent_registry=cs._agent_registry,
            task_queue=cs._task_queue,
            executor=executor,
        )
        cs._a2a_server_task = asyncio.create_task(a2a_srv.serve())  # 改名避免混淆
        yield
        cs._a2a_server_task.cancel()
        try:
            await cs._a2a_server_task
        except asyncio.CancelledError:
            pass
        await cs._stop_cleanup_task()

    app = FastAPI(title="SAR Coordinator", lifespan=lifespan)
    health.register_routes(app, cs)
    workers.register_routes(app, cs)
    app.add_websocket_route("/ws/worker/{worker_id}", self._handle_worker_ws)
    return app
```

### 3.3 `_handle_worker_ws()` 方法

从 `start()` 的嵌套定义提取为独立方法。导入放在方法体开头以保持延迟加载。

```python
async def _handle_worker_ws(self, websocket: WebSocket, worker_id: str):
    """Worker WebSocket 连接处理。"""
    from fastapi import WebSocket, WebSocketDisconnect
    await websocket.accept()
    async with self._coord_server._worker_ws_lock:
        self._coord_server._worker_ws[worker_id] = websocket
    try:
        while True:
            data = await websocket.receive_text()
            await self._coord_server._handle_worker_message(
                worker_id, json.loads(data))
    except WebSocketDisconnect:
        async with self._coord_server._worker_ws_lock:
            self._coord_server._worker_ws.pop(worker_id, None)
```

### 3.4 重构后的 `start()` 和 `stop()`

```python
async def start(self):
    """启动协调器：建 Router → 建 App → 启动 uvicorn"""
    import uvicorn
    from openharness_a2a.coordinator.server import CoordinatorServer

    # 1. 创建最小化的 CoordinatorServer（借用 registry/queue/memory/llm）
    self._coord_server = CoordinatorServer(
        host="0.0.0.0", port=self._port, a2a_port=self._port + 1,
    )

    # 2. 构建 RouterAgent + 工具
    router = self._build_router()

    # 3. 构建 FastAPI 应用
    app = self._build_app(router)

    # 4. 启动 uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="info")
    self._uvicorn_server = uvicorn.Server(config)
    self._server_task = asyncio.create_task(self._uvicorn_server.serve())

    logger.info(
        "SAR Coordinator starting on port %d with agents: %s",
        self._port, ", ".join(self._agent_names),
    )
    await asyncio.sleep(0.5)

async def stop(self):
    """优雅关闭协调器服务。"""
    if hasattr(self, "_uvicorn_server") and self._uvicorn_server is not None:
        self._uvicorn_server.should_exit = True
    if self._server_task is not None:
        self._server_task.cancel()
        try:
            await self._server_task
        except asyncio.CancelledError:
            pass
    logger.info("SAR Coordinator stopped")
```

### 3.5 `__init__()` 变更

- 新增 `self._pending_tasks: dict = {}` 替代原先 `start()` 内的局部变量
- **移除** `self._sar_tools` 列表（原先在 `__init__` 中创建 `QuerySARStateTool` 存于此列表，`start()` 再遍历注册 → 改为在 `_build_router()` 中统一创建并注册）
- 工具创建不再分散在 `__init__` 和 `start()` 两处

```python
def __init__(self, ...):
    # ... 原有参数存储不变 ...
    self._pending_tasks: dict = {}
    
    # 注意：_build_router 不再在 __init__ 中调用，
    # 因为此时 CoordinatorServer 尚未创建（在 start() 中创建）
```

## 4. 测试方案

### 4.1 第 1 层：静态验证（新增，不启动网络）

| 测试 | 验证内容 | 断言要点 |
|------|---------|---------|
| `test_router_registers_all_tools` | `_build_router()` 注册了 7 个工具 | tool_names 包含所有预期名称，len==7 |
| `test_router_tools_share_pending_tasks` | 任务类工具共享同一个 `pending_tasks` | push/wait/cancel 三个工具引用同一个 dict |
| `test_app_has_correct_routes` | `_build_app()` 创建了正确的路由 | /health, /ws/worker/{id} 等路由存在 |
| `test_router_system_prompt_contains_agents` | 系统提示词正确注入 agent 名称 | 输出包含 Alice/Bob 和 SAR 关键词 |
| `test_handle_worker_ws_signature` | `_handle_worker_ws` 是正确签名的协程方法 | 方法存在，接受 (self, websocket, worker_id)，地址与 app 注册的路由一致 |
| `test_router_has_query_sar_state_tool` | `_build_router()` 包含 SAR 专属工具 | `query_sar_state` 工具存在，其 `_barrier` 引用与 coordinator 的 barrier 一致 |

### 4.2 第 2 层：集成测试（新增，启动协程但不依赖外部 LLM）

| 测试 | 验证内容 | 断言要点 |
|------|---------|---------|
| `test_coordinator_start_stop_cycle` | `start()` + `stop()` 生命周期正常 | start 不抛异常，端口绑定成功，stop 不抛异常 |

### 4.3 第 3 层：回归测试（已有）

```bash
pytest integration/test_sar_barrier.py -v     # 5 tests，全部 PASS
pytest integration/test_integration.py -v     # 2 tests，全部 PASS
```

### 4.4 第 4 层：端到端验证（人工）

```bash
python integration/experiment.py --scene=1 --agents=2
```

### 4.5 测试注意事项

- 第 1 层测试不依赖 MARoS 的 `rclpy`，可以在任何环境中运行
- 第 2 层测试需要 MARoS path 配置正确，但不需要 ROS master
- 所有测试使用非常规端口（如 9099）避免与开发环境冲突

## 5. 实施步骤

```
Step 1: 在 __init__ 中增加 self._pending_tasks = {}，移除 self._sar_tools
Step 2: 提取 _build_router() 方法（含工具注册 + 补充本方法内所需的导入）
Step 3: 提取 _build_app(router) 方法（含 lifespan + A2A task 更名为 _a2a_server_task）
Step 4: 提取 _handle_worker_ws() 方法（含补充 WebSocket 导入）
Step 5: 精简 start() — 补充 CoordinatorServer 导入、恢复 logger.info
Step 6: 统一处理各方法的前置条件 docstring
Step 7: 对比重构前后的代码，确认无遗漏
Step 8: 写测试（第 1 层 6 个 + 第 2 层 1 个）
Step 9: 运行回归测试（test_sar_barrier.py + test_integration.py）
```

每步之间可以单独提交，且每步完成后都可用 `test_integration.py` 验证。

## 6. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| `_coord_server._*` 内部属性名称变化 | 低（已锁定 MARoS commit 722c4de） | 启动时 AttributeError | 第 2 层集成测试捕获 |
| pending_tasks 引用传递错误 | 低 | 工具间任务状态不同步 | 第 1.2 层测试验证 |
| lifespan 闭包变量捕获错误 | 低 | 启动/关闭异常 | 第 2 层测试验证 |
| import 路径遗漏 | 低 | ModuleNotFoundError | 第 2 层测试验证 |
| `_server_task` 管理逻辑遗漏 | 中 | stop() 关不干净 | 端口被占用手工发现 |
