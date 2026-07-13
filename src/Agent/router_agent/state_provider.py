"""StateProvider abstraction for runtime state injection into ContextManager.

Provides a generic protocol and DTO so that ContextManager can refresh runtime
state before each LLM request without directly depending on SAR-specific
backends (SARBarrier, SemanticMapStore, EventStore, TaskStore).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class RuntimeState:
    """Versioned snapshot of runtime state consumed by ContextManager.

    Attributes:
        version: Monotonic version counter (e.g., SAR env step). Used to skip
            redundant refreshes when the snapshot has not changed.
        env_step: The environment step this snapshot corresponds to.
        observed_at: Monotonic timestamp when the snapshot was taken.
        payload: Structured state content (step_budget, semantic_summary, etc.).
        stale: True if the snapshot is stale (e.g., refresh failed and we are
            falling back to a previous snapshot).
        refresh_error: Error message if the latest refresh attempt failed.
    """

    version: int = 0
    env_step: int = 0
    observed_at: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    stale: bool = False
    refresh_error: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        """Convenience accessor for payload fields."""
        return self.payload.get(key, default)


@runtime_checkable
class StateProvider(Protocol):
    """Protocol for a read-only runtime state provider."""

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Implementations should be lightweight reads of cached state. The
        context_id is optional and may be used by providers that need to filter
        task state by A2A context.
        """
        ...
