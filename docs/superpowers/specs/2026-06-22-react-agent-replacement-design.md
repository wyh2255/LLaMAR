---
日期: 2026-06-22
文档类型：架构设计文档
文档概述: 用自写 ReAct 循环替代 mini_agent，实现 Worker 与 MARoS 完全解耦
---

# ReAct Agent 替换设计：去掉 mini_agent 依赖

## 1. 背景与动机

### 当前问题

Worker 的 ReAct 循环依赖 `mini_agent` 包，导致：

1. **依赖链过长**：Worker → transport.py → C2FixedMiniAgentAdapter → mini_agent.Agent → LLMClient → OpenAI，4 层中间层
2. **无用工具开销**：mini_agent 自带 ReadTool/WriteTool/BashTool，SAR 场景完全用不到，每步白白消耗 ~500 tokens
3. **MARoS 耦合**：依赖 a2a_lib（tool_decorator.py）、openharness_a2a（transport.py），无法独立运行
4. **调试困难**：ReAct 循环在 mini_agent 内部是黑箱，无法控制 tool 执行策略
5. **已知瓶颈**：实验数据显示 Worker 120 秒内 13 步，12 步是导航/探索，0 步灭火/救人

### 改造目标

- Worker 完全与 MARoS 解耦，零 a2a_lib / openharness_a2a / mini_agent 依赖
- 自写 ReAct 循环，代码量 ~150 行，完全可控
- 保留 A2A HTTP/WebSocket 通信协议（与 Coordinator 兼容）
- 所有改动仅在 `integration/` 目录内，MARoS 代码不动

## 2. 架构设计

### 改造前后对比

```
改造前（4 层）：
SARWorker → transport.py(MARoS) → C2FixedMiniAgentAdapter → mini_agent.Agent → LLMClient → OpenAI

改造后（1 层）：
SARWorker → a2a_server.py(自写) → WorkerReActAgent → SimpleLLMClient → OpenAI
```

### 新建文件（3 个）

| 文件 | 行数 | 职责 |
|------|------|------|
| `integration/sar_workers/tool_defs.py` | ~60 | 极简 @tool 装饰器 + ToolDef 数据类 |
| `integration/sar_workers/react_agent.py` | ~150 | Worker 端 ReAct 循环 |
| `integration/sar_workers/a2a_server.py` | ~250 | 精简版 A2A HTTP server + WebSocket client |

### 修改文件（3 个）

| 文件 | 改动 |
|------|------|
| `integration/sar_workers/tools.py` | `@tool` import 从 `_maros_compat` 改为 `tool_defs`（改 1 行） |
| `integration/sar_workers/sar_worker.py` | 核心改造：用 react_agent + a2a_server 替代 transport.py |
| `integration/_maros_compat.py` | 精简：去掉 `tool` 导出，只保留 `Skill` + a2a_lib shim（供 Coordinator 用） |

### 不动的文件

| 文件 | 原因 |
|------|------|
| MARoS 全部代码 | 用户要求不动 |
| `integration/sar_workers/skills.py` | 不依赖 mini_agent |
| `integration/coordinator/*` | Coordinator 不受影响 |
| `integration/sar_barrier.py` | Barrier 接口不变 |
| `integration/experiment.py` | 实验脚本接口不变 |

## 3. 详细设计

### 3.1 tool_defs.py — 极简 @tool 装饰器

替代 a2a_lib 的 `tool_decorator.py`（280 行）→ 自写 ~60 行。

```python
"""极简 @tool 装饰器 — 不依赖任何外部包。"""
from __future__ import annotations
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolDef:
    """工具定义 — 包含名称、描述、参数 schema、执行函数。"""
    name: str
    description: str
    parameters: dict                     # JSON Schema 格式
    func: Callable                       # 原始函数
    _node: Any = field(default=None, repr=False)  # bind 后的 SARWorker

    def bind(self, node: Any) -> "ToolDef":
        """绑定 SARWorker 实例，返回新 ToolDef（不修改原对象）。"""
        return ToolDef(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            func=self.func,
            _node=node,
        )

    def to_schema(self) -> dict:
        """转换为 OpenAI function calling schema 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

    async def execute(self, **kwargs) -> str:
        """执行工具函数，自动注入 bind 的 node 参数。"""
        return await self.func(self._node, **kwargs)


def tool(name: str, description: str):
    """极简 @tool 装饰器 — 从函数签名自动生成 JSON Schema。

    用法：
        @tool(name="navigate_to", description="Navigate to an object")
        async def navigate_to(node, target_id: str) -> str:
            ...
    """
    def decorator(func):
        schema = _signature_to_schema(func)
        return ToolDef(name=name, description=description,
                       parameters=schema, func=func)
    return decorator


def _signature_to_schema(func: Callable) -> dict:
    """从 inspect.signature 自动生成 JSON Schema（仅处理 str/int/float/bool 类型）。"""
    sig = inspect.signature(func)
    properties = {}
    required = []
    for param_name, param in sig.parameters.items():
        if param_name == "node":
            continue  # node 是注入参数，不暴露给 LLM
        prop = {"type": _python_type_to_json(param.annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        properties[param_name] = prop
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _python_type_to_json(annotation) -> str:
    """Python 类型注解 → JSON Schema type 字符串。"""
    if annotation is str:
        return "string"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    return "string"  # 默认 fallback
```

