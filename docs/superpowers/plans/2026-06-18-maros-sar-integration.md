# MARoS × LLaMAR SAR Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate MARoS distributed multi-LLM architecture (Coordinator + A2A Workers + @tool) with LLaMAR's SAR grid environment via a synchronous barrier pattern.

**Architecture:** SARBarrier collects per-agent actions → executes `env.step()` synchronously → broadcasts observations back to Workers. Workers are self-contained (no ROS 2), assembling MARoS `transport.py` + `@tool` + mini-agent. Coordinator uses `my_a2a` RouterAgent with SAR-specific tools.

**Tech Stack:** Python 3.10+, asyncio, MARoS transport.py/C2FixedMiniAgentAdapter/mini-agent, LLaMAR SAREnv/Controller, uvicorn + starlette (A2A HTTP), pytest

## Global Constraints

- No ROS 2 dependency in integration code — all Workers and Coordinator run without `rclpy`
- LLaMAR `SAR/` directory is read-only — never modify existing SAR files
- MARoS `transport.py`, `tool_decorator.py`, `my_a2a/coordinator/` are read-only — import only, no modification
- All new code in `LLaMAR/integration/` directory
- Python imports for MARoS components via `sys.path` injection (both repos are siblings)
- Agent names: Alice, Bob, Charlie, David, Emma, Finn (from `SAREnv.AGENT_NAMES`)
- Default grid: 3×3 (from `Coordinate.WIDTH=3, HEIGHT=3`)

---

### Task 1: Project scaffolding and import path setup

**Files:**
- Create: `integration/__init__.py`
- Create: `integration/sar_barrier.py` (skeleton)
- Create: `integration/sar_workers/__init__.py`
- Create: `integration/sar_workers/tools.py` (skeleton)
- Create: `integration/sar_workers/skills.py` (skeleton)
- Create: `integration/sar_workers/sar_worker.py` (skeleton)
- Create: `integration/sar_workers/prompt.md` (skeleton)
- Create: `integration/sar_workers/manifest.yaml` (skeleton)
- Create: `integration/coordinator/__init__.py`
- Create: `integration/coordinator/sar_coordinator.py` (skeleton)
- Create: `integration/coordinator/sar_router_tools.py` (skeleton)
- Create: `integration/experiment.py` (skeleton)

**Interfaces:**
- Produces: Directory structure that all subsequent tasks populate

- [ ] **Step 1: Create all directories**

```bash
mkdir -p /home/wyh/daily_work/LLaMAR/integration/sar_workers
mkdir -p /home/wyh/daily_work/LLaMAR/integration/coordinator
```

- [ ] **Step 2: Create `integration/__init__.py`**

```python
"""MARoS × LLaMAR SAR Integration — Distributed multi-LLM planning on SAR grid environment."""
```

- [ ] **Step 3: Create `integration/sar_workers/__init__.py`**

```python
"""SAR Workers — self-contained A2A workers for SAR agents (Alice, Bob, ...)."""
```

- [ ] **Step 4: Create `integration/coordinator/__init__.py`**

```python
"""SAR Coordinator — MARoS RouterAgent adapted for Search & Rescue task coordination."""
```

- [ ] **Step 5: Create skeleton files for all remaining modules**

```bash
touch /home/wyh/daily_work/LLaMAR/integration/sar_barrier.py
touch /home/wyh/daily_work/LLaMAR/integration/sar_workers/tools.py
touch /home/wyh/daily_work/LLaMAR/integration/sar_workers/skills.py
touch /home/wyh/daily_work/LLaMAR/integration/sar_workers/sar_worker.py
touch /home/wyh/daily_work/LLaMAR/integration/sar_workers/prompt.md
touch /home/wyh/daily_work/LLaMAR/integration/sar_workers/manifest.yaml
touch /home/wyh/daily_work/LLaMAR/integration/coordinator/sar_coordinator.py
touch /home/wyh/daily_work/LLaMAR/integration/coordinator/sar_router_tools.py
touch /home/wyh/daily_work/LLaMAR/integration/experiment.py
```

- [ ] **Step 6: Verify imports work — write a quick smoke test and run it**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/maros_ws/a2a_lib')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/my_a2a/src')
from integration import sar_barrier
print('Scaffolding OK')
"
```
Expected: `Scaffolding OK`

- [ ] **Step 7: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/
git commit -m "feat: scaffold integration/ directory structure

Creates sar_barrier, sar_workers, coordinator, experiment skeletons.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: SARBarrier — synchronous action collector + env wrapper

**Files:**
- Modify: `integration/sar_barrier.py` (full implementation)

**Interfaces:**
- Produces: `class SARBarrier`
  - `__init__(self, num_agents: int, scene: int = 1, seed: int = 42) -> None`
  - `async submit_action(self, agent_idx: int, action: str) -> dict` — returns per-agent obs dict
  - `_execute_step(self) -> None` — called internally when all actions collected
  - `get_current_obs(self, agent_idx: int) -> str` — returns formatted obs text for prompt injection
  - `is_finished(self) -> bool`
  - `get_metrics(self) -> dict` — `{"coverage": float, "transport_rate": float, "steps": int}`
  - `get_env_snapshot(self) -> dict` — all visible objects + agent states for Coordinator
  - `stop(self) -> None` — cleanup

- [ ] **Step 1: Write the full SARBarrier implementation**

```python
"""SARBarrier — synchronous action collector wrapping LLaMAR SAREnv."""
import asyncio
import sys
from pathlib import Path

# Ensure LLaMAR SAR is importable
_llamar_root = Path(__file__).resolve().parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

from SAR.env import SAREnv


