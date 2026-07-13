---
日期: 2026-07-12
文档类型: 技术文档
文档概述: A2A → Coordinator → Worker → Barrier 的完整数据流向，
   追踪 context_id / task_id / query 三要素在系统中的路径。
   包含完整的系统事件类型注册表、生产者→消费者流图及交叉引用。
---

# 完整数据流向

## 1. 总览

```
┌─ 外部 Client ───────────────────────────────────────────────────────────┐
│  A2A SendMessage(task_id, context_id, query)                           │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │ HTTP/JSON-RPC
                          ▼
┌─ Coordinator (port 8081) ───────────────────────────────────────────────┐
│  DefaultRequestHandler.on_message_send()                                │
│    └→ CoordinatorAgentExecutor.execute(context, event_queue)            │
│                                                                         │
│  ① context.current_task → task_id, context_id, query                   │
│  ② event_queue → TaskUpdater(流式更新 SSE)                             │
│  ③ TaskQueue.enqueue(DistributedTask(task_id, query))                  │
│  ④ 构建 A2ACoordinatorSink(event_queue, task_id, context_id)           │
│     + TeeSink([A2ACoordinatorSink, CallbackSink(router_cb)])           │
│  ⑤ 构建 coordinator tools:                                              │
│     [DispatchTaskTool, QueryTaskEventsTool, VerifyResultTool,           │
│      QueryTaskResultsTool, UpdatePlanTool, RespondWorkerTool,           │
│      SARFinishTaskTool, QueryWorkersTool]  ← QueryWorkersTool 始终可用 │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  _execute_agentic() → orchestrator loop:                        │   │
│  │    RouterAgent ReAct 或 DAG 模式                                 │   │
│  │                                                                  │   │
│  │  AgentController.submit(                                        │   │
│  │    context_id,     ──── 会话标识 → _get_session(cid)            │   │
│  │    query,           ──── 用户输入 → add_user_message(query)      │   │
│  │    sink,            ──── 输出通道 → agent.run(step_callback)     │   │
│  │    extra_tools,     ──── 运行时工具 → factory_kwargs → 构建器   │   │
│  │    system_prompt_override, ──── 可选覆写 system prompt          │   │
│  │    cancel_event,    ──── 异步取消信号                            │   │
│  │    task_id,         ──── 用于 snapshot 存储                      │   │
│  │    initial_messages,─── 快照恢复时的消息历史                     │   │
│  │  )                                                               │   │
│  │                                                                  │   │
│  │  ① _get_session_lock(context_id) → 串行化同一会话               │   │
│  │  ② _get_session(context_id) → 取/建 ContextManager              │   │
│  │  ③ agent_factory → build_router_agent → LLMClient + Agent      │   │
│  │  ④ agent.attach_context(ctx) → CoordinatorSARHooks              │   │
│  │  ⑤ agent.add_user_message(query)                                │   │
│  │  ⑥ agent.run(cancel_event, step_callback=sink.emit)             │   │
│  │     → ReAct 循环开始                                             │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  sink.emit → A2ACoordinatorSink                                         │
│    → TaskStatusUpdateEvent → TaskLogger (NDJSON)                        │
│    → router_cb() → ExperimentLogger → token_usage.csv / interactions    │
│                                                                         │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
                             ▼
┌─ RouterAgent ReAct 循环 ───────────────────────────────────────────────┐
│                                                                         │
│  while step < max_steps:                                                │
│    LLM.generate(messages, tools=base_tools + sar_tools)                 │
│    ├─ base_tools: dispatch_task, query_task_events, respond_worker,     │
│    │  finish_task, query_workers (始终可用)                             │
│    ├─ sar_tools (注入自 sar_orch/coordinator.py):                       │
│    │  query_sar_state (oracle 模式) / query_semantic_map,              │
│    │  query_team_status (semantic 模式)                                 │
│    ↓                                                                    │
│    ① query_sar_state(barrier)                                          │
│       → barrier.get_env_snapshot()                                      │
│       → 返回 grid / agents / fires / persons                            │
│    ② dispatch_task(agent_id, prompt)  ←── 非阻塞异步派发               │
│       → Router.send_task_async()                                       │
│         → A2A send_message(return_immediately=True,                     │
│            push_notification_config={url:"/a2a/push-callback"})         │
│         → TaskStore.register_future(tid) = asyncio.Future             │
│         → 立即返回 Worker 的 Task(WORKING), 不阻塞                      │
│    ③ query_task_events([task_ids], timeout=5.0)                        │
│       → 读取 EventStore 中该任务已有的 push callback 事件               │
│       → 返回 RUNNING / COMPLETED / FAILED / INPUT_REQUIRED 等状态       │
│       → timeout 内出现 actionable 状态则提前返回                        │
│    ④ respond_worker(task_id, response)  ←── 回复 Worker 暂停求助       │
│       → A2A send_message(task_id) 恢复 Worker                           │
│    ⑤ finish_task(success, summary)                                     │
│       → ToolResult → agent._task_complete = True                        │
│  ↓                                                                      │
│  每步 sink.emit → A2ACoordinatorSink → EventQueue → SSE                │
└──────────────────────────────────────────────────────────────────────────┘

           dispatch_task → A2A SendMessage (非阻塞)
           return_immediately + TaskPushNotificationConfig
                             │
                             ▼
┌─ Worker A2A Server (port 8191+) ─────────────────────────────────────────┐
│  DefaultRequestHandler.on_message_send()                                 │
│    └→ AgentAdapter.execute(context, event_queue)                         │
│                                                                          │
│  ① task_id = context.task_id  (coordinator 分配的 subtask id)           │
│  ② context_id = context.context_id  (原始 mission context_id)           │
│  ③ query = context.get_user_input()  ("search area A")                  │
│  ④ A2AWorkerSink(event_queue, task_id, context_id)                      │
│     + TeeSink([A2AWorkerSink, CallbackSink(step_callback)])             │
│                                                                          │
│  ⑤ 检查 snapshot: ctx.load_snapshot(task_id)                             │
│     ├─ 有快照 → "Resuming after help" + initial_messages=snapshot       │
│     └─ 无快照 → "Starting work" + 注入 AskCoordinatorTool               │
│                                                                          │
│  AgentController.submit(context_id, query, sink,                         │
│    task_id=task_id, initial_messages=snapshot_or_None)                   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  AgentController.submit()                                       │   │
│  │                                                                  │   │
│  │  ① _get_session(cid) → WorkerContextManager                     │   │
│  │  ② agent_factory → build_agent → LLMClient + Agent             │   │
│  │  ③ agent.attach_context(ctx) → WorkerSARHooks                    │   │
│  │  ④ 有 initial_messages:                                          │   │
│  │     agent.messages = list(initial_messages)  ← 恢复消息历史      │   │
│  │     if last.message 有 tool_calls:                               │   │
│  │       agent.messages.append(tool_result)  ← 追加 coordinator 回复│   │
│  │     否则 agent.add_user_message(query)                           │   │
│  │    无 initial_messages:                                          │   │
│  │     agent.add_user_message(query)                                │   │
│  │  ⑤ agent.run(step_callback=sink.emit) → ReAct 循环开始           │   │
│  │  ⑥ 若 result.need_input and task_id:                             │   │
│  │     ctx.save_snapshot(task_id, agent.messages)  ← 拍照保持进度    │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
                             ▼
┌─ Worker Agent ReAct 循环 ───────────────────────────────────────────────┐
│                                                                          │
│  while step < max_steps:                                                 │
│    hooks.pre_llm → ctx.assemble(step_callback sink)                      │
│    LLM.generate(messages, tools=[NavigateTo, Move, Explore,              │
│      GetSupply, StoreSupply, UseSupply, CarryPerson, DropOffPerson,     │
│      GetAgentState, ClearInventory, ReportObservation,                  │
│      QuerySharedMemory, NoOp, FinishTask, AskCoordinator])               │
│    hooks.post_llm → ctx.prune_history()                                  │
│    ↓                                                                     │
│    for tool_call:                                                        │
│      hooks.pre_tool → sink.emit("tool_start", ...)                       │
│      result = await tool.execute(**arguments)                            │
│      │                                                                    │
│      ├─ SAR tool (NavigateTo, Move, ...):                                │
│      │  → barrier.submit_action(agent_idx, action_str)                   │
│      │    ┌──────────────────────────────────────────────────────┐      │
│      │    │  SARBarrier.submit_action(agent_idx, action)         │      │
│      │    │                                                      │      │
│      │    │  ① _action_queue[agent_idx] = action                 │      │
│      │    │  ② 等待所有 N 个 agent 提交 action                   │      │
│      │    │     或超时 (60s) → 自动填充 NoOp                     │      │
│      │    │  ③ asyncio.to_thread(_execute_step)                  │      │
│      │    │     ├─ env.step(actions) → observations              │      │
│      │    │     └─ _obs_events[agent_idx].set()                  │      │
│      │    │  ④ 返回 {observation, step, finished}                │      │
│      │    └──────────────────────────────────────────────────────┘      │
│      │                                                                  │
│      ├─ AskCoordinator (问 Coordinator 求助):                           │
│      │  raises NeedInputError(question)                                 │
│      │  → Agent.run() catches → RunResult(need_input=True)             │
│      │  → AgentController 保存 snapshot + 返回                         │
│      │  → AgentAdapter: updater.requires_input(message=question)       │
│      │    → A2A TASK_STATE_INPUT_REQUIRED                               │
│      │  → return 让出控制权, Worker 等 Coordinator 回复                │
│      │                                                                  │
│      ├─ FinishTask:                                                     │
│      │  → ToolResult → agent._task_complete = True                     │
│      │                                                                  │
│      └─ NoOp:                                                          │
│         → [MISSION COMPLETE] / [Step N] Mission in progress             │
│                                                                          │
│      hooks.post_tool → sink.emit("tool_result", ...)                    │
│      hooks.post_tool → ctx.observe(tool_name, result, success)          │
│  ↓                                                                       │
│  sink.emit → A2AWorkerSink → TaskStatusUpdateEvent → SSE                │
│  step_callback → ExperimentLogger → token_usage.csv / interactions      │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

          Result return path
               │
               ▼
┌─ 结果回溯 ──────────────────────────────────────────────────────────────┐
│                                                                          │
│  Worker Agent → RunResult(content, success, steps_used, need_input)      │
│    → AgentController._normalize()                                       │
│    → AgentAdapter.execute():                                             │
│      ├─ result.need_input → updater.requires_input() → return           │
│      └─ else → updater.add_artifact() + updater.complete()              │
│                                                                          │
│  Worker A2A Server 自动发 PushNotification:                               │
│    → EventConsumer._update_task_state()                                  │
│    → push_sender.send_notification()                                     │
│    → HTTP POST → http://coordinator:8080/a2a/push-callback              │
│      ├─ {artifact_update: {task_id, artifact: {parts: [...]}}}          │
│      └─ {status_update: {task_id, status: {state: COMPLETED}}}          │
│                                                                          │
│  Coordinator push-callback handler:                                       │
│    ├─ artifact_update: _push_artifact_cache[tid].extend(texts)           │
│    │     event_store.append(tid, "artifact_update", text=combined)       │
│    ├─ status_update (COMPLETED/FAILED):                                  │
│    │     event_store.append(tid, "status_update", state)                 │
│    │     (保留 _push_artifact_cache / resolve_global_future 供旧代码使用) │
│    └─ status_update (INPUT_REQUIRED):                                    │
│          event_store.append(tid, "help_request", text=question)          │
│          (非 terminal 状态，不 resolve Future)                           │
│                                                                          │
│  query_task_events() → RouterAgent 下次 LLM 调用                         │
│  → 所有 subtask 完成 → RouterAgent finish_task()                         │
│  → CoordinatorAgentExecutor._execute_agentic()                           │
│    → updater.add_artifact() + updater.complete()                         │
│    → A2A JSON-RPC response → 外部 Client                                 │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

┌─ INPUT_REQUIRED 暂停/恢复路径 ──────────────────────────────────────────┐
│                                                                          │
│  暂停方向 (Worker → Coordinator):                                        │
│                                                                          │
│  Worker Agent 调用 ask_coordinator(question)                             │
│    → AskCoordinatorTool.execute()                                        │
│      → raises NeedInputError(question)                                   │
│    → Agent.run() catches NeedInputError                                  │
│      → 返回 RunResult(need_input=True, content=question)                 │
│    → AgentController.submit():                                           │
│      → result.need_input and task_id                                     │
│      → ctx.save_snapshot(task_id, agent.messages)  ← deepcopy            │
│    → AgentAdapter.execute():                                             │
│      → updater.requires_input(message=question)                          │
│        → A2A TASK_STATE_INPUT_REQUIRED + question 文本                   │
│      → return (控制权交还 A2A 事件循环)                                  │
│    → Worker A2A Server 自动发 PushNotification:                          │
│      → HTTP POST → coordinator:/a2a/push-callback                        │
│        {statusUpdate: {taskId, status: {state: INPUT_REQUIRED,           │
│          message: {parts: [{text: question}]}}}}                          │
│    → Coordinator push-callback handler:                                   │
│      → event_store.append(tid, "help_request", text=question)            │
│                                                                          │
│  恢复方向 (Coordinator → Worker):                                        │
│                                                                          │
│  RouterAgent 下次 LLM 调用时通过 ContextManager 看到 help_request 事件    │
│    → LLM 决定调用 respond_worker(task_id="dispatch-1", response="...")   │
│    → RespondWorkerTool.execute():                                        │
│      → 查 TaskStore.get_node(dispatch_id) → 获 worker_id                │
│      → 通过 _store._dispatch_to_worker 映射 dispatch_id → worker_task_id│
│      → 查 AgentRegistry.get(worker_id) → 获 endpoint                    │
│      → A2A create_client(endpoint)                                      │
│      → send_message(Message(role=ROLE_USER, parts=[Part(text=response)], │
│           task_id=worker_task_id))                                       │
│      → 标准 A2A SendMessage → Worker A2A Server                          │
│    → AgentAdapter.execute() 再次被调用:                                   │
│      → ctx.load_snapshot(task_id) → 取回之前保存的 messages              │
│      → 有 snapshot → "Resuming after help"                               │
│      → controller.submit(cid, query, sink, task_id, initial_messages)    │
│    → AgentController.submit():                                           │
│      → agent.messages = list(initial_messages)  ← 恢复完整消息历史       │
│      → 判断 last message 是否有 tool_calls:                              │
│        → 有: 追加 tool_result Message (模拟 tool 返回)                   │
│          agent.messages.append(Message(role="tool",                      │
│            content=response, tool_call_id=..., name=...))                │
│        → 无: agent.add_user_message(query)                                │
│      → agent.run() 继续 ReAct 循环                                       │
│      → Agent 看到 Coordinator 的回复作为 AskCoordinator 的 tool result   │
│        (role=tool, content=response, tool_call_id=原 ask 的 call_id)     │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

┌─ Token 记录路径 ─────────────────────────────────────────────────────────┐
│                                                                          │
│  LLM.generate() 返回 LLMResponse.usage                                  │
│    ├── prompt_tokens, completion_tokens, total_tokens                   │
│    ├── cache_hit_tokens (DeepSeek: prompt_cache_hit_tokens               │
│    │                     Anthropic: cache_read_input_tokens)             │
│    └── cache_miss_tokens (DeepSeek: prompt_cache_miss_tokens             │
│                           Anthropic: input+cache_creation)               │
│                                                                          │
│  Agent.run() 累加到 Agent 实例:                                          │
│    self.api_[prompt/completion/total/cache_hit/cache_miss]_tokens        │
│    self.cumulative_* += ... (跨步累积)                                   │
│    _create_summary() 也累加 summarization tokens (+ cache)              │
│                                                                          │
│  sink.emit("llm_response", usage=response.usage)                        │
│    → A2ACoordinatorSink / A2AWorkerSink: (写入 [DATA], 略过 usage)       │
│    → CallbackSink(step_callback):                                        │
│                                                                          │
│  [Worker 侧] step_callback = worker.py:_step_callback()                  │
│    → exp_logger.log_token_usage(step, agent_name,                        │
│        prompt_tokens, completion_tokens, total_tokens,                   │
│        cache_hit_tokens, cache_miss_tokens)                              │
│    → token_usage.csv: Step, Agent, PromptTokens,                         │
│      CompletionTokens, TotalTokens,                                      │
│      CacheHitTokens, CacheMissTokens,                                    │
│      RunID, LLMLatencyMs, Model, PromptVersion                           │
│    → summary.csv 增量写入（每 agent 5 列）:                              │
│      AlicePromptTokens, AliceCompletionTokens, AliceTotalTokens,         │
│      AliceCacheHitTokens, AliceCacheMissTokens                           │
│                                                                          │
│  [Coordinator 侧] router_cb = coordinator.py:_router_cb()               │
│    → exp_logger.log_token_usage(step, "Coordinator", ...)                │
│    → token_usage.csv                                                    │
│    → summary.csv 增量写入                                                │
│                                                                          │
│  Agent Interactions:                                                     │
│    step_callback("tool_start", ...)                                      │
│    + step_callback("tool_result", ...)                                   │
│    → exp_logger.log_agent_interaction(step, agent, tool,                 │
│        args, observation, llm_output)                                    │
│    → agent_interactions.csv                                              │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

┌─ 实验入口 poll 循环 (experiment.py) ─────────────────────────────────────┐
│                                                                          │
│  experiment.py:run_experiment() 创建:                                    │
│    SARBarrier(num_agents, scene, seed)  ← 内部创建 SAREnv               │
│    ExperimentLogger(log_dir)                                             │
│    SARCoordinator(barrier, agents, ...).start()                          │
│    SARWorker(agent_id, barrier, ...).start() × N                        │
│                                                                          │
│  并发运行的循环:                                                          │
│    ┌─ A2A 编排: CoordinatorAgentExecutor → RouterAgent ReAct             │
│    │  (独立 asyncio task, 决定"做什么")                                  │
│    ├─ Worker A2A Server: 接收 dispatch, AgentAdapter.execute             │
│    │  (每个 Worker 独立 asyncio task, 执行"怎么做")                      │
│    └─ experiment.py poll loop:                                           │
│       while not barrier.is_finished()                                     │
│         and barrier.get_metrics()["steps"] < max_steps:                 │
│         await asyncio.sleep(2.0)                                           │
│         metrics = barrier.get_metrics()                                   │
│         logger.log_trajectory(step, metrics)                              │
│         logger.flush_summary()  ← crash-safe 增量写入                    │
│       coordinator.stop()                                                 │
│       agent.stop()                                                       │
│       barrier.stop()  ← 设置 _stopped + 所有 event.set() 唤醒 Worker    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

┌─ Map UI SSE 端点 ────────────────────────────────────────────────────────┐
│                                                                          │
│  server.set_barrier(barrier) 注入 SARBarrier 引用                         │
│                                                                          │
│  GET /map/state (SSE):                                                   │
│    async def event_generator():                                          │
│      while True:                                                         │
│        if disconnected: break                                            │
│        snapshot = barrier.get_env_snapshot()                             │
│          → {step, finished, coverage, transport_rate,                    │
│             agents: N, fires: N, persons: N,                             │
│             snapshot: {grid: [[x,y,z], ...], agents, fires, persons,     │
│               reservoirs, deposits, flammables}}                         │
│        yield SSE data: json.dumps(snapshot, default=_serialize)          │
│        if current_step != last_step:                                     │
│          await asyncio.sleep(0.5)  ← 每 0.5s 轮询，仅 step 变化时推送    │
│                                                                          │
│  HTML 客户端 (/ui/map):                                                  │
│    EventSource("/map/state") → 接收 JSON → 渲染网格                      │
│    颜色: 背景白色, 火势 beige→orange→red, agent 粉色,                    │
│           reservoir 蓝色, deposit 黑色, person 紫色                      │
│    侧边栏: step, coverage, transport, inventory, fire/person 详情       │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. context_id / task_id / query 对照表

| 阶段 | context_id | task_id | query | 备注 |
|------|-----------|---------|-------|------|
| 外部 Client → Coordinator A2A | 上游传入/自动生成 | 客户端分配 v4 UUID | "Extinguish all fires..." | 原始任务入口 |
| CoordinatorAgentExecutor.execute | `task.context_id` | `task.id` | `context.get_user_input()` | protobuf 提取 |
| TaskQueue.enqueue | — | 同上 task_id | 原始 query | DistributedTask 生命周期 |
| _execute_agentic → TaskStore | 传入 cid | — | `original_request` | 编排状态容器 |
| AgentController.submit (coord) | **不变** | — (不传) | 原始 query | → ContextManager key |
| ContextManager (coord) | dict key | — | — | 跨 ReAct 循环复用 |
| RouterAgent ReAct loop | — | internal step | add_user_message(query) | LLM 循环 |
| DispatchTaskTool → Worker | **传入 context_id** | `dispatch-1` 自动生成 | subtask instruction | `send_task_async` |
| Router.send_task_async | 设 `message.context_id` | 同上 | prompt | A2A protobuf |
| WorkerA2AServer receive | **原始 mission cid** | subtask id | instruction | protobuf 解析 |
| AgentAdapter.execute | cid | subtask id | `get_user_input()` | TaskUpdater 绑定 |
| AgentController.submit (worker) | **不变** | task_id(用于snapshot) | instruction | → Worker CtxManager key |
| ContextManager (worker) | dict key | — | — | 跨 subtask 复用 |
| Worker Agent ReAct loop | — | — | add_user_message(query) | LLM 循环 |
| SAR tool execute | — | — | action string | `barrier.submit_action` |
| snapshot save | — | **task_id 作为 snapshot key** | — | `ctx.save_snapshot(tid, messages)` |
| snapshot resume | — | **task_id 匹配 snapshot key** | response text | `initial_messages=snapshot` |
| RespondWorkerTool | — | **worker 的 subtask id** | coordinator 回复 | A2A send_message |
| ExperimentLogger | — | step, agent | 仅 CSV 行标识 | 不追踪 ID |

## 3. 关键设计点

1. **context_id 全局不变**：从入口到所有 worker 子任务，context_id 始终是原始 mission 标识，用于 `_get_session()` 查找 `ContextManager`。Coordinator 和 Worker 各自的 `ContextManager` 通过此键跨多次 `submit()` 调用复用，实现跨子任务记忆

2. **task_id 分层**：coordinator 级 task_id 由外部客户端分配（用于 SSE 关联 + TaskLogger），worker 级 subtask id 由 `DispatchTaskTool` 自动生成（如 `dispatch-1`，用于 push callback、Future 匹配和 snapshot 存储）

3. **异步推送模式**：`DispatchTaskTool` 使用 `send_task_async()` + `return_immediately=True` + `TaskPushNotificationConfig`，Worker 运行期间通过 HTTP POST `/a2a/push-callback` 主动推送状态/结果到 `EventStore`。Coordinator 通过 `query_task_events()` 查询 EventStore 中的最新状态（含 `INPUT_REQUIRED`），而非阻塞等待 Future。支持并行派发多个任务且不阻塞 Coordinator LLM 循环

4. **TaskUpdater 绑定 (task_id, context_id)**：每个 executor 创建 `TaskUpdater(event_queue, task_id, context_id)`，确保 SSE 事件能关联到正确的 A2A 任务

5. **EventSink 单向流**：`agent.run(step_callback=sink.emit)` → `sink.emit(type_, **data)` → `A2A*Sink` → `TaskStatusUpdateEvent` → `EventQueue` → SSE。Agent 内核不依赖任何传输层。`TeeSink` 扇出到多个 sink（传输 + 外部 step_callback 日志），`CallbackSink` 包装旧式回调

6. **INPUT_REQUIRED 暂停/恢复**：Worker 端 `AskCoordinatorTool` 抛出 `NeedInputError`，`Agent.run()` 捕获后返回 `RunResult(need_input=True)`，`AgentController.submit()` 保存完整消息快照，`AgentAdapter` 调用 `updater.requires_input()` 设置 A2A 标准 INPUT_REQUIRED 状态。Coordinator 收到后，LLM 通过 `RespondWorkerTool` 用标准 A2A `send_message` 回复，Worker 从快照恢复消息历史并追加 tool_result，Agent 如同刚收到 tool 返回一样继续执行

7. **threading 同步原语**：`SARBarrier` 使用 `threading.Event` 和 `threading.Lock`（非 asyncio），因为 Worker 运行在不同线程的独立事件循环中。`asyncio.Event.set()` 使用 `loop.call_soon()`（非 `call_soon_threadsafe`），跨线程调用会导致 waiter 永远醒不来

8. **Step-based 循环 vs A2A 编排并行**：`experiment.py` 的 poll 循环直接检查 `barrier.is_finished()` 和 `get_metrics()["steps"]`，与 A2A 编排（CoordinatorAgentExecutor + RouterAgent）并行运行。A2A 编排决定"做什么"（dispatch → collect → plan），poll 循环检查"世界是否完成"。两者通过 barrier 共享状态

9. **消息快照深拷贝**：`ContextManager.save_snapshot()` 使用 `copy.deepcopy()` 保存完整 Pydantic Message 列表。恢复时 `AgentController.submit()` 直接设置 `agent.messages = list(initial_messages)`，并在 ask_coordinator 的最后一个 tool_call 后追加 tool result，Agent 感知不到暂停发生过

10. **Token 双路径记录**：Worker 端的 `step_callback`（`worker.py:_step_callback`）记录各 Agent 的 token 用量；Coordinator 端的 `router_cb`（`coordinator.py:_router_cb`）记录 Coordinator 自身的 token 用量。两者写入同一个 `token_usage.csv`，`summary.csv` 每步增量写入确保 crash-safe

11. **KV 缓存追踪**：LLM 客户端从 API 响应提取缓存命中/未命中 token 数（DeepSeek: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`；OpenAI: `prompt_tokens_details.cached_tokens`；Anthropic: `cache_read_input_tokens` / `cache_creation_input_tokens`）。通过 `TokenUsage.cache_hit_tokens` / `cache_miss_tokens` 透传至 `token_usage.csv`。对于 DeepSeek 和 OpenAI 保证 `cache_hit + cache_miss == prompt_tokens`；Anthropic 的 `input_tokens` 可能与 `cache_creation_input_tokens` 有重叠，等式不一定成立。缓存率 = `ΣCacheHitTokens / ΣPromptTokens`

