# AGENTS.md

## SAR Experiment

```bash
cd "$(git rev-parse --show-toplevel)"
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
- `--max-steps` override max environment steps (default: 50)
- `--mode` `semantic|oracle` (default: semantic; semantic hides oracle truth from coordinator)
- `--sandbox-profile` `off|workspace` (default: workspace; `off` disables path sandboxing)
- `--memory-read-mode` `legacy|shadow|read_port` (default: `read_port` since H3 retirement 2026-08-10; canonical Memory is the official path. `legacy` retained as rollback target; experiment auto-generates the per-run callback secret for `shadow|read_port`)
- `--long-term-mode` `off|shadow|read` (default: `off`; run-local long-term memory. **`read` officially available since G4 2026-08-12**: injects published-only long-term memories as a budgeted `### Long-term Memory` section into coordinator-only Context; `shadow` persists without injecting; workers NEVER see the section or `long_term_revision`. Requires `reflection_*` or generic `.env` model keys; `memory_read_mode=shadow` + `long_term_mode=read` combo is fail-closed)
- System Health 诊断通道（agentic 审查者，**P5 正式可用 2026-08-16**；非独立 CLI 参数）：随 `--long-term-mode` 取 `shadow|read`（即 `long-term-mode != off`）自动接线，无需单独开关。诊断循环（DiagnosisLoop，max_rounds=3 / diagnosis_sec=150（当前 `long_term.config` 运行配置；配置缺失时代码默认 90），四件只读工具 `query_projection` / `temporal_flow` / `supervision` / `control_journal`）随 rolling 触发（每 5 步）；terminal 不另起诊断循环，产出 coordinator-only `### System Health` 段注入（worker 永不可见）；开关与配置在 `long_term.config` 的 `[diagnosis]` 段：`inject_enabled`（默认 true，独立消融旋钮 A2）/ `min_confidence`（默认 0.6）/ `max_rounds` / `diagnosis_sec` / `section_budget_threshold`（默认 3，R3-2 修订）。D8 语义：增强非必需、绝不阻塞（超时丢弃、fail-closed）。验收证据见 Key Gotchas。

## SAR Benchmark (full sweep)

```bash
cd "$(git rev-parse --show-toplevel)"
# Run all 100 combinations (5 scenes × 4 agent counts × 5 seeds)
# --run-timeout 600s prevents stuck runs from blocking progress
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 600

# Filter by specific scene(s):
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 600 --scene 5

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

## Render Human-Readable HTML Report

After a run completes, render the CSV/JSON/NDJSON outputs into a single self-contained HTML report:

```bash
cd "$(git rev-parse --show-toplevel)"
PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
  uv run python -m render_sar_report.cli \
  --results-dir sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS \
  --logs-dir logs
```

Default output: `<results_dir>/report.html`.

The report includes: run overview, per-step timeline, coordinator decisions, token usage charts, semantic map evolution, and full LLM traces.

## UI — Coordinator Web Console

Experiment running → open in browser:

| URL | Page | Description |
|-----|------|-------------|
| `http://localhost:8080/ui` | Task Console | Submit tasks, view task cards |
| `http://localhost:8080/ui/debug` | Debug Viewer | Task execution logs (NDJSON) |
| `http://localhost:8080/ui/map` | SAR Map | 轻量实时网格地图（SSE 自动刷新） |
| `http://localhost:8080/dashboard` | SAR Dashboard | 多视图监控：物理真相、语义地图、轨迹与时间轴 |

SAR UI assets (`sar_orch/ui/`) are deliberately kept outside the generic `src/a2a` backend:
- `task_ui.html` — Task Console
- `debug.html` — Debug Viewer
- `map.html` — lightweight real-time grid map
- `dashboard/index.html` — full SAR monitoring dashboard
- `SARCoordinator` injects this directory with `create_server(ui_dir=...)`; a generic server without `ui_dir` returns 404 for UI routes.

