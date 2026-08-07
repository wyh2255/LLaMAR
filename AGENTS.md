# AGENTS.md

## SAR Experiment

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map
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
- `--mode` `semantic` (default: semantic; the coordinator only sees the semantic map, never raw ground-truth state)
- `--sandbox-profile` `off|workspace` (default: workspace; `off` disables path sandboxing)

## SAR Benchmark (full sweep)

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map
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
cd /home/wyh/daily_work/LLaMAR-sematic_map
PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
  uv run python -m render_sar_report.cli \
  --results-dir sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS \
  --logs-dir logs
```

Default output: `<results_dir>/report.html`.

The report includes: run overview, per-step timeline, coordinator decisions, token usage charts, semantic map evolution, and full LLM traces.

## SAR Eval Agent

Offline evaluation of a completed SAR experiment results directory. The codebase has two intentionally separate report families:

1. **legacy** — the direct CLI path without an injected workflow adapter. It writes root-level `eval_report.{json,md}` plus `eval_workspace/` for backward compatibility.
2. **attempt-v2** — the workflow control plane used by the benchmark eval/gate adapter. It keeps a series of attempts under `eval_attempts/`, then publishes exactly one verified `selected_attempt.json` pointer for formal aggregation.

Never aggregate or gate the two families together.

### Legacy compatibility CLI

```bash
cd /home/wyh/daily_work/LLaMAR_evel

# Deterministic-only legacy report (zero LLM cost).
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.cli \
  --results-dir sar_orch/results/20260719_141217_s2_s42_a4 \
  --no-llm-judge

# Explicit legacy aggregate and gate.
env PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.aggregate \
  --results-root sar_orch/results --family legacy
env PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.gate \
  --results-root sar_orch/results/benchmark \
  --baseline sar_orch/results/baseline_v1 --family legacy
```

The direct CLI keeps this legacy path unless a host explicitly injects `set_workflow_adapter()` / `workflow_adapter`; workflow-only locator flags (`--resume-attempt`, `--eval-run-id`, `--attempt-id`, `--attempt-root`) are for that injected path.

### attempt-v2 workflow outputs and aggregation

The workflow path records attempts beneath:

```text
<results-dir>/eval_attempts/<eval_run_id>/
├── selected_attempt.json
└── attempts/<attempt_id>/
    ├── input_manifest.json
    ├── audit/final_ledger.json
    └── reports/eval_report.{json,md}
```

- Only `SUCCEEDED` attempts with a verified input-manifest → final-ledger → report digest chain may publish `selected_attempt.json`.
- `PARTIAL`, `FAILED`, and `CANCELLED` attempts remain diagnostic artifacts; they never enter formal attempt-family aggregate/gate statistics.
- A later verified success may replace the selected pointer only under the series lease and expected-revision CAS; the pointer revision is retained for audit.
- `aggregate_attempts()` reads only `selected_attempt.json`; an absent, malformed, wrong-family, wrong-semantic-version, or digest-mismatched attempt is recorded as skipped/incompatible rather than guessed or pooled.

```bash
# Explicit attempt-family aggregate and gate. Current and baseline must use
# the same family.
env PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.aggregate \
  --results-root sar_orch/results --family attempt
env PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.gate \
  --results-root sar_orch/results/benchmark \
  --baseline sar_orch/results/baseline_attempt_v2 --family attempt
```

### Benchmark integration and gate policy

```bash
# --eval uses the deterministic attempt workflow; --gate implies --eval.
# Neither flag authorizes a real LLM call.
env PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/benchmark.py --concurrency 2 --gate \
  --gate-baseline sar_orch/results/baseline_attempt_v2
