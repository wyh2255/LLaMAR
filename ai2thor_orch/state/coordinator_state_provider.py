"""AI2ThorCoordinatorStateProvider — runtime state for the coordinator.

Reads coordinator observation data from the AI2ThorBarrier and presents it
as a `RuntimeState` compatible with `CoordinatorContextManager`.

No SAR-specific fields (fires, persons, reservoirs) are included — the
payload contains only AI2Thor-relevant state.
"""

from __future__ import annotations

from typing import Any

from Agent.router_agent.state_provider import RuntimeState
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier


class AI2ThorCoordinatorStateProvider:
    """StateProvider for the coordinator in an AI2Thor mission.

    Args:
        barrier: The shared AI2ThorBarrier instance.
    """

    def __init__(self, barrier: AI2ThorBarrier) -> None:
        self._barrier = barrier

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh RuntimeState for the coordinator.

        Payload keys (AI2Thor-specific, no SAR fields):
          - scene: str
          - step_budget: dict with current_step, max_steps, remaining
          - team_status_summary: list of agent state dicts
          - task_status_view: list (empty — AI2Thor has no SAR task model)
          - mission_finished: bool
          - run_status: dict representation of RunStatus
          - visible_objects: list of alias strings across all agents
          - objects_of_interest: alias-based object descriptors
        """
        coord = self._barrier.snapshot_coordinator()
        run_status = self._barrier.get_run_status()

        # Build visible objects list deduped across all agents
        visible_aliases: list[str] = []
        seen: set[str] = set()
        for obj in coord.objects:
            alias = obj.get("alias", "")
            if alias and alias not in seen:
                seen.add(alias)
                visible_aliases.append(alias)

        payload: dict[str, Any] = {
            "scene": coord.scene,
            "step_budget": {
                "current_step": coord.step,
                "max_steps": coord.max_steps,
                "remaining": max(0, coord.max_steps - coord.step),
            },
            "team_status_summary": {
                "agents": [
                    {
                        "agent_id": a.get("name", f"Agent{i}"),
                        "position": a.get("position"),
                        "inventory": a.get("inventory", []),
                    }
                    for i, a in enumerate(coord.agents)
                ]
            },
            "task_status_view": [],
            "mission_finished": run_status.finished,
            "run_status": {
                "step": run_status.step,
                "max_steps": run_status.max_steps,
                "finished": run_status.finished,
                "stopped": run_status.stopped,
                "stop_reason": run_status.stop_reason,
                "timeout_agents": list(run_status.timeout_agents),
                "domain_metrics": dict(run_status.domain_metrics),
            },
            "visible_objects": visible_aliases,
        }

        return RuntimeState(
            version=self._barrier.round_no,
            env_step=coord.step,
            payload=payload,
        )

