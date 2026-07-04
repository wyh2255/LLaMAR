# AGENTS.md

## SAR Experiment (my_a2a Framework)

```bash
cd /home/wyh/daily_work/LLaMAR
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42
```

Options:
- `--scene` 1-5 (SAR scene number)
- `--agents` 1-6 (number of rescue robots)
- `--seed` random seed
- `--model` LLM model (default: deepseek-v4-flash)
- `--provider` LLM provider (default: openai)
- `--api-base` API base URL (default: https://api.deepseek.com)
- `--max-steps` override max environment steps (default: scene's task_timeout)
- `--sandbox-profile` `off|workspace` (default: workspace; `off` disables path sandboxing)

## SAR Benchmark (full sweep)

```bash
cd /home/wyh/daily_work/LLaMAR
# Run all 100 combinations (5 scenes × 4 agent counts × 5 seeds)
# --run-timeout 600s prevents stuck runs from blocking progress
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 600

# After completion, aggregate results:
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/aggregate.py
```

### Monitor progress
```bash
bash sar_orch/check_benchmark.sh               # Quick summary
tail -f sar_orch/results/benchmark_nohup.log   # Live log
cat sar_orch/results/benchmark_monitor.log      # 10-min auto-check log

# Auto-monitor (runs every 10 min, auto-restarts on crash):
bash sar_orch/monitor_benchmark.sh &
```

## A2A Framework (standalone)

```bash
# Coordinator
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run a2a --host 0.0.0.0 --port 8080 --a2a-port 8081 \
  --env-file .env --no-verifier

# Worker
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run a2a-worker --worker-id worker-1 --coordinator-url ws://localhost:8080 \
  --a2a-host 0.0.0.0 --a2a-port 8090 --capabilities "python,web-browsing" \
  --env-file .env
```

## Lint

```bash
uv run --with ruff ruff check src/ sar_orch/
uv run --with ruff ruff format src/ sar_orch/
```

## Output Files

Every experiment run creates a directory under `sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS/`:

| CSV | Content |
|-----|---------|
| `trajectory.csv` | Per-step metrics (coverage, transport rate, actions) |
| `agent_interactions.csv` | Per-agent tool calls with args, observation, LLM output |
| `router_interactions.csv` | Coordinator subtask dispatch history |
| `token_usage.csv` | **Each LLM call** — Step, Agent, PromptTokens, CompletionTokens, TotalTokens |
| `summary.csv` | Aggregate metrics + **per-agent cumulative token totals** (updated each step) |

## UI — Coordinator Web Console

Experiment running → open in browser:

| URL | Page | Description |
|-----|------|-------------|
| `http://localhost:8080/ui` | Task Console | Submit tasks, view task cards |
| `http://localhost:8080/ui/debug` | Debug Viewer | Task execution logs (NDJSON) |
| `http://localhost:8080/ui/map` | SAR Map | **Real-time grid map** with SSE auto-refresh |

Map UI features (`src/a2a/coordinator/ui/map.html`):
- Colored grid table: fire intensity (beige→orange→red), agent (pink), reservoir (blue), deposit (black), person (purple)
- Sidebar: step counter, coverage/transport metrics, agent inventory, fire/person details
- SSE endpoint `/map/state` pushes grid JSON every 500ms from `barrier.get_env_snapshot()`
- Navigation bar switches between Tasks / Debug / Map

Server integration (`src/a2a/coordinator/server.py`):
- `server.set_barrier(barrier)` — inject SARBarrier reference before `server.run()`
- `/map/state` SSE — self-contained; returns `{step, finished, coverage, transport_rate, agents, fires, persons, snapshot}`

## Key Gotchas

- **PYTHONPATH**: Always include `src:` for a2a and Agent imports
- **no_proxy**: Always set `no_proxy="localhost,0.0.0.0,127.0.0.1"` to bypass Privoxy
- **.env fields must be lowercase**: `provider`, `api_key`, `api_base`, `model`
- **Tool execute() returns ToolResult**: Not plain str — Agent framework checks `.success`
- **Startup order**: Coordinator MUST start before Workers (WebSocket connection)
- **Worker ID**: Must match agent names (e.g., "Alice") for AgentRegistry dispatch
- **Coordinator extra_tools**: Use `extra_tools` parameter (not `tools_dir`) for tools that need runtime dependencies like barrier
- **Worker prompt**: Must explicitly prohibit bash/file tools for domain-specific agents
- **Map UI requires barrier**: Call `server.set_barrier(barrier)` after `create_server()` before `server.run()`, otherwise `/ui/map` shows "Waiting for SARBarrier..."
- **Map grid serialization**: `get_env_snapshot()` returns Coordinate objects; SSE endpoint serializes via `json.dumps(snapshot, default=lambda o: o.get())` to produce `[x,y,z]` lists
- **Flammable NONE cells**: SAR grid has many Flammable cells with intensity=NONE (undiscovered fires); map renders them as white
- **NavigateTo is teleport**: SAR `GridEngine.move_object()` does direct `set_position()` — one call is enough. Tool now returns `"Arrived at X. Position: (x,y,z)."` to avoid LLM confusion.
- **get_agent_state (GPS)**: Zero-cost state query. Does NOT call `submit_action()`, does NOT consume a step. Use when agent needs to confirm position/inventory.
- **get_position() returns tuple**: `(x, y, z)` tuple, NOT a Coordinate object. Use `pos[0]`/`pos[1]`/`pos[2]` not `pos.x`/`pos.y`/`pos.z`.
- **120s timeout tight**: Scene 1 has 120s task_timeout. With 2 agents + A2A LLM overhead, ~9-16 steps fit. Agents can explore fully but rarely reach firefighting phase.
- **Token tracking**: `Agent` class (both copies) has `api_prompt_tokens`, `api_completion_tokens`, `cumulative_total_tokens` etc. Modified fields: `__init__` (6 new fields) + `run()` (store usage) + `_create_summary()` (track summarization tokens). Sync changes between `worker_agent/` and `router_agent/`.
- **Thread safety**: `ExperimentLogger` uses `threading.Lock` — all `log_*()` methods wrapped with `with self._lock:`.
- **Crash-safe summary**: `flush_summary()` called after each poll step; `summary.csv` always has latest data even if process is killed.
- **Coordinator agent name**: Token logged as `"Coordinator"` (hardcoded in `sar_orch/coordinator.py` `_router_cb`).
- **Benchmark port conflicts**: `run_experiment()` now accepts `coordinator_port` and `agent_base_port`; benchmark uses offset=0→8080/8191, offset=1→8090/8201, etc.
- **Benchmark run timeout**: Always use `--run-timeout 600` (or higher) to prevent stuck runs from blocking the entire benchmark. Without this, a single stuck agent loop can stall all remaining runs indefinitely.
- **Benchmark log dirs**: Use `--run-timeout` and explicit `log_dir` to prevent two concurrent runs from writing to the same timestamped log directory.
- **Barrier uses threading primitives (NOT asyncio)**: `SARBarrier` uses `threading.Event`/`threading.Lock` because workers run in separate threads with separate asyncio event loops. asyncio.Event.set() uses `loop.call_soon()` (not `call_soon_threadsafe`) — waiters in other threads never wake. See ADR-011.
- **TimeoutAgents in trajectory.csv**: `TimeoutAgents` column lists agent indices that were auto-filled with NoOp due to barrier timeout. `[]` means all agents submitted normally. Use this to filter system-injected NoOps from LLM-chosen NoOps during prompt analysis.
- **Wall-clock 600s limit**: Single experiments have a 600s wall-clock safety net in the poll loop. Adjust `wall_clock_limit` in `experiment.py` if longer runs are needed.
- **query_sar_state returns step info**: Snapshot now includes `step`, `max_steps`, `finished` — coordinator can make step-budget-aware decisions.
- **Worker auto-NoOp**: `no_op` tool returns `[MISSION COMPLETE]` or `[Step N] Mission in progress`. Workers auto-no_op after main task (5-cap then return). Coordinator doesn't need to pad tasks with NoOp but should still give longest useful chains.
- **Poll loop exits on a2a_task.done()**: When coordinator orchestration completes (normally or max_steps), the poll loop breaks immediately — no more 60s-per-step idle spinning.
- **barrier.stop() wakes workers**: `stop()` sets `_stopped=True` + all `event.set()` — waiting workers unblock and return immediately.
## System Documentation

Comprehensive architecture docs at `docs/system_docs/`:
- [`docs/system_docs/框架.md`](docs/system_docs/框架.md) — Framework overview: A2A transport, Agent/AgentLang kernels, SAR orchestration
- [`docs/system_docs/data_flow.md`](docs/system_docs/data_flow.md) — Full data flow tracing context_id / task_id / query end-to-end
- [`docs/system_docs/logging_map.md`](docs/system_docs/logging_map.md) — Complete logging system: every record point, trigger, fields, files

- **SARCoordinator.submit_task uses A2A SDK Client**: `sar_orch/coordinator.py` now uses `create_client()` + `client.send_message()` instead of raw HTTP JSON-RPC POST. Uses protobuf types (`SendMessageRequest`, `Message`, `Part`, `Role`) from `a2a.types.a2a_pb2`. Responses are serialized via `MessageToDict`.
