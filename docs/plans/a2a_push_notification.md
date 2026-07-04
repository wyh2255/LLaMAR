---
日期: 2026-07-03
文档类型: 技术实施计划
文档概述: A2A PushNotification 改造方案 — 消除 Coordinator Agent 阻塞等待 Worker 结果的问题
---

# A2A PushNotification 改造方案

## 问题

`CollectResultsTool` 阻塞等待 Worker 结果，浪费编排时间：

```
dispatch_task → asyncio.create_task(push_task HTTP stream) → 
collect_results → await asyncio.gather(*futures) → 阻塞等全部 stream 完成
```

## 目标

使用 A2A PushNotification 标准协议，让 Worker 完成任务后主动推送结果，Coordinator 不再阻塞等待：

```
dispatch_task → fire-and-forget 发任务 (return_immediately) + 注册 Future
Worker 完成 → EventConsumer → PushNotificationSender → HTTP POST callback
Coordinator 收到 callback → resolve Future → collect_results 立即返回
```

## 架构

```
Coordinator (port 8080/8081)                  Worker (port 8191)
─────────────────────────────                  ─────────────────

FastAPI (8080)          A2A Server (8081)      A2A Server
  │                        │                     │
  │  POST /a2a/push-cb    │                     │
  │  └─ resolve Future    │                     │
  │                        │                     │
  │                        │ send_task_async()   │
  │                        │ ──JSON-RPC────────► │
  │                        │   (streaming=False, │
  │                        │    return_immediately)
  │                        │                     │
  │                        │ ◄──Task(WORKING)── │
  │                        │                     │
  │                        │                     │ AgentExecutor.run()
  │                        │                     │   └─ 完成任务
  │                        │                     │ EventConsumer
  │                        │                     │   └─ PushNotificationEvent
  │                        │                     │ push_sender.send_notification()
  │ ◄──HTTP POST───────── │                     │
  │   (StreamResponse)     │                     │
  │                        │                     │
  │ resolve_global_future()│                     │
  │   → Future resolved    │                     │
```

## 改动清单

### 1. `src/a2a/coordinator/task_store.py` — 模块级 Future 注册表

**新增**（模块级，`class TaskStore` 之前）：

```python
import asyncio
from typing import Any

_global_future_registry: dict[str, asyncio.Future] = {}

def resolve_global_future(task_id: str, result: Any) -> None:
    """由 push callback handler 调用，解析 dispatch_task 创建的 Future。"""
    future = _global_future_registry.pop(task_id, None)
    if future is not None and not future.done():
        future.set_result(result)
```

**TaskStore 类新增方法**：

```python
def register_future(self, task_id: str) -> asyncio.Future:
    """注册 Future，同时存入本地 _futures 和全局注册表。"""
    future = asyncio.get_running_loop().create_future()
    self._futures[task_id] = future
    _global_future_registry[task_id] = future
    return future
```

设计说明：
- `_futures` 供 `CollectResultsTool` 读取（原有路径不变）
- `_global_future_registry` 供 `resolve_global_future()` 写入（push callback 路径）
- 两个 dict 指向同一个 Future 对象

### 2. `src/a2a/coordinator/router.py` — 新增 `send_task_async()` + 非流式 Client

**导入**（line 16-22，加 `TaskPushNotificationConfig`）：

```python
from a2a.types.a2a_pb2 import (
    SendMessageRequest,
    Message,
    Part,
    StreamResponse,
    Role,
    TaskPushNotificationConfig,  # 新增
)
```

**`__init__` 新增字段**（line 282 后）：

```python
self._sdk_clients_non_streaming: dict[str, Client] = {}
```

说明：`streaming=True` 的客户端会导致 server 路由到 `on_message_send_stream`，后者**忽略** `return_immediately`。必须使用 `streaming=False` 的独立客户端池。

**新增方法**：