```

- Run directories without `trajectory.csv` are skipped, not turned into empty failed episodes.
- `dispatch_pass_rate_mean`, `hallucination_rate_mean`, and `balance_mean` remain report diagnostics. `gate.py` rejects them in both absolute and regression configuration; they cannot be re-enabled through a custom config.
- Workflow exit codes are `0` for `SUCCEEDED`/diagnostic `PARTIAL`, `1` for `FAILED`, `2` for invalid configuration/locator, busy lease, or publish conflict, and `130` for `CANCELLED`. A cancelled attempt never publishes a selected pointer.
- Real-LLM calibration is a separately authorized activity; deterministic / fake-runner evaluation does not authorize it.

### Key gotchas

- **Family boundary**: legacy scanning prunes `eval_attempts/`; attempt aggregation never recursively trusts arbitrary `eval_report.json` files.
- **Formal versus diagnostic result**: a `PARTIAL` report may be useful to inspect, but only a selected `SUCCEEDED` attempt can affect formal aggregate/gate output.
- **Action aliases**: `agent_interactions.csv` `CarryPerson(...)` maps to `Carry(...)`; unmapped failures are counted as `unmapped_failures`, never silently dropped.
- **evidence_ref logical line number**: `agent_interactions.csv:L<N>` counts CSV records including the header; embedded Observation newlines mean it is not a physical file line number.
- **Grader registry**: `sar_orch/eval/graders/__init__.py` `ALL_GRADERS` / `run_all_graders()` is the shared deterministic source of truth.

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
- Cell inspector: click any grid cell to list all objects at that coordinate (panel above Agents; selection persists across SSE re-renders)
- Zoom slider in the top bar adjusts cell size via the `--cell` CSS variable
- SSE endpoint `/map/state` pushes grid JSON every 500ms from `barrier.get_env_snapshot()`
- Shared navigation switches between Tasks / Map / Debug / Dashboard

Dashboard UI features (`sar_orch/ui/dashboard/index.html`):
- 4 views (ground truth / semantic map / trajectory / timeline replay) + right-rail panels (metrics, agents, fires, persons, observation stream, tokens, step log)
- Metric trend sparkline: client-side SVG line chart of coverage/transport per step (accumulated from `/dashboard/stream`, capped at 240 points)
- Timeline replay speed switchable via 0.5×/1×/2× buttons (restarts the timer mid-replay)

All three monitoring pages (console, map, dashboard) share one brighter dark palette (`--bg #1e2a3d`, `--surface #2a3a52`, `--accent #60a5fa` family) defined in each file's `:root` block — keep them in sync when adjusting colors.

Server integration (`src/a2a/coordinator/server.py`):
- `server.set_barrier(barrier)` — inject SARBarrier reference before `server.run()`
- `server.set_semantic_map(map)` — inject SemanticMapStore for observation ingestion
- `/map/state` SSE — self-contained; returns `{step, finished, coverage, transport_rate, agents, fires, persons, snapshot}`
- `/dashboard/stream` SSE — unified dashboard feed (`{step, coverage, transport_rate, env_snapshot, semantic_map, trajectory_history, observation_stream, last_step_log, router_tokens}`); only pushes on step change. Data sources live on `SARBarrier`: `_execute_step()` records `_trajectory_history` (step 0 recorded at init) and `_observation_stream` (deque, maxlen 200) — do not remove `get_trajectory_history()`/`get_observation_stream()`, the generator silently swallows AttributeError and clients see an empty stream.
- `/semantic-map` GET — returns `SemanticMapStore.snapshot()` JSON
- `/api/mission-graph` GET — returns `SARCoordinatorStateProvider.mission_graph_snapshot()` (mission_dag_view + physical_dispatches_view + task_status_view + step_budget)
- `/api/user-command` POST `{text}` — enqueue a mid-run user command (requires `server.set_user_command_queue(queue)`)

## SAR Console (one-click launcher + live monitoring)