Map UI features (`sar_orch/ui/map.html`):
- Colored grid table: fire intensity (beige→orange→red), agent (pink), reservoir (blue), deposit (black), person (purple)
- Responsive sidebar: step/coverage/transport metrics, agent inventory, fire/person details, and legend
- SSE endpoint `/map/state` pushes grid JSON every 500ms from `barrier.get_env_snapshot()`
- Shared navigation switches between Tasks / Map / Debug / Dashboard

Server integration (`src/a2a/coordinator/server.py`):
- `server.set_barrier(barrier)` — inject SARBarrier reference before `server.run()`
- `server.set_semantic_map(map)` — inject SemanticMapStore for observation ingestion
- `/map/state` SSE — self-contained; returns `{step, finished, coverage, transport_rate, agents, fires, persons, snapshot}`
- `/semantic-map` GET — returns `SemanticMapStore.snapshot()` JSON

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
- **Observation ingestion pipeline**: Worker `report_observation` → `A2AWorkerSink` ([DATA] block, limit 12000) → A2A push → coordinator `_extract_observation_from_status_text()` → `SemanticMapStore.ingest_observation()`. Fully automatic, no extra connections.
- **TaskWatchdog (Phase 3)**: `TaskWatchdog` runs as a single `asyncio.Task` inside the Coordinator event loop. It detects `TASK_STALE`, `WORKER_UNREACHABLE`, `TASK_DEADLINE_WARNING`, and `TASK_DEADLINE_EXCEEDED` using an independent `SupervisionStateStore`. First version only emits actionable events into runtime state and EventStore; it does not auto-cancel or reassign tasks. Alerts surface in the Coordinator Environment State block.
- **TaskWatchdog progress rules**: Progress is recorded on terminal status updates, `artifact_update`, `observation_report`, `INPUT_REQUIRED`, and on domain metric changes (coverage/transport_rate/finished). LLM responses, duplicate heartbeats, NoOp, and step advances without domain delta do not refresh progress.
- **TaskWatchdog boundaries**: No WakeQueue in Phase 3. Actionable events enter `CoordinatorStateProvider` and are consumed by the existing orchestration loop at the next `pre_llm`. `last_heartbeat` and `last_contact_at` are tracked separately: heartbeat updates both; A2A push callback updates `last_contact_at` via `TaskWatchdog.record_worker_contact`.
- **SupervisionStateStore**: Independent persistent store for per-task supervision state, active alerts, and unacknowledged actionable events. It is shared between `TaskWatchdog` and `SARCoordinatorStateProvider` so runtime state and Environment State reflect the same view.
- **Semantic vs Oracle mode**: `--mode semantic` auto-injects the latest semantic map, team status, and task status into the Coordinator's Context before each LLM request; `query_sar_state` is only registered in `--mode oracle`. `query_semantic_map` and `query_team_status` tool classes remain available but are no longer registered as LLM-visible tools in semantic mode (debug/fallback). Mode is set via `experiment.py --mode` or `benchmark.py --mode`.
- **Memory read mode default = `read_port`** (H3 retirement, 2026-08-10): canonical Memory is the official path; `shadow|read_port` fail closed without a protected callback secret (>= 16 bytes) — `experiment.py` auto-generates it per run, direct `SARCoordinator`/`SARWorker`/`create_server` callers must pass `coordinator_secret`. `legacy` is retained as the rollback target and stays available.
- **Long-term memory `read` official since G4 (2026-08-12)**: `--long-term-mode read` injects published-only long-term memories into coordinator Context (`### Long-term Memory` section, one line per memory_key, budget-capped with TRUNCATED drop order Task > Spatial > Embodied > Long-term > Temporal > Freshness). Worker views NEVER contain the section or `long_term_revision` (ACL gate `_is_system and mode=="read"`). `memory_read_mode=shadow` + `long_term_mode=read` is fail-closed (`MemoryConfigError("invalid_mode_combo")`). Long-term DB lives at `<memory_root>/long_term/long_term.sqlite3` and is retained with the run logs (manual cleanup, no auto purge).
- **System Health 诊断通道 official since P5 (2026-08-16)**: agentic 审查者诊断随 `--long-term-mode != off` 自动接线（非独立 CLI 参数）。DiagnosisLoop（max_rounds=3 / diagnosis_sec=150（当前 `long_term.config` 运行配置；配置缺失时代码默认 90），四件只读工具 `query_projection`/`temporal_flow`/`supervision`/`control_journal`）基于 coordinator 决策事件 + 在线事件流生成诊断，以 coordinator-only `### System Health` 段注入（worker 永不可见，ACL 双门控），store 在 `<memory_root>/diagnosis/diagnosis.sqlite3`；validator fail-closed（防回声室：禁诊断引诊断）、置信度门控 `min_confidence≥0.6`；D8 语义：增强非必需、绝不阻塞（超时丢弃、fail-closed）。配置在 `long_term.config` `[diagnosis]` 段（inject_enabled/min_confidence/max_rounds/diagnosis_sec/section_budget_threshold）。验收证据：真实模型 smoke 3/3（`sar_orch/results/diagnosis_smoke_20260816_092513.json`）+ read 10-run 矩阵 10/10 完成（`sar_orch/results/long_term_memory_read_20260816_172405/`，avg cov 0.741 vs 基线 0.781、avg tr 0.725 vs 0.783 无退化）+ pytest 1945 passed 零回归 + ruff 零新增。
- **Coordinator runtime state injection**: `SARCoordinator.start()` creates a `SARCoordinatorStateProvider` that reads `SARBarrier`, `SemanticMapStore`, `EventStore`, `TaskStore`, and `SupervisionStateStore` and projects a versioned runtime snapshot into `CoordinatorContextManager` every LLM round. State is not refreshed within the same SAR env step if the version has not changed.
- **CancelTaskTool available**: Coordinator can cancel running worker tasks via `cancel_task(task_id=...)`. Worker receives `TASK_CANCEL` and exits immediately. Useful to break out of infinite exploration loops.
- **`max_steps` defaults to 50**: Latest commit changed default from scene's task_timeout (120-1200) to fixed 50. `semantic_map.update_step_budget()` is called each poll step so coordinator sees real-time step budget.
- **skills/render-sar-report**: Self-contained HTML report generator. Must use `PYTHONPATH="skills/render-sar-report:$PYTHONPATH"`. If files are missing from working tree, run `git checkout HEAD -- skills/` to restore.
- **Coordinator prompt selection**: `state_mode=semantic` loads `prompts/coordinator/system.semantic.md`; `oracle` mode uses `prompts/coordinator/system.oracle.md` or the default `system.md`.
- **Coordinator should dispatch to ALL agents every round**: Workers auto-no_op after their main task, but idle agents with no task won't submit anything → barrier waits 60s timeout. Prompt enforces this.

