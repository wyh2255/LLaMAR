# Worker→Coordinator Interrupt / Help Request Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Worker LLM to pause execution and ask Coordinator for clarification, then resume when Coordinator responds — via symmetric HTTP POST callbacks (not A2A message/send).

**Architecture:** Worker sends `[HELP]` artifact via POST to Coordinator's push-callback; Coordinator's Agent loop sees it via EventStore context injection; Coordinator responds via POST to Worker's push-callback; Worker resolves a per-task Future to resume the Agent loop.

**Tech Stack:** Python 3.10+, asyncio, A2A SDK protobuf, Starlette, httpx

## Global Constraints

- Coordinator→Worker and Worker→Coordinator both use HTTP POST → `/a2a/push-callback` — never `client.send_message()` (creates duplicate Agent loop)
- Worker callback URL = `http://{host}:{port}/a2a/push-callback` derived from `AgentRegistry` endpoint
- Worker push sender is not used — raw `httpx.post()` is simpler and avoids SDK coupling
- `asyncio.wait_for(future, 120s)` timeout for help request
- `[HELP]` prefix in artifact text distinguishes help requests from regular artifacts
- Worker per-task Futures must be cleaned up on Agent loop exit (CancelledError)

---

### Task 1: WorkerFutureRegistry

**Files:**
- Create: `src/a2a/worker/future_registry.py`

**Interfaces:**
- Consumes: nothing
- Produces: `register_future(task_id: str) -> asyncio.Future`, `resolve_future(task_id: str, result: Any) -> None`

- [ ] **Step 1: Write the new file**

```python
import asyncio
import threading
from typing import Any

_worker_future_registry: dict[str, asyncio.Future] = {}
_lock = threading.Lock()

def register_future(task_id: str) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    with _lock:
        _worker_future_registry[task_id] = future
    return future

def resolve_future(task_id: str, result: Any) -> None:
    with _lock:
        future = _worker_future_registry.pop(task_id, None)
    if future is not None and not future.done():
        future.set_result(result)
```

- [ ] **Step 2: Lint**

```bash
uv run --with ruff ruff check src/a2a/worker/future_registry.py
```

- [ ] **Step 3: Commit**

```bash
git add src/a2a/worker/future_registry.py
git commit -m "feat: add WorkerFutureRegistry for interrupt help requests"
```

---

### Task 2: Worker /a2a/push-callback endpoint

**Files:**
- Modify: `src/a2a/worker/a2a_server.py:124-131`

**Interfaces:**
- Consumes: `resolve_future` from Task 1 (`a2a.worker.future_registry`)
- Produces: `POST /a2a/push-callback` on Worker's Starlette app

- [ ] **Step 1: Add imports**

```python
# Add these imports near the top (line 8, after 'import httpx')
import json
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
```

- [ ] **Step 2: Add the handler function and route registration**

Replace the routes block (lines 124-127):

```python
    async def _worker_push_callback(request: Request) -> JSONResponse:
        from google.protobuf.json_format import ParseDict
        from a2a.types.a2a_pb2 import StreamResponse
        from a2a.worker.future_registry import resolve_future

        body = await request.json()
        sr = StreamResponse()
        ParseDict(body, sr)

        if sr.HasField("artifact_update"):
            au = sr.artifact_update
            texts = [p.text for p in au.artifact.parts if p.text]
            if texts and au.task_id:
                resolve_future(au.task_id, " ".join(texts))

        return JSONResponse({"status": "ok"})

    routes = [
        Route("/a2a/push-callback", _worker_push_callback, methods=["POST"]),
    ]
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, rpc_url="/api/v1/jsonrpc/"))
    app = Starlette(routes=routes)
```

**Important**: Starlette uses `Route` objects rather than decorator-style routing. The `_worker_push_callback` handler must be defined as a standalone `async def` inside `create_worker_a2a_server` so it closes over the local scope.

- [ ] **Step 3: Lint**

```bash
uv run --with ruff ruff check src/a2a/worker/a2a_server.py
```

- [ ] **Step 4: Commit**

