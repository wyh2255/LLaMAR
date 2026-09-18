"""UnityController — 真实 ``ai2thor.controller.Controller`` 的编排层适配器（env-contract P5-4）。

职责（把 unity 模式接到与 fake 完全相同的消费面上）：

1. **启动**：``ai2thor.controller.Controller`` + floorplan 启动（headless / GPU /
   X display 配置经环境变量或构造参数给定，见 :func:`unity_launch_options`），
   并以 ``agentCount=<num_agents>`` 初始化多 agent（初始化后立刻校验事件里的
   agent 数——数量不符即 fail-fast，绝不静默降级为单 agent）。
2. **动作映射**：编排层动作串 ↔ AI2Thor API 参数（见 :meth:`UnityController.build_action`）：
   ``MoveAhead`` / ``RotateLeft`` / ``LookUp(30)`` / ``PickupObject(<objectId>)`` /
   ``PutObject(<receptacleId>)`` / ``OpenObject`` / ``CloseObject`` /
   ``SliceObject`` / ``CleanObject`` / ``ToggleObjectOn`` / ``ToggleObjectOff``
   （后四者 = 论文任务集其余任务类型的交互动词，参数同为 objectId） /
   ``Done`` / ``NoOp``（空动作 ``Pass``）/ ``GetReachablePositions``（只读查询，
   F-nav）/ ``Teleport``（dict 动作携带 ``position`` / ``rotation``，F-nav
   navigate 的移动宏动作）。``Teleport`` 不带 ``horizon`` 时由映射层注入
   **夹取后的当前相机 horizon**（见 :meth:`UnityController._teleport_horizon`；
   真 build 缺省把相机 euler.x 原样交给 ``teleportFull``，边界浮点残差会触发
   ``ArgumentOutOfRangeException`` 并让该 agent 之后所有 Teleport 全拒）。
3. **事件归一化**：``MultiAgentEvent`` → 每 agent 一份普通 metadata dict，补齐
   barrier 消费面依赖的 ``agents`` 列表（``position`` / ``rotation`` /
   ``inventory.objects``）与 ``objects`` 缺省回退，使 unity 与 fake 的
   ``ControllerExecutor.execute_step`` 返回结构一致。
4. **错误处理**：
   - 动作串非法 / 字段缺失（编排层自身的契约错误）→ :class:`ActionMappingError`，
     由 barrier 记为回合执行错误（响亮失败，不静默）；
   - 空手 ``PutObject``（LLM 可犯的域错误）→ 返回 ``lastActionSuccess=False`` 的
     软失败事件，LLM 在观测里看到失败并可重试；
   - ai2thor 对非法参数抛的 ``ValueError``（``InvalidAction`` / ``InvalidArgument``
     等调用错误）→ 转软失败事件并保留 ``errorMessage``（单个 agent 的非法交互不应
     让整回合所有 agent 失败）；基础设施级异常（``RuntimeError`` / ``TimeoutError`` /
     ``UnityCrashException``）不捕获，向上抛给 barrier 的回合错误路径。
5. **随机初始布局（F-seed）**：``spawn_mode="random"`` 时在 ``agentCount``
   校验（:meth:`UnityController._verify_initial_agent_count`）完成后、任何
   回合前执行一次 ``InitialRandomSpawn(randomSeed=spawn_seed)``（设计
   2026-09-17 §3.2）；失败响亮抛（fail-fast，绝不静默跑默认布局冒充随机化）。
   ``spawn_mode="default"``（缺省）逐字节零副作用——与论文 baseline 同布局。
6. **运行时关键帧捕获（F-frame；设计 2026-09-17 §1.4）**：``frame_store``
   注入（装配层在 ``LLAMAR_AI2THOR_FRAMES=1`` 时接线）后，在三个捕获点写帧：
   ①初始化后每 agent ``init`` 帧（先 ``Pass`` 强制渲染——实测初始
   ``last_event`` 帧为 None；任务布局 ``preinit`` 已先行，init 帧 = 任务
   初始布局）；②语义关键动作**成功**后按 tag 记录
   （``PickupObject/PutObject/OpenObject/CloseObject`` /
   ``SliceObject/CleanObject/ToggleObjectOn/ToggleObjectOff`` / ``Teleport``
   + ``Done``；失败动作绝不记帧）；③run 终结（``stop()``）每 agent ``final``
   帧 + 一张 ``overhead``（``ToggleMapView`` 往返，同原版 ``_get_ceiling_image``）。
   ``frame_store=None``（缺省）时逐字节零副作用——不起 Pass、零额外动作。

   帧的回合编号（``round_no``）口径：与 coordinator 环境视图的 step 同源
   ——ControllerExecutor 每回合把该回合 N 个动作按 agent 槽位 ``0..N-1``
   顺序下发（``enumerate`` 顺序），故「agent 0 的动作到达」= 新回合开始，
   计数自增后本回合捕获全部携带该值（init 帧 = 0）。navigate 的只读查询
   ``GetReachablePositions``（不占回合）不计入。默认关闭时该计数器不维护
   （零行为差异）。

依赖方向：本模块只依赖 stdlib；``ai2thor`` 包在启动时惰性 import（fake 路径与
CI 永不触发），缺失时抛带安装指引的 ``ImportError``（``uv sync --extra
ai2thor-unity``）。

术语：**raw objectId** 指 AI2Thor 的 ``"Mug|-01.5|+00.9|+02.3"`` 形式 id；
worker 工具在提交动作前已把可见 alias 解析回 raw id（``AliasRegistry``），
本适配器只消费 raw id，不接触 alias。
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

#: 启动配置环境变量前缀（详见 docs/system_docs/ai2thor_a100_runbook.md）。
ENV_PREFIX = "LLAMAR_AI2THOR_"

#: 解析：``Name`` 或 ``Name(args)``（raw objectId 内含 ``|`` 与坐标，但不含括号）。
_ACTION_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*(.*?)\s*\))?\s*$")

#: 无需参数的方向动作（AI2Thor 同名动作）。
_DIRECTION_ACTIONS = frozenset({"MoveAhead", "MoveBack", "MoveLeft", "MoveRight"})

#: 携带单个 ``objectId`` 的动作。后四个 = 论文任务集其余任务类型的交互动词
#: （W1 D4：SliceObject/CleanObject/ToggleObjectOn/ToggleObjectOff 均为 AI2Thor
#: 5.0 同名动作，参数同为 ``objectId``）。
_OBJECT_ID_ACTIONS = frozenset(
    {
        "PickupObject",
        "OpenObject",
        "CloseObject",
        "SliceObject",
        "CleanObject",
        "ToggleObjectOn",
        "ToggleObjectOff",
    }
)

#: 携带 ``degrees`` 参数的动作（``LookUp(30)`` / ``RotateLeft(90)``）。
_ANGLE_PARAM_ACTIONS = frozenset({"LookUp", "LookDown", "RotateLeft", "RotateRight"})

#: 编排层空动作 → AI2Thor 空动作 ``Pass``。
#: ``NoOp``（idle 占位 / 超时填充）与 ``Done``（worker 任务完成标记，语义由
#: barrier 承载）在仿真侧都执行一次合法空动作；``Idle`` 兼容旧写法。
_EMPTY_ACTION_ALIASES = frozenset({"NoOp", "NoOp()", "Idle", "Done"})

#: AI2Thor 空动作名（直通，供探针/手工调用）。
_PASSTHROUGH_EMPTY_ACTIONS = frozenset({"Pass"})

#: navigate 的只读查询动作（barrier.query_reachable_positions；不占回合）——
#: 帧捕获的回合计数不计入该动作（见模块 docstring 帧捕获节）。
_READ_ONLY_QUERY_ACTION = "GetReachablePositions"

#: 语义关键动作 → 帧 tag（F-frame 捕获点②；设计 §1.4）。
#: ``Done`` 在映射层落到空动作 ``Pass``，但帧 tag 取编排层原名——``NoOp``
#: 等其它空动作别名不记帧（只有 worker 的 Done 工具算语义关键动作）。
_ACTION_FRAME_TAGS = {
    "PickupObject": "pickup_ok",
    "PutObject": "put_ok",
    "OpenObject": "open_ok",
    "CloseObject": "close_ok",
    # W1 D4：任务集其余交互动词的关键帧条目（与 pickup 同待遇）。
    "SliceObject": "slice_ok",
    "CleanObject": "clean_ok",
    "ToggleObjectOn": "toggle_on_ok",
    "ToggleObjectOff": "toggle_off_ok",
    "Teleport": "navigate_ok",
    "Done": "done",
}

#: 平台名 → ai2thor.platform 类名（CloudRendering 需 libvulkan1 + NVIDIA 驱动）。
_PLATFORM_ALIASES = {
    "cloud": "CloudRendering",
    "cloudrendering": "CloudRendering",
    "linux": "Linux64",
    "linux64": "Linux64",
}

#: ``Teleport`` 缺省 horizon 的夹取区间（度）。真 build 的 ``teleportFull``
#: 对 horizon 严格校验 ∈ [-30, 60]（``BaseFPSAgentController`` 的
#: ``maxUpwardLookAngle`` = 30 / ``maxDownwardLookAngle`` = 60，RP4 实测异常
#: 原文 ``Each horizon must be in [-30:60]``）。两侧各留 0.1° 余量：相机俯仰
#: 经 quaternion→euler 回读带 ~1e-5 浮点残差，盯在 +60 界上即得到 60.00002
#: ——严格校验下越界被拒；0.1° 与 build 的 look 步进精度（0.1 的倍数）同粒度。
_TELEPORT_HORIZON_MIN: float = -29.9
_TELEPORT_HORIZON_MAX: float = 59.9


class ActionMappingError(ValueError):
    """编排层动作无法映射为 AI2Thor 动作（契约错误，响亮失败）。"""


def _env_flag(name: str, default: bool) -> bool:
    """读布尔型环境变量（``0/false/no/off`` 为假，其余非空为真）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int | None) -> int | None:
    """读整型环境变量（空串 / 未设置返回缺省；非法值抛 ValueError fail-fast）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def frames_enabled() -> bool:
    """``LLAMAR_AI2THOR_FRAMES`` 运行帧捕获开关（缺省关；F-frame 设计 §1.4）。

    唯一读取点（``unity_launch_options`` 的 headless 互斥校验与装配层
    ``Ai2ThorEnvPack`` 的 store 接线共用）——勿在别处重复解析该变量。
    """
    return _env_flag(f"{ENV_PREFIX}FRAMES", False)


def _frame_tag(action: str | dict[str, Any]) -> str | None:
    """编排层动作 → 关键帧 tag（F-frame 捕获点②；非关键动作 → ``None``）。

    tag 取**编排层动作原名**：``Done`` 虽在映射层落到空动作 ``Pass``，仍记为
    ``done``；``NoOp`` / ``Idle`` / ``Pass`` 等其它空动作别名不记帧。
    """
    raw_name = action.get("action") if isinstance(action, dict) else action
    name: str | None = None
    if isinstance(raw_name, str):
        match = _ACTION_RE.match(raw_name)
        name = match.group(1) if match else raw_name.strip()
    if not name:
        return None
    return _ACTION_FRAME_TAGS.get(name)


def unity_launch_options(
    *,
    scene: str,
    num_agents: int,
    width: int | None = None,
    height: int | None = None,
    grid_size: float | None = None,
    visibility_distance: float | None = None,
    headless: bool | None = None,
    platform: str | None = None,
    x_display: str | None = None,
    gpu_device: int | None = None,
) -> dict[str, Any]:
    """合成 ``ai2thor.controller.Controller`` 启动参数（显式参数 > 环境变量 > 缺省）。

    环境变量（A100 运行手册同源）：

    - ``LLAMAR_AI2THOR_WIDTH`` / ``LLAMAR_AI2THOR_HEIGHT``（缺省 300×300，观测
      走 metadata，不需要大图）；
    - ``LLAMAR_AI2THOR_GRID_SIZE``（缺省 0.25）/ ``LLAMAR_AI2THOR_VISIBILITY``
      （缺省 1.5，与迁移前 AI2ThorEnv 的 ``visibilityDistance`` 一致）；
    - ``LLAMAR_AI2THOR_HEADLESS``（缺省 **1**：远程 headless 主机为常态；显式
      设 0 才开窗渲染）；
    - ``LLAMAR_AI2THOR_FRAMES``（缺省关；F-frame 帧捕获开关）：置位时**强制
      ``headless=False``**（headless 下 ai2thor 5.0 强制 ``renderImage=False``、
      帧全 None——实测事实），platform 不受影响（仍 CloudRendering 离屏渲染）。
      互斥校验：FRAMES=1 且 headless 被**显式**置真（``headless=True`` 参数或
      ``LLAMAR_AI2THOR_HEADLESS=1``）→ ``ValueError`` fail-fast（不静默丢帧）；
      HEADLESS 未设置 / 显式 0 时 FRAMES 直接获胜。
    - ``LLAMAR_AI2THOR_PLATFORM``（``cloud`` → ``CloudRendering`` 无显示 GPU 渲染；
      ``linux`` → ``Linux64``；缺省交给 ai2thor 自行选择）；
    - ``LLAMAR_AI2THOR_X_DISPLAY``（如 ``:0``；缺省沿用 ``DISPLAY``）；
    - ``LLAMAR_AI2THOR_GPU_DEVICE``（传给 ai2thor 的 ``gpu_device``）。

    Returns:
        直接可展开给 ``Controller(**options)`` 的参数字典；``platform`` 以
        *类名* 字符串给出（真正解析成 platform 类在启动函数里惰性完成，便于
        无 ai2thor 环境单测该函数）。

    Raises:
        ValueError: ``num_agents < 1`` / 未知 platform / FRAMES 与 headless
            显式置真互斥（fail-fast，启动前响亮报错）。
    """
    if num_agents < 1:
        raise ValueError(f"num_agents must be >= 1, got {num_agents}")

    # headless 解析（F-frame 开关优先于环境缺省，但与显式置真互斥）：
    #   1) 显式参数 > 环境变量 > 缺省 True（既有口径不变）；
    #   2) FRAMES=1：headless 被显式置真（参数 True / env 显式非空真值）→
    #      响亮报错；否则强制 False（帧捕获的唯一途径）。
    if headless is not None:
        resolved_headless = bool(headless)
        headless_pinned = True
    else:
        raw_headless = os.environ.get(f"{ENV_PREFIX}HEADLESS")
        headless_pinned = raw_headless is not None and raw_headless.strip() != ""
        resolved_headless = _env_flag(f"{ENV_PREFIX}HEADLESS", True)
    if frames_enabled():
        if resolved_headless and headless_pinned:
            raise ValueError(
                "LLAMAR_AI2THOR_FRAMES=1 与 headless=True 互斥：ai2thor 5.0 "
                "headless 模式强制 renderImage=False（帧全 None），将静默丢帧。"
                "请去掉 LLAMAR_AI2THOR_HEADLESS=1 / 显式 headless=True——"
                "FRAMES=1 会自动强制 headless=False（CloudRendering 离屏渲染）。"
            )
        resolved_headless = False

    options: dict[str, Any] = {
        "scene": scene,
        "width": width if width is not None else _env_int(f"{ENV_PREFIX}WIDTH", 300),
        "height": height
        if height is not None
        else _env_int(f"{ENV_PREFIX}HEIGHT", 300),
        "headless": resolved_headless,
        # 多 agent 初始化（ai2thor 初始化参数；Controller.reset 会带其重发 Initialize）
        "agentCount": num_agents,
        "gridSize": (
            grid_size
            if grid_size is not None
            else float(os.environ.get(f"{ENV_PREFIX}GRID_SIZE", "0.25"))
        ),
        "visibilityDistance": (
            visibility_distance
            if visibility_distance is not None
            else float(os.environ.get(f"{ENV_PREFIX}VISIBILITY", "1.5"))
        ),
    }

    platform_name = (
        platform
        if platform is not None
        else os.environ.get(f"{ENV_PREFIX}PLATFORM", "")
    )
    if platform_name:
        key = platform_name.strip().lower()
        if key not in _PLATFORM_ALIASES:
            raise ValueError(
                f"未知 platform {platform_name!r}；可选 {sorted(set(_PLATFORM_ALIASES))}"
            )
        options["platform"] = _PLATFORM_ALIASES[key]

    display = (
        x_display if x_display is not None else os.environ.get(f"{ENV_PREFIX}X_DISPLAY")
    )
    if display:
        options["x_display"] = display

    gpu = (
        gpu_device
        if gpu_device is not None
        else _env_int(f"{ENV_PREFIX}GPU_DEVICE", None)
    )
    if gpu is not None:
        options["gpu_device"] = gpu

    return options


def _build_ai2thor_controller(options: dict[str, Any]) -> Any:
    """默认工厂：惰性 import ai2thor 并启动真实 Controller。

    ``platform`` 字符串在此解析为 ``ai2thor.platform`` 类；ai2thor 缺失时抛
    带安装指引的 ``ImportError``（冒烟脚本据此返回退出码 2）。
    """
    try:
        import ai2thor.controller
    except ImportError as exc:  # pragma: no cover - 本机装了 ai2thor，A100 才走到
        raise ImportError(
            "unity 模式需要 ai2thor 包（GPU 主机执行 `uv sync --extra ai2thor-unity`）："
            f"{exc}"
        ) from exc

    kwargs = dict(options)
    platform_name = kwargs.pop("platform", None)
    if platform_name is not None:
        from ai2thor import platform as platform_mod

        kwargs["platform"] = getattr(platform_mod, platform_name)

    logger.info(
        "启动 AI2Thor Controller: scene=%s agents=%s headless=%s platform=%s",
        kwargs.get("scene"),
        kwargs.get("agentCount"),
        kwargs.get("headless"),
        platform_name,
    )
    return ai2thor.controller.Controller(**kwargs)


class _NormalizedEvent:
    """归一化事件——与 ``FakeEvent`` 同构的最小面（executor 只读 ``.metadata``）。"""

    __slots__ = ("metadata",)

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata: dict[str, Any] = metadata

    def __repr__(self) -> str:
        keys = sorted(self.metadata)[:8]
        return (
            f"_NormalizedEvent(lastAction={self.metadata.get('lastAction')!r}, "
            f"success={self.metadata.get('lastActionSuccess')!r}, keys={keys})"
        )


#: 合法的初始布局模式（F-seed；``default`` = 论文 baseline 同布局）。
_SPAWN_MODES = frozenset({"default", "random"})


def normalize_spawn_options(
    spawn_mode: str, spawn_seed: int | None
) -> tuple[str, int | None]:
    """校验 ``spawn_mode`` / ``spawn_seed``（fail-fast），返回校验后的元组。

    ``spawn_seed`` 的「缺省 = run seed」解析属于装配层职责（``run_experiment``
    已解析后才下传）；本函数只强制 random 模式必须携带显式整数（静默用 0
    之类猜测会让布局不可复现）。
    """
    if spawn_mode not in _SPAWN_MODES:
        raise ValueError(f"未知 spawn_mode {spawn_mode!r}；可选 {sorted(_SPAWN_MODES)}")
    if spawn_seed is not None and (
        isinstance(spawn_seed, bool) or not isinstance(spawn_seed, int)
    ):
        raise TypeError(f"spawn_seed 必须是 int，得到 {type(spawn_seed).__name__}")
    if spawn_mode == "random" and spawn_seed is None:
        raise ValueError(
            "spawn_mode='random' 需要显式 spawn_seed"
            "（缺省解析 = run seed，在 run_experiment 层完成）"
        )
    return spawn_mode, spawn_seed


def _event_metadata_view(event: Any) -> dict[str, Any] | None:
    """事件 metadata 读取（单 ``Event`` → 自身；``MultiAgentEvent`` → 首事件回退）。"""
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, dict):
        return metadata
    events = getattr(event, "events", None)
    if events:
        metadata = getattr(events[0], "metadata", None)
        return metadata if isinstance(metadata, dict) else None
    return None


def apply_initial_random_spawn(controller: Any, *, spawn_seed: int | None) -> Any:
    """对 controller 执行一次 ``InitialRandomSpawn`` 并校验成功（fail-fast）。

    - 调用形状与迁移前 ``AI2Thor/base_env.py`` 的 ``random_spawn`` 一致
      （``InitialRandomSpawn(randomSeed=seed)``；场景级动作，不带 ``agentId``）；
    - fake 与 unity 两条构造路径共用同一执行/校验口径
      （``FakeController`` 接受并记录该动作、布局不变——fake 本就确定性）；
    - 失败（``lastActionSuccess`` 非真 / 无 metadata 可核）→ 响亮抛
      ``RuntimeError``：随机化未生效时绝不静默继续跑默认布局冒充随机化
      （设计 2026-09-17 §3.2）。

    Returns:
        原始事件对象（调用方如需刷新 metadata 缓存自取）。
    """
    if spawn_seed is None:
        raise ValueError("InitialRandomSpawn 需要显式 spawn_seed（不接受缺省猜测）")
    event = controller.step(
        {"action": "InitialRandomSpawn", "randomSeed": int(spawn_seed)}
    )
    metadata = _event_metadata_view(event) or {}
    if not metadata.get("lastActionSuccess"):
        raise RuntimeError(
            "InitialRandomSpawn 失败——spawn_mode=random 未生效即终止"
            "（fail-fast，拒绝以默认布局冒充随机化）："
            f"lastActionSuccess={metadata.get('lastActionSuccess')!r} "
            f"errorMessage={metadata.get('errorMessage')!r}"
        )
    return event


class UnityController:
    """真实 AI2Thor Controller 的编排层包装（多 agent / 动作映射 / 事件归一化）。

    Args:
        scene: FloorPlan 名（如 ``FloorPlan1``）。
        num_agents: 参与 agent 数——必须与 barrier 的 ``num_agents`` 一致
            （``Ai2ThorEnvPack.build_barrier`` 透传），初始化后校验。
        controller_factory: 测试注入点（接收 :func:`unity_launch_options` 的
            kwargs 字典，返回 ai2thor Controller 兼容对象）；``None`` 时启动
            真实 Controller。
        spawn_mode: 初始布局模式（F-seed）——``"default"``（缺省，与论文
            baseline 同布局）或 ``"random"``（``InitialRandomSpawn`` 随机化，
            失败即抛）。
        spawn_seed: 布局随机化 seed（``spawn_mode="random"`` 时必填；缺省
            解析 = run seed，由 ``run_experiment`` 装配层完成）。
        frame_store: 运行时关键帧存储（F-frame；``ai2thor_orch.frames.FrameStore``
            兼容对象）。``None``（缺省）时帧捕获完全关闭、逐字节零副作用；
            装配层在 ``LLAMAR_AI2THOR_FRAMES=1`` 时注入（见
            ``Ai2ThorEnvPack.build_barrier``）。
        scene_initializer: 任务布局初始化器（D5；``load_task`` 装入
            TaskContract 的 ``SceneInitializer`` 实例）。非 ``None`` 时在
            Initialize + ``agentCount`` 校验 +（random 时 InitialRandomSpawn）
            之后、任何回合（含 init 帧捕获）之前调用 ``preinit(event,
            controller)`` 摆出任务初始布局；失败 fail-fast（布局错 = 结果
            无对齐意义，绝不静默跳过）。
        其余参数: 见 :func:`unity_launch_options`（``None`` 走环境变量/缺省）。

    线程约定：所有 ``step`` 调用必须来自 ``ControllerExecutor`` 的单线程池
    （ai2thor 的 FIFO 管道非线程安全；见 migration design §3.4）。
    """

    def __init__(
        self,
        *,
        scene: str = "FloorPlan1",
        num_agents: int = 2,
        controller_factory: Any = None,
        width: int | None = None,
        height: int | None = None,
        grid_size: float | None = None,
        visibility_distance: float | None = None,
        headless: bool | None = None,
        platform: str | None = None,
        x_display: str | None = None,
        gpu_device: int | None = None,
        spawn_mode: str = "default",
        spawn_seed: int | None = None,
        frame_store: Any = None,
        scene_initializer: Any = None,
    ) -> None:
        if num_agents < 1:
            raise ValueError(f"num_agents must be >= 1, got {num_agents}")
        # F-seed：spawn 参数先于 Unity 启动校验（配置错误时不起仿真进程）。
        self._spawn_mode, self._spawn_seed = normalize_spawn_options(
            spawn_mode, spawn_seed
        )

        self._scene = scene
        self._num_agents = num_agents
        self._stopped = False
        #: 最近一次各 agent 的归一化 metadata（PutObject 持物解析 / Fridge 判定用）。
        self._last_metadata: dict[int, dict[str, Any]] = {}
        #: 最近一次 action dict（诊断/测试读面）。
        self._last_action: dict[str, Any] | None = None
        #: F-frame 帧存储（``None`` = 捕获关闭，零副作用）与回合计数器。
        self._frame_store: Any = frame_store
        self._round_counter: int = 0

        options = unity_launch_options(
            scene=scene,
            num_agents=num_agents,
            width=width,
            height=height,
            grid_size=grid_size,
            visibility_distance=visibility_distance,
            headless=headless,
            platform=platform,
            x_display=x_display,
            gpu_device=gpu_device,
        )
        self._launch_options = options
        factory = controller_factory or _build_ai2thor_controller
        self._controller = factory(dict(options))
        self._verify_initial_agent_count()
        if self._spawn_mode == "random":
            # 设计 §3.2 接线点：agentCount 校验完成后、任何回合前，执行一次
            # InitialRandomSpawn；失败响亮抛（绝不静默跑默认布局冒充随机化）。
            spawn_event = apply_initial_random_spawn(
                self._controller, spawn_seed=self._spawn_seed
            )
            self._absorb_agent_metadata(spawn_event)
        if scene_initializer is not None:
            # D5 对齐接线：与原版 AI2Thor/env_new.py reset() 同序 —— Initialize +
            # agentCount 校验 +（random 时 InitialRandomSpawn）之后、任何回合
            # （含 F-frame init 帧捕获）之前，调用任务布局 preinit；失败响亮抛
            # （布局错 = 结果无对齐意义，绝不静默跳过）。
            self._apply_scene_preinit(scene_initializer)
        if frame_store is not None:
            # F-frame 捕获点①：初始化（含随机布局）后的每 agent init 帧。
            self._capture_init_frames()

    # ── 属性面（诊断 / 冒烟探针）─────────────────────────────────────────

    @property
    def controller(self) -> Any:
        """底层 ai2thor Controller（冒烟探针读 build/版本信息用）。"""
        return self._controller

    @property
    def launch_options(self) -> dict[str, Any]:
        """实际用于启动的参数字典（副本）。"""
        return dict(self._launch_options)

    @property
    def num_agents(self) -> int:
        return self._num_agents

    @property
    def last_metadata(self) -> dict[int, dict[str, Any]]:
        """各 agent 最近归一化 metadata（副本）。"""
        return {k: dict(v) for k, v in self._last_metadata.items()}

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def spawn_mode(self) -> str:
        """初始布局模式（F-seed：``default`` / ``random``）。"""
        return self._spawn_mode

    @property
    def spawn_seed(self) -> int | None:
        """布局随机化 seed（仅 ``spawn_mode="random"`` 时参与 InitialRandomSpawn）。"""
        return self._spawn_seed

    @property
    def frame_store(self) -> Any:
        """运行时关键帧存储（F-frame；``None`` = 捕获关闭、零副作用）。"""
        return self._frame_store

    # ── 动作映射（P5-4 契约面；单测直接对拍）────────────────────────────

    def build_action(
        self, action: str | dict[str, Any], agent_idx: int
    ) -> dict[str, Any]:
        """把编排层动作映射为 AI2Thor ``step()`` 参数字典。

        Args:
            action: 动作串（``"MoveAhead"`` / ``"LookUp(30)"`` /
                ``"PickupObject(Mug|-01.5|+00.9|+02.3)"``）或 executor 透传的
                动作 dict（``{"action": "MoveAhead"}``；额外键作为附加参数透传，
                本方法合成的键优先）。
            agent_idx: agent 序号（作为 ``agentId`` 注入）。

        Returns:
            ``{"action": ..., "agentId": ...}`` 及动作所需参数。

        Raises:
            ActionMappingError: 动作串无法解析 / 缺少必需参数 / ``degrees`` 非数值。
        """
        if not (0 <= agent_idx < self._num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self._num_agents})"
            )

        extras: dict[str, Any] = {}
        if isinstance(action, dict):
            raw = action.get("action")
            if not isinstance(raw, str):
                raise ActionMappingError(
                    f"动作 dict 缺少字符串 'action' 键: {action!r}"
                )
            extras = {k: v for k, v in action.items() if k not in ("action", "agentId")}
        elif isinstance(action, str):
            raw = action
        else:
            raise ActionMappingError(
                f"动作必须是 str 或 dict，得到 {type(action).__name__}"
            )

        name, inner = self._parse_action_string(raw)

        if name in _EMPTY_ACTION_ALIASES or name in _PASSTHROUGH_EMPTY_ACTIONS:
            # NoOp / Done 在仿真侧执行合法空动作 Pass（barrier 语义不依赖仿真动作名）。
            mapped: dict[str, Any] = {"action": "Pass"}
        elif name in _DIRECTION_ACTIONS:
            mapped = {"action": name}
        elif name == "GetReachablePositions":
            # navigate 的只读查询面（barrier.query_reachable_positions 经
            # executor 下发）：无参数元数据动作，结果在 event metadata 的
            # ``actionReturn``。
            if inner is not None:
                raise ActionMappingError(f"GetReachablePositions 不接受参数: {raw!r}")
            mapped = {"action": name}
        elif name == "Teleport":
            # navigate 的移动宏动作（F-nav）：position/rotation 经 dict 透传
            # （迁移前 base_env.agent_init_pos 的同一原生形状）；字符串形式
            # （如 ``Teleport(x=1)``）不接受——参数必须走 dict。
            if inner is not None:
                raise ActionMappingError(
                    f"Teleport 只接受 dict 形式（position/rotation 参数）: {raw!r}"
                )
            position = extras.get("position")
            rotation = extras.get("rotation")
            if not isinstance(position, dict) or not isinstance(rotation, dict):
                raise ActionMappingError(
                    "Teleport 需要 dict 形式的 position 与 rotation 参数"
                )
            mapped = {"action": name}
            # horizon 兜底注入（RP4 真机归因）：缺省时显式给出该 agent 当前
            # 相机 horizon 的夹取值——真 build 缺省取相机 euler.x 原样透传，
            # 越界残差会让 teleportFull 抛异常并连锁拒绝之后所有 Teleport
            # （见 :meth:`_teleport_horizon`）。调用方显式传 horizon 则不覆盖。
            if "horizon" not in extras:
                mapped["horizon"] = self._teleport_horizon(agent_idx)
        elif name in _OBJECT_ID_ACTIONS:
            if not inner:
                raise ActionMappingError(f"{name} 缺少 objectId 参数: {raw!r}")
            mapped = {"action": name, "objectId": inner}
        elif name == "PutObject":
            mapped = self._map_put_object(raw, inner, agent_idx)
        elif name in _ANGLE_PARAM_ACTIONS:
            mapped = {"action": name}
            if inner is not None:
                try:
                    mapped["degrees"] = int(float(inner))
                except (TypeError, ValueError) as exc:
                    raise ActionMappingError(f"{name} 的度数为非数值: {raw!r}") from exc
        else:
            raise ActionMappingError(
                f"未映射的编排层动作 {name!r}（动作串 {raw!r}）；"
                "worker 工具之外的动作不允许直接下发"
            )

        mapped["agentId"] = agent_idx
        if extras:
            merged = {**extras, **mapped}
            self._last_action = merged
            return merged
        self._last_action = mapped
        return mapped

    def _map_put_object(
        self, raw: str, inner: str | None, agent_idx: int
    ) -> dict[str, Any]:
        """``PutObject(<receptacleId>)`` → 官方 API 形式。

        worker 的 ``put`` 工具只提交**目标容器**的 raw id；该 build 的
        ``PutObject`` 语义 = ``objectId`` 传**目标容器**（把手上持有物放进
        它），**不存在** ``receptacleObjectId`` 参数（RP2 真机 6 次同签名拒绝：
        ``Action: "PutObject" called with invalid argument: 'receptacleObjectId'``）。
        持物仍从该 agent 最近 metadata 的 ``inventoryObjects`` 解析——空手时
        不允许猜测：直接软失败（见 :meth:`step_for_agent`）。

        目标为 Fridge 时追加 ``forceAction=True``（迁移前 ``base_env.parse_action``
        的既有约定，绕过 AI2Thor issue #1210 的冰箱放置限制）。
        """
        if not inner:
            raise ActionMappingError(f"PutObject 缺少 receptacle objectId: {raw!r}")
        if self._held_object_id(agent_idx) is None:
            raise _EmptyHandError(
                f"PutObject 要求该 agent 手上持有物体，但 agent {agent_idx} 的 "
                f"inventory 为空，无法放置到 {inner}"
            )
        mapped: dict[str, Any] = {"action": "PutObject", "objectId": inner}
        if self._is_fridge(inner):
            mapped["forceAction"] = True
        return mapped

    @staticmethod
    def _parse_action_string(action: str) -> tuple[str, str | None]:
        """解析 ``Name`` / ``Name(inner)``；非法格式抛 :class:`ActionMappingError`。"""
        match = _ACTION_RE.match(action)
        if match is None:
            raise ActionMappingError(f"无法解析动作串: {action!r}")
        name, inner = match.group(1), match.group(2)
        return name, (inner if inner else None)

    def _held_object_id(self, agent_idx: int) -> str | None:
        """该 agent 手上物体的 raw objectId（无则 ``None``）。"""
        metadata = self._last_metadata.get(agent_idx) or {}
        inventory = metadata.get("inventoryObjects")
        if isinstance(inventory, list) and inventory:
            first = inventory[0]
            if isinstance(first, dict):
                held = first.get("objectId")
                return str(held) if held else None
            return str(first)
        return None

    def _is_fridge(self, receptacle_id: str) -> bool:
        """目标容器是否冰箱（按最近 metadata 的 objectType，回退 id 子串）。"""
        for metadata in self._last_metadata.values():
            for obj in metadata.get("objects") or []:
                if not isinstance(obj, dict):
                    continue
                if obj.get("objectId") == receptacle_id:
                    return str(obj.get("objectType", "")).lower() == "fridge"
        return "fridge" in receptacle_id.lower()

    def _teleport_horizon(self, agent_idx: int) -> float:
        """``Teleport`` 缺省 horizon：该 agent 当前 cameraHorizon 夹取到合法区间。

        真机语义（ai2thor 5.0.0 ``PhysicsRemoteFPSAgentController.Teleport``）：
        动作不带 ``horizon`` 时，Unity 把 ``m_Camera.transform.localEulerAngles.x``
        原样交给 ``teleportFull`` —— 相机俯仰经 quaternion→euler 回读带浮点
        残差，盯在 ±界上（RP4 现场为 60.00002）会触发严格校验
        （``ArgumentOutOfRangeException: Each horizon must be in [-30:60]``）
        —— 且该 agent 之后**每一步** Teleport 都被同一异常拒绝
        （RP4 attempt1 实证：step50 起 navigate 18 连败，跨 agent 传染）。

        这里显式给出夹取后的合法值：保留当前俯仰（贴近真机缺省语义），同时
        保证绝不越界；顺带把已越界的残差值复位回合法区间（自愈）。metadata
        缺失 / 非数值 / NaN → 回退 0.0（水平视角，同初始化姿态）。
        """
        metadata = self._last_metadata.get(agent_idx) or {}
        agent_state = metadata.get("agent")
        horizon = (
            agent_state.get("cameraHorizon") if isinstance(agent_state, dict) else None
        )
        if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
            return 0.0
        value = float(horizon)
        if math.isnan(value):
            return 0.0
        return min(max(value, _TELEPORT_HORIZON_MIN), _TELEPORT_HORIZON_MAX)

    # ── 步进面（ControllerExecutor 消费）────────────────────────────────

    def step(
        self, action: str | dict[str, Any], *, agent_idx: int | None = None
    ) -> Any:
        """执行一个动作（``agent_idx`` 缺省为 0）——``step_for_agent`` 的简写面。"""
        return self.step_for_agent(
            agent_idx=0 if agent_idx is None else agent_idx, action=action
        )

    def step_for_agent(self, *, agent_idx: int, action: str | dict[str, Any]) -> Any:
        """执行某个 agent 的动作并返回**归一化**事件（``.metadata`` 面与 fake 一致）。

        异常语义见模块 docstring：映射/空手错误与 ai2thor 的调用级
        ``ValueError`` 都转成 ``lastActionSuccess=False`` 的软失败事件
        （LLM 在观测里看到失败）；基础设施异常（RuntimeError / TimeoutError /
        UnityCrash）向上抛。

        F-frame（仅 ``frame_store`` 注入时）：成功路径上按语义动作记关键帧
        （失败/软失败不记帧）；回合编号维护见模块 docstring 帧捕获节。
        """
        if self._stopped:
            raise RuntimeError("UnityController is stopped")

        capture_round = self._track_capture_round(agent_idx, action) if (
            self._frame_store is not None
        ) else None

        try:
            action_dict = self.build_action(action, agent_idx)
        except _EmptyHandError as exc:
            # 空手 PutObject：域级软失败（保留最近状态面，不前进仿真）。
            logger.info("PutObject 空手软失败: agent=%d action=%r", agent_idx, action)
            return self._failure_event(
                agent_idx=agent_idx,
                action_name="PutObject",
                message=str(exc),
                error_code="EmptyHand",
            )
        # 其余 ActionMappingError 直接上抛（编排层契约错误，响亮失败）。

        try:
            event = self._controller.step(action_dict)
        except ValueError as exc:
            # ai2thor 对 InvalidAction / InvalidArgument 等**调用级**错误抛
            # ValueError；转软失败，避免单个 agent 的非法交互拖垮整回合。
            logger.warning(
                "ai2thor 拒绝动作（软失败）: agent=%d action=%r error=%s",
                agent_idx,
                action_dict,
                exc,
            )
            return self._failure_event(
                agent_idx=agent_idx,
                action_name=str(action_dict.get("action", "")),
                message=str(exc),
                error_code="InvalidAction",
            )

        normalized = self._normalize_event(event, agent_idx)
        self._last_metadata[agent_idx] = dict(normalized.metadata)
        # 多 agent 事件里的同伴状态一并缓存（inventory/objects 用于后续解析）。
        agents = normalized.metadata.get("agents")
        if isinstance(agents, list):
            for idx, entry in enumerate(agents):
                if idx == agent_idx or not isinstance(entry, dict):
                    continue
                cached = dict(self._last_metadata.get(idx) or {})
                cached.setdefault("agents", agents)
                cached.setdefault("objects", normalized.metadata.get("objects"))
                cached.setdefault("name", entry.get("name"))
                cached.setdefault("position", entry.get("position"))
                cached.setdefault("rotation", entry.get("rotation"))
                inventory = entry.get("inventory")
                if isinstance(inventory, dict) and inventory.get("objects"):
                    cached.setdefault("inventoryObjects", inventory["objects"])
                self._last_metadata[idx] = cached
        self._capture_action_frame(action, normalized, event, agent_idx, capture_round)
        return normalized

    def reset(self, scene: str | None = None) -> Any:
        """重置到指定场景（``None`` 用当前场景），返回归一化事件。"""
        if self._stopped:
            raise RuntimeError("UnityController is stopped")
        target = scene or self._scene
        event = self._controller.reset(target)
        self._scene = target
        normalized = self._normalize_event(event, 0)
        for idx in range(self._num_agents):
            metadata = dict(normalized.metadata)
            agents = metadata.get("agents")
            if isinstance(agents, list) and idx < len(agents):
                entry = agents[idx]
                metadata.setdefault("name", entry.get("name"))
                metadata.setdefault("position", entry.get("position"))
                inventory = entry.get("inventory")
                if isinstance(inventory, dict):
                    metadata.setdefault("inventoryObjects", inventory.get("objects"))
            self._last_metadata[idx] = metadata
        return normalized

    def stop(self) -> None:
        """幂等停止：先做 run 终结帧捕获（F-frame 捕获点③），再关闭底层 Controller。

        收尾路径不允许抛：帧捕获与 Controller.stop 各自防抖（失败仅记录）。
        """
        if self._stopped:
            return
        self._stopped = True
        if self._frame_store is not None:
            try:
                self._capture_final_frames()
            except Exception:
                logger.exception("run 终结帧捕获失败（已忽略）")
        try:
            self._controller.stop()
        except Exception:
            # 收尾路径不允许再抛：记录后忽略（Unity 进程可能已退出）。
            logger.exception("底层 Controller.stop() 失败（已忽略）")

    # ── 内部：帧捕获（F-frame；仅 frame_store 注入时激活）───────────────

    def _track_capture_round(self, agent_idx: int, action: str | dict[str, Any]) -> int:
        """维护帧捕获回合编号（口径与 coordinator 环境视图的 step 同源）。

        回合边界：ControllerExecutor 每回合把 N 个动作按 agent 槽位 ``0..N-1``
        顺序逐个下发，故 agent 0 的动作到达 = 新回合开始（自增后本回合捕获携带
        新值）。navigate 的只读查询 ``GetReachablePositions`` 不占回合、不计入。
        无状态漂移依赖真实运行路径：仅 ControllerExecutor 驱动的流量产生正确
        编号（probe/手工直调不经此路径时编号可能不准——捕获为素材通道，不
        影响回合语义）。
        """
        raw_name = action.get("action") if isinstance(action, dict) else action
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if agent_idx == 0 and name != _READ_ONLY_QUERY_ACTION:
            self._round_counter += 1
        return self._round_counter

    def _capture_init_frames(self) -> None:
        """捕获点①：初始化（含随机布局）后每 agent 一张 ``init`` 帧（round 0）。

        初始 ``last_event`` 帧为 None（A100 实测）——每 agent 各执行一次
        ``Pass`` 强制渲染（合法空动作，不推进任何回合语义），再从该 agent 的
        事件取帧。取不到帧（渲染缺失）只记日志跳过（素材通道可缺、run 不可断）。
        """
        store = self._frame_store
        if store is None:
            return
        for idx in range(self._num_agents):
            frame = self._render_and_extract(idx)
            if frame is None:
                continue
            self._record_frame_safely(store, idx, 0, frame, "init")

    def _capture_action_frame(
        self,
        action: str | dict[str, Any],
        normalized: Any,
        raw_event: Any,
        agent_idx: int,
        capture_round: int | None,
    ) -> None:
        """捕获点②：语义关键动作**成功**后按 tag 记帧（失败/软失败不记）。"""
        store = self._frame_store
        if store is None or capture_round is None:
            return
        if not normalized.metadata.get("lastActionSuccess"):
            return
        tag = _frame_tag(action)
        if tag is None:
            return
        frame = self._extract_frame(raw_event, agent_idx)
        if frame is None:
            return
        self._record_frame_safely(store, agent_idx, capture_round, frame, tag)

    def _capture_final_frames(self) -> None:
        """捕获点③：run 终结每 agent ``final`` 帧 + 一张 ``overhead``。

        ``overhead`` 走 ``ToggleMapView``（第三方相机在 CloudRendering 下返回
        空帧，实测不可用）——拍完 **toggle 回来**（同原版
        ``_get_ceiling_image``，``AI2Thor/env_new.py:569-574``），且回切在
        取帧失败时也必须执行（绝不把地图视角留在 toggle 态）。
        """
        store = self._frame_store
        if store is None:
            return
        round_no = self._round_counter  # 已执行回合数（= 最后一步的 step 号）
        for idx in range(self._num_agents):
            frame = self._render_and_extract(idx)
            if frame is None:
                continue
            self._record_frame_safely(store, idx, round_no, frame, "final")
        # overhead：agent 0 的俯视全屋图（一张；多 agent 下与原版同口径）。
        try:
            event = self._controller.step({"action": "ToggleMapView", "agentId": 0})
        except Exception:
            logger.exception("overhead 帧渲染（ToggleMapView）失败（跳过，不回切）")
            return
        try:
            frame = self._extract_frame(event, 0)
            if frame is not None:
                self._record_frame_safely(store, 0, round_no, frame, "overhead")
        finally:
            try:
                self._controller.step({"action": "ToggleMapView", "agentId": 0})
            except Exception:
                logger.exception("ToggleMapView 回切失败（已忽略）")

    def _render_and_extract(self, agent_idx: int) -> Any | None:
        """对某 agent 执行一次 ``Pass`` 强制渲染并取帧（取不到 → None，响亮记日志）。"""
        try:
            event = self._controller.step({"action": "Pass", "agentId": agent_idx})
        except Exception:
            logger.exception("帧渲染（Pass）失败：agent=%d（跳过该帧）", agent_idx)
            return None
        frame = self._extract_frame(event, agent_idx)
        if frame is None:
            logger.warning(
                "帧缺失（agent=%d）：渲染事件里无 frame（headless 下 ai2thor 会"
                "强制 renderImage=False——检查 FRAMES 开关与 headless 配置）",
                agent_idx,
            )
        return frame

    @staticmethod
    def _extract_frame(event: Any, agent_idx: int) -> Any | None:
        """从原始事件取该 agent 的帧（多 agent → ``events[agent_idx].frame``）。

        ``MultiAgentEvent`` 顶层 ``frame`` 为 None（实测）——多 agent 时只认
        子事件；单 ``Event`` 直接读自身。结构不符 / 帧为 None → ``None``
        （绝不合成假帧）。
        """
        events = getattr(event, "events", None)
        target = event
        if isinstance(events, (list, tuple)) and 0 <= agent_idx < len(events):
            target = events[agent_idx]
        return getattr(target, "frame", None)

    @staticmethod
    def _record_frame_safely(
        store: Any, agent_idx: int, round_no: int, frame: Any, tag: str
    ) -> None:
        """写帧（捕获为素材通道：store 异常只记录，绝不打断回合/收尾）。"""
        try:
            store.record(
                agent_idx=agent_idx, round_no=round_no, frame_rgb=frame, tag=tag
            )
        except Exception:
            logger.exception(
                "帧记录失败（agent=%d round=%d tag=%s；已忽略）",
                agent_idx,
                round_no,
                tag,
            )

    # ── 内部：启动校验 / 事件归一化 / 软失败 ────────────────────────────

    def _verify_initial_agent_count(self) -> None:
        """初始化后校验事件中的 agent 数（agentCount 未生效时 fail-fast）。"""
        event = getattr(self._controller, "last_event", None)
        if event is None:
            return  # 注入式假 controller 可能不提供 last_event
        events = getattr(event, "events", None)
        observed = len(events) if events else 1
        if observed != self._num_agents:
            raise RuntimeError(
                f"AI2Thor 初始化返回 {observed} 个 agent 事件，而编排层要求 "
                f"{self._num_agents} 个（agentCount 未生效或与 barrier 不一致）；"
                "请确认 Unity build 支持 agentCount 且 num_agents 对齐"
            )
        self._absorb_agent_metadata(event)

    def _absorb_agent_metadata(self, event: Any) -> None:
        """把事件里的各 agent metadata 写入 ``_last_metadata`` 缓存。

        初始校验与随机布局（``InitialRandomSpawn``）后各调用一次——spawn 会
        改变 agent 位姿/物体位置，缓存必须随新事件刷新（否则后续
        ``_teleport_horizon`` / 持物解析读到布局前的旧状态）。
        """
        events = getattr(event, "events", None)
        for idx, sibling in enumerate(events if events else [event]):
            self._last_metadata[idx] = dict(self._event_metadata(sibling) or {})

    def _apply_scene_preinit(self, scene_initializer: Any) -> None:
        """调用任务布局 ``preinit``（D5；双签名兼容 / fail-fast 见
        ``ai2thor_orch.contracts.task.invoke_scene_preinit``）。

        与原版 ``AI2Thor/env_new.py`` reset() 同一调用面：preinit 直接作用
        **raw controller**（``controller.step(action=..., **params)`` 摆物体 /
        开关容器）。返回值（约定为最新 event）用于刷新 ``_last_metadata``——
        preinit 改变了物体布局，初始化期的旧 metadata 必须作废。
        """
        from ai2thor_orch.contracts.task import invoke_scene_preinit

        event = invoke_scene_preinit(
            scene_initializer,
            getattr(self._controller, "last_event", None),
            self._controller,
        )
        if event is not None and getattr(event, "metadata", None):
            self._absorb_agent_metadata(event)

    @staticmethod
    def _event_metadata(event: Any) -> dict[str, Any] | None:
        metadata = getattr(event, "metadata", None)
        return metadata if isinstance(metadata, dict) else None

    def _normalize_event(self, event: Any, agent_idx: int) -> _NormalizedEvent:
        """``MultiAgentEvent`` / ``Event`` → 每 agent 一份补全后的 metadata dict。"""
        events = getattr(event, "events", None)
        siblings: list[Any] = list(events) if events else [event]
        per_agent = (
            self._event_metadata(siblings[agent_idx])
            if agent_idx < len(siblings)
            else None
        ) or {}
        metadata: dict[str, Any] = dict(per_agent)

        # ``objects``：容器 metadata 缺失时按 顶层 → 兄弟事件 回退（5.0 各 build
        # 的 MultiAgentEvent 结构略有差异，保证 barrier/verifier 的消费面稳定）。
        if not metadata.get("objects"):
            for source in [event, *siblings]:
                candidate = (self._event_metadata(source) or {}).get("objects")
                if candidate:
                    metadata["objects"] = candidate
                    break

        # ``agents``：barrier 按 agent 槽位索引 position/rotation/inventory；
        # 真实 metadata 只有 ``agent``/``inventoryObjects``，这里合成为列表。
        if not isinstance(metadata.get("agents"), list):
            metadata["agents"] = [
                self._agent_view(sibling, idx) for idx, sibling in enumerate(siblings)
            ]

        return _NormalizedEvent(metadata)

    @classmethod
    def _agent_view(cls, event: Any, idx: int) -> dict[str, Any]:
        """单个 agent 的槽位视图（barrier ``agents[i]`` 消费面）。"""
        metadata = cls._event_metadata(event) or {}
        agent_state = metadata.get("agent")
        agent_state = agent_state if isinstance(agent_state, dict) else {}
        inventory = metadata.get("inventoryObjects")
        return {
            "agentId": metadata.get("agentId", idx),
            "name": f"Agent{idx}",
            "position": agent_state.get("position"),
            "rotation": agent_state.get("rotation"),
            "cameraHorizon": agent_state.get("cameraHorizon"),
            "isStanding": agent_state.get("isStanding"),
            "inventory": {
                "objects": list(inventory) if isinstance(inventory, list) else []
            },
        }

    def _failure_event(
        self,
        *,
        agent_idx: int,
        action_name: str,
        message: str,
        error_code: str,
    ) -> _NormalizedEvent:
        """合成软失败事件（保留最近状态面，lastActionSuccess=False）。"""
        metadata = dict(self._last_metadata.get(agent_idx) or {})
        metadata["lastAction"] = action_name
        metadata["lastActionSuccess"] = False
        metadata["errorMessage"] = message
        metadata["errorCode"] = error_code
        metadata.pop("actionReturn", None)
        if "agents" not in metadata:
            metadata["agents"] = [
                self._agent_view(None, idx) for idx in range(self._num_agents)
            ]
        return _NormalizedEvent(metadata)


class _EmptyHandError(ActionMappingError):
    """空手 ``PutObject``：映射层可识别的域错误（软失败而非抛出场）。"""