class SARBarrier:
    """Collect per-agent actions, execute env.step() synchronously, broadcast observations.

    Wraps LLaMAR's SAREnv with an async barrier:
      1. Workers call submit_action(agent_idx, action) → await
      2. When all N agents have submitted → _execute_step()
      3. Observations distributed → awaiting Workers resume

    Timeout: if an agent hasn't submitted within 30s of the first submission,
    NoOp is auto-filled and the step proceeds.
    """

    STEP_TIMEOUT = 30.0  # seconds to wait for all agents before auto-NoOp

    def __init__(self, num_agents: int, scene: int = 1, seed: int = 42):
        if not (1 <= num_agents <= 6):
            raise ValueError(f"num_agents must be 1-6, got {num_agents}")

        self.num_agents = num_agents
        self.scene = scene
        self.seed = seed

        # Create and reset the SAR environment
        self.env = SAREnv(num_agents=num_agents, scene=scene, seed=seed)
        self.env.reset()

        # Per-step state
        self._step_counter: int = 0
        self._action_queue: dict[int, str] = {}
        self._current_obs: dict[int, str] = {}
        self._finished: bool = False

        # Async synchronization — re-created each step
        self._obs_events: list[asyncio.Event] = [
            asyncio.Event() for _ in range(num_agents)
        ]
        self._step_lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """Submit this agent's action and wait for all agents to submit.

        Returns a dict with the agent's observation and step metadata.
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(f"agent_idx {agent_idx} out of range [0, {self.num_agents})")

        # Register this agent's action
        async with self._step_lock:
            self._action_queue[agent_idx] = action
            all_submitted = len(self._action_queue) == self.num_agents

        # If we're the last agent, execute the step (sets all events)
        if all_submitted:
            await self._execute_step()
        else:
            # Wait for our specific event (set by _execute_step when all agents submit)
            # With timeout: if other agents never submit, auto-fill with NoOp
            try:
                await asyncio.wait_for(
                    self._obs_events[agent_idx].wait(),
                    timeout=self.STEP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                async with self._step_lock:
                    for i in range(self.num_agents):
                        if i not in self._action_queue:
                            self._action_queue[i] = "NoOp"
                await self._execute_step()

        # Return this agent's observation
        obs_text = self._current_obs.get(agent_idx, "")
        return {
            "observation": obs_text,
            "agent_name": self.env.agent_names[agent_idx],
            "step": self._step_counter,
            "finished": self._finished,
            "success": True,
        }

    def get_current_obs(self, agent_idx: int) -> str:
        """Return the latest formatted observation for prompt injection."""
        return self._current_obs.get(agent_idx, "No observation yet.")

    def is_finished(self) -> bool:
        """Check if the task is complete."""
        return self._finished

    def get_metrics(self) -> dict:
        """Return current task metrics."""
        return {
            "coverage": self.env.checker.get_coverage(),
            "transport_rate": self.env.checker.get_transport_rate(),
            "steps": self._step_counter,
            "finished": self._finished,
        }

    def get_env_snapshot(self) -> dict:
        """Return current environment state for Coordinator's query_sar_state tool.

        Includes all visible objects with positions, intensities, types, and agent states.
        """
        all_objects = self.env.controller.field.all_objects(expand=True, with_memory=False)
        snapshot = {
            "agents": [],
            "fires": [],
            "persons": [],
            "reservoirs": [],
            "deposits": [],
            "flammables": [],
        }
        for obj in all_objects:
            obj_dict = {
                "name": getattr(obj, "name", str(obj)),
                "position": getattr(obj, "position", None),
                "object_id": getattr(obj, "object_id", str(obj)),
                "type": type(obj).__name__,
            }
            type_name = type(obj).__name__
            if type_name == "AbsAgent":
                inv = self.env.controller.get_inventory_by_object(obj)
                obj_dict["inventory"] = inv
                snapshot["agents"].append(obj_dict)
            elif type_name == "Fire":
                obj_dict["average_intensity"] = str(getattr(obj, "average_intensity", "?"))
                obj_dict["fire_type"] = str(getattr(obj, "fire_type", "?"))
                snapshot["fires"].append(obj_dict)
            elif type_name == "Person":
                obj_dict["load"] = getattr(obj, "load", 2)
                obj_dict["status"] = str(getattr(obj, "status", "?"))
                snapshot["persons"].append(obj_dict)
            elif type_name == "Reservoir":
                obj_dict["resource_type"] = str(getattr(obj, "resource_type", "?"))
                snapshot["reservoirs"].append(obj_dict)
            elif type_name == "Deposit":
                obj_dict["inventory"] = str(getattr(obj, "inventory", {}))
                snapshot["deposits"].append(obj_dict)
            elif type_name == "Flammable":
                obj_dict["intensity"] = str(getattr(obj, "intensity", "?"))
                snapshot["flammables"].append(obj_dict)
        return snapshot

    def stop(self):
        """Clean up the environment."""
        if hasattr(self, "env"):
            self.env.stop()

    # ── Internal ────────────────────────────────────────────────────────────

    async def _execute_step(self):
        """Execute one env.step() with all collected actions, then broadcast obs."""
        # Collect actions in agent order
        actions = []
        for i in range(self.num_agents):
            actions.append(self._action_queue.get(i, "NoOp"))

        # Execute synchronously (env.step is blocking, run in thread)
        obs_text, act_successes = await asyncio.to_thread(self.env.step, actions)

        # Parse per-agent observations from env state
        for i in range(self.num_agents):
            # Use env.generate_obs_text to get formatted observation
            obs, _ = self.env.generate_obs_text(i)
            state = self.env.get_agent_state(i)
            full_obs = f"{obs}\n{state}"
            self._current_obs[i] = full_obs
            self._obs_events[i].set()

        self._step_counter += 1

        # Check task completion
        self._finished = self.env.checker.check_success()

        # Reset for next step
        self._action_queue.clear()
        self._obs_events = [asyncio.Event() for _ in range(self.num_agents)]
```

- [ ] **Step 2: Write the unit test for SARBarrier**

Create `integration/test_sar_barrier.py`:

```python
"""Unit tests for SARBarrier — requires no LLM, no Worker, just the barrier."""
import asyncio
import sys
from pathlib import Path

_llamar_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_llamar_root))

from integration.sar_barrier import SARBarrier


def test_barrier_creation():
    """Barrier creates SAREnv and resets successfully."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    assert barrier.num_agents == 2
    assert barrier.env.initialized
    assert barrier._step_counter == 0
    assert not barrier.is_finished()
    barrier.stop()


