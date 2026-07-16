# event_flow.md Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `docs/system_docs/event_flow.md`, a code-level event/control-flow document for the LLaMAR SAR experiment, complementing the existing `data_flow.md`.

**Architecture:** The document follows a hybrid structure: (1) end-to-end timeline from experiment initialization to shutdown, (2) per-component event reference tables, and (3) appendices for special flows (INPUT_REQUIRED, barrier timeout, crash-safe logging, Map SSE). Each task maps to one component/phase and is executed by a subagent that traces the relevant source files.

**Tech Stack:** Markdown documentation; source code in `sar_orch/` and `src/`.

## Global Constraints

- Output file must be `docs/system_docs/event_flow.md`.
- Document must include YAML metadata header matching existing system docs style.
- Code-level references must include file paths and function names; line numbers are optional but helpful.
- Must not contradict `docs/system_docs/data_flow.md`, `docs/system_docs/logging_map.md`, or `docs/system_docs/框架.md`.
- Must cross-reference `data_flow.md` and `logging_map.md` where appropriate.
- All findings must be derived from the current codebase, not hallucinated.

---

### Task 1: Trace experiment.py initialization and shutdown

**Files:**
- Read: `sar_orch/experiment.py`
- Output section: `event_flow.md` Phase 0 and Phase 10

**Interfaces:**
- Consumes: None
- Produces: Detailed event list for `run_experiment()`, barrier/logger/coordinator/worker startup order, poll loop, and cleanup.

- [ ] **Step 1: Read the full `sar_orch/experiment.py` file**

Use `read` tool to load the entire file.

- [ ] **Step 2: Document initialization order**

List the exact sequence:
1. `SARBarrier(num_agents, scene, seed)` creation
2. `ExperimentLogger(...)` creation
3. `build_run_metadata(...)` and `write_metadata()`
4. `SARCoordinator(...)` creation and `coordinator.start()`
5. Sleep/wait for coordinator ports
6. `SARWorker(...)` creation and `worker.start()` per agent
7. Submit top-level task via `coordinator.submit_task(...)`

- [ ] **Step 3: Document the poll loop and shutdown**

List the exact sequence:
1. `while not barrier.is_finished() and steps < max_steps`
2. `await asyncio.sleep(2.0)`
3. `barrier.get_metrics()`
4. `logger.log_trajectory(step, metrics)`
5. `logger.flush_summary()`
6. Exit conditions: finished, max_steps, wall_clock timeout, `a2a_task.done()`
7. `coordinator.stop()`, `agent.stop()`, `barrier.stop()`
8. `classify_end_reason(...)`
9. `run_metrics.json` write

- [ ] **Step 4: Produce draft content for Phase 0 and Phase 10**

Write a Markdown snippet with:
- Trigger condition
- Call stack (file: function)
- State changes
- Outputs (files, futures, events)

- [ ] **Step 5: Verify against `data_flow.md`**

Confirm the initialization order matches `data_flow.md` §4 "实验入口 poll 循环".

---

### Task 2: Trace Coordinator-side event flow

**Files:**
- Read: `sar_orch/coordinator.py`
- Read: `src/a2a/coordinator/agent_executor.py`
- Read: `src/a2a/coordinator/router.py`
- Output section: `event_flow.md` Phase 1–4, Phase 9, and Coordinator event reference table

**Interfaces:**
- Consumes: Top-level task submitted by `experiment.py` (Task 1)
- Produces: Coordinator ReAct loop events, `_router_cb` events, `dispatch_task` tool details.

- [ ] **Step 1: Read `sar_orch/coordinator.py` fully**

Use `read` tool.

- [ ] **Step 2: Read `src/a2a/coordinator/agent_executor.py`**

Focus on `CoordinatorAgentExecutor.execute()` and `_execute_agentic()`.

- [ ] **Step 3: Read `src/a2a/coordinator/router.py`**

Focus on `send_task_async()` and `RouterAgent` planning loop.

