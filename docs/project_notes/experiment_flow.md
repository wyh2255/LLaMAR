---
日期: 2026-06-29
文档类型: 流程文档
文档概述: SAR 端到端实验完整流程说明，涵盖启动时序、每步执行、日志链路、清理时序和关键设计
---

# SAR 实验完整流程

## 架构概览

```
experiment.py  (入口)
  ├─ SARBarrier      ── 封装 SAREnv，多智能体步进同步器
  ├─ ExperimentLogger ── CSV 日志（5 个文件）
  ├─ SARCoordinator  ── FastAPI:8080 + A2A:8081（后台线程）
  │   └─ RouterAgent (LLM) + QuerySARStateTool
  └─ SARWorker × N   ── A2A Server:8191~8196（后台线程）
      └─ WorkerAgent (LLM) + 11 个 SAR 工具
```

## 文件职责

| 文件 | 职责 |
|------|------|
| `sar_orch/experiment.py` | 实验入口，编排完整生命周期 |
| `sar_orch/coordinator.py` | `SARCoordinator` 包装类，启动 server + 提交 A2A 任务 |
| `sar_orch/worker.py` | `SARWorker` 包装类，启动 A2A server + WebSocket 连接 |
| `sar_orch/barrier.py` | `SARBarrier` 多智能体步进同步器，封装 `SAREnv` |
| `sar_orch/logger.py` | `ExperimentLogger` CSV 日志（5 文件，线程安全，增量兜底） |
| `sar_orch/benchmark.py` | 批量运行器，子进程隔离，并发 2 轮，超时 kill |
| `sar_orch/tools/worker/__init__.py` | 导出 11 个 `SAR_WORKER_TOOLS` |
| `sar_orch/tools/coordinator/query_sar_state.py` | Coordinator 的全局状态查询工具 |
| `sar_orch/prompts/worker/system.md` | Worker 系统提示词（含 11 工具 + 规则） |
| `sar_orch/prompts/coordinator/system.md` | Coordinator 系统提示词（含派活策略） |

## 启动时序

```python
# experiment.py:run_experiment()
# 1. 创建 Barrier
barrier = SARBarrier(num_agents=N, scene=S, seed=SEED)
max_steps = max_steps or barrier.env.task_timeout

# 2. 创建 Logger
exp_logger = ExperimentLogger(experiment_name="sar_experiment")

# 3. 创建 Coordinator (先启动, 绑定端口)
coordinator = SARCoordinator(port=8080, a2a_port=8081, barrier=..., ...)
coord_task = asyncio.create_task(coordinator.start())
await asyncio.sleep(3.0)  # 等待端口绑定

# 4. 创建 Workers (后启动, 连 Coordinator WS)
for name in ["Alice", "Bob", ...]:
    worker = SARWorker(worker_id=name, a2a_port=8191+i, ...)
    worker.start()           # 启动 daemon thread
await asyncio.sleep(2.0)    # 等待注册

# 5. 提交任务 (fire-and-forget, 不阻塞 poll)
a2a_task = asyncio.create_task(coordinator.submit_task("Extinguish all fires..."))

# 6. Poll loop (立即开始, 与 A2A 并行)
while not barrier.is_finished() and steps < max_steps:
    await asyncio.sleep(2.0)
    metrics = barrier.get_metrics()
    exp_logger.log_step(...)     # trajectory.csv
    exp_logger.flush_summary()   # summary.csv 增量写入
```

### 关键规则

- **Coordinator 必须先启动**：Worker 在 `start()` 中通过 WebSocket 连接 Coordinator，连接失败则抛异常
- **Worker ID 必须匹配 agent 名称**：如 `"Alice"`、`"Bob"`，Coordinator 按名称 dispatch
- **每步 poll 2s**：不阻塞，与 A2A LLM 编排并行
- **poll 超时基于步数**：`steps < max_steps`，非 wall-clock（与原 LLaMAR 论文对齐）

## 每步执行

```
      Worker LLM                Barrier                    SAREnv
     ┌──────────┐            ┌───────────┐            ┌──────────────┐
     │ 收到subtask│            │           │            │              │
     │ 调tool    │──action──> │submit_action│──queue──> │              │
     │          │            │  全收集齐?  │            │              │
     │          │            │  yes↓      │            │              │
     │          │            │_execute_step│──actions──>│  env.step()  │
     │          │<──obs──── │  广播obs   │<──obs───── │              │
     │  继续LLM  │            │           │            │              │
     └──────────┘            └───────────┘            └──────────────┘
```

### Barrier 同步机制

