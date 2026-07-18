"""FakeController — deterministic fake for unit-testing the AI2Thor barrier.

Provides a controllable ``step()`` / ``reset()`` / ``stop()`` implementation
that returns structured metadata without needing a real Unity build.
"""

from __future__ import annotations

import time
from typing import Any


class FakeEvent:
    """Fake event returned by ``FakeController.step()``.

    The ``.metadata`` attribute is the key surface consumed by
    :class:`~ai2thor_orch.executor.controller_executor.ControllerExecutor`.
    """

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata: dict[str, Any] = metadata

    def __repr__(self) -> str:
        return f"FakeEvent(metadata_keys={list(self.metadata.keys())})"


def make_default_metadata(
    scene: str = "FloorPlan1",
    num_agents: int = 1,
    has_objects: bool = True,
) -> dict[str, Any]:
    """Factory: build a plausible default metadata dict for a scene.

    The returned dict mimics the structure of real ai2thor event metadata
    enough for unit tests to exercise metadata field access.

    Args:
        scene: Unity scene name.
        num_agents: Number of agents to include.
        has_objects: Whether to include a non-empty ``objects`` list.

    Returns:
        A dict with keys typical of ai2thor ``Event.metadata``.
    """
    agents = []
    for i in range(num_agents):
        agents.append(
            {
                "name": f"Agent{i}",
                "position": {"x": i * 1.0, "y": 0.0, "z": i * 0.5},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                "inventory": {"objects": []},
            }
        )
    objects = []
    if has_objects:
        objects = [
            {
                "objectId": "Mug|-01.5|+00.9|+02.3",
                "objectType": "Mug",
                "position": {"x": -1.5, "y": 0.9, "z": 2.3},
                "visible": True,
            },
            {
                "objectId": "CounterTop|+00.0|+00.0|+00.0",
                "objectType": "CounterTop",
                "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                "visible": True,
            },
            {
                "objectId": "Apple|+01.2|+00.5|+00.8",
                "objectType": "Apple",
                "position": {"x": 1.2, "y": 0.5, "z": 0.8},
                "visible": True,
            },
        ]
    reachable_positions = [
        {"x": float(i), "y": 0.0, "z": float(i)}
        for i in range(10)
    ]
    return {
        "agents": agents,
        "objects": objects,
        "reachablePositions": reachable_positions,
        "sceneName": scene,
        "cameraPosition": {"x": 0.0, "y": 1.0, "z": 0.0},
        "lastAction": "Pass",
        "lastActionSuccess": True,
        "errorMessage": "",
        "errorCode": "",
    }


class FakeController:
    """Deterministic fake ai2thor Controller for unit testing.

    Args:
        script: Optional list of metadata dicts to replay sequentially
            on each ``step()`` call.  If ``None``, calls default to
            ``make_default_metadata()`` with correct arguments.
        fail_on_action: If set, the named action string will trigger an
            exception on ``step()``.
        delay_seconds: If > 0, ``step()`` sleeps this long before returning
            (for timeout testing).
        metadata_override: If set, this dict is returned verbatim as the
            event metadata (overrides script).
    """

    def __init__(
        self,
        script: list[dict[str, Any]] | None = None,
        fail_on_action: str | None = None,
        delay_seconds: float = 0.0,
        metadata_override: dict[str, Any] | None = None,
    ) -> None:
        self._script: list[dict[str, Any]] | None = script
        self._script_index: int = 0
        self._fail_on_action: str | None = fail_on_action
        self._delay_seconds: float = delay_seconds
        self._metadata_override: dict[str, Any] | None = metadata_override
        self.stop_call_count: int = 0
        self.step_call_count: int = 0
        self.actions_received: list[dict[str, Any]] = []

    def step(self, action_or_dict: str | dict[str, Any]) -> FakeEvent:
        """Simulate a controller step.

        Returns a ``FakeEvent`` with ``.metadata`` derived from:
        1. ``metadata_override`` if set;
        2. the next entry in ``script`` if available;
        3. ``make_default_metadata()`` otherwise.
        """
        self.step_call_count += 1

        if isinstance(action_or_dict, str):
            action_name = action_or_dict
        elif isinstance(action_or_dict, dict):
            action_name = action_or_dict.get("action", str(action_or_dict))
        else:
            action_name = str(action_or_dict)

        self.actions_received.append(
            {"action": action_name, "raw": action_or_dict}
        )

        if self._delay_seconds > 0:
            time.sleep(self._delay_seconds)

        if self._fail_on_action and action_name == self._fail_on_action:
            raise RuntimeError(f"FakeController: injected failure on action '{action_name}'")

        if self._metadata_override is not None:
            metadata = dict(self._metadata_override)
        elif self._script is not None and self._script_index < len(self._script):
            metadata = dict(self._script[self._script_index])
            self._script_index += 1
        else:
            num_agents = len(
                make_default_metadata().get("agents", [])
            )
            metadata = make_default_metadata(
                scene="FloorPlan1",
                num_agents=num_agents,
                has_objects=True,
            )

        metadata["lastAction"] = action_name
        if "lastActionSuccess" not in metadata:
            metadata["lastActionSuccess"] = True

        return FakeEvent(metadata=metadata)

    def reset(self, scene: str = "FloorPlan1") -> FakeEvent:
        """Simulate a scene reset."""
        return FakeEvent(
            metadata=make_default_metadata(scene=scene, num_agents=1, has_objects=True)
        )

    def stop(self) -> None:
        """Record stop call (idempotent)."""
        self.stop_call_count += 1
