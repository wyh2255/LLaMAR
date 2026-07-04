---
日期: 2026-06-29
文档类型: 技术方案
文档概述: Worker Agent 文本返回拦截机制 — 当 Worker LLM 返回纯文本时，通过 WebSocket 向 Coordinator 请求新指令，实现单次 A2A 任务内的多轮交互
---

# Worker Report-Back 机制

## 问题

Worker Agent 的 LLM 在调用完 `get_agent_state()`（GPS，不消耗 step）后，返回纯文本而非工具调用，导致 `agent.run()` 在 `agent.py:504` 直接退出。Barrier step 永远不推进，benchmark 全部超时。

```python
# agent.py:503-510 — 当前逻辑
if not response.tool_calls:
    return response.content  # ← 直接退出，barrier 永远不步进
```

## 核心思路

在 `agent.run()` 中加 hook：当 LLM 返回纯文本时，不退出，而是：

1. 通过 WebSocket 把文本发给 Coordinator
2. Coordinator 做一次 RouterAgent 单轮推理，基于全局环境状态生成新指令
3. 新指令作为 user message 追加到 Agent 消息历史
4. Agent 循环继续，LLM 基于新提示生成工具调用

```
Worker agent.run()                     Coordinator Server
       │                                       │
       │ LLM 返回文本                            │
       │ (无 tool_calls)                        │
       ↓                                       │
  text_response_callback(text) ──WS──>  need_instructions
       │                                       │
       │                              RouterAgent 单轮推理:
       │                              1. query_sar_state 取快照
       │                              2. system prompt + 快照 + Worker 文本
       │                              3. LLM → 新指令
       │                                       │
       │ <──WS── new_instructions(text)         │
       │                                       │
  add user message 到历史                        │
  continue agent loop                           │
       │                                       │
       │ LLM → tool_call(navigate_to/fight...) ✅
       ↓                                       │
  (正常推进 barrier step)                       │
```

## 实现方案

### 1. `src/Agent/worker_agent/agent.py` — agent.run() 新增 callback

**改动点**：`run()` 方法新增 `text_response_callback` 参数。在 LLM 返回无 tool_calls 时，调 callback 替代 return。

```python
async def run(
    self,
    cancel_event: Optional[asyncio.Event] = None,
    step_callback: Optional[Callable[..., Awaitable[None]]] = None,
    text_response_callback: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
) -> str:
```

修改 `agent.py:503-510`：

```python
# 检查是否任务完成（无 tool calls）
if not response.tool_calls:
    if text_response_callback:
        reply = await text_response_callback(response.content)
        if reply:
            # Coordinator 发回了新指令，追加为 user message 继续
            self.messages.append(Message(role="user", content=reply))
            step += 1
            continue
    # 无 callback 或 callback 返回空 → 按原逻辑退出
    return response.content
```

### 2. `src/a2a/worker/coordinator_client.py` — 双向通信

当前：只有发送（register + heartbeat），**无接收循环**。

**改动**：

```python
class CoordinatorWebSocketClient:
    def __init__(self, ...):
        ...
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._receive_task: asyncio.Task | None = None

    async def connect(self):
        await self._ws.connect(self._coordinator_url + f"/ws/worker/{self._worker_id}")
        await self._send({"type": "register", "payload": {"worker_id": self._worker_id, "a2a_endpoint": self._a2a_endpoint}})
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())  # 新增

    async def _receive_loop(self):
        """持续接收 WS 消息，将 new_instructions 分发给等待的 future"""
        try:
            async for message in self._ws:
                data = json.loads(message)
                if data.get("type") == "new_instructions":
                    payload = data.get("payload", {})
                    reply_id = payload.get("reply_id")
                    if reply_id in self._pending_replies:
                        future = self._pending_replies.pop(reply_id)
                        future.set_result(payload.get("text", ""))
        except Exception as e:
            logger.warning("WS receive loop ended: %s", e)

    async def send_need_instructions(self, text: str, task_id: str) -> str:
        """发送文本回 Coordinator，等待回复"""
        reply_id = f"{task_id}_{uuid4().hex[:8]}"
        future = asyncio.get_event_loop().create_future()
        self._pending_replies[reply_id] = future
        await self._send({
            "type": "need_instructions",
            "payload": {
                "worker_id": self._worker_id,
                "task_id": task_id,
                "text": text,
                "reply_id": reply_id,
            },
        })
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            return ""  # 超时返回空，agent 按原逻辑退出
```

### 3. `src/a2a/worker/agent_adapter.py` — 构造 callback

在 `AgentAdapter.execute()` 中，构造 `_text_cb` 闭包，通过 WS client 发送请求。

