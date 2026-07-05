"""SARBarrier -- synchronous action collector wrapping LLaMAR SAREnv.

Implements a multi-agent synchronization barrier: collects actions from all
agents, executes env.step() atomically, then broadcasts observations back.

Uses threading.Event and threading.Lock (not asyncio primitives) because
workers run in separate threads with separate asyncio event loops — asyncio
sync primitives are NOT safe across event loops in different threads.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

# SAR/ must be on sys.path because it uses flat imports (not a proper package)
_sar_dir = Path(__file__).resolve().parent.parent / "SAR"
if str(_sar_dir) not in sys.path:
    sys.path.insert(0, str(_sar_dir))

from env import SAREnv  # noqa: E402


class SARBarrier:
    """Collect per-agent actions, execute env.step() synchronously, broadcast observations.

    Wraps LLaMAR's SAREnv with a threading-based barrier:
      1. Workers call submit_action(agent_idx, action) -> await
      2. When all N agents have submitted -> _execute_step()
      3. Observations distributed -> awaiting Workers resume

    Timeout: if an agent hasn't submitted within 60s of the first submission,
    NoOp is auto-filled and the step proceeds.
    """

    STEP_TIMEOUT: float = 60.0  # seconds

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
                        self._current_timeout_agents = timeout_agents
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

    def stop(self):
        """Clean up the environment and wake any workers waiting on the barrier."""
        self._stopped = True
        self._finished = True
        for ev in self._obs_events:
            ev.set()
        if hasattr(self, "env"):
            self.env.stop()

    # -- Internal -------------------------------------------------------------

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
            self._last_completed_subtasks_delta = sorted(completed - self._previous_completed_subtasks)
            self._previous_completed_subtasks = completed

            observations = []
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

            self._step_counter += 1
            self._finished = self.env.checker.check_success()

            # Save last step log
            self._last_actions = list(actions)
            self._last_successes = list(act_successes) if act_successes else []
            self._last_observations = list(observations)
            self._last_timeout_agents = list(self._current_timeout_agents)
            self._current_timeout_agents = []

            self._action_queue.clear()

            # Wake all waiting agents
            for ev in self._obs_events:
                ev.set()
