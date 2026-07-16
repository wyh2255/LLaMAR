---
日期: 2026-07-05
文档类型: 实施计划
文档概述: 针对 LLaMAR SAR 实验数据采集层接口已定义但调用方未正确传值导致的 10 个缺口进行修复，涉及 A2A 关联键贯通、延迟测量、事件流扩展、子任务生命周期、轨迹字段补齐和空字段回填。
---

# 数据采集层缺口补充计划

## 1. 背景

`ExperimentLogger`（`sar_orch/logger.py`）的接口定义已相当完备——每个 CSV 文件的列定义、`log_*()` 方法的参数签名都已就位。但是 **调用方（worker/coordinator/barrier）未正确传入数据**，导致以下 10 个缺口：

| # | 缺口 | 影响 | 根因 | 修复涉及文件数 |
|---|------|------|------|--------------|
| 1 | `LLMLatencyMs` 始终 = 0.0 | 无法回答 "LLM 慢不慢" | Agent 核心 `llm.chat()` 调用未暴露耗时 | 4 |
| 2 | `context_id` / `task_id` 未传到回调 | 无法跨 CSV 关联 dispatch→worker→barrier | A2A sink 构造时未注入 ID | 4 |
| 3 | `events.ndjson` 仅含 dispatch_task | 事件流不完整，无法追踪全链路 | 仅 `coordinator.py:158` 一处调用 `log_event()` | 2 |
| 4 | `subtasks.csv` 仅记录 assigned | 无 subtask 时间线 | 无状态更新机制 | 2 |
| 5 | `agent_interactions` ErrorType 为空 | 无法诊断工具失败原因 | 调用方未传入——且 SAR 工具总是返回 `success=True` | 1 |
| 6 | `agent_interactions` Thinking 为空 | 丢失 model reasoning 痕迹 | 调用方未传入 | 3 |
| 7 | 无 Worker 激活延迟（原 DispatchLatencyMs） | 无法量化 A2A 调度预热开销 | A2A 协议不携带 dispatch timestamp | 1 |
| 8 | `EndReason` 缺 3 个值 | 结束原因归因不准确 | worker_timeout / environment_error / manual_interrupt 未区分 | 1 |
| 9 | `trajectory` 缺 GlobalStateDigest | 每步缺少环境状态摘要 | barrier 未生成 digest 字符串 | 3 |
| 10 | `prompt_hash` 未记录 | Prompt 版本不可复现 | 仅硬编码 `"baseline"` | 1 |

## 2. 执行顺序与依赖关系

```
Task 1 (A2A IDs) ──┬── Task 2b (Worker activation latency)
                    └── Task 3b (subtasks lifecycle)

Task 2a (LLMLatencyMs)        ── 独立
Task 3a (events.ndjson)       ── 独立
Task 4a (GlobalStateDigest)   ── 独立
Task 4b (EndReason)           ── 独立
Task 5a (ErrorType)           ── 独立
Task 5b (Thinking)            ── 独立
Task 5c (Env metadata)        ── 独立
Task 2-补 (Router 对齐)        ── 独立
```

**推荐执行顺序**：Task 2-补 → 1 → 2a → 3a → 3b → 4a → 4b → 5a → 5b → 5c → 2b（2b 数据价值较低，可最后做; 2-补 无依赖可最先做）。

## 3. Task 1：打通 A2A 跨层关联键

**目标**：让 `context_id` 和 `coordinator_task_id` / `worker_task_id` 贯穿所有日志行。

### 3.1 改动清单

| 文件 | 位置 | 改动 |
|------|------|------|
| `src/a2a/coordinator/agent_executor.py` | L335-336 | 用闭包包裹 `router_step_callback`，注入 `task_id`、`context_id` |
| `src/a2a/worker/agent_adapter.py` | L158-163 | 同上，包裹 `step_callback` |
| `sar_orch/coordinator.py` | `_router_cb` | 签名增加 `**kwargs`，接收 `task_id=`、`context_id=`，传给所有 `log_*()` |
| `sar_orch/worker.py` | `_step_callback` | 签名增加 `**kwargs`，接收 `task_id=`、`context_id=`，传给 `log_token_usage()` 等 |
| `sar_orch/logger.py` | `_ensure_file()` | `agent_interactions.csv`、`router_interactions.csv`、`trajectory.csv`、`token_usage.csv` headers 均增加 `ContextID`、`CoordinatorTaskID` |
| `sar_orch/logger.py` | `log_token_usage()` | 签名增加 `context_id`、`coordinator_task_id` 参数，写入 `token_usage.csv` |
| `sar_orch/logger.py` | `log_agent_interaction()` | 签名增加 `context_id`、`coordinator_task_id` 参数，写入 `agent_interactions.csv` |
| `sar_orch/logger.py` | `log_router_interaction()` | 签名增加 `context_id`、`coordinator_task_id` 参数，写入 `router_interactions.csv` |
| `sar_orch/logger.py` | `log_coordinator_state()` | 签名增加 `context_id`、`coordinator_task_id` 参数（写入 `agent_interactions.csv` 行） |

