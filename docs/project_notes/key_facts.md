# Key Facts

Project configuration, ports, URLs, and conventions.

## Ports

| Service | Port | Description |
|---------|------|-------------|
| SAR Coordinator REST/WS | 8080 | HTTP API + WebSocket + UI |
| SAR Coordinator A2A | 8081 | A2A JSON-RPC |
| SAR Worker Alice | 8191 | Worker A2A Server |
| SAR Worker Bob | 8192 | Worker A2A Server |
| SAR Worker Charlie | 8193 | Worker A2A Server |
| SAR Worker David | 8194 | Worker A2A Server |
| SAR Worker Emma | 8195 | Worker A2A Server |
| SAR Worker Finn | 8196 | Worker A2A Server |
| Benchmark offset 0 coordinator | 8080 | Benchmark concurrent run 0 |
| Benchmark offset 0 A2A | 8081 | Benchmark concurrent run 0 |
| Benchmark offset 0 Alice | 8191 | Benchmark concurrent run 0 |
| Benchmark offset 0 Bob | 8192 | Benchmark concurrent run 0 |
| Benchmark offset 1 coordinator | 8090 | Benchmark concurrent run 1 |
| Benchmark offset 1 A2A | 8091 | Benchmark concurrent run 1 |
| Benchmark offset 1 Alice | 8201 | Benchmark concurrent run 1 |
| Benchmark offset 1 Bob | 8202 | Benchmark concurrent run 1 |

## UI Endpoints

| URL | Description |
|-----|-------------|
| `http://localhost:8080/ui` | Task Console |
| `http://localhost:8080/ui/debug` | Debug Viewer |
| `http://localhost:8080/ui/map` | **SAR Real-time Map** (SSE auto-refresh) |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/a2a/jsonrpc` | POST | 外部 A2A 客户端入口 |
| `/api/v1/jsonrpc/` | POST | 内部 A2A SDK 通信 |
| `/logs/{task_id}/stream` | GET | SSE 实时日志流 |
| `/map/state` | GET | SSE 实时地图状态流 |

## Environment

| Variable | Value |
|----------|-------|
| `.env` file | Project root, lowercase keys |
| `PYTHONPATH` | Must include `src:` |
| `no_proxy` | `localhost,0.0.0.0,127.0.0.1` |
| Provider | `openai` (DeepSeek API) |
| Model | `deepseek-v4-flash` |
| API Base | `https://api.deepseek.com` |

## Key Directories

| Path | Purpose |
|------|---------|
| `src/a2a/` | my_a2a framework (coordinator + worker + builtin_tools) |
| `src/Agent/` | Mini-Agent framework (router_agent + worker_agent, two copies) |
| `src/Agent/{worker_agent,router_agent}/context.py` | ContextManager (三层记忆策略 none/summary/hybrid) |
| `src/Agent/{worker_agent,router_agent}/hooks.py` | LLM 调用前后钩子 (记录/摘要触发) |
| `sar_orch/tools/{worker,coordinator}/finish_task.py` | finish_task 工具 (显式子任务结束通知) |
| `sar_orch/` | SAR orchestration layer (barrier, coordinator, worker, tools, prompts, experiment) |
| `SAR/` | LLaMAR SAR environment engine (core.py, env.py) |
| `integration/` | Old MARoS integration (reference, not used) |
| `logs/agent/` | Agent run logs (sar_coordinator/ + sar_worker/<worker-id>/) |
| `sar_orch/results/` | 新编排系统的 Experiment CSV logs (see below) |
| `results/` | **原始 LLaMAR 论文实验结果**（TSV 格式基线对比） |

## 新编排系统实验 CSV（sar_orch/results/）

Each experiment creates a timestamped directory `sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS/` with:

| CSV | Source | Fields | Status |
|-----|--------|--------|--------|
| `trajectory.csv` | `experiment.py` poll loop | Step, Actions, Successes, Observations, Coverage, TransportRate, Finished | ✅ 实时写入 |
| `agent_interactions.csv` | Worker `step_callback` | Step, Agent, ToolName, ToolArgs, Action, Observation, LLMOutput | ✅ 实时写入 |
| `router_interactions.csv` | Coordinator `router_step_callback` | Step, Subtask, AssignedTo | ✅ 实时写入 |
| `token_usage.csv` | Worker/Coordinator `llm_response` callback | Step, Agent, PromptTokens, CompletionTokens, TotalTokens | ✅ 实时写入 |
| `summary.csv` | `exp_logger.flush_summary()` + `close()` | ExperimentName, TotalSteps, FinalCoverage, FinalTransportRate, Finished, `{Agent}PromptTokens`, `{Agent}CompletionTokens`, `{Agent}TotalTokens` | ✅ 每步增量写入 |

## 原始 LLaMAR 论文实验结果（results/）

TSV 格式基线对比，每行一次运行：

| 文件 | 方法 | 列 |
|------|------|----|
| `results/llamar_1.tsv` | LLaMAR | steps, balance, coverage, success rate, transport rate |
| `results/llamar_2.tsv` | LLaMAR variant | 同上 |
| `results/llamar_3.tsv` | LLaMAR variant | 同上 |
| `results/llamar_text.tsv` | LLaMAR (text-only) | 同上 |
| `results/act.tsv` | ACT baseline | 同上 |
| `results/coela.tsv` | CoELA baseline | 同上 |
| `results/cot.tsv` | Chain-of-Thought baseline | 同上 |
| `results/react.tsv` | ReAct baseline | 同上 |

运行入口：`SAR/baselines/llamar.py`，配置文件：`meta/llamar.sh`

## Semantic Map Architecture

The semantic map provides a partial-observability layer between the SAR environment and the Coordinator LLM:

```
Worker ReportObservationTool
  → A2AWorkerSink.emit("tool_result")       # [DATA] block + full JSON (limit 12000)
  → TaskStatusUpdateEvent (A2A push)         # via push notification
  → _extract_observation_from_status_text()  # parse [DATA] blocks
  → SemanticMapStore.ingest_observation()    # merge + persist to JSONL
```

| Component | File | Role |
|-----------|------|------|
| `SemanticMapStore` | `sar_orch/semantic_map.py` | Thread-safe in-memory store with conflict/staleness detection |
| `ObservationRecord` | `sar_orch/semantic_map.py` | Data model for incoming observations |
| `SemanticObject` | `sar_orch/semantic_map.py` | Merged view of an object (fire/person/reservoir/deposit) |
| `AgentSemanticState` | `sar_orch/semantic_map.py` | Per-agent tracked state (position, inventory, task) |
| `ReportObservationTool` | `sar_orch/tools/worker/report_observation.py` | Worker-side tool (non-blocking, 0-step cost) |
| `QuerySharedMemoryTool` | `sar_orch/tools/worker/query_shared_memory.py` | Worker HTTP query to `/semantic-map` endpoint |
| `QuerySemanticMapTool` | `sar_orch/tools/coordinator/query_semantic_map.py` | Coordinator snapshot query |
| `QueryTeamStatusTool` | `sar_orch/tools/coordinator/query_team_status.py` | Coordinator team status query |
| `set_semantic_map()` | `src/a2a/coordinator/server.py:199` | Injects SemanticMapStore into coordinator server |
| `/semantic-map` endpoint | `src/a2a/coordinator/server.py:491` | HTTP GET → store snapshot JSON |

## Coordinator Tools

| Tool | Modes | Description |
|------|-------|-------------|
| `query_semantic_map` | semantic only | Full semantic map snapshot (fires, persons, agents, step_budget) |
| `query_team_status` | semantic only | Team status summary (agents, recent_obs, stale, conflicts) |
| `query_sar_state` | oracle only | Direct environment oracle read (grid, fires, persons) |
| `dispatch_task` | both | Assign task to a worker agent |
| `query_task_events` | both | Poll worker task statuses (RUNNING/COMPLETED/FAILED/CANCELED/INPUT_REQUIRED) |
| `respond_worker` | both | Reply to INPUT_REQUIRED worker |
| `finish_task` | both | Mark mission as complete |
| `cancel_task` | both | Cancel a running worker task |

## Skills Directory

`skills/` contains reusable, self-contained skills with their own `SKILL.md`:

