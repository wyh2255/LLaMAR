# AGENTS.md

> 本文件只收录**有兼容性的契约级内容**（CLI 命令面、环境约定、稳定行为不变量、跨版本兼容声明）。易变细节（行号、验收数值、日期、phase 标签）不在此记录，详见 `docs/system_docs/` 对应文档。

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
- `--max-steps` override max environment steps (不传时 = scene task_timeout：scene1=1200、scene2-5=35)
- `--mode` `semantic|oracle` (default: semantic; semantic hides oracle truth from coordinator)
- `--sandbox-profile` `off|workspace` (default: workspace; `off` disables path sandboxing)
- `--memory-read-mode` `legacy|shadow|read_port` (default: `read_port`，canonical Memory 为官方路径，H3 起 legacy 仅作回滚目标；`shadow|read_port` 无受保护回调密钥 fail-closed)
- `--long-term-mode` `off|shadow|read` (default: `off`；`read` 为官方可用模式：仅向 coordinator Context 注入已发布长期记忆 `### Long-term Memory` 段，worker 永不可见；`shadow` 只持久化不注入；`memory_read_mode=shadow` + `long_term_mode=read` 组合 fail-closed)
- 系统健康诊断通道（agentic 审查者，无独立 CLI 参数）：随 `--long-term-mode != off` 自动接线。DiagnosisLoop（配置在 `long_term.config` 的 `[diagnosis]` 段，max_rounds=3 / diagnosis_sec=150；四件只读工具 `query_projection` / `temporal_flow` / `supervision` / `control_journal`）随 rolling 每 5 步触发，产出 coordinator-only `### System Health` 段注入（worker 永不可见）。D8 语义：增强非必需、**绝不阻塞**（超时丢弃、fail-closed、置信度门控 min_confidence≥0.6）。详见 `docs/system_docs/memory.md`。

## SAR Benchmark (full sweep)