### 3.2 关键实现细节

```python
# agent_executor.py — 闭包注入
def _wrap_cb(cb, task_id, context_id):
    def wrapped(type_, **data):
        return cb(type_, task_id=task_id, context_id=context_id, **data)
    return wrapped

if self._router_step_callback is not None:
    wrapped = _wrap_cb(self._router_step_callback, task_id, context_id)
    sink = TeeSink([sink, CallbackSink(wrapped)])
```

```python
# coordinator.py — _router_cb 接收，所有 log_*() 调用都增加关联键
def _router_cb(event_type: str, **kw):
    task_id = kw.pop("task_id", "")
    context_id = kw.pop("context_id", "")
    # log_token_usage / log_router_interaction / log_subtask / log_event / log_coordinator_state
    # 均增加：context_id=context_id, coordinator_task_id=task_id
```

```python
# worker.py — _step_callback 接收，llm_response / tool_result 都转发 context
# log_token_usage 和 log_agent_interaction 均需新增 task_id / context_id 参数
def _step_callback(type_: str, **data):
    task_id = data.pop("task_id", "")
    context_id = data.pop("context_id", "")
    # ...
    if type_ == "llm_response" and usage is not None and exp is not None:
        exp.log_token_usage(
            ...,  # 原有参数不变
            context_id=context_id,
            coordinator_task_id=task_id,
        )
    if type_ == "tool_result" and self._pending_tool is not None:
        exp.log_agent_interaction(
            ...,  # 原有参数不变
            context_id=context_id,
            coordinator_task_id=task_id,
        )
```

```python
# logger.py — headers_map 更新
"agent_interactions": [
    "Step", "Agent", "ToolName", "ToolArgs", "Action", "Observation",
    "LLMInput", "LLMOutput", "Thinking", "RunID", "CorrelationID",
    "EventType", "ToolLatencyMs", "ErrorType",
    "ContextID", "CoordinatorTaskID",  # ← 新增
],
"router_interactions": [
    "Step", "Subtask", "AssignedTo", "RunID",
    "CorrelationID", "WorkerTaskID", "EventType",
    "ContextID", "CoordinatorTaskID",  # ← 新增
],
"trajectory": [
    # ... 原有 16 列最后增加
    "ContextID", "CoordinatorTaskID",
],
"token_usage": [
    # ... 原有列最后增加
    "ContextID", "CoordinatorTaskID",
],
```

### 3.3 验证方式

单次实验（`scene=1 agents=2 seed=42`）后检查 `agent_interactions.csv`，任意行 `ContextID` 非空。

---

## 4. Task 2：补齐关键延迟指标

### 4.1 Task 2a — LLMLatencyMs

**目标**：`token_usage.csv` 的 `LLMLatencyMs` 列不再为 0.0。

#### 改动清单

| 文件 | 位置 | 改动 |
|------|------|------|
| `src/Agent/worker_agent/agent.py` | L535 (`llm.generate`) | `llm.generate()` 前后 `time.monotonic()`，以 `llm_latency_ms` 传入回调 |
| `src/Agent/router_agent/agent.py` | L519 (`llm.generate`) | 同上 |
| `sar_orch/worker.py` | L142-151 | 从 `data` 中提取 `llm_latency_ms` 传给 `log_token_usage()` |
| `sar_orch/coordinator.py` | L124-135 | 从 `kw` 中提取 `llm_latency_ms` 传给 `log_token_usage()` |

#### 关键实现

```python
# worker_agent/agent.py — 包裹 llm.generate()
import time

_t0 = time.monotonic()
response = await self.llm.generate(messages=messages_for_llm, tools=tool_list)
llm_latency_ms = (time.monotonic() - _t0) * 1000

# 在 step_callback("llm_response", ...) 调用中增加
await step_callback(
    "llm_response",
    content=response.content,
    tool_calls=response.tool_calls,
    usage=response.usage,
    input_messages=messages_for_llm,
    llm_latency_ms=llm_latency_ms,   # ← 新增
    thinking=response.thinking,      # ← 见 Task 5b
)
```

