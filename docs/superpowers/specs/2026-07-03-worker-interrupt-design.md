---
日期: 2026-07-03
文档类型: 技术设计规格
文档概述: Worker→Coordinator 中断求助机制 — Worker 执行中遇到疑问时暂停并向 Coordinator 请求帮助
---

# Worker→Coordinator 中断求助机制

## 问题

Worker 的 LLM 在执行任务过程中遇到指令模糊或决策困难时，无法向 Coordinator 请求澄清。当前只能自主决策（可能出错）或静默失败。

## 目标

提供 `ask_coordinator(question)` 工具，让 Worker 在 Agent loop 中暂停执行，通过 push notification 向 Coordinator 求助，等 Coordinator 回复后再继续。

## 架构

```
Worker                                       Coordinator
══════                                       ═══════════
Agent loop                                   Agent loop
  think                                       think
  → ask_coordinator("路被堵了，绕哪？")       → 下轮 loop 看到 EventStore 注入
    ├─ future = registry.register(task_id)    → respond_to_worker("alice-1", "东面绕")
    ├─ push_sender → Coordinator callback       └─ httpx.post(worker_callback_url,
    │    artifact_update("[HELP] 路被堵了")         StreamResponse{artifact_update: "东面绕"})
    └─ await future  ← 阻塞

push callback ◄── HTTP POST ─────────────────
  resolve_future(task_id, "东面绕")

Agent loop 继续
  observe: "东面绕"
  think: "好，从东面绕行"
```

**关键设计决策**：两条通信链路均使用 HTTP POST → `/a2a/push-callback`，**不经过 A2A `client.send_message()`**。原因是 `send_message` 会在 Worker 上启动第二个 Agent loop，与正在阻塞等待的 Agent 冲突。

## 组件

### 1. WorkerFutureRegistry（新建）

`src/a2a/worker/future_registry.py` — Worker 端的异步 Future 注册表。

```python
_worker_future_registry: dict[str, asyncio.Future] = {}

def register_future(task_id: str) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    _worker_future_registry[task_id] = future
    return future

def resolve_future(task_id: str, result: Any) -> None:
    future = _worker_future_registry.pop(task_id, None)
    if future and not future.done():
        future.set_result(result)
```

设计参照 Coordinator 的 `task_store.resolve_global_future`，但绑定在 Worker 侧。每个 Worker 进程一个全局 registry。

### 2. Worker `/a2a/push-callback` 端点

`src/a2a/worker/a2a_server.py` — 在 Starlette routes 列表中加入：

```python
@app.post("/a2a/push-callback")
async def worker_push_callback(request):
    from google.protobuf.json_format import ParseDict
    from a2a.types.a2a_pb2 import StreamResponse
    from a2a.worker.future_registry import resolve_future

    body = await request.json()
    sr = StreamResponse()
    ParseDict(body, sr)

    task_id = None
    if sr.HasField("artifact_update"):
        au = sr.artifact_update
        task_id = au.task_id
        texts = [p.text for p in au.artifact.parts if p.text]
        if texts and task_id:
            resolve_future(task_id, " ".join(texts))

    return {"status": "ok"}
```

注意：Worker 的 A2A Server 用 Starlette（不是 FastAPI），需要手动创建 `Starlette(routes=[...])` 路由。选项：在 `create_worker_a2a_server` 内创建 `Starlette` 实例后，在 `create_jsonrpc_routes` 之前或之后追加自定义 Route。

### 3. AskCoordinatorTool（新建）

`src/a2a/worker/tools/ask_coordinator.py` — Worker Agent 调用的工具。

```python
class AskCoordinatorTool(Tool):
    def __init__(self, coordinator_callback_url: str, task_id: str):
        self._coordinator_url = coordinator_callback_url.rstrip("/") + "/a2a/push-callback"
        self._task_id = task_id

    async def execute(self, question: str) -> ToolResult:
        from a2a.worker.future_registry import register_future, resolve_future
        future = register_future(self._task_id)

        import json
        from google.protobuf.json_format import MessageToDict
        from a2a.types.a2a_pb2 import StreamResponse, Part, Artifact
        import httpx

        sr = StreamResponse()
        sr.artifact_update.task_id = self._task_id
        sr.artifact_update.artifact.CopyFrom(
            Artifact(parts=[Part(text=f"[HELP] {question}")])
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                await client.post(
                    self._coordinator_url,
                    content=json.dumps(MessageToDict(sr)),
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:
                resolve_future(self._task_id, f"[send failed: {e}]")
                return ToolResult(success=False, content=f"[send failed: {e}]")

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            return ToolResult(success=True, content=result)
        except asyncio.TimeoutError:
            resolve_future(self._task_id, "[no response from coordinator]")
            return ToolResult(success=False, content="[no response from coordinator]")
```