```bash
git add src/a2a/worker/a2a_server.py
git commit -m "feat: add Worker /a2a/push-callback endpoint for interrupt responses"
```

---

### Task 3: Coordinator push callback [HELP] extension + EventStore help_request rendering

**Files:**
- Modify: `src/a2a/coordinator/server.py:397-403` (artifact_update branch)
- Modify: `src/a2a/coordinator/event_store.py:95-103` (get_summary rendering)

**Interfaces:**
- Consumes: `event_store` singleton from `a2a.coordinator.event_store`
- Produces: `help_request` event type in EventStore

- [ ] **Step 1: Add [HELP] detection to server.py**

Replace the artifact_update branch at lines 397-403:

```python
            elif sr.HasField("artifact_update"):
                au = sr.artifact_update
                task_id = au.task_id
                if au.HasField("artifact"):
                    texts = [p.text for p in au.artifact.parts if p.text]
                    if texts and task_id:
                        combined = " ".join(texts)
                        if combined.startswith("[HELP] "):
                            event_store.append(task_id, "help_request", text=combined[6:])
                        else:
                            _push_artifact_cache.setdefault(task_id, []).extend(texts)
                            event_store.append(task_id, "artifact_update", text=combined)
```

- [ ] **Step 2: Add help_request rendering to event_store.py**

Insert after the `artifact_update` branch at line 101:

```python
                    elif r.event_type == "help_request":
                        text_short = (r.text or "")[:200]
                        task_lines.append(f"  {ts_str} HELP: {text_short}")
```

- [ ] **Step 3: Lint**

```bash
uv run --with ruff ruff check src/a2a/coordinator/server.py src/a2a/coordinator/event_store.py
```

- [ ] **Step 4: Commit**

```bash
git add src/a2a/coordinator/server.py src/a2a/coordinator/event_store.py
git commit -m "feat: detect [HELP] artifacts in push callback, add help_request event type"
```

---

### Task 4: AskCoordinatorTool (+ tools/ directory)

**Files:**
- Create: `src/a2a/worker/tools/__init__.py` (empty)
- Create: `src/a2a/worker/tools/ask_coordinator.py`

**Interfaces:**
- Consumes: `register_future`, `resolve_future` from Task 1
- Produces: `AskCoordinatorTool` class with `execute(question: str) -> ToolResult`

- [ ] **Step 1: Create tools/ directory and __init__.py**

```bash
mkdir -p src/a2a/worker/tools
touch src/a2a/worker/tools/__init__.py
```

- [ ] **Step 2: Write ask_coordinator.py**

```python
"""AskCoordinatorTool — Worker Agent 向 Coordinator 请求帮助。"""

import asyncio
import json
import logging
from typing import Any

from google.protobuf.json_format import MessageToDict
from a2a.types.a2a_pb2 import Artifact, Part, StreamResponse

from a2a.worker.future_registry import register_future, resolve_future
from Agent.worker_agent.tools.base import Tool, ToolResult
import httpx

logger = logging.getLogger(__name__)

HELP_TIMEOUT = 120.0


class AskCoordinatorTool(Tool):
    """暂停执行并向 Coordinator 请求帮助。"""

    def __init__(
        self,
        coordinator_callback_url: str,
        task_id: str,
    ) -> None:
        self._coordinator_url = coordinator_callback_url.rstrip("/") + "/a2a/push-callback"
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "ask_coordinator"

    @property
    def description(self) -> str:
        return (
            "Pause execution and ask the coordinator for help or clarification. "
            "The coordinator will see your question and respond. Blocks until "
            f"the coordinator replies (timeout: {HELP_TIMEOUT}s)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or help request to send to the coordinator",
                },
            },
            "required": ["question"],
        }

    async def execute(self, question: str) -> ToolResult:
        future = register_future(self._task_id)

        sr = StreamResponse()
        sr.artifact_update.task_id = self._task_id
        sr.artifact_update.artifact.CopyFrom(
            Artifact(parts=[Part(text=f"[HELP] {question}")])
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                await client.post(
                    self._coordinator_url,
                    content=json.dumps(MessageToDict(sr)),
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:
                resolve_future(self._task_id, f"[send failed: {e}]")
                return ToolResult(
                    success=False,
                    content=f"Failed to send help request: {e}",
                )

        try:
            reply = await asyncio.wait_for(future, timeout=HELP_TIMEOUT)
            return ToolResult(success=True, content=reply)
        except asyncio.TimeoutError:
            resolve_future(self._task_id, "[no response from coordinator]")
            return ToolResult(
                success=False,
                content="Coordinator did not respond within 120s. Proceed with your best judgment.",
            )
```