```python
# worker.py — 转发
if usage is not None and self._exp_logger is not None:
    self._exp_logger.log_token_usage(
        step=..., agent=...,
        prompt_tokens=..., completion_tokens=..., total_tokens=...,
        cache_hit_tokens=..., cache_miss_tokens=...,
        llm_latency_ms=data.get("llm_latency_ms", 0.0),  # ← 原本缺省 0.0
    )
```

#### 验证方式

`token_usage.csv` 中 `LLMLatencyMs` > 0（应接近 DeepSeek API 实际响应时间，通常 500-5000ms）。

### 4.2 Task 补充 — Router Agent 回调对齐（Worker/Router 一致性修复）

**问题**：交叉验证发现 Worker Agent 和 Router Agent 的 `step_callback` 存在两处不一致，建议在本次改动中一并补齐：

| 不一致处 | Worker Agent | Router Agent | 影响 | 修复方式 |
|---------|-------------|-------------|------|---------|
| `input_messages` | ✅ 传入 `step_callback("llm_response", input_messages=...)` | ❌ 未传入 | Router 侧 `llm_response` 无法获取 LLM 输入 | 在 `router_agent/agent.py` 的 `step_callback("llm_response")` 调用中增加 `input_messages=messages_for_llm` |
| `step_boundary` | ✅ 每步结束时回调 `step_callback("step_boundary", ...)` | ❌ 完全缺失 | Router 侧缺少 step 边界的回调通知 | 在 `router_agent/agent.py` 的 step 循环末尾增加与 Worker Agent 相同的 `step_boundary` 调用 |

#### 改动清单

| 文件 | 位置 | 改动 |
|------|------|------|
| `src/Agent/router_agent/agent.py` | `step_callback("llm_response")` | 增加 `input_messages=messages_for_llm` |
| `src/Agent/router_agent/agent.py` | step 循环末尾 | 新增 `step_callback("step_boundary", step=step, max_steps=self.max_steps, elapsed=...)` |

#### 验证方式

Router Agent 的 `_step_callback` 能收到 `input_messages` 且在 step 结束时收到 `step_boundary`。

---

### 4.3 Task 2b — Worker 激活延迟

**目标**：量化 Worker 从收到 A2A task 到首次执行工具之间的预热开销。

> **注意**：原计划方案中的 DispatchLatencyMs（coordinator dispatch → worker 首调用）因 A2A 协议不携带 dispatch timestamp 而不可行。取而代之的是 **Worker 端测量首次 LLM response → 首次 tool execution** 的间隔，用于评估 worker 预热和调度开销。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/worker.py` | `__init__` | 新增 `_task_received_at = 0.0` |
| `sar_orch/worker.py` | `_step_callback("llm_response")` | 首次 `llm_response` 时记录 `_task_received_at = time.monotonic()` |
| `sar_orch/worker.py` | `_step_callback("tool_start")` | 首次 `tool_start` 计算 `activation_latency_ms` 并存入 `_pending_tool` |
| `sar_orch/worker.py` | `_step_callback("tool_result")` | 从 `_pending_tool` 提取 `activation_latency_ms` 传给 `log_agent_interaction()` |
| `sar_orch/logger.py` | `log_agent_interaction()` / `_ensure_file()` | 签名增加 `activation_latency_ms` 参数；headers 增加 `ActivationLatencyMs` 列 |

#### 关键实现

```python
# worker.py __init__ — 新增状态
self._task_received_at = 0.0

# _step_callback — 首条 llm_response 到达时记为 "task received"
if type_ == "llm_response":
    if self._task_received_at == 0.0:
        self._task_received_at = time.monotonic()
    # ... 原有 llm_response 逻辑不变

# _step_callback - tool_start — 首次计算激活延迟
elif type_ == "tool_start":
    self._call_seq += 1
    activation_ms = 0.0
    if self._task_received_at > 0:
        activation_ms = (time.monotonic() - self._task_received_at) * 1000.0
        self._task_received_at = 0.0  # 只计算一次，下次 tool_start 不再触发
    self._pending_tool = {
        "tool_name": data.get("tool_name", ""),
        "arguments": data.get("arguments", {}),
        "started_at": time.monotonic(),
        "correlation_id": f"{self.agent_name}-tool-{self._call_seq}",
        "activation_latency_ms": activation_ms,  # ← 新增
    }