## 4. 事件系统总览

### 4.1 事件类型分层注册表

事件系统分为九个层次，每层有独立的事件类型集。各层之间存在三种层级关系：**包含**（粗→细粒度）、**派生**（低级→高级语义）、**生命周期**（时序状态机）。

#### 第1层：A2A Protobuf TaskState 枚举

| 值 | 编号 | 含义 |
|----|------|------|
| `TASK_STATE_UNSPECIFIED` | 0 | 未知 |
| `TASK_STATE_SUBMITTED` | 1 | 已提交 |
| `TASK_STATE_WORKING` | 2 | 处理中 |
| `TASK_STATE_COMPLETED` | 3 | 完成（终态） |
| `TASK_STATE_FAILED` | 4 | 失败（终态） |
| `TASK_STATE_CANCELLED` | 5 | 取消（终态） |
| `TASK_STATE_INPUT_REQUIRED` | 6 | 等待输入（中断态） |
| `TASK_STATE_REJECTED` | 7 | 拒绝（终态） |
| `TASK_STATE_AUTH_REQUIRED` | 8 | 需认证 |

到 Python 项目级 `TaskStatus` (`src/a2a/shared/types.py`) 的映射：

```
Protobuf WORKING         → TaskStatus.RUNNING
Protobuf COMPLETED       → TaskStatus.COMPLETED
Protobuf FAILED          → TaskStatus.FAILED
Protobuf CANCELLED       → TaskStatus.CANCELLED
Protobuf INPUT_REQUIRED  → TaskStatus.RUNNING (暂停中)
其他                      → TaskStatus.PENDING
```

