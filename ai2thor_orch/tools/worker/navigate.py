"""navigate tool — 传送到目标对象旁并面向它（baseline ``NavigateTo(obj)`` 宏的同能级替代）。

链路（读面只读、移动面一次 barrier 回合）：

1. ``AliasRegistry.raw(target)``（或裸类型名唯一命中）→ raw objectId；
   解析不到 → fail-closed 可行动错误（与迁移前「只能去见过的对象」语义一致）；
2. ``barrier.latest_object_metadata(raw_id)`` → 最新 controller metadata 里的
   对象坐标（纯只读，不烧回合）；
3. ``barrier.query_reachable_positions()`` → ``GetReachablePositions`` 查询结果
   （只读、按 run 缓存、不烧回合）；
4. (x, z) 平面取离目标最近的可达点 → ``Teleport``（position dict + rotation
   dict、面向目标、yaw snap 到最近 90°、带 agentId——与迁移前
   ``AI2Thor/base_env.py:56-63`` 同一原生动作）→ 提交一个 barrier 回合；
   Teleport 失败 → 次近候选点重试，至多 3 个；全失败返回可行动错误
   （提示 move/rotate 微调或先探索）。

口径说明（论文对比）：我们的 1 step = 1 决策（本例成功路径 = 1 个 Teleport
回合；可达点查询是只读 metadata 读取、不占决策）。迁移前 baseline 的
``step_num`` 在 ``NavigateTo`` 宏内每个微动作也 +1（``AI2Thor/env_new.py:534``）
——论文对比以 transport_rate / success 为主指标，steps 单列口径。
"""

from __future__ import annotations

import math
from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
from ai2thor_orch.tools.worker._barrier_helpers import action_failure_error
from ai2thor_orch.visibility import AliasRegistry

#: 候选可达点上限（最近优先；全部失败即 fail，避免无限重试）。
_MAX_CANDIDATES = 3

#: 朝向 snap 步长（度）——AI2Thor canonical horizontal angles = 0/90/180/270，
#: 与迁移前 ``closest_angles(H_ANGLES, ...)`` 的口径一致。
_YAW_STEP_DEGREES = 90.0


def facing_yaw(dx: float, dz: float) -> float:
    """面向位移 ``(dx, dz)`` 的 yaw（度，snap 到最近 90°）。

    AI2Thor 约定：``rotation.y``（yaw）= 0 朝 +Z，前向 = (sin yaw, cos yaw)。
    返回值域 ``[0, 360)``——0/90/180/270 四取一。
    """
    yaw = math.degrees(math.atan2(dx, dz))
    snapped = round(yaw / _YAW_STEP_DEGREES) * _YAW_STEP_DEGREES
    return float(snapped % 360)


def nearest_candidates(
    position: dict[str, Any],
    reachable: list[dict[str, Any]],
    *,
    limit: int = _MAX_CANDIDATES,
) -> list[dict[str, float]]:
    """(x, z) 平面按到目标距离升序取前 ``limit`` 个可达点。

    返回值为规范化的 ``{"x", "y", "z"}`` 浮点字典（x/z 必需，y 缺省 0.0）；
    距离相同时按可达集原始顺序稳定排序。无法解析（缺 x/z / 非数值）的点跳过。
    """
    target_x = float(position["x"])
    target_z = float(position["z"])
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, point in enumerate(reachable):
        if not isinstance(point, dict):
            continue
        try:
            point_x, point_z = float(point["x"]), float(point["z"])
        except (KeyError, TypeError, ValueError):
            continue
        distance_sq = (point_x - target_x) ** 2 + (point_z - target_z) ** 2
        scored.append((distance_sq, index, point))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [
        {
            "x": float(point["x"]),
            "y": float(point.get("y", 0.0)),
            "z": float(point["z"]),
        }
        for _, _, point in scored[:limit]
    ]


class NavigateTool(Tool):
    """Teleport beside a seen object and face it (one barrier round)."""

    def __init__(
        self,
        barrier: AI2ThorBarrier,
        agent_idx: int,
        alias_registry: AliasRegistry,
    ) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx
        self._alias_registry = alias_registry

    @property
    def name(self) -> str:
        return "navigate"

    @property
    def description(self) -> str:
        return (
            "Teleport to the nearest reachable spot beside a seen object "
            "(any distance, one round) and face it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Alias of the object to go to (e.g. Fridge_1, "
                        "CounterTop_2) or its bare type name (e.g. Fridge) "
                        "when that name is unambiguous."
                    ),
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        raw_id, resolve_error = self._resolve_target(target)
        if raw_id is None:
            return ToolResult(success=False, error=resolve_error)

        obj = self._barrier.latest_object_metadata(raw_id)
        if obj is None:
            return ToolResult(
                success=False,
                error=(
                    f"Unknown object alias: {target}. No controller metadata "
                    "for this object yet — explore until it enters your view, "
                    "then navigate."
                ),
            )
        position = obj.get("position")
        if not isinstance(position, dict):
            return ToolResult(
                success=False,
                error=f"Object {target} has no position in the latest metadata.",
            )
        try:
            float(position["x"]), float(position["z"])
        except (KeyError, TypeError, ValueError):
            return ToolResult(
                success=False,
                error=f"Object {target} has no usable (x, z) position.",
            )

        reachable = await self._barrier.query_reachable_positions()
        if not reachable:
            return ToolResult(
                success=False,
                error=(
                    f"Cannot navigate to {target}: reachable positions are "
                    "unavailable. Move/rotate step by step instead."
                ),
            )

        candidates = nearest_candidates(position, reachable)
        last_error = ""
        for candidate in candidates:
            yaw = facing_yaw(
                float(position["x"]) - candidate["x"],
                float(position["z"]) - candidate["z"],
            )
            action: dict[str, Any] = {
                "action": "Teleport",
                "position": dict(candidate),
                "rotation": {"x": 0.0, "y": yaw, "z": 0.0},
            }
            result = await self._barrier.submit_action(self._agent_idx, action)
            # 观测文本兜底脱敏（与其余工具一致：raw objectId 永不外泄）。
            obs = self._alias_registry.redact(result.observation)
            if result.success:
                where = ""
                if result.position is not None:
                    pos_x, pos_y, pos_z = result.position
                    where = f" Position: ({pos_x:.2f}, {pos_y:.2f}, {pos_z:.2f})."
                return ToolResult(
                    success=True,
                    content=f"Arrived beside {target}.{where} {obs}".rstrip(),
                )
            last_error = action_failure_error(f"Failed to navigate to {target}", result)

        return ToolResult(
            success=False,
            error=(
                f"{last_error}. Move/rotate closer to {target} and retry, or "
                "explore first to bring it into view."
            ),
        )

    # -- 内部 ------------------------------------------------------------------

    def _resolve_target(self, target: str) -> tuple[str | None, str]:
        """别名 → raw objectId；裸类型名唯一命中时回退，其余 fail-closed。"""
        raw_id = self._alias_registry.raw(target)
        if raw_id is not None:
            return raw_id, ""

        matches = self._alias_registry.aliases_for_type(target)
        if len(matches) == 1:
            resolved = self._alias_registry.raw(matches[0])
            if resolved is not None:
                return resolved, ""
        if matches:
            return None, (
                f"Unknown object alias: {target}. Multiple objects match this "
                f"type name — use the exact alias: {', '.join(matches)}."
            )
        return None, (
            f"Unknown object alias: {target}. Ensure the object is visible "
            "(only objects you have seen can be navigated to)."
        )


__all__ = ["NavigateTool", "facing_yaw", "nearest_candidates"]
