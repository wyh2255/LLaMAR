---
日期: 2026-06-22
文档类型：实现计划
文档概述: ReAct Agent 替换实现计划 — 7 个 Task，TDD 方式，逐步替换 mini_agent
---

# ReAct Agent 替换实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用自写 ReAct 循环替代 mini_agent，实现 Worker 与 MARoS 完全解耦。

**Architecture:** 新建 3 个文件（tool_defs.py / react_agent.py / a2a_server.py），修改 3 个文件（tools.py / sar_worker.py / _maros_compat.py）。Worker 通过自写 A2A server 与 Coordinator 通信，内部用 WorkerReActAgent 驱动 LLM tool calling 循环。

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, websockets, openai (via llm_shim.py)

## Global Constraints

- 所有改动仅在 `integration/` 目录内，MARoS 代码不动
- A2A 协议与 Coordinator 完全兼容（JSON-RPC 2.0, WebSocket 注册/心跳）
- SARBarrier 接口不变（tool 函数仍调用 `node._barrier.submit_action()`）
- 复用 `integration/coordinator/llm_shim.py` 的 SimpleLLMClient + 数据类型
- 每个 Task 完成后可独立测试，不依赖后续 Task

## File Structure

```
integration/sar_workers/
  ├── tool_defs.py      ← NEW: 极简 @tool 装饰器 + ToolDef (Task 1)
  ├── react_agent.py    ← NEW: WorkerReActAgent ReAct 循环 (Task 2)
  ├── a2a_server.py     ← NEW: 精简版 A2A HTTP+WS 通信层 (Task 3)
  ├── tools.py          ← MODIFY: 换 import (Task 4)
  ├── sar_worker.py     ← MODIFY: 核心改造 (Task 5)
  ├── skills.py         ← 不动
  └── prompt.md         ← 不动
integration/
  ├── _maros_compat.py  ← MODIFY: 精简 (Task 6)
  ├── coordinator/      ← 不动
  └── sar_barrier.py    ← 不动
```

---

### Task 1: tool_defs.py — 极简 @tool 装饰器

**Files:**
- Create: `integration/sar_workers/tool_defs.py`
- Create: `integration/sar_workers/test_tool_defs.py`

**Interfaces:**
- Produces: `tool(name, description)` 装饰器，返回 `ToolDef` 实例
- Produces: `ToolDef.bind(node) -> ToolDef`，`ToolDef.execute(**kwargs) -> str`，`ToolDef.to_schema() -> dict`

- [ ] **Step 1: 创建 tool_defs.py**

```python
"""极简 @tool 装饰器 — 不依赖任何外部包。"""
from __future__ import annotations
import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolDef:
    """工具定义 — 包含名称、描述、参数 schema、执行函数。"""
    name: str
    description: str
    parameters: dict
    func: Callable
    _node: Any = field(default=None, repr=False)

    def bind(self, node: Any) -> ToolDef:
        return ToolDef(
            name=self.name, description=self.description,
            parameters=self.parameters, func=self.func, _node=node,
        )

    def to_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

    async def execute(self, **kwargs) -> str:
        return await self.func(self._node, **kwargs)


def tool(name: str, description: str):
    def decorator(func):
        schema = _signature_to_schema(func)
        return ToolDef(name=name, description=description,
                       parameters=schema, func=func)
    return decorator


def _signature_to_schema(func: Callable) -> dict:
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}
    param_docs = _parse_docstring_args(func)
    properties, required = {}, []
    for param_name, param in sig.parameters.items():
        if param_name == "node":
            continue
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
    doc = inspect.getdoc(func) or ""
    result, in_args, current_param = {}, False, None
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            if stripped and not stripped[0].isspace() and ":" in stripped:
                name, desc = stripped.split(":", 1)
                current_param = name.strip()
                result[current_param] = desc.strip()
            elif stripped and current_param:
                result[current_param] += " " + stripped
            elif not stripped:
                in_args = False
    return result
```

