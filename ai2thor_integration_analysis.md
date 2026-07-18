# AI2Thor Integration Analysis: SAR Framework Abstraction Points

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     EXPERIMENT RUNNER (experiment.py)               │
│  Creates barrier → coordinator → workers → polls → cleanup          │
└──────┬────────────────────┬──────────────────────┬──────────────────┘
       │                    │                      │
       ▼                    ▼                      ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐
│ SARCoordinator│   │ SARWorker    │   │ SARBarrier           │
│ (coord.py)    │   │ (worker.py)  │   │ (barrier.py)         │
│               │   │              │   │                      │
│ Has: barrier  │   │ Has: barrier │   │ Wraps: SAREnv        │
│ Uses:         │   │ Uses:        │   │ Methods:             │
│ - state_prov │   │ - tools[]    │   │ - submit_action()    │
│ - tools[]     │   │ - state_prov │   │ - get_metrics()      │
│ - semantic_map│   │ - observer   │   │ - get_env_snapshot() │
│ - event_store │   │              │   │ - is_finished()      │
│               │   │              │   │ - stop()             │
└──────┬────────┘   └──────┬───────┘   │ - get_last_step_log()│
       │                   │            │ - get_current_obs()  │
       │                   │            └──────────────────────┘
       │                   │                      ▲
       │    ┌──────────────┘                      │
       │    │  Worker Tools call                  │
       │    │  barrier.submit_action()            │
       ▼    ▼                                     │
┌─────────────────────┐                           │
│  A2A Coordinator    │     A2A Worker            │
│  Server (generic)   │     Server (generic)      │
│                     │                           │
│ - RouterAgent(LLM)  │  - AgentAdapter           │
│ - TaskQueue         │  - StateProvider          │
│ - WorkerRegistry    │  - Tools (mix of env-     │
│ - AgentRegistry     │    specific & generic)     │
│ - WebSocket mgmt    │                           │
└─────────────────────┴───────────────────────────┘
```

---

## 1. The SARBarrier Interface — What Any New Barrier Must Implement

The `SARBarrier` (barrier.py, 422 lines) wraps `SAREnv` and serves as the **synchronization point** between environment execution and multi-agent orchestration. Here is the **mandatory interface**:

### Public Methods (required by consumers)

```python
class EnvironmentBarrier(ABC):
    """Any new environment barrier must implement this interface."""

    @abstractmethod
    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """
        Submit an action from one agent and wait for all agents to submit.
        Returns dict with keys:
          - observation (str): formatted observation text
          - agent_name (str): name of this agent
          - step (int): current env step counter
          - finished (bool): whether task is complete
          - success (bool): whether action succeeded
          - structured_observations (list[dict]): structured observation data
          - structured_position (tuple): agent's (x,y,z) position
          - structured_inventory (any): agent's inventory
        """
        ...

    @abstractmethod
    def is_finished(self) -> bool:
        """Return True when the overall task/mission is complete."""
        ...

    @abstractmethod
    def get_metrics(self) -> dict:
        """
        Return current task metrics. Used for:
          - Experiment poll loop checks: metrics["steps"]
          - Final metrics reporting
        Minimum keys: steps, finished
        """
        ...

    @abstractmethod
    def get_env_snapshot(self) -> dict:
        """
        Return full environment state snapshot. Used by:
          - QuerySARStateTool (coordinator oracle mode)
          - CoordinatorStateProvider._build_team_status()
        The snapshot structure is domain-specific — must contain agent
        positions/inventory as a minimum.
        """
        ...

    @abstractmethod
    def stop(self):
        """Clean up environment and wake any waiting workers."""
        ...

    # Optional but used by experiment runner:
    def get_last_step_log(self) -> dict:
        """
        Return data from the most recently executed step.
        Used by experiment.py poll loop for CSV logging.
        Keys used: actions, successes, observations, timeout_agents,
                    error_types, step_duration_ms, completed_subtasks_delta
        """
        ...

    # Optional but used by worker state provider & read-only tools:
    def get_current_obs(self, agent_idx: int) -> str:
        """Return the latest formatted observation for prompt injection."""
        ...