`TaskStatus` 合法转换：`PENDING → {RUNNING, CANCELLED}`，`RUNNING → {COMPLETED, FAILED, CANCELLED}`。

#### 第2层：A2A 框架事件 (Sink → EventQueue → SSE / TaskLogger)

| 生产者 | metadata.event_type | 触发条件 | 输出 |
|--------|-------------------|----------|------|
| A2ACoordinatorSink | `llm_thinking` | RouterAgent LLM 响应 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `dispatch` | Router 调用 dispatch_task | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `query_task_events` | Router 查询子任务 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `verify` | Router 验证子任务 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `replan` | Router 更新计划 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `tool_call` | Router 通用工具调用 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `task_complete` | Router 查询到子任务完成 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `help_request` | Router 查询到求助 | SSE + TaskLogger NDJSON |
| A2ACoordinatorSink | `task_status` | Router 查询到中间态 | SSE + TaskLogger NDJSON |
| A2AWorkerSink | `llm_response` ([DATA]) | Worker LLM 响应 | SSE [DATA] → EventStore |
| A2AWorkerSink | `tool_start` ([DATA]) | Worker 工具开始 | SSE [DATA] → EventStore |
| A2AWorkerSink | `tool_result` ([DATA]) | Worker 工具结果 | SSE [DATA] → EventStore |