- [ ] **Step 2: 创建 test_tool_defs.py**

```python
"""tool_defs.py 单元测试。"""
import pytest
import asyncio
from integration.sar_workers.tool_defs import tool, ToolDef


def test_tool_creates_tool_def():
    @tool(name="test_tool", description="A test tool")
    async def my_tool(node, target_id: str) -> str:
        """Navigate to target.

        Args:
            target_id: The target ID
        """
        return f"navigated to {target_id}"

    assert isinstance(my_tool, ToolDef)
    assert my_tool.name == "test_tool"
    assert my_tool.description == "A test tool"


def test_tool_schema_generation():
    @tool(name="nav", description="Navigate")
    async def nav(node, target_id: str) -> str:
        """Nav tool.

        Args:
            target_id: Target object ID
        """
        return "ok"

    schema = nav.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nav"
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "target_id" in params["properties"]
    assert params["properties"]["target_id"]["type"] == "string"
    assert params["properties"]["target_id"]["description"] == "Target object ID"
    assert "target_id" in params["required"]


def test_tool_no_params():
    @tool(name="explore", description="Explore")
    async def explore(node) -> str:
        """Explore area."""
        return "explored"

    schema = explore.to_schema()
    params = schema["function"]["parameters"]
    assert params["properties"] == {}


def test_tool_bind():
    @tool(name="t", description="t")
    async def t(node) -> str:
        return "ok"

    mock_node = object()
    bound = t.bind(mock_node)
    assert bound._node is mock_node
    assert bound.name == "t"
    # 原始 ToolDef 不变
    assert t._node is None


def test_tool_execute():
    @tool(name="t", description="t")
    async def t(node, x: str) -> str:
        return f"got {x}"

    class MockNode:
        pass

    bound = t.bind(MockNode())
    result = asyncio.get_event_loop().run_until_complete(bound.execute(x="hello"))
    assert result == "got hello"
```
```

- [ ] **Step 3: 运行测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/sar_workers/test_tool_defs.py -v`
Expected: 5 tests PASSED

- [ ] **Step 4: Commit**

```bash
git add integration/sar_workers/tool_defs.py integration/sar_workers/test_tool_defs.py
git commit -m "feat: add minimal @tool decorator (tool_defs.py) with tests"
```

---

### Task 2: react_agent.py — Worker 端 ReAct 循环

**Files:**
- Create: `integration/sar_workers/react_agent.py`
- Create: `integration/sar_workers/test_react_agent.py`

**Interfaces:**
- Consumes: `SimpleLLMClient` + `Message` from `integration/coordinator/llm_shim.py`
- Consumes: `ToolDef` from `integration/sar_workers/tool_defs.py`
- Produces: `WorkerReActAgent(llm_client, tools, system_prompt, max_steps)`，`.run(user_message) -> str`，`.update_system_prompt(prompt)`

- [ ] **Step 1: 创建 react_agent.py**

```python
"""Worker 端 ReAct 循环 — 替代 mini_agent.Agent。"""
from __future__ import annotations
import json
import logging
from typing import Any

from integration.coordinator.llm_shim import SimpleLLMClient, Message
from integration.sar_workers.tool_defs import ToolDef

logger = logging.getLogger(__name__)


class WorkerReActAgent:
    """Worker 端 ReAct 循环。

    每步调用 LLM，有 tool_calls 则执行工具并继续，无则返回最终文本。
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
        self._system_prompt = prompt

    async def run(self, user_message: str = "") -> str:
        self._messages = [Message(role="system", content=self._system_prompt)]
        if user_message:
            self._messages.append(Message(role="user", content=user_message))

        for step in range(self._max_steps):
            logger.debug(f"ReAct step {step + 1}/{self._max_steps}")
            response = await self._llm.generate(
                messages=self._messages, tools=self._tool_schemas,
            )
            self._messages.append(Message(
                role="assistant", content=response.content or "",
                tool_calls=response.tool_calls,
            ))
            if not response.tool_calls:
                return response.content or ""

            for tc in response.tool_calls:
                tool_name = tc.function.name
                tool_def = self._tools.get(tool_name)
                if tool_def is None:
                    result = f"Error: unknown tool '{tool_name}'"
                else:
                    try:
                        args = tc.function.arguments
                        if isinstance(args, str):
                            args = json.loads(args)
                        result = await tool_def.execute(**args)
                    except Exception as e:
                        result = f"Error executing {tool_name}: {e}"
                self._messages.append(Message(
                    role="tool", content=result,
                    tool_call_id=tc.id, name=tool_name,
                ))

        return f"Max steps ({self._max_steps}) exceeded"
```