def test_barrier_snapshot():
    """get_env_snapshot returns plausible dict."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    snap = barrier.get_env_snapshot()
    assert "agents" in snap
    assert "fires" in snap
    assert "persons" in snap
    assert len(snap["agents"]) == 2
    barrier.stop()


def test_barrier_metrics():
    """get_metrics returns expected keys."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    metrics = barrier.get_metrics()
    assert "coverage" in metrics
    assert "transport_rate" in metrics
    assert "steps" in metrics
    assert metrics["steps"] == 0
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_two_agents():
    """Two agents submit actions → step executes → both get obs back."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(agent_idx, action):
        return await barrier.submit_action(agent_idx, action)

    # Run both agents concurrently
    results = await asyncio.gather(
        agent(0, "NoOp"),
        agent(1, "NoOp"),
    )

    assert len(results) == 2
    assert results[0]["step"] == 1
    assert results[1]["step"] == 1
    assert "observation" in results[0]
    assert "observation" in results[1]
    assert barrier._step_counter == 1
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_timeout():
    """If only one agent submits, NoOp is auto-filled after timeout."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    barrier.STEP_TIMEOUT = 0.5  # Short timeout for test

    result = await barrier.submit_action(0, "NoOp")
    # Should complete (second agent auto-NoOp), not hang
    assert result["step"] == 1
    barrier.stop()
```

- [ ] **Step 3: Run tests and verify they pass**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -m pytest integration/test_sar_barrier.py -v
```
Expected: 5 passed

- [ ] **Step 4: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/sar_barrier.py integration/test_sar_barrier.py
git commit -m "feat: SARBarrier — synchronous action collector + env wrapper

Implements async barrier pattern over LLaMAR SAREnv:
- submit_action() collects per-agent actions, executes env.step() when all ready
- 30s timeout with auto-NoOp fallback for stuck agents
- get_env_snapshot() for Coordinator query_sar_state
- Full unit test coverage (5 tests)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: SAR Tools — @tool functions for all 10 SAR actions

**Files:**
- Modify: `integration/sar_workers/tools.py` (full implementation)

**Interfaces:**
- Consumes: `SARBarrier.submit_action(agent_idx, action) -> dict`
- Produces: 10 async `@tool` functions, each calling `barrier.submit_action()`
  - `navigate_to(node, target_id: str) -> str`
  - `move(node, direction: str) -> str`
  - `explore(node) -> str`
  - `carry_person(node, person_id: str) -> str`
  - `drop_off_person(node, person_id: str, deposit_id: str) -> str`
  - `get_supply(node, source_id: str, supply_type: str) -> str`
  - `store_supply(node, deposit_id: str) -> str`
  - `use_supply(node, fire_id: str, supply_type: str) -> str`
  - `clear_inventory(node) -> str`
  - `no_op(node) -> str`

- [ ] **Step 1: Write tools.py**

```python
"""SAR domain @tool functions — each formats a LLaMAR action string and submits via barrier.

All tools follow the same pattern:
  1. Format LLaMAR action string
  2. await node._barrier.submit_action(node._agent_idx, action)
  3. Return the observation text for the LLM to reason about
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import MARoS @tool decorator (no ROS dependency)
_maros_a2a_lib = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

from a2a_lib.tool_decorator import tool


# ── Movement ────────────────────────────────────────────────────────────────


@tool(name="navigate_to", description="Navigate to an object by its ID. Use this to move toward any visible object.")
async def navigate_to(node, target_id: str) -> str:
    """Navigate to the specified object in the SAR grid.

    Args:
        target_id: ID of the target object (e.g., "WaterSource_1", "GreatFire_Region_1")
    """
    action = f"NavigateTo({target_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="move", description="Move one step in a cardinal direction or diagonal.")
async def move(node, direction: str) -> str:
    """Move the agent one step in the specified direction.

    Args:
        direction: Direction to move — Up, Down, Left, Right, UpLeft, UpRight, DownLeft, DownRight, Center
    """
    action = f"Move({direction})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="explore", description="Explore unknown surrounding area. Multiple steps at once.")
async def explore(node) -> str:
    """Explore the surrounding area by moving in multiple directions.

    Use this when you cannot see objects you expect nearby or need to discover new fires/people.
    """
    action = "Explore()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Person Rescue ───────────────────────────────────────────────────────────


@tool(name="carry_person", description="Pick up a trapped person. At least 2 agents must carry simultaneously to succeed.")
async def carry_person(node, person_id: str) -> str:
    """Pick up a trapped person. Requires coordination with other agents.

    Args:
        person_id: ID of the person to carry (e.g., "LostTimmy")
    """
    action = f"Carry({person_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="drop_off_person", description="Drop a carried person at a safe deposit location.")
async def drop_off_person(node, person_id: str, deposit_id: str) -> str:
    """Drop a carried person at a deposit. All carrying agents must be at the deposit.

    Args:
        person_id: ID of the person being carried
        deposit_id: ID of the deposit to drop them at
    """
    action = f"DropOff({person_id}, {deposit_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Supply Management ───────────────────────────────────────────────────────


@tool(name="get_supply", description="Collect firefighting supplies from a reservoir or deposit.")
async def get_supply(node, source_id: str, supply_type: str) -> str:
    """Get firefighting supply from a source.

    Args:
        source_id: ID of the reservoir or deposit to get supply from
        supply_type: Type of supply — "Water" or "Sand"
    """
    action = f"GetSupply({source_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="store_supply", description="Store your current supplies at a deposit.")
async def store_supply(node, deposit_id: str) -> str:
    """Store all carried supplies at a deposit.

    Args:
        deposit_id: ID of the deposit to store supplies at
    """
    action = f"StoreSupply({deposit_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="use_supply", description="Use firefighting supply on a fire to extinguish it.")
async def use_supply(node, fire_id: str, supply_type: str) -> str:
    """Use carried supply on a fire to reduce its intensity.

    Args:
        fire_id: ID of the fire to extinguish (use _Region suffix, e.g., "GreatFire_Region_1")
        supply_type: Type of supply to use — "Water" or "Sand"
    """
    action = f"UseSupply({fire_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="clear_inventory", description="Clear your entire inventory (drop all carried items).")
async def clear_inventory(node) -> str:
    """Drop all items from your inventory."""
    action = "ClearInventory()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Meta ────────────────────────────────────────────────────────────────────


@tool(name="no_op", description="Do nothing this step. Use when waiting for other agents or when your task is complete.")
async def no_op(node) -> str:
    """Take no action this step."""
    action = "NoOp"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Tool registry ───────────────────────────────────────────────────────────


SAR_TOOLS = [
    navigate_to,
    move,
    explore,
    carry_person,
    drop_off_person,
    get_supply,
    store_supply,
    use_supply,
    clear_inventory,
    no_op,
]
```