#### 第3层：Agent 内核事件 (AgentLogger NDJSON)

| 生产者 | 事件 | 文件路径 | 关键字段 |
|--------|------|----------|----------|
| RouterAgent Logger | `llm_request` | `logs/agent/sar_coordinator/<tid>.ndjson` | messages[], tools[], step_index |
| RouterAgent Logger | `llm_response` | 同上 | content, thinking?, tool_calls?, usage |
| RouterAgent Logger | `tool_result` | 同上 | tool_name, arguments, success, result |
| WorkerAgent Logger | `llm_request` | `logs/agent/sar_worker/<wid>/<tid>.ndjson` | 同上 |
| WorkerAgent Logger | `llm_response` | 同上 | 同上 |
| WorkerAgent Logger | `tool_result` | 同上 | 同上 |

#### 第4层：SAR 实验层 (ExperimentLogger CSV / NDJSON)

| 事件类型 | 生产者 | 输出文件 | 说明 |
|----------|--------|----------|------|
| `dispatch_task` | Coordinator._router_cb | router_interactions.csv + events.ndjson | 派发子任务 |
| `respond_worker` | Coordinator._router_cb | router_interactions.csv | 回复 Worker 求助 |
| `query_sar_state` | Coordinator._router_cb | agent_interactions.csv | 查询环境状态 |
| `query_task_events` | Coordinator._router_cb | router_interactions.csv | 查询子任务状态 |
| `finish_task` | Coordinator._router_cb | router_interactions.csv | 标记完成 |
| `tool_result` | Worker._step_callback | agent_interactions.csv | 记录 SAR 工具执行结果 |
| `observation_ingested` | SemanticMapStore | semantic_map.jsonl | 观察数据已摄入语义地图 |

