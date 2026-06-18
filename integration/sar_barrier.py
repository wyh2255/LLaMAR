"""SARBarrier — synchronous action collector wrapping LLaMAR SAREnv."""
import asyncio
import sys
from pathlib import Path

# Ensure LLaMAR and SAR directories are importable
# (SAR uses flat imports like "import core", "from env import SAREnv")
_llamar_root = Path(__file__).resolve().parent.parent
_sar_dir = _llamar_root / "SAR"
for p in [_llamar_root, _sar_dir]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from env import SAREnv


class SARBarrier:
    """Collect per-agent actions, execute env.step() synchronously, broadcast observations.

    Wraps LLaMAR's SAREnv with an async barrier:
      1. Workers call submit_action(agent_idx, action) -> await
      2. When all N agents have submitted -> _execute_step()
      3. Observations distributed -> awaiting Workers resume

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

        # Async synchronization -- re-created each step
        self._obs_events: list[asyncio.Event] = [
            asyncio.Event() for _ in range(num_agents)
        ]
        self._step_lock = asyncio.Lock()

    # -- Public API ---------------------------------------------------------

    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """Submit this agent's action and wait for all agents to submit.

        Returns a dict with the agent's observation and step metadata.
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

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
            # Use class_name() to resolve decorator wrappers (IdWrapper, CollidableWrapper, etc.)
            type_name = obj.class_name() if hasattr(obj, "class_name") else type(obj).__name__
            obj_dict = {
                "name": getattr(obj, "name", str(obj)),
                "position": getattr(obj, "position", None),
                "object_id": getattr(obj, "object_id", str(obj)),
                "type": type_name,
            }
            if type_name == "AbsAgent":
                # Access inventory directly from the agent object
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
                obj_dict["resource_type"] = str(
                    getattr(obj, "resource_type", "?")
                )
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

    # -- Internal -----------------------------------------------------------

    async def _execute_step(self):
        """Execute one env.step() with all collected actions, then broadcast obs."""
        # Collect actions in agent order, normalizing "NoOp" to "NoOp()" for parse_action
        actions = []
        for i in range(self.num_agents):
            raw_action = self._action_queue.get(i, "NoOp")
            # Ensure actions have parentheses (required by env.parse_action)
            if "(" not in raw_action:
                raw_action = raw_action + "()"
            actions.append(raw_action)

        # Execute synchronously (env.step is blocking, run in thread)
        obs_text, act_successes = await asyncio.to_thread(self.env.step, actions)

        # Parse per-agent observations from env state
        for i in range(self.num_agents):
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