```

### Critical Implementation Details

The barrier has two major responsibilities:

1. **Action synchronization**: Multiple agents call `submit_action()` concurrently (from separate threads with separate asyncio event loops). The barrier collects all actions, executes `env.step()` atomically, then broadcasts observations back. Uses `threading.Event` and `threading.Lock` (not asyncio primitives) because workers are in different threads.

2. **Structured observations**: After each step, the barrier builds `structured_observations` — a list of `ObservationRecord`-compatible dicts with object_type, position, attributes, confidence. These flow through `WorkerReportPublisher` (dedup) → A2A push notifications → coordinator `SemanticMapStore.ingest_observation()`.

---

## 2. Relationship Between Worker Tools and the Barrier

### The Pattern

Every **action tool** (a tool that consumes an environment step) follows this pattern:

```python
class NavigateToTool(Tool):
    name = "navigate_to"
    
    def __init__(self, barrier, agent_idx: int):
        self._barrier = barrier
        self._agent_idx = agent_idx

    async def execute(self, target_id: str, **kwargs) -> ToolResult:
        action = f"NavigateTo({target_id})"
        result = await self._barrier.submit_action(self._agent_idx, action)
        obs = result["observation"]
        # Build custom content from barrier state
        return tool_result_from_barrier(result, overrides={...})
```

### Three Categories of Worker Tools

| Category | Tool | Depends on Barrier? | Reusable for AI2Thor? |
|---|---|---|---|
| **Action tools** (consume a step) | `NavigateToTool`, `MoveTool`, `ExploreTool`, `CarryPersonTool`, `DropOffPersonTool`, `GetSupplyTool`, `StoreSupplyTool`, `UseSupplyTool`, `ClearInventoryTool`, `NoOpTool` | **Yes** — requires `barrier.submit_action()` | **No** — must be reimplemented |
| **Read-only tools** (no step consumed) | `GetAgentStateTool` | **Yes** — requires `barrier.env.controller.get()`, `barrier.get_current_obs()` | **No** — must be reimplemented |
| **Generic tools** (no barrier dependency) | `ReportObservationTool`, `QuerySharedMemoryTool`, `FinishTaskTool`, `AskCoordinatorTool`, `ReadMailboxTool`, `A2ASendMailTool` | **No** — environment-agnostic | **Yes** — fully reusable |

**Total: ~15 worker tools, of which ~10 are SAR-specific and need reimplementation.**

### The `tool_result_from_barrier()` Helper

`_barrier_helpers.py` builds `ToolResult` objects that carry both:
- **content** (text observation for LLM consumption)
- **data** (structured dict with `observations`, `position`, `inventory` for the A2A push notification pipeline)

The `WorkerReportPublisher` applies observation dedup before the data reaches the coordinator.

### Tools Are Instantiated in `SARWorker.start()`

The `SARWorker` class (worker.py) creates tool instances from `SAR_WORKER_TOOLS` (the list), binding the barrier and agent_idx:

```python
for tool_cls in SAR_WORKER_TOOLS:
    if tool_cls.__name__ == "ReportObservationTool":
        tools.append(tool_cls(agent_name=self.agent_name, ...))
    elif tool_cls.__name__ == "QuerySharedMemoryTool":
        tools.append(tool_cls(semantic_map_url=http_url))
    elif ... generic tools ...
    else:
        tools.append(tool_cls(barrier=self._barrier, agent_idx=self.agent_idx))