#### 第5层：EventStore 事件 (push-callback)

| 事件类型 | 触发条件 | 文件 | 最大记录数 |
|----------|----------|------|-----------|
| `task_created` | 任务派发 | `events_<tid>.ndjson` | 500/task |
| `status_update` | push-callback 收到状态变更 | 同上 | 500/task |
| `artifact_update` | Worker 产出最终文本 | 同上 | 500/task |
| `help_request` | Worker 发起 INPUT_REQUIRED | 同上 | 500/task |
| `observation_report` | push-callback 收到 [DATA] 观测 | 同上 | 500/task |

#### 第6层：TaskLogger 事件 (NDJSON, max 10MB)

| 事件 | source | 触发条件 |
|------|--------|----------|
| `meta` | TaskLogger | 任务初始化 |
| `raw_request` | executor | 收到 A2A 请求 |
| `task_start` | executor | 任务开始处理 |
| `agentic_start` | executor | 进入编排模式 |
| `task_error` | executor | 任务出错 |
| `task_final` | executor | 最终结果 |
| `done` | executor | 编排完成 |
| `plan` | router | DAG 计划生成 |
| `layer_start` | executor | DAG 层开始 |
| `task_complete` | worker | 子任务完成 |
| `replan` | router | Router 重新规划 |
| `verify` | router | 验证结果 |
| `llm_response` | router | Router LLM 调用 |
| `tool_result` | router | Router 工具执行 |
| `task_status` | router | Worker 状态更新 |
| `help_request` | router | Worker 需要输入 |
| `dispatch` | router | 任务派发 |
| `tool_call` | router | Router 工具调用 |

