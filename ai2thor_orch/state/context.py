"""AI2Thor Context subclasses — override _render_environment_view for AI2Thor missions.

These subclasses inherit the SAR pinned-state schema but replace only the
environment rendering to output AI2Thor-relevant state instead of SAR fields
(fires, persons, reservoirs).
"""

from __future__ import annotations

from typing import Any

from Agent.router_agent.context import (
    CoordinatorContextManager,
    CoordinatorPinnedState,
)
from Agent.worker_agent.context import (
    WorkerContextManager,
    WorkerPinnedState,
)


class AI2ThorWorkerContextManager(WorkerContextManager):
    """Worker context manager for AI2Thor missions.

    Overrides _render_environment_view() to output AI2Thor-specific
    state (scene, position, visible objects) instead of SAR fields
    (fires, persons, reservoirs). Pinned state schema is inherited
    unchanged from WorkerPinnedState.
    """

    def _render_environment_view(self) -> str:
        """Render AI2Thor environment view.

        Output format::
            Scene: {scene} | Step: {step}/{max_steps}
            You are {agent_name} at {position}, holding: {inventory or 'nothing'}
            Visible objects: {alias list}

        Reads data from RuntimeState payload, not from pinned state,
        to avoid rendering SAR-specific fields.
        """
        rs = self._runtime_state
        if rs is None:
            return ""

        payload = rs.payload
        scene = payload.get("scene", "?")
        step = payload.get("step", 0)
        max_steps = payload.get("max_steps", 0)
        agent_name = payload.get("agent_name", f"Agent{payload.get('agent_idx', 0)}")

        # Position
        pos = payload.get("position")
        if pos and len(pos) >= 3:
            pos_str = f"({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})"
        elif pos:
            pos_str = str(pos)
        else:
            pos_str = "unknown"

        # Inventory
        inv = payload.get("inventory", [])
        inv_str = ", ".join(str(i) for i in inv) if inv else "nothing"

        # Visible objects
        vis = payload.get("visible_objects", [])
        vis_str = ", ".join(str(v) for v in vis) if vis else "none"

        lines = [
            f"Scene: {scene} | Step: {step}/{max_steps}",
            f"You are {agent_name} at {pos_str}, holding: {inv_str}",
            f"Visible objects: {vis_str}",
        ]
        return "\n".join(lines)


class AI2ThorCoordinatorContextManager(CoordinatorContextManager):
    """Coordinator context manager for AI2Thor missions.

    Overrides _render_environment_view() to output AI2Thor-specific
    state (scene, agents summary, visible objects) instead of SAR fields
    (fires, persons, reservoirs, deposits). Pinned state schema is
    inherited unchanged from CoordinatorPinnedState.
    """

    def _render_environment_view(self) -> str:
        """Render AI2Thor coordinator environment view.

        Output format::
            Scene: {scene} | Step: {step}/{max_steps}
            Agents: {name}({position}, holding: {inv}) | ...
            Objects of interest: {alias list}

        Reads data from RuntimeState payload, not from pinned state,
        to avoid rendering SAR-specific fields.
        """
        rs = self._runtime_state
        if rs is None:
            return ""

        payload = rs.payload
        scene = payload.get("scene", "?")
        step_budget = payload.get("step_budget", {})
        current_step = step_budget.get("current_step", 0)
        max_steps = step_budget.get("max_steps", 0)

        lines = [
            f"Scene: {scene} | Step: {current_step}/{max_steps}",
        ]

        # Agents
        team = payload.get("team_status_summary", {})
        agents_raw = team.get("agents", []) if isinstance(team, dict) else []
        if agents_raw:
            agent_parts = []
            for a in agents_raw:
                name = a.get("agent_id", "?")
                pos = a.get("position")
                if pos and isinstance(pos, dict):
                    pos_str = f"({pos.get('x', 0):.1f}, {pos.get('y', 0):.1f}, {pos.get('z', 0):.1f})"
                elif pos and isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    pos_str = f"({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})"
                else:
                    pos_str = "?"
                inv = a.get("inventory", [])
                inv_str = ", ".join(str(i) for i in inv) if inv else "empty"
                agent_parts.append(f"{name}({pos_str}, holding: {inv_str})")
            lines.append("Agents: " + " | ".join(agent_parts))

        # Objects of interest
        vis = payload.get("visible_objects", [])
        if vis:
            lines.append(f"Objects of interest: {', '.join(vis)}")
        else:
            lines.append("Objects of interest: none")

        return "\n".join(lines)