# _step_callback - tool_result — 传给 log_agent_interaction
elif type_ == "tool_result" and self._pending_tool is not None:
    # ...
    exp.log_agent_interaction(
        step=...,
        agent=self.agent_name,
        tool_name=tool_name,
        tool_args=json.dumps(args, ensure_ascii=False),
        action=_build_action(tool_name, args),
        observation=data.get("content", ""),
        llm_input=self._last_llm_input,
        llm_output=self._last_llm_output,
        correlation_id=self._pending_tool["correlation_id"],
        event_type="tool_result",
        tool_latency_ms=tool_latency_ms,
        activation_latency_ms=self._pending_tool["activation_latency_ms"],  # ← 新增
    )
```

```python
# logger.py — log_agent_interaction 签名 + headers 更新
def log_agent_interaction(
    self,
    ...,
    error_type: str = "",
    activation_latency_ms: float = 0.0,  # ← 新增
):
    with self._lock:
        self._ensure_file("agent_interactions")
        row = {
            # ... 原有字段不变
            "ErrorType": error_type,
            "ActivationLatencyMs": activation_latency_ms,  # ← 新增
        }

# _ensure_file — headers 增加（最终合并结果：Task 1 + Task 2b）
"agent_interactions": [
    "Step", "Agent", "ToolName", "ToolArgs", "Action", "Observation",
    "LLMInput", "LLMOutput", "Thinking", "RunID", "CorrelationID",
    "EventType", "ToolLatencyMs", "ErrorType",
    "ContextID", "CoordinatorTaskID",       # ← Task 1 新增
    "ActivationLatencyMs",                  # ← Task 2b 新增（在 Task 1 之后追加）
],
```

#### 验证方式

`agent_interactions.csv` 中第一条工具调用行的 `ActivationLatencyMs` > 0（通常为几百 ms，视 LLM 首 token 响应时间而定）。

---

## 5. Task 3：完善事件流与子任务生命周期

### 5.1 Task 3a — events.ndjson 扩展

**目标**：`events.ndjson` 不再只有 `dispatch_task`，而是覆盖 coordinator 所有工具和 worker 关键事件。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/coordinator.py` | `_router_cb` | 为 `respond_worker`、`query_sar_state`、`query_task_events`、`finish_task` 各增加 `log_event()`，覆盖 `tool_start` 和 `tool_result` |
| `sar_orch/worker.py` | `_step_callback` | 为 `llm_response`、`tool_start`、`tool_result`、`step_boundary` 各增加 `log_event()` |

#### 关键实现

```python
# coordinator.py — 在现有 tool_start/result 分支中插入：
elif tool_name == "respond_worker":
    self._exp_logger.log_event(
        "respond_worker",
        step=step,
        agent="Coordinator",
        payload=args,
    )
# query_sar_state, query_task_events, finish_task 同理

# coordinator.py — 在 tool_result 分支中（query_sar_state 已有）：
elif tool_name == "query_task_events":
    self._exp_logger.log_event(
        "query_task_events_result",
        step=step,
        agent="Coordinator",
        content=(content or "")[:1000],
    )
```

```python
# worker.py — _step_callback 每个分支增加：
if type_ == "llm_response":
    self._exp_logger.log_event(
        "worker_llm_response",
        step=getattr(self._barrier, "_step_counter", 0),
        agent=self.agent_name,
        payload={"token_count": getattr(usage, "total_tokens", 0)} if usage else {},
    )
elif type_ == "tool_start":
    self._exp_logger.log_event(
        "worker_tool_start",
        step=getattr(self._barrier, "_step_counter", 0),
        agent=self.agent_name,
        payload={"tool_name": data.get("tool_name"), "args": data.get("arguments")},
    )
```

#### 验证方式

`events.ndjson` 中出现 `worker_llm_response`、`worker_tool_start`、`worker_tool_result`、`respond_worker`、`query_sar_state_result`、`query_task_events_result`、`finish_task` 事件类型。

### 5.2 Task 3b — subtasks 生命周期跟踪

**目标**：`subtasks.csv` 不再只有 status=`"assigned"`，而是记录完整的 assigned → in_progress → completed/failed 时间线。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/coordinator.py` | `_router_cb` | 增加 `query_task_events` 的 `tool_result` 处理：解析 JSON 响应，对 `"completed"`/`"failed"`/`"canceled"` 状态调用 `log_subtask(status=...)` |
| `sar_orch/logger.py` | `log_subtask()` | 确保追加写入（已实现），支持 status 更新 |

#### 关键实现

```python
# coordinator.py — query_task_events tool_result
elif event_type == "tool_result":
    tool_name = kw.get("tool_name", "")
    content = kw.get("content", "")
    if tool_name == "query_task_events":
        try:
            task_states = json.loads(content)
            for ts in (task_states if isinstance(task_states, list) else []):
                tid = ts.get("task_id", "")
                state = ts.get("state", "")
                if state in ("completed", "failed", "canceled"):
                    self._exp_logger.log_subtask(
                        subtask_id=tid,
                        status=state,
                        step=step,
                    )
        except (json.JSONDecodeError, TypeError):
            pass