- [ ] **Step 4: Document Phase 1 — top-level task entry**

Describe:
- `DefaultRequestHandler.on_message_send()` → `CoordinatorAgentExecutor.execute()`
- Extraction of `context_id`, `task_id`, `query`
- `TaskQueue.enqueue(DistributedTask(...))`
- `A2ACoordinatorSink` + `CallbackSink(_router_cb)` creation

- [ ] **Step 5: Document Phase 2 — RouterAgent ReAct start**

Describe:
- `AgentController.submit(context_id, query, sink, ...)`
- `_get_session_lock(context_id)` and `_get_session(context_id)`
- `agent_factory` → `build_router_agent()`
- `agent.attach_context(ctx)` → `CoordinatorSARHooks`
- `agent.add_user_message(query)`
- `agent.run(cancel_event, step_callback=sink.emit)`

- [ ] **Step 6: Document Phase 3 — Coordinator query tools**

For each tool (`query_sar_state`, `query_semantic_map`, `query_team_status`):
- Trigger condition
- Tool execute() body
- Return value shape
- `_router_cb` logging

- [ ] **Step 7: Document Phase 4 — dispatch_task**

Describe:
- `DispatchTaskTool.execute()`
- `self._dispatch_seq` increment
- `Router.send_task_async()`
- A2A `send_message(return_immediately=True, push_notification_config=...)`
- `TaskStore.register_future(tid)`
- `_router_cb` logging: `router_interactions.csv`, `subtasks.csv`, `events.ndjson`

- [ ] **Step 8: Document Phase 9 — query_task_events and loop continuation**

Describe:
- `QueryTaskEventsTool.execute()`
- Reading from `EventStore`
- How `INPUT_REQUIRED`, `COMPLETED`, `FAILED` affect RouterAgent next step
- `RespondWorkerTool.execute()` for resuming paused workers

- [ ] **Step 9: Produce Coordinator event reference table**

Columns: Event name, Trigger, Call stack, Consumer, Outputs.

---

### Task 3: Trace A2A transport event flow

**Files:**
- Read: `src/a2a/coordinator/router.py`
- Read: `src/a2a/coordinator/server.py` (push-callback handler)
- Read: `src/a2a/coordinator/event_store.py`
- Read: `src/a2a/worker/a2a_server.py`
- Read: `src/a2a/worker/agent_adapter.py`
- Output section: `event_flow.md` A2A Transport event reference table

**Interfaces:**
- Consumes: `dispatch_task` output from Coordinator (Task 2)
- Produces: A2A send/receive/push-callback event details.

- [ ] **Step 1: Read A2A coordinator files**

Use `read` tool on router.py, server.py, event_store.py.

- [ ] **Step 2: Read A2A worker files**

Use `read` tool on a2a_server.py, agent_adapter.py.

- [ ] **Step 3: Document outgoing send_task_async path**

Describe:
- `Router.send_task_async()`
- Protobuf `Message`, `Part`, `TaskPushNotificationConfig`
- HTTP/JSON-RPC to Worker A2A Server

- [ ] **Step 4: Document Worker incoming path**

Describe:
- `DefaultRequestHandler.on_message_send()`
- `AgentAdapter.execute(context, event_queue)`
- `TaskUpdater(event_queue, task_id, context_id)`
- Snapshot load / fresh start

- [ ] **Step 5: Document push-callback path**

Describe:
- Worker A2A Server auto-sends PushNotification
- `EventConsumer._update_task_state()` → `push_sender.send_notification()`
- HTTP POST to `http://coordinator:8080/a2a/push-callback`
- Coordinator handler: `_push_artifact_cache`, `event_store.append(...)`
- `INPUT_REQUIRED` handling

- [ ] **Step 6: Produce A2A Transport event reference table**

Columns: Event name, Trigger, Call stack, Consumer, Outputs.

---

### Task 4: Trace Worker-side event flow