- [ ] **Step 2: Write skills.py**

```python
"""SAR Skill definitions — marries tools into coordinator-discoverable capabilities."""
from a2a_lib.skill import Skill
from integration.sar_workers.tools import (
    navigate_to, move, explore,
    carry_person, drop_off_person,
    get_supply, store_supply, use_supply, clear_inventory,
    no_op,
)


FIREFIGHTING_SKILL = Skill(
    name="firefighting",
    description="Extinguish fires by navigating to them and using appropriate supplies (water for non-chemical, sand for chemical)",
    tools=["navigate_to", "move", "get_supply", "use_supply", "clear_inventory"],
    examples=[
        "Navigate to WaterSource_1, get water, then go to GreatFire_Region_1 and use the water on it",
        "Get sand from SandReservoir_1, navigate to ChemicalFire_Region_1, use sand on it",
    ],
)

RESCUE_SKILL = Skill(
    name="rescue",
    description="Rescue trapped persons by carrying them (requires 2+ agents) and dropping them at a deposit",
    tools=["navigate_to", "move", "carry_person", "drop_off_person"],
    examples=[
        "Go to LostTimmy, carry them, then drop them off at Deposit_1",
        "Navigate to LostTimmy and wait for another agent before carrying",
    ],
)

SUPPLY_CHAIN_SKILL = Skill(
    name="supply_chain",
    description="Manage supplies — collect from reservoirs, store at deposits for other agents",
    tools=["navigate_to", "move", "get_supply", "store_supply", "clear_inventory"],
    examples=[
        "Get water from WaterSource_1 and store it at Deposit_1 for the firefighter",
    ],
)

EXPLORATION_SKILL = Skill(
    name="exploration",
    description="Explore unknown areas to discover fires, persons, or resources",
    tools=["move", "explore"],
    examples=[
        "Explore the area to find undiscovered fires",
    ],
)

SAR_SKILLS = [FIREFIGHTING_SKILL, RESCUE_SKILL, SUPPLY_CHAIN_SKILL, EXPLORATION_SKILL]
```

- [ ] **Step 3: Verify tools import correctly**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/maros_ws/a2a_lib')
from integration.sar_workers.tools import SAR_TOOLS
print(f'Loaded {len(SAR_TOOLS)} tools:')
for t in SAR_TOOLS:
    print(f'  {t.name}: {t.description[:60]}...')