- [ ] **Step 2: 创建 test_react_agent.py**

```python
"""react_agent.py 单元测试 — 使用 mock LLM client + pytest-asyncio。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from integration.sar_workers.react_agent import WorkerReActAgent
from integration.sar_workers.tool_defs import tool
from integration.coordinator.llm_shim import Message, LLMResponse, ToolCall, FunctionCall


def make_mock_llm(responses):
    client = AsyncMock()
    client.generate = AsyncMock(side_effect=responses)
    return client


@pytest.fixture
def simple_tool():
    @tool(name="echo", description="Echo input")
    async def echo(node, text: str) -> str:
        return f"echoed: {text}"
    return echo


@pytest.mark.asyncio
async def test_react_no_tool_calls():
    llm = make_mock_llm([LLMResponse(content="Done!")])
    agent = WorkerReActAgent(llm_client=llm, tools=[], system_prompt="test")
    result = await agent.run("hi")
    assert result == "Done!"
    assert llm.generate.call_count == 1


@pytest.mark.asyncio
async def test_react_with_tool_calls(simple_tool):
    llm = make_mock_llm([
        LLMResponse(tool_calls=[ToolCall(
            id="tc1", function=FunctionCall(name="echo", arguments={"text": "hello"})
        )]),
        LLMResponse(content="Echo done"),
    ])
    agent = WorkerReActAgent(llm_client=llm, tools=[simple_tool], system_prompt="test")
    result = await agent.run()
    assert result == "Echo done"
    assert llm.generate.call_count == 2
    tool_msgs = [m for m in agent._messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "echoed: hello"


@pytest.mark.asyncio
async def test_react_unknown_tool():
    llm = make_mock_llm([
        LLMResponse(tool_calls=[ToolCall(
            id="tc1", function=FunctionCall(name="nonexistent", arguments={})
        )]),
        LLMResponse(content="Fixed"),
    ])
    agent = WorkerReActAgent(llm_client=llm, tools=[], system_prompt="test")
    result = await agent.run()
    tool_msgs = [m for m in agent._messages if m.role == "tool"]
    assert "Error" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_react_max_steps():
    infinite_calls = [
        LLMResponse(tool_calls=[ToolCall(
            id=f"tc{i}", function=FunctionCall(name="echo", arguments={"text": "x"})
        )]) for i in range(5)
    ]
    llm = make_mock_llm(infinite_calls)
    @tool(name="echo", description="Echo")
    async def echo(node, text: str) -> str:
        return "ok"
    agent = WorkerReActAgent(llm_client=llm, tools=[echo], system_prompt="test", max_steps=3)
    result = await agent.run()
    assert "Max steps" in result
```

- [ ] **Step 3: 运行测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/sar_workers/test_react_agent.py -v`
Expected: 4 tests PASSED

- [ ] **Step 4: Commit**

```bash
git add integration/sar_workers/react_agent.py integration/sar_workers/test_react_agent.py
git commit -m "feat: add WorkerReActAgent (react_agent.py) with tests"
```

---

### Task 3: a2a_server.py — 精简版 A2A 通信层

**Files:**
- Create: `integration/sar_workers/a2a_server.py`
- Create: `integration/sar_workers/test_a2a_server.py`

**Interfaces:**
- Consumes: `WorkerReActAgent` from `react_agent.py`
- Produces: `A2AWorkerServer(agent_name, port, coordinator_url, react_agent, skills, model)`，`.start_in_thread()`，`.stop()`
- Protocol: JSON-RPC 2.0 at `/api/v1/jsonrpc/` with **SSE 流响应**，AgentCard at `/.well-known/agent-card.json`，WebSocket at `ws://coordinator/ws/worker/{id}`

