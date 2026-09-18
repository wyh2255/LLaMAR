"""AI2Thor Context subclasses — override _render_environment_view for AI2Thor missions.

These subclasses inherit the SAR pinned-state schema but replace only the
environment rendering to output AI2Thor-relevant state instead of SAR fields
(fires, persons, reservoirs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from Agent.router_agent.context import (
    ContextConfig,
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
            Visible now: {alias list — 'none — use rotate/move to search.' when empty}

        Reads data from RuntimeState payload, not from pinned state,
        to avoid rendering SAR-specific fields.
        """
        rs = self._runtime_state
        if rs is None:
            return ""

        payload = rs.payload
        scene = payload.get("scene") or "(unknown)"
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

        # Visible objects — the agent's current field of view (the barrier
        # filters by the metadata ``visible`` flag), not a house inventory;
        # when empty, point the agent at the search action.
        vis = payload.get("visible_objects", [])
        if vis:
            vis_str = ", ".join(str(v) for v in vis)
        else:
            vis_str = "none — use rotate/move to search."

        lines = [
            f"Scene: {scene} | Step: {step}/{max_steps}",
            f"You are {agent_name} at {pos_str}, holding: {inv_str}",
            f"Visible now: {vis_str}",
        ]
        return "\n".join(lines)


class AI2ThorCoordinatorContextManager(CoordinatorContextManager):
    """Coordinator context manager for AI2Thor missions.

    Overrides _render_environment_view() to output AI2Thor-specific
    state (scene, agents summary, visible objects, P2 sightings) instead
    of SAR fields (fires, persons, reservoirs, deposits). Pinned state
    schema is inherited unchanged from CoordinatorPinnedState.

    P2 空间记忆：``### Sightings`` 段独立渲染——与现有各段用空行隔离，
    sightings 为空时整段省略（prompt diff 可控）。预算截断 = 最新 K 条
    （``sightings_budget``，缺省 :attr:`SIGHTINGS_BUDGET_DEFAULT` = 30，
    config 可调：EnvPack 经 session 工厂下传）。
    """

    #: ``### Sightings`` 段预算缺省（最新 K 条；构造参数 ``sightings_budget``
    #: 可调）。
    SIGHTINGS_BUDGET_DEFAULT: int = 30

    def __init__(
        self,
        config: ContextConfig | None = None,
        token_limit: int = 80000,
        log_dir: str | Path | None = None,
        state_provider: Any = None,
        skills_dir: str | Path | None = None,
        *,
        sightings_budget: int | None = None,
    ) -> None:
        super().__init__(
            config=config,
            token_limit=token_limit,
            log_dir=log_dir,
            state_provider=state_provider,
            skills_dir=skills_dir,
        )
        if sightings_budget is None:
            sightings_budget = self.SIGHTINGS_BUDGET_DEFAULT
        if sightings_budget < 1:
            raise ValueError(
                f"sightings_budget must be >= 1, got {sightings_budget}"
            )
        #: ``### Sightings`` 段预算（最新 K 条；渲染层截断）。
        self.sightings_budget: int = int(sightings_budget)

    def _render_environment_view(self) -> str:
        """Render AI2Thor coordinator environment view.

        Output format::
            Scene: {scene} | Step: {step}/{max_steps}
            Agents: {name}({position}, holding: {inv}) | ...
            Objects of interest: {alias list}

            ### Task Progress
            Completed: {n}/{total} subtasks
            - {item}: {c}/{t} done | — missing: {subtask list}   # 常驻段

            ### Sightings
            step {N} {agent} saw {alias} ({x}, {z})     # 最新 K 条（如有）

        Reads data from RuntimeState payload, not from pinned state,
        to avoid rendering SAR-specific fields.
        """
        rs = self._runtime_state
        if rs is None:
            return ""

        payload = rs.payload
        scene = payload.get("scene") or "(unknown)"
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

        # Task Progress（P0b 常驻锚点：task_progress 存在即每轮渲染，含全部完成态
        # —— 完成进度不允许因「已完成」而消失；缺失时整段省略，与上段空行隔离）。
        progress_lines = self._render_task_progress(payload)
        if progress_lines:
            lines.append("")
            lines.extend(progress_lines)

        # Sightings（P2 空间记忆；独立段：空则整段省略，与上段空行隔离）。
        # payload 契约：newest-first（由 SightingStore.latest_sightings 保证）
        # —— 预算截断即「最新 K 条」。
        sightings = payload.get("sightings")
        if isinstance(sightings, list) and sightings:
            rendered = [
                self._render_sighting_line(entry)
                for entry in sightings[: self.sightings_budget]
                if isinstance(entry, dict)
            ]
            if rendered:
                lines.append("")
                lines.append("### Sightings")
                lines.extend(rendered)

        return "\n".join(lines)

    @staticmethod
    def _render_sighting_line(entry: dict[str, Any]) -> str:
        """单条 sighting 行：``step {N} {agent} saw {alias} ({x}, {z})``。

        坐标缺失/非数值 → ``?``（渲染绝不猜值）。
        """
        step = entry.get("step", "?")
        agent = entry.get("agent", "?")
        alias = entry.get("alias", "?")
        x = entry.get("x")
        z = entry.get("z")
        x_str = f"{x:.1f}" if isinstance(x, (int, float)) else "?"
        z_str = f"{z:.1f}" if isinstance(z, (int, float)) else "?"
        return f"step {step} {agent} saw {alias} ({x_str}, {z_str})"

    @classmethod
    def _render_task_progress(cls, payload: dict[str, Any]) -> list[str]:
        """渲染 ``### Task Progress`` 段（``task_progress`` 缺失 → 空列表，整段省略）。

        数据源是 TaskMetricsTracker 的动作证据口径（已执行动作），不是 verifier 仿真
        真值。首行给总进度 ``Completed: {n}/{total} subtasks``；随后按 ``item_progress``
        的物品顺序（由 tracker 保证与 contract 一致）逐物品一行，容器开关分组渲染成
        ``X door`` 行。payload 缺 ``item_progress``（例如只补了坐标口径的旧 payload）时
        退化为只列缺项明细，绝不因此丢掉该段。
        """
        task_progress = payload.get("task_progress")
        if not isinstance(task_progress, dict):
            return []

        completed = task_progress.get("completed_count", "?")
        total = task_progress.get("total_count", "?")
        missing_raw = task_progress.get("missing_subtasks")
        missing = (
            [str(entry) for entry in missing_raw]
            if isinstance(missing_raw, list)
            else []
        )
        lines = ["### Task Progress", f"Completed: {completed}/{total} subtasks"]

        item_progress = payload.get("item_progress")
        rendered = (
            [cls._render_progress_group(group) for group in item_progress]
            if isinstance(item_progress, list)
            else []
        )
        if rendered:
            lines.extend(rendered)
        elif missing:
            lines.append(f"- Missing: {', '.join(missing)}")
        return lines

    @staticmethod
    def _render_progress_group(group: Any) -> str:
        """单个物品的进度行（``- {item}: {c}/{t} done`` 或 ``… — missing: {list}``）。

        容器开关分组（``door``）渲染成 ``- {item} door: open done, close missing``
        —— 按 open/close 固定顺序，只列实际存在的动作。
        """
        if not isinstance(group, dict):
            return "- ?"
        item = group.get("item", "?")
        subtasks_raw = group.get("subtasks")
        subtasks = (
            [str(entry) for entry in subtasks_raw]
            if isinstance(subtasks_raw, list)
            else []
        )
        missing_raw = group.get("missing")
        missing = (
            [str(entry) for entry in missing_raw]
            if isinstance(missing_raw, list)
            else []
        )

        if group.get("door"):
            marks = []
            for verb, label in (("openobject", "open"), ("closeobject", "close")):
                action = next(
                    (entry for entry in subtasks if _subtask_verb(entry) == verb), None
                )
                if action is not None:
                    state = "missing" if action in missing else "done"
                    marks.append(f"{label} {state}")
            return f"- {item} door: {', '.join(marks) if marks else 'unknown'}"

        total = group.get("total")
        if not isinstance(total, int):
            total = len(subtasks)
        completed = group.get("completed")
        if not isinstance(completed, int):
            completed = max(0, total - len(missing))
        if not missing:
            return f"- {item}: {total}/{total} done"
        return f"- {item}: {completed}/{total} — missing: {', '.join(missing)}"


def _subtask_verb(subtask: str) -> str:
    """子任务的动作名（``OpenObject(Fridge)`` → ``openobject``）。"""
    return subtask.split("(", 1)[0].strip().lower()