"
```
Expected: `Loaded 10 tools:` with list of tool names

- [ ] **Step 4: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/sar_workers/tools.py integration/sar_workers/skills.py
git commit -m "feat: SAR @tool functions — 10 actions mapped to LLaMAR SAR

navigate_to, move, explore, carry_person, drop_off_person,
get_supply, store_supply, use_supply, clear_inventory, no_op.
Plus 4 skills: firefighting, rescue, supply_chain, exploration.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: SARWorker — self-contained A2A worker per agent

**Files:**
- Modify: `integration/sar_workers/sar_worker.py` (full implementation)
- Modify: `integration/sar_workers/prompt.md` (system prompt template)
- Modify: `integration/sar_workers/manifest.yaml` (metadata)

**Interfaces:**
- Consumes: `SARBarrier`, `SAR_TOOLS`, `SAR_SKILLS`, MARoS `transport.py` (`C2FixedMiniAgentAdapter`, `start_a2a_transport`)
- Produces: `class SARWorker`
  - `__init__(self, agent_name: str, agent_idx: int, barrier: SARBarrier, port: int, coordinator_url: str) -> None`
  - `start(self) -> None` — launch A2A HTTP server (non-blocking)
  - `stop(self) -> None` — graceful shutdown

- [ ] **Step 1: Create MockNode for transport compatibility**

`start_a2a_transport()` requires a `node` parameter with `get_logger()` and `_get_system_prompt_with_history()`. We inject a lightweight mock:

```python
"""SARWorker — self-contained A2A Worker per SAR agent. No ROS 2 dependency."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# ── Import path setup ───────────────────────────────────────────────────────
_llamar_root = Path(__file__).resolve().parent.parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_a2a_lib = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

from integration.sar_workers.tools import SAR_TOOLS
from integration.sar_workers.skills import SAR_SKILLS

logger = logging.getLogger(__name__)


class _MockNode:
    """Lightweight mock of ROS 2 Node for transport.py compatibility.

    start_a2a_transport() calls:
      - node.get_logger().warning(...)  → redirect to logging
      - node._get_system_prompt_with_history(prompt) → return prompt as-is

    No rclpy needed.
    """

    class _MockLogger:
        def warning(self, msg, *args, **kwargs):
            logger.warning(msg, *args, **kwargs)
        def info(self, msg, *args, **kwargs):
            logger.info(msg, *args, **kwargs)
        def error(self, msg, *args, **kwargs):
            logger.error(msg, *args, **kwargs)

    def __init__(self, name: str = "sar_worker"):
        self._name = name
        self._logger = self._MockLogger()

    def get_logger(self):
        return self._logger

    def _get_system_prompt_with_history(self, base_prompt: str) -> str:
        """Return the prompt as-is (no history file merge)."""
        return base_prompt


class SARWorker:
    """Self-contained A2A Worker for one SAR agent.

    Each SARWorker:
      - Has a reference to the shared SARBarrier for action submission
      - Runs an A2A HTTP server (via MARoS transport.py) for Coordinator communication
      - Uses mini-agent for LLM ReAct loop with SAR tools
      - Dynamically rebuilds system prompt with latest observations

    No ROS 2 dependency — mocks the Node interface for transport.py.
    """

    def __init__(
        self,
        agent_name: str,
        agent_idx: int,
        barrier,               # SARBarrier
        port: int,
        coordinator_url: str = "ws://localhost:8080",
        model: str = "deepseek-v4-flash",
    ):
        self.agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port
        self._coordinator_url = coordinator_url
        self._model = model

        # Bind tools to self so @tool functions receive this worker as 'node'
        # (node._barrier and node._agent_idx work in tool bodies)
        self._tools = [t.bind(self) for t in SAR_TOOLS]

        # Build initial system prompt
        self._system_prompt = self._build_system_prompt()

        # Create mock node for transport.py
        self._mock_node = _MockNode(name=f"sar_{agent_name.lower()}")

        # Transport server handles (set by start())
        self._server = None
        self._a2a_thread = None
        self._ws_thread = None

        # Current subtask (set by Coordinator via A2A)
        self._current_subtask = "No subtask assigned yet."

    def _build_system_prompt(self) -> str:
        """Build system prompt with current observation injected."""
        base = _load_prompt_template()
        obs = self._barrier.get_current_obs(self._agent_idx)
        subtask_info = f"\n\n## Current Subtask\n{self._current_subtask}"
        return base + "\n\n## Current Environment State\n" + obs + subtask_info

    def update_subtask(self, subtask: str):
        """Called by Coordinator via A2A to set the current subtask."""
        self._current_subtask = subtask

    def start(self):
        """Start the A2A HTTP server (non-blocking, runs in background threads)."""
        from a2a_lib.transport import start_a2a_transport

        def _prompt_factory() -> str:
            return self._build_system_prompt()

        self._server, self._a2a_thread, self._ws_thread = start_a2a_transport(
            node=self._mock_node,
            worker_id=self.agent_name,
            port=self._port,
            coordinator_url=self._coordinator_url,
            skills=SAR_SKILLS,
            backend="mini_agent",
            model=self._model,
            tools=self._tools,
            system_prompt=self._system_prompt,
            map_client_factory=None,  # No map_server in SAR
            task_logger=None,          # Optional: could wire TaskLogger later
        )

        if self._server is None:
            logger.warning(
                "A2A server not started (a2a.server unavailable). "
                "Worker %s running in degraded mode.",
                self.agent_name,
            )

    def stop(self):
        """Graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True


# ── Prompt template ─────────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"
_DEFAULT_PROMPT = """You are a search and rescue robot in a grid environment.
Your job is to help extinguish fires and rescue trapped persons.
Use your tools to navigate, collect supplies, fight fires, and carry people to safety."""


def _load_prompt_template() -> str:
    """Load the system prompt template from prompt.md."""
    try:
        return _PROMPT_PATH.read_text()
    except FileNotFoundError:
        logger.warning("prompt.md not found at %s, using default", _PROMPT_PATH)
        return _DEFAULT_PROMPT
```

- [ ] **Step 2: Write prompt.md**

```markdown
You are a search and rescue robot named {agent_name} operating in a grid environment.

## Your Mission
Work with other robots to:
1. EXTINGUISH all fires — navigate to fires and use supplies (water/sand) on them
2. RESCUE all trapped persons — carry them (requires 2+ robots) and drop them at deposits

## Environment Rules
- The grid is 3x3. Positions use (x, y) coordinates.
- Fires have intensity levels: none → low → medium → high. They grow over time.
- Fire types: "non-chemical" (water or sand works) and "chemical" (only sand works).
- Each fire has multiple regions (e.g., GreatFire_Region_1, GreatFire_Region_2) — all must be extinguished.
- Persons need at least 2 robots carrying simultaneously to be rescued.
- Reservoirs provide infinite water or sand when you call get_supply there.
- Your inventory capacity is 3 items.

## Available Tools
- `navigate_to(target_id)` — Move to an object by ID
- `move(direction)` — Move one step (Up/Down/Left/Right + diagonals)
- `explore()` — Explore unknown surrounding area
- `get_supply(source_id, supply_type)` — Collect Water or Sand from a reservoir/deposit
- `use_supply(fire_id, supply_type)` — Use carried supply on a fire
- `carry_person(person_id)` — Pick up a trapped person (needs 2+ robots)
- `drop_off_person(person_id, deposit_id)` — Drop person at safe deposit
- `store_supply(deposit_id)` — Store your supplies at a deposit
- `clear_inventory()` — Drop everything you're carrying
- `no_op()` — Do nothing this step

## Strategy Tips
- Check the observation carefully — it tells you what's around you and globally visible
- Coordinate with other robots: if someone else is getting water, you might navigate to the fire and wait
- For person rescue, make sure another robot is also at the person's location before calling carry_person
- Use no_op when waiting for other robots to arrive or when your subtask is complete
```

- [ ] **Step 3: Write manifest.yaml**

```yaml
# SAR Worker manifest — per-agent metadata for Coordinator discovery
robot_id: "{{agent_name}}"
port: "{{port}}"
namespace: "sar"
skills:
  - firefighting
  - rescue
  - supply_chain
  - exploration