**关键协议细节（来自 Coordinator 实际代码）：**
- Coordinator 的 `PushTaskTool._push_via_a2a()` 用 `httpx.stream("POST", url)` 读取 **SSE 流**
- 通过 `async for line in response.aiter_lines()` 解析 `data: {...}` 格式
- `extract_final_text()` 从 `result.artifact.parts[].text` 提取最终结果
- JSON-RPC 响应必须用 `StreamingResponse(media_type="text/event-stream")`，不是普通 JSON

- [ ] **Step 1: 创建 a2a_server.py**

```python
"""精简版 A2A Worker 服务器 — 替代 MARoS transport.py。

协议兼容 Coordinator（push_task.py + server.py + worker_registry.py）：
- JSON-RPC 2.0 task endpoint with SSE streaming response
- AgentCard at /.well-known/agent-card.json
- WebSocket registration + heartbeat + task result notification

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
        model: LLM 模型名（写入 AgentCard）
    """

    def __init__(
        self,
        agent_name: str,
        port: int,
        coordinator_url: str,
        react_agent: Any,
        skills: list[Any] = None,
        model: str = "deepseek-v4-flash",
    ):
        self._agent_name = agent_name
        self._port = port
        self._coordinator_url = coordinator_url.rstrip("/")
        self._react_agent = react_agent
        self._skills = skills or []
        self._model = model
        self._a2a_endpoint = f"http://localhost:{port}/"

        self._running = False
        self._ws = None
        self._thread: Optional[threading.Thread] = None

    # ── HTTP 路由 ──────────────────────────────────────────────────

    def _create_app(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse

        app = FastAPI(title=f"SAR Worker: {self._agent_name}")

        @app.get("/.well-known/agent-card.json")
        async def agent_card():
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
                "backend": "react_agent",
                "model": self._model,
                "skills": skills_list,
                "interfaces": ["a2a-jsonrpc"],
            }

        @app.post("/api/v1/jsonrpc/")
        async def jsonrpc_handler(request: Request):
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

                # 返回 SSE 流（Coordinator 通过 httpx.stream 读取）
                return StreamingResponse(
                    self._sse_stream(task_id, task_text),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            else:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    "id": req_id,
                }, status_code=400)

        return app

    async def _sse_stream(self, task_id: str, task_text: str):
        """SSE 流生成器 — 先发 working 状态，执行任务，再发 completed/failed。"""
        # 事件 1: 任务已接受（working 状态）
        working_event = {
            "jsonrpc": "2.0",
            "result": {"status": {"state": "working"}, "id": task_id},
        }
        yield f"data: {json.dumps(working_event)}\n\n"

        # 执行 ReAct 循环
        try:
            result_text = await self._react_agent.run(user_message=task_text)
            logger.info(f"[{self._agent_name}] Task {task_id} completed: {result_text[:80]}...")

            # 事件 2: 任务完成（带 artifact）
            complete_event = {
                "jsonrpc": "2.0",
                "result": {
                    "artifact": {"parts": [{"text": result_text}]},
                    "status": {"state": "completed"},
                    "id": task_id,
                },
            }
            yield f"data: {json.dumps(complete_event)}\n\n"
        except Exception as e:
            logger.error(f"[{self._agent_name}] Task {task_id} failed: {e}")
            error_event = {
                "jsonrpc": "2.0",
                "result": {
                    "artifact": {"parts": [{"text": f"Error: {e}"}]},
                    "status": {"state": "failed"},
                    "id": task_id,
                },
            }
            yield f"data: {json.dumps(error_event)}\n\n"

        # 通过 WebSocket 通知 Coordinator（额外保障）
        await self._ws_send({
            "type": "task_complete",
            "payload": {"task_id": task_id, "result": result_text if "result_text" in dir() else str(e)},
        })

    # ── WebSocket 客户端 ───────────────────────────────────────────

    async def _ws_send(self, data: dict):
        if self._ws:
            try:
                await self._ws.send(json.dumps(data))
            except Exception as e:
                logger.warning(f"[{self._agent_name}] WS send failed: {e}")

    async def _ws_loop(self):
        import websockets

        ws_url = (
            f"ws://{self._coordinator_url.replace('http://', '').replace('https://', '')}"
            f"/ws/worker/{self._agent_name}"
        )

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "type": "register",
                        "payload": {
                            "worker_id": self._agent_name,
                            "a2a_endpoint": self._a2a_endpoint,
                        },
                    }))
                    logger.info(f"[{self._agent_name}] Registered with Coordinator at {ws_url}")

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
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"[{self._agent_name}] A2A server starting on port {self._port}")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"[{self._agent_name}] Server loop error: {e}")
        finally:
            loop.close()

    async def _serve(self):
        import uvicorn

        app = self._create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="warning")
        server = uvicorn.Server(config)

        http_task = asyncio.create_task(server.serve())
        ws_task = asyncio.create_task(self._ws_loop())

        try:
            await asyncio.gather(http_task, ws_task)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"[{self._agent_name}] A2A server stopped")
```