| Skill | Path | Purpose |
|-------|------|---------|
| render-sar-report | `skills/render-sar-report/` | Generate human-readable HTML report from experiment CSV/JSON/NDJSON outputs |

Usage:
```bash
PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
  uv run python -m render_sar_report.cli \
  --results-dir sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS \
  --logs-dir logs
```

## CLI Commands

```bash
# SAR Experiment
cd /home/wyh/daily_work/LLaMAR
# SAR Experiment (semantic mode — default)
cd /home/wyh/daily_work/LLaMAR-sematic_map
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42

# SAR Experiment (oracle mode)
cd /home/wyh/daily_work/LLaMAR-sematic_map
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --mode oracle

# SAR Benchmark (full sweep)
cd /home/wyh/daily_work/LLaMAR-sematic_map
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 600 --mode semantic

# Aggregate results
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/aggregate.py

# Standalone Coordinator
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run a2a --host 0.0.0.0 --port 8080 --a2a-port 8081 --env-file .env --no-verifier

# Standalone Worker
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run a2a-worker --worker-id worker-1 --coordinator-url ws://localhost:8080 \
  --a2a-host 0.0.0.0 --a2a-port 8090 --capabilities "python,web-browsing" --env-file .env

# Lint
uv run --with ruff ruff check src/ sar_orch/
uv run --with ruff ruff format src/ sar_orch/
```

## Architecture

```
experiment.py
    ├─ SARBarrier → SAREnv (grid world)
    ├─ SARCoordinator → CoordinatorServer (FastAPI:8080 + A2A:8081)
    │   └─ RouterAgent + QuerySARStateTool(barrier)
    └─ SARWorker × N → A2A Server (8191~8196)
        └─ WorkerAgent + 10 SAR tools (navigate_to ~ no_op)

Worker tools → submit_action() → barrier → env.step() → broadcast observations
Coordinator → query_sar_state → dispatch_task → worker A2A → execute → collect_results

## Worker Tools List

| Tool | SAR Action | Consumes Step | Notes |
|------|-----------|---------------|-------|
| `get_agent_state()` | — | ❌ No | GPS: reads position/inventory/obs directly from barrier |
| `navigate_to(target_id)` | `NavigateTo` | ✅ Yes | Teleport. Returns `"Arrived at X. Position: (x,y,z)."` |
| `move(direction)` | `Move` | ✅ Yes | One-step grid movement |
| `explore()` | `Explore` | ✅ Yes | Random multi-step exploration |
| `get_supply(source_id, type)` | `GetSupply` | ✅ Yes | Collect Water/Sand from reservoir/deposit |
| `use_supply(fire_id, type)` | `UseSupply` | ✅ Yes | Use carried supply on a fire |
| `carry_person(person_id)` | `Carry` | ✅ Yes | Pick up person (needs 2+ agents) |
| `drop_off_person(person_id, deposit_id)` | `DropOff` | ✅ Yes | Drop person at deposit |
| `store_supply(deposit_id)` | `StoreSupply` | ✅ Yes | Store carried supplies |
| `clear_inventory()` | `ClearInventory` | ✅ Yes | Drop all carried items |
| `no_op()` | `NoOp` | ✅ Yes | Do nothing

## env.step() 反馈数据结构

`env.step(actions)` 返回 `(input_dict_string, act_successes_list)`。

### env.input_dict 全部字段（每步更新）

| 字段 | 内容 | 示例 |
|------|------|------|
| `Task` | 全局任务描述 | `"Using the appropriate resources, extinguish all the fires: ..."` |
| `{name}'s observation` | 局部+全局观测 | `Directly around me: {Left: [...], ...}  Globally: [...]  Names: [...]` |
| `{name}'s state` | 位置+库存 | `"I am at co-ordinates: (22, 18) and I am holding {'Sand': 0, 'Water': 1, 'Person': 0}."` |
| `{name}'s previous action` | 上一步动作结果（含error_type文本） | `"I tried to use water on GreatFire and was successful."` |
| `{name}'s previous failures` | 历史失败累积列表 | `"Previously, I have tried to navigate to ..., but was unsuccessful"` 或 `"None"` |
| `{name}'s previous observation` | 上一步的Names列表（仅名字） | `"['GreatFire', 'LostTimmy', ...]"` |
| `Robots' subtasks` | 当前子任务列表 | checker 自动生成 |
| `Robots' combined memory` | 共享记忆 | |
| `Robots' open subtasks` | 未完成子任务 | |
| `Robots' completed subtasks` | 已完成子任务 | |

