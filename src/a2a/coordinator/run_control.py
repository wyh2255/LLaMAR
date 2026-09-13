"""内核级 run-control 契约：环境无关的运行状态 DTO + 生命周期停止协议。

本模块是内核（src/a2a）自有契约，供各场景包（SAR / AI2Thor 等编排层）各自满足：

- ``RunStatus``：运行状态 DTO，由场景侧 run-control 实现（barrier / controller）
  返回；字段契约与实现方案 §3.1 逐项一致（step / max_steps / finished /
  stopped / stop_reason / timeout_agents / domain_metrics）。
- ``EnvironmentRunControl``：环境无关的停止协议。CoordinatorServer 只依赖该协议
  做 cancel/shutdown，不感知任何具体 barrier；场景适配器注册后即可被调用。

去耦边界（P2a）：本模块禁止 import 任何场景包（``*_orch``），也不得反向引用其
类型定义；类型对齐由场景实现侧保证（``isinstance(obj, EnvironmentRunControl)``
可做运行时校验）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RunStatus:
    """DTO for EnvironmentRunControl — environment-agnostic run status.

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


@runtime_checkable
class EnvironmentRunControl(Protocol):
    """Environment-agnostic lifecycle stop protocol.

    Implementations (scenario pack barriers / run-control adapters):
    - SAR 场景包 barrier：SARBarrier
    - AI2Thor 场景包 barrier：AI2ThorBarrier

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