## Output Files

Every experiment run creates a unified directory under `logs/YYYYMMDD_HHMMSS/`:

| File | Content |
|------|---------|
| `trajectory.csv` | Per-step metrics (coverage, transport rate, actions, timeout_agents) |
| `agent_interactions.csv` | Per-agent tool calls with args, observation, LLM output |
| `router_interactions.csv` | Coordinator subtask dispatch history |
| `token_usage.csv` | **Each LLM call** — Step, Agent, PromptTokens, CompletionTokens, TotalTokens, CacheHitTokens, CacheMissTokens |
| `summary.csv` | Aggregate metrics + **per-agent cumulative token totals** (updated each step) |
| `events.ndjson` | NDJSON event log (status_update, artifact_update, observation_report, help_request) |
| `subtasks.csv` | Subtask lifecycle (assigned, running, completed, failed, canceled) |
| `semantic_map.jsonl` | (When semantic mode) Observation ingestion event log |
| `metadata.json` | Run metadata (scene, agents, seed, model, prompt_version, code_commit) |
| `<task>.ndjson` | Coordinator router event trace (LLM calls, tool calls, task lifecycle) |
| `Alice/<task>.ndjson` | Alice agent's detailed interaction log |
| `Bob/<task>.ndjson` | Bob agent's detailed interaction log |
| ... | (one subdirectory per agent) |