**Files:**
- Read: `sar_orch/worker.py`
- Read: `src/Agent/worker_agent/agent.py`
- Read: `src/Agent/controller/controller.py`
- Read: `src/Agent/worker_agent/context.py`
- Output section: `event_flow.md` Phase 5–7 and Worker event reference table

**Interfaces:**
- Consumes: A2A task received by Worker (Task 3)
- Produces: Worker ReAct loop, tool execution, `AskCoordinatorTool`, `FinishTaskTool` details.

- [ ] **Step 1: Read `sar_orch/worker.py` fully**

Use `read` tool.

- [ ] **Step 2: Read Worker Agent and Controller**

Use `read` tool on `src/Agent/worker_agent/agent.py`, `src/Agent/controller/controller.py`, and `src/Agent/worker_agent/context.py`.

- [ ] **Step 3: Document Worker startup and `_step_callback`**

Describe:
- `SARWorker.start()` → `create_worker_a2a_server()`
- `_step_callback(type_, **data)`
- `llm_response`, `tool_start`, `tool_result` handling
- `_build_action()` mapping
- `token_usage.csv` and `agent_interactions.csv` logging

- [ ] **Step 4: Document Worker Agent ReAct loop**

Describe:
- `AgentController.submit(...)`
- `agent.run(step_callback=sink.emit)`
- `hooks.pre_llm` → `ctx.assemble()`
- `LLM.generate(messages, tools=...)`
- `hooks.post_llm` → `ctx.prune_history()`
- Tool loop with `hooks.pre_tool` / `hooks.post_tool`
- `ctx.observe(tool_name, result, success)`

- [ ] **Step 5: Document SAR tool execution**

For tools that consume steps:
- `tool.execute()` → `barrier.submit_action(agent_idx, action_str)`
- Return `ToolResult`

For zero-cost tools (`GetAgentState`, `ReportObservation`, `QuerySharedMemory`):
- Direct environment/semantic map access
- No `submit_action()` call

- [ ] **Step 6: Document AskCoordinatorTool and FinishTaskTool**

Describe:
- `AskCoordinatorTool.execute()` raises `NeedInputError(question)`
- `Agent.run()` catches → `RunResult(need_input=True)`
- `AgentController.submit()` saves snapshot via `ctx.save_snapshot(task_id, messages)`
- `AgentAdapter.execute()` calls `updater.requires_input(message=question)`
- `FinishTaskTool.execute()` sets `agent._task_complete = True`

- [ ] **Step 7: Produce Worker event reference table**

Columns: Event name, Trigger, Call stack, Consumer, Outputs.

---

### Task 5: Trace SARBarrier synchronization event flow

**Files:**
- Read: `sar_orch/barrier.py`
- Output section: `event_flow.md` Phase 6 and SARBarrier event reference table

**Interfaces:**
- Consumes: `submit_action()` calls from Worker tools (Task 4)
- Produces: Synchronized step execution, observations, timeout handling.

- [ ] **Step 1: Read `sar_orch/barrier.py` fully**

Use `read` tool.

- [ ] **Step 2: Document `submit_action()` flow**

Describe:
- `_step_lock` acquire
- `_action_queue[agent_idx] = action`
- Check if all agents submitted
- If yes: `asyncio.to_thread(self._execute_step, current_step)`
- If no: wait on `threading.Event` with 60s timeout

- [ ] **Step 3: Document `_execute_step()` flow**

Describe:
- `self.env.step(actions)`
- Generate observations
- Update `_last_actions`, `_last_successes`, `_last_observations`
- Set all `_obs_events`
- Increment `_step_counter`

- [ ] **Step 4: Document timeout NoOp fill**

Describe:
- 60s deadline
- Missing agents filled with `NoOp`
- `_current_timeout_agents` recorded
- `_last_timeout_agents` exposed via `get_last_step_log()`

- [ ] **Step 5: Document `stop()` wake-up semantics**