```bash
cd "$(git rev-parse --show-toplevel)"
# Run all 100 combinations (5 scenes × 4 agent counts × 5 seeds)
# --run-timeout (default 3600s) prevents stuck runs from blocking progress
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 3600

# Filter by specific scene(s):
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --run-timeout 3600 --scene 5

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

Default output: `<results_dir>/report.html`. The report includes: run overview, per-step timeline, coordinator decisions, token usage charts, semantic map evolution, and full LLM traces.

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
- **NavigateTo is teleport**: SAR `GridEngine.move_object()` does direct `set_position()` — one call is enough. Tool returns `"Arrived at X. Position: (x,y,z)."` to avoid LLM confusion.
- **get_agent_state (GPS)**: Zero-cost state query. Does NOT call `submit_action()`, does NOT consume a step. Use when agent needs to confirm position/inventory.
- **get_position() returns tuple**: `(x, y, z)` tuple, NOT a Coordinate object. Use `pos[0]`/`pos[1]`/`pos[2]` not `pos.x`/`pos.y`/`pos.z`.
- **Agent class token tracking**: Both copies (worker_agent/ and router_agent/) own `api_prompt_tokens`, `api_completion_tokens`, `cumulative_total_tokens` etc. Sync changes between the two copies (`__init__` fields, `run()` usage tracking, `_create_summary()` summarization tokens).
- **Thread safety**: `ExperimentLogger` uses `threading.Lock` — all `log_*()` methods wrapped with `with self._lock:`.
- **Crash-safe summary**: `flush_summary()` called after each poll step; `summary.csv` always has latest data even if process is killed.
- **Coordinator agent name**: Token logged as `"Coordinator"` (hardcoded in `sar_orch/coordinator.py` `_router_cb`).
- **Benchmark port conflicts**: `run_experiment()` accepts `coordinator_port` and `agent_base_port`; benchmark offsets per concurrency block. Concurrent runs never share ports.
- **Benchmark run timeout**: `--run-timeout` default **3600s** — keep it set so a stuck agent loop cannot stall the whole benchmark. Always pass explicit `log_dir` for concurrent runs to avoid directory collisions.
- **Barrier uses threading primitives (NOT asyncio)**: `SARBarrier` uses `threading.Event`/`threading.Lock` because workers run in separate threads with separate asyncio event loops (ADR-011).
- **TimeoutAgents in trajectory.csv**: `TimeoutAgents` column lists agent indices that were auto-filled with NoOp due to barrier timeout. `[]` means all agents submitted normally. Use this to filter system-injected NoOps from LLM-chosen NoOps during prompt analysis.
- **Wall-clock safety net**: single experiments have a **3600s** wall-clock limit in the poll loop (`wall_clock_limit`, written to metadata as `wall_clock_timeout`).
- **query_sar_state returns step info**: Snapshot includes `step`, `max_steps`, `finished` — coordinator can make step-budget-aware decisions.
- **Worker auto-NoOp**: `no_op` tool returns `[MISSION COMPLETE]` or `[Step N] Mission in progress`. Workers auto-no_op after main task (5-cap then return). Coordinator doesn't need to pad tasks with NoOp.
- **Poll loop exits on a2a_task.done()**: When coordinator orchestration completes (normally or max_steps), the poll loop breaks immediately.
- **barrier.stop() wakes workers**: `stop()` sets `_stopped=True` + all `event.set()` — waiting workers unblock and return immediately.
- **Observation ingestion pipeline**: Worker `report_observation` → A2AWorkerSink (`[DATA]` block; content limit 12000 applies to tool_result content, structured_data not truncated) → A2A push → coordinator extracts observations (provenance-tagged) → `SemanticMapStore.ingest_observation()`. Fully automatic, no extra connections.
- **TaskWatchdog**: runs as a single `asyncio.Task` inside the Coordinator event loop with an independent `SupervisionStateStore`. Detects `TASK_STALE`, `WORKER_UNREACHABLE`, `TASK_DEADLINE_WARNING`, `TASK_DEADLINE_EXCEEDED` and emits actionable events into runtime state / EventStore (no auto-cancel). Progress refreshes only on terminal status updates, artifact_update, observation_report, INPUT_REQUIRED, or domain metric changes — not on plain LLM responses, duplicate heartbeats, NoOp, or step advances without domain delta. `last_heartbeat` and `last_contact_at` are tracked separately.
- **SupervisionStateStore**: independent persistent per-task supervision store shared by TaskWatchdog and SARCoordinatorStateProvider, so runtime state and Environment State show the same view.
- **Semantic vs Oracle mode**: `--mode semantic` auto-injects semantic map / team status / task status into Coordinator Context each LLM round; `query_sar_state` is only registered in `--mode oracle`. `query_semantic_map` / `query_team_status` / `query_shared_memory` tool classes remain implemented but are not registered for LLM use (debug/fallback only).
- **Memory read mode default = `read_port`**: canonical Memory is the official path; `shadow|read_port` fail closed without a protected callback secret (>= 16 bytes) — `experiment.py` auto-generates it per run, direct callers must pass `coordinator_secret`. `legacy` is the rollback target.
- **Long-term memory `read` official**: `--long-term-mode read` injects published-only long-term memories into coordinator Context (`### Long-term Memory` section, one line per memory_key, budget-capped). Worker views NEVER contain the section (ACL-gated system-only). DB at `<memory_root>/long_term/long_term.sqlite3`, retained with run logs.
- **System Health 诊断通道**: 随 `--long-term-mode != off` 自动接线（无独立 CLI）；诊断循环产出 coordinator-only `### System Health` 段注入（worker 永不可见）；D8：增强非必需、绝不阻塞（超时丢弃、fail-closed）。store 在 `<memory_root>/diagnosis/diagnosis.sqlite3`。
- **Coordinator runtime state injection**: `SARCoordinator.start()` creates a `SARCoordinatorStateProvider` that projects a versioned runtime snapshot into coordinator Context every LLM round; state not refreshed within the same env step if version unchanged.
- **CancelTaskTool**: Coordinator can cancel running worker tasks via `cancel_task(task_id=...)`. Worker receives `TASK_CANCEL` and exits immediately.
- **`max_steps` semantics**: 单跑 experiment.py 不传 `--max-steps` 时 = scene task_timeout（scene1=1200、scene2-5=35）；benchmark `--max-steps` 默认 50（设 0 禁用步数截断，仅靠 `--run-timeout`）。`semantic_map.update_step_budget()` is called each poll step.
- **skills/render-sar-report**: Self-contained HTML report generator. Must use `PYTHONPATH="skills/render-sar-report:$PYTHONPATH"`. If files are missing from working tree, run `git checkout HEAD -- skills/` to restore.
- **Coordinator prompt selection**: `state_mode=semantic` loads `prompts/coordinator/system.semantic.md`; `oracle` mode uses `prompts/coordinator/system.oracle.md` or the default `system.md`.
- **Coordinator should dispatch to ALL agents every round**: Workers auto-no_op after their main task, but idle agents with no task won't submit anything → barrier waits 60s timeout. Prompt enforces this.

## Output Files

单次实验输出统一在 **`sar_orch/results/{YYYYMMDD_HHMMSS}_s{scene}_s{seed}_a{agents}/`**（`--log-dir` 显式指定时以其为准；内部子目录 coordinator/、workers/<AgentName>/、supervision/）。benchmark 并发产物在 `sar_orch/results/benchmark/scene_{S}/agents_{A}/seed_{N}/`，聚合输出 `sar_orch/results/benchmark_aggregated.tsv`（15 列）。

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
| `run_metrics.json` | Run-level metrics |
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
- **Observation ingestion**: Worker side `sink.py` builds `[DATA]` JSON blocks (content_limit=12000 for report_observation). Coordinator parses them from push callback status text and ingests into `SemanticMapStore`.
- **Semantic map query tools**: `query_semantic_map` (coordinator full snapshot), `query_team_status` (coordinator team summary), and `query_shared_memory` (worker HTTP query to `/semantic-map`) are now debug/fallback tools. In semantic mode, the equivalent data is automatically injected into Coordinator Context. Tool classes remain for manual testing / future fallback.
- **CancelTaskTool**: `src/a2a/builtin_tools/cancel_task.py` — cancels by `task_id`. Registered in `CoordinatorAgentExecutor` extra tools. Worker receives `TASK_CANCEL` via A2A protocol, agent loop exits immediately.