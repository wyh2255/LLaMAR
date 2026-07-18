"""Pure data contracts for the AI2Thor orchestration layer.

All types are plain dataclasses with zero external dependencies.
Designed to be serialisable via dataclasses.asdict() and json.dumps().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionRequest:
    """An action submitted by an agent in a given round."""

    agent_idx: int
    action: str
    round_no: int


@dataclass(frozen=True)
class ActionResult:
    """The result of a single agent's action after controller execution."""

    agent_idx: int
    observation: str
    success: bool
    position: tuple[float, float, float] | None = None
    inventory: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundResult:
    """Aggregated result for one completed round."""

    round_no: int
    results: list[ActionResult] = field(default_factory=list)
    timeout_agents: list[int] = field(default_factory=list)
    finished: bool = False
    domain_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublicObservation:
    """What a single agent can observe — visible_objects use aliases, not raw objectId."""

    agent_idx: int
    text: str
    position: tuple[float, float, float] | None = None
    rotation: dict[str, float] | None = None
    inventory: list[str] = field(default_factory=list)
    visible_objects: list[str] = field(default_factory=list)
    step: int = 0


@dataclass(frozen=True)
class CoordinatorObservation:
    """Global view for the coordinator — all agents and objects in the scene."""

    round_no: int = 0
    agents: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    scene: str = ""
    step: int = 0
    max_steps: int = 0


@dataclass(frozen=True)
class AgentPublicState:
    """Publicly visible state of a single agent."""

    agent_idx: int = 0
    name: str = ""
    position: tuple[float, float, float] | None = None
    rotation: dict[str, float] | None = None
    inventory: list[str] = field(default_factory=list)
    current_task_id: str = ""
    status: str = ""


@dataclass(frozen=True)
class RunStatus:
    """DTO for G3 EnvironmentRunControl — environment-agnostic run status.

    Field names must match the implementation plan §3.1 exactly:
    step / max_steps / finished / stopped / stop_reason / timeout_agents / domain_metrics.
    """

    step: int = 0
    max_steps: int = 0
    finished: bool = False
    stopped: bool = False
    stop_reason: str = ""
    timeout_agents: list[int] = field(default_factory=list)
    domain_metrics: dict[str, Any] = field(default_factory=dict)