Describe:
- `_stopped = True`, `_finished = True`
- All `_obs_events.set()` to unblock waiting workers

- [ ] **Step 6: Produce SARBarrier event reference table**

Columns: Event name, Trigger, Call stack, Consumer, Outputs.

---

### Task 6: Trace Logger and UI/SSE event flow

**Files:**
- Read: `sar_orch/logger.py`
- Read: `src/a2a/coordinator/server.py` (SSE `/map/state`)
- Read: `src/a2a/coordinator/task_logger.py`
- Read: `src/Agent/router_agent/logger.py` and `src/Agent/worker_agent/logger.py`
- Output section: `event_flow.md` Logger/UI/SSE event reference table

**Interfaces:**
- Consumes: Events from Coordinator (Task 2), Worker (Task 4), Barrier (Task 5)
- Produces: CSV/JSON/NDJSON log files and Map SSE events.

- [ ] **Step 1: Read `sar_orch/logger.py` fully**

Use `read` tool.

- [ ] **Step 2: Read logging-related coordinator/agent files**

Use `read` tool on task_logger.py, router_agent/logger.py, worker_agent/logger.py, and server.py SSE section.

- [ ] **Step 3: Document `ExperimentLogger` write points**

For each method:
- `log_step` / `log_trajectory`
- `log_agent_interaction`
- `log_router_interaction`
- `log_coordinator_state`
- `log_token_usage`
- `log_subtask`
- `log_event`
- `flush_summary`
- `write_metadata`

Include trigger file/function and output file.

- [ ] **Step 4: Document Agent/Task/EventStore loggers**

Describe:
- `AgentLogger.log_request/log_response/log_tool_result`
- `TaskLogger.log_event`
- `EventStore.append`
- Output paths

- [ ] **Step 5: Document `/map/state` SSE**

Describe:
- `server.set_barrier(barrier)`
- `GET /map/state` → `event_generator()`
- `barrier.get_env_snapshot()` every 500ms
- Push only on step change
- HTML client rendering

- [ ] **Step 6: Produce Logger + UI/SSE event reference table**

Columns: Event name, Trigger, Call stack, Consumer, Outputs.

---

### Task 7: Synthesize final `event_flow.md`

**Files:**
- Read: outputs from Tasks 1–6
- Read: `docs/system_docs/data_flow.md`, `docs/system_docs/logging_map.md`, `docs/system_docs/框架.md`
- Create: `docs/system_docs/event_flow.md`

**Interfaces:**
- Consumes: All subagent-produced event chains and tables
- Produces: Final Markdown document.

- [ ] **Step 1: Draft the metadata header and overview diagram**

```markdown
---
日期: 2026-07-06
文档类型: 技术文档
文档概述: LLaMAR SAR 实验完整事件触发链，覆盖函数调用、回调、Future resolve、TaskState 转换、SSE 推送等代码级细节
---
```

Draw ASCII overview diagram covering all components.

- [ ] **Step 2: Write Phase 0 – Phase 10 timeline**

Use subagent outputs. Each phase includes:
- Trigger condition
- Call stack
- State changes
- Outputs

- [ ] **Step 3: Write per-component event reference tables**

Combine tables from Tasks 2–6. Ensure consistent column names.

- [ ] **Step 4: Write special flow appendices**

- A. INPUT_REQUIRED pause/resume
- B. Barrier timeout NoOp fill
- C. Crash-safe log flush
- D. Map SSE state push

- [ ] **Step 5: Add cross-references**

Link to `data_flow.md`, `logging_map.md`, `框架.md`.

- [ ] **Step 6: Self-review for consistency**

Checklist:
- [ ] No contradictions with `data_flow.md`
- [ ] All log file references match `logging_map.md`
- [ ] Function names and callbacks match current source code
- [ ] No TBD/TODO placeholders
- [ ] YAML header present

- [ ] **Step 7: Request user review**

Inform user the document is ready at `docs/system_docs/event_flow.md` and ask for feedback.
