"""Legacy compatibility shim — re-exports SemanticMapStore symbols from sar_orch.map.store.

Prefer ``from sar_orch.map import ...`` for new code.
"""

from sar_orch.map.store import (
    AgentSemanticState,
    ObservationRecord,
    SemanticMapStore,
    SemanticObject,
    TERMINAL_STATUS_ORDER,
)

__all__ = [
    "AgentSemanticState",
    "ObservationRecord",
    "SemanticMapStore",
    "SemanticObject",
    "TERMINAL_STATUS_ORDER",
]
