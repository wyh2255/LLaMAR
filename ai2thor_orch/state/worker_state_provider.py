"""AI2ThorWorkerStateProvider — runtime state for a single worker agent.

Reads public observation data from the AI2ThorBarrier and presents it
as a `RuntimeState` compatible with `WorkerContextManager`.
"""

from __future__ import annotations

from typing import Any

from Agent.router_agent.state_provider import RuntimeState, StateProvider
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier


class AI2ThorWorkerStateProvider:
    """StateProvider for a single AI2Thor worker agent.

    Args:
        barrier: The shared AI2ThorBarrier instance.
        agent_idx: This worker's agent index (0-based).
    """

    def __init__(self, barrier: AI2ThorBarrier, agent_idx: int) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh RuntimeState for this worker.

        Payload keys:
          - position: (x, y, z) tuple or None
          - rotation: dict or None
          - inventory: list of held object type names
          - step: current environment step
          - visible_objects: list of object aliases
          - current_task: str (empty if none)
          - mission_status: str

        No SAR-specific fields (fires, persons, reservoirs) are included.
        """
        obs = self._barrier.snapshot_public(self._agent_idx)
        run_status = self._barrier.get_run_status()
        coord = self._barrier.snapshot_coordinator()

        payload: dict[str, Any] = {
            "scene": coord.scene,
            "agent_name": f"Agent{self._agent_idx}",
            "agent_idx": self._agent_idx,
            "position": obs.position,
            "rotation": obs.rotation,
            "inventory": list(obs.inventory),
            "step": obs.step,
            "max_steps": run_status.max_steps,
            "visible_objects": list(obs.visible_objects),
            "current_task": "",
            "mission_status": "in_progress",
        }

        return RuntimeState(
            version=self._barrier.round_no,
            env_step=obs.step,
            payload=payload,
        )