#### 第7层：WebSocket 内部协议消息

定义自 `src/a2a/shared/types.py:61-75`，用于 Coordinator ↔ Worker 的 WebSocket 通道。

| 常量 | type 字符串 | 方向 | 用途 |
|------|------------|------|------|
| `WS_REGISTER` | `register` | Worker → Coordinator | Worker 注册自身 endpoint |
| `WS_HEARTBEAT` | `heartbeat` | Worker → Coordinator | 周期性保活 |
| `WS_TASK_PROGRESS` | `task_progress` | Worker → Coordinator | 任务进度更新 |
| `WS_CANCEL_TASK` | `cancel_task` | Coordinator → Worker | 取消任务 |
| `WS_SHUTDOWN` | `shutdown` | Coordinator → Worker | 关闭指令 |
| `WS_RELAY_A2A` | `relay_a2a` | Coordinator → Worker | 中转 A2A 消息 |

#### 第8层：PlanNode 状态

定义自 `src/a2a/coordinator/task_store.py:14-36`，DAG 编排层。

| 字段 | 取值 | 设置者 |
|------|------|--------|
| `status` | `"pending"`, `"skipped"` | RouterAgent 通过 `update_plan` |
| `state` | `"pending"`, `"running"`, `"done"`, `"failed"`, `"verified"` | 系统 auto-write 通过 `set_state()` |