```

---

## 3. What the Coordinator Expects from the Environment

### Direct Barrier Usage

`SARCoordinator` (coordinator.py) uses the barrier in these ways:

| Usage | Where | What's accessed |
|---|---|---|
| `self._barrier.env` | `_extract_prior_objects()` | `barrier.env.reservoirs`, `barrier.env.deposits`, `barrier.env.agent_names` |
| `self._barrier.env.checker.coverage` | `start()` | Ground-truth object names for MapRecall metric |
| `self._barrier._step_counter` | `_router_cb` callback | Step number for logging |
| `self._barrier.is_finished()` | Passed to state provider | Mission completion status |
| `self._barrier.get_env_snapshot()` | Used by QuerySARStateTool ≈ | Full env state dump |

### Coordinator Tools (SAR-specific)

| Tool | Purpose | Barrier Dependency |
|---|---|---|
| `QuerySARStateTool` | Oracle-mode state query | `barrier.get_env_snapshot()`, `barrier._step_counter`, `barrier.is_finished()` |
| `QuerySemanticMapTool` | Query the semantic map (no oracle) | None — uses SemanticMapStore directly |
| `QueryTeamStatusTool` | Worker status from semantic map | None — uses SemanticMapStore directly |
| `FinishTaskTool` | Signal mission complete | None |
| `SendMessageTool` | Dispatch tasks to workers | None — generic A2A builtin tool |

### Semantic Map Priors

In `SARCoordinator.start()`, the semantic map is initialized with environment priors:

```python
semantic_map.init_priors(
    reservoirs=[...],        # From barrier.env.reservoirs
    deposits=[...],          # From barrier.env.deposits
    agents=[...],            # From barrier.env.agent_names
    rules={"Chemical": "Sand", "Non-chemical": "Water"},
    step_budget={...},
    task_objective="Extinguish all fires and rescue all persons",
)
```

### CoordinatorServer's Direct Barrier References

The generic `CoordinatorServer` (src/a2a/coordinator/server.py) also holds optional barrier/semantic_map references:

- `self._barrier` — Set via `server.set_barrier(barrier)`. Used for `/map/state` SSE endpoint and experiment cancellation (`barrier.stop()`).
- `self._semantic_map` — Set via `server.set_semantic_map(semantic_map)`. Used for observation ingestion from worker push notifications.

### State Provider Expectations

`SARCoordinatorStateProvider.snapshot()` reads:
- `barrier._step_counter` — env step versioning
- `barrier.is_finished()` — mission state
- `semantic_map.get_step_budget()` — step budget
- `barrier.get_env_snapshot()` — live agent positions (semantic mode only)
- `event_store` — task state events
- `supervision_state_store` — supervision state
- `task_store` — task plan

The generic `StateProvider` protocol only requires:
```python
class StateProvider(Protocol):
    def snapshot(self, context_id: str | None = None) -> RuntimeState: ...
```

---

## 4. What the Experiment Runner Expects

The experiment runner (experiment.py) orchestrates the lifecycle:

### Creation Phase
1. Creates `SARBarrier(num_agents, scene, seed)` → must reset env
2. Creates `ExperimentLogger` with log directory
3. Creates `SARCoordinator(..., barrier=barrier, exp_logger=exp_logger, ...)`
4. Creates `SARWorker(..., barrier=barrier, exp_logger=exp_logger, ...)` for each agent

### Polling Phase
```python
while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
    await asyncio.sleep(poll_interval)
    # Check coordinator/A2A errors
    # Check wall-clock limit
    metrics = barrier.get_metrics()  # steps, coverage, transport_rate, finished
    
    # Log step data
    step_log = barrier.get_last_step_log()  # if available
    exp_logger.log_step(
        step_num=metrics["steps"],
        actions=step_log["actions"],
        successes=step_log["successes"],
        ...
    )