```

> **注意**：上述实现假设 `query_task_events` 的响应内容是 JSON 数组，每个元素包含 `task_id` 和 `state` 字段。实施前需验证 `A2ACoordinatorSink` 或 `A2AWorkerSink` 的 task event 实际输出格式，确认字段名和嵌套层级。如格式不符，需调整 JSON 解析逻辑。

#### 验证方式

`subtasks.csv` 中同一 `SubtaskID` 出现多行：至少 `"assigned"` + `"completed"`（或 `"failed"`）。

---

## 6. Task 4：补齐轨迹字段与结束原因

### 6.1 Task 4a — GlobalStateDigest

**目标**：`trajectory.csv` 每行包含可读的环境状态摘要。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/barrier.py` | `_execute_step()` | 每步生成 digest 字符串（≤200 字符） |
| `sar_orch/barrier.py` | `get_last_step_log()` | 新增 `global_state_digest` 返回 |
| `sar_orch/logger.py` | `_ensure_file()` / `log_step()` | `trajectory` headers 增加 `GlobalStateDigest` |
| `sar_orch/experiment.py` | L344-362 | `log_step()` 传入 `global_state_digest` |

#### 关键实现

```python
# barrier.py — 在 _execute_step() 末尾，更新 _last_xxx 之前
digest_parts = []
# Agent info
for i in range(self.num_agents):
    state = self.env.get_agent_state(i)
    pos = state.get("position", "?")
    inv = state.get("inventory", {})
    digest_parts.append(f"A{i}:({pos[0]},{pos[1]})")
# Fire counts
all_objs = self.env.controller.field.all_objects(expand=True, with_memory=False)
active_fires = sum(1 for o in all_objs if getattr(o, "average_intensity", 0) > 0)
total_fires = sum(1 for o in all_objs if type(o).__name__ == "Fire")
digest_parts.append(f"fire:{active_fires}/{total_fires}")
# Person status
persons = [o for o in all_objs if type(o).__name__ == "Person"]
rescued = sum(1 for p in persons if getattr(p, "status", "") == "rescued")
digest_parts.append(f"pers:{len(persons)}/resc:{rescued}")
# Coverage
coverage = self.env.checker.get_coverage()
digest_parts.append(f"cov:{coverage:.0%}")

self._last_global_state_digest = " ".join(digest_parts)[:200]
```

> **注意**：上述 digest 代码依赖以下 GridEngine API，实施前需验证 barrier 的 `self.env` 上是否存在：
> - `get_agent_state(i)` 返回字典，含 `position` 和 `inventory` 键
> - `controller.field.all_objects(expand=True, with_memory=False)` 返回对象列表
> - `checker.get_coverage()` 返回 0-1 的浮点数
>
> 如 API 签名或返回结构不符，需调整对应的字段访问逻辑。

#### 验证方式

`trajectory.csv` 每行 `GlobalStateDigest` 格式如 `"A0:(3,4) A1:(7,2) fire:2/5 pers:3/resc:1 cov:45%"`。

### 6.2 Task 4b — EndReason 扩展

**目标**：`classify_end_reason()` 增加 `worker_timeout`、`environment_error`、`manual_interrupt` 三个值。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/experiment.py` | `classify_end_reason()` | 增加 3 个条件分支 |
| `sar_orch/experiment.py` | `run_experiment()` | 增加 `worker_timeout` 检测（从 barrier 获取) |
| `sar_orch/experiment.py` | `run_experiment()` | 注册 SIGINT/SIGTERM 信号处理器（在 event loop 内部） |

#### 关键实现

```python
# experiment.py — classify_end_reason 扩展
def classify_end_reason(
    *,
    finished: bool,
    steps: int,
    max_steps: int,
    elapsed_seconds: float,
    wall_clock_limit: float,
    a2a_done: bool,
    a2a_error: bool,
    coordinator_error: bool,
    worker_timeout: bool = False,       # ← 新增
    environment_error: bool = False,    # ← 新增
    manual_interrupt: bool = False,     # ← 新增
) -> str:
    if manual_interrupt:
        return "manual_interrupt"
    if finished:
        return "success"
    if environment_error:
        return "environment_error"
    if coordinator_error or a2a_error:
        return "framework_error"
    if worker_timeout:
        return "worker_timeout"
    if elapsed_seconds >= wall_clock_limit:
        return "wall_clock_timeout"
    if steps >= max_steps:
        return "max_steps_reached"
    if a2a_done:
        return "coordinator_finished_early"
    return "stopped_before_success"