**对 tools.py 的影响**：只需改一行 import：
```python
# 之前: from integration._maros_compat import tool
# 之后: from integration.sar_workers.tool_defs import tool
```

函数体完全不变（navigate_to、move、explore 等 10 个工具）。

### 3.2 react_agent.py — Worker 端 ReAct 循环

替代 `mini_agent.Agent`（570 行）→ 自写 ~150 行。参考 `RouterAgent.run()` 方法。

```python
"""Worker 端 ReAct 循环 — 替代 mini_agent.Agent。"""
from __future__ import annotations
import logging
from typing import Any

from integration.coordinator.llm_shim import SimpleLLMClient, Message
from integration.sar_workers.tool_defs import ToolDef

logger = logging.getLogger(__name__)


class WorkerReActAgent:
    """Worker 端 ReAct 循环。

    每步调用 LLM，如果有 tool_calls 则执行工具并继续，
    如果无 tool_calls 则返回最终文本结果。

    与 mini_agent.Agent 的关键区别：
    - 去掉 ReadTool/WriteTool/BashTool 等内置工具
    - 去掉 token 估算和消息压缩（SAR 任务 5-10 步，不需要）
    - 去掉 cancel_event（SARBarrier 超时机制已覆盖）
    - system_prompt 由外部传入（SARWorker 的 _build_system_prompt()）
    - 直接复用 llm_shim.py 的 Message/ToolCall/LLMResponse 类型
    """

    def __init__(
        self,
        llm_client: SimpleLLMClient,
        tools: list[ToolDef],
        system_prompt: str,
        max_steps: int = 20,
    ):
        self._llm = llm_client
        self._tools = {t.name: t for t in tools}
        self._tool_schemas = [t.to_schema() for t in tools]
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._messages: list[Message] = []

    def update_system_prompt(self, prompt: str):
        """更新 system prompt（SARWorker 子任务变化时调用）。"""
        self._system_prompt = prompt

    async def run(self, user_message: str = "") -> str:
        """ReAct 主循环。返回最终文本结果。"""
        self._messages = [Message(role="system", content=self._system_prompt)]
        if user_message:
            self._messages.append(Message(role="user", content=user_message))

        for step in range(self._max_steps):
            logger.debug(f"ReAct step {step + 1}/{self._max_steps}")

            # 1. 调 LLM
            response = await self._llm.generate(
                messages=self._messages,
                tools=self._tool_schemas,
            )

            # 2. 追加 assistant message
            self._messages.append(Message(
                role="assistant",
                content=response.content or "",
                tool_calls=response.tool_calls,
            ))

            # 3. 无 tool_calls → 任务完成
            if not response.tool_calls:
                logger.debug(f"ReAct finished at step {step + 1}: {response.content[:100]}")
                return response.content or ""

            # 4. 执行 tool calls
            for tc in response.tool_calls:
                tool_name = tc.function.name
                tool_def = self._tools.get(tool_name)
                if tool_def is None:
                    result = f"Error: unknown tool '{tool_name}'"
                    logger.warning(f"Unknown tool call: {tool_name}")
                else:
                    try:
                        args = tc.function.arguments
                        if isinstance(args, str):
                            import json
                            args = json.loads(args)
                        result = await tool_def.execute(**args)
                    except Exception as e:
                        result = f"Error executing {tool_name}: {e}"
                        logger.error(f"Tool {tool_name} failed: {e}")

                self._messages.append(Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                    name=tool_name,
                ))

        logger.warning(f"ReAct exceeded max steps ({self._max_steps})")
        return f"Max steps ({self._max_steps}) exceeded"
```

### 3.3 a2a_server.py — 精简版 A2A 通信层

替代 MARoS 的 `transport.py`（830 行）→ 自写 ~250 行。

核心职责：
1. FastAPI HTTP server：提供 AgentCard + 接收 Coordinator 下发的任务
2. WebSocket client：连接 Coordinator，注册 Worker，接收子任务
3. 桥接：收到任务后调用 WorkerReActAgent.run()