capabilities:
  - navigation
  - fire_suppression
  - person_rescue
  - supply_management
  - exploration
```

- [ ] **Step 4: Verify SARWorker can be imported and instantiated (without starting server)**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/maros_ws/a2a_lib')

from integration.sar_workers.sar_worker import SARWorker
from integration.sar_barrier import SARBarrier

barrier = SARBarrier(num_agents=2, scene=1, seed=42)
worker = SARWorker(
    agent_name='Alice',
    agent_idx=0,
    barrier=barrier,
    port=8191,
    coordinator_url='ws://localhost:8080',
)
print(f'Worker: {worker.agent_name}, port={worker._port}')
print('System prompt length:', len(worker._system_prompt))
print('SARWorker instantiation OK')
"
```
Expected: `SARWorker instantiation OK` with prompt length > 500

- [ ] **Step 5: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/sar_workers/sar_worker.py integration/sar_workers/prompt.md integration/sar_workers/manifest.yaml
git commit -m "feat: SARWorker — self-contained A2A worker per SAR agent

No ROS 2 dependency — uses MockNode for transport.py compatibility.
Dynamically rebuilds system prompt with latest barrier observations.
Includes prompt.md with full SAR domain knowledge.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Coordinator — SAR task coordination with query_sar_state tool

**Files:**
- Modify: `integration/coordinator/sar_router_tools.py` (full implementation)
- Modify: `integration/coordinator/sar_coordinator.py` (full implementation)

**Interfaces:**
- Consumes: `SARBarrier.get_env_snapshot()`, MARoS `my_a2a` (RouterAgent, CoordinatorServer)
- Produces: `class SARCoordinator` — starts the MARoS Coordinator with SAR-specific config
  - `__init__(self, barrier: SARBarrier, agent_names: list[str], worker_ports: dict[str, int]) -> None`
  - `start(self) -> None`
  - `stop(self) -> None`

- [ ] **Step 1: Write sar_router_tools.py**

```python
"""SAR-specific RouterAgent tools — replaces map_server query_map with SARBarrier query_sar_state."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class QuerySARStateTool:
    """Router tool: query the current SAR environment state.

    Replaces MARoS's query_map tool. Queries SARBarrier directly instead of ROS map_server.
    """

    def __init__(self, barrier):
        self._barrier = barrier

    @property
    def name(self) -> str:
        return "query_sar_state"

    @property
    def description(self) -> str:
        return (
            "Get the current state of the SAR environment. "
            "Returns all fires (position, intensity, type), persons (position, status), "
            "reservoirs (position, resource_type), deposits, and agent states (position, inventory). "
            "Use this to understand the current situation before assigning subtasks."
        )

    async def execute(self, **kwargs) -> str:
        """Execute the query and return formatted state."""
        snapshot = self._barrier.get_env_snapshot()
        return json.dumps(snapshot, indent=2, default=str)

    def to_schema(self) -> dict:
        """Return OpenAI tool schema for RouterAgent."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }
```

- [ ] **Step 2: Write sar_coordinator.py**

```python
"""SAR Coordinator — MARoS RouterAgent adapted for Search & Rescue task coordination."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

# ── Import path setup ───────────────────────────────────────────────────────
_llamar_root = Path(__file__).resolve().parent.parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_my_a2a = Path("/home/wyh/daily_work/MARoS/my_a2a/src")
if str(_maros_my_a2a) not in sys.path:
    sys.path.insert(0, str(_maros_my_a2a))

logger = logging.getLogger(__name__)

SAR_COORDINATOR_SYSTEM_PROMPT = """You are a Search & Rescue task coordinator. Your job is to:

1. Receive rescue mission descriptions from the user
2. Break them into subtasks assigned to specific rescue robots ({agent_names})
3. Monitor subtask completion and dynamically reassign as the situation changes

## Available Robots and Their Capabilities
{agent_descriptions}

## How to Coordinate
- Use `query_sar_state` to see the current environment: fires, persons, resources, agent positions
- Use `push_task` to assign a subtask to a specific robot
- Use `wait_for_result` to wait until a robot completes its subtask
- Use `cancel_task` if a robot's task is no longer relevant
- Use `note` to record important coordination decisions

## Task Priority
1. **Life safety first**: Rescue trapped persons before fighting fires
2. **Containment**: Prevent fires from spreading (extinguish medium+ intensity fires)
3. **Efficiency**: Have some robots collect supplies while others fight fires

## Environment Knowledge
- Fires spread when intensity reaches MEDIUM or higher
- Chemical fires can ONLY be extinguished with sand
- Non-chemical fires can be extinguished with water OR sand
- Persons need at least 2 robots to carry simultaneously
- Each robot can carry up to 3 supply items
- Reservoirs provide UNLIMITED water or sand
"""


class SARCoordinator:
    """Launch MARoS Coordinator with SAR-specific tools and configuration.

    Uses my_a2a's CoordinatorServer directly (bypasses ROS 2 ros_node wrapper).
    """

    def __init__(
        self,
        barrier,                        # SARBarrier
        agent_names: list[str],         # ["Alice", "Bob", ...]
        worker_ports: dict[str, int],   # {"Alice": 8191, "Bob": 8192, ...}
        port: int = 8080,
        model: str = "claude-opus-4-5",
    ):
        self._barrier = barrier
        self._agent_names = agent_names
        self._worker_ports = worker_ports
        self._port = port
        self._model = model

        # Build agent descriptions for prompt
        agent_descs = []
        for name in agent_names:
            port = worker_ports.get(name, 8190)
            agent_descs.append(
                f"- **{name}**: SAR rescue robot. A2A endpoint at http://localhost:{port}/. "
                f"Can navigate, collect supplies, extinguish fires, carry persons."
            )
        self._system_prompt = SAR_COORDINATOR_SYSTEM_PROMPT.format(
            agent_names=", ".join(agent_names),
            agent_descriptions="\n".join(agent_descs),
        )

        # Build SAR-specific router tools
        from integration.coordinator.sar_router_tools import QuerySARStateTool
        self._sar_tools = [QuerySARStateTool(barrier)]

        self._server: Optional[Any] = None

    async def start(self):
        """Start the Coordinator server with SAR configuration.

        Uses my_a2a's CoordinatorServer but injects SAR-specific system prompt
        and the query_sar_state tool.
        """
        # Import the MARoS coordinator server
        from openharness_a2a.coordinator.server import CoordinatorServer

        # Configure with SAR-specific settings
        self._server = CoordinatorServer(
            port=self._port,
            model=self._model,
            system_prompt_override=self._system_prompt,
            extra_tools=self._sar_tools,
        )

        logger.info(
            "SAR Coordinator starting on port %d with agents: %s",
            self._port,
            ", ".join(self._agent_names),
        )

        # CoordinatorServer.start() runs the FastAPI + WebSocket server
        await self._server.start()
        logger.info("SAR Coordinator started successfully")

    async def stop(self):
        """Graceful shutdown."""
        if self._server is not None:
            await self._server.stop()
            logger.info("SAR Coordinator stopped")
```