```python
async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
    task = context.current_task
    task_id = task.id if task else ""

    agent = self._build_agent()
    self._current_agent = agent
    agent.add_user_message(query)

    # text_response_callback
    async def _text_cb(text: str) -> str | None:
        if self._ws_client and task_id:
            return await self._ws_client.send_need_instructions(text, task_id)
        return None

    final_text = await agent.run(
        step_callback=_step_handler,
        text_response_callback=_text_cb,
    )
```

需要 `AgentAdapter` 持有 `_ws_client` 引用。在 `a2a_server.py` 创建 `AgentAdapter` 时传入：

```python
adapter = AgentAdapter(
    model=model,
    provider=provider,
    ...
    ws_client=coordinator_client,  # 新增参数
)
```

### 4. `src/a2a/coordinator/server.py` — 处理 need_instructions

在 `_handle_worker_message()` 中，新增 `need_instructions` 消息类型。

```python
elif msg_type == "need_instructions":
    worker_id = payload["worker_id"]
    text = payload["text"]
    reply_id = payload["reply_id"]

    # 通过 RouterAgent 单轮推理生成新指令
    instructions = await self._router.generate_instructions(
        worker_id=worker_id,
        worker_text=text,
    )

    # 回复 Worker
    ws = self._worker_ws.get(worker_id)
    if ws:
        await ws.send_json({
            "type": "new_instructions",
            "payload": {"reply_id": reply_id, "text": instructions},
        })
```

### 5. `src/a2a/coordinator/router.py` — 单轮推理

新增 `generate_instructions()` 方法。使用 RouterAgent 的 system prompt + 环境快照 + Worker 文本，做一次 LLM 调用。

```python
async def generate_instructions(self, worker_id: str, worker_text: str) -> str:
    # 1. 取环境快照（如果有 query_sar_state 工具）
    env_snapshot = ""
    for tool in self._extra_tools:
        if tool.name == "query_sar_state":
            result = await tool.execute()
            if result.success:
                env_snapshot = result.content
            break

    # 2. 取 Worker 信息
    agent_info = self._registry.get(worker_id)

    # 3. 构造 prompt
    prompt = f"""## Current Environment State
{env_snapshot}

## Worker Report
Worker {worker_id} reports: {worker_text}

## Task
Based on the current state and the worker's report, give {worker_id} the next concrete instruction.
The instruction must be a complete execution chain (e.g., NavigateTo → GetSupply → UseSupply),
NOT an exploration task. Be specific and actionable."""

    # 4. 单次 LLM 调用
    llm_client = self._build_llm_client()
    response = await llm_client.generate(
        messages=[
            Message(role="system", content=self.agentic_prompt),
            Message(role="user", content=prompt),
        ]
    )
    return response.content
```

`_build_llm_client()` 从 `_build_agent()` 中提取出来，独立为方法。

## 风险与边界

| 风险 | 缓解 |
|------|------|
| **无限循环**: Worker 反复返回文本，Coordinator 反复派活，永不结束 | Agent 的 `max_steps` 硬限制；callback 返回空则退出；30s WS 超时兜底 |
| **Coordinator LLM 调用耗时**: 单轮推理 ~2-5s，Worker 等待期间占用 WS | 30s 超时足够，超时后 Worker 自动退出（`wait_for` TimeoutError → 返回空） |
| **消息历史膨胀**: 每轮 report-back 追加 user message，token 快速累积 | Agent 已有 summarization 机制（`token_limit=80000`），超出自动摘要 |
| **并发 WS 消息**: 多个 Worker 同时发 need_instructions，reply_id 冲突 | `reply_id = task_id + uuid8`，天然唯一 |
| **RouterAgent 正在 collect_results**: 此时 LLM 调用是否冲突？ | 不同 LLM client 实例，不共享状态，不冲突。WebSocket handler 和 agentic loop 在同一个事件循环中，asyncio 自然交替执行 |

## 改动文件清单

| # | 文件 | 改动量 | 说明 |
|---|------|--------|------|
| 1 | `src/Agent/worker_agent/agent.py` | +8 行 | `run()` 新增 `text_response_callback` 参数 + `if not response.tool_calls` 分支 |
| 2 | `src/a2a/worker/coordinator_client.py` | +40 行 | `_receive_loop()` + `send_need_instructions()` + `_pending_replies` |
| 3 | `src/a2a/worker/agent_adapter.py` | +10 行 | `_text_cb` 闭包 + `_ws_client` 属性 |
| 4 | `src/a2a/worker/a2a_server.py` | +3 行 | `create_worker_a2a_server()` 传递 `ws_client` |
| 5 | `src/a2a/coordinator/server.py` | +12 行 | `_handle_worker_message` 新增 `need_instructions` |
| 6 | `src/a2a/coordinator/router.py` | +35 行 | `generate_instructions()` + `_build_llm_client()` |

**总计约 +110 行代码**。