Standalone FastAPI service (default :9000) that launches `experiment.py` as a subprocess and monitors it:

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.console.server --port 9000
# Open http://localhost:9000/
```

Files: `sar_orch/console/server.py` (backend), `sar_orch/console/index.html` (single-file frontend).

Features:
- **Run control**: `POST /api/run/start` (scene/agents/seed/model/mode/max_steps/task) spawns `sar_orch/experiment.py` with explicit `--log-dir sar_orch/results/console_<ts>_...`; one run at a time; `POST /api/run/stop` terminate→kill; `GET /api/run/status` with subprocess log tail.
- **Live feed**: `GET /api/logs/stream-all` — single multiplexed SSE stream for every `*.ndjson` under the run log dir (events wrapped as `{path, source, event}`; replay + follow). The page opens exactly ONE EventSource — never one per file: browsers cap HTTP/1.1 connections per origin at ~6 and a run produces ~20 log files, so per-file streams starve all other requests (status, mission graph, stop). Legacy `GET /api/logs` + `GET /api/logs/stream?path=` remain for debugging.
- **User commands**: UI input → `POST /api/command` → forwarded to coordinator `POST /api/user-command`.
- **Mission Graph**: frontend polls `/coord/api/mission-graph` (1.5s) and renders the task DAG (state-colored nodes, depends_on edges; synthesized from dispatches when the LLM hasn't called `update_plan`) + per-worker dispatch tables. The final graph stays visible after the run ends (last snapshot is not wiped).
- **Center view tabs**: the middle column switches between Mission Graph / Map / Dashboard. Map and Dashboard are embedded as iframes (`/coord/ui/map`, `/coord/dashboard`), loaded only while a run is active; an already-loaded iframe keeps its last frame after the run ends. Topbar links still open them in a new tab.
- **Proxy**: `GET /coord/{path}` forwards to the coordinator :8080 (SSE passthrough for `/map/state`, `/dashboard/stream`; JSON re-serialized; HTML and other content passed through with the original content-type) so the page stays same-origin. Root-level aliases (`/ui`, `/ui/map`, `/ui/debug`, `/dashboard`, `/semantic-map`, `/map/state`, `/dashboard/stream`) forward the same way so iframe-embedded pages — which fetch absolute paths like `/map/state` — work on the console origin.

User-command injection chain: console → `POST /api/user-command` → `UserCommandQueue` (`sar_orch/user_command_queue.py`, threading.Lock) → `SARCoordinatorStateProvider.snapshot()` drains into `payload["user_commands"]` + bumps `_runtime_version` → `CoordinatorPinnedState.user_commands` → rendered as `### User Commands` in the Context Memory block at the next `pre_llm` round. Drained commands appear exactly once; they do NOT interrupt the current LLM round.

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
- **Wall-clock limit**: Single experiments have a wall-clock safety net in the poll loop (default 3600s). Configure via `experiment.py --wall-clock-limit <seconds>`; `0` disables the limit (unlimited).
- **query_sar_state returns step info**: Snapshot now includes `step`, `max_steps`, `finished` — coordinator can make step-budget-aware decisions.
- **Worker auto-NoOp**: `no_op` tool returns `[MISSION COMPLETE]` or `[Step N] Mission in progress`. Workers auto-no_op after main task (5-cap then return). Coordinator doesn't need to pad tasks with NoOp but should still give longest useful chains.
- **Poll loop exits on a2a_task.done()**: When coordinator orchestration completes (normally or max_steps), the poll loop breaks immediately — no more 60s-per-step idle spinning.
- **barrier.stop() wakes workers**: `stop()` sets `_stopped=True` + all `event.set()` — waiting workers unblock and return immediately.
- **Observation ingestion pipeline**: Worker `report_observation` → `A2AWorkerSink` ([DATA] block, limit 12000) → A2A push → coordinator `_extract_observation_from_status_text()` → `SemanticMapStore.ingest_observation()`. Fully automatic, no extra connections.
- **TaskWatchdog (Phase 3)**: `TaskWatchdog` runs as a single `asyncio.Task` inside the Coordinator event loop. It detects `TASK_STALE`, `WORKER_UNREACHABLE`, `TASK_DEADLINE_WARNING`, and `TASK_DEADLINE_EXCEEDED` using an independent `SupervisionStateStore`. First version only emits actionable events into runtime state and EventStore; it does not auto-cancel or reassign tasks. Alerts surface in the Coordinator Context Memory block.
- **TaskWatchdog progress rules**: Progress is recorded on terminal status updates, `artifact_update`, `observation_report`, `INPUT_REQUIRED`, and on domain metric changes (coverage/transport_rate/finished). LLM responses, duplicate heartbeats, NoOp, and step advances without domain delta do not refresh progress.
- **TaskWatchdog boundaries**: No WakeQueue in Phase 3. Actionable events enter `CoordinatorStateProvider` and are consumed by the existing orchestration loop at the next `pre_llm`. `last_heartbeat` and `last_contact_at` are tracked separately: heartbeat updates both; A2A push callback updates `last_contact_at` via `TaskWatchdog.record_worker_contact`.
- **SupervisionStateStore**: Independent persistent store for per-task supervision state, active alerts, and unacknowledged actionable events. It is shared between `TaskWatchdog` and `SARCoordinatorStateProvider` so runtime state and Context Memory reflect the same view.
- **Semantic mode**: `--mode semantic` auto-injects the latest semantic map, team status, and task status into the Coordinator's Context before each LLM request. `query_semantic_map` and `query_team_status` tool classes remain available but are no longer registered as LLM-visible tools (debug/fallback). Mode is set via `experiment.py --mode` or `benchmark.py --mode`.
- **Coordinator runtime state injection**: `SARCoordinator.start()` creates a `SARCoordinatorStateProvider` that reads `SARBarrier`, `SemanticMapStore`, `EventStore`, `TaskStore`, and `SupervisionStateStore` and projects a versioned runtime snapshot into `CoordinatorContextManager` every LLM round. State is not refreshed within the same SAR env step if the version has not changed.
- **CancelTaskTool available**: Coordinator can cancel running worker tasks via `cancel_task(task_id=...)`. Worker receives `TASK_CANCEL` and exits immediately. Useful to break out of infinite exploration loops.
- **SAR Console assumes port 8080**: `sar_orch/console/server.py` spawns experiment.py with default ports (8080/8191+) and proxies `localhost:8080`. Do not run it alongside a benchmark or another experiment on the same ports.
- **SAR Console keeps proxy env vars**: unlike `benchmark.py` (which strips `http_proxy`/`https_proxy`), the console inherits them and only extends `no_proxy` with localhost — required when the LLM gateway (e.g. `.env` `api_base`) is only reachable through a proxy. `experiment.py` CLI defaults (`--model`/`--provider`/`--api-base`) already come from `.env`.
- **`max_steps` defaults to 50**: Latest commit changed default from scene's task_timeout (120-1200) to fixed 50. `semantic_map.update_step_budget()` is called each poll step so coordinator sees real-time step budget.
- **skills/render-sar-report**: Self-contained HTML report generator. Must use `PYTHONPATH="skills/render-sar-report:$PYTHONPATH"`. If files are missing from working tree, run `git checkout HEAD -- skills/` to restore.
- **Coordinator prompt selection**: The coordinator always loads `prompts/coordinator/system.md` (the only coordinator prompt; semantic-mode content).
- **Coordinator should dispatch to ALL agents every round**: Workers auto-no_op after their main task, but idle agents with no task won't submit anything → barrier waits 60s timeout. Prompt enforces this.
- **Aborted worker_task guard**: `MissionRuntime.abort()` records the runtime's worker_task_ids into `MissionRuntimeManager._aborted_worker_tasks` (bounded, 512). The legacy push-callback path (active_runtime is None) rejects callbacks hitting that set with `{"status": "ignored", "reason": "aborted_worker_task"}` + a `aborted_worker_task_callback` diagnostic — otherwise late post-abort callbacks would write EventStore/SemanticMap unchecked.

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
| [`docs/system_docs/sandbox.md`](docs/system_docs/sandbox.md) | Agent sandbox policy for workspace isolation |
| [`docs/system_docs/sar_console.md`](docs/system_docs/sar_console.md) | SAR Console: one-click launcher, live monitoring frontend, user-command injection chain, mission graph |
| [`docs/system_docs/eval_agent.md`](docs/system_docs/eval_agent.md) | Eval Agent 设计：确定性 grader + LLM judge 架构、数据陷阱、防线设计（新手向） |

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
