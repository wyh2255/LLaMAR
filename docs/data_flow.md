---
日期: 2026-07-03
文档类型: 技术文档
文档概述: A2A → Coordinator → Worker → Barrier 的完整数据流向，
  追踪 context_id / task_id / query 三要素在系统中的路径。
---

# 完整数据流向

## 1. 总览

```
┌─ 外部 Client ───────────────────────────────────────────────────────────┐
│  A2A SendMessage(task_id, context_id, query)                           │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │ HTTP/JSON-RPC
                          ▼
┌─ Coordinator ───────────┼───────────────────────────────────────────────┐
│  CoordinatorA2AServer   │                                              │
│    (port 8081)          │                                              │
│                          │                                              │
│  ┌──────────────────────▼──────────────────────────────────────────┐   │
│  │  CoordinatorAgentExecutor.execute(context, event_queue)         │   │
│  │                                                                 │   │
│  │  ① context.current_task → task_id, context_id, query           │   │
│  │  ② event_queue → TaskUpdater(流式更新 SSE)                     │   │
│  │  ③ TaskQueue.enqueue(DistributedTask(task_id, query))          │   │
│  │  ④ 构建 A2ACoordinatorSink(event_queue, task_id, context_id)   │   │
│  │     + TeeSink([A2ACoordinatorSink, CallbackSink(router_cb)])   │   │
│  │  ⑤ 构建 coordinator tools:                                      │   │
│  │     [DispatchTaskTool, CollectResultsTool, VerifyResultTool,    │   │
│  │      QueryTaskResultsTool, UpdatePlanTool, SARFinishTaskTool]   │   │
│  └──────────────────────────┬───────────────────────────────────────┘   │
│                              │                                           │
│                              ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  AgentController.submit(                                       │   │
│  │    context_id,     ──── 会话标识 → _get_session(cid) → Context│   │
│  │    query,           ──── 用户输入 → add_user_message(query)    │   │
│  │    sink,            ──── 输出通道 → agent.run(step_callback)    │   │
│  │    extra_tools,     ──── 运行时工具 → _merge_runtime_kwargs    │   │
│  │    system_prompt_override,                                     │   │
│  │  )                                                              │   │
│  │                                                                 │   │
│  │  ① _get_session_lock(context_id)  → 串行化同一会话              │   │
│  │  ② _get_session(context_id)  → 取/建 ContextManager            │   │
│  │  ③ agent_factory(extra_tools, system_prompt_override)          │   │
│  │     → build_router_agent → LLMClient + Agent                   │   │
│  │  ④ agent.attach_context(ctx)  → CoordinatorSARHooks            │   │
│  │  ⑤ agent.add_user_message(query)                               │   │
│  │  ⑥ agent.run(cancel_event, step_callback=sink.emit)            │   │
│  │     → ReAct 循环开始                                            │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │ 每个 step 发射:
                             │ sink.emit("llm_response", ...)
                             │ sink.emit("tool_start", ...)
                             │ sink.emit("tool_result", ...)
                             ▼
                    ┌───────────────────┐
                    │  A2ACoordinator   │  → TaskStatusUpdateEvent
                    │  Sink             │  → TaskLogger (NDJSON)
                    └───────────────────┘


┌─ RouterAgent ReAct 循环 ───────────────────────────────────────────────┐
│                                                                         │
│  while step < max_steps:                                                │
│    LLM.generate(messages, tools=[query_sar_state, dispatch_task, ...])  │
│    ↓                                                                    │
│    ① query_sar_state(barrier)                                          │
│       → barrier.get_env_snapshot()                                      │
│       → 返回 grid / agents / fires / persons                            │
│    ② dispatch_task(agent_id="Alice", prompt="search area A")           │
│       ──A2A HTTP──→ Worker A2A Server (Alice)                          │
│       → TaskStore.futures[subtask_id] = asyncio.Future()               │
│    ③ collect_results()                                                  │
│       → TaskStore 收集已完成的 subtask 结果                              │
│    ④ finish_task() → agent._task_complete = True                       │
│  ↓                                                                      │
│  每步 sink.emit → A2ACoordinatorSink → EventQueue → SSE to client      │
└──────────────────────────────────────────────────────────────────────────┘


┌─ Worker ───────────────────────────────────────────────────────────────┐
│                                                                         │
│  WorkerA2AServer (port 8191+)  ←── dispatch_task 的 A2A SendMessage    │
│    │                                                                     │
│    ▼                                                                     │
│  AgentAdapter.execute(context, event_queue)                            │
│    │                                                                     │
│    ① task_id = context.task_id  (coordinator 分配的 subtask id)        │
│    ② context_id = context.context_id  (原始 mission context_id)        │
│    ③ query = context.get_user_input()  ("search area A")               │
│    ④ A2AWorkerSink(event_queue, task_id, context_id)                   │
│       + TeeSink([A2AWorkerSink, CallbackSink(step_callback)])           │
│                                                                         │
│    ▼                                                                     │
│  AgentController.submit(context_id, query, sink)                       │
│    (extra_tools / system_prompt_override 为空)                           │
│    │                                                                     │
│    ① agent_factory() → build_agent → LLMClient + Agent                 │
│    ② agent.attach_context(ctx) → WorkerSARHooks                         │
│    ③ agent.add_user_message(query)                                      │
│    ④ agent.run(step_callback=sink.emit) → ReAct 循环开始                │
│                                                                         │
└──────────────────────────────────────────────────────────────────────────┘


┌─ Worker Agent ReAct 循环 ─────────────────────────────────────────────┐
│                                                                         │
│  while step < max_steps:                                                │
│    LLM.generate(messages, tools=[NavigateTo, Move, Explore, ...])       │
│    ↓                                                                    │
│    ① NavigateTo(x=3, y=5)                                              │
│       → barrier.submit_action(agent_idx, "NavigateTo(3,5)")            │
│         ┌──────────────────────────────────────────────────────┐        │
│         │  SARBarrier.submit_action(agent_idx, action)         │        │
│         │                                                     │        │
│         │  ① _action_queue[agent_idx] = action                │        │
│         │  ② 等待所有 N 个 agent 提交 action                   │        │
│         │     或超时 (60s) → 自动填充 NoOp                     │        │
│         │  ③ asyncio.to_thread(_execute_step)                 │        │
│         │     ├─ env.step(actions) → observations             │        │
│         │     └─ _obs_events[agent_idx].set()                 │        │
│         │  ④ 返回 {observation, agent_name, step, finished}   │        │
│         └──────────────────────────────────────────────────────┘        │
│    ↓                                                                    │
│    observation → next LLM call                                          │
│    ↓                                                                    │
│    ② no_op("[MISSION COMPLETE]")                                        │
│       → barrier.submit_action(...)                                      │
│       → agent._task_complete = True                                    │
│  ↓                                                                      │
│  每步 sink.emit → A2AWorkerSink → TaskStatusUpdateEvent → SSE           │
│  step_callback("llm_response", ...) → ExperimentLogger → token_usage    │
│  step_callback("tool_result", ...)  → ExperimentLogger → interactions   │
│                                                                         │
└──────────────────────────────────────────────────────────────────────────┘


┌─ 结果回溯 ──────────────────────────────────────────────────────────────┐
│                                                                         │
│  Worker Agent → RunResult(content, success, steps_used)                 │
│    → AgentController._normalize() → RunResult                           │
│    → AgentAdapter.execute() → updater.add_artifact() + updater.complete()│
│    → A2A JSON-RPC response → Coordinator (dispatch_task 的 Future)     │
│                                                                         │
│  Coordinator TaskStore.futures[subtask_id] = result_text                │
│  → collect_results() 读取 → RouterAgent 下次 LLM 调用                   │
│  → 所有 subtask 完成 → RouterAgent finish_task()                        │
│  → CoordinatorAgentExecutor._execute_agentic()                          │
│    → updater.add_artifact() + updater.complete()                        │
│    → A2A JSON-RPC response → 外部 Client                                │
│                                                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. context_id / task_id / query 对照表

| 阶段 | context_id | task_id | query |
|------|-----------|---------|-------|
| 外部 Client → Coordinator | 上游任务传入 | 客户端分配 | "Extinguish all fires" |
| CoordinatorAgentExecutor | `task.context_id` | `task.id` | `context.get_user_input()` |
| AgentController.submit | `context_id` | — | `query` |
| _get_session(cid) | dict key → ContextManager | — | — |
| RouterAgent ReAct | — | — | add_user_message |
| dispatch_task → Worker | 保持不变 | 新 subtask id | "search area A" |
| AgentAdapter.execute | 原始 mission cid | subtask id | "search area A" |
| Worker Agent ReAct | — | — | add_user_message |
| barrier.submit_action | — | — | action string |

## 3. 关键设计点

1. **context_id 不变**：从入口到所有 worker 子任务，context_id 始终是原始 mission 标识，用于 `_get_session()` 查找 `ContextManager`
2. **task_id 分层**：coordinator 级 task_id 由外部客户端分配，worker 级 subtask id 由 `DispatchTaskTool` 分配
3. **TaskUpdater 绑定 (task_id, context_id)**：每个 executor 创建 `TaskUpdater(event_queue, task_id, context_id)`，确保 SSE 事件能关联到正确的 A2A 任务
4. **A2A Sink 绑定 (task_id, context_id)**：`A2ACoordinatorSink(event_queue, task_id, context_id)`，确保 step_callback 发射的 `llm_response`/`tool_start`/`tool_result` 事件能路由到正确的任务
5. **EventSink 单向流**：`agent.run(step_callback=sink.emit)` → `sink.emit(type_, **data)` → `A2A*Sink` → `TaskStatusUpdateEvent` → `EventQueue` → SSE，不反向影响 Agent 执行
