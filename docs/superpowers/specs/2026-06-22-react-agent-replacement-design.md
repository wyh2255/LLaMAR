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
    """从 inspect.signature 自动生成 JSON Schema。

    处理 from __future__ import annotations 导致的字符串化类型注解。
    同时从 docstring 的 Args 段提取参数 description。
    """
    import typing
    sig = inspect.signature(func)

    # get_type_hints 正确处理 PEP 563 字符串化注解
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}

    # 从 docstring 提取参数描述
    param_docs = _parse_docstring_args(func)

    properties = {}
    required = []
    for param_name, param in sig.parameters.items():
        if param_name == "node":
            continue  # node 是注入参数，不暴露给 LLM
        annotation = hints.get(param_name, str)
        prop = {"type": _python_type_to_json(annotation)}
        if param_name in param_docs:
            prop["description"] = param_docs[param_name]
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        properties[param_name] = prop
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _python_type_to_json(annotation) -> str:
    """Python 类型注解 → JSON Schema type 字符串。"""
    if annotation is str or annotation == "str":
        return "string"
    if annotation is int or annotation == "int":
        return "integer"
    if annotation is float or annotation == "float":
        return "number"
    if annotation is bool or annotation == "bool":
        return "boolean"
    return "string"


def _parse_docstring_args(func: Callable) -> dict:
    """从 docstring 的 Args 段提取参数描述。

    支持 Google style docstring：
        Args:
            param_name: description text
    """
    doc = inspect.getdoc(func) or ""
    result = {}
    in_args = False
    current_param = None
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            if stripped and not stripped[0].isspace() and ":" in stripped:
                # 新参数行
                name, desc = stripped.split(":", 1)
                current_param = name.strip()
                result[current_param] = desc.strip()
            elif stripped and current_param:
                # 续行描述
                result[current_param] += " " + stripped
            elif not stripped:
                in_args = False
    return result
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

替代 MARoS 的 `transport.py`（830 行）→ 自写 ~300 行。

核心职责：
1. FastAPI HTTP server：JSON-RPC 2.0 端点（接收 Coordinator 派发的任务）+ AgentCard
2. WebSocket client：连接 Coordinator `/ws/worker/{id}`，注册 Worker + 心跳
3. 桥接：收到任务后调用 WorkerReActAgent.run()，通过 WebSocket 回报状态

**协议兼容性要求**（基于 Coordinator 实际代码）：
- HTTP 端点：`POST /api/v1/jsonrpc/`，JSON-RPC 2.0 格式，method="SendMessage"
- AgentCard：`GET /.well-known/agent-card.json`
- WebSocket URL：`ws://coordinator:port/ws/worker/{worker_id}`
- 注册消息：`{"type": "register", "payload": {"worker_id": "...", "a2a_endpoint": "http://host:port/"}}`
- 心跳：每 30 秒发送 `{"type": "heartbeat", "payload": {"worker_id": "..."}}`
- 任务回报：通过 WebSocket 发送 `{"type": "task_complete", "payload": {"task_id": "...", "result": "..."}}`