## System Documentation

### Architecture docs at `docs/system_docs/`

| File | Content |
|------|---------|
| [`docs/system_docs/框架.md`](docs/system_docs/框架.md) | Framework overview: A2A transport, Agent kernel, SAR orchestration |
| [`docs/system_docs/data_flow.md`](docs/system_docs/data_flow.md) | Full data flow tracing context_id / task_id / query end-to-end, including semantic map and observation pipeline |
| [`docs/system_docs/logging_map.md`](docs/system_docs/logging_map.md) | Complete logging system: every record point, trigger, fields, files |
| [`docs/system_docs/experiment_design.md`](docs/system_docs/experiment_design.md) | Experiment design and orchestration details |
| [`docs/system_docs/contextmanager.md`](docs/system_docs/contextmanager.md) | ContextManager design: three-tier memory strategy (none/summary/hybrid) |
| [`docs/system_docs/memory.md`](docs/system_docs/memory.md) | Canonical Memory system: contracts/store/ingestion/auth, projection/export/redaction, truth boundary, read_port wiring, evaluation |
| [`docs/system_docs/sandbox.md`](docs/system_docs/sandbox.md) | Agent sandbox policy for workspace isolation |

### Project notes at `docs/project_notes/`

| File | Content |
|------|---------|
| [`docs/project_notes/bugs.md`](docs/project_notes/bugs.md) | Bug log with dates, root causes, solutions, and prevention |
| [`docs/project_notes/decisions.md`](docs/project_notes/decisions.md) | Architectural Decision Records (ADRs) with context and trade-offs |
| [`docs/project_notes/key_facts.md`](docs/project_notes/key_facts.md) | Project config, ports, environment, CLI commands, data structures |
| [`docs/project_notes/issues.md`](docs/project_notes/issues.md) | Work log with dates, status, and descriptions |

### Design docs at `docs/plans/`

| File | Content |
|------|---------|
| `docs/plans/agent_context_management_plan.md` | ContextManager redesign plan |
| `docs/plans/2026-07-04-context-manager-redesign.md` | Context manager redesign specifics |
| `docs/plans/2026-07-05-data-collection-gaps-supplement.md` | Data collection gap analysis |
| `docs/plans/2026-07-05-experiment-observability-improvements.md` | Observability improvement plan |
| `docs/plans/a2a_push_notification.md` | A2A push notification design |
| `docs/plans/coordinator-worker-independent-package-plan.md` | Independent package plan |

### Key implementation details

- **SARCoordinator.submit_task uses A2A SDK Client**: `sar_orch/coordinator.py` uses `create_client()` + `client.send_message()` instead of raw HTTP JSON-RPC POST. Uses protobuf types (`SendMessageRequest`, `Message`, `Part`, `Role`) from `a2a.types.a2a_pb2`. Responses are serialized via `MessageToDict`.
- **Observation ingestion**: Worker side `sink.py:86` builds `[DATA]` JSON blocks (content_limit=12000 for report_observation). Coordinator `server.py:68-78` (`_extract_observation_from_status_text`) parses them from push callback status text. Ingested at `server.py:471-477`.
- **Semantic map query tools**: `query_semantic_map` (coordinator full snapshot), `query_team_status` (coordinator team summary), and `query_shared_memory` (worker HTTP query to `/semantic-map`) are now debug/fallback tools. In semantic mode, the equivalent data is automatically injected into Coordinator Context by `SARCoordinatorStateProvider` before each LLM round. The tool classes remain available for manual testing or future fallback paths. See `sar_orch/tools/coordinatoor/` and `sar_orch/tools/worker/query_shared_memory.py`.
- **CancelTaskTool**: `src/a2a/builtin_tools/cancel_task.py` — cancels by `task_id`. Registered in `CoordinatorAgentExecutor` extra tools. Worker receives `TASK_CANCEL` via A2A protocol, agent loop exits immediately.
