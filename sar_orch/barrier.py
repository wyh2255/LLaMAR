"""SARBarrier -- synchronous action collector wrapping LLaMAR SAREnv.

Implements a multi-agent synchronization barrier: collects actions from all
agents, executes env.step() atomically, then broadcasts observations back.

Uses threading.Event and threading.Lock (not asyncio primitives) because
workers run in separate threads with separate asyncio event loops — asyncio
sync primitives are NOT safe across event loops in different threads.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

# SAR/ must be on sys.path because it uses flat imports (not a proper package)
_sar_dir = Path(__file__).resolve().parent.parent / "SAR"
if str(_sar_dir) not in sys.path:
    sys.path.insert(0, str(_sar_dir))

from env import SAREnv

# Type mapping from env object class_name() to ObservationRecord object_type
_OBS_TYPE_MAP = {
    "Fire": "fire",
    "Flammable": "fire",
    "Person": "person",
    "Reservoir": "reservoir",
    "Deposit": "deposit",
    "AbsAgent": "agent",
}


def _obs_position(obj_dict: dict) -> tuple[int, int, int] | None:
    """Convert _wrap_object_readable position dict to (x,y,z) tuple."""
    pos = obj_dict.get("position")
    if not isinstance(pos, dict):
        return None
    coords = []
    for axis in ("x", "y", "z"):
        v = pos.get(axis)
        if v is None:
            return None
        coords.append(int(v))
    return tuple(coords)


def _extract_obs_attributes(obj_dict: dict) -> dict:
    """Extract type-specific attributes from a _wrap_object_readable dict."""
    attrs = {}
    tp = obj_dict.get("type", "")
    if tp == "Flammable":
        attrs["intensity"] = obj_dict.get("intensity", "?")
        attrs["fire_type"] = obj_dict.get("fire_type", "?")
        attrs["parent_fire"] = obj_dict.get("parent_fire", "")
    elif tp == "Fire":
        attrs["average_intensity"] = obj_dict.get("average_intensity", "?")
        attrs["fire_type"] = obj_dict.get("fire_type", "?")
    elif tp == "Person":
        attrs["status"] = obj_dict.get("status", "unknown")
        attrs["load"] = obj_dict.get("load", 2)
    elif tp == "Reservoir":
        attrs["resource_type"] = obj_dict.get("resource_type", "?")
    elif tp == "Deposit" or tp == "AbsAgent":
        attrs["inventory"] = str(obj_dict.get("inventory", ""))
    return attrs


class SARBarrier:
    """Collect per-agent actions, execute env.step() synchronously, broadcast observations.

    Wraps LLaMAR's SAREnv with a threading-based barrier:
      1. Workers call submit_action(agent_idx, action) -> await
      2. When all N agents have submitted -> _execute_step()
      3. Observations distributed -> awaiting Workers resume

    Timeout: if an agent hasn't submitted within 60s of the first submission,
    NoOp is auto-filled and the step proceeds. The timeout is tunable via the
    SAR_STEP_TIMEOUT environment variable (seconds; default 60) — raise it
    only when the LLM gateway is rate-limiting and agents need longer to
    produce an action; keep it identical across A/B comparison runs.
    """

    STEP_TIMEOUT: float = float(os.environ.get("SAR_STEP_TIMEOUT", "60.0"))  # seconds

    def __init__(self, num_agents: int, scene: int = 1, seed: int = 42):
        """Initialize the SAR barrier environment.

        Args:
            num_agents: Number of agents in the simulation (1-6)
            scene: Scene number to load (default 1)
            seed: Random seed for reproducibility (default 42)
        """
        if not (1 <= num_agents <= 6):
            raise ValueError(f"num_agents must be 1-6, got {num_agents}")

        self.num_agents = num_agents
        self.scene = scene
        self.seed = seed

        self.env = SAREnv(num_agents=num_agents, scene=scene, seed=seed)
        self.env.reset()

        self._step_counter: int = 0
        self._action_queue: dict[int, str] = {}
        self._current_obs: dict[int, str] = {}
        self._current_structured_obs: dict[int, dict] = {}
        self._finished: bool = False

        # threading primitives — safe across worker thread event loops
        self._obs_events: list[threading.Event] = [
            threading.Event() for _ in range(num_agents)
        ]
        self._step_lock = threading.Lock()

        # Last step log (for experiment logger)
        self._last_actions: list[str] = []
        self._last_successes: list[bool] = []
        self._last_observations: list[str] = []
        self._last_timeout_agents: list[int] = []
        self._current_timeout_agents: list[int] = []
        self._stopped: bool = False

        # Step diagnostics
        self._last_error_types: list[str] = []
        self._last_step_duration_ms: float = 0.0
        self._last_completed_subtasks_delta: list[str] = []
        self._previous_completed_subtasks: set[str] = set()

        # Every completed step's log, since the last drain_step_logs() call.
        # A slow poller (e.g. experiment.py's fixed-interval loop) can miss
        # steps if it only ever reads the single latest snapshot above —
        # this buffer lets callers log every step exactly once regardless
        # of how many steps ran between polls.
        self._pending_step_logs: list[dict] = []

        # Dashboard stream history (consumed by /dashboard/stream):
        # per-step agent positions and a rolling buffer of per-agent
        # observation summaries. Step 0 is recorded up front so trajectory
        # lines have an origin point.
        self._trajectory_history: list[dict] = []
        self._observation_stream: deque[dict] = deque(maxlen=200)
        self._record_positions(step=0)

    # -- Public API -----------------------------------------------------------

    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """Submit this agent's action and wait for all agents to submit.

        Args:
            agent_idx: Index of the agent submitting (0-based)
            action: Action string (e.g. "NavigateTo(target_id)")

        Returns:
            dict with keys: observation, agent_name, step, finished, success
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

        if self._stopped or self._finished:
            return {
                "observation": "",
                "agent_name": self.env.agent_names[agent_idx],
                "step": self._step_counter,
                "finished": True,
                "success": False,
            }

        with self._step_lock:
            current_step = self._step_counter
            # Clear stale event from previous step
            self._obs_events[agent_idx].clear()
            self._action_queue[agent_idx] = action
            all_submitted = len(self._action_queue) == self.num_agents
            if all_submitted:
                self._current_timeout_agents = []

        if all_submitted:
            await asyncio.to_thread(self._execute_step, current_step)
        else:
            deadline = time.monotonic() + self.STEP_TIMEOUT
            while True:
                if self._stopped or self._finished:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Timeout: fill NoOp for missing agents and execute
                    with self._step_lock:
                        if self._step_counter > current_step:
                            break  # Step already executed by another agent
                        timeout_agents = []
                        for i in range(self.num_agents):
                            if i not in self._action_queue:
                                self._action_queue[i] = "NoOp"
                                timeout_agents.append(i)
                        # Accumulate rather than overwrite: multiple waiting
                        # agents compute their own deadline independently,
                        # so two can expire close together and both reach
                        # this block for the same step. Whichever runs
                        # second finds the slots the first already filled
                        # and recomputes an empty "missing" list — a plain
                        # assignment would let that spurious empty result
                        # erase the first agent's correct timeout record.
                        self._current_timeout_agents = sorted(
                            set(self._current_timeout_agents) | set(timeout_agents)
                        )
                    await asyncio.to_thread(self._execute_step, current_step)
                    break

                # Wait for event (threading.Event.wait is thread-safe)
                triggered = await asyncio.to_thread(
                    self._obs_events[agent_idx].wait, remaining
                )

                if self._stopped or self._finished:
                    break

                with self._step_lock:
                    if self._step_counter > current_step:
                        break  # Step executed

                if not triggered:
                    continue  # Timeout, loop will re-check remaining

                # Triggered but step didn't advance — re-clear and keep waiting
                self._obs_events[agent_idx].clear()

        obs_text = self._current_obs.get(agent_idx, "")
        structured = self._current_structured_obs.get(agent_idx, {})
        return {
            "observation": obs_text,
            "agent_name": self.env.agent_names[agent_idx],
            "step": self._step_counter,
            "finished": self._finished,
            "success": True,
            "structured_observations": structured.get("observations", []),
            "structured_position": structured.get("position"),
            "structured_inventory": structured.get("inventory"),
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
        """Return current environment state for coordinator queries."""
        all_objects = self.env.controller.field.all_objects(
            expand=True, with_memory=False
        )
        snapshot = {
            "agents": [],
            "fires": [],
            "persons": [],
            "reservoirs": [],
            "deposits": [],
            "flammables": [],
        }
        for obj in all_objects:
            type_name = (
                obj.class_name() if hasattr(obj, "class_name") else type(obj).__name__
            )
            obj_dict = {
                "name": getattr(obj, "name", str(obj)),
                "position": getattr(obj, "position", None),
                "object_id": getattr(obj, "object_id", str(obj)),
                "type": type_name,
            }
            if type_name == "AbsAgent":
                inv = getattr(obj, "inventory", {})
                obj_dict["inventory"] = inv
                snapshot["agents"].append(obj_dict)
            elif type_name == "Fire":
                obj_dict["average_intensity"] = str(
                    getattr(obj, "average_intensity", "?")
                )
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

    def get_last_step_log(self) -> dict:
        """Return the log data from the most recently executed step."""
        return {
            "actions": list(self._last_actions),
            "successes": list(self._last_successes),
            "observations": list(self._last_observations),
            "timeout_agents": list(self._last_timeout_agents),
            "error_types": list(self._last_error_types),
            "step_duration_ms": self._last_step_duration_ms,
            "completed_subtasks_delta": list(self._last_completed_subtasks_delta),
        }

    def get_trajectory_history(self) -> list[dict]:
        """Per-step agent positions for the dashboard trajectory/timeline views.

        Each entry is ``{"step": int, "agents": [{"agent_id", "name",
        "position": [x, y, z] | None}]}``. Step 0 (post-reset positions) is
        recorded at init so trajectory lines have an origin point.
        """
        return list(self._trajectory_history)

    def get_observation_stream(self, limit: int = 40) -> list[dict]:
        """Most recent per-agent observation summaries, oldest first.

        Each entry is ``{"step", "agent", "text"}`` where ``text`` is a
        compact one-line summary of what the agent saw that step.
        """
        if limit <= 0:
            return []
        return list(self._observation_stream)[-limit:]

    def _record_positions(self, step: int) -> None:
        """Snapshot all agent positions into the trajectory history."""
        agents = []
        for i in range(self.num_agents):
            pos = None
            try:
                p = self.env.controller.get("agents", i).get_position()
                pos = [p[0], p[1], p[2]]
            except Exception:
                structured = getattr(self, "_current_structured_obs", {}).get(i, {})
                sp = structured.get("position")
                if sp:
                    pos = list(sp)
            agents.append(
                {
                    "agent_id": i,
                    "name": self.env.agent_names[i],
                    "position": pos,
                }
            )
        self._trajectory_history.append({"step": step, "agents": agents})

    def drain_step_logs(self) -> list[dict]:
        """Return and clear every step log buffered since the last drain.

        Unlike get_last_step_log() (which only ever exposes the single most
        recent step), this returns one entry per step that actually
        executed, each carrying its own step number and metrics snapshot.
        A caller polling on a fixed interval can call this every tick and
        log every entry — no step is silently skipped even if several
        completed between polls.
        """
        with self._step_lock:
            drained = self._pending_step_logs
            self._pending_step_logs = []
        return drained

    def stop(self):
        """Clean up the environment and wake any workers waiting on the barrier."""
        self._stopped = True
        self._finished = True
        for ev in self._obs_events:
            ev.set()
        if hasattr(self, "env"):
            self.env.stop()

    # -- Structured observations -----------------------------------------------

    def _build_structured_obs(self, agent_idx: int) -> dict:
        """Build structured observation data for one agent after a step.

        Returns a dict with keys: observations (list), position, inventory.
        Called inside _execute_step() which already holds _step_lock.
        """
        try:
            obs_dct = self.env.controller.get_observation(agent_idx)
            visible = obs_dct.get("global_obs", [])
            agent = self.env.controller.get("agents", agent_idx)
            pos = agent.get_position()
            inventory = self.env.controller.get_inventory(agent_idx)
        except Exception:
            return {"observations": [], "position": None, "inventory": []}

        observations = []
        for obj_dict in visible:
            tp = obj_dict.get("type", "")
            obj_type = _OBS_TYPE_MAP.get(tp)
            if obj_type is None:
                continue
            obs_pos = _obs_position(obj_dict)
            attrs = _extract_obs_attributes(obj_dict)
            observations.append(
                {
                    "reporter": self.env.agent_names[agent_idx],
                    "step": self._step_counter,
                    "object_type": obj_type,
                    "name": obj_dict.get("name"),
                    "position": list(obs_pos) if obs_pos else None,
                    "attributes": attrs,
                    "confidence": 1.0,
                }
            )

        return {
            "observations": observations,
            "position": (pos[0], pos[1], pos[2]) if pos else None,
            "inventory": inventory,
        }

    def _execute_step(self, expected_step: int):
        """Execute one env.step() with all collected actions, then broadcast obs.

        Called via asyncio.to_thread from submit_action. The ``expected_step``
        guard prevents double execution when multiple agents timeout
        simultaneously.
        """
        with self._step_lock:
            # Prevent double execution for the same step
            if self._step_counter != expected_step:
                return

            actions = []
            for i in range(self.num_agents):
                raw_action = self._action_queue.get(i, "NoOp")
                if "(" not in raw_action:
                    raw_action = raw_action + "()"
                actions.append(raw_action)

            # Clear events so waiters for the NEXT step start fresh
            for ev in self._obs_events:
                ev.clear()

            started = time.monotonic()
            obs_text, act_successes = self.env.step(actions)
            self._last_step_duration_ms = (time.monotonic() - started) * 1000.0

            error_type = ""
            event = getattr(self.env, "event", None)
            if isinstance(event, dict):
                error_type = str(event.get("error_type", "") or "")
            error_types = []
            for success in act_successes or []:
                error_types.append("" if success else error_type)
            self._last_error_types = error_types

            completed = set(getattr(self.env.checker, "subtasks_completed", []) or [])
            self._last_completed_subtasks_delta = sorted(
                completed - self._previous_completed_subtasks
            )
            self._previous_completed_subtasks = completed

            observations = []
            self._current_structured_obs = {}
            for i in range(self.num_agents):
                obs, _ = self.env.generate_obs_text(i)
                state = self.env.get_agent_state(i)
                action_feedback = self.env.input_dict.get(
                    f"{self.env.agent_names[i]}'s previous action", ""
                )
                failure_feedback = self.env.input_dict.get(
                    f"{self.env.agent_names[i]}'s previous failures", ""
                )
                full_obs = f"{obs}\n{state}\n{action_feedback}\n{failure_feedback}"
                self._current_obs[i] = full_obs
                observations.append(full_obs)
                self._current_structured_obs[i] = self._build_structured_obs(i)

            self._step_counter += 1
            self._finished = self.env.checker.check_success()

            # Save last step log
            self._last_actions = list(actions)
            self._last_successes = list(act_successes) if act_successes else []
            self._last_observations = list(observations)
            self._last_timeout_agents = list(self._current_timeout_agents)
            self._current_timeout_agents = []

            # Buffer this step's full log + metrics, snapshotted now so a
            # slow poller can still log every step exactly once even if
            # several steps complete between polls (see drain_step_logs()).
            self._pending_step_logs.append(
                {
                    "step": self._step_counter,
                    "actions": list(self._last_actions),
                    "successes": list(self._last_successes),
                    "observations": list(self._last_observations),
                    "timeout_agents": list(self._last_timeout_agents),
                    "error_types": list(self._last_error_types),
                    "step_duration_ms": self._last_step_duration_ms,
                    "completed_subtasks_delta": list(
                        self._last_completed_subtasks_delta
                    ),
                    "coverage": self.env.checker.get_coverage(),
                    "transport_rate": self.env.checker.get_transport_rate(),
                    "finished": self._finished,
                }
            )

            # Dashboard stream: trajectory point + per-agent observation
            # summaries for the /dashboard/stream SSE feed.
            self._record_positions(self._step_counter)
            for i in range(self.num_agents):
                structured = self._current_structured_obs.get(i, {})
                names = [
                    o["name"]
                    for o in structured.get("observations", [])
                    if o.get("name")
                ]
                if names:
                    shown = ", ".join(names[:6])
                    if len(names) > 6:
                        shown += f" +{len(names) - 6} more"
                    text = f"observed {len(names)} objects: {shown}"
                else:
                    text = "no objects in view"
                self._observation_stream.append(
                    {
                        "step": self._step_counter,
                        "agent": self.env.agent_names[i],
                        "text": text,
                    }
                )

            self._action_queue.clear()

            # Wake all waiting agents
            for ev in self._obs_events:
                ev.set()
