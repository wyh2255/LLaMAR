---
日期: 2026-07-04
文档类型: 技术设计文档
文档概述: 用 A2A 标准 TASK_STATE_INPUT_REQUIRED 状态重构 Worker↔Coordinator 求助机制，替代 [HELP] artifact + future_registry hack
---

# 设计：用 A2A input-required 重构 Worker↔Coordinator 求助

## 1. 目标

用 A2A 标准的 `TASK_STATE_INPUT_REQUIRED` 状态替代当前的 `[HELP]` artifact + `future_registry` hack，实现 Worker 暂停求助、Coordinator 回复恢复的标准流程。

## 2. 当前问题

当前实现（commit 6578283..HEAD）完全绕过 A2A 任务状态机：
- Worker 的 `AskCoordinatorTool` 发送 `[HELP]` 前缀 artifact 到 Coordinator 的 push-callback
- Worker 阻塞等待 `future_registry` 中的 asyncio.Future（120s 超时）
- Coordinator 的 `RespondWorkerTool` 通过 HTTP POST 回复到 Worker 的 `/a2a/push-callback`
- Worker 的 push-callback 端点 resolve future，唤醒工具

问题：
1. `[HELP]` 是纯文本约定，非标准 A2A 语义
2. 绕过 A2A task 状态机，coordinator 无法通过 `GetTask()` 知道 worker 卡住
3. 自己手写 future/回调，A2A 的 `blocking: true` 已提供等待-恢复语义
4. Worker push-callback 端点是额外攻击面

## 3. 架构总览

```
Worker Agent                          Coordinator
─────────────                         ────────────
ask_coordinator("目标在哪？")
    │ 抛出 NeedInputError
    ▼
Agent.run() 捕获 → RunResult(need_input=True)
    │
    ▼
submit() 保存快照 ctx.save_snapshot(task_id, agent.messages)
    │
    ▼
execute() 调用 updater.requires_input(question)  ──→  INPUT_REQUIRED 状态
execute() 返回，事件队列关闭                            通过 streaming/push 到达 Coordinator
                                                         │
                                                         ▼
                                                    event_store 记录 help_request
                                                    Router Agent 看到求助
                                                    调用 respond_worker(task_id, reply)
                                                         │
                                                    A2A client.send_message(task_id=相同, text=reply)
                                                         │
execute() 再次被调用 ←─────────────────────────────────←┘
    │
    ▼
load_snapshot(task_id) → 恢复完整 messages
追加 tool_result（coordinator 的回复）
submit() 用恢复的 messages 继续 Agent.run()
    │
    ▼
Agent 从 ask_coordinator 之后的步骤继续执行
```

## 4. 组件变更

### 4.1 新增 NeedInputError 异常

`src/a2a/worker/need_input.py`:

```python
class NeedInputError(Exception):
    """Worker agent 需要向 Coordinator 请求输入时抛出。"""
    def __init__(self, question: str):
        self.question = question
```

### 4.2 ContextManager — 快照存储

`src/Agent/worker_agent/context.py` 的 `ContextManager` 基类加：

```python
self._task_snapshots: dict[str, list[Message]] = {}

def save_snapshot(self, task_id: str, messages: list[Message]) -> None:
    self._task_snapshots[task_id] = list(messages)

def load_snapshot(self, task_id: str) -> list[Message] | None:
    return self._task_snapshots.pop(task_id, None)
```

`load_snapshot` 用 `pop` 取出后删除——恢复是一次性的。

### 4.3 AskCoordinatorTool — 重写

`src/a2a/worker/tools/ask_coordinator.py`:

```python
class AskCoordinatorTool(Tool):
    @property
    def name(self): return "ask_coordinator"
    @property
    def parameters(self):
        return {"properties": {"question": {"type": "string"}}, "required": ["question"]}
    async def execute(self, question: str) -> ToolResult:
        raise NeedInputError(question)
```

不再需要 `coordinator_callback_url`、`task_id`、HTTP POST、future。纯信号。

### 4.4 RunResult — 加 need_input 字段

`src/Agent/worker_agent/types.py`（或 RunResult 定义处）:

```python
@dataclass
class RunResult:
    success: bool
    content: str = ""
    need_input: bool = False  # 新增
```

### 4.5 Agent.run() — 捕获 NeedInputError

在工具执行循环的 try-except 中加：

```python
try:
    result = await tool.execute(**arguments)
except NeedInputError as e:
    return RunResult(success=False, content=e.question, need_input=True)
```

此时 `self.messages` 包含到 ask_coordinator tool_call 为止的完整历史。

### 4.6 AgentController.submit() — 快照保存 + 恢复支持

新增两个参数：

```python
async def submit(
    self, context_id, query, sink=None, *,
    task_id: str | None = None,
    initial_messages: list[Message] | None = None,
    ...
) -> RunResult:
```

**保存路径**（暂停时）：
```python
result = await agent.run(...)
if result.need_input and task_id:
    ctx.save_snapshot(task_id, agent.messages)
return result
```

**恢复路径**（coordinator 回复后）：
```python
if initial_messages:
    agent.messages = list(initial_messages)
    last = agent.messages[-1]
    if last.role == "assistant" and last.tool_calls:
        # 补 tool_result，内容是 coordinator 的回复
        agent.messages.append(Message(
            role="tool",
            content=query,
            tool_call_id=last.tool_calls[-1].id,
        ))
    else:
        agent.add_user_message(query)
else:
    agent.add_user_message(query)
```

