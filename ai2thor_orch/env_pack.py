"""AI2Thor 环境包 —— EnvPack 契约的 AI2Thor 实现（env-contract P5-2）。

契约面（``src/orchestration/env_pack.py``，P4-2/3/4 定型）逐类落地：

1. **barrier 工厂**：``AI2ThorBarrier`` + ``ControllerExecutor``（fake/unity
   分支在 :func:`create_controller`）；返回的 barrier 同时是 run control
   （``request_stop`` / ``stop`` / ``get_run_status``，G8 消费点
   ``set_run_control``）。
2. **worker 工具注册表**：``AI2THOR_WORKER_TOOLS``（7 件）逐类注入
   ``barrier`` / ``agent_idx`` / ``alias_registry``（与 barrier 共用同一
   ``AliasRegistry`` 实例）。
3. **coordinator 工具工厂**：内核注入口 ``finish_task_tool_factory`` 直通；
   ``finish_task`` 的完成判定走**现有 verifier 语义**——
   ``ai2thor_orch.verifier.verify_round`` 对 barrier 最近回合做
   postcondition 真值判定；环境真值不可达（未过 barrier 工厂）时回退内核
   注入的 ``completion_validator``。``build_coordinator_tools`` 返回 ``[]``
   （AI2Thor 无 oracle 模式；运行态经 state provider 自动注入）。
4. **state provider 工厂**：``AI2ThorCoordinatorStateProvider`` /
   ``AI2ThorWorkerStateProvider``（构造时捕获 ctx，供第 5 类工厂回读）。
5. **Context·session 工厂**：``AI2Thor{Coordinator,Worker}ContextManager``。
   注入式 session 工厂替换内核缺省 factory（`build_default_session_factory`），
   因此必须自行携带内核 ContextConfig 的同参构造（strategy/recent_messages/
   pinned_enabled 为骨架常量；state_mode / memory_read_mode / prune_policy
   取自 ctx——``prune_policy`` 为 P5-2 补齐的 CoordinatorEnv 契约字段）。
6. **prompts 目录**：树内布局 ``ai2thor_orch/prompts/{coordinator,worker}``；
   skills 目录不存在 → ``None``（内核不注入）。
7. **观察与域数据钩子**：AI2Thor 暂无观测摄取源 / 域摘要器 → ``None``。
8. **日志与产物**：无控制台 UI → ``ui_dir=None``。
9. **附属 LLM**：无 → 缺省 no-op。

依赖方向（硬不变量）：本模块只依赖内核（``src/Agent`` + ``src/a2a``）、
``orchestration.env_pack`` 契约与 ``ai2thor_orch`` 内模块——绝不 import
``sar_orch``（守卫：``ai2thor_orch/tests/test_env_pack.py`` 的 import 探针）。

fake/unity：``fake`` 用 ``ai2thor_orch.tests.fakes.FakeController``（确定性、
无 Unity / 无 GPU 依赖，与迁移前 ``ai2thor_experiment`` 一致）；``unity`` 走
``ai2thor_orch.executor.unity_controller.UnityController``（P5-4：真实
``ai2thor.controller.Controller`` + ``agentCount`` 多 agent 初始化 + 动作映射
+ 事件归一化；启动参数经 ``LLAMAR_AI2THOR_*`` 环境变量配置）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestration.env_pack import EnvPack

logger = logging.getLogger(__name__)

#: worker AgentCard 能力标签（A2A 注册面；SAR 侧同位置常量对齐）。
_AI2THOR_WORKER_CAPABILITIES = ["ai2thor", "navigation", "manipulation"]

#: 工具名 → AI2Thor 动作名别名映射（``agent_interactions.csv`` 的 Action
#: 标签格式化；方向型工具的名字+方向在 :meth:`Ai2ThorEnvPack.format_worker_action`
#: 内合成，与其 ``execute()`` 实际提交的 action 字符串逐字一致）。
_WORKER_ACTION_ALIASES = {
    "move": "Move",
    "rotate": "Rotate",
    "look": "Look",
    "pickup": "PickupObject",
    "put": "PutObject",
    "open_close": "OpenClose",
    "done": "Done",
}

#: 方向型动作映射（与 ``ai2thor_orch/tools/worker/{move,rotate,look}.py`` 的
#: ``_DIRECTION_MAP`` 同构；一致性由测试 ``test_format_matches_submitted_actions``
#: 对工具实际提交 action 逐条断言）。
_MOVE_DIRECTIONS = {
    "ahead": "MoveAhead",
    "back": "MoveBack",
    "left": "MoveLeft",
    "right": "MoveRight",
}
_ROTATE_DIRECTIONS = {"left": "RotateLeft", "right": "RotateRight"}
_LOOK_DIRECTIONS = {"up": "LookUp(30)", "down": "LookDown(30)"}

#: 方向型工具 → 方向映射（format_worker_action 的分派表）。
_DIRECTION_MAPS = {
    "move": _MOVE_DIRECTIONS,
    "rotate": _ROTATE_DIRECTIONS,
    "look": _LOOK_DIRECTIONS,
}

#: 骨架 ContextConfig 的常量面（与 orchestration.coordinator / worker 内联构造
#: 逐字段一致；state_mode / memory_read_mode / prune_policy 为运行期变量）。
_CONTEXT_STRATEGY = "hybrid"
_CONTEXT_RECENT_MESSAGES = 12
_CONTEXT_PINNED_ENABLED = True
#: 内核两侧骨架的 token_limit（``create_server`` / ``create_worker_a2a_server``）。
_CONTEXT_TOKEN_LIMIT = 80000


def create_controller(*, mode: str, scene: str, num_agents: int) -> Any:
    """按模式构造底层 controller（fake/unity 分支；P5-4 unity 接线点）。

    - ``fake``：确定性 :class:`~ai2thor_orch.tests.fakes.FakeController`，
      不依赖 ``ai2thor`` 包真运行（CI / 本机无 GPU 可用）；``scene`` 透传面
      保持与迁移前一致（fake controller 不消费 scene）。
    - ``unity``：真实 ``ai2thor.controller.Controller`` 适配器
      （:class:`~ai2thor_orch.executor.unity_controller.UnityController`）：
      floorplan 启动 + ``agentCount=num_agents`` 多 agent 初始化 + 动作映射 +
      事件归一化。启动参数（headless / platform / X display / GPU）经
      ``LLAMAR_AI2THOR_*`` 环境变量配置（见 A100 运行手册）。

    ``num_agents`` 必填：controller 的 ``agentCount`` 必须与 barrier 的
    ``num_agents`` 同口径，缺省猜测会让多 agent 初始化静默错位。
    """
    if mode == "fake":
        from ai2thor_orch.tests.fakes import FakeController

        return FakeController()
    if mode == "unity":
        from ai2thor_orch.executor.unity_controller import UnityController

        return UnityController(scene=scene, num_agents=num_agents)
    raise ValueError(f"Unknown mode: {mode!r}. Use 'fake' or 'unity'.")


def _build_context_config(
    *, role: str, state_mode: str, memory_read_mode: str, prune_policy: str
):
    """重建内核骨架同参的 ``ContextConfig``（session 工厂替换内核缺省 factory）。

    coordinator / worker 两侧内核各自持有独立的 ``ContextConfig`` 类
    （``Agent.router_agent.context`` / ``Agent.worker_agent.context``，字段同构）
    ——按 role 取对应类，避免跨模块类混用。
    """
    if role == "worker":
        from Agent.worker_agent.context import ContextConfig
    else:
        from Agent.router_agent.context import ContextConfig

    return ContextConfig(
        strategy=_CONTEXT_STRATEGY,
        recent_messages=_CONTEXT_RECENT_MESSAGES,
        pinned_enabled=_CONTEXT_PINNED_ENABLED,
        state_mode=state_mode,
        memory_read_mode=memory_read_mode,
        prune_policy=prune_policy,
    )


class Ai2ThorEnvPack(EnvPack):
    """AI2Thor 环境包（契约面见模块 docstring）。

    构造参数：
        task_id / scene：任务契约加载输入（``AI2Thor/Tasks/<task_id>``；当前
            verifier 首版只支持 ``3_transport_groceries``，不支持时立刻
            抛 ``NotImplementedError``——fail-fast）。
        mode / step_timeout：build_barrier 的缺省运行参数（env_params 可覆盖；
            ``mode`` ∈ ``{fake, unity}``）。
        coordinator_prompts_dir / worker_prompts_dir：prompts 根目录覆盖
            （``None`` = 树内布局 ``ai2thor_orch/prompts/{coordinator,worker}``）。
    """

    name = "ai2thor"

    def __init__(
        self,
        *,
        task_id: str = "3_transport_groceries",
        scene: str = "FloorPlan1",
        mode: str = "fake",
        step_timeout: float = 60.0,
        coordinator_prompts_dir: str | None = None,
        worker_prompts_dir: str | None = None,
    ) -> None:
        from ai2thor_orch.contracts.task import load_task

        self._task_id = task_id
        self._scene = scene
        self._mode = mode
        self._step_timeout = step_timeout
        #: 任务契约（finish_task 完成判定的 verifier 输入；fail-fast 加载）。
        self._contract = load_task(task_id, scene)
        _pack_dir = Path(__file__).resolve().parent
        self._coordinator_prompts_dir = coordinator_prompts_dir or str(
            _pack_dir / "prompts" / "coordinator"
        )
        self._worker_prompts_dir = worker_prompts_dir or str(
            _pack_dir / "prompts" / "worker"
        )
        #: barrier 工厂产物（同 run 内回读；finish_task 真值判定与 alias 共用）。
        self._barrier: Any = None
        #: 第 4 类工厂产物与 ctx（第 5 类 session 工厂按契约顺序回读——内核
        #: 装配窗口：观察源 → 摘要器 → state provider → 工具 → session 工厂）。
        self._coordinator_ctx: Any = None
        self._worker_ctx: Any = None
        self._coordinator_state_provider: Any = None
        self._worker_state_provider: Any = None

    # ── 1. barrier 工厂（装配层 P4-4 消费）──────────────────────────────

    def build_barrier(self, *, num_agents: int, seed: int, **env_params: Any) -> Any:
        """构造 ``AI2ThorBarrier``（controller → ``ControllerExecutor`` → barrier）。

        ``env_params``（装配层经 ``AssemblySpec.env_params`` 透传）：
            ``scene`` / ``mode`` / ``step_timeout`` 覆盖构造缺省；
            ``max_steps`` **必填**——barrier 的步数预算必须与 run 的
            ``max_steps`` 同口径，缺省猜测会静默跑偏，故 fail-fast。

        ``seed`` 按契约签名接收；AI2Thor fake controller 为确定性实现，
        seed 供装配层写入 run metadata（barrier 无随机面）。
        """
        from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
        from ai2thor_orch.executor.controller_executor import ControllerExecutor
        from ai2thor_orch.visibility import AliasRegistry

        mode = env_params.pop("mode", self._mode)
        scene = env_params.pop("scene", self._scene)
        step_timeout = env_params.pop("step_timeout", self._step_timeout)
        if "max_steps" not in env_params:
            raise TypeError(
                "Ai2ThorEnvPack.build_barrier requires max_steps=<int> "
                "(the run's step budget must reach the barrier unchanged)"
            )
        max_steps = env_params.pop("max_steps")
        if env_params:
            raise TypeError(
                f"Ai2ThorEnvPack.build_barrier got unexpected env_params: "
                f"{sorted(env_params)}"
            )

        controller = create_controller(mode=mode, scene=scene, num_agents=num_agents)
        executor = ControllerExecutor(controller)
        barrier = AI2ThorBarrier(
            num_agents=num_agents,
            executor=executor,
            max_steps=max_steps,
            step_timeout=step_timeout,
            # 单一共享实例：worker 工具（build_worker_tools）与 barrier 的
            # 观测脱敏读同一个注册表。
            alias_registry=AliasRegistry(),
            # P5-3：任务契约驱动 barrier 的逐回合验证（finished 成功真值）
            # 与任务指标（get_metrics / step log 的 coverage / transport_rate）。
            contract=self._contract,
        )
        self._barrier = barrier
        logger.info(
            "AI2Thor barrier initialized (mode=%s scene=%s agents=%d max_steps=%d)",
            mode,
            scene,
            num_agents,
            max_steps,
        )
        return barrier

    # ── 2. worker 工具注册表 + 运行期附属面（P4-3 消费）─────────────────

    async def build_worker_tools(self, ctx) -> list[Any]:
        """构造 worker 完整工具列表（``AI2THOR_WORKER_TOOLS`` 逐类装配）。

        7 件工具统一注入 ``barrier`` / ``agent_idx`` / ``alias_registry``
        （注册表实例取自 ``ctx.barrier.alias_registry``——与 barrier 观测
        脱敏共用同一实例）。AI2Thor 无 Map Agent MCP / peer-mail 附属面，
        因此无 MCP 装载与条件剪枝。
        """
        from ai2thor_orch.tools.worker import AI2THOR_WORKER_TOOLS

        registry = ctx.barrier.alias_registry
        return [
            tool_cls(
                barrier=ctx.barrier,
                agent_idx=ctx.agent_idx,
                alias_registry=registry,
            )
            for tool_cls in AI2THOR_WORKER_TOOLS
        ]

    @property
    def worker_capabilities(self) -> list[str]:
        """worker AgentCard 能力标签（A2A 注册面）。"""
        return list(_AI2THOR_WORKER_CAPABILITIES)

    def format_worker_action(self, tool_name: str, args: dict) -> str:
        """``Action`` 标签（``agent_interactions.csv``）：工具实提交的 AI2Thor 动作串。

        与各工具 ``execute()`` 提交的动作逐字一致（一致性由测试对
        fake barrier 捕获的 submit 参数断言）：方向型工具按方向合成
        （``move(ahead)`` → ``MoveAhead``），alias 型工具保留可见 alias
        （``pickup(object_alias=Mug_1)`` → ``PickupObject(Mug_1)``，绝不
        暴露 raw objectId；无参动作不带括号：``done`` → ``Done``）。未映射
        工具名回退基类格式 ``name(args...)``。
        """
        action_name = _WORKER_ACTION_ALIASES.get(tool_name)
        if action_name is None:
            return super().format_worker_action(tool_name, args)
        direction_map = _DIRECTION_MAPS.get(tool_name)
        if direction_map is not None:
            direction = str(args.get("direction", ""))
            return direction_map.get(direction, f"{action_name}({direction})")
        if tool_name == "open_close":
            verb = "OpenObject" if args.get("action") == "open" else "CloseObject"
            return f"{verb}({args.get('object_alias')})"
        if args:
            arg_parts = ", ".join(str(v) for v in args.values())
            return f"{action_name}({arg_parts})"
        # 无参动作（done → "Done"）不带括号，与实提交动作串一致。
        return action_name

    # ── 3. coordinator 工具工厂 + 内核注入口直通（P4-2 消费）─────────────

    def build_coordinator_tools(self, ctx) -> list:
        """AI2Thor 无 oracle 模式 / 无运行期 coordinator 工具 → 空列表。

        （运行态经 state provider 每回合自动注入；mission 完成工具经第 3 类
        内核注入口 ``finish_task_tool_factory`` 单独供给。）
        """
        return []

    def finish_task_tool_factory(self, store=None, *, completion_validator=None):
        """内核 executor 注入口：AI2Thor ``finish_task`` 工具工厂。

        完成判定（success=True 时）走**现有 verifier 语义**：
        ``ai2thor_orch.verifier.verify_round`` 对 barrier 最近回合
        （``barrier.last_round_result()``）做 postcondition 校验；环境真值
        不可达（本工厂在 ``build_barrier`` 之前被调用）时回退内核注入的
        ``completion_validator``（barrier.is_finished 口径）。两者都没有时
        不设校验（SAR 工具同构语义）。

        ``store`` 透传：若提供 ``mark_finished`` 则记账 mission 终态
        （既有任务存储 ledger 面）。
        """
        from ai2thor_orch.tools.coordinator.finish_task import FinishTaskTool

        if self._barrier is not None and self._contract is not None:
            validator = self._verified_completion_validator()
        else:
            validator = completion_validator
        return FinishTaskTool(store, completion_validator=validator)

    def _verified_completion_validator(self):
        """构造环境侧真值判定闭包（现有 verifier：见模块 docstring 第 3 类）。"""
        barrier, contract = self._barrier, self._contract

        def _validate() -> bool:
            from ai2thor_orch.verifier.verifier import verify_round

            verdict = verify_round(barrier.last_round_result(), contract)
            return bool(verdict["verified_completion"])

        return _validate

    # ── 4. state provider 工厂（P4-2 / P4-3 消费）──────────────────────

    def build_coordinator_state_provider(self, ctx) -> Any:
        """构造 ``AI2ThorCoordinatorStateProvider``（并捕获 ctx 供 session 工厂回读）。"""
        from ai2thor_orch.state.coordinator_state_provider import (
            AI2ThorCoordinatorStateProvider,
        )

        provider = AI2ThorCoordinatorStateProvider(barrier=ctx.barrier)
        self._coordinator_ctx = ctx
        self._coordinator_state_provider = provider
        return provider

    def build_worker_state_provider(self, ctx) -> Any:
        """构造 ``AI2ThorWorkerStateProvider``（并捕获 ctx 供 session 工厂回读）。"""
        from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider

        provider = AI2ThorWorkerStateProvider(
            barrier=ctx.barrier, agent_idx=ctx.agent_idx
        )
        self._worker_ctx = ctx
        self._worker_state_provider = provider
        return provider

    # ── 5. Context·session 工厂（P4-2 / P4-3 消费）─────────────────────

    def build_session_factory(self, *, role: str):
        """返回 AI2Thor Context 子类的零参 session 工厂（内核 per-context 调用）。

        契约顺序约束：本工厂在骨架装配窗口内位于 state provider 工厂**之后**
        （观察源 → 摘要器 → state provider → 工具 → session 工厂），被构造的
        ContextManager 必须携带此前构建的 state provider（运行态注入的载体）。
        因此先经过对应角色的 state provider 工厂是前置条件；缺失时 fail-fast
        （静默产出无状态注入的 Context 是隐性降级）。
        """
        from ai2thor_orch.state.context import (
            AI2ThorCoordinatorContextManager,
            AI2ThorWorkerContextManager,
        )

        if role == "coordinator":
            ctx = self._coordinator_ctx
            if ctx is None or self._coordinator_state_provider is None:
                raise RuntimeError(
                    "Ai2ThorEnvPack.build_session_factory(role='coordinator') 必须在 "
                    "build_coordinator_state_provider(ctx) 之后调用"
                    "（契约装配窗口顺序：state provider → session 工厂）"
                )
            provider = self._coordinator_state_provider

            def _coordinator_session():
                return AI2ThorCoordinatorContextManager(
                    _build_context_config(
                        role="coordinator",
                        state_mode=ctx.state_mode,
                        memory_read_mode=ctx.memory_read_mode,
                        prune_policy=ctx.prune_policy,
                    ),
                    _CONTEXT_TOKEN_LIMIT,
                    ctx.log_dir,
                    state_provider=provider,
                    skills_dir=self.coordinator_skills_dir,
                )

            return _coordinator_session

        if role == "worker":
            ctx = self._worker_ctx
            if ctx is None or self._worker_state_provider is None:
                raise RuntimeError(
                    "Ai2ThorEnvPack.build_session_factory(role='worker') 必须在 "
                    "build_worker_state_provider(ctx) 之后调用"
                    "（契约装配窗口顺序：state provider → session 工厂）"
                )
            provider = self._worker_state_provider

            def _worker_session():
                return AI2ThorWorkerContextManager(
                    _build_context_config(
                        role="worker",
                        # 内核 worker 骨架的 state_mode 为常量 semantic。
                        state_mode="semantic",
                        memory_read_mode=ctx.memory_read_mode,
                        prune_policy=ctx.prune_policy,
                    ),
                    _CONTEXT_TOKEN_LIMIT,
                    ctx.log_dir,
                    state_provider=provider,
                    skills_dir=self.worker_skills_dir,
                )

            return _worker_session

        raise ValueError(f"Unknown role: {role!r}. Use 'coordinator' or 'worker'.")

    # ── 6. prompts 目录（装配层消费）───────────────────────────────────

    @property
    def coordinator_prompts_dir(self) -> str | None:
        return self._coordinator_prompts_dir

    @property
    def coordinator_skills_dir(self) -> str | None:
        # AI2Thor 无树内 skills 目录（SAR 的 <prompts>/../../skills 约定不适用）。
        return None

    @property
    def worker_prompts_dir(self) -> str | None:
        return self._worker_prompts_dir

    @property
    def worker_skills_dir(self) -> str | None:
        return None

    # ── 7. 观察与域数据钩子（P4-2 消费）────────────────────────────────

    def build_observation_source(self, ctx) -> Any | None:
        """AI2Thor 暂无观测摄取源（不接 set_semantic_map）→ ``None``。"""
        return None

    def build_domain_summarizer(self, ctx) -> Any | None:
        """AI2Thor 暂无域摘要器 → ``None``。"""
        return None

    # ── 8. 日志与产物配置 ──────────────────────────────────────────────

    @property
    def ui_dir(self) -> str | None:
        """AI2Thor 无控制台 UI → ``None``（内核不挂 UI 路由）。"""
        return None


__all__ = ["Ai2ThorEnvPack", "create_controller"]