```python
async def _get_non_streaming_sdk_client(self, agent_id: str, endpoint: str) -> Client:
    """获取或创建 Worker 的非流式 SDK Client。"""
    if agent_id not in self._sdk_clients_non_streaming:
        httpx_client = await self._get_httpx_client()
        config = ClientConfig(
            streaming=False,
            httpx_client=httpx_client,
            supported_protocol_bindings=[
                TransportProtocol.JSONRPC,
                TransportProtocol.HTTP_JSON,
            ],
        )
        self._sdk_clients_non_streaming[agent_id] = await create_client(endpoint, config)
    return self._sdk_clients_non_streaming[agent_id]

async def send_task_async(
    self,
    agent_id: str,
    prompt: str,
    callback_url: str,
    task_id: str,
    context_id: str | None = None,
) -> str:
    """非阻塞发送任务到 Worker（return_immediately + push_notification_config）。

    返回 task_id，结果由 push callback 异步交付。
    """
    agent_info = self._registry.get(agent_id)
    message = Message(role=Role.ROLE_USER, parts=[Part(text=prompt)], task_id=task_id)
    if context_id:
        message.context_id = context_id

    push_config = TaskPushNotificationConfig(url=callback_url)
    request = SendMessageRequest(message=message)
    request.configuration.return_immediately = True
    request.configuration.task_push_notification_config.CopyFrom(push_config)

    client = await self._get_non_streaming_sdk_client(agent_id, agent_info.endpoint)
    async for stream_response in client.send_message(request):
        if stream_response.HasField("task"):
            return stream_response.task.id
    return ""
```

**`close()` 方法末尾添加清理**（line 675）：

```python
for client in self._sdk_clients_non_streaming.values():
    try:
        await client.close()
    except Exception:
        pass
self._sdk_clients_non_streaming.clear()
```

### 3. `src/a2a/builtin_tools/dispatch_task.py` — 改用 Future + send_task_async

**构造函数**：

```python
def __init__(self, store: TaskStore, coordinator_host: str = "localhost", coordinator_port: int = 8080):
    self._store = store
    self._coordinator_host = coordinator_host
    self._coordinator_port = coordinator_port
```

**`execute()` 方法**（改动 `asyncio.create_task` 部分）：

```python
# 原代码：
# future = asyncio.create_task(
#     router.push_task(agent_id, prompt, context_id=self._store.context_id)
# )
# self._store._futures[task_id] = future

# 新代码：
future = self._store.register_future(task_id)
callback_url = f"http://{self._coordinator_host}:{self._coordinator_port}/a2a/push-callback"

async def _dispatch_with_error_handling():
    try:
        await self._store._router.send_task_async(
            agent_id, prompt, callback_url, task_id,
            context_id=self._store.context_id,
        )
    except Exception as e:
        # send_task_async 失败时 resolve Future，避免 CollectResultsTool 永久挂起
        if not future.done():
            future.set_result(f"[dispatch failed: {e}]")

asyncio.create_task(_dispatch_with_error_handling())
```

其余逻辑（守卫、task_id 生成、plan node、dispatched_count、set_state）不变。

### 4. `src/a2a/builtin_tools/collect_results.py` — 处理 push 路径的字符串结果

在结果处理循环的 `else` 前新增分支（push callback resolve 时传入的是 str，非 `list[StreamResponse]`）：

```python
elif isinstance(raw, str):
    # Push notification 路径：raw 已经是结果文本
    text = raw
    self._store._results[tid] = text
    truncated = text[:_RESULT_TRUNCATE]
    if len(text) > _RESULT_TRUNCATE:
        truncated += "...[truncated, use query_task_results for full output]"
    output.append({"task_id": tid, "success": True, "result": truncated})
    self._store.set_state(tid, "done", result=truncated)
```

原 `else` 分支（`list[StreamResponse]` 路径）保持不变，保证向下兼容。

### 5. `src/a2a/coordinator/server.py` — Push callback 端点 + artifact 缓存

**模块级变量**（`CoordinatorServer` 类前，line 53-54）：

```python
import asyncio

# Push notification artifact text cache
# Worker 可能多次推送（artifact + status），需聚合
_push_artifact_cache: dict[str, list[str]] = {}
```

**`_build_app()` 内新增路由**：