**注意**：工具通过原始 HTTP POST 发送——**不**使用 A2A SDK 的 `push_sender`。SDK 的 `push_sender` 绑定到 `DefaultRequestHandler` 的 `EventConsumer`，在工具上下文中不可用。原始 POST 更简洁，且与 Coordinator 回调处理程序兼容。httpx 客户端在每次 `execute()` 调用中短生命周期创建。

**Coordinator 回调 URL 注入**：`create_worker_a2a_server` 接受 `coordinator_callback_url` 参数，传递给 `AgentAdapter`，后者注入到 `AskCoordinatorTool` 构造器。

### 4. Worker push callback 和 future 生命周期

关键问题：Worker Agent loop 通过 `ask_coordinator → await future` 保持等待。为确保 future 在 Agent loop 完成时被解除，Agent loop 完成时需取消所有未完成的 help futures。`AgentAdapter.execute()` 的超时处理应在 `CancelledError` 上调用 `resolve_future(task_id, "[cancelled]")`。

### 5. Coordinator push callback 扩展

`src/a2a/coordinator/server.py` — 现有推送回调处理程序识别 `[HELP]` 前缀：

```python
# artifact_update 分支内
texts = [p.text for p in au.artifact.parts if p.text]
if texts:
    combined = " ".join(texts)
    if combined.startswith("[HELP] "):
        event_store.append(task_id, "help_request", text=combined[6:])
    else:
        _push_artifact_cache.setdefault(task_id, []).extend(texts)
        event_store.append(task_id, "artifact_update", text=combined)
```

注意：`[HELP]` 事件**不是**终端事件——不会触发 `resolve_global_future`。它仅写入 EventStore。

### 6. EventStore 帮助事件渲染

`src/a2a/coordinator/event_store.py` — `get_summary()` 中添加格式化分支：

```python
elif r.event_type == "help_request":
    text_short = (r.text or "")[:200]
    task_lines.append(f"  {ts_str} HELP: {text_short}")
```

### 7. RespondWorkerTool（新建）

`src/a2a/builtin_tools/respond_worker.py` — Coordinator Agent 调用的工具。

```python
from a2a.coordinator.agent_registry import AgentNotFoundError

class RespondWorkerTool(Tool):
    def __init__(self, store: TaskStore, registry: AgentRegistry):
        self._store = store
        self._registry = registry

    async def execute(self, task_id: str, response: str) -> ToolResult:
        import json
        import httpx
        from urllib.parse import urlparse
        from google.protobuf.json_format import MessageToDict
        from a2a.types.a2a_pb2 import StreamResponse, Part, Artifact

        node = self._store.get_node(task_id)
        if not node or not node.worker_id:
            return ToolResult(success=False, content=f"Task '{task_id}' not found")

        try:
            agent_info = self._registry.get(node.worker_id)
        except AgentNotFoundError:
            return ToolResult(success=False, content=f"Worker '{node.worker_id}' not found")

        parsed = urlparse(agent_info.endpoint)
        worker_callback_url = f"{parsed.scheme}://{parsed.netloc}/a2a/push-callback"

        sr = StreamResponse()
        sr.artifact_update.task_id = task_id
        sr.artifact_update.artifact.CopyFrom(
            Artifact(parts=[Part(text=response)])
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                await client.post(
                    worker_callback_url,
                    content=json.dumps(MessageToDict(sr)),
                    headers={"Content-Type": "application/json"},
                )
                return ToolResult(success=True, content=f"Response sent to {task_id}")
            except Exception as e:
                return ToolResult(success=False, content=f"[reply failed: {e}]")
```

**Worker callback URL 推导**：从 `AgentRegistry` 中的 Worker endpoint 直接构造——`http://{host}:{port}/a2a/push-callback`。无需从推送通知负载中提取，因为 Coordinator 已经持有 Worker 的端点信息。

### 8. AgentAdapter 工具注入

`src/a2a/worker/agent_adapter.py` — 新增构造参数并注入：

```python
def __init__(self, ..., coordinator_callback_url: str = "", ...):
    self._coordinator_callback_url = coordinator_callback_url
```

在 `execute()` 中创建工具时，`AskCoordinatorTool` 添加到 `extra_tools` 列表。`task_id` 来自 `context.current_task.id`（在 `execute()` 中可用）。httpx 客户端在 `AskCoordinatorTool.execute()` 内部短生命周期创建。

**注意**：Worker 端 `AgentAdapter` 的 agent 工厂（`agent_factory=lambda **kw: self._build_agent()`）当前忽略 `extra_tools` 等运行时 kwargs。需要在 `execute()` 中将 `ask_tool` 直接拼接到 `self._extra_tools` 后再提交给 controller，绕过工厂的 kwarg 限制。

### 9. Coordinator AgentExecutor 工具注入

`src/a2a/coordinator/agent_executor.py` — `RespondWorkerTool` 添加到 `_execute_agentic` 中的 `tools` 列表中：