```python
"""精简版 A2A Worker 服务器 — 替代 MARoS transport.py。

只保留 A2A 协议核心功能：
- FastAPI HTTP server: AgentCard + task endpoint
- WebSocket client: 连接 Coordinator 注册 + 接收子任务
- 桥接 ReAct agent 执行任务

不依赖 a2a_lib、openharness_a2a、mini_agent。
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
import websockets

logger = logging.getLogger(__name__)


class TaskRequest(BaseModel):
    """Coordinator 下发的任务请求。"""
    task_id: str
    task_text: str


class A2AWorkerServer:
    """精简版 A2A Worker 服务器。

    Args:
        agent_name: Worker 名称（如 "Alice"）
        port: HTTP server 端口
        coordinator_url: Coordinator WebSocket 地址（如 "ws://localhost:8080"）
        react_agent: WorkerReActAgent 实例
        skills: Skill 定义列表（用于 AgentCard 描述 Worker 能力）
    """

    def __init__(
        self,
        agent_name: str,
        port: int,
        coordinator_url: str,
        react_agent: Any,
        skills: list[Any] = None,
    ):
        self._agent_name = agent_name
        self._port = port
        self._coordinator_url = coordinator_url
        self._react_agent = react_agent
        self._skills = skills or []

        self._app = FastAPI(title=f"SAR Worker: {agent_name}")
        self._setup_routes()

        self._server: Optional[uvicorn.Server] = None
        self._http_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None

    def _setup_routes(self):
        """注册 FastAPI 路由。"""

        @self._app.get("/")
        async def agent_card():
            """返回 AgentCard — Coordinator 通过此接口发现 Worker 能力。"""
            skills_desc = [getattr(s, 'description', str(s)) for s in self._skills]
            return {
                "name": self._agent_name,
                "description": f"SAR search and rescue robot: {self._agent_name}",
                "skills": skills_desc,
                "url": f"http://localhost:{self._port}",
            }

        @self._app.post("/tasks")
        async def handle_task(task: TaskRequest):
            """接收 Coordinator 下发的任务，执行 ReAct 循环，返回结果。"""
            logger.info(f"[{self._agent_name}] Received task: {task.task_text[:80]}...")
            try:
                result = await self._react_agent.run(user_message=task.task_text)
                logger.info(f"[{self._agent_name}] Task completed: {result[:80]}...")
                return {"task_id": task.task_id, "status": "completed", "result": result}
            except Exception as e:
                logger.error(f"[{self._agent_name}] Task failed: {e}")
                return {"task_id": task.task_id, "status": "error", "result": str(e)}

    async def _ws_loop(self):
        """WebSocket 客户端 — 连接 Coordinator 并注册 Worker。"""
        while True:
            try:
                async with websockets.connect(self._coordinator_url) as ws:
                    # 注册 Worker
                    await ws.send(json.dumps({
                        "type": "register",
                        "agent_name": self._agent_name,
                        "port": self._port,
                    }))
                    logger.info(f"[{self._agent_name}] Registered with Coordinator")

                    # 保持连接，接收消息
                    async for message in ws:
                        data = json.loads(message)
                        logger.debug(f"[{self._agent_name}] WS recv: {data.get('type', 'unknown')}")
                        # 可扩展：处理 Coordinator 推送的实时指令
            except websockets.ConnectionClosed:
                logger.warning(f"[{self._agent_name}] WS disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"[{self._agent_name}] WS error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def start(self):
        """启动 HTTP server + WebSocket client（非阻塞）。"""
        config = uvicorn.Config(self._app, host="0.0.0.0", port=self._port,
                                log_level="warning")
        self._server = uvicorn.Server(config)
        self._http_task = asyncio.create_task(self._server.serve())
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info(f"[{self._agent_name}] A2A server started on port {self._port}")

    async def stop(self):
        """停止 HTTP server + WebSocket client。"""
        if self._server:
            self._server.should_exit = True
        if self._ws_task:
            self._ws_task.cancel()
        logger.info(f"[{self._agent_name}] A2A server stopped")
```

### 3.4 sar_worker.py 改造