#### 第9层：实验 EndReason 枚举

| EndReason | 条件 | 含义 |
|-----------|------|------|
| `success` | barrier.is_finished() | 所有 SAR 目标完成 |
| `framework_error` | Coordinator/A2A 异常 | 基础设施故障 |
| `wall_clock_timeout` | 耗时 >= 3600s | 超 1 小时硬限制 |
| `max_steps_reached` | steps >= max_steps (默认 50) | 步数预算耗尽 |
| `coordinator_finished_early` | A2A task.done() 早于 barrier 完成 | 编排提前结束 |
| `stopped_before_success` | 其他情况 | 兜底 |

### 4.1.8 事件层级关系图

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ① 包含层级 (粗粒度 ← 细粒度)                                            │
│                                                                          │
│  StreamResponse (oneof)                                                  │
│    ├── TaskStatusUpdateEvent (状态变更)                                  │
│    │    └── metadata.event_type (sink 层语义标注)                        │
│    │         ├── "llm_thinking" / "dispatch" / "verify" / "replan"       │
│    │         ├── "tool_call" / "query_task_events"                        │
│    │         └── "task_complete" / "help_request" / "task_status"        │
│    ├── TaskArtifactUpdateEvent (产物变更)                                │
│    └── Message (直接消息)                                                │
│                                                                          │
│  TaskStatusUpdateEvent.text (oneof 字段)                                 │
│    ├── 普通文本 → TaskLogger / EventStore / ExperimentLogger            │
│    └── [DATA] JSON 块 (内含 "ev" 字段)                                  │
│         ├── "llm_response" (Worker LLM 输出)                             │
│         ├── "tool_start"   (Worker 工具调用开始)                         │
│         └── "tool_result"  (Worker 工具执行结果)                         │
│                                                                          │
│  ② 派生层级 (低级事件 → 语义提升)                                       │
│                                                                          │
│  第3层 Agent 内核         第2层 A2A 框架      第5层 EventStore         第4层 SAR 实验 / 语义地图        │
│  ┌──────────────┐        ┌────────────┐       ┌──────────────┐         ┌──────────────────────┐      │
│  │ tool_result  │ ────→  │[DATA] 块   │ ───→  │observation_  │ ────→   │ observation_ingested │      │
│  │ (Worker)     │        │ ev=tool_   │        │report        │         │ (SemanticMapStore)   │      │
│  │              │        │ result     │        │ (EventStore) │         └──────────────────────┘      │
│  └──────────────┘        └────────────┘        └──────────────┘                                      │
│                                                                                                      │
│  ③ 生命周期层级 (时序状态机)                                                                         │
│                                                                                                      │
│  TaskStatus:      PENDING ──→ RUNNING ──→ COMPLETED / FAILED / CANCELLED                             │
│                                                                                                      │
│  TaskLogger序列:   meta ──→ raw_request ──→ task_start ──→ agentic_start ──→ plan ──→               │
│                   layer_start ──→ (task_complete × N) ──→ done                                       │
│                                                                                                      │
│  Subtask:         assigned ──→ in_progress ──→ completed / failed / canceled                         │
│                                                                                                      │
│  ④ 扇出层级 (单一来源 → 多路持久化)                                                                 │
│                                                                                                      │
│  step_callback(type_, data)                                                                          │
│    └─ TeeSink ──┬── A2A*Sink ──→ EventQueue ──→ SSE / Push callback                                 │
│                  └── CallbackSink ──→ ExperimentLogger ──→ CSV                                        │
│  Agent.run() -> AgentLogger ──→ NDJSON (独立于 TeeSink)                                              │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 事件生产者→消费者流图

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Protobuf 层 (A2A protocol)                                             │
│  TaskState(9 states) → TaskStatusUpdateEvent / TaskArtifactUpdateEvent  │
│  ∈ StreamResponse  →  SSE 推送 / JSON-RPC 响应                         │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────── A2A 框架层 ──────────────────────────────────────────┐
│                                                                        │
│  ┌─ A2AWorkerSink ───────────┐   ┌─ A2ACoordinatorSink ───────────┐  │
│  │  Worker React step_callback│   │  Router ReAct step_callback    │  │
│  │                           │   │                                │  │
│  │  "llm_response" ── [DATA] │   │  "llm_thinking" ──────────┐    │  │
│  │  "tool_start"   ── [DATA] │   │  "dispatch"    ───────────┤    │  │
│  │  "tool_result"  ── [DATA] │   │  "query_task_events" ─────┤    │  │
│  │                           │   │  "verify"      ───────────┤    │  │
│  │            TaskStatus-    │   │  "replan"      ───────────┤    │  │
│  │            UpdateEvent    │   │  "tool_call"   ───────────┤    │  │
│  └───────────────────────────┘   │  "task_complete" ─────────┤    │  │
│                                  │  "help_request" ─────────┤    │  │
│                                  │  "task_status"  ─────────┘    │  │
│                                  └───────────────────────────────┘  │
│                                           │                         │
│                                           ▼                         │
│                                  ┌──────────────────┐              │
│                                  │  EventQueue      │              │
│                                  │  → EventConsumer │              │
│                                  │  → SSE           │              │
│                                  │  → Push callback │              │
│                                  └────────┬─────────┘              │
└───────────────────────────────────────────┼──────────────────────────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
        ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
        │ Coordinator      │   │ TaskLogger       │   │ AgentLogger      │
        │ push-callback    │   │ (NDJSON)         │   │ (NDJSON)         │
        │ → EventStore     │   │ logs/<tid>.ndjson│   │ logs/agent/...   │
        │ events_<tid>.json│   │ meta, task_start │   │ llm_request      │
        │ status_update    │   │ llm_response     │   │ llm_response     │
        │ artifact_update  │   │ dispatch, replan │   │ tool_result      │
        │ help_request     │   │ task_complete    │   └──────────────────┘
        │ observation_report│  └──────────────────┘
        └──────────────────┘
                    │
                    ▼
        ┌────────────────────────────────────────────┐
        │ SAR 实验层 (ExperimentLogger CSV/NDJSON)    │
        │                                            │
        │  router_cb() → router_interactions.csv     │
        │    dispatch_task, respond_worker,           │
        │    query_sar_state, query_task_events,      │
        │    finish_task                              │
        │                                            │
        │  step_callback → agent_interactions.csv    │
        │    tool_result                              │
        │                                            │
        │  SemanticMapStore → semantic_map.jsonl     │
        │    observation_ingested                    │
        │                                            │
        │  events.ndjson                             │
        │    dispatch_task (等)                       │
        └────────────────────────────────────────────┘