```python
@app.post("/a2a/push-callback")
async def handle_push_notification(request: Request):
    from google.protobuf.json_format import ParseDict
    from a2a.types.a2a_pb2 import StreamResponse, TaskState
    from a2a.coordinator.task_store import resolve_global_future

    body = await request.json()
    sr = StreamResponse()
    ParseDict(body, sr)

    task_id = None
    is_terminal = False

    if sr.HasField("task"):
        t = sr.task
        task_id = t.id
        is_terminal = t.status.state in (
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
        )
    elif sr.HasField("artifact_update"):
        au = sr.artifact_update
        task_id = au.task_id
        if au.HasField("artifact"):
            texts = [p.text for p in au.artifact.parts if p.text]
            if texts:
                _push_artifact_cache.setdefault(task_id, []).extend(texts)
    elif sr.HasField("status_update"):
        su = sr.status_update
        task_id = su.task_id
        if su.HasField("status"):
            is_terminal = su.status.state in (
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
            )

    if task_id and is_terminal:
        # 短延迟窗口，等待可能乱序到达的 artifact_update
        async def _resolve_with_delay():
            await asyncio.sleep(0.1)
            parts = _push_artifact_cache.pop(task_id, [])
            text = " ".join(parts) if parts else "(no artifact text)"
            resolve_global_future(task_id, text)
        asyncio.create_task(_resolve_with_delay())

    return {"status": "ok"}
```

设计说明：
- Worker 的 `EventConsumer` 对 **每个** `PushNotificationEvent`（即所有 Task/TaskStatusUpdateEvent/TaskArtifactUpdateEvent）都调用 `push_sender.send_notification()`
- 因此同一个 task_id 可能收到多次 HTTP POST
- `_push_artifact_cache` 按 `list[str]` 累积所有 artifact 文本
- terminal 事件触发时延迟 100ms resolve，给可能乱序到达的 artifact 留缓冲窗口
- 延迟通过 `asyncio.create_task` 实现，不阻塞 callback 端点响应

### 6. `src/a2a/worker/a2a_server.py` — 启用 PushNotification

**改动点**：

```python
import httpx
from a2a.server.tasks import InMemoryPushNotificationConfigStore, BasePushNotificationSender

# AgentCard 启用 push_notifications
capabilities = AgentCapabilities(streaming=True, push_notifications=True)

# 创建 push 基础设施（在 DefaultRequestHandler 创建前）
push_config_store = InMemoryPushNotificationConfigStore()
push_sender = BasePushNotificationSender(
    httpx_client=httpx.AsyncClient(),
    config_store=push_config_store,
)

# 注入 DefaultRequestHandler
request_handler = DefaultRequestHandler(
    agent_executor=executor,
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
    push_config_store=push_config_store,
    push_sender=push_sender,
)
```

注意：`BasePushNotificationSender.__init__` 的第一个参数是 `httpx_client`，第二个才是 `config_store`。顺序错误会导致运行时 TypeError。

### 7. `src/a2a/coordinator/agent_executor.py` — 贯穿传递 callback host/port

**`__init__` 新增参数**：

```python
def __init__(
    self,
    ...
    coordinator_host: str = "localhost",
    coordinator_port: int = 8080,
):
    ...
    self._coordinator_host = coordinator_host
    self._coordinator_port = coordinator_port
```

**`_execute_agentic` 中创建 `DispatchTaskTool` 处**（line 284）：

```python
# 原：
# DispatchTaskTool(store),
# 改为：
DispatchTaskTool(
    store,
    coordinator_host=self._coordinator_host,
    coordinator_port=self._coordinator_port,
),
```

### 8. `src/a2a/coordinator/a2a_server.py` — 贯穿参数

**`create_coordinator_a2a_server` 函数签名**：

```python
def create_coordinator_a2a_server(
    ...,
    coordinator_host: str = "localhost",
    coordinator_port: int = 8080,
):
    executor = CoordinatorAgentExecutor(
        ...,
        coordinator_host=coordinator_host,
        coordinator_port=coordinator_port,
    )
```

**`server.py` `_build_app().lifespan` 调用处**（line 183-198）：

```python
a2a_srv = create_coordinator_a2a_server(
    host=self._host,
    port=self._a2a_port,
    coordinator_host=self._host,     # 新增
    coordinator_port=self._port,     # 新增（注意是 self._port，不是 self._a2a_port）
    ...
)
```

注意：`self._host` = "0.0.0.0"（绑定地址），但回调 URL 需要 Worker 可达的地址。在 dispatch_task 接收时 `coordinator_host` 默认 "localhost" 在开发环境足够。生产环境需要从配置读取实际可达地址。

