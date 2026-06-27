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