### 4.7 AgentAdapter.execute() — 暂停/恢复调度

```python
async def execute(self, context, event_queue):
    task = context.current_task
    task_id = task.id
    context_id = task.context_id
    updater = TaskUpdater(event_queue, task_id, context_id)

    # 恢复路径
    snapshot = ctx.load_snapshot(task_id)

    if snapshot:
        await updater.start_work(message=new_text_message("Resuming after help"))
        query = context.get_user_input()
        result = await self._controller.submit(
            context_id, query, sink,
            task_id=task_id,
            initial_messages=snapshot,
        )
    else:
        await updater.start_work(message=new_text_message("Starting work"))
        query = context.get_user_input()
        result = await self._controller.submit(
            context_id, query, sink,
            task_id=task_id,
        )

    if result.need_input:
        await updater.requires_input(message=new_text_message(result.content))
        return

    await updater.add_artifact(parts=[Part(text=result.content)], name="result")
    await updater.complete()
```

### 4.8 RespondWorkerTool — 用 A2A client 恢复

```python
class RespondWorkerTool(Tool):
    async def execute(self, task_id: str, response: str) -> ToolResult:
        node = self._store.get_node(task_id)
        if node is None or not node.worker_id:
            return ToolResult(success=False, content=f"Task '{task_id}' not found")

        agent_info = self._registry.get(node.worker_id)

        client = create_client(endpoint=agent_info.endpoint)
        message = Message(role=ROLE_USER, parts=[Part(text=response)], task_id=task_id)
        request = SendMessageRequest(message=message)

        async for sr in client.send_message(request):
            break  # 确认 worker 恢复

        return ToolResult(success=True, content=f"Response sent to {task_id}")
```

### 4.9 Coordinator push-callback — INPUT_REQUIRED 检测

`src/a2a/coordinator/server.py` 的 push-callback handler 加：

```python
if su.status.state == TASK_STATE_INPUT_REQUIRED:
    question = ""
    if su.status.HasField("message"):
        question = " ".join(p.text for p in su.status.message.parts if p.text)
    event_store.append(task_id, "help_request", text=question)
```

## 5. 删除的组件

| 文件 | 操作 |
|------|------|
| `src/a2a/worker/future_registry.py` | 删除 |
| `src/a2a/worker/tools/ask_coordinator.py` | 重写 |
| `src/a2a/builtin_tools/respond_worker.py` | 重写 |
| Worker `/a2a/push-callback` 端点 | 删除 |
| Coordinator `[HELP]` 前缀检测 | 删除 |
| `coordinator_callback_url` 参数 | 从多处移除 |

## 6. 完整数据流

```
步骤  Worker                              Coordinator
────  ──────────────────────────          ──────────────────
 1    Agent.run() 循环执行中
 2    LLM 输出 ask_coordinator tool_call
 3    tool.execute() 抛 NeedInputError
 4    run() 捕获 → RunResult(need_input=True)
 5    submit() save_snapshot(task_id, messages)
 6    execute() updater.requires_input(question)
 7    execute() 返回，事件队列关闭
 8                                        收到 INPUT_REQUIRED
 9                                        event_store 记 help_request
10                                        Router Agent 看到求助
11                                        调用 respond_worker(task_id, reply)
12                                        A2A send_message(task_id, reply)
13    DefaultRequestHandler 收到消息
14    检测 task 状态 INPUT_REQUIRED（非 terminal）
15    再次调用 execute(context, event_queue)
16    load_snapshot(task_id) → 完整 messages
17    补 tool_result（coordinator 的 reply）
18    submit() 用恢复的 messages 继续
19    Agent.run() 从 ask_coordinator 之后继续
20    ... 最终 complete()
21    execute() updater.complete()
22                                        收到 COMPLETED
23                                        resolve_global_future(task_id, result)
24                                        Router Agent 拿到结果
```

## 7. 错误处理

| 场景 | 处理 |
|------|------|
| Coordinator 不回复 | Worker task 停在 INPUT_REQUIRED，push_task future 超时 |
| 恢复后再次 ask_coordinator | 正常——save_snapshot 覆盖旧快照 |
| snapshot 被多次 load | `pop` 语义，第二次返回 None |
| Worker 崩溃 | snapshot 在内存中丢失，task 停在 INPUT_REQUIRED |
| ask_coordinator 是 batch 中的一个 | run() 立即返回，其余 tool_call 不执行，恢复时补 result |

## 8. 测试策略

| 测试 | 覆盖点 |
|------|--------|
| `test_need_input_error` | NeedInputError 可创建、携带 question |
| `test_context_manager_snapshot` | save/load/pop 语义、跨 task_id 隔离 |
| `test_ask_coordinator_raises` | execute() 抛 NeedInputError |
| `test_agent_run_catches_need_input` | run() 捕获异常，返回 RunResult(need_input=True) |
| `test_submit_saves_snapshot` | need_input 时 save_snapshot 被调用 |
| `test_submit_restores_snapshot` | initial_messages 恢复 + tool_result 补齐 |
| `test_execute_requires_input` | need_input 时 updater.requires_input 调用 |
| `test_execute_resume_from_snapshot` | load_snapshot → 恢复路径 |
| `test_respond_worker_uses_a2a_client` | send_message(task_id, response) |
| `test_coordinator_detects_input_required` | push-callback INPUT_REQUIRED → help_request |
| `test_full_pause_resume_cycle` | 集成测试 |