- [ ] **Step 3: Lint**

```bash
uv run --with ruff ruff check src/a2a/worker/tools/__init__.py src/a2a/worker/tools/ask_coordinator.py
```

- [ ] **Step 4: Commit**

```bash
git add src/a2a/worker/tools/
git commit -m "feat: add AskCoordinatorTool for worker-to-coordinator help requests"
```

---

### Task 5: Inject AskCoordinatorTool into Worker AgentAdapter

**Files:**
- Modify: `src/a2a/worker/agent_adapter.py:47-67` (constructor)
- Modify: `src/a2a/worker/agent_adapter.py:119-143` (execute method)
- Modify: `src/a2a/worker/a2a_server.py:90-108` (executor construction)

**Interfaces:**
- Consumes: `AskCoordinatorTool` from Task 4
- Produces: Constructor param `coordinator_callback_url` on AgentAdapter, tool injection in execute()

- [ ] **Step 1: Add constructor param to AgentAdapter**

Add `coordinator_callback_url` parameter at line 67 (before closing `):`):

```python
        coordinator_callback_url: str = "",
    ):
        ...
        self._coordinator_callback_url = coordinator_callback_url
```

The full `__init__` signature line at line 67 becomes:

```python
        require_explicit_completion: bool = False,
        coordinator_callback_url: str = "",
    ):
        ...
        self._require_explicit_completion = require_explicit_completion
        self._coordinator_callback_url = coordinator_callback_url
```

- [ ] **Step 2: Inject AskCoordinatorTool in execute()**

In the `execute()` method at line 134, after `query = context.get_user_input()`, add tool injection.
**Critical**: Worker's `AgentAdapter` factory is `agent_factory=lambda **kw: self._build_agent()`,
which ignores runtime kwargs (including `extra_tools` from `controller.submit()`).
Therefore `ask_tool` must be prepended to `self._extra_tools` directly, so it
survives the factory's kwarg black hole:

```python
        query = context.get_user_input() or ""

        from a2a.worker.tools.ask_coordinator import AskCoordinatorTool
        from a2a.worker.future_registry import resolve_future

        # Prepend to _extra_tools — agent factory ignores submit()'s extra_tools kwarg
        ask_tool = AskCoordinatorTool(
            coordinator_callback_url=self._coordinator_callback_url,
            task_id=task_id,
        )
        self._extra_tools = [ask_tool] + self._extra_tools
```

Do NOT change the `controller.submit()` call — `extra_tools` is already passed
via the constructor and merged by `_build_agent()` → `_merge_runtime_kwargs()`.
The prepend above ensures `ask_tool` is always available.

Update the `CancelledError` handler at line 152 to clean up pending help requests:

```python
        except asyncio.CancelledError:
            resolve_future(task_id, "[cancelled]")
            await updater.cancel()
            raise
```

Update the general `Exception` handler at line 156 similarly:

```python
        except Exception as e:
            resolve_future(task_id, "[cancelled]")
            await updater.failed(message=new_text_message(str(e)))
            raise
```

- [ ] **Step 3: Pass coordinator_callback_url from a2a_server.py**

In `src/a2a/worker/a2a_server.py`, add `coordinator_callback_url` parameter and pass to AgentAdapter. Modify the function signature at line 27-48:

```python
def create_worker_a2a_server(
    ...
    require_explicit_completion: bool = False,
    coordinator_callback_url: str = "",  # new
) -> uvicorn.Server:
```

And at the executor construction (line 90):