```

```python
# experiment.py — 信号处理（asyncio 兼容，必须在 asyncio.run() 内部调用）
import asyncio
import signal

# 注意：loop.add_signal_handler() 需要运行中的 event loop，因此信号注册
# 必须放在 run_experiment() 函数内部（poll 循环之前）而非 main() 中。

async def run_experiment(...):
    interrupt_event = asyncio.Event()

    def _signal_handler():
        logger.warning("Interrupt received, stopping...")
        interrupt_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows / non-UNIX 不支持 add_signal_handler
            logger.warning("Signal handler not supported on this platform")
            break

    # ... poll loop 中检查
    manual_interrupt = interrupt_event.is_set()
```

> **重要**：`loop.add_signal_handler()` 必须在 `asyncio.run(run_experiment())` 已经启动后的协程内部调用，不能放在模块顶层或 `main()` 同步代码中（因为那时 event loop 可能尚未运行或尚未绑定到当前线程）。使用 `asyncio.get_running_loop()` 确保在正确的位置获取 loop。

#### 验证方式

- Ctrl+C 后 `run_metrics.json` 的 `end_reason` 为 `"manual_interrupt"`
- barrier 超时且环境未成功时 `end_reason` 为 `"worker_timeout"`

### 6.3 Task 4c — prompt_hash

**目标**：`metadata.json` 包含 prompt 内容的 SHA256 哈希。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/experiment.py` | 新增 `_compute_prompt_hash()` | 对 prompt 目录下所有 `.md` 文件内容做 SHA256 |
| `sar_orch/experiment.py` | `build_run_metadata()` | 增加 `prompt_hash` 字段 |

#### 关键实现

```python
import hashlib

def _compute_prompt_hash(*prompts_dirs: str) -> str:
    """Compute SHA256 of all prompt files in the given directories."""
    hasher = hashlib.sha256()
    for d in prompts_dirs:
        for f in sorted(Path(d).glob("*.md")):
            hasher.update(f.name.encode())
            hasher.update(f.read_bytes())
    return hasher.hexdigest()
```

```python
# build_run_metadata() — 实际传递明确的 prompt 目录路径
from pathlib import Path

base = Path(__file__).parent  # sar_orch/
coordinator_prompts = str(base / "prompts" / "coordinator")
worker_prompts = str(base / "prompts" / "worker")
prompt_hash = _compute_prompt_hash(coordinator_prompts, worker_prompts)
# 加入返回的 dict
```

> **注意**：prompt 目录路径使用相对于 `sar_orch/` 的约定：
> - Coordinator: `sar_orch/prompts/coordinator/`（含 `system.md`、`system.semantic.md`、`system.oracle.md`）
> - Worker: `sar_orch/prompts/worker/`（含 `system.md`）
>
> 若后期增加 prompts 目录，只需在 `_compute_prompt_hash()` 调用处追加路径参数即可。

#### 验证方式

`metadata.json` 中 `prompt_hash` 为 64 字符 hex 字符串（同一 prompt 文件集下两次运行结果应一致）。

---

## 7. Task 5：补齐空字段与环境元数据

### 7.1 Task 5a — ErrorType

**目标**：`agent_interactions.csv` 的 `ErrorType` 列不再为空。

> **说明**：审查中发现 SAR 工具总是返回 `success=True`（`tool_result` 的 `success` 参数始终为 True），无法从框架层面获取失败类型。因此采用 **observation 文本分析** 策略，从工具返回的内容中推断错误类型。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/worker.py` | `_step_callback("tool_result")` | 从 `data.get("content", "")` 中匹配错误关键词 |

#### 关键实现

```python
def _infer_error_type(observation: str) -> str:
    """Heuristic error type inference from SAR observation text.

    Note: This is best-effort keyword matching. SAR's grid environment uses
    natural language observations (e.g., "Cannot navigate to (5,3): invalid cell")
    rather than structured error codes. The keyword list should be tuned by
    reviewing real observation data after initial runs.
    """
    obs_lower = observation.lower()
    if not obs_lower:
        return ""

    # SAR-specific spatial errors
    if any(kw in obs_lower for kw in (
        "invalid cell", "out of bounds", "not reachable",
        "no path", "blocked", "cannot move",
    )):
        return "invalid_target"

    # Agent/rescue state errors
    if any(kw in obs_lower for kw in (
        "already rescued", "already carrying", "not carrying",
        "no person", "not found", "no supply", "already has",
    )):
        return "state_conflict"

    # General action failure markers
    if any(kw in obs_lower for kw in (
        "cannot", "unable to", "failed", "error:",
    )):
        return "action_failed"

    if "timeout" in obs_lower:
        return "timeout"

    return ""

