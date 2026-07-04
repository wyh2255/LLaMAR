# Logging Simplification — AgentLogger NDJSON + Remove A2AWorkerSink NDJSON

**Goal:** Merge AgentLogger and A2AWorkerSink NDJSON into one NDJSON output, removing ~90% content redundancy.

**Architecture:** AgentLogger changes from free-text JSON dump format to single-line NDJSON, gains task_id/context_id/usage/ISO timestamp. A2AWorkerSink NDJSON writing is removed (EventQueue push path preserved).

**Tech Stack:** Python 3.10+, NDJSON, threading

## Global Constraints

- All tests must pass: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/ -v`
- Lint: `uv run --with ruff ruff check src/Agent/worker_agent/logger.py src/a2a/worker/sink.py src/Agent/worker_agent/agent.py src/a2a/worker/agent_adapter.py`
- No new dependencies

---

## File Structure

| File | Operation | Responsibility |
|------|-----------|---------------|
| `src/Agent/worker_agent/logger.py` | Modify | Format → NDJSON, add task_id/context_id/usage/ISO timestamp |
| `src/a2a/worker/sink.py` | Modify | Remove NDJSON writing (keep EventQueue push) |
| `src/Agent/worker_agent/agent.py` | Modify | Pass `usage` to `log_response()` |
| `src/a2a/worker/agent_adapter.py` | Modify | Remove `log_dir` from A2AWorkerSink creation |

### Task 1: AgentLogger NDJSON format

**Files:**
- Modify: `src/Agent/worker_agent/logger.py`

**Interfaces:**
- Produces: `AgentLogger.__init__(task_id="", context_id="")` — new params
- Produces: `log_request(messages, tools, step_index=0)` — NDJSON line per call
- Produces: `log_response(content, thinking, tool_calls, finish_reason, usage=None)` — NDJSON line with usage
- Produces: `log_tool_result(tool_name, arguments, success, result="", error="")` — NDJSON line

Each line written as `ndjson_entry` dict with fields: `ts` (ISO UTC), `task_id`, `context_id`, `event`, plus event-specific fields below.

File naming: `{task_id}.ndjson` instead of `agent_run_*.log`.
File opened on first write and reused (append mode).

- [ ] **Step 1: Read the current file**
- [ ] **Step 2: Rewrite `__init__`** — accept `task_id=""`, `context_id=""`, change log_dir, file opened lazily on first write
- [ ] **Step 3: Rewrite `log_request()`** — NDJSON with `event="llm_request"`, `messages`, `tools`, `step_index`
- [ ] **Step 4: Rewrite `log_response()`** — NDJSON with `event="llm_response"`, `content`, `thinking`, `tool_calls`, `finish_reason`, `usage`
- [ ] **Step 5: Rewrite `log_tool_result()`** — NDJSON with `event="tool_result"`, `tool_name`, `arguments`, `success`, `result`, `error`
- [ ] **Step 6: Run tests + lint**

### Task 2: Remove NDJSON from A2AWorkerSink

**Files:**
- Modify: `src/a2a/worker/sink.py`

**Interfaces:**
- Consumes: Nothing from Task 1
- Produces: `A2AWorkerSink.__init__(event_queue, task_id, context_id)` — no `log_dir` param (or keep but ignore — backward compat)
- Removes: `_ndjson_fh`, `_write_ndjson()`, all `self._write_ndjson(ndjson_entry)` calls in `emit()`

- [ ] **Step 1: Read the current file**
- [ ] **Step 2: Remove `_ndjson_fh` from `__init__`**, remove `log_dir` param
- [ ] **Step 3: Remove `_write_ndjson()` method**
- [ ] **Step 4: Remove all `self._write_ndjson(ndjson_entry)` calls in `emit()`**
- [ ] **Step 5: Run tests + lint**

### Task 3: Pass usage to AgentLogger

**Files:**
- Modify: `src/Agent/worker_agent/agent.py`

**Interfaces:**
- Consumes: `AgentLogger.log_response(..., usage=response.usage)` from Task 1
- At line 539-544: add `usage=response.usage` to `self.logger.log_response()` call

- [ ] **Step 1: Read lines 539-544**
- [ ] **Step 2: Add `usage=response.usage` to log_response call**
- [ ] **Step 3: Run tests + lint**

### Task 4: Remove log_dir from A2AWorkerSink creation

**Files:**
- Modify: `src/a2a/worker/agent_adapter.py`

**Interfaces:**
- Consumes: `A2AWorkerSink(event_queue, task_id, context_id)` from Task 2 — no log_dir
- At line 143-147: change `A2AWorkerSink(event_queue, task_id, context_id, log_dir=...)` → `A2AWorkerSink(event_queue, task_id, context_id)`
- Remove `_log_dir` class default (keep `__init__` assignment for backward compat with server construction)

- [ ] **Step 1: Read lines 143-147**
- [ ] **Step 2: Remove log_dir from A2AWorkerSink call**
- [ ] **Step 3: Run tests + lint**

## Execution order

1. Task 1 (logger.py) — core change, needs careful review
2. Task 2 (sink.py) — independent from Task 1
3. Task 3 (agent.py) — depends on Task 1 interface
4. Task 4 (agent_adapter.py) — depends on Task 2 interface
