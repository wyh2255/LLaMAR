"""Legacy compatibility shim — re-exports WorkerReportPublisher from sar_orch.map.publisher.

Prefer ``from sar_orch.map import WorkerReportPublisher`` for new code.
"""

from sar_orch.map.publisher import WorkerReportPublisher

__all__ = [
    "WorkerReportPublisher",
]