```

### Cleanup Phase
```python
barrier.stop()  # In finally block
```

### Must-Have Barrier Methods for Experiment Runner

| Method | When Called | Purpose |
|---|---|---|
| `SARBarrier(num_agents=N, ...)` | Start | Create + reset env |
| `barrier.is_finished()` | Every poll iteration | Check completion |
| `barrier.get_metrics()`["steps"] | Every poll iteration | Step limit check |
| `barrier.get_metrics()` | End | Final metrics |
| `barrier.get_last_step_log()` | Each new step | Step data for CSV logging |
| `barrier.stop()` | Cleanup | Shutdown |

---

## 5. Abstraction Layer Design — What's Reusable vs. Environment-Specific

### ✅ Fully Reusable (environment-agnostic)

| Module | Files | Purpose |
|---|---|---|
| **A2A Coordinator Server** | `src/a2a/coordinator/server.py` | FastAPI server, WS management, task queue, watchdog |
| **A2A Coordinator A2A Server** | `src/a2a/coordinator/a2a_server.py` | A2A protocol server |
| **RouterAgent** | `src/a2a/coordinator/router.py` | LLM task router |
| **A2A Worker Server** | `src/a2a/worker/a2a_server.py` | Worker HTTP + A2A server |
| **AgentAdapter** | `src/a2a/worker/agent_adapter.py` | Generic agent execution adapter |
| **CoordinatorClient** | `src/a2a/worker/coordinator_client.py` | WebSocket client for coordinator connection |
| **Builtin Coordinator Tools** | `src/a2a/builtin_tools/send_message.py`, `query_task_events.py`, `configure_team.py`, `send_mail.py` | Generic comm/team tools |
| **Builtin Worker Tools** | `src/a2a/worker/tools/ask_coordinator.py`, `read_mailbox.py`, `send_peer_mail.py` | Generic comm tools |
| **StateProvider Protocol** | `src/Agent/router_agent/state_provider.py` | `RuntimeState` DTO + `StateProvider` protocol |
| **ContextManager** | `src/Agent/router_agent/context.py` | Pinned state + recent window + compression |
| **SandboxPolicy** | `src/Agent/sandbox.py` | File system sandboxing |
| **ExperimentLogger** | `sar_orch/logger.py` | CSV/NDJSON logging — fully generic |
| **ObservationRecord / SemanticObject** | `sar_orch/semantic_map.py` | Domain model classes — domain-agnostic structures |
| `ObservationRecord` | | reporter, step, object_type, name, position, attributes, confidence |
| **WorkerReportPublisher** | `sar_orch/observation_publisher.py` | Client-side observation dedup — generic algorithm |
| **CoordinatorServer generic parts** | TaskQueue, WorkerRegistry, AgentRegistry, MeshGuide, TaskWatchdog, SupervisionStateStore, EventStore | All fully generic |

### 🔄 Partially Reusable (needs adaptation)

| Module | Reusable Part | Needs Change |
|---|---|---|
| **SemanticMapStore** | `ingest_observation()`, `_merge_locked()`, `snapshot()`, dedup logic | Domain types `(fire/person/reservoir/deposit/agent)` are SAR-specific. Object type mapping, attribute extraction, and `_unknowns_locked()` logic are all SAR-specific. **Recommend**: make object type registry configurable or subclass. |
| **SARCoordinator** | Lifecycle (`start`, `stop`, `submit_task`), callback wiring | `_extract_prior_objects()` reads `barrier.env.reservoirs/deposits`. Semantic map init with SAR-specific priors. Coordinator tool list. **Recommend**: parameterize init_priors data + tool list. |
| **SARWorker** | Lifecycle (`start`, `stop`, `clear_sessions`), callback wiring | Tool list selection (`SAR_WORKER_TOOLS`). Capabilities list. Action mapping function `_build_action()`. **Recommend**: make tool list and capabilities constructor parameters. |

### ❌ Must Be Reimplemented for AI2Thor

| Component | SAR Implementation | What's Needed for AI2Thor |
|---|---|---|
| **Barrier** | `sar_orch/barrier.py` (422 lines) | New `AI2ThorBarrier` wrapping AI2Thor controller. Same interface (submit_action, get_metrics, get_env_snapshot, is_finished, stop). |
| **Worker Action Tools** | 10 SAR tools (navigate_to.py, move.py, explore.py, carry_person.py, drop_off_person.py, get_supply.py, store_supply.py, use_supply.py, clear_inventory.py, no_op.py) | New action tools calling `barrier.submit_action()` with AI2Thor action syntax. Same pattern: `(barrier, agent_idx)` → `barrier.submit_action()`. |
| **Read-only Worker Tool** | `get_agent_state.py` | New GPS tool reading from AI2Thor barrier. |
| **Coordinator Oracle Tool** | `query_sar_state.py` | New oracle tool calling `barrier.get_env_snapshot()` with AI2Thor-specific rendering. |
| **Coordinator State Provider** | `sar_orch/coordinator_state_provider.py` | New provider reading from AI2Thor barrier. Same `StateProvider` protocol. |
| **Worker State Provider** | `sar_orch/worker_state_provider.py` | New provider reading from AI2Thor barrier. Same `StateProvider` protocol. |

---

## 6. Recommended Abstraction Design for AI2Thor

### Option A: Minimal Interface Class

Define a `Protocol` or `ABC` that any environment barrier must implement:

```python
# src/a2a/shared/environment_barrier.py
from abc import ABC, abstractmethod

class EnvironmentBarrier(ABC):
    """Interface that any environment (SAR, AI2Thor, etc.) must implement."""

    num_agents: int

    @abstractmethod
    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """Submit action, wait for all agents, return obs dict."""
        ...

    @abstractmethod
    def is_finished(self) -> bool:
        ...

    @abstractmethod
    def get_metrics(self) -> dict:
        ...

    @abstractmethod
    def get_env_snapshot(self) -> dict:
        ...

    @abstractmethod
    def stop(self):
        ...

    def get_last_step_log(self) -> dict:
        return {}

    def get_current_obs(self, agent_idx: int) -> str:
        return ""
