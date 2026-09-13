"""ControllerExecutor — serial wrapper around an ai2thor Controller.

Holds a dedicated ``ThreadPoolExecutor(max_workers=1)`` so that all Controller
calls are serialised on a single background thread.  This executor is **never**
shared with asyncio's default executor, avoiding the deadlock documented in
R2 of the implementation plan.

The barrier currently calls ``execute_step`` / ``reset`` via
``asyncio.to_thread`` (asyncio default executor).  The dedicated pool held
here is available for a future change that routes controller calls onto it
explicitly for true serial isolation.  Event waits use
``run_in_executor(None, ...)`` on the default pool.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class ControllerExecutor:
    """Serialises all ai2thor Controller calls on a dedicated thread pool.

    Args:
        controller: An ai2thor.controller.Controller instance (or a
            :class:`~ai2thor_orch.tests.fakes.FakeController`).
    """

    def __init__(self, controller: Any) -> None:
        self._controller: Any = controller
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ai2thor-exec"
        )
        self._stopped: bool = False

    # -- Public API -----------------------------------------------------------

    def execute_step(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute one round of actions on the controller.

        Each action dict is passed to ``controller.step(action_dict)``
        sequentially.  Returns a list of structured metadata dicts, one per
        action, preserving input order.

        Multi-agent controllers (P5-4): when the controller exposes
        ``step_for_agent(agent_idx=..., action=...)`` (see
        :class:`~ai2thor_orch.executor.unity_controller.UnityController`),
        the action list position is forwarded as the agent index so each
        action reaches the right agent (``agentId``).  Plain controllers
        (``FakeController``) keep the single-argument ``step`` contract
        unchanged.

        Args:
            actions: List of action dicts, each with at least an ``"action"`` key.

        Returns:
            List of dicts with keys: agent_metadata (per-agent event.metadata),
            raw_event (the full event object as dict).
        """
        if self._stopped:
            raise RuntimeError("ControllerExecutor is stopped")

        step_for_agent = getattr(self._controller, "step_for_agent", None)
        results: list[dict[str, Any]] = []
        for agent_idx, action in enumerate(actions):
            if step_for_agent is not None:
                event = step_for_agent(agent_idx=agent_idx, action=action)
            else:
                event = self._controller.step(action)
            metadata = getattr(event, "metadata", {}) if hasattr(event, "metadata") else event
            results.append(
                {
                    "agent_metadata": metadata,
                    "raw_event": event if isinstance(event, dict) else str(event),
                }
            )
        return results

    def reset(self, scene: str) -> dict[str, Any]:
        """Reset the controller to a new scene and return initial metadata.

        Calls ``controller.step({"action": "Reset", "scene": scene})`` or
        equivalent.
        """
        if self._stopped:
            raise RuntimeError("ControllerExecutor is stopped")

        if hasattr(self._controller, "reset"):
            event = self._controller.reset(scene)
        else:
            # Some Controller APIs use step('Reset') or step with a dict
            event = self._controller.step({"action": "Reset", "scene": scene})
        metadata = getattr(event, "metadata", {}) if hasattr(event, "metadata") else event
        return {"initial_metadata": metadata, "raw_event": event}

    def stop(self) -> None:
        """Idempotent stop — closes controller and shuts down the executor."""
        if self._stopped:
            return
        self._stopped = True
        try:
            if hasattr(self._controller, "stop"):
                self._controller.stop()
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    @property
    def controller(self) -> Any:
        """The underlying controller reference."""
        return self._controller

    @property
    def is_stopped(self) -> bool:
        """Whether this executor has been stopped."""
        return self._stopped