- [ ] **Step 3: Verify imports resolve correctly**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/my_a2a/src')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/maros_ws/a2a_lib')

from integration.sar_barrier import SARBarrier
from integration.coordinator.sar_router_tools import QuerySARStateTool

barrier = SARBarrier(num_agents=2, scene=1, seed=42)
tool = QuerySARStateTool(barrier)
print(f'Tool: {tool.name}')
print(f'Schema keys: {list(tool.to_schema().keys())}')
print('Coordinator tools import OK')
"
```
Expected: `Coordinator tools import OK`

- [ ] **Step 4: Verify coordinator can be instantiated**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/my_a2a/src')
sys.path.insert(0, '/home/wyh/daily_work/MARoS/maros_ws/a2a_lib')

from integration.sar_barrier import SARBarrier
from integration.coordinator.sar_coordinator import SARCoordinator

barrier = SARBarrier(num_agents=2, scene=1, seed=42)
coordinator = SARCoordinator(
    barrier=barrier,
    agent_names=['Alice', 'Bob'],
    worker_ports={'Alice': 8191, 'Bob': 8192},
    port=8080,
)
print(f'Coordinator configured for agents: {coordinator._agent_names}')
print('Prompt includes agent names:', all(n in coordinator._system_prompt for n in ['Alice', 'Bob']))
print('SARCoordinator instantiation OK')
"
```
Expected: `SARCoordinator instantiation OK`

- [ ] **Step 5: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/coordinator/sar_router_tools.py integration/coordinator/sar_coordinator.py
git commit -m "feat: SAR Coordinator — RouterAgent with query_sar_state tool

Adapts MARoS Coordinator for SAR: system prompt with rescue priorities,
QuerySARStateTool replacing ROS map_server query, per-agent descriptions.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Experiment script — end-to-end run

**Files:**
- Modify: `integration/experiment.py` (full implementation)

**Interfaces:**
- Consumes: `SARBarrier`, `SARWorker`, `SARCoordinator`
- Produces: Runnable experiment script with CLI args: `--scene`, `--agents`, `--seed`

- [ ] **Step 1: Write experiment.py**