### controller.event（raw_step 每动作返回）

```python
event = {
    'success': True/False,
    'error_type': '',     # 'not_visible' | 'not_interactable' | 'restricted_action' | ''
    'info': '',           # 详细失败原因（get_act_text 未使用，只用作 debug）
    'global_obs': [...],  # 全局可见物体列表
    'local_obs': {...},   # 周围 9 方向物体
}
```

### error_type → get_act_text() 映射到 LLM 提示文本

| error_type | LLM 看到的文本 |
|-----------|---------------|
| `""` | `"I tried to {action} and was not successful."` |
| `"not_visible"` | `"I tried to {action} but it wasn't visible and was not successful."` |
| `"not_interactable"` | `"I tried to {action} but I wasn't close enough and was not successful."` |
| `"restricted_action"` | `"I tried to {action} but I was already holding person and was not successful."` |

注意：`ToolResult(success=True)` 是**传输层确认**与动作成败无关；实际成功/失败只通过 `previous action` 文本反馈。

### checker 指标

| 方法 | 返回 |
|------|------|
| `get_coverage()` | `0.0~1.0`（已检查区域占比） |
| `get_transport_rate()` | `0.0~1.0`（已救人占比） |
| `check_success()` | `True/False`（全部子任务完成） |

### barrier 现有 API（可被 Coordinator/Worker 调用的）

| 方法 | 返回 |
|------|------|
| `submit_action()` | `{observation, agent_name, step, finished, success=True}` |
| `get_metrics()` | `{coverage, transport_rate, steps, finished}` |
| `get_env_snapshot()` | 全环境对象分类字典（坐标/类型/库存/强度） |
| `get_current_obs(agent_idx)` | 最新完整 observation 文本 |
| `get_last_step_log()` | `{actions, successes, observations, timeout_agents}` |

### barrier 同步原语（2026-07-01 变更）

- **使用 `threading.Event` + `threading.Lock`**（非 asyncio 版本）
- 原因: worker 在独立线程运行独立 asyncio 事件循环，asyncio 原语跨线程不安全
- `_execute_step` 是 sync 函数，通过 `asyncio.to_thread()` 调用
- `expected_step` 参数防止多 agent 同时超时导致重复执行
- `stop()` 设 `_stopped=True` + 全部 `event.set()` 唤醒等待中的 worker

### experiment.py 安全机制

| 机制 | 值 | 说明 |
|------|-----|------|
| wall-clock 超时 | 600s | 单次实验硬上限，防止 barrier 超时空转 |
| poll_interval | 2.0s | 轮询 barrier 步进的间隔 |
| a2a_task.done() 退出 | ✓ | coordinator 编排结束后立即退出 poll loop |
| STEP_TIMEOUT | 60s | barrier 等待单个 agent 提交的超时 |

### trajectory.csv 列（2026-07-01 更新）

| 列 | 说明 |
|----|------|
| Step | 步号 |
| Actions | 各 agent 的动作字符串列表 |
| Successes | 各 agent 动作成功标志列表 |
| Observations | 各 agent 的 observation 文本 |
| Coverage | 探索覆盖率 0.0~1.0 |
| TransportRate | 运输率 0.0~1.0 |
| Finished | 任务是否完成 |
| **TimeoutAgents** | **被系统超时填充 NoOp 的 agent 索引列表（[] 表示全部正常提交）** |

### query_sar_state 返回（2026-07-01 更新）

除了环境快照外，现在还返回:
- `step`: 当前步数
- `max_steps`: 步数预算
- `finished`: 任务是否完成

### no_op 工具返回（2026-07-01 更新）

除了 observation 文本外，现在还返回:
- `[MISSION COMPLETE]` 后缀 → worker 应返回成功摘要
- `[Step N] Mission in progress` 后缀 → worker 应继续 no_op
- 连续 5 次 no_op 后 worker 强制返回（让 coordinator 重新规划）