```python
# 关键改动：

class SARWorker:
    def __init__(self, agent_name, agent_idx, barrier, port,
                 coordinator_url="ws://localhost:8080",
                 model="deepseek-v4-flash"):
        # ... 保持不变 ...
        
        # 新增：创建 LLM 客户端
        from integration.coordinator.llm_shim import SimpleLLMClient
        self._llm_client = SimpleLLMClient(model=self._model)
        
        # 新增：ReAct agent（start 时初始化）
        self._react_agent = None
        self._a2a_server = None

    def start(self):
        """启动 Worker — 创建 ReAct agent + A2A server。"""
        from integration.sar_workers.react_agent import WorkerReActAgent
        from integration.sar_workers.a2a_server import A2AWorkerServer

        # 1. 创建 ReAct agent
        self._react_agent = WorkerReActAgent(
            llm_client=self._llm_client,
            tools=self._tools,
            system_prompt=self._build_system_prompt(),
        )

        # 2. 创建并启动 A2A server
        self._a2a_server = A2AWorkerServer(
            agent_name=self.agent_name,
            port=self._port,
            coordinator_url=self._coordinator_url,
            react_agent=self._react_agent,
            skills=SAR_SKILLS,
        )
        asyncio.get_event_loop().run_until_complete(self._a2a_server.start())

    def update_subtask(self, subtask: str):
        """更新子任务 — 同时更新 ReAct agent 的 system prompt。"""
        self._current_subtask = subtask
        if self._react_agent:
            self._react_agent.update_system_prompt(self._build_system_prompt())
```

### 3.5 _maros_compat.py 精简

去掉 `tool` 导出（不再需要 tool_decorator.py），只保留：
- `Skill` 导出（skills.py 需要）
- a2a_lib shim（Coordinator 的 RouterAgent 可能还需要）
- `get_start_a2a_transport()` 保留但 Worker 不再调用

```python
# 删除：
# _tool_decorator = _load_by_path("_maros_td", "tool_decorator.py")
# tool = _tool_decorator.tool

# 保留：
_skill_mod = _load_by_path("_maros_skill", "skill.py")
Skill = _skill_mod.Skill
# get_start_a2a_transport() 保留（Coordinator 可能用）
```

## 4. 依赖关系（改造后）

```
integration/sar_workers/
  ├── tool_defs.py      → (无外部依赖，仅 stdlib: inspect, dataclasses)
  ├── react_agent.py    → llm_shim.py (SimpleLLMClient + Message)
  ├── a2a_server.py     → fastapi, uvicorn, websockets (标准 Python 包)
  ├── tools.py          → tool_defs.py (改 import)
  ├── skills.py         → _maros_compat.py (Skill，保留)
  └── sar_worker.py     → react_agent.py + a2a_server.py + tool_defs.py + llm_shim.py
```

**不再依赖**：mini_agent、a2a_lib.tool_decorator、openharness_a2a.transport

## 5. 兼容性保证

| 组件 | 影响 | 说明 |
|------|------|------|
| SARBarrier | 无影响 | tool 函数仍然调用 `node._barrier.submit_action()` |
| SARCoordinator | 无影响 | RouterAgent 的 ReAct 循环不变 |
| experiment.py | 无影响 | SARWorker 接口不变（start/update_subtask） |
| IntegrationLogger | 无影响 | `_log_tool_call` 在 tools.py 中不变 |
| Skills | 无影响 | skills.py 不依赖 mini_agent |
| A2A 协议 | 兼容 | AgentCard + task endpoint 接口保持一致 |

## 6. 测试计划

### 单元测试

1. `test_tool_defs.py` — 测试 @tool 装饰器、ToolDef.bind()、ToolDef.execute()、JSON Schema 生成
2. `test_react_agent.py` — 测试 ReAct 循环（mock LLM client）、tool 执行、错误处理、max_steps 限制
3. `test_a2a_server.py` — 测试 HTTP 路由（AgentCard、/tasks）、WebSocket 注册

### 集成测试

4. 更新 `test_integration.py` — 验证 Barrier→Worker→Coordinator 链路仍然正常
5. 更新 `test_sar_barrier.py` — 验证 Barrier 单元测试不受影响

### 端到端验证

6. 运行 `experiment.py --scene=1 --agents=2 --seed=42`，对比改造前后的：
   - Coverage / Transport Rate
   - 每步 LLM 调用时间（预期减少，因为去掉了无用工具的 token）
   - 总步骤数

## 7. 预期收益

| 指标 | 改造前 | 改造后预期 |
|------|--------|-----------|
| 每步 LLM 调用时间 | 2-3s | 1-2s（去掉无用工具减少 token） |
| 无用工具 token 开销 | ~500 tokens/步 | 0 |
| 中间层复杂度 | 3 层（transport + adapter + mini_agent） | 1 层（a2a_server） |
| 代码可维护性 | 依赖外部包 ~1500 行 | 自有 ~460 行 |
| 调试难度 | 高（黑箱） | 低（全部可见） |
| MARoS 依赖 | a2a_lib + openharness_a2a + mini_agent | 零 |