- [ ] **Step 2: 创建 test_a2a_server.py**

```python
"""a2a_server.py 单元测试 — 测试 HTTP 路由、SSE 格式和消息格式。"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from integration.sar_workers.a2a_server import A2AWorkerServer


@pytest.fixture
def server():
    react_agent = AsyncMock()
    react_agent.run = AsyncMock(return_value="task done")
    skill = MagicMock()
    skill.name = "firefighting"
    skill.description = "Extinguish fires"
    skill.tool_names = ["use_supply", "navigate_to"]
    srv = A2AWorkerServer(
        agent_name="Alice", port=8191,
        coordinator_url="http://localhost:8080",
        react_agent=react_agent, skills=[skill],
        model="deepseek-v4-flash",
    )
    return srv


def test_agent_card(server):
    """AgentCard 端点返回正确格式，包含 backend 和 model 字段。"""
    app = server._create_app()
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Alice"
    assert data["version"] == "1.0"
    assert data["backend"] == "react_agent"
    assert data["model"] == "deepseek-v4-flash"
    assert "a2a-jsonrpc" in data["interfaces"]
    assert len(data["skills"]) == 1
    assert data["skills"][0]["name"] == "firefighting"


def test_jsonrpc_send_message_returns_sse(server):
    """JSON-RPC SendMessage 返回 SSE 流（不是普通 JSON）。"""
    app = server._create_app()
    client = TestClient(app)
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "task-001",
                "role": "ROLE_USER",
                "parts": [{"text": "Extinguish the fire"}],
            }
        },
        "id": "req-001",
    }
    resp = client.post("/api/v1/jsonrpc/", json=payload)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # 解析 SSE 事件
    events = []
    for line in resp.text.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))

    # 至少有 working + completed 两个事件
    assert len(events) >= 2
    assert events[0]["result"]["status"]["state"] == "working"
    assert events[-1]["result"]["status"]["state"] == "completed"
    assert "task done" in events[-1]["result"]["artifact"]["parts"][0]["text"]


def test_jsonrpc_unknown_method(server):
    """未知 method 返回 -32601 错误。"""
    app = server._create_app()
    client = TestClient(app)
    payload = {"jsonrpc": "2.0", "method": "UnknownMethod", "id": "req-002"}
    resp = client.post("/api/v1/jsonrpc/", json=payload)
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == -32601
```

