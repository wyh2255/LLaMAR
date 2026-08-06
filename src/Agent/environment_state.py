"""Generic Environment State query/view contracts shared by all agents.

This module is intentionally backend-independent: it must not import ``a2a``
or ``sar_orch``.  It defines the canonical ``Freshness`` enum (Phase 0) and the
minimal read-only query/view DTOs used by the EnvironmentStateProvider path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Freshness(str, Enum):
    """Freshness of an Environment State projection.

    Only three values are allowed.  A projection that cannot be built safely is
    never silently reused: it must be marked ``STALE`` (with a reason and source
    revision) or ``UNAVAILABLE`` (no safe projection / read error).
    """

    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class EnvironmentStateQuery:
    """Read-only inputs for constructing an Environment State view.

    ``viewer_role`` / ``viewer_id`` are derived from an authenticated principal;
    they are never trusted from an LLM or HTTP request.
    """

    scope_id: str
    viewer_role: str = "coordinator"
    viewer_id: str = ""
    current_dispatch_id: str | None = None
    temporal_cursor: int = 0
    token_budget: int = 0
    required_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentStateView:
    """Immutable Environment State view (a rebuildable read, not truth)."""

    freshness: Freshness
    reason: str = ""
    source_revision: int | None = None
    sections: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def fresh(cls, source_revision: int | None = None) -> "EnvironmentStateView":
        return cls(Freshness.FRESH, source_revision=source_revision)

    @classmethod
    def stale(
        cls, reason: str, source_revision: int | None = None
    ) -> "EnvironmentStateView":
        return cls(Freshness.STALE, reason=reason, source_revision=source_revision)

    @classmethod
    def unavailable(cls, reason: str) -> "EnvironmentStateView":
        return cls(Freshness.UNAVAILABLE, reason=reason)


__all__ = ["EnvironmentStateQuery", "EnvironmentStateView", "Freshness"]
