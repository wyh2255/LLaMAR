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
| `summary.csv` | `exp_logger.close()` | ExperimentName, TotalSteps, FinalCoverage, FinalTransportRate, Finished | ✅ 实验结束时写入 |

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

## CLI Commands

```bash
# SAR Experiment
cd /home/wyh/daily_work/LLaMAR
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42

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
```