- [ ] **Step 3: 运行测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/sar_workers/test_a2a_server.py -v`
Expected: 3 tests PASSED

- [ ] **Step 4: Commit**

```bash
git add integration/sar_workers/a2a_server.py integration/sar_workers/test_a2a_server.py
git commit -m "feat: add A2AWorkerServer with JSON-RPC 2.0 SSE streaming + WebSocket"
```

---

### Task 4: 修改 tools.py — 换 import

**Files:**
- Modify: `integration/sar_workers/tools.py:14`

**Interfaces:**
- 无接口变化，工具函数签名和行为完全不变

- [ ] **Step 1: 修改 tools.py 的 import**

```python
# 之前（第 14 行）:
from integration._maros_compat import tool

# 之后:
from integration.sar_workers.tool_defs import tool
```

- [ ] **Step 2: 运行现有工具测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/test_sar_barrier.py -v`
Expected: 5 tests PASSED（工具函数行为不变）

- [ ] **Step 3: Commit**

```bash
git add integration/sar_workers/tools.py
git commit -m "refactor: switch tools.py to use local tool_defs instead of _maros_compat"
```

---

### Task 5: 修改 sar_worker.py — 核心改造

**Files:**
- Modify: `integration/sar_workers/sar_worker.py`

**Interfaces:**
- Consumes: `WorkerReActAgent` from `react_agent.py`
- Consumes: `A2AWorkerServer` from `a2a_server.py`
- Consumes: `SimpleLLMClient` from `integration/coordinator/llm_shim.py`
- 公共接口不变：`SARWorker(agent_name, agent_idx, barrier, port, coordinator_url, model)`，`.start()`，`.update_subtask(subtask)`

- [ ] **Step 1: 重写 sar_worker.py**

关键改动：
1. `__init__` 中创建 `SimpleLLMClient`
2. `start()` 中创建 `WorkerReActAgent` + `A2AWorkerServer`，调用 `start_in_thread()`
3. 去掉 `_MockNode`、`_build_system_prompt` 中对 transport.py 的依赖
4. `update_subtask()` 同时更新 ReAct agent 的 system prompt

```python
"""SARWorker — 每个 SAR 智能体的独立 Worker，不依赖 MARoS。"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional, Callable

from integration.sar_workers.tools import SAR_TOOLS
from integration.sar_workers.skills import SAR_SKILLS

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = """You are {agent_name}, a search and rescue robot.
Work with other robots to extinguish all fires and rescue all trapped persons.
"""


class SARWorker:
    """单个 SAR 智能体的独立 Worker。

    使用自写 ReAct agent + A2A server，不依赖 mini_agent / a2a_lib / transport.py。
    """

    def __init__(
        self,
        agent_name: str,
        agent_idx: int,
        barrier,
        port: int,
        coordinator_url: str = "http://localhost:8080",
        model: str = "deepseek-v4-flash",
    ):
        self.agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port
        # 兼容 ws:// 和 http:// 前缀（experiment.py 可能传 ws://）
        self._coordinator_url = coordinator_url.replace("ws://", "http://")
        self._model = model

        self._current_subtask = "No subtask assigned yet."
        self._tools = [t.bind(self) for t in SAR_TOOLS]
        self._system_prompt = self._build_system_prompt()

        from integration.coordinator.llm_shim import SimpleLLMClient
        self._llm_client = SimpleLLMClient(model=self._model)

        self._react_agent = None
        self._a2a_server = None

    def _build_system_prompt(self) -> str:
        base = _load_prompt_template()
        base = base.replace("{agent_name}", self.agent_name)
        try:
            obs = self._barrier.get_current_obs(self._agent_idx)
        except Exception:
            obs = "No observation available yet."
        subtask_info = f"\n\n## Current Subtask\n{self._current_subtask}"
        return base + "\n\n## Current Environment State\n" + obs + subtask_info

    def update_subtask(self, subtask: str):
        self._current_subtask = subtask
        if self._react_agent:
            self._react_agent.update_system_prompt(self._build_system_prompt())

    def start(self):
        from integration.sar_workers.react_agent import WorkerReActAgent
        from integration.sar_workers.a2a_server import A2AWorkerServer

        self._react_agent = WorkerReActAgent(
            llm_client=self._llm_client,
            tools=self._tools,
            system_prompt=self._build_system_prompt(),
        )
        self._a2a_server = A2AWorkerServer(
            agent_name=self.agent_name,
            port=self._port,
            coordinator_url=self._coordinator_url,
            react_agent=self._react_agent,
            skills=SAR_SKILLS,
            model=self._model,
        )
        self._a2a_server.start_in_thread()
        logger.info(f"[{self.agent_name}] Worker started on port {self._port}")

    def stop(self):
        """停止 Worker — 清理 A2A server 资源。"""
        if self._a2a_server:
            self._a2a_server.stop()
        logger.info(f"[{self.agent_name}] Worker stopped")


def _load_prompt_template() -> str:
    prompt_path = Path(__file__).parent / "prompt.md"
    try:
        return prompt_path.read_text()
    except FileNotFoundError:
        logger.warning("prompt.md not found at %s, using default", prompt_path)
        return _DEFAULT_PROMPT
```