# 在 _step_callback("tool_result") 中使用：
error_type = _infer_error_type(data.get("content", ""))
exp.log_agent_interaction(
    ...,
    error_type=error_type,
)
```

#### 验证方式

`agent_interactions.csv` 的 `ErrorType` 列在无效目标或失败动作时不为空（如 `"invalid_target"`）。

### 7.2 Task 5b — Thinking

**目标**：`agent_interactions.csv` 的 `Thinking` 列不再为空。

| 文件 | 位置 | 改动 |
|------|------|------|
| `src/Agent/worker_agent/agent.py` | `step_callback("llm_response")` | 增加 `thinking=response.thinking` |
| `src/Agent/router_agent/agent.py` | 同上 | 增加 `thinking=response.thinking` |
| `sar_orch/worker.py` | `_step_callback("llm_response")` | 提取 `data.get("thinking", "")` 存入实例变量，在 `tool_result` 中传给 `log_agent_interaction(thinking=...)` |

> **注意**：LLMResponse 的字段名是 `thinking`（不是 `reasoning_content`），类型为 `str | None`。DeepSeek 等模型会返回 reasoning 内容。

#### 验证方式

使用 DeepSeek 模型时（或模拟 reasoning 内容），`agent_interactions.csv` 中 `Thinking` 列包含模型推理过程。

### 7.3 Task 5c — 环境元数据入轨迹

**目标**：`trajectory.csv` 每行包含 `EnvName`、`ScenarioID`、`Seed`。

| 文件 | 位置 | 改动 |
|------|------|------|
| `sar_orch/logger.py` | `_ensure_file()` / `log_step()` | headers 增加 `EnvName`、`ScenarioID`、`Seed`；方法签名增加对应参数 |
| `sar_orch/experiment.py` | L344-362 | 从 `metadata` 获取值传入 `log_step()` |

#### 验证方式

`trajectory.csv` 每行 `EnvName` = `"SAR"`、`ScenarioID` = `"scene_1"`、`Seed` = `42`。

---

## 8. 回归验证

每个 Task 实施后，执行以下命令确认不破坏现有功能：

```bash
# Lint
uv run --with ruff ruff check src/ sar_orch/

# 单次实验 smoke test（确保完成并生成所有日志文件）
cd /home/wyh/daily_work/LLaMAR
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 1 --seed 42 --sandbox-profile off
```

预期输出文件列表：

| 文件 | Task 覆盖 |
|------|-----------|
| `trajectory.csv` | 4a (GlobalStateDigest), 5c (EnvName/ScenarioID/Seed) |
| `agent_interactions.csv` | 1 (ContextID/CoordinatorTaskID), 2b (ActivationLatencyMs), 5a (ErrorType), 5b (Thinking) |
| `router_interactions.csv` | 1 (ContextID/CoordinatorTaskID) |
| `token_usage.csv` | 1 (ContextID/CoordinatorTaskID), 2a (LLMLatencyMs > 0) |
| `subtasks.csv` | 3b (status 多行) |
| `events.ndjson` | 3a (多种 event_type) |
| `metadata.json` | 4c (prompt_hash) |
| `run_metrics.json` | 4b (end_reason 含新值) |

另外执行以下字段值检查脚本，验证新列的非空率：

```bash
cd /home/wyh/daily_work/LLaMAR
RESULT_DIR=$(ls -td sar_orch/results/sar_experiment_*/ | head -1)

echo "=== LLMLatencyMs > 0 ==="
python3 -c "
import csv
with open('${RESULT_DIR}token_usage.csv') as f:
    rows = list(csv.DictReader(f))
    total = len(rows)
    filled = sum(1 for r in rows if float(r.get('LLMLatencyMs', 0)) > 0)
    print(f'LLMLatencyMs > 0: {filled}/{total} ({100*filled/total:.0f}%)')
"

echo "=== ContextID non-empty ==="
for csv_file in token_usage.csv agent_interactions.csv router_interactions.csv trajectory.csv; do
  python3 -c "
import csv
with open('${RESULT_DIR}${csv_file}') as f:
    rows = list(csv.DictReader(f))
    total = len(rows)
    filled = sum(1 for r in rows if r.get('ContextID', '').strip())
    print(f'{csv_file}: ContextID non-empty {filled}/{total} ({100*filled/total:.0f}%)')
