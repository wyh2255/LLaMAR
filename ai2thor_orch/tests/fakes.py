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
            # 视角外样本（RP1b 教训：假对象全 visible=True 时"滤 visible"与
            # 不滤在离线测试里等价，真机才露馅——全屋 77 对象清单冒充视野）。
            # 至少保留一个 visible=False 成员，使过滤行为在单测中可辨。
            {
                "objectId": "Knife|+02.0|+00.8|+01.5",
                "objectType": "Knife",
                "position": {"x": 2.0, "y": 0.8, "z": 1.5},
                "visible": False,
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


#: FakeController 支持的新 manipulation 动作（PA-W2）→ 成功后写回的状态位
#: ``(键, 值)``。成功条件宽口径：``objectId`` 存在于本轮场景 ``objects`` 列表
#: 即成功——可见性 / 可行性前置校验不做（真机的业务裁决由 Unity 侧逻辑负责，
#: 离线只复现「对象不可解析 → 动作失败」这一确定性分支）。
_MANIPULATION_STATES: dict[str, tuple[str, bool]] = {
    "SliceObject": ("isSliced", True),
    "CleanObject": ("isDirty", False),
    "ToggleObjectOn": ("isToggled", True),
    "ToggleObjectOff": ("isToggled", False),
}

#: 对象不可解析时的失败文案（逐字对齐 A100 录制的真机 errorMessage 前缀——
#: ``Agent.error_taxonomy`` 据此归到 ``object_not_visible`` 域类）。
_OBJECT_NOT_RESOLVED_MESSAGE = "Target object not found within the specified visibility"


def _split_object_action(action_name: str) -> tuple[str, str | None]:
    """``"SliceObject(Mug|-01.5|+00.9|+02.3)"`` → ``("SliceObject", "Mug|…")``。

    非 ``Verb(objectId)`` 形态（无括号 / 括号不闭合）时 ``objectId`` 为
    ``None``——调用方按「缺 objectId」处理。
    """
    verb, _, rest = action_name.partition("(")
    if rest.endswith(")"):
        return verb, rest[:-1]
    return verb, None


class FakeController:
    """Deterministic fake ai2thor Controller for unit testing.

    所有动作（含场景级 ``InitialRandomSpawn``——F-seed spawn_mode=random 的
    fake 路径）都确定性接受并记录到 :attr:`actions_received`；布局不随
    seed 变化（fake 本就确定性，随机化只验证调用形状/传递链，不模拟换布局）。

    slice / clean / toggle（PA-W2，``SliceObject`` / ``CleanObject`` /
    ``ToggleObjectOn`` / ``ToggleObjectOff``）额外模拟状态：``objectId``
    存在于本轮场景 objects → 成功 + 状态位写回 :attr:`object_states`
    （``isSliced`` / ``isDirty`` / ``isToggled``）；不存在 →
    ``lastActionSuccess=False`` + 真机口径 ``errorMessage``。

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
        #: 最近一次 ``step()`` 返回的事件（真机 controller 同款属性；
        #: ``invoke_scene_preinit`` / ``_apply_scene_preinit`` 经
        #: ``getattr(controller, "last_event", None)`` 取初始 event 传给 preinit）。
        self.last_event: FakeEvent | None = None
        #: manipulation 动作成功后的对象状态位账本
        #: （``objectId`` → ``{"isSliced" / "isDirty" / "isToggled": 值}``）。
        self.object_states: dict[str, dict[str, Any]] = {}

    def step(
        self,
        action_or_dict: str | dict[str, Any] | None = None,
        **action_kwargs: Any,
    ) -> FakeEvent:
        """Simulate a controller step (both real call shapes).

        Accepts the shapes the real controller / task preinit files use:
        ``step("MoveAhead")``, ``step({"action": ..., ...})`` and the
        keyword form ``step(action="PlaceObjectAtPoint", objectId=...,
        position=...)`` (FloorPlan ``preinit`` bodies call it this way).

        Returns a ``FakeEvent`` with ``.metadata`` derived from:
        1. ``metadata_override`` if set;
        2. the next entry in ``script`` if available;
        3. ``make_default_metadata()`` otherwise.
        """
        self.step_call_count += 1

        if action_kwargs:
            merged: dict[str, Any] = (
                dict(action_or_dict) if isinstance(action_or_dict, dict) else {}
            )
            if action_or_dict is not None and not isinstance(action_or_dict, dict):
                merged.setdefault("action", action_or_dict)
            merged.update(action_kwargs)
            if not merged.get("action"):
                raise TypeError(
                    "step() missing required argument 'action' "
                    f"(got kwargs: {sorted(action_kwargs)})"
                )
            action_or_dict = merged

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

        is_default_metadata = False
        if self._metadata_override is not None:
            metadata = dict(self._metadata_override)
        elif self._script is not None and self._script_index < len(self._script):
            metadata = dict(self._script[self._script_index])
            self._script_index += 1
        else:
            is_default_metadata = True
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

        # slice / clean / toggle（PA-W2）：以「objectId 是否存在于本轮场景
        # objects」判定成败；成功时写回状态位。其余动作行为逐字不变。
        failure_message = self._apply_manipulation_state(action_name, metadata)
        if failure_message is not None:
            metadata["lastActionSuccess"] = False
            metadata["errorMessage"] = failure_message
        if is_default_metadata and isinstance(metadata.get("objects"), list):
            # 默认 metadata 的 objects 每步重建：把累计状态位投影回场景物体，
            # 使翻转经事件 metadata 同样可查（真机 objects 条目携带同名字段）。
            # script / override 载具不投影——避免改写调用方持有的嵌套对象。
            # 投影是场景状态视图，与本步动作成败无关（失败步同样投影）。
            for obj in metadata["objects"]:
                if isinstance(obj, dict) and obj.get("objectId") in self.object_states:
                    obj.update(self.object_states[obj["objectId"]])

        event = FakeEvent(metadata=metadata)
        self.last_event = event
        return event

    def _apply_manipulation_state(
        self, action_name: str, metadata: dict[str, Any]
    ) -> str | None:
        """slice / clean / toggle 动作的确定性状态变化。

        Args:
            action_name: 本步动作串（``"SliceObject(Mug|…)"`` 形态）。
            metadata: 本步将返回的事件 metadata（场景 objects 读取面）。

        Returns:
            失败文案（``None`` = 成功或非本类动作）。
        """
        verb, object_id = _split_object_action(action_name)
        state = _MANIPULATION_STATES.get(verb)
        if state is None:
            return None

        objects = metadata.get("objects")
        scene_ids = (
            {
                obj.get("objectId")
                for obj in objects
                if isinstance(obj, dict)
            }
            if isinstance(objects, list)
            else set()
        )
        if not object_id:
            return f"{verb} requires an objectId argument"
        if object_id not in scene_ids:
            return f"{_OBJECT_NOT_RESOLVED_MESSAGE}: {object_id}"

        state_key, value = state
        self.object_states.setdefault(object_id, {})[state_key] = value
        return None

    def reset(self, scene: str = "FloorPlan1") -> FakeEvent:
        """Simulate a scene reset."""
        event = FakeEvent(
            metadata=make_default_metadata(scene=scene, num_agents=1, has_objects=True)
        )
        self.last_event = event
        return event

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


#: 真 build 的动作参数白名单（只对登记的动作生效；值 = (合法参数集, 报错文案里
#: 的 ``Expected arguments`` 段)）。RP2 教训：离线 fake 不校验参数 → 真机才炸。
#: ``PutObject`` 的语义 = ``objectId`` 传**目标容器**（放置手上持有物），合法
#: 参数不含 ``receptacleObjectId``（报文取自 A100 真机 CloudRendering build）。
_ACTION_ARGUMENT_SCHEMAS: dict[str, tuple[frozenset[str], str]] = {
    "PutObject": (
        frozenset({"objectId", "forceAction", "placeStationary", "randomSeed"}),
        (
            "String objectId, Boolean forceAction = False, "
            "Boolean placeStationary = True, Int32 randomSeed = 0"
        ),
    ),
}

#: 全动作公共参数（动作名 + agent 槽位），不参与未知参数判定。
_COMMON_ACTION_ARGUMENTS: frozenset[str] = frozenset({"action", "agentId"})

#: ``GetReachablePositions`` 的固定可达集（navigate 工具的只读查询面与
#: ``Teleport`` 命中校验共用同一真源；真机为整场景网格点，离线用固定小集合
#: 保持确定性。y 取 agent 站立高度 0.9，与真机可达点口径一致）。
_REACHABLE_POSITIONS: list[dict[str, float]] = [
    {"x": -1.5, "y": 0.9, "z": 0.0},
    {"x": -1.25, "y": 0.9, "z": 0.25},
    {"x": -1.0, "y": 0.9, "z": 0.5},
]

#: ``Teleport`` 位置命中判定的坐标容差（(x, z) 平面；网格步长 0.25，
#: 逐位透传时相等，容差只防浮点噪声）。
_POSITION_TOLERANCE = 1e-3

#: 相机 horizon 的 build 界限（度）。horizon = 60 为俯视 60°、-30 为仰视 30°
#: （真 build ``BaseFPSAgentController``：maxDownwardLookAngle=60 /
#: maxUpwardLookAngle=30；``teleportFull`` 严格校验 ``> 60 / < -30`` 即抛）。
_MAX_DOWNWARD_HORIZON = 60.0
_MAX_UPWARD_HORIZON = 30.0

#: 相机俯仰回读残差的复现量级（度）：真机实测锚点 = Teleport 越界告警值
#: 60.00002（RP4 attempt1，trajectory.csv 第 50-53 行）；本 mock 取每次 look
#: ~1e-5 的残差，LookDown(30)×2 恰好复现该值。正是该残差让 ``teleportFull``
#: 的严格校验在 +60 界上炸掉（缺陷复现所需）。
_CAMERA_HORIZON_DRIFT = 1e-5


class MockA2TController:
    """ai2thor ``Controller`` 的最小行为替身（覆盖编排层消费面）。

    - ``step(action_dict)`` 记录调用并按 action 施加确定性状态变化；
    - 每步返回 ``MultiAgentEvent``（``agent_count`` > 1）或单 ``Event``；
    - ``PutObject`` 按**真 build 的参数白名单**校验（``objectId`` = 目标容器，
      合法参数见 ``_ACTION_ARGUMENT_SCHEMAS``）：出现 ``receptacleObjectId``
      等未知参数 → ``lastActionSuccess=False`` 且 ``errorMessage`` 含
      ``invalid argument``（RP2 教训：离线 fake 不校验参数 → 真机才炸）；
    - ``fail_on``：指定 action 名 → 抛 ``ValueError``（模拟 ai2thor 调用级拒绝）；
    - ``boom_on``：指定 action 名 → 抛 ``RuntimeError``（模拟基础设施异常）；
    - ``LookUp`` / ``LookDown`` 维护每 agent 相机 horizon：越界守卫按 0.1°
      粒度（拒绝时相机不动、报文同真机），界内更新带 ~1e-5 euler 回读残差
      （复现 +60 界上的 60.00002）；``Teleport`` 不带 ``horizon`` 时按真机
      语义取当前 horizon——越界即软失败并回真机异常原文（RP4 缺陷复现），
      带合法 horizon 则成功并写回相机（自愈路径，随 harness 修复落地）；
    - ``InitialRandomSpawn``（F-seed）：场景级动作，确定性接受并记录到
      ``steps``（布局不变——离线只验证调用形状与传递链）；
    - ``SliceObject`` / ``CleanObject`` / ``ToggleObjectOn`` /
      ``ToggleObjectOff``（PA-W2/D4）：``objectId`` 存在于场景 ``objects`` →
      成功 + 状态位（``isSliced`` / ``isDirty`` / ``isToggled``）写回该
      物体系目（后续事件 ``objects`` 视图可见）；缺失 / 不可解析 → 真机
      文案软失败——与单 agent ``FakeController`` 同口径（W3 补：此前这
      四个编排层真用动词落到 ``unhandled action`` 拒绝分支）。

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
                    "parentReceptacles": ["CounterTop|+00.0|+00.9|+02.3"],
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
        self._rotations: dict[int, dict[str, float]] = {
            i: {"x": 0.0, "y": 90.0 * i, "z": 0.0} for i in range(agent_count)
        }
        self._inventory: dict[int, list[dict[str, Any]]] = {
            i: [] for i in range(agent_count)
        }
        #: 每 agent 相机俯仰（horizon，度；60 = 俯视上限，-30 = 仰视上限）。
        self._camera_horizons: dict[int, float] = {i: 0.0 for i in range(agent_count)}
        self.last_event: MockA2TEvent | MockA2TMultiAgentEvent = self._make_event(
            "Initialize", success=True
        )

    def step(
        self,
        action: dict[str, Any] | None = None,
        **action_kwargs: Any,
    ) -> MockA2TEvent | MockA2TMultiAgentEvent:
        """模拟一次 ``controller.step(...)``（记录 + 状态变化 + 事件返回）。

        兼容真机两种调用形态：``step(action_dict)`` 与
        ``step(action=..., **params)``（任务 ``preinit`` 走后者；参数合并成
        同一 dict 后按既有路径处理）。
        """
        if action_kwargs:
            merged: dict[str, Any] = dict(action or {})
            merged.update(action_kwargs)
            action = merged
        elif action is None:
            raise TypeError("step() missing required argument 'action'")
        name = str(action.get("action", ""))
        agent_id = int(action.get("agentId", 0))
        self.steps.append(dict(action))

        # 真 build 的客户端参数校验（RP2）：登记动作出现未知参数 → 拒绝，
        # 不施加状态变化、不进入下方动作分支（真机同样达不到仿真侧）。
        schema = _ACTION_ARGUMENT_SCHEMAS.get(name)
        if schema is not None:
            allowed, expected = schema
            unknown = sorted(
                key for key in action if key not in allowed | _COMMON_ACTION_ARGUMENTS
            )
            if unknown:
                message = (
                    f'\n\tAction: "{name}" called with invalid argument: '
                    f"{unknown[0]!r}\n"
                    f"\tExpected arguments: {expected}\n"
                    f"\tYour arguments: {', '.join(repr(k) for k in action)}\n"
                )
                self.last_event = self._make_event(name, success=False, message=message)
                return self.last_event

        if name in self.boom_on:
            raise RuntimeError(f"unity crashed on {name}")
        if name in self.fail_on:
            raise ValueError(f'Action: "{name}" called with invalid argument: simulated')

        success = True
        message = ""
        error_code = "SimulatedFailure"
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
            # 该 build 的语义：objectId = 目标容器，放置手上持有物。
            receptacle = action.get("objectId")
            holding = list(self._inventory[agent_id])
            if not holding:
                success, message = False, "Agent is not holding an object"
            else:
                self._inventory[agent_id] = []
                first = holding[0]
                held_id = first.get("objectId") if isinstance(first, dict) else None
                target = self._find(held_id)
                if target is not None:
                    # 真机语义（Bug C / RP3）：存完整 objectId，不剥成裸类型名
                    target["parentReceptacles"] = [str(receptacle)]
        elif name == "GetReachablePositions":
            pass  # actionReturn 由 _make_event 填充
        elif name == "Teleport":
            # navigate 的移动原语（真机 = 全量 Teleport：position dict +
            # rotation dict + agentId，与迁移前 base_env.agent_init_pos 同形状）。
            # horizon 语义与真 build 对齐（PhysicsRemoteFPSAgentController.
            # Teleport → teleportFull）：动作不带 horizon → 取当前相机
            # euler.x 原样透传；严格越界（> 60 / < -30）→ 软失败并回真机
            # 异常原文（RP4 attempt1 的连锁拒绝由此复现）。
            # 目标点必须落在可达集内 → 写回位置/朝向/horizon 并成功；否则
            # 软失败（工具按候选点降序 fallback，至多 3 个）。
            horizon = action.get("horizon")
            if horizon is None:
                horizon = self._camera_horizons[agent_id]
            try:
                horizon_value = float(horizon)
            except (TypeError, ValueError):
                horizon_value = float("nan")
            if not (-_MAX_UPWARD_HORIZON <= horizon_value <= _MAX_DOWNWARD_HORIZON):
                success, message = (
                    False,
                    "ArgumentOutOfRangeException: Specified argument was out "
                    "of the range of valid values.\nParameter name: Each "
                    f"horizon must be in [-30:60]. You gave {horizon_value}.",
                )
            else:
                position = action.get("position")
                if not isinstance(position, dict) or not self._is_reachable(position):
                    success, message = (
                        False,
                        f"Teleport target {position!r} is not a reachable position",
                    )
                else:
                    self._camera_horizons[agent_id] = round(horizon_value, 5)
                    self._positions[agent_id] = {
                        "x": float(position.get("x", 0.0)),
                        "y": float(position.get("y", 0.9)),
                        "z": float(position.get("z", 0.0)),
                    }
                    rotation = action.get("rotation")
                    if isinstance(rotation, dict):
                        self._rotations[agent_id] = {
                            axis: float(rotation.get(axis, 0.0))
                            for axis in ("x", "y", "z")
                        }
        elif name in ("LookUp", "LookDown"):
            # look 原语：真 build 先做 ±界守卫（0.1° 粒度，越界 → 拒且相机
            # 不动），界内 Rotate 后相机 euler 回读带 ~1e-5 残差。
            success, message, error_code = self._look_result(
                name, agent_id, action.get("degrees")
            )
        elif name in _MANIPULATION_STATES:
            # PA-W2/D4：slice / clean / toggle 编排层新动词——与
            # ``FakeController._apply_manipulation_state`` 同口径：objectId
            # 存在于场景 objects → 成功 + 状态位写回（投影进后续事件的
            # objects 视图）；缺失 / 不可解析 → 真机文案软失败。
            object_id = action.get("objectId")
            if not object_id:
                success, message = False, f"{name} requires an objectId argument"
            else:
                target = self._find(object_id)
                if target is None:
                    success, message = (
                        False,
                        f"{_OBJECT_NOT_RESOLVED_MESSAGE}: {object_id}",
                    )
                else:
                    state_key, value = _MANIPULATION_STATES[name]
                    target[state_key] = value
        elif name in (
            "Pass",
            "RotateLeft",
            "RotateRight",
            "OpenObject",
            "CloseObject",
            "Done",
            # F-seed：场景级随机初始布局（fake 路径的 spawn_mode=random）。
            # 确定性接受并记录（self.steps），布局不随 seed 变化。
            "InitialRandomSpawn",
        ):
            pass
        else:
            success, message = False, f"unhandled action {name}"

        # 与真实 ai2thor 一致：step() 把结果记到 last_event
        self.last_event = self._make_event(
            name, success=success, message=message, error_code=error_code
        )
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

    def _look_result(
        self, name: str, agent_id: int, degrees: Any
    ) -> tuple[bool, str, str]:
        """``LookUp`` / ``LookDown``：真 build 守卫 + 带残差的相机俯仰更新。

        - 守卫：目标角按 0.1° 粒度比对 ±界（真 build ``checkForUpDownAngleLimit``
          同粒度）——越界 → 拒（errorCode 取真机同名 ``LookDownCantExceedMin``，
          up/down 共用该枚举值）、相机不动；
        - 界内：写回 ``目标角 + ~1e-5 euler 回读残差``（复现真机 +60 界上的
          60.00002，使 Teleport 越界连锁在离线可测）。
        """
        try:
            step = float(degrees)
        except (TypeError, ValueError):
            step = 0.0
        if step == 0:
            step = 30.0  # 真 build：degrees == 0 → 默认 30
        sign = 1.0 if name == "LookDown" else -1.0
        target = self._camera_horizons[agent_id] + sign * step
        rounded = round(target, 1)
        if rounded > _MAX_DOWNWARD_HORIZON:
            return (
                False,
                "can't look down beyond "
                f"{_MAX_DOWNWARD_HORIZON:g} degrees below the forward horizon",
                "LookDownCantExceedMin",
            )
        if rounded < -_MAX_UPWARD_HORIZON:
            return (
                False,
                "can't look up beyond "
                f"{_MAX_UPWARD_HORIZON:g} degrees above the forward horizon",
                "LookDownCantExceedMin",
            )
        self._camera_horizons[agent_id] = round(target + _CAMERA_HORIZON_DRIFT, 5)
        return True, "", ""

    def _is_reachable(self, position: dict[str, Any]) -> bool:
        """``(x, z)`` 平面是否命中固定可达集（Teleport 校验用）。

        真机可达点来自 ``GetReachablePositions`` 的网格；这里按同一集合做
        命中判定（容差 ``_POSITION_TOLERANCE``），y 不参与判定（可达点的 y
        由动作方按候选点原样透传）。
        """
        try:
            x, z = float(position["x"]), float(position["z"])
        except (KeyError, TypeError, ValueError):
            return False
        return any(
            abs(x - point["x"]) < _POSITION_TOLERANCE
            and abs(z - point["z"]) < _POSITION_TOLERANCE
            for point in _REACHABLE_POSITIONS
        )

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
                "rotation": dict(self._rotations[agent_id]),
                "cameraHorizon": self._camera_horizons[agent_id],
                "isStanding": True,
            },
            "objects": [dict(obj) for obj in self.objects],
            "inventoryObjects": [dict(obj) for obj in self._inventory[agent_id]],
            "sceneName": self.scene,
            "screenWidth": 300,
            "screenHeight": 300,
        }

    def _make_event(
        self,
        action: str,
        *,
        success: bool,
        message: str = "",
        error_code: str = "SimulatedFailure",
    ) -> MockA2TEvent | MockA2TMultiAgentEvent:
        events: list[MockA2TEvent] = []
        for agent_id in range(self.agent_count):
            metadata = self._agent_metadata(agent_id)
            metadata.update(
                {
                    "lastAction": action,
                    "lastActionSuccess": success,
                    "errorMessage": message,
                    "errorCode": "" if success else error_code,
                }
            )
            if action == "GetReachablePositions":
                metadata["actionReturn"] = [dict(p) for p in _REACHABLE_POSITIONS]
            events.append(MockA2TEvent(metadata))
        if self.agent_count == 1:
            return events[0]
        return MockA2TMultiAgentEvent(events)