```python
#!/usr/bin/env python3
"""End-to-end MARoS × LLaMAR SAR experiment runner.

Usage:
    python3 integration/experiment.py --scene=1 --agents=3 --seed=42

Launches:
    1. SARBarrier (LLaMAR SAREnv)
    2. N × SARWorker (one per agent, per-agent LLM ReAct loop)
    3. SARCoordinator (MARoS RouterAgent task decomposition)

The Coordinator receives a task description (e.g., "Extinguish all fires and
rescue all persons"), decomposes it, and pushes subtasks to Workers.
Workers execute actions through the SARBarrier synchronously.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# ── Import path setup ───────────────────────────────────────────────────────
_llamar_root = Path(__file__).resolve().parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_a2a_lib = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

_maros_my_a2a = Path("/home/wyh/daily_work/MARoS/my_a2a/src")
if str(_maros_my_a2a) not in sys.path:
    sys.path.insert(0, str(_maros_my_a2a))

from integration.sar_barrier import SARBarrier
from integration.sar_workers.sar_worker import SARWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")


# Port assignments for up to 6 agents
AGENT_PORTS = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    coordinator_model: str = "claude-opus-4-5",
) -> dict:
    """Run one full SAR experiment.

    Returns metrics dict with keys: finished, steps, coverage, transport_rate, elapsed_seconds.
    """
    agent_names = SARWorker._determine_names(num_agents) if False else [
        "Alice", "Bob", "Charlie", "David", "Emma", "Finn"
    ][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Agent model: %s, Coordinator model: %s", model, coordinator_model)
    logger.info("=" * 60)

    # 1. Create barrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    logger.info("SARBarrier initialized — env.task_timeout=%d", barrier.env.task_timeout)

    # 2. Create workers
    workers = {}
    for i, name in enumerate(agent_names):
        port = AGENT_PORTS[name]
        worker = SARWorker(
            agent_name=name,
            agent_idx=i,
            barrier=barrier,
            port=port,
            coordinator_url=f"ws://localhost:{COORDINATOR_PORT}",
            model=model,
        )
        workers[name] = worker
        worker.start()
        logger.info("Worker %s started on port %d", name, port)

    # Give workers a moment to start their HTTP servers
    await asyncio.sleep(1.0)

    # 3. Start coordinator
    from integration.coordinator.sar_coordinator import SARCoordinator
    coordinator = SARCoordinator(
        barrier=barrier,
        agent_names=agent_names,
        worker_ports={name: AGENT_PORTS[name] for name in agent_names},
        port=COORDINATOR_PORT,
        model=coordinator_model,
    )
    logger.info("SARCoordinator starting on port %d", COORDINATOR_PORT)

    start_time = time.time()

    try:
        # Start coordinator (this typically blocks on its own event loop)
        # In practice, run coordinator in a separate task
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for task completion or timeout
        task_timeout = barrier.env.task_timeout
        poll_interval = 2.0
        elapsed = 0.0

        while not barrier.is_finished() and elapsed < task_timeout:
            await asyncio.sleep(poll_interval)
            elapsed = time.time() - start_time
            metrics = barrier.get_metrics()
            logger.info(
                "Step %d | Coverage: %.2f | Transport: %.2f | Finished: %s",
                metrics["steps"],
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
            )

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        if barrier.is_finished():
            logger.info("TASK COMPLETED in %.1f seconds, %d steps", elapsed_total, final_metrics["steps"])
        else:
            logger.warning("TASK TIMEOUT after %.1f seconds, %d steps", elapsed_total, final_metrics["steps"])

        return final_metrics

    finally:
        # Cleanup
        for worker in workers.values():
            worker.stop()
        await coordinator.stop()
        barrier.stop()
        logger.info("Cleanup complete")


def main():
    parser = argparse.ArgumentParser(description="MARoS × LLaMAR SAR Experiment")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument("--agents", type=int, default=2, help="Number of agents (1-6)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash", help="LLM model for workers")
    parser.add_argument("--coordinator-model", type=str, default="claude-opus-4-5", help="LLM model for coordinator")
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            coordinator_model=args.coordinator_model,
        )
    )

    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS")
    print("=" * 60)
    print(f"  Finished:       {metrics['finished']}")
    print(f"  Steps:          {metrics['steps']}")
    print(f"  Coverage:       {metrics['coverage']:.2f}")
    print(f"  Transport Rate: {metrics['transport_rate']:.2f}")
    print(f"  Elapsed:        {metrics['elapsed_seconds']:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify experiment script parses args correctly**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 integration/experiment.py --help
```
Expected: argparse help output with --scene, --agents, --seed, --model, --coordinator-model

- [ ] **Step 3: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/experiment.py
git commit -m "feat: experiment.py — end-to-end MARoS × LLaMAR SAR runner

Launches Barrier → Workers → Coordinator in sequence.
Polls barrier metrics until task completion or timeout.
CLI: --scene, --agents, --seed, --model, --coordinator-model.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: End-to-end smoke test — simple 2-agent run

**Files:**
- Create: `integration/test_integration.py` (basic smoke test)

**Interfaces:**
- Consumes: All previous tasks
- Produces: Integration test that verifies the full pipeline starts without crashing

- [ ] **Step 1: Write test_integration.py**

```python
"""Minimal integration smoke test — verifies the full pipeline starts."""
import asyncio
import sys
from pathlib import Path

import pytest

_llamar_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_llamar_root))
sys.path.insert(0, "/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
sys.path.insert(0, "/home/wyh/daily_work/MARoS/my_a2a/src")

from integration.sar_barrier import SARBarrier
from integration.sar_workers.sar_worker import SARWorker
from integration.coordinator.sar_coordinator import SARCoordinator
from integration.coordinator.sar_router_tools import QuerySARStateTool


def test_barrier_worker_tool_chain():
    """Verify Barrier → Tools → Worker instantiation chain works end-to-end."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    worker_alice = SARWorker(
        agent_name="Alice",
        agent_idx=0,
        barrier=barrier,
        port=8191,
        coordinator_url="ws://localhost:8080",
    )
    worker_bob = SARWorker(
        agent_name="Bob",
        agent_idx=1,
        barrier=barrier,
        port=8192,
        coordinator_url="ws://localhost:8080",
    )

    # Verify workers have bound tools (tools are FunctionTool instances with _bound_node set)
    assert len(worker_alice._tools) == 10
    assert worker_alice.agent_name == "Alice"
    assert worker_bob.agent_name == "Bob"

    # Verify coordinator tool works
    tool = QuerySARStateTool(barrier)
    assert tool.name == "query_sar_state"

    barrier.stop()


@pytest.mark.asyncio
async def test_two_agents_submit_and_get_obs():
    """Alice and Bob both submit NoOp and get observations back."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(idx):
        return await barrier.submit_action(idx, "NoOp")

    r0, r1 = await asyncio.gather(agent(0), agent(1))

    assert "observation" in r0
    assert "observation" in r1
    assert "Alice" in r0["observation"] or "I am at" in r0["observation"]
    assert "Bob" in r1["observation"] or "I am at" in r1["observation"]

    barrier.stop()
```

- [ ] **Step 2: Run the integration smoke test**

```bash
cd /home/wyh/daily_work/LLaMAR && python3 -m pytest integration/test_integration.py -v
```
Expected: tests pass (or skip if network/LLM unavailable for full chain)

- [ ] **Step 3: Commit**

```bash
cd /home/wyh/daily_work/LLaMAR
git add integration/test_integration.py
git commit -m "test: integration smoke test for Barrier→Worker→Coordinator chain

Verifies instantiation chain and tool binding work end-to-end.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Implementation Order

```
Task 1 (scaffold) → Task 2 (SARBarrier) → Task 3 (tools+skills)
                                          ↘
                                            Task 4 (SARWorker) → Task 5 (Coordinator)
                                                                     ↘
                                                                       Task 6 (experiment.py)
                                                                          ↘
                                                                            Task 7 (smoke test)
```

Tasks 3 and 4 can be done in parallel after Task 2. Tasks 5, 6, 7 are sequential.