"
done

echo "=== GlobalStateDigest format ==="
python3 -c "
import csv, re
with open('${RESULT_DIR}trajectory.csv') as f:
    rows = list(csv.DictReader(f))
    for i, r in enumerate(rows[:3]):
        d = r.get('GlobalStateDigest', '')
        print(f'  Row {i}: {d}')
    print(f'  Total rows: {len(rows)}')
"

echo "=== EnvName/ScenarioID/Seed ==="
python3 -c "
import csv
with open('${RESULT_DIR}trajectory.csv') as f:
    rows = list(csv.DictReader(f))
    r = rows[0]
    print(f'EnvName={r.get(\"EnvName\")} ScenarioID={r.get(\"ScenarioID\")} Seed={r.get(\"Seed\")}')
"

echo "=== Event types in events.ndjson ==="
python3 -c "
import json
types = set()
with open('${RESULT_DIR}events.ndjson') as f:
    for line in f:
        if line.strip():
            e = json.loads(line)
            types.add(e.get('event_type', ''))
print(f'Event types ({len(types)}): {sorted(types)}')
"
```

---

## 9. 附录：审查确认的关键修正

以下是在审查阶段发现并修正的原方案错误：

| 项目 | 原方案 | 审查发现 | 修正 |
|------|--------|---------|------|
| DispatchLatencyMs | Worker 计算 coordinator dispatch → 首调用延迟 | Worker 不可知 coordinator 的 dispatch_sent_at | 改为 Worker 端测量 task received → first tool |
| ErrorType | 从 `tool_result.success` 提取 | SAR 工具总是返回 `success=True` | 改为 observation 文本关键词分析 |
| Thinking | `response.reasoning_content` | 正确属性为 `response.thinking` | 修正属性名 |
| 回调兼容 | 闭包直接注入 `task_id`、`context_id` | 回调需 `**kwargs` 避免 TypeError | 增加 `**kwargs` catch-all |
| subtasks 检测 | "从 query_task_events 检测完成" | 需要明确的 JSON 解析和 state 映射 | 补充了代码示例 |

### 9.1 文档审查后的补充修正

以下是在文档审查阶段发现并补充的修正（2026-07-05 文档审查后追加）：

| 项目 | 原方案 | 审查发现 | 修正 |
|------|--------|---------|------|
| 方法名 | `llm.chat()` | 实际方法为 `llm.generate()` | Task 2a 代码示例已修正 |
| token_usage 关联键 | 只加了 3 个 CSV（agent/router/trajectory） | `token_usage.csv` 同样需要 ContextID 才能跨文件 join | Task 1 改动清单和代码示例已补充 |
| Task 2b 实现粒度 | 仅有 `_first_tool` 标志，无实际延迟计算 | 代码不完整，无法实施 | 补充了完整的 `_task_received_at` + 首次 `llm_response` 记时 + `tool_start` 计算逻辑 |
| query_task_events 格式 | 假设返回 `[{task_id, state}]` 数组 | A2A 实际响应格式未验证，可能嵌套层级不同 | Task 3b 增加了格式验证注意事项 |
| GlobalStateDigest API | 假设 `env.get_agent_state()` 等 API 存在 | 未验证 barrier 的 `self.env` 实际类型和可用 API | Task 4a 增加了 API 验证注意事项 |
| prompt 目录路径 | 未指定具体的 prompt 目录位置 | `build_run_metadata()` 不知道传什么路径 | 明确了 `sar_orch/prompts/coordinator/` 和 `sar_orch/prompts/worker/` |
| 信号注册位置 | 在模块顶层调用 `loop.add_signal_handler()` | 需要运行中的 event loop（必须在 `run_experiment()` 协程内部） | 改为在 `run_experiment()` 内部使用 `asyncio.get_running_loop()` |
| Router agent 不匹配 | 未意识到 Worker/Router Agent 回调不一致 | Worker 传 `input_messages`、有 `step_boundary`；Router 两者都缺 | 新增 §4.2 Task 补充 对齐 |
| ErrorType 关键词 | `"cannot"`, `"blocked"`, `"unable"`, `"failed"` 过于通用 | 可能在正常 SAR 反馈中误匹配 | 增加了 SAR 特有空间/状态关键词，分为 `invalid_target`/`state_conflict`/`action_failed` 三级 |
| 回归验证 | 仅检查文件存在 + lint | 新列可能为空或格式不对 | 增加了字段值检查脚本，验证非空率 |