1. Worker tool 调用 `barrier.submit_action(agent_idx, action)`
2. action 入队，检查是否全部 N 个 agent 已提交
3. 全部到齐 → 立即执行 `_execute_step()`
4. 未到齐 → 等待 `asyncio.Event`，**15s 超时** → 未提交者自动填充 `NoOp` 后执行
5. `_execute_step()` 调 `env.step(actions)`（同步，`asyncio.to_thread`）→ 生成 observations → 广播给所有 agent

```python
# barrier.py 核心
async def submit_action(self, agent_idx: int, action: str) -> dict:
    async with self._step_lock:
        self._action_queue[agent_idx] = action
        all_submitted = len(self._action_queue) == self.num_agents
    if all_submitted:
        await self._execute_step()
    else:
        await asyncio.wait_for(self._obs_events[agent_idx].wait(), timeout=STEP_TIMEOUT)
    return {"observation": obs_text, "finished": self._finished, ...}

async def _execute_step(self):
    actions = [self._action_queue[i] for i in range(self.num_agents)]
    obs_text, act_successes = await asyncio.to_thread(self.env.step, actions)
    for i in range(self.num_agents):
        self._current_obs[i] = f"{obs}{state}"
        self._obs_events[i].set()
    self._step_counter += 1
    self._finished = self.env.checker.check_success()
```

### 工具执行 vs 步进

| 类型 | 工具 | 调用 `submit_action`? | 消耗 step |
|------|------|-----------------------|-----------|
| 步进工具 | `navigate_to`, `move`, `explore`, `get_supply`, `use_supply`, `carry_person`, `drop_off_person`, `store_supply`, `clear_inventory`, `no_op` | ✅ 是 | ✅ 是 |
| GPS 工具 | `get_agent_state` | ❌ 否（直接读 `env.controller`） | ❌ 否 |

### Coordinator 编排

**RouterAgent**（LLM Agent）每轮执行：

1. 调 `query_sar_state` → 获取全局环境快照（fires/persons/reservoirs/agents 位置和状态）
2. LLM 推理 → 决定派活对象和任务
3. 调 `dispatch_task(agent_id, prompt)` → 通过 A2A JSON-RPC 发给 Worker 执行
4. 调 `collect_results` → 等待 Worker 返回
5. 重复直到任务全部完成或达到 `router_max_steps=20`

**关键设计**：
- `extra_tools=[QuerySARStateTool(barrier=...)]` 运行时注入，绕过 `tools_dir` 静态加载限制
- `orchestration_timeout=1200s`（必须 >= barrier `task_timeout`）
- `max_tasks_per_run=50`（每次 dispatch 可含多个子任务）

### Worker 执行

**WorkerAgent**（LLM Agent）收到 subtask 后：

1. LLM 解析 subtask → 选择 SAR 工具
2. 调工具 → `tool.execute()` → `barrier.submit_action()` → 阻塞等待 barrier 步进
3. 收到 observation → LLM 判断下一步
4. 重复直到 subtask 完成或 `max_steps=50`

**关键设计**：
- `include_base_tools=False`：彻底移除 bash/read_file/write_file，不从 prompt 层面约束
- `CoordinatorWebSocketClient`：连 Coordinator，接收 `dispatch_task` 消息
- step_callback 是 **sync 函数**：`AgentAdapter._step_handler` 不 await 外部回调

## 日志链路

### 5 个 CSV 文件

| CSV | 写入时机 | 写入者 | 保护 |
|-----|---------|--------|------|
| `trajectory.csv` | Poll loop 每检测到新 step | `experiment.py` | 去重（`_last_step_logged`） |
| `agent_interactions.csv` | Worker `tool_start` + `tool_result` 配对触发 | `SARWorker._step_callback` | `threading.Lock` |
| `router_interactions.csv` | Coordinator `dispatch_task` tool_start | `SARCoordinator._router_cb` | `threading.Lock` |
| `token_usage.csv` | 每次 LLM response（Worker + Coordinator） | 两端的 `llm_response` callback | `threading.Lock` |
| `summary.csv` | 每 poll step 增量覆盖 + 结束 `close()` | `experiment.py` | 覆盖写入，不怕 kill |

### 写入路径

```
Worker LLM response ──→ _step_callback("llm_response") ──→ log_token_usage()
Worker tool_start   ──→ _step_callback("tool_start")   ──→ 缓存 pending_tool
Worker tool_result  ──→ _step_callback("tool_result")  ──→ log_agent_interaction()
                                                              (合并 tool_name/args + obs + llm_output)

Coord LLM response  ──→ _router_cb("llm_response")     ──→ log_token_usage()
Coord dispatch_task  ──→ _router_cb("tool_start")       ──→ log_router_interaction()
                              (提取 prompt + agent_id)

Poll loop           ──→ barrier.get_metrics()           ──→ log_step()
                     ──→                                 ──→ flush_summary()
```