- [ ] **Step 2: 运行集成测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/test_sar_barrier.py -v`
Expected: 5 tests PASSED

- [ ] **Step 3: Commit**

```bash
git add integration/sar_workers/sar_worker.py
git commit -m "refactor: rewrite SARWorker to use self-contained ReAct agent + A2A server"
```

---

### Task 6: 精简 _maros_compat.py

**Files:**
- Modify: `integration/_maros_compat.py`

**Interfaces:**
- 保留：`Skill` 导出、`get_start_a2a_transport()`、a2a_lib shim
- 删除：`tool` 导出（不再需要）

- [ ] **Step 1: 删除 tool 导出**

```python
# 删除这两行：
# _tool_decorator = _load_by_path("_maros_td", "tool_decorator.py")
# tool = _tool_decorator.tool

# 更新 __all__：
__all__ = ["Skill", "get_start_a2a_transport"]
```

- [ ] **Step 2: 验证 Coordinator 不受影响**

Run: `cd /home/wyh/daily_work/LLaMAR && python -c "from integration._maros_compat import Skill; print('OK')"`
Expected: `OK`

- [ ] **Step 3: 运行全部测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/ -v`
Expected: 所有测试 PASSED

- [ ] **Step 4: Commit**

```bash
git add integration/_maros_compat.py
git commit -m "refactor: remove tool export from _maros_compat (Worker no longer needs it)"
```

---

### Task 7: 端到端验证

**Files:**
- Verify: `integration/experiment.py` 无需修改
- Verify: `integration/test_integration.py` 仍然通过

- [ ] **Step 1: 运行全部单元测试**

Run: `cd /home/wyh/daily_work/LLaMAR && python -m pytest integration/ -v`
Expected: 所有测试 PASSED

- [ ] **Step 2: 运行端到端实验**

Run: `cd /home/wyh/daily_work/LLaMAR && NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 python integration/experiment.py --scene=1 --agents=2 --seed=42`
Expected: 实验正常运行，Coverage 和 Transport Rate 有值（不要求 Finished=True，只要架构跑通）

- [ ] **Step 3: 验证零 MARoS 依赖**

Run: `cd /home/wyh/daily_work/LLaMAR && python -c "
from integration.sar_workers.tool_defs import tool, ToolDef
from integration.sar_workers.react_agent import WorkerReActAgent
from integration.sar_workers.a2a_server import A2AWorkerServer
print('All imports OK — zero MARoS dependency')
"`
Expected: `All imports OK — zero MARoS dependency`

- [ ] **Step 4: Final Commit**

```bash
git add -A
git commit -m "feat: complete ReAct agent replacement — mini_agent fully decoupled"
```