```python
"""精简版 A2A Worker 服务器 — 替代 MARoS transport.py。

协议兼容 Coordinator（push_task.py + server.py + worker_registry.py）：
- JSON-RPC 2.0 task endpoint
- AgentCard at /.well-known/agent-card.json
- WebSocket registration + heartbeat

不依赖 a2a_lib、openharness_a2a、mini_agent。
"""
from __future__ import annotations
import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


class A2AWorkerServer:
    """精简版 A2A Worker 服务器。

    Args:
        agent_name: Worker 名称（如 "Alice"）
        port: HTTP server 端口
        coordinator_url: Coordinator HTTP 地址（如 "http://localhost:8080"）
        react_agent: WorkerReActAgent 实例
        skills: Skill 定义列表
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
        self._coordinator_url = coordinator_url.rstrip("/")
        self._react_agent = react_agent
        self._skills = skills or []
        self._a2a_endpoint = f"http://localhost:{port}/"

        # 状态
        self._running = False
        self._http_server = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None

    # ── HTTP 路由 ──────────────────────────────────────────────────

    def _create_app(self):
        """创建 FastAPI app，注册路由。"""
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        app = FastAPI(title=f"SAR Worker: {self._agent_name}")

        @app.get("/.well-known/agent-card.json")
        async def agent_card():
            """AgentCard — Coordinator 通过此接口发现 Worker 能力。"""
            skills_list = []
            for s in self._skills:
                skills_list.append({
                    "name": getattr(s, "name", str(s)),
                    "description": getattr(s, "description", ""),
                    "tools": getattr(s, "tool_names", []),
                })
            return {
                "version": "1.0",
                "name": self._agent_name,
                "description": f"SAR search and rescue robot: {self._agent_name}",
                "url": self._a2a_endpoint,
                "skills": skills_list,
                "interfaces": ["a2a-jsonrpc"],
            }

        @app.post("/api/v1/jsonrpc/")
        async def jsonrpc_handler(request: Request):
            """JSON-RPC 2.0 端点 — 接收 Coordinator 下发的 SendMessage。"""
            body = await request.json()
            method = body.get("method", "")
            req_id = body.get("id", str(uuid.uuid4()))

            if method == "SendMessage":
                params = body.get("params", {})
                message = params.get("message", {})
                parts = message.get("parts", [])
                task_text = parts[0].get("text", "") if parts else ""
                task_id = message.get("messageId", req_id)

                logger.info(f"[{self._agent_name}] JSON-RPC SendMessage: {task_text[:80]}...")

                # 异步执行 ReAct（不阻塞 HTTP 响应）
                asyncio.create_task(self._execute_task(task_id, task_text))

                return JSONResponse({
                    "jsonrpc": "2.0",
                    "result": {"status": "accepted", "taskId": task_id},
                    "id": req_id,
                })
            else:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    "id": req_id,
                }, status_code=400)

        return app

    async def _execute_task(self, task_id: str, task_text: str):
        """执行任务并通过 WebSocket 回报结果。"""
        try:
            result = await self._react_agent.run(user_message=task_text)
            logger.info(f"[{self._agent_name}] Task {task_id} completed: {result[:80]}...")
            await self._ws_send({
                "type": "task_complete",
                "payload": {"task_id": task_id, "status": "completed", "result": result},
            })
        except Exception as e:
            logger.error(f"[{self._agent_name}] Task {task_id} failed: {e}")
            await self._ws_send({
                "type": "task_complete",
                "payload": {"task_id": task_id, "status": "failed", "result": str(e)},
            })

    # ── WebSocket 客户端 ───────────────────────────────────────────

    async def _ws_send(self, data: dict):
        """通过 WebSocket 发送消息。"""
        if self._ws:
            try:
                await self._ws.send(json.dumps(data))
            except Exception as e:
                logger.warning(f"[{self._agent_name}] WS send failed: {e}")

    async def _ws_loop(self):
        """WebSocket 客户端 — 连接 Coordinator，注册 + 心跳。"""
        import websockets

        ws_url = f"ws://{self._coordinator_url.replace('http://', '').replace('https://', '')}/ws/worker/{self._agent_name}"

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    # 注册
                    await ws.send(json.dumps({
                        "type": "register",
                        "payload": {
                            "worker_id": self._agent_name,
                            "a2a_endpoint": self._a2a_endpoint,
                        },
                    }))
                    logger.info(f"[{self._agent_name}] Registered with Coordinator at {ws_url}")

                    # 心跳循环 + 消息接收
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        async for message in ws:
                            data = json.loads(message)
                            logger.debug(f"[{self._agent_name}] WS recv: {data.get('type', 'unknown')}")
                    finally:
                        heartbeat_task.cancel()
                        self._ws = None

            except websockets.ConnectionClosed:
                logger.warning(f"[{self._agent_name}] WS disconnected, reconnecting in 2s...")
                self._ws = None
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"[{self._agent_name}] WS error: {e}, reconnecting in 5s...")
                self._ws = None
                await asyncio.sleep(5)

    async def _heartbeat_loop(self, ws):
        """每 30 秒发送心跳。"""
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "payload": {"worker_id": self._agent_name},
                }))
            except Exception:
                break

    # ── 启动/停止 ──────────────────────────────────────────────────

    def start_in_thread(self):
        """在独立线程中启动 HTTP server + WebSocket（同步接口）。

        experiment.py 的主循环已在 asyncio 中运行，不能用 run_until_complete。
        正确做法：新线程 + 新事件循环（与 transport.py 模式一致）。
        """
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"[{self._agent_name}] A2A server starting on port {self._port}")

    def _run_loop(self):
        """线程入口 — 创建新事件循环，启动 HTTP + WebSocket。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"[{self._agent_name}] Server loop error: {e}")
        finally:
            loop.close()

    async def _serve(self):
        """启动 uvicorn HTTP server + WebSocket 客户端。"""
        import uvicorn

        app = self._create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="warning")
        server = uvicorn.Server(config)

        # 并行运行 HTTP server 和 WebSocket
        http_task = asyncio.create_task(server.serve())
        ws_task = asyncio.create_task(self._ws_loop())

        try:
            await asyncio.gather(http_task, ws_task)
        except asyncio.CancelledError:
            pass

    def stop(self):
        """停止服务器。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"[{self._agent_name}] A2A server stopped")
```

### 3.4 sar_worker.py 改造

```python
# 关键改动：

class SARWorker:
    def __init__(self, agent_name, agent_idx, barrier, port,
                 coordinator_url="http://localhost:8080",
                 model="deepseek-v4-flash"):
        # ... 保持不变 ...
        
        # 新增：创建 LLM 客户端
        from integration.coordinator.llm_shim import SimpleLLMClient
        self._llm_client = SimpleLLMClient(model=self._model)
        
        # 新增：ReAct agent（start 时初始化）
        self._react_agent = None
        self._a2a_server = None

    def start(self):
        """启动 Worker — 创建 ReAct agent + A2A server。

        注意：experiment.py 的主循环已在 asyncio 中运行，不能用
        run_until_complete。A2A server 在独立线程中启动新事件循环。
        """
        from integration.sar_workers.react_agent import WorkerReActAgent
        from integration.sar_workers.a2a_server import A2AWorkerServer

        # 1. 创建 ReAct agent
        self._react_agent = WorkerReActAgent(
            llm_client=self._llm_client,
            tools=self._tools,
            system_prompt=self._build_system_prompt(),
        )

        # 2. 创建并启动 A2A server（独立线程，新事件循环）
        self._a2a_server = A2AWorkerServer(
            agent_name=self.agent_name,
            port=self._port,
            coordinator_url=self._coordinator_url,
            react_agent=self._react_agent,
            skills=SAR_SKILLS,
        )
        self._a2a_server.start_in_thread()  # 同步调用，非阻塞

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
