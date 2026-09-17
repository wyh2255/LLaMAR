"""AI2Thor worker 视觉层：把当前轮 POV 关键帧注入 worker 的 LLM 请求（F-vlm）。

设计依据
========
``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md`` §2
（VLM 帧注入；2026-09-18 编排侧拍板的开关口径）：

- **激活开关**：``LLAMAR_AI2THOR_VLM=1``（本模块为唯一读取点）。
  **双开关**：还须 ``LLAMAR_AI2THOR_FRAMES=1`` 且 ``mode=unity``——缺一则
  整条通道不激活（纯文本口径），并由 :func:`warn_misconfigured_vision`
  在装配期响亮警告（log，不抛异常）。
- **注入形态**：worker 每轮 LLM 请求中，Environment State 消息升级为
  ``[{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]``
  两段式 parts（OpenAI 兼容多模态口径；C1 smoke 实测通过，见
  ``.hermes/ai2thor/C1-vlm-smoke-20260917.md``）。
- **新鲜度契约**：帧必须与当前回合对齐。帧由 ``FrameStore``（捕获点：
  ``UnityController``）以 ``round_no``（= 已完成步数，与 Environment State
  渲染的 ``step`` 同口径）为键记录；步内记录的帧归入下一轮快照，天然满足
  "每轮拿到的是本轮生成时点的 POV"。若最新帧轮次与当前 step 不一致
  （首帧未到、乱序、重放降级等），**不附图**并在 text 末尾附加文字注记，
  绝不附陈旧帧。
- **降级契约**：无帧 / 未激活 / 编码失败 → 消息原样（fail-open，不阻塞
  本轮请求）；帧轮次不匹配 → 纯文本 + 注记。
- **不附图侧**：coordinator 不注入视觉（D2 口径，设计 §2.6）；SAR 侧完全
  不受影响（本模块只被 ``ai2thor_orch`` 装配层引用；依赖方向硬约束见
  ``test_env_pack.py`` 的 import 探针）。

接线机制（为什么是 session 工厂 + Context 子类）
================================================
src 内核的 ``WorkerHooks`` 注入口在 ``attach_context(...)`` 上，首条提示不是
LLM 请求、且 hooks 对"已组装的消息内容"没有变更面；真正被 LLM 消费的请求体
由 ``AI2ThorWorkerContextManager.assemble()`` 组装。因此本通道在**装配层**
收口：``Ai2ThorEnvPack.build_session_factory`` 在视觉激活时返回
:class:`VisionWorkerContextManager`（本模块），由其在 ``assemble()`` 中把
**本次新追加的** Environment State 消息升级为多模态 parts。src/ 内核零改动。

严格边界（注入面）：只升级本次 assemble 新追加的 Environment State 消息
（对象身份比对：尾消息是历史消息时一律不触碰）；历史消息与 system prompt
逐字节保持不变——这也是 DeepSeek 前缀缓存的稳定前缀口径。
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from ai2thor_orch.executor.unity_controller import (
    ENV_PREFIX,
    _env_flag,
    _env_int,
    frames_enabled,
)
from ai2thor_orch.state.context import AI2ThorWorkerContextManager

if TYPE_CHECKING:
    from Agent.worker_agent.schema import Message

logger = logging.getLogger(__name__)

#: VLM 帧注入开关（缺省关；本模块为唯一读取点）。
#: 语义与 ``LLAMAR_AI2THOR_FRAMES`` 同口径（``0/false/no/off`` 为假，其余非空为真）。
VLM_SWITCH_ENV = f"{ENV_PREFIX}VLM"

#: JPEG 编码质量（缺省 80；质量/体积折中，与 C1 smoke 口径一致）。
JPEG_QUALITY_DEFAULT = 80

#: 注记前缀（不附图时的纯文本说明，供 transcript 审查定位）。
VISION_NOTE_PREFIX = "[vision]"

# upgrade_env_block 结果代码（日志/测试口径）。
OUTCOME_ATTACHED = "attached"  # 已升级为 text+image parts
OUTCOME_STALE = "stale_frame"  # 帧轮次 != 当前 step，仅附文字注记
OUTCOME_NO_FRAME = "no_frame"  # 尚无任何帧，消息原样
OUTCOME_DISABLED = "disabled"  # 通道未激活，消息原样
OUTCOME_NOT_TEXT = "not_text"  # 消息内容非纯文本（不二次处理）
OUTCOME_ERROR = "encode_error"  # 编码失败，降级纯文本（fail-open）


def vlm_enabled() -> bool:
    """``LLAMAR_AI2THOR_VLM`` 开关（缺省关）。"""
    return _env_flag(VLM_SWITCH_ENV, False)


def frame_resolution() -> tuple[int, int]:
    """运行分辨率 ``(width, height)``。

    与 ``unity_launch_options`` 同源同缺省：``LLAMAR_AI2THOR_WIDTH`` /
    ``LLAMAR_AI2THOR_HEIGHT``，缺省 300×300。
    """
    width = _env_int(f"{ENV_PREFIX}WIDTH", 300)
    height = _env_int(f"{ENV_PREFIX}HEIGHT", 300)
    return (
        int(width) if width is not None else 300,
        int(height) if height is not None else 300,
    )


def vision_switches_ready(*, mode: str) -> bool:
    """开关层面是否就位：``VLM=1 ∧ FRAMES=1 ∧ mode=unity``。

    run metadata 的 ``vision_enabled`` 以此为准（帧存储由装配期
    ``build_barrier`` 在同条件下必然接线，见 :func:`vision_channel_active`）。
    """
    return vlm_enabled() and frames_enabled() and mode == "unity"


def vision_channel_active(*, mode: str, frame_store: Any | None) -> bool:
    """视觉注入通道是否**实际**激活（开关就位 ∧ 帧源就位）。"""
    return frame_store is not None and vision_switches_ready(mode=mode)


def resolve_vision_config(*, mode: str, model: str) -> dict[str, Any]:
    """run metadata 视觉三字段（``vision_enabled`` / ``vision_model`` / ``frame_resolution``）。

    未激活时 ``vision_model`` 与 ``frame_resolution`` 均为 ``None``（字段
    始终存在，schema 稳定——sweep 聚合侧据此过滤口径）。
    """
    if vision_switches_ready(mode=mode):
        width, height = frame_resolution()
        return {
            "vision_enabled": True,
            "vision_model": model,
            "frame_resolution": [width, height],
        }
    return {"vision_enabled": False, "vision_model": None, "frame_resolution": None}


def warn_misconfigured_vision(*, mode: str, frame_store: Any | None) -> None:
    """装配期启动检查：VLM 开关与帧源不匹配时响亮警告（log，不抛异常）。

    由 ``Ai2ThorEnvPack.build_barrier`` 在帧存储/控制器就位后调用一次。
    缺配情形：
    1) ``VLM=1`` 但 ``FRAMES`` 未开——无帧源；
    2) ``VLM=1`` ∧ ``FRAMES=1`` 但 ``mode=fake``——mock Controller 无帧；
    3) ``VLM=1`` ∧ ``FRAMES=1`` ∧ ``mode=unity`` 但帧存储未就位（装配链异常）。
    全部就位时 INFO 一条激活日志（分辨率 + 编码口径）。
    """
    if not vlm_enabled():
        return
    if not frames_enabled():
        logger.warning(
            "%s=1 但 %sFRAMES 未开：无帧源，视觉注入不会激活，本次 run 为纯文本口径",
            VLM_SWITCH_ENV,
            ENV_PREFIX,
        )
        return
    if mode != "unity":
        logger.warning(
            "%s=1 且 %sFRAMES=1 但 mode=%s：fake 无帧源，视觉注入不会激活，本次 run 为纯文本口径",
            VLM_SWITCH_ENV,
            ENV_PREFIX,
            mode,
        )
        return
    if frame_store is None:
        logger.warning(
            "%s=1 且 %sFRAMES=1 但帧存储未接线：视觉注入不会激活（检查装配链）",
            VLM_SWITCH_ENV,
            ENV_PREFIX,
        )
        return
    width, height = frame_resolution()
    logger.info(
        "AI2Thor 视觉注入已激活：分辨率=%dx%d jpeg_quality=%d（worker 每轮请求附当前轮 POV）",
        width,
        height,
        JPEG_QUALITY_DEFAULT,
    )


def _coerce_step(step: Any) -> int | None:
    """把 state payload 的 step 归一到 int；缺失/非法 → ``None``（不附图）。"""
    if step is None:
        return None
    try:
        return int(step)
    except (TypeError, ValueError):
        return None


class VisionHooks:
    """把当前轮 POV 帧升级进 Environment State 消息（视觉激活时装配层构造）。

    与具体 ContextManager 解耦：:class:`VisionWorkerContextManager.assemble`
    在追加完文本 Environment State 块后调用 :meth:`upgrade_env_block`。

    线程模型：每个 worker 由 ``build_session_factory`` 的闭包各自构造实例
    （构造线程 = 该 worker 的装配线程），无跨线程共享。
    """

    def __init__(
        self,
        *,
        frame_store: Any = None,
        agent_idx: int = 0,
        enabled: bool = False,
        jpeg_quality: int = JPEG_QUALITY_DEFAULT,
    ) -> None:
        self._frame_store = frame_store
        self._agent_idx = agent_idx
        self._enabled = enabled
        self._jpeg_quality = jpeg_quality
        #: 首帧注入一次性日志闩（确认通道真的通了，又不刷屏）。
        self._attached_once = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def upgrade_env_block(self, message: Message, *, step: Any) -> str:
        """把 Environment State 消息 ``message`` 升级为 text+image parts。

        Args:
            message: 本次 assemble 新追加的 Environment State 消息（role=user）。
            step: 当前回合（Environment State 渲染的同一个值；缺失 → 不附图）。

        Returns:
            结果代码（``OUTCOME_*``；仅日志/测试用）。契约：
            帧轮次 == 当前 step → 升级为 parts（原 text 原样为第一段）；
            不等 / 无帧 / 编码失败 → 纯文本（不匹配时附注记，绝不附旧帧）；
            未激活 / 内容非纯文本 → 原样返回。
        """
        if not self._enabled:
            return OUTCOME_DISABLED
        content = message.content
        if not isinstance(content, str):
            # 已是多模态 / 异常形态：本通道只升级纯文本块，不做二次处理。
            return OUTCOME_NOT_TEXT
        if self._frame_store is None:
            return OUTCOME_NO_FRAME
        entry = self._frame_store.latest(self._agent_idx)
        if entry is None:
            return OUTCOME_NO_FRAME
        frame_round, frame = entry

        current = _coerce_step(step)
        if current is None or int(frame_round) != current:
            message.content = content + self._stale_note(int(frame_round), current)
            return OUTCOME_STALE
        try:
            data_url = self._encode_frame(frame)
        except Exception:
            logger.exception(
                "POV 帧编码失败，本轮降级为纯文本（agent_idx=%d）", self._agent_idx
            )
            return OUTCOME_ERROR
        message.content = [
            {"type": "text", "text": content},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        if not self._attached_once:
            self._attached_once = True
            logger.info(
                "worker 视觉注入首帧完成（agent_idx=%d step=%s round=%s）",
                self._agent_idx,
                current,
                frame_round,
            )
        return OUTCOME_ATTACHED

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _stale_note(self, frame_round: int, current: int | None) -> str:
        """不附图时的文字注记（英文，与 Environment State 块同语种）。"""
        if current is None:
            return (
                f"\n\n{VISION_NOTE_PREFIX} no POV frame attached: current step "
                f"unavailable (latest frame: round {frame_round}); a frame is "
                "never attached out of round alignment."
            )
        return (
            f"\n\n{VISION_NOTE_PREFIX} no POV frame attached: latest frame is "
            f"from round {frame_round}, current step is {current}; a stale "
            "frame is never attached."
        )

    def _encode_frame(self, frame: Any) -> str:
        """RGB ndarray → ``data:image/jpeg;base64,...``。

        与 ``FrameStore`` 落盘同口径：cv2 吃 BGR（3 通道 ``[..., ::-1]``，
        4 通道 ``[..., [2,1,0,3]]``）；编码参数 = JPEG 质量（缺省 80）。
        """
        import cv2  # 可选重依赖 → 懒加载（纯文本路径不触碰）
        import numpy as np

        array = np.asarray(frame)
        if array.ndim == 3 and array.shape[2] == 3:
            array = np.ascontiguousarray(array[:, :, ::-1])  # RGB → BGR
        elif array.ndim == 3 and array.shape[2] == 4:
            array = np.ascontiguousarray(array[:, :, [2, 1, 0, 3]])  # RGBA → BGRA
        ok, buffer = cv2.imencode(
            ".jpg", array, [int(cv2.IMWRITE_JPEG_QUALITY), int(self._jpeg_quality)]
        )
        if not ok:
            raise ValueError("cv2.imencode 编码失败")
        return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode(
            "ascii"
        )


class VisionWorkerContextManager(AI2ThorWorkerContextManager):
    """:class:`AI2ThorWorkerContextManager` + 视觉注入（机制见模块 docstring）。

    仅当视觉通道激活时由 ``Ai2ThorEnvPack.build_session_factory`` 选用；
    缺省路径仍返回 ``AI2ThorWorkerContextManager``（关掉开关时逐字节零差异）。
    """

    def __init__(
        self, *args: Any, vision_hooks: VisionHooks | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._vision_hooks = vision_hooks

    def assemble(self, system_prompt: str, messages: list[Message]) -> list[Message]:
        """组装请求体，并把**本次新追加的** Environment State 块升级为视觉 parts。

        注入面判定：``result[-1]`` 是本次 assemble 新建的 Environment State
        消息（role=user 且不是 ``messages`` 中的同一对象）；raw 策略 / 未闭合
        tool call / 无环境块时尾消息即历史消息 → 不触碰（与内核语义一致）。
        """
        result = super().assemble(system_prompt, messages)
        hooks = self._vision_hooks
        if hooks is None or not result:
            return result
        tail = result[-1]
        if tail.role != "user" or (messages and tail is messages[-1]):
            return result
        runtime_state = self._runtime_state
        step = runtime_state.get("step") if runtime_state is not None else None
        hooks.upgrade_env_block(tail, step=step)
        return result


__all__ = [
    "JPEG_QUALITY_DEFAULT",
    "OUTCOME_ATTACHED",
    "OUTCOME_DISABLED",
    "OUTCOME_ERROR",
    "OUTCOME_NOT_TEXT",
    "OUTCOME_NO_FRAME",
    "OUTCOME_STALE",
    "VISION_NOTE_PREFIX",
    "VLM_SWITCH_ENV",
    "VisionHooks",
    "VisionWorkerContextManager",
    "frame_resolution",
    "resolve_vision_config",
    "vision_channel_active",
    "vision_switches_ready",
    "vlm_enabled",
    "warn_misconfigured_vision",
]