```

#### 典型事件流路径示例

```
[Worker Agent 执行 report_observation]
  ↓ step_callback("tool_result", ...)
  A2AWorkerSink.emit("tool_result", content="[DATA] {"ev":"tool_result", ...}")
  → TaskStatusUpdateEvent(state=WORKING) → EventQueue → SSE
  → Coordinator push-callback → server.py
    → _extract_observation_from_status_text() → EventStore.append("observation_report")
    → SemanticMapStore.ingest_observation() → semantic_map.jsonl ("observation_ingested")
  ↓ 同时
  WorkerAgent.logger.log_tool_result() → logs/agent/sar_worker/<wid>/<tid>.ndjson
  ↓ 同时 (实验模式)
  Worker._step_callback() → ExperimentLogger
    → log_agent_interaction() → agent_interactions.csv
    → log_token_usage() → token_usage.csv

[Coordinator Router 调度子任务]
  RouterAgent 调用 dispatch_task(agent_id, prompt)
  ↓ step_callback("tool_start", tool_name="dispatch_task")
  A2ACoordinatorSink.emit("tool_start") → metadata.event_type="dispatch"
  → EventQueue → SSE → TaskLogger.log_event("dispatch")
  ↓ step_callback("tool_result", ...)
  Router._router_cb → ExperimentLogger
    → log_router_interaction("dispatch_task") → router_interactions.csv
    → log_subtask(status="assigned") → subtasks.csv
    → log_event("dispatch_task") → events.ndjson
```

### 4.3 事件与数据流交叉引用

| 事件类型 / 概念 | data_flow.md 对应位置 | 关联设计点 |
|----------------|----------------------|-----------|
| `TaskStatusUpdateEvent` | §1.④ EventSink + 结果回溯路径 | §5 EventSink 单向流 |
| INPUT_REQUIRED | INPUT_REQUIRED 暂停/恢复路径 | §6 暂停/恢复 |
| `dispatch_task` | §1.② RouterAgent dispatch_task | §3 异步推送模式 |
| `respond_worker` | INPUT_REQUIRED 恢复方向 | §6 暂停/恢复 |
| `help_request` | INPUT_REQUIRED 暂停方向 | §6 暂停/恢复 |
| `artifact_update` | 结果回溯 push-callback | §3 push-callback |
| `status_update` | 结果回溯 push-callback | §3 push-callback |
| `observation_report` | — | §4.2 典型事件流示例：report_observation |
| `observation_ingested` | — | §4.2 典型事件流示例：report_observation |
| EndReason 枚举 | poll 循环 §1 | — |
| `tool_result` (日志) | Token 记录路径 (§1) + agent_interactions | — |