```python
    executor = AgentAdapter(
        ...
        require_explicit_completion=require_explicit_completion,
        coordinator_callback_url=coordinator_callback_url,
    )
```

- [ ] **Step 4: Lint**

```bash
uv run --with ruff ruff check src/a2a/worker/agent_adapter.py src/a2a/worker/a2a_server.py src/a2a/worker/tools/ask_coordinator.py
```

- [ ] **Step 5: Commit**

```bash
git add src/a2a/worker/agent_adapter.py src/a2a/worker/a2a_server.py
git commit -m "feat: inject AskCoordinatorTool into Worker AgentAdapter and a2a_server"
```

---

### Task 6: RespondWorkerTool

**Files:**
- Create: `src/a2a/builtin_tools/respond_worker.py`

**Interfaces:**
- Consumes: `TaskStore` (get_node), `AgentRegistry` (get endpoint — **throws `AgentNotFoundError`**, does NOT return None)
- Produces: `RespondWorkerTool` class with `execute(task_id: str, response: str) -> ToolResult`

- [ ] **Step 1: Write the new file**

```python
"""RespondWorkerTool — Coordinator Agent 回复 Worker 的帮助请求。"""

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from google.protobuf.json_format import MessageToDict
from a2a.types.a2a_pb2 import Artifact, Part, StreamResponse

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError

logger = logging.getLogger(__name__)


class RespondWorkerTool(Tool):
    """回复 Worker 发起的帮助请求。"""

    def __init__(self, store: TaskStore, registry: AgentRegistry) -> None:
        self._store = store
        self._registry = registry

    @property
    def name(self) -> str:
        return "respond_worker"

    @property
    def description(self) -> str:
        return (
            "Respond to a worker's help request. Call this when a worker is "
            "asking for clarification. Sends the response directly back to "
            "the worker so it can continue execution."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID of the worker that needs help",
                },
                "response": {
                    "type": "string",
                    "description": "The response/guidance to send back to the worker",
                },
            },
            "required": ["task_id", "response"],
        }

    async def execute(self, task_id: str, response: str) -> ToolResult:
        node = self._store.get_node(task_id)
        if node is None or not node.worker_id:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in dispatched tasks.",
            )

        try:
            agent_info = self._registry.get(node.worker_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{node.worker_id}' not found in registry.",
            )

        parsed = urlparse(agent_info.endpoint)
        worker_callback_url = f"{parsed.scheme}://{parsed.netloc}/a2a/push-callback"

        sr = StreamResponse()
        sr.artifact_update.task_id = task_id
        sr.artifact_update.artifact.CopyFrom(
            Artifact(parts=[Part(text=response)])
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                await client.post(
                    worker_callback_url,
                    content=json.dumps(MessageToDict(sr)),
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    content=f"Failed to send response to worker: {e}",
                )

        return ToolResult(
            success=True,
            content=f"Response sent to {task_id}: {response}",
        )
```

- [ ] **Step 2: Lint**

```bash
uv run --with ruff ruff check src/a2a/builtin_tools/respond_worker.py
```

- [ ] **Step 3: Commit**

```bash
git add src/a2a/builtin_tools/respond_worker.py
git commit -m "feat: add RespondWorkerTool for coordinator-to-worker help replies"
```

---

### Task 7: Inject RespondWorkerTool into Coordinator AgentExecutor

**Files:**
- Modify: `src/a2a/coordinator/agent_executor.py:35-42` (imports)
- Modify: `src/a2a/coordinator/agent_executor.py:289-300` (tools list)

**Interfaces:**
- Consumes: `RespondWorkerTool` from Task 6
- Produces: Tool available to Coordinator Agent in agentic mode
- Note: `RespondWorkerTool` no longer accepts `httpx.AsyncClient` — client is created inside `execute()`

- [ ] **Step 1: Add import**

Add after the existing `dispatch_task` import (line 37):

```python
from a2a.builtin_tools.respond_worker import RespondWorkerTool
```

- [ ] **Step 2: Add to tools list**

