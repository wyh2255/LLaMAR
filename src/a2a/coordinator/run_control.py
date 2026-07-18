"""EnvironmentRunControl protocol — environment-agnostic stop protocol.

SAR and AI2Thor each adapt this protocol. The CoordinatorServer only depends
on this protocol, not on any specific barrier implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ai2thor_orch.contracts.types import RunStatus


@runtime_checkable
class EnvironmentRunControl(Protocol):
    """Environment-agnostic lifecycle stop protocol.

    Implementations:
    - SARBarrier (sar_orch/barrier.py)
    - AI2ThorBarrier (ai2thor_orch/barrier/ai2thor_barrier.py)

    The CoordinatorServer relies exclusively on this protocol for
    cancel/shutdown operations.  Concrete adapters register via
    ``set_run_control()``; the server does not know about barriers.
    """

    def request_stop(self, reason: str) -> None:
        """Request graceful stop with a reason string.

        The adapter should record the reason, set internal stop flags,
        and wake any waiting workers / agent threads.  Idempotent.
        """
        ...

    def stop(self) -> None:
        """Immediate stop — terminate the environment and wake waiters.

        Typically calls ``request_stop()`` internally and then shuts down
        the underlying executor / controller.
        """
        ...

    def get_run_status(self) -> RunStatus:
        """Return current run status DTO.

        Step count, finished/stopped flags, stop reason, timeout agents,
        and domain-specific metrics.
        """
        ...