### 线程安全

- `ExperimentLogger` 所有 `log_*` 方法都用 `with self._lock:`（`threading.Lock`）
- 多个 Worker 的 step_callback 可能同时写日志 → Lock 保证行不交错

## 清理时序

```python
finally:
    # 1. 停 Workers
    for worker in workers.values():
        worker.stop()          # server.should_exit=True + stop_event.set() + thread.join(10s)

    # 2. 停 Coordinator
    await coordinator.stop()   # server.shutdown()

    # 3. 停 Barrier
    barrier.stop()             # env.stop()

    # 4. 关 Logger
    exp_logger.close()         # flush_summary() + 关文件句柄
```

### 异常兜底

| 异常场景 | 兜底措施 |
|---------|---------|
| Shell timeout (SIGTERM) | `finally` 不执行 → 但 `flush_summary()` 已每 step 写入，`summary.csv` 有截至上一完整 step 的数据 |
| Worker/Coodinator 崩溃 | poll loop 检测 `coord_task.done()` 或 `a2a_task.done()` 并记录错误 |
| A2A 任务未完成但 barrier 已结束 | `a2a_task.cancel()` 清理孤儿协程 |
| Benchmark 超时 | 子进程 `proc.kill()`，`run_metrics.json` 由子进程正常退出时写入 |

## Benchmark 流程

```
benchmark.py (入口)
  │
  ├─ 构建 100 个 BenchmarkRun (5 scenes × 4 agent counts × 5 seeds)
  ├─ Semaphore(2) 控制并发
  │
  ├─ run_single():
  │   ├─ 分配 port_offset (0/1) → coordinator_port=8080+offset*10, agent_base=8191+offset*10
  │   ├─ 写 meta.json
  │   └─ asyncio.create_subprocess_exec() 启动 experiment.py 子进程
  │       ├─ env: 清除 http_proxy, 设置 no_proxy, PYTHONPATH
  │       ├─ await proc.communicate(timeout=run_timeout)
  │       ├─ 超时 → proc.kill()
  │       └─ 读 run_metrics.json → 写 result.json
  │
  ├─ retry 机制: 失败轮次自动重试 N 次
  └─ 写 index.json (全量运行索引)
```

### 子进程隔离

- **问题**：线程内 uvicorn daemon 不释放端口，后续实验绑定失败
- **方案**：每个实验启动独立子进程，进程退出时所有端口自动释放
- **代理问题**：子进程继承 `http_proxy`，WebSocket 连 localhost 被拦截
- **解决**：子进程 env 中清除 `http_proxy`/`https_proxy`/`HTTP_PROXY`/`HTTPS_PROXY`

## 关键设计

### ADR-003: `extra_tools` 运行时注入

`QuerySARStateTool` 需要 `barrier` 实例（运行时才能创建），不能用 `tools_dir` 静态加载。
框架为 `create_server()` / `RouterAgent.__init__()` 新增 `extra_tools` 参数。

### ADR-005: `step_callback` 日志接入

Worker 端 `step_callback` 捕获 `tool_start` + `tool_result`，配对后写入 `agent_interactions.csv`。
Coordinator 端 `router_step_callback` 捕获 `dispatch_task` 的 `tool_start`，写入 `router_interactions.csv`。

### ADR-006: `fire-and-forget` 任务提交

`submit_task()` 用 `asyncio.create_task()` 后台运行，poll loop 立即执行，不阻塞。
优化前：A2A 编排除结束后 poll loop 才开始，CSV 丢失。
优化后：poll loop 与 A2A 并行，CSV 每步实时写入。

### ADR-009: 增量 `flush_summary`

`summary.csv` 每 poll step 后覆盖写入，即使 shell timeout 杀死进程也不丢数据。
优化前：只在 `close()` 中写入，异常退出时丢失。

## 端口分配

| 用途 | 偏移 0 | 偏移 1 | ... |
|------|--------|--------|-----|
| Coordinator HTTP/A2A | 8080/8081 | 8090/8091 | +10 |
| Alice | 8191 | 8201 | +10 |
| Bob | 8192 | 8202 | +10 |
| Charlie | 8193 | 8203 | +10 |
| David | 8194 | 8204 | +10 |
| Emma | 8195 | 8205 | +10 |
| Finn | 8196 | 8206 | +10 |