```

### Option B: Parameterize All SAR-Specific Components

```python
# New ai2thor_orchestration/
# ├── barrier.py          -> AI2ThorBarrier(EnvironmentBarrier)
# ├── coordinator.py      -> Minimal wrapper around CoordinatorServer
# ├── worker.py           -> Minimal wrapper, accepts tool list + capabilities
# ├── tools/
# │   ├── worker/         -> AI2Thor action tools
# │   └── coordinator/    -> AI2Thor query tools (if any)
# ├── state_providers/
# │   ├── coordinator.py  -> AI2ThorCoordinatorStateProvider
# │   └── worker.py       -> AI2ThorWorkerStateProvider
# └── experiment.py       -> Run AI2Thor experiment
```

### Key Design Principle

**The A2A coordinator/worker framework does NOT import SAR-specific code.** The bridge is through:
1. **Barrier** — injected into coordinator/worker constructors as a plain object
2. **StateProvider** — implements the generic `StateProvider` protocol
3. **Tools** — list of environment-specific Tool instances passed to `extra_tools`
4. **Experiment runner** — imports everything and wires it together

This means the A2A core (`src/a2a/coordinator/server.py`, `src/a2a/worker/a2a_server.py`, etc.) is already environment-agnostic. The SAR-specific orchestration (`sar_orch/`) is a **usage layer** on top.

### What Changes for AI2Thor

| Change | Effort | What to Copy |
|---|---|---|
| `AI2ThorBarrier` | Medium (300-500 loc) | Copy `barrier.py` structure, replace `SAREnv` with AI2Thor API |
| Action tools (~10) | Medium (30-50 loc each) | Copy pattern from SAR tools, change action strings |
| Read-only tools (~2) | Small (30-50 loc each) | Copy `get_agent_state.py`, `query_sar_state.py` |
| State providers (2) | Small (150-200 loc each) | Copy `coordinator_state_provider.py`, `worker_state_provider.py` |
| Coordinator wrapper | Small (100-150 loc) | Copy `coordinator.py`, parameterize with AI2Thor tools/priors |
| Worker wrapper | Small (100-150 loc) | Copy `worker.py`, parameterize with AI2Thor tool list |
| Experiment runner | Small (100-150 loc) | Copy `experiment.py`, change class names |
| SemanticMapStore | Medium (100-200 loc modification) | Subclass or parameterize domain types |
| `__init__.py` tool lists | Small | Define `AI2THOR_WORKER_TOOLS` list |
| A2A core | **None** | Already environment-agnostic |

### Data Flow for AI2Thor (same as SAR)

```
Worker LLM → calls tool
    → tool.submit_action(agent_idx, action)
        → AI2ThorBarrier collects all N agent actions
        → barrier.execute_step() atomically
            → AI2ThorEnv.step(actions)
            → build structured observations
        → returns {observation, structured_observations, ...}
    → tool_result_from_barrier() builds ToolResult
        → content → LLM context
        → data → WorkerReportPublisher.dedup() → A2A push notification
            → CoordinatorServer receives notification
            → extract_auto_observations()
            → SemanticMapStore.ingest_observation()
            → available in next coordinator state snapshot
```

---

## Summary: Concrete Action Plan for AI2Thor Integration

1. **Define `EnvironmentBarrier` ABC/protocol** → `src/a2a/shared/environment_barrier.py`
2. **Implement `AI2ThorBarrier`** → ai2thor_orch/barrier.py (implements the interface)
3. **Reimplement worker action tools** → ai2thor_orch/tools/worker/*.py (same pattern, AI2Thor actions)
4. **Adapt state providers** → ai2thor_orch/state_providers/coordinator.py, worker.py
5. **Adapt coordinator wrapper** → ai2thor_orch/coordinator.py (tool list + priors)
6. **Adapt worker wrapper** → ai2thor_orch/worker.py (tool list)
7. **Adapt experiment runner** → ai2thor_orch/experiment.py
8. **Adapt SemanticMapStore** → Either subclass for AI2Thor object types or make configurable
9. **Everything in `src/a2a/`** → Zero changes needed
10. **`ExperimentLogger`** → Zero changes needed
11. **`WorkerReportPublisher`** → Zero changes needed