```python
tools = [
    ...,
    RespondWorkerTool(store, self._registry),
]
```
注意：工具有自己的短生命周期 httpx 客户端（在 `execute()` 内部创建）。HTTP POST 是一个单次操作，不需要持久化客户端。

## 数据流完整示例

```
1. Worker Agent 执行中 → ask_coordinator("路被堵了，绕哪？")
   ├─ register_future: _worker_future_registry["alice-1"] = Future
   ├─ raw httpx.post → POST Coordinator callback
   │    StreamResponse{artifact_update: {task_id: "alice-1", artifact: {parts: [{text: "[HELP] 路被堵了"}]}}}
   └─ await future  ← 阻塞了（最多 120s）

2. Coordinator push callback
   ├─ artifact_update: texts = ["[HELP] 路被堵了"]
   ├─ 开始于 "[HELP]" → event_store.append("alice-1", "help_request", text="路被堵了")
   └─ 非终端 → return {"status": "ok"}

3. Coordinator Agent 下一轮 loop
   ├─ pre_llm → ContextManager._render_memory_block()
   │    → EventStore.get_summary() ↓
   │    ### Worker Events
   │    - alice-1:
   │      10:23:45 HELP: 路被堵了
   │
   ├─ LLM 看到上下文中的帮助请求
   └─ 调用 respond_to_worker("alice-1", "东面绕")

4. respond_to_worker 执行
   ├─ 从 AgentRegistry 查 Worker 回调 URL
   ├─ 构造 StreamResponse{artifact_update: {task_id: "alice-1", artifact: {parts: [{text: "东面绕"}]}}}
   └─ HTTP POST → worker:8191/a2a/push-callback

5. Worker push callback
   ├─ artifact_update: task_id="alice-1", text=["东面绕"]
   └─ resolve_future("alice-1", "东面绕")
        → _worker_future_registry["alice-1"].set_result("东面绕")

6. ask_coordinator Future 已 resolve
   ├─ await future → "东面绕"
   └─ 返回 ToolResult(success=True, content="东面绕")

7. Worker Agent loop 继续
   └─ LLM: "好，从东面绕行"
```

## 错误场景

| 场景 | 处理 |
|------|------|
| Coordinator 从不回复 | `asyncio.wait_for(future, 120s)` 抛出 `TimeoutError`，返回 `[no response from coordinator]` |
| 任务不在最新调度中 | `get_node(task_id)` 返回 None → 返回 `"No dispatched task found"` 错误 |
| Agent loop 在等待期间退出 | `AgentAdapter.execute()` 的 try/finally 调用 `resolve_future(task_id, "[cancelled]")` |
| 重复的帮助请求 | 第二次调用会覆盖 `_worker_future_registry[task_id]` 中的 future。先前的 future 保持未解析状态，但在超时后会被垃圾回收。对于幂等性，可接受 |
| Worker 在 AgentRegistry 中不存在 | `AgentRegistry.get()` 抛出 `AgentNotFoundError`，工具捕获后返回 `"Worker '...' not found"` |

## 不需要修改的部分

| 组件 | 原因 |
|------|------|
| `router.py` 的 `send_task_async` | 未使用——帮助响应通过直接 HTTP POST 传输 |
| `CollectResultsTool` | `[HELP]` 事件不是 terminal，不与之交互 |
| `ContextManager.observe()` | 帮助请求不是工具结果——它们来自 push callback |
| `AgentHooks` | 无需新的钩子——EventStore 使用现有注入路径 |

## 文件变更清单

| 文件 | 操作 | 行数（预估） |
|------|------|-------------|
| `src/a2a/worker/tools/__init__.py` | 新建 | ~0 |
| `src/a2a/worker/future_registry.py` | 新建 | ~30 |
| `src/a2a/worker/a2a_server.py` | 修改（添加路由 + coordinator_callback_url 参数） | +50 |
| `src/a2a/worker/tools/ask_coordinator.py` | 新建 | ~80 |
| `src/a2a/coordinator/builtin_tools/respond_worker.py` | 新建 | ~80 |
| `src/a2a/coordinator/server.py` | 修改（`[HELP]` 前缀处理） | +10 |
| `src/a2a/coordinator/event_store.py` | 修改（help_request 渲染） | +5 |
| `src/Agent/router_agent/context.py` | 无需修改（已集成 EventStore） | 0 |
| `src/a2a/worker/agent_adapter.py` | 修改（注入工具 + CancelledError 清理） | +20 |
| `src/a2a/coordinator/agent_executor.py` | 修改（注入 RespondWorkerTool） | +5 |
| `src/a2a/worker/cli.py` | 修改（添加 `--coordinator-callback-url` 选项） | +5 |
| `sar_orch/worker.py` | 修改（传递 coordinator_callback_url） | +5 |

## 后续工作

- **超时适配 Worker**：Worker 的超时逻辑需要在 `ask_coordinator` 阻塞时暂停步骤计数，而非依赖 Agent loop。
- **安全**：Worker 推送回调端点免于身份验证的生产加固。
