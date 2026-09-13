"""FakeController — deterministic fake for unit-testing the AI2Thor barrier.

Provides a controllable ``step()`` / ``reset()`` / ``stop()`` implementation
that returns structured metadata without needing a real Unity build.

Also provides ``MockA2TController`` — a richer double shaped like the real
``ai2thor 5.0`` Python API (MultiAgentEvent / per-agent metadata /
``agentCount`` / ``agentId`` / ``ValueError`` on invalid calls).  It backs the
P5-4 unity-adapter unit tests and the unity branch of
``scripts/ai2thor_runtime_smoke.py``, both of which must run without a GPU.
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


# ═══════════════════════════════════════════════════════════════════════════
# ai2thor 5.0 形状的多 agent 替身（P5-4 unity 适配层 / 冒烟探针共用）
# ═══════════════════════════════════════════════════════════════════════════


class MockA2TEvent:
    """单 agent ``Event`` 替身（``events=[self]``，与 ai2thor 5.0 同构）。"""

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        self.events = [self]


class MockA2TMultiAgentEvent:
    """``MultiAgentEvent`` 替身：``events`` 为每 agent 一份 ``MockA2TEvent``。"""

    def __init__(self, events: list[MockA2TEvent]) -> None:
        self.events = events
        self.metadata = events[0].metadata


class MockA2TController:
    """ai2thor ``Controller`` 的最小行为替身（覆盖编排层消费面）。

    - ``step(action_dict)`` 记录调用并按 action 施加确定性状态变化；
    - 每步返回 ``MultiAgentEvent``（``agent_count`` > 1）或单 ``Event``；
    - ``fail_on``：指定 action 名 → 抛 ``ValueError``（模拟 ai2thor 调用级拒绝）；
    - ``boom_on``：指定 action 名 → 抛 ``RuntimeError``（模拟基础设施异常）。

    Args:
        agent_count: ``agentCount`` 初始化参数（决定事件里的 agent 数）。
        scene: 场景名（写入 metadata ``sceneName``）。
        objects: 场景物体列表（缺省 Mug + Fridge，够 barrier/verifier 消费）。
        fail_on / boom_on: 注入异常的 action 名集合。
    """

    def __init__(
        self,
        *,
        agent_count: int = 2,
        scene: str = "FloorPlan1",
        objects: list[dict[str, Any]] | None = None,
        fail_on: set[str] | None = None,
        boom_on: set[str] | None = None,
    ) -> None:
        self.agent_count = agent_count
        self.scene = scene
        self.steps: list[dict[str, Any]] = []
        self.stop_count = 0
        self.fail_on = set(fail_on or ())
        self.boom_on = set(boom_on or ())
        self.objects: list[dict[str, Any]] = list(
            objects
            if objects is not None
            else [
                {
                    "objectId": "Mug|-01.5|+00.9|+02.3",
                    "objectType": "Mug",
                    "position": {"x": -1.5, "y": 0.9, "z": 2.3},
                    "visible": True,
                    "parentReceptacles": ["CounterTop"],
                },
                {
                    "objectId": "Fridge|+00.0|+00.0|+01.0",
                    "objectType": "Fridge",
                    "position": {"x": 0.0, "y": 0.0, "z": 1.0},
                    "visible": True,
                    "parentReceptacles": [],
                },
            ]
        )
        self._positions: dict[int, dict[str, float]] = {
            i: {"x": float(i), "y": 0.9, "z": 0.0} for i in range(agent_count)
        }
        self._inventory: dict[int, list[dict[str, Any]]] = {
            i: [] for i in range(agent_count)
        }
        self.last_event: MockA2TEvent | MockA2TMultiAgentEvent = self._make_event(
            "Initialize", success=True
        )

    def step(self, action: dict[str, Any]) -> MockA2TEvent | MockA2TMultiAgentEvent:
        """模拟一次 ``controller.step(dict)``（记录 + 状态变化 + 事件返回）。"""
        name = str(action.get("action", ""))
        agent_id = int(action.get("agentId", 0))
        self.steps.append(dict(action))

        if name in self.boom_on:
            raise RuntimeError(f"unity crashed on {name}")
        if name in self.fail_on:
            raise ValueError(f'Action: "{name}" called with invalid argument: simulated')

        success = True
        message = ""
        if name == "MoveAhead":
            self._positions[agent_id]["z"] += 0.25
        elif name == "PickupObject":
            target = self._find(action.get("objectId"))
            if target is None:
                success, message = False, "Object not found"
            else:
                self._inventory[agent_id] = [target]
                target["parentReceptacles"] = [f"Agent{agent_id}"]
        elif name == "PutObject":
            held = action.get("objectId")
            receptacle = action.get("receptacleObjectId")
            if not self._inventory[agent_id]:
                success, message = False, "Agent is not holding an object"
            else:
                self._inventory[agent_id] = []
                target = self._find(held)
                if target is not None:
                    target["parentReceptacles"] = [str(receptacle).split("|")[0]]
        elif name == "GetReachablePositions":
            pass  # actionReturn 由 _make_event 填充
        elif name in ("Pass", "RotateLeft", "RotateRight", "LookUp", "LookDown",
                      "OpenObject", "CloseObject", "Done"):
            pass
        else:
            success, message = False, f"unhandled action {name}"

        # 与真实 ai2thor 一致：step() 把结果记到 last_event
        self.last_event = self._make_event(name, success=success, message=message)
        return self.last_event

    def reset(self, scene: str) -> MockA2TEvent | MockA2TMultiAgentEvent:
        """模拟 ``controller.reset(scene)``。"""
        self.scene = scene
        self.last_event = self._make_event("Reset", success=True)
        return self.last_event

    def stop(self) -> None:
        """记录 stop 调用（幂等由调用方保证）。"""
        self.stop_count += 1

    # -- 内部 ------------------------------------------------------------------

    def _find(self, object_id: Any) -> dict[str, Any] | None:
        for obj in self.objects:
            if obj["objectId"] == object_id:
                return obj
        return None

    def _agent_metadata(self, agent_id: int) -> dict[str, Any]:
        return {
            "agentId": agent_id,
            "agent": {
                "position": dict(self._positions[agent_id]),
                "rotation": {"x": 0.0, "y": 90.0 * agent_id, "z": 0.0},
                "cameraHorizon": 0.0,
                "isStanding": True,
            },
            "objects": [dict(obj) for obj in self.objects],
            "inventoryObjects": [dict(obj) for obj in self._inventory[agent_id]],
            "sceneName": self.scene,
            "screenWidth": 300,
            "screenHeight": 300,
        }

    def _make_event(
        self, action: str, *, success: bool, message: str = ""
    ) -> MockA2TEvent | MockA2TMultiAgentEvent:
        events: list[MockA2TEvent] = []
        for agent_id in range(self.agent_count):
            metadata = self._agent_metadata(agent_id)
            metadata.update(
                {
                    "lastAction": action,
                    "lastActionSuccess": success,
                    "errorMessage": message,
                    "errorCode": "" if success else "SimulatedFailure",
                }
            )
            if action == "GetReachablePositions":
                metadata["actionReturn"] = [
                    {"x": -1.5, "y": 0.9, "z": 0.0},
                    {"x": -1.25, "y": 0.9, "z": 0.25},
                    {"x": -1.0, "y": 0.9, "z": 0.5},
                ]
            events.append(MockA2TEvent(metadata))
        if self.agent_count == 1:
            return events[0]
        return MockA2TMultiAgentEvent(events)