## 不需要改动的

| 组件 | 原因 |
|------|------|
| `AgentAdapter.execute()` (`worker/agent_adapter.py`) | `EventConsumer` 在 SDK 内部自动处理 push 通知发送 |
| `Agent.run()` 循环 | 没有任何改动 |
| `router.push_task()` | 保留兼容旧路径 |
| `CollectResultsTool._extract_text()` 旧逻辑 | 新分支在 `else` 之前，不碰原有 `list[StreamResponse]` 路径 |
| `TaskStore` 其他方法 | `register_future` 是新增，不影响现有行为 |

## 数据流完整示例

```
1. DispatchTaskTool.execute("Alice", "fight fire", task_id="alice-1")
   ├─ store.register_future("alice-1") → asyncio.Future
   ├─ store._futures["alice-1"] = Future          (CollectResultsTool 读)
   ├─ _global_future_registry["alice-1"] = Future  (push callback 写)
   └─ asyncio.create_task(router.send_task_async("Alice", ..., task_id="alice-1"))
        └─ 非流式 A2A client.send_message()
             └─ JSON-RPC: SendMessage(request={message: {task_id: "alice-1"}, configuration: {return_immediately: True, task_push_notification_config: {url: "http://localhost:8080/a2a/push-callback"}}})

2. Worker A2A Server 接收
   ├─ DefaultRequestHandler.on_message_send()
   ├─ 创建 ActiveTask + AgentExecutor 后台执行
   └─ 返回 Task(id="alice-1", state=WORKING) 立即返回

3. Worker AgentExecutor 执行完毕
   ├─ Enqueue TaskArtifactUpdateEvent(artifact.parts=["Region extinguished!"])
   ├─ Enqueue TaskStatusUpdateEvent(state=COMPLETED)
   └─ EventConsumer._update_task_state()
        └─ push_sender.send_notification("alice-1", event)
             └─ HTTP POST → http://localhost:8080/a2a/push-callback
                  ├─ {artifact_update: {task_id: "alice-1", artifact: {parts: [{text: "Region extinguished!"}]}}}
                  └─ {status_update: {task_id: "alice-1", status: {state: "COMPLETED"}}}

4. Coordinator push callback
   ├─ artifact_update: _push_artifact_cache["alice-1"] = ["Region extinguished!"]
   ├─ status_update (COMPLETED) → asyncio.create_task(_resolve_with_delay)
   │    └─ 100ms delay → pop artifacts → "Region extinguished!"
   │    └─ resolve_global_future("alice-1", "Region extinguished!")
   └─ _global_future_registry["alice-1"].set_result("Region extinguished!")

5. CollectResultsTool.execute(["alice-1"])
   ├─ futures = [store._futures["alice-1"]]  → 已 resolved
   ├─ await asyncio.gather() → 立即返回 "Region extinguished!"
   └─ result: {"task_id": "alice-1", "success": true, "result": "Region extinguished!"}
```

## 风险与注意事项

1. **`0.0.0.0` 不可路由** — callback URL 中的 `coordinator_host` 默认 "localhost" 在开发环境工作。生产环境需从配置读取。
2. **并发编排冲突** — `_global_future_registry` 是进程级单例，同时运行的多个编排使用相同的 task_id 会冲突。当前 SAR 场景每次只运行一个编排，无此风险。
3. **push 回调可能丢失** — `BasePushNotificationSender` 使用 HTTP POST，无重试机制。若 callback 端点暂时不可用，通知会丢失。可后续加入重试。
4. **多次推送** — `EventConsumer` 对每个状态变更都调用 `push_sender.send_notification()`。应用程序器要正确处理非 terminal 事件（忽略或缓存）。
5. **dispatch 失败容错** — `send_task_async` 抛异常时（Worker 不可达、网络超时），`_dispatch_with_error_handling()` 将 Future resolve 为错误文本。`CollectResultsTool` 不会永久挂起，但主流程需判断 result 前缀来识别 dispatch 失败。
6. **artifact 延迟窗口竞态** — 100ms 延迟覆盖 HTTP 乱序送达的常见场景。极端网络延迟下 terminal 事件可能先于 artifact 到达，窗口内未收到 artifact 会导致 `"(no artifact text)"`。