Add `RespondWorkerTool` to the tools list at lines 289-300:

```python
        tools = [
            UpdatePlanTool(store),
            DispatchTaskTool(
                store,
                coordinator_host=self._coordinator_host,
                coordinator_port=self._coordinator_port,
            ),
            CollectResultsTool(store),
            VerifyResultTool(store),
            QueryTaskResultsTool(store.results),
            RespondWorkerTool(store, self._registry),
            SARFinishTaskTool(store),
        ]
```

- [ ] **Step 3: Lint**

```bash
uv run --with ruff ruff check src/a2a/coordinator/agent_executor.py
```

- [ ] **Step 4: Commit**

```bash
git add src/a2a/coordinator/agent_executor.py
git commit -m "feat: register RespondWorkerTool in Coordinator AgentExecutor"
```

---

### Task 8: Update callers (cli.py + sar_orch/worker.py)

**Files:**
- Modify: `src/a2a/worker/cli.py` (add `--coordinator-callback-url` option)
- Modify: `sar_orch/worker.py` (pass callback URL to `create_worker_a2a_server`)

**Interfaces:**
- Consumes: `create_worker_a2a_server`'s new `coordinator_callback_url` param (from Task 5)
- Produces: CLI and SAR worker pass the callback URL through

**Context:** Coordinator's A2A endpoint is typically `ws://host:port`, but the callback needs HTTP: `http://host:port`. The URL derivation: `ws://` → `http://`, `wss://` → `https://`.

- [ ] **Step 1: Add cli.py `--coordinator-callback-url` option**

Locate the argument parser in `src/a2a/worker/cli.py` and add:

```python
parser.add_argument(
    "--coordinator-callback-url",
    default="",
    help="Coordinator HTTP callback URL for help requests (e.g. http://host:8080)",
)
```

Pass the value to `create_worker_a2a_server(...)`:

```python
coordinator_callback_url=args.coordinator_callback_url,
```

- [ ] **Step 2: Update sar_orch/worker.py**

Locate the `create_worker_a2a_server(...)` call and add:

```python
# Derive HTTP callback URL from websocket coordinator URL
coord_ws = coordinator_url  # e.g. "ws://host:8080"
coord_http = coord_ws.replace("ws://", "http://").replace("wss://", "https://")
coordinator_callback_url=coord_http,
```

- [ ] **Step 3: Lint**

```bash
uv run --with ruff ruff check src/a2a/worker/cli.py sar_orch/worker.py
```

- [ ] **Step 4: Commit**

```bash
git add src/a2a/worker/cli.py sar_orch/worker.py
git commit -m "feat: pass coordinator_callback_url from CLI and SAR worker"
```

---

## Verification

After all tasks complete, run a full lint:

```bash
uv run --with ruff ruff check src/a2a/ src/Agent/ sar_orch/
```

Expected: zero new lint errors (only pre-existing ones).

## Post-review Fixes Incorporated

The following issues from the pre-flight review were fixed in this plan:

| Issue | Fix |
|-------|-----|
| `AgentRegistry.get()` throws instead of returning None (C-1) | Task 6: wrapped in `try/except AgentNotFoundError` |
| `agent_factory` ignores `extra_tools` kwargs (C-2) | Task 5: prepend to `self._extra_tools` directly |
| Missing caller updates for `coordinator_callback_url` (C-3) | Task 8: added CLI option + SAR worker derivation |
| `src/a2a/worker/tools/` directory doesn't exist (M-1) | Task 4: creates `__init__.py` in new directory |
| `error=` vs `content=` inconsistency (M-4) | Task 6: all `ToolResult` use `content=` consistently |
| Import from `router_agent.tools.base` instead of `worker_agent` (m-1) | Task 4: fixed import path |
| Spec §3/§8 `httpx_client` inconsistency (m-2) | Spec updated to short-lived client pattern |
| Spec data flow shows `push_config` (m-3) | Spec updated to use `AgentRegistry` derivation |
| `_HELP_TIMEOUT` naming (m-4) | Task 4: renamed to `HELP_TIMEOUT` |
